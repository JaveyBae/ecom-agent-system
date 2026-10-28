"""回放页的时间轴：把一段售后对话记录拆成一帧一帧，左边画顾客手机上看到的，右边画开发者看到的。

每一帧是顾客说话、一次工具调用或 agent 回复。工具调用这一帧带上：过了哪几道关、卡在哪一关（按被拦下的
原因对到关卡）、结果里值得看的几项（判断结论、金额、规则编号、办理权限档），以及顾客手机上会出现的
确认卡片或系统提示。被拦下的调用不会走到确认卡片，所以只出现在开发者那一栏。
"""

from __future__ import annotations

import json

# 办理类工具的关卡顺序，和 agent/tools.py 一致；查询类只过参数和身份
WRITE_GATES = ("参数", "身份", "政策", "凭证", "权限 / 风控", "确认卡片", "执行")
READ_GATES = ("参数", "身份", "查询")
# 被拦下的原因 → 卡在哪一关（下标）
FAILED_AT = {"参数错误": 0, "越权": 1, "业务错误": 2, "政策不允许": 2, "凭证不足": 3, "需转人工": 4, "顾客未确认": 5, "工具错误": 6}
WRITES = {
    "create_after_sales", "cancel_after_sales", "cancel_order", "change_address",
    "urge_order", "request_platform_intervention", "transfer_to_human", "remember_note", "buy_now",
}
CONFIRMED = {"create_after_sales", "cancel_after_sales", "cancel_order", "change_address", "request_platform_intervention", "buy_now"}
EVIDENCE = {"create_after_sales"}  # 只有提交售后要看凭证
POLICY = {"create_after_sales", "cancel_order", "change_address", "request_platform_intervention", "cancel_after_sales"}
CARD_TITLES = {
    "create_after_sales": "提交售后申请", "cancel_after_sales": "撤销售后申请", "cancel_order": "取消订单",
    "change_address": "修改收货地址", "request_platform_intervention": "申请平台介入", "buy_now": "确认下单",
}


def _parse(content):
    try:
        return json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return content


def _money(value):
    return None if value is None else f"{value:g} 元"


def _gates(name, failed=None, skipped=False):
    """每道关的状态：pass 过了、fail 卡在这、todo 没走到、na 这个工具不过这一关。"""
    labels = WRITE_GATES if name in WRITES else READ_GATES
    states = []
    for index, label in enumerate(labels):
        if name in WRITES and ((label == "凭证" and name not in EVIDENCE) or (label == "政策" and name not in POLICY)
                               or (label == "确认卡片" and name not in CONFIRMED)):
            states.append({"label": label, "state": "na"})
        elif skipped:
            states.append({"label": label, "state": "todo"})
        elif failed is None or index < failed:
            states.append({"label": label, "state": "pass"})
        elif index == failed:
            states.append({"label": label, "state": "fail"})
        else:
            states.append({"label": label, "state": "todo"})
    return states


def _facts(name, arguments, result):
    """结果里值得开发者一眼看到的几项。"""
    if not isinstance(result, dict):
        return []
    facts = []
    decision = result.get("decision") if isinstance(result.get("decision"), dict) else None
    source = decision or result
    if name == "check_after_sales" or decision:
        facts += [("能不能办", "可以" if source.get("allowed") else "不可以"), ("说明", source.get("explanation")),
                  ("金额", _money(source.get("refund_amount"))), ("截止", source.get("deadline")),
                  ("办理权限", source.get("authority"))]
    elif name in ("get_order", "list_orders"):
        orders = result.get("orders") if name == "list_orders" else [result]
        for order in (orders or [])[:3]:
            title = ((order.get("items") or [{}])[0].get("title") or "")[:18]
            facts.append((order.get("order_id"), f"{order.get('status')} · {_money(order.get('total_amount'))} · {title}"))
    elif name == "track_shipment":
        facts.append(("物流", result.get("status")))
        for shipment in result.get("shipments") or []:
            facts += [("提示", note) for note in shipment.get("notes") or []]
        facts += [("权限", note) for note in result.get("notes") or []]
    elif name == "create_after_sales":
        facts += [("售后单", result.get("ticket_id")), ("状态", result.get("status")), ("金额", _money(result.get("refund_amount"))),
                  ("下一步", result.get("next_step"))]
    elif name == "transfer_to_human":
        facts += [("工单", result.get("work_order_id")), ("摘要", arguments.get("summary"))]
    elif name == "urge_order":
        facts += [("工单", result.get("work_order_id")), ("类型", result.get("kind"))]
    rules = source.get("rule_ids") or []
    if rules:
        facts.append(("规则", "、".join(rules)))
    return [{"label": label, "value": value} for label, value in facts if value not in (None, "", [])]


def _card(name, arguments, result):
    """顾客手机上的确认卡片：要确认的内容和确认后的结果。"""
    fields = {
        "create_after_sales": [("订单", arguments.get("order_id")), ("类型", arguments.get("type")), ("原因", arguments.get("reason")),
                               ("金额", _money(result.get("refund_amount")))],
        "cancel_order": [("订单", arguments.get("order_id"))],
        "cancel_after_sales": [("售后单", arguments.get("ticket_id"))],
        "change_address": [("订单", arguments.get("order_id")), ("新地址", arguments.get("address"))],
        "request_platform_intervention": [("售后单", arguments.get("ticket_id")), ("说明", arguments.get("statement"))],
        "buy_now": [("商品", (result.get("title") or arguments.get("asin") or "")[:30]), ("数量", arguments.get("quantity")),
                    ("总价", _money(result.get("amount") or result.get("total_amount")))],
    }[name]
    outcome = None
    if result.get("ticket_id"):
        outcome = f"售后单 {result['ticket_id']} · {result.get('status')}"
    elif result.get("order_id") and name == "buy_now":
        outcome = f"已下单 {result['order_id']}"
    elif result.get("status"):
        outcome = str(result["status"])
    return {"title": CARD_TITLES[name], "fields": [{"label": k, "value": str(v)} for k, v in fields if v not in (None, "")],
            "outcome": outcome}


def _notice(name, result):
    if name == "transfer_to_human":
        return "正在为您转接人工客服，请稍候"
    if name == "urge_order":
        return f"已提醒商家处理（工单 {result.get('work_order_id')}）"
    return None


def build_timeline(record):
    """一段对话记录 → 帧列表。tool 帧的 status：ok 办成、blocked 被关卡拦下、skipped 转人工后没执行。"""
    blocked = list(record.get("blocked") or [])
    results = {m.get("tool_call_id"): _parse(m.get("content")) for m in record["messages"] if m.get("role") == "tool"}
    frames, turn = [], 0
    for message in record["messages"]:
        role = message.get("role")
        if role == "user":
            frames.append({"type": "customer", "text": message.get("content") or ""})
        elif role == "assistant":
            calls = message.get("tool_calls") or []
            if message.get("content"):  # 和工具调用一起发的话（“我帮您查一下”）先说
                frames.append({"type": "reply", "text": message["content"]})
            if calls:
                turn += 1
            for call in calls:
                function = call.get("function") or {}
                name = function.get("name")
                arguments = _parse(function.get("arguments") or "{}")
                arguments = arguments if isinstance(arguments, dict) else {"raw": arguments}
                result = results.get(call.get("id"))
                frame = {"type": "tool", "turn": turn, "tool": name, "arguments": arguments, "result": result,
                         "write": name in WRITES, "status": "ok", "outcome": None, "message": None}
                error = result.get("error") if isinstance(result, dict) else None
                if error:
                    match = next((i for i, b in enumerate(blocked) if b["tool"] == name), None)
                    entry = blocked.pop(match) if match is not None else {"outcome": "业务错误", "message": error}
                    frame.update(outcome=entry["outcome"], message=entry["message"])
                    frame["status"] = "skipped" if entry["outcome"] == "转人工后未执行" else "blocked"
                    frame["gates"] = _gates(name, skipped=frame["status"] == "skipped", failed=FAILED_AT.get(entry["outcome"], 2))
                else:
                    frame["gates"] = _gates(name)
                    if name in CONFIRMED and isinstance(result, dict):
                        frame["card"] = _card(name, arguments, result)
                    frame["notice"] = _notice(name, result if isinstance(result, dict) else {})
                frame["facts"] = _facts(name, arguments, result)
                frames.append(frame)
    frames.append({"type": "end", "ended": record.get("ended"), "error": record.get("error")})
    return frames


def grade_rows(grade):
    """评分卡：每项检查过没过、是不是硬性要求。"""
    hard = set(grade.get("hard") or [])
    return [{"name": name, "passed": bool(passed), "hard": name in hard} for name, passed in (grade.get("checks") or {}).items()]
