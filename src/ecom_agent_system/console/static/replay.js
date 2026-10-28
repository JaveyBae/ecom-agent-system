"use strict";

// 售后对话回放：/api/console/replay 把一段对话拆成帧，这里按同一个进度把帧画到手机和开发者面板上。
// 对照时两份记录（比如新旧 harness）用同一个进度一起播放。

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
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

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
const TOOLS = {
  list_orders: "查订单列表", get_order: "查订单", track_shipment: "查物流", check_after_sales: "售后判断",
  create_after_sales: "提交售后", get_after_sales: "查售后进度", cancel_after_sales: "撤销售后",
  cancel_order: "取消订单", change_address: "改地址", urge_order: "催单", request_platform_intervention: "申请平台介入",
  list_work_orders: "查工单", search_policy: "检索法规", transfer_to_human: "转人工", remember_note: "记备注",
  consult_legal: "问法规顾问", review_evidence: "凭证审核", search_products: "搜商品", get_product: "看商品", buy_now: "下单",
};
const ENDED = { customer_done: "顾客没有再问", transferred: "已转人工", max_steps: "超过步数", error: "接口出错" };
const STATUS = { ok: ["执行了", "good"], blocked: ["被拦下", "bad"], skipped: ["没有执行", "warn"] };
const HIGHLIGHT = new Set(["能不能办", "金额", "办理权限", "售后单", "权限"]);
const DEFAULTS = { run: "suite", file: "aftersales-dsflash.zero7-k3.jsonl", cmp: "aftersales-dsflash.zero7-k3-legacy.jsonl",
                   s: "SO260825000049-escalate_amount", t: "1" };

const state = { runs: [], list: [], cmpList: [], panels: [], pos: 0, timer: null };

// ---------------------------------------------------------------- 地址栏里的选择

function readHash() {
  const params = new URLSearchParams(location.hash.slice(1));
  return Object.fromEntries(["run", "file", "cmp", "s", "t", "p"].map((key) => [key, params.get(key)]));
}

function writeHash() {
  const [scenario, trial] = ($("#scenario").value || "|").split("|");
  const params = new URLSearchParams({ run: $("#run").value, file: $("#file").value, cmp: $("#cmp").value, s: scenario, t: trial });
  history.replaceState(null, "", `#${params}`);
}

async function api(path, params) {
  const response = await fetch(`${path}?${new URLSearchParams(params)}`);
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || response.statusText);
  return response.json();
}

const short = (file) => file.replace(/^aftersales-?/, "").replace(/\.jsonl$/, "") || file;

// ---------------------------------------------------------------- 选择器

async function loadRuns(wanted) {
  state.runs = (await api("/api/console/runs", {})).runs.filter((run) => Object.keys(run.aftersales_files).length);
  const run = $("#run");
  run.replaceChildren(...state.runs.map((r) => h("option", { value: r.name }, r.name)));
  run.value = state.runs.some((r) => r.name === wanted.run) ? wanted.run : (state.runs.find((r) => r.name === DEFAULTS.run) || state.runs[0] || {}).name;
  fillFiles(wanted);
}

function fillFiles(wanted) {
  const info = state.runs.find((r) => r.name === $("#run").value);
  const files = Object.entries(info ? info.aftersales_files : {});
  const option = ([name, count]) => h("option", { value: name }, `${short(name)}（${count}）`);
  $("#file").replaceChildren(...files.map(option));
  $("#cmp").replaceChildren(h("option", { value: "" }, "（不对照）"), ...files.map(option));
  const has = (name) => files.some(([file]) => file === name);
  $("#file").value = has(wanted.file) ? wanted.file : has(DEFAULTS.file) ? DEFAULTS.file : (files[0] || [""])[0];
  $("#cmp").value = wanted.cmp !== null && (wanted.cmp === "" || has(wanted.cmp)) ? wanted.cmp
    : $("#file").value === DEFAULTS.file && has(DEFAULTS.cmp) ? DEFAULTS.cmp : "";
}

async function loadLists(wanted) {
  const run = $("#run").value;
  const [main, other] = await Promise.all([
    api("/api/console/aftersales", { run, file: $("#file").value }),
    $("#cmp").value ? api("/api/console/aftersales", { run, file: $("#cmp").value }) : null,
  ]);
  state.list = main.conversations;
  state.cmpList = other ? other.conversations : [];
  fillScenarios(wanted);
}

function fillScenarios(wanted) {
  const other = new Map(state.cmpList.map((c) => [`${c.scenario_id}|${c.trial || 1}`, c.passed]));
  const diffOnly = $("#diff-only").checked && other.size;
  const groups = new Map();
  for (const c of state.list) {
    const key = `${c.scenario_id}|${c.trial || 1}`;
    const theirs = other.get(key);
    if (diffOnly && (theirs === undefined || theirs === c.passed)) continue;
    const marks = other.size ? `${c.passed ? "✓" : "✗"} / ${theirs === undefined ? "–" : theirs ? "✓" : "✗"}` : c.passed ? "✓" : "✗";
    const label = `${marks}  ${c.scenario_id.replace(/-[a-z_]+$/, "")}  第 ${c.trial || 1} 次`;
    if (!groups.has(c.kind)) groups.set(c.kind, []);
    groups.get(c.kind).push(h("option", { value: key }, label));
  }
  const select = $("#scenario");
  select.replaceChildren(...[...groups].map(([kind, options]) => h("optgroup", { label: KINDS[kind] || kind }, options)));
  const wantedKey = wanted && wanted.s ? `${wanted.s}|${wanted.t || 1}` : `${DEFAULTS.s}|${DEFAULTS.t}`;
  if ([...select.options].some((o) => o.value === wantedKey)) select.value = wantedKey;
  if (!select.value && select.options.length) select.value = select.options[0].value;
}

// ---------------------------------------------------------------- 加载一段对话

async function loadConversation() {
  stop();
  writeHash();
  const stage = $("#stage");
  const key = $("#scenario").value;
  if (!key) {
    stage.className = "rp-stage";
    stage.replaceChildren(h("p", { class: "empty" }, $("#diff-only").checked ? "两份记录没有结果不同的场景。" : "这份记录里没有对话。"));
    return;
  }
  const [scenario, trial] = key.split("|");
  const run = $("#run").value;
  const files = [$("#file").value, $("#cmp").value].filter(Boolean);
  const results = await Promise.all(files.map((file) =>
    api("/api/console/replay", { run, file, scenario, trial }).catch((error) => ({ missing: error.message, file }))));
  state.panels = results.map((data) => ({ data, rendered: 0, node: null }));
  stage.className = `rp-stage${state.panels.length > 1 ? " compare" : ""}`;
  stage.replaceChildren(...state.panels.map(buildPanel));
  state.pos = 1;
  const longest = Math.max(...state.panels.map((p) => (p.data.frames || []).length));
  $("#scrub").max = String(longest);
  render();
}

function harnessLabel(data) {
  return data.legacy ? "旧 harness" : "当前 harness";
}

function expectedText(expected) {
  if (!expected) return "—";
  const parts = (expected.tickets || []).map((t) => `提交${t.type}（${t.reason}）`);
  for (const action of expected.actions || []) parts.push(TOOLS[action.tool] || action.tool || String(action));
  if (expected.transfer) parts.push("转人工");
  return parts.length ? parts.join("、") : "不该办任何事，讲清规则和办法";
}

function buildPanel(panel) {
  const data = panel.data;
  if (data.missing) {
    panel.node = h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", {}, short(data.file))),
      h("p", { class: "empty" }, `这份记录里没有同一个场景：${data.missing}`));
    return panel.node;
  }
  const order = data.order;
  const item = order && order.items && order.items[0];
  const clock = (data.now || "").slice(11, 16);
  panel.chat = h("div", { class: "chat", "aria-live": "polite" });
  panel.dev = h("div", { class: "dev" });
  const phone = h("div", { class: "phone" },
    h("div", { class: "screen" },
      h("div", { class: "statusbar" }, h("span", {}, clock), h("span", { class: "island" }), h("span", {}, "5G ▮▮▮")),
      h("div", { class: "app-head" }, h("span", { class: "avatar" }, "客"),
        h("span", {}, h("b", {}, "官方客服"), h("small", {}, "智能助手 · 7×24 小时"))),
      order ? h("div", { class: "order-card" }, h("span", { class: "thumb" }, "🛍"),
        h("div", {}, h("div", { class: "title" }, item ? item.title : order.order_id),
          h("div", { class: "meta" }, h("span", { class: "status" }, order.status), ` · ¥${order.total_amount} · ${order.order_id}`))) : null,
      panel.chat));
  const brief = h("div", { class: "brief" },
    kv("情形", `${KINDS[data.kind] || data.kind} · ${data.category}`),
    kv("顾客想要", data.goal || "—"),
    kv("标准答案", expectedText(data.expected)),
    kv("这一次", `${harnessLabel(data)} · 第 ${data.trial || 1} 次试验 · ${ENDED[data.ended] || data.ended}`),
    data.usage ? kv("用量", `${data.usage.model_calls} 次模型调用 · ${data.usage.tool_calls} 次工具调用 · 输入 ${data.usage.input_tokens} token`) : null);
  panel.node = h("section", { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", {}, harnessLabel(data)),
      h("span", { class: `badge ${data.grade.passed ? "good" : "bad"}` }, data.grade.passed ? "通过" : "没通过"),
      h("span", { class: "file" }, short(data.file))),
    h("div", { class: "panel-body" }, phone, h("div", { class: "dev" }, brief, panel.dev)));
  return panel.node;
}

function kv(label, value) {
  return h("div", { class: "rp-kv" }, h("span", {}, label), h("span", {}, value));
}

// ---------------------------------------------------------------- 画帧

function inline(text) {
  // **加粗** 和 `代码`，其余原样
  const nodes = [];
  for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`)/)) {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) nodes.push(h("b", {}, part.slice(2, -2)));
    else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) nodes.push(h("code", {}, part.slice(1, -1)));
    else if (part) nodes.push(part);
  }
  return nodes;
}

function markdown(text) {
  const blocks = [];
  let list = null;
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    const bullet = line.match(/^(?:[-*•]|\d+[.、)])\s+(.*)$/);
    if (bullet) {
      if (!list) blocks.push((list = h("ul", {})));
      list.append(h("li", {}, inline(bullet[1])));
    } else {
      list = null;
      if (line) blocks.push(h("p", {}, inline(line.replace(/^#+\s*/, ""))));
    }
  }
  return blocks;
}

function phoneNodes(frame) {
  if (frame.type === "customer") return [h("div", { class: "bubble me" }, frame.text)];
  if (frame.type === "reply") return [h("div", { class: "bubble bot" }, markdown(frame.text))];
  if (frame.type === "end") return [h("div", { class: "sys" }, `对话结束 · ${ENDED[frame.ended] || frame.ended}`)];
  if (frame.type !== "tool" || frame.status !== "ok") return [];
  const nodes = [];
  if (frame.card) {
    const card = frame.card;
    nodes.push(h("div", { class: "confirm" },
      h("div", { class: "c-head" }, h("span", {}, card.title), h("span", {}, "请确认")),
      h("dl", {}, card.fields.map((f) => [h("dt", {}, f.label), h("dd", {}, f.value)])),
      h("div", { class: "c-actions" }, h("span", {}, "取消"), h("span", { class: "ok" }, "✓ 已确认")),
      card.outcome ? h("div", { class: "c-done" }, card.outcome) : null));
  }
  if (frame.notice) nodes.push(h("div", { class: "sys" }, frame.notice));
  return nodes;
}

function gateChain(gates) {
  const nodes = [];
  gates.forEach((gate, index) => {
    if (index) nodes.push(h("span", { class: "arrow" }, "›"));
    const mark = { pass: "✓ ", fail: "✗ ", todo: "", na: "" }[gate.state];
    nodes.push(h("span", { class: `gate ${gate.state}`, title: { pass: "过了", fail: "卡在这一关", todo: "没走到", na: "这个工具不过这一关" }[gate.state] },
      mark + gate.label));
  });
  return h("div", { class: "gates" }, nodes);
}

function json(value) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function devEntry(frame, index, panel) {
  const attrs = { class: `entry ${frame.type}${frame.type === "tool" ? ` ${frame.status}` : ""}`, onclick: () => seek(index + 1) };
  if (frame.type === "customer") {
    return h("div", attrs, h("div", { class: "who" }, h("b", {}, "顾客"), "说"), h("div", { class: "text" }, frame.text));
  }
  if (frame.type === "reply") {
    return h("div", attrs, h("div", { class: "who" }, h("b", {}, "agent"), "回复顾客"), h("div", { class: "text" }, frame.text.replace(/\*\*/g, "")));
  }
  if (frame.type === "tool") {
    const [label, tone] = STATUS[frame.status];
    return h("div", attrs,
      h("div", { class: "who" }, h("b", {}, `${TOOLS[frame.tool] || frame.tool}`), h("span", { class: "mono" }, frame.tool),
        `第 ${frame.turn} 轮`, h("span", { class: `badge ${tone}` }, frame.outcome ? `${label}：${frame.outcome}` : label)),
      gateChain(frame.gates),
      frame.message ? h("p", { class: "why" }, frame.message) : null,
      frame.facts.length ? h("dl", { class: "facts" }, frame.facts.map((f) =>
        [h("dt", {}, f.label), h("dd", { class: HIGHLIGHT.has(f.label) ? "hl" : null }, f.value)])) : null,
      h("details", {}, h("summary", {}, "参数和返回"), h("pre", {}, `参数 ${json(frame.arguments)}\n\n返回 ${json(frame.result)}`)));
  }
  const grade = panel.data.grade;
  const rows = [...grade.rows].sort((a, b) => (a.passed - b.passed) || (b.hard - a.hard));
  return h("div", { class: "entry end" },
    h("div", { class: "who" }, h("b", {}, "评分"), ENDED[frame.ended] || frame.ended,
      h("span", { class: `badge ${grade.passed ? "good" : "bad"}` }, grade.passed ? "通过" : "没通过")),
    frame.error ? h("p", { class: "why" }, frame.error) : null,
    h("div", { class: "grade" }, rows.map((row) => h("div", { class: `row ${row.passed ? "ok" : "no"}` },
      h("span", { class: "mark" }, row.passed ? "✓" : "✗"), row.name, row.hard ? h("span", { class: "tag" }, "硬性") : null))));
}

function typing(frame) {
  const text = frame.type === "tool" ? `正在为您${TOOLS[frame.tool] || "处理"}` : "正在输入";
  return h("div", { class: "typing" }, text, h("i"), h("i"), h("i"));
}

function renderPanel(panel) {
  if (panel.data.missing) return;
  const frames = panel.data.frames;
  const shown = Math.min(state.pos, frames.length);
  if (shown < panel.rendered) {  // 往回拖：整块重画，不放动画
    panel.chat.replaceChildren();
    panel.dev.replaceChildren();
    panel.rendered = 0;
    panel.node.classList.add("still");
  } else {
    panel.node.classList.remove("still");
  }
  $(".typing", panel.chat)?.remove();
  for (let i = panel.rendered; i < shown; i += 1) {
    panel.chat.append(...phoneNodes(frames[i]));
    panel.dev.append(devEntry(frames[i], i, panel));
  }
  panel.rendered = shown;
  if (shown < frames.length && frames[shown].type !== "customer") panel.chat.append(typing(frames[shown]));
  panel.dev.querySelectorAll(".entry.current").forEach((node) => node.classList.remove("current"));
  const current = panel.dev.children[shown - 1];
  if (current) {
    current.classList.add("current");
    const box = panel.dev.parentElement;  // 开发者一栏自己滚动，页面不跟着跳
    const top = current.offsetTop - box.offsetTop;
    if (top < box.scrollTop || top + current.offsetHeight > box.scrollTop + box.clientHeight) {
      box.scrollTop = Math.max(0, top + current.offsetHeight - box.clientHeight + 12);
    }
  }
  panel.chat.scrollTop = panel.chat.scrollHeight;
}

function render() {
  state.panels.forEach(renderPanel);
  const longest = Number($("#scrub").max);
  $("#scrub").value = String(state.pos);
  $("#step").textContent = `${state.pos} / ${longest}`;
  $("#play").textContent = state.timer ? "⏸ 暂停" : state.pos >= longest ? "↺ 重播" : "▶ 播放";
}

// ---------------------------------------------------------------- 播放

function seek(pos) {
  state.pos = Math.max(0, Math.min(pos, Number($("#scrub").max)));
  render();
}

function stop() {
  clearTimeout(state.timer);
  state.timer = null;
}

function delayFor(pos) {
  const base = Number($("#speed").value);
  const frame = state.panels.map((p) => (p.data.frames || [])[pos]).find(Boolean);
  if (!frame) return base;
  return frame.type === "tool" ? base * 0.7 : frame.type === "reply" ? base * 1.4 : base;
}

function tick() {
  if (state.pos >= Number($("#scrub").max)) {
    stop();
    render();
    return;
  }
  state.timer = setTimeout(() => {
    state.pos += 1;
    render();
    tick();
  }, delayFor(state.pos));
  render();
}

function togglePlay() {
  if (state.timer) {
    stop();
    render();
    return;
  }
  if (state.pos >= Number($("#scrub").max)) seek(1);
  tick();
}

// ---------------------------------------------------------------- 启动

function measureHeader() {  // 顶栏高度随选择器换行变化，手机和条目的吸顶位置跟着它
  document.documentElement.style.setProperty("--head", `${$(".topbar").offsetHeight}px`);
}

async function init() {
  measureHeader();
  window.addEventListener("resize", measureHeader);
  const wanted = readHash();
  await loadRuns(wanted);
  await loadLists(wanted);
  await loadConversation();
  if (wanted.p) seek(wanted.p === "end" ? Number($("#scrub").max) : Number(wanted.p) || 1);  // #…&p=end 直接看结果，截图和分享用
  $("#run").addEventListener("change", async () => { fillFiles({}); await loadLists({}); await loadConversation(); });
  $("#file").addEventListener("change", async () => { await loadLists({ s: ($("#scenario").value || "|").split("|")[0] }); await loadConversation(); });
  $("#cmp").addEventListener("change", async () => {
    const [s, t] = ($("#scenario").value || "|").split("|");
    await loadLists({ s, t });
    await loadConversation();
  });
  $("#diff-only").addEventListener("change", async () => { fillScenarios(null); await loadConversation(); });
  $("#scenario").addEventListener("change", loadConversation);
  $("#play").addEventListener("click", togglePlay);
  $("#prev").addEventListener("click", () => { stop(); seek(state.pos - 1); });
  $("#next").addEventListener("click", () => { stop(); seek(state.pos + 1); });
  $("#first").addEventListener("click", () => { stop(); seek(1); });
  $("#last").addEventListener("click", () => { stop(); seek(Number($("#scrub").max)); });
  $("#scrub").addEventListener("input", (event) => { stop(); seek(Number(event.target.value)); });
  document.addEventListener("keydown", (event) => {
    if (event.target.closest("select, input")) return;
    const actions = { " ": togglePlay, ArrowLeft: () => { stop(); seek(state.pos - 1); }, ArrowRight: () => { stop(); seek(state.pos + 1); },
                      Home: () => { stop(); seek(1); }, End: () => { stop(); seek(Number($("#scrub").max)); } };
    if (actions[event.key]) {
      event.preventDefault();
      actions[event.key]();
    }
  });
}

init().catch((error) => {
  $("#stage").replaceChildren(h("p", { class: "empty" }, `加载失败：${error.message}`));
});
