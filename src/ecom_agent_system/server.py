"""知识库检索台：FastAPI 接口加 static/ 下的静态页面。

    uv run ecom-kb serve        # 然后打开 http://127.0.0.1:8000

页面把 BM25、向量两路各自的名次和融合结果并排显示，可以调融合权重、开关同义词扩展，
平台规则可以展开它依据的法条。两路检索的名次按（查询、层级、是否扩展）缓存，拖动权重
滑块时只重新融合，不重新检索。
"""

from __future__ import annotations

import threading
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from ecom_agent_system.knowledge_base import EXPANSION_WEIGHT, FUSION_WEIGHTS, RRF_K, Hit, KnowledgeBase, rrf_fuse

STATIC_DIR = Path(__file__).resolve().parent / "static"
LEVEL_ORDER = ("法律", "行政法规", "司法解释", "部门规章", "规范性文件", "平台规则")
ARTICLE_FIELDS = (
    "chunk_id",
    "title",
    "level",
    "article_no",
    "chapter",
    "text",
    "status",
    "issuer",
    "promulgated",
    "effective",
    "legal_basis",
)


def _article(chunk):
    source = chunk.get("source_file") or ""
    # 国家法律法规数据库的 docx 链接带签名，过期后打不开，只给网页来源加链接
    source_url = source if source.startswith("http") and not source.endswith(".docx") else None
    return {
        **{key: chunk.get(key) for key in ARTICLE_FIELDS},
        "citation": Hit(chunk, 0.0).citation,
        "source_url": source_url,
    }


def _level_key(level):
    return (LEVEL_ORDER.index(level) if level in LEVEL_ORDER else len(LEVEL_ORDER), level)


def create_app(knowledge_base):
    app = FastAPI(title="售后知识库检索台")
    add_knowledge_routes(app, knowledge_base)
    # 放在最后：/api 下的路由优先匹配
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def add_knowledge_routes(app, knowledge_base):
    """检索台的接口：/api/info、/api/search、/api/article。控制台（console）也挂这一组。"""
    by_citation = {Hit(chunk, 0.0).citation: chunk for chunk in knowledge_base.chunks}
    level_counts = Counter(chunk["level"] for chunk in knowledge_base.chunks)
    lock = threading.Lock()  # 向量模型不保证线程安全，检索串行执行

    @lru_cache(maxsize=256)
    def cached_rankings(query, levels, expand):
        with lock:
            return knowledge_base.rankings(query, levels=list(levels) or None, expand=expand)

    @app.get("/api/info")
    def info():
        return {
            "model": knowledge_base.manifest.get("model"),
            "built_at": knowledge_base.manifest.get("built_at"),
            "chunks": len(knowledge_base.chunks),
            "documents": len({chunk["title"] for chunk in knowledge_base.chunks}),
            "levels": [{"level": level, "count": level_counts[level]} for level in sorted(level_counts, key=_level_key)],
            "synonyms": len(knowledge_base.synonyms),
            "fusion_weights": FUSION_WEIGHTS,
            "expansion_weight": EXPANSION_WEIGHT,
            "rrf_k": RRF_K,
        }

    @app.get("/api/search")
    def search(
        q: Annotated[str, Query(min_length=1, max_length=2000)],
        k: Annotated[int, Query(ge=1, le=50)] = 10,
        dense_weight: Annotated[float, Query(ge=0, le=1)] = FUSION_WEIGHTS["dense"],
        expand: bool = True,
        level: Annotated[list[str] | None, Query()] = None,
    ):
        query = q.strip()
        if not query:
            raise HTTPException(422, "查询不能为空")
        levels = tuple(sorted(set(level or []), key=_level_key))
        unknown = set(levels) - set(level_counts)
        if unknown:
            raise HTTPException(422, f"未知的层级: {sorted(unknown)}")

        rankings = cached_rankings(query, levels, expand)
        weights = {"bm25": 1 - dense_weight, "dense": dense_weight}
        fused = rrf_fuse(rankings, weights)
        entries = dict(fused)
        positions = {index: position for position, (index, _) in enumerate(fused, start=1)}

        def hit(index):
            entry = entries[index]
            return {
                **_article(knowledge_base.chunks[index]),
                "ranks": entry["ranks"],
                "fused_rank": positions[index],
                "score": entry["score"],
            }

        tokens, extra = knowledge_base.bm25_query(query, expand)
        return {
            "query": query,
            "levels": list(levels),
            "weights": weights,
            "expand": expand,
            "bm25_query": {
                "tokens": tokens,
                "expansion_tokens": extra,
                "synonyms": {phrase: terms for phrase, terms in knowledge_base.synonyms.items() if phrase in query},
            },
            "fused": [hit(index) for index, _ in fused[:k]],
            "bm25": [hit(index) for index in rankings["bm25"][:k]],
            "dense": [hit(index) for index in rankings["dense"][:k]],
        }

    @app.get("/api/article")
    def article(citation: str):
        chunk = by_citation.get(citation)
        if chunk is None:
            raise HTTPException(404, f"知识库里没有{citation}")
        return _article(chunk)


def serve(host="127.0.0.1", port=8000):
    import uvicorn

    knowledge_base = KnowledgeBase.load()
    knowledge_base.rankings("预热")  # 启动时就加载向量模型，第一次检索不用等
    print(f"检索台已就绪：http://{host}:{port}")
    uvicorn.run(create_app(knowledge_base), host=host, port=port)
