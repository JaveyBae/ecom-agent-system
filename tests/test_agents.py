"""子 agent：法规顾问接成工具，凭证审核员接在凭证关卡后。受限的工具、轨迹和用量的合并、结论的解析、不用模型的桩。"""

from datetime import datetime, timedelta

from ecom_agent_system.agent.agents import LegalAdvisor, build_agents, parse_verdict, stub_agents
from ecom_agent_system.agent.bridge import assistant_message
from ecom_agent_system.agent.conversation import run_conversation, system_prompt
from ecom_agent_system.agent.customers import ScriptedCustomer
from ecom_agent_system.agent.grading import grade, summarize_records
from ecom_agent_system.agent.tools import GATES, Toolbox
from ecom_agent_system.shop import logistics
from ecom_agent_system.shop.db import parse

T0 = datetime(2026, 10, 1, 10, 0, 0)
BLACK = {"颜色分类": "黑色"}
QUALITY = {"type": "退货退款", "reason": "质量问题"}


def yes(name, arguments):
    return True


def _delivered(shop, order_id):
    rows = shop.conn.execute(
        "SELECT e.time, e.code FROM shipment_events e JOIN shipments s USING (tracking_no)"
        " WHERE s.order_id = ? AND s.direction = '发货' ORDER BY e.time, e.id",
        (order_id,),
    ).fetchall()
    return min(parse(row["time"]) for row in rows if row["code"] in logistics.DELIVERED)


def _signed(shop):
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    return order_id, _delivered(shop, order_id) + timedelta(days=1)


class ScriptedModel:
    """按脚本回复；记下每次调用拿到的工具名和消息，好检查主 agent 和子 agent 各看到什么。"""

    def __init__(self, decisions):
        self.decisions, self.seen, self.prompts = list(decisions), [], []

    def complete(self, messages, tools):
        self.seen.append([tool["function"]["name"] for tool in tools])
        self.prompts.append(list(messages))  # 当时看到的消息；列表之后还会被追加
        return assistant_message(self.decisions.pop(0), len(self.decisions))


def _scenario(order_id, opening, **expected):
    return {"scenario_id": f"{order_id}-test", "order_id": order_id, "kind": "test", "opening": opening, "follow_ups": [],
            "accept": "any", "expected": {"tickets": [], "transfer": False, "refund_total": 0.0, **expected}}


def test_legal_advisor_searches_and_the_main_agent_only_gets_the_conclusion(shop, knowledge_base):
    order_id, now = _signed(shop)
    advisor_model = ScriptedModel([
        {"tool": "search_policy", "arguments": {"query": "七日内退货", "scope": "法律法规"}},
        {"content": "可以退。依据《甲法》第一条，消费者自收到商品之日起七日内可以退货。"},
    ])
    main = ScriptedModel([
        {"tool": "consult_legal", "arguments": {"question": "收到七天内能退货吗"}},
        {"content": "可以的，依据《甲法》第一条，七日内可以退货。"},
    ])
    toolbox = Toolbox(shop, knowledge_base, "U000001", now, yes, agents=build_agents(["legal"], advisor_model))
    scenario = _scenario(order_id, "法律上七天内能退吗？", legal_basis=["《甲法》第一条"])
    result = run_conversation(toolbox, main, ScriptedCustomer(scenario))

    assert "consult_legal" in main.seen[0] and "search_policy" not in main.seen[0]  # 主 agent 没有 search_policy
    assert advisor_model.seen[0] == ["search_policy"] and "法规顾问" in advisor_model.prompts[0][0]["content"]
    assert "七天" in advisor_model.prompts[0][1]["content"] and len(advisor_model.prompts[0]) == 2  # 只拿到问题，看不到对话
    trace = result["trace"]
    assert [(e["tool"], e.get("agent")) for e in trace] == [("search_policy", "legal"), ("consult_legal", None)]
    assert trace[1]["result"]["citations"] == ["《甲法》第一条"] and trace[1]["result"]["searched"] == 1
    usage = result["usage"]
    assert usage["model_calls"] == 4 and usage["tool_calls"] == 2  # 主 agent 2 次、顾问 2 次模型调用；工具各 1 次
    assert usage["agents"]["legal"]["runs"] == 1 and usage["agents"]["legal"]["model_calls"] == 2 and usage["agents"]["legal"]["tool_calls"] == 1

    record = {**result, "executed": toolbox.executed, "blocked": toolbox.blocked}
    checks = grade(scenario, record)["checks"]
    assert checks["检索到了法律依据"] and checks["回复引用了法条"]  # 顾问检索到的也算
    assert "consult_legal" in toolbox.call("search_policy", {"query": "退货"})["error"]  # 主 agent 不能绕过顾问
    assert toolbox.blocked[-1]["outcome"] == "参数错误"


def test_a_sub_agent_cannot_use_tools_outside_its_role(shop, knowledge_base):
    order_id, now = _signed(shop)
    advisor_model = ScriptedModel([
        {"tool": "create_after_sales", "arguments": {"order_id": order_id, "type": "退货退款", "reason": "七天无理由"}},
        {"tool": "get_order", "arguments": {"order_id": order_id}},
        {"content": "没有找到相关法规。"},
    ])
    toolbox = Toolbox(shop, knowledge_base, "U000001", now, yes, agents={"legal": LegalAdvisor(advisor_model)})
    result = toolbox.call("consult_legal", {"question": "能退吗"})
    assert result["citations"] == [] and result["searched"] == 0
    assert [(b["tool"], b["outcome"]) for b in toolbox.blocked] == [("create_after_sales", "越权"), ("get_order", "越权")]
    assert "法规顾问只能用" in toolbox.blocked[0]["message"] and toolbox.executed == []
    steps = toolbox.delegations[0]["steps"]
    assert [s["tool"] for s in steps] == ["create_after_sales", "get_order"] and all("error" in s["result"] for s in steps)

    endless = ScriptedModel([{"tool": "search_policy", "arguments": {"query": "退货"}}] * 4)
    toolbox = Toolbox(shop, knowledge_base, "U000001", now, yes, agents={"legal": LegalAdvisor(endless)})
    result = toolbox.call("consult_legal", {"question": "能退吗"})
    assert result["answer"] == "法规顾问没有给出结论" and result["searched"] == 4  # 步数用完


def test_evidence_reviewer_blocks_fabricated_evidence_and_passes_the_customers_words(shop, knowledge_base):
    order_id, now = _signed(shop)
    request = {"order_id": order_id, **QUALITY}

    rejecting = ScriptedModel([{"content": "顾客只说质量不行，没有提过耳机不出声。\n结论：驳回，原因：问题描述不是顾客说的"}])
    toolbox = Toolbox(shop, knowledge_base, "U000001", now, yes, agents=build_agents(["evidence"], rejecting))
    toolbox.hear("质量不行，具体说不上来")
    result = toolbox.call("create_after_sales", {**request, "evidence": "左边耳机无法出声"})
    assert "凭证审核员驳回" in result["error"] and toolbox.blocked[-1]["outcome"] == "凭证不足"
    assert shop.conn.execute("SELECT COUNT(*) FROM after_sales").fetchone()[0] == 0
    review = toolbox.delegations[0]["steps"][-1]
    assert review["tool"] == "review_evidence" and review["result"]["verdict"] == "驳回" and "不是顾客说的" in review["result"]["reason"]
    task = rejecting.prompts[0][1]["content"]
    assert "蓝牙耳机" in task and "质量不行，具体说不上来" in task and "左边耳机无法出声" in task  # 商品、顾客原话、填写的描述
    assert "凭证审核员" in rejecting.prompts[0][0]["content"] and rejecting.seen[0] == ["search_policy"]

    untouched = ScriptedModel([])  # 太短的描述不劳审核员；“质量不好”这种四个字以上的空话交给审核员判（关键词检查让位）
    toolbox = Toolbox(shop, knowledge_base, "U000001", now, yes, agents=build_agents(["evidence"], untouched))
    assert toolbox.call("create_after_sales", {**request, "evidence": "坏了"})["error"].startswith("按质量问题申请需要说明具体问题")
    assert toolbox.delegations == []

    approving = ScriptedModel([{"content": "结论：通过"}])
    toolbox = Toolbox(shop, knowledge_base, "U000001", now, yes, agents=build_agents(["evidence"], approving))
    toolbox.hear("左边耳机没声音")
    ticket = toolbox.call("create_after_sales", {**request, "evidence": "左边耳机无法出声"})
    assert ticket["ticket_id"] and toolbox.executed[-1]["tool"] == "create_after_sales"
    assert toolbox.delegations[-1]["steps"][-1]["result"]["verdict"] == "通过"


def test_with_a_reviewer_the_keyword_check_yields_to_its_judgement(shop, knowledge_base):
    """顾客说“转起来不太顺，有点卡”：关键词表里没有这些词，没有审核员就会被拦；有审核员时由审核员判它是不是具体问题。"""
    order_id, now = _signed(shop)
    request = {"order_id": order_id, **QUALITY, "evidence": "转起来不太顺，有一点卡"}
    plain = Toolbox(shop, knowledge_base, "U000001", now, yes)
    assert "P-QA-05" in plain.call("create_after_sales", request)["decision"]["rule_ids"] and plain.blocked[-1]["outcome"] == "凭证不足"
    approving = Toolbox(shop, knowledge_base, "U000001", now, yes, agents=build_agents(["evidence"], ScriptedModel([{"content": "结论：通过"}])))
    approving.hear("转起来不太顺，有一点卡")
    assert approving.call("create_after_sales", request)["ticket_id"]
    empty = Toolbox(shop, knowledge_base, "U000001", now, yes, agents=stub_agents(["evidence"], "approve"))
    assert "需要说明具体问题" in empty.call("create_after_sales", {**request, "evidence": "坏"})["error"]  # 空话仍然不行


def test_stub_reviewers_bracket_the_real_one_and_follow_the_evidence_gate_switch(shop, knowledge_base):
    order_id, now = _signed(shop)
    request = {"order_id": order_id, **QUALITY, "evidence": "左边耳机无法出声"}
    rejecting = Toolbox(shop, knowledge_base, "U000001", now, yes, agents=stub_agents(["evidence", "legal"], "reject"))
    assert rejecting.call("create_after_sales", request)["error"].startswith("凭证审核员驳回")
    assert rejecting.call("consult_legal", {"question": "能退吗"})["citations"] == []

    open_ = Toolbox(shop, knowledge_base, "U000001", now, yes, gates=[g for g in GATES if g != "evidence"],
                    agents=stub_agents(["evidence"], "reject"))
    assert open_.call("create_after_sales", request)["ticket_id"]  # 关掉凭证关卡，审核员也跟着不看
    assert open_.delegations == []

    order2, now2 = _signed(shop)
    approving = Toolbox(shop, knowledge_base, "U000001", now2, yes, agents=stub_agents(["evidence"], "approve"))
    assert approving.call("create_after_sales", {**request, "order_id": order2})["ticket_id"]


def test_parse_verdict_reads_the_conclusion_line_and_tolerates_sloppy_formats():
    assert parse_verdict("分析……\n结论：驳回，原因：顾客没有提过黑屏") == ("驳回", "顾客没有提过黑屏")
    assert parse_verdict("结论: 通过") == ("通过", "结论: 通过")
    assert parse_verdict("这份描述和顾客说的一致，可以通过。") == ("通过", "这份描述和顾客说的一致，可以通过。")
    assert parse_verdict("描述里的毛病顾客没说过，不通过。") == ("驳回", "描述里的毛病顾客没说过，不通过。")
    assert parse_verdict("") == ("未判定", "审核员没有给出结论")


def test_reports_count_sub_agent_usage_and_review_verdicts(shop, knowledge_base):
    order_id, now = _signed(shop)
    main = ScriptedModel([
        {"tool": "create_after_sales", "arguments": {"order_id": order_id, **QUALITY, "evidence": "左边耳机无法出声"}},
        {"content": "已为您按质量问题申请退货。"},
    ])
    reviewer = ScriptedModel([{"content": "结论：通过"}])
    toolbox = Toolbox(shop, knowledge_base, "U000001", now, yes, agents=build_agents(["evidence"], reviewer))
    scenario = _scenario(order_id, "左边耳机无法出声，要退货", tickets=[QUALITY])
    result = run_conversation(toolbox, main, ScriptedCustomer(scenario))
    assert [(e["tool"], e.get("agent")) for e in result["trace"]] == [("review_evidence", "evidence"), ("create_after_sales", None)]
    record = {**result, "scenario": scenario, "executed": toolbox.executed, "blocked": toolbox.blocked, "agents": ["evidence"]}
    record["grade"] = grade(scenario, record)
    assert record["grade"]["passed"]
    report = summarize_records([record])
    assert report["agents"]["evidence"]["runs"] == 1 and report["agents"]["evidence"]["model_calls"] == 1
    assert report["evidence_reviews"] == {"reviewed": 1, "rejected": 0, "undecided": 0}
    assert "agents" not in summarize_records([{**record, "usage": {}, "trace": []}])  # 没接子 agent 的报告不多这两项


def test_legacy_harness_restores_the_old_wording_and_the_bare_check_result(shop, knowledge_base):
    order_id, now = _signed(shop)
    fresh, legacy = system_prompt(shop, "U000001", T0), system_prompt(shop, "U000001", T0, legacy=True)
    assert "直接调用工具去办" in fresh and "不要再追问细节" in fresh
    assert "调用后系统会给顾客弹出确认卡片" in legacy and "要先问清具体问题" in legacy and "直接调用工具去办" not in legacy
    request = {"order_id": order_id, "type": "退货退款", "reason": "七天无理由"}
    assert Toolbox(shop, knowledge_base, "U000001", now, yes).call("check_after_sales", request)["authority"] == "直接办理"
    assert "authority" not in Toolbox(shop, knowledge_base, "U000001", now, yes, legacy=True).call("check_after_sales", request)


def test_the_system_prompt_sends_legal_questions_to_the_advisor_when_one_is_attached(shop):
    assert "consult_legal" in system_prompt(shop, "U000001", T0, advisor=True)
    plain = system_prompt(shop, "U000001", T0)
    assert "search_policy" in plain and "consult_legal" not in plain
    assert "consult_legal" not in system_prompt(shop, "U000001", T0, knowledge=False, advisor=True)
