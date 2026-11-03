/* trading-agents web UI: plain JS, no build step. Read-only except for queueing an analysis on the live page. */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path) {
  const r = await fetch(path);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* not json */ }
    throw Object.assign(new Error(msg), { status: r.status });
  }
  return r.json();
}

const money = v => v == null ? "–" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const num = (v, d = 2) => v == null ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
const pct = (v, d = 2) => v == null ? "–" : (v >= 0 ? "+" : "") + Number(v).toFixed(d) + "%";
const sign = v => v > 0 ? "g" : v < 0 ? "r" : "d";
const RATING = { Buy: "g", Overweight: "g", Hold: "a", Underweight: "r", Sell: "r" };
const ratingPill = r => r ? `<span class="pill fill ${RATING[r] || "d"}">${esc(r)}</span>` : '<span class="f">–</span>';
const dur = s => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s % 60).padStart(2, "0"); };
const mins = s => s < 90 ? "about a minute" : s < 5400 ? `about ${Math.round(s / 60)} min` : `about ${(s / 3600).toFixed(1)} h`;
const hhmmss = iso => iso ? iso.slice(11, 19) : "";
const when = iso => iso ? iso.slice(0, 10) + " " + iso.slice(11, 16) : "";
const team = n => /Bull/.test(n) ? "g" : /Bear/.test(n) ? "r" : /Manager/.test(n) ? "m" : n === "Trader" ? "b" : /Aggressive|Conservative|Neutral/.test(n) ? "a" : "c";
const PALETTE = ["#45e07a", "#5ccfe6", "#ffb84d", "#d291ff", "#6aa7ff", "#ff8fa3", "#c3e88d"];
const TABS = [["dash", "dashboard"], ["live", "live"], ["runs", "analyses"], ["cycles", "daily runs"], ["market", "market"], ["flow", "flow"]];
const STANCE = { enter_new: ["g", "entering new positions"], maintain: ["c", "maintaining current positions"], rebalance: ["a", "rebalancing"], do_nothing: ["d", "doing nothing"] };

const S = {
  tab: "dash", arg: null, config: {}, live: { running: false, log: [], queue: { items: [], max: 3, waiting: 0 } }, schedule: null,
  portfolio: null, portfolioErr: null, analyses: [], trades: [], watchlist: [], filter: "", q: "", detail: null, stats: null, flowSel: null,
  market: { ticker: "AAPL", data: null, loading: false }, open: {}, down: false,
};

/* ---------- routing ---------- */
function go(tab, arg) { location.hash = arg ? `${tab}/${arg}` : tab; }
function route() {
  const [tab, ...rest] = location.hash.slice(1).split("/");
  S.tab = TABS.some(t => t[0] === tab) ? tab : "dash";
  S.arg = rest.join("/") || null;
  if (S.tab === "runs" && S.arg && S.detail?.id !== S.arg) loadDetail(S.arg);
  if (S.tab === "market") loadMarket(S.arg || S.market.ticker);
  paint(true);
}

/* ---------- small helpers ---------- */
function stat(k, v, s = "", c = "") {
  return `<div class="stat"><div class="k">${k}</div><div class="v ${c}">${v}</div><div class="s">${s}</div></div>`;
}
function panel(title, body, extra = "", cls = "") {
  return `<section class="panel ${cls}"><h2>${title}${extra ? `<span class="r">${extra}</span>` : ""}</h2>${body}</section>`;
}
const empty = t => `<div class="empty">${t}</div>`;
const setHTML = (id, html) => { const e = document.getElementById(id); if (e && e.dataset.h !== html) { e.innerHTML = html; e.dataset.h = html; } };

function nextRunText() {
  const sc = S.schedule;
  if (!sc || !sc.enabled) return ['<span class="a">off</span>', "scheduler disabled"];
  return [`<span data-next="${esc(sc.next_run)}">…</span>`, `${sc.rule} · ${sc.execute ? "trades the account" : "dry run"}`];
}
function progressBar(done, total, width = 24) {
  const f = total ? Math.round(width * done / total) : 0;
  return `<span class="progress g">${"█".repeat(f)}<span class="f">${"░".repeat(width - f)}</span></span> <span class="d">${done}/${total}</span>`;
}
function liveSummary() {
  const L = S.live, a = L.analysis;
  if (!L.running) {
    const last = L.last;
    return `<span class="dot"></span> <b>idle</b>` + (last ? ` <span class="d">· last: ${esc(last.kind)} ${esc(last.label)} ${last.ok ? '<span class="g">ok</span>' : '<span class="r">failed</span>'} in ${dur(last.seconds)} — ${esc(last.message)}</span>` : "");
  }
  const done = a ? a.stages.filter(s => s.done).length : 0, total = a ? a.stages.length : 0;
  const cur = a ? a.stages.filter(s => s.state === "running").map(s => s.name) : [];
  return `<span class="dot busy"></span> <b class="a">running</b> ${esc(L.kind)} <b>${esc(L.label)}</b> <span class="d">· <span data-since="${esc(L.started)}"></span></span>` +
    (a ? `<div style="margin-top:6px">${esc(a.ticker)} ${progressBar(done, total)} <span class="a">${esc(cur.join(", "))}</span></div>` : "");
}
function recentTable(list) {
  if (!list.length) return empty("No analyses yet.");
  return `<div class="table-wrap"><table>${list.map(a =>
    `<tr class="click" data-go="runs/${esc(a.id)}"><td class="f">${esc(when(a.time))}</td><td class="c">${esc(a.ticker)}</td><td>${ratingPill(a.rating)}</td><td class="n">${money(a.price)}</td></tr>`).join("")}</table></div>`;
}

/* ---------- dashboard ---------- */
function txTable(list) {
  if (!list || !list.length) return empty("No transactions yet. Orders the account really took (and cash added) show up here.");
  const kinds = { buy: ["BUY", "g"], sell: ["SELL", "r"], deposit: ["CASH IN", "c"] };
  return `<div class="table-wrap"><table><tr><th>time</th><th>type</th><th>symbol</th><th class="n">qty</th><th class="n">price</th><th class="n">amount</th><th class="n">p&amp;l</th><th>note</th></tr>${list.slice(0, 25).map(t => {
    const [lbl, c] = kinds[t.type] || [String(t.type).toUpperCase(), ""];
    return `<tr${t.symbol ? ` class="click" data-go="market/${esc(t.symbol)}"` : ""}><td class="f">${esc(when(t.time))}</td><td class="${c}">${lbl}</td><td class="c">${esc(t.symbol || "")}</td>` +
      `<td class="n">${t.qty != null ? num(t.qty, 3) : "—"}</td><td class="n">${t.price != null ? money(t.price) : "—"}</td><td class="n">${t.amount != null ? money(t.amount) : "—"}</td>` +
      `<td class="n ${t.pnl != null ? sign(t.pnl) : ""}">${t.pnl != null ? (t.pnl >= 0 ? "+" : "−") + money(Math.abs(t.pnl)) : ""}</td><td class="d" style="padding-left:16px">${esc(t.note || "")}</td></tr>`;
  }).join("")}</table></div>`;
}

function viewDash() {
  const p = S.portfolio;
  let h = "";
  if (S.portfolioErr) h += panel("portfolio", `<div class="r">${esc(S.portfolioErr)}</div>`);
  if (p) {
    const hist = p.equity_history || [];
    const base = p.deposited ?? (hist.length ? hist[0].equity : p.equity);
    const pnl = p.equity - base;
    const [nx, nxs] = nextRunText();
    h += `<div class="stats">
      ${stat("equity", money(p.equity), esc(p.mode))}
      ${stat("cash", money(p.cash), pct(p.cash / p.equity * 100, 1) + " of equity")}
      ${stat("p&amp;l", `${pnl >= 0 ? "+" : "−"}${money(Math.abs(pnl))}`, pct(base ? pnl / base * 100 : 0) + (p.deposited != null ? " on " + money(base) + " put in" : ""), sign(pnl))}
      ${stat("drawdown", p.drawdown_pct ? "−" + p.drawdown_pct.toFixed(2) + "%" : "0.00%", "peak " + money(p.peak_equity), p.drawdown_pct > 5 ? "r" : "")}
      ${stat("next daily run", nx, nxs)}
    </div>`;
    const total = p.equity || 1;
    const rows = p.positions.map((x, i) => ({ lbl: x.symbol, v: x.market_value, c: PALETTE[i % PALETTE.length] }));
    rows.push({ lbl: "cash", v: p.cash, c: "#35493f" });
    const alloc = rows.map(r => `<div class="bar"><span class="lbl">${esc(r.lbl)}</span><span class="track"><span class="fill" style="display:block;width:${(r.v / total * 100).toFixed(1)}%;background:${r.c}"></span></span><span class="val">${(r.v / total * 100).toFixed(1)}% · ${money(r.v)}</span></div>`).join("");
    h += `<div class="grid g2">${panel("equity curve", '<div id="eqChart"></div>')}${panel("allocation", alloc)}</div>`;
    const pos = p.positions.length ? `<div class="table-wrap"><table><tr><th>symbol</th><th class="n">qty</th><th class="n">avg entry</th><th class="n">price</th><th class="n">value</th><th class="n">p&amp;l</th></tr>${p.positions.map(x =>
      `<tr class="click" data-go="market/${esc(x.symbol)}"><td class="c">${esc(x.symbol)}</td><td class="n">${num(x.qty, 4)}</td><td class="n">${money(x.avg_entry)}</td><td class="n">${money(x.price)}</td><td class="n">${money(x.market_value)}</td><td class="n ${sign(x.pnl_pct)}">${pct(x.pnl_pct)}</td></tr>`).join("")}</table></div>` : empty("No open positions. The daily cycle decides when to open some.");
    h += panel("positions", pos, `${p.positions.length} open`);
    h += panel("recent transactions", txTable(p.transactions), p.transactions?.length ? `last ${Math.min(25, p.transactions.length)}` : "");
  } else if (!S.portfolioErr) h += panel("portfolio", empty("loading…"));
  h += `<div class="grid g2e">${panel("live", liveSummary() + `<div style="margin-top:8px"><a data-go="live">open pipeline view →</a></div>`, "", S.live.running ? "hot" : "")}
    ${panel("recent analyses", recentTable(S.analyses.slice(0, 6)), `<a data-go="runs">all</a>`)}</div>`;
  return h;
}

/* ---------- live ---------- */
function stageRow(s, i, isLastDone) {
  const key = `${s.name}#${i}`;
  const ico = s.state === "done" ? '<span class="g">✔</span>' : s.state === "running" ? '<span class="spin">◐</span>' : '<span class="f">○</span>';
  const secs = s.done ? dur(s.seconds) : s.state === "running" ? "…" : "";
  if (!s.done) return `<div class="stage ${s.state}"><summary>${ico}<span class="nm ${s.state === "running" ? team(s.name) : "f"}">${esc(s.name)}</span><span class="sec">${secs}</span></summary></div>`;
  const open = S.open[key] ?? isLastDone;
  return `<details class="stage done" data-key="${esc(key)}" ${open ? "open" : ""}><summary>${ico}<span class="nm ${team(s.name)}">${esc(s.name)}</span><span class="sec">${secs}</span></summary><div class="out">${esc(s.output || "(no text)")}</div></details>`;
}

const LIVE_SKELETON = `<div id="liveRoot"><div id="lv-status"></div><div id="lv-pipe"></div>
  <section class="panel"><h2>queue an analysis<span class="r" id="lv-qcount"></span></h2>
    <form class="qform" id="qForm" autocomplete="off"><input id="qTicker" maxlength="7" placeholder="ticker, e.g. NVDA" aria-label="Ticker to analyse"><button class="btn" id="qBtn" type="submit">queue</button></form>
    <div class="msg err" id="qLock" hidden></div><div class="msg" id="qMsg"></div><div id="lv-qlist"></div>
    <div class="chips" id="qChips" style="margin-top:10px"></div></section>
  <div id="lv-log"></div></div>`;

function queueList() {
  const Q = S.live.queue || { items: [], max: 3 };
  if (!Q.items.length) return empty(Q.lock ? "Nothing queued." : "Nothing queued. The pipeline is free: your analysis starts right away.");
  const avg = Q.avg_analysis_seconds || 1500;
  return Q.items.map(it => {
    const src = it.source === "user" ? "visitor" : it.source;
    const eta = it.running ? '<span class="a">running now</span>' : it.held ? '<span class="a">waits for the daily cycle</span>' : it.kind === "analysis" ? `starts in ${mins(avg * it.position)}` : "after the ones ahead";
    return `<div class="qrow"><span class="pos">${it.running ? "▶" : "#" + it.position}</span><span class="lbl ${it.running ? "a" : ""}">${esc(it.label)}</span><span class="f">${esc(src)}</span><span class="eta">${eta}</span></div>`;
  }).join("");
}

function fillLive() {
  const L = S.live, a = L.analysis, Q = L.queue || { waiting: 0, max: 3 };
  setHTML("lv-status", panel("status", liveSummary() + (S.schedule?.enabled ? `<div class="d" style="margin-top:6px">next daily cycle in ${nextRunText()[0]} · ${esc(S.schedule.rule)}</div>` : ""), "", L.running ? "hot" : ""));
  if (a) {
    const done = a.stages.filter(s => s.done).length, lastDone = a.stages.map(s => s.done).lastIndexOf(true);
    setHTML("lv-pipe", panel(`pipeline — ${esc(a.ticker)}`, a.stages.map((s, i) => stageRow(s, i, i === lastDone)).join(""), progressBar(done, a.stages.length, 16)));
  } else setHTML("lv-pipe", L.running ? panel("pipeline", empty("Screening the watchlist or planning orders (no stock analysis at this moment)…")) : "");
  const qc = document.getElementById("lv-qcount"); if (qc) qc.textContent = `${Q.waiting}/${Q.max} waiting`;
  const lk = document.getElementById("qLock"), locked = !!Q.lock;
  if (lk) { lk.hidden = !locked; lk.textContent = locked ? Q.lock.message : ""; }
  document.querySelectorAll("#qTicker,#qBtn,#qChips .chip").forEach(el => { el.disabled = locked; });
  setHTML("lv-qlist", queueList());
  const chips = document.getElementById("qChips");
  if (chips && !chips.dataset.f && S.watchlist.length) { chips.innerHTML = S.watchlist.map(t => `<button class="chip" data-analyze="${esc(t)}">${esc(t)}</button>`).join(""); chips.dataset.f = "1"; }
  const log = (L.log || []).slice(-80).map(l => `<div><span class="t">${esc(hhmmss(l.t))}</span>${esc(l.text)}</div>`).join("");
  const lb = document.getElementById("logbox"), atBottom = !lb || lb.scrollHeight - lb.scrollTop - lb.clientHeight < 30;
  setHTML("lv-log", panel("log", `<div class="log" id="logbox">${log || empty("nothing yet")}</div>`));
  const nb = document.getElementById("logbox"); if (nb && atBottom) nb.scrollTop = nb.scrollHeight;
}

async function queueAnalysis(ticker) {
  const msg = $("#qMsg"), btn = $("#qBtn");
  const t = ticker.trim().toUpperCase();
  if (!t || S.live.queue?.lock) return;
  if (btn) btn.disabled = true;
  try {
    const r = await fetch("/api/queue", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ticker: t }) });
    const d = await r.json();
    if (!r.ok) {
      const ahead = (d.queue?.items || []).map(i => `${i.running ? "running" : "#" + i.position}: ${i.label}`).join("  ·  ");
      msg.className = "msg err"; msg.textContent = (d.error || d.detail || "Could not queue that.") + (ahead ? `\nIn the queue now → ${ahead}` : "");
    } else {
      const avg = d.queue.avg_analysis_seconds || 1500;
      msg.className = "msg ok"; msg.textContent = d.position === 1 ? `${t} is next: it starts right away.` : `${t} is queued at position ${d.position}, starting in ${mins(avg * (d.position - 1))} or so. Watch it on this page.`;
      $("#qTicker").value = "";
    }
    pollLive();
  } catch (e) { msg.className = "msg err"; msg.textContent = "Could not reach the server."; }
  if (btn) btn.disabled = !!S.live.queue?.lock;
}

/* ---------- analyses ---------- */
function filteredAnalyses() {
  const q = S.q.trim().toLowerCase();
  return S.analyses.filter(a => (!S.filter || a.ticker === S.filter) && (!q || `${a.ticker} ${a.rating} ${a.time}`.toLowerCase().includes(q)));
}
function runListHtml() {
  const list = filteredAnalyses();
  return list.length ? `<div class="table-wrap"><table>${list.map(a =>
    `<tr class="click ${S.arg === a.id ? "sel" : ""}" data-go="runs/${esc(a.id)}"><td class="f">${esc(when(a.time))}</td><td class="c">${esc(a.ticker)}</td><td>${ratingPill(a.rating)}</td><td class="n">${money(a.price)}</td></tr>`).join("")}</table></div>` : empty("No analyses match.");
}
function viewRuns() {
  const tickers = [...new Set(S.analyses.map(a => a.ticker))].sort();
  const chips = `<div class="chips" style="margin-bottom:10px"><button class="chip ${S.filter ? "" : "on"}" data-filter="">all</button>${tickers.map(t => `<button class="chip ${S.filter === t ? "on" : ""}" data-filter="${esc(t)}">${esc(t)}</button>`).join("")}</div>`;
  const search = `<div class="qform"><input id="runSearch" value="${esc(S.q)}" placeholder="search: ticker, Buy, 2026-11…" style="text-transform:none;width:100%" aria-label="Search analyses"></div>`;
  return `<div class="grid g-runs">${panel("analyses", search + chips + `<div id="runList">${runListHtml()}</div>`, `${S.analyses.length}`)}<div>${runDetail()}</div></div>`;
}
function runDetail() {
  const r = S.detail;
  if (!S.arg) return panel("detail", empty("Select an analysis to read every agent's output and reasoning."));
  if (!r) return panel("detail", empty("loading…"));
  if (r.error) return panel("detail", `<div class="r">${esc(r.error)}</div>`);
  const st = r.state, d = st.final_decision || {};
  const hist = S.analyses.filter(a => a.ticker === st.ticker && a.id !== r.id).slice(0, 5);
  let h = panel(`${esc(st.ticker)} · ${esc(when(r.time))}`, `
    <div class="split"><div><div class="big ${RATING[d.rating] || ""}">${esc(d.rating || "–")}</div>
    <div class="d">price ${money(st.last_price)} · target ${money(d.target_price)} · stop ${money(d.stop_loss)} · size ${num(d.position_size, 1)}%</div></div>
    <div class="d" style="text-align:right">${esc(r.model || "")}<br>took ${dur(r.seconds || 0)}</div></div>
    <div class="prose" style="margin-top:12px">${esc(d.justification || "")}</div>
    ${st.past_context ? `<div class="d prose" style="margin-top:10px"><b>memory →</b> ${esc(st.past_context)}</div>` : ""}`);
  const stages = (r.stages || []).map((s, i) => {
    const key = `run:${s.name}#${i}`;
    return `<details class="stage done" data-key="${esc(key)}" ${S.open[key] ? "open" : ""}><summary><span class="g">✔</span><span class="nm ${team(s.name)}">${esc(s.name)}</span><span class="sec">${dur(s.seconds || 0)}</span></summary><div class="out">${esc(s.output || "(no text)")}</div></details>`;
  }).join("");
  h += panel("agent transcript", `<div class="d" style="margin-bottom:6px"><a data-expand="1">expand all</a> · <a data-expand="0">collapse all</a></div>` + stages);
  if (hist.length) h += panel(`earlier calls on ${esc(st.ticker)}`, recentTable(hist));
  return h;
}

/* ---------- daily runs ---------- */
function viewCycles() {
  if (!S.trades.length) return panel("daily runs", empty(S.schedule?.enabled ? "No daily cycle has run yet. The next one starts automatically (see the countdown on the dashboard)." : "No daily cycle has run yet."));
  return S.trades.map(t => {
    const ids = t.analysis_ids || {}, screen = t.screen || {};
    const stance = STANCE[t.stance];
    const decs = Object.entries(t.decisions || {}).map(([sym, d]) =>
      `<span style="margin-right:16px">${ids[sym] ? `<a data-go="runs/${esc(ids[sym])}">${esc(sym)}</a>` : `<span class="c">${esc(sym)}</span>`} ${ratingPill(d.rating)}${screen[sym] ? ` <span class="f">${esc(screen[sym].why)}</span>` : ""}</span>`).join("");
    const errs = Object.entries(t.errors || {}).map(([sym, e]) => `<div class="r">${esc(sym)}: ${esc(e)}</div>`).join("");
    const ord = (t.approved || []).length ? `<div class="table-wrap"><table><tr><th>side</th><th>symbol</th><th class="n">notional</th><th class="n">stop</th><th class="n">take</th><th>reason</th></tr>${t.approved.map(o =>
      `<tr><td class="${o.side === "buy" ? "g" : "r"}">${esc(o.side.toUpperCase())}</td><td class="c">${esc(o.symbol)}</td><td class="n">${money(o.notional)}</td><td class="n">${num(o.stop_loss)}</td><td class="n">${num(o.take_profit)}</td><td class="prose">${esc(o.reason)}</td></tr>`).join("")}</table></div>` : '<div class="d">no orders approved</div>';
    const rej = (t.rejected || []).map(o => `<div class="r">✘ ${esc(o.side.toUpperCase())} ${esc(o.symbol)} — ${esc(o.reason)}</div>`).join("");
    const fills = (t.fills || []).map(f => `<div class="${f.error ? "r" : "g"}">${f.error ? "✘" : "✔"} ${esc(f.side)} ${esc(f.symbol)} ${esc(f.order_id || f.error)}</div>`).join("");
    return panel(`${esc(when(t.time))} · ${t.executed ? '<span class="a">TRADED</span>' : "dry run"}`, `
      <div class="d">equity ${money(t.equity)} · cash ${money(t.cash)} · candidates ${esc((t.candidates || []).join(", ") || "none")}</div>
      ${stance ? `<div style="margin:8px 0">decision: <b class="${stance[0]}">${esc(stance[1])}</b></div>` : ""}
      <div style="margin:8px 0">${decs || '<span class="d">no analyses completed</span>'}</div>${errs}
      ${t.summary ? `<div class="prose" style="margin:8px 0"><b class="m">planner →</b> ${esc(t.summary)}</div>` : ""}
      ${ord}${rej}${fills}`);
  }).join("");
}

/* ---------- market ---------- */
function viewMarket() {
  const M = S.market, d = M.data;
  const chips = `<div class="chips">${S.watchlist.map(t => `<button class="chip ${t === M.ticker ? "on" : ""}" data-go="market/${esc(t)}">${esc(t)}</button>`).join("")}</div>`;
  let h = panel("ticker", chips + `<div class="d" style="margin-top:8px">or search any ticker in the box at the top (press /)</div>`);
  if (M.loading && !d) return h + panel(esc(M.ticker), empty("loading prices, fundamentals and news…"));
  if (!d) return h;
  h += panel(`${esc(M.ticker)} price · 1y`, d.priceErr ? `<div class="r">${esc(d.priceErr)}</div>` : '<div id="pxChart"></div>', "● = our analyses");
  if (!d.priceErr) h += panel("rsi (14)", '<div id="rsiChart"></div>');
  const f = d.fund && typeof d.fund === "object" ? Object.entries(d.fund).filter(([, v]) => v == null || typeof v !== "object") : [];
  const fmtF = v => typeof v === "number" ? (Math.abs(v) >= 1e9 ? (v / 1e9).toFixed(2) + "B" : Math.abs(v) >= 1e6 ? (v / 1e6).toFixed(2) + "M" : num(v, 3)) : esc(v ?? "–");
  const fund = d.fundErr ? `<div class="r">${esc(d.fundErr)}</div>` : `<div class="kv">${f.map(([k, v]) => `<span class="k">${esc(k)}</span><span class="v">${fmtF(v)}</span>`).join("")}</div>`;
  const news = d.newsErr ? `<div class="r">${esc(d.newsErr)}</div>` : `<ul class="news">${(d.news || []).map(n => {
    const ts = n.providerPublishTime; const ms = typeof ts === "number" ? ts * 1000 : Date.parse(ts);
    const t = isNaN(ms) ? String(ts || "") : new Date(ms).toISOString().slice(0, 10);
    return `<li>${n.link ? `<a href="${esc(n.link)}" target="_blank" rel="noopener">${esc(n.title)}</a>` : esc(n.title)}<div class="f">${esc(t)} ${esc(n.publisher || "")}</div></li>`;
  }).join("") || empty("no headlines")}</ul>`;
  h += `<div class="grid g2e">${panel("fundamentals", fund)}${panel("news", news)}</div>`;
  h += panel(`our calls on ${esc(M.ticker)}`, recentTable(S.analyses.filter(a => a.ticker === M.ticker)));
  return h;
}

/* ---------- paint ---------- */
let lastHtml = "";
function paint(force) {
  $("#tabs").innerHTML = TABS.map(([k, n], i) => `<button class="${S.tab === k ? "on" : ""}" data-go="${k}"><b>${i + 1}</b>${n}</button>`).join("");
  if (S.tab === "live") {
    if (!document.getElementById("liveRoot") || force) { $("#view").innerHTML = LIVE_SKELETON; lastHtml = ""; wireLive(); }
    fillLive();
  } else {
    const body = { dash: viewDash, runs: viewRuns, cycles: viewCycles, market: viewMarket, flow: viewFlow }[S.tab]();
    if (force || body !== lastHtml || document.getElementById("liveRoot")) {
      const y = window.scrollY;
      $("#view").innerHTML = body;
      lastHtml = body;
      window.scrollTo(0, y);
      drawCharts();
      if (S.tab === "runs") wireRuns();
      if (S.tab === "flow") wireFlow();
    }
    if (S.tab === "flow") flowSync();
  }
  tickClocks();
  const dot = $("#llmDot");
  dot.className = "dot " + (S.down ? "bad" : S.live.running ? "busy" : "ok");
  dot.title = S.down ? "backend unreachable" : S.live.running ? "pipeline running" : "backend online, idle";
  $("#modelTag").textContent = S.config.model ? `${S.config.model.replace(/^.*\//, "")}${S.config.local ? " · local" : ""}` : "";
}

function wireLive() {
  $("#qForm").addEventListener("submit", e => { e.preventDefault(); queueAnalysis($("#qTicker").value); });
}
function wireRuns() {
  const i = $("#runSearch");
  if (i) i.addEventListener("input", () => { S.q = i.value; const l = $("#runList"); if (l) l.innerHTML = runListHtml(); });
}

function drawCharts() {
  const p = S.portfolio, eq = $("#eqChart");
  if (eq && p) {
    const h = p.equity_history || [];
    lineChart(eq, { labels: h.map(x => x.date), height: 220, fmt: v => "$" + Math.round(v).toLocaleString(),
      series: [{ name: "equity", color: "#45e07a", data: h.map(x => x.equity), area: true }] });
  }
  const d = S.market.data;
  if (d && d.rows) {
    const labels = d.rows.map(r => r[0]);
    const col = k => d.rows.map(r => r[1][k]);
    const markers = S.analyses.filter(a => a.ticker === S.market.ticker).map(a => {
      const day = a.time.slice(0, 10); const idx = labels.indexOf(day);
      return idx < 0 ? null : { label: day, y: col("close")[idx], color: RATING[a.rating] === "r" ? "#ff6b6b" : RATING[a.rating] === "a" ? "#ffb84d" : "#45e07a", title: `${a.rating} @ ${a.price}` };
    }).filter(Boolean);
    const px = $("#pxChart");
    if (px) lineChart(px, { labels, height: 300, fmt: v => v.toFixed(2), markers, series: [
      { name: "close", color: "#c8d3cc", data: col("close"), width: 1.8 },
      { name: "SMA 50", color: "#ffb84d", data: col("close_50_sma"), width: 1.2 },
      { name: "SMA 200", color: "#d291ff", data: col("close_200_sma"), width: 1.2 },
      { name: "boll upper", color: "#5ccfe6", data: col("boll_ub"), width: 1, dash: "2 4", opacity: .5, hideTip: true },
      { name: "boll lower", color: "#5ccfe6", data: col("boll_lb"), width: 1, dash: "2 4", opacity: .5, hideTip: true }] });
    const rs = $("#rsiChart");
    if (rs) lineChart(rs, { labels, height: 130, fmt: v => v.toFixed(0), hlines: [{ y: 70, color: "#6b3a3a" }, { y: 30, color: "#2c5a3f" }], series: [{ name: "RSI", color: "#6aa7ff", data: col("rsi"), hideLegend: true }] });
  }
}

/* ---------- clocks ---------- */
function tickClocks() {
  const now = new Date();
  $("#clock").textContent = now.toLocaleTimeString([], { hour12: false }) + " · NY " + now.toLocaleTimeString("en-GB", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit" });
  document.querySelectorAll("[data-next]").forEach(e => {
    const t = Date.parse(e.dataset.next);
    if (!t) { e.textContent = "–"; return; }
    const s = Math.max(0, (t - Date.now()) / 1000), days = Math.floor(s / 86400);
    e.textContent = (days ? days + "d " : "") + dur(s % 86400);
  });
  document.querySelectorAll("[data-since]").forEach(e => { e.textContent = dur((Date.now() - Date.parse(e.dataset.since)) / 1000); });
}

/* ---------- data loading ---------- */
async function loadDetail(id) {
  S.detail = null; paint(true);
  try { S.detail = await api(`/api/analyses/${encodeURIComponent(id)}`); } catch (e) { S.detail = { id, error: e.message }; }
  if (S.arg === id) paint(true);
}
async function loadMarket(t) {
  t = t.toUpperCase();
  S.market = { ticker: t, data: null, loading: true };
  paint(true);
  const get = p => api(p).then(r => r, e => ({ error: e.message }));
  const [px, fu, nw] = await Promise.all([get(`/api/data/technicals/${t}?days=250`), get(`/api/data/fundamentals/${t}`), get(`/api/data/news/${t}`)]);
  if (S.market.ticker !== t) return;
  const rows = px.data ? Object.entries(px.data) : null;
  S.market = { ticker: t, loading: false, data: { rows, priceErr: px.error || (rows ? null : "no price data for that ticker"), fund: fu.data, fundErr: fu.error, news: nw.data, newsErr: nw.error } };
  if (S.tab === "market") paint(true);
}
async function refreshStatic() {
  const safe = p => api(p).catch(() => null);
  const [an, tr, sc, stt] = await Promise.all([safe("/api/analyses"), safe("/api/trades?limit=30"), safe("/api/schedule"), safe("/api/stats")]);
  if (an) S.analyses = an.analyses;
  if (tr) S.trades = tr.runs;
  if (sc) S.schedule = sc;
  if (stt) S.stats = stt;
  const dl = $("#tickerList");
  if (dl) dl.innerHTML = [...new Set([...S.watchlist, ...S.analyses.map(a => a.ticker)])].sort().map(t => `<option value="${esc(t)}">`).join("");
  paint();
}
async function refreshPortfolio() {
  try { S.portfolio = await api("/api/portfolio"); S.portfolioErr = null; } catch (e) { S.portfolioErr = e.message; }
  paint(S.tab === "dash");
}
async function pollLive() {
  try {
    const prev = S.live.last?.ended, wasRunning = S.live.running;
    S.live = await api("/api/live");
    S.down = false;
    if ((S.live.last?.ended !== prev) || (wasRunning && !S.live.running)) { refreshStatic(); refreshPortfolio(); }
  } catch (_) { S.down = true; }
  paint();
}

/* ---------- events ---------- */
document.addEventListener("click", e => {
  const el = e.target.closest("[data-go],[data-analyze],[data-filter],[data-expand]");
  if (!el) return;
  e.preventDefault();
  if (el.dataset.go) { const [t, ...r] = el.dataset.go.split("/"); go(t, r.join("/")); }
  else if (el.dataset.analyze) { $("#qTicker").value = el.dataset.analyze; queueAnalysis(el.dataset.analyze); }
  else if ("filter" in el.dataset) { S.filter = el.dataset.filter; paint(true); }
  else if (el.dataset.expand) {
    document.querySelectorAll("#view details.stage").forEach(d => { d.open = el.dataset.expand === "1"; S.open[d.dataset.key] = d.open; });
  }
});
document.addEventListener("toggle", e => { if (e.target.dataset?.key) S.open[e.target.dataset.key] = e.target.open; }, true);
$("#searchForm").addEventListener("submit", e => {
  e.preventDefault();
  const v = $("#search").value.trim().toUpperCase();
  if (/^[A-Z]{1,5}([.-][A-Z])?$/.test(v)) { go("market", v); $("#search").blur(); }
});
document.addEventListener("keydown", e => {
  const typing = /INPUT|TEXTAREA/.test(document.activeElement.tagName);
  if (e.key === "Escape") document.activeElement.blur();
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "/") { e.preventDefault(); $("#search").focus(); }
  const i = parseInt(e.key, 10);
  if (i >= 1 && i <= TABS.length) go(TABS[i - 1][0]);
});
let rz; window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(drawCharts, 150); });
window.addEventListener("hashchange", route);

/* ---------- boot ---------- */
(async function boot() {
  api("/api/config").then(c => { S.config = c; paint(); }).catch(() => {});
  api("/api/watchlist").then(w => { S.watchlist = w.tickers; refreshStatic(); }).catch(() => {});
  await Promise.all([refreshStatic(), pollLive()]);
  route();
  refreshPortfolio();
  setInterval(pollLive, 2000);
  setInterval(refreshStatic, 20000);
  setInterval(refreshPortfolio, 60000);
  setInterval(tickClocks, 1000);
})();
