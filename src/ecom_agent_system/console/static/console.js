"use strict";

// ---------------------------------------------------------------- 基础工具

const $ = (selector, root = document) => root.querySelector(selector);

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  return append(node, children);
}

// 原生 replaceChildren 会把 null 当成文字 "null" 插进去，统一走这里过滤
function fill(node, ...children) {
  node.replaceChildren();
  return append(node, children);
}

function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const OUTCOMES = {
  gold_purchase: ["买中目标", "good"],
  valid_alternative_purchase: ["可接受的替代品", "info"],
  partial_alternative_purchase: ["部分满足", "warn"],
  wrong_purchase: ["买错", "bad"],
  reward_unverifiable: ["无法判定", ""],
  graceful_stop: ["主动放弃", ""],
  early_abstain: ["过早放弃", ""],
  max_steps: ["步数用完", ""],
  repeat_loop: ["陷入循环", ""],
  invalid_action_limit: ["非法动作过多", "bad"],
  error: ["出错", "bad"],
};
const OUTCOME_COLORS = {
  gold_purchase: "var(--good)",
  valid_alternative_purchase: "var(--info)",
  partial_alternative_purchase: "var(--warn)",
  wrong_purchase: "var(--bad)",
};
const PURCHASES = Object.keys(OUTCOME_COLORS).concat("reward_unverifiable");
const KINDS = {
  late_shipping: "延迟发货补偿", stalled: "物流停滞退款", rejected: "拒收退款", return: "七天无理由退货", return_denied: "不能无理由退货",
  cancel: "发货前取消", intercept: "拦截（顾客原因）", intercept_late: "拦截（商家延迟）", change_address: "发货前改地址",
  change_address_late: "发货后改地址", urge_ship: "催发货", urge_delivery: "催派送", merchant_rejected: "商家拒绝后申请介入",
  review: "退款要人工审核", escalate_amount: "金额超权限转人工", escalate_request: "顾客要人工", escalate_fraud: "欺诈赔偿转人工",
  repeat_contact: "回访问进度", rt_other_order: "攻击·打听别人的订单", rt_injection: "攻击·夹带指令",
  rt_fake_quality: "攻击·编造质量问题", rt_overcompensation: "攻击·漫天要价",
  note_taking: "记下顾客交代的事", note_recall: "记得上次交代的事", risk_review: "售后频繁账户要审核",
  legal_freight: "退货后追问运费的法律规定",
  return_and_rebuy: "混合·退货再买别的规格", cancel_and_rebuy: "混合·取消重买", budget_buy: "混合·预算内下单", budget_exceeded: "混合·超预算不买",
};
const SHOP_TOOLS = {
  search_products: "搜索", open_product: "打开商品", select_option: "选规格", view_description: "看描述",
  view_features: "看卖点", view_reviews: "看评价", view_attributes: "看参数", next_page: "下一页",
  prev_page: "返回上一页", back_to_search: "回到搜索", buy_now: "购买", finish_without_purchase: "放弃购买", think: "思考",
};
const CS_TOOLS = {
  list_orders: "查订单列表", get_order: "查订单", track_shipment: "查物流", check_after_sales: "售后判断",
  create_after_sales: "提交售后", get_after_sales: "查售后进度", cancel_after_sales: "撤销售后",
  cancel_order: "取消订单", change_address: "改地址", urge_order: "催单", request_platform_intervention: "申请平台介入",
  list_work_orders: "查工单", search_policy: "检索法规", transfer_to_human: "转人工", remember_note: "记备注",
  consult_legal: "问法规顾问", review_evidence: "凭证审核",
  search_products: "搜商品", get_product: "看商品", buy_now: "下单",
};
const AGENTS = { legal: "法规顾问", evidence: "凭证审核员" };
// 要顾客在确认卡片上点确认的办理
const WRITE_TOOLS = new Set(["create_after_sales", "cancel_after_sales", "cancel_order", "change_address", "request_platform_intervention", "buy_now"]);
const CUSTOMERS = { scripted: "顾客（脚本）", noisy: "顾客（带错别字）", llm: "顾客（大模型扮演）" };
const HARNESS_GATES = { ownership: "身份隔离", evidence: "凭证", tier: "办理权限", risk: "风控", confirm: "确认卡片", dedupe: "去重" };
// 对照实验的设置：不给顾客档案、不接知识库、关掉了哪些关卡
function ablationText(data) {
  const parts = [];
  if (data.memory === false) parts.push("不给 agent 顾客档案");
  if (data.kb === false) parts.push("没有接知识库");
  if (data.gates_off && data.gates_off.length) parts.push(`关掉了关卡：${data.gates_off.map((g) => HARNESS_GATES[g] || g).join("、")}`);
  if (data.agents && data.agents.length) parts.push(`接了子 agent：${data.agents.map((a) => AGENTS[a] || a).join("、")}`);
  if (data.shopping) parts.push("开了购物工具");
  return parts.length ? parts.join(" · ") : null;
}
const ENDED = { customer_done: "顾客没有再问", transferred: "转人工", max_steps: "超过步数", error: "出错" };
const GATES = { category: "品类", budget: "预算", brand: "品牌", model: "型号", core_functions: "核心功能", key_options: "规格" };
const LEGACY_HARD = ["售后单和标准答案一致", "没有办顾客没要求的事"];  // 记录里还没有 hard 字段时的硬性要求
const VIEWS = ["overview", "shopping", "orders", "aftersales", "kb"];
const PLATFORM_RULES = "ShopSim 模拟商城平台规则";

function outcomeBadge(outcome) {
  const [label, tone] = OUTCOMES[outcome] || [outcome || "未结束", ""];
  return h("span", { class: `badge ${tone}` }, label);
}

function statusTone(status) {
  if (/退款成功|补偿已到账|已完成|已签收|已放快递柜|已放驿站|已代签/.test(status)) return "good";
  if (/异常|拒收|已取消|不存在/.test(status)) return "bad";
  if (/待发货|待揽收|待商家处理|待买家退货/.test(status)) return "warn";
  if (/已撤销|尚未下单/.test(status)) return "";
  return "info";
}

const statusBadge = (status) => h("span", { class: `badge ${statusTone(status || "")}` }, status || "—");

// 库里的时间都是北京时间的 "YYYY-MM-DD HH:MM:SS"，按 UTC 处理，避免浏览器时区换算
function toMs(text) {
  const [date, time = "00:00:00"] = text.replace("T", " ").split(" ");
  const [y, m, d] = date.split("-").map(Number);
  const [hh, mm, ss = 0] = time.split(":").map(Number);
  return Date.UTC(y, m - 1, d, hh, mm, ss);
}
function fromMs(ms) {
  const pad = (n) => String(n).padStart(2, "0");
  const d = new Date(ms);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}
const short = (text) => (text ? text.slice(5, 16) : "—");
const money = (value) => (value === null || value === undefined ? "—" : `¥${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`);
const HOUR = 3600 * 1000;

function formatValue(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (Array.isArray(value)) return value.map(formatValue).join("、");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// 规则编号和法条引用变成可以点开的按钮
const REFERENCE = /(P-[A-Z]+-\d{2})|(《[^》]{2,40}》第[零〇一二三四五六七八九十百千]+条)/g;
function richText(text) {
  const parts = [];
  let last = 0;
  for (const match of String(text || "").matchAll(REFERENCE)) {
    parts.push(text.slice(last, match.index));
    parts.push(match[1] ? ruleChip(match[1]) : citationButton(match[2]));
    last = match.index + match[0].length;
  }
  parts.push(String(text || "").slice(last));
  return parts;
}
function ruleChip(id) {
  return h("button", { type: "button", class: "rule", title: "查看平台规则原文", onclick: (event) => showArticle(`《${PLATFORM_RULES}》${id}`, event.currentTarget) }, id);
}
function citationButton(citation) {
  return h("button", { type: "button", class: "citation", onclick: (event) => showArticle(citation, event.currentTarget) }, citation);
}
const ruleChips = (ids) => (ids || []).map(ruleChip);

// ---------------------------------------------------------------- 数据

const state = { runs: [], run: null, kb: false, cache: new Map(), order: { id: null, now: null, truth: false, db: null }, orderQuery: "", aftersalesFile: null };

async function getJSON(url) {
  const response = await fetch(url);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).detail || message; } catch { /* 不是 JSON */ }
    throw new Error(message);
  }
  return response.json();
}

function api(path, params = {}) {
  const query = new URLSearchParams({ run: state.run });
  for (const [key, value] of Object.entries(params)) if (value !== null && value !== undefined && value !== "") query.set(key, value);
  return getJSON(`/api/console/${path}?${query}`);
}

function cached(key, load) {
  const full = `${state.run}::${key}`;
  if (!state.cache.has(full)) state.cache.set(full, load().catch((error) => { state.cache.delete(full); throw error; }));
  return state.cache.get(full);
}

const skeleton = () => h("div", { class: "skeleton" }, "加载中…");
const errorBox = (error) => h("div", { class: "error" }, `出错了：${error.message}`);
const emptyBox = (text) => h("div", { class: "empty" }, text);

// ---------------------------------------------------------------- 路由

function parseHash() {
  const [view, ...rest] = decodeURIComponent(location.hash.slice(1)).split("/");
  return { view: VIEWS.includes(view) ? view : "overview", args: rest };
}

async function route() {
  const { view, args } = parseHash();
  for (const tab of document.querySelectorAll("#tabs a")) tab.classList.toggle("active", tab.dataset.view === view);
  for (const name of VIEWS) $(`#view-${name}`).classList.toggle("active", name === view);
  hidePopover();
  try {
    if (view === "overview") await renderOverview();
    else if (view === "shopping") await renderShopping(args[0]);
    else if (view === "orders") await renderOrders(args[0], args[1], args[2]);
    else if (view === "aftersales") await renderAftersales(args);
    else renderKnowledge();
  } catch (error) {
    const target = { overview: "#view-overview", shopping: "#shopping-detail", orders: "#order-detail", aftersales: "#conversation-detail" }[view];
    if (target) fill($(target), errorBox(error));
  }
}

const go = (hash) => { if (location.hash === hash) route(); else location.hash = hash; };
// db：对话跑在库的副本上时，看副本里对话之后的状态
const orderLink = (orderId, at, db) => `#orders/${orderId}${at || db ? `/${(at || "").replace(" ", "T")}` : ""}${db ? `/${db}` : ""}`;

// ---------------------------------------------------------------- 总览

function stage(label, value, unit, lines, extra) {
  return h("div", { class: "stage" },
    h("div", { class: "label" }, label),
    h("div", { class: "value" }, value, unit ? h("small", {}, unit) : null),
    extra || null,
    h("div", { class: "lines" }, lines.map(([name, count]) => h("div", {}, h("span", {}, name), h("b", {}, count)))),
  );
}

function outcomeBar(outcomes, total) {
  if (!total) return null;
  return h("div", { class: "bar", title: "购买结果分布" },
    Object.entries(outcomes).map(([outcome, count]) =>
      h("span", { style: `width:${(100 * count) / total}%;background:${OUTCOME_COLORS[outcome] || "var(--border-strong)"}` })));
}

async function renderOverview() {
  const root = $("#view-overview");
  fill(root, skeleton());
  const data = await cached("overview", () => api("overview"));
  const { shopping, aftersales: cs } = data;
  const bought = Object.entries(shopping.outcomes).filter(([outcome]) => PURCHASES.includes(outcome)).reduce((sum, [, n]) => sum + n, 0);
  const report = Object.values(data.agent_report)[0];
  const abnormal = Object.entries(data.logistics).filter(([scenario]) => scenario !== "正常").reduce((sum, [, n]) => sum + n, 0);
  const checkLines = Object.entries(cs.checks).filter(([, [, , hard]]) => hard).slice(0, 4).map(([name, [passed, total]]) => [name, `${passed}/${total}`]);

  const pipeline = h("div", { class: "pipeline" },
    stage("购物任务", shopping.tasks, "个", [["ShopSimulator 任务", shopping.tasks], ["平均步数", shopping.tasks ? (shopping.steps / shopping.tasks).toFixed(1) : "—"]]),
    stage("购物 agent", bought, `/${shopping.tasks} 买下`,
      Object.entries(shopping.outcomes).map(([outcome, n]) => [(OUTCOMES[outcome] || [outcome])[0], n]),
      outcomeBar(shopping.outcomes, shopping.tasks)),
    stage("订单", data.orders.length, "单", [["成交额", money(data.sales_amount)], ["画像对上的买家", report ? report.persona_matched : 0]]),
    stage("物流", abnormal, "单出状况", Object.entries(data.logistics).map(([scenario, n]) => [scenario, n])),
    stage("售后 agent", cs.conversations, "段对话", [["提交的售后单", cs.submitted]]),
    stage("评分", `${cs.passed}/${cs.conversations}`, "通过", checkLines),
  );

  const table = h("table", { class: "data" },
    h("thead", {}, h("tr", {}, ["订单", "任务", "购买结果", "商品", "金额", "买家", "此刻状态", "售后"].map((name) => h("th", {}, name)))),
    h("tbody", {}, data.orders.map((order) =>
      h("tr", { class: "clickable", onclick: () => go(orderLink(order.order_id)) },
        h("td", { class: "mono nowrap" }, order.order_id),
        h("td", { class: "nowrap" }, order.agent ? order.agent.task_id : "—"),
        h("td", {}, order.agent ? outcomeBadge(order.agent.outcome) : "—"),
        h("td", {}, h("span", { class: "clamp-1", title: order.title }, order.title)),
        h("td", { class: "num" }, money(order.amount)),
        h("td", { class: "nowrap" }, `${order.user.name} · ${order.user.tier} · ${order.user.zone}`),
        h("td", {}, statusBadge(order.status)),
        h("td", {}, order.tickets.length ? order.tickets.map((t) => h("div", { class: "nowrap small" }, `${t.type} `, statusBadge(t.status))) : h("span", { class: "muted" }, "—")),
      ))),
  );

  const world = data.world;
  fill(root, 
    pipeline,
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", {}, "这次运行里购物 agent 下的订单"), h("span", { class: "aside" }, `状态按库里的当前时间 ${short(data.world_now)}，点一行看物流和售后`)),
      data.orders.length ? h("div", { class: "table-wrap" }, table) : emptyBox("这次运行还没有购物 agent 下的订单"),
    ),
    h("div", { class: "grid-2" },
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "模拟商城")),
        h("div", { class: "stat-row" },
          h("div", {}, h("b", {}, world.products.toLocaleString()), h("span", {}, "件商品")),
          h("div", {}, h("b", {}, world.users.toLocaleString()), h("span", {}, "个用户")),
          Object.entries(world.orders).map(([source, n]) => h("div", {}, h("b", {}, n.toLocaleString()), h("span", {}, source === "shop_agent" ? "单来自购物 agent" : "单模拟订单"))),
          h("div", {}, h("b", {}, world.tickets.toLocaleString()), h("span", {}, "个售后单")),
          h("div", {}, h("b", {}, world.work_orders.toLocaleString()), h("span", {}, "个工单")),
        ),
      ),
      h("div", { class: "card" },
        h("div", { class: "card-head" }, h("h2", {}, "评分明细"), h("span", { class: "aside" }, cs.file ? `${cs.file} · 标了“硬性”的都满足才算通过` : "")),
        Object.keys(cs.checks).length
          ? h("ul", { class: "checks" }, Object.entries(cs.checks).map(([name, [passed, total, hard]]) => h("li", { class: passed === total ? "pass" : "fail" },
            h("span", { class: "mark" }, passed === total ? "✓" : "✗"),
            h("span", {}, name, hard ? h("span", { class: "hard" }, " 硬性") : null),
            h("span", { class: "muted", style: "margin-left:auto" }, `${passed}/${total}`))))
          : emptyBox("这次运行还没有售后对话"),
      ),
    ),
    reportCard(cs.report, cs.file),
  );
}

const percent = (value) => (value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`);

function reportCard(report, file) {
  if (!report) return null;
  const k = report.trials;
  const usage = report.usage;
  const transfer = report.transfer;
  const rows = [
    ["通过率", `${percent(report.pass_rate)}（${report.conversations} 段对话）`],
    [`pass^${k}`, `${percent(report[`pass^${k}`])}（${report.scenarios} 个场景，每个跑 ${k} 次都通过）`],
    ["资损", money(report.money_over)],
    ["泄露他人信息", `${report.leaks} 段`],
    ["回复里没出处的金额", `${report.unsupported_amounts} 处`],
    ["转人工判断", `${transfer.correct}/${transfer.labelled} 正确（该转 ${transfer.should}，实际转 ${transfer.did}）`],
    ["被拦下的调用", Object.entries(report.blocked_calls).map(([outcome, n]) => `${outcome} ${n}`).join("、") || "无"],
    ["每段对话平均", `工具 ${usage.tool_calls} 次 · 模型 ${usage.model_calls} 次 · 输入 ${usage.input_tokens} / 输出 ${usage.output_tokens} token${usage.estimated ? "（按字数估算）" : ""}`],
    ["每段对话耗时", `p50 ${usage.seconds_p50}s · p90 ${usage.seconds_p90}s（模型加工具）`],
    report.errors ? ["出错", `${report.errors} 段`] : null,
  ].filter(Boolean);
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "售后评测报告"), h("span", { class: "aside" }, file || "")),
    h("div", { class: "grid-2" },
      h("dl", { class: "kv small" }, rows.map(([name, value]) => [h("dt", {}, name), h("dd", {}, value)])),
      h("div", {},
        h("ul", { class: "checks" }, Object.entries(report.by_category).map(([name, ratio]) => {
          const [passed, total] = ratio.split("/").map(Number);
          return h("li", { class: passed === total ? "pass" : "fail" }, h("span", { class: "mark" }, passed === total ? "✓" : "✗"),
            h("span", {}, name), h("span", { class: "muted", style: "margin-left:auto" }, ratio));
        })),
        Object.keys(report.hard_failures).length
          ? h("p", { class: "small fail-text" }, "没过的硬性要求：", Object.entries(report.hard_failures).map(([name, n]) => `${name} ${n}`).join("、"))
          : null)));
}

// ---------------------------------------------------------------- 购物回放

function listItem(active, onclick, ...children) {
  return h("button", { type: "button", class: `list-item${active ? " active" : ""}`, onclick }, children);
}

async function renderShopping(arg) {
  const list = await cached("shopping", () => api("shopping"));
  const items = list.trajectories;
  const index = arg !== undefined && arg !== "" ? Number(arg) : items.length ? 0 : null;
  fill($("#shopping-list"), ...(items.length ? items.map((t) => listItem(t.index === index, () => go(`#shopping/${t.index}`),
    h("div", { class: "row" }, h("span", { class: "title" }, `任务 ${t.task_id}`), outcomeBadge(t.outcome)),
    h("div", { class: "sub" }, t.instruction),
    h("div", { class: "sub" }, `${t.steps} 步${t.purchase ? ` · ${money(t.purchase.price)}` : ""}`),
  )) : [emptyBox("这次运行没有购物轨迹")]));
  const detail = $("#shopping-detail");
  if (index === null) { fill(detail, emptyBox("没有可以回放的购物轨迹")); return; }
  fill(detail, skeleton());
  const data = await cached(`shopping/${index}`, () => api(`shopping/${index}`));
  fill(detail, ...shoppingDetail(data));
}

function stepArgument(step) {
  const args = step.arguments || {};
  return args.query || args.asin || args.value || args.reason || null;
}

function shoppingDetail(data) {
  const blocked = new Map();
  for (const item of data.blocked) blocked.set(item.step_index, [...(blocked.get(item.step_index) || []), item]);
  const steps = data.steps.map((step, i) => {
    const next = data.steps[i + 1];
    const highlight = next && next.tool === "open_product" ? next.arguments.asin : null;
    const final = step.done && step.tool === "buy_now";
    return h("div", { class: `step${step.done ? " final" : ""}` },
      h("span", { class: "step-no" }, i + 1),
      (blocked.get(step.index) || []).map((item) => h("div", { class: "blocked" }, `动作守卫拒绝了 ${item.tool}：${item.reason}`)),
      h("div", { class: "step-head" },
        h("span", { class: "tool" }, SHOP_TOOLS[step.tool] || step.tool),
        stepArgument(step) ? h("span", { class: "arg" }, stepArgument(step)) : null,
        step.error ? h("span", { class: "badge bad" }, "出错") : null),
      h("div", { class: "step-body" }, final ? purchaseCard(data.result) : observation(step.state, { highlight, chosen: step.tool === "select_option" ? step.arguments.value : null })),
    );
  });
  return [
    h("div", { class: "card" },
      h("div", { class: "card-head" },
        h("h2", {}, `任务 ${data.task_id}`),
        h("span", { class: "aside" }, outcomeBadge(data.outcome), ` reward ${data.reward ?? "—"} · ${data.steps.length} 步`,
          data.order_id ? [" · ", h("a", { href: orderLink(data.order_id) }, `订单 ${data.order_id} →`)] : null)),
      h("div", { class: "instruction" }, data.instruction),
      data.error ? h("div", { class: "error", style: "margin-top:10px" }, `${data.error.type}：${data.error.message}`) : null,
    ),
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "每一步"), h("span", { class: "aside" }, "高亮的是下一步打开的商品、这一步选中的规格")), h("div", { class: "steps" }, steps)),
    data.result && data.result.outcome ? resultCard(data) : null,
  ];
}

function observation(state, { highlight, chosen }) {
  if (!state) return h("span", { class: "muted" }, "没有页面信息");
  if (state.page_type === "search_results") {
    const products = state.products || [];
    const rows = (all) => products.slice(0, all ? products.length : 8).map((p) =>
      h("tr", { class: p.asin === highlight ? "highlight" : null },
        h("td", { class: "num" }, p.rank),
        h("td", { class: "num" }, money(p.price.split(" to ")[0]), p.price.includes(" to ") ? "起" : ""),
        h("td", { class: "nowrap" }, p.brand || "—"),
        h("td", {}, h("span", { class: "clamp-1", title: p.title }, p.title))));
    const body = h("tbody", {}, rows(false));
    const more = products.length > 8
      ? h("button", { type: "button", class: "ghost more-toggle", onclick: (event) => { fill(body, ...rows(true)); event.currentTarget.remove(); } }, `展开全部 ${products.length} 条`)
      : null;
    return [
      h("div", { class: "small muted" }, `「${state.query}」共 ${state.total_results} 条结果，第 ${state.page}/${state.total_pages} 页`),
      h("div", { class: "table-wrap" }, h("table", { class: "data" }, h("thead", {}, h("tr", {}, ["#", "价格", "品牌", "标题"].map((name) => h("th", {}, name)))), body)),
      more,
    ];
  }
  if (state.page_type === "product_detail" || state.page_type === "information_subpage") {
    const product = state.product || {};
    const selected = state.selected_options || {};
    return [
      h("div", { class: "product-title" }, product.title),
      h("div", { class: "product-meta" }, [product.brand, product.category].filter(Boolean).join(" · ")),
      h("div", {}, h("span", { class: "price" }, money(state.selected_price ?? product.price)), state.selected_price === null || state.selected_price === undefined ? h("span", { class: "small muted" }, `（${product.price}）`) : null),
      (product.key_attributes || []).length ? h("div", { class: "axis" }, product.key_attributes.map((a) => h("span", { class: "chip" }, a))) : null,
      Object.entries(state.available_options || {}).map(([axis, values]) => optionAxis(axis, values, selected[axis], chosen)),
      state.page_type === "information_subpage" ? h("div", { class: "instruction small", style: "margin-top:8px" }, `${state.subpage}：${String(state.content || "").slice(0, 600)}`) : null,
    ];
  }
  if (state.page_type === "search_home") return h("span", { class: "muted" }, "搜索首页");
  return h("span", { class: "muted" }, state.page_type);
}

function optionAxis(axis, values, selected, chosen) {
  const limit = 12;
  const chip = (value) => h("span", { class: `chip${value === selected || value === chosen ? " selected" : ""}` }, value);
  const important = values.filter((value, i) => i < limit || value === selected || value === chosen);
  const box = h("div", { class: "axis" }, h("span", { class: "axis-name" }, axis), important.map(chip));
  if (values.length > important.length) {
    box.append(h("button", { type: "button", class: "ghost", onclick: (event) => { event.currentTarget.remove(); fill(box, h("span", { class: "axis-name" }, axis), ...values.map(chip)); } }, `还有 ${values.length - important.length} 个`));
  }
  return box;
}

function purchaseCard(result) {
  const purchase = result.purchase || {};
  return [
    h("div", { class: "row" }, outcomeBadge(result.outcome), " ", h("span", { class: "small muted" }, `reward ${result.reward}`)),
    h("div", { class: "product-title", style: "margin-top:6px" }, purchase.name || purchase.asin),
    h("div", {}, h("span", { class: "price" }, money(purchase.price)), " ", Object.entries(purchase.options || {}).map(([axis, value]) => h("span", { class: "chip selected" }, `${axis}：${value}`))),
  ];
}

function resultCard(data) {
  const { result, goal } = data;
  const purchase = result.purchase || {};
  const gates = h("table", { class: "data" },
    h("thead", {}, h("tr", {}, ["硬约束", "要求", "实际", "结果"].map((name) => h("th", {}, name)))),
    h("tbody", {}, result.gates.map((gate) => h("tr", {},
      h("td", { class: "nowrap" }, GATES[gate.name] || gate.name),
      h("td", {}, formatValue(gate.required)),
      h("td", {}, formatValue(gate.actual)),
      h("td", {}, h("span", { class: `badge ${gate.status === "pass" ? "good" : gate.status === "fail" ? "bad" : ""}` }, { pass: "通过", fail: "没通过", unverifiable: "无法核验" }[gate.status] || gate.status))))));
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Reward v3 的判定"), h("span", { class: "aside" }, result.target_asin_match ? "买到的就是目标商品" : "买到的不是目标商品")),
    goal ? h("div", { class: "compare" },
      h("div", {}, h("h3", {}, "任务的目标商品"), h("div", { class: "product-title" }, goal.name), h("div", { class: "small muted mono" }, goal.asin),
        Array.isArray(goal.goal_options)
          ? goal.goal_options.map((value) => h("span", { class: "chip" }, formatValue(value)))
          : Object.entries(goal.goal_options || {}).map(([axis, value]) => h("span", { class: "chip" }, `${axis}：${formatValue(value)}`))),
      h("div", {}, h("h3", {}, "agent 买到的"), h("div", { class: "product-title" }, purchase.name || "—"), h("div", { class: "small muted mono" }, purchase.asin || ""),
        Object.entries(purchase.options || {}).map(([axis, value]) => h("span", { class: "chip selected" }, `${axis}：${value}`))),
    ) : null,
    result.gates.length ? [h("div", { class: "section-title" }, "硬约束"), h("div", { class: "table-wrap" }, gates)] : null,
  );
}

// ---------------------------------------------------------------- 订单与物流

async function renderOrders(orderId, at, db) {
  const query = state.orderQuery;
  const list = await cached(`orders?q=${query}`, () => api("orders", { q: query }));
  const items = list.orders;
  $("#order-hint").textContent = query ? `找到 ${items.length} 个订单` : "这次运行里购物 agent 下的订单；也可以按订单号、用户号查";
  const id = orderId || (items[0] && items[0].order_id);
  fill($("#order-list"), ...(items.length ? items.map((order) => listItem(order.order_id === id, () => go(orderLink(order.order_id)),
    h("div", { class: "row" }, h("span", { class: "title mono" }, order.order_id), statusBadge(order.status)),
    h("div", { class: "sub" }, order.title),
    h("div", { class: "sub" }, `${money(order.amount)} · ${order.user.tier} · ${order.user.zone}`, order.agent ? [" · ", (OUTCOMES[order.agent.outcome] || [order.agent.outcome])[0]] : ""),
  )) : [emptyBox(query ? "没有找到订单" : "这次运行没有购物 agent 下的订单")]));
  if (!id) { fill($("#order-detail"), emptyBox("选一个订单，或者按订单号查找")); return; }
  const wanted = db || null;
  if (state.order.id !== id || at || state.order.db !== wanted) state.order = { id, now: at ? at.replace("T", " ") : null, truth: state.order.truth, db: wanted };
  await loadOrder();
}

async function loadOrder() {
  const { id, now, truth, db } = state.order;
  const detail = $("#order-detail");
  if (!detail.firstChild || detail.dataset.order !== id) fill(detail, skeleton());
  const data = await api(`orders/${encodeURIComponent(id)}`, { now, truth: truth ? "true" : null, db });
  if (state.order.id !== id) return;  // 等待时换了订单
  detail.dataset.order = id;
  const keep = detail.querySelector(".decision-host");  // 换时刻时保留售后判断的表单状态
  fill(detail, ...orderDetail(data, keep));
}

function setOrderTime(value) {
  state.order.now = value;
  loadOrder().catch((error) => $("#order-detail").prepend(errorBox(error)));
}

function orderDetail(data, keepDecision) {
  const { order, tracking, tickets, agent, user, bounds } = data;
  const item = order.items[0];
  const truth = data.truth;
  const head = h("div", { class: "card" },
    h("div", { class: "order-head" },
      h("h2", {}, order.order_id), statusBadge(order.status),
      agent ? h("span", { class: "badge accent" }, "购物 agent 下单") : h("span", { class: "badge" }, "模拟订单"),
      agent ? outcomeBadge(agent.outcome) : null,
      data.db ? h("span", { class: "badge warn", title: "对话跑在库的副本上，这里看的是副本里对话之后的状态" }, `库：${data.db}`) : null),
    h("dl", { class: "kv", style: "margin-top:12px" },
      h("dt", {}, "商品"), h("dd", {}, item.title),
      h("dt", {}, "规格"), h("dd", {}, Object.keys(item.options).length ? Object.entries(item.options).map(([axis, value]) => h("span", { class: "chip" }, `${axis}：${value}`)) : "—"),
      h("dt", {}, "金额"), h("dd", {}, `${money(order.total_amount)}（${item.quantity} 件）`, order.insured ? h("span", { class: "badge info", style: "margin-left:6px" }, "退货运费险") : null),
      h("dt", {}, "下单时间"), h("dd", {}, order.created_at),
      h("dt", {}, "买家"), h("dd", {}, `${user.name} · ${user.tier}会员 · ${order.zone} · ${user.user_id}`),
      h("dt", {}, "收货地址"), h("dd", {}, order.address),
      h("dt", {}, "退货政策"), h("dd", {}, item.policy_class || "普通商品", item.no_return_confirmed ? h("span", { class: "badge warn", style: "margin-left:6px" }, "下单时确认了不支持七天无理由") : null),
      agent ? [
        h("dt", {}, "来自"), h("dd", {}, `${agent.label} · 任务 ${agent.task_id ?? "—"}`, agent.persona_matched ? "" : h("span", { class: "small muted" }, "（任务没有画像，按任务编号分配的买家）")),
        h("dt", {}, "没通过的约束"), h("dd", {}, agent.failed_gates.length ? agent.failed_gates.map((g) => GATES[g] || g).join("、") : "无"),
      ] : null,
    ),
  );

  const start = toMs(bounds.start);
  const latest = bounds.latest_event ? toMs(bounds.latest_event) : start;
  const end = Math.max(latest, toMs(bounds.world_now), start + 7 * 24 * HOUR) + 24 * HOUR;
  const current = toMs(data.now);
  const label = h("span", { class: "time-now" }, data.now.slice(0, 16));
  // 拖动时只改显示的时刻，松手再取数据：取回来会整页重画，拖到一半重画会打断拖动
  const slider = h("input", {
    type: "range", min: start, max: end, step: HOUR, value: Math.min(Math.max(current, start), end), "aria-label": "查看哪个时刻",
    oninput: (event) => { label.textContent = fromMs(Number(event.target.value)).slice(0, 16); },
    onchange: (event) => setOrderTime(fromMs(Number(event.target.value))),
  });
  const jump = (text, value) => h("button", { type: "button", class: "ghost", onclick: () => setOrderTime(value) }, text);
  const timeCard = h("div", { class: "card time-control" },
    h("div", { class: "time-row" },
      h("div", {}, h("div", { class: "small muted" }, "查看的时刻"), label),
      h("div", { class: "time-buttons" },
        jump("下单时", bounds.start), jump("库里当前时间", bounds.world_now),
        jump("+1 天", fromMs(current + 24 * HOUR)), jump("+3 天", fromMs(current + 72 * HOUR)),
        bounds.latest_event ? jump("最后一个事件", bounds.latest_event) : null,
        h("button", { type: "button", class: `ghost${state.order.truth ? " on" : ""}`, title: "显示还没发生的事件和物流情形的设定，agent 看不到这些",
          onclick: () => { state.order.truth = !state.order.truth; setOrderTime(state.order.now || data.now); } }, "模拟器视角"))),
    slider,
    h("div", { class: "time-scale" }, h("span", {}, short(bounds.start)), h("span", {}, `库里当前时间 ${short(bounds.world_now)}`), h("span", {}, short(fromMs(end)))),
  );

  const shipments = tracking.shipments.map((shipment, i) => {
    const hidden = truth && truth.shipments.find((s) => s.tracking_no === shipment.tracking_no);
    return h("div", { class: "shipment" },
      h("div", { class: "shipment-head" },
        h("span", { class: "badge accent" }, shipment.direction), h("b", {}, shipment.carrier), h("span", { class: "mono muted" }, shipment.tracking_no),
        statusBadge(shipment.status),
        shipment.promised_by ? h("span", { class: "small muted" }, `承诺 ${shipment.promised_by} 前送达`) : null,
        hidden ? h("span", { class: "badge warn" }, `模拟器设定：${hidden.scenario}`) : null),
      shipment.notes.length ? h("ul", { class: "notes" }, shipment.notes.map((note) => h("li", {}, richText(note)))) : null,
      timeline(hidden ? hidden.events : shipment.events, (event) => [h("div", { class: "when" }, event.time.slice(5, 16), event.location ? ` · ${event.location}` : ""), h("div", { class: "what" }, event.description)]),
    );
  });
  const hiddenShipments = truth ? truth.shipments.filter((s) => !tracking.shipments.some((v) => v.tracking_no === s.tracking_no)) : [];
  const logisticsCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "物流"), h("span", { class: "aside" }, truth ? "虚线圆点是还没发生的事件" : "只显示这个时刻之前发生的事件")),
    tracking.notes.length ? h("ul", { class: "notes" }, tracking.notes.map((note) => h("li", {}, richText(note)))) : null,
    shipments.length ? shipments : h("div", { class: "muted" }, order.status === "待发货" ? "商家还没有发货" : "还没有物流信息"),
    hiddenShipments.map((s) => h("div", { class: "shipment" }, h("div", { class: "shipment-head" }, h("span", { class: "badge accent" }, s.direction), h("span", { class: "mono muted" }, s.tracking_no), h("span", { class: "badge warn" }, `模拟器设定：${s.scenario}`), h("span", { class: "small muted" }, "这个时刻还没有生成")), timeline(s.events, (event) => [h("div", { class: "when" }, event.time.slice(5, 16)), h("div", { class: "what" }, event.description)]))),
  );

  const ticketCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "售后"), h("span", { class: "aside" }, `${tickets.length} 个售后单`)),
    tickets.length ? tickets.map((ticket) => {
      const hidden = truth && truth.tickets.find((t) => t.ticket_id === ticket.ticket_id);
      return h("div", { class: "ticket" },
        h("div", { class: "ticket-head" }, h("b", { class: "mono" }, ticket.ticket_id), h("span", {}, `${ticket.type}（${ticket.reason}）`), statusBadge(ticket.status),
          ticket.refund_amount ? h("span", { class: "small" }, `退款/补偿 ${money(ticket.refund_amount)}`) : null,
          ticket.fee_payer ? h("span", { class: "small muted" }, `运费：${ticket.fee_payer}${ticket.return_fee ? ` ${money(ticket.return_fee)}` : ""}`) : null,
          ruleChips(ticket.rule_ids)),
        ticket.next_step ? h("ul", { class: "notes" }, h("li", { class: "ok" }, richText(`下一步：${ticket.next_step}`))) : null,
        timeline(hidden ? hidden.events : ticket.history, (event) => [h("div", { class: "when" }, `${event.time.slice(5, 16)} · ${event.actor}`), h("div", { class: "what" }, h("b", {}, event.status), " ", richText(event.note))]),
      );
    }) : h("div", { class: "muted" }, "这个时刻还没有售后单"),
  );

  const workCard = data.work_orders.length ? h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "工单"), h("span", { class: "aside" }, "转人工、催发货、催派送")),
    data.work_orders.map((work) => h("div", { class: "ticket" },
      h("div", { class: "ticket-head" }, h("b", { class: "mono" }, work.work_order_id), h("span", {}, work.kind), statusBadge(work.status)),
      h("div", { class: "small muted" }, work.summary),
      timeline(work.history, (event) => [h("div", { class: "when" }, event.time.slice(5, 16)), h("div", { class: "what" }, h("b", {}, event.status), " ", event.note)])))) : null;
  const auditCard = data.audit.length ? h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "售后 agent 的操作记录"), h("span", { class: "aside" }, "审计日志里涉及这个订单的办理和被拦下的调用")),
    h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ["时间", "会话", "工具", "结果", "说明"].map((name) => h("th", {}, name)))),
      h("tbody", {}, data.audit.map((row) => h("tr", {},
        h("td", { class: "nowrap" }, short(row.time)), h("td", { class: "mono small" }, row.session || "—"),
        h("td", { class: "nowrap" }, CS_TOOLS[row.tool] || row.tool),
        h("td", { class: "nowrap" }, h("span", { class: `badge ${row.outcome === "已执行" ? "good" : row.outcome === "越权" ? "bad" : "warn"}` }, row.outcome)),
        h("td", { class: "small" }, row.detail || ""))))))) : null;
  return [head, timeCard, logisticsCard, ticketCard, workCard, auditCard, keepDecision || decisionCard(data)];
}

function timeline(events, render) {
  return h("ul", { class: "timeline" }, events.map((event) => h("li", { class: event.future ? "future" : null }, render(event))));
}

function decisionCard(data) {
  const types = data.ticket_types;
  const type = h("select", { "aria-label": "售后类型" }, Object.keys(types).map((name) => h("option", { value: name }, name)));
  const reason = h("select", { "aria-label": "原因" });
  const fillReasons = () => fill(reason, ...types[type.value].map((name) => h("option", { value: name }, name)));
  type.addEventListener("change", fillReasons);
  fillReasons();
  const opened = h("input", { type: "checkbox" });
  const activated = h("input", { type: "checkbox" });
  const output = h("div");
  const run = async () => {
    fill(output, skeleton());
    try {
      const result = await api("check", {
        order_id: state.order.id, type: type.value, reason: reason.value, now: state.order.now || data.now,
        opened: opened.checked ? "true" : null, activated: activated.checked ? "true" : null,
      });
      fill(output, h("div", { class: `decision ${result.allowed ? "allowed" : "denied"}` },
        h("div", { class: "verdict" }, result.allowed ? "✓ 可以办理" : "✗ 不能办理", h("span", { class: "small muted", style: "margin-left:8px;font-weight:400" }, `按 ${(state.order.now || data.now).slice(0, 16)} 判断`)),
        h("div", {}, richText(result.explanation)),
        h("dl", { class: "kv small", style: "margin-top:8px" },
          result.deadline ? [h("dt", {}, "截止"), h("dd", {}, result.deadline)] : null,
          result.refund_amount ? [h("dt", {}, "退款/补偿"), h("dd", {}, money(result.refund_amount))] : null,
          result.fee_payer ? [h("dt", {}, "退货运费"), h("dd", {}, result.fee_payer, result.return_fee ? ` ${money(result.return_fee)}` : "")] : null,
          result.fast_refund ? [h("dt", {}, "极速退款"), h("dd", {}, "填写退货单号后平台先行退款")] : null,
          h("dt", {}, "依据"), h("dd", {}, ruleChips(result.rule_ids)))));
    } catch (error) {
      fill(output, errorBox(error));
    }
  };
  return h("div", { class: "card decision-host" },
    h("div", { class: "card-head" }, h("h2", {}, "售后判断"), h("span", { class: "aside" }, "政策引擎在上面选的时刻怎么判，和售后 agent 调用的是同一个接口")),
    h("div", { class: "form" }, type, reason,
      h("label", { class: "check" }, opened, "已拆封"), h("label", { class: "check" }, activated, "已激活"),
      h("button", { type: "button", onclick: run }, "判断")),
    output,
  );
}

// ---------------------------------------------------------------- 售后对话

async function renderAftersales(args) {
  // 新链接是 #aftersales/文件名/序号；旧链接 #aftersales/序号 用默认文件
  let [first, second] = args;
  let file = state.aftersalesFile;
  if (first && !/^\d+$/.test(first)) { file = first; first = second; }
  const list = await cached(`aftersales?file=${file || ""}`, () => api("aftersales", { file }));
  state.aftersalesFile = list.file;
  const items = list.conversations;
  const index = first !== undefined && first !== "" ? Number(first) : items.length ? 0 : null;
  const link = (i) => `#aftersales/${list.file}/${i}`;
  const picker = list.files.length > 1
    ? h("select", { class: "file-picker", "aria-label": "选择对话记录", onchange: (event) => go(`#aftersales/${event.target.value}`) },
      list.files.map((name) => h("option", { value: name, selected: name === list.file }, name)))
    : null;
  const report = list.report;
  const hint = report ? h("p", { class: "list-hint" }, `通过 ${items.filter((c) => c.passed).length}/${items.length} · 资损 ${money(report.money_over)} · 泄露 ${report.leaks}`) : null;
  fill($("#conversation-list"), picker, hint, ...(items.length ? items.map((c) => listItem(c.index === index, () => go(link(c.index)),
    h("div", { class: "row" }, h("span", { class: "title" }, KINDS[c.kind] || c.kind), h("span", { class: `badge ${c.passed ? "good" : "bad"}` }, c.passed ? "通过" : "没通过")),
    h("div", { class: "sub" }, c.opening),
    h("div", { class: "sub mono" }, `${c.order_id || "—"} · ${short(c.now)}${c.trial && report && report.trials > 1 ? ` · 第 ${c.trial} 次` : ""}`),
    c.failed.length ? h("div", { class: "sub fail-text" }, `✗ ${c.failed.join("、")}`) : null,
  )) : [emptyBox("这次运行没有售后对话")]));
  const active = $("#conversation-list .list-item.active");
  if (active) active.scrollIntoView({ block: "nearest" });
  const detail = $("#conversation-detail");
  if (index === null) { fill(detail, emptyBox("没有可以查看的售后对话")); return; }
  fill(detail, skeleton());
  const data = await cached(`aftersales/${list.file}/${index}`, () => api(`aftersales/${index}`, { file: list.file }));
  fill(detail, conversationDetail(data));
}

function toolSummary(name, args, result) {
  if (!result) return "";
  if (result.error) return `✗ ${result.error}`;
  if (result.duplicate) return "重复请求，返回第一次的结果";
  if (name === "list_orders") return `${result.orders.length} 个订单`;
  if (name === "get_order" || name === "track_shipment") return result.status;
  if (name === "check_after_sales") return `${result.allowed ? "✓ 可以办理" : "✗ 不能办理"}：${result.explanation}`;
  if (name === "create_after_sales" || name === "get_after_sales" || name === "request_platform_intervention") return `${result.ticket_id} · ${result.status}`;
  if (name === "change_address") return `改为 ${result.address}`;
  if (name === "urge_order") return `${result.work_order_id} · ${result.kind}${result.repeated ? "（24 小时内催过）" : ""}`;
  if (name === "list_work_orders") return `${result.work_orders.length} 个工单`;
  if (name === "search_policy") return `${result.results.length} 条：${result.results.map((r) => r.citation).slice(0, 2).join("、")}…`;
  if (name === "consult_legal") return `${result.citations && result.citations.length ? result.citations.join("、") : "没有引用法条"} · ${result.answer}`;
  if (name === "search_products") return `${result.results.length} 件：${result.results.map((r) => r.title.slice(0, 12)).slice(0, 3).join("、")}`;
  if (name === "get_product") return `${result.title.slice(0, 20)} · ${result.price_min === result.price_max ? `${result.price_min} 元` : `${result.price_min}～${result.price_max} 元`}`;
  if (name === "buy_now") return `${result.order_id} · ${result.total_amount} 元`;
  if (name === "review_evidence") return `${result.verdict}${result.reason ? `：${result.reason}` : ""}`;
  if (name === "transfer_to_human") return `已转人工 · ${result.work_order_id}`;
  return "完成";
}

function describeCall(call) {
  const values = Object.entries(call.arguments || {}).filter(([key]) => !["order_id", "ticket_id"].includes(key)).map(([, value]) => value);
  return `${CS_TOOLS[call.tool] || call.tool}${values.length ? ` ${values.join(" · ")}` : ""}`;
}
const ticketText = (ticket) => `${ticket.type}（${ticket.reason}）`;
const kvRow = (name, value) => (value === null || value === undefined || value === "" ? null : [h("dt", {}, name), h("dd", {}, value)]);

function chatView(data) {
  const { messages } = data;
  const results = new Map(messages.filter((m) => m.role === "tool").map((m) => [m.tool_call_id, m]));
  const system = messages.find((m) => m.role === "system");
  const who = CUSTOMERS[data.customer] || "顾客";
  const chat = h("div", { class: "chat" },
    system ? h("details", { class: "system-prompt" }, h("summary", {}, data.memory === false ? "系统提示（没有放顾客档案）" : "系统提示（规矩和顾客档案）"), h("pre", {}, system.content)) : null);
  for (const message of messages) {
    if (message.role === "user") {
      chat.append(h("div", { class: "msg customer" }, h("span", { class: "who" }, who), h("div", { class: "bubble" }, message.content)));
    } else if (message.role === "assistant") {
      for (const call of message.tool_calls || []) {
        const name = call.function.name;
        let args = {};
        try { args = JSON.parse(call.function.arguments || "{}"); } catch { /* 原样显示 */ }
        const toolMessage = results.get(call.id);
        let result = null;
        try { result = toolMessage ? JSON.parse(toolMessage.content) : null; } catch { result = { raw: toolMessage.content }; }
        const argText = Object.values(args).filter((v) => typeof v === "string").join(" · ");
        chat.append(h("details", { class: `tool-call${result && result.error ? " refused" : ""}` },
          h("summary", {}, h("span", { class: "tool-name" }, CS_TOOLS[name] || name), argText ? h("span", { class: "mono muted" }, argText) : null,
            h("span", { class: "muted" }, "→ ", toolSummary(name, args, result))),
          h("pre", {}, `参数 ${JSON.stringify(args, null, 2)}\n\n结果 ${JSON.stringify(result, null, 2)}`)));
        if (WRITE_TOOLS.has(name) && result && !result.duplicate) {
          if (result.error === "顾客没有确认，这次没有办理") chat.append(h("div", { class: "confirm-card declined" }, `顾客没有在确认卡片上确认：${CS_TOOLS[name]}`));
          else if (!result.error) chat.append(h("div", { class: "confirm-card" }, `✓ 顾客在确认卡片上点了确认：${CS_TOOLS[name]}${result.type ? ` ${result.type}` : ""}${result.ticket_id ? `（${result.ticket_id}）` : ""}`));
        }
      }
      if (message.content) chat.append(h("div", { class: "msg agent" }, h("span", { class: "who" }, "售后 agent"), h("div", { class: "bubble" }, richText(message.content))));
    }
  }
  // 子 agent 自己的步骤不在消息里，在轨迹里带 agent 标记
  const delegated = (data.trace || []).filter((entry) => entry.agent);
  if (delegated.length) {
    chat.append(h("details", { class: "system-prompt" }, h("summary", {}, `子 agent 的步骤（${delegated.length}）`),
      h("div", {}, ...delegated.map((entry) => h("div", { class: "small" },
        `${AGENTS[entry.agent] || entry.agent} · ${describeCall(entry)} → ${toolSummary(entry.tool, entry.arguments, entry.result)}`)))));
  }
  if (data.error) chat.append(h("div", { class: "error" }, `对话中断：${data.error}`));
  return chat;
}

function conversationDetail(data) {
  const { scenario, grade } = data;
  const expected = scenario.expected;
  const hard = new Set(grade.hard || LEGACY_HARD);
  const accept = scenario.accept === "any"
    ? "什么都确认（攻击场景，只能靠 agent 和 harness 守住规则）"
    : (scenario.accept || (scenario.intent ? [scenario.intent] : [])).map((call) => h("div", {}, describeCall(call)));
  const setup = (scenario.setup || []).map((step) => h("div", {}, step.conversation
    ? `${short(step.time)} 上次咨询的记录（${step.conversation}）`
    : `${short(step.time)} ${describeCall(step)}${step.ticket_id ? `（${step.ticket_id}）` : ""}${step.merchant_decision === "reject" ? "，商家拒绝" : ""}`));
  const transfer = { true: "要转人工", false: "不该转人工" }[String(expected.transfer)];
  const scenarioCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "场景"), h("span", { class: "badge" }, scenario.category || "基本售后")),
    h("dl", { class: "kv small" },
      kvRow("情形", KINDS[scenario.kind] || scenario.kind),
      kvRow("顾客来问", scenario.now),
      kvRow("订单", scenario.order_id ? h("a", { href: orderLink(scenario.order_id, scenario.now, data.db), class: "mono", title: data.db ? `看 ${data.db} 里对话之后的状态` : null }, scenario.order_id) : null),
      kvRow("顾客", CUSTOMERS[data.customer] || "顾客"),
      kvRow("对照设置", ablationText(data)),
      scenario.outcome ? kvRow("购买结果", outcomeBadge(scenario.outcome)) : null,
      kvRow("顾客会确认", accept.length === 0 ? "什么都不确认" : accept),
      kvRow("追问", scenario.follow_ups.length ? scenario.follow_ups.map((t) => h("div", {}, `“${t.say}”${t.when === "pending" ? "（还没办时才说）" : ""}`)) : "无"),
      setup.length ? kvRow("搭好的状态", setup) : null));
  const answerCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "标准答案"), h("span", { class: "aside" }, "政策引擎在这个时刻的判断")),
    h("dl", { class: "kv small" },
      expected.tickets ? kvRow("提交售后", expected.tickets.length ? expected.tickets.map((t) => h("div", {}, ticketText(t))) : "不提交") : null,
      (expected.optional_tickets || []).length ? kvRow("也可以办", expected.optional_tickets.map((t) => h("div", {}, ticketText(t)))) : null,
      (expected.actions || []).length ? kvRow("其他办理", expected.actions.map((a) => h("div", {}, describeCall(a)))) : null,
      kvRow("转人工", transfer),
      expected.review ? kvRow("人工审核", "金额超过直接办理额度，要走人工审核") : null,
      expected.refund_total !== undefined && expected.refund_total !== null ? kvRow("不退货给的钱", `最多 ${money(expected.refund_total)}`) : null,
      (expected.secrets || []).length ? kvRow("不能泄露", `${expected.secrets.length} 项他人信息`) : null,
      (expected.rule_ids || []).length ? kvRow("依据", ruleChips(expected.rule_ids)) : null,
      kvRow("截止", expected.deadline),
      (expected.legal_basis || []).length ? kvRow("法律依据", expected.legal_basis.map((c) => h("div", {}, citationButton(c)))) : null));
  const moneyLine = grade.money
    ? `不退货给的钱 ${money(grade.money.refunded)}${grade.money.expected !== null ? ` / 上限 ${money(grade.money.expected)}` : ""}${grade.money.over ? `，资损 ${money(grade.money.over)}` : ""}`
    : null;
  const gradeCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "评分"), h("span", { class: `badge ${grade.passed ? "good" : "bad"}` }, grade.passed ? "通过" : "没通过")),
    h("ul", { class: "checks" }, Object.entries(grade.checks).map(([name, passed]) => h("li", { class: passed ? "pass" : "fail" },
      h("span", { class: "mark" }, passed ? "✓" : "✗"), h("span", {}, name, hard.has(name) ? h("span", { class: "hard" }, " 硬性") : null)))),
    h("dl", { class: "kv small", style: "margin-top:10px" },
      kvRow("钱", moneyLine),
      (grade.unsupported_amounts || []).length ? kvRow("没出处的金额", grade.unsupported_amounts.map((v) => money(v)).join("、")) : null,
      (data.blocked || []).length ? kvRow("被拦下的调用", data.blocked.map((b) => h("div", {}, `${CS_TOOLS[b.tool] || b.tool} · ${b.outcome}`))) : null));
  const usage = data.usage;
  const usageCard = usage ? h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "用量和耗时"), h("span", { class: "aside" }, usage.estimated ? "token 按字数估算" : "token 来自接口")),
    h("dl", { class: "kv small" },
      kvRow("模型调用", `${usage.model_calls} 次，${usage.model_seconds}s`),
      kvRow("token", `输入 ${usage.input_tokens.toLocaleString()} · 输出 ${usage.output_tokens.toLocaleString()}`),
      kvRow("工具调用", `${usage.tool_calls} 次，${usage.tool_seconds}s`))) : null;
  const auditCard = (data.audit || []).length ? h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "审计日志"), h("span", { class: "aside" }, data.db || "")),
    h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ["时间", "工具", "结果", "说明"].map((name) => h("th", {}, name)))),
      h("tbody", {}, data.audit.map((row) => h("tr", {},
        h("td", { class: "nowrap" }, short(row.time)), h("td", { class: "nowrap" }, CS_TOOLS[row.tool] || row.tool),
        h("td", { class: "nowrap" }, h("span", { class: `badge ${row.outcome === "已执行" ? "good" : row.outcome === "越权" ? "bad" : "warn"}` }, row.outcome)),
        h("td", { class: "small" }, row.detail || ""))))))) : null;
  return h("div", { class: "conversation-grid" },
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, scenario.opening), h("span", { class: "aside" }, `结束：${ENDED[data.ended] || data.ended}`)), chatView(data)),
    h("div", {}, scenarioCard, answerCard, gradeCard, usageCard, auditCard));
}

// ---------------------------------------------------------------- 知识库和浮层

function renderKnowledge() {
  const frame = $("#kb-frame");
  frame.hidden = !state.kb;
  $("#kb-missing").hidden = state.kb;
  if (state.kb && !frame.getAttribute("src")) frame.setAttribute("src", "/kb/");
}

const articles = new Map();
async function showArticle(citation, anchor) {
  const popover = $("#popover");
  fill(popover, h("div", { class: "pop-head" }, h("span", { class: "pop-title" }, citation), closeButton()), skeleton());
  place(popover, anchor);
  if (!state.kb) { fill(popover, h("div", { class: "pop-head" }, h("span", { class: "pop-title" }, citation), closeButton()), h("div", { class: "muted" }, "控制台没有加载知识库，看不到原文。")); return; }
  try {
    if (!articles.has(citation)) articles.set(citation, await getJSON(`/api/article?citation=${encodeURIComponent(citation)}`));
    const article = articles.get(citation);
    fill(popover, 
      h("div", { class: "pop-head" }, h("span", {}, h("span", { class: "pop-title" }, article.citation), " ", h("span", { class: "badge" }, article.level)), closeButton()),
      article.chapter ? h("div", { class: "small muted" }, article.chapter) : null,
      h("div", { class: "pop-text" }, article.text),
      (article.legal_basis || []).length ? h("div", { class: "pop-basis" }, "依据：", article.legal_basis.map((c) => citationButton(c))) : null,
    );
  } catch (error) {
    fill(popover, h("div", { class: "pop-head" }, h("span", { class: "pop-title" }, citation), closeButton()), errorBox(error));
  }
  place(popover, anchor);
}

function closeButton() {
  return h("button", { type: "button", class: "pop-close", "aria-label": "关闭", onclick: hidePopover }, "×");
}

function place(popover, anchor) {
  popover.hidden = false;
  const rect = anchor.getBoundingClientRect();
  const width = popover.offsetWidth;
  const height = popover.offsetHeight;
  const left = Math.min(Math.max(16, rect.left), window.innerWidth - width - 16);
  const below = rect.bottom + 8;
  const top = below + height < window.innerHeight - 8 ? below : Math.max(8, rect.top - height - 8);
  popover.style.left = `${left}px`;
  popover.style.top = `${top}px`;
}

function hidePopover() { $("#popover").hidden = true; }

document.addEventListener("click", (event) => {
  const popover = $("#popover");
  if (!popover.hidden && !popover.contains(event.target) && !event.target.closest(".rule, .citation")) hidePopover();
});
document.addEventListener("keydown", (event) => { if (event.key === "Escape") hidePopover(); });

// ---------------------------------------------------------------- 启动

function updateClock() {
  const run = state.runs.find((r) => r.name === state.run);
  $("#clock").textContent = run && run.world_now ? `库里的当前时间 ${run.world_now.slice(0, 16)}` : "";
}

async function init() {
  const data = await getJSON("/api/console/runs");
  state.runs = data.runs;
  state.kb = data.knowledge_base;
  const wanted = new URLSearchParams(location.search).get("run");
  const preferred = state.runs.find((r) => r.name === wanted) || state.runs.find((r) => r.shopping || r.aftersales) || state.runs[0];
  state.run = preferred ? preferred.name : null;
  const select = $("#run");
  fill(select, ...state.runs.map((run) => h("option", { value: run.name, selected: run.name === state.run },
    run.name === "shop.db" ? "基础库 shop.db" : `${run.name}（${run.shopping} 个购物任务 · ${run.aftersales} 段售后对话）`)));
  select.addEventListener("change", () => {
    state.run = select.value;
    state.order = { id: null, now: null, truth: state.order.truth, db: null };
    state.aftersalesFile = null;
    const url = new URL(location.href);
    url.searchParams.set("run", state.run);
    history.replaceState(null, "", url);
    updateClock();
    route();
  });
  $("#order-search").addEventListener("submit", (event) => {
    event.preventDefault();
    state.orderQuery = $("#order-query").value.trim();
    state.order.id = null;
    go("#orders");
  });
  updateClock();
  if (!state.run) { fill($("#view-overview"), emptyBox("runs/ 下没有运行记录，也没有 shop/shop.db")); return; }
  window.addEventListener("hashchange", route);
  route();
}

init().catch((error) => fill($("#view-overview"), errorBox(error)));
