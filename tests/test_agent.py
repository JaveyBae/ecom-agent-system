"""售后 agent：文件桥、工具的权限和确认、场景生成、一段完整对话的评分。"""

import json
import threading
from datetime import datetime

from ecom_agent_system.agent import bridge
from ecom_agent_system.agent.bridge import FileBridge, assistant_message
from ecom_agent_system.agent.conversation import run_conversation
from ecom_agent_system.agent.scenarios import FREIGHT_LAW, LAW_PUSHBACK, ScriptedCustomer, build_scenarios, grade
from ecom_agent_system.agent.tools import Toolbox
from ecom_agent_system.shop.db import parse
from ecom_agent_system.shop.purchases import Buyers, record_purchase

NOW = datetime(2026, 10, 24, 12, 0, 0)
CONSUMER_LAW = {
    "chunk_id": "c#25",
    "title": "中华人民共和国消费者权益保护法",
    "level": "法律",
    "article_no": "第二十五条",
    "chapter": "",
    "text": "退回商品的运费由消费者承担；经营者和消费者另有约定的，按照约定。",
    "status": "有效",
}


def _buy(shop, asin, outcome, scenario, task_id, options=None, price=199.0):
    result = {
        "done": True, "termination_reason": outcome, "reward": 0.0,
        "purchase": {"asin": asin, "options": options or {}, "price": price}, "goal": {},
    }
    return record_purchase(shop, Buyers(shop.conn), result, now=NOW, label="claude", task_id=task_id, scenario=scenario)


def _owner(shop, order_id):
    return shop.conn.execute("SELECT user_id FROM orders WHERE order_id = ?", (order_id,)).fetchone()[0]


class ScriptedModel:
    """按顺序给出决策；决策可以是函数，按当前消息临时生成。"""

    def __init__(self, decisions):
        self.decisions = list(decisions)

    def complete(self, messages, tools):
        decision = self.decisions.pop(0)
        return assistant_message(decision(messages) if callable(decision) else decision, len(self.decisions))


# ---------------------------------------------------------------- 文件桥


def test_file_bridge_lets_an_outside_player_act_as_the_model(tmp_path):
    model = FileBridge(tmp_path, poll=0.01)
    tools = [{"type": "function", "function": {"name": "search_products", "parameters": {"properties": {"query": {"type": "string"}}}}}]
    replies = []

    def agent():
        messages = [{"role": "system", "content": "规则"}, {"role": "user", "content": "买一副耳机"}]
        replies.append(model.complete(messages, tools))
        messages += [replies[0], {"role": "tool", "name": "search_products", "content": "搜索结果：A1 蓝牙耳机"}]
        replies.append(model.complete(messages, tools))
        model.close()

    thread = threading.Thread(target=agent)
    thread.start()
    first = bridge.show(tmp_path, timeout=5)
    assert "【系统提示】\n规则" in first and "【用户】\n买一副耳机" in first and "search_products(query: string)" in first
    second = bridge.reply(tmp_path, {"tool": "search_products", "arguments": {"query": "耳机"}}, timeout=5)
    assert "搜索结果：A1 蓝牙耳机" in second and "买一副耳机" not in second  # 只显示新增的消息
    assert bridge.reply(tmp_path, {"content": "买好了"}, timeout=5) == "全部跑完了。"
    thread.join(5)
    assert replies[0]["tool_calls"][0]["function"] == {"name": "search_products", "arguments": '{"query": "耳机"}'}
    assert replies[1] == {"role": "assistant", "content": "买好了"}


# ---------------------------------------------------------------- 工具


def test_tools_only_see_the_customers_own_orders(shop, knowledge_base):
    mine = _buy(shop, "A1", "gold_purchase", "正常", task_id=1)
    other = next(order for order in (_buy(shop, "A5", "gold_purchase", "正常", task_id=task) for task in range(2, 40))
                 if _owner(shop, order) != _owner(shop, mine))
    toolbox = Toolbox(shop, knowledge_base, _owner(shop, mine), NOW, confirm=lambda name, arguments: True)
    assert [order["order_id"] for order in toolbox.call("list_orders", {})["orders"]] == [mine]
    assert toolbox.call("get_order", {"order_id": other}) == {"error": f"订单 {other} 不存在", "decision": None}
    assert "error" in toolbox.call("get_order", {"order_id": mine, "verbose": True})  # schema 外的参数
    assert toolbox.call("cancel_order", {"order_id": other})["error"] == f"订单 {other} 不存在"
    assert shop.conn.execute("SELECT cancelled_at FROM orders WHERE order_id = ?", (other,)).fetchone()[0] is None


def test_write_tools_need_the_customers_confirmation(shop, knowledge_base):
    order = _buy(shop, "A1", "wrong_purchase", "正常", task_id=1)
    later = NOW.replace(day=30)
    asked = []
    request = {"order_id": order, "type": "退货退款", "reason": "七天无理由"}

    def decline(name, arguments):
        asked.append(name)
        return False

    declined = Toolbox(shop, knowledge_base, _owner(shop, order), later, decline).call("create_after_sales", request)
    assert declined["error"] == "顾客没有确认，这次没有办理" and asked == ["create_after_sales"]
    assert shop.get_order(order, now=later)["after_sales"] == []
    approved = Toolbox(shop, knowledge_base, _owner(shop, order), later, lambda name, arguments: True)
    ticket = approved.call("create_after_sales", request)
    assert ticket["type"] == "退货退款" and approved.executed[0]["result"]["ticket_id"] == ticket["ticket_id"]
    # 政策不允许的，不会去打扰顾客
    noodles = _buy(shop, "A2", "wrong_purchase", "正常", task_id=2, price=30.0)
    refused = Toolbox(shop, knowledge_base, _owner(shop, noodles), later, decline).call(
        "create_after_sales", {**request, "order_id": noodles}
    )
    assert "P-RET-02" in refused["decision"]["rule_ids"] and asked == ["create_after_sales"]


def test_search_policy_filters_by_scope(shop, make_knowledge_base):
    toolbox = Toolbox(shop, make_knowledge_base(extra_chunks=[CONSUMER_LAW]), "U000001", NOW, lambda name, arguments: True)
    laws = toolbox.call("search_policy", {"query": "退货运费谁承担", "scope": "法律法规"})["results"]
    assert "《中华人民共和国消费者权益保护法》第二十五条" in [result["citation"] for result in laws]
    assert toolbox.call("search_policy", {"query": "退货运费谁承担", "scope": "平台规则"})["results"] == []


# ---------------------------------------------------------------- 场景和对话


def test_scenarios_follow_each_orders_timeline(shop):
    late = _buy(shop, "A1", "wrong_purchase", "延迟发货", task_id=1)
    noodles = _buy(shop, "A2", "partial_alternative_purchase", "正常", task_id=2, price=30.0)
    headphones = _buy(shop, "A1", "gold_purchase", "正常", task_id=3)
    scenarios = {scenario["order_id"]: scenario for scenario in build_scenarios(shop, "claude")}
    assert scenarios[late]["kind"] == "late_shipping"
    assert scenarios[late]["expected"]["tickets"] == [{"order_id": late, "type": "延迟发货补偿", "reason": "延迟发货"}]  # 标准答案带订单号
    food = scenarios[noodles]
    assert (food["kind"], food["expected"]["tickets"], food["follow_ups"]) == ("return_denied", [], [{"say": LAW_PUSHBACK, "when": "always"}])
    assert "P-RET-02" in food["expected"]["rule_ids"] and "《中华人民共和国消费者权益保护法》第二十五条" in food["expected"]["legal_basis"]
    returnable = scenarios[headphones]
    assert returnable["expected"]["tickets"] == [{"order_id": headphones, "type": "退货退款", "reason": "七天无理由"}]
    assert returnable["expected"]["legal_basis"] == FREIGHT_LAW  # 第一个能退货的场景追问运费
    assert "用不上了" in returnable["opening"]


def test_refund_only_amounts_set_review_and_transfer(shop):
    small = _buy(shop, "A1", "gold_purchase", "丢件", task_id=1, price=30.0)
    medium = _buy(shop, "A1", "gold_purchase", "丢件", task_id=2, price=300.0)
    large = _buy(shop, "A1", "gold_purchase", "丢件", task_id=3, price=1500.0)
    scenarios = {scenario["order_id"]: scenario for scenario in build_scenarios(shop, "claude")}
    assert scenarios[small]["kind"] == "stalled" and scenarios[small]["expected"].get("review") is None
    assert scenarios[small]["expected"]["refund_total"] == 30
    assert scenarios[medium]["kind"] == "stalled" and scenarios[medium]["expected"]["review"] is True
    # 超过 1000 元的仅退款智能客服不能办，标准答案是转人工、不提交
    assert scenarios[large]["kind"] == "escalate_amount" and scenarios[large]["category"] == "转人工"
    assert scenarios[large]["expected"]["tickets"] == [] and scenarios[large]["expected"]["transfer"] is True


def test_fake_shipping_counts_as_late_only_after_the_pickup_window(shop):
    order = _buy(shop, "A1", "gold_purchase", "虚假发货", task_id=1)
    scenario = build_scenarios(shop, "claude")[0]
    assert scenario["kind"] == "late_shipping" and "显示已经发货了" in scenario["opening"]
    # 顾客来问时，填单已经超过 24 小时还没揽收，按 P-SHIP-03 视为没发货，可以申请补偿
    assert scenario["expected"]["tickets"] == [{"order_id": order, "type": "延迟发货补偿", "reason": "延迟发货"}]
    assert shop.track_shipment(order, now=parse(scenario["now"]))["status"] == "已发货，待揽收"


def test_conversation_is_graded_against_the_policy_engine(shop, make_knowledge_base):
    order = _buy(shop, "A1", "wrong_purchase", "正常", task_id=1)
    scenario = build_scenarios(shop, "claude")[0]
    request = scenario["intent"]["arguments"]
    deadline = parse(scenario["expected"]["deadline"])
    model = ScriptedModel([
        {"tool": "list_orders"},
        {"tool": "check_after_sales", "arguments": request},
        {"tool": "create_after_sales", "arguments": request},
        {"content": f"已为您申请七天无理由退货（P-RET-01），请在 {deadline.month}月{deadline.day}日前寄回。"},
        {"tool": "search_policy", "arguments": {"query": "退货运费谁承担", "scope": "法律法规"}},
        {"content": "按《消费者权益保护法》第二十五条，退货运费由消费者承担，另有约定的从约定。"},
    ])
    customer = ScriptedCustomer(scenario)
    toolbox = Toolbox(shop, make_knowledge_base(extra_chunks=[CONSUMER_LAW]), scenario["user_id"], parse(scenario["now"]), customer.approve)
    record = {**run_conversation(toolbox, model, customer), "executed": toolbox.executed}
    result = grade(scenario, record, shop)
    assert result["passed"] and all(result["checks"].values()), result["checks"]
    assert set(result["checks"]) >= {"说了截止日期", "检索到了法律依据", "回复引用了法条", "转人工判断正确", "没有多退钱"}
    # 顾客的同意那句被跳过了（已经办好），直接问了运费
    assert [message["content"] for message in record["messages"] if message["role"] == "user"][1] == scenario["follow_ups"][-1]["say"]
    assert shop.get_order(order, now=parse(scenario["now"]))["after_sales"][0]["type"] == "退货退款"


def test_grading_catches_unrequested_actions(shop, knowledge_base):
    _buy(shop, "A1", "wrong_purchase", "正常", task_id=1)
    scenario = build_scenarios(shop, "claude", freight_question=False)[0]
    request = {**scenario["intent"]["arguments"], "type": "退货退款", "reason": "质量问题", "evidence": "通电后无法开机"}  # 顾客没说质量问题
    model = ScriptedModel([{"tool": "create_after_sales", "arguments": request}, {"content": "已经帮您按质量问题退货了。"}, {"content": "好的"}])
    customer = ScriptedCustomer(scenario)
    toolbox = Toolbox(shop, knowledge_base, scenario["user_id"], parse(scenario["now"]), customer.approve)
    record = {**run_conversation(toolbox, model, customer), "executed": toolbox.executed}
    assert toolbox.executed == []  # 顾客没有确认
    result = grade(scenario, record)
    assert result["passed"] is False and result["checks"]["售后单和标准答案一致"] is False
    assert json.loads(record["messages"][3]["content"])["error"] == "顾客没有确认，这次没有办理"
