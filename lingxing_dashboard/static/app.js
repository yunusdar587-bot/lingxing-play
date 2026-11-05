"use strict";

const state = {
  mode: localStorage.getItem("storageMode") || "daily",
  dashboard: null,
  selected: null,
  detail: null,
  excluded: new Set(JSON.parse(localStorage.getItem("excludedSids") || "[]").map(Number)),
  history: null,
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, digits = 2) =>
  v === null || v === undefined ? "—" : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const cny = (v) => (v === null || v === undefined ? "—" : "¥" + fmt(v));
const pct = (v) => (v === null || v === undefined ? "—" : (v * 100).toFixed(2) + "%");

const STATUS_LABELS = {
  ok: "就绪",
  no_q: "无 Q",
  no_age: "无库龄",
  no_volume: "无体积",
  no_rate: "无价目行",
  no_fx: "无汇率",
  unknown_lot: "含开账未知批次",
  no_t0: "未开账",
  unmapped: "fnsku 未映射",
  no_report: "上月报告未出",
  no_dims: "无尺寸",
};

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

function reasonText(reasons) {
  const labels = state.dashboard?.reason_labels || {};
  return Object.entries(reasons || {})
    .map(([k, n]) => `${labels[k] || STATUS_LABELS[k] || k} ${n}`)
    .join("，");
}

function renderModeSwitch() {
  document.querySelectorAll(".mode-switch button").forEach((b) => b.classList.toggle("active", b.dataset.mode === state.mode));
}

function renderSignBanner(d) {
  const el = $("sign-banner");
  const sc = d.sign_check || {};
  if (sc.status !== "failed") {
    el.classList.add("hidden");
    return;
  }
  const rows = Object.entries(sc.field_signs || {})
    .map(([f, v]) => `<tr><td>${esc(f)}</td><td class="${v.ok ? "" : "sign-bad"}">${fmt(v.value)}</td><td>应 ${esc(v.expected)}</td></tr>`)
    .join("");
  el.innerHTML = `<strong>符号验收未通过，调整后毛利暂不展示。</strong>
    样本 msku ${esc(sc.msku)}（店铺 ${esc(sc.sid)}）：加回原退款和原仓储后利润变化 ${fmt(sc.change)}，费用量级 ${fmt(sc.magnitude)}。
    当前按 COST_SIGN=${esc(sc.cost_sign)} 计算。<table>${rows}</table>
    <div>${(sc.lines || []).map(esc).join("<br>")}</div>`;
  el.classList.remove("hidden");
}

function renderTotal(d) {
  const listed = d.total.stores;
  const picked = listed.filter((s) => !state.excluded.has(s.sid));
  let sum = 0;
  let any = false;
  for (const s of picked) {
    const amt = s.amounts ? s.amounts[state.mode] : null;
    if (amt === null || amt === undefined) continue;
    sum += amt;
    any = true;
  }
  $("total-label").textContent = picked.length === listed.length
    ? "全部店铺人民币合计（各店「本店今天」相加）"
    : `已勾选 ${picked.length} 家店铺人民币合计（各店「本店今天」相加）`;
  $("store-pick-summary").textContent = `参与合计的店铺（${picked.length}/${listed.length}）`;
  $("total-value").textContent = d.adjusted_hidden ? "—（符号验收未通过）" : any || !picked.length ? cny(any ? sum : 0) : "—";
  $("total-mode").textContent = d.mode_labels[state.mode];
  $("total-tz").innerHTML = listed
    .map((s) => {
      const on = !state.excluded.has(s.sid);
      const note = s.counted && !s.counted[state.mode] ? " · 未就绪，未计入" : "";
      return `<label class="tz-chip${on ? "" : " off"}"><input class="store-check" type="checkbox" data-sid="${s.sid}"${on ? " checked" : ""}>${esc(s.name)} · ${esc(s.site)} · ${esc(s.tz)} · ${esc(s.local_date)}${esc(note)}</label>`;
    })
    .join("");
}

function renderStores(d) {
  $("stores").innerHTML = d.stores
    .map((s) => {
      const snap = s.snapshot;
      const m = snap ? snap.modes[state.mode] : null;
      let delta = "";
      if (m && m.delta !== null && m.delta !== undefined) {
        const cls = m.delta >= 0 ? "pos" : "neg";
        delta = `<span class="delta ${cls}">${m.delta >= 0 ? "▲" : "▼"} ${cny(Math.abs(m.delta))}</span>`;
      } else {
        delta = `<span class="sub">无上一张快照</span>`;
      }
      let value = "今天尚无快照";
      if (m) value = d.adjusted_hidden ? "—" : m.adjusted === null ? "未就绪" : cny(m.adjusted);
      const unready = m && m.excluded
        ? `<div class="unready">未就绪 ${m.excluded} 个 msku，已排除（${esc(reasonText(m.excluded_reasons))}）</div>`
        : `<div class="unready" style="color:var(--pos)">无未就绪 msku</div>`;
      const fallback = m && state.mode === "order" && m.fallback_to_daily ? `<div class="unready">订单仓储未就绪，按每日仓储费计算</div>` : "";
      return `<div class="store ${state.selected === s.sid ? "selected" : ""}" data-sid="${s.sid}">
        <div class="row1"><span class="name">${esc(s.name)}</span><span class="site-badge">${esc(s.site_name)}</span></div>
        <div class="value">${value}</div>
        <div class="meta"><span>本店今天 ${esc(s.local_date)} ${snap ? esc(snap.local_time) : ""}</span>${delta}</div>
        <div class="meta"><span>${esc(s.tz)}</span></div>
        ${snap ? unready : ""}${fallback}
      </div>`;
    })
    .join("");
  document.querySelectorAll(".store").forEach((el) =>
    el.addEventListener("click", () => {
      state.selected = Number(el.dataset.sid);
      renderStores(state.dashboard);
      loadDetail();
    })
  );
}

function renderPrice(d) {
  const p = d.price;
  const a = p.active;
  let html = `<div class="price-active"><div class="ver">${esc(a.version)}</div>
    生效日期 ${esc(a.effective_date)}<br>${a.confirmed_at ? "确认于 " + esc(a.confirmed_at) : "初始版本（未经人工确认切换）"}
    <div class="sub">${esc(a.notes)}</div></div>`;
  for (const v of p.pending) {
    html += `<div class="pending"><strong>新版本 ${esc(v.version)}</strong>（生效 ${esc(v.effective_date)}）已写入代码，尚未启用。
      <div class="sub">${esc(v.notes)}</div>
      <button class="btn" data-version="${esc(v.version)}">确认启用 ${esc(v.version)}</button></div>`;
  }
  $("price").innerHTML = html;
  document.querySelectorAll("#price .btn").forEach((b) =>
    b.addEventListener("click", async () => {
      const ver = b.dataset.version;
      if (!confirm(`确认启用价目表 ${ver}？弃置费与仓储费会按新版本重算。`)) return;
      b.disabled = true;
      const r = await fetch("/api/price-table/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version: ver }),
      });
      const body = await r.json();
      if (!r.ok) alert(body.error || "确认失败");
      await refresh();
    })
  );
}

function renderNotices(d) {
  const summary = $("notice-card").querySelector("summary");
  summary.textContent = `提示（${d.notices.length}）`;
  $("notices").innerHTML = d.notices.map((n) => `<li class="${esc(n.level)}">${esc(n.text)}</li>`).join("");
  $("polls").innerHTML = d.polls
    .map((p) => `<tr><td>${esc((p.scheduled_at || "").replace("T", " ").slice(5, 16))} UTC</td><td>${esc(p.status)}</td><td>${esc(p.message || "")}</td></tr>`)
    .join("");
  $("anomalies").innerHTML = d.anomalies.length
    ? d.anomalies.map((a) => `<li>${esc(a.at.slice(0, 16))} ${esc(a.kind)} ${esc(a.msku || "")}：${esc(a.message)}</li>`).join("")
    : "<li>暂无</li>";
}

function renderWaterfall(detail) {
  const s = detail.summary[state.mode];
  const hidden = detail.adjusted_hidden;
  const sc = detail.sign_check || {};
  const signs = sc.status === "failed" ? sc.field_signs || {} : {};
  const max = Math.max(1, ...s.steps.map((x) => Math.abs(x.amount || 0)), Math.abs(s.steps_total || 0));
  const bar = (v, cls) => `<div class="bar ${cls}" style="width:${(Math.abs(v || 0) / max) * 100}%"></div>`;
  let html = "";
  s.steps.forEach((st, i) => {
    const cls = i === 0 ? "base" : (st.amount || 0) >= 0 ? "pos" : "neg";
    const bad = signs[st.key] && !signs[st.key].ok ? " sign-bad" : "";
    html += `<tr class="${i === 0 ? "start" : ""}"><td class="${bad}">${esc(st.label)}</td>
      <td class="bar-cell">${st.amount === null ? "" : bar(st.amount, cls)}</td><td class="num${bad}">${st.amount === null ? "—" : cny(st.amount)}</td></tr>`;
  });
  const totalText = hidden ? "—" : s.adjusted === null ? "未就绪" : cny(s.steps_total);
  const totalBar = hidden || s.adjusted === null ? "" : bar(s.steps_total, "base");
  html += `<tr class="total"><td>调整后毛利</td><td class="bar-cell">${totalBar}</td><td class="num">${totalText}</td></tr>`;
  $("waterfall").innerHTML = html;
  const notes = [`计入 ${s.included} 个 msku`];
  if (s.excluded) notes.push(`未计入 ${s.excluded} 个（${reasonText(s.excluded_reasons)}），其原毛利 ${cny(s.excluded_gross_profit)}`);
  if (state.mode === "order") {
    notes.push(`今日售出：已知批次 ${s.order_known_units} 件，开账未知批次 ${s.order_unknown_units} 件`);
    if (s.fallback_to_daily) notes.push("订单仓储未就绪，本模式下毛利仍按每日仓储费计算");
    else if (s.order_unready_mskus) notes.push(`订单仓储未就绪 ${s.order_unready_mskus} 个 msku，未计入`);
  }
  $("waterfall-note").textContent = notes.join("；");

  $("retained").innerHTML = Object.values(s.retained)
    .map((r) => `<tr><td>${esc(r.label)}</td><td class="num">${r.amount === null ? "接口未返回" : cny(r.amount)}</td></tr>`)
    .join("");
}

function renderCurve(detail) {
  const pts = detail.curve.filter((p) => p[state.mode] !== null && p[state.mode] !== undefined);
  if (!pts.length) {
    $("curve").innerHTML = `<div class="empty">${detail.adjusted_hidden ? "符号验收未通过，不展示" : "暂无可展示的快照"}</div>`;
    return;
  }
  const W = 560, H = 200, P = { l: 64, r: 12, t: 12, b: 26 };
  const vals = pts.map((p) => p[state.mode]);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  if (hi === lo) hi = lo + 1;
  const x = (i) => P.l + (pts.length === 1 ? (W - P.l - P.r) / 2 : (i * (W - P.l - P.r)) / (pts.length - 1));
  const y = (v) => P.t + ((hi - v) * (H - P.t - P.b)) / (hi - lo);
  const path = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[state.mode]).toFixed(1)}`).join(" ");
  const ticks = [lo, (lo + hi) / 2, hi];
  const labelEvery = Math.max(1, Math.ceil(pts.length / 8));
  $("curve").innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    ${ticks.map((t) => `<line x1="${P.l}" x2="${W - P.r}" y1="${y(t)}" y2="${y(t)}" stroke="#e4e8ee"/>
      <text x="${P.l - 6}" y="${y(t) + 4}" font-size="10" text-anchor="end" fill="#6b7686">${fmt(t, 0)}</text>`).join("")}
    <line x1="${P.l}" x2="${W - P.r}" y1="${y(0)}" y2="${y(0)}" stroke="#b8c0cc" stroke-dasharray="3 3"/>
    <path d="${path}" fill="none" stroke="#2856d8" stroke-width="2"/>
    ${pts.map((p, i) => `<circle cx="${x(i)}" cy="${y(p[state.mode])}" r="3" fill="#2856d8"><title>${esc(p.local_time)} ${cny(p[state.mode])}</title></circle>`).join("")}
    ${pts.map((p, i) => (i % labelEvery === 0 ? `<text x="${x(i)}" y="${H - 8}" font-size="10" text-anchor="middle" fill="#6b7686">${esc(p.local_time)}</text>` : "")).join("")}
  </svg>`;
}

function statusTag(st, okText) {
  if (st === "ok") return `<span class="tag ok">${esc(okText || "就绪")}</span>`;
  return `<span class="tag warn">${esc(STATUS_LABELS[st] || st)}</span>`;
}

function renderMskuTable(detail) {
  const mode = state.mode === "order" && detail.summary.order.fallback_to_daily ? "daily" : state.mode;
  const head = `<tr><th>MSKU</th><th class="num">销量</th><th class="num">退货率 %</th><th class="num">预计退货成本</th>
    <th class="num">单件弃置费</th><th class="num">单日仓储费</th><th class="num">已过时段仓储</th>
    <th>订单仓储费</th><th class="num">调整后毛利</th></tr>`;
  const body = detail.rows
    .map((r) => {
      const adj = r.adjusted[mode];
      const order =
        r.order_storage_status === "ok"
          ? `${cny(r.order_storage_cny)} <span class="sub">已知 ${r.order_known_units} 件</span>`
          : `${statusTag(r.order_storage_status)} <span class="sub">已知 ${r.order_known_units} / 未知 ${r.order_unknown_units} 件</span>`;
      const qPct = r.q === null || r.q === undefined ? "" : String(Math.round(r.q * 10000) / 100);
      const qHint = r.q_source === "manual" ? "手填" : r.q_source === "assumed" ? "查不到，暂按 0" : (r.q_flagged ? "已打标" : "");
      const q = `<input class="q-input" type="number" min="0" max="100" step="0.01" inputmode="decimal"
        data-msku="${esc(r.msku)}" value="${esc(qPct)}" aria-label="${esc(r.msku)} 退货率百分数">`
        + (qHint ? `<div class="sub">${esc(qHint)}</div>` : "");
      const rc = r.return_cost_status === "ok" ? cny(r.return_cost) : `<span class="tag warn">未就绪</span>`;
      const disp = cny(r.disposal_unit_cny) + (r.disposal_status !== "ok" ? " " + statusTag(r.disposal_status) : "");
      const daily = r.daily_storage_status === "ok" ? cny(r.daily_storage_cny) : statusTag(r.daily_storage_status);
      const elapsed = r.daily_storage_status === "ok" ? cny(r.elapsed_storage_cny) : "—";
      const adjText = detail.adjusted_hidden ? "—" : adj === null ? `<span class="tag warn">未计入</span>` : cny(adj);
      return `<tr class="${adj === null ? "excluded" : ""}">
        <td><div>${esc(r.msku)}</div><div class="sub">${esc(r.fnsku)} ${esc(r.size_tier)}</div></td>
        <td class="num">${fmt(r.afn_volume, 0)}</td><td class="num">${q}</td><td class="num">${rc}</td>
        <td class="num">${disp}</td><td class="num">${daily}</td><td class="num">${elapsed}</td>
        <td>${order}</td><td class="num">${adjText}</td></tr>`;
    })
    .join("");
  $("msku-table").innerHTML = head + body;
}

function renderDetail() {
  const detail = state.detail;
  if (!detail) return;
  $("detail").classList.remove("hidden");
  $("detail-title").textContent = detail.name;
  if (!detail.summary) {
    $("detail-sub").textContent = "本店今天尚无快照";
    $("waterfall").innerHTML = "";
    $("retained").innerHTML = "";
    $("msku-table").innerHTML = "";
    $("curve").innerHTML = "";
    return;
  }
  const s = detail.summary;
  const fx = s.fx ? `汇率 ${s.fx.code}→CNY ${s.fx.rate}（${s.fx.month}${s.fx.fallback ? "，非当月" : ""}）` : "无汇率";
  const q = s.q_window ? `Q 窗口 ${s.q_window.start}~${s.q_window.end}` : "Q 未就绪";
  $("detail-sub").textContent = `${s.site_name} · ${s.tz} · 本店今天 ${s.local_date} ${s.local_time} · 已过 ${(s.elapsed_fraction * 100).toFixed(1)}% · ${fx} · ${q} · 价目 ${s.price_version}`;
  const m = s[state.mode];
  $("detail-adjusted").textContent = detail.adjusted_hidden ? "—" : m.adjusted === null ? "未就绪" : cny(m.adjusted);
  $("detail-margin").textContent = m.margin === null || m.margin === undefined ? "" : `调整后毛利率 ${pct(m.margin)}`;
  renderWaterfall(detail);
  renderCurve(detail);
  renderMskuTable(detail);
}

async function loadDetail() {
  if (!state.selected) return;
  state.detail = await getJSON(`/api/store/${state.selected}`);
  renderDetail();
}

function slotValue(input) {
  return input.value ? input.value.replace("T", " ") : "";
}

function renderHistory() {
  const box = $("hist-result");
  const view = state.history;
  if (!view) {
    box.innerHTML = "";
    return;
  }
  let sum = 0;
  let any = false;
  let html = "";
  for (const store of view.stores) {
    const last = store.points.length ? store.points[store.points.length - 1] : null;
    const lastAmt = last ? last.adjusted[state.mode] : null;
    if (!state.excluded.has(store.sid) && lastAmt !== null && lastAmt !== undefined) {
      sum += lastAmt;
      any = true;
    }
    const rows = store.points.map((p) => {
      const amt = p.adjusted[state.mode];
      return `<tr>
        <td>${esc(p.local_slot)}</td>
        <td class="num">${amt === null || amt === undefined ? "—" : cny(amt)}</td>
        <td class="num">${p.afn_volume === null || p.afn_volume === undefined ? "—" : fmt(p.afn_volume, 0)}</td>
        <td class="num">${p.msku_count ?? "—"}</td>
        <td><button class="link" type="button" data-sid="${store.sid}" data-slot="${esc(p.local_slot)}">${p.has_payload ? "接口明细" : "仅汇总"}</button></td>
      </tr>`;
    }).join("");
    html += `<div class="history-store">
      <h3>${esc(store.name)} <span class="sub">${esc(store.site_name)} · ${esc(store.tz)} · 留存 ${store.kept}/${view.slot_count}</span></h3>
      ${rows ? `<table class="history-table"><tr><th>当地刻度</th><th class="num">调整后毛利</th><th class="num">销量</th><th class="num">MSKU</th><th></th></tr>${rows}</table>` : `<div class="sub">这一段没有留存</div>`}
      <div class="hist-detail" id="hist-detail-${store.sid}"></div>
    </div>`;
  }
  const total = state.dashboard && state.dashboard.adjusted_hidden ? "—（符号验收未通过）" : any ? cny(sum) : "—";
  box.innerHTML = `<div class="sub">${esc(view.start)} 至 ${esc(view.end)}，每 15 分钟。已勾选店铺在各自区间内最后一条留存的合计 ${total}。每条是该店当地 0 点到该刻度的累计，不是这 15 分钟的增量。</div>${html}`;
}

async function loadHistoryBounds() {
  try {
    const b = await getJSON("/api/history/bounds");
    const note = $("hist-note");
    if (!b.count) {
      note.textContent = "库里还没有留存。下一次 15 分钟轮询之后可以拉取。时刻按各店自己的时区。";
      return;
    }
    note.textContent = `库内 ${b.count} 条，最早 ${b.min}，最晚 ${b.max}。开始和结束是钟面时间，各店按自己的时区去对，只返回已经留下的刻度。`;
    if (!$("hist-start").value) $("hist-start").value = b.max.slice(0, 10) + "T00:00";
    if (!$("hist-end").value) $("hist-end").value = b.max.replace(" ", "T");
  } catch (e) {
    $("hist-note").textContent = "历史库读取失败：" + e.message;
  }
}

async function pullHistory() {
  const start = slotValue($("hist-start"));
  const end = slotValue($("hist-end"));
  const btn = $("hist-load");
  if (!start || !end) {
    $("hist-note").textContent = "请先选择开始和结束时刻。";
    return;
  }
  btn.disabled = true;
  try {
    const r = await fetch(`/api/history?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`, { cache: "no-store" });
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || r.status);
    state.history = body;
    renderHistory();
  } catch (e) {
    state.history = null;
    $("hist-result").innerHTML = "";
    $("hist-note").textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

function renderAll() {
  const d = state.dashboard;
  if (!d) return;
  $("generated").textContent = `更新于 ${new Date(d.generated_at).toLocaleString("zh-CN")}（浏览器本地时间）`;
  renderModeSwitch();
  renderSignBanner(d);
  renderTotal(d);
  renderStores(d);
  renderPrice(d);
  renderNotices(d);
  renderHistory();
  renderDetail();
}

async function refresh() {
  try {
    state.dashboard = await getJSON("/api/dashboard");
    if (!state.selected && state.dashboard.stores.length) state.selected = state.dashboard.stores[0].sid;
    renderAll();
    await loadDetail();
    if (!state.historyBoundsLoaded) {
      state.historyBoundsLoaded = true;
      await loadHistoryBounds();
    }
  } catch (e) {
    $("generated").textContent = "加载失败：" + e.message;
  }
}

$("msku-table").addEventListener("change", async (ev) => {
  const input = ev.target;
  if (!input.classList || !input.classList.contains("q-input") || !state.selected) return;
  const raw = input.value.trim();
  const percent = raw === "" ? null : Number(raw);
  if (percent !== null && (!Number.isFinite(percent) || percent < 0 || percent > 100)) {
    input.classList.add("bad");
    return;
  }
  input.classList.remove("bad");
  input.disabled = true;
  try {
    const res = await fetch(`/api/store/${state.selected}/return-rate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ msku: input.dataset.msku, percent }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || res.status);
    state.detail = body;
    state.dashboard = await getJSON("/api/dashboard");
    renderAll();
  } catch (e) {
    input.disabled = false;
    input.classList.add("bad");
    $("generated").textContent = "退货率保存失败：" + e.message;
  }
});

$("hist-load").addEventListener("click", pullHistory);
$("hist-result").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button.link");
  if (!btn) return;
  const sid = btn.dataset.sid;
  const slot = btn.dataset.slot;
  const host = $("hist-detail-" + sid);
  if (!host) return;
  host.innerHTML = `<div class="sub">读取 ${esc(slot)} …</div>`;
  try {
    const d = await getJSON(`/api/history/detail?sid=${encodeURIComponent(sid)}&slot=${encodeURIComponent(slot)}`);
    if (!d.has_payload) {
      host.innerHTML = `<div class="sub">${esc(slot)} 只有汇总，没有接口明细。</div>`;
      return;
    }
    if (!d.rows.length) {
      host.innerHTML = `<div class="sub">${esc(slot)} 的接口没有订单行。</div>`;
      return;
    }
    const cur = d.currency || "";
    const body = d.rows.map((r) => `<tr>
      <td>${esc(r.msku)}</td><td class="num">${fmt(r.afn_volume, 0)}</td>
      <td class="num">${fmt(r.gross_profit)}</td><td class="num">${fmt(r.net_amount)}</td>
      <td class="num">${fmt(r.selling_fee)}</td><td class="num">${fmt(r.refund_amount)}</td>
    </tr>`).join("");
    host.innerHTML = `<div class="sub">${esc(d.name)} ${esc(slot)} 接口原值${cur ? "（" + esc(cur) + "）" : ""}，该店当地 0 点至该刻度累计</div>
      <table class="history-table"><tr><th>MSKU</th><th class="num">销量</th><th class="num">原毛利</th><th class="num">净额</th><th class="num">销售佣金</th><th class="num">退款</th></tr>${body}</table>`;
  } catch (e) {
    host.innerHTML = `<div class="sub">读取失败：${esc(e.message)}</div>`;
  }
});

$("total-tz").addEventListener("change", (ev) => {
  const box = ev.target;
  if (!box.classList || !box.classList.contains("store-check") || !state.dashboard) return;
  const sid = Number(box.dataset.sid);
  if (box.checked) state.excluded.delete(sid);
  else state.excluded.add(sid);
  localStorage.setItem("excludedSids", JSON.stringify([...state.excluded]));
  renderTotal(state.dashboard);
  renderHistory();
});

document.querySelectorAll(".mode-switch button").forEach((b) =>
  b.addEventListener("click", () => {
    state.mode = b.dataset.mode;
    localStorage.setItem("storageMode", state.mode);
    renderAll();
  })
);

refresh();
setInterval(refresh, 60000);
