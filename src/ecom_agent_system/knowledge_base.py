"""法规知识库：按条切分的法律法规，BM25 + 向量混合检索，加权 RRF 融合。

语料来自 knowledge/processed/*.jsonl：法规条文由 knowledge/crawl_*.py 抓取，平台规则
由 knowledge/build_platform_rules.py 从 platform_rules.yaml 生成，每条记录是一条条文或
一条规则。索引是一个归一化后的向量矩阵加上同序的元数据，几千条规模不需要向量数据库。
BM25 的查询会按 knowledge/synonyms.yaml 把口语说法扩展成法规用语。

    uv run python -m ecom_agent_system.knowledge_base build
    uv run python -m ecom_agent_system.knowledge_base search "拆封的耳机能七天无理由退货吗"
    uv run python -m ecom_agent_system.knowledge_base serve    # 检索台网页
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = PROJECT_ROOT / "knowledge" / "processed"
INDEX_DIR = PROJECT_ROOT / "knowledge" / "index"
SYNONYMS_PATH = PROJECT_ROOT / "knowledge" / "synonyms.yaml"

DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"
# Qwen3-Embedding 只给查询加指令，文档侧不加
QUERY_INSTRUCTION = "Instruct: 给定电商购物或售后场景中的问题，检索能够回答该问题的法律法规条款\nQuery: "
RRF_K = 60  # RRF 的平滑常数，沿用原论文的常用取值
# 各检索器在 RRF 里的权重。knowledge/tune_fusion.py 在冒烟查询集上扫描了不同权重，见 knowledge/README.md
FUSION_WEIGHTS = {"bm25": 0.5, "dense": 0.5}
# 同义词扩展出的词在 BM25 里降权：和原词同权时会把查询带偏
EXPANSION_WEIGHT = 0.3

METADATA_FIELDS = (
    "chunk_id",
    "title",
    "level",
    "issuer",
    "article_no",
    "chapter",
    "text",
    "promulgated",
    "effective",
    "status",
    "source_file",
    "legal_basis",  # 只有平台规则有：该规则依据的法规条文
)


@dataclass
class Hit:
    chunk: dict
    score: float
    ranks: dict = field(default_factory=dict)  # 各检索器中的名次（从 1 开始），未召回则缺省

    @property
    def citation(self):
        return f"《{self.chunk['title']}》{self.chunk['article_no']}"


def chunk_text(record):
    """带上法规名和章节再做向量化：同样一句“七日内退货”，出处不同含义也不同。"""
    header = " ".join(part for part in (f"《{record['title']}》", record.get("chapter"), record["article_no"]) if part)
    return f"{header}\n{record['text']}"


_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]")


def tokenize(text):
    import jieba

    return [token for token in jieba.lcut_for_search(text) if _TOKEN_RE.search(token)]


def load_synonyms(path=SYNONYMS_PATH):
    import yaml

    table = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    # YAML 会把 12315 这样的键读成整数
    return {str(phrase): list(terms) for phrase, terms in table.items()}


def expansion_terms(query, synonyms):
    """查询里出现的口语说法对应的法规用语，查询里已有的词不再追加。"""
    terms = [term for phrase, targets in synonyms.items() if phrase in query for term in targets]
    return [term for term in dict.fromkeys(terms) if term not in query]


def rrf_fuse(rankings, weights=FUSION_WEIGHTS):
    """加权 RRF：检索器给第 r 名贡献 weight / (RRF_K + r)。返回按总分排序的 [(下标, {score, ranks})]。"""
    fused = {}
    for name, ranking in rankings.items():
        for rank, index in enumerate(ranking, start=1):
            entry = fused.setdefault(index, {"score": 0.0, "ranks": {}})
            entry["score"] += weights[name] / (RRF_K + rank)
            entry["ranks"][name] = rank
    return sorted(fused.items(), key=lambda item: -item[1]["score"])


def load_corpus(corpus_dir=CORPUS_DIR):
    records = []
    for path in sorted(Path(corpus_dir).glob("*.jsonl")):
        with path.open(encoding="utf-8") as stream:
            records.extend(json.loads(line) for line in stream if line.strip())
    ids = [record["chunk_id"] for record in records]
    duplicates = {chunk_id for chunk_id in ids if ids.count(chunk_id) > 1}
    if duplicates:
        raise ValueError(f"duplicate chunk ids in corpus: {sorted(duplicates)[:5]}")
    return records


def _device():
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def load_encoder(model_name=DEFAULT_MODEL):
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(model_name, device=_device())
    model.max_seq_length = 1024  # 最长的条文约 900 字，留出余量
    return model


def build_index(corpus_dir=CORPUS_DIR, index_dir=INDEX_DIR, model_name=DEFAULT_MODEL, batch_size=16):
    records = load_corpus(corpus_dir)
    if not records:
        raise ValueError(f"no corpus records under {corpus_dir}; run knowledge/crawl_*.py first")
    texts = [chunk_text(record) for record in records]
    encoder = load_encoder(model_name)
    embeddings = encoder.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=True,
        convert_to_numpy=True,
    ).astype(np.float32)

    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    np.save(index_dir / "embeddings.npy", embeddings)
    with (index_dir / "chunks.jsonl").open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps({key: record.get(key) for key in METADATA_FIELDS}, ensure_ascii=False) + "\n")
    manifest = {
        "model": model_name,
        "dimension": int(embeddings.shape[1]),
        "chunks": len(records),
        "documents": len({record["title"] for record in records}),
        "corpus_sha256": hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest(),
        "query_instruction": QUERY_INSTRUCTION,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (index_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


class KnowledgeBase:
    def __init__(self, chunks, embeddings, manifest, encoder=None, synonyms=None):
        from rank_bm25 import BM25Okapi

        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings are misaligned")
        self.chunks = chunks
        self.embeddings = embeddings
        self.manifest = manifest
        self.synonyms = synonyms or {}
        self._encoder = encoder
        self._bm25 = BM25Okapi([tokenize(chunk_text(chunk)) for chunk in chunks])

    @classmethod
    def load(cls, index_dir=INDEX_DIR, encoder=None, synonyms_path=SYNONYMS_PATH):
        index_dir = Path(index_dir)
        manifest = json.loads((index_dir / "manifest.json").read_text())
        with (index_dir / "chunks.jsonl").open(encoding="utf-8") as stream:
            chunks = [json.loads(line) for line in stream if line.strip()]
        embeddings = np.load(index_dir / "embeddings.npy")
        return cls(chunks, embeddings, manifest, encoder, load_synonyms(synonyms_path))

    @property
    def encoder(self):
        if self._encoder is None:
            self._encoder = load_encoder(self.manifest["model"])
        return self._encoder

    def _mask(self, levels=None, titles=None):
        mask = np.ones(len(self.chunks), dtype=bool)
        if levels:
            mask &= np.array([chunk["level"] in levels for chunk in self.chunks])
        if titles:
            mask &= np.array([chunk["title"] in titles for chunk in self.chunks])
        return mask

    def _ranked(self, scores, mask, limit):
        scores = np.where(mask, scores, -np.inf)
        order = np.argsort(-scores, kind="stable")[:limit]
        return [int(index) for index in order if np.isfinite(scores[index])]

    def bm25_query(self, query, expand=True):
        """BM25 的查询词：(查询本身的分词, 同义词扩展出的词)，扩展词不含查询里已有的词。"""
        tokens = tokenize(query)
        if not expand:
            return tokens, []
        extra = tokenize(" ".join(expansion_terms(query, self.synonyms)))
        return tokens, [token for token in dict.fromkeys(extra) if token not in tokens]

    def bm25_ranking(self, query, mask, limit, expand=True):
        tokens, extra = self.bm25_query(query, expand)
        scores = np.asarray(self._bm25.get_scores(tokens))
        if extra:
            # BM25 的得分是各查询词得分之和，所以扩展词可以单独打分再降权相加
            scores = scores + EXPANSION_WEIGHT * np.asarray(self._bm25.get_scores(extra))
        return self._ranked(scores, mask, limit)

    def dense_ranking(self, query, mask, limit):
        vector = self.encoder.encode(
            [query], prompt=self.manifest["query_instruction"], normalize_embeddings=True, convert_to_numpy=True
        )[0]
        return self._ranked(self.embeddings @ vector, mask, limit)

    def rankings(self, query, mode="hybrid", candidates=50, levels=None, titles=None, expand=True):
        """各检索器各自的前 candidates 名：{检索器: [chunk 下标, ...]}。expand 控制 BM25 是否做同义词扩展。"""
        if mode not in {"bm25", "dense", "hybrid"}:
            raise ValueError(f"unknown search mode: {mode}")
        mask = self._mask(levels, titles)
        rankings = {}
        if mode in {"bm25", "hybrid"}:
            rankings["bm25"] = self.bm25_ranking(query, mask, candidates, expand)
        if mode in {"dense", "hybrid"}:
            rankings["dense"] = self.dense_ranking(query, mask, candidates)
        return rankings

    def search(self, query, k=5, mode="hybrid", candidates=50, levels=None, titles=None):
        """mode: bm25 / dense / hybrid。levels、titles 用于按元数据先过滤再检索。"""
        fused = rrf_fuse(self.rankings(query, mode, candidates, levels, titles))
        return [Hit(self.chunks[index], entry["score"], entry["ranks"]) for index, entry in fused[:k]]


def _print_hits(hits):
    for position, hit in enumerate(hits, start=1):
        ranks = ", ".join(f"{name}#{rank}" for name, rank in hit.ranks.items())
        print(f"{position}. {hit.citation}  [{hit.chunk['level']}·{hit.chunk['status']}]  ({ranks})")
        print("   " + hit.chunk["text"].replace("\n", "\n   ")[:400])
        if hit.chunk.get("legal_basis"):
            print("   依据：" + "、".join(hit.chunk["legal_basis"]))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="把 knowledge/processed 下的条文向量化并建立索引")
    build.add_argument("--model", default=DEFAULT_MODEL)
    build.add_argument("--batch-size", type=int, default=16)
    search = commands.add_parser("search", help="检索条文")
    search.add_argument("query")
    search.add_argument("-k", type=int, default=5)
    search.add_argument("--mode", choices=["bm25", "dense", "hybrid"], default="hybrid")
    search.add_argument("--level", action="append", help="只检索某一效力层级，例如 法律、部门规章，可重复")
    serve = commands.add_parser("serve", help="启动检索台网页")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.command == "build":
        manifest = build_index(model_name=args.model, batch_size=args.batch_size)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    elif args.command == "serve":
        from ecom_agent_system import server

        server.serve(args.host, args.port)
    else:
        knowledge_base = KnowledgeBase.load()
        extra = expansion_terms(args.query, knowledge_base.synonyms)
        if extra and args.mode != "dense":
            print("BM25 查询扩展：" + "、".join(extra))
        _print_hits(knowledge_base.search(args.query, k=args.k, mode=args.mode, levels=args.level))


if __name__ == "__main__":
    main()
