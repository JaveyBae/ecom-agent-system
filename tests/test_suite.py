"""评测套件：在一个小世界上生成全部情形的场景，参考解法必须全部通过，基线策略只能在该过的情形上过。"""

import shutil
from datetime import datetime

import pytest

from ecom_agent_system.agent.baselines import BASELINES
from ecom_agent_system.agent.scenarios import ReferenceModel, run_scenarios
from ecom_agent_system.agent.suite import KINDS, build_suite
from ecom_agent_system.shop.db import connect, ensure_agent_schema, parse
from ecom_agent_system.shop.service import Shop
from ecom_agent_system.shop.world import build_world

from conftest import SHOP_RULES

NOW = datetime(2026, 10, 24, 12, 0, 0)
CITIES = (("浙江省", "杭州市"), ("江苏省", "苏州市"), ("广东省", "广州市"), ("四川省", "成都市"), ("新疆", "乌鲁木齐"))
PRODUCTS = (  # (类目, 标题, 价格)：普通商品覆盖三档金额，另有食品和定作
    ("影音电器›耳机/耳麦", "蓝牙耳机", 199), ("影音电器›数据线", "快充数据线", 25), ("影音电器›投影仪", "家用投影仪", 2999),
    ("粮油调味/速食/干货/烘焙›速食", "方便面", 30), ("全屋定制›门", "定制木门", 1500), ("影音电器›音箱", "便携音箱", 599),
)
# 所有情形都要能生成；数量少的情形（破损拒收、大额停滞、售后频繁账户的延迟发货）在小世界里可能凑不齐
RARE = {"rejected", "escalate_amount", "review", "stalled", "risk_review", "return_and_rebuy", "cancel_and_rebuy"}  # 小世界的商品没有规格可换


def _items():
    items = []
    for index in range(60):
        province, city = CITIES[index % len(CITIES)]
        category, title, price = PRODUCTS[index % len(PRODUCTS)]
        persona = {"用户ID": f"P{index}", "注册时间": "2020-01-01T08:00:00Z", "地区信息": {"省份": province, "城市": city, "区县": "中心区"},
                   "人口属性": {"会员等级": "普通会员" if index % 4 else "黄金会员"}}
        items.append({"asin": f"B{index:03d}", "tag": "train", "title": f"{title}{index}号", "shop_name": "测试店", "category": category,
                      "attribute": [], "customization_options": {}, "images": [], "pricing": [price], "user_persona": persona})
    return items


@pytest.fixture(scope="module")
def suite(tmp_path_factory, request):
    """生成一个 60 位顾客、900 单的小世界，在副本上生成套件。返回 (套件库的路径, 场景)。"""
    from ecom_agent_system.shop.catalog import CategoryPolicy

    directory = tmp_path_factory.mktemp("suite")
    conn = connect(directory / "world.db")
    build_world(conn, _items(), rules=SHOP_RULES, categories=CategoryPolicy.load(), seed=3, orders=900, now=NOW, days=45)
    conn.close()
    shutil.copyfile(directory / "world.db", directory / "suite.db")
    shop = _open(directory / "suite.db")
    scenarios = build_suite(shop, seed=0, per_kind=1)
    shop.conn.commit()
    shop.conn.close()
    return directory, scenarios


def _open(path):
    conn = connect(path)
    ensure_agent_schema(conn)
    return Shop(conn, SHOP_RULES, seed=3)


def _copy(directory, name):
    shutil.copyfile(directory / "suite.db", directory / name)
    return _open(directory / name)


def test_suite_covers_the_situations_with_distinct_customers(suite):
    directory, scenarios = suite
    kinds = {scenario["kind"] for scenario in scenarios}
    assert set(KINDS) - kinds <= RARE, set(KINDS) - kinds
    assert len({scenario["user_id"] for scenario in scenarios}) == len(scenarios)  # 场景之间互不影响
    shop = _open(directory / "suite.db")
    for scenario in scenarios:
        assert scenario["category"] and scenario["reference"] and parse(scenario["now"]) <= NOW
        if scenario["kind"] == "merchant_rejected":  # 搭好的状态已经在库里
            ticket_id = scenario["setup"][0]["ticket_id"]
            assert shop.get_after_sales(ticket_id, now=parse(scenario["now"]))["status"] == "商家拒绝"
        if scenario["kind"] == "repeat_contact":
            assert shop.conn.execute("SELECT summary FROM conversations WHERE user_id = ?", (scenario["user_id"],)).fetchone()
        if scenario["accept"] == "any":
            assert scenario["category"] == "对抗"


def test_reference_solutions_pass_every_hard_check(suite, make_knowledge_base):
    directory, scenarios = suite
    shop = _copy(directory, "reference.db")
    records = run_scenarios(shop, make_knowledge_base(), scenarios, None, directory / "reference.jsonl", model_for=ReferenceModel)
    failed = {r["scenario"]["scenario_id"]: [n for n in r["grade"]["hard"] if not r["grade"]["checks"][n]] for r in records if not r["grade"]["passed"]}
    assert failed == {}
    assert sum(r["grade"]["money"]["over"] for r in records) == 0


NOTHING_TO_SAY = {"rt_other_order", "rt_injection"}  # 只说“好的”也算过的情形：拒绝就是全部该做的
SHOULD_TRANSFER = {"escalate_amount", "escalate_request", "escalate_fraud", "rt_overcompensation"}
# 顺从型（知道顾客要什么就办什么、不看规则）过不了的情形：要拒绝的、要判断的、关卡会拦的、不该许诺的、要记事和要讲清审核的
COMPLY_FAILS = {"return_denied", "change_address_late", "escalate_amount", "escalate_fraud", "rt_injection", "rt_fake_quality",
                "rt_overcompensation", "note_taking", "note_recall", "risk_review", "legal_freight",  # 追问法律依据的要引用法条
                "budget_exceeded"}  # 超预算：顾客什么都不确认，顺从型只会说“办好了”


@pytest.mark.parametrize("name, allowed, must_fail", [
    ("silent", NOTHING_TO_SAY, None),
    ("transfer", SHOULD_TRANSFER, None),
    ("comply", None, COMPLY_FAILS),
    ("comply-fabricate", None, COMPLY_FAILS),
])
def test_baselines_only_pass_where_they_should(suite, make_knowledge_base, name, allowed, must_fail):
    """评分要是有空子，强化学习一定会钻：几个明显不对的策略只能在该过的情形上过。"""
    directory, scenarios = suite
    shop = _copy(directory, f"{name}.db")
    records = run_scenarios(shop, make_knowledge_base(), scenarios, None, directory / f"{name}.jsonl", model_for=BASELINES[name])
    passed = {r["scenario"]["kind"] for r in records if r["grade"]["passed"]}
    if allowed is not None:
        assert passed <= allowed, passed - allowed
    if must_fail is not None:
        assert not (passed & must_fail), passed & must_fail
    fake = next((r for r in records if r["scenario"]["kind"] == "rt_fake_quality"), None)
    if fake is not None and name.startswith("comply"):  # 编造质量问题：顺从型被凭证关卡拦下，造假型过了关卡但凭证不是顾客说的
        failed = {n for n in fake["grade"]["hard"] if fake["grade"]["checks"][n] is False}
        assert ("凭证是顾客说的" if name == "comply-fabricate" else "没有被关卡拦下的办理") in failed, failed
