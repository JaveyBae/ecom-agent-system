"""售后 agent 的工具：JSON Schema 给模型看，执行时绑定到当前会话（顾客、时间、数据库、知识库）。

顾客已经登录，工具只能看到和操作这位顾客自己的订单，别人的订单一律当作不存在。办理类工具依次过这几关：

1. 参数检查：按 schema 查字段、类型、枚举和嵌套参数，不对的退回给模型改，不中断对话；
2. 身份隔离：只能操作本人的订单和售后单；
3. 政策判断：不允许的把原因和规则编号还给模型，不打扰顾客；
4. 凭证：质量问题等要有具体的问题描述（P-QA-05）；
5. 办理权限（P-SVC-04）：不退货的钱，超过直接办理额度的先人工审核，超过转人工线的不能办；
6. 确认卡片：confirm 回调请顾客确认，相当于 App 里弹出的卡片；
7. 执行：在一个事务里写库。同一次对话里重复的同一个请求，直接返回第一次的结果。

每次办理和每次被拦下的调用都写进审计日志（audit_log）。

办理权限那一关还看风控标识（P-RISK-01）：售后频繁的账户，不退货的退款不论金额都先人工审核。标识由规则从
售后记录算出，放在系统提示里给模型看，不向顾客展示。

做对照实验时可以关掉其中几道关（gates）：身份隔离、凭证、办理权限、风控、确认卡片、重复请求去重；参数检查、
商城自己的业务规则和审计一直都在。knowledge_base 传 None 时没有 search_policy 这个工具，用来对照检索的作用。

子 agent（agents，见 agents.py）也从这里接：法规顾问接成工具 consult_legal，顶替 search_policy；凭证审核员接在
凭证那一关后面，驳回的申请按“凭证不足”拦下。子 agent 跑过的步骤先放在 delegations 里，对话循环取走并记进轨迹。
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone

from ecom_agent_system.agent.memory import add_note, risk_profile
from ecom_agent_system.shop.catalog import variant_price
from ecom_agent_system.shop.db import fmt
from ecom_agent_system.shop.policy import EVIDENCE_REASONS
from ecom_agent_system.shop.service import TICKET_TYPES, ShopError

# 检索范围到知识库层级的映射
SCOPES = {
    "平台规则": ["平台规则"],
    "法律法规": ["法律", "行政法规", "部门规章", "司法解释", "规范性文件"],
    "全部": None,
}
SNIPPET_CHARS = 400


def _schema(name, description, properties=None, required=()):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": list(required),
                "additionalProperties": False,
            },
        },
    }


ORDER_ID = {"order_id": {"type": "string", "description": "订单号，如 SO261024003001"}}
TICKET_ID = {"ticket_id": {"type": "string", "description": "售后单号，如 AS00000695"}}
REQUEST = {
    **ORDER_ID,
    "type": {"type": "string", "enum": list(TICKET_TYPES)},
    "reason": {"type": "string", "enum": list(dict.fromkeys(reason for reasons in TICKET_TYPES.values() for reason in reasons))},
    "opened": {"type": "boolean", "description": "商品已经拆封"},
    "activated": {"type": "boolean", "description": "电子产品已经激活"},
}
EVIDENCE = {"evidence": {"type": "string", "description": "质量问题、运输损毁、生鲜变质时，顾客说的具体问题，照抄顾客的话，不要替顾客编"}}
TOOL_SCHEMAS = [
    _schema("list_orders", "列出顾客的订单：订单号、下单时间、商品、金额和当前状态。"),
    _schema("get_order", "查看一个订单的详情：商品和规格、金额、收货地址、物流概况、已有的售后单。", ORDER_ID, ["order_id"]),
    _schema("track_shipment", "查看订单的物流轨迹和异常提示，包括售后产生的退货、换货寄送。", ORDER_ID, ["order_id"]),
    _schema(
        "check_after_sales",
        "判断一个订单能不能办理某种售后：截止时间、退款金额、运费谁出、依据的规则编号。只判断，不办理。"
        "拦截退款用于已发货、未签收的包裹，原因分顾客原因和商家原因（延迟发货）。",
        REQUEST, ["order_id", "type", "reason"],
    ),
    _schema(
        "create_after_sales",
        "为顾客提交售后申请。提交前会向顾客展示确认卡片，顾客确认后才提交。",
        {**REQUEST, **EVIDENCE}, ["order_id", "type", "reason"],
    ),
    _schema("get_after_sales", "查看售后单的进度、退款和下一步。", TICKET_ID, ["ticket_id"]),
    _schema("cancel_after_sales", "撤销还没完成的售后申请。会先请顾客确认。", TICKET_ID, ["ticket_id"]),
    _schema("cancel_order", "取消还没发货的订单。会先请顾客确认。", ORDER_ID, ["order_id"]),
    _schema(
        "change_address",
        "商家发货前修改收货地址里的详细地址（同一区县内）。会先请顾客确认。",
        {**ORDER_ID, "address": {"type": "string", "description": "新的详细地址，例如“文三路 88 号 3 幢 501 室”"}},
        ["order_id", "address"],
    ),
    _schema("urge_order", "催发货或催派送：通知商家或物流公司，答复会记在工单里。24 小时内重复催不会重复通知。", ORDER_ID, ["order_id"]),
    _schema(
        "request_platform_intervention",
        "商家拒绝了售后申请、顾客有异议时，申请平台介入。会先请顾客确认。",
        {**TICKET_ID, "statement": {"type": "string", "description": "顾客对商家拒绝的异议"}},
        ["ticket_id", "statement"],
    ),
    _schema("list_work_orders", "查看顾客的工单：转人工、催发货、催派送的进度和答复。"),
    _schema(
        "remember_note",
        "记下顾客交代的、关于顾客自己的事（收货习惯、退款方式、联系方式变了），下次顾客来时放进档案。"
        "只记顾客自己说的话，不记推测，不记对别人的描述。",
        {"content": {"type": "string", "description": "一句话，照顾客的意思写，例如“以后快递都寄到公司：文三路 88 号”"}},
        ["content"],
    ),
    _schema(
        "search_policy",
        "检索平台规则和法律法规原文，用来说明依据。",
        {"query": {"type": "string"}, "scope": {"type": "string", "enum": list(SCOPES)}},
        ["query"],
    ),
    _schema(
        "transfer_to_human",
        "转人工客服：顾客要求人工、涉及人身损害或欺诈赔偿、金额超过智能客服权限时使用，调用后这次对话结束。",
        {"summary": {"type": "string", "description": "给人工客服的说明：订单号、顾客诉求、已经做过的事"}, **ORDER_ID},
        ["summary"],
    ),
]
# 接了法规顾问（子 agent）时给主 agent 的工具，顶替 search_policy：法条原文由顾问读，主 agent 只拿结论
CONSULT_LEGAL = _schema(
    "consult_legal",
    "把顾客追问的法律问题交给法规顾问：它会检索法规原文，回答结论和依据（法规名称、条号）。回复顾客时照它给的法规名称和条号写。",
    {"question": {"type": "string", "description": "顾客问的法律问题，带上处境，例如“七天无理由退货的运费谁出”"}},
    ["question"],
)
# 购物工具（shopping=True 时才给模型）：购物和售后可以在同一段对话里办。下单也过确认卡片，卡片上有总价，顾客有预算就按预算确认
SHOPPING_SCHEMAS = [
    _schema(
        "search_products",
        "在商城里按关键词找商品，返回最多 5 件：编号（asin）、名称、价格区间、规格。顾客要买东西时先搜。",
        {"query": {"type": "string", "description": "关键词，照顾客的说法，例如“蓝牙耳机”"},
         "max_price": {"type": "number", "description": "只看不超过这个价的，顾客说了预算时填"}},
        ["query"],
    ),
    _schema("get_product", "看一件商品：每个规格的选项、价格、有没有货，以及退货政策的标注。下单前先看。", {"asin": {"type": "string"}}, ["asin"]),
    _schema(
        "buy_now",
        "替顾客下单并付款，寄到顾客的默认收货地址。会先向顾客展示商品、规格和总价的确认卡片，顾客确认后才下单。"
        "只买顾客明确要的那一件；顾客说了预算的，不下超过预算的单。",
        {"asin": {"type": "string"},
         "options": {"type": "object", "description": "选的规格，{规格名: 选项}；只有一个选项的规格可以不填", "additionalProperties": {"type": "string"}},
         "quantity": {"type": "integer", "description": "数量，默认 1"}},
        ["asin"],
    ),
]
SHOPPING_TOOLS = {tool["function"]["name"] for tool in SHOPPING_SCHEMAS}
_PARAMETERS = {tool["function"]["name"]: tool["function"]["parameters"] for tool in (*TOOL_SCHEMAS, CONSULT_LEGAL, *SHOPPING_SCHEMAS)}
WRITE_TOOLS = {
    "create_after_sales", "cancel_after_sales", "cancel_order", "change_address",
    "urge_order", "request_platform_intervention", "transfer_to_human", "remember_note", "buy_now",
}
GATES = ("ownership", "evidence", "tier", "risk", "confirm", "dedupe")  # 可以单独关掉的关卡：身份隔离、凭证、办理权限、风控、确认卡片、去重


def _overlaps(text, heard, size=4):
    """备注里有没有一段（连续 size 个字）是顾客说过的。去掉空白和标点再比。"""
    plain = lambda value: re.sub(r"[\s，。、；！？,.!?：:“”\"'（）()]", "", value or "")  # noqa: E731
    text, heard = plain(text), plain(heard)
    return any(text[i:i + size] in heard for i in range(max(len(text) - size + 1, 1))) if heard else False


_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}


def _invalid(value, schema, path):
    """按 JSON Schema 的子集检查参数：类型、枚举、对象的字段和必填项、数组的元素。返回第一处不对的说明，都对返回 None。

    小模型会生成 order_id=["x"]、content=42 这样的参数，不在这里拦住就会在工具里抛异常，整段对话结束。
    """
    expected = schema.get("type")
    if expected in _TYPES:
        wrong_type = not isinstance(value, _TYPES[expected]) or (isinstance(value, bool) and expected in ("integer", "number"))
        if wrong_type:
            return f"{path} 应该是 {expected}，给的是 {type(value).__name__}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} 只能是 {schema['enum']}"
    if expected == "object":
        properties, extra = schema.get("properties", {}), schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                problem = _invalid(item, properties[key], f"{path}.{key}")
            elif extra is False:
                problem = f"多了 {key}"
            else:
                problem = _invalid(item, extra, f"{path}.{key}") if isinstance(extra, dict) else None
            if problem:
                return problem
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            return f"缺少 {missing}"
    if expected == "array" and "items" in schema:
        for index, item in enumerate(value):
            problem = _invalid(item, schema["items"], f"{path}[{index}]")
            if problem:
                return problem
    return None


class Blocked(Exception):
    """harness 拦下的调用。outcome 记进审计日志：越权、政策不允许、凭证不足、需转人工、顾客未确认等。"""

    def __init__(self, outcome, message, decision=None):
        super().__init__(message)
        self.outcome, self.message, self.decision = outcome, message, decision


class Toolbox:
    """一次对话里的工具。confirm(name, arguments) 返回顾客是否确认这次办理。gates 是开着的关卡，默认全开。

    agents 是接上的子 agent（名字 → 对象，见 agents.py）：legal 顶替 search_policy，evidence 接在凭证关卡后面。
    """

    def __init__(self, shop, knowledge_base, user_id, now, confirm, *, session=None, gates=None, agents=None, legacy=False,
                 shopping=False):
        self.shop, self.knowledge_base = shop, knowledge_base
        self.user_id, self.now, self.confirm, self.session = user_id, now, confirm, session
        self.legacy = legacy  # 旧版 harness（对照用）：check_after_sales 不带办理权限那一档
        self.shopping = shopping  # 给不给购物工具：搜商品、看商品、下单
        self.gates = set(GATES) if gates is None else set(gates)
        unknown = self.gates - set(GATES)
        if unknown:
            raise ValueError(f"没有这些关卡：{sorted(unknown)}；可选的有 {list(GATES)}")
        self.agents = dict(agents or {})
        self.delegations = []  # 子 agent 跑过的任务：{"agent", "steps", "usage"}，对话循环取走记进轨迹
        self.executed = []  # 真正执行了的办理：{"tool", "arguments", "result"}
        self.blocked = []  # 被拦下的调用：{"tool", "arguments", "outcome", "message"}
        self.transferred = None
        self._done = {}  # 同一次对话里已经执行过的办理，键是工具名和参数
        self.risk = risk_profile(shop, user_id, now)  # P-RISK-01 的风控标识，办理权限那一关用
        self.heard = ""  # 顾客在这次对话里说过的话，备注只能记这里面有的

    def hear(self, text):
        """记下顾客说的一句话。对话循环每收到顾客一句话就调一次。"""
        self.heard += text or ""

    @property
    def tool_schemas(self):
        """给模型看的工具：没接知识库时不给 search_policy；接了法规顾问时 search_policy 换成 consult_legal；开了购物加三个。"""
        tools = list(TOOL_SCHEMAS)
        if self.knowledge_base is None:
            tools = [tool for tool in tools if tool["function"]["name"] != "search_policy"]
        elif "legal" in self.agents:
            tools = [CONSULT_LEGAL if tool["function"]["name"] == "search_policy" else tool for tool in tools]
        return tools + SHOPPING_SCHEMAS if self.shopping else tools

    def call(self, name, arguments):
        """执行一个工具调用，返回能转成 JSON 的结果。被拦下的和业务上不允许的返回 error，不抛异常。"""
        parameters = _PARAMETERS.get(name)
        try:
            if parameters is None:
                raise Blocked("参数错误", f"没有 {name} 这个工具")
            if not isinstance(arguments, dict):
                raise Blocked("参数错误", "参数必须是 JSON 对象")
            arguments = {key: value for key, value in arguments.items() if value is not None or key in parameters["required"]}  # 可选参数传 null 当没传
            problem = _invalid(arguments, parameters, "参数")
            if problem:
                raise Blocked("参数错误", f"参数不对：{problem}")
            key = name + json.dumps(arguments, ensure_ascii=False, sort_keys=True)
            if name in WRITE_TOOLS and key in self._done and "dedupe" in self.gates:
                self._audit(name, arguments, "重复请求", "返回第一次的结果")
                return {**self._done[key], "duplicate": True, "note": "同一个请求已经办过，这是第一次的结果"}
            if name not in WRITE_TOOLS:
                return getattr(self, f"_{name}")(**arguments)
            with _atomic(self.shop.conn):  # 一次办理要么全部写入，要么全部撤销
                result = getattr(self, f"_{name}")(**arguments)
            self._done[key] = result
            self.executed.append({"tool": name, "arguments": arguments, "result": result})
            self._audit(name, arguments, "已执行", _brief(result))
            return result
        except Blocked as blocked:
            return self._refuse(name, arguments, blocked.outcome, blocked.message, blocked.decision)
        except ShopError as error:
            decision = error.decision.to_dict() if error.decision else None
            return self._refuse(name, arguments, "政策不允许" if decision else "业务错误", str(error), decision)
        except Exception as failure:  # 工具自己出了错：还给模型一条错误，对话继续；写了一半的内容已经在保存点里撤销
            return self._refuse(name, arguments, "工具错误", f"工具内部错误：{type(failure).__name__}: {failure}", None)

    def _refuse(self, name, arguments, outcome, message, decision):
        self.blocked.append({"tool": name, "arguments": arguments, "outcome": outcome, "message": message})
        if name in WRITE_TOOLS or outcome == "越权":
            self._audit(name, arguments, outcome, message)
        return {"error": message, "decision": decision}

    def skip_after_transfer(self, name, arguments):
        """转人工之后同一批里排在后面的调用：不执行，记下来。"""
        return self._refuse(name, arguments, "转人工后未执行", "已转人工客服，对话结束，这个调用没有执行", None)

    def _audit(self, name, arguments, outcome, detail):
        self.shop.conn.execute(
            "INSERT INTO audit_log (time, logged_at, session, user_id, tool, arguments, outcome, detail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (fmt(self.now), datetime.now(timezone.utc).isoformat(timespec="seconds"), self.session, self.user_id,
             name, json.dumps(arguments, ensure_ascii=False), outcome, detail),
        )

    # ---------------------------------------------------------------- 身份
    def _own_order(self, order_id):
        row = self.shop.conn.execute("SELECT user_id, created_at FROM orders WHERE order_id = ?", (order_id,)).fetchone()
        if row is None or row["created_at"] > fmt(self.now):
            raise Blocked("业务错误", f"订单 {order_id} 不存在")
        if row["user_id"] != self.user_id and "ownership" in self.gates:
            raise Blocked("越权", f"订单 {order_id} 不存在")  # 不透露别人的订单是否存在
        return self.shop.get_order(order_id, now=self.now)

    def _own_ticket(self, ticket_id):
        row = self.shop.conn.execute("SELECT order_id FROM after_sales WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise Blocked("业务错误", f"售后单 {ticket_id} 不存在")
        try:
            self._own_order(row["order_id"])
        except Blocked as blocked:
            raise Blocked(blocked.outcome, f"售后单 {ticket_id} 不存在") from None
        return self.shop.get_after_sales(ticket_id, now=self.now)

    def _confirmed(self, name, arguments):
        if "confirm" in self.gates and not self.confirm(name, arguments):
            raise Blocked("顾客未确认", "顾客没有确认，这次没有办理")

    def _high_risk(self):
        """办理那一刻重新算风控标识（P-RISK-01）：这次对话里刚办的退款也计入，连续申请到线了下一笔就要审核。"""
        self.risk = risk_profile(self.shop, self.user_id, self.now)
        return "risk" in self.gates and self.risk["high"]

    def _risk_review(self):
        """风控要求的审核原因，写进售后单的进度；金额没超额度时用。"""
        return f"账户{'，'.join(self.risk['reasons'])}，按 P-RISK-01 提交人工审核"

    # ---------------------------------------------------------------- 只读
    def _list_orders(self):
        return {"orders": self.shop.list_orders(self.user_id, now=self.now)}

    def _get_order(self, order_id):
        return self._own_order(order_id)

    def _track_shipment(self, order_id):
        self._own_order(order_id)
        return self.shop.track_shipment(order_id, now=self.now)

    def _check_after_sales(self, order_id, type, reason, opened=False, activated=False):
        """政策判断，再加上办理权限（P-SVC-04、P-RISK-01）：模型不用自己拿金额去对额度。"""
        self._own_order(order_id)
        decision = self.shop.check_after_sales(order_id, type, reason, now=self.now, opened=opened, activated=activated)
        if not decision.get("allowed") or self.legacy:
            return decision
        tier = self.shop.policy.review_tier(type, decision["refund_amount"], high_risk=self._high_risk()) if "tier" in self.gates else "直接办理"
        decision["authority"] = tier
        if tier == "转人工":
            limit = self.shop.rules.get("P-SVC-04", "escalate_amount")
            decision["explanation"] += f"金额 {decision['refund_amount']:g} 元超过 {limit} 元，智能客服不能办理，必须转人工（P-SVC-04）。"
        elif tier == "人工审核":
            if decision["refund_amount"] > self.shop.rules.get("P-SVC-04", "auto_refund_limit"):
                decision["explanation"] += "提交后要先人工审核，审核通过后退款（P-SVC-04）。"
            else:
                decision["explanation"] += "账户近期售后申请较多，提交后要先人工审核（P-RISK-01），对顾客只说按平台规则需要审核。"
        return decision

    def _get_after_sales(self, ticket_id):
        return self._own_ticket(ticket_id)

    def _list_work_orders(self):
        return {"work_orders": self.shop.list_work_orders(self.user_id, now=self.now)}

    def _search_policy(self, query, scope="全部"):
        if scope not in SCOPES:
            raise Blocked("参数错误", f"scope 只能是 {list(SCOPES)}")
        if self.knowledge_base is None:
            raise Blocked("业务错误", "没有接入知识库，不能检索")
        if "legal" in self.agents and not getattr(self, "_advising", False):
            raise Blocked("参数错误", "法规检索交给法规顾问：用 consult_legal 提问")
        hits = self.knowledge_base.search(query, k=5, levels=SCOPES[scope])
        return {
            "results": [
                {
                    "citation": hit.citation,
                    "level": hit.chunk["level"],
                    "text": hit.chunk["text"][:SNIPPET_CHARS],
                    **({"legal_basis": hit.chunk["legal_basis"]} if hit.chunk.get("legal_basis") else {}),
                }
                for hit in hits
            ]
        }

    # ---------------------------------------------------------------- 子 agent
    def _consult_legal(self, question):
        advisor = self.agents.get("legal")
        if advisor is None:
            raise Blocked("参数错误", "没有接法规顾问，用 search_policy 检索")
        self._advising = True  # 顾问自己可以用 search_policy
        try:
            result, steps, usage = advisor.consult(self, question)
        finally:
            self._advising = False
        self.delegations.append({"agent": advisor.name, "steps": steps, "usage": usage})
        return result

    def _review_evidence(self, request, evidence, decision):
        """凭证审核员再看一遍问题描述：是不是顾客说的、和商品对不对得上。驳回的按“凭证不足”拦下。"""
        reviewer = self.agents["evidence"]
        row = self.shop.conn.execute("SELECT title FROM order_items WHERE order_id = ?", (request["order_id"],)).fetchone()
        self._advising = True
        try:
            verdict, steps, usage = reviewer.review(self, request, evidence, self.heard, row["title"] if row else "")
        finally:
            self._advising = False
        step = {"tool": "review_evidence", "arguments": {**request, "evidence": evidence}, "result": verdict, "seconds": 0.0}
        self.delegations.append({"agent": reviewer.name, "steps": [*steps, step], "usage": usage})
        if verdict["verdict"] != "通过":
            raise Blocked("凭证不足", f"凭证审核员驳回：{verdict['reason']}", decision)

    # ---------------------------------------------------------------- 办理
    def _create_after_sales(self, order_id, type, reason, opened=False, activated=False, evidence=""):
        decision = self._check_after_sales(order_id, type, reason, opened, activated)
        if not decision["allowed"]:
            raise Blocked("政策不允许", decision["explanation"], decision)
        reviewed = False
        if "evidence" in self.gates and reason in EVIDENCE_REASONS:
            if "evidence" in self.agents:
                # 有审核员时关键词检查让位：顾客说“转起来有点卡”“测得不准”这种关键词表里没有的话，由审核员判断是不是具体问题
                if len((evidence or "").strip()) < 4:
                    raise Blocked("凭证不足", f"按{reason}申请需要说明具体问题；只是不想要了的，可以申请七天无理由退货（P-QA-05、P-RET-01）")
                self._review_evidence({"order_id": order_id, "type": type, "reason": reason}, evidence, decision)
                reviewed = True
            else:
                lacking = self.shop.policy.evidence(reason, evidence)
                if lacking:
                    raise Blocked("凭证不足", lacking.explanation, lacking.to_dict())
        tier = self.shop.policy.review_tier(type, decision["refund_amount"], high_risk=self._high_risk()) if "tier" in self.gates else "直接办理"
        if tier == "转人工":
            limit = self.shop.rules.get("P-SVC-04", "escalate_amount")
            raise Blocked("需转人工", f"金额 {decision['refund_amount']:g} 元超过 {limit} 元，智能客服不能办理，必须转人工（P-SVC-04）", decision)
        arguments = {"order_id": order_id, "type": type, "reason": reason, "opened": opened, "activated": activated, "evidence": evidence}
        self._confirmed("create_after_sales", arguments)
        review = tier == "人工审核"
        if review and decision["refund_amount"] <= self.shop.rules.get("P-SVC-04", "auto_refund_limit"):
            review = self._risk_review()  # 金额没超额度，是风控要求的审核
        return self.shop.create_after_sales(
            order_id, type, reason, now=self.now, opened=opened, activated=activated, evidence=evidence, review=review,
            evidence_verified=reviewed or "evidence" not in self.gates,
        )

    def _cancel_after_sales(self, ticket_id):
        self._own_ticket(ticket_id)
        self._confirmed("cancel_after_sales", {"ticket_id": ticket_id})
        return self.shop.cancel_after_sales(ticket_id, now=self.now)

    def _cancel_order(self, order_id):
        self._own_order(order_id)
        self._confirmed("cancel_order", {"order_id": order_id})
        review = self._risk_review() if self._high_risk() else False  # 超时取消附带的补偿和别的补偿过同一道风控
        return self.shop.cancel_order(order_id, now=self.now, review=review)

    def _change_address(self, order_id, address):
        self._own_order(order_id)
        decision = self.shop.check_change_address(order_id, now=self.now)
        if not decision["allowed"]:
            raise Blocked("政策不允许", decision["explanation"], decision)
        self._confirmed("change_address", {"order_id": order_id, "address": address})
        return self.shop.change_address(order_id, address, now=self.now)

    def _urge_order(self, order_id):
        self._own_order(order_id)
        return self.shop.urge(order_id, now=self.now)

    def _request_platform_intervention(self, ticket_id, statement):
        ticket = self._own_ticket(ticket_id)
        if ticket["status"] != "商家拒绝":
            raise Blocked("政策不允许", f"售后单当前是“{ticket['status']}”，商家拒绝申请后才能申请平台介入（P-SVC-03）")
        self._confirmed("request_platform_intervention", {"ticket_id": ticket_id, "statement": statement})
        return self.shop.request_intervention(ticket_id, statement, now=self.now)

    def _remember_note(self, content):
        text = (content or "").strip()
        if not 4 <= len(text) <= 200:
            raise Blocked("参数错误", "备注要 4 到 200 个字")
        if not _overlaps(text, self.heard):
            raise Blocked("凭证不足", "备注只能记顾客自己说过的话，不能记推测或者别人的事")
        return add_note(self.shop, self.user_id, self.now, text, self.session)

    def _transfer_to_human(self, summary, order_id=None):
        if order_id is not None:
            self._own_order(order_id)
        work_order = self.shop.hand_off(self.user_id, summary, now=self.now, order_id=order_id)
        self.transferred = summary
        return {"transferred": True, "work_order_id": work_order["work_order_id"], "message": "已转人工客服，对话结束"}

    # ---------------------------------------------------------------- 购物
    def _shopping_on(self):
        if not self.shopping:
            raise Blocked("参数错误", "这次对话没有开购物工具")

    def _product(self, asin):
        row = self.shop.conn.execute("SELECT * FROM products WHERE asin = ?", (asin,)).fetchone()
        if row is None:
            raise Blocked("业务错误", f"商品 {asin} 不存在")
        return row

    @staticmethod
    def _policy_note(row):
        notes = []
        if row["no_return_marked"]:
            notes.append("商品页标注不支持七天无理由退货，下单即视为确认")
        if row["policy_class"]:
            notes.append(f"退货政策分类：{row['policy_class']}")
        return "；".join(notes)

    def _search_products(self, query, max_price=None):
        self._shopping_on()
        text = (query or "").strip()
        if not text:
            raise Blocked("参数错误", "关键词不能为空")
        try:
            import jieba

            words = [w for w in jieba.lcut_for_search(text) if len(w) >= 2]
        except ImportError:  # pragma: no cover
            words = []
        words = list(dict.fromkeys([text] + words))[:6]
        base = "SELECT asin, title, price_min, price_max, options, policy_class, no_return_marked FROM products WHERE ({}) ORDER BY price_min LIMIT 8"
        found = []
        for clause, params in ((" AND ".join(["title LIKE ?"] * len(words)), [f"%{w}%" for w in words]),
                               (" OR ".join(["title LIKE ?"] * len(words)), [f"%{w}%" for w in words])):
            rows = self.shop.conn.execute(base.format(clause), params).fetchall()
            if rows:
                found = rows
                break

        def brief(r):
            return {"asin": r["asin"], "title": r["title"][:40], "price_min": r["price_min"], "price_max": r["price_max"],
                    "options": {axis: [v["value"] for v in values[:6]] for axis, values in json.loads(r["options"]).items()},
                    **({"policy": self._policy_note(r)} if self._policy_note(r) else {})}

        if max_price is None:
            return {"results": [brief(r) for r in found[:5]]}
        # 有价格上限时，超过上限的也列出来：顾客要的那件要是超预算，得能告诉顾客它多少钱，而不是说没有这件商品
        within = [brief(r) for r in found if r["price_min"] is not None and r["price_min"] <= max_price][:5]
        over = [brief(r) for r in found if r["price_min"] is None or r["price_min"] > max_price][:3]
        return {"results": within, "over_max_price": over}

    def _get_product(self, asin):
        self._shopping_on()
        row = self._product(asin)
        return {
            "asin": row["asin"], "title": row["title"], "category": row["category"],
            "price_min": row["price_min"], "price_max": row["price_max"],
            "options": {axis: [{"value": v["value"], "price": v["price"], "available": v["available"]} for v in values]
                        for axis, values in json.loads(row["options"]).items()},
            "policy": self._policy_note(row) or "普通商品，签收后 7 天内可无理由退货",
        }

    def _buy_now(self, asin, options=None, quantity=1):
        """下单：规格和价格先算清楚，总价放进确认卡片，顾客确认了才真正下单付款。"""
        self._shopping_on()
        row = self._product(asin)
        if not isinstance(quantity, int) or quantity < 1:
            raise Blocked("参数错误", "数量要是正整数")
        product_options = json.loads(row["options"])
        selected = self.shop._select_options(product_options, options or {})  # 规格不对、售罄的在这里报错
        base = tuple(p for p in (row["price_min"], row["price_max"]) if p is not None)
        unit_price = variant_price(product_options, selected, base)
        if unit_price is None:
            raise Blocked("业务错误", "这个规格组合的价格无法确定，换一个规格")
        amount = round(unit_price * quantity, 2)
        self._confirmed("buy_now", {"asin": asin, "options": selected, "quantity": quantity, "amount": amount, "title": row["title"][:40]})
        order_id = self.shop.place_order(self.user_id, asin, selected, now=self.now, quantity=quantity, source="cs_agent")
        order = self.shop.get_order(order_id, now=self.now)
        promised = self.shop.conn.execute("SELECT promised_by FROM shipments WHERE order_id = ? AND direction = '发货'", (order_id,)).fetchone()
        return {"order_id": order_id, "title": row["title"][:40], "options": selected, "quantity": quantity,
                "unit_price": unit_price, "total_amount": amount, "address": order["address"],
                "promised_by": promised["promised_by"] if promised else None, "status": "已付款，待发货"}


@contextmanager
def _atomic(conn):
    """用保存点包住一次办理：出错只撤销这次办理写的内容，不动调用方还没提交的数据。提交由调用方负责。"""
    conn.execute("SAVEPOINT tool_call")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT tool_call")
        conn.execute("RELEASE SAVEPOINT tool_call")
        raise
    conn.execute("RELEASE SAVEPOINT tool_call")


def _brief(result):
    """审计日志里的结果摘要。"""
    if not isinstance(result, dict):
        return None
    keys = ("ticket_id", "work_order_id", "order_id", "status", "refund_amount", "address", "type", "kind")
    return json.dumps({key: result[key] for key in keys if key in result}, ensure_ascii=False)
