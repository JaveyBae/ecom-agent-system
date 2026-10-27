import copy

import pytest
import yaml

from build_platform_rules import RULES_PATH, RuleError, legal_citations, render_text, to_records, validate

CITATION = "《某某法》第一条"


def _document(*rules):
    return {
        "platform": "测试商城",
        "title": "测试商城平台规则",
        "version": "test",
        "updated": "2026-10-23",
        "rules": list(rules),
    }


def _rule(rule_id="P-RET-01", text="签收次日起{days}日内可以退货。", params=None, legal_basis=None):
    return {
        "id": rule_id,
        "category": "七天无理由退货",
        "title": "退货期限",
        "text": text,
        "params": {"days": 7} if params is None else params,
        "legal_basis": [CITATION] if legal_basis is None else legal_basis,
    }


def test_repository_rules_are_valid():
    document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    rendered = validate(document, legal_citations())
    assert len(rendered) == len(document["rules"])
    records = to_records(document, rendered)
    assert all(record["level"] == "平台规则" for record in records)
    assert len({record["chunk_id"] for record in records}) == len(records)


def test_render_text_joins_lines_and_fills_params():
    rule = _rule(text="  第一段，{days}日。\n  第二段。\n", params={"days": 15})
    assert render_text(rule) == "第一段，15日。\n第二段。"


@pytest.mark.parametrize(
    ("params", "message"),
    [({}, "没有对应参数"), ({"days": 7, "fee": 10}, "没有出现在文本里")],
)
def test_render_text_requires_params_to_match_placeholders(params, message):
    with pytest.raises(RuleError, match=message):
        render_text(_rule(params=params))


def test_validate_reports_every_problem():
    broken = _document(
        _rule("P-RET-01"),
        _rule("P-RET-01"),
        _rule("P-SHIP-01", text="见 P-MEM-09，{days}日。"),
        _rule("P-QA-01", legal_basis=["《某某法》第九十九条"]),
    )
    with pytest.raises(RuleError) as error:
        validate(broken, {CITATION})
    message = str(error.value)
    assert "规则编号重复: P-RET-01" in message
    assert "不存在的规则 P-MEM-09" in message
    assert "法律依据不在知识库里: 《某某法》第九十九条" in message


def test_changing_a_param_changes_the_text():
    document = _document(_rule())
    changed = copy.deepcopy(document)
    changed["rules"][0]["params"]["days"] = 15
    assert validate(document, {CITATION})["P-RET-01"] == "签收次日起7日内可以退货。"
    assert validate(changed, {CITATION})["P-RET-01"] == "签收次日起15日内可以退货。"
