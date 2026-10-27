import copy
import sys
from pathlib import Path

import numpy as np
import pytest
import yaml

from ecom_agent_system.shop.catalog import CATEGORY_POLICY_PATH, CategoryPolicy, import_products, import_users
from ecom_agent_system.shop.db import connect, create_schema
from ecom_agent_system.shop.rules import PlatformRules
from ecom_agent_system.shop.service import Shop

# 抓取脚本放在 knowledge/ 下作为独立脚本运行，测试时把这个目录加入导入路径
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "knowledge"))

VOCABULARY = ("退货", "快递", "赔偿")


class KeywordEncoder:
    """测试用的假向量模型：按关键词是否出现生成向量，避免下载真实模型。"""

    def encode(self, texts, prompt=None, normalize_embeddings=True, convert_to_numpy=True):
        vectors = np.array([[float(word in text) for word in VOCABULARY] for text in texts]) + 1e-6
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


def _chunk(chunk_id, title, level, article_no, text):
    return {
        "chunk_id": chunk_id,
        "title": title,
        "level": level,
        "article_no": article_no,
        "chapter": "",
        "text": text,
        "status": "有效",
    }


CHUNKS = [
    _chunk("a#1", "甲法", "法律", "第一条", "消费者有权自收到商品之日起七日内退货。"),
    _chunk("a#2", "甲法", "法律", "第二条", "经营者有欺诈行为的，应当增加赔偿。"),
    _chunk("b#1", "乙办法", "部门规章", "第一条", "快递在运输中丢失的，按照保价规则赔偿。"),
]


@pytest.fixture
def make_knowledge_base():
    """返回一个工厂：在三条测试条文之外可以再加条目，也可以带同义词表。"""
    from ecom_agent_system.knowledge_base import KnowledgeBase

    def make(extra_chunks=(), synonyms=None):
        chunks = [*CHUNKS, *extra_chunks]
        encoder = KeywordEncoder()
        embeddings = encoder.encode([chunk["text"] for chunk in chunks]).astype(np.float32)
        manifest = {"model": "keyword-test", "query_instruction": ""}
        return KnowledgeBase(chunks, embeddings, manifest, encoder=encoder, synonyms=synonyms)

    return make


@pytest.fixture
def knowledge_base(make_knowledge_base):
    return make_knowledge_base()


# ---------------------------------------------------------------- 模拟商城
# 五件商品覆盖普通商品和几类退货例外；A3、A4 的用户画像相同，导入后只有四个用户

SHOP_RULES = PlatformRules.load()


def _persona(code, province, city, tier):
    return {"用户ID": code, "注册时间": "2020-01-01T08:00:00Z", "地区信息": {"省份": province, "城市": city, "区县": "中心区"}, "人口属性": {"会员等级": tier}}


def _item(asin, category, title, pricing, persona, options=None):
    return {
        "asin": asin,
        "tag": "train",
        "title": title,
        "shop_name": "测试店",
        "category": category,
        "attribute": [],
        "customization_options": options or {},
        "images": [],
        "pricing": list(pricing),
        "user_persona": persona,
    }


COLORS = {"颜色分类": [{"value": "黑色", "price": 199, "is_available": True}, {"value": "白色", "price": 219, "is_available": True}]}
PLATINUM = _persona("U3", "广东省", "深圳市", "铂金会员")
ITEMS = [
    _item("A1", "影音电器›耳机/耳麦", "蓝牙耳机", (199, 219), _persona("U1", "浙江省", "杭州市", "普通会员"), COLORS),
    _item("A2", "粮油调味/速食/干货/烘焙›速食", "方便面", (30,), _persona("U2", "新疆", "乌鲁木齐", "黄金会员")),
    _item("A3", "全屋定制›门", "定制木门", (1500,), PLATINUM),
    _item("A4", "鲜花速递/花卉仿真/绿植园艺›种子/种苗/球根", "多肉种苗", (25,), copy.deepcopy(PLATINUM)),  # 画像重复
    _item("A5", "智能设备›智能翻译机", "翻译机", (899,), _persona("U4", "上海", "上海", "钻石会员")),
]


@pytest.fixture
def shop_items():
    return ITEMS


@pytest.fixture
def categories():
    document = yaml.safe_load(CATEGORY_POLICY_PATH.read_text(encoding="utf-8"))
    return CategoryPolicy({**document, "mark_rate": 1.0})  # 可以标注的商品都标注了，结果确定


@pytest.fixture
def shop(categories):
    conn = connect(":memory:", check_same_thread=False)  # 订单网关在线程池里处理请求
    create_schema(conn)
    import_products(conn, ITEMS, categories, seed=1)
    import_users(conn, ITEMS, seed=1)
    return Shop(conn, SHOP_RULES, categories, seed=1)
