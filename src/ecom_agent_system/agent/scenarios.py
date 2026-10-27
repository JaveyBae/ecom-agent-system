"""从订单生成售后场景，跑售后对话并评分。

每个订单按它的物流情形挑一个时刻让顾客来找客服（situation）：

- 超过发货时限还没揽收的，在时限过后来问，想要延迟发货补偿；
- 物流停滞的，在停滞超过 P-SHIP-07 的天数之后来问，想要退款；
- 破损拒收的，拒收后来问退款；
- 签收了的，签收两天后来申请七天无理由退货：买错的说和想要的不一样，买对的说用不上了。

标准答案由政策引擎在那个时刻判断：允许的，应当提交对应的售后单；不允许的，不应提交，并且要讲清依据。
不退货就给的钱按 P-SVC-04 分级：超过直接办理额度的提交后要人工审核（expected.review），超过转人工线的
不能办，要转人工（kind 记为 escalate_amount）。每个场景带一份参考解法（reference），由 ReferenceModel 照着执行。

build_scenarios 用购物 agent 买的单；拦截、改地址、催单、商家拒绝、转人工、回访和攻击场景见 suite.py。
"""

from __future__ import annotations

import json
import re
import time
from datetime import timedelta

from ecom_agent_system.agent.bridge import assistant_message
from ecom_agent_system.agent.conversation import run_conversation
from ecom_agent_system.agent.customers import LLMCustomer, NoisyCustomer, ScriptedCustomer
from ecom_agent_system.agent.grading import MONEY_TYPES, grade
from ecom_agent_system.agent.memory import remember, risk_profile
from ecom_agent_system.agent.tools import GATES, Toolbox
from ecom_agent_system.shop import logistics
from ecom_agent_system.shop.db import fmt, get_meta, parse, set_meta
from ecom_agent_system.shop.policy import EVIDENCE_REASONS

__all__ = ["LLMCustomer", "NoisyCustomer", "ScriptedCustomer", "build_scenarios", "grade", "run_scenarios"]

LAW_PUSHBACK = "可是法律上不是规定网购七天内都能无理由退货吗？凭什么不行？"
FREIGHT_QUESTION = "顺便问一下，退货的运费一般是谁出？法律上是怎么规定的？"
FREIGHT_LAW = ["《中华人民共和国消费者权益保护法》第二十五条", "《网络购买商品七日无理由退货暂行办法》第十八条"]
GOODBYE = "好的，还有其他问题随时找我。"
# 不能办时，回复里总得有一个表示拒绝的词；什么都不说、只说“好的”不算讲清楚了
REFUSAL_WORDS = ["不支持", "不适用", "不能", "无法", "不符合", "没法", "不可以", "不属于", "已过", "超过", "过了"]
CUSTOMERS = {"scripted": ScriptedCustomer, "noisy": NoisyCustomer}


def short_name(item):
    """顾客嘴里的商品名：标题开头一段，去掉“10个装）”这类数量说明。类目不可靠，配件常挂在整机类目下。"""
    title = re.sub(r"^[（(【\[]?[0-9一二三四五六七八九十]+[个只件支瓶包袋片]装[）)】\]]?", "", item["title"]).strip()
    return f"「{title[:12]}」"


def day(moment):
    """10月5日：和评分里认的日期写法一致。"""
    moment = parse(moment) if isinstance(moment, str) else moment
    return f"{moment.month}月{moment.day}日"


def rules_text(rule_ids):
    return "（" + "、".join(rule_ids) + "）" if rule_ids else ""


def outbound(conn, order_id):
    shipment = conn.execute("SELECT * FROM shipments WHERE order_id = ? AND direction = '发货'", (order_id,)).fetchone()
    events = conn.execute(
        "SELECT time, code FROM shipment_events WHERE tracking_no = ? ORDER BY time, id", (shipment["tracking_no"],)
    ).fetchall()
    return shipment, [(parse(event["time"]), event["code"]) for event in events]


def situation(shop, order, item, *, wrong=False):
    """挑顾客来问的时刻和来意：(情形, 时刻, 开场白, 想办的售后, 同意办理时说的话, 问题描述)。"""
    shipment, events = outbound(shop.conn, order["order_id"])
    paid, name = parse(order["created_at"]), short_name(item)
    deadline = paid + timedelta(hours=shop.rules.get("P-SHIP-01", "ship_hours"))
    entered = next((moment for moment, code in events if code == "已发货"), None)
    picked = next((moment for moment, code in events if code == "已揽收"), None)
    delivered = next((moment for moment, code in events if code in logistics.DELIVERED), None)
    shipped = shop.policy.ship_time(entered, picked, picked) if entered and picked else None  # P-SHIP-03 认定的发货时间
    # 按时填了单号的，要等填单 24 小时还没揽收，才算没发货（P-SHIP-03），从那以后才超时
    late_from = deadline
    if entered is not None and entered <= deadline:
        late_from = max(deadline, entered + timedelta(hours=shop.rules.get("P-SHIP-03", "fake_ship_hours")))
    if shipped is not None and shipped > late_from + timedelta(hours=1):
        now = late_from + min((shipped - late_from) / 2, timedelta(hours=12))
        if entered < now:  # 填了单号但一直没揽收
            opening = f"我{paid.month}月{paid.day}日买的{name}，显示已经发货了，可物流一直没动静，怎么回事？"
        else:
            opening = f"我{paid.month}月{paid.day}日买的{name}，到现在还没发货，怎么回事？"
        return ("late_shipping", now, opening, ("延迟发货补偿", "延迟发货"), "那能给点补偿吗？", None)
    if shipment["scenario"] in ("物流停滞", "丢件"):
        gaps = [(later - earlier, earlier) for (earlier, _), (later, _) in zip(events, events[1:])]
        stalled_from = events[-1][0] if shipment["scenario"] == "丢件" else max(gaps)[1]
        now = stalled_from + timedelta(days=logistics.stall_days(shop.rules, order["zone"]), hours=6)
        return ("stalled", now, f"我买的{name}，物流好几天没更新了，是不是丢了？", ("仅退款", "物流异常"), "那能直接给我退款吗？", None)
    if shipment["scenario"] == "破损拒收":
        return ("rejected", events[-1][0] + timedelta(hours=3), f"我买的{name}送到的时候外包装破了，我当场拒收了，钱怎么退？",
                ("仅退款", "运输损毁"), "那麻烦帮我申请退款。", "送到时外包装破了，当场拒收")
    if delivered is not None:
        feeling = "和我想要的不一样" if wrong else "用不上了"
        return ("return", delivered + timedelta(days=2, hours=3), f"我买的{name}收到了，{feeling}，想退掉。",
                ("退货退款", "七天无理由"), "可以，麻烦帮我办理。", None)
    return None


def _granted(decision, request_type):
    """提交成功后回复顾客的话：办了什么、多少钱、截止日期和依据。"""
    amount, rule_ids = decision["refund_amount"], rules_text(decision["rule_ids"])
    if request_type == "延迟发货补偿":
        deadline = parse(decision["deadline"])
        return f"商家应在{day(deadline)} {deadline:%H:%M}前发货，已经超时，已为您申请延迟发货补偿 {amount:g} 元{rule_ids}。"
    if request_type == "仅退款":
        return f"已为您申请仅退款 {amount:g} 元{rule_ids}。"
    fee = f"退货运费 {decision['return_fee']:g} 元由您承担。" if decision["fee_payer"] == "消费者" else f"退货运费由{decision['fee_payer']}承担。"
    return f"已为您申请七天无理由退货{rule_ids}，请在{day(decision['deadline'])}前寄回，退款 {amount:g} 元。{fee}"


def standard_scenario(shop, order, item, *, wrong=False, ask_freight=False):
    """按订单的物流情形生成一个基本售后场景，见模块说明。order 要有 order_id、user_id、created_at、zone。"""
    found = situation(shop, order, item, wrong=wrong)
    if found is None:
        return None
    kind, now, opening, (request_type, reason), consent, evidence = found
    now = now.replace(microsecond=0)
    order_id = order["order_id"]
    request = {"order_id": order_id, "type": request_type, "reason": reason}
    decision = shop.check_after_sales(order_id, request_type, reason, now=now)
    high_risk = risk_profile(shop, order["user_id"], now)["high"]
    tier = shop.policy.review_tier(request_type, decision["refund_amount"], high_risk=high_risk) if decision["allowed"] else None
    submit = {**request, **({"evidence": evidence} if reason in EVIDENCE_REASONS else {})}
    follow_ups, legal_basis = [], []
    expected = {"tickets": [], "transfer": False, "refund_total": 0.0, "rule_ids": decision["rule_ids"],
                "deadline": decision["deadline"], "legal_basis": []}
    check = {"tool": "check_after_sales", "arguments": request}
    if decision["allowed"] and tier == "转人工":
        kind = "escalate_amount"
        limit = shop.rules.get("P-SVC-04", "escalate_amount")
        summary = f"订单 {order_id}：顾客申请{request_type}（{reason}），金额 {decision['refund_amount']:g} 元超过 {limit} 元，按 P-SVC-04 转人工。"
        follow_ups.append({"say": consent, "when": "pending"})  # 和能办的情形一样，顾客会明确提出要退款；agent 讲完情况再转也来得及
        expected.update(transfer=True, rule_ids=[], deadline=None)
        reference = [[check, {"tool": "transfer_to_human", "arguments": {"summary": summary, "order_id": order_id},
                              "content": f"这笔退款金额超过 {limit} 元，智能客服不能直接办理，我帮您转人工客服（P-SVC-04）。"}]]
    elif decision["allowed"]:
        follow_ups.append({"say": consent, "when": "pending"})  # agent 还没办时，顾客表示同意
        reply = _granted(decision, request_type)
        if tier == "人工审核":
            limit = shop.rules.get("P-SVC-04", "auto_refund_limit")
            if decision["refund_amount"] > limit:
                reply += f"金额超过 {limit} 元，需要人工审核，审核通过后退款（P-SVC-04）。"
            else:  # 金额没超额度，是账户近期售后较多（P-RISK-01）；不对顾客提“风控”
                reply += "按平台规则，您的账户近期售后申请较多，这笔需要先人工审核，审核通过后到账（P-RISK-01）。"
            expected.update(review=True, mention=["审核"])
        expected.update(tickets=[{"order_id": order_id, "type": request_type, "reason": reason}],
                        refund_total=decision["refund_amount"] if request_type in MONEY_TYPES else 0.0)
        reference = [[check, {"tool": "create_after_sales", "arguments": submit}, {"content": reply}]]
        if kind == "return" and ask_freight:
            follow_ups.append({"say": FREIGHT_QUESTION, "when": "always"})
            legal_basis = FREIGHT_LAW
            reference.append([
                {"tool": "search_policy", "arguments": {"query": "退货运费由谁承担", "scope": "法律法规"}},
                {"content": f"依据{'、'.join(FREIGHT_LAW)}，七天无理由退货的运费由消费者承担，经营者和消费者另有约定的按约定。"},
            ])
    else:
        reference = [[check, {"content": f"{decision['explanation']}{rules_text(decision['rule_ids'])}"}]]
        expected["mention"] = REFUSAL_WORDS
        if "P-RET-02" in decision["rule_ids"]:
            follow_ups.append({"say": LAW_PUSHBACK, "when": "always"})
            legal_basis = shop.rules.legal_basis["P-RET-02"]
            reference.append([
                {"tool": "search_policy", "arguments": {"query": "七日无理由退货 不适用 商品", "scope": "法律法规"}},
                {"content": f"七天无理由退货有法定的例外，依据{'、'.join(legal_basis)}，这类商品不适用（P-RET-02）。"},
            ])
        if kind == "return":
            kind = "return_denied"
    expected["legal_basis"] = legal_basis
    name = short_name(item)
    return {
        "scenario_id": f"{order_id}-{kind}",
        "order_id": order_id,
        "user_id": order["user_id"],
        "kind": kind,
        "category": "转人工" if kind == "escalate_amount" else "基本售后",
        "now": fmt(now),
        "opening": opening,
        "follow_ups": follow_ups,
        "accept": [{"tool": "create_after_sales", "arguments": request}],
        "intent": {"tool": "create_after_sales", "arguments": request},
        "customer_brief": f"你在 ShopSim 买了{name}（订单号 {order_id}，客服问了再说）。{opening}",
        "goal": {"延迟发货补偿": "拿到延迟发货的补偿", "仅退款": "拿到退款", "退货退款": "把商品退掉、拿回货款"}[request_type],
        "expected": expected,
        "reference": reference,
    }


def build_scenarios(shop, label, *, freight_question=True):
    """给这个购物 agent 买的每个订单生成一个售后场景。第一个能退货的场景追问一句退货运费的法律规定。"""
    orders = shop.conn.execute(
        "SELECT a.order_id, a.outcome, o.user_id, o.created_at, o.zone FROM agent_purchases a"
        " JOIN orders o USING (order_id) WHERE a.label = ? ORDER BY o.created_at",
        (label,),
    ).fetchall()
    scenarios, ask_freight = [], freight_question
    for order in orders:
        item = shop.conn.execute("SELECT * FROM order_items WHERE order_id = ?", (order["order_id"],)).fetchone()
        scenario = standard_scenario(shop, order, item, wrong=order["outcome"] != "gold_purchase", ask_freight=ask_freight)
        if scenario is None:
            continue
        if scenario["expected"]["legal_basis"] == FREIGHT_LAW:
            ask_freight = False
        scenarios.append({**scenario, "outcome": order["outcome"]})
    return scenarios


class ReferenceModel:
    """照着场景里的参考解法回复：每个顾客回合一组步骤，前面是工具调用，最后一句回复顾客。

    参考解法知道标准答案，用来检查场景本身：它应当通过全部硬性要求，没通过说明场景或评分有问题。
    """

    def __init__(self, scenario):
        self.turns = scenario.get("reference") or []

    def complete(self, messages, tools):
        users = [index for index, message in enumerate(messages) if message["role"] == "user"]
        turn = len(users) - 1
        position = sum(message["role"] == "assistant" for message in messages[users[-1]:])
        steps = self.turns[turn] if turn < len(self.turns) else []
        step = steps[position] if position < len(steps) else {"content": GOODBYE}
        return assistant_message(step, len(messages))


def make_customer(kind, scenario, *, model=None, shop=None):
    if kind == "llm":
        if model is None:
            raise ValueError("大模型扮演的顾客需要一个模型")
        user = shop.get_user(scenario["user_id"]) if shop is not None else None
        persona = f"{user['tier']}会员，住在{user['address'][:6]}一带" if user else None
        return LLMCustomer(scenario, model, persona=persona)
    return CUSTOMERS[kind](scenario)


def run_scenarios(shop, knowledge_base, scenarios, model, output, *, model_for=None, customer="scripted", customer_model=None,
                  memory=True, trial=1, label="run", db=None, gates=None, agents=None, legacy=False, shopping=None):
    """逐个跑售后场景，每段对话的记录和评分追加到 output（JSONL）。跑完把库里的当前时间推到最后一段对话。

    model_for(scenario) 为每个场景单独给一个模型（参考解法和基线策略用）；customer 是 scripted、noisy 或 llm；
    memory=False 时系统提示里不放顾客档案，用来对照记忆的作用。每段对话的摘要写进 conversations 表。
    db 是这次跑在哪个库上（文件名），记进每条记录，控制台按它找对话之后的订单状态。
    knowledge_base 传 None、gates 只开一部分关卡，都是对照实验，记在每条记录的 kb 和 gates_off 里。
    agents 是接上的子 agent（名字 → 对象，见 agents.py），记在每条记录的 agents 里。
    shopping 是给不给购物工具：None 时混合情形（标准答案里有购买，或类别是“混合”）自动开，其余不开；True/False 强制。
    """
    records = []
    gates_off = sorted(set(GATES) - set(gates)) if gates is not None else []
    for scenario in scenarios:
        session = f"{label}-t{trial}-{scenario['scenario_id']}"
        started = parse(scenario["now"])
        person = make_customer(customer, scenario, model=customer_model, shop=shop)
        shop_tools = shopping if shopping is not None else bool(scenario["expected"].get("purchases") is not None and (
            scenario["expected"].get("purchases") or scenario.get("category") == "混合"))
        toolbox = Toolbox(shop, knowledge_base, scenario["user_id"], started, person.approve, session=session, gates=gates, agents=agents,
                          legacy=legacy, shopping=shop_tools)
        clock = time.perf_counter()
        result = run_conversation(toolbox, model_for(scenario) if model_for else model, person, memory=memory, catch_errors=True, legacy=legacy)
        record = {
            "scenario": scenario, "trial": trial, "session": session, "customer": customer, "memory": memory, "db": db,
            "kb": knowledge_base is not None, "gates_off": gates_off, "agents": sorted(agents) if agents else [], "legacy": legacy,
            "shopping": shop_tools,
            **result, "executed": toolbox.executed, "blocked": toolbox.blocked, "seconds": round(time.perf_counter() - clock, 3),
        }
        remember(shop, session, scenario["user_id"], started, person.opening, toolbox.executed, record["ended"])
        record["grade"] = grade(scenario, record, shop)
        shop.conn.commit()
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        records.append(record)
    latest = max((parse(scenario["now"]) for scenario in scenarios), default=None)
    current = get_meta(shop.conn, "world_now")
    if latest and (current is None or latest > parse(current)):
        set_meta(shop.conn, "world_now", fmt(latest))
        shop.conn.commit()
    return records
