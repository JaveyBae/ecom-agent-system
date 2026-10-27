"""生成模拟商城的数据：导入商品和用户，按随机种子生成历史订单、物流和售后。

订单分布在“当前时间”之前的若干天里。每个订单的物流情形、顾客会不会申请售后、什么时候
寄回，都由随机种子决定。顾客的动作走和 agent 一样的 Shop 接口，所以生成的数据满足同样的
规则检查；动作时间都早于“当前时间”，之后的进度（例如还没到的退款）已经排好但查不到。
"""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import datetime, timedelta, timezone

from ecom_agent_system.shop import logistics
from ecom_agent_system.shop.catalog import import_products, import_users, variant_price
from ecom_agent_system.shop.db import create_schema, fmt, parse, set_meta
from ecom_agent_system.shop.service import Shop, ShopError

# 按收货区域抽样，保证偏远地区等少数情形也有足够的订单
ZONE_WEIGHTS = {"同城": 0.1, "江浙沪": 0.3, "一般": 0.5, "偏远": 0.1}
PRICE_RANGE = (1, 20000)  # 排除 0 元和天价的异常商品
# 签收后申请售后的概率：七天无理由退货、质量问题退货、质量问题换货
NO_REASON_RETURN_RATE = 0.12
QUALITY_RETURN_RATE = 0.05
QUALITY_EXCHANGE_RATE = 0.03
INTERVENTION_RATE = 0.75  # 被商家拒绝后申请平台介入的比例
# 顾客说明问题时的说法（P-QA-05）
EVIDENCE = {
    "质量问题": ("收到时外壳有裂痕", "通电后无法开机", "表面有明显划痕", "按键失灵，没有反应", "做工有瑕疵，开线了"),
    "运输损毁": ("外包装破损，里面的商品也压坏了",),
    "生鲜变质": ("收到时已经腐烂变质，有异味",),
}


def build_world(conn, items, *, rules, categories, seed, orders, now, days):
    create_schema(conn)
    products = import_products(conn, items, categories, seed)
    users = import_users(conn, items, seed)
    shop = Shop(conn, rules, categories, seed)
    rng = random.Random(f"{seed}:world")
    order_ids = generate_orders(shop, rng, orders, now - timedelta(days=days), now - timedelta(hours=1))
    actions = Counter()
    for order_id in order_ids:
        simulate_customer(shop, order_id, now, random.Random(f"{seed}:customer:{order_id}"), actions)
    for key, value in {
        "world_now": fmt(now),
        "seed": seed,
        "days": days,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }.items():
        set_meta(conn, key, value)
    conn.commit()
    return {"products": products, "users": users, "orders": len(order_ids), "customer_actions": dict(actions)}


def _catalog(conn):
    rows = conn.execute("SELECT asin, options, price_min, price_max FROM products").fetchall()
    return [
        (row["asin"], json.loads(row["options"]), tuple(p for p in (row["price_min"], row["price_max"]) if p is not None))
        for row in rows
    ]


def _pick_item(rng, catalog):
    """随机挑一件价格确定、在合理范围内的商品和规格。"""
    for _ in range(200):
        asin, options, base = rng.choice(catalog)
        selected = {axis: rng.choice([v for v in values if v["available"]] or values)["value"] for axis, values in options.items()}
        price = variant_price(options, selected, base)
        if price is not None and PRICE_RANGE[0] <= price <= PRICE_RANGE[1]:
            return asin, selected
    raise RuntimeError("找不到价格确定的商品")


def generate_orders(shop, rng, count, start, end):
    users = {
        zone: [row["user_id"] for row in shop.conn.execute("SELECT user_id FROM users WHERE zone = ?", (zone,))]
        for zone in ZONE_WEIGHTS
    }
    zones = [zone for zone in ZONE_WEIGHTS if users[zone]]
    catalog = _catalog(shop.conn)
    moments = sorted((start + (end - start) * rng.random()).replace(microsecond=0) for _ in range(count))
    order_ids = []
    for moment in moments:
        zone = rng.choices(zones, weights=[ZONE_WEIGHTS[zone] for zone in zones])[0]
        while True:
            asin, selected = _pick_item(rng, catalog)
            try:
                order_ids.append(
                    shop.place_order(
                        rng.choice(users[zone]), asin, selected, now=moment,
                        quantity=1 if rng.random() < 0.9 else 2, insured=rng.random() < 0.3,
                    )
                )
                break
            except ShopError:
                continue
    return order_ids


def _timeline(conn, order_id):
    shipment = conn.execute("SELECT * FROM shipments WHERE order_id = ? AND direction = '发货'", (order_id,)).fetchone()
    events = conn.execute(
        "SELECT time, code FROM shipment_events WHERE tracking_no = ? ORDER BY time, id", (shipment["tracking_no"],)
    ).fetchall()
    return shipment, [(parse(event["time"]), event["code"]) for event in events]


def _first_event(conn, ticket_id, status):
    row = conn.execute(
        "SELECT time FROM after_sales_events WHERE ticket_id = ? AND status = ? ORDER BY time LIMIT 1", (ticket_id, status)
    ).fetchone()
    return parse(row["time"]) if row else None


def simulate_customer(shop, order_id, now, rng, actions, no_reason_rate=NO_REASON_RETURN_RATE):
    """按订单的物流情形模拟顾客的售后动作，动作时间都早于 now。

    no_reason_rate 是签收后申请七天无理由退货的概率。购物 agent 买的单按买得对不对调整这个概率，
    见 purchases.NO_REASON_RETURN_RATE。
    """

    def attempt(label, moment, action, *args, **kwargs):
        if moment is None or moment >= now:
            return None
        try:
            result = action(*args, now=moment.replace(microsecond=0), **kwargs)
        except ShopError:
            actions[f"{label}（不符合条件，未办理）"] += 1
            return None
        actions[label] += 1
        return result

    order = shop.conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    item = shop.conn.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchone()
    shipment, events = _timeline(shop.conn, order_id)
    scenario = shipment["scenario"]
    deadline = parse(order["created_at"]) + timedelta(hours=shop.rules.get("P-SHIP-01", "ship_hours"))

    if scenario in ("延迟发货", "虚假发货"):
        picked = next(moment for moment, code in events if code == "已揽收")
        cancel_from = deadline + timedelta(hours=shop.rules.get("P-SHIP-02", "delay_cancel_hours"))
        if picked > cancel_from and rng.random() < 0.4:
            if attempt("取消订单", cancel_from + timedelta(hours=rng.uniform(1, 12)), shop.cancel_order, order_id):
                return
        if rng.random() < 0.6:
            attempt("延迟发货补偿", max(deadline, picked) + timedelta(hours=rng.uniform(1, 24)),
                    shop.create_after_sales, order_id, "延迟发货补偿", "延迟发货")

    if scenario in ("物流停滞", "丢件"):
        gaps = [(later - earlier, earlier) for (earlier, _), (later, _) in zip(events, events[1:])]
        stalled_from = events[-1][0] if scenario == "丢件" else max(gaps)[1]
        noticed = stalled_from + timedelta(days=logistics.stall_days(shop.rules, order["zone"]), hours=rng.uniform(2, 48))
        if rng.random() < 0.7 and attempt("仅退款（物流异常）", noticed, shop.create_after_sales, order_id, "仅退款", "物流异常"):
            return
    if scenario == "破损拒收":
        if rng.random() < 0.8:
            attempt("仅退款（运输损毁）", events[-1][0] + timedelta(hours=rng.uniform(1, 24)),
                    shop.create_after_sales, order_id, "仅退款", "运输损毁", evidence=rng.choice(EVIDENCE["运输损毁"]))
        return

    delivered = next((moment for moment, code in events if code in logistics.DELIVERED), None)
    if delivered is None:
        return
    if item["policy_class"] == "鲜活易腐":
        if rng.random() < 0.3:
            attempt("仅退款（生鲜变质）", delivered + timedelta(hours=rng.uniform(1, 30)),
                    shop.create_after_sales, order_id, "仅退款", "生鲜变质", evidence=rng.choice(EVIDENCE["生鲜变质"]))
        return
    roll = rng.random()
    if roll < no_reason_rate:
        kind, reason = "退货退款", "七天无理由"
    elif roll < no_reason_rate + QUALITY_RETURN_RATE:
        kind, reason = "退货退款", "质量问题"
    elif roll < no_reason_rate + QUALITY_RETURN_RATE + QUALITY_EXCHANGE_RATE:
        kind, reason = "换货", "质量问题"
    else:
        return
    applied = delivered + timedelta(hours=rng.uniform(2, 24 * 6))
    evidence = rng.choice(EVIDENCE[reason]) if reason in EVIDENCE else None
    ticket = attempt(f"{kind}（{reason}）", applied, shop.create_after_sales, order_id, kind, reason,
                     opened=rng.random() < 0.5, evidence=evidence)
    if ticket is None:
        return
    if rng.random() < 0.05:
        attempt("撤销售后", applied + timedelta(minutes=rng.randint(10, 600)), shop.cancel_after_sales, ticket["ticket_id"])
        return
    rejected = _first_event(shop.conn, ticket["ticket_id"], "商家拒绝")
    if rejected and rng.random() < INTERVENTION_RATE:
        attempt("申请平台介入", rejected + timedelta(hours=rng.uniform(2, 48)), shop.request_intervention,
                ticket["ticket_id"], "我是正常申请售后，商家拒绝没有道理")
    agreed = _first_event(shop.conn, ticket["ticket_id"], "待买家退货")
    if agreed and rng.random() < 0.85:
        attempt("寄回商品", agreed + timedelta(hours=rng.uniform(2, 72)), shop.submit_return_shipment, ticket["ticket_id"])
