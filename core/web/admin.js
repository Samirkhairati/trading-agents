/* Owner page: add cash, sell positions, run the daily cycle. Not linked from anywhere. */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = v => v == null ? "–" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const pct = v => (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "%";
const sign = v => v > 0 ? "g" : v < 0 ? "r" : "d";
let timer = null, flash = null;

async function call(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let d = {}; try { d = await r.json(); } catch (_) { /* empty */ }
  if (!r.ok) throw Object.assign(new Error(d.detail || d.error || r.statusText), { status: r.status });
  return d;
}

function loginView(err) {
  clearInterval(timer);
  $("#who").textContent = "";
  $("#app").innerHTML = `<section class="panel" style="max-width:420px;margin:60px auto"><h2>owner login</h2>
    <form id="lf" class="qform" autocomplete="off"><input id="pw" type="password" placeholder="password" style="text-transform:none;flex:1" autofocus aria-label="Password"><button class="btn" type="submit">enter</button></form>
    <div class="msg err">${esc(err || "")}</div></section>`;
  $("#lf").addEventListener("submit", async e => {
    e.preventDefault();
    try { await call("/api/admin/login", { password: $("#pw").value }); load(); } catch (x) { loginView(x.message); }
  });
}

function show(m, ok = true) { flash = { m, ok }; render(window.__ov); }

async function load() {
  try { window.__ov = await call("/api/admin/overview"); } catch (e) { if (e.status === 401) return loginView(); window.__ov = null; show(e.message, false); return; }
  render(window.__ov);
  clearInterval(timer); timer = setInterval(async () => { try { window.__ov = await call("/api/admin/overview"); if (!document.activeElement || document.activeElement.tagName !== "INPUT") render(window.__ov); } catch (e) { if (e.status === 401) loginView(); } }, 6000);
}

function render(ov) {
  if (!ov) return;
  const p = ov.portfolio, q = ov.queue;
  const pnl = p.deposited != null ? p.equity - p.deposited : null;
  $("#who").innerHTML = `<a href="#" id="lo">log out</a>`;
  const msg = flash ? `<div class="msg ${flash.ok ? "ok" : "err"}">${esc(flash.m)}</div>` : "";
  const pos = p.positions.length ? `<div class="table-wrap"><table><tr><th>symbol</th><th class="n">qty</th><th class="n">avg</th><th class="n">price</th><th class="n">value</th><th class="n">p&amp;l</th><th></th></tr>${p.positions.map(x =>
    `<tr><td class="c">${esc(x.symbol)}</td><td class="n">${x.qty.toFixed(4)}</td><td class="n">${money(x.avg_entry)}</td><td class="n">${money(x.price)}</td><td class="n">${money(x.market_value)}</td><td class="n ${sign(x.pnl_pct)}">${pct(x.pnl_pct)}</td><td class="n"><button class="btn warn" data-sell="${esc(x.symbol)}">sell</button></td></tr>`).join("")}</table></div>
    <div style="margin-top:10px"><button class="btn warn" data-sell="ALL">sell everything</button></div>` : '<div class="empty">No open positions.</div>';
  const queue = q.items.length ? q.items.map(i => `<div class="qrow"><span class="pos">${i.running ? "▶" : "#" + i.position}</span><span class="lbl">${esc(i.label)}</span><span class="f">${esc(i.source)}</span>${i.running ? '<span class="a">running</span>' : `<button class="btn warn" data-cancel="${esc(i.id)}">cancel</button>`}</div>`).join("") : '<div class="empty">Queue is empty.</div>';
  const ledger = p.ledger.length ? `<div class="table-wrap"><table><tr><th>time</th><th>type</th><th>detail</th></tr>${p.ledger.slice(0, 30).map(e =>
    `<tr><td class="f">${esc(e.time.slice(0, 16).replace("T", " "))}</td><td class="${e.type === "buy" ? "g" : e.type === "sell" ? "r" : "c"}">${esc(e.type)}</td><td>${e.type === "deposit" ? money(e.amount) : esc(e.symbol) + " " + (e.type === "sell" ? `${(+e.qty).toFixed(3)} @ ${money(e.price)} (${money(e.pnl)})` : money(e.notional))}</td></tr>`).join("")}</table></div>` : '<div class="empty">Nothing yet.</div>';
  $("#app").innerHTML = `${msg}
    <div class="stats"><div class="stat"><div class="k">equity</div><div class="v">${money(p.equity)}</div><div class="s">${esc(p.mode)}</div></div>
      <div class="stat"><div class="k">cash</div><div class="v">${money(p.cash)}</div></div>
      <div class="stat"><div class="k">put in so far</div><div class="v">${money(p.deposited)}</div><div class="s ${pnl == null ? "" : sign(pnl)}">${pnl == null ? "" : "p&amp;l " + money(pnl)}</div></div></div>
    <div class="grid g2e">
      <section class="panel"><h2>add cash</h2>${p.can_deposit ? `<form id="cf" class="qform" autocomplete="off"><input id="amt" type="number" min="1" step="any" placeholder="amount in $" style="text-transform:none" aria-label="Amount"><button class="btn" type="submit">add</button></form>` : '<div class="empty">This account type cannot take deposits.</div>'}</section>
      <section class="panel"><h2>daily cycle</h2><div class="qform"><button class="btn" data-daily="0">run now: dry run</button><button class="btn warn" data-daily="1">run now: trade</button></div>
        <div class="d">Queued behind whatever is running. The automatic one still happens on schedule.</div></section></div>
    <section class="panel"><h2>positions</h2>${pos}</section>
    <div class="grid g2e"><section class="panel"><h2>queue</h2>${queue}</section><section class="panel"><h2>ledger</h2>${ledger}</section></div>`;
}

document.addEventListener("click", async e => {
  const t = e.target.closest("button,a"); if (!t) return;
  try {
    if (t.id === "lo") { e.preventDefault(); await call("/api/admin/logout", {}); loginView(); }
    else if (t.dataset.sell) {
      const what = t.dataset.sell === "ALL" ? "ALL positions" : t.dataset.sell;
      if (!confirm(`Sell ${what} at the current price?`)) return;
      const r = await call("/api/admin/sell", { symbol: t.dataset.sell }); flash = { m: `Sold ${r.sold.join(", ")}`, ok: true }; await load();
    } else if (t.dataset.cancel) { await call("/api/admin/cancel", { id: t.dataset.cancel }); await load(); }
    else if (t.dataset.daily !== undefined) {
      const ex = t.dataset.daily === "1";
      if (ex && !confirm("Run the daily cycle and let it trade the account?")) return;
      const r = await call("/api/admin/daily", { execute: ex }); flash = { m: `Queued (position ${r.position}).`, ok: true }; await load();
    }
  } catch (x) { show(x.message, false); }
});
document.addEventListener("submit", async e => {
  if (e.target.id !== "cf") return;
  e.preventDefault();
  const amount = parseFloat($("#amt").value);
  if (!(amount > 0)) return show("Enter an amount above 0.", false);
  if (!confirm(`Add ${money(amount)} to the account?`)) return;
  try { await call("/api/admin/cash", { amount }); flash = { m: `Added ${money(amount)}.`, ok: true }; await load(); } catch (x) { show(x.message, false); }
});
load();
