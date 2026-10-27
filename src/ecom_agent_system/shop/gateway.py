"""ShopSimulator 前面的订单网关。

shopping-grpo 的购物 agent（评测、数据采集）通过 HTTP 调用 ShopSimulator 的 /api/shop_agent。把它的
SHOPSIM_BASE_URL 指到网关，请求和响应原样转发；agent 点 Buy Now 买下商品时，网关把这次购买记成订单，
agent 一侧不用改代码。网关的时钟从库里的“当前时间”开始，跟着真实时间往前走；每记一单，就把库里的
“当前时间”推到这一刻。

    uv run ecom-shop gateway --label grpo-step100                          # 监听 5701，转发到 5700
    SHOPSIM_BASE_URL=http://127.0.0.1:5701 bash scripts/evaluate.sh grpo    # 在 shopping-grpo 仓库里运行

veRL 训练的 rollout 也走这个接口。训练时不要接网关，否则每条 rollout 的购买都会记成订单。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import timedelta
from urllib.request import Request, urlopen

from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse

from ecom_agent_system.shop.db import connect, ensure_agent_schema, fmt, get_meta, parse, set_meta
from ecom_agent_system.shop.purchases import Buyers, record_purchase
from ecom_agent_system.shop.service import Shop

log = logging.getLogger(__name__)


def http_sender(upstream, timeout=60):
    """把请求原样 POST 给 ShopSimulator，返回它的 JSON 响应。"""
    endpoint = upstream.rstrip("/") + "/api/shop_agent"

    def send(payload):
        request = Request(
            endpoint, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST"
        )
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    return send


def _task_id(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _task_from_session(session):
    """interact 返回的 idx 是 ShopSimulator 的会话名，形如 slot-3-8187，最后一段是任务编号。"""
    match = re.search(r"-(\d+)$", str(session or ""))
    return int(match.group(1)) if match else None


class Gateway:
    """转发请求，记住每个环境槽位在做哪个任务，买下商品时记订单。send 默认走 HTTP，测试时换成假的 ShopSimulator。"""

    def __init__(self, conn, *, label, send, start=None, shop=None):
        ensure_agent_schema(conn)
        self.conn = conn
        self.label = label
        self.send = send
        self.shop = shop or Shop(conn, seed=int(get_meta(conn, "seed", 0)))
        self.buyers = Buyers(conn)
        self.start = start or parse(get_meta(conn, "world_now"))
        self.started = time.monotonic()
        self.leases = {}  # env_idx → 任务编号
        self.recorded = []
        self.failures = []
        self.lock = threading.Lock()

    def now(self):
        return (self.start + timedelta(seconds=time.monotonic() - self.started)).replace(microsecond=0)

    def handle(self, payload):
        """转发一个请求，返回 (ShopSimulator 的响应, 记下的订单号或 None)。"""
        response = self.send(payload)
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict) or result.get("error"):
            return response, None
        action, env_idx = payload.get("action"), payload.get("env_idx")
        with self.lock:
            if action == "reset":
                self.leases[result.get("env_idx")] = _task_id(payload.get("idx"))
            elif action == "release_one":
                self.leases.pop(env_idx, None)
            elif action == "release_all":
                self.leases.clear()
            elif action == "interact" and result.get("done") and (result.get("purchase") or {}).get("asin"):
                task_id = self.leases.get(env_idx)
                if task_id is None:  # 网关是中途接上的，没见到这个槽位的 reset
                    task_id = _task_from_session(result.get("idx"))
                return response, self._record(result, task_id)
        return response, None

    def _record(self, result, task_id):
        moment = self.now()
        try:
            order_id = record_purchase(self.shop, self.buyers, result, now=moment, label=self.label, task_id=task_id)
            current = get_meta(self.conn, "world_now")
            if current is None or moment > parse(current):
                set_meta(self.conn, "world_now", fmt(moment))
            self.conn.commit()
        except Exception as error:  # 记不下来也不能打断 agent 的评测
            self.conn.rollback()
            log.exception("任务 %s 的购买没有记成订单", task_id)
            self.failures.append({"task_id": task_id, "asin": result["purchase"].get("asin"), "error": str(error)})
            return None
        self.recorded.append(order_id)
        log.info(
            "记下订单 %s：任务 %s，%s，%s 元",
            order_id, task_id, result.get("termination_reason"), result["purchase"].get("price"),
        )
        return order_id

    def status(self):
        with self.lock:
            return {
                "label": self.label,
                "clock": fmt(self.now()),
                "active_leases": len(self.leases),
                "recorded": len(self.recorded),
                "latest_orders": self.recorded[-10:],
                "failures": self.failures[-10:],
            }


def create_app(gateway):
    app = FastAPI(title="ShopSimulator 订单网关")

    @app.post("/api/shop_agent")
    def shop_agent(payload: dict = Body(...)):
        try:
            response, order_id = gateway.handle(payload)
        except OSError as error:  # 连不上 ShopSimulator，或它返回了 HTTP 错误
            return JSONResponse({"error": f"转发到 ShopSimulator 失败：{error}"}, status_code=502)
        return JSONResponse(response, headers={"X-Shop-Order-Id": order_id} if order_id else None)

    @app.get("/api/gateway")
    def status():
        return gateway.status()

    return app


def serve(db_path, *, upstream, label, host="127.0.0.1", port=5701, start=None):
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    conn = connect(db_path, check_same_thread=False)  # 请求在线程池里处理，写库由 Gateway 的锁串行化
    gateway = Gateway(conn, label=label, send=http_sender(upstream), start=start)
    log.info("订单网关 http://%s:%s → %s，标签 %s，模拟时钟从 %s 开始", host, port, upstream, label, fmt(gateway.start))
    uvicorn.run(create_app(gateway), host=host, port=port, log_level="warning")
