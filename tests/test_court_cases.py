from crawl_court_cases import dispute_type, extract_citations, mentioned_laws, split_cases

LAW_NAMES = {
    "中华人民共和国消费者权益保护法": "中华人民共和国消费者权益保护法",
    "消费者权益保护法": "中华人民共和国消费者权益保护法",
    "中华人民共和国消费者权益保护法实施条例": "中华人民共和国消费者权益保护法实施条例",
    "消费者权益保护法实施条例": "中华人民共和国消费者权益保护法实施条例",
    "中华人民共和国民法典": "中华人民共和国民法典",
    "民法典": "中华人民共和国民法典",
}


def test_split_cases_handles_numbered_and_wrapped_titles():
    paragraphs = [
        "目录",
        "案例1",
        "：限时免单条款约定条件成就，经营者应当依约免单——张某与某购物平台信息网络买卖合同纠纷案",
        "案例1",
        "提供酒店在线预订服务方应当履行协助退订",  # 要旨折成了两行
        "等合同附随义务",
        "——熊某诉某旅行社网络服务合同纠纷案",
        "【基本案情】",
        "熊某通过平台预订酒店。",
        "【裁判结果】",
        "法院判决退还房费。",
        "【典型意义】",
        "本案明确了附随义务。",
        "案例2",
        "经营者不合理排除“七日无理由退货”权利，不应支持——胡某与韩某信息网络买卖合同纠纷案",
        "【基本案情】胡某购买手机后申请退货。",  # 小标题和正文在同一行
        "【裁判结果】",
        "支持退货。",
        "【典型意义】",
        "第一段。",
        "第二段。",
    ]
    cases = split_cases(paragraphs)
    assert [case["case_name"] for case in cases] == [
        "熊某诉某旅行社网络服务合同纠纷案",
        "胡某与韩某信息网络买卖合同纠纷案",
    ]
    assert cases[0]["holding"] == "提供酒店在线预订服务方应当履行协助退订等合同附随义务"
    assert cases[0]["significance"] == "本案明确了附随义务。"
    assert cases[1]["facts"] == "胡某购买手机后申请退货。"
    assert cases[1]["significance"] == "第一段。\n第二段。"


def test_split_cases_handles_unnumbered_titles():
    paragraphs = [
        "今天最高人民法院发布了典型案例。",
        "对于网络店铺客服的行为店铺应当负责",
        "——李某诉某书店信息网络买卖合同纠纷案",
        "基本案情",
        "李某在网店购书。",
        "裁判结果",
        "书店退款。",
        "典型意义",
        "客服行为对店铺发生效力。",
    ]
    (case,) = split_cases(paragraphs)
    assert case["holding"] == "对于网络店铺客服的行为店铺应当负责"
    assert case["judgment"] == "书店退款。"


def test_extract_citations_with_and_without_title_marks():
    text = (
        "依据《中华人民共和国消费者权益保护法》第二十五条、第五十五条第一款，"
        "并参照消费者权益保护法实施条例第十九条和民法典第四百九十六条的规定。"
        "《消费者权益保护法》的立法目的……"
    )
    assert extract_citations(text, LAW_NAMES) == [
        ("中华人民共和国消费者权益保护法", "第二十五条"),
        ("中华人民共和国消费者权益保护法", "第五十五条"),
        ("消费者权益保护法实施条例", "第十九条"),
        ("民法典", "第四百九十六条"),
    ]


def test_dispute_type_prefers_the_most_specific_cause():
    assert dispute_type("张某与周某、某购物平台信息网络买卖合同纠纷案") == "信息网络买卖合同纠纷"
    assert dispute_type("郑某诉某公司网络侵权责任纠纷案") == "网络侵权责任纠纷"
    assert dispute_type("某某离婚纠纷案") is None


def test_mentioned_laws_does_not_double_count_shorter_names():
    text = "应当适用消费者权益保护法实施条例的规定"
    assert mentioned_laws(text, LAW_NAMES) == ["中华人民共和国消费者权益保护法实施条例"]
