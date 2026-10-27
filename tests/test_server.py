import pytest
from fastapi.testclient import TestClient

from ecom_agent_system.server import create_app

PLATFORM_RULE = {
    "chunk_id": "p#1",
    "title": "测试商城平台规则",
    "level": "平台规则",
    "article_no": "P-SHIP-01",
    "chapter": "物流",
    "text": "包裹在运输途中丢失的，由商家先行赔付。",
    "status": "有效",
    "legal_basis": ["《乙办法》第一条"],
}


@pytest.fixture
def client(make_knowledge_base):
    knowledge_base = make_knowledge_base([PLATFORM_RULE], synonyms={"包裹": ["快递"]})
    return TestClient(create_app(knowledge_base))


def _citations(hits):
    return [hit["citation"] for hit in hits]


def test_info_lists_levels_in_legal_order(client):
    info = client.get("/api/info").json()
    assert [item["level"] for item in info["levels"]] == ["法律", "部门规章", "平台规则"]
    assert info["chunks"] == 4 and info["documents"] == 3 and info["synonyms"] == 1


def test_search_returns_both_rankings_and_fusion(client):
    body = client.get("/api/search", params={"q": "包裹丢了要赔偿吗", "k": 3}).json()
    assert len(body["fused"]) == 3
    assert {"bm25", "dense"} <= set(body["fused"][0]["ranks"])
    assert body["bm25_query"]["synonyms"] == {"包裹": ["快递"]}
    assert "快递" in body["bm25_query"]["expansion_tokens"]
    # 每一路里的条目都标出了它在融合结果里的名次
    fused = _citations(client.get("/api/search", params={"q": "包裹丢了要赔偿吗", "k": 50}).json()["fused"])
    for hit in body["bm25"] + body["dense"]:
        assert fused[hit["fused_rank"] - 1] == hit["citation"]


def test_dense_weight_moves_fusion_to_one_retriever(client):
    query = {"q": "包裹丢了要赔偿吗"}
    bm25_only = client.get("/api/search", params={**query, "dense_weight": 0}).json()
    dense_only = client.get("/api/search", params={**query, "dense_weight": 1}).json()
    assert _citations(bm25_only["fused"]) == _citations(bm25_only["bm25"])
    assert _citations(dense_only["fused"]) == _citations(dense_only["dense"])


def test_expand_flag_turns_synonyms_off(client):
    body = client.get("/api/search", params={"q": "包裹丢了", "expand": "false"}).json()
    assert body["expand"] is False
    assert body["bm25_query"]["expansion_tokens"] == []
    # 关闭后仍然告诉前端问题里有哪些说法在同义词表里
    assert body["bm25_query"]["synonyms"] == {"包裹": ["快递"]}


def test_level_filter_and_parameter_validation(client):
    body = client.get("/api/search", params={"q": "赔偿", "level": ["平台规则"]}).json()
    assert {hit["level"] for hit in body["fused"]} == {"平台规则"}
    assert client.get("/api/search", params={"q": "赔偿", "level": ["地方法规"]}).status_code == 422
    assert client.get("/api/search", params={"q": "赔偿", "dense_weight": 1.5}).status_code == 422
    assert client.get("/api/search", params={"q": "   "}).status_code == 422


def test_article_lookup_follows_legal_basis(client):
    article = client.get("/api/article", params={"citation": "《乙办法》第一条"}).json()
    assert article["text"].startswith("快递在运输中丢失")
    assert client.get("/api/article", params={"citation": "《乙办法》第九条"}).status_code == 404


def test_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "售后知识库检索台" in response.text
