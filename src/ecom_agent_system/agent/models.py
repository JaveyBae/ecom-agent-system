"""模型后端：OpenAI 兼容接口的客户端、超时重试、本机 transformers 跑的 Qwen，以及估算 token。

harness 只要求模型提供 complete(messages, tools)，返回一条 assistant 消息。接口返回的用量放在 last_usage 里，
不混进消息，因为有的接口会拒绝消息里多出来的字段。
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.request


def _ssl_context():
    """HTTPS 用的证书：uv 装的 Python 在 macOS 上没有系统根证书，有 certifi 就用它的，没有就按默认。"""
    try:
        import certifi
    except ImportError:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def estimate_tokens(text):
    """粗略估算 token：中文大约 1.4 个字一个 token，其他字符大约 3.5 个一个。接口不返回用量时才用。"""
    cjk = sum(1 for char in text if "一" <= char <= "鿿")
    return round(cjk / 1.4 + (len(text) - cjk) / 3.5)


class OpenAIModel:
    """OpenAI 兼容的 /chat/completions：vLLM 部署的 Qwen、DeepSeek、百炼都可以。"""

    def __init__(self, base_url, model, api_key, *, timeout=60, temperature=0.0, max_tokens=1024, extra_body=None):
        self.endpoint = base_url.rstrip("/") + "/chat/completions"
        self.model, self.api_key = model, api_key
        self.timeout, self.temperature, self.max_tokens = timeout, temperature, max_tokens
        self.extra_body = extra_body or {}
        self.last_usage = None

    def complete(self, messages, tools):
        payload = {"model": self.model, "messages": messages, "temperature": self.temperature, "max_tokens": self.max_tokens}
        if tools:
            payload["tools"] = tools
        payload.update(self.extra_body)
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout, context=_ssl_context()) as response:
            data = json.loads(response.read().decode("utf-8"))
        usage = data.get("usage") or {}
        self.last_usage = {"input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")}
        message = data["choices"][0]["message"]
        # 只留下一轮要回传的字段；开了深度思考的接口要求原样回传 reasoning_content
        keep = ("role", "content", "tool_calls", "reasoning_content")
        return {key: message[key] for key in keep if message.get(key) is not None} | {"role": "assistant"}


class RetryingModel:
    """超时、网络错误、429 和 5xx 自动重试，等待时间按 backoff 翻倍、不超过 max_wait 秒。其他错误直接抛出。

    限流严的接口（智谱的免费模型并发 1 都会间歇 429）要把 retries 调到 8 左右、backoff 调到 2 秒，并且串行跑。
    """

    def __init__(self, model, *, retries=2, backoff=1.0, max_wait=60.0, sleep=time.sleep):
        self.model, self.retries, self.backoff, self.max_wait, self.sleep = model, retries, backoff, max_wait, sleep
        self.last_usage = None
        self.retried = 0  # 累计重试次数

    def complete(self, messages, tools):
        for attempt in range(self.retries + 1):
            try:
                message = self.model.complete(messages, tools)
                self.last_usage = getattr(self.model, "last_usage", None)
                return message
            except urllib.error.HTTPError as error:
                if error.code != 429 and error.code < 500:
                    raise
                failure = error
            except (TimeoutError, urllib.error.URLError, ConnectionError) as error:
                failure = error
            if attempt < self.retries:
                self.retried += 1
                self.sleep(min(self.backoff * 2**attempt, self.max_wait))
        raise failure


# ---------------------------------------------------------------- 本机 transformers

TOOL_CALL = re.compile(r"<tool_call>\s*<function=([^>\s]+)>(.*?)</function>\s*</tool_call>", re.DOTALL)
PARAMETER = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", re.DOTALL)
THINK = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def _typed(value, kind):
    """按工具参数的类型转换 Qwen 写在 <parameter> 里的文字。"""
    value = value.strip()
    if kind == "boolean":
        return value.lower() in ("true", "1", "yes", "是")
    if kind in ("integer", "number", "object", "array"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def parse_qwen_reply(text, tools, call_id="call"):
    """把 Qwen 生成的文字变成 OpenAI 格式的 assistant 消息：<think> 里的是推理，<tool_call> 里的是工具调用。

    开了深度思考却没写完（被 max_new_tokens 截断）的，把没闭合的 <think> 整段丢掉。
    """
    reasoning = "\n".join(part.strip() for part in THINK.findall(text)).strip()
    text = THINK.sub("", text)
    if "<think>" in text:
        text = text[: text.index("<think>")]
    types = {tool["function"]["name"]: tool["function"]["parameters"].get("properties", {}) for tool in tools or []}
    calls = []
    for index, match in enumerate(TOOL_CALL.finditer(text)):
        name, body = match.group(1), match.group(2)
        arguments = {key: _typed(value, types.get(name, {}).get(key, {}).get("type")) for key, value in PARAMETER.findall(body)}
        calls.append({"id": f"{call_id}_{index}", "type": "function",
                      "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}})
    message = {"role": "assistant", "content": TOOL_CALL.sub("", text).strip()}
    if calls:
        message["tool_calls"] = calls
    if reasoning:
        message["reasoning_content"] = reasoning
    return message


def _for_template(message):
    """Qwen 的模板要求 tool_calls 的 arguments 是对象，OpenAI 格式里是 JSON 字符串。"""
    calls = message.get("tool_calls")
    if not calls:
        return message
    converted = []
    for call in calls:
        function = call.get("function") or {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"raw": arguments}
        converted.append({**call, "function": {**function, "arguments": arguments}})
    return {**message, "tool_calls": converted}


class HFModel:
    """本机用 transformers 跑 Qwen3.5 系列模型，不用起服务。工具调用按 Qwen 的 <tool_call><function=…> 格式解析。

    默认用 MPS（Apple 芯片）或 CUDA，没有就用 CPU。temperature 为 0 时贪心解码，否则按 Qwen 推荐的采样参数
    （top_p 0.8、top_k 20）。随机种子在加载时设一次，同样的场景顺序结果可复现。
    """

    def __init__(self, name, *, device=None, temperature=0.0, max_new_tokens=512, thinking=False, seed=0):
        import torch
        from transformers import AutoModelForImageTextToText, AutoTokenizer

        self.torch, self.temperature, self.max_new_tokens, self.thinking = torch, temperature, max_new_tokens, thinking
        self.tokenizer = AutoTokenizer.from_pretrained(name)
        self.device = device or ("mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu")
        try:
            model = AutoModelForImageTextToText.from_pretrained(name, dtype=torch.bfloat16)  # Qwen3.5 是多模态结构，只用文字
        except ValueError:
            from transformers import AutoModelForCausalLM

            model = AutoModelForCausalLM.from_pretrained(name, dtype=torch.bfloat16)
        self.model = model.to(self.device).eval()
        im_end = self.tokenizer.convert_tokens_to_ids("<|im_end|>")
        self.eos = sorted({token for token in (self.tokenizer.eos_token_id, im_end) if token is not None})
        self.pad = self.tokenizer.pad_token_id if self.tokenizer.pad_token_id is not None else self.eos[0]
        torch.manual_seed(seed)
        self.last_usage = None
        self.calls = 0

    def complete(self, messages, tools):
        prompt = self.tokenizer.apply_chat_template(
            [_for_template(message) for message in messages], tools=tools or None, add_generation_prompt=True,
            tokenize=False, enable_thinking=self.thinking,
        )
        encoded = self.tokenizer(prompt, return_tensors="pt").to(self.device)
        length = int(encoded["input_ids"].shape[1])
        options = {"max_new_tokens": self.max_new_tokens, "eos_token_id": self.eos, "pad_token_id": self.pad}
        if self.temperature > 0:
            options.update(do_sample=True, temperature=self.temperature, top_p=0.8, top_k=20)
        else:
            options["do_sample"] = False
        with self.torch.no_grad():
            output = self.model.generate(**encoded, **options)
        generated = output[0, length:]
        self.last_usage = {"input_tokens": length, "output_tokens": int(generated.shape[0])}
        self.calls += 1
        return parse_qwen_reply(self.tokenizer.decode(generated, skip_special_tokens=True), tools, f"hf{self.calls}")
