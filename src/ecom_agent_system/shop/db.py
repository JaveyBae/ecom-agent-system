"""SQLite 表结构和时间格式。

时间一律存成北京时间的 "YYYY-MM-DD HH:MM:SS" 字符串，按字符串比较就是按时间比较。
物流轨迹和售后进度在发生动作时就整条生成，包括未来的时间点；查询时只返回不晚于
“当前时间”的部分，所以同一份数据可以在任意时刻查看。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ecom_agent_system.shop.rules import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "shop" / "shop.db"
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE products (
    asin TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    shop_name TEXT,
    category TEXT NOT NULL,
    domain TEXT,
    price_min REAL,
    price_max REAL,
    options TEXT NOT NULL,                        -- JSON：{规格名: [{value, price, available}]}
    attributes TEXT NOT NULL,                     -- JSON 列表
    image TEXT,
    policy_class TEXT,                            -- 退货政策分类，如 食品、定作；NULL 为普通商品
    no_return_marked INTEGER NOT NULL DEFAULT 0,  -- 商品页标注了“不支持七天无理由退货”
    durable INTEGER NOT NULL DEFAULT 0,           -- 耐用商品（P-QA-02）
    source_split TEXT                             -- ShopSimulator 里的 train / eval
);

CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE,
    tier TEXT NOT NULL,
    province TEXT NOT NULL,
    city TEXT NOT NULL,
    district TEXT,
    address TEXT NOT NULL,
    zone TEXT NOT NULL,
    registered_at TEXT,
    persona TEXT                                  -- ShopSimulator 用户画像原文（JSON）
);

CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users,
    created_at TEXT NOT NULL,                     -- 付款时间
    total_amount REAL NOT NULL,
    insured INTEGER NOT NULL DEFAULT 0,           -- 投保了退货运费险
    receiver TEXT NOT NULL,
    phone TEXT NOT NULL,
    province TEXT NOT NULL,
    city TEXT NOT NULL,
    district TEXT,
    address TEXT NOT NULL,
    zone TEXT NOT NULL,
    cancelled_at TEXT,
    cancel_reason TEXT,
    source TEXT NOT NULL                          -- simulated：模拟生成；shop_agent：购物 agent 下的单
);

CREATE TABLE order_items (
    item_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders,
    asin TEXT NOT NULL REFERENCES products,
    title TEXT NOT NULL,                          -- 下单时的快照，商品信息以后变了也不影响售后
    category TEXT NOT NULL,
    options TEXT NOT NULL,                        -- JSON：下单时选的规格
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    amount REAL NOT NULL,
    policy_class TEXT,
    no_return_confirmed INTEGER NOT NULL DEFAULT 0  -- 下单时确认了“不支持七天无理由退货”
);

CREATE TABLE after_sales (
    ticket_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders,
    item_id TEXT NOT NULL REFERENCES order_items,
    type TEXT NOT NULL,                           -- 退货退款 / 换货 / 仅退款 / 延迟发货补偿
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    refund_amount REAL NOT NULL DEFAULT 0,
    return_fee REAL NOT NULL DEFAULT 0,
    fee_payer TEXT,
    rule_ids TEXT NOT NULL,                       -- JSON：受理时依据的平台规则
    evidence TEXT                                 -- 质量问题等需要的问题描述（P-QA-05）
);

CREATE TABLE after_sales_events (
    id INTEGER PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES after_sales,
    time TEXT NOT NULL,
    status TEXT NOT NULL,
    actor TEXT NOT NULL,                          -- 买家 / 商家 / 平台
    note TEXT NOT NULL
);

CREATE TABLE shipments (
    tracking_no TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders,
    ticket_id TEXT REFERENCES after_sales,
    direction TEXT NOT NULL,                      -- 发货 / 退货 / 换货
    carrier TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    zone TEXT NOT NULL,
    created_at TEXT NOT NULL,                     -- 填写物流单号的时间
    promised_by TEXT,                             -- 承诺送达日期（P-SHIP-08）
    scenario TEXT NOT NULL                        -- 模拟器选的情形，相当于标准答案，不展示给 agent
);

CREATE TABLE shipment_events (
    id INTEGER PRIMARY KEY,
    tracking_no TEXT NOT NULL REFERENCES shipments,
    time TEXT NOT NULL,
    code TEXT NOT NULL,
    location TEXT,
    description TEXT NOT NULL
);

CREATE INDEX order_items_by_order ON order_items(order_id);
CREATE INDEX orders_by_user ON orders(user_id, created_at);
CREATE INDEX shipments_by_order ON shipments(order_id);
CREATE INDEX shipment_events_by_tracking ON shipment_events(tracking_no, time);
CREATE INDEX after_sales_by_order ON after_sales(order_id);
CREATE INDEX after_sales_events_by_ticket ON after_sales_events(ticket_id, time);
"""

# 购物 agent 的购买记录。旧库里没有这张表，接入时再建，所以用 IF NOT EXISTS
AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_purchases (
    order_id TEXT PRIMARY KEY REFERENCES orders,
    label TEXT NOT NULL,                          -- 哪个购物 agent，例如模型或实验名
    task_id INTEGER,                              -- ShopSimulator 的任务编号
    attempt_index INTEGER,
    trajectory_id TEXT UNIQUE,                    -- 评测轨迹的 ID；网关实时记录的购买没有
    persona_matched INTEGER NOT NULL,             -- 任务的用户画像对上了库里的用户；没对上的按任务编号分配
    outcome TEXT,                                 -- Reward v3 的购买结果，如 gold_purchase、wrong_purchase
    reward REAL,
    reward_valid INTEGER,
    failed_gates TEXT NOT NULL,                   -- JSON：没通过的硬约束，如 category、budget
    instruction TEXT,                             -- 任务里的用户需求
    recorded_at TEXT NOT NULL,                    -- 记录时的真实时间（UTC）
    customer_actions TEXT                         -- JSON：导入时模拟的顾客收货后动作
);
CREATE INDEX IF NOT EXISTS agent_purchases_by_label ON agent_purchases(label);

-- 工单：转人工、催发货、催派送。状态看 work_order_events 里不晚于“当前时间”的最后一条
CREATE TABLE IF NOT EXISTS work_orders (
    work_order_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users,
    order_id TEXT REFERENCES orders,
    ticket_id TEXT REFERENCES after_sales,
    kind TEXT NOT NULL,                           -- 转人工 / 催发货 / 催派送
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_order_events (
    id INTEGER PRIMARY KEY,
    work_order_id TEXT NOT NULL REFERENCES work_orders,
    time TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS work_orders_by_user ON work_orders(user_id, created_at);

-- 订单信息的修改记录（目前只有收货地址），按时间生效，查询时取不晚于“当前时间”的最后一次
CREATE TABLE IF NOT EXISTS order_changes (
    id INTEGER PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders,
    time TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT NOT NULL
);

-- 审计日志：agent 的每次办理和每次被拦下的调用
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    time TEXT NOT NULL,                           -- 模拟时间
    logged_at TEXT NOT NULL,                      -- 真实时间（UTC）
    session TEXT,
    user_id TEXT,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,                      -- JSON
    outcome TEXT NOT NULL,                        -- 已执行 / 顾客未确认 / 政策不允许 / 越权 / 参数错误 / 重复请求 / 需转人工 / 出错
    detail TEXT
);

-- 售后对话的摘要，下次同一位顾客来时放进上下文
CREATE TABLE IF NOT EXISTS conversations (
    session TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users,
    started_at TEXT NOT NULL,
    summary TEXT NOT NULL,
    actions TEXT NOT NULL                         -- JSON：这次对话办成的事
);
CREATE INDEX IF NOT EXISTS conversations_by_user ON conversations(user_id, started_at);

-- 顾客备注：顾客自己交代的、关于自己的事（收货习惯、退款方式），agent 用 remember_note 记下，下次放进档案
CREATE TABLE IF NOT EXISTS customer_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users,
    created_at TEXT NOT NULL,
    session TEXT,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS customer_notes_by_user ON customer_notes(user_id, created_at);
"""


def connect(path=DB_PATH, *, check_same_thread=True):
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn):
    conn.executescript(SCHEMA)
    ensure_agent_schema(conn)


def ensure_agent_schema(conn):
    """接入 agent 以后加的表和列。旧库在打开时补上，已有的不动。"""
    conn.executescript(AGENT_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(after_sales)")}
    if "evidence" not in columns:
        conn.execute("ALTER TABLE after_sales ADD COLUMN evidence TEXT")


def fmt(moment):
    return moment.strftime(TIME_FORMAT)


def parse(text):
    return datetime.strptime(text, TIME_FORMAT) if text else None


def get_meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
