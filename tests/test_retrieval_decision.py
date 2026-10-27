"""检索决策评测集的格式：标签合法，标注的条文都在语料里，来源案例都存在。"""

import json
from pathlib import Path

from ecom_agent_system.agent.tools import SCOPES, TOOL_SCHEMAS
from ecom_agent_system.knowledge_base import Hit, load_corpus

EVAL_DIR = Path(__file__).resolve().parents[1] / "knowledge" / "eval"
QUESTIONS = [json.loads(line) for line in (EVAL_DIR / "retrieval_decision.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
TYPES = {"法规", "法规（短问题）", "平台规则", "订单工具", "闲聊", "超出范围"}
TOOLS = {tool["function"]["name"] for tool in TOOL_SCHEMAS}


def test_labels_are_consistent():
    assert len({q["id"] for q in QUESTIONS}) == len(QUESTIONS)
    for q in QUESTIONS:
        assert q["type"] in TYPES, q["id"]
        assert q["retrieval"] in {"required", "optional", "unnecessary"}, q["id"]
        assert q["question"].strip() and q["answer"]["conclusion"].strip(), q["id"]
        assert set(q["expected_tools"]) <= TOOLS - {"search_policy"}, q["id"]  # 检索由 retrieval 字段标注
        assert set(q["court_citations"]) <= set(q["expected_citations"]), q["id"]
        if q["retrieval"] == "required":
            assert q["scope"] in SCOPES, q["id"]
        else:
            assert q["scope"] is None and not q["expected_citations"], q["id"]


def test_citations_and_cases_exist():
    corpus = {Hit(record, 0.0).citation for record in load_corpus()}
    missing = [(q["id"], c) for q in QUESTIONS for c in q["expected_citations"] if c not in corpus]
    assert missing == []
    cases = {json.loads(line)["case_id"] for line in (EVAL_DIR / "court_cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()}
    assert {q["source_case"] for q in QUESTIONS if q["source_case"]} <= cases
