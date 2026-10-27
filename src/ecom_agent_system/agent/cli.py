"""ecom-agent：跑购物 agent 和售后 agent。模型可以由 Claude Code（或人）通过文件桥扮演。

    # 购物：用 shopping-grpo 自己的评测循环，经订单网关在 ShopSimulator 里买东西
    uv run ecom-agent shop 4412 9031 --shopping-grpo <仓库> --bridge runs/demo/bridge --out runs/demo/shopping.jsonl
    # 售后：从这个购物 agent 买的单生成场景，再跑售后对话并评分
    uv run ecom-agent scenarios --db runs/demo/shop.db --label claude --out runs/demo/scenarios.json
    uv run ecom-agent aftersales --db runs/demo/shop.db --scenarios runs/demo/scenarios.json --bridge runs/demo/bridge --out runs/demo/aftersales.jsonl
    # 扮演模型的一方：看这一轮的请求，写回决策
    uv run ecom-agent bridge show runs/demo/bridge
    uv run ecom-agent bridge reply runs/demo/bridge '{"tool": "search_products", "arguments": {"query": "儿童推车"}}'

    # 评测套件：从世界库挑订单生成 22 种情形的场景，先用参考解法检查场景本身
    uv run ecom-agent suite --dir runs/suite
    uv run ecom-agent aftersales --db runs/suite/shop.db --scenarios runs/suite/scenarios.json --reference --out runs/suite/aftersales-reference.jsonl
    # 接真实模型：OpenAI 兼容接口，或者本机 transformers；每个场景跑 3 次算 pass^3；顾客可以换成带错别字的或者大模型扮演的
    uv run ecom-agent aftersales ... --openai qwen3-8b --base-url http://127.0.0.1:8000/v1 --trials 3 --customer noisy
    # API 可以并发：8 个进程各跑一份（--shard i/8），report 合并；--extra-body 传接口私有字段，比如关掉 DeepSeek 的思考模式
    uv run ecom-agent aftersales ... --openai deepseek-flash --base-url https://api.deepseek.com --api-key-env DEEPSEEK_API_KEY \
        --extra-body '{"thinking": {"type": "disabled"}}' --shard 1/8 --out runs/suite/aftersales-dsflash.s1.jsonl
    uv run ecom-agent aftersales ... --hf Qwen/Qwen3.5-2B --temperature 0.7 --out runs/suite/aftersales-qwen3.5-2b.jsonl
    # 基线策略（不用模型）和对照实验：关掉关卡、不接知识库、不给顾客档案
    uv run ecom-agent aftersales ... --baseline comply --out runs/suite/aftersales-comply.jsonl
    uv run ecom-agent aftersales ... --hf Qwen/Qwen3.5-2B --gates-off all --out runs/suite/aftersales-qwen-nogates.jsonl
    # 子 agent：法规顾问（legal）顶替 search_policy，凭证审核员（evidence）接在凭证关卡后；默认和主 agent 同一个模型
    uv run ecom-agent aftersales ... --hf Qwen/Qwen3.5-2B --agents legal,evidence --out runs/suite/aftersales-qwen-agents.jsonl
    uv run ecom-agent aftersales ... --baseline comply-fabricate --agents evidence --sub-hf Qwen/Qwen3.5-2B --out ...
    uv run ecom-agent aftersales ... --baseline comply-fabricate --agents evidence --agents-stub reject --out ...  # 审核员的上下界
    # 汇总、用新的评分规则重新打分、回归对比：NEW 比 BASE 有场景从通过变成不通过时退出码为 1
    uv run ecom-agent report runs/suite/aftersales-reference.jsonl
    uv run ecom-agent regrade runs/suite/aftersales-old.jsonl --out runs/suite/aftersales-old.regraded.jsonl
    uv run ecom-agent regress runs/suite/aftersales-base.jsonl runs/suite/aftersales-new.jsonl

aftersales 默认在库的副本上跑（每次试验一份，放在输出文件旁边），--db 指的库不会被改；--in-place 直接改 --db。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

from ecom_agent_system.agent import bridge
from ecom_agent_system.agent.agents import AGENT_NAMES, build_agents, stub_agents
from ecom_agent_system.agent.baselines import BASELINES
from ecom_agent_system.agent.bridge import FileBridge
from ecom_agent_system.agent.flywheel import export_sft, harvest, merge_scenarios
from ecom_agent_system.agent.grading import compare, grade, load_records, summarize_records
from ecom_agent_system.agent.models import estimate_tokens
from ecom_agent_system.agent.scenarios import ReferenceModel, build_scenarios, run_scenarios
from ecom_agent_system.agent.suite import KINDS, build_suite
from ecom_agent_system.agent.tools import GATES
from ecom_agent_system.shop.db import DB_PATH, connect, ensure_agent_schema, get_meta
from ecom_agent_system.shop.service import Shop


def _shop(path):
    if not Path(path).exists():
        sys.exit(f"{path} 不存在，先运行 ecom-shop build")
    conn = connect(path)
    ensure_agent_schema(conn)
    return Shop(conn, seed=int(get_meta(conn, "seed", 0)))


def shop_tasks(args):
    """在 shopping-grpo 的评测循环里跑购物任务，模型换成文件桥。"""
    sys.path.insert(0, str(Path(args.shopping_grpo).resolve() / "src"))
    from shopping_grpo.evaluation.rollout import collect_tasks

    model = FileBridge(args.bridge)
    try:
        written = collect_tasks(
            [{"task_id": task_id} for task_id in args.task_ids], client=model,
            output_path=Path(args.out), base_url=args.base_url, max_steps=args.max_steps,
        )
    finally:
        model.close()
    for trajectory in written:
        terminal = trajectory.get("terminal_result") or {}
        purchase = terminal.get("purchase") or {}
        print(json.dumps({
            "task_id": trajectory["task_id"], "status": trajectory["status"], "steps": len(trajectory["steps"]),
            "outcome": terminal.get("termination_reason"), "reward": trajectory.get("final_reward"),
            "asin": purchase.get("asin"), "price": purchase.get("price"),
        }, ensure_ascii=False))


def suite(args):
    """把世界库复制到运行目录，在副本上生成评测套件（搭场景的数据写在副本里）。"""
    directory = Path(args.dir)
    db, path = directory / "shop.db", directory / "scenarios.json"
    if db.exists() and not args.force:
        sys.exit(f"{db} 已经存在；要重新生成加 --force")
    if not Path(args.world).exists():
        sys.exit(f"{args.world} 不存在，先运行 ecom-shop build")
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.world, db)
    shop = _shop(db)
    kinds = args.kinds.split(",") if args.kinds else None
    unknown = sorted(set(kinds or ()) - set(KINDS))
    if unknown:
        sys.exit(f"没有这些情形：{unknown}；可选的有 {KINDS}")
    scenarios = build_suite(shop, seed=args.seed, per_kind=args.per_kind, kinds=kinds)
    shop.conn.commit()
    path.write_text(json.dumps(scenarios, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    counts = {}
    for scenario in scenarios:
        counts[scenario["kind"]] = counts.get(scenario["kind"], 0) + 1
    short = sorted(kind for kind in (kinds or KINDS) if counts.get(kind, 0) < args.per_kind)
    print(json.dumps({"scenarios": len(scenarios), "db": str(db), "file": str(path), "by_kind": counts,
                      "not_enough_orders": short}, ensure_ascii=False, indent=2))


def _openai(model, base_url, key_env, temperature, extra_body=None, retries=2, backoff=1.0):
    from ecom_agent_system.agent.models import OpenAIModel, RetryingModel

    key = os.environ.get(key_env)
    if not key:
        sys.exit(f"环境变量 {key_env} 里没有 API key")
    return RetryingModel(OpenAIModel(base_url, model, key, temperature=temperature, extra_body=extra_body), retries=retries, backoff=backoff)


def _extra_body(args):
    """--extra-body 里的 JSON，原样并进每个请求：关 DeepSeek 的思考模式之类的接口私有字段。"""
    if not args.extra_body:
        return None
    try:
        body = json.loads(args.extra_body)
    except json.JSONDecodeError as error:
        sys.exit(f"--extra-body 不是合法的 JSON：{error}")
    if not isinstance(body, dict):
        sys.exit("--extra-body 要是一个 JSON 对象")
    return body


def _agent_model(args):
    """(model, model_for)：文件桥、OpenAI 兼容接口、本机 transformers，或者每个场景一个参考解法、基线策略。"""
    if args.reference:
        return None, ReferenceModel
    if args.baseline:
        return None, BASELINES[args.baseline]
    if args.openai:
        return _openai(args.openai, args.base_url, args.api_key_env, args.temperature, _extra_body(args), args.retries, args.backoff), None
    if args.hf:
        from ecom_agent_system.agent.models import HFModel

        return HFModel(args.hf, temperature=args.temperature, max_new_tokens=args.max_new_tokens, thinking=args.thinking, seed=args.seed), None
    return FileBridge(args.bridge), None


def _gates(args):
    """开着的关卡；没有 --gates-off 时返回 None，表示全开。"""
    if not args.gates_off:
        return None
    off = set(GATES) if args.gates_off == "all" else set(args.gates_off.split(","))
    unknown = sorted(off - set(GATES))
    if unknown:
        sys.exit(f"没有这些关卡：{unknown}；可选的有 {list(GATES)} 或 all")
    return [gate for gate in GATES if gate not in off]


def _agents(args, model):
    """子 agent：默认用主 agent 的模型；--sub-hf / --sub-openai 换一个；--agents-stub 用不带模型的桩。"""
    if not args.agents:
        return None
    names = args.agents.split(",")
    unknown = sorted(set(names) - set(AGENT_NAMES))
    if unknown:
        sys.exit(f"没有这些子 agent：{unknown}；可选的有 {list(AGENT_NAMES)}")
    if "legal" in names and args.no_kb:
        sys.exit("法规顾问要用知识库，不能和 --no-kb 一起用")
    if args.agents_stub:
        return stub_agents(names, args.agents_stub)
    sub_model = model
    if args.sub_openai:
        sub_model = _openai(args.sub_openai, args.base_url, args.api_key_env, 0.0, _extra_body(args), args.retries, args.backoff)
    elif args.sub_hf and args.sub_hf != args.hf:
        from ecom_agent_system.agent.models import HFModel

        sub_model = HFModel(args.sub_hf, temperature=0.0, max_new_tokens=args.max_new_tokens, seed=args.seed)
    if sub_model is None:
        sys.exit("子 agent 需要模型：--sub-hf 或 --sub-openai；不用模型的话用 --agents-stub approve|reject")
    return build_agents(names, sub_model)


def _customer_model(args):
    if args.customer != "llm":
        return None
    if args.customer_bridge:
        return FileBridge(args.customer_bridge)
    if args.customer_openai:
        return _openai(args.customer_openai, args.customer_base_url or args.base_url, args.customer_api_key_env or args.api_key_env, 0.7,
                       _extra_body(args), args.retries, args.backoff)
    sys.exit("--customer llm 需要 --customer-openai 或 --customer-bridge")


def aftersales(args):
    from ecom_agent_system.knowledge_base import KnowledgeBase

    scenarios = json.loads(Path(args.scenarios).read_text(encoding="utf-8"))
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [s for s in scenarios if s["kind"] in wanted or s.get("category") in wanted]
    if args.shard:  # 并发：几个进程各跑一份，场景按位置轮流分，之后 report 合并各份记录
        try:
            index, total = (int(part) for part in args.shard.split("/"))
        except ValueError:
            sys.exit("--shard 写成 i/n，例如 3/8")
        if not 1 <= index <= total:
            sys.exit(f"--shard {args.shard} 超出范围")
        scenarios = scenarios[index - 1::total]
    if not scenarios:
        sys.exit("没有要跑的场景")
    out = Path(args.out)
    if out.exists() and not args.append:
        sys.exit(f"{out} 已经存在；接着写加 --append")
    if args.in_place and args.trials > 1:
        sys.exit("--in-place 只能跑一次试验")
    out.parent.mkdir(parents=True, exist_ok=True)
    knowledge_base = None if args.no_kb else KnowledgeBase.load()
    gates = _gates(args)
    model, model_for = _agent_model(args)
    agents = _agents(args, model)
    customer_model = _customer_model(args)
    records = []
    try:
        for trial in range(1, args.trials + 1):
            db = Path(args.db) if args.in_place else out.with_name(f"{out.stem}.t{trial}.db")
            if not args.in_place:
                if not Path(args.db).exists():
                    sys.exit(f"{args.db} 不存在")
                shutil.copyfile(args.db, db)  # 每次试验都从同一份库开始
            shop = _shop(db)
            try:
                records += run_scenarios(
                    shop, knowledge_base, scenarios, model, out, model_for=model_for, customer=args.customer,
                    customer_model=customer_model, memory=not args.no_memory, trial=trial, label=args.label, db=db.name,
                    gates=gates, agents=agents, legacy=args.legacy_harness, shopping=True if args.shopping else None,
                )
            finally:
                shop.conn.close()
    finally:
        for backend in (model, customer_model):
            if hasattr(backend, "close"):
                backend.close()
    for record in records:
        grade = record["grade"]
        failed = [name for name in grade["hard"] if not grade["checks"][name]]
        print(json.dumps({"trial": record["trial"], "scenario": record["scenario"]["scenario_id"], "ended": record["ended"],
                          "passed": grade["passed"], "failed": failed}, ensure_ascii=False))
    print(json.dumps(summarize_records(records), ensure_ascii=False, indent=2))


def regrade(args):
    """用现在的评分规则重新给记录打分。人工审核那一项要查库，记录里的 db 指的库放在记录文件旁边。

    --scenarios 给新生成的场景文件时，同编号的场景换成新的（标准答案变了也能重新打分）；对话本身不变。
    """
    source = Path(args.records)
    records = load_records(source)
    if args.scenarios:
        latest = {s["scenario_id"]: s for s in json.loads(Path(args.scenarios).read_text(encoding="utf-8"))}
        for record in records:
            record["scenario"] = latest.get(record["scenario"]["scenario_id"], record["scenario"])
    shops = {}
    try:
        for record in records:
            db = record.get("db")
            if db and db not in shops:
                path = source.parent / db
                shops[db] = _shop(path) if path.exists() else None
            record["grade"] = grade(record["scenario"], record, shops.get(db))
    finally:
        for shop in shops.values():
            if shop is not None:
                shop.conn.close()
    with open(args.out, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(summarize_records(records), ensure_ascii=False, indent=2))


def regress(args):
    base, new = load_records(args.base), load_records(args.new)
    result = {"compare": compare(base, new), "base": summarize_records(base), "new": summarize_records(new)}
    # 有回归、新结果缺场景、有出错的对话、试验次数变少都算失败：漏跑一半的评测不能放行
    problems = [p for p in result["compare"]["problems"] if not (args.allow_missing and p.startswith("only_in_base"))]
    result["failed"] = problems
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if problems else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ecom-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    bridge_parser = commands.add_parser("bridge", help="扮演模型：看这一轮的请求，写回决策")
    bridge_parser.add_argument("action", choices=["show", "reply"])
    bridge_parser.add_argument("directory")
    bridge_parser.add_argument("decision", nargs="?", help='reply 的决策：{"tool": ..., "arguments": {...}} 或 {"content": "..."}')
    bridge_parser.add_argument("--timeout", type=float, default=120, help="等下一轮请求最多多少秒")

    shop_parser = commands.add_parser("shop", help="用 shopping-grpo 的评测循环跑购物任务")
    shop_parser.add_argument("task_ids", nargs="+", type=int, help="ShopSimulator 任务编号")
    shop_parser.add_argument("--shopping-grpo", required=True, help="shopping-grpo-longhorizon 仓库的路径")
    shop_parser.add_argument("--base-url", default="http://127.0.0.1:5701", help="订单网关（或 ShopSimulator）的地址")
    shop_parser.add_argument("--bridge", required=True, help="文件桥目录")
    shop_parser.add_argument("--out", required=True, help="轨迹写到哪个 JSONL")
    shop_parser.add_argument("--max-steps", type=int, default=35)

    scenario_parser = commands.add_parser("scenarios", help="从购物 agent 买的单生成售后场景")
    scenario_parser.add_argument("--db", default=DB_PATH)
    scenario_parser.add_argument("--label", required=True, help="哪个购物 agent 的订单")
    scenario_parser.add_argument("--out", required=True)

    suite_parser = commands.add_parser("suite", help="从世界库生成评测套件")
    suite_parser.add_argument("--world", default=DB_PATH, help="世界库，默认 shop/shop.db")
    suite_parser.add_argument("--dir", required=True, help="运行目录：库的副本 shop.db 和 scenarios.json 写在这里")
    suite_parser.add_argument("--per-kind", type=int, default=3, help="每种情形几个场景")
    suite_parser.add_argument("--seed", type=int, default=0)
    suite_parser.add_argument("--kinds", help="只生成这些情形，逗号分隔")
    suite_parser.add_argument("--force", action="store_true", help="运行目录里已经有库时覆盖")

    aftersales_parser = commands.add_parser("aftersales", help="跑售后对话并评分")
    aftersales_parser.add_argument("--db", default=DB_PATH)
    aftersales_parser.add_argument("--scenarios", required=True)
    aftersales_parser.add_argument("--out", required=True, help="对话记录和评分写到哪个 JSONL")
    backend = aftersales_parser.add_mutually_exclusive_group(required=True)
    backend.add_argument("--bridge", help="文件桥目录：Claude Code 或人扮演模型")
    backend.add_argument("--openai", metavar="MODEL", help="OpenAI 兼容接口上的模型名")
    backend.add_argument("--reference", action="store_true", help="照场景里的参考解法执行，检查场景和评分")
    backend.add_argument("--hf", metavar="MODEL", help="本机用 transformers 跑的模型名或路径，如 Qwen/Qwen3.5-2B")
    backend.add_argument("--baseline", choices=list(BASELINES), help="不用模型的基线策略，用来校准评分")
    aftersales_parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1", help="OpenAI 兼容接口的地址")
    aftersales_parser.add_argument("--api-key-env", default="OPENAI_API_KEY", help="从哪个环境变量读 API key")
    aftersales_parser.add_argument("--extra-body", metavar="JSON", help='并进每个请求的字段，如 \'{"thinking": {"type": "disabled"}}\'')
    aftersales_parser.add_argument("--temperature", type=float, default=0.0)
    aftersales_parser.add_argument("--retries", type=int, default=2, help="接口超时、429、5xx 时重试几次；限流严的接口用 8")
    aftersales_parser.add_argument("--backoff", type=float, default=1.0, help="第一次重试等几秒，之后翻倍，最多 60 秒")
    aftersales_parser.add_argument("--max-new-tokens", type=int, default=512, help="--hf 每次生成最多多少 token")
    aftersales_parser.add_argument("--thinking", action="store_true", help="--hf 时开深度思考")
    aftersales_parser.add_argument("--seed", type=int, default=0, help="--hf 采样的随机种子")
    aftersales_parser.add_argument("--gates-off", metavar="GATES", help=f"关掉哪些关卡做对照：逗号分隔的 {list(GATES)}，或 all")
    aftersales_parser.add_argument("--no-kb", action="store_true", help="不接知识库：没有 search_policy 工具，对照检索的作用")
    aftersales_parser.add_argument("--agents", metavar="NAMES", help=f"接上哪些子 agent，逗号分隔的 {list(AGENT_NAMES)}；默认一个都不接")
    aftersales_parser.add_argument("--agents-stub", choices=["approve", "reject"], help="子 agent 用不带模型的桩：凭证审核员全部通过或全部驳回")
    aftersales_parser.add_argument("--sub-hf", metavar="MODEL", help="子 agent 用这个本机模型（和 --hf 相同时共用）；默认用主 agent 的模型")
    aftersales_parser.add_argument("--sub-openai", metavar="MODEL", help="子 agent 用 OpenAI 兼容接口上的这个模型")
    aftersales_parser.add_argument("--customer", choices=["scripted", "noisy", "llm"], default="scripted", help="顾客怎么扮演")
    aftersales_parser.add_argument("--customer-openai", metavar="MODEL", help="--customer llm 时扮演顾客的模型")
    aftersales_parser.add_argument("--customer-base-url")
    aftersales_parser.add_argument("--customer-api-key-env")
    aftersales_parser.add_argument("--customer-bridge", help="--customer llm 时由文件桥扮演顾客")
    aftersales_parser.add_argument("--no-memory", action="store_true", help="系统提示里不放顾客档案，对照记忆的作用")
    aftersales_parser.add_argument("--legacy-harness", action="store_true", help="用旧版提示词第 3、4 条，判断结果不带办理权限档；对照 2026-10-26 的改动")
    aftersales_parser.add_argument("--shopping", action="store_true", help="所有场景都给购物工具（搜商品、看商品、下单）；默认只有混合情形开")
    aftersales_parser.add_argument("--trials", type=int, default=1, help="每个场景跑几次，算 pass^k")
    aftersales_parser.add_argument("--only", help="只跑这些情形或类别，逗号分隔")
    aftersales_parser.add_argument("--shard", metavar="i/n", help="只跑第 i 份（共 n 份），几个进程并发时用；report 能合并各份的记录")
    aftersales_parser.add_argument("--label", default="run", help="会话编号的前缀")
    aftersales_parser.add_argument("--in-place", action="store_true", help="直接在 --db 上跑，不复制")
    aftersales_parser.add_argument("--append", action="store_true", help="输出文件已经存在时接着写")

    report_parser = commands.add_parser("report", help="汇总一次运行的评分")
    report_parser.add_argument("records", nargs="+", help="aftersales 输出的 JSONL")
    regrade_parser = commands.add_parser("regrade", help="用现在的评分规则重新给一份记录打分")
    regrade_parser.add_argument("records", help="aftersales 输出的 JSONL，用过的库在同一目录")
    regrade_parser.add_argument("--out", required=True, help="重新打分后的记录写到哪里")
    regrade_parser.add_argument("--scenarios", help="新的场景文件：同编号场景的标准答案换成新的再打分")

    regress_parser = commands.add_parser("regress", help="两次运行逐个场景对比：有回归、新结果缺场景、有出错的对话或试验次数变少时退出码为 1")
    regress_parser.add_argument("base")
    regress_parser.add_argument("new")
    regress_parser.add_argument("--allow-missing", action="store_true", help="新结果故意只跑了子集（--only、--shard）时，缺场景不算失败")

    harvest_parser = commands.add_parser("harvest", help="数据回流：对话记录 → 新场景（顾客的原话）或回归集")
    harvest_parser.add_argument("records", nargs="+", help="aftersales 输出的 JSONL")
    harvest_parser.add_argument("--out", required=True, help="新场景写到哪个 JSON")
    harvest_parser.add_argument("--failed-only", action="store_true", help="只取没通过的对话")
    harvest_parser.add_argument("--lines", choices=["harvested", "original"], default="harvested",
                                help="harvested：用对话里顾客的原话做新场景；original：保留原台词（回归集）")
    harvest_parser.add_argument("--suffix", default="harvest", help="新场景编号的后缀")
    harvest_parser.add_argument("--merge-into", metavar="SCENARIOS", help="把新场景并进这份场景文件，编号相同的保留原来的")
    sft_parser = commands.add_parser("export-sft", help="数据回流：通过的对话 → SFT 样本（messages + tools）")
    sft_parser.add_argument("records", nargs="+", help="aftersales 输出的 JSONL")
    sft_parser.add_argument("--out", required=True, help="样本写到哪个 JSONL")
    sft_parser.add_argument("--include-failed", action="store_true", help="没通过的也导出")
    args = parser.parse_args(argv)

    if args.command == "bridge":
        if args.action == "show":
            print(bridge.show(args.directory, args.timeout))
        elif not args.decision:
            sys.exit("reply 需要一个决策 JSON")
        else:
            print(bridge.reply(args.directory, json.loads(args.decision), args.timeout))
    elif args.command == "shop":
        shop_tasks(args)
    elif args.command == "scenarios":
        scenarios = build_scenarios(_shop(args.db), args.label)
        Path(args.out).write_text(json.dumps(scenarios, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps([{key: scenario[key] for key in ("scenario_id", "kind", "now", "expected")} for scenario in scenarios], ensure_ascii=False, indent=2))
    elif args.command == "suite":
        suite(args)
    elif args.command == "aftersales":
        aftersales(args)
    elif args.command == "report":
        print(json.dumps(summarize_records([r for path in args.records for r in load_records(path)]), ensure_ascii=False, indent=2))
    elif args.command == "regrade":
        regrade(args)
    elif args.command == "harvest":
        records = [r for path in args.records for r in load_records(path)]
        scenarios = harvest(records, failed_only=args.failed_only, lines=args.lines, suffix=args.suffix)
        Path(args.out).write_text(json.dumps(scenarios, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        summary = {"scenarios": len(scenarios), "from_records": len(records), "file": args.out,
                   "by_kind": dict(sorted(Counter(s["kind"] for s in scenarios).items()))}
        if args.merge_into:
            base = json.loads(Path(args.merge_into).read_text(encoding="utf-8"))
            merged = merge_scenarios(base, scenarios)
            Path(args.merge_into).write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            summary.update(merged_into=args.merge_into, total=len(merged))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    elif args.command == "export-sft":
        records = [r for path in args.records for r in load_records(path)]
        examples = export_sft(records, include_failed=args.include_failed)
        with open(args.out, "w", encoding="utf-8") as handle:
            for example in examples:
                handle.write(json.dumps(example, ensure_ascii=False) + "\n")
        tokens = sum(estimate_tokens(json.dumps(e["messages"], ensure_ascii=False)) for e in examples)
        print(json.dumps({"examples": len(examples), "from_records": len(records), "estimated_tokens": tokens, "file": args.out,
                          "by_kind": dict(sorted(Counter(e["kind"] for e in examples).items()))}, ensure_ascii=False, indent=2))
    else:
        sys.exit(regress(args))


if __name__ == "__main__":
    main()
