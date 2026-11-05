"use strict";

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (s) => s ? new Date(s).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "—";
const fmtMoney = (v) => v === null || v === undefined ? "—" : "¥" + v.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtPct = (v) => v === null || v === undefined ? "—" : (v * 100).toFixed(1) + "%";
const fmtQty = (v) => (v ?? 0).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
const TZ_LABEL = "浏览器本地时间";

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) { const e = new Error(body.error || res.statusText); e.status = res.status; throw e; }
  return body;
}

let timer = null;
function schedule(fn, seconds) {
  clearTimeout(timer);
  timer = setTimeout(fn, Math.max(30, seconds || 900) * 1000);
}

/* ======================================================================== 配置页 */
const cfg = {
  data: null,
  mode: null,
  columns: [{ name: "", sids: [] }, { name: "", sids: [] }, { name: "", sids: [] }],
  qChoice: {},    // key -> {kind: "zero"|"pct", pct: "string"}
  qReviewed: {},  // key -> bool，每次打开配置页都要重新逐条核对
  ackDims: false,
  ackLots: false,
  serverError: "",
};

function selectedSids() {
  return new Set(cfg.columns.flatMap((c) => c.sids));
}

async function openConfig(prefill) {
  $("#board-view").classList.add("hidden");
  $("#config-view").classList.remove("hidden");
  window.scrollTo(0, 0);
  cfg.data = await api("/api/config");
  if (prefill) {
    const saved = cfg.data.saved;
    cfg.mode = saved ? saved.mode : null;
    cfg.columns = saved ? saved.columns.map((c) => ({ name: c.name, sids: [...c.sids] }))
      : [{ name: "", sids: [] }, { name: "", sids: [] }, { name: "", sids: [] }];
    cfg.qChoice = {};
    cfg.qReviewed = {};
    cfg.ackDims = cfg.ackLots = false;
    cfg.serverError = "";
  }
  for (const r of cfg.data.review.q_missing) {
    if (!(r.key in cfg.qChoice)) {
      cfg.qChoice[r.key] = r.current.kind === "pct" ? { kind: "pct", pct: String(r.current.pct) } : { kind: "zero", pct: "" };
    }
  }
  const known = new Set(cfg.data.stores.map((s) => s.sid));
  cfg.columns.forEach((c) => { c.sids = c.sids.filter((s) => known.has(s)); });
  renderConfig();
  schedule(() => openConfig(false), refreshSecondsFrom(cfg.data.next_poll_at, cfg.data.now));
}

function refreshSecondsFrom(nextPoll, now) {
  if (!nextPoll) return 900;
  return (new Date(nextPoll) - new Date(now)) / 1000 + 90;
}

function renderConfig() {
  const d = cfg.data;
  $("#cfg-sub").textContent = `数据时间 ${fmtTime(d.now)}（${TZ_LABEL}）· 下次轮询 ${fmtTime(d.next_poll_at)}`;
  const sign = d.sign_check || {};
  const signEl = $("#cfg-sign");
  if (sign.status === "failed") {
    signEl.className = "banner";
    signEl.textContent = `符号验收未通过：排行榜上的调整后毛利与毛利率将不展示，只按销量排序。${sign.message || ""}`;
  } else if (sign.status !== "passed") {
    signEl.className = "banner warn";
    signEl.textContent = "符号验收尚未完成，等待首次轮询。";
  } else {
    signEl.className = "banner hidden";
  }
  renderReady();
  renderQ();
  renderDims();
  renderLots();
  renderColumns();
  renderPrice();
  renderProblems();
}

function renderReady() {
  const sel = selectedSids();
  const rows = cfg.data.stores.map((s) => {
    const status = s.ready
      ? `<span class="ok">已就绪</span> · 快照 ${fmtTime(s.snapshot_at)}（本店时间 ${esc(s.local_time)}）`
      : `<span class="wait">未就绪</span> · 等待${esc(s.waiting_for)}${s.last_error ? `<br><span class="bad">上次轮询出错：${esc(s.last_error)}</span>` : ""}`;
    return `<tr class="${sel.has(s.sid) ? "" : "inactive"}"><td>${esc(s.name)}</td><td>${esc(s.site_name)}</td>
      <td>${esc(s.local_date)}</td><td>${status}</td><td>${sel.has(s.sid) ? "已选入" : "未选入"}</td></tr>`;
  }).join("");
  $("#ready-table").innerHTML = `<tr><th>店铺</th><th>站点</th><th>本店今天</th><th>状态</th><th>排行榜</th></tr>${rows}`;
}

function qNeeded() {
  const sel = selectedSids();
  return cfg.data.review.q_missing.filter((r) => sel.has(r.sid));
}

function qValid(key) {
  const c = cfg.qChoice[key];
  if (!c || !c.kind) return false;
  if (c.kind === "zero") return true;
  const v = Number(c.pct);
  return c.pct !== "" && Number.isFinite(v) && v >= 0 && v <= 100;
}

function renderQCounter() {
  const need = qNeeded();
  const done = need.filter((r) => cfg.qReviewed[r.key] && qValid(r.key)).length;
  const el = $("#q-counter");
  el.textContent = need.length ? `已核对 ${done} / ${need.length}` : "选入的店铺没有需要处理的 MSKU";
  el.className = "counter" + (done === need.length ? " done" : "");
}

function renderQ() {
  const list = cfg.data.review.q_missing;
  const sel = selectedSids();
  if (!list.length) {
    $("#q-table").innerHTML = `<tr><td class="empty">没有缺退货率的 MSKU。</td></tr>`;
    renderQCounter();
    return;
  }
  const rows = list.map((r) => {
    const c = cfg.qChoice[r.key];
    const active = sel.has(r.sid);
    const k = esc(r.key);
    return `<tr class="${active ? "" : "inactive"}" data-key="${k}">
      <td>${esc(r.store)}</td><td>${esc(r.msku)}</td>
      <td class="num">${fmtQty(r.afn_volume)}</td><td class="num">${fmtMoney(r.net_amount)}</td>
      <td>${esc(r.current.text)}</td>
      <td>
        <label class="choice"><input type="radio" name="q-${k}" value="zero" ${c.kind === "zero" ? "checked" : ""}> 恢复按 0</label>
        <label class="choice"><input type="radio" name="q-${k}" value="pct" ${c.kind === "pct" ? "checked" : ""}>
          <input class="pct-input" type="number" min="0" max="100" step="0.1" placeholder="百分数" value="${esc(c.pct)}"> %</label>
      </td>
      <td><label class="choice"><input type="checkbox" class="q-ack" ${cfg.qReviewed[r.key] ? "checked" : ""}> 已核对</label></td>
    </tr>`;
  }).join("");
  $("#q-table").innerHTML = `<tr><th>店铺</th><th>MSKU</th><th>今日销量</th><th>今日净额</th><th>当前值</th><th>处理</th><th></th></tr>${rows}`;
  renderQCounter();
}

$("#q-table").addEventListener("input", (e) => {
  const tr = e.target.closest("tr[data-key]");
  if (!tr) return;
  const key = tr.dataset.key;
  const c = cfg.qChoice[key];
  if (e.target.type === "radio") c.kind = e.target.value;
  if (e.target.classList.contains("pct-input")) {
    c.pct = e.target.value;
    c.kind = "pct";
    tr.querySelector('input[value="pct"]').checked = true;
  }
  if (e.target.classList.contains("q-ack")) cfg.qReviewed[key] = e.target.checked;
  renderQCounter();
  renderProblems();
});

function simpleTable(head, rows) {
  return `<table class="list"><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr>${rows.join("")}</table>`;
}

function renderDims() {
  const list = cfg.data.review.dims_from_listing;
  const sel = selectedSids();
  $("#dims-body").innerHTML = `<p class="hint">下列 MSKU 的体积或弃置费没有用月度仓储费报告，而是用 Listing 包装尺寸 / 重量（${list.length} 条）。</p>` +
    (list.length
      ? simpleTable(["店铺", "MSKU", "尺寸分段", "改用的部分"], list.map((x) =>
        `<tr class="${sel.has(x.sid) ? "" : "inactive"}"><td>${esc(x.store)}</td><td>${esc(x.msku)}</td>
          <td>${esc(x.size_tier || "—")}</td><td>${esc(x.uses)}</td></tr>`))
      : `<div class="empty">无。全部 MSKU 的尺寸都来自月度仓储费报告。</div>`);
  $("#ack-dims").checked = cfg.ackDims;
}

function renderLots() {
  const r = cfg.data.review;
  const sel = selectedSids();
  const note = cfg.mode === "order"
    ? `<p class="hint bad">当前选择「${esc(cfg.data.mode_labels.order)}」：下列 MSKU 不进入行，也不计入合计。</p>`
    : `<p class="hint">这些 MSKU 含开账未知批次，无法按「在仓仓储」口径计算；若选择该口径，它们将不进入行与合计。</p>`;
  $("#lots-body").innerHTML = note + (r.unknown_lots.length
    ? simpleTable(["店铺", "MSKU", "已售已知批次件数", "已售未知批次件数"], r.unknown_lots.map((x) =>
      `<tr class="${sel.has(x.sid) ? "" : "inactive"}"><td>${esc(x.store)}</td><td>${esc(x.msku)}</td>
        <td class="num">${fmtQty(x.known_units)}</td><td class="num">${fmtQty(x.unknown_units)}</td></tr>`))
    : `<div class="empty">无。</div>`);
  $("#ack-lots").checked = cfg.ackLots;
}

$("#ack-dims").addEventListener("change", (e) => { cfg.ackDims = e.target.checked; renderProblems(); });
$("#ack-lots").addEventListener("change", (e) => { cfg.ackLots = e.target.checked; renderProblems(); });

function renderColumns() {
  const labels = cfg.data.mode_labels;
  $("#mode-pick").innerHTML = ["daily", "order"].map((m) =>
    `<button data-mode="${m}" class="${cfg.mode === m ? "active" : ""}">${esc(labels[m])}</button>`).join("");
  const owner = {};
  cfg.columns.forEach((c, i) => c.sids.forEach((s) => { owner[s] = i; }));
  $("#col-grid").innerHTML = cfg.columns.map((c, i) => {
    const opts = cfg.data.stores.map((s) => {
      const taken = owner[s.sid] !== undefined && owner[s.sid] !== i;
      const tag = taken ? `已在「${esc(cfg.columns[owner[s.sid]].name || `第 ${owner[s.sid] + 1} 列`)}」`
        : s.ready ? esc(s.site_name) : `${esc(s.site_name)} · 未就绪`;
      return `<label class="store-opt ${taken ? "disabled" : ""}">
        <input type="checkbox" data-col="${i}" data-sid="${s.sid}" ${c.sids.includes(s.sid) ? "checked" : ""} ${taken ? "disabled" : ""}>
        ${esc(s.name)} <span class="tag">${tag}</span></label>`;
    }).join("");
    return `<div class="col-card">
      <input type="text" data-name="${i}" maxlength="30" placeholder="第 ${i + 1} 列名字（必填）" value="${esc(c.name)}" class="${c.name.trim() ? "" : "invalid"}">
      ${opts}</div>`;
  }).join("");
}

$("#mode-pick").addEventListener("click", (e) => {
  const m = e.target.dataset.mode;
  if (!m) return;
  cfg.mode = m;
  renderColumns();
  renderLots();
  renderProblems();
});

$("#col-grid").addEventListener("input", (e) => {
  if (e.target.dataset.name !== undefined) {
    cfg.columns[Number(e.target.dataset.name)].name = e.target.value;
    e.target.classList.toggle("invalid", !e.target.value.trim());
    renderProblems();
    return;
  }
  if (e.target.dataset.sid !== undefined) {
    const col = cfg.columns[Number(e.target.dataset.col)];
    const sid = Number(e.target.dataset.sid);
    col.sids = e.target.checked ? [...col.sids, sid] : col.sids.filter((s) => s !== sid);
    renderColumns();
    renderReady();
    renderQ();
    renderDims();
    renderLots();
    renderProblems();
  }
});
$("#col-grid").addEventListener("change", (e) => {
  if (e.target.dataset.name !== undefined) renderColumns();
});

function renderPrice() {
  const p = cfg.data.price;
  let html = `<h2>亚马逊价目表版本</h2><div>当前生效：<b>${esc(p.active ? p.active.version : "无")}</b>${p.active && p.active.confirmed_at ? `（确认于 ${fmtTime(p.active.confirmed_at)}）` : ""}</div>`;
  for (const v of p.pending || []) {
    html += `<div class="pending">新版本 <b>${esc(v.version)}</b>（${esc(v.effective_date || "")} 起）待人工确认，确认前不生效。
      <button class="btn small" data-version="${esc(v.version)}">确认生效</button></div>`;
  }
  $("#sec-price").innerHTML = html;
}

$("#sec-price").addEventListener("click", async (e) => {
  const v = e.target.dataset.version;
  if (!v || !confirm(`确认让价目表 ${v} 生效？`)) return;
  try {
    await api("/api/price-table/confirm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ version: v }) });
    await openConfig(false);
  } catch (err) { alert(err.message); }
});

function problems() {
  const out = [];
  const byId = Object.fromEntries(cfg.data.stores.map((s) => [s.sid, s]));
  for (const s of selectedSids()) {
    if (!byId[s].ready) out.push(`${byId[s].name} 还没有本店今天的快照，等待${byId[s].waiting_for}`);
  }
  const need = qNeeded();
  const bad = need.filter((r) => !qValid(r.key));
  const unchecked = need.filter((r) => !cfg.qReviewed[r.key]);
  if (bad.length) out.push(`退货率：${bad.length} 条百分数无效（应为 0–100）`);
  if (unchecked.length) out.push(`退货率：${unchecked.length} 条未勾选「已核对」`);
  if (!cfg.ackDims) out.push("尺寸来源：请勾选「已查看」");
  if (!cfg.ackLots) out.push("开账未知批次：请勾选「已查看」");
  if (!cfg.mode) out.push("请选择仓储口径");
  cfg.columns.forEach((c, i) => { if (!c.name.trim()) out.push(`第 ${i + 1} 列名字不能为空`); });
  if (!selectedSids().size) out.push("三列里至少放一家店铺");
  return out;
}

function renderProblems() {
  const list = problems();
  const items = list.map((p) => `<li>${esc(p)}</li>`);
  if (cfg.serverError) items.unshift(`<li>${esc(cfg.serverError)}</li>`);
  $("#problems").innerHTML = items.join("");
  $("#confirm-btn").disabled = list.length > 0;
}

$("#confirm-btn").addEventListener("click", async () => {
  if (problems().length) return;
  const sel = selectedSids();
  const need = cfg.data.review.q_missing.filter((r) => sel.has(r.sid));
  const payload = {
    mode: cfg.mode,
    columns: cfg.columns.map((c) => ({ name: c.name.trim(), sids: c.sids })),
    reviewed_dims: cfg.ackDims,
    reviewed_unknown_lots: cfg.ackLots,
    q_reviewed: need.map((r) => r.key),
    q_overrides: Object.fromEntries(need.map((r) => {
      const c = cfg.qChoice[r.key];
      return [r.key, c.kind === "zero" ? { kind: "zero" } : { kind: "pct", pct: Number(c.pct) }];
    })),
  };
  $("#confirm-btn").disabled = true;
  try {
    await api("/api/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    cfg.serverError = "";
    await openBoard();
  } catch (err) {
    cfg.serverError = `保存失败：${err.message}`;
    await openConfig(false);
  }
});

$("#cfg-reload").addEventListener("click", () => openConfig(false));

/* ======================================================================== 排行榜 */
async function openBoard() {
  let d;
  try {
    d = await api("/api/leaderboard");
  } catch (err) {
    if (err.status === 409) return openConfig(true);
    $("#board-banner").className = "banner";
    $("#board-banner").textContent = `读取失败：${err.message}，15 分钟后重试。`;
    schedule(openBoard, 900);
    return;
  }
  $("#config-view").classList.add("hidden");
  $("#board-view").classList.remove("hidden");
  renderBoard(d);
  schedule(openBoard, d.refresh_in_seconds);
}

function renderBoard(d) {
  $("#board-mode").textContent = d.mode_label;
  $("#board-sub").textContent = `最新快照 ${fmtTime(d.latest_snapshot_at)} · 下次刷新约 ${fmtTime(new Date(Date.now() + d.refresh_in_seconds * 1000).toISOString())}（${TZ_LABEL}）`;
  const msgs = [];
  if (d.adjusted_hidden) msgs.push("符号验收未通过：调整后毛利与调整后毛利率不展示，暂按销量排序。");
  for (const w of d.waiting) msgs.push(`${w.store} 已进入新的一天，等待${w.waiting_for}后再计入。`);
  const banner = $("#board-banner");
  banner.className = msgs.length ? "banner" + (d.adjusted_hidden ? "" : " warn") : "banner hidden";
  banner.innerHTML = msgs.map(esc).join("<br>");

  $("#board").innerHTML = d.columns.map((c) => {
    const cell = (v) => `<td class="${v !== null && v < 0 ? "neg" : ""}">`;
    const rows = c.rows.length ? c.rows.map((r) => `<tr>
        <td><span class="store">${esc(r.store)}</span>${esc(r.msku)}</td>
        <td>${fmtQty(r.afn_volume)}</td>${cell(r.margin)}${fmtPct(r.margin)}</td>${cell(r.adjusted)}${fmtMoney(r.adjusted)}</td>
      </tr>`).join("") : `<tr class="no-rows"><td colspan="4">当前口径下没有可计入的 MSKU</td></tr>`;
    const t = c.total;
    return `<section class="board-col">
      <div class="col-head"><div class="col-name">${esc(c.name)}</div><div class="col-stores">${c.stores.map(esc).join("、") || "未选店铺"}</div></div>
      <table class="rank">
        <tr><th>店铺 · MSKU</th><th>销量</th><th>调整后毛利率</th><th>调整后毛利</th></tr>
        <tr class="total"><td>合计</td><td>${fmtQty(t.afn_volume)}</td>${cell(t.margin)}${fmtPct(t.margin)}</td>${cell(t.adjusted)}${fmtMoney(t.adjusted)}</td></tr>
        ${rows}
      </table></section>`;
  }).join("");
}

$("#exit-btn").addEventListener("click", () => openConfig(true));

openConfig(true).catch((err) => { $("#cfg-sub").textContent = `加载失败：${err.message}`; });
