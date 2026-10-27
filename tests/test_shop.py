import copy
import random
from datetime import datetime, timedelta

import pytest

from ecom_agent_system.shop import logistics
from ecom_agent_system.shop.catalog import variant_price, zone_of
from ecom_agent_system.shop.db import parse
from ecom_agent_system.shop.policy import ItemContext, Policy
from ecom_agent_system.shop.rules import PlatformRules
from ecom_agent_system.shop.service import Shop, ShopError

T0 = datetime(2026, 10, 1, 10, 0, 0)
RULES = PlatformRules.load()


def _event_time(shop, order_id, codes, direction="发货"):
    marks = ", ".join("?" for _ in codes)
    row = shop.conn.execute(
        f"SELECT e.time FROM shipment_events e JOIN shipments s USING (tracking_no)"
        f" WHERE s.order_id = ? AND s.direction = ? AND e.code IN ({marks}) ORDER BY e.time LIMIT 1",
        (order_id, direction, *codes),
    ).fetchone()
    return parse(row["time"])


def _last_event_time(shop, order_id):
    row = shop.conn.execute(
        "SELECT MAX(e.time) AS time FROM shipment_events e JOIN shipments s USING (tracking_no) WHERE s.order_id = ?",
        (order_id,),
    ).fetchone()
    return parse(row["time"])


# ---------------------------------------------------------------- 导入


def test_import_dedupes_personas_and_classifies_products(shop):
    users = {row["user_id"]: (row["tier"], row["zone"]) for row in shop.conn.execute("SELECT * FROM users")}
    assert users == {
        "U000001": ("普通", "同城"),
        "U000002": ("黄金", "偏远"),
        "U000003": ("铂金", "一般"),
        "U000004": ("钻石", "江浙沪"),
    }
    products = {row["asin"]: (row["policy_class"], row["no_return_marked"]) for row in shop.conn.execute("SELECT * FROM products")}
    assert products == {
        "A1": (None, 0),
        "A2": ("食品", 1),
        "A3": ("定作", 1),
        "A4": ("鲜活易腐", 1),
        "A5": ("激活类电子产品", 1),
    }


def test_zone_and_category_prefix_matching(categories, shop_items):
    assert zone_of("浙江省", "杭州市") == "同城"
    assert zone_of("上海市", "上海市") == "江浙沪"
    assert zone_of("西藏自治区", "拉萨市") == "偏远"
    assert zone_of("广东省", "广州市") == "一般"
    assert categories.classify("鲜花速递/花卉仿真/绿植园艺›种子/种苗/球根›多肉植物") == "鲜活易腐"
    assert categories.classify("鲜花速递/花卉仿真/绿植园艺›园艺用品") is None
    unused = categories.unused_keys(item["category"] for item in shop_items)  # 传入生成器也要能用
    assert "全屋定制" not in unused and "影音电器" not in unused and "茶" in unused


def test_variant_price_follows_shopsimulator_rules():
    sizes = {"尺码": [{"value": "S", "price": 10, "available": True}, {"value": "M", "price": 10, "available": True}]}
    colors = {"颜色": [{"value": "黑", "price": 10, "available": True}, {"value": "白", "price": 12, "available": True}]}
    assert variant_price({**colors, **sizes}, {"颜色": "白", "尺码": "S"}) == 12
    assert variant_price(sizes, {"尺码": "M"}) == 10
    two_varying = {**colors, "尺码": [{"value": "S", "price": 10, "available": True}, {"value": "M", "price": 15, "available": True}]}
    assert variant_price(two_varying, {"颜色": "白", "尺码": "M"}) is None
    assert variant_price({}, {}, (99.0,)) == 99.0
    assert variant_price({}, {}, (99.0, 120.0)) is None


# ---------------------------------------------------------------- 物流


def test_outbound_timeline_is_reproducible_and_ordered():
    plan = dict(paid_at=T0, zone="一般", city="深圳市", district="南山区", receiver="张三")
    first = logistics.plan_outbound(random.Random(7), RULES, scenario="正常", **plan)
    second = logistics.plan_outbound(random.Random(7), RULES, scenario="正常", **plan)
    assert first == second
    shipment, events = first
    times = [moment for moment, *_ in events]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert times[0] > T0 and events[-1][1] == "已签收"
    assert shipment["promised_by"] == (shipment["created_at"].date() + timedelta(days=5)).isoformat()
    for scenario, last_code in (("丢件", "运输中"), ("破损拒收", "拒收"), ("未经同意放驿站", "已放驿站")):
        _, events = logistics.plan_outbound(random.Random(7), RULES, scenario=scenario, **plan)
        assert events[-1][1] == last_code, scenario


def test_stall_is_reported_after_threshold_days():
    for zone, days in (("一般", 7), ("偏远", 12)):
        shipment, events = logistics.plan_outbound(
            random.Random(3), RULES, paid_at=T0, zone=zone, city="某市", district=None, receiver="李四", scenario="丢件"
        )
        rows = [{"time": moment.strftime("%Y-%m-%d %H:%M:%S"), "code": code, "location": location, "description": text}
                for moment, code, location, text in events]
        last = parse(rows[-1]["time"])
        view = {**shipment, "direction": "发货"}
        assert logistics.summarize(RULES, view, rows, last + timedelta(days=days - 1))["status"] == "运输中"
        assert logistics.summarize(RULES, view, rows, last + timedelta(days=days))["status"] == "物流异常"


# ---------------------------------------------------------------- 政策引擎


def _ctx(**changes):
    base = dict(tier="普通", zone="一般", insured=False, amount=100.0, delivered_at=T0)
    return ItemContext(**{**base, **changes})


def test_return_window_depends_on_member_tier():
    policy = Policy(RULES)
    for tier, days in (("普通", 7), ("黄金", 7), ("铂金", 15), ("钻石", 30)):
        last_day = datetime.combine(T0.date() + timedelta(days=days), datetime.max.time()).replace(microsecond=0)
        assert policy.no_reason_return(_ctx(tier=tier), last_day).allowed, tier
        assert not policy.no_reason_return(_ctx(tier=tier), last_day + timedelta(seconds=1)).allowed, tier


def test_return_fee_and_fast_refund():
    policy = Policy(RULES)
    now = T0 + timedelta(days=1)
    cases = [
        (_ctx(), 10, "消费者", False),
        (_ctx(zone="偏远"), 25, "消费者", False),
        (_ctx(insured=True), 0, "退货运费险", False),
        (_ctx(tier="黄金"), 0, "平台（会员权益）", False),
        (_ctx(tier="铂金"), 0, "平台（会员权益）", True),
    ]
    for ctx, fee, payer, fast in cases:
        decision = policy.no_reason_return(ctx, now)
        assert (decision.return_fee, decision.fee_payer, decision.fast_refund) == (fee, payer, fast)


def test_category_exceptions_need_marking_or_opening():
    policy = Policy(RULES)
    now = T0 + timedelta(days=1)
    assert not policy.no_reason_return(_ctx(policy_class="定作", policy_kind="法定不适用"), now).allowed
    food = dict(policy_class="食品", policy_kind="确认后不适用")
    assert not policy.no_reason_return(_ctx(**food, confirmed=True), now).allowed
    assert policy.no_reason_return(_ctx(**food, confirmed=False), now).allowed  # 商家没标注不能拒绝
    game = _ctx(policy_class="数字化商品", policy_kind="拆封后不适用")
    assert not policy.no_reason_return(game, now, opened=True).allowed
    assert policy.no_reason_return(game, now, opened=False).allowed
    device = _ctx(policy_class="激活类电子产品", policy_kind="激活后不适用", confirmed=True)
    assert not policy.no_reason_return(device, now, activated=True).allowed
    assert policy.no_reason_return(device, now, activated=False).allowed
    assert "P-RET-03" in policy.no_reason_return(_ctx(), now, opened=True).rule_ids  # 拆封查验不影响退货


def test_quality_issue_options_shrink_over_time():
    policy = Policy(RULES)
    at_day = lambda day: T0 + timedelta(days=day)  # noqa: E731
    assert policy.quality_issue(_ctx(), at_day(7), "退货").allowed
    assert not policy.quality_issue(_ctx(), at_day(8), "退货").allowed
    assert policy.quality_issue(_ctx(), at_day(8), "换货").allowed
    assert policy.quality_issue(_ctx(), at_day(16), "换货").options == ["维修"]


def test_shipping_delay_compensation_and_cancellation():
    policy = Policy(RULES)
    assert [policy.delay_compensation(amount) for amount in (50, 300, 1000)] == [5, 15, 30]
    # 填写单号后 24 小时内揽收，以填写时间为发货时间；超过 24 小时才揽收，以揽收时间为准
    assert policy.ship_time(T0, T0 + timedelta(hours=3), T0 + timedelta(hours=5)) == T0
    assert policy.ship_time(T0, T0 + timedelta(hours=30), T0 + timedelta(hours=31)) == T0 + timedelta(hours=30)
    assert policy.ship_time(T0, None, T0 + timedelta(hours=25)) is None
    deadline = T0 + timedelta(hours=48)
    assert not policy.shipping_delay(T0, None, deadline, 100).allowed
    late = policy.shipping_delay(T0, None, deadline + timedelta(hours=72, minutes=1), 100)
    assert late.allowed and "取消订单" in late.options and late.refund_amount == 5


def test_rule_parameters_drive_decisions():
    rules = copy.deepcopy(RULES)
    rules.params["P-MEM-01"]["days_platinum"] = 20
    assert Policy(rules).return_days("铂金") == (20, "P-MEM-01")


# ---------------------------------------------------------------- 业务流程


def test_order_validation_and_cancel_before_shipping(shop):
    with pytest.raises(ShopError, match="请选择颜色分类"):
        shop.place_order("U000001", "A1", {}, now=T0)
    with pytest.raises(ShopError, match="没有“红色”"):
        shop.place_order("U000001", "A1", {"颜色分类": "红色"}, now=T0)
    order_id = shop.place_order("U000001", "A1", {"颜色分类": "白色"}, now=T0, scenario="正常")
    early = T0 + timedelta(minutes=30)  # 最早付款 1 小时后才发货
    order = shop.get_order(order_id, now=early)
    assert order["status"] == "待发货" and order["total_amount"] == 219
    result = shop.cancel_order(order_id, now=early)
    assert result["refund_amount"] == 219 and result["compensation"] is None
    assert shop.get_order(order_id, now=T0 + timedelta(days=10))["status"] == "已取消"
    assert shop.track_shipment(order_id, now=T0 + timedelta(days=10))["shipments"] == []
    with pytest.raises(ShopError, match="已经取消"):
        shop.cancel_order(order_id, now=early + timedelta(minutes=1))


def test_platinum_member_gets_fast_refund(shop):
    order_id = shop.place_order("U000003", "A1", {"颜色分类": "黑色"}, now=T0, scenario="正常")
    applied = _event_time(shop, order_id, logistics.DELIVERED) + timedelta(hours=2)
    check = shop.check_after_sales(order_id, "退货退款", "七天无理由", now=applied)
    assert check["allowed"] and check["fast_refund"] and check["return_fee"] == 0
    ticket = shop.create_after_sales(order_id, "退货退款", "七天无理由", now=applied)
    assert ticket["status"] == "待商家处理"
    with pytest.raises(ShopError, match="商家还没有同意"):
        shop.submit_return_shipment(ticket["ticket_id"], now=applied + timedelta(minutes=1))
    agreed = applied + timedelta(hours=48, minutes=1)  # 超过处理时限一定已同意（商家同意或系统自动同意）
    assert shop.get_after_sales(ticket["ticket_id"], now=agreed)["status"] == "待买家退货"
    assert shop.submit_return_shipment(ticket["ticket_id"], now=agreed)["status"] == "买家已退货"
    refunded = shop.get_after_sales(ticket["ticket_id"], now=agreed + timedelta(minutes=11))
    assert refunded["status"] == "退款成功" and "极速退款" in refunded["history"][-1]["note"]
    with pytest.raises(ShopError, match="已经退款"):
        shop.create_after_sales(order_id, "退货退款", "七天无理由", now=agreed + timedelta(hours=1))


def test_regular_member_is_refunded_after_merchant_receives_return(shop):
    order_id = shop.place_order("U000001", "A1", {"颜色分类": "黑色"}, now=T0, scenario="正常")
    applied = _event_time(shop, order_id, logistics.DELIVERED) + timedelta(hours=1)
    ticket = shop.create_after_sales(order_id, "退货退款", "七天无理由", now=applied, opened=True)
    assert (ticket["return_fee"], ticket["fee_payer"]) == (10, "消费者")
    sent = applied + timedelta(hours=49)
    shop.submit_return_shipment(ticket["ticket_id"], now=sent)
    received = _event_time(shop, order_id, ("已签收",), direction="退货")
    assert shop.get_after_sales(ticket["ticket_id"], now=received)["status"] == "商家已收货"
    final = shop.get_after_sales(ticket["ticket_id"], now=received + timedelta(days=3))
    assert final["status"] == "退款成功" and final["refund_amount"] == 199
    statuses = [event["status"] for event in final["history"]]
    assert statuses == ["待商家处理", "待买家退货", "买家已退货", "商家已收货", "退款成功"]


def test_marked_food_cannot_be_returned_without_reason(shop):
    order_id = shop.place_order("U000002", "A2", {}, now=T0, scenario="正常")
    applied = _event_time(shop, order_id, logistics.DELIVERED) + timedelta(hours=1)
    check = shop.check_after_sales(order_id, "退货退款", "七天无理由", now=applied)
    assert not check["allowed"] and check["rule_ids"] == ["P-RET-02"]
    with pytest.raises(ShopError) as error:
        shop.create_after_sales(order_id, "退货退款", "七天无理由", now=applied)
    assert error.value.decision.rule_ids == ["P-RET-02"]


def test_lost_parcel_can_be_refunded_once_logistics_stalls(shop):
    order_id = shop.place_order("U000003", "A1", {"颜色分类": "黑色"}, now=T0, scenario="丢件")
    last = _last_event_time(shop, order_id)
    with pytest.raises(ShopError, match="物流没有异常"):
        shop.create_after_sales(order_id, "仅退款", "物流异常", now=last + timedelta(days=3))
    stalled = last + timedelta(days=7, hours=1)
    assert shop.track_shipment(order_id, now=stalled)["status"] == "物流异常"
    ticket = shop.create_after_sales(order_id, "仅退款", "物流异常", now=stalled)
    final = shop.get_after_sales(ticket["ticket_id"], now=stalled + timedelta(hours=49))
    assert final["status"] == "退款成功" and final["refund_amount"] == 199


def test_late_shipping_compensation_is_paid_once(shop):
    order_id = shop.place_order("U000004", "A5", {}, now=T0, scenario="延迟发货")
    deadline = T0 + timedelta(hours=48)
    with pytest.raises(ShopError, match="没有超过发货时效"):
        shop.create_after_sales(order_id, "延迟发货补偿", "延迟发货", now=deadline - timedelta(hours=1))
    ticket = shop.create_after_sales(order_id, "延迟发货补偿", "延迟发货", now=deadline + timedelta(hours=1))
    assert ticket["refund_amount"] == 30  # 899 × 5% 超过上限 30 元
    assert shop.get_after_sales(ticket["ticket_id"], now=deadline + timedelta(hours=4))["status"] == "补偿已到账"
    with pytest.raises(ShopError, match="已经申请过"):
        shop.create_after_sales(order_id, "延迟发货补偿", "延迟发货", now=deadline + timedelta(hours=5))
