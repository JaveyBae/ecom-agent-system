"""整条链路的控制台：总览、购物回放、订单和物流、售后对话；知识库检索台挂在 /kb/。

    uv run ecom-console        # 读 runs/ 下的每次运行，打开 http://127.0.0.1:8800

只读：页面看的是数据库和运行记录（购物轨迹、售后对话），不调用模型。订单页可以把“当前时间”往后拨，看
物流和售后怎么推进；打开“模拟器视角”会显示还没发生的事件和物流情形的设定，这些 agent 都看不到。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from ecom_agent_system.agent.grading import summarize_records
from ecom_agent_system.server import STATIC_DIR as KNOWLEDGE_STATIC_DIR
from ecom_agent_system.server import add_knowledge_routes
from ecom_agent_system.shop.catalog import CategoryPolicy
from ecom_agent_system.shop.cli import resolve_now
from ecom_agent_system.shop.db import DB_PATH, connect, ensure_agent_schema, fmt, get_meta, parse
from ecom_agent_system.shop.purchases import agent_orders
from ecom_agent_system.shop.rules import PROJECT_ROOT, PlatformRules
from ecom_agent_system.shop.service import TICKET_TYPES, Shop, ShopError

STATIC_DIR = Path(__file__).resolve().parent / "static"
RUNS_DIR = PROJECT_ROOT / "runs"
BASE_RUN = "shop.db"  # 不属于任何运行的基础库
SHOPPING, AFTERSALES = "shopping.jsonl", "aftersales.jsonl"


class Runs:
    """runs/ 下的每个目录是一次运行：shop.db、shopping.jsonl（购物轨迹）、aftersales*.jsonl（售后对话）。

    ecom-agent aftersales 默认在库的副本上跑，副本（如 aftersales-reference.t1.db）也在运行目录里，
    每条对话记录的 db 字段写着它跑在哪个库上。
    """

    def __init__(self, runs_dir=RUNS_DIR, base_db=DB_PATH):
        self.runs_dir, self.base_db = Path(runs_dir), Path(base_db)
        self.rules, self.categories = PlatformRules.load(), CategoryPolicy.load()

    def names(self):
        names = sorted(path.name for path in self.runs_dir.iterdir() if path.is_dir()) if self.runs_dir.is_dir() else []
        return names + ([BASE_RUN] if self.base_db.exists() else [])

    def _directory(self, run):
        if run not in self.names():
            raise HTTPException(404, f"没有 {run} 这次运行")
        return None if run == BASE_RUN else self.runs_dir / run

    def databases(self, run):
        directory = self._directory(run)
        return sorted(path.name for path in directory.glob("*.db")) if directory is not None else []

    def aftersales_files(self, run):
        directory = self._directory(run)
        if directory is None:
            return []
        names = sorted(path.name for path in directory.glob("aftersales*.jsonl"))
        return sorted(names, key=lambda name: name != AFTERSALES)  # aftersales.jsonl 排第一

    @contextmanager
    def shop(self, run, db=None):
        """打开这次运行的库；db 是运行目录里的另一个库（对话跑在副本上时），默认 shop.db。"""
        directory = self._directory(run)
        if db:
            if db not in self.databases(run):
                raise HTTPException(404, f"{run} 里没有 {db}")
            path = directory / db
        else:
            path = self.base_db if directory is None or not (directory / "shop.db").exists() else directory / "shop.db"
        if not path.exists():
            raise HTTPException(404, f"{run} 没有数据库")
        conn = connect(path)
        try:
            ensure_agent_schema(conn)
            yield Shop(conn, self.rules, self.categories, seed=int(get_meta(conn, "seed", 0)))
        finally:
            conn.close()

    def records(self, run, name):
        directory = self._directory(run)
        path = directory / name if directory is not None else None
        if path is None or not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _world_now(shop):
    return parse(get_meta(shop.conn, "world_now"))


def _moment(text, world_now):
    try:
        return resolve_now(text, world_now)
    except argparse.ArgumentTypeError as error:
        raise HTTPException(422, str(error)) from None


def _agent_rows(shop):
    rows = shop.conn.execute(
        "SELECT a.*, o.created_at FROM agent_purchases a JOIN orders o USING (order_id) ORDER BY o.created_at"
    ).fetchall()
    return [dict(row) for row in rows]


def _order_brief(shop, order_id, now, agent=None):
    """订单列表里的一行：商品、金额、买家、此刻的状态和售后单。"""
    order = shop.conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    item = shop.conn.execute("SELECT title FROM order_items WHERE order_id = ?", (order_id,)).fetchone()
    user = shop.conn.execute("SELECT name, tier, zone FROM users WHERE user_id = ?", (order["user_id"],)).fetchone()
    visible = order["created_at"] <= fmt(now)
    detail = shop.get_order(order_id, now=now) if visible else None
    return {
        "order_id": order_id,
        "created_at": order["created_at"],
        "title": item["title"],
        "amount": order["total_amount"],
        "source": order["source"],
        "user": {"user_id": order["user_id"], **dict(user)},
        "status": detail["status"] if detail else "尚未下单",
        "tickets": detail["after_sales"] if detail else [],
        "agent": None if agent is None else {key: agent[key] for key in ("label", "task_id", "outcome")},
    }


def _instruction(trajectory):
    text = (trajectory.get("initial_result") or {}).get("instruction") or ""
    return text.removeprefix("Instruction:").strip()


def _shopping_summary(index, trajectory, orders_by_task):
    terminal = trajectory.get("terminal_result") or {}
    purchase = terminal.get("purchase") or {}
    return {
        "index": index,
        "task_id": trajectory.get("task_id"),
        "status": trajectory.get("status"),
        "steps": len(trajectory.get("steps") or []),
        "outcome": terminal.get("termination_reason") or trajectory.get("status"),
        "reward": trajectory.get("final_reward"),
        "instruction": _instruction(trajectory),
        "purchase": {key: purchase.get(key) for key in ("asin", "name", "price", "options")} if purchase else None,
        "order_id": orders_by_task.get(trajectory.get("task_id")),
    }


def _shopping_detail(index, trajectory, orders_by_task):
    terminal = trajectory.get("terminal_result") or {}
    detail = terminal.get("reward_detail") or {}
    goal = terminal.get("goal") or {}
    gates = [
        {"name": name, "status": gate.get("status"), "required": gate.get("required"), "actual": gate.get("actual")}
        for name, gate in (detail.get("hard_gates") or {}).items() if isinstance(gate, dict)
    ]
    return {
        **_shopping_summary(index, trajectory, orders_by_task),
        "initial_state": (trajectory.get("initial_result") or {}).get("observation_state"),
        "steps": [
            {
                "index": step.get("step_index"),
                "tool": step.get("tool_name"),
                "arguments": step.get("parameters"),
                "state": (step.get("result") or {}).get("observation_state"),
                "done": step.get("done"),
                "error": step.get("error"),
            }
            for step in trajectory.get("steps") or []
        ],
        "blocked": [
            {
                "step_index": blocked.get("step_index"),
                "tool": ((blocked.get("tool_call") or {}).get("function") or {}).get("name"),
                "reason": blocked.get("reason"),
            }
            for blocked in trajectory.get("blocked_tool_calls") or []
        ],
        "result": {
            "outcome": terminal.get("termination_reason"),
            "reward": terminal.get("reward"),
            "reward_valid": terminal.get("reward_valid"),
            "target_asin_match": detail.get("target_asin_match"),
            "purchase": terminal.get("purchase") or None,
            "gates": gates,
            "dimensions": detail.get("dimension_scores"),
        },
        "goal": {key: goal.get(key) for key in ("asin", "name", "category", "goal_options", "price_upper")} if goal else None,
        "error": trajectory.get("error"),
    }


def _conversation_summary(index, record):
    scenario, grade = record["scenario"], record["grade"]
    usage = record.get("usage") or {}
    return {
        "index": index,
        "scenario_id": scenario["scenario_id"],
        "kind": scenario["kind"],
        "category": scenario.get("category", "基本售后"),
        "order_id": scenario["order_id"],
        "now": scenario["now"],
        "opening": scenario["opening"],
        "passed": grade["passed"],
        "failed": [name for name in grade.get("hard", []) if not grade["checks"][name]],
        "ended": record.get("ended"),
        "trial": record.get("trial"),
        "customer": record.get("customer", "scripted"),
        "memory": record.get("memory", True),
        "kb": record.get("kb", True),
        "gates_off": record.get("gates_off", []),
        "agents": record.get("agents", []),
        "shopping": record.get("shopping", False),
        "tool_calls": usage.get("tool_calls", len(record.get("trace") or [])),
    }


def _truth(shop, order_id, now):
    """模拟器视角：这个订单全部的物流和售后事件（包括还没发生的），以及物流情形的设定。"""
    shipments = []
    for row in shop.conn.execute("SELECT * FROM shipments WHERE order_id = ? ORDER BY created_at", (order_id,)):
        events = shop.conn.execute(
            "SELECT time, code, location, description FROM shipment_events WHERE tracking_no = ? ORDER BY time, id",
            (row["tracking_no"],),
        ).fetchall()
        shipments.append({
            "tracking_no": row["tracking_no"],
            "direction": row["direction"],
            "scenario": row["scenario"],
            "events": [{**dict(event), "future": event["time"] > fmt(now)} for event in events],
        })
    tickets = []
    for row in shop.conn.execute("SELECT ticket_id FROM after_sales WHERE order_id = ? ORDER BY created_at", (order_id,)):
        events = shop.conn.execute(
            "SELECT time, status, actor, note FROM after_sales_events WHERE ticket_id = ? ORDER BY time, id", (row["ticket_id"],)
        ).fetchall()
        tickets.append({"ticket_id": row["ticket_id"], "events": [{**dict(event), "future": event["time"] > fmt(now)} for event in events]})
    return {"shipments": shipments, "tickets": tickets}


def _check_counts(records):
    """每项检查的通过数：{名称: [通过, 总数, 是否硬性]}。旧记录没有 hard 字段，按当时的两项硬性要求算。"""
    checks = {}
    for record in records:
        hard = set(record["grade"].get("hard") or ("售后单和标准答案一致", "没有办顾客没要求的事"))
        for name, passed in record["grade"]["checks"].items():
            counts = checks.setdefault(name, [0, 0, name in hard])
            counts[0] += int(passed)
            counts[1] += 1
    return checks


def _audit_rows(shop, *, session=None, order_id=None):
    if session is not None:
        rows = shop.conn.execute("SELECT * FROM audit_log WHERE session = ? ORDER BY id", (session,))
    else:
        rows = shop.conn.execute("SELECT * FROM audit_log WHERE arguments LIKE ? ORDER BY id", (f'%"{order_id}"%',))
    return [{**dict(row), "arguments": json.loads(row["arguments"])} for row in rows]


def _latest_event(shop, order_id):
    row = shop.conn.execute(
        "SELECT MAX(time) FROM (SELECT e.time FROM shipment_events e JOIN shipments s USING (tracking_no) WHERE s.order_id = ?"
        " UNION ALL SELECT e.time FROM after_sales_events e JOIN after_sales a USING (ticket_id) WHERE a.order_id = ?)",
        (order_id, order_id),
    ).fetchone()
    return row[0]


def create_app(runs, knowledge_base=None):
    app = FastAPI(title="ShopSim Agent 控制台")
    if knowledge_base is not None:
        add_knowledge_routes(app, knowledge_base)

    def orders_by_task(shop):
        return {row["task_id"]: row["order_id"] for row in _agent_rows(shop) if row["task_id"] is not None}

    @app.get("/api/console/runs")
    def list_runs():
        result = []
        for name in runs.names():
            with runs.shop(name) as shop:
                labels = [row[0] for row in shop.conn.execute("SELECT DISTINCT label FROM agent_purchases ORDER BY label")]
                world_now = get_meta(shop.conn, "world_now")
            files = {file: len(runs.records(name, file)) for file in runs.aftersales_files(name)}
            result.append({
                "name": name,
                "labels": labels,
                "world_now": world_now,
                "shopping": len(runs.records(name, SHOPPING)),
                "aftersales": sum(files.values()),
                "aftersales_files": files,
            })
        return {"runs": result, "knowledge_base": knowledge_base is not None}

    def conversations_of(run, file):
        files = runs.aftersales_files(run)
        if file and file not in files:
            raise HTTPException(404, f"{run} 里没有 {file}")
        chosen = file or (files[0] if files else None)
        return chosen, files, runs.records(run, chosen) if chosen else []

    @app.get("/api/console/overview")
    def overview(run: str):
        trajectories = runs.records(run, SHOPPING)
        file, _, conversations = conversations_of(run, None)
        with runs.shop(run) as shop:
            now = _world_now(shop)
            agents = _agent_rows(shop)
            count = lambda sql: shop.conn.execute(sql).fetchone()[0]  # noqa: E731
            world = {
                "products": count("SELECT COUNT(*) FROM products"),
                "users": count("SELECT COUNT(*) FROM users"),
                "orders": {row[0]: row[1] for row in shop.conn.execute("SELECT source, COUNT(*) FROM orders GROUP BY source")},
                "tickets": count("SELECT COUNT(*) FROM after_sales"),
                "work_orders": count("SELECT COUNT(*) FROM work_orders"),
            }
            logistics = Counter(
                row[0] for row in shop.conn.execute(
                    "SELECT s.scenario FROM shipments s JOIN agent_purchases a USING (order_id) WHERE s.direction = '发货'"
                )
            )
            orders = [_order_brief(shop, row["order_id"], now, row) for row in agents]
            report = agent_orders(shop, now)
        return {
            "world_now": fmt(now),
            "world": world,
            "shopping": {
                "tasks": len(trajectories),
                "outcomes": dict(Counter((t.get("terminal_result") or {}).get("termination_reason") or t.get("status") for t in trajectories)),
                "steps": sum(len(t.get("steps") or []) for t in trajectories),
            },
            "orders": orders,
            "sales_amount": round(sum(order["amount"] for order in orders), 2),
            "logistics": dict(logistics),
            "agent_report": report,
            "aftersales": {
                "file": file,
                "conversations": len(conversations),
                "passed": sum(record["grade"]["passed"] for record in conversations),
                "submitted": sum(1 for record in conversations for entry in record.get("executed") or [] if entry["tool"] == "create_after_sales"),
                "checks": _check_counts(conversations),
                "report": summarize_records(conversations) if conversations else None,
            },
        }

    @app.get("/api/console/shopping")
    def shopping(run: str):
        with runs.shop(run) as shop:
            by_task = orders_by_task(shop)
        return {"trajectories": [_shopping_summary(i, t, by_task) for i, t in enumerate(runs.records(run, SHOPPING))]}

    @app.get("/api/console/shopping/{index}")
    def shopping_detail(run: str, index: int):
        trajectories = runs.records(run, SHOPPING)
        if not 0 <= index < len(trajectories):
            raise HTTPException(404, f"没有第 {index} 条购物轨迹")
        with runs.shop(run) as shop:
            by_task = orders_by_task(shop)
        return _shopping_detail(index, trajectories[index], by_task)

    @app.get("/api/console/aftersales")
    def aftersales(run: str, file: str | None = None):
        chosen, files, records = conversations_of(run, file)
        return {
            "file": chosen,
            "files": files,
            "conversations": [_conversation_summary(i, r) for i, r in enumerate(records)],
            "report": summarize_records(records) if records else None,
        }

    @app.get("/api/console/aftersales/{index}")
    def aftersales_detail(run: str, index: int, file: str | None = None):
        _, _, records = conversations_of(run, file)
        if not 0 <= index < len(records):
            raise HTTPException(404, f"没有第 {index} 段售后对话")
        record = records[index]
        db = record.get("db")
        audit = []
        if record.get("session") and (db is None or db in runs.databases(run)):
            with runs.shop(run, db) as shop:
                audit = _audit_rows(shop, session=record["session"])
        return {
            **_conversation_summary(index, record),
            "scenario": record["scenario"],
            "messages": record["messages"],
            "executed": record.get("executed") or [],
            "blocked": record.get("blocked") or [],
            "trace": record.get("trace") or [],
            "usage": record.get("usage"),
            "error": record.get("error"),
            "db": db,
            "audit": audit,
            "grade": record["grade"],
        }

    @app.get("/api/console/orders")
    def orders(run: str, q: Annotated[str, Query(max_length=40)] = ""):
        query = q.strip()
        with runs.shop(run) as shop:
            now = _world_now(shop)
            agents = {row["order_id"]: row for row in _agent_rows(shop)}
            if not query:
                order_ids = list(reversed(agents))
            elif query.upper().startswith("U"):
                order_ids = [row[0] for row in shop.conn.execute(
                    "SELECT order_id FROM orders WHERE user_id = ? ORDER BY created_at DESC LIMIT 50", (query.upper(),)
                )]
            else:
                order_ids = [row[0] for row in shop.conn.execute(
                    "SELECT order_id FROM orders WHERE order_id LIKE ? ORDER BY created_at DESC LIMIT 50", (query.upper() + "%",)
                )]
            return {"orders": [_order_brief(shop, order_id, now, agents.get(order_id)) for order_id in order_ids]}

    @app.get("/api/console/orders/{order_id}")
    def order_detail(order_id: str, run: str, now: str | None = None, truth: bool = False, db: str | None = None):
        with runs.shop(run, db) as shop:
            world_now = _world_now(shop)
            row = shop.conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
            if row is None:
                raise HTTPException(404, f"订单 {order_id} 不存在")
            moment = max(_moment(now, world_now), parse(row["created_at"]))
            detail = shop.get_order(order_id, now=moment)
            agent = shop.conn.execute("SELECT * FROM agent_purchases WHERE order_id = ?", (order_id,)).fetchone()
            latest = _latest_event(shop, order_id)
            result = {
                "now": fmt(moment),
                "bounds": {"start": row["created_at"], "world_now": fmt(world_now), "latest_event": latest},
                "order": detail,
                "tracking": shop.track_shipment(order_id, now=moment),
                "tickets": [shop.get_after_sales(ticket["ticket_id"], now=moment) for ticket in detail["after_sales"]],
                "user": shop.get_user(row["user_id"]),
                "work_orders": [w for w in shop.list_work_orders(row["user_id"], now=moment) if w["order_id"] == order_id],
                "audit": [entry for entry in _audit_rows(shop, order_id=order_id) if entry["time"] <= fmt(moment)],
                "db": db,
                "agent": None if agent is None else {
                    **dict(agent),
                    "failed_gates": json.loads(agent["failed_gates"]),
                    "customer_actions": json.loads(agent["customer_actions"] or "null"),
                },
                "ticket_types": TICKET_TYPES,
            }
            if truth:
                result["truth"] = _truth(shop, order_id, moment)
            return result

    @app.get("/api/console/check")
    def check(run: str, order_id: str, type: str, reason: str, now: str | None = None, opened: bool = False, activated: bool = False):
        if reason not in TICKET_TYPES.get(type, ()):
            raise HTTPException(422, f"{type} 没有“{reason}”这个原因")
        with runs.shop(run) as shop:
            moment = _moment(now, _world_now(shop))
            try:
                return shop.check_after_sales(order_id, type, reason, now=moment, opened=opened, activated=activated)
            except ShopError as error:
                raise HTTPException(422, str(error)) from None

    if knowledge_base is not None:
        app.mount("/kb", StaticFiles(directory=KNOWLEDGE_STATIC_DIR, html=True), name="knowledge")
    # 放在最后：/api 下的路由优先匹配
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="console")
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ecom-console", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", default=RUNS_DIR, help="运行记录目录，默认 runs/")
    parser.add_argument("--db", default=DB_PATH, help="基础库，默认 shop/shop.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--no-kb", action="store_true", help="不加载知识库（启动更快，知识库一页不可用）")
    args = parser.parse_args(argv)

    import uvicorn

    knowledge_base = None
    if not args.no_kb:
        from ecom_agent_system.knowledge_base import KnowledgeBase

        knowledge_base = KnowledgeBase.load()
        knowledge_base.rankings("预热")  # 启动时加载向量模型，第一次检索不用等
    print(f"控制台已就绪：http://{args.host}:{args.port}")
    uvicorn.run(create_app(Runs(args.runs, args.db), knowledge_base), host=args.host, port=args.port, log_level="warning")
