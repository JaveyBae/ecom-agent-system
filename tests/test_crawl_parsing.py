from crawl_flk import chinese_number, sequence_gaps, split_articles
from crawl_rules import gov_html_paragraphs, join_pdf_lines


def test_chinese_number():
    cases = {"一": 1, "十": 10, "十五": 15, "二十": 20, "一百零五": 105, "一百四十八": 148, "一千二百六十": 1260}
    for text, number in cases.items():
        assert chinese_number(text) == number


def test_split_articles_tracks_chapters_and_paragraphs():
    paragraphs = [
        "中华人民共和国某某法",
        "目录",
        "第一章 总则",  # 目录里的章名会被后面真正的章名覆盖
        "第一章 总则",
        "第一条 为了保护消费者的合法权益，制定本法。",
        "第二条　消费者为生活消费需要购买商品，适用本法。",
        "本条第二款。",
        "第二章 经营者的义务",
        "第一节 一般规定",
        "第三条 经营者应当依法提供商品。",
        "第三章 附则",
        "第四条 本法自公布之日起施行。",
    ]
    articles = split_articles(paragraphs)
    assert [article["article_no"] for article in articles] == ["第一条", "第二条", "第三条", "第四条"]
    assert articles[1]["text"] == "消费者为生活消费需要购买商品，适用本法。\n本条第二款。"
    assert articles[0]["chapter"] == "第一章 总则"
    assert articles[2]["chapter"] == "第二章 经营者的义务 / 第一节 一般规定"
    # 进入新的一章后，上一章的节不能残留
    assert articles[3]["chapter"] == "第三章 附则"
    assert sequence_gaps(articles) == []


def test_sequence_gaps_reports_missing_article():
    articles = [{"article_no": number} for number in ("第一条", "第二条", "第四条")]
    assert sequence_gaps(articles) == [(3, 4)]


def test_join_pdf_lines_repairs_wrapped_text():
    raw_lines = [
        "第二十四条  经营快递业务的企业提供快递服务，应当与寄件",
        "人订立服务合同。 ",
        " 7 ",  # 页码
        "第二十 五条  经营快递业务的企业应当保证用户能够查知",  # pypdf 在条号中插入了空格
        "下列内容，违反本办法",
        "第二十六条规定的，依法处理： ",  # 折行后以引用开头，不是新的一条
        "（一）快递服务承诺事项； ",
        "第七章  监督管理 ",
        "第四十一条  邮政管理部门依法履行 3000 元 罚款职责。",
    ]
    paragraphs = join_pdf_lines(raw_lines)
    assert paragraphs[0] == "第二十四条 经营快递业务的企业提供快递服务，应当与寄件人订立服务合同。"
    assert paragraphs[1] == "第二十五条 经营快递业务的企业应当保证用户能够查知下列内容，违反本办法第二十六条规定的，依法处理："
    assert paragraphs[2] == "（一）快递服务承诺事项；"
    assert paragraphs[3] == "第七章 监督管理"
    assert "3000元罚款" in paragraphs[4]
    articles = split_articles(paragraphs)
    assert [article["article_no"] for article in articles] == ["第二十四条", "第二十五条", "第四十一条"]
    assert articles[2]["chapter"] == "第七章 监督管理"


def test_gov_html_paragraphs_reads_desktop_copy_only():
    page = (
        '<div class="pages_content mhide" id="UCAP-CONTENT">'
        "<p>某某办法</p><p>第一条&nbsp; 为了规范网络交易，制定本办法。</p>"
        "</div>"
        '<div class="pages_content pages_contentm pchide"><p>第一条 重复的移动版正文</p></div>'
    ).encode("utf-8")
    # 容器的开始和结束标签都不能有残片混进正文
    assert gov_html_paragraphs(page) == ["某某办法", "第一条\xa0 为了规范网络交易，制定本办法。"]


def test_gov_html_paragraphs_handles_gazette_layout_with_nested_divs():
    # 国务院公报页面没有移动版副本，容器里还嵌套着 div，容器后面是页脚
    page = (
        '<div class="content"><div class="pages_content" id="UCAP-CONTENT">'
        "<p>某某办法</p><div><p>第一章　总　　则</p></div>"
        "<p>第一条　为了规范平台规则，制定本办法。</p><br/>第二条　本办法自公布之日起施行。"
        "</div></div><div class=\"footer\"><p>版权所有</p></div>"
    ).encode("utf-8")
    paragraphs = gov_html_paragraphs(page)
    assert paragraphs[-1] == "第二条　本办法自公布之日起施行。"
    articles = split_articles(paragraphs)
    assert [article["article_no"] for article in articles] == ["第一条", "第二条"]
    assert articles[0]["chapter"] == "第一章 总 则"


def test_container_paragraphs_matches_class_and_drops_boilerplate():
    from crawl_rules import source_paragraphs

    page = (
        '<div class="wrapper"><div class="main-content x">'
        "<p>第一条 为了加强直播营销管理，制定本办法。</p><div><p>第二条 本办法自公布之日起施行。</p></div>"
        "<p>免责声明：本网站提供的法规仅供参考。</p></div></div><div class=\"footer\">网站地图</div>"
    ).encode("utf-8")
    source = {"format": "html", "container_class": "main-content"}
    assert source_paragraphs(source, page) == [
        "第一条 为了加强直播营销管理，制定本办法。",
        "第二条 本办法自公布之日起施行。",
    ]


def test_to_halfwidth_keeps_chinese_punctuation():
    from crawl_rules import to_halfwidth

    assert to_halfwidth("本规定从２００１年１１月１５日起实行，折旧率０．５％（年）。") == "本规定从2001年11月15日起实行，折旧率0.5%（年）。"


def test_split_appendices_turns_tables_into_text():
    from crawl_rules import split_appendices

    articles = [
        {"article_no": "第一条", "chapter": "", "text": "正文。"},
        {
            "article_no": "第二条",
            "chapter": "",
            "text": "\n".join(
                [
                    "本规定自2001年11月15日起实行。",
                    "附录1：实施三包的商品目录",  # 来源页面只有标题、没有内容的附件要丢掉
                    "附录3：商品性能故障表",
                    "┌────┬────┐",
                    "│名称│性能故障│",
                    "│主机│无法开机│",
                    "└────┴────┘",
                ]
            ),
        },
    ]
    articles, appendices = split_appendices(articles)
    assert articles[-1]["text"] == "本规定自2001年11月15日起实行。"
    assert appendices == [{"article_no": "附录3", "chapter": "附件", "text": "商品性能故障表\n名称 性能故障\n主机 无法开机"}]
