"""ecom-shop：生成和查看模拟商城的数据，接入购物 agent。

    uv run ecom-shop build --products <ShopSimulator 的 fine_items_eval_train_all.json.gz>
    uv run ecom-shop stats
    uv run ecom-shop user U000123
    uv run ecom-shop orders U000123
    uv run ecom-shop order SO2610...
    uv run ecom-shop track SO2610...
    uv run ecom-shop check SO2610... 退货退款 七天无理由 [--opened]
    uv run ecom-shop ticket AS00000012

    uv run ecom-shop gateway --label grpo                                # 在 ShopSimulator 前面记下购物 agent 的购买
    uv run ecom-shop import-trajectories trajectories.jsonl --label grpo  # 把评测轨迹里的购买补记成历史订单
    uv run ecom-shop agent-orders                                        # 按购物 agent 汇总它买的单

查询默认在库里的“当前时间”查看，可以用 --now 换一个时刻：写具体时间，或者写 +3d、+12h、-2d 这样
相对当前时间的偏移，例如看几天后退款到没到。输出是 JSON，和以后 agent 工具的返回值一致。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from ecom_agent_system.shop.catalog import CategoryPolicy, load_items
from ecom_agent_system.shop.db import DB_PATH, TIME_FORMAT, connect, get_meta, parse
from ecom_agent_system.shop.purchases import agent_orders, import_trajectories, read_trajectories
from ecom_agent_system.shop.rules import PlatformRules
from ecom_agent_system.shop.service import Shop, ShopError
from ecom_agent_system.shop.world import build_world

OFFSET_UNITS = {"d": "days", "h": "hours", "m": "minutes"}


def _time(text):
    for pattern in (TIME_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"时间格式应为 YYYY-MM-DD HH:MM[:SS]，或 +3d、+12h 这样的偏移：{text}")


def resolve_now(text, world_now):
    """--now 可以写具体时间，也可以写相对库里当前时间的偏移，如 +3d、+12h、-30m。"""
    if not text:
        return world_now
    match = re.fullmatch(r"([+-])(\d+(?:\.\d+)?)([dhm])", text.strip())
    if match is None:
        return _time(text)
    sign, amount, unit = match.groups()
    offset = timedelta(**{OFFSET_UNITS[unit]: float(amount)})
    return world_now + offset if sign == "+" else world_now - offset


def build(args):
    if not args.products:
        sys.exit("需要用 --products 或环境变量 SHOPSIM_PRODUCTS 指定 ShopSimulator 的商品数据文件")
    items = load_items(args.products)
    categories = CategoryPolicy.load()
    unused = categories.unused_keys(item.get("category") or "" for item in items)
    if unused:
        print(f"注意：category_policy.yaml 里这些类目没有匹配到商品：{unused}", file=sys.stderr)
    target = Path(args.db)
    temporary = target.with_suffix(".building")
    temporary.unlink(missing_ok=True)
    conn = connect(temporary)
    stats = build_world(
        conn, items, rules=PlatformRules.load(), categories=categories,
        seed=args.seed, orders=args.orders, now=args.now, days=args.days,
    )
    conn.close()
    temporary.replace(target)  # 生成成功才替换旧库
    print(json.dumps({"db": str(target), **stats}, ensure_ascii=False, indent=2))


def stats(conn, now):
    def count(sql, *args):
        return {row[0]: row[1] for row in conn.execute(sql, args)}

    status_sql = (
        "SELECT status, COUNT(*) FROM (SELECT e.status FROM after_sales_events e"
        " WHERE e.time <= ? AND e.id = (SELECT e2.id FROM after_sales_events e2 WHERE e2.ticket_id = e.ticket_id"
        " AND e2.time <= ? ORDER BY e2.time DESC, e2.id DESC LIMIT 1)) GROUP BY status"
    )
    return {
        "world_now": get_meta(conn, "world_now"),
        "products": conn.execute("SELECT COUNT(*) FROM products").fetchone()[0],
        "users_by_tier": count("SELECT tier, COUNT(*) FROM users GROUP BY tier"),
        "users_by_zone": count("SELECT zone, COUNT(*) FROM users GROUP BY zone"),
        "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        "orders_by_source": count("SELECT source, COUNT(*) FROM orders GROUP BY source"),
        "cancelled_orders": conn.execute("SELECT COUNT(*) FROM orders WHERE cancelled_at IS NOT NULL").fetchone()[0],
        "sales_amount": round(conn.execute("SELECT SUM(total_amount) FROM orders").fetchone()[0] or 0, 2),
        "orders_by_zone": count("SELECT zone, COUNT(*) FROM orders GROUP BY zone"),
        "items_by_policy_class": count("SELECT COALESCE(policy_class, '普通商品'), COUNT(*) FROM order_items GROUP BY 1"),
        "shipments_by_scenario": count("SELECT scenario, COUNT(*) FROM shipments WHERE direction = '发货' GROUP BY scenario"),
        "after_sales_by_type": count("SELECT type || '（' || reason || '）', COUNT(*) FROM after_sales GROUP BY 1"),
        "after_sales_by_status": count(status_sql, now, now),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ecom-shop", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DB_PATH, help="数据库文件，默认 shop/shop.db")
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="从 ShopSimulator 商品数据生成模拟商城数据库")
    build_parser.add_argument("--products", default=os.environ.get("SHOPSIM_PRODUCTS"))
    build_parser.add_argument("--seed", type=int, default=42)
    build_parser.add_argument("--orders", type=int, default=3000)
    build_parser.add_argument("--now", type=_time, default=_time("2026-10-24 12:00:00"), help="模拟世界的“当前时间”")
    build_parser.add_argument("--days", type=int, default=60, help="订单分布在当前时间之前多少天内")
    for name, arguments in {
        "stats": [],
        "user": ["user_id"],
        "orders": ["user_id"],
        "order": ["order_id"],
        "track": ["order_id"],
        "check": ["order_id", "type", "reason"],
        "ticket": ["ticket_id"],
    }.items():
        command = commands.add_parser(name)
        for argument in arguments:
            command.add_argument(argument)
        command.add_argument("--now", help="查看哪个时刻的状态，默认是库里的当前时间")
        if name == "check":
            command.add_argument("--opened", action="store_true", help="商品已拆封")
            command.add_argument("--activated", action="store_true", help="商品已激活")

    gateway_parser = commands.add_parser("gateway", help="在 ShopSimulator 前面转发请求，把购物 agent 的购买记成订单")
    gateway_parser.add_argument("--label", required=True, help="这批购买来自哪个购物 agent，例如 grpo-step100")
    gateway_parser.add_argument(
        "--upstream", default=os.environ.get("SHOPSIM_UPSTREAM", "http://127.0.0.1:5700"), help="ShopSimulator 的地址"
    )
    gateway_parser.add_argument("--host", default="127.0.0.1")
    gateway_parser.add_argument("--port", type=int, default=5701)
    gateway_parser.add_argument("--now", help="模拟时钟从哪个时刻开始，默认是库里的当前时间")
    import_parser = commands.add_parser("import-trajectories", help="把购物 agent 评测轨迹里的购买补记成历史订单")
    import_parser.add_argument("paths", nargs="+", help="shopping-grpo 评测或采集写的 trajectories.jsonl")
    import_parser.add_argument("--label", required=True, help="这批购买来自哪个购物 agent")
    import_parser.add_argument("--days", type=float, default=20, help="购买时间分布在当前时间之前多少天内")
    import_parser.add_argument("--no-react", action="store_true", help="不模拟顾客收货以后的动作")
    report_parser = commands.add_parser("agent-orders", help="按购物 agent 汇总它买的单")
    report_parser.add_argument("--label", help="只看这个购物 agent")
    report_parser.add_argument("--now", help="统计到哪个时刻，默认是库里的当前时间")
    args = parser.parse_args(argv)

    if args.command == "build":
        build(args)
        return
    if not Path(args.db).exists():
        sys.exit(f"{args.db} 不存在，先运行 ecom-shop build")
    conn = connect(args.db)
    world_now = parse(get_meta(conn, "world_now"))
    try:
        now = resolve_now(getattr(args, "now", None), world_now)
    except argparse.ArgumentTypeError as error:
        sys.exit(str(error))
    if args.command == "gateway":
        from ecom_agent_system.shop.gateway import serve  # 其他命令不用加载 fastapi 和 uvicorn

        conn.close()
        serve(args.db, upstream=args.upstream, label=args.label, host=args.host, port=args.port, start=now)
        return
    shop = Shop(conn, seed=int(get_meta(conn, "seed", 0)))
    if args.command == "import-trajectories":
        if args.days <= 0:
            sys.exit("--days 必须为正数")
        result = import_trajectories(
            shop, read_trajectories(args.paths), label=args.label,
            start=world_now - timedelta(days=args.days), end=world_now - timedelta(hours=1),
            react_until=None if args.no_react else world_now,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    try:
        result = {
            "stats": lambda: stats(conn, now.strftime(TIME_FORMAT)),
            "user": lambda: shop.get_user(args.user_id),
            "orders": lambda: shop.list_orders(args.user_id, now=now),
            "order": lambda: shop.get_order(args.order_id, now=now),
            "track": lambda: shop.track_shipment(args.order_id, now=now),
            "check": lambda: shop.check_after_sales(
                args.order_id, args.type, args.reason, now=now, opened=args.opened, activated=args.activated
            ),
            "ticket": lambda: shop.get_after_sales(args.ticket_id, now=now),
            "agent-orders": lambda: agent_orders(shop, now, args.label),
        }[args.command]()
    except ShopError as error:
        result = {"error": str(error), "decision": error.decision.to_dict() if error.decision else None}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
