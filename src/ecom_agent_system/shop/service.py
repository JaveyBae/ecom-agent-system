"""模拟商城的业务操作：下单、取消订单、查订单、查物流、售后申请和进度。

每个方法都显式传入当前时间 now。物流轨迹和售后进度在动作发生时整条生成（包括未来的
时间点），查询只返回不晚于 now 的部分。方法返回能直接转成 JSON 的 dict，以后作为 agent
的工具；不允许的操作抛出 ShopError，消息可以直接展示给用户。
"""

from __future__ import annotations

import json
import random
from datetime import timedelta

from ecom_agent_system.shop import logistics
from ecom_agent_system.shop.catalog import CategoryPolicy, variant_price
from ecom_agent_system.shop.db import fmt, parse
from ecom_agent_system.shop.policy import EVIDENCE_REASONS, Decision, ItemContext, Policy
from ecom_agent_system.shop.rules import PlatformRules

RETURN_ADDRESS = "浙江省杭州市余杭区仓前街道 ShopSim 杭州退货仓，收件人：售后组，电话 0571-8888-0000"
TICKET_TYPES = {
    "退货退款": ("七天无理由", "质量问题"),
    "换货": ("质量问题",),
    "仅退款": ("物流异常", "运输损毁", "生鲜变质"),
    "延迟发货补偿": ("延迟发货",),
    "拦截退款": ("顾客原因", "商家原因"),
}
FINAL_STATUSES = ("退款成功", "补偿已到账", "已完成", "已撤销", "已关闭", "拦截失败")
# 商家拒绝申请的概率：(未拆封, 已拆封)。商家的拒绝多数站不住，平台介入后支持买家的概率见 _arbitrate
REJECT_RATE = {"七天无理由": (0.06, 0.4), "质量问题": (0.25, 0.25)}
REJECT_NOTE = {
    "七天无理由": ("商家拒绝：商品不符合退货条件", "商家拒绝：商品已拆封，影响二次销售"),
    "质量问题": ("商家拒绝：认为不属于质量问题", "商家拒绝：认为不属于质量问题"),
}


class ShopError(Exception):
    """业务上不允许的操作。decision 是政策引擎的判断结果（如果有）。"""

    def __init__(self, message, decision=None):
        super().__init__(message)
        self.decision = decision


def _mask(phone):
    return f"{phone[:3]}****{phone[-4:]}"


class Shop:
    def __init__(self, conn, rules=None, categories=None, seed=0):
        self.conn = conn
        self.rules = rules or PlatformRules.load()
        self.categories = categories or CategoryPolicy.load()
        self.policy = Policy(self.rules)
        self.seed = seed

    def _rng(self, kind, key):
        return random.Random(f"{self.seed}:{kind}:{key}")

    def _one(self, sql, args, missing):
        row = self.conn.execute(sql, args).fetchone()
        if row is None:
            raise ShopError(missing)
        return row

    # ---------------------------------------------------------------- 用户
    def find_user(self, *, phone=None, email=None):
        """按注册手机号或邮箱找到用户，办理售后前核实身份用（P-SVC-01）。"""
        if not phone and not email:
            raise ShopError("需要注册手机号或注册邮箱")
        row = self._one(
            "SELECT * FROM users WHERE phone = ? OR email = ?",
            (phone or "", (email or "").strip().lower()),
            "没有找到这个手机号或邮箱对应的用户",
        )
        return self._user_view(row)

    def get_user(self, user_id):
        return self._user_view(self._one("SELECT * FROM users WHERE user_id = ?", (user_id,), "用户不存在"))

    def _user_view(self, row):
        return {
            "user_id": row["user_id"],
            "name": row["name"],
            "tier": row["tier"],
            "phone": _mask(row["phone"]),
            "email": row["email"],
            "address": f"{row['province']}{row['city']}{row['district'] or ''}{row['address']}",
            "zone": row["zone"],
        }

    # ---------------------------------------------------------------- 下单
    def place_order(self, user_id, asin, options, *, now, quantity=1, insured=False, unit_price=None, scenario=None, source="simulated"):
        """下单并付款，同时生成这张订单的发货物流。

        options 是 {规格名: 选项}，只有一个选项的规格可以不填。unit_price 为空时按所选规格
        计算。scenario 指定物流情形，为空时按概率抽取。
        """
        user = self._one("SELECT * FROM users WHERE user_id = ?", (user_id,), "用户不存在")
        product = self._one("SELECT * FROM products WHERE asin = ?", (asin,), "商品不存在")
        if quantity < 1:
            raise ShopError("数量至少为 1")
        product_options = json.loads(product["options"])
        selected = self._select_options(product_options, options or {})
        if unit_price is None:
            base = tuple(price for price in (product["price_min"], product["price_max"]) if price is not None)
            unit_price = variant_price(product_options, selected, base)
            if unit_price is None:
                raise ShopError("这个规格组合的价格无法确定")
        return self._create_order(user, product, selected, now, quantity, insured, unit_price, scenario, source)

    def import_order(self, user_id, asin, options, *, now, unit_price, insured=False, scenario=None, source="shop_agent"):
        """记下店铺前台已经成交的一单，例如购物 agent 在 ShopSimulator 里买下的商品。

        前台已经检查过规格、算好了价格，这里不再检查，规格和成交价照前台的记录存。
        """
        user = self._one("SELECT * FROM users WHERE user_id = ?", (user_id,), "用户不存在")
        product = self._one("SELECT * FROM products WHERE asin = ?", (asin,), f"商品 {asin} 不存在")
        return self._create_order(user, product, dict(options), now, 1, insured, unit_price, scenario, source)

    def _create_order(self, user, product, selected, now, quantity, insured, unit_price, scenario, source):
        user_id, asin = user["user_id"], product["asin"]
        sequence = self.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] + 1
        order_id = f"SO{now:%y%m%d}{sequence:06d}"
        amount = round(unit_price * quantity, 2)
        self.conn.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (order_id, user_id, fmt(now), amount, int(insured), user["name"], user["phone"], user["province"],
             user["city"], user["district"], user["address"], user["zone"], source),
        )
        self.conn.execute(
            "INSERT INTO order_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"{order_id}-1", order_id, asin, product["title"], product["category"],
             json.dumps(selected, ensure_ascii=False), quantity, unit_price, amount, product["policy_class"],
             product["no_return_marked"]),  # 商品页有标注的，下单时必须确认才能付款
        )
        shipment, events = logistics.plan_outbound(
            self._rng("ship", order_id), self.rules, paid_at=now, zone=user["zone"], city=user["city"],
            district=user["district"], receiver=user["name"], scenario=scenario,
        )
        self._save_shipment(order_id, None, "发货", shipment, events)
        return order_id

    def _select_options(self, product_options, chosen):
        unknown = set(chosen) - set(product_options)
        if unknown:
            raise ShopError(f"商品没有这些规格：{'、'.join(sorted(unknown))}")
        selected = {}
        for axis, values in product_options.items():
            if axis not in chosen:
                if len(values) > 1:
                    raise ShopError(f"请选择{axis}")
                selected[axis] = values[0]["value"]
                continue
            match = next((value for value in values if value["value"] == str(chosen[axis])), None)
            if match is None:
                raise ShopError(f"{axis}没有“{chosen[axis]}”这个选项")
            if not match["available"]:
                raise ShopError(f"{axis}“{chosen[axis]}”已售罄")
            selected[axis] = match["value"]
        return selected

    def _save_shipment(self, order_id, ticket_id, direction, shipment, events):
        self.conn.execute(
            "INSERT INTO shipments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (shipment["tracking_no"], order_id, ticket_id, direction, shipment["carrier"], shipment["origin"],
             shipment["destination"], shipment["zone"], fmt(shipment["created_at"]), shipment["promised_by"],
             shipment["scenario"]),
        )
        self.conn.executemany(
            "INSERT INTO shipment_events (tracking_no, time, code, location, description) VALUES (?, ?, ?, ?, ?)",
            [(shipment["tracking_no"], fmt(moment), code, location, text) for moment, code, location, text in events],
        )

    # ---------------------------------------------------------------- 订单和物流
    def _order(self, order_id, now):
        order = self._one("SELECT * FROM orders WHERE order_id = ?", (order_id,), f"订单 {order_id} 不存在")
        if order["created_at"] > fmt(now):
            raise ShopError(f"订单 {order_id} 不存在")
        return order

    def _cancelled(self, order, now):
        return order["cancelled_at"] is not None and order["cancelled_at"] <= fmt(now)

    def _events(self, tracking_no, now):
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT time, code, location, description FROM shipment_events"
                " WHERE tracking_no = ? AND time <= ? ORDER BY time, id",
                (tracking_no, fmt(now)),
            )
        ]

    def _shipping(self, order, now):
        """发货单在 now 的事实：认定的发货时间、签收时间和物流状态。"""
        shipment = self.conn.execute(
            "SELECT * FROM shipments WHERE order_id = ? AND direction = '发货'", (order["order_id"],)
        ).fetchone()
        events = self._events(shipment["tracking_no"], now) if shipment else []
        if not events:
            return {"shipment": shipment, "summary": None, "entered_at": None, "shipped_at": None, "delivered_at": None}
        times = {event["code"]: parse(event["time"]) for event in reversed(events)}  # 同一代码取最早的一次
        summary = logistics.summarize(self.rules, dict(shipment), events, now)
        return {
            "shipment": shipment,
            "summary": summary,
            "entered_at": times.get("已发货"),
            "shipped_at": self.policy.ship_time(times.get("已发货"), times.get("已揽收"), now),
            "delivered_at": parse(summary["delivered_at"]) if summary["delivered_at"] else None,
        }

    def _order_status(self, order, shipping, now):
        if self._cancelled(order, now):
            return "已取消"
        if shipping["summary"] is None:
            return "待发货"
        if shipping["summary"]["status"] == "待揽收":
            return "已发货，待揽收"
        return shipping["summary"]["status"]

    def list_orders(self, user_id, *, now):
        rows = self.conn.execute(
            "SELECT o.*, i.title FROM orders o JOIN order_items i ON i.order_id = o.order_id"
            " WHERE o.user_id = ? AND o.created_at <= ? ORDER BY o.created_at DESC",
            (user_id, fmt(now)),
        ).fetchall()
        return [
            {
                "order_id": row["order_id"],
                "created_at": row["created_at"],
                "title": row["title"],
                "total_amount": row["total_amount"],
                "status": self._order_status(row, self._shipping(row, now), now),
            }
            for row in rows
        ]

    def get_order(self, order_id, *, now):
        order = self._order(order_id, now)
        shipping = self._shipping(order, now)
        items = self.conn.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY item_id", (order_id,))
        tickets = self.conn.execute(
            "SELECT ticket_id FROM after_sales WHERE order_id = ? AND created_at <= ? ORDER BY created_at",
            (order_id, fmt(now)),
        )
        summary = shipping["summary"]
        return {
            "order_id": order_id,
            "user_id": order["user_id"],
            "created_at": order["created_at"],
            "status": self._order_status(order, shipping, now),
            "total_amount": order["total_amount"],
            "insured": bool(order["insured"]),
            "receiver": order["receiver"],
            "address": self._full_address(order, now),
            "zone": order["zone"],
            "cancelled_at": order["cancelled_at"] if self._cancelled(order, now) else None,
            "items": [
                {
                    "item_id": item["item_id"],
                    "asin": item["asin"],
                    "title": item["title"],
                    "options": json.loads(item["options"]),
                    "quantity": item["quantity"],
                    "unit_price": item["unit_price"],
                    "amount": item["amount"],
                    "policy_class": item["policy_class"],
                    "no_return_confirmed": bool(item["no_return_confirmed"]),
                }
                for item in items
            ],
            "shipping": None
            if summary is None
            else {
                "status": summary["status"],
                "carrier": summary["carrier"],
                "tracking_no": summary["tracking_no"],
                "shipped_at": fmt(shipping["shipped_at"]) if shipping["shipped_at"] else None,
                "promised_by": summary["promised_by"],
                "delivered_at": summary["delivered_at"],
            },
            "after_sales": [self._ticket_brief(row["ticket_id"], now) for row in tickets],
        }

    def _full_address(self, order, now):
        row = self.conn.execute(
            "SELECT new_value FROM order_changes WHERE order_id = ? AND field = 'address' AND time <= ? ORDER BY time DESC, id DESC LIMIT 1",
            (order["order_id"], fmt(now)),
        ).fetchone()
        detail = row["new_value"] if row else order["address"]
        return f"{order['province']}{order['city']}{order['district'] or ''}{detail}"

    def check_change_address(self, order_id, *, now):
        """只判断能不能改地址（P-SHIP-11），不修改。"""
        order = self._order(order_id, now)
        shipping = self._shipping(order, now)
        return self.policy.change_address(shipping["entered_at"], self._cancelled(order, now)).to_dict()

    def change_address(self, order_id, address, *, now):
        """发货前修改详细地址（P-SHIP-11）。修改按时间记录，查看更早的时刻还是旧地址。"""
        order = self._order(order_id, now)
        shipping = self._shipping(order, now)
        decision = self.policy.change_address(shipping["entered_at"], self._cancelled(order, now))
        if not decision.allowed:
            raise ShopError(decision.explanation, decision)
        detail = (address or "").strip()
        if not 4 <= len(detail) <= 60 or "\n" in detail:
            raise ShopError("详细地址要在 4 到 60 个字之间，例如“文三路 88 号 3 幢 501 室”")
        old = self._full_address(order, now)
        self.conn.execute(
            "INSERT INTO order_changes (order_id, time, field, old_value, new_value) VALUES (?, ?, 'address', ?, ?)",
            (order_id, fmt(now), old, detail),
        )
        return {
            "order_id": order_id,
            "old_address": old,
            "address": self._full_address(order, now),
            "changed_at": fmt(now),
            "explanation": decision.explanation,
            "rule_ids": decision.rule_ids,
        }

    def track_shipment(self, order_id, *, now):
        """订单的全部物流：发货，以及售后产生的退货、换货寄送。"""
        order = self._order(order_id, now)
        shipping = self._shipping(order, now)
        notes = []
        if not self._cancelled(order, now):
            delay = self.policy.shipping_delay(parse(order["created_at"]), shipping["shipped_at"], now, order["total_amount"])
            if delay.allowed or shipping["shipped_at"] is None:  # 按时发货的不提示
                notes.append(delay.explanation + "（" + "、".join(delay.rule_ids) + "）")
            entered, shipped = shipping["entered_at"], shipping["shipped_at"]
            if entered and shipped is None:
                notes.append("填写运单号后 24 小时仍没有揽收记录，视为虚假发货，按延迟发货处理（P-SHIP-03）")
            elif entered and shipped != entered:
                notes.append(f"填写运单号后超过 24 小时才揽收，按虚假发货处理，以揽收时间 {fmt(shipped)} 为发货时间（P-SHIP-03）")
        shipments = []
        for row in self.conn.execute("SELECT * FROM shipments WHERE order_id = ? ORDER BY created_at", (order_id,)):
            events = self._events(row["tracking_no"], now)
            if events:
                shipments.append(logistics.summarize(self.rules, dict(row), events, now))
        return {
            "order_id": order_id,
            "status": self._order_status(order, shipping, now),
            "notes": notes,
            "shipments": shipments,
        }

    def cancel_order(self, order_id, *, now, review=False):
        """发货前取消订单（P-SHIP-04）；超时未发货的同时发放延迟发货补偿（P-SHIP-02）。review 和 create_after_sales 的一样：
        附带的补偿要先人工审核时传真值或审核原因，售后频繁的账户（P-RISK-01）由调用方决定。"""
        order = self._order(order_id, now)
        if self._cancelled(order, now):
            raise ShopError("订单已经取消")
        shipping = self._shipping(order, now)
        decision = self.policy.cancel_order(parse(order["created_at"]), shipping["shipped_at"], now, order["total_amount"])
        if not decision.allowed:
            raise ShopError(decision.explanation, decision)
        compensation = None
        if self.policy.shipping_delay(parse(order["created_at"]), None, now, order["total_amount"]).allowed:
            try:
                compensation = self.create_after_sales(order_id, "延迟发货补偿", "延迟发货", now=now, review=review)
            except ShopError:
                pass  # 已经申请过补偿
        tracking_no = shipping["shipment"]["tracking_no"]
        self.conn.execute("DELETE FROM shipment_events WHERE tracking_no = ? AND time > ?", (tracking_no, fmt(now)))
        self.conn.execute(
            "UPDATE orders SET cancelled_at = ?, cancel_reason = ? WHERE order_id = ?",
            (fmt(now), "买家在发货前取消", order_id),
        )
        return {
            "order_id": order_id,
            "cancelled_at": fmt(now),
            "refund_amount": order["total_amount"],
            "explanation": decision.explanation,
            "rule_ids": decision.rule_ids,
            "compensation": compensation,
        }

    # ---------------------------------------------------------------- 售后
    def _item(self, order_id, item_id):
        if item_id is None:
            rows = self.conn.execute("SELECT * FROM order_items WHERE order_id = ?", (order_id,)).fetchall()
            if len(rows) != 1:
                raise ShopError("这个订单有多件商品，需要指定 item_id")
            return rows[0]
        return self._one(
            "SELECT * FROM order_items WHERE order_id = ? AND item_id = ?", (order_id, item_id), "订单里没有这件商品"
        )

    def _context(self, order, item, delivered_at):
        tier = self._one("SELECT tier FROM users WHERE user_id = ?", (order["user_id"],), "用户不存在")["tier"]
        durable = self._one("SELECT durable FROM products WHERE asin = ?", (item["asin"],), "商品不存在")["durable"]
        return ItemContext(
            tier=tier,
            zone=order["zone"],
            insured=bool(order["insured"]),
            amount=item["amount"],
            policy_class=item["policy_class"],
            policy_kind=self.categories.kind(item["policy_class"]),
            class_rules=tuple(self.categories.rule_ids(item["policy_class"])),
            confirmed=bool(item["no_return_confirmed"]),
            durable=bool(durable),
            delivered_at=delivered_at,
        )

    def _decide(self, order, item, kind, reason, now, *, opened=False, activated=False):
        if reason not in TICKET_TYPES.get(kind, ()):
            choices = "；".join(f"{name}：{'、'.join(reasons)}" for name, reasons in TICKET_TYPES.items())
            raise ShopError(f"售后类型和原因不匹配。可选的有 {choices}")
        if self._cancelled(order, now):
            return Decision(False, "订单已取消，取消时已全额退款。", ["P-SHIP-04"])
        shipping = self._shipping(order, now)
        if kind == "延迟发货补偿":
            return self.policy.shipping_delay(parse(order["created_at"]), shipping["shipped_at"], now, order["total_amount"])
        ctx = self._context(order, item, shipping["delivered_at"])
        if kind == "拦截退款":
            return self.policy.intercept(ctx, parse(order["created_at"]), shipping, now, reason)
        if kind == "仅退款":
            return self.policy.refund_only(ctx, now, reason, shipping["summary"])
        if reason == "质量问题":
            return self.policy.quality_issue(ctx, now, "退货" if kind == "退货退款" else "换货")
        return self.policy.no_reason_return(ctx, now, opened=opened, activated=activated)

    def check_after_sales(self, order_id, kind, reason, *, now, item_id=None, opened=False, activated=False):
        """只判断能不能办理、怎么办理，不创建售后单。"""
        order = self._order(order_id, now)
        decision = self._decide(order, self._item(order_id, item_id), kind, reason, now, opened=opened, activated=activated)
        result = {"type": kind, "reason": reason, **decision.to_dict()}
        if decision.allowed and reason in EVIDENCE_REASONS:
            result["explanation"] += "提交时要说明具体问题，例如哪里破损、什么功能失灵，并提供照片或视频（P-QA-05）。"
            result["rule_ids"] = result["rule_ids"] + ["P-QA-05"]
            result["requires_evidence"] = True
        return result

    def create_after_sales(self, order_id, kind, reason, *, now, item_id=None, opened=False, activated=False,
                           evidence=None, review=False, merchant_decision=None, evidence_verified=False):
        """提交售后。review 为真时先进入人工审核（P-SVC-04）；传字符串则把它作为审核原因写进进度，例如 P-RISK-01 的
        风控审核。merchant_decision 只在搭测试场景时用，强制商家同意或拒绝。evidence_verified 为真表示问题描述已经由
        调用方（凭证审核员）核过，这里不再按关键词检查，只要求非空。"""
        order = self._order(order_id, now)
        item = self._item(order_id, item_id)
        decision = self._decide(order, item, kind, reason, now, opened=opened, activated=activated)
        if not decision.allowed:
            raise ShopError(decision.explanation, decision)
        if evidence_verified and reason in EVIDENCE_REASONS:
            if len((evidence or "").strip()) < 4:
                raise ShopError(f"按{reason}申请需要说明具体问题（P-QA-05）")
        else:
            missing = self.policy.evidence(reason, evidence)
            if missing:
                raise ShopError(missing.explanation, missing)
        self._check_duplicate(order_id, item["item_id"], kind, now)

        sequence = self.conn.execute("SELECT COUNT(*) FROM after_sales").fetchone()[0] + 1
        ticket_id = f"AS{sequence:08d}"
        self.conn.execute(
            "INSERT INTO after_sales (ticket_id, order_id, item_id, type, reason, created_at, refund_amount, return_fee,"
            " fee_payer, rule_ids, evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, order_id, item["item_id"], kind, reason, fmt(now), decision.refund_amount,
             decision.return_fee, decision.fee_payer, json.dumps(decision.rule_ids), (evidence or "").strip() or None),
        )
        if kind == "拦截退款":
            self._intercept(ticket_id, order, decision, now)
            return self.get_after_sales(ticket_id, now=now)
        if kind == "延迟发货补偿":
            self._event(ticket_id, now, "平台处理中", "买家", "买家申请延迟发货补偿")
        else:
            self._event(ticket_id, now, "待商家处理", "买家", f"买家申请{kind}，原因：{reason}")
        start = now
        if review:
            limit = self.rules.get("P-SVC-04", "auto_refund_limit")
            note = review if isinstance(review, str) else f"金额 {decision.refund_amount:g} 元超过智能客服直接办理的额度 {limit} 元，提交人工审核（P-SVC-04）"
            self._event(ticket_id, now, "待人工审核", "平台", note)
            start = now + timedelta(hours=self._rng("review", ticket_id).uniform(1, 8))
            self._event(ticket_id, start, "人工审核通过", "人工客服", "人工审核通过，继续处理")
        self._schedule_response(ticket_id, kind, reason, decision.refund_amount, start, opened=opened, merchant_decision=merchant_decision)
        return self.get_after_sales(ticket_id, now=now)

    def _check_duplicate(self, order_id, item_id, kind, now):
        rows = self.conn.execute(
            "SELECT ticket_id, type FROM after_sales WHERE order_id = ? AND item_id = ? AND created_at <= ?",
            (order_id, item_id, fmt(now)),
        ).fetchall()
        for row in rows:
            status = self._status(row["ticket_id"], now)
            if status == "已撤销":
                continue
            if kind == "延迟发货补偿" or row["type"] == "延迟发货补偿":
                if kind == row["type"]:
                    raise ShopError(f"这个订单已经申请过延迟发货补偿（{row['ticket_id']}）")
            elif status not in FINAL_STATUSES:
                raise ShopError(f"这件商品已有进行中的售后（{row['ticket_id']}，{status}），不能重复申请")
            elif status == "退款成功":
                raise ShopError(f"这件商品已经退款（{row['ticket_id']}）")

    def _event(self, ticket_id, moment, status, actor, note):
        self.conn.execute(
            "INSERT INTO after_sales_events (ticket_id, time, status, actor, note) VALUES (?, ?, ?, ?, ?)",
            (ticket_id, fmt(moment), status, actor, note),
        )

    def _schedule_response(self, ticket_id, kind, reason, amount, now, *, opened=False, merchant_decision=None):
        """模拟商家或平台的处理。商家在处理时限内答复，超时由系统自动同意（P-RET-06）；
        退货、换货申请有一定概率被商家拒绝（REJECT_RATE），之后要靠平台介入（P-SVC-06、P-SVC-03）。"""
        rng = self._rng("merchant", ticket_id)
        if kind == "延迟发货补偿":
            moment = now + timedelta(minutes=rng.randint(5, 120))
            self._event(ticket_id, moment, "补偿已到账", "平台", f"延迟发货补偿 {amount:g} 元已到账（P-SHIP-02）")
            return
        hours = self.rules.get("P-RET-06", "respond_hours")
        delay = timedelta(hours=rng.uniform(0.5, hours * 1.25))
        if merchant_decision == "reject":
            delay = min(delay, timedelta(hours=hours - 1))
        if delay > timedelta(hours=hours):
            moment, actor, prefix = now + timedelta(hours=hours), "平台", "商家超时未处理，系统自动同意（P-RET-06）"
        else:
            moment, actor, prefix = now + delay, "商家", "商家同意申请"
            rate = REJECT_RATE.get(reason, (0, 0))[int(bool(opened))] if kind in ("退货退款", "换货") else 0
            if merchant_decision == "reject" or (merchant_decision is None and rng.random() < rate):
                self._event(ticket_id, moment, "商家拒绝", "商家", REJECT_NOTE[reason][int(bool(opened))])
                return
        if kind == "仅退款":
            self._event(ticket_id, moment, "退款成功", actor, f"{prefix}，退款 {amount:g} 元已原路退回")
        else:
            self._event(ticket_id, moment, "待买家退货", actor, f"{prefix}，请寄回商品。退货地址：{RETURN_ADDRESS}")

    def request_intervention(self, ticket_id, statement, *, now):
        """商家拒绝后申请平台介入（P-SVC-03、P-SVC-06），平台在 3 个工作日内判定。"""
        ticket = self._ticket(ticket_id, now)
        status = self._status(ticket_id, now)
        if status != "商家拒绝":
            raise ShopError(f"当前状态是“{status}”，商家拒绝申请后才能申请平台介入（P-SVC-03）")
        note = (statement or "").strip()[:100]
        self._event(ticket_id, now, "平台介入中", "买家", f"买家申请平台介入：{note}" if note else "买家申请平台介入")
        self._arbitrate(ticket, now)
        return self.get_after_sales(ticket_id, now=now)

    def _arbitrate(self, ticket, now):
        """平台判定。七天无理由退货的拒绝多半不成立（拆封查验不影响退货，P-RET-03），质量争议有一部分商家胜。"""
        rng = self._rng("platform", ticket["ticket_id"])
        moment = now + timedelta(hours=rng.uniform(4, self.rules.get("P-SVC-03", "intervene_days") * 24))
        if rng.random() < (0.9 if ticket["reason"] == "七天无理由" else 0.7):
            self._event(ticket["ticket_id"], moment, "待买家退货", "平台",
                        f"平台判定商家的拒绝理由不成立，支持买家（P-SVC-06），请寄回商品。退货地址：{RETURN_ADDRESS}")
        else:
            self._event(ticket["ticket_id"], moment, "已关闭", "平台",
                        "平台判定商家的拒绝理由成立，售后关闭；对结果有异议的，可以投诉、调解、仲裁或者诉讼（P-SVC-03）")

    def _intercept(self, ticket_id, order, decision, now):
        """拦截包裹（P-SHIP-05）：拦截生效前已经签收的算拦截失败；成功的退回商家，商家收到后退款。"""
        shipment = self.conn.execute(
            "SELECT * FROM shipments WHERE order_id = ? AND direction = '发货'", (order["order_id"],)
        ).fetchone()
        events = self.conn.execute(
            "SELECT time, code, location FROM shipment_events WHERE tracking_no = ? ORDER BY time, id", (shipment["tracking_no"],)
        ).fetchall()
        delivered = next((parse(event["time"]) for event in events if event["code"] in logistics.DELIVERED), None)
        rng = self._rng("intercept", ticket_id)
        done = now + timedelta(hours=rng.uniform(2, 12))
        self._event(ticket_id, now, "拦截中", "平台", "已通知物流公司拦截包裹")
        if delivered is not None and delivered <= done:
            self._event(ticket_id, delivered, "拦截失败", "物流", "包裹在拦截生效前已经签收，拦截失败；可以申请七天无理由退货（P-SHIP-05）")
            return
        location = next((event["location"] for event in reversed(events) if parse(event["time"]) <= done), logistics.WAREHOUSE_CITY)
        returned = done + timedelta(hours=rng.uniform(24, 72))
        self.conn.execute("DELETE FROM shipment_events WHERE tracking_no = ? AND time > ?", (shipment["tracking_no"], fmt(done)))
        self.conn.executemany(
            "INSERT INTO shipment_events (tracking_no, time, code, location, description) VALUES (?, ?, ?, ?, ?)",
            [(shipment["tracking_no"], fmt(done), "拦截退回", location, "包裹已拦截，正在退回商家"),
             (shipment["tracking_no"], fmt(returned), "退回商家", logistics.WAREHOUSE_CITY, "包裹已退回商家，商家已签收")],
        )
        self._event(ticket_id, done, "已拦截，退回中", "物流", "包裹已拦截，正在退回商家")
        self._event(ticket_id, returned, "商家已收到退回商品", "商家", "商家已收到拦截退回的包裹")
        fee = f"扣除往返运费 {decision.return_fee:g} 元，" if decision.fee_payer == "消费者" else ""
        self._event(ticket_id, returned + timedelta(hours=rng.uniform(1, 24)), "退款成功", "商家",
                    f"{fee}退款 {decision.refund_amount:g} 元已原路退回")

    # ---------------------------------------------------------------- 工单：转人工、催单
    def _open_work_order(self, user_id, kind, summary, now, *, order_id=None, ticket_id=None):
        sequence = self.conn.execute("SELECT COUNT(*) FROM work_orders").fetchone()[0] + 1
        work_order_id = f"WO{sequence:08d}"
        self.conn.execute(
            "INSERT INTO work_orders VALUES (?, ?, ?, ?, ?, ?, ?)",
            (work_order_id, user_id, order_id, ticket_id, kind, summary, fmt(now)),
        )
        return work_order_id

    def _work_event(self, work_order_id, moment, status, note):
        self.conn.execute(
            "INSERT INTO work_order_events (work_order_id, time, status, note) VALUES (?, ?, ?, ?)",
            (work_order_id, fmt(moment), status, note),
        )

    def get_work_order(self, work_order_id, *, now):
        row = self._one("SELECT * FROM work_orders WHERE work_order_id = ?", (work_order_id,), f"工单 {work_order_id} 不存在")
        if row["created_at"] > fmt(now):
            raise ShopError(f"工单 {work_order_id} 不存在")
        history = [dict(event) for event in self.conn.execute(
            "SELECT time, status, note FROM work_order_events WHERE work_order_id = ? AND time <= ? ORDER BY time, id",
            (work_order_id, fmt(now)),
        )]
        return {
            "work_order_id": work_order_id,
            "kind": row["kind"],
            "order_id": row["order_id"],
            "ticket_id": row["ticket_id"],
            "summary": row["summary"],
            "created_at": row["created_at"],
            "status": history[-1]["status"] if history else "待处理",
            "history": history,
        }

    def list_work_orders(self, user_id, *, now):
        rows = self.conn.execute(
            "SELECT work_order_id FROM work_orders WHERE user_id = ? AND created_at <= ? ORDER BY created_at DESC",
            (user_id, fmt(now)),
        ).fetchall()
        return [self.get_work_order(row["work_order_id"], now=now) for row in rows]

    def hand_off(self, user_id, summary, *, now, order_id=None):
        """转人工客服（P-SVC-04）：开一张工单，人工客服几分钟内接入。"""
        work_order_id = self._open_work_order(user_id, "转人工", summary, now, order_id=order_id)
        self._work_event(work_order_id, now, "待人工处理", "已转人工客服，等待接入")
        joined = now + timedelta(minutes=self._rng("human", work_order_id).randint(1, 15))
        self._work_event(work_order_id, joined, "人工处理中", "人工客服已接入")
        return self.get_work_order(work_order_id, now=now)

    def urge(self, order_id, *, now):
        """催发货或催派送（P-SHIP-12）。商家或物流公司过一段时间答复，答复里是原本排好的发货、送达时间。"""
        order = self._order(order_id, now)
        if self._cancelled(order, now):
            raise ShopError("订单已经取消，不需要催促")
        shipping = self._shipping(order, now)
        if shipping["delivered_at"] is not None:
            raise ShopError("包裹已经签收，不需要催促")
        status = shipping["summary"]["status"] if shipping["summary"] else None
        if status in ("已拦截，退回中", "已退回商家", "已拒收"):
            raise ShopError(f"包裹{status}，不需要催促")
        kind = "催发货" if shipping["entered_at"] is None else "催派送"
        recent = self.conn.execute(
            "SELECT work_order_id FROM work_orders WHERE order_id = ? AND kind = ? AND created_at > ? AND created_at <= ?",
            (order_id, kind, fmt(now - timedelta(hours=24)), fmt(now)),
        ).fetchone()
        paid = parse(order["created_at"])
        deadline = paid + timedelta(hours=self.rules.get("P-SHIP-01", "ship_hours"))
        notes, rule_ids = [], ["P-SHIP-12"]
        if kind == "催发货":
            notes.append(f"商家应在 {fmt(deadline)} 前发货（P-SHIP-01）。")
            rule_ids.append("P-SHIP-01")
            if now > deadline:
                notes.append("已经超过发货时效，可以申请延迟发货补偿（P-SHIP-02）。")
                rule_ids.append("P-SHIP-02")
        elif shipping["summary"]["promised_by"]:
            notes.append(f"承诺 {shipping['summary']['promised_by']} 前送达（P-SHIP-08）。")
            rule_ids.append("P-SHIP-08")
        if recent:
            result = self.get_work_order(recent["work_order_id"], now=now)
            return {**result, "repeated": True, "notes": ["24 小时内已经催过，不重复通知。"] + notes, "rule_ids": rule_ids}

        tracking_no = shipping["shipment"]["tracking_no"]
        future = self.conn.execute(
            "SELECT time, code FROM shipment_events WHERE tracking_no = ? AND time > ? ORDER BY time, id", (tracking_no, fmt(now))
        ).fetchall()
        rng = self._rng("urge", f"{order_id}:{fmt(now)}")
        if kind == "催发货":
            planned = next((parse(event["time"]) for event in future if event["code"] == "已发货"), None)
            replied = now + timedelta(hours=rng.uniform(1, self.rules.get("P-SHIP-12", "urge_reply_hours")))
            if planned is not None and planned <= replied:
                reply = f"商家答复：已于 {planned:%m-%d %H:%M} 发货"
            elif planned is not None:
                reply = f"商家答复：预计 {planned:%m-%d %H:%M} 前发货"
            else:
                reply = "商家答复：正在备货，会尽快发货"
            notified = "已通知商家"
        else:
            planned = next((parse(event["time"]) for event in future if event["code"] in logistics.DELIVERED), None)
            replied = now + timedelta(hours=rng.uniform(1, 6))
            reply = f"物流公司答复：已加急，预计 {planned:%m-%d} 送达" if planned else "物流公司答复：正在核实包裹位置"
            notified = "已通知物流公司"
        work_order_id = self._open_work_order(order["user_id"], kind, f"顾客催促{kind[1:]}", now, order_id=order_id)
        self._work_event(work_order_id, now, notified, f"顾客{kind}，已{notified[1:]}")
        self._work_event(work_order_id, replied, "已答复", reply)
        return {**self.get_work_order(work_order_id, now=now), "repeated": False, "notes": notes, "rule_ids": rule_ids}

    def submit_return_shipment(self, ticket_id, *, now):
        """买家寄回商品并填写退货物流单号（运单号由模拟器生成）。"""
        ticket = self._ticket(ticket_id, now)
        status = self._status(ticket_id, now)
        if ticket["type"] not in ("退货退款", "换货"):
            raise ShopError("这个售后不需要寄回商品")
        if status == "待商家处理":
            due = parse(ticket["created_at"]) + timedelta(hours=self.rules.get("P-RET-06", "respond_hours"))
            raise ShopError(f"商家还没有同意申请，最晚 {fmt(due)} 前处理，逾期系统自动同意（P-RET-06）")
        if status != "待买家退货":
            raise ShopError(f"当前状态是“{status}”，不需要寄回商品")

        order = self._order(ticket["order_id"], now)
        rng = self._rng("return", ticket_id)
        shipment, events = logistics.plan_return(
            rng, self.rules, sent_at=now, zone=order["zone"], city=order["city"], district=order["district"]
        )
        self._save_shipment(order["order_id"], ticket_id, "退货", shipment, events)
        received_at = events[-1][0]
        self._event(ticket_id, now, "买家已退货", "买家", f"买家已寄回，{shipment['carrier']} {shipment['tracking_no']}")
        refund = ticket["refund_amount"]
        if ticket["type"] == "换货":
            self._event(ticket_id, received_at, "商家已收货", "商家", "商家已收到退回的商品")
            ship_at = received_at + timedelta(hours=rng.uniform(2, 48))
            new_shipment, new_events = logistics.plan_outbound(
                rng, self.rules, paid_at=ship_at, zone=order["zone"], city=order["city"], district=order["district"],
                receiver=order["receiver"], ship_at=ship_at,
            )
            self._save_shipment(order["order_id"], ticket_id, "换货", new_shipment, new_events)
            self._event(ticket_id, ship_at, "换货已发出", "商家", f"新商品已发出，{new_shipment['carrier']} {new_shipment['tracking_no']}")
            self._event(ticket_id, new_events[-1][0], "已完成", "买家", "新商品已签收")
            return self.get_after_sales(ticket_id, now=now)

        tier = self._one("SELECT tier FROM users WHERE user_id = ?", (order["user_id"],), "用户不存在")["tier"]
        if ticket["reason"] == "七天无理由" and self.rules.tier_at_least(tier, "铂金"):
            moment = now + timedelta(minutes=rng.randint(1, 10))
            self._event(ticket_id, moment, "退款成功", "平台", f"极速退款：平台先行退款 {refund:g} 元（P-MEM-01）")
            return self.get_after_sales(ticket_id, now=now)
        self._event(ticket_id, received_at, "商家已收货", "商家", "商家已收到退货")
        days = self.rules.get("P-RET-07", "refund_days")
        if rng.random() < 0.9:
            moment = received_at + timedelta(hours=rng.uniform(1, days * 24 - 1))
            self._event(ticket_id, moment, "退款成功", "商家", f"商家已退款 {refund:g} 元，原路退回")
        else:
            moment = received_at + timedelta(days=days)
            self._event(ticket_id, moment, "退款成功", "平台", f"商家逾期未处理，平台自动退款 {refund:g} 元（P-RET-07）")
        return self.get_after_sales(ticket_id, now=now)

    def cancel_after_sales(self, ticket_id, *, now):
        self._ticket(ticket_id, now)
        status = self._status(ticket_id, now)
        if status not in ("待商家处理", "待买家退货", "商家拒绝", "待人工审核"):
            raise ShopError(f"当前状态是“{status}”，不能撤销")
        self.conn.execute("DELETE FROM after_sales_events WHERE ticket_id = ? AND time > ?", (ticket_id, fmt(now)))
        self._event(ticket_id, now, "已撤销", "买家", "买家撤销了申请")
        return self.get_after_sales(ticket_id, now=now)

    def _ticket(self, ticket_id, now):
        ticket = self._one("SELECT * FROM after_sales WHERE ticket_id = ?", (ticket_id,), f"售后单 {ticket_id} 不存在")
        if ticket["created_at"] > fmt(now):
            raise ShopError(f"售后单 {ticket_id} 不存在")
        return ticket

    def _history(self, ticket_id, now):
        return self.conn.execute(
            "SELECT time, status, actor, note FROM after_sales_events WHERE ticket_id = ? AND time <= ? ORDER BY time, id",
            (ticket_id, fmt(now)),
        ).fetchall()

    def _status(self, ticket_id, now):
        return self._history(ticket_id, now)[-1]["status"]

    def _ticket_brief(self, ticket_id, now):
        ticket = self._ticket(ticket_id, now)
        return {"ticket_id": ticket_id, "type": ticket["type"], "reason": ticket["reason"], "status": self._status(ticket_id, now)}

    def get_after_sales(self, ticket_id, *, now):
        ticket = self._ticket(ticket_id, now)
        history = self._history(ticket_id, now)
        status = history[-1]["status"]
        shipments = []
        for row in self.conn.execute("SELECT * FROM shipments WHERE ticket_id = ? ORDER BY created_at", (ticket_id,)):
            events = self._events(row["tracking_no"], now)
            if events:
                shipments.append(logistics.summarize(self.rules, dict(row), events, now))
        return {
            "ticket_id": ticket_id,
            "order_id": ticket["order_id"],
            "item_id": ticket["item_id"],
            "type": ticket["type"],
            "reason": ticket["reason"],
            "created_at": ticket["created_at"],
            "status": status,
            "refund_amount": ticket["refund_amount"],
            "return_fee": ticket["return_fee"],
            "fee_payer": ticket["fee_payer"],
            "rule_ids": json.loads(ticket["rule_ids"]),
            "next_step": self._next_step(ticket, status, history),
            "history": [dict(row) for row in history],
            "shipments": shipments,
        }

    def _next_step(self, ticket, status, history):
        if status in FINAL_STATUSES:
            return None
        if status == "平台处理中":
            return "平台正在处理延迟发货补偿"
        if status == "待人工审核":  # 原因在进度里：金额超过额度（P-SVC-04）或者账户售后频繁（P-RISK-01），别一概说成金额
            return f"{history[-1]['note']}；人工审核通过后继续处理"
        if status == "人工审核通过":
            return "人工审核已通过，等待商家处理"
        if status == "商家拒绝":
            return f"{history[-1]['note']}。对拒绝有异议的，可以申请平台介入，平台在 {self.rules.get('P-SVC-03', 'intervene_days')} 个工作日内处理（P-SVC-06、P-SVC-03）"
        if status == "平台介入中":
            return f"平台正在处理，{self.rules.get('P-SVC-03', 'intervene_days')} 个工作日内给出结果（P-SVC-03）"
        if status == "拦截中":
            return "已通知物流公司拦截，拦截成功后包裹退回商家"
        if status == "已拦截，退回中":
            return "包裹正在退回商家，商家收到后退款"
        if status == "商家已收到退回商品":
            return "商家已收到退回的包裹，将尽快退款"
        if status == "待商家处理":
            due = parse(ticket["created_at"]) + timedelta(hours=self.rules.get("P-RET-06", "respond_hours"))
            return f"等待商家处理，最晚 {fmt(due)} 前处理，逾期系统自动同意（P-RET-06）"
        if status == "待买家退货":
            fee = "" if ticket["fee_payer"] in (None, "商家") or not ticket["return_fee"] else f"退货运费 {ticket['return_fee']:g} 元由消费者承担。"
            return f"请寄回商品并填写退货物流单号。退货地址：{RETURN_ADDRESS}。{fee}"
        if status == "买家已退货":
            return "退货运输中，商家收到后处理"
        if status == "商家已收货":
            if ticket["type"] == "换货":
                return "商家已收到退回的商品，将发出新商品"
            due = parse(history[-1]["time"]) + timedelta(days=self.rules.get("P-RET-07", "refund_days"))
            return f"商家应在 {fmt(due)} 前退款，逾期平台自动退款（P-RET-07）"
        if status == "换货已发出":
            return "新商品运输中"
        return None
