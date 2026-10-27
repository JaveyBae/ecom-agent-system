"""商品和用户：从 ShopSimulator 的商品数据导入。

商品数据（fine_items_eval_train_all.json.gz）不随仓库提交，建库时指定路径。部分商品带有
ShopSimulator 生成的用户画像，按内容去重后作为用户；画像里没有姓名和联系方式，这些是
按随机种子生成的假数据。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import random
from pathlib import Path

import yaml

from ecom_agent_system.shop.logistics import WAREHOUSE_CITY
from ecom_agent_system.shop.rules import PROJECT_ROOT

CATEGORY_POLICY_PATH = PROJECT_ROOT / "shop" / "category_policy.yaml"

JZH = {"浙江省", "江苏省", "上海市"}
REMOTE = {"新疆维吾尔自治区", "西藏自治区", "青海省", "宁夏回族自治区", "甘肃省", "内蒙古自治区"}
PROVINCES = {  # 画像里有“上海”“广西”这样的简称
    "北京": "北京市",
    "天津": "天津市",
    "上海": "上海市",
    "重庆": "重庆市",
    "内蒙古": "内蒙古自治区",
    "广西": "广西壮族自治区",
    "西藏": "西藏自治区",
    "宁夏": "宁夏回族自治区",
    "新疆": "新疆维吾尔自治区",
    "香港": "香港特别行政区",
    "澳门": "澳门特别行政区",
}
TIERS = {"普通会员": "普通", "黄金会员": "黄金", "铂金会员": "铂金", "钻石会员": "钻石"}
SURNAMES = "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘蒋蔡余杜叶程苏魏吕丁沈姚卢姜崔钟谭陆汪范金石廖贾夏韦方白邹孟熊秦邱江尹薛段雷侯龙史陶黎贺顾毛郝龚邵万钱严武戴莫孔汤"
GIVEN_NAMES = "伟芳娜敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英华玉兰萍红鹏辉建文斌宇浩凯雪琳晨欣怡婷佳慧颖梦晓雨嘉子轩涵若思"
STREETS = ("人民路", "解放路", "建设路", "中山路", "文化路", "学府路", "新华路", "长江路", "滨江路", "环城北路", "和平路", "胜利街")


def load_items(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def _match(category, mapping):
    """按“›”分隔的类目路径前缀匹配，取最长的一项。"""
    parts = category.split("›")
    for size in range(len(parts), 0, -1):
        key = "›".join(parts[:size])
        if key in mapping:
            return key
    return None


class CategoryPolicy:
    def __init__(self, document):
        self.classes = document["classes"]
        self.categories = document["categories"]
        self.mark_rate = float(document.get("mark_rate", 0))
        self.durable = list(document.get("durable", []))
        unknown = set(self.categories.values()) - set(self.classes)
        if unknown:
            raise ValueError(f"category_policy.yaml 里有未定义的分类: {sorted(unknown)}")

    @classmethod
    def load(cls, path=CATEGORY_POLICY_PATH):
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def classify(self, category):
        key = _match(category, self.categories)
        return self.categories[key] if key else None

    def kind(self, policy_class):
        return self.classes[policy_class]["kind"] if policy_class else None

    def rule_ids(self, policy_class):
        return list(self.classes[policy_class]["rules"]) if policy_class else []

    def is_durable(self, category):
        return _match(category, dict.fromkeys(self.durable)) is not None

    def unused_keys(self, categories):
        """映射表里没有匹配到任何商品的类目，通常是类目名写错了。"""
        categories = list(categories)
        used = {_match(category, self.categories) for category in categories}
        used |= {_match(category, dict.fromkeys(self.durable)) for category in categories}
        return sorted((set(self.categories) | set(self.durable)) - used)


def _price(value):
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price >= 0 else None


def normalize_options(raw):
    """ShopSimulator 的 customization_options → {规格名: [{value, price, available}]}。"""
    options = {}
    for axis, entries in (raw or {}).items():
        values = [
            {"value": str(entry["value"]), "price": _price(entry.get("price")), "available": bool(entry.get("is_available", True))}
            for entry in entries or []
            if isinstance(entry, dict) and entry.get("value") is not None
        ]
        if values:
            options[str(axis)] = values
    return options


def variant_price(options, selected, base_prices=()):
    """按 ShopSimulator 的规则算所选规格的单价，算不出唯一价格时返回 None。

    只有一个规格维度的价格各不相同时，取所选那一项的价格；所有规格同价时取这个价格；
    没有规格时取唯一的基础价。有两个以上维度的价格各不相同时无法确定。
    """
    priced = {
        axis: {entry["value"]: entry["price"] for entry in values if entry["price"] is not None}
        for axis, values in options.items()
    }
    varying = [axis for axis, prices in priced.items() if len(set(prices.values())) > 1]
    if len(varying) > 1:
        return None
    if varying:
        return priced[varying[0]].get(selected.get(varying[0]))
    constant = {price for prices in priced.values() for price in prices.values()}
    if len(constant) == 1:
        return constant.pop()
    if not options and len(set(base_prices)) == 1:
        return base_prices[0]
    return None


def product_record(item, policy, rng):
    category = item.get("category") or ""
    policy_class = policy.classify(category)
    kind = policy.kind(policy_class)
    if kind is None:
        marked = False
    elif kind in ("法定不适用", "拆封后不适用"):
        marked = True  # 法定不适用的商品页上都会写明
    else:
        marked = rng.random() < policy.mark_rate
    pricing = [price for price in map(_price, item.get("pricing") or []) if price is not None]
    images = item.get("images") or []
    return {
        "asin": str(item["asin"]),
        "title": item["title"],
        "shop_name": item.get("shop_name"),
        "category": category,
        "domain": item.get("domain_zh"),
        "price_min": min(pricing) if pricing else None,
        "price_max": max(pricing) if pricing else None,
        "options": json.dumps(normalize_options(item.get("customization_options")), ensure_ascii=False),
        "attributes": json.dumps(item.get("attribute") or [], ensure_ascii=False),
        "image": images[0] if images else None,
        "policy_class": policy_class,
        "no_return_marked": int(marked),
        "durable": int(policy.is_durable(category)),
        "source_split": item.get("tag"),
    }


def import_products(conn, items, policy, seed):
    rows, seen = [], set()
    for item in items:
        asin = str(item.get("asin") or "")
        if not asin or not item.get("title") or asin in seen:
            continue
        seen.add(asin)
        rows.append(product_record(item, policy, random.Random(f"{seed}:mark:{asin}")))
    _insert(conn, "products", rows)
    return len(rows)


def _insert(conn, table, rows):
    if rows:
        columns = list(rows[0])
        placeholders = ", ".join(":" + column for column in columns)
        conn.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", rows)


def normalize_province(name):
    name = (name or "").strip()
    for short, full in PROVINCES.items():
        if name.startswith(short):
            return full
    return name if not name or name.endswith("省") else name + "省"


def normalize_city(name, province):
    name = (name or "").strip()
    if not name:
        return province  # 直辖市的画像有时只写了省份
    return name if name.endswith(("市", "州", "盟", "地区")) else name + "市"


def zone_of(province, city):
    """收货地址所在区域（P-SHIP-08）。同城指和杭州仓同城。"""
    if city == WAREHOUSE_CITY:
        return "同城"
    if province in JZH:
        return "江浙沪"
    if province in REMOTE:
        return "偏远"
    return "一般"


def persona_key(persona):
    """画像内容的指纹，和字段顺序无关。"""
    return hashlib.sha1(json.dumps(persona, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def personas(items):
    """去重后的用户画像。画像里的用户 ID 有重复的占位值，所以按画像内容去重。"""
    seen = set()
    for item in items:
        persona = item.get("user_persona")
        if not isinstance(persona, dict) or not persona:
            continue
        key = persona_key(persona)
        if key not in seen:
            seen.add(key)
            yield persona


def _phone(rng):
    return "1" + rng.choice("3456789") + "".join(rng.choice("0123456789") for _ in range(9))


def user_record(index, persona, rng):
    region = persona.get("地区信息") or {}
    province = normalize_province(region.get("省份"))
    city = normalize_city(region.get("城市"), province)
    district = (region.get("区县") or "").strip() or None
    user_id = f"U{index:06d}"
    address = f"{rng.choice(STREETS)}{rng.randint(1, 999)}号"
    if rng.random() < 0.7:
        address += f"{rng.randint(1, 30)}幢{rng.randint(1, 30)}0{rng.randint(1, 4)}室"
    registered = str(persona.get("注册时间") or "").replace("T", " ").replace("Z", "")[:19] or None
    return {
        "user_id": user_id,
        "name": rng.choice(SURNAMES) + "".join(rng.choice(GIVEN_NAMES) for _ in range(rng.choice((1, 2)))),
        "phone": _phone(rng),
        "email": f"{user_id.lower()}@shopsim.example",
        "tier": TIERS.get((persona.get("人口属性") or {}).get("会员等级"), "普通"),
        "province": province,
        "city": city,
        "district": district,
        "address": address,
        "zone": zone_of(province, city),
        "registered_at": registered,
        "persona": json.dumps(persona, ensure_ascii=False),
    }


def import_users(conn, items, seed):
    rows, phones = [], set()
    for index, persona in enumerate(personas(items), start=1):
        rng = random.Random(f"{seed}:user:{index}")
        row = user_record(index, persona, rng)
        while row["phone"] in phones:
            row["phone"] = _phone(rng)
        phones.add(row["phone"])
        rows.append(row)
    _insert(conn, "users", rows)
    return len(rows)
