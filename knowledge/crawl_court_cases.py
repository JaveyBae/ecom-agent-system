"""抓取最高人民法院发布的消费者权益保护、网络消费典型案例，整理成评测用的案例。

每个案例拆成要旨、案名、基本案情、裁判结果、典型意义，并抽取文中引用的法条，
对应到法规知识库里的条文。输出写入 knowledge/eval/court_cases.jsonl。

案例里有法院给出的答案，只能用来评测，不能放进知识库，否则 Agent 可以直接检索到答案。

用法: uv run python knowledge/crawl_court_cases.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from crawl_flk import force_ipv4
from crawl_rules import container_paragraphs, fetch

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "raw" / "court"
OUT_PATH = ROOT / "eval" / "court_cases.jsonl"
KB_CORPUS = (ROOT / "processed" / "laws.jsonl", ROOT / "processed" / "rules.jsonl")
PLATFORM_RULES = ROOT / "processed" / "platform_rules.jsonl"

BATCHES = [
    {"id": "spc-20220315", "date": "2022-03-15", "title": "最高法发布消费者权益保护典型案例", "page": "350961", "cases": 10},
    {"id": "spc-20230315", "date": "2023-03-15", "title": "网络消费典型案例", "page": "393481", "cases": 10},
    {"id": "spc-20250616", "date": "2025-06-16", "title": "最高人民法院发布网络消费民事典型案例", "page": "467931", "cases": 5},
    {"id": "spc-20260315", "date": "2026-03-15", "title": "最高人民法院发布消费者权益保护典型案例", "page": "494701", "cases": 6},
]
PAGE_URL = "https://www.court.gov.cn/zixun/xiangqing/{page}.html"

SECTIONS = {"基本案情": "facts", "裁判结果": "judgment", "典型意义": "significance"}
SECTION_RE = re.compile(r"^【?(基本案情|裁判结果|典型意义)】?[：:]?\s*(.*)$")
CASE_NUMBER_RE = re.compile(r"^(案例\s*[0-9一二三四五六七八九十]+|目录)$")
ONLINE_SHOPPING_DISPUTES = ("信息网络买卖合同纠纷", "网络购物合同纠纷", "网络买卖合同纠纷", "网络信息购物合同纠纷")
# 案名以案由结尾（如“……信息网络买卖合同纠纷案”），按长度倒序匹配，先认更具体的案由
DISPUTE_TYPES = sorted(
    (
        *ONLINE_SHOPPING_DISPUTES,
        "网络服务合同纠纷",
        "服务合同纠纷",
        "承揽合同纠纷",
        "房屋买卖合同纠纷",
        "车辆租赁合同纠纷",
        "合同纠纷",
        "网络侵权责任纠纷",
        "侵权责任纠纷",
        "产品责任纠纷",
        "产品销售者责任纠纷",
        "个人信息保护纠纷",
    ),
    key=len,
    reverse=True,
)
NUMERAL = "一二三四五六七八九十百千零"


def _section(line):
    match = SECTION_RE.match(line)
    # 只把短行当作小标题，避免正文里恰好以“基本案情”开头的句子被误判
    if match and len(line) <= len(match.group(1)) + 4 + len(match.group(2)) and (not match.group(2) or line.startswith("【")):
        return match.group(1), match.group(2)
    return None


def _title_block(paragraphs, marker, floor):
    """从“基本案情”往前找案例标题：要旨一到几行，加上以“——”开头的案名。

    遇到句号结尾的正文、小标题、“案例N”编号，或者另一个案名（目录里的条目）时停下。
    """
    start, lines = marker, []
    for index in range(marker - 1, floor - 1, -1):
        line = paragraphs[index]
        if CASE_NUMBER_RE.match(line) or line.endswith(("。", "；")) or _section(line):
            break
        if "——" in line and any("——" in collected for collected in lines):
            break
        lines.insert(0, line)
        start = index
        if len(lines) == 4:
            break
    return start, "".join(lines)


def split_cases(paragraphs):
    markers = [index for index, line in enumerate(paragraphs) if (_section(line) or ("",))[0] == "基本案情"]
    cases, floor = [], 0
    titles = []
    for marker in markers:
        titles.append(_title_block(paragraphs, marker, floor))
        floor = marker + 1
    for number, (marker, (title_start, title)) in enumerate(zip(markers, titles)):
        end = titles[number + 1][0] if number + 1 < len(titles) else len(paragraphs)
        sections, current = {}, None
        for line in paragraphs[marker:end]:
            if CASE_NUMBER_RE.match(line):  # 下一个案例的编号，不属于本案正文
                continue
            header = _section(line)
            if header:
                current = SECTIONS[header[0]]
                sections[current] = [header[1]] if header[1] else []
            elif current:
                sections[current].append(line)
        holding, _, case_name = title.partition("——")
        cases.append(
            {
                "holding": holding.strip("：: "),
                "case_name": case_name.strip(),
                **{key: "\n".join(sections.get(key, [])) for key in SECTIONS.values()},
            }
        )
    return cases


def _law_names(titles):
    """知识库里的法规名，以及去掉“中华人民共和国”的简称，按长度倒序避免短名先匹配。"""
    names = {}
    for title in titles:
        names[title] = title
        if title.startswith("中华人民共和国"):
            names[title[len("中华人民共和国"):]] = title
    return names


_ARTICLE_RUN = r"((?:第[{n}]+条(?:第[{n}]+款)?(?:第[（(][{n}]+[)）]项)?[、，和及与]?)+)".format(n=NUMERAL)


def extract_citations(text, law_names):
    """抽取“《某法》第X条、第Y条”和不带书名号的“依据某法第X条”，只保留到“条”。

    不带书名号的只认知识库里的法规名，按长度倒序匹配，
    这样“消费者权益保护法实施条例”不会被当成“消费者权益保护法”。
    """
    names = "|".join(re.escape(name) for name in sorted(law_names, key=len, reverse=True))
    patterns = (
        re.compile(r"《([^》]{2,60})》" + _ARTICLE_RUN),
        re.compile(r"(?<!《)(" + names + r")" + _ARTICLE_RUN),
    )
    citations = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            for article in re.findall(rf"第[{NUMERAL}]+条", match.group(2)):
                citations.append((match.group(1), article))
    return list(dict.fromkeys(citations))


def dispute_type(case_name):
    name = case_name.removesuffix("案")
    return next((kind for kind in DISPUTE_TYPES if name.endswith(kind)), None)


def mentioned_laws(text, law_names):
    """文中提到的法规名（不论是否写了条号）。有些案例只写“依据消费者权益保护法”，不写条号。"""
    found = []
    for name in sorted(law_names, key=len, reverse=True):
        if name in text and not any(name in longer for longer in found):
            found.append(name)
    return sorted({law_names[name] for name in found})


def main():
    force_ipv4()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    kb = set()
    for path in KB_CORPUS:
        with path.open(encoding="utf-8") as stream:
            kb.update((record["title"], record["article_no"]) for record in map(json.loads, stream))
    law_names = _law_names({title for title, _ in kb})
    core = set()
    with PLATFORM_RULES.open(encoding="utf-8") as stream:
        for record in map(json.loads, stream):
            core.update(record["legal_basis"])

    records, report, problems = [], [], []
    for batch in BATCHES:
        url = PAGE_URL.format(page=batch["page"])
        paragraphs = container_paragraphs(fetch(url, RAW_DIR / f"{batch['page']}.html"), "zoom")
        cases = split_cases(paragraphs)
        if len(cases) != batch["cases"]:
            problems.append(f"{batch['id']}: 公布 {batch['cases']} 个案例，解析出 {len(cases)} 个")
        for number, case in enumerate(cases, start=1):
            case_id = f"{batch['id']}-{number:02d}"
            missing = [key for key in ("holding", "case_name", *SECTIONS.values()) if not case[key]]
            if missing:
                problems.append(f"{case_id}: 缺少 {missing}")
            kind = dispute_type(case["case_name"])
            if kind is None:
                problems.append(f"{case_id}: 没有识别出案由: {case['case_name']}")
            reasoning = "\n".join((case["judgment"], case["significance"]))
            citations = []
            for name, article in extract_citations(reasoning, law_names):
                title = law_names.get(name) or law_names.get(name.removeprefix("中华人民共和国"))
                resolved = f"《{title}》{article}" if title and (title, article) in kb else None
                citations.append({"raw": f"《{name}》{article}", "kb": resolved, "in_core": resolved in core})
            records.append(
                {
                    "case_id": case_id,
                    "batch": {key: batch[key] for key in ("id", "date", "title")} | {"url": url},
                    **case,
                    "dispute_type": kind,
                    "online_shopping": kind in ONLINE_SHOPPING_DISPUTES,
                    "citations": citations,
                    "laws_mentioned": mentioned_laws(reasoning, law_names),
                    "source": "最高人民法院 court.gov.cn",
                }
            )
        report.append(f"{batch['date']} {batch['title']}: {len(cases)} 个案例")

    with OUT_PATH.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("\n".join(report))
    print(f"共 {len(records)} 个案例，其中网购买卖类 {sum(r['online_shopping'] for r in records)} 个 → {OUT_PATH}")
    if problems:
        print("解析问题:\n  " + "\n  ".join(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
