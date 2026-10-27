"""控制台的接口：运行列表、总览、购物回放、售后对话、订单（换时刻、模拟器视角）和售后判断。"""

import json
import shutil
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from ecom_agent_system.agent.bridge import assistant_message
from ecom_agent_system.agent.scenarios import run_scenarios
from ecom_agent_system.console import Runs, create_app
from ecom_agent_system.shop import logistics
from ecom_agent_system.shop.catalog import import_products, import_users
from ecom_agent_system.shop.db import connect, create_schema, fmt, parse, set_meta
from ecom_agent_system.shop.purchases import Buyers, record_purchase
from ecom_agent_system.shop.rules import PlatformRules
from ecom_agent_system.shop.service import Shop

NOW = datetime(2026, 10, 24, 12, 0, 0)
STEPS = [
    {"step_index": 0, "tool_name": "search_products", "parameters": {"query": "蓝牙耳机"}, "done": False,
     "result": {"observation_state": {"page_type": "search_results", "query": "蓝牙耳机", "total_results": 1, "page": 1, "total_pages": 1,
                                      "products": [{"rank": 1, "asin": "A1", "price": "199.0 to 219.0", "brand": "测试店", "title": "蓝牙耳机"}]}}},
    {"step_index": 1, "tool_name": "buy_now", "parameters": {}, "done": True, "result": {"observation_state": {"page_type": "terminal"}}},
]


def _terminal():
    return {
        "done": True, "reward": 1.0, "termination_reason": "gold_purchase", "reward_valid": True,
        "purchase": {"asin": "A1", "name": "蓝牙耳机", "options": {"颜色分类": "白色"}, "price": 219.0},
        "reward_detail": {"target_asin_match": True, "hard_gates": {"budget": {"status": "pass", "required": 300.0, "actual": 219.0}}},
        "goal": {"asin": "A1", "name": "蓝牙耳机", "goal_options": {"颜色分类": "白色"}, "user_persona": {}},
    }


@pytest.fixture
def setup(tmp_path, categories, shop_items):
    run = tmp_path / "runs" / "demo"
    run.mkdir(parents=True)
    conn = connect(run / "shop.db")
    create_schema(conn)
    import_products(conn, shop_items, categories, seed=1)
    import_users(conn, shop_items, seed=1)
    set_meta(conn, "seed", 1)
    shop = Shop(conn, PlatformRules.load(), categories, seed=1)
    order_id = record_purchase(shop, Buyers(conn), _terminal(), now=NOW, label="claude", task_id=7, scenario="正常")
    delivered = parse(conn.execute(
        "SELECT MIN(e.time) FROM shipment_events e JOIN shipments s USING (tracking_no) WHERE s.order_id = ? AND e.code IN (?, ?, ?, ?)",
        (order_id, *logistics.DELIVERED),
    ).fetchone()[0])
    ticket = shop.create_after_sales(order_id, "退货退款", "七天无理由", now=delivered + timedelta(days=1))
    set_meta(conn, "world_now", fmt(delivered + timedelta(days=2)))
    conn.commit()
    conn.close()
    (run / "shopping.jsonl").write_text(json.dumps({
        "trajectory_id": "t1", "task_id": 7, "status": "done", "final_reward": 1.0, "steps": STEPS, "blocked_tool_calls": [],
        "initial_result": {"instruction": "Instruction: 想买一副白色蓝牙耳机"}, "terminal_result": _terminal(),
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    scenario = {
        "scenario_id": f"{order_id}-return", "order_id": order_id, "kind": "return", "now": fmt(delivered + timedelta(days=1)),
        "opening": "耳机不想要了", "outcome": "gold_purchase", "follow_ups": [],
        "intent": {"tool": "create_after_sales", "arguments": {"order_id": order_id, "type": "退货退款", "reason": "七天无理由"}},
        "expected": {"tickets": [{"type": "退货退款", "reason": "七天无理由"}], "rule_ids": ["P-RET-01"], "deadline": None, "legal_basis": []},
    }
    record = {
        "scenario": scenario, "ended": "customer_done",
        "messages": [{"role": "system", "content": "规矩"}, {"role": "user", "content": "耳机不想要了"}, {"role": "assistant", "content": "已为您申请退货（P-RET-01）"}],
        "trace": [], "executed": [{"tool": "create_after_sales", "arguments": scenario["intent"]["arguments"], "result": ticket}],
        "grade": {"passed": True, "checks": {"售后单和标准答案一致": True, "回复带了规则编号": True}},
    }
    (run / "aftersales.jsonl").write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"runs": tmp_path / "runs", "base_db": tmp_path / "missing.db", "order_id": order_id, "ticket": ticket, "delivered": delivered}


@pytest.fixture
def client(setup, knowledge_base):
    return TestClient(create_app(Runs(setup["runs"], setup["base_db"]), knowledge_base))


def test_runs_and_overview(client, setup):
    runs = client.get("/api/console/runs").json()
    assert runs["knowledge_base"] is True
    assert [(run["name"], run["labels"], run["shopping"], run["aftersales"]) for run in runs["runs"]] == [("demo", ["claude"], 1, 1)]
    overview = client.get("/api/console/overview", params={"run": "demo"}).json()
    assert overview["shopping"] == {"tasks": 1, "outcomes": {"gold_purchase": 1}, "steps": 2}
    assert [order["order_id"] for order in overview["orders"]] == [setup["order_id"]]
    assert overview["logistics"] == {"正常": 1} and overview["sales_amount"] == 219.0
    assert overview["aftersales"]["passed"] == 1 and overview["aftersales"]["submitted"] == 1
    assert overview["aftersales"]["checks"]["售后单和标准答案一致"] == [1, 1, True]  # 旧记录按当时的两项硬性要求算
    assert overview["aftersales"]["report"]["pass_rate"] == 1.0


def test_shopping_and_aftersales_records(client, setup):
    trajectories = client.get("/api/console/shopping", params={"run": "demo"}).json()["trajectories"]
    assert trajectories[0]["order_id"] == setup["order_id"] and trajectories[0]["instruction"] == "想买一副白色蓝牙耳机"
    detail = client.get("/api/console/shopping/0", params={"run": "demo"}).json()
    assert [step["tool"] for step in detail["steps"]] == ["search_products", "buy_now"]
    assert detail["result"]["gates"] == [{"name": "budget", "status": "pass", "required": 300.0, "actual": 219.0}]
    assert client.get("/api/console/shopping/5", params={"run": "demo"}).status_code == 404
    conversation = client.get("/api/console/aftersales/0", params={"run": "demo"}).json()
    assert conversation["passed"] is True and conversation["executed"][0]["result"]["ticket_id"] == setup["ticket"]["ticket_id"]


def test_order_can_be_viewed_at_any_moment(client, setup):
    order_id = setup["order_id"]
    listed = client.get("/api/console/orders", params={"run": "demo"}).json()["orders"]
    assert [order["order_id"] for order in listed] == [order_id] and listed[0]["agent"]["task_id"] == 7
    at_purchase = client.get(f"/api/console/orders/{order_id}", params={"run": "demo", "now": fmt(NOW), "truth": True}).json()
    assert at_purchase["order"]["status"] == "待发货" and at_purchase["tickets"] == []
    truth = at_purchase["truth"]["shipments"][0]
    assert truth["scenario"] == "正常" and all(event["future"] for event in truth["events"])  # 下单那一刻物流都还没发生
    later = client.get(f"/api/console/orders/{order_id}", params={"run": "demo"}).json()  # 默认库里的当前时间
    assert later["order"]["status"] == "已签收" and [t["ticket_id"] for t in later["tickets"]] == [setup["ticket"]["ticket_id"]]
    assert "truth" not in later and later["agent"]["failed_gates"] == []
    by_user = client.get("/api/console/orders", params={"run": "demo", "q": later["user"]["user_id"]}).json()["orders"]
    assert order_id in [order["order_id"] for order in by_user]


def test_policy_check_and_errors(client, setup):
    order_id = setup["order_id"]
    check = client.get("/api/console/check", params={
        "run": "demo", "order_id": order_id, "type": "退货退款", "reason": "七天无理由", "now": fmt(setup["delivered"] + timedelta(hours=5)),
    }).json()
    assert check["allowed"] is True and check["rule_ids"] and check["deadline"]
    assert client.get("/api/console/check", params={"run": "demo", "order_id": order_id, "type": "换货", "reason": "七天无理由"}).status_code == 422
    assert client.get("/api/console/orders/SO000", params={"run": "demo"}).status_code == 404
    assert client.get("/api/console/overview", params={"run": "../etc"}).status_code == 404
    assert client.get(f"/api/console/orders/{order_id}", params={"run": "demo", "now": "下周"}).status_code == 422


def test_pages_and_knowledge_routes_are_served(client):
    assert "ShopSim Agent 控制台" in client.get("/").text
    assert "售后知识库检索台" in client.get("/kb/").text
    assert client.get("/api/info").json()["chunks"] == 3


def test_runs_on_database_copies_show_their_audit_log(client, setup, knowledge_base):
    """ecom-agent aftersales 默认在库的副本上跑：对话详情的审计日志、订单页的工单都从副本里读，基础库不变。"""
    suite = setup["runs"] / "suite"
    suite.mkdir()
    shutil.copyfile(setup["runs"] / "demo" / "shop.db", suite / "shop.db")
    shutil.copyfile(suite / "shop.db", suite / "aftersales-ref.t1.db")
    conn = connect(suite / "aftersales-ref.t1.db")
    order_id, now = setup["order_id"], setup["delivered"] + timedelta(days=2)
    user_id = conn.execute("SELECT user_id FROM orders WHERE order_id = ?", (order_id,)).fetchone()[0]
    scenario = {"scenario_id": "s1", "kind": "escalate_request", "category": "转人工", "order_id": order_id, "user_id": user_id,
                "now": fmt(now), "opening": "转人工", "follow_ups": [], "accept": [], "expected": {"tickets": [], "transfer": True}}

    class Model:
        def complete(self, messages, tools):
            return assistant_message({"tool": "transfer_to_human", "arguments": {"summary": f"订单 {order_id} 要人工", "order_id": order_id}}, 1)

    run_scenarios(Shop(conn, PlatformRules.load(), seed=1), knowledge_base, [scenario], Model(), suite / "aftersales-ref.jsonl",
                  label="ref", db="aftersales-ref.t1.db")
    conn.close()
    runs = {run["name"]: run for run in client.get("/api/console/runs").json()["runs"]}
    assert runs["suite"]["aftersales_files"] == {"aftersales-ref.jsonl": 1}
    listing = client.get("/api/console/aftersales", params={"run": "suite"}).json()
    assert listing["file"] == "aftersales-ref.jsonl" and listing["report"]["pass_rate"] == 1.0
    detail = client.get("/api/console/aftersales/0", params={"run": "suite", "file": "aftersales-ref.jsonl"}).json()
    assert [(a["tool"], a["outcome"]) for a in detail["audit"]] == [("transfer_to_human", "已执行")]
    assert detail["db"] == "aftersales-ref.t1.db" and detail["category"] == "转人工"
    after = client.get(f"/api/console/orders/{order_id}", params={"run": "suite", "db": "aftersales-ref.t1.db", "now": fmt(now + timedelta(hours=1))}).json()
    assert after["work_orders"][0]["kind"] == "转人工" and after["audit"][0]["tool"] == "transfer_to_human"
    assert client.get(f"/api/console/orders/{order_id}", params={"run": "suite"}).json()["work_orders"] == []  # 基础库没被改
    assert client.get(f"/api/console/orders/{order_id}", params={"run": "suite", "db": "../demo/shop.db"}).status_code == 404
    assert client.get("/api/console/aftersales", params={"run": "suite", "file": "../demo/aftersales.jsonl"}).status_code == 404
