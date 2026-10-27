"use strict";

const EXAMPLES = [
  "耳机拆封试了一下，还能七天无理由退货吗",
  "快递在路上丢了，商家让我自己去找快递公司",
  "买到假货，商家只同意退款，不肯赔偿",
  "快递员没打电话，直接把包裹放驿站了",
  "直播间买的衣服和主播说的不一样",
  "刚买完就降价了，能申请价保吗",
  "商家老打推销电话，还泄露了我的地址",
];
const TOP_K = 10;

const state = {
  info: null,
  defaultWeight: 0.5,
  weight: 0.5,
  expand: true,
  levels: new Set(),
  seq: 0,
  marks: null,
  hits: new Map(),
  hovered: null,
};
const articleCache = new Map();

const $ = (selector, root = document) => root.querySelector(selector);

function escapeHTML(value) {
  const entities = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  return String(value ?? "").replace(/[&<>"']/g, (char) => entities[char]);
}

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

async function fetchJSON(url) {
  const response = await fetch(url);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : body.detail?.[0]?.msg;
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return body;
}

const shortTitle = (title) => title.replace(/^中华人民共和国/, "");
const shortCitation = (citation) => citation.replace(/^《中华人民共和国/, "《");
const allLevels = () => state.info.levels.map((item) => item.level);
const allLevelsSelected = () => state.levels.size === state.info.levels.length;

function setStatus(message, kind = "") {
  const status = $("#status");
  status.textContent = message;
  status.className = `status ${kind}`.trim();
}

/* 高亮：黄色是问题里的词，绿色是同义词扩展出的词；单字太常见，不高亮 */
function buildMarks(bm25Query) {
  const classOf = new Map();
  for (const token of bm25Query.expansion_tokens) if (token.length >= 2) classOf.set(token, "hl-x");
  for (const token of bm25Query.tokens) if (token.length >= 2) classOf.set(token, "hl-q");
  const alternation = [...classOf.keys()].sort((a, b) => b.length - a.length).map(escapeRegExp).join("|");
  return { classOf, pattern: alternation ? new RegExp(alternation, "g") : null };
}

function highlight(text, marks) {
  if (!marks?.pattern) return escapeHTML(text);
  let html = "";
  let last = 0;
  for (const match of text.matchAll(marks.pattern)) {
    html += escapeHTML(text.slice(last, match.index));
    html += `<mark class="${marks.classOf.get(match[0])}">${escapeHTML(match[0])}</mark>`;
    last = match.index + match[0].length;
  }
  return html + escapeHTML(text.slice(last));
}

function levelBadge(level, small = false) {
  return `<span class="badge${small ? " small" : ""}" data-level="${escapeHTML(level)}">${escapeHTML(level)}</span>`;
}

function rankPill(name, label, rank) {
  if (!rank) return `<span class="rank ${name} missing" title="不在${label}前 50 名">${label} —</span>`;
  return `<span class="rank ${name}" title="${label}名次">${label} #${rank}</span>`;
}

function cardHTML(hit, position) {
  const basis = (hit.legal_basis || [])
    .map((citation) => `<button type="button" class="chip basis-chip" data-citation="${escapeHTML(citation)}">${escapeHTML(shortCitation(citation))}</button>`)
    .join("");
  const status = hit.status && hit.status !== "有效" ? `<span class="badge status">${escapeHTML(hit.status)}</span>` : "";
  const source = hit.source_url
    ? `<a class="link" href="${escapeHTML(hit.source_url)}" target="_blank" rel="noopener noreferrer">原文 ↗</a>`
    : "";
  return `
    <li class="card" data-chunk="${escapeHTML(hit.chunk_id)}">
      <div class="card-top">
        <span class="pos">${position}</span>
        <div class="card-heading">
          <h3 class="citation"><span class="doc">《${escapeHTML(hit.title)}》</span><span class="art">${escapeHTML(hit.article_no)}</span></h3>
          <div class="meta">${levelBadge(hit.level)}${status}${hit.chapter ? `<span class="chapter">${escapeHTML(hit.chapter)}</span>` : ""}</div>
        </div>
        <div class="ranks">${rankPill("bm25", "BM25", hit.ranks.bm25)}${rankPill("dense", "向量", hit.ranks.dense)}</div>
      </div>
      <p class="text clamp">${highlight(hit.text, state.marks)}</p>
      <div class="card-foot">
        <button type="button" class="link toggle-text" hidden>展开全文</button>
        ${source}
        <span class="score" title="加权 RRF 得分">RRF ${hit.score.toFixed(4)}</span>
      </div>
      ${basis ? `<div class="basis"><span class="basis-label">依据</span>${basis}</div><div class="basis-detail" hidden></div>` : ""}
    </li>`;
}

function compactHTML(hit, rank) {
  const inTop = hit.fused_rank <= TOP_K;
  return `
    <li class="compact-item" data-chunk="${escapeHTML(hit.chunk_id)}" tabindex="0">
      <span class="c-rank">${rank}</span>
      <span class="c-body">
        <span class="c-cite"><span class="c-doc">${escapeHTML(shortTitle(hit.title))}</span><span class="c-art">${escapeHTML(hit.article_no)}</span></span>
        <span class="c-meta">${levelBadge(hit.level, true)}<span class="c-fused${inTop ? " top" : ""}">融合 #${hit.fused_rank}</span></span>
      </span>
    </li>`;
}

function renderAnalysis(data) {
  const { tokens, expansion_tokens: extra, synonyms } = data.bm25_query;
  const weight = state.info.expansion_weight;
  $("#tokens").innerHTML =
    tokens.map((token) => `<span class="token${token.length < 2 ? " weak" : ""}">${escapeHTML(token)}</span>`).join("") +
    extra.map((token) => `<span class="token added" title="同义词扩展出的词，BM25 里权重 ${weight}">${escapeHTML(token)}</span>`).join("");

  const mappings = Object.entries(synonyms);
  if (!mappings.length) {
    $("#synonyms").innerHTML = `<span class="muted">问题里没有同义词表中的说法</span>`;
    return;
  }
  $("#synonyms").innerHTML =
    mappings
      .map(([phrase, terms]) => `<span class="mapping${data.expand ? "" : " off"}">${escapeHTML(phrase)} <span class="arrow">→</span> ${terms.map(escapeHTML).join("、")}</span>`)
      .join("") + (data.expand ? "" : `<span class="muted">扩展已关闭</span>`);
}

function render(data) {
  state.marks = buildMarks(data.bm25_query);
  state.hits = new Map([...data.fused, ...data.bm25, ...data.dense].map((hit) => [hit.chunk_id, hit]));
  state.hovered = null;
  $("#intro").hidden = true;
  $("#analysis").hidden = false;
  $("#results").hidden = false;
  renderAnalysis(data);

  const { dense, bm25 } = data.weights;
  $("#fused-meta").textContent = `向量 ${dense.toFixed(2)} · BM25 ${bm25.toFixed(2)} · RRF k=${state.info.rrf_k}`;
  $("#fused-list").innerHTML = data.fused.length
    ? data.fused.map((hit, index) => cardHTML(hit, index + 1)).join("")
    : `<li class="card muted">这个范围里没有结果</li>`;
  $("#bm25-list").innerHTML = data.bm25.map((hit, index) => compactHTML(hit, index + 1)).join("");
  $("#dense-list").innerHTML = data.dense.map((hit, index) => compactHTML(hit, index + 1)).join("");
  requestAnimationFrame(showTextToggles);
}

/* 只有被截断的正文才显示“展开全文” */
function showTextToggles() {
  for (const card of document.querySelectorAll("#fused-list .card")) {
    const text = $(".text", card);
    const toggle = $(".toggle-text", card);
    if (text && toggle) toggle.hidden = text.scrollHeight <= text.clientHeight + 2;
  }
}

async function search() {
  const query = $("#query").value.trim();
  if (!query) {
    $("#query").focus();
    return;
  }
  const params = new URLSearchParams({ q: query, k: TOP_K, dense_weight: state.weight, expand: state.expand });
  if (!allLevelsSelected()) for (const level of state.levels) params.append("level", level);

  const seq = ++state.seq;
  const started = performance.now();
  document.body.classList.add("is-loading");
  setStatus("检索中…");
  try {
    const data = await fetchJSON(`/api/search?${params}`);
    if (seq !== state.seq) return; // 已经有更新的请求
    writeURL(query);
    render(data);
    const scope = allLevelsSelected() ? "全部层级" : [...state.levels].join("、");
    setStatus(`在${scope}中检索，用时 ${Math.round(performance.now() - started)} ms`);
  } catch (error) {
    if (seq === state.seq) setStatus(`检索失败：${error.message}`, "error");
  } finally {
    if (seq === state.seq) document.body.classList.remove("is-loading");
  }
}

function searchIfQuery() {
  if ($("#query").value.trim()) search();
}

const searchSoon = debounce(searchIfQuery, 120);

function writeURL(query) {
  const params = new URLSearchParams({ q: query });
  if (state.weight !== state.defaultWeight) params.set("w", state.weight);
  if (!state.expand) params.set("expand", "0");
  if (!allLevelsSelected()) for (const level of state.levels) params.append("level", level);
  history.replaceState(null, "", `?${params}`);
}

function syncControls() {
  $("#weight").value = state.weight;
  $("#weight-value").textContent = state.weight.toFixed(2);
  const preset = state.weight === 0 ? "bm25" : state.weight === 1 ? "dense" : "hybrid";
  for (const button of document.querySelectorAll("#presets button")) {
    button.setAttribute("aria-pressed", String(button.dataset.preset === preset));
  }
  $("#expand").checked = state.expand;
  for (const input of document.querySelectorAll("#levels input")) input.checked = state.levels.has(input.value);
}

function renderInfo() {
  const { info } = state;
  const stats = [
    ["份文件", info.documents],
    ["条目", info.chunks.toLocaleString("zh-CN")],
    ["条同义词", info.synonyms],
    ["向量模型", info.model.split("/").pop()],
  ];
  $("#stats").innerHTML = stats.map(([label, value]) => `<div><dt>${label}</dt><dd>${escapeHTML(value)}</dd></div>`).join("");
  $("#levels").innerHTML = info.levels
    .map(({ level, count }) => `
      <label class="level-toggle" data-level="${escapeHTML(level)}">
        <input type="checkbox" value="${escapeHTML(level)}" checked>
        <span>${escapeHTML(level)} <small>${count}</small></span>
      </label>`)
    .join("");
  const built = info.built_at ? ` · 索引构建于 ${info.built_at.slice(0, 10)}` : "";
  $("#footer").textContent =
    `BM25（jieba 分词）+ ${info.model} 向量检索，加权 RRF 融合（k=${info.rrf_k}）；` +
    `同义词扩展只作用于 BM25，扩展词权重 ${info.expansion_weight}${built}`;
}

function applyURL() {
  const params = new URLSearchParams(location.search);
  const known = new Set(allLevels());
  const levels = params.getAll("level").filter((level) => known.has(level));
  state.levels = new Set(levels.length ? levels : known);
  const weight = Number.parseFloat(params.get("w"));
  state.weight = Number.isFinite(weight) && weight >= 0 && weight <= 1 ? weight : state.defaultWeight;
  state.expand = params.get("expand") !== "0";
  syncControls();
  const query = params.get("q");
  if (query) {
    $("#query").value = query;
    search();
  }
}

function focusChunk(item) {
  const id = item.dataset.chunk;
  const card = $(`#fused-list [data-chunk="${CSS.escape(id)}"]`);
  if (card) {
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.remove("flash");
    void card.offsetWidth; // 重新触发动画
    card.classList.add("flash");
    return;
  }
  // 不在融合前 10 名里：在原地展开正文
  const existing = $(".c-preview", item);
  if (existing) {
    existing.remove();
    return;
  }
  const preview = document.createElement("p");
  preview.className = "c-preview";
  preview.innerHTML = highlight(state.hits.get(id)?.text ?? "", state.marks);
  $(".c-body", item).append(preview);
}

async function toggleBasis(chip) {
  const card = chip.closest(".card");
  const detail = $(".basis-detail", card);
  if (chip.classList.contains("active")) {
    chip.classList.remove("active");
    detail.hidden = true;
    return;
  }
  for (const other of card.querySelectorAll(".basis-chip.active")) other.classList.remove("active");
  chip.classList.add("active");
  detail.hidden = false;
  detail.innerHTML = `<p class="muted">加载中…</p>`;
  const { citation } = chip.dataset;
  try {
    if (!articleCache.has(citation)) {
      articleCache.set(citation, await fetchJSON(`/api/article?citation=${encodeURIComponent(citation)}`));
    }
    if (!chip.classList.contains("active")) return;
    const article = articleCache.get(citation);
    detail.innerHTML = `
      <div class="basis-head">${levelBadge(article.level)}<b>${escapeHTML(article.citation)}</b></div>
      <p class="text">${highlight(article.text, state.marks)}</p>`;
  } catch (error) {
    detail.innerHTML = `<p class="error">${escapeHTML(error.message)}</p>`;
  }
}

function setLinked(id, on) {
  for (const element of document.querySelectorAll(`#results [data-chunk="${CSS.escape(id)}"]`)) {
    element.classList.toggle("linked", on);
  }
}

function bindEvents() {
  $("#examples").innerHTML = EXAMPLES.map((text) => `<button type="button" class="example">${escapeHTML(text)}</button>`).join("");
  $("#examples").addEventListener("click", (event) => {
    const example = event.target.closest(".example");
    if (!example) return;
    $("#query").value = example.textContent;
    search();
  });

  $("#search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    search();
  });

  $("#levels").addEventListener("change", (event) => {
    const input = event.target;
    if (input.checked) state.levels.add(input.value);
    else if (state.levels.size > 1) state.levels.delete(input.value);
    else {
      input.checked = true; // 至少保留一个层级
      return;
    }
    searchIfQuery();
  });

  $("#scope-shortcuts").addEventListener("click", (event) => {
    const scope = event.target.closest("[data-scope]")?.dataset.scope;
    if (!scope) return;
    const levels = allLevels().filter((level) =>
      scope === "all" ? true : scope === "platform" ? level === "平台规则" : level !== "平台规则",
    );
    if (!levels.length) return;
    state.levels = new Set(levels);
    syncControls();
    searchIfQuery();
  });

  $("#presets").addEventListener("click", (event) => {
    const preset = event.target.closest("[data-preset]")?.dataset.preset;
    if (!preset) return;
    state.weight = { bm25: 0, dense: 1 }[preset] ?? state.defaultWeight;
    syncControls();
    searchIfQuery();
  });

  $("#weight").addEventListener("input", (event) => {
    state.weight = Number(event.target.value);
    syncControls();
    searchSoon();
  });

  $("#expand").addEventListener("change", (event) => {
    state.expand = event.target.checked;
    searchIfQuery();
  });

  const results = $("#results");
  results.addEventListener("mouseover", (event) => {
    const item = event.target.closest("[data-chunk]");
    if (item === state.hovered) return;
    if (state.hovered) setLinked(state.hovered.dataset.chunk, false);
    state.hovered = item;
    if (item) setLinked(item.dataset.chunk, true);
  });
  results.addEventListener("mouseleave", () => {
    if (state.hovered) setLinked(state.hovered.dataset.chunk, false);
    state.hovered = null;
  });
  results.addEventListener("click", (event) => {
    const toggle = event.target.closest(".toggle-text");
    if (toggle) {
      const text = $(".text", toggle.closest(".card"));
      text.classList.toggle("clamp");
      toggle.textContent = text.classList.contains("clamp") ? "展开全文" : "收起";
      return;
    }
    const chip = event.target.closest(".basis-chip");
    if (chip) {
      toggleBasis(chip);
      return;
    }
    const item = event.target.closest(".compact-item");
    if (item) focusChunk(item);
  });
  results.addEventListener("keydown", (event) => {
    const item = event.target.closest(".compact-item");
    if (item && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      focusChunk(item);
    }
  });

  document.addEventListener("keydown", (event) => {
    const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName);
    if (event.key === "/" && !typing) {
      event.preventDefault();
      $("#query").focus();
    }
  });
}

async function init() {
  bindEvents();
  try {
    state.info = await fetchJSON("/api/info");
  } catch (error) {
    setStatus(`连不上检索服务：${error.message}`, "error");
    return;
  }
  state.defaultWeight = state.info.fusion_weights.dense;
  renderInfo();
  applyURL();
}

init();
