"""画架构图：生成 docs/architecture.{en,zh}.svg（矢量，任意放大不糊）。

只用标准库。改文字改 LABELS，改布局改下面的坐标；重新生成：uv run python docs/make_architecture.py
转成 PNG（macOS 自带工具）：qlmanage -t -s 4800 -o docs docs/architecture.en.svg，再用 sips 裁掉四周留白。
"""

from pathlib import Path

W, H = 2400, 1380
FONT = "-apple-system, 'Helvetica Neue', 'PingFang SC', 'Hiragino Sans GB', 'Noto Sans CJK SC', Arial, sans-serif"

LABELS = {
    "en": {
        "title": "E-commerce Agent System: trained shopping agent + harness-guarded after-sales agent",
        "shopping": "Shopping",
        "shop_agent": ["Shopping Agent", "Qwen3.5-2B, SFT + GRPO", "in ShopSimulator: search, compare, choose options, buy"],
        "gateway": ["Order Gateway", "proxies ShopSimulator, records each purchase as an order", "planned: Buy Now pauses for a confirmation card"],
        "buy": "Buy Now",
        "to_mall": "order",
        "mall": ["Simulated Mall (SQLite)", "23,421 products · 4,666 users", "orders · 9 logistics scenarios", "policy engine (every verdict cites a rule)",
                 "after-sales flow · manual review", "merchant rejection · platform intervention", "work orders · audit log", "customer memory"],
        "aftersales": "After-sales",
        "customers": ["Customers", "scripted / noisy /", "LLM-played / real"],
        "cs_agent": ["After-sales Agent", "any model: DeepSeek, GLM,", "Qwen3.5-2B, Claude bridge", "prompt: rules + customer profile"],
        "tool_calls": ["tool", "calls"],
        "harness": "Harness — the model picks the tool, the gates decide whether it may",
        "harness_note": "every write passes all gates in order",
        "gates": [["Arguments", "schema check"], ["Identity", "own orders only"], ["Policy", "engine verdict"], ["Evidence", "named defect"],
                  ["Authority", "& risk tiers"], ["Confirmation", "customer card"], ["Execute", "savepoint · audit"]],
        "read": "read (own orders only)",
        "write": "write",
        "legal": ["Legal Advisor (sub-agent)", "searches the law itself,", "returns verdict + article numbers"],
        "reviewer": ["Evidence Reviewer (sub-agent)", "is the defect description", "what the customer actually said?"],
        "kb": ["Knowledge Base", "27 laws + 40 platform rules = 2,590 articles", "BM25 + embeddings, RRF fusion"],
        "consult": "consult_legal",
        "blocked": "blocked call → audit log, counts as agent failure",
        "eval": "Evaluation & Feedback",
        "scenarios": ["Scenario Generation", "30 kinds × 8 = 237 scenarios", "ground truth = policy engine's verdict", "at the moment the customer asks"],
        "evaluation": ["Evaluation", "16 hard checks + process checks", "calibrated by the reference solution (ceiling)", "and 4 cheating baselines (floor)"],
        "reports": ["Reports", "pass rate · pass^k · money lost · leaks", "blocked calls · regress (CI)"],
        "feedback": ["Data Feedback", "harvest: dialogues → new scenarios / regression set", "export-sft: passing dialogues → SFT samples"],
        "lines": "scripts + confirm rules",
        "ground_truth": "ground truth",
        "dialogues": "dialogues",
        "records": "records",
        "harvest": "harvest → new scenarios",
        "sft": "export-sft → SFT samples for the 2B",
    },
    "zh": {
        "title": "电商 agent 系统：训练出来的购物 agent + 关卡守着的售后 agent",
        "shopping": "购物",
        "shop_agent": ["购物 agent", "Qwen3.5-2B，SFT + GRPO", "在 ShopSimulator 里搜索、比较、选规格、点 Buy Now"],
        "gateway": ["订单网关", "代理 ShopSimulator，把每次购买记成订单", "规划：Buy Now 先停下来弹确认卡片"],
        "buy": "Buy Now",
        "to_mall": "订单",
        "mall": ["模拟商城（SQLite）", "23,421 件商品 · 4,666 个用户", "订单 · 9 种物流情形", "政策引擎（每个结论带规则编号）",
                 "售后流程 · 人工审核", "商家拒绝 · 平台介入", "工单 · 审计日志", "顾客记忆"],
        "aftersales": "售后",
        "customers": ["顾客", "脚本 / 带错别字 /", "大模型扮演 / 真实用户"],
        "cs_agent": ["售后 agent", "任意模型：DeepSeek、GLM、", "Qwen3.5-2B、Claude 文件桥", "系统提示 = 规则 + 顾客档案"],
        "tool_calls": ["工具", "调用"],
        "harness": "Harness：模型只决定调什么工具，关卡决定能不能办",
        "harness_note": "每次写操作按顺序过全部关卡",
        "gates": [["参数校验", "按 schema 查"], ["身份隔离", "只看本人订单"], ["政策判断", "引擎给结论"], ["凭证检查", "要具体问题"],
                  ["办理权限", "与风控档"], ["确认卡片", "顾客确认"], ["执行", "保存点 · 审计"]],
        "read": "只读查询（只限本人订单）",
        "write": "写操作",
        "legal": ["法规顾问（子 agent）", "自己检索法规，", "只交回结论和条号"],
        "reviewer": ["凭证审核员（子 agent）", "问题描述是不是", "顾客自己说的？"],
        "kb": ["知识库", "27 部法规 + 40 条平台规则 = 2,590 条", "BM25 + 向量，RRF 融合"],
        "consult": "consult_legal",
        "blocked": "被拦下的调用 → 审计日志，算 agent 失败",
        "eval": "评测与数据回流",
        "scenarios": ["场景生成", "30 种情形 × 8 = 237 段", "标准答案 = 政策引擎在", "顾客来问那一刻的判断"],
        "evaluation": ["评分", "16 项硬性要求 + 过程检查", "用参考解法（天花板）", "和 4 个作弊基线（地板）校准"],
        "reports": ["报告", "通过率 · pass^k · 资损 · 泄露", "被拦下的调用 · regress 回归（CI）"],
        "feedback": ["数据回流", "harvest：对话记录 → 新场景 / 回归集", "export-sft：通过的对话 → SFT 样本"],
        "lines": "台词 + 确认规则",
        "ground_truth": "标准答案",
        "dialogues": "对话记录",
        "records": "记录",
        "harvest": "harvest → 新场景",
        "sft": "export-sft → 给 2B 的 SFT 样本",
    },
}

C = {
    "blue": "#2F6FBA", "blue_fill": "#E9F1FB", "blue_box": "#D7E6F7",
    "orange": "#D9742A", "orange_fill": "#FDEFE2", "harness_fill": "#FAE1C9",
    "green": "#3E9A4F", "green_fill": "#E7F4E8",
    "purple": "#7E57C2", "purple_fill": "#EEE7F8",
    "grey": "#5B6678", "grey_fill": "#EEF1F5", "ink": "#1F2933", "red": "#C8352E", "white": "#FFFFFF",
}


class SVG:
    def __init__(self):
        self.parts = []

    def rect(self, x, y, w, h, fill, stroke, r=18, sw=3, dash=None):
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{extra}/>')

    def text(self, x, y, s, size=22, weight="normal", fill=C["ink"], anchor="middle", halo=False):
        s = s.replace("&", "&amp;").replace("<", "&lt;")
        stroke = f' stroke="{C["white"]}" stroke-width="{size * 0.45}" paint-order="stroke" stroke-linejoin="round"' if halo else ""
        self.parts.append(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}"{stroke}>{s}</text>')

    def box(self, x, y, w, h, lines, fill, stroke, title_size=26, body_size=20, sw=3):
        self.rect(x, y, w, h, fill, stroke, sw=sw)
        gap = body_size + 8
        total = title_size + 10 + gap * (len(lines) - 1)
        ty = y + (h - total) / 2 + title_size - 4
        self.text(x + w / 2, ty, lines[0], title_size, "bold")
        for i, line in enumerate(lines[1:], 1):
            self.text(x + w / 2, ty + 10 + gap * i, line, body_size, fill="#3A4452")

    def arrow(self, points, color, label=None, dash=None, both=False, label_dy=-12, label_anchor="middle", label_at=None, width=4):
        d = " ".join(f"{'M' if i == 0 else 'L'} {x} {y}" for i, (x, y) in enumerate(points))
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        start = f' marker-start="url(#head-{color}-r)"' if both else ""
        self.parts.append(f'<path d="{d}" fill="none" stroke="{C[color]}" stroke-width="{width}"{extra} marker-end="url(#head-{color})"{start}/>')
        if label:
            if label_at is None:
                (x1, y1), (x2, y2) = points[0], points[1]
                label_at = ((x1 + x2) / 2, (y1 + y2) / 2)
            self.text(label_at[0], label_at[1] + label_dy, label, 20, "bold", C[color], label_anchor, halo=True)

    def render(self):
        heads = "".join(
            f'<marker id="head-{name}" markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto" markerUnits="userSpaceOnUse">'
            f'<path d="M 0 0 L 12 6 L 0 12 z" fill="{C[name]}"/></marker>'
            f'<marker id="head-{name}-r" markerWidth="12" markerHeight="12" refX="2" refY="6" orient="auto" markerUnits="userSpaceOnUse">'
            f'<path d="M 12 0 L 0 6 L 12 12 z" fill="{C[name]}"/></marker>'
            for name in ("blue", "orange", "green", "purple", "grey", "red")
        )
        body = "\n".join(self.parts)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 {W} {H}" font-family="{FONT}">'
                f'<defs>{heads}</defs><rect width="{W}" height="{H}" fill="{C["white"]}"/>\n{body}\n</svg>\n')


def draw(lang):
    t = LABELS[lang]
    s = SVG()
    s.text(W / 2, 64, t["title"], 44, "bold")

    # ---- 购物带
    s.rect(60, 100, 1680, 215, C["blue_fill"], C["blue"])
    s.text(104, 142, t["shopping"], 34, "bold", C["blue"], "start")
    s.box(140, 160, 600, 130, t["shop_agent"], C["white"], C["blue"], 26, 20)
    s.box(960, 160, 620, 130, t["gateway"], C["white"], C["blue"], 26, 20)
    s.arrow([(740, 225), (960, 225)], "blue", t["buy"])
    s.arrow([(1580, 225), (1800, 225)], "blue", t["to_mall"])

    # ---- 模拟商城
    s.box(1800, 100, 540, 790, t["mall"], C["green_fill"], C["green"], 30, 22, sw=4)

    # ---- 售后带
    s.rect(60, 340, 1680, 660, C["orange_fill"], C["orange"])
    s.text(104, 382, t["aftersales"], 34, "bold", C["orange"], "start")
    s.box(120, 520, 200, 130, t["customers"], C["white"], C["orange"], 26, 19)
    s.box(400, 505, 340, 160, t["cs_agent"], C["white"], C["orange"], 26, 19)
    s.arrow([(320, 585), (400, 585)], "orange", both=True)
    s.arrow([(740, 585), (780, 585)], "orange")
    s.text(760, 566, t["tool_calls"][0], 18, "bold", C["orange"], halo=True)
    s.text(760, 612, t["tool_calls"][1], 18, "bold", C["orange"], halo=True)

    # harness 和七道关
    s.rect(780, 420, 970, 300, C["harness_fill"], C["orange"], sw=4)
    s.text(800, 458, t["harness"], 24, "bold", C["orange"], "start")
    gx, gw, gap = 792, 126, 12
    centers = []
    for i, (name, sub) in enumerate(t["gates"]):
        x = gx + i * (gw + gap)
        s.rect(x, 500, gw, 150, C["white"], C["orange"], r=12)
        cx = x + gw / 2
        centers.append(cx)
        s.parts.append(f'<circle cx="{x + 20}" cy="{520}" r="14" fill="{C["orange"]}"/>')
        s.text(x + 20, 526, str(i + 1), 17, "bold", C["white"])
        s.text(cx, 590, name, 20, "bold")
        s.text(cx, 620, sub, 15, fill="#3A4452")
        if i:
            s.arrow([(x - gap, 575), (x, 575)], "orange", width=3)
    s.text(1265, 695, t["harness_note"], 19, "normal", C["orange"])

    # 读 / 写 到商城
    s.arrow([(centers[1], 420), (centers[1], 390), (1800, 390)], "green", t["read"], label_at=(1500, 390), label_dy=-12)  # 从 harness 顶边出发，不压标题
    s.arrow([(1750, 575), (1800, 575)], "green", t["write"], label_dy=-14)

    # 子 agent 和知识库
    s.box(820, 770, 330, 100, t["legal"], C["purple_fill"], C["purple"], 22, 18)
    s.box(1230, 770, 330, 100, t["reviewer"], C["purple_fill"], C["purple"], 22, 18)
    s.box(820, 895, 500, 90, t["kb"], C["purple_fill"], C["purple"], 22, 18)
    s.arrow([(centers[0], 650), (centers[0], 770)], "purple", t["consult"], label_at=(centers[0] + 12, 710), label_anchor="start", label_dy=0)
    s.arrow([(centers[3], 650), (centers[3], 770)], "purple")
    s.arrow([(985, 870), (985, 895)], "purple")

    # 被拦下的调用 → 报告（红色虚线）
    s.arrow([(1640, 720), (1640, 1015), (1430, 1015), (1430, 1090)], "red", t["blocked"], dash="12 9", label_at=(1655, 960), label_anchor="start", label_dy=0)

    # ---- 评测带
    s.rect(60, 1030, 2280, 300, C["grey_fill"], C["grey"])
    s.text(104, 1072, t["eval"], 34, "bold", C["grey"], "start")
    s.box(120, 1095, 460, 170, t["scenarios"], C["white"], C["grey"], 26, 20)
    s.box(660, 1095, 520, 170, t["evaluation"], C["white"], C["grey"], 26, 20)
    s.box(1260, 1095, 400, 170, t["reports"], C["white"], C["grey"], 26, 20)
    s.box(1740, 1095, 560, 170, t["feedback"], C["white"], C["grey"], 26, 20)
    s.arrow([(580, 1180), (660, 1180)], "grey", t["ground_truth"], label_dy=-14)
    s.arrow([(1180, 1180), (1260, 1180)], "grey")
    s.arrow([(1660, 1180), (1740, 1180)], "grey", t["records"], label_dy=-14)

    # 场景 → 顾客；对话记录 → 评分
    s.arrow([(220, 1095), (220, 650)], "grey", t["lines"], label_at=(232, 880), label_anchor="start", label_dy=0)
    s.arrow([(570, 665), (570, 1015), (920, 1015), (920, 1095)], "grey", t["dialogues"], label_at=(585, 850), label_anchor="start", label_dy=0)

    # 回流：harvest 回场景；export-sft 回购物 agent（蓝色虚线）
    s.arrow([(1900, 1265), (1900, 1288), (350, 1288), (350, 1265)], "blue", t["harvest"], dash="12 9", label_at=(1120, 1288), label_dy=7)
    s.arrow([(2100, 1265), (2100, 1313), (78, 1313), (78, 225), (140, 225)], "blue", t["sft"], dash="12 9", label_at=(700, 1313), label_dy=7)

    return s.render()


if __name__ == "__main__":
    here = Path(__file__).parent
    for lang in ("en", "zh"):
        out = here / f"architecture.{lang}.svg"
        out.write_text(draw(lang), encoding="utf-8")
        print("wrote", out)
