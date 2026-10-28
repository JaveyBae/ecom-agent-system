# 命令参考

四个命令：`ecom-shop`（模拟商城）、`ecom-kb`（知识库）、`ecom-agent`（agent、评测、回流）、`ecom-console`（控制台）。都用 `uv run` 启动，`--help` 有完整参数。

## 准备

```bash
uv sync
uv run pytest -q

# 模拟商城：演示世界（种子 42，3,000 单）和评测世界（种子 7，10,000 单）
uv run ecom-shop build --products <ShopSimulator 的 fine_items_eval_train_all.json.gz>     # 也可以设环境变量 SHOPSIM_PRODUCTS
uv run ecom-shop --db shop/eval.db build --products <同上> --orders 10000 --seed 7

# 知识库：抓取法规、校验平台规则、建索引
uv run python knowledge/crawl_flk.py
uv run python knowledge/crawl_rules.py
uv run python knowledge/build_platform_rules.py
uv run ecom-kb build
uv run ecom-kb search "耳机拆封试了一下，还能七天无理由退货吗" -k 3
```

查看商城数据：`ecom-shop stats | orders U000123 | order SO… | track SO… | check SO… 退货退款 七天无理由 | ticket AS…`，都可以加 `--now "+5d"` 换一个时刻看。

## 评测套件

```bash
# 从评测世界生成场景：30 种情形各 8 段，库复制一份到运行目录
uv run ecom-agent suite --world shop/eval.db --dir runs/suite --per-kind 8        # --kinds 只生成几种；--force 覆盖

# 参考解法和四个基线：检查场景和评分本身
uv run ecom-agent aftersales --db runs/suite/shop.db --scenarios runs/suite/scenarios.json --reference --out runs/suite/aftersales-reference.jsonl
uv run ecom-agent aftersales --db runs/suite/shop.db --scenarios runs/suite/scenarios.json --baseline comply --out runs/suite/aftersales-comply.jsonl
#   --baseline silent | transfer | comply | comply-fabricate
```

`aftersales` 默认在库的副本上跑（每次试验一份，放在输出文件旁边，如 `aftersales-x.t1.db`），`--db` 指的库不会被改；`--in-place` 直接改。一段对话出错记为 error，不影响后面的场景。

### 接模型

```bash
# OpenAI 兼容接口：DeepSeek、vLLM、百炼等。接口超时、网络错误、429 和 5xx 自动重试
uv run ecom-agent aftersales --db runs/suite/shop.db --scenarios runs/suite/scenarios.json \
    --openai deepseek-flash --base-url https://api.deepseek.com --api-key-env DEEPSEEK_API_KEY \
    --extra-body '{"thinking": {"type": "disabled"}}' --temperature 0 --out runs/suite/aftersales-dsflash.jsonl

# 并发：n 个进程各跑一份，report 合并各份记录
for i in 1 2 3 4 5 6 7 8; do
  uv run ecom-agent aftersales ... --shard $i/8 --out runs/suite/aftersales-dsflash.s$i.jsonl &
done; wait
uv run ecom-agent report runs/suite/aftersales-dsflash.s*.jsonl

# 本机 transformers（Apple 芯片用 MPS）：一段对话约 1 分钟，不能并发
uv run ecom-agent aftersales ... --hf Qwen/Qwen3.5-2B --temperature 0.7 --seed 1 --out runs/suite/aftersales-qwen3.5-2b.jsonl
#   --thinking 开深度思考；--max-new-tokens 每次生成的上限

# 文件桥：Claude Code 或人扮演模型（见下面“扮演模型”）
uv run ecom-agent aftersales ... --bridge runs/suite/bridge --out runs/suite/aftersales-bridge.jsonl
```

常用选项：

| 选项 | 作用 |
|---|---|
| `--trials 3` | 每个场景跑 3 次（每次从同一份库副本开始），报告里给 pass^3（3 次全过的场景比例）、pass@3（至少过 1 次）和每次试验各过了多少 |
| `--only 对抗,记忆`、`--only rejected,escalate_amount` 或 `--only SO260919004030-stalled` | 只跑这些类别、情形或场景编号，可以混着写；接口出错的几段重跑时给编号，另起一个 `--out`，再把记录并进原文件 |
| `--retries 8 --backoff 2` | 接口超时、429、5xx 时重试几次、第一次等几秒（之后翻倍，最多 60 秒）；默认 2 次、1 秒。限流严的免费模型要串行并调到 8 和 2 |
| `--customer scripted \| noisy \| llm` | 脚本顾客；带错别字、不打标点、带表情的脚本顾客（种子固定）；大模型扮演的顾客（再给 `--customer-openai`、`--customer-base-url`、`--customer-api-key-env`，或 `--customer-bridge`） |
| `--gates-off ownership,evidence,tier,risk,confirm,dedupe` 或 `all` | 关掉几道关做对照：身份隔离、凭证、办理权限、风控、确认卡片、去重。参数检查、商城的业务规则和审计一直在 |
| `--no-kb` | 不接知识库：没有 `search_policy`，提示词改成凭所知说明法规 |
| `--no-memory` | 系统提示里不放顾客档案 |
| `--legacy-harness` | 旧版提示词第 3、4 条，判断结果不带办理权限档（复现 2026-10-26 之前的 harness） |
| `--shopping` | 所有场景都给购物工具；默认只有混合情形开 |
| `--agents legal,evidence` | 接法规顾问、凭证审核员；`--sub-hf MODEL` / `--sub-openai MODEL` 给子 agent 换模型；`--agents-stub approve \| reject` 用不带模型的桩画上下界 |
| `--label`、`--append` | 会话编号前缀；输出文件已存在时接着写 |

### 汇总、重新打分、回归

```bash
uv run ecom-agent report runs/suite/aftersales-dsflash.jsonl                 # 通过率、pass^k、pass@k、每次试验、按类别和情形、资损、泄露、转人工、被拦下的调用、用量、耗时
uv run ecom-agent regrade runs/suite/aftersales-old.jsonl --out runs/suite/aftersales-old.regraded.jsonl   # 评分规则改了，不重跑模型
#   --scenarios 新的场景文件：同编号场景的标准答案换成新的再打分
uv run ecom-agent regress runs/suite/aftersales-v1.jsonl runs/suite/aftersales-v2.jsonl                    # 逐场景对比
#   有回归、新结果缺场景、有出错的对话、试验次数变少时退出码为 1；故意只跑子集时加 --allow-missing
```

报告里的 `blocked_calls` 按原因统计被拦下的调用（越权、政策不允许、业务错误、凭证不足、需转人工、顾客未确认、参数错误、工具错误、转人工后未执行），`hard_failures` 按硬性要求统计失败次数；重复请求不算拦下，返回第一次的结果，只进审计日志。

### 子 agent 的对比实验

```bash
# 凭证审核员拦不拦编出来的凭证：用会编凭证的基线，在它会提交凭证的两种情形上跑
uv run ecom-agent aftersales ... --baseline comply-fabricate --agents evidence --sub-hf Qwen/Qwen3.5-2B \
    --only rejected,rt_fake_quality --out runs/suite/aftersales-comply-fabricate.evidence-qwen.jsonl
uv run ecom-agent aftersales ... --baseline comply-fabricate --agents evidence --agents-stub reject --only rejected,rt_fake_quality --out ...   # 下界
uv run ecom-agent aftersales ... --reference --agents evidence --sub-hf Qwen/Qwen3.5-2B --only rejected --out ...                            # 真凭证会不会被误拦
# 法规顾问：在顾客追问法律依据的情形上比单 agent 和加顾问
uv run ecom-agent aftersales ... --hf Qwen/Qwen3.5-2B --agents legal --only return_denied,legal_freight --out ...
```

## 数据回流

```bash
# 对话记录 → 新场景：标准答案照抄，台词取顾客在对话里真正说过的话（大模型扮演的顾客、带错别字的顾客、线上对话都从这里进评测集）
uv run ecom-agent harvest runs/suite/aftersales-dsflash.llmcust.jsonl --out runs/suite/scenarios-harvest.json --suffix llm
# 回归集：只取没过的对话，保留原台词；改了提示词或 harness 之后只重跑这些
uv run ecom-agent harvest runs/suite/aftersales-dsflash-full.jsonl --failed-only --lines original --out runs/suite/scenarios-hard.json
#   --merge-into runs/suite/scenarios.json 并进现有场景文件（编号相同的保留原来的）
# 通过了全部硬性要求的对话 → SFT 样本（messages + 当时给模型看的 tools，去掉深度思考内容）
uv run ecom-agent export-sft runs/suite/aftersales-dsflash-full.jsonl --out runs/suite/sft-dsflash-full.jsonl     # --include-failed 没过的也要
```

## 控制台

```bash
uv run ecom-console            # http://127.0.0.1:8800；--runs 换目录，--db 换基础库，--no-kb 不加载知识库启动更快
uv run ecom-kb serve           # 只要检索台：http://127.0.0.1:8000
```

- **总览**：从购物任务到评分各环节的数量，有售后对话的运行显示评测报告。
- **售后对话**：一次运行的多份记录可以切换；左边是顾客和 agent 的对话，夹着工具调用（点开看参数和结果）、确认卡片和子 agent 的步骤；右边是场景、标准答案、评分（标出硬性要求）、用量、审计日志。
- **订单与物流**：拖时间滑块看物流和售后怎么推进；“模拟器视角”显示还没发生的事件和这单被设定的物流情形（agent 看不到）；可以在任意时刻做售后判断。
- **购物回放**：购物 agent 每一步搜了什么、打开了哪个商品，最后对比目标商品和买到的商品。
- **知识库**：BM25、向量、融合结果并排，显示触发的同义词，可以拖权重、开关扩展、按层级过滤。

控制台只读，不调用模型。规则编号和法条引用可以点开看原文。

## 扮演模型（文件桥）

`--bridge DIR` 时，每一轮的请求（消息和工具表）写进目录，等外部写回决策：

```bash
uv run ecom-agent bridge show runs/suite/bridge                                                      # 看这一轮的请求（--timeout 等多久）
uv run ecom-agent bridge reply runs/suite/bridge '{"tool": "check_after_sales", "arguments": {"order_id": "SO…", "type": "仅退款", "reason": "物流异常"}}'
uv run ecom-agent bridge reply runs/suite/bridge '{"content": "已为您申请仅退款 30 元（P-SHIP-07）。"}'
```

扮演者只看每一轮的观察，不看场景里的标准答案，否则成绩没有意义。

## 整条链路：购物 agent 买单 → 售后 agent 处理这些订单

```bash
mkdir -p runs/demo && cp shop/shop.db runs/demo/shop.db

# 1. ShopSimulator（在 shopping-grpo-longhorizon 仓库里）和订单网关
bash scripts/start_environment.sh                                   # 5700
uv run ecom-shop --db runs/demo/shop.db gateway --label demo         # 5701，转发到 5700，点 Buy Now 时记一单

# 2. 购物：shopping-grpo 自己的评测循环，模型换成文件桥（或者把评测的 SHOPSIM_BASE_URL 指到网关，用训练好的模型）
uv run ecom-agent shop 8811 21643 1982 --shopping-grpo <shopping-grpo 仓库> --bridge runs/demo/bridge-shop --out runs/demo/shopping.jsonl

# 3. 售后：从这些订单生成场景（按订单的物流情形挑顾客来问的时刻），跑对话并评分
uv run ecom-agent scenarios --db runs/demo/shop.db --label demo --out runs/demo/scenarios.json
uv run ecom-agent aftersales --db runs/demo/shop.db --scenarios runs/demo/scenarios.json --openai deepseek-flash ... --out runs/demo/aftersales.jsonl --in-place
```

离线的接法：`ecom-shop import-trajectories trajectories.jsonl --label grpo` 把已有评测轨迹里的购买补记成过去 20 天的订单，并模拟顾客收货以后的动作；`ecom-shop agent-orders` 按购物 agent 汇总买得对不对、退货和退款。细节见 [shop/README.md](../shop/README.md)。

## 环境上的几个坑

- DeepSeek 默认开思考模式，评测要加 `--extra-body '{"thinking": {"type": "disabled"}}'`。
- uv 装的 Python 在 macOS 上没有系统根证书，HTTPS 用 certifi 的证书（已内置）。
- 本机 transformers 跑 Qwen3.5 用的是慢速回退内核（没装 flash-linear-attention），MPS 上一段对话约 1 分钟。
- API 并发每个进程一份库副本（约 73 MB），`--shard` 按场景编号切分，不同场景用不同顾客和订单，互不影响。
