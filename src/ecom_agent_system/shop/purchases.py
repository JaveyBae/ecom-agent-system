"""把购物 agent 在 ShopSimulator 里的购买记成订单。

ShopSimulator 负责挑选和下单：agent 点 Buy Now 时，环境返回买了哪件商品、选了哪些规格、按规格算出的
成交价，以及这次买得对不对（Reward v3 的 termination_reason）。付款以后的发货、物流和售后在这边的
数据库里继续。两种接法共用 record_purchase：

- gateway.py：在 ShopSimulator 前面转发请求，agent 一买下就记成订单；
- import_trajectories：读购物 agent 评测或采集时写的 trajectories.jsonl，把里面的购买补记成历史订单，
  再模拟顾客收货以后的动作。
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from datetime import datetime, timezone

from ecom_agent_system.shop.catalog import persona_key, variant_price
from ecom_agent_system.shop.db import ensure_agent_schema, fmt
from ecom_agent_system.shop.service import ShopError
from ecom_agent_system.shop.world import NO_REASON_RETURN_RATE as BASE_RETURN_RATE
from ecom_agent_system.shop.world import simulate_customer

# 收货后申请七天无理由退货的概率，按 Reward v3 的购买结果区分。买中目标商品的和模拟订单一样，
# 越不符合需求越容易退。这是模拟用的假设，不是统计出来的数。
NO_REASON_RETURN_RATE = {
    "gold_purchase": BASE_RETURN_RATE,
    "valid_alternative_purchase": 0.2,
    "partial_alternative_purchase": 0.45,
    "wrong_purchase": 0.8,
}
INSURED_RATE = 0.3  # 和模拟订单一样，三成买家买了退货运费险


class Buyers:
    """按 ShopSimulator 任务里的用户画像找到库里的用户。

    只有约五分之一的任务带画像。没有画像、或者画像不在库里的任务，按任务编号固定分给一个用户，
    同一个任务每次都分给同一个人。
    """

    def __init__(self, conn):
        rows = conn.execute("SELECT user_id, persona FROM users ORDER BY user_id").fetchall()
        if not rows:
            raise ShopError("库里没有用户，先运行 ecom-shop build")
        self.user_ids = [row["user_id"] for row in rows]
        self.by_persona = {persona_key(json.loads(row["persona"])): row["user_id"] for row in rows if row["persona"]}

    def find(self, persona, key):
        """返回 (user_id, 是否按画像匹配上)。"""
        if isinstance(persona, dict) and persona:
            user_id = self.by_persona.get(persona_key(persona))
            if user_id:
                return user_id, True
        digest = hashlib.sha1(f"task:{key}".encode("utf-8")).hexdigest()
        return self.user_ids[int(digest, 16) % len(self.user_ids)], False


def purchase_options(product_options, chosen):
    """把 ShopSimulator 记录的规格对回商品的写法。

    ShopSimulator 把规格名和选项都转成了小写，而且只选一部分规格也能下单。这里不区分大小写地对回
    原来的写法；只有一个选项的规格自动带上，没选的规格不记。
    """
    axes = {axis.casefold(): axis for axis in product_options}
    selected = {}
    for key, value in (chosen or {}).items():
        axis = axes.get(str(key).casefold(), str(key))
        values = [option["value"] for option in product_options.get(axis, [])]
        selected[axis] = next((option for option in values if option.casefold() == str(value).casefold()), str(value))
    for axis, values in product_options.items():
        if axis not in selected and len(values) == 1:
            selected[axis] = values[0]["value"]
    return selected


def _unit_price(product, options, price):
    if price is not None:
        return float(price)
    base = tuple(value for value in (product["price_min"], product["price_max"]) if value is not None)
    resolved = variant_price(json.loads(product["options"]), options, base)
    if resolved is None:
        raise ShopError(f"商品 {product['asin']} 的成交价无法确定")
    return resolved


def record_purchase(shop, buyers, result, *, now, label, task_id=None, attempt_index=None, trajectory_id=None, scenario=None):
    """把以购买结束的一次 ShopSimulator 交互记成订单，返回订单号。

    result 是 interact 的终局结果：purchase 是买了什么，goal 里的用户画像决定是谁买的。不用 reset
    返回的画像，因为任务没有画像时，ShopSimulator 会沿用这个环境槽位上一个任务的画像。
    """
    purchase = result.get("purchase") or {}
    if not purchase.get("asin"):
        raise ValueError("这次交互没有购买")
    asin = str(purchase["asin"])
    product = shop.conn.execute("SELECT * FROM products WHERE asin = ?", (asin,)).fetchone()
    if product is None:
        raise ShopError(f"商品 {asin} 不在库里")
    goal = result.get("goal") or {}
    user_id, matched = buyers.find(goal.get("user_persona"), task_id if task_id is not None else trajectory_id)
    options = purchase_options(json.loads(product["options"]), purchase.get("options"))
    key = trajectory_id or f"{label}:{task_id}:{fmt(now)}"
    order_id = shop.import_order(
        user_id, asin, options, now=now, unit_price=_unit_price(product, options, purchase.get("price")),
        insured=random.Random(f"{shop.seed}:insure:{key}").random() < INSURED_RATE, scenario=scenario,
    )
    detail = result.get("reward_detail") or {}
    failed = sorted(
        name for name, gate in (detail.get("hard_gates") or {}).items() if isinstance(gate, dict) and gate.get("status") == "fail"
    )
    shop.conn.execute(
        "INSERT INTO agent_purchases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (order_id, label, task_id, attempt_index, trajectory_id, int(matched),
         result.get("termination_reason") or detail.get("termination_reason"), result.get("reward"),
         int(bool(result.get("reward_valid", detail.get("reward_valid", True)))), json.dumps(failed),
         goal.get("instruction_text"), datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    return order_id


def react(shop, order_id, outcome, until, rates=NO_REASON_RETURN_RATE):
    """模拟顾客收到这单以后到 until 为止的动作，记在 agent_purchases.customer_actions 里。"""
    actions = Counter()
    rng = random.Random(f"{shop.seed}:customer:{order_id}")
    simulate_customer(shop, order_id, until, rng, actions, no_reason_rate=rates.get(outcome, BASE_RETURN_RATE))
    shop.conn.execute(
        "UPDATE agent_purchases SET customer_actions = ? WHERE order_id = ?",
        (json.dumps(dict(actions), ensure_ascii=False), order_id),
    )
    return actions


def read_trajectories(paths):
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def import_trajectories(shop, rows, *, label, start, end, react_until=None, rates=NO_REASON_RETURN_RATE):
    """把轨迹里的购买补记成 start 到 end 之间的历史订单。

    每笔购买的时间由轨迹 ID 决定，在区间里均匀分布，按时间先后入库。react_until 不为空时，模拟
    顾客收货以后到这个时刻为止的动作。已经导入过的轨迹（trajectory_id 相同）跳过，可以重复运行。
    """
    ensure_agent_schema(shop.conn)
    buyers = Buyers(shop.conn)
    seen = {row[0] for row in shop.conn.execute("SELECT trajectory_id FROM agent_purchases WHERE trajectory_id IS NOT NULL")}
    skipped, purchases = Counter(), []
    for row in rows:
        trajectory_id = row.get("trajectory_id")
        if not ((row.get("terminal_result") or {}).get("purchase") or {}).get("asin"):
            skipped["没有购买"] += 1
        elif trajectory_id is not None and trajectory_id in seen:
            skipped["已经导入过"] += 1
        else:
            seen.add(trajectory_id)
            key = trajectory_id or f"{row.get('task_id')}:{row.get('attempt_index')}"
            moment = start + (end - start) * random.Random(f"{shop.seed}:import:{label}:{key}").random()
            purchases.append((moment.replace(microsecond=0), row))
    outcomes, actions, matched, errors = Counter(), Counter(), 0, []
    for moment, row in sorted(purchases, key=lambda pair: pair[0]):
        result = row["terminal_result"]
        try:
            order_id = record_purchase(
                shop, buyers, result, now=moment, label=label, task_id=row.get("task_id"),
                attempt_index=row.get("attempt_index"), trajectory_id=row.get("trajectory_id"),
            )
        except ShopError as error:  # 例如数据库是用另一版商品数据生成的，商品对不上
            skipped["记不进库"] += 1
            errors.append({"trajectory_id": row.get("trajectory_id"), "error": str(error)})
            continue
        outcome = result.get("termination_reason")
        outcomes[outcome] += 1
        matched += shop.conn.execute("SELECT persona_matched FROM agent_purchases WHERE order_id = ?", (order_id,)).fetchone()[0]
        if react_until is not None:
            actions.update(react(shop, order_id, outcome, react_until, rates))
    shop.conn.commit()
    return {
        "label": label,
        "imported": sum(outcomes.values()),
        "skipped": dict(skipped),
        "errors": errors[:10],
        "persona_matched": matched,
        "outcomes": dict(outcomes),
        "customer_actions": dict(actions),
    }


def agent_orders(shop, now, label=None):
    """按购物 agent 汇总它买的单：买得对不对、成交额、售后和退款，只算 now 之前发生的。"""
    ensure_agent_schema(shop.conn)
    where, args = ("AND a.label = ?", (label,)) if label else ("", ())
    rows = shop.conn.execute(
        "SELECT a.label, a.order_id, a.outcome, a.persona_matched, a.customer_actions, o.total_amount"
        f" FROM agent_purchases a JOIN orders o USING (order_id) WHERE o.created_at <= ? {where} ORDER BY o.created_at",
        (fmt(now), *args),
    ).fetchall()
    report = {}
    for row in rows:
        entry = report.setdefault(row["label"], {
            "purchases": 0, "persona_matched": 0, "sales_amount": 0.0, "outcomes": Counter(),
            "returns_by_outcome": {}, "after_sales": Counter(), "after_sales_status": Counter(),
            "refunded_amount": 0.0, "return_shipping_paid_by": Counter(), "buyer_return_fees": 0.0,
            "rejected_requests": Counter(),
        })
        entry["purchases"] += 1
        entry["persona_matched"] += row["persona_matched"]
        entry["sales_amount"] += row["total_amount"]
        entry["outcomes"][row["outcome"]] += 1
        returned = False
        for brief in shop.get_order(row["order_id"], now=now)["after_sales"]:
            ticket = shop.get_after_sales(brief["ticket_id"], now=now)
            entry["after_sales"][f"{ticket['type']}（{ticket['reason']}）"] += 1
            entry["after_sales_status"][ticket["status"]] += 1
            returned |= ticket["type"] == "退货退款" and ticket["status"] != "已撤销"
            if ticket["status"] == "退款成功":
                entry["refunded_amount"] += ticket["refund_amount"]
            if ticket["fee_payer"]:  # 退货运费谁出；return_fee 是消费者自己付的部分
                entry["return_shipping_paid_by"][ticket["fee_payer"]] += 1
                entry["buyer_return_fees"] += ticket["return_fee"]
        by_outcome = entry["returns_by_outcome"].setdefault(row["outcome"], {"orders": 0, "return_requested": 0})
        by_outcome["orders"] += 1
        by_outcome["return_requested"] += int(returned)
        for action, count in json.loads(row["customer_actions"] or "{}").items():
            if action.endswith("（不符合条件，未办理）"):
                entry["rejected_requests"][action.removesuffix("（不符合条件，未办理）")] += count
    for entry in report.values():
        for key in ("sales_amount", "refunded_amount", "buyer_return_fees"):
            entry[key] = round(entry[key], 2)
        for key in ("outcomes", "after_sales", "after_sales_status", "return_shipping_paid_by", "rejected_requests"):
            entry[key] = dict(entry[key])
    return report
