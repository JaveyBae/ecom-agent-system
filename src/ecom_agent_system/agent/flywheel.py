"""数据回流：把跑过的对话变回场景和训练数据，让评测集和训练集跟着真实对话长。

- harvest：从对话记录里取顾客真正说过的话（大模型扮演的顾客、带错别字的顾客，或者将来的线上对话），做成同一个
  订单、同一个时刻、同一份标准答案的新场景。标准答案由订单和时刻决定，和顾客怎么说无关，所以可以照抄；
  只有说法是新的。--failed-only 只取没通过的对话，--lines original 保留原来的台词，就是一份“回归集”：改了提示词
  或 harness 之后只重跑这些。
- export_sft：把通过了全部硬性要求的对话导出成带工具调用的 SFT 样本（messages + tools），给小模型做监督微调。
  没通过的不要，出错的不要，深度思考的内容去掉。

线上数据接进来时，只要把真实对话整理成 aftersales 输出的记录格式，这两步就能直接用。
"""

from __future__ import annotations

import copy

from ecom_agent_system.agent.tools import CONSULT_LEGAL, SHOPPING_SCHEMAS, TOOL_SCHEMAS


def customer_lines(record):
    return [m.get("content") or "" for m in record["messages"] if m["role"] == "user"]


def harvest(records, *, failed_only=False, lines="harvested", suffix="harvest"):
    """对话记录 → 场景。lines 是 harvested（用对话里顾客的原话）或 original（保留场景原来的台词）。"""
    scenarios, seen = [], set()
    for record in records:
        scenario = record["scenario"]
        passed = record.get("grade", {}).get("passed")
        if failed_only and passed:
            continue
        if record.get("ended") == "error":
            continue
        said = customer_lines(record)
        original = [scenario["opening"]] + [turn["say"] for turn in scenario.get("follow_ups", [])]
        if lines == "harvested":
            if not said or said == original:
                continue  # 说法和场景里一样，没有新东西
            new = {**scenario, "scenario_id": f"{scenario['scenario_id']}-{suffix}", "opening": said[0],
                   "follow_ups": [{"say": line, "when": "always"} for line in said[1:]]}
        else:
            new = dict(scenario)
        if new["scenario_id"] in seen:
            continue
        seen.add(new["scenario_id"])
        new["source"] = {"session": record.get("session"), "customer": record.get("customer"), "passed": passed,
                         "lines": lines}
        scenarios.append(new)
    return scenarios


def merge_scenarios(base, extra):
    """合并两份场景，编号相同的以 base 为准。"""
    known = {s["scenario_id"] for s in base}
    return list(base) + [s for s in extra if s["scenario_id"] not in known]


def tool_schemas_for(record):
    """这段对话当时给模型看的工具表，按记录里的设置重建。"""
    tools = list(TOOL_SCHEMAS)
    if not record.get("kb", True):
        tools = [t for t in tools if t["function"]["name"] != "search_policy"]
    elif "legal" in (record.get("agents") or []):
        tools = [CONSULT_LEGAL if t["function"]["name"] == "search_policy" else t for t in tools]
    if record.get("shopping"):
        tools += SHOPPING_SCHEMAS
    return tools


def export_sft(records, *, include_failed=False):
    """通过的对话 → SFT 样本：{"messages", "tools", "scenario_id", "kind"}。"""
    examples = []
    for record in records:
        if record.get("ended") == "error":
            continue
        if not include_failed and not record.get("grade", {}).get("passed"):
            continue
        messages = []
        for message in record["messages"]:
            message = copy.deepcopy(message)
            message.pop("reasoning_content", None)
            messages.append(message)
        examples.append({
            "scenario_id": record["scenario"]["scenario_id"], "kind": record["scenario"]["kind"],
            "messages": messages, "tools": tool_schemas_for(record),
        })
    return examples
