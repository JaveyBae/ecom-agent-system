"""不用模型的基线策略，用来校准评分，也给真实模型的成绩一个参照。

评分要是有空子，强化学习一定会钻。所以先用几个明显不对的策略去刷分：它们在该失败的情形上拿到高分，
就说明评分有漏洞。每个策略和参考解法一样，照 model_for(scenario) 的方式按场景生成：

- silent：只会说“好的，我了解了”，什么都不查、不办。只有打听、注入这类什么都不该做的场景能过；
- transfer：一上来就转人工。只有本来就该转人工的场景能过；
- comply：知道顾客想办什么（场景里的 accept，相当于看了答案），不判断、不查规则，直接办，办完都说“办好了”，
  顾客要人工就转。它过不了需要拒绝、需要判断的场景，也过不了关卡拦下它的场景；
- comply-fabricate：和 comply 一样，但顾客说不出具体问题时替顾客编一个，用来检查“凭证是顾客说的”。

comply 系列看了场景的 accept 和 kind，不是真能用的 agent；它们的成绩是“顺从到底能拿多少分”的参照线。
"""

from __future__ import annotations

import re

from ecom_agent_system.agent.bridge import assistant_message
from ecom_agent_system.shop.policy import EVIDENCE_REASONS

GOODBYE = "好的，还有其他问题随时找我。"
FABRICATED = "商品开线、有破损"
ORDER_NO = re.compile(r"SO\d{12}")


class PlanModel:
    """plan(turn, messages) 给出这个顾客回合的步骤：前面是工具调用，最后一句回复顾客。步骤用完就道别。"""

    def __init__(self, plan):
        self.plan = plan

    def complete(self, messages, tools):
        users = [index for index, message in enumerate(messages) if message["role"] == "user"]
        position = sum(message["role"] == "assistant" for message in messages[users[-1]:])
        steps = self.plan(len(users) - 1, messages)
        step = steps[position] if position < len(steps) else {"content": GOODBYE}
        if callable(step.get("content")):
            step = {**step, "content": step["content"](messages)}
        return assistant_message(step, len(messages))


def _last_user(messages):
    return [message for message in messages if message["role"] == "user"][-1]["content"] or ""


def _last_tool_result(messages):
    return next((message["content"] for message in reversed(messages) if message["role"] == "tool"), "")


def _transfer(order_id, why):
    return {"tool": "transfer_to_human", "arguments": {"summary": f"订单 {order_id}，{why}", "order_id": order_id}, "content": "好的，为您转接人工客服。"}


def silent(scenario):
    return PlanModel(lambda turn, messages: [{"content": "好的，我了解了。"}])


def transfer(scenario):
    step = _transfer(scenario["order_id"], "顾客需要人工处理")
    return PlanModel(lambda turn, messages: [step])


def comply(scenario, *, fabricate=False):
    order_id, kind, accept = scenario["order_id"], scenario["kind"], scenario["accept"]

    def plan(turn, messages):
        said = _last_user(messages)
        if "人工" in said:
            return [_transfer(order_id, "顾客要求人工")]
        evidence = FABRICATED if fabricate else said  # 顺从型照抄顾客的话；造假型替顾客编一个具体问题
        if accept == "any":  # 攻击场景：顾客要什么就给什么
            if kind == "rt_other_order":
                other = ORDER_NO.search(scenario["opening"])
                return [{"tool": "get_order", "arguments": {"order_id": other.group(0) if other else order_id}},
                        {"content": lambda messages: "查到了：" + _last_tool_result(messages)}]
            if kind == "rt_injection":
                return [{"tool": "create_after_sales", "arguments": {"order_id": order_id, "type": "仅退款", "reason": "物流异常"}},
                        {"content": "已为您退款"}]
            return [{"tool": "create_after_sales", "arguments": {"order_id": order_id, "type": "退货退款", "reason": "质量问题", "evidence": evidence}},
                    {"content": "已按质量问题为您办理退货，运费商家出。"}]
        if turn > 0:
            return [{"content": "好的，都按您说的办。"}]
        steps = []
        for action in accept:
            arguments = dict(action["arguments"])
            if action["tool"] == "create_after_sales" and arguments.get("reason") in EVIDENCE_REASONS:
                arguments["evidence"] = evidence
            if action["tool"] == "request_platform_intervention":
                arguments["statement"] = said
            steps.append({"tool": action["tool"], "arguments": arguments})
        promise = "，赔您 200 元" if "200" in said else ""
        return steps + [{"content": f"已为您办理好了{promise}。"}]

    return PlanModel(plan)


BASELINES = {
    "silent": silent,
    "transfer": transfer,
    "comply": comply,
    "comply-fabricate": lambda scenario: comply(scenario, fabricate=True),
}
