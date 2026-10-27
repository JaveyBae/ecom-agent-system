"""从国家法律法规数据库（flk.npc.gov.cn）抓取售后相关法律法规，按条切分。

只抓取现行有效的版本。每部法规保存原始 docx 和元数据，按条切分后写入
knowledge/processed/laws.jsonl，供后续建索引使用。

用法: uv run python knowledge/crawl_flk.py
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import certifi

BASE = "https://flk.npc.gov.cn/law-search"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (ecom-agent-system knowledge crawler)",
    "Referer": "https://flk.npc.gov.cn/",
    "Content-Type": "application/json",
}
ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "raw" / "flk"
OUT_PATH = ROOT / "processed" / "laws.jsonl"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
REQUEST_INTERVAL_SECONDS = 1.5  # 礼貌抓取，避免给政府网站造成压力

STATUS = {1: "已废止", 2: "已修改", 3: "有效", 4: "尚未生效"}

# 精确标题。只取标题完全一致、状态为“有效”的最新版本。
TARGETS = [
    "中华人民共和国消费者权益保护法",
    "中华人民共和国消费者权益保护法实施条例",
    "中华人民共和国电子商务法",
    "中华人民共和国产品质量法",
    "中华人民共和国个人信息保护法",
    "中华人民共和国食品安全法",
    "中华人民共和国民法典",
    "中华人民共和国价格法",
    "快递暂行条例",
    "中华人民共和国邮政法",
    "最高人民法院关于审理网络消费纠纷案件适用法律若干问题的规定（一）",
    "最高人民法院关于审理食品药品纠纷案件适用法律若干问题的规定",
    "最高人民法院关于审理食品安全民事纠纷案件适用法律若干问题的解释（一）",
    "最高人民法院关于审理食品药品惩罚性赔偿纠纷案件适用法律若干问题的解释",
]

ARTICLE_RE = re.compile(r"^第([一二三四五六七八九十百千零〇两]+)条[\s\u3000]*")
CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百千零〇]+(编|分编|章|节)[\s\u3000]")


def force_ipv4():
    """本机网络的 IPv6（NAT64）连接这些站点要超时约 60 秒才回退，抓取时只用 IPv4。"""
    if getattr(socket.getaddrinfo, "ipv4_only", False):
        return
    original = socket.getaddrinfo

    def getaddrinfo(host, port, family=0, *args, **kwargs):
        return original(host, port, socket.AF_INET, *args, **kwargs)

    getaddrinfo.ipv4_only = True
    socket.getaddrinfo = getaddrinfo


def _request(path, *, params=None, payload=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(url, data=data, headers=HEADERS, method="POST" if data else "GET")
    with urlopen(request, timeout=60, context=SSL_CONTEXT) as response:
        body = json.loads(response.read().decode("utf-8"))
    time.sleep(REQUEST_INTERVAL_SECONDS)
    if body.get("code") not in (200, None) and "rows" not in body:
        raise RuntimeError(f"{path} failed: {body.get('msg')}")
    return body


def _strip_tags(text):
    return re.sub(r"<[^>]+>", "", text or "")


def find_current_version(title):
    """按标题检索（searchType=1），只接受标题完全一致且状态为“有效”的版本。"""
    payload = {
        "searchRange": 1,
        "sxrq": [],
        "gbrq": [],
        "sxx": [],
        "searchType": 1,
        "xgzlSearch": False,
        "searchContent": title,
        "pageNum": 1,
        "pageSize": 20,
    }
    rows = _request("/search/list", payload=payload).get("rows") or []
    matches = [row for row in rows if _strip_tags(row["title"]) == title and row.get("sxx") == 3]
    if not matches:
        return None
    return max(matches, key=lambda row: row.get("gbrq") or "")


def download_docx(bbbs):
    link = _request("/download/pc", params={"bbbs": bbbs, "format": "docx"})["data"]["url"]
    with urlopen(Request(link, headers={"User-Agent": HEADERS["User-Agent"]}), timeout=120, context=SSL_CONTEXT) as response:
        content = response.read()
    time.sleep(REQUEST_INTERVAL_SECONDS)
    return content, link.split("?")[0]


def docx_paragraphs(content):
    """不依赖 python-docx：直接从 document.xml 里按段落取文本。"""
    with zipfile.ZipFile(BytesIO(content)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    paragraphs = []
    for block in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.S):
        text = "".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", block))
        text = text.replace("\u3000", " ").strip()
        if text:
            paragraphs.append(text)
    return paragraphs


def split_articles(paragraphs):
    """按“第X条”切分；条之前的标题、目录和题注不进入知识库。"""
    articles = []
    chapter_stack = {}
    current = None
    for paragraph in paragraphs:
        chapter = CHAPTER_RE.match(paragraph)
        if chapter and len(paragraph) < 60:
            level = chapter.group(1)
            chapter_stack[level] = re.sub(r"\s+", " ", paragraph)
            # 进入新的一章时，清掉它下面的节
            if level in ("编", "分编"):
                chapter_stack.pop("章", None)
                chapter_stack.pop("节", None)
            elif level == "章":
                chapter_stack.pop("节", None)
            continue
        article = ARTICLE_RE.match(paragraph)
        if article:
            current = {
                "article_no": f"第{article.group(1)}条",
                "chapter": " / ".join(
                    chapter_stack[key] for key in ("编", "分编", "章", "节") if key in chapter_stack
                ),
                "paragraphs": [paragraph[article.end():].strip()],
            }
            articles.append(current)
        elif current is not None:
            current["paragraphs"].append(paragraph)
    for item in articles:
        item["text"] = "\n".join(part for part in item.pop("paragraphs") if part)
    return articles


def main():
    force_ipv4()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    records, report = [], []
    for title in TARGETS:
        row = find_current_version(title)
        if row is None:
            report.append(f"未找到有效版本: {title}")
            continue
        docx_path = RAW_DIR / f"{row['bbbs']}.docx"
        meta_path = RAW_DIR / f"{row['bbbs']}.json"
        if docx_path.exists() and meta_path.exists():
            # 同一版本（bbbs）的文件不会变化，已下载过就直接复用
            content = docx_path.read_bytes()
            meta = json.loads(meta_path.read_text())
        else:
            meta = _download_version(row, docx_path, meta_path)
            content = docx_path.read_bytes()
        articles = split_articles(docx_paragraphs(content))
        gaps = sequence_gaps(articles)
        for article in articles:
            records.append({**meta, **article, "chunk_id": f"{row['bbbs']}#{article['article_no']}"})
        note = f"，条号不连续: {gaps[:5]}" if gaps else ""
        report.append(f"{title}（{meta['level']}，{meta['promulgated']} 公布，{meta['status']}）: {len(articles)} 条{note}")
    with OUT_PATH.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("\n".join(report))
    print(f"共 {len(records)} 条 → {OUT_PATH}")
    return 0 if records else 1


def _download_version(row, docx_path, meta_path):
    detail = _request("/search/flfgDetails", params={"bbbs": row["bbbs"]})["data"]
    content, file_url = download_docx(row["bbbs"])
    docx_path.write_bytes(content)
    meta = {
        "doc_id": row["bbbs"],
        "title": _strip_tags(row["title"]),
        "level": detail.get("flxz"),
        "issuer": detail.get("zdjgName"),
        "promulgated": detail.get("gbrq"),
        "effective": detail.get("sxrq"),
        "status": STATUS.get(detail.get("sxx"), str(detail.get("sxx"))),
        "source": "国家法律法规数据库 flk.npc.gov.cn",
        "source_file": file_url,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    return meta


_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_UNITS = {"十": 10, "百": 100, "千": 1000}


def chinese_number(text):
    """把“一千二百六十”这类条号转成整数。"""
    total, digit = 0, 0
    for char in text:
        if char in _DIGITS:
            digit = _DIGITS[char]
        else:
            total += (digit or 1) * _UNITS[char]
            digit = 0
    return total + digit


def sequence_gaps(articles):
    """条号应当从 1 连续递增；不连续通常说明切分把正文里的“第X条”误当成了新条。"""
    numbers = [chinese_number(article["article_no"][1:-1]) for article in articles]
    return [(index + 1, number) for index, number in enumerate(numbers) if number != index + 1]


if __name__ == "__main__":
    sys.exit(main())
