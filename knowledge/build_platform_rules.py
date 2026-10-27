"""校验平台规则文件，并把规则转换成知识库条目。

读取 knowledge/platform_rules.yaml，检查以下几点后渲染规则文本，写入
knowledge/processed/platform_rules.jsonl，和法规条文一起建索引：
  - 规则编号唯一、格式正确
  - 文本里的 {占位符} 和 params 一一对应
  - legal_basis 里的条文在法规知识库里存在
  - 文本里提到的其他规则编号存在

用法: uv run python knowledge/build_platform_rules.py
"""

from __future__ import annotations

import json
import re
import string
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
RULES_PATH = ROOT / "platform_rules.yaml"
OUT_PATH = ROOT / "processed" / "platform_rules.jsonl"
LEGAL_CORPUS = (ROOT / "processed" / "laws.jsonl", ROOT / "processed" / "rules.jsonl")
RULE_ID_RE = re.compile(r"P-[A-Z]+-\d{2}")
REQUIRED_FIELDS = ("id", "category", "title", "text", "legal_basis")


class RuleError(ValueError):
    pass


def legal_citations(paths=LEGAL_CORPUS):
    citations = set()
    for path in paths:
        with Path(path).open(encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    record = json.loads(line)
                    citations.add(f"《{record['title']}》{record['article_no']}")
    return citations


def render_text(rule):
    """去掉 YAML 块里每行的缩进，再用 params 填充占位符；一行是一段。"""
    template = "\n".join(line.strip() for line in rule["text"].splitlines() if line.strip())
    params = rule.get("params") or {}
    fields = {name for _, name, _, _ in string.Formatter().parse(template) if name}
    missing = sorted(fields - params.keys())
    unused = sorted(params.keys() - fields)
    if missing:
        raise RuleError(f"{rule['id']}: 文本里的占位符没有对应参数: {missing}")
    if unused:
        raise RuleError(f"{rule['id']}: 参数没有出现在文本里: {unused}")
    return template.format(**params)


def validate(document, citations):
    """返回 {规则编号: 渲染后的文本}；发现任何问题都汇总后一起报出。"""
    rules = document.get("rules") or []
    ids = [rule.get("id") for rule in rules]
    errors = [f"规则编号重复: {rule_id}" for rule_id in sorted({i for i in ids if ids.count(i) > 1})]
    rendered = {}
    for rule in rules:
        absent = [name for name in REQUIRED_FIELDS if name not in rule]
        if absent:
            errors.append(f"{rule.get('id', '?')}: 缺少字段 {absent}")
            continue
        if not RULE_ID_RE.fullmatch(rule["id"]):
            errors.append(f"{rule['id']}: 规则编号格式应为 P-类别-两位数字")
        try:
            text = render_text(rule)
        except RuleError as error:
            errors.append(str(error))
            continue
        for reference in RULE_ID_RE.findall(text):
            if reference not in ids:
                errors.append(f"{rule['id']}: 文本引用了不存在的规则 {reference}")
        for citation in rule["legal_basis"] or []:
            if citation not in citations:
                errors.append(f"{rule['id']}: 法律依据不在知识库里: {citation}")
        rendered[rule["id"]] = text
    if errors:
        raise RuleError("\n".join(errors))
    return rendered


def to_records(document, rendered):
    return [
        {
            "chunk_id": f"platform#{rule['id']}",
            "doc_id": "shopsim-platform-rules",
            "title": document["title"],
            "level": "平台规则",
            "issuer": f"{document['platform']}（虚构平台）",
            "article_no": rule["id"],
            "chapter": f"{rule['category']} / {rule['title']}",
            "text": rendered[rule["id"]],
            "promulgated": str(document["updated"]),
            "effective": None,
            "status": "模拟（草案）",
            "version": document["version"],
            "source": "自拟规则（虚构平台）",
            "source_file": "knowledge/platform_rules.yaml",
            "legal_basis": list(rule["legal_basis"] or []),
            "params": dict(rule.get("params") or {}),
        }
        for rule in document["rules"]
    ]


def main():
    document = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    try:
        rendered = validate(document, legal_citations())
    except RuleError as error:
        print(f"平台规则校验失败:\n{error}", file=sys.stderr)
        return 1
    records = to_records(document, rendered)
    with OUT_PATH.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    with_basis = sum(1 for record in records if record["legal_basis"])
    categories = sorted({record["chapter"].split(" / ")[0] for record in records})
    print(f"{document['title']} {document['version']}: {len(records)} 条规则，{len(categories)} 个类别，"
          f"{with_basis} 条标注了法律依据 → {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
