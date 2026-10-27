"""物流模拟：填写运单号时就按随机种子生成整条轨迹，查询时只返回不晚于当前时间的部分。

简化：所有商品都从杭州仓发货，退货也寄回杭州仓。送达天数按 P-SHIP-08 的区域时效，
物流停滞按 P-SHIP-07 判定。每张发货单有一个情形（scenario），决定它会不会延迟发货、
运输延误、停滞、丢件、未经同意放快递柜或驿站、被代签、破损拒收。情形存在 shipments
表里，相当于标准答案，不展示给 agent。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from ecom_agent_system.shop.db import parse

WAREHOUSE_CITY = "杭州市"
WAREHOUSE_DISTRICT = "余杭区"
CARRIERS = (
    ("中通快递", "ZT"),
    ("圆通速递", "YT"),
    ("韵达快递", "YD"),
    ("申通快递", "ST"),
    ("极兔速递", "JT"),
    ("顺丰速运", "SF"),
    ("京东快递", "JD"),
    ("邮政快递包裹", "YZ"),
)

# 发货单的情形和出现的概率
SCENARIOS = {
    "正常": 0.71,
    "延迟发货": 0.06,
    "虚假发货": 0.02,
    "运输延误": 0.08,
    "物流停滞": 0.03,
    "丢件": 0.02,
    "未经同意放快递柜": 0.03,
    "未经同意放驿站": 0.02,
    "代签": 0.02,
    "破损拒收": 0.01,
}
DELIVERED = ("已签收", "已放快递柜", "已放驿站", "已代签")
ZONE_ETA = {"同城": "eta_city", "江浙沪": "eta_jzh", "一般": "eta_general", "偏远": "eta_remote"}
STATUS_BY_CODE = {
    "已发货": "待揽收", "已揽收": "已揽收", "运输中": "运输中", "派送中": "派送中", "拒收": "已拒收",
    "拦截退回": "已拦截，退回中", "退回商家": "已退回商家",
}
STOPPED = ("拒收", "拦截退回", "退回商家")  # 不会再往收件人走的状态，不算停滞，也不算超时


def eta_days(rules, zone):
    return rules.get("P-SHIP-08", ZONE_ETA[zone])


def stall_days(rules, zone):
    return rules.get("P-SHIP-07", "stall_days_remote" if zone == "偏远" else "stall_days")


def pick_scenario(rng):
    return rng.choices(list(SCENARIOS), weights=list(SCENARIOS.values()))[0]


def _short(city):
    return city[:-1] if city.endswith("市") else city


def _moment(day, hour_from, hour_to, rng):
    """某一天 hour_from 点到 hour_to 点之间的随机时刻，精确到分钟。"""
    return datetime.combine(day, time()) + timedelta(minutes=rng.randint(hour_from * 60, hour_to * 60 - 1))


def _ending(scenario, rng, receiver):
    pickup_code = f"{rng.randint(1, 30)}-{rng.randint(1, 6)}-{rng.randint(1000, 9999)}"
    return {
        "未经同意放快递柜": ("已放快递柜", f"快件已放入【丰巢智能柜】，凭取件码 {pickup_code} 取件"),
        "未经同意放驿站": ("已放驿站", f"快件已由【菜鸟驿站】代收，凭取件码 {pickup_code} 取件"),
        "代签": ("已代签", "已签收，签收人：门卫代签"),
        "破损拒收": ("拒收", "派送时发现外包装破损，收件人当面查看后拒收，快件将退回商家"),
    }.get(scenario, ("已签收", f"已签收，签收人：{receiver}"))


def _leg(rng, *, picked_at, deliver_day, origin, dest_city, dest_district, ending):
    """揽收之后的运输轨迹：转运、干线运输（大约每天一条）、到达营业部、派送、签收。"""
    same_city = _short(origin) == _short(dest_city)
    deliver_at = _moment(deliver_day, 10, 19, rng)
    while deliver_at < picked_at + timedelta(hours=18 if same_city else 30):
        deliver_at += timedelta(days=1)

    origin_hub = f"{_short(origin)}转运中心"
    station = f"{_short(dest_city)}{dest_district or ''}营业部"
    next_stop = station if same_city else f"{_short(dest_city)}转运中心"
    t = picked_at + timedelta(hours=rng.uniform(2, 5))
    events = [(t, "运输中", origin, f"快件已到达【{origin_hub}】")]
    t += timedelta(hours=rng.uniform(1, 4))
    events.append((t, "运输中", origin, f"快件离开【{origin_hub}】，发往【{next_stop}】"))
    if not same_city:
        arrive = datetime.combine(deliver_at.date(), time(6)) - timedelta(hours=rng.uniform(6, 14))
        t += timedelta(hours=rng.uniform(18, 30))
        while t < arrive - timedelta(hours=6):
            events.append((t, "运输中", None, f"快件运输中，下一站【{next_stop}】"))
            t += timedelta(hours=rng.uniform(18, 30))
        events.append((arrive, "运输中", dest_city, f"快件已到达【{next_stop}】"))
    station_at = _moment(deliver_at.date(), 6, 8, rng)
    events.append((station_at, "运输中", dest_city, f"快件已到达【{station}】"))
    events.append((station_at + timedelta(minutes=rng.randint(30, 110)), "派送中", dest_city, "快递员正在派送"))
    events.append((deliver_at, ending[0], dest_city, ending[1]))
    return _monotonic(events, rng)


def _monotonic(events, rng):
    """保证事件时间严格递增：随机生成的时刻偶尔会和前一条重叠。"""
    fixed = []
    for moment, *rest in events:
        if fixed and moment <= fixed[-1][0]:
            moment = fixed[-1][0] + timedelta(minutes=rng.randint(10, 40))
        fixed.append((moment, *rest))
    return fixed


def _stall(rules, events, zone, rng, *, lost):
    """在干线运输途中停滞：物流停滞是隔了超过判定天数才恢复，丢件是之后不再更新。"""
    cut = min(3, len(events) - 1)
    head, tail = events[:cut], events[cut:]
    if lost:
        return head
    gap = timedelta(days=stall_days(rules, zone) + rng.uniform(1, 4))
    return head + [(moment + gap, *rest) for moment, *rest in tail]


def plan_outbound(rng, rules, *, paid_at, zone, city, district, receiver, scenario=None, ship_at=None):
    """生成一张发货单：返回 (shipment 字段, 事件列表)。

    ship_at 为空时按 P-SHIP-01 的发货时效和情形决定发货时间；不为空时是换货补发，按时、
    按“正常”情形发出。
    """
    carrier, prefix = rng.choice(CARRIERS)
    tracking_no = prefix + "".join(rng.choice("0123456789") for _ in range(12))
    if ship_at is None:
        scenario = scenario or pick_scenario(rng)
        ship_hours = rules.get("P-SHIP-01", "ship_hours")
        if scenario == "延迟发货":
            ship_at = paid_at + timedelta(hours=ship_hours + rng.uniform(2, 120))
        else:
            ship_at = paid_at + timedelta(hours=rng.uniform(1, ship_hours - 4))
    else:
        scenario = "正常"
    if scenario == "虚假发货":
        fake_hours = rules.get("P-SHIP-03", "fake_ship_hours")
        picked_at = ship_at + timedelta(hours=rng.uniform(fake_hours + 4, fake_hours + 48))
    else:
        picked_at = ship_at + timedelta(hours=rng.uniform(0.5, 8))

    eta = eta_days(rules, zone)
    days = eta + rng.randint(1, 3) if scenario == "运输延误" else rng.randint(max(1, round(eta * 0.6)), eta)
    events = [
        (ship_at, "已发货", WAREHOUSE_CITY, f"商家已发货，{carrier}，运单号 {tracking_no}"),
        (picked_at, "已揽收", WAREHOUSE_CITY, f"{carrier}{_short(WAREHOUSE_CITY)}{WAREHOUSE_DISTRICT}营业部已揽收"),
    ]
    events += _leg(
        rng,
        picked_at=picked_at,
        deliver_day=ship_at.date() + timedelta(days=days),
        origin=WAREHOUSE_CITY,
        dest_city=city,
        dest_district=district,
        ending=_ending(scenario, rng, receiver),
    )
    if scenario in ("物流停滞", "丢件"):
        events = events[:2] + _stall(rules, events[2:], zone, rng, lost=scenario == "丢件")
    shipment = {
        "tracking_no": tracking_no,
        "carrier": carrier,
        "origin": f"浙江省{WAREHOUSE_CITY}{WAREHOUSE_DISTRICT}",
        "destination": f"{city}{district or ''}",
        "zone": zone,
        "created_at": ship_at,
        "promised_by": (ship_at.date() + timedelta(days=eta)).isoformat(),
        "scenario": scenario,
    }
    return shipment, events


def plan_return(rng, rules, *, sent_at, zone, city, district):
    """买家寄回退货：从买家所在城市寄到杭州仓，按区域时效送达，不模拟异常。"""
    carrier, prefix = rng.choice(CARRIERS)
    tracking_no = prefix + "".join(rng.choice("0123456789") for _ in range(12))
    picked_at = sent_at + timedelta(hours=rng.uniform(0.5, 6))
    eta = eta_days(rules, zone)
    events = [(picked_at, "已揽收", city, f"{carrier}{_short(city)}{district or ''}营业部已揽收")]
    events += _leg(
        rng,
        picked_at=picked_at,
        deliver_day=sent_at.date() + timedelta(days=rng.randint(max(1, round(eta * 0.6)), eta)),
        origin=city,
        dest_city=WAREHOUSE_CITY,
        dest_district=WAREHOUSE_DISTRICT,
        ending=("已签收", "已签收，签收人：ShopSim 杭州退货仓"),
    )
    shipment = {
        "tracking_no": tracking_no,
        "carrier": carrier,
        "origin": f"{city}{district or ''}",
        "destination": f"浙江省{WAREHOUSE_CITY}{WAREHOUSE_DISTRICT}",
        "zone": zone,
        "created_at": sent_at,
        "promised_by": None,
        "scenario": "正常",
    }
    return shipment, events


def summarize(rules, shipment, events, now):
    """一张物流单在 now 时刻的状态。events 只含不晚于 now 的事件，按时间排序，不能为空。"""
    last = events[-1]
    code = last["code"]
    delivered = code in DELIVERED
    idle = now - parse(last["time"])
    stalled = not delivered and code not in STOPPED and idle >= timedelta(days=stall_days(rules, shipment["zone"]))
    promised = shipment["promised_by"]
    overdue = not delivered and code not in STOPPED and promised is not None and now.date() > date.fromisoformat(promised)

    notes = []
    if stalled:
        notes.append(f"物流信息已 {idle.days} 天未更新，视为物流异常，可以申请补发或全额退款（P-SHIP-07、P-SHIP-06）")
    if overdue:
        notes.append(f"已超过承诺送达日期 {promised}（P-SHIP-08）")
    if code in ("已放快递柜", "已放驿站"):
        notes.append("快件被放入快递柜或驿站，轨迹里没有征得收件人同意的记录（P-SHIP-09）")
    if code == "已代签":
        notes.append("快件由他人代签（P-SHIP-09）；没有收到的可以申请核实，核实期间不计入退货期限")
    if code == "拒收":
        notes.append("包装破损拒收，按运输途中损毁处理，可以申请补发或全额退款（P-SHIP-10、P-SHIP-06）")
    return {
        "tracking_no": shipment["tracking_no"],
        "carrier": shipment["carrier"],
        "direction": shipment["direction"],
        "status": "物流异常" if stalled else STATUS_BY_CODE.get(code, "已签收"),
        "promised_by": promised,
        "delivered_at": last["time"] if delivered else None,
        "notes": notes,
        "events": [
            {"time": event["time"], "location": event["location"], "description": event["description"]}
            for event in events
        ],
    }
