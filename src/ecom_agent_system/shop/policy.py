"""售后政策引擎：判断一件商品能办理哪种售后、期限到哪天、运费谁出，并给出依据的规则编号。

只做判断，不读写数据库；订单和物流的状态由 service 模块整理好传进来。数值全部来自
platform_rules.yaml，改规则参数，判断结果跟着变。
"""

from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta

from ecom_agent_system.shop.db import fmt


@dataclass(frozen=True)
class ItemContext:
    """判断一件已购商品的售后所需的信息。"""

    tier: str
    zone: str
    insured: bool  # 投保了退货运费险
    amount: float  # 这件商品的实付金额
    policy_class: str | None = None  # 退货政策分类，如 食品、定作；None 为普通商品
    policy_kind: str | None = None  # 法定不适用 / 拆封后不适用 / 确认后不适用 / 激活后不适用
    class_rules: tuple = ()
    confirmed: bool = False  # 下单时确认了“不支持七天无理由退货”
    durable: bool = False
    delivered_at: datetime | None = None


@dataclass
class Decision:
    allowed: bool
    explanation: str
    rule_ids: list
    deadline: datetime | None = None
    refund_amount: float = 0.0  # 退款或补偿金额
    return_fee: float = 0.0
    fee_payer: str | None = None
    fast_refund: bool = False
    refund_days: int | None = None
    options: list = field(default_factory=list)

    def to_dict(self):
        data = asdict(self)
        data["deadline"] = fmt(self.deadline) if self.deadline else None
        return data


def _end_of_day(day):
    return datetime.combine(day, time(23, 59, 59))


def _add_months(moment, months):
    month = moment.month - 1 + months
    year, month = moment.year + month // 12, month % 12 + 1
    return moment.replace(year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1]))


def _unique(items):
    return list(dict.fromkeys(items))


# 需要说明具体问题的申请原因（P-QA-05），以及“具体问题”里常见的说法
EVIDENCE_REASONS = ("质量问题", "运输损毁", "生鲜变质")
DEFECT_WORDS = (
    "破", "裂", "坏", "损", "碎", "断", "漏", "缺", "少件", "错发", "划痕", "变形", "生锈", "污渍", "瑕疵", "褪色",
    "起球", "开线", "脱胶", "异响", "故障", "失灵", "无法", "不能", "不亮", "不响", "不转", "不制冷", "不加热",
    "充不进", "黑屏", "花屏", "死机", "进水", "发霉", "变质", "腐", "臭", "过敏",
    # 大模型扮演的顾客会这么说：压瘪、撕开、转起来卡、测不准
    "瘪", "撕", "凹", "卡顿", "卡住", "不准", "不顺", "不灵", "没反应", "无反应", "不工作", "松动", "掉漆", "歪", "异味", "不显示", "不出声", "不通电",
)


def evidence_ok(text):
    """问题描述是否具体：要说出哪里坏了、什么功能不正常，只说“质量不好”不算。"""
    return bool(text) and len(text.strip()) >= 4 and any(word in text for word in DEFECT_WORDS)


class Policy:
    def __init__(self, rules):
        self.rules = rules

    # ---------------------------------------------------------------- 七天无理由退货
    def return_days(self, tier):
        """会员对应的退货期限（天）和依据（P-RET-01、P-MEM-01）。"""
        if tier == "铂金":
            return self.rules.get("P-MEM-01", "days_platinum"), "P-MEM-01"
        if tier == "钻石":
            return self.rules.get("P-MEM-01", "days_diamond"), "P-MEM-01"
        return self.rules.get("P-RET-01", "return_days"), "P-RET-01"

    def return_fee(self, ctx):
        """七天无理由退货的运费：(金额, 承担方, 依据)。"""
        if ctx.insured:
            return 0.0, "退货运费险", "P-RET-05"
        if self.rules.tier_at_least(ctx.tier, "黄金"):
            return 0.0, "平台（会员权益）", "P-MEM-01"
        fee = self.rules.get("P-RET-05", "fee_remote" if ctx.zone == "偏远" else "fee_normal")
        return float(fee), "消费者", "P-RET-05"

    def no_reason_return(self, ctx, now, *, opened=False, activated=False):
        if ctx.delivered_at is None:
            return Decision(
                False,
                "还没有签收。七天无理由退货自签收次日起算；未发货的订单可以直接取消，已发货的可以申请拦截。",
                ["P-RET-01", "P-SHIP-04", "P-SHIP-05"],
            )
        label, kind, class_rules = ctx.policy_class, ctx.policy_kind, list(ctx.class_rules)
        if kind == "法定不适用":
            return Decision(False, f"{label}商品不适用七天无理由退货；有质量问题的可以按质量问题退换。", class_rules + ["P-QA-01"])
        if kind == "拆封后不适用" and opened:
            return Decision(False, f"已拆封的{label}不适用七天无理由退货。", class_rules)
        if kind == "确认后不适用" and ctx.confirmed:
            return Decision(False, f"{label}商品页标注了不支持七天无理由退货，下单时已确认。", class_rules)
        if kind == "激活后不适用" and ctx.confirmed and activated:
            return Decision(False, "商品已激活，商品页标注了激活后不支持七天无理由退货，下单时已确认。", class_rules)

        days, window_rule = self.return_days(ctx.tier)
        deadline = _end_of_day(ctx.delivered_at.date() + timedelta(days=days))
        if now > deadline:
            return Decision(
                False, f"已超过退货期限：签收次日起 {days} 日内，截至 {deadline:%Y-%m-%d}。", [window_rule], deadline=deadline
            )

        notes = [f"可以申请七天无理由退货，期限为签收次日起 {days} 日，截至 {deadline:%Y-%m-%d}。"]
        rule_ids = [window_rule]
        if kind == "拆封后不适用":
            notes.append(f"{label}未拆封，可以退。")
        elif kind == "激活后不适用":
            notes.append("商品未激活，可以退。" if not activated else "商品页没有标注激活后不支持退货，不能以已激活为由拒绝。")
        elif kind == "确认后不适用":
            notes.append(f"{label}商品页没有标注不支持七天无理由退货，不能以品类为由拒绝。")
        if kind is not None:
            rule_ids += class_rules
        if opened:
            notes.append("为查验商品拆开包装不影响退货。")
            rule_ids.append("P-RET-03")
        fee, payer, fee_rule = self.return_fee(ctx)
        notes.append(f"退货运费 {fee:g} 元由消费者承担。" if payer == "消费者" else f"退货运费由{payer}承担。")
        fast = self.rules.tier_at_least(ctx.tier, "铂金")
        refund_days = self.rules.get("P-RET-07", "refund_days")
        notes.append("填写退货物流单号后平台先行退款。" if fast else f"商家收到退货后 {refund_days} 日内退款。")
        rule_ids += [fee_rule, "P-RET-07"] + (["P-MEM-01"] if fast else [])
        return Decision(
            True,
            "".join(notes),
            _unique(rule_ids),
            deadline=deadline,
            refund_amount=ctx.amount,
            return_fee=fee,
            fee_payer=payer,
            fast_refund=fast,
            refund_days=refund_days,
        )

    # ---------------------------------------------------------------- 质量问题
    def quality_issue(self, ctx, now, want):
        """质量问题的退货或换货（P-QA-01）。want 为“退货”或“换货”。"""
        if ctx.delivered_at is None:
            return Decision(False, "还没有签收；运输途中丢失、损毁的由商家补发或全额退款。", ["P-SHIP-06"])
        if ctx.policy_class == "鲜活易腐":
            hours = self.rules.get("P-CAT-02", "fresh_report_hours")
            return Decision(False, f"生鲜商品在签收后 {hours} 小时内反馈腐坏、变质的，申请仅退款。", ["P-CAT-02"])
        day = (now.date() - ctx.delivered_at.date()).days
        return_days = self.rules.get("P-QA-01", "qa_return_days")
        exchange_days = self.rules.get("P-QA-01", "qa_exchange_days")
        if day <= return_days:
            options = ["退货", "换货", "维修"]
        elif day <= exchange_days:
            options = ["换货", "维修"]
        else:
            options = ["维修"]
        allowed = want in options
        if allowed:
            notes = [f"质量问题，签收后第 {day} 天，可以选择{'、'.join(options)}，往返运费由商家承担。"]
        else:
            notes = [f"质量问题，签收后第 {day} 天，只能选择{'、'.join(options)}。"]
            if want == "退货" and "换货" in options:
                notes.append("符合法定解除合同条件的仍可以退货，需要转人工判断。")
        rule_ids = ["P-QA-01"]
        if ctx.durable:
            months = self.rules.get("P-QA-02", "burden_months")
            if now <= _add_months(ctx.delivered_at, months):
                notes.append(f"耐用商品签收后 {months} 个月内发现的瑕疵由商家举证。")
                rule_ids.append("P-QA-02")
        window = return_days if want == "退货" else exchange_days
        return Decision(
            allowed,
            "".join(notes),
            rule_ids,
            deadline=_end_of_day(ctx.delivered_at.date() + timedelta(days=window)),
            refund_amount=ctx.amount if want == "退货" else 0.0,
            fee_payer="商家",
            refund_days=self.rules.get("P-RET-07", "refund_days") if want == "退货" else None,
            options=options,
        )

    # ---------------------------------------------------------------- 仅退款
    def refund_only(self, ctx, now, reason, shipping):
        """仅退款只用于运输途中丢失、损毁和生鲜变质（P-SVC-05）。shipping 是发货单在 now 的状态。"""
        status = shipping["status"] if shipping else None
        if reason == "物流异常":
            if status == "物流异常":
                return Decision(True, "物流信息长时间未更新，视为物流异常，商家应当补发或全额退款。", ["P-SHIP-07", "P-SHIP-06"], refund_amount=ctx.amount)
            days = self.rules.get("P-SHIP-07", "stall_days")
            remote = self.rules.get("P-SHIP-07", "stall_days_remote")
            return Decision(False, f"物流没有异常：物流信息连续 {days} 日（偏远地区 {remote} 日）未更新才算物流异常。", ["P-SHIP-07"])
        if reason == "运输损毁":
            if status == "已拒收":
                return Decision(True, "包装破损已拒收，按运输途中损毁处理，商家应当补发或全额退款。", ["P-SHIP-10", "P-SHIP-06"], refund_amount=ctx.amount)
            return Decision(False, "签收后发现损坏的，按质量问题申请退货或换货；签收前发现包装破损的可以拒收。", ["P-SVC-05", "P-SHIP-10", "P-QA-01"])
        hours = self.rules.get("P-CAT-02", "fresh_report_hours")
        if ctx.policy_class == "鲜活易腐" and ctx.delivered_at and now - ctx.delivered_at <= timedelta(hours=hours):
            return Decision(
                True,
                f"生鲜商品签收后 {hours} 小时内反馈变质，提供照片后商家应当退款。",
                ["P-CAT-02"],
                deadline=ctx.delivered_at + timedelta(hours=hours),
                refund_amount=ctx.amount,
            )
        return Decision(False, f"生鲜商品需要在签收后 {hours} 小时内反馈腐坏、变质。", ["P-CAT-02"])

    # ---------------------------------------------------------------- 发货
    def ship_time(self, entered_at, picked_at, now):
        """按 P-SHIP-03 认定的发货时间。

        填写单号后 24 小时内揽收的，以填写时间为准；超过 24 小时才揽收的，以揽收时间为准；
        还没揽收且已超过 24 小时的，视为未发货（返回 None）。
        """
        if entered_at is None or entered_at > now:
            return None
        limit = entered_at + timedelta(hours=self.rules.get("P-SHIP-03", "fake_ship_hours"))
        if picked_at is not None and picked_at <= now:
            return entered_at if picked_at <= limit else picked_at
        return entered_at if now <= limit else None

    def delay_compensation(self, amount):
        rate = self.rules.get("P-SHIP-02", "delay_rate_pct") / 100
        low, high = self.rules.get("P-SHIP-02", "delay_min"), self.rules.get("P-SHIP-02", "delay_max")
        return round(min(max(amount * rate, low), high), 2)

    def shipping_delay(self, paid_at, shipped_at, now, amount):
        """是否超过发货时效（P-SHIP-01、P-SHIP-02）。shipped_at 是认定的发货时间，未发货为 None。"""
        deadline = paid_at + timedelta(hours=self.rules.get("P-SHIP-01", "ship_hours"))
        if (shipped_at or now) <= deadline:
            return Decision(False, f"没有超过发货时效：商家应在 {fmt(deadline)} 前发货。", ["P-SHIP-01"], deadline=deadline)
        compensation = self.delay_compensation(amount)
        notes = [f"超过发货时效（应在 {fmt(deadline)} 前发货）"]
        options = ["延迟发货补偿"]
        if shipped_at is None:
            notes.append("，至今未发货")
            cancel_from = deadline + timedelta(hours=self.rules.get("P-SHIP-02", "delay_cancel_hours"))
            if now > cancel_from:
                notes.append("，已超过发货时效 72 小时，可以取消订单并获得全额退款，补偿照常赔付")
                options.append("取消订单")
        notes.append(f"。可以申请延迟发货补偿 {compensation:g} 元。")
        return Decision(True, "".join(notes), ["P-SHIP-01", "P-SHIP-02"], deadline=deadline, refund_amount=compensation, options=options)

    def evidence(self, reason, text):
        """质量问题、运输损毁、生鲜变质需要说明具体问题（P-QA-05）。不需要或者合格时返回 None。"""
        if reason not in EVIDENCE_REASONS or evidence_ok(text):
            return None
        return Decision(
            False,
            "按" + reason + "申请需要说明具体问题，例如哪里破损、什么功能失灵，并提供照片或视频；只是不想要了的，可以申请七天无理由退货。",
            ["P-QA-05", "P-RET-01"],
        )

    def review_tier(self, kind, amount, *, high_risk=False):
        """智能客服的办理权限（P-SVC-04）：直接办理、提交人工审核，还是必须转人工。只管不退货的钱。

        high_risk 是 P-RISK-01 的风控标识：售后频繁的账户，不论金额都先人工审核。
        """
        if kind not in ("仅退款", "延迟发货补偿"):
            return "直接办理"
        if amount > self.rules.get("P-SVC-04", "escalate_amount"):
            return "转人工"
        if amount > self.rules.get("P-SVC-04", "auto_refund_limit") or high_risk:
            return "人工审核"
        return "直接办理"

    def intercept(self, ctx, paid_at, shipping, now, reason):
        """已发货、未签收的包裹申请拦截（P-SHIP-05）。reason 为“顾客原因”或“商家原因”。"""
        if shipping["shipped_at"] is None:
            return Decision(False, "商品还没有发货，可以直接取消订单，平台立即全额退款。", ["P-SHIP-04"])
        if ctx.delivered_at is not None:
            return Decision(False, "包裹已经签收，不能拦截；可以申请七天无理由退货。", ["P-SHIP-05", "P-RET-01"])
        status = (shipping["summary"] or {}).get("status")
        if status == "物流异常":
            return Decision(False, "物流已经异常，直接申请仅退款（物流异常）更合适，不需要拦截。", ["P-SHIP-07", "P-SHIP-06"])
        if status == "已拒收":
            return Decision(False, "包裹已拒收，按运输损毁申请仅退款。", ["P-SHIP-10", "P-SHIP-06"])
        late = self.shipping_delay(paid_at, shipping["shipped_at"], now, ctx.amount).allowed
        if reason == "商家原因":
            if not late:
                return Decision(False, "商家没有延迟发货，只能按顾客原因申请拦截，往返运费由消费者承担。", ["P-SHIP-05", "P-SHIP-01"])
            return Decision(
                True,
                "商家延迟发货，可以申请拦截，往返运费由商家承担；拦截成功、商品退回商家后全额退款。拦截前包裹已经签收的，拦截失败。",
                ["P-SHIP-05", "P-SHIP-02"], refund_amount=ctx.amount, fee_payer="商家",
            )
        fee = 2 * self.rules.get("P-RET-05", "fee_remote" if ctx.zone == "偏远" else "fee_normal")
        return Decision(
            True,
            f"可以申请拦截。拦截成功、商品退回商家后退款，往返运费 {fee:g} 元由消费者承担，从退款中扣除。"
            "拦截前包裹已经签收的，拦截失败，可以申请七天无理由退货。",
            ["P-SHIP-05", "P-RET-05"], refund_amount=round(max(ctx.amount - fee, 0.0), 2), return_fee=float(fee), fee_payer="消费者",
        )

    def change_address(self, entered_at, cancelled):
        """发货前修改收货地址（P-SHIP-11）。entered_at 是商家填写运单号的时间，没填为 None。"""
        if cancelled:
            return Decision(False, "订单已经取消。", ["P-SHIP-04"])
        if entered_at is not None:
            return Decision(False, "商家已经发货，不能修改地址；可以申请拦截，退款后重新下单。", ["P-SHIP-11", "P-SHIP-05"])
        return Decision(True, "商家还没有发货，可以修改收货地址里的详细地址（同一区县内）。", ["P-SHIP-11"])

    def cancel_order(self, paid_at, shipped_at, now, amount):
        """发货前取消订单（P-SHIP-04）；已发货的只能拦截（P-SHIP-05）。"""
        if shipped_at is not None:
            return Decision(False, "商品已经发货，不能直接取消；可以申请拦截，或者签收后申请退货。", ["P-SHIP-04", "P-SHIP-05"])
        delay = self.shipping_delay(paid_at, None, now, amount)
        reason = "商品还没有发货，可以取消订单，平台立即全额退款。"
        if delay.allowed:
            reason += f"已超过发货时效，延迟发货补偿 {delay.refund_amount:g} 元照常赔付。"
        return Decision(True, reason, _unique(["P-SHIP-04"] + (delay.rule_ids if delay.allowed else [])), refund_amount=amount)
