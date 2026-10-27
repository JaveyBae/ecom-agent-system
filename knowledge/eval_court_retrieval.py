"""用最高法典型案例评测法规检索的召回率。

查询只用案例的“基本案情”（模拟用户描述自己的遭遇），不带裁判结果；相关条文取
法院在裁判结果和典型意义里引用、且能对应到知识库的条文。没有可对应引用的案例跳过。

指标：
  - 命中率@k：前 k 条里至少有一条法院引用的条文
  - 覆盖率@k：法院引用的条文里，有多少比例出现在前 k 条

除了 bm25 / dense / hybrid，还评测“分层”方式：先检索 3 条平台规则，取它们标注的
法律依据，再用混合检索的结果补足到 k 条。

用法: uv run python knowledge/eval_court_retrieval.py [--field holding]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ecom_agent_system.knowledge_base import KnowledgeBase

CASES_PATH = Path(__file__).resolve().parent / "eval" / "court_cases.jsonl"
OFFICIAL_LEVELS = ["法律", "行政法规", "司法解释", "部门规章", "规范性文件"]
CUTOFFS = (5, 10, 20)
LAYERED_PLATFORM_RULES = 3


def law_ranking(knowledge_base, query, mode, k):
    return [hit.citation for hit in knowledge_base.search(query, k=k, mode=mode, levels=OFFICIAL_LEVELS)]


def layered_ranking(knowledge_base, query, k):
    """平台规则的法律依据排在前面，然后用法规混合检索的结果补足。"""
    rules = knowledge_base.search(query, k=LAYERED_PLATFORM_RULES, mode="hybrid", levels=["平台规则"])
    linked = [citation for rule in rules for citation in (rule.chunk.get("legal_basis") or [])]
    ranking = list(dict.fromkeys(linked))
    for citation in law_ranking(knowledge_base, query, "hybrid", k):
        if len(ranking) >= k:
            break
        if citation not in ranking:
            ranking.append(citation)
    return ranking[:k]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--field", choices=["facts", "holding"], default="facts", help="用哪部分当查询")
    args = parser.parse_args()

    cases = []
    for line in CASES_PATH.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        relevant = list(dict.fromkeys(c["kb"] for c in case["citations"] if c["kb"]))
        if relevant:
            core = {c["kb"] for c in case["citations"] if c["kb"] and c["in_core"]}
            cases.append((case, relevant, core))
    knowledge_base = KnowledgeBase.load()
    k_max = max(CUTOFFS)

    rankings = {}
    for mode in ("bm25", "dense", "hybrid", "layered"):
        rankings[mode] = [
            layered_ranking(knowledge_base, case[args.field], k_max)
            if mode == "layered"
            else law_ranking(knowledge_base, case[args.field], mode, k_max)
            for case, _, _ in cases
        ]

    total_articles = sum(len(relevant) for _, relevant, _ in cases)
    core_articles = sum(len(core) for _, _, core in cases)
    print(
        f"{len(cases)} 个案例（查询字段: {args.field}），法院引用且在知识库中的条文 {total_articles} 条，"
        f"其中属于核心包 {core_articles} 条\n"
    )
    header = "mode     " + "".join(f"命中@{k:<4}" for k in CUTOFFS) + "".join(f"覆盖@{k:<4}" for k in CUTOFFS)
    print(header)
    for mode, ranked in rankings.items():
        hit_rates, coverages = [], []
        for k in CUTOFFS:
            hit_rates.append(sum(any(c in r[:k] for c in rel) for r, (_, rel, _) in zip(ranked, cases)) / len(cases))
            coverages.append(sum(sum(c in r[:k] for c in rel) for r, (_, rel, _) in zip(ranked, cases)) / total_articles)
        print(f"{mode:<9}" + "".join(f"{value:<8.2f}" for value in hit_rates + coverages))

    print("\n按条文是否在核心包分开统计（hybrid / layered 的覆盖率@10）：")
    for mode in ("hybrid", "layered"):
        found_core = sum(sum(c in r[:10] for c in core) for r, (_, _, core) in zip(rankings[mode], cases))
        found_tail = sum(sum(c in r[:10] for c in rel if c not in core) for r, (_, rel, core) in zip(rankings[mode], cases))
        print(
            f"  {mode:<8} 核心包 {found_core}/{core_articles}  长尾 {found_tail}/{total_articles - core_articles}"
        )

    print("\n混合检索前 10 名一条都没命中的案例：")
    for ranked, (case, relevant, _) in zip(rankings["hybrid"], cases):
        if not any(c in ranked[:10] for c in relevant):
            print(f"  {case['case_id']} {case['holding'][:30]}")
            print(f"     法院引用: {[c.replace('中华人民共和国', '') for c in relevant]}")
            print(f"     检索前 3: {[c.replace('中华人民共和国', '') for c in ranked[:3]]}")


if __name__ == "__main__":
    main()
