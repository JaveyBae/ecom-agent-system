"""在冒烟查询集上扫描 RRF 的融合权重，同时看同义词扩展的作用。

每条查询只做一次 BM25 和向量检索，再用不同的向量权重 w（BM25 权重为 1 - w）融合：
w = 0 等于只用 BM25，w = 1 等于只用向量。有、无同义词扩展各算一遍。

只在 eval/ 下的冒烟查询集上扫描；最高法案例集（eval_court_retrieval.py）留作选定之后
报告结果，不参与选择。

用法: uv run python knowledge/tune_fusion.py
"""

from __future__ import annotations

import json
from pathlib import Path

from ecom_agent_system.knowledge_base import Hit, KnowledgeBase, expansion_terms, rrf_fuse

EVAL_DIR = Path(__file__).resolve().parent / "eval"
OFFICIAL_LEVELS = ["法律", "行政法规", "司法解释", "部门规章", "规范性文件"]
# 和 README 冒烟测试表里的四种组合一致：(名称, 查询集, 检索范围)
SETTINGS = [
    ("法规·只查法规", "smoke_queries.jsonl", OFFICIAL_LEVELS),
    ("法规·全部层级", "smoke_queries.jsonl", None),
    ("平台·只查平台", "smoke_platform_queries.jsonl", ["平台规则"]),
    ("平台·全部层级", "smoke_platform_queries.jsonl", None),
]
DENSE_WEIGHTS = [step / 10 for step in range(11)]
CUTOFF = 5
MRR_DEPTH = 10


def load_queries(name):
    lines = (EVAL_DIR / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def metrics(knowledge_base, cases, dense_weight):
    """cases: [(各检索器的名次, 相关条文)]。返回 (R@5, MRR@10)。"""
    weights = {"bm25": 1 - dense_weight, "dense": dense_weight}
    first_ranks = []
    for rankings, relevant in cases:
        fused = rrf_fuse(rankings, weights)[:MRR_DEPTH]
        citations = [Hit(knowledge_base.chunks[index], entry["score"]).citation for index, entry in fused]
        first_ranks.append(next((rank for rank, c in enumerate(citations, start=1) if c in relevant), None))
    recall = sum(1 for rank in first_ranks if rank and rank <= CUTOFF) / len(first_ranks)
    mrr = sum(1 / rank for rank in first_ranks if rank) / len(first_ranks)
    return recall, mrr


def main():
    knowledge_base = KnowledgeBase.load()

    # 向量检索不做扩展，两种情况共用同一份向量名次，只有 BM25 的名次不同
    cases = {"无同义词": {}, "有同义词": {}}
    for name, query_file, levels in SETTINGS:
        cases["无同义词"][name], cases["有同义词"][name] = [], []
        for query in load_queries(query_file):
            relevant = set(query["relevant"])
            rankings = knowledge_base.rankings(query["query"], levels=levels)
            cases["有同义词"][name].append((rankings, relevant))
            plain_bm25 = knowledge_base.rankings(query["query"], mode="bm25", levels=levels, expand=False)["bm25"]
            cases["无同义词"][name].append(({"bm25": plain_bm25, "dense": rankings["dense"]}, relevant))

    for query_file in dict.fromkeys(query_file for _, query_file, _ in SETTINGS):
        queries = load_queries(query_file)
        expanded = [query for query in queries if expansion_terms(query["query"], knowledge_base.synonyms)]
        print(f"{query_file}: 同义词扩展在 {len(expanded)}/{len(queries)} 条查询里起作用")

    names = [name for name, _, _ in SETTINGS]
    print("\nw = 向量权重（BM25 权重 1 - w）；每格是 R@5 / MRR@10\n")
    print(f"{'':<10}{'w':<5}" + "".join(f"{name:<14}" for name in names) + "平均")
    means = {}
    for variant, by_setting in cases.items():
        for dense_weight in DENSE_WEIGHTS:
            scores = [metrics(knowledge_base, by_setting[name], dense_weight) for name in names]
            means[variant, dense_weight] = tuple(sum(values) / len(values) for values in zip(*scores))
            cells = "".join(f"{recall:.2f} / {mrr:.3f}   " for recall, mrr in scores)
            recall, mrr = means[variant, dense_weight]
            print(f"{variant:<8}  {dense_weight:<5.1f}{cells}{recall:.2f} / {mrr:.3f}")
        print()
    for metric, label in ((0, "R@5"), (1, "MRR@10")):
        weight = max(DENSE_WEIGHTS, key=lambda w: (means["有同义词", w][metric], means["有同义词", w][1 - metric]))
        recall, mrr = means["有同义词", weight]
        print(f"有同义词时平均 {label} 最高：w = {weight:.1f}（R@5 {recall:.2f}，MRR@10 {mrr:.3f}）")


if __name__ == "__main__":
    main()
