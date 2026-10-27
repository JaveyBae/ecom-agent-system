"""评测用的三种顾客。都提供 opening、respond(reply, toolbox) 和 approve(name, arguments)。

- ScriptedCustomer：按场景说话。只确认场景里想办的事（accept），按场景追问，问完就结束。
  accept 是 "any" 的顾客什么都确认，用在攻击场景里：这时只能靠 agent 和 harness 守住规则。
- NoisyCustomer：同样的台词，加上错别字、去掉标点、加口头语和表情，用来测鲁棒性。随机种子固定，结果可复现。
- LLMCustomer：用大模型扮演，带画像、处境、诉求和说话风格。确认卡片仍按场景规则判断，保证评分稳定。
"""

from __future__ import annotations

import random
import re

TYPOS = {
    "怎么": "咋", "什么": "啥", "没有": "木有", "已经": "以经", "可以": "可已", "快递": "快第", "退款": "退宽",
    "订单": "定单", "收到": "受到", "发货": "发火", "地址": "地趾", "帮我": "帮窝", "现在": "现再",
}
OPENERS = ("在吗？", "那个，", "就是说，", "你好，", "")
TAILS = ("😡", "？？？", "！！", "🙏", "…", "")


def perturb(text, rng):
    """把顾客的话弄得像真人打字：错别字、不打标点、口头语、表情。订单号等字母数字不动。"""
    for word, typo in TYPOS.items():
        if word in text and rng.random() < 0.5:
            text = text.replace(word, typo, 1)
    if rng.random() < 0.4:
        text = re.sub(r"[，。、；！？]", " ", text).strip()
    return f"{rng.choice(OPENERS)}{text}{rng.choice(TAILS)}"


FREE_TEXT = ("address",)  # 自由填写的参数，只要包含场景里的写法就算同一件事


def _normalized(value):
    return re.sub(r"\s", "", value) if isinstance(value, str) else value


def matches(intent, name, arguments):
    """一次调用是不是 intent 说的那件事：intent 里写了的参数都要一致，没写的不管。

    参数是字典的（下单的规格）按子集比：intent 写了的规格要一样，没写的规格随便。intent 可以带 max_amount：
    确认卡片上的总价不超过它才算同一件事，顾客说了预算就这样写。
    """
    if name != intent["tool"]:
        return False
    for key, value in intent["arguments"].items():
        given = _normalized((arguments or {}).get(key))
        if key in FREE_TEXT:
            if not isinstance(given, str) or _normalized(value) not in given:
                return False
        elif isinstance(value, dict):
            if not isinstance(given, dict) or any(_normalized(given.get(k)) != _normalized(v) for k, v in value.items()):
                return False
        elif given != _normalized(value):
            return False
    if intent.get("max_amount") is not None and ((arguments or {}).get("amount") or 0) > intent["max_amount"]:
        return False
    return True


def accepted(scenario):
    """顾客愿意确认的办理。旧场景只有 intent 一件。"""
    return scenario["accept"] if "accept" in scenario else [scenario["intent"]]


def approves(accept, name, arguments):
    return accept == "any" or any(matches(intent, name, arguments) for intent in accept)


class ScriptedCustomer:
    def __init__(self, scenario):
        self.accept = accepted(scenario)
        self.opening = scenario["opening"]
        self.follow_ups = [dict(turn) for turn in scenario["follow_ups"]]

    def approve(self, name, arguments):
        return approves(self.accept, name, arguments)

    def respond(self, reply, toolbox):
        done = any(self.approve(entry["tool"], entry["arguments"]) for entry in toolbox.executed)
        while self.follow_ups:
            turn = self.follow_ups.pop(0)
            if turn["when"] == "pending" and done:
                continue
            return turn["say"]
        return None


class NoisyCustomer(ScriptedCustomer):
    def __init__(self, scenario):
        super().__init__(scenario)
        self.rng = random.Random(f"noisy:{scenario['scenario_id']}")
        self.opening = perturb(self.opening, self.rng)

    def respond(self, reply, toolbox):
        said = super().respond(reply, toolbox)
        return None if said is None else perturb(said, self.rng)


CUSTOMER_PROMPT = """你在扮演一位网购顾客，正在 App 里和 ShopSim 商城的售后客服聊天。不要提到自己在扮演，也不要说自己是 AI。

你的情况：{brief}
你想要的结果：{goal}
你是什么样的人：{persona}
说话风格：{style}

规则：
1. 每次只说一两句话，像在手机上打字一样口语化。
2. 只根据“你的情况”回答客服的问题；情况里没有的细节就说记不清，不要编造。
3. 客服要办理的事会在 App 里弹出确认卡片，确认由系统按你想要的结果处理，你不用在聊天里说“确认”。
4. 想要的结果已经办成，或者客服讲清楚了办不了而你接受了，就在这句话最后加上 ###结束。
5. 客服问你问题时要回答，不要一直重复同一句话。"""


class LLMCustomer:
    """大模型扮演的顾客。model 和 agent 用的接口一样：complete(messages, tools)。"""

    def __init__(self, scenario, model, *, persona=None, max_turns=6):
        self.scenario, self.model, self.max_turns = scenario, model, max_turns
        self.accept = accepted(scenario)
        self.system = CUSTOMER_PROMPT.format(
            brief=scenario.get("customer_brief") or scenario["opening"],
            goal=scenario.get("goal") or "把问题解决",
            persona=persona or "普通网购用户",
            style=scenario.get("style") or "语气平和，偶尔有错别字",
        )
        self.history = []
        self.turns = 0
        self.finished = False
        self.opening = self._say("（客服接入了对话，请你先开口说明来意。）")

    def _say(self, prompt):
        messages = [{"role": "system", "content": self.system}, *self.history, {"role": "user", "content": prompt}]
        text = (self.model.complete(messages, []).get("content") or "").strip()
        self.history += [{"role": "user", "content": prompt}, {"role": "assistant", "content": text}]
        if "###结束" in text:
            self.finished = True
            text = text.replace("###结束", "").strip()
        return text

    def approve(self, name, arguments):
        return approves(self.accept, name, arguments)

    def respond(self, reply, toolbox):
        self.turns += 1
        if self.finished or self.turns >= self.max_turns:
            return None
        text = self._say(reply)
        return text or None
