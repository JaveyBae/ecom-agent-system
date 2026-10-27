"""购物和售后在同一段对话里：搜商品、看商品、下单（带总价的确认卡片、预算）、评分里的购买检查，以及数据回流。"""

import json
from datetime import datetime

from ecom_agent_system.agent.conversation import system_prompt
from ecom_agent_system.agent.customers import approves, matches
from ecom_agent_system.agent.flywheel import export_sft, harvest, merge_scenarios
from ecom_agent_system.agent.grading import grade
from ecom_agent_system.agent.tools import SHOPPING_TOOLS, Toolbox

T0 = datetime(2026, 10, 1, 10, 0, 0)


def yes(name, arguments):
    return True


def _record(messages, executed=(), trace=(), blocked=()):
    return {"messages": messages, "executed": list(executed), "trace": list(trace), "blocked": list(blocked), "ended": "customer_done"}


def test_shopping_tools_are_only_offered_when_switched_on(shop, knowledge_base):
    plain = Toolbox(shop, knowledge_base, "U000001", T0, yes)
    assert not SHOPPING_TOOLS & {t["function"]["name"] for t in plain.tool_schemas}
    assert "没有开购物工具" in plain.call("search_products", {"query": "耳机"})["error"]
    shopping = Toolbox(shop, knowledge_base, "U000001", T0, yes, shopping=True)
    assert SHOPPING_TOOLS <= {t["function"]["name"] for t in shopping.tool_schemas}
    assert "buy_now" in system_prompt(shop, "U000001", T0, shopping=True) and "也帮顾客下单" in system_prompt(shop, "U000001", T0, shopping=True)
    assert "buy_now" not in system_prompt(shop, "U000001", T0)


def test_search_and_product_details_come_from_the_catalog(shop, knowledge_base):
    toolbox = Toolbox(shop, knowledge_base, "U000001", T0, yes, shopping=True)
    found = toolbox.call("search_products", {"query": "蓝牙耳机"})["results"]
    assert [r["asin"] for r in found] == ["A1"] and found[0]["options"] == {"颜色分类": ["黑色", "白色"]}
    assert toolbox.call("search_products", {"query": "蓝牙耳机", "max_price": 100})["results"] == []
    product = toolbox.call("get_product", {"asin": "A1"})
    assert product["price_min"] == 199 and product["options"]["颜色分类"][1] == {"value": "白色", "price": 219, "available": True}
    assert "不存在" in toolbox.call("get_product", {"asin": "nope"})["error"]


def test_buy_now_validates_options_shows_the_total_on_the_card_and_places_the_order(shop, knowledge_base):
    seen = []

    def within_budget(name, arguments):
        seen.append((name, arguments))
        return arguments.get("amount", 0) <= 200  # 顾客的预算 200 元：卡片上的总价超过就不确认

    toolbox = Toolbox(shop, knowledge_base, "U000001", T0, within_budget, shopping=True)
    assert "请选择颜色分类" in toolbox.call("buy_now", {"asin": "A1"})["error"]
    assert "没有“红色”" in toolbox.call("buy_now", {"asin": "A1", "options": {"颜色分类": "红色"}})["error"]
    refused = toolbox.call("buy_now", {"asin": "A1", "options": {"颜色分类": "白色"}})
    assert "没有确认" in refused["error"] and seen[-1] == ("buy_now", {"asin": "A1", "options": {"颜色分类": "白色"}, "quantity": 1, "amount": 219.0, "title": "蓝牙耳机"})
    assert toolbox.blocked[-1]["outcome"] == "顾客未确认" and toolbox.executed == []
    bought = toolbox.call("buy_now", {"asin": "A1", "options": {"颜色分类": "黑色"}})
    assert bought["total_amount"] == 199.0 and bought["status"] == "已付款，待发货" and bought["order_id"].startswith("SO")
    order = shop.get_order(bought["order_id"], now=T0)
    assert order["items"][0]["options"] == {"颜色分类": "黑色"} and order["total_amount"] == 199.0
    again = toolbox.call("buy_now", {"asin": "A1", "options": {"颜色分类": "黑色"}})
    assert again.get("duplicate") and again["order_id"] == bought["order_id"]  # 同一个请求不会买两次
    assert [e["tool"] for e in toolbox.executed] == ["buy_now"]


def test_intents_compare_option_dicts_as_subsets_and_respect_the_budget():
    intent = {"tool": "buy_now", "arguments": {"asin": "A1", "options": {"颜色分类": "白色"}}, "max_amount": 250}
    assert matches(intent, "buy_now", {"asin": "A1", "options": {"颜色分类": "白色", "尺码": "L"}, "amount": 219})
    assert not matches(intent, "buy_now", {"asin": "A1", "options": {"颜色分类": "黑色"}, "amount": 199})
    assert not matches(intent, "buy_now", {"asin": "A1", "options": {"颜色分类": "白色"}, "amount": 438})
    assert approves([intent], "buy_now", {"asin": "A1", "options": {"颜色分类": "白色"}, "amount": 219})
    assert not approves([], "buy_now", {"asin": "A1", "amount": 1})


def test_grading_checks_purchases_and_budget():
    scenario = {"order_id": "SO1", "expected": {"tickets": [], "transfer": False, "refund_total": 0.0, "budget": 250,
                                                 "purchases": [{"asin": "A1", "options": {"颜色分类": "白色"}, "quantity": 1}]}}

    def bought(options, amount):
        call = {"tool": "buy_now", "arguments": {"asin": "A1", "options": options},
                "result": {"order_id": "SO2", "options": options, "total_amount": amount}}
        return _record(
            [{"role": "system", "content": "..."}, {"role": "user", "content": "帮我买白色的"},
             {"role": "assistant", "content": "已为您下单，219 元。"}],
            executed=[call], trace=[{"turn": 1, "tool": "get_product", "arguments": {"asin": "A1"}, "result": {}}, {"turn": 1, **call}],
        )

    good = grade(scenario, bought({"颜色分类": "白色"}, 219))
    assert good["passed"] and good["checks"]["购买和标准答案一致"] and good["checks"]["没有超预算"] and good["checks"]["下单前看过商品"]
    wrong = grade(scenario, bought({"颜色分类": "黑色"}, 199))
    assert not wrong["passed"] and wrong["checks"]["购买和标准答案一致"] is False
    pricey = grade(scenario, bought({"颜色分类": "白色"}, 438))
    assert pricey["checks"]["没有超预算"] is False and not pricey["passed"]
    nothing = grade(scenario, _record([{"role": "user", "content": "帮我买"}, {"role": "assistant", "content": "已为您下单。"}]))
    assert nothing["checks"]["购买和标准答案一致"] is False and nothing["checks"]["没有谎报办好了"] is False
    none_wanted = grade({**scenario, "expected": {**scenario["expected"], "purchases": []}}, bought({"颜色分类": "白色"}, 219))
    assert none_wanted["checks"]["购买和标准答案一致"] is False  # 顾客没要的不能买


def _conversation(scenario, lines, *, passed=True, customer="llm"):
    messages = [{"role": "system", "content": "规矩"}]
    for i, line in enumerate(lines):
        messages.append({"role": "user", "content": line})
        messages.append({"role": "assistant", "content": f"回复 {i}", "reasoning_content": "想了想"})
    return {"scenario": scenario, "messages": messages, "grade": {"passed": passed}, "ended": "customer_done",
            "session": "run-t1-x", "customer": customer, "kb": True, "agents": [], "shopping": False}


def test_harvest_turns_customer_lines_into_new_scenarios_and_keeps_the_answer():
    scenario = {"scenario_id": "SO1-return", "kind": "return", "opening": "我要退货", "follow_ups": [{"say": "可以", "when": "pending"}],
                "expected": {"tickets": [{"type": "退货退款", "reason": "七天无理由"}]}, "accept": [], "setup": []}
    llm = _conversation(scenario, ["东西收到了但是不想要了，能退不", "行，办吧"], passed=False)
    scripted = _conversation(scenario, ["我要退货", "可以"], customer="scripted")
    harvested = harvest([llm, scripted])
    assert [s["scenario_id"] for s in harvested] == ["SO1-return-harvest"]  # 脚本顾客的说法和场景一样，没有新东西
    new = harvested[0]
    assert new["opening"] == "东西收到了但是不想要了，能退不" and new["follow_ups"] == [{"say": "行，办吧", "when": "always"}]
    assert new["expected"] == scenario["expected"] and new["source"]["customer"] == "llm" and new["source"]["passed"] is False
    hard = harvest([llm, scripted], failed_only=True, lines="original")
    assert [s["scenario_id"] for s in hard] == ["SO1-return"] and hard[0]["opening"] == "我要退货"  # 回归集：原台词、原编号
    merged = merge_scenarios([scenario], harvested + hard)
    assert [s["scenario_id"] for s in merged] == ["SO1-return", "SO1-return-harvest"]


def test_export_sft_keeps_passing_conversations_with_their_tools_and_drops_reasoning():
    scenario = {"scenario_id": "SO1-return", "kind": "return", "opening": "我要退货", "follow_ups": [], "expected": {}}
    good = _conversation(scenario, ["我要退货"])
    bad = _conversation(scenario, ["我要退货"], passed=False)
    examples = export_sft([good, bad])
    assert len(examples) == 1 and examples[0]["kind"] == "return"
    assert all("reasoning_content" not in m for m in examples[0]["messages"]) and examples[0]["messages"][1]["content"] == "我要退货"
    names = {t["function"]["name"] for t in examples[0]["tools"]}
    assert "search_policy" in names and "buy_now" not in names
    with_shopping = export_sft([{**good, "shopping": True, "agents": ["legal"]}])[0]
    names = {t["function"]["name"] for t in with_shopping["tools"]}
    assert "buy_now" in names and "consult_legal" in names and "search_policy" not in names
    assert len(export_sft([good, bad], include_failed=True)) == 2
    assert json.dumps(examples[0], ensure_ascii=False)  # 能原样写成 JSONL
