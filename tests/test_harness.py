"""harness 的各道关和补上的业务：凭证、办理权限、重复请求、事务、审计、改地址、拦截、催单、商家拒绝和平台介入、
转人工、顾客档案、三种顾客、模型接口和重试、批量评测里的出错。"""

import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from ecom_agent_system.agent.bridge import assistant_message
from ecom_agent_system.agent.conversation import run_conversation, system_prompt
from ecom_agent_system.agent.customers import LLMCustomer, NoisyCustomer, ScriptedCustomer
from ecom_agent_system.agent.memory import customer_profile, remember
from ecom_agent_system.agent.models import OpenAIModel, RetryingModel
from ecom_agent_system.agent.scenarios import run_scenarios
from ecom_agent_system.agent.tools import TOOL_SCHEMAS, Toolbox
from ecom_agent_system.shop import logistics
from ecom_agent_system.shop.db import fmt, get_meta, parse, set_meta
from ecom_agent_system.shop.service import ShopError

T0 = datetime(2026, 10, 1, 10, 0, 0)
BLACK = {"颜色分类": "黑色"}


def yes(name, arguments):
    return True


def _times(shop, order_id):
    """发货物流里每种事件第一次出现的时间。"""
    rows = shop.conn.execute(
        "SELECT e.time, e.code FROM shipment_events e JOIN shipments s USING (tracking_no)"
        " WHERE s.order_id = ? AND s.direction = '发货' ORDER BY e.time, e.id",
        (order_id,),
    ).fetchall()
    times = {}
    for row in rows:
        times.setdefault(row["code"], parse(row["time"]))
    return times


def _delivered(shop, order_id):
    times = _times(shop, order_id)
    return min(times[code] for code in logistics.DELIVERED if code in times)


def _audit(shop):
    return [(row["tool"], row["outcome"]) for row in shop.conn.execute("SELECT tool, outcome FROM audit_log ORDER BY id")]


class ScriptedModel:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    def complete(self, messages, tools):
        return assistant_message(self.decisions.pop(0), len(self.decisions))


# ---------------------------------------------------------------- 资损控制：凭证、办理权限、重复请求、事务


def test_quality_claims_need_a_specific_problem(shop, knowledge_base):
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    toolbox = Toolbox(shop, knowledge_base, "U000001", _delivered(shop, order_id) + timedelta(days=1), yes)
    request = {"order_id": order_id, "type": "退货退款", "reason": "质量问题"}
    assert toolbox.call("check_after_sales", request)["requires_evidence"] is True
    for vague in ({}, {"evidence": "质量不好"}):
        assert "P-QA-05" in toolbox.call("create_after_sales", {**request, **vague})["decision"]["rule_ids"]
    ticket = toolbox.call("create_after_sales", {**request, "evidence": "左边耳机无法出声"})
    assert [entry["outcome"] for entry in toolbox.blocked] == ["凭证不足", "凭证不足"]
    stored = shop.conn.execute("SELECT evidence FROM after_sales WHERE ticket_id = ?", (ticket["ticket_id"],)).fetchone()[0]
    assert stored == "左边耳机无法出声"


def test_refunds_follow_the_agent_authority_tiers(shop, knowledge_base):
    """P-SVC-04：50 元以内直接退，50 到 1000 元先人工审核，超过 1000 元不能办。"""
    results = {}
    for amount in (30, 300, 1500):
        order_id = shop.place_order("U000003", "A1", BLACK, now=T0, scenario="丢件", unit_price=amount)
        last = parse(shop.conn.execute(
            "SELECT MAX(e.time) FROM shipment_events e JOIN shipments s USING (tracking_no) WHERE s.order_id = ?", (order_id,)
        ).fetchone()[0])  # 丢件：最后一条之后不再更新
        now = last + timedelta(days=logistics.stall_days(shop.rules, "一般"), hours=1)
        toolbox = Toolbox(shop, knowledge_base, "U000003", now, yes)
        check = toolbox.call("check_after_sales", {"order_id": order_id, "type": "仅退款", "reason": "物流异常"})
        assert check["authority"] == {30: "直接办理", 300: "人工审核", 1500: "转人工"}[amount]  # 判断时就告诉模型办理权限
        assert ("P-SVC-04" in check["explanation"]) == (amount > 50)
        results[amount] = (toolbox, now, toolbox.call("create_after_sales", {"order_id": order_id, "type": "仅退款", "reason": "物流异常"}))
    toolbox, now, direct = results[30]
    assert "待人工审核" not in [event["status"] for event in direct["history"]]
    toolbox, now, reviewed = results[300]
    assert [event["status"] for event in reviewed["history"]] == ["待商家处理", "待人工审核"]
    later = shop.get_after_sales(reviewed["ticket_id"], now=now + timedelta(days=4))
    assert [event["status"] for event in later["history"]][-2:] == ["人工审核通过", "退款成功"]
    toolbox, now, refused = results[1500]
    assert "P-SVC-04" in refused["error"] and toolbox.blocked[0]["outcome"] == "需转人工" and toolbox.executed == []


def test_repeated_requests_are_answered_once_and_everything_is_audited(shop, knowledge_base):
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    other = shop.place_order("U000002", "A2", {}, now=T0, scenario="正常")
    now = _delivered(shop, order_id) + timedelta(days=1)
    toolbox = Toolbox(shop, knowledge_base, "U000001", now, yes, session="s1")
    request = {"order_id": order_id, "type": "退货退款", "reason": "七天无理由"}
    first = toolbox.call("create_after_sales", request)
    again = toolbox.call("create_after_sales", request)  # 模型重试、网络重发时常见
    assert again["duplicate"] is True and again["ticket_id"] == first["ticket_id"]
    assert len(toolbox.executed) == 1 and shop.conn.execute("SELECT COUNT(*) FROM after_sales").fetchone()[0] == 1
    toolbox.call("get_order", {"order_id": other})  # 别人的订单：越权，要记
    toolbox.call("list_orders", {})  # 只读的不记
    declined = Toolbox(shop, knowledge_base, "U000001", now, lambda name, arguments: False, session="s2")
    declined.call("cancel_after_sales", {"ticket_id": first["ticket_id"]})
    assert _audit(shop) == [
        ("create_after_sales", "已执行"), ("create_after_sales", "重复请求"), ("get_order", "越权"), ("cancel_after_sales", "顾客未确认"),
    ]
    assert {row[0] for row in shop.conn.execute("SELECT session FROM audit_log")} == {"s1", "s2"}


def test_a_failed_call_rolls_back_only_its_own_writes(shop, knowledge_base, monkeypatch):
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    set_meta(shop.conn, "probe", "调用方还没提交的数据")
    original = shop.change_address

    def broken(order_id, address, *, now):
        original(order_id, address, now=now)  # 已经写了一条修改记录
        raise ShopError("地址服务出错")

    monkeypatch.setattr(shop, "change_address", broken)
    toolbox = Toolbox(shop, knowledge_base, "U000001", T0 + timedelta(minutes=30), yes)
    assert toolbox.call("change_address", {"order_id": order_id, "address": "文三路88号3幢501室"})["error"] == "地址服务出错"
    assert shop.conn.execute("SELECT COUNT(*) FROM order_changes").fetchone()[0] == 0
    assert get_meta(shop.conn, "probe") == "调用方还没提交的数据"
    assert _audit(shop) == [("change_address", "业务错误")]


# ---------------------------------------------------------------- 高频业务：改地址、拦截、催单


def test_address_can_change_only_before_shipping(shop, knowledge_base):
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    before = T0 + timedelta(minutes=30)  # 最早付款 1 小时后才发货
    changed = Toolbox(shop, knowledge_base, "U000001", before, yes).call(
        "change_address", {"order_id": order_id, "address": "文三路88号3幢501室"}
    )
    assert changed["address"].endswith("文三路88号3幢501室") and changed["rule_ids"] == ["P-SHIP-11"]
    assert shop.get_order(order_id, now=before)["address"].endswith("文三路88号3幢501室")
    assert not shop.get_order(order_id, now=before - timedelta(minutes=1))["address"].endswith("文三路88号3幢501室")
    late = Toolbox(shop, knowledge_base, "U000001", _times(shop, order_id)["已发货"] + timedelta(minutes=5), yes)
    refused = late.call("change_address", {"order_id": order_id, "address": "建设路12号"})
    assert "P-SHIP-05" in refused["decision"]["rule_ids"] and late.blocked[0]["outcome"] == "政策不允许"


def test_intercept_returns_the_parcel_and_refunds_minus_round_trip_freight(shop):
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    now = _times(shop, order_id)["已揽收"] + timedelta(hours=1)  # 同城最快揽收 18 小时后送达，来得及拦
    decision = shop.check_after_sales(order_id, "拦截退款", "顾客原因", now=now)
    assert decision["allowed"] and (decision["return_fee"], decision["refund_amount"]) == (20, 179)
    ticket = shop.create_after_sales(order_id, "拦截退款", "顾客原因", now=now)
    assert ticket["status"] == "拦截中"
    later = now + timedelta(days=5)
    final = shop.get_after_sales(ticket["ticket_id"], now=later)
    assert final["status"] == "退款成功" and "扣除往返运费 20 元" in final["history"][-1]["note"]
    assert shop.track_shipment(order_id, now=later)["status"] == "已退回商家"
    codes = list(_times(shop, order_id))
    assert codes[-2:] == ["拦截退回", "退回商家"] and not set(codes) & set(logistics.DELIVERED)


def test_intercept_fails_if_the_parcel_is_delivered_first(shop):
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    delivered = _delivered(shop, order_id)
    ticket = shop.create_after_sales(order_id, "拦截退款", "顾客原因", now=delivered - timedelta(hours=1))  # 拦截至少 2 小时才生效
    final = shop.get_after_sales(ticket["ticket_id"], now=delivered + timedelta(hours=1))
    assert final["status"] == "拦截失败" and "七天无理由" in final["history"][-1]["note"]
    assert shop.get_order(order_id, now=delivered + timedelta(hours=1))["status"] == "已签收"


def test_free_intercept_only_when_the_merchant_shipped_late(shop):
    decisions = {}
    for scenario in ("正常", "延迟发货"):
        order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario=scenario)
        now = _times(shop, order_id)["已揽收"] + timedelta(hours=1)
        decisions[scenario] = shop.check_after_sales(order_id, "拦截退款", "商家原因", now=now)
    assert not decisions["正常"]["allowed"] and "P-SHIP-01" in decisions["正常"]["rule_ids"]
    late = decisions["延迟发货"]
    assert late["allowed"] and (late["fee_payer"], late["return_fee"], late["refund_amount"]) == ("商家", 0, 199)


def test_urging_is_deduplicated_and_answered_with_the_plan(shop, knowledge_base):
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    times = _times(shop, order_id)
    now = T0 + timedelta(minutes=30)
    first = Toolbox(shop, knowledge_base, "U000001", now, yes).call("urge_order", {"order_id": order_id})
    assert first["kind"] == "催发货" and first["repeated"] is False and "P-SHIP-01" in first["rule_ids"]
    again = Toolbox(shop, knowledge_base, "U000001", now + timedelta(minutes=10), yes).call("urge_order", {"order_id": order_id})
    assert again["repeated"] is True and again["work_order_id"] == first["work_order_id"]
    answered = shop.get_work_order(first["work_order_id"], now=now + timedelta(hours=25))
    assert answered["status"] == "已答复" and f"{times['已发货']:%m-%d %H:%M}" in answered["history"][-1]["note"]
    in_transit = times["已揽收"] + timedelta(hours=2)
    delivery = shop.urge(order_id, now=in_transit)
    assert delivery["kind"] == "催派送" and "P-SHIP-08" in delivery["rule_ids"]
    assert [w["kind"] for w in shop.list_work_orders("U000001", now=in_transit)] == ["催派送", "催发货"]


# ---------------------------------------------------------------- 商家和平台、转人工


def test_merchant_rejection_can_be_taken_to_the_platform(shop, knowledge_base):
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    applied = _delivered(shop, order_id) + timedelta(days=1)
    ticket = shop.create_after_sales(order_id, "退货退款", "七天无理由", now=applied, opened=True, merchant_decision="reject")
    ticket_id = ticket["ticket_id"]
    rejected = parse(shop.conn.execute(
        "SELECT time FROM after_sales_events WHERE ticket_id = ? AND status = '商家拒绝'", (ticket_id,)
    ).fetchone()[0])
    assert rejected - applied < timedelta(hours=shop.rules.get("P-RET-06", "respond_hours"))
    early = Toolbox(shop, knowledge_base, "U000001", applied + timedelta(minutes=1), yes)
    assert "商家拒绝申请后才能申请平台介入" in early.call("request_platform_intervention", {"ticket_id": ticket_id, "statement": "拆封查验"})["error"]
    now = rejected + timedelta(hours=2)
    progress = shop.get_after_sales(ticket_id, now=now)
    assert progress["status"] == "商家拒绝" and "平台介入" in progress["next_step"]
    result = Toolbox(shop, knowledge_base, "U000001", now, yes).call(
        "request_platform_intervention", {"ticket_id": ticket_id, "statement": "拆封查验不影响退货"}
    )
    assert result["status"] == "平台介入中"
    final = shop.get_after_sales(ticket_id, now=now + timedelta(days=4))
    assert final["status"] in ("待买家退货", "已关闭") and final["history"][-1]["actor"] == "平台"


def test_transfer_opens_a_work_order_and_ends_the_conversation(shop, knowledge_base):
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    now = T0 + timedelta(days=3)
    model = ScriptedModel([{"tool": "transfer_to_human", "arguments": {"summary": f"订单 {order_id}，顾客要求人工", "order_id": order_id},
                            "content": "马上为您转人工。"}])
    customer = ScriptedCustomer({"opening": "转人工", "follow_ups": [{"say": "喂？", "when": "always"}], "accept": []})
    result = run_conversation(Toolbox(shop, knowledge_base, "U000001", now, customer.approve), model, customer)
    assert result["ended"] == "transferred" and [m["role"] for m in result["messages"]] == ["system", "user", "assistant", "tool"]
    work_order = shop.list_work_orders("U000001", now=now + timedelta(minutes=20))[0]
    assert (work_order["kind"], work_order["status"], work_order["order_id"]) == ("转人工", "人工处理中", order_id)


# ---------------------------------------------------------------- 记忆


def test_profile_shows_recent_orders_open_tickets_and_past_conversations(shop):
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    applied = _delivered(shop, order_id) + timedelta(days=1)
    request = {"order_id": order_id, "type": "退货退款", "reason": "七天无理由"}
    ticket = shop.create_after_sales(order_id, "退货退款", "七天无理由", now=applied, merchant_decision="agree")
    remember(shop, "s1", "U000001", applied, "耳机不想要了", [{"tool": "create_after_sales", "arguments": request, "result": ticket}], "customer_done")
    profile = customer_profile(shop, "U000001", applied + timedelta(hours=1))
    assert order_id in profile and ticket["ticket_id"] in profile and "提交售后" in profile
    assert "没有订单和咨询记录" in customer_profile(shop, "U000001", T0 - timedelta(days=1))  # 只看得到当时已经发生的
    assert "顾客档案（截至" in system_prompt(shop, "U000001", applied)
    assert "顾客档案" not in system_prompt(shop, "U000001", applied, memory=False)


# ---------------------------------------------------------------- 顾客


def test_noisy_customer_is_reproducible_and_keeps_ids():
    scenario = {"scenario_id": "s1", "opening": "我的订单 SO261001000001 现在怎么还没发货？可以退款吗？",
                "follow_ups": [{"say": "可以，帮我办理。", "when": "always"}], "accept": []}
    first, second = NoisyCustomer(scenario), NoisyCustomer(scenario)
    assert first.opening == second.opening and "SO261001000001" in first.opening
    nobody = SimpleNamespace(executed=[])
    assert first.respond("好的", nobody) == second.respond("好的", nobody)
    assert any(NoisyCustomer({**scenario, "scenario_id": str(i)}).opening != scenario["opening"] for i in range(10))


def test_llm_customer_plays_from_the_brief_and_ends_with_the_marker():
    lines = iter(["你好，我的耳机不想要了。", "好的，谢谢###结束"])
    prompts = []

    class Model:
        def complete(self, messages, tools):
            prompts.append(messages)
            return {"role": "assistant", "content": next(lines)}

    scenario = {"scenario_id": "s1", "opening": "耳机不想要了", "follow_ups": [], "customer_brief": "你买了耳机，不想要了", "goal": "退货",
                "accept": [{"tool": "create_after_sales", "arguments": {"order_id": "SO1", "type": "退货退款"}}]}
    customer = LLMCustomer(scenario, Model(), persona="黄金会员")
    assert customer.opening == "你好，我的耳机不想要了。" and "你买了耳机，不想要了" in prompts[0][0]["content"]
    assert customer.approve("create_after_sales", {"order_id": "SO1", "type": "退货退款", "reason": "七天无理由"})
    assert not customer.approve("cancel_order", {"order_id": "SO1"})  # 确认仍按场景规则
    nobody = SimpleNamespace(executed=[])
    assert customer.respond("已为您提交退货申请", nobody) == "好的，谢谢"
    assert prompts[1][-1] == {"role": "user", "content": "已为您提交退货申请"}
    assert customer.respond("不客气", nobody) is None


# ---------------------------------------------------------------- 模型接口


def _serve(statuses, calls):
    """假的 /chat/completions：按 statuses 依次返回状态码，200 时返回一个工具调用。"""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.headers["Authorization"], body))
            status = statuses[min(len(calls), len(statuses)) - 1]
            payload = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "list_orders", "arguments": "{}"}}]}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 8}} if status == 200 else {"error": "boom"}
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_openai_client_parses_tool_calls_and_retries_server_errors():
    calls, sleeps = [], []
    server = _serve([500, 200], calls)
    try:
        model = RetryingModel(OpenAIModel(f"http://127.0.0.1:{server.server_port}/v1", "qwen", "key"), sleep=sleeps.append)
        message = model.complete([{"role": "user", "content": "我的订单"}], TOOL_SCHEMAS)
    finally:
        server.shutdown()
    assert message == {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "list_orders", "arguments": "{}"}}]}
    assert model.last_usage == {"input_tokens": 120, "output_tokens": 8} and model.retried == 1 and sleeps == [1.0]
    authorization, body = calls[-1]
    assert authorization == "Bearer key" and body["model"] == "qwen" and body["tools"] == TOOL_SCHEMAS


def test_client_errors_are_not_retried():
    calls = []
    server = _serve([400], calls)
    try:
        model = RetryingModel(OpenAIModel(f"http://127.0.0.1:{server.server_port}/v1", "qwen", "key"), sleep=lambda seconds: None)
        with pytest.raises(HTTPError):
            model.complete([{"role": "user", "content": "你好"}], [])
    finally:
        server.shutdown()
    assert len(calls) == 1 and model.retried == 0


def test_usage_uses_reported_tokens_and_estimates_otherwise(shop, knowledge_base):
    customer = ScriptedCustomer({"opening": "看看我的订单", "follow_ups": [], "accept": []})

    class Reporting(ScriptedModel):
        def complete(self, messages, tools):
            self.last_usage = {"input_tokens": 100, "output_tokens": 10}
            return super().complete(messages, tools)

    decisions = [{"tool": "list_orders"}, {"content": "您目前没有订单。"}]
    reported = run_conversation(Toolbox(shop, knowledge_base, "U000001", T0, yes), Reporting(decisions), customer)["usage"]
    assert (reported["model_calls"], reported["tool_calls"], reported["input_tokens"], reported["estimated"]) == (2, 1, 200, False)
    estimated = run_conversation(Toolbox(shop, knowledge_base, "U000001", T0, yes), ScriptedModel(decisions), customer)["usage"]
    assert estimated["estimated"] is True and estimated["input_tokens"] > 0


def test_batch_runs_record_model_errors_and_continue(shop, knowledge_base, tmp_path):
    class Broken:
        def complete(self, messages, tools):
            raise TimeoutError("文件桥等不到回复")

    scenarios = [{"scenario_id": f"s{i}", "kind": "return", "order_id": None, "user_id": "U000001", "now": fmt(T0), "opening": "在吗",
                  "follow_ups": [], "accept": [], "expected": {"tickets": [], "transfer": False}} for i in range(2)]
    records = run_scenarios(shop, knowledge_base, scenarios, Broken(), tmp_path / "out.jsonl")
    assert [record["ended"] for record in records] == ["error", "error"]
    assert "TimeoutError" in records[0]["error"] and not records[0]["grade"]["passed"]
    assert shop.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 2
    assert len((tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()) == 2


# ---------------------------------------------------------------- 对照实验：关掉关卡、不接知识库


def test_gates_can_be_switched_off_for_ablations(shop, knowledge_base):
    """关掉关卡后，别人的订单查得到、大额退款直接办、同一个请求办两次、不用顾客确认。"""
    mine = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    other = shop.place_order("U000002", "A2", {}, now=T0, scenario="正常")
    big = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="丢件", unit_price=1500)
    last = parse(shop.conn.execute(
        "SELECT MAX(e.time) FROM shipment_events e JOIN shipments s USING (tracking_no) WHERE s.order_id = ?", (big,)
    ).fetchone()[0])
    now = max(_delivered(shop, mine) + timedelta(days=1), last + timedelta(days=logistics.stall_days(shop.rules, "一般"), hours=1))
    never = lambda name, arguments: False  # noqa: E731
    guarded = Toolbox(shop, knowledge_base, "U000001", now, never)
    assert "error" in guarded.call("get_order", {"order_id": other})
    assert guarded.call("create_after_sales", {"order_id": big, "type": "仅退款", "reason": "物流异常"})["error"].startswith("金额")
    assert guarded.call("create_after_sales", {"order_id": mine, "type": "退货退款", "reason": "七天无理由"})["error"] == "顾客没有确认，这次没有办理"
    assert [entry["outcome"] for entry in guarded.blocked] == ["越权", "需转人工", "顾客未确认"]

    open_ = Toolbox(shop, knowledge_base, "U000001", now, never, gates=())
    assert open_.call("get_order", {"order_id": other})["order_id"] == other  # 别人的订单
    refund = open_.call("create_after_sales", {"order_id": big, "type": "仅退款", "reason": "物流异常"})
    assert refund["refund_amount"] == 1500 and "待人工审核" not in [event["status"] for event in refund["history"]]
    request = {"order_id": mine, "type": "退货退款", "reason": "七天无理由"}
    first, second = open_.call("create_after_sales", request), open_.call("create_after_sales", request)
    assert "ticket_id" in first and "duplicate" not in second and "error" in second  # 没有确认；重发不再返回第一次的结果，由商城的规则拒绝
    assert [entry["outcome"] for entry in open_.blocked] == ["业务错误"] and len(open_.executed) == 2
    with pytest.raises(ValueError):
        Toolbox(shop, knowledge_base, "U000001", now, yes, gates=("firewall",))


def test_without_a_knowledge_base_there_is_no_search_tool(shop):
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    toolbox = Toolbox(shop, None, "U000001", T0 + timedelta(hours=1), yes)
    names = [tool["function"]["name"] for tool in toolbox.tool_schemas]
    assert "search_policy" not in names and len(names) == len(TOOL_SCHEMAS) - 1
    assert "知识库" in toolbox.call("search_policy", {"query": "七天无理由"})["error"]
    assert "search_policy" in system_prompt(shop, "U000001", T0) and "search_policy" not in system_prompt(shop, "U000001", T0, knowledge=False)
    model = ScriptedModel([{"content": "您好。"}])
    result = run_conversation(toolbox, model, ScriptedCustomer({"opening": "在吗", "follow_ups": [], "accept": []}))
    assert "search_policy" not in result["messages"][0]["content"] and order_id


# ---------------------------------------------------------------- 参数校验、工具内部错误、转人工后的同批调用


def test_arguments_are_checked_against_the_schema_and_a_broken_tool_does_not_end_the_conversation(shop, knowledge_base, monkeypatch):
    """小模型会生成 order_id=["x"]、content=42 这样的参数：退回“参数错误”让它改，不能抛异常结束整段对话。"""
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")
    toolbox = Toolbox(shop, knowledge_base, "U000001", T0 + timedelta(minutes=30), yes)
    bad = (
        ("get_order", {"order_id": [order_id]}),
        ("remember_note", {"content": 42}),
        ("check_after_sales", {"order_id": order_id, "type": "退货", "reason": "七天无理由"}),  # 枚举外
        ("check_after_sales", {"order_id": order_id, "type": "退货退款", "reason": "七天无理由", "opened": "true"}),
        ("buy_now", {"asin": "A1", "options": "黑色"}),  # 嵌套参数的类型
        ("get_order", {"order_id": order_id, "extra": 1}),
    )
    for name, arguments in bad:
        result = toolbox.call(name, arguments)
        assert "参数不对" in result["error"], (name, result)
    assert toolbox.executed == [] and {b["outcome"] for b in toolbox.blocked} == {"参数错误"}
    nulled = toolbox.call("check_after_sales", {"order_id": order_id, "type": "退货退款", "reason": "七天无理由", "opened": None})  # 可选参数传 null 当没传
    assert "error" not in nulled and nulled["allowed"] is False

    original = shop.change_address

    def broken(order_id, address, *, now):
        original(order_id, address, now=now)  # 已经写了一条修改记录
        raise RuntimeError("地址服务崩了")

    monkeypatch.setattr(shop, "change_address", broken)
    result = toolbox.call("change_address", {"order_id": order_id, "address": "文三路88号3幢501室"})
    assert result["error"].startswith("工具内部错误：RuntimeError") and toolbox.blocked[-1]["outcome"] == "工具错误"
    assert shop.conn.execute("SELECT COUNT(*) FROM order_changes").fetchone()[0] == 0  # 写了一半的内容撤销了
    assert _audit(shop)[-1] == ("change_address", "工具错误")


def test_nothing_in_the_same_batch_runs_after_a_transfer(shop, knowledge_base):
    """模型一次回复里既转人工又取消订单：转人工是对话的终点，排在它后面的调用不执行，只给回执。"""
    order_id = shop.place_order("U000001", "A1", BLACK, now=T0, scenario="正常")

    class Model:
        def complete(self, messages, tools):
            calls = [("transfer_to_human", {"summary": f"订单 {order_id}，顾客要人工", "order_id": order_id}), ("cancel_order", {"order_id": order_id})]
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
                for i, (name, args) in enumerate(calls)
            ]}

    toolbox = Toolbox(shop, knowledge_base, "U000001", T0 + timedelta(minutes=30), yes)
    customer = ScriptedCustomer({"opening": "给我转人工，顺便把这单取消了", "follow_ups": [], "accept": []})
    result = run_conversation(toolbox, Model(), customer)
    assert result["ended"] == "transferred" and [e["tool"] for e in toolbox.executed] == ["transfer_to_human"]
    assert toolbox.blocked[-1]["outcome"] == "转人工后未执行"
    assert shop.conn.execute("SELECT cancelled_at FROM orders WHERE order_id = ?", (order_id,)).fetchone()[0] is None
    assert [m["name"] for m in result["messages"] if m["role"] == "tool"] == ["transfer_to_human", "cancel_order"]  # 两个调用都有回执
    assert [e["tool"] for e in result["trace"]] == ["transfer_to_human", "cancel_order"] and "没有执行" in result["trace"][-1]["result"]["error"]


def test_tracking_hints_carry_the_agent_authority(shop, knowledge_base):
    """物流提示说“可以申请全额退款”时，同一条结果里写明这单超没超智能客服的权限（P-SVC-04）；旧版 harness 不写。"""
    for amount, expected in ((30, None), (300, "人工审核"), (1500, "转人工")):
        order_id = shop.place_order("U000003", "A1", BLACK, now=T0, scenario="丢件", unit_price=amount)
        last = parse(shop.conn.execute(
            "SELECT MAX(e.time) FROM shipment_events e JOIN shipments s USING (tracking_no) WHERE s.order_id = ?", (order_id,)
        ).fetchone()[0])
        now = last + timedelta(days=logistics.stall_days(shop.rules, "一般"), hours=1)
        tracked = Toolbox(shop, knowledge_base, "U000003", now, yes).call("track_shipment", {"order_id": order_id})
        assert any("全额退款" in note for shipment in tracked["shipments"] for note in shipment["notes"])
        assert tracked.get("authority") == expected and ("P-SVC-04" in "".join(tracked["notes"])) == (expected is not None), tracked["notes"]
        legacy = Toolbox(shop, knowledge_base, "U000003", now, yes, legacy=True).call("track_shipment", {"order_id": order_id})
        assert "authority" not in legacy and "P-SVC-04" not in "".join(legacy["notes"])
    normal = shop.place_order("U000003", "A1", BLACK, now=T0, scenario="正常")
    assert "authority" not in Toolbox(shop, knowledge_base, "U000003", T0 + timedelta(hours=1), yes).call("track_shipment", {"order_id": normal})


def test_rate_limits_are_retried_with_capped_backoff():
    """限流严的接口：429 按 backoff 翻倍重试，单次等待不超过 max_wait。"""
    calls, sleeps = [], []
    server = _serve([429, 429, 429, 200], calls)
    try:
        model = RetryingModel(OpenAIModel(f"http://127.0.0.1:{server.server_port}/v1", "glm", "key"), retries=3, backoff=40, sleep=sleeps.append)
        message = model.complete([{"role": "user", "content": "我的订单"}], TOOL_SCHEMAS)
    finally:
        server.shutdown()
    assert message["tool_calls"] and model.retried == 3 and sleeps == [40, 60, 60] and len(calls) == 4
