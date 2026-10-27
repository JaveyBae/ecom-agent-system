"""售后 agent 的子 agent：法规顾问和凭证审核员。主 agent 是编排者，子 agent 以工具或关卡的形式接进 Toolbox。

每个子 agent 有自己的系统提示、只能用一小部分工具（受限的 Toolbox）、有步数上限，用的是和主 agent 一样的
complete(messages, tools) 接口：可以是同一个模型，也可以换一个。它们不看顾客的对话，只拿主 agent 或 harness
交给它们的材料；交回去的只有结构化的结论。它们自己的工具调用记进对话轨迹，带 agent 标记；模型调用和 token
并进对话的用量，另外按 agent 分开记一份。

- 法规顾问（legal）：主 agent 的工具 consult_legal(question)。只带 search_policy，检索法规原文、读条文，
  回答结论和依据（法规名称、条号）。接上它以后主 agent 没有 search_policy，法条原文不进主对话的上下文；
- 凭证审核员（evidence）：接在凭证关卡上。按质量问题、运输损毁、生鲜变质提交的申请，过了关键词检查之后
  再由审核员看一遍：填进 evidence 的问题描述、顾客在这次对话里的原话、商品是什么，判断问题描述是不是顾客
  说的、和商品对不对得上。驳回的申请按“凭证不足”拦下，原因还给主 agent。审核员没有任何办理工具。

stub_agents 给出不用模型的桩：审核员全部通过或全部驳回，用来给审核员的作用画出上下界，和评分的基线一个道理。
"""

from __future__ import annotations

import json
import re
import time

from ecom_agent_system.agent.conversation import record_usage
from ecom_agent_system.agent.grading import article_forms
from ecom_agent_system.agent.tools import TOOL_SCHEMAS

AGENT_NAMES = ("legal", "evidence")
ROLES = {"legal": "法规顾问", "evidence": "凭证审核员"}
LAW = re.compile(r"《[^》]+》")
VERDICT = re.compile(r"结论[:：]?\s*(通过|驳回)")
REASON = re.compile(r"原因[:：]\s*(.+)")

LEGAL_SYSTEM = """你是 ShopSim 商城售后客服的法规顾问。客服把顾客追问的法律问题交给你。
做法：用 search_policy 检索法规原文（scope 填“法律法规”；要查平台自己的规定再检索“平台规则”），读检索结果，然后回答客服：
1. 结论：一两句话，说清法律上怎么规定；
2. 依据：写出法规名称和条号，照检索结果里的 citation 原样写，例如《中华人民共和国消费者权益保护法》第二十五条。
只依据检索到的原文；检索不到就说没有找到，不要凭记忆写条号。你的回答是给客服看的，不要和顾客说话。"""

EVIDENCE_SYSTEM = """你是 ShopSim 商城售后的凭证审核员。客服按质量问题、运输损毁或生鲜变质给顾客提交售后，提交前把申请交给你审一遍。
你会拿到：申请的类型和原因、商品名称、客服填写的问题描述（evidence）、顾客在这次对话里说过的原话。你只判断两件事：
1. 问题描述是不是顾客自己说的：描述里的毛病顾客一个都没提过，就是客服替顾客编的，驳回；换个说法、概括一下没关系；
2. 问题描述和商品对不对得上：衣服说“黑屏”、茶叶说“漏电”这种对不上的，驳回。
只看给你的材料，不猜顾客的动机，不管金额和期限（那些由规则判）。可以用 search_policy 查平台规则里对凭证的要求（P-QA-05）。
最后一行只写一句：结论：通过 或 结论：驳回，原因：（一句话）。"""


def parse_verdict(text):
    """从审核员的回答里取结论：(通过|驳回|未判定, 原因)。"""
    text = (text or "").strip()
    match = VERDICT.search(text)
    if match:
        verdict = match.group(1)
    elif "驳回" in text or "不通过" in text:
        verdict = "驳回"
    elif "通过" in text:
        verdict = "通过"
    else:
        return "未判定", "审核员没有给出结论"
    reason = REASON.search(text)
    reason = reason.group(1).strip() if reason else (text.splitlines()[-1].strip() if text else "")
    return verdict, reason[:120]


def cited(citation, text):
    """回答里有没有引用这条检索结果：法规名称在，条号也在（第二十五条或第25条都算）。"""
    law = LAW.search(citation or "")
    forms = article_forms(citation or "")
    return bool(law) and law.group(0) in text and (not forms or any(form in text for form in forms))


class RestrictedToolbox:
    """子 agent 看到的 Toolbox：只有 allowed 里的工具，其余一律拒绝，并记进主 Toolbox 的 blocked（越权）。"""

    def __init__(self, toolbox, role, allowed):
        self.toolbox, self.role, self.allowed = toolbox, role, tuple(allowed)

    @property
    def tool_schemas(self):
        offered = {tool["function"]["name"] for tool in self.toolbox.tool_schemas} | {"search_policy"}
        return [tool for tool in TOOL_SCHEMAS if tool["function"]["name"] in self.allowed and tool["function"]["name"] in offered]

    def call(self, name, arguments):
        if name not in self.allowed:
            return self.toolbox._refuse(name, arguments, "越权", f"{self.role}只能用 {list(self.allowed)}，不能用 {name}", None)
        return self.toolbox.call(name, arguments)


class SubAgent:
    """一个子 agent：自己的系统提示、能用的工具、步数上限。run 跑一次任务，返回 (最后的回答, 步骤, 用量)。"""

    name = ""
    tools = ()
    max_steps = 4
    system = ""

    def __init__(self, model):
        self.model = model

    @property
    def role(self):
        return ROLES[self.name]

    def run(self, toolbox, task):
        view = RestrictedToolbox(toolbox, self.role, self.tools)
        tools = view.tool_schemas
        messages = [{"role": "system", "content": self.system}, {"role": "user", "content": task}]
        steps, usage = [], {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated": False, "model_seconds": 0.0}
        for _ in range(self.max_steps):
            started = time.perf_counter()
            assistant = self.model.complete(messages, tools)
            record_usage(self.model, messages, tools, assistant, usage, time.perf_counter() - started)
            messages.append(assistant)
            calls = assistant.get("tool_calls") or []
            if not calls:
                usage["model_seconds"] = round(usage["model_seconds"], 3)
                return assistant.get("content") or "", steps, usage
            for call in calls:
                name = call["function"]["name"]
                started = time.perf_counter()
                try:
                    arguments = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments, result = None, {"error": "参数不是合法的 JSON"}
                else:
                    result = view.call(name, arguments)
                steps.append({"tool": name, "arguments": arguments, "result": result, "seconds": round(time.perf_counter() - started, 4)})
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "name": name, "content": json.dumps(result, ensure_ascii=False)})
        usage["model_seconds"] = round(usage["model_seconds"], 3)
        return "", steps, usage  # 步数用完还没给结论


class LegalAdvisor(SubAgent):
    name, tools, max_steps, system = "legal", ("search_policy",), 4, LEGAL_SYSTEM

    def consult(self, toolbox, question):
        """回答一个法律问题。citations 是回答里真正引用了的检索结果，主 agent 照着写就不会凭记忆引用。"""
        answer, steps, usage = self.run(toolbox, f"顾客的问题：{question}")
        retrieved = [r["citation"] for s in steps if s["tool"] == "search_policy" for r in (s["result"] or {}).get("results", [])]
        result = {
            "answer": answer or "法规顾问没有给出结论",
            "citations": [c for c in dict.fromkeys(retrieved) if cited(c, answer)],
            "searched": sum(s["tool"] == "search_policy" for s in steps),
        }
        return result, steps, usage


class EvidenceReviewer(SubAgent):
    name, tools, max_steps, system = "evidence", ("search_policy",), 3, EVIDENCE_SYSTEM

    def review(self, toolbox, request, evidence, heard, title):
        """审一份申请的问题描述。返回 {"verdict": 通过|驳回|未判定, "reason", "answer"}。"""
        task = (f"申请：{request['type']}，原因：{request['reason']}，订单 {request['order_id']}\n商品：{title}\n"
                f"客服填写的问题描述：{evidence or '（空）'}\n顾客这次说过的话：{heard or '（没有）'}")
        answer, steps, usage = self.run(toolbox, task)
        verdict, reason = parse_verdict(answer)
        return {"verdict": verdict, "reason": reason, "answer": answer}, steps, usage


class StubAdvisor:
    """不用模型的法规顾问：什么都查不到。只为了让 --agents-stub 能和 legal 一起写。"""

    name = "legal"

    def consult(self, toolbox, question):
        return {"answer": "没有找到相关法规原文", "citations": [], "searched": 0}, [], _no_usage()


class StubReviewer:
    """不用模型的凭证审核员：全部通过或全部驳回，给审核员的作用画上下界。"""

    name = "evidence"

    def __init__(self, verdict):
        self.verdict = verdict

    def review(self, toolbox, request, evidence, heard, title):
        return {"verdict": self.verdict, "reason": f"桩：全部{self.verdict}", "answer": f"结论：{self.verdict}"}, [], _no_usage()


def _no_usage():
    return {"model_calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated": False, "model_seconds": 0.0}


def build_agents(names, model):
    """按名字建子 agent，都用同一个模型。"""
    unknown = sorted(set(names) - set(AGENT_NAMES))
    if unknown:
        raise ValueError(f"没有这些子 agent：{unknown}；可选的有 {list(AGENT_NAMES)}")
    kinds = {"legal": LegalAdvisor, "evidence": EvidenceReviewer}
    return {name: kinds[name](model) for name in AGENT_NAMES if name in names}


def stub_agents(names, verdict):
    """不用模型的桩：verdict 是 approve 或 reject。"""
    word = {"approve": "通过", "reject": "驳回"}[verdict]
    stubs = {"legal": StubAdvisor(), "evidence": StubReviewer(word)}
    return {name: stubs[name] for name in AGENT_NAMES if name in names}
