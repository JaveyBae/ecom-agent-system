"""检索决策评测集的检索基线：直接拿顾客的原话去检索，不经过 agent 改写，看标注的条文排第几。

    uv run python knowledge/eval_retrieval_decision.py

只评“必须检索”且标了条文的题。agent 该不该检索、写的检索词好不好、答案对不对，要接上模型才能评。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from ecom_agent_system.agent.tools import SCOPES
from ecom_agent_system.knowledge_base import KnowledgeBase

EVAL_PATH = Path(__file__).resolve().parent / "eval" / "retrieval_decision.jsonl"
MODES = ("bm25", "dense", "hybrid")
DEPTH = 20


def first_rank(hits, expected):
    """标注的条文里排得最靠前的一条的名次，前 DEPTH 名里没有则为 None。"""
    return next((position for position, hit in enumerate(hits, start=1) if hit.citation in expected), None)


def rate(ranks, k):
    return sum(rank is not None and rank <= k for rank in ranks) / len(ranks)


def main():
    questions = [json.loads(line) for line in EVAL_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    graded = [q for q in questions if q["retrieval"] == "required" and q["expected_citations"]]
    knowledge_base = KnowledgeBase.load()
    ranks = defaultdict(list)
    misses = []
    for question in graded:
        expected = set(question["expected_citations"])
        for mode in MODES:
            hits = knowledge_base.search(question["question"], k=DEPTH, mode=mode, levels=SCOPES[question["scope"]])
            rank = first_rank(hits, expected)
            ranks[(question["type"], mode)].append(rank)
            ranks[("全部", mode)].append(rank)
            if mode == "hybrid" and (rank is None or rank > 5):
                misses.append((question["id"], rank, hits[0].citation))
    print(f"{'题型':<10}{'方式':<8}{'题数':>4}{'命中@1':>8}{'命中@5':>8}{'命中@10':>9}{'命中@20':>9}")
    for (kind, mode), values in ranks.items():
        print(f"{kind:<10}{mode:<8}{len(values):>4}" + "".join(f"{rate(values, k):>9.2f}" for k in (1, 5, 10, 20)))
    print("\n混合检索前 5 名没有命中的题（题号、标注条文的最好名次、第 1 名）：")
    for question_id, rank, top in misses:
        print(f"  {question_id}: {rank or f'>{DEPTH}'}，第 1 名是 {top}")


if __name__ == "__main__":
    main()
