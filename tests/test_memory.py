"""按登录顾客的记忆：风控标识驱动审核关卡、备注只记顾客说过的话、档案里有风控、备注和购物偏好。"""

import json
from datetime import datetime, timedelta

from ecom_agent_system.agent.bridge import assistant_message
from ecom_agent_system.agent.conversation import run_conversation, system_prompt
from ecom_agent_system.agent.customers import ScriptedCustomer
from ecom_agent_system.agent.memory import customer_profile, preferences, risk_profile, summarize
from ecom_agent_system.agent.tools import GATES, Toolbox
from ecom_agent_system.shop import logistics
from ecom_agent_system.shop.db import parse

T0 = datetime(2026, 10, 1, 10, 0, 0)
BLACK = {"颜色分类": "黑色"}


def yes(name, arguments):
    return True


def _lost_order(shop, user, amount, day):
    """一个丢件的订单，和顾客该来要退款的时刻。"""
    order_id = shop.place_order(user, "A1", BLACK, now=T0 + timedelta(days=day), scenario="丢件", unit_price=amount)
    last = parse(shop.conn.execute(
        "SELECT MAX(e.time) FROM shipment_events e JOIN shipments s USING (tracking_no) WHERE s.order_id = ?", (order_id,)
    ).fetchone()[0])
    return order_id, last + timedelta(days=logistics.stall_days(shop.rules, "一般"), hours=1)


def _refund(shop, knowledge_base, user, order_id, now, **kwargs):
    toolbox = Toolbox(shop, knowledge_base, user, now, yes, **kwargs)
    return toolbox, toolbox.call("create_after_sales", {"order_id": order_id, "type": "仅退款", "reason": "物流异常"})


def _statuses(ticket):
    return [event["status"] for event in ticket["history"]]


def test_frequent_refunds_flag_the_account_and_force_review(shop, knowledge_base):
    """P-RISK-01：近 90 天两次不退货的退款以后，30 元的仅退款也要先人工审核；关掉 risk 这道关就不用。"""
    orders = sorted((_lost_order(shop, "U000001", 30, day) for day in range(4)), key=lambda pair: pair[1])  # 按顾客来的先后
    first, a = _refund(shop, knowledge_base, "U000001", *orders[0])
    second, b = _refund(shop, knowledge_base, "U000001", *orders[1])
    assert not first.risk["high"] and not second.risk["high"]
    assert "待人工审核" not in _statuses(a) and "待人工审核" not in _statuses(b)  # 30 元在额度内，直接办

    open_gate = Toolbox(shop, knowledge_base, "U000001", orders[2][1], yes, gates=set(GATES) - {"risk"})
    assert open_gate.risk["high"] and open_gate.risk["refunds"] == 2
    direct = open_gate.call("create_after_sales", {"order_id": orders[2][0], "type": "仅退款", "reason": "物流异常"})
    assert "待人工审核" not in _statuses(direct)  # 关掉风控那一关，照旧直接办

    guarded, reviewed = _refund(shop, knowledge_base, "U000001", *orders[3])
    assert guarded.risk["high"] and _statuses(reviewed) == ["待商家处理", "待人工审核"]
    assert "P-RISK-01" in json.dumps(reviewed["history"], ensure_ascii=False)  # 进度里写的是风控原因，不是金额超额
    profile = customer_profile(shop, "U000001", orders[3][1])
    assert "风控" in profile and "P-RISK-01" in profile and "不要向顾客提" in profile
    assert "风控" not in customer_profile(shop, "U000001", T0 + timedelta(hours=1))  # 还没有任何售后记录的时候


def test_notes_only_keep_what_the_customer_said_and_show_up_next_time(shop, knowledge_base):
    for day in range(2):
        shop.place_order("U000002", "A2", {}, now=T0 + timedelta(days=day), scenario="正常")
    now = T0 + timedelta(days=3)
    toolbox = Toolbox(shop, knowledge_base, "U000002", now, yes, session="s1")
    guessed = toolbox.call("remember_note", {"content": "顾客大概是做生意的，以后寄到公司"})
    assert "顾客自己说过的话" in guessed["error"] and toolbox.blocked[-1]["outcome"] == "凭证不足"
    toolbox.hear("对了，以后我的快递都寄到公司，文三路88号3幢501室，帮我记一下。")
    note = toolbox.call("remember_note", {"content": "以后快递都寄到公司：文三路 88 号 3 幢 501 室"})
    assert note["noted"] and toolbox.executed[-1]["tool"] == "remember_note"
    assert shop.conn.execute("SELECT outcome FROM audit_log WHERE tool = 'remember_note' ORDER BY id").fetchall()[-1][0] == "已执行"

    later = customer_profile(shop, "U000002", now + timedelta(days=1))
    assert "顾客备注" in later and "文三路 88 号" in later and "购物偏好" in later and "共 2 单" in later
    assert "文三路" not in customer_profile(shop, "U000002", now - timedelta(hours=1))  # 记之前的时刻看不到
    assert preferences(shop, "U000001", now) is None  # 没有订单，不算偏好
    assert "记备注" in summarize("在吗", toolbox.executed, "customer_done")


def test_the_conversation_loop_feeds_what_the_customer_says_to_the_toolbox(shop, knowledge_base):
    shop.place_order("U000002", "A2", {}, now=T0, scenario="正常")

    class Model:
        def __init__(self):
            self.steps = [{"tool": "remember_note", "arguments": {"content": "以后快递都寄到公司：文三路88号"}}, {"content": "记下了。"}]

        def complete(self, messages, tools):
            return assistant_message(self.steps.pop(0), len(messages))

    toolbox = Toolbox(shop, knowledge_base, "U000002", T0 + timedelta(hours=2), yes)
    customer = ScriptedCustomer({"opening": "以后我的快递都寄到公司，文三路88号，帮我记一下。", "follow_ups": [], "accept": []})
    result = run_conversation(toolbox, Model(), customer)
    assert [e["tool"] for e in toolbox.executed] == ["remember_note"] and result["ended"] == "customer_done"
    assert "remember_note" in system_prompt(shop, "U000002", T0) and "风控信息只给你看" in system_prompt(shop, "U000002", T0)
