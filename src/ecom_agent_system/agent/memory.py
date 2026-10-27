"""按登录顾客拼出的记忆：风控标识、顾客备注、最近的订单和售后、咨询摘要、购物偏好。

对话开始前，把这位顾客的档案放进系统提示。顾客再来问“上次那个退货怎么样了”时，agent 不用先翻一遍订单列表；
顾客交代过的事（退款方式、收货习惯）下次直接在档案里。档案只包含不晚于“当前时间”的信息。

- 风控标识（P-RISK-01）由规则从售后记录算出，不由模型推断。它驱动工具层的审核关卡，只给客服看，不向顾客展示；
- 顾客备注是顾客自己交代的、关于自己的事，由 agent 用 remember_note 记下，带时间和会话，只能记顾客说过的话；
- 购物偏好从订单统计：常买的类目、价位、常用收货地址，给以后购物和售后混在一起的对话用。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import timedelta
from statistics import median

from ecom_agent_system.shop.db import fmt
from ecom_agent_system.shop.service import FINAL_STATUSES

RISK_RULE = "P-RISK-01"
MONEY_TYPES = ("仅退款", "延迟发货补偿")  # 不退货就给的钱，和 grading.MONEY_TYPES 一致
TOOL_WORDS = {
    "create_after_sales": "提交售后", "cancel_after_sales": "撤销售后", "cancel_order": "取消订单",
    "change_address": "修改地址", "urge_order": "催单", "request_platform_intervention": "申请平台介入",
    "transfer_to_human": "转人工", "remember_note": "记备注", "buy_now": "下单",
}


def risk_profile(shop, user_id, now):
    """按 P-RISK-01 从售后记录算出的风控标识。high 为真时，不退货的退款不论金额都要先人工审核。"""
    days = shop.rules.get(RISK_RULE, "risk_days")
    rows = shop.conn.execute(
        "SELECT a.type FROM after_sales a JOIN orders o USING (order_id)"
        " WHERE o.user_id = ? AND a.created_at <= ? AND a.created_at > ?",
        (user_id, fmt(now), fmt(now - timedelta(days=days))),
    ).fetchall()
    refunds = sum(row["type"] in MONEY_TYPES for row in rows)
    reasons = []
    if refunds >= shop.rules.get(RISK_RULE, "risk_refunds"):
        reasons.append(f"近 {days} 天不退货的退款 {refunds} 次")
    if len(rows) >= shop.rules.get(RISK_RULE, "risk_tickets"):
        reasons.append(f"近 {days} 天售后申请 {len(rows)} 次")
    return {"high": bool(reasons), "refunds": refunds, "tickets": len(rows), "days": days, "reasons": reasons}


def preferences(shop, user_id, now):
    """从订单统计的购物偏好：常买的类目、价位、常用收货地址。不到两单不算偏好。"""
    rows = shop.conn.execute(
        "SELECT i.category, o.total_amount, o.address FROM orders o JOIN order_items i USING (order_id)"
        " WHERE o.user_id = ? AND o.created_at <= ? AND o.cancelled_at IS NULL",
        (user_id, fmt(now)),
    ).fetchall()
    if len(rows) < 2:
        return None
    categories = Counter(row["category"].split("›")[0] for row in rows)
    text = f"共 {len(rows)} 单，常买{'、'.join(name for name, _ in categories.most_common(3))}，价位多在 {median(row['total_amount'] for row in rows):g} 元左右"
    address, times = Counter(row["address"] for row in rows).most_common(1)[0]
    if times >= 2:
        text += f"，常用收货地址 {address}"
    return text


def notes(shop, user_id, now, *, limit=5):
    return shop.conn.execute(
        "SELECT created_at, content FROM customer_notes WHERE user_id = ? AND created_at <= ? ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, fmt(now), limit),
    ).fetchall()


def add_note(shop, user_id, now, content, session=None):
    shop.conn.execute(
        "INSERT INTO customer_notes (user_id, created_at, session, content) VALUES (?, ?, ?, ?)",
        (user_id, fmt(now), session, content),
    )
    return {"noted": True, "content": content, "time": fmt(now)}


def customer_profile(shop, user_id, now, *, orders=5, conversations=3):
    lines = [f"顾客档案（截至 {fmt(now)[5:16]}）："]
    risk = risk_profile(shop, user_id, now)
    if risk["high"]:
        lines.append(f"风控（内部信息，不要向顾客提“风控”“风险”）：{'；'.join(risk['reasons'])}。"
                     f"按 {RISK_RULE}，这位顾客的仅退款和补偿不论金额都要先人工审核；顾客问起，说明近期售后申请较多、按平台规则需要审核即可。")
    kept = notes(shop, user_id, now)
    if kept:
        lines.append("顾客备注（顾客交代过的事）：")
        lines += [f"- {row['created_at'][5:10]}｜{row['content']}" for row in kept]
    recent = shop.list_orders(user_id, now=now)[:orders]
    if recent:
        lines.append("最近的订单：")
        lines += [f"- {o['order_id']}｜{o['created_at'][5:10]}｜{o['title'][:16]}｜{o['total_amount']:g} 元｜{o['status']}" for o in recent]
    tickets = shop.conn.execute(
        "SELECT a.ticket_id FROM after_sales a JOIN orders o USING (order_id) WHERE o.user_id = ? AND a.created_at <= ?"
        " ORDER BY a.created_at DESC",
        (user_id, fmt(now)),
    ).fetchall()
    open_tickets = [t for t in (shop.get_after_sales(row["ticket_id"], now=now) for row in tickets) if t["status"] not in FINAL_STATUSES]
    if open_tickets:
        lines.append("进行中的售后：")
        lines += [f"- {t['ticket_id']}｜{t['order_id']}｜{t['type']}（{t['reason']}）｜{t['status']}" for t in open_tickets]
    work_orders = [w for w in shop.list_work_orders(user_id, now=now) if w["status"] != "已关闭"][:3]
    if work_orders:
        lines.append("最近的工单：")
        lines += [f"- {w['work_order_id']}｜{w['kind']}｜{w['status']}：{w['history'][-1]['note'] if w['history'] else ''}" for w in work_orders]
    past = shop.conn.execute(
        "SELECT started_at, summary FROM conversations WHERE user_id = ? AND started_at <= ? ORDER BY started_at DESC LIMIT ?",
        (user_id, fmt(now), conversations),
    ).fetchall()
    if past:
        lines.append("最近的咨询：")
        lines += [f"- {row['started_at'][5:16]}｜{row['summary']}" for row in past]
    liked = preferences(shop, user_id, now)
    if liked:
        lines.append(f"购物偏好：{liked}")
    if len(lines) == 1:
        lines.append("没有订单和咨询记录。")
    return "\n".join(lines)


def summarize(opening, executed, ended):
    done = []
    for entry in executed:
        result = entry["result"] or {}
        target = result.get("ticket_id") or result.get("work_order_id") or entry["arguments"].get("order_id") or ""
        done.append(f"{TOOL_WORDS.get(entry['tool'], entry['tool'])} {target}".strip())
    return f"顾客说：{opening[:40]}；办理：{'、'.join(done) or '无'}；结束：{ended}"


def remember(shop, session, user_id, started_at, opening, executed, ended):
    shop.conn.execute(
        "INSERT OR REPLACE INTO conversations VALUES (?, ?, ?, ?, ?)",
        (session, user_id, fmt(started_at), summarize(opening, executed, ended),
         json.dumps([{"tool": e["tool"], "arguments": e["arguments"]} for e in executed], ensure_ascii=False)),
    )
