"""在标注好的查询集上比较 BM25、向量和混合检索。

每条查询标注了一个或多个相关条文（命中任意一个即算召回）。eval/ 下的查询集都只有
十几条，是冒烟测试，用来确认索引和检索链路正常，不能当作正式评测结果。

用法:
    uv run python knowledge/eval_retrieval.py                     # 法规查询，只在法规里检索
    uv run python knowledge/eval_retrieval.py --all-levels        # 法规查询，平台规则也参与检索
    uv run python knowledge/eval_retrieval.py --queries knowledge/eval/smoke_platform_queries.jsonl --level 平台规则
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ecom_agent_system.knowledge_base import KnowledgeBase

DEFAULT_QUERIES = Path(__file__).resolve().parent / "eval" / "smoke_queries.jsonl"
OFFICIAL_LEVELS = ["法律", "行政法规", "司法解释", "部门规章", "规范性文件"]
CUTOFFS = (1, 3, 5, 10)


def first_relevant_rank(hits, relevant):
    for rank, hit in enumerate(hits, start=1):
        if hit.citation in relevant:
            return rank
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--level", action="append", help="只在这些层级里检索，可重复；默认只检索法规")
    scope.add_argument("--all-levels", action="store_true", help="检索全部条目，包括平台规则")
    args = parser.parse_args()
    levels = None if args.all_levels else (args.level or OFFICIAL_LEVELS)

    queries = [json.loads(line) for line in args.queries.read_text(encoding="utf-8").splitlines() if line.strip()]
    knowledge_base = KnowledgeBase.load()
    known = {f"《{chunk['title']}》{chunk['article_no']}" for chunk in knowledge_base.chunks}
    unknown = sorted({citation for query in queries for citation in query["relevant"]} - known)
    if unknown:
        raise SystemExit(f"标注里的条文不在索引中: {unknown}")

    results = {}
    for mode in ("bm25", "dense", "hybrid"):
        ranks = []
        for query in queries:
            hits = knowledge_base.search(query["query"], k=max(CUTOFFS), mode=mode, levels=levels)
            ranks.append(first_relevant_rank(hits, set(query["relevant"])))
        results[mode] = ranks

    searched = sum(1 for chunk in knowledge_base.chunks if levels is None or chunk["level"] in levels)
    print(
        f"{len(queries)} 条查询，在 {searched}/{len(knowledge_base.chunks)} 条中检索"
        f"（{'全部层级' if levels is None else '、'.join(levels)}），向量模型 {knowledge_base.manifest['model']}\n"
    )
    header = "mode    " + "".join(f"R@{cutoff:<5}" for cutoff in CUTOFFS) + "MRR@10"
    print(header)
    for mode, ranks in results.items():
        recalls = "".join(
            f"{sum(1 for rank in ranks if rank and rank <= cutoff) / len(ranks):<7.2f}" for cutoff in CUTOFFS
        )
        mrr = sum(1 / rank for rank in ranks if rank) / len(ranks)
        print(f"{mode:<8}{recalls}{mrr:.3f}")

    print("\n混合检索前 5 名未命中的查询：")
    for query, rank in zip(queries, results["hybrid"]):
        if not rank or rank > 5:
            top = knowledge_base.search(query["query"], k=3, mode="hybrid", levels=levels)
            print(f"  {query['id']} {query['query']}")
            print(f"     期望 {query['relevant']}，相关条文名次 {rank or '>10'}")
            print(f"     实际前 3: {[hit.citation for hit in top]}")


if __name__ == "__main__":
    main()
