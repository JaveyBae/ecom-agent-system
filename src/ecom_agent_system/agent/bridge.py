"""文件桥：让 Claude Code（或人）在本地直接扮演模型。

agent 的循环每轮调用 model.complete(messages, tools)。FileBridge 把这一轮的请求写进目录，等外部写回决策：

    request.json      {"seq": 3, "messages": [...], "tools": [...]}；全部跑完时是 {"seq": 9, "done": true}
    response-3.json   {"tool": "search_products", "arguments": {"query": "..."}} 或 {"content": "回复顾客的话"}

`ecom-agent bridge show / reply` 负责读请求、写回复，只打印上一轮之后新增的消息。complete 的返回值和
OpenAI 兼容接口的 assistant 消息一样，所以 shopping-grpo 的购物评测循环和这边的售后循环都能直接用；
以后换成真实模型，循环不用改。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

REQUEST = "request.json"
CURSOR = "cursor.json"  # 记录回复到了第几轮、这一段对话已经显示了几条消息


def _write(path, payload):
    """先写临时文件再改名，读的一方不会读到写了一半的 JSON。"""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def assistant_message(reply, seq):
    message = {"role": "assistant", "content": reply.get("content") or ""}
    if reply.get("tool"):
        message["tool_calls"] = [
            {
                "id": f"call_{seq}",
                "type": "function",
                "function": {"name": reply["tool"], "arguments": json.dumps(reply.get("arguments") or {}, ensure_ascii=False)},
            }
        ]
    return message


class FileBridge:
    """模型后端：每轮把请求写进目录，等外部写回决策。"""

    def __init__(self, directory, *, timeout=3600.0, poll=0.1):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        for stale in (*self.directory.glob("response-*.json"), self.directory / REQUEST, self.directory / CURSOR):
            stale.unlink(missing_ok=True)
        self.timeout, self.poll = timeout, poll
        self.seq = 0

    def complete(self, messages, tools):
        self.seq += 1
        _write(self.directory / REQUEST, {"seq": self.seq, "messages": messages, "tools": tools})
        path = self.directory / f"response-{self.seq}.json"
        deadline = time.monotonic() + self.timeout
        while not path.exists():
            if time.monotonic() > deadline:
                raise TimeoutError(f"等了 {self.timeout:.0f} 秒，没有收到第 {self.seq} 轮的回复")
            time.sleep(self.poll)
        reply = json.loads(path.read_text(encoding="utf-8"))
        path.unlink()
        return assistant_message(reply, self.seq)

    def close(self):
        _write(self.directory / REQUEST, {"seq": self.seq + 1, "done": True})


# ---------------------------------------------------------------- 扮演模型的一方


def _cursor(directory):
    path = Path(directory) / CURSOR
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"seq": 0, "shown": 0, "system": None}


def wait_request(directory, after_seq, timeout=120.0, poll=0.1):
    """等到序号大于 after_seq 的请求出现。"""
    path = Path(directory) / REQUEST
    deadline = time.monotonic() + timeout
    while True:
        if path.exists():
            request = json.loads(path.read_text(encoding="utf-8"))
            if request["seq"] > after_seq:
                return request
        if time.monotonic() > deadline:
            raise TimeoutError(f"{timeout:.0f} 秒内没有新的请求，agent 进程可能已经退出")
        time.sleep(poll)


def _tool_signature(tool):
    function = tool.get("function", tool)
    parameters = function.get("parameters", {}).get("properties", {})
    arguments = []
    for name, spec in parameters.items():
        kind = "|".join(spec["enum"]) if "enum" in spec else spec.get("type", "")
        arguments.append(f"{name}: {kind}")
    return f"{function['name']}({', '.join(arguments)})"


def render(request, cursor):
    """把请求里上次之后新增的消息排成文本；返回 (文本, 新的 cursor)。"""
    messages = request["messages"]
    new_session = not any(message["role"] == "assistant" for message in messages)
    shown = 0 if new_session else cursor["shown"]
    lines = [f"── 第 {request['seq']} 轮{'（新任务）' if new_session else ''} ──"]
    system = cursor["system"]
    for message in messages[shown:]:
        role, content = message["role"], message.get("content") or ""
        if role == "assistant":
            continue  # 自己上一轮的决策
        if role == "system":
            digest = hashlib.sha1(content.encode("utf-8")).hexdigest()
            lines.append("【系统提示】同上" if digest == system else f"【系统提示】\n{content}")
            system = digest
        elif role == "user":
            lines.append(f"【用户】\n{content}")
        else:
            lines.append(f"【工具结果 {message.get('name') or ''}】\n{content}")
    if new_session:
        lines.append("【可用工具】" + "；".join(_tool_signature(tool) for tool in request["tools"]))
    return "\n".join(lines), {"seq": cursor["seq"], "shown": len(messages), "system": system}


def show(directory, timeout=120.0):
    """等下一轮请求，打印新增的消息。"""
    cursor = _cursor(directory)
    request = wait_request(directory, cursor["seq"], timeout)
    if request.get("done"):
        return "全部跑完了。"
    text, cursor = render(request, cursor)
    _write(Path(directory) / CURSOR, cursor)
    return text


def reply(directory, decision, timeout=120.0):
    """写回这一轮的决策，再等下一轮请求并打印。decision 是 {"tool", "arguments"} 或 {"content"}。"""
    if not (decision.get("tool") or decision.get("content")):
        raise ValueError("回复里要有 tool（调用工具）或 content（回复文本）")
    cursor = _cursor(directory)
    request = wait_request(directory, cursor["seq"], timeout=0)
    if request.get("done"):
        return "全部跑完了。"
    _write(Path(directory) / f"response-{request['seq']}.json", decision)
    cursor["seq"] = request["seq"]
    _write(Path(directory) / CURSOR, cursor)
    return show(directory, timeout)
