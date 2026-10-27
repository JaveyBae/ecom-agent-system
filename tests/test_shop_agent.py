"""购物 agent 的购买记成订单：用户画像匹配、规格写法、轨迹导入、顾客收货后的反应和订单网关。"""

import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from ecom_agent_system.shop.cli import resolve_now
from ecom_agent_system.shop.db import get_meta, parse, set_meta
from ecom_agent_system.shop.gateway import Gateway, create_app
from ecom_agent_system.shop.purchases import (
    Buyers,
    agent_orders,
    import_trajectories,
    purchase_options,
    react,
    record_purchase,
)

NOW = datetime(2026, 10, 24, 12, 0, 0)
ALWAYS_RETURN = {"wrong_purchase": 1.0}


def _persona(shop, user_id):
    return json.loads(shop.conn.execute("SELECT persona FROM users WHERE user_id = ?", (user_id,)).fetchone()["persona"])


def _result(asin, options, price, outcome, persona=None, failed=()):
    """ShopSimulator 的 interact 以购买结束时返回的内容（只留用得到的字段）。"""
    return {
        "done": True,
        "reward": {"gold_purchase": 1.0, "wrong_purchase": -0.85}.get(outcome, 0.2),
        "termination_reason": outcome,
        "reward_valid": True,
        "reward_detail": {"hard_gates": {gate: {"status": "fail" if gate in failed else "pass"} for gate in ("category", "budget")}},
        "purchase": {"asin": asin, "options": options, "price": price},
        "goal": {"user_persona": persona or {}, "instruction_text": "想买一副白色的蓝牙耳机"},
    }


def _trajectory(trajectory_id, task_id, result, initial_persona=None):
    return {
        "trajectory_id": trajectory_id,
        "task_id": task_id,
        "attempt_index": 0,
        "status": "done",
        "initial_result": {"user_persona": initial_persona or {}},
        "terminal_result": result,
    }


# ---------------------------------------------------------------- 买家和规格


def test_buyers_match_persona_regardless_of_key_order(shop):
    buyers = Buyers(shop.conn)
    persona = _persona(shop, "U000003")
    reordered = dict(reversed(list(persona.items())))  # ShopSimulator 会调整画像的字段顺序
    assert buyers.find(reordered, 8187) == ("U000003", True)
    fallback = buyers.find({}, 8187)
    assert fallback[1] is False
    assert Buyers(shop.conn).find({"会员等级": "不存在"}, 8187) == fallback  # 同一个任务总是分给同一个人


def test_purchase_options_restore_case_and_allow_partial_selection():
    options = {
        "Color": [{"value": "Black", "price": 10, "available": True}, {"value": "White", "price": 12, "available": True}],
        "尺码": [{"value": "S", "price": 10, "available": True}, {"value": "M", "price": 10, "available": True}],
        "版本": [{"value": "标准版", "price": 10, "available": True}],
    }
    assert purchase_options(options, {"color": "white"}) == {"Color": "White", "版本": "标准版"}
    assert purchase_options(options, {"颜色": "红色"}) == {"颜色": "红色", "版本": "标准版"}  # 对不上的照原样记


# ---------------------------------------------------------------- 记订单和顾客反应


def test_record_purchase_keeps_simulator_price_and_outcome(shop):
    result = _result("A1", {"颜色分类": "白色"}, 219.0, "wrong_purchase", persona=_persona(shop, "U000003"), failed=("category",))
    order_id = record_purchase(shop, Buyers(shop.conn), result, now=NOW, label="grpo", task_id=8187)
    order = shop.get_order(order_id, now=NOW)
    assert (order["user_id"], order["status"]) == ("U000003", "待发货")
    assert (order["items"][0]["options"], order["items"][0]["unit_price"]) == ({"颜色分类": "白色"}, 219.0)
    row = shop.conn.execute(
        "SELECT a.*, o.source FROM agent_purchases a JOIN orders o USING (order_id) WHERE order_id = ?", (order_id,)
    ).fetchone()
    assert (row["label"], row["task_id"], row["outcome"], row["persona_matched"], row["source"]) == (
        "grpo", 8187, "wrong_purchase", 1, "shop_agent"
    )
    assert json.loads(row["failed_gates"]) == ["category"]


def test_wrong_purchase_leads_to_return_unless_category_forbids(shop):
    buyers = Buyers(shop.conn)
    headphones = record_purchase(
        shop, buyers, _result("A1", {"颜色分类": "黑色"}, 199.0, "wrong_purchase"), now=NOW, label="grpo", task_id=1, scenario="正常"
    )
    noodles = record_purchase(
        shop, buyers, _result("A2", {}, 30.0, "wrong_purchase"), now=NOW, label="grpo", task_id=2, scenario="正常"
    )
    later = NOW + timedelta(days=30)
    assert react(shop, headphones, "wrong_purchase", later, ALWAYS_RETURN)["退货退款（七天无理由）"] == 1
    tickets = shop.get_order(headphones, now=later)["after_sales"]
    assert [(ticket["type"], ticket["reason"]) for ticket in tickets] == [("退货退款", "七天无理由")]
    # 商品页标注了不支持七天无理由退货的食品，买错了也退不了
    assert react(shop, noodles, "wrong_purchase", later, ALWAYS_RETURN) == {"退货退款（七天无理由）（不符合条件，未办理）": 1}
    assert shop.get_order(noodles, now=later)["after_sales"] == []
    report = agent_orders(shop, later)["grpo"]
    assert report["outcomes"] == {"wrong_purchase": 2}
    assert report["returns_by_outcome"]["wrong_purchase"]["orders"] == 2
    assert report["rejected_requests"] == {"退货退款（七天无理由）": 1}
    assert report["sales_amount"] == 229.0


def test_import_trajectories_backdates_purchases_and_is_idempotent(shop):
    rows = [
        # reset 时的画像是上一个任务残留的，要以 goal 里的为准
        _trajectory("t1", 11, _result("A5", {}, 899.0, "gold_purchase", persona=_persona(shop, "U000004")), _persona(shop, "U000001")),
        _trajectory("t2", 12, _result("A1", {"颜色分类": "白色"}, 219.0, "partial_alternative_purchase")),
        {"trajectory_id": "t3", "task_id": 13, "terminal_result": {"done": True, "termination_reason": "max_steps", "purchase": {}}},
        _trajectory("t4", 14, _result("Z9", {}, 10.0, "gold_purchase")),  # 商品不在库里
    ]
    start, end = NOW - timedelta(days=20), NOW - timedelta(hours=1)
    summary = import_trajectories(shop, rows, label="sft", start=start, end=end, react_until=NOW)
    assert (summary["imported"], summary["skipped"], summary["persona_matched"]) == (2, {"没有购买": 1, "记不进库": 1}, 1)
    assert summary["errors"] == [{"trajectory_id": "t4", "error": "商品 Z9 不在库里"}]
    assert summary["outcomes"] == {"gold_purchase": 1, "partial_alternative_purchase": 1}
    orders = shop.conn.execute(
        "SELECT a.trajectory_id, o.user_id, o.created_at FROM agent_purchases a JOIN orders o USING (order_id)"
    ).fetchall()
    assert all(start <= parse(row["created_at"]) < end for row in orders)
    assert {row["trajectory_id"]: row["user_id"] for row in orders}["t1"] == "U000004"
    again = import_trajectories(shop, rows, label="sft", start=start, end=end, react_until=NOW)
    assert (again["imported"], again["skipped"]) == (0, {"没有购买": 1, "已经导入过": 2, "记不进库": 1})


# ---------------------------------------------------------------- 订单网关


class FakeShopSimulator:
    """按 /api/shop_agent 的协议回话。reset 返回上一个任务残留的画像，Buy Now 返回购买和本任务的画像。"""

    def __init__(self, persona, stale_persona, asin="A1"):
        self.persona, self.stale_persona, self.asin = persona, stale_persona, asin

    def __call__(self, payload):
        action = payload["action"]
        if action == "reset":
            return {"result": {"env_idx": 3, "idx": payload["idx"], "instruction": "首页", "user_persona": self.stale_persona}}
        if action != "interact":
            return {"result": {"message": "Environment 3 has been released"}}
        if payload["response"] == "click[buy now]":
            result = _result(self.asin, {"颜色分类": "白色"}, 219.0, "gold_purchase", persona=self.persona)
            return {"result": {**result, "env_idx": 3, "idx": "slot-3-8187", "over": True}}
        if payload["response"] == "finish[no_suitable_product]":
            return {"result": {"done": True, "reward": -0.15, "termination_reason": "graceful_stop", "purchase": {}, "env_idx": 3}}
        return {"result": {"done": False, "reward": 0.0, "instruction": "搜索结果", "env_idx": 3}}


def _gateway(shop, **fake):
    set_meta(shop.conn, "world_now", "2026-10-24 12:00:00")
    upstream = FakeShopSimulator(_persona(shop, "U000004"), _persona(shop, "U000001"), **fake)
    gateway = Gateway(shop.conn, label="grpo", send=upstream, shop=shop)
    return gateway, TestClient(create_app(gateway))


def _post(client, **payload):
    return client.post("/api/shop_agent", json=payload)


def test_gateway_records_purchase_and_passes_responses_through(shop):
    gateway, client = _gateway(shop)
    assert _post(client, action="reset", idx=8187).json()["result"]["env_idx"] == 3
    search = _post(client, action="interact", env_idx=3, response="search[蓝牙耳机]")
    assert search.json()["result"]["done"] is False and "x-shop-order-id" not in search.headers
    buy = _post(client, action="interact", env_idx=3, response="click[buy now]")
    assert buy.json()["result"]["purchase"]["asin"] == "A1"  # 响应原样返回
    order_id = buy.headers["x-shop-order-id"]
    row = shop.conn.execute("SELECT label, task_id, outcome FROM agent_purchases WHERE order_id = ?", (order_id,)).fetchone()
    assert tuple(row) == ("grpo", 8187, "gold_purchase")
    assert shop.get_order(order_id, now=gateway.now())["user_id"] == "U000004"  # 用 goal 里的画像
    assert parse(get_meta(shop.conn, "world_now")) >= NOW
    _post(client, action="release_one", env_idx=3)
    assert gateway.leases == {}
    status = client.get("/api/gateway").json()
    assert (status["recorded"], status["latest_orders"], status["failures"]) == (1, [order_id], [])


def test_gateway_ignores_stops_and_survives_recording_errors(shop):
    gateway, client = _gateway(shop, asin="NOT-IN-DB")
    _post(client, action="reset", idx=8187)
    stop = _post(client, action="interact", env_idx=3, response="finish[no_suitable_product]")
    assert "x-shop-order-id" not in stop.headers and gateway.recorded == []
    buy = _post(client, action="interact", env_idx=3, response="click[buy now]")
    assert buy.status_code == 200 and buy.json()["result"]["done"] is True  # 记不下来也不影响 agent
    assert [failure["asin"] for failure in gateway.failures] == ["NOT-IN-DB"]


def test_gateway_reads_task_from_session_when_reset_was_missed(shop):
    gateway, client = _gateway(shop)
    order_id = _post(client, action="interact", env_idx=3, response="click[buy now]").headers["x-shop-order-id"]
    assert shop.conn.execute("SELECT task_id FROM agent_purchases WHERE order_id = ?", (order_id,)).fetchone()[0] == 8187


def test_now_accepts_offsets_from_world_clock():
    assert resolve_now("+3d", NOW) == NOW + timedelta(days=3)
    assert resolve_now("-12h", NOW) == NOW - timedelta(hours=12)
    assert resolve_now("2026-10-30 08:00", NOW) == datetime(2026, 10, 30, 8, 0)
    assert resolve_now(None, NOW) == NOW
