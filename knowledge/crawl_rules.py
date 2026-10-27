"""抓取国家法律法规数据库没有收录的部门规章和规范性文件，按条切分。

部门规章不在 flk.npc.gov.cn 里。这里只用政府网站上的静态页面：优先用中国政府网
政策文件库和国务院公报，其次是发文部门（交通运输部、中央网信办）、商务部全球法规网
和地方市场监管局的法规栏目。输出格式与 laws.jsonl 相同，写入 processed/rules.jsonl。

用法: uv run python knowledge/crawl_rules.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from urllib.request import Request, urlopen

from pypdf import PdfReader

from crawl_flk import SSL_CONTEXT, force_ipv4, sequence_gaps, split_articles

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "raw" / "rules"
OUT_PATH = ROOT / "processed" / "rules.jsonl"
USER_AGENT = "Mozilla/5.0 (ecom-agent-system knowledge crawler)"
REQUEST_INTERVAL_SECONDS = 1.5

# 现行有效性没有像 flk 那样的接口可查，状态按来源页面的修订记录人工确认。
SOURCES = [
    {
        "doc_id": "samr-2017-90",
        "title": "网络购买商品七日无理由退货暂行办法",
        "issuer": "国家市场监督管理总局",
        "promulgated": "2017-01-06",
        "effective": "2017-03-15",
        "version": "根据2020年10月23日国家市场监督管理总局令第31号修订",
        "url": "https://www.gov.cn/zhengce/zhengceku/2020-11/03/content_5557118.htm",
        "format": "gov_html",
    },
    {
        "doc_id": "samr-2021-37",
        "title": "网络交易监督管理办法",
        "issuer": "国家市场监督管理总局",
        "promulgated": "2021-03-15",
        "effective": "2021-05-01",
        "version": "国家市场监督管理总局令第37号",
        "url": "https://www.gov.cn/zhengce/zhengceku/2021-03/16/content_5593226.htm",
        "format": "gov_html",
    },
    {
        "doc_id": "samr-2025-116",
        "title": "网络交易平台规则监督管理办法",
        "issuer": "国家市场监督管理总局、国家互联网信息办公室",
        "promulgated": "2025-12-18",
        "effective": "2026-02-01",
        "version": "市场监管总局、国家网信办令第116号",
        "url": "https://www.gov.cn/gongbao/2026/issue_12666/202604/content_7065115.html",
        "format": "gov_html",
    },
    {
        "doc_id": "mot-2023-22",
        "title": "快递市场管理办法",
        "issuer": "交通运输部",
        "promulgated": "2023-12-17",
        "effective": "2024-03-01",
        "version": "交通运输部令2023年第22号",
        "url": "https://xxgk.mot.gov.cn/jigou/fgs/202401/P020240104504317085126.pdf",
        "format": "pdf",
    },
    {
        "doc_id": "samr-2020-32",
        "title": "规范促销行为暂行规定",
        "issuer": "国家市场监督管理总局",
        "promulgated": "2020-10-29",
        "effective": "2020-12-01",
        "version": "国家市场监督管理总局令第32号",
        "url": "https://www.gov.cn/zhengce/zhengceku/2020-11/06/content_5557730.htm",
        "format": "gov_html",
    },
    {
        "doc_id": "samr-2022-56",
        "title": "明码标价和禁止价格欺诈规定",
        "issuer": "国家市场监督管理总局",
        "promulgated": "2022-04-14",
        "effective": "2022-07-01",
        "version": "国家市场监督管理总局令第56号",
        "url": "https://www.gov.cn/gongbao/content/2022/content_5699926.htm",
        "format": "gov_html",
    },
    {
        "doc_id": "samr-2025-117",
        "title": "直播电商监督管理办法",
        "issuer": "国家市场监督管理总局、国家互联网信息办公室",
        "promulgated": "2025-12-18",
        "effective": "2026-02-01",
        "version": "市场监管总局、国家网信办令第117号",
        "url": "https://www.cac.gov.cn/2026-01/07/c_1769516655383334.htm",
        "format": "html",
        "container_class": "main-content",
        "source_name": "中央网信办（中国网信网）",
    },
    {
        "doc_id": "samr-2025-121",
        "title": "市场监督管理投诉举报处理办法",
        "issuer": "国家市场监督管理总局",
        "promulgated": "2025-12-30",
        "effective": "2026-04-15",
        "version": "国家市场监督管理总局令第121号",
        "url": "https://policy.mofcom.gov.cn/claw/clawContent.shtml?id=105600",
        "format": "html",
        "container_class": "article",
        "source_name": "商务部全球法规网",
    },
    {
        "doc_id": "saic-2015-73",
        "title": "侵害消费者权益行为处罚办法",
        "issuer": "国家市场监督管理总局",
        "promulgated": "2015-01-05",
        "effective": "2015-03-15",
        "version": "原国家工商行政管理总局令第73号公布，根据2020年10月23日国家市场监督管理总局令第31号修订",
        "url": "https://scjgj.beijing.gov.cn/cxfw/flfgcxfw/xfzqybhl/202006/t20200618_1928080.html",
        "format": "html",
        "container_id": "div_zhengwen",
        "source_name": "北京市市场监督管理局",
        "status_note": "中国政府网只有 2015 年原文，这里是 2020 年修订后的全文（第九条已按修订决定改写）",
    },
    {
        "doc_id": "cac-2021-livestream",
        "title": "网络直播营销管理办法（试行）",
        "issuer": "国家互联网信息办公室、公安部、商务部、文化和旅游部、国家税务总局、国家市场监督管理总局、国家广播电视总局",
        "promulgated": "2021-04-23",
        "effective": "2021-05-25",
        "version": "七部门联合发布（试行）",
        "level": "规范性文件",
        "url": "https://www.cac.gov.cn/2021-04/22/c_1620670982794847.htm",
        "format": "html",
        "container_class": "main-content",
        "source_name": "中央网信办（中国网信网）",
    },
    # 三包规定。微型计算机三包规定只找到地方监管局网站和第三方转载，前者从当前网络访问超时，暂未收录
    {
        "doc_id": "setc-1995-458",
        "title": "部分商品修理更换退货责任规定",
        "issuer": "国家经济贸易委员会等",
        "promulgated": "1995-08-25",
        "effective": "1995-08-25",
        "version": "国经贸〔1995〕458号",
        "level": "规范性文件",
        "url": "https://policy.mofcom.gov.cn/claw/clawContent.shtml?id=1613",
        "format": "html",
        "container_class": "article",
        "source_name": "商务部全球法规网",
        "status_note": "国家市场监督管理总局官网把它列在现行行政规范性文件中；附件商品目录在来源页面上只有标题，未收录",
    },
    {
        "doc_id": "aqsiq-2001-4",
        "title": "移动电话机商品修理更换退货责任规定",
        "issuer": "国家质量监督检验检疫总局、国家工商行政管理总局、信息产业部",
        "promulgated": "2001-09-17",
        "effective": "2001-11-15",
        "version": "国家质检总局、国家工商总局、信息产业部令第4号",
        "url": "https://scjgj.beijing.gov.cn/cxfw/flfgcxfw/xfzqybhl/202006/t20200618_1928076.html",
        "format": "html",
        "container_id": "div_zhengwen",
        "source_name": "北京市市场监督管理局",
        "status_note": "收录在北京市市场监督管理局的法规查询栏目，现行有效性未经数据库核验",
    },
    {
        "doc_id": "aqsiq-2002-24",
        "title": "家用视听商品修理更换退货责任规定",
        "issuer": "国家质量监督检验检疫总局、信息产业部",
        "promulgated": "2002-07-23",
        "effective": "2002-09-01",
        "version": "国家质检总局、信息产业部令第24号",
        "url": "https://scjgj.beijing.gov.cn/cxfw/flfgcxfw/xfzqybhl/202006/t20200618_1928081.html",
        "format": "html",
        "container_id": "div_zhengwen",
        "source_name": "北京市市场监督管理局",
        "status_note": "收录在北京市市场监督管理局的法规查询栏目，现行有效性未经数据库核验",
    },
]

# 来源页面附带的网站声明，不属于法规正文
BOILERPLATE_PREFIXES = ("免责声明",)
APPENDIX_RE = re.compile(r"^(附录|附件|附表)\s*([0-9０-９一二三四五六七八九十]*)\s*[：:]?\s*(.*)$")
BOX_DRAWING_RE = re.compile(r"[─-╿]+")

_CJK = r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]"


def fetch(url, path):
    if path.exists():
        return path.read_bytes()
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60, context=SSL_CONTEXT) as response:
        content = response.read()
    path.write_bytes(content)
    time.sleep(REQUEST_INTERVAL_SECONDS)
    return content


class _ContainerText(HTMLParser):
    """收集第一个匹配的 div 里的文本，按嵌套深度找到它真正的结束标签。

    按 id 匹配，或者按 class 匹配（class 里包含这个类名即可）。
    """

    def __init__(self, container_id=None, container_class=None):
        super().__init__(convert_charrefs=True)
        self.container_id = container_id
        self.container_class = container_class
        self.depth = 0
        self.found = False
        self.finished = False
        self.parts = []

    def _matches(self, attrs):
        attrs = dict(attrs)
        if self.container_id is not None:
            return attrs.get("id") == self.container_id
        return self.container_class in (attrs.get("class") or "").split()

    def handle_starttag(self, tag, attrs):
        if self.finished:
            return
        if not self.found:
            if tag == "div" and self._matches(attrs):
                self.found, self.depth = True, 1
            return
        if tag == "div":
            self.depth += 1
        if tag in ("p", "br", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if not self.found or self.finished:
            return
        if tag == "p":
            self.parts.append("\n")
        elif tag == "div":
            self.depth -= 1
            self.finished = self.depth == 0

    def handle_data(self, data):
        if self.found and not self.finished:
            self.parts.append(data)


def container_paragraphs(content, container_id=None, *, container_class=None):
    if (container_id is None) == (container_class is None):
        raise ValueError("pass exactly one of container_id or container_class")
    parser = _ContainerText(container_id, container_class)
    parser.feed(content.decode("utf-8", errors="ignore"))
    if not parser.found:
        selector = f"id={container_id!r}" if container_id else f"class={container_class!r}"
        raise ValueError(f"page layout changed: container {selector} not found")
    text = "".join(parser.parts)
    return [line.strip() for line in text.split("\n") if line.strip()]


def gov_html_paragraphs(content):
    """中国政府网的政策文件库和国务院公报页面，正文都在 UCAP-CONTENT 容器里。

    政策文件库页面另有一份移动版正文，放在这个容器外面，所以只取这个容器就不会重复。
    """
    return container_paragraphs(content, "UCAP-CONTENT")


def pdf_paragraphs(content):
    lines = []
    for page in PdfReader(BytesIO(content)).pages:
        lines.extend((page.extract_text() or "").split("\n"))
    return join_pdf_lines(lines)


def join_pdf_lines(raw_lines):
    """PDF 换行发生在固定宽度处，需要先把行拼回段落。

    只在条、章、项（“（一）”）开头处断段；同一条里的自然段在 PDF 中无法可靠
    区分，合并成一段对检索影响不大。
    """
    lines = []
    for line in raw_lines:
        line = line.strip()
        # pypdf 偶尔会在条号中间插入空格，例如“第二十 五条”
        line = re.sub(r"^第[一二三四五六七八九十百 ]+(条|章|节)", lambda m: m.group(0).replace(" ", ""), line)
        if line and not re.fullmatch(r"\d{1,3}", line):  # 去掉页码
            lines.append(line)
    paragraphs = []
    for line in lines:
        # 标题后面跟空白（“第二十四条  经营……”）；折行后以引用开头的行（“第二十五条规定……”）不算新的一条
        starts_block = re.match(r"^(第[一二三四五六七八九十百]+(条|章|节)[ \t\u3000]|（[一二三四五六七八九十]+）)", line)
        if starts_block or not paragraphs:
            paragraphs.append(line)
        else:
            paragraphs[-1] += line
    # pypdf 会在中文字符之间插入空格，去掉中文与中文之间、中文与数字之间的空白
    cleaned = []
    for paragraph in paragraphs:
        paragraph = re.sub(rf"(?<={_CJK})\s+(?={_CJK})", "", paragraph)
        paragraph = re.sub(rf"(?<={_CJK})\s+(?=\d)|(?<=\d)\s+(?={_CJK})", "", paragraph)
        # 上面会把“第七章  监督管理”压成“第七章监督管理”，标题后要留一个空格，章节标题才能被识别
        paragraph = re.sub(r"^(第[一二三四五六七八九十百]+(?:条|章|节))(?=\S)", r"\1 ", paragraph)
        cleaned.append(paragraph)
    return cleaned


def to_halfwidth(text):
    """全角数字和字母转成半角（“２００１年”→“2001年”），中文标点保持不变。"""
    text = re.sub(r"[０-９Ａ-Ｚａ-ｚ]", lambda m: chr(ord(m.group(0)) - 0xFEE0), text)
    return re.sub(r"(?<=\d)．(?=\d)", ".", text).replace("％", "%")


def split_appendices(articles):
    """把最后一条后面的附录、附件拆成单独的条目，表格框线换成空格。

    只有标题、没有内容的附件（来源页面没有附上正文）直接丢掉。
    """
    if not articles:
        return articles, []
    lines = articles[-1]["text"].split("\n")
    start = next((index for index, line in enumerate(lines) if APPENDIX_RE.match(line)), None)
    if start is None:
        return articles, []
    articles[-1]["text"] = "\n".join(lines[:start]).strip()
    appendices = []
    for line in lines[start:]:
        match = APPENDIX_RE.match(line)
        if match:
            title = match.group(3).strip()
            appendices.append({"label": f"{match.group(1)}{match.group(2)}", "title": title, "lines": []})
            continue
        cleaned = re.sub(r"\s+", " ", BOX_DRAWING_RE.sub(" ", line)).strip()
        if cleaned:
            appendices[-1]["lines"].append(cleaned)
    records, seen = [], set()
    for appendix in appendices:
        if not appendix["lines"]:
            continue
        label = appendix["label"]
        while label in seen:  # 来源里没写编号的附件可能重名
            label += "'"
        seen.add(label)
        text = "\n".join(([appendix["title"]] if appendix["title"] else []) + appendix["lines"])
        records.append({"article_no": label, "chapter": "附件", "text": text})
    return articles, records


def source_paragraphs(source, content):
    if source["format"] == "pdf":
        return pdf_paragraphs(content)
    if source["format"] == "gov_html":
        paragraphs = gov_html_paragraphs(content)
    else:
        paragraphs = container_paragraphs(
            content, source.get("container_id"), container_class=source.get("container_class")
        )
    return [p for p in paragraphs if not p.startswith(BOILERPLATE_PREFIXES)]


def main():
    force_ipv4()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    records, report = [], []
    for source in SOURCES:
        suffix = ".pdf" if source["format"] == "pdf" else ".html"
        content = fetch(source["url"], RAW_DIR / f"{source['doc_id']}{suffix}")
        paragraphs = [to_halfwidth(p) for p in source_paragraphs(source, content)]
        articles, appendices = split_appendices(split_articles(paragraphs))
        meta = {
            "doc_id": source["doc_id"],
            "title": source["title"],
            "level": source.get("level", "部门规章"),
            "issuer": source["issuer"],
            "promulgated": source["promulgated"],
            "effective": source["effective"],
            "status": "有效",
            "version": source["version"],
            "source": source.get("source_name") or ("中国政府网" if "gov.cn" in source["url"] else "交通运输部"),
            "source_file": source["url"],
        }
        if source.get("status_note"):
            meta["status_note"] = source["status_note"]
        for article in articles + appendices:
            records.append({**meta, **article, "chunk_id": f"{source['doc_id']}#{article['article_no']}"})
        gaps = sequence_gaps(articles)
        note = f"，条号不连续: {gaps[:5]}" if gaps else ""
        extra = f" + {len(appendices)} 个附件" if appendices else ""
        report.append(f"{source['title']}（{source['version']}）: {len(articles)} 条{extra}{note}")
    with OUT_PATH.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    print("\n".join(report))
    print(f"共 {len(records)} 条 → {OUT_PATH}")
    return 0 if records else 1


if __name__ == "__main__":
    sys.exit(main())
