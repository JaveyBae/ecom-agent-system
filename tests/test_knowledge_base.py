import pytest

from ecom_agent_system.knowledge_base import FUSION_WEIGHTS, KnowledgeBase, expansion_terms, rrf_fuse


def test_each_mode_finds_the_matching_article(knowledge_base):
    for mode in ("bm25", "dense", "hybrid"):
        hits = knowledge_base.search("网购的东西能退货吗", k=1, mode=mode)
        assert hits[0].citation == "《甲法》第一条", mode


def test_hybrid_records_ranks_from_both_retrievers(knowledge_base):
    hit = knowledge_base.search("快递丢了怎么赔偿", k=1, mode="hybrid")[0]
    assert hit.citation == "《乙办法》第一条"
    assert set(hit.ranks) == {"bm25", "dense"}
    expected = sum(FUSION_WEIGHTS[name] / (60 + rank) for name, rank in hit.ranks.items())
    assert hit.score == pytest.approx(expected)


def test_rrf_fuse_prefers_the_heavier_retriever():
    rankings = {"bm25": [0, 1], "dense": [1, 0]}
    assert [index for index, _ in rrf_fuse(rankings, {"bm25": 0.7, "dense": 0.3})] == [0, 1]
    assert [index for index, _ in rrf_fuse(rankings, {"bm25": 0.3, "dense": 0.7})] == [1, 0]


def test_expansion_terms_skip_words_already_in_query():
    synonyms = {"七天": ["七日"], "退钱": ["退款"], "快递": ["快件"]}
    # “退款”已经在查询里，“快递”没有出现
    assert expansion_terms("七天内能退钱吗，退款要多久", synonyms) == ["七日"]


def test_synonyms_bridge_colloquial_words_for_bm25(make_knowledge_base):
    query = "包裹丢了怎么办"
    plain = make_knowledge_base()
    expanded = make_knowledge_base(synonyms={"包裹": ["快递"], "丢了": ["丢失"]})
    # 没有同义词时查询和三条条文没有一个词重合
    assert plain.search(query, k=1, mode="bm25")[0].citation != "《乙办法》第一条"
    assert expanded.search(query, k=1, mode="bm25")[0].citation == "《乙办法》第一条"
    tokens, extra = expanded.bm25_query(query)
    assert "快递" in extra and "快递" not in tokens
    # 关掉扩展就和没有同义词表一样
    assert expanded.rankings(query, mode="bm25", expand=False) == plain.rankings(query, mode="bm25")


def test_level_filter_is_applied_before_ranking(knowledge_base):
    hits = knowledge_base.search("赔偿", k=5, mode="hybrid", levels=["部门规章"])
    assert [hit.citation for hit in hits] == ["《乙办法》第一条"]


def test_misaligned_index_is_rejected(knowledge_base):
    with pytest.raises(ValueError):
        KnowledgeBase(knowledge_base.chunks, knowledge_base.embeddings[:2], knowledge_base.manifest)


def test_unknown_mode_is_rejected(knowledge_base):
    with pytest.raises(ValueError):
        knowledge_base.search("退货", mode="sparse")
