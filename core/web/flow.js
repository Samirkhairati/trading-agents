/* The "flow" page: an interactive map of how data and decisions move between the agents. */
const FLOW_NODES = [
  // daily cycle band
  { id: "Scheduler", x: 20, y: 24, w: 160, h: 56, k: "code", t: "Scheduler", s: "weekdays 16:30 NY",
    does: "Starts the daily cycle on its own every weekday after the US market closes. If the server was down or redeployed past that time and no cycle ran today, it queues one right after start-up.",
    gets: "The clock (DAILY_AT / DAILY_DAYS settings).", makes: "A queued daily-cycle job.", note: "The countdown on the dashboard and live pages is this timer." },
  { id: "Screener", x: 225, y: 24, w: 160, h: 56, k: "code", t: "Screener", s: "picks stocks, no AI",
    does: "Chooses which stocks deserve the expensive analysis today: every stock we already hold, plus the top watchlist names by size of the last 5-day move and volume spike. Stocks analysed in the last 3 days are skipped.",
    gets: "The 20-stock watchlist and our current positions.", makes: "Today's candidate list with a score and the reason each was picked (see the daily runs page).", note: "Plain Python, no language model." },
  { id: "Each pick", x: 430, y: 24, w: 160, h: 56, k: "code", t: "Analyse each pick", s: "one pipeline run per stock",
    does: "Runs the full analysis pipeline below once per candidate, one after another (the model is a single CPU worker).", gets: "One ticker at a time.", makes: "A rating, target, stop and size per stock, saved to the analyses page.", note: "Visitors queueing a stock from the live page use the same pipeline." },
  { id: "Planner", x: 635, y: 24, w: 160, h: 56, k: "mgr", t: "Portfolio planner", s: "AI: enter · keep · skip",
    does: "Looks at the account, what we hold and all fresh ratings, then picks an overall stance: enter new stocks, maintain what we hold, rebalance, or do nothing. For every stock it decides continue, add, exit, or skip, with a reason.",
    gets: "Cash, positions with their P&L, ratings from every analysis in this cycle, recent trades.", makes: "A plan: stance + one intent per stock (buy / sell / hold, weight, stop, take-profit).", note: "Uses the stronger model slot. It only proposes; it cannot place an order." },
  { id: "Risk gate", x: 835, y: 24, w: 145, h: 56, k: "risk", t: "Risk gate", s: "rules, can veto",
    does: "Checks every proposed order against fixed rules: max 10% per stock, keep 10% cash, max 5 positions, every buy needs a stop-loss below the entry, minimum order size, and no new buys if equity is 10% below its peak. Sells always pass.",
    gets: "Proposed orders, the account and the peak equity.", makes: "Approved orders and rejected orders with the exact reason.", note: "The AI cannot override it." },
  { id: "Queue", x: 20, y: 118, w: 160, h: 50, k: "code", t: "Visitor queue", s: "max 3 waiting",
    does: "Lets a visitor request an analysis of any listed US stock from the live page. At most 3 can wait at once and one per visitor; when it is full you are told who is ahead.", gets: "A ticker typed on the live page.", makes: "A queued analysis job.", note: "Jobs run strictly one at a time." },
  { id: "Broker", x: 835, y: 118, w: 145, h: 50, k: "code", t: "Simulated account", s: "paper money",
    does: "Fills approved orders instantly at the latest market price and keeps cash, positions and a ledger. Nothing here touches real money.", gets: "Approved orders; deposits and manual sells by the owner.", makes: "Updated cash and positions, the equity curve.", note: "State is kept in a Docker volume so it survives redeploys." },
  { id: "Log", x: 635, y: 118, w: 160, h: 50, k: "code", t: "Trade log + dashboard", s: "what the UI shows",
    does: "Stores every cycle (candidates, stance, approved and rejected orders, fills) and every full analysis transcript.", gets: "Everything above.", makes: "The dashboard, daily runs and analyses pages.", note: "" },
  // pipeline
  { id: "d-prices", x: 30, y: 262, w: 200, h: 44, k: "data", t: "Prices and indicators", s: "yfinance",
    does: "One year of daily prices plus moving averages, RSI, MACD, Bollinger bands and ATR.", gets: "The ticker.", makes: "A compact table of the last few days and the latest indicator values.", note: "Failure to get prices stops the run early with a clear error." },
  { id: "d-fund", x: 270, y: 262, w: 200, h: 44, k: "data", t: "Fundamentals", s: "yfinance",
    does: "About 28 fundamental fields (valuation, margins, growth, debt) and recent statements.", gets: "The ticker.", makes: "Text for the Fundamental analyst, clipped to fit the small local model.", note: "A failed data source becomes a 'no data' note, never a crash." },
  { id: "d-news", x: 510, y: 262, w: 200, h: 44, k: "data", t: "News and social", s: "headlines, StockTwits, Reddit",
    does: "Recent headlines and social chatter about the stock.", gets: "The ticker.", makes: "Text for the Sentiment analyst.", note: "Reddit is off by default (slow, rate limited)." },
  { id: "d-macro", x: 750, y: 262, w: 220, h: 44, k: "data", t: "Macro", s: "FRED, Polymarket",
    does: "Interest-rate, inflation and unemployment series and prediction-market odds.", gets: "Nothing stock specific.", makes: "Text for the News analyst.", note: "" },
  { id: "Fundamental Analyst", x: 30, y: 350, w: 200, h: 52, k: "analyst", t: "Fundamental Analyst", s: "score + report",
    does: "Judges the business: valuation, profitability, growth, balance sheet.", gets: "Fundamentals data.", makes: "A 1-10 score and a short written report.", note: "Runs at the same time as the other three analysts." },
  { id: "Technical Analyst", x: 270, y: 350, w: 200, h: 52, k: "analyst", t: "Technical Analyst", s: "score + report",
    does: "Reads the price action and indicators: trend, momentum, support and resistance.", gets: "Prices and indicators.", makes: "A 1-10 score and a short written report.", note: "" },
  { id: "Sentiment Analyst", x: 510, y: 350, w: 200, h: 52, k: "analyst", t: "Sentiment Analyst", s: "band + narrative",
    does: "Measures the mood around the stock from headlines and social posts.", gets: "News and social text.", makes: "A sentiment band (Bullish to Bearish), a score and a narrative.", note: "" },
  { id: "News Analyst", x: 750, y: 350, w: 220, h: 52, k: "analyst", t: "News / Macro Analyst", s: "score + report",
    does: "Explains how the macro backdrop (rates, inflation, odds) affects the stock.", gets: "Macro data.", makes: "A 1-10 score and a short report.", note: "" },
  { id: "Bull Researcher", x: 270, y: 470, w: 210, h: 52, k: "debate", t: "Bull Researcher", s: "argues to buy",
    does: "Makes the strongest case FOR owning the stock using the four reports, and rebuts the bear's last argument.", gets: "The four analyst reports and the debate so far.", makes: "One argument per round.", note: "The debate repeats for several rounds (2 by default); each round is one bull and one bear turn." },
  { id: "Bear Researcher", x: 530, y: 470, w: 210, h: 52, k: "debate", t: "Bear Researcher", s: "argues against",
    does: "Makes the strongest case AGAINST owning the stock and rebuts the bull.", gets: "The four analyst reports and the debate so far.", makes: "One argument per round.", note: "" },
  { id: "Research Manager", x: 400, y: 575, w: 210, h: 52, k: "mgr", t: "Research Manager", s: "judge of the debate",
    does: "Weighs both sides and commits to a recommendation (Buy to Sell) with a rationale and concrete actions.", gets: "Reports and the full debate.", makes: "An investment plan.", note: "Uses the stronger model slot." },
  { id: "Trader", x: 400, y: 665, w: 210, h: 52, k: "trader", t: "Trader", s: "turns it into a trade",
    does: "Converts the plan into a concrete proposal: action, entry, target and stop-loss.", gets: "The investment plan and recent prices.", makes: "A trade proposal.", note: "" },
  { id: "Aggressive Analyst", x: 80, y: 760, w: 220, h: 52, k: "risk", t: "Aggressive Analyst", s: "risk view 1",
    does: "Argues for taking more risk and a larger position.", gets: "The trader's proposal and the reports.", makes: "A risk assessment, max position size and a 1-10 risk score.", note: "" },
  { id: "Conservative Analyst", x: 390, y: 760, w: 230, h: 52, k: "risk", t: "Conservative Analyst", s: "risk view 2",
    does: "Argues for caution, smaller size and tighter stops.", gets: "The proposal and the aggressive view.", makes: "A risk assessment, max position size and a risk score.", note: "" },
  { id: "Neutral Analyst", x: 710, y: 760, w: 220, h: 52, k: "risk", t: "Neutral Analyst", s: "risk view 3",
    does: "Balances the other two and suggests a middle path.", gets: "The proposal and both earlier views.", makes: "A risk assessment, max position size and a risk score.", note: "" },
  { id: "Portfolio Manager", x: 400, y: 855, w: 210, h: 52, k: "mgr", t: "Portfolio Manager", s: "final rating",
    does: "Makes the final call for this stock after reading everything, including how your previous call on it worked out.", gets: "Reports, plan, trade proposal, the three risk views and the memory note.", makes: "Rating (Buy, Overweight, Hold, Underweight, Sell), target, stop, size and a justification.", note: "Uses the stronger model slot. The result is saved to the analyses page." },
  { id: "Memory", x: 80, y: 855, w: 220, h: 52, k: "data", t: "Memory", s: "past calls + realised return",
    does: "Remembers the previous decision on this stock and computes how it did since (price now versus then).", gets: "The decision log.", makes: "A short note the Portfolio Manager must reflect on.", note: "No extra model call." },
  { id: "Rating", x: 400, y: 950, w: 210, h: 46, k: "out", t: "Rating", s: "5-tier rating, target, stop",
    does: "The output of one analysis. In a daily cycle the planner then decides what to do with it; for a visitor request it is simply saved and shown.", gets: "", makes: "", note: "" },
];
const FLOW_EDGES = [
  ["Scheduler", "Screener", "r", "l"], ["Screener", "Each pick", "r", "l"], ["Planner", "Risk gate", "r", "l"], ["Risk gate", "Broker", "b", "t"], ["Broker", "Log", "l", "r"],
  ["Each pick", "Planner", "r", "l"], ["Queue", "Each pick", "r", "b", "visitor"],
  ["d-fund", "Fundamental Analyst", "b", "t"], ["d-prices", "Technical Analyst", "b", "t"], ["d-news", "Sentiment Analyst", "b", "t"], ["d-macro", "News Analyst", "b", "t"],
  ["Fundamental Analyst", "J1", "b", "t"], ["Technical Analyst", "J1", "b", "t"], ["Sentiment Analyst", "J1", "b", "t"], ["News Analyst", "J1", "b", "t"],
  ["J1", "Bull Researcher", "b", "t"], ["J1", "Bear Researcher", "b", "t"],
  ["Bull Researcher", "Bear Researcher", "r", "l", "× 2 rounds", "both"],
  ["Bull Researcher", "Research Manager", "b", "t"], ["Bear Researcher", "Research Manager", "b", "t"],
  ["Research Manager", "Trader", "b", "t"], ["Trader", "Aggressive Analyst", "b", "t"], ["Aggressive Analyst", "Conservative Analyst", "r", "l"], ["Conservative Analyst", "Neutral Analyst", "r", "l"],
  ["Neutral Analyst", "Portfolio Manager", "b", "t"], ["Memory", "Portfolio Manager", "r", "l"], ["Portfolio Manager", "Rating", "b", "t"],
];
const FLOW_ORDER = ["Scheduler", "Screener", "Each pick", "d-prices", "Fundamental Analyst", "Bull Researcher", "Bear Researcher", "Research Manager", "Trader", "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst", "Portfolio Manager", "Rating", "Planner", "Risk gate", "Broker", "Log"];
const FLOW_COL = { code: "#7b8a82", data: "#6aa7ff", analyst: "#5ccfe6", debate: "#ffb84d", mgr: "#d291ff", trader: "#6aa7ff", risk: "#ff6b6b", out: "#45e07a" };
const FLOW_KIND = { code: "plain code", data: "data source", analyst: "analyst agent", debate: "debate agent", mgr: "manager agent", trader: "trader agent", risk: "risk agent", out: "output" };

let flowTimer = null;

function flowNode(id) { return id === "J1" ? { x: 490, y: 432, w: 0, h: 0 } : FLOW_NODES.find(n => n.id === id); }
function flowAnchor(n, side) {
  if (n.w === 0) return [n.x, n.y];
  return { l: [n.x, n.y + n.h / 2], r: [n.x + n.w, n.y + n.h / 2], t: [n.x + n.w / 2, n.y], b: [n.x + n.w / 2, n.y + n.h] }[side];
}

function viewFlow() {
  const edges = FLOW_EDGES.map(([a, b, sa, sb, label, both], i) => {
    const [x1, y1] = flowAnchor(flowNode(a), sa), [x2, y2] = flowAnchor(flowNode(b), sb);
    const horiz = sa === "r" || sa === "l";
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2;
    const d = horiz ? `M${x1},${y1} L${x2},${y2}` : `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`;
    const lab = label ? `<text x="${mx}" y="${my - 6}" text-anchor="middle" class="fl-edge-lab">${label}</text>` : "";
    return `<g class="fl-edge" data-a="${a}" data-b="${b}"><path d="${d}" fill="none" marker-end="url(#arr)" ${both ? 'marker-start="url(#arr)"' : ""}/>${lab}</g>`;
  }).join("");
  const nodes = FLOW_NODES.map(n => `<g class="fl-node" data-id="${n.id}" tabindex="0" role="button" aria-label="${n.t}">
      <rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="4" style="--c:${FLOW_COL[n.k]}"/>
      <text x="${n.x + n.w / 2}" y="${n.y + (n.s ? n.h / 2 - 3 : n.h / 2 + 4)}" text-anchor="middle" class="fl-t">${n.t}</text>
      ${n.s ? `<text x="${n.x + n.w / 2}" y="${n.y + n.h / 2 + 13}" text-anchor="middle" class="fl-s">${n.s}</text>` : ""}
    </g>`).join("");
  const svg = `<svg viewBox="0 0 1000 1020" class="flow" role="img" aria-label="Agent flow diagram">
    <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#4a5850"/></marker></defs>
    <text x="20" y="14" class="fl-band">DAILY CYCLE · automatic, weekdays</text>
    <rect x="12" y="236" width="976" height="778" rx="6" class="fl-box"/>
    <text x="24" y="228" class="fl-band">ANALYSIS PIPELINE · one stock · runs for every pick and every visitor request</text>
    <circle cx="490" cy="432" r="4" fill="#4a5850"/>
    ${edges}${nodes}</svg>`;
  const legend = Object.entries(FLOW_KIND).map(([k, v]) => `<span><i style="background:${FLOW_COL[k]}"></i>${v}</span>`).join("");
  return `<div class="grid g2">
    <section class="panel"><h2>agent flow<span class="r">click any box</span></h2>
      <div class="legend" style="margin-bottom:8px">${legend}</div>
      <div class="d" style="margin-bottom:8px"><button class="chip" id="flowPlay">▶ walk through</button> <span id="flowRun" class="a"></span></div>${svg}</section>
    <div><section class="panel" id="flowInfo" style="position:sticky;top:60px"><h2>info</h2><div id="flowBody"></div></section></div></div>`;
}

function flowSelect(id) {
  S.flowSel = id;
  document.querySelectorAll(".fl-node").forEach(g => g.classList.toggle("sel", g.dataset.id === id));
  document.querySelectorAll(".fl-edge").forEach(e => e.classList.toggle("hot", e.dataset.a === id || e.dataset.b === id));
  const n = FLOW_NODES.find(x => x.id === id);
  const body = document.getElementById("flowBody");
  if (!body) return;
  if (!n) { body.innerHTML = '<div class="empty">Select a box to see what that step does, what it reads and what it produces.</div>'; return; }
  const avg = S.stats && S.stats.stages && S.stats.stages[id];
  const row = (k, v) => v ? `<div class="kv" style="margin-top:8px"><span class="k">${k}</span><span class="v" style="text-align:left">${esc(v)}</span></div>` : "";
  body.innerHTML = `<div class="big" style="font-size:18px;color:${FLOW_COL[n.k]}">${esc(n.t)}</div><div class="d">${FLOW_KIND[n.k]}</div>
    <p class="prose" style="margin:10px 0">${esc(n.does)}</p>${row("reads", n.gets)}${row("produces", n.makes)}
    ${avg ? row("typical time", `${dur(avg)} per run (average of ${S.stats.runs} saved analyses)`) : ""}${n.note ? `<p class="d prose" style="margin-top:10px">${esc(n.note)}</p>` : ""}`;
}

function wireFlow() {
  document.querySelectorAll(".fl-node").forEach(g => {
    g.addEventListener("click", () => flowSelect(g.dataset.id));
    g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flowSelect(g.dataset.id); } });
  });
  const play = document.getElementById("flowPlay");
  if (play) play.onclick = () => {
    if (flowTimer) { clearInterval(flowTimer); flowTimer = null; play.textContent = "▶ walk through"; return; }
    let i = 0; play.textContent = "■ stop";
    const step = () => { flowSelect(FLOW_ORDER[i % FLOW_ORDER.length]); i++; };
    step(); flowTimer = setInterval(step, 2600);
  };
  flowSelect(S.flowSel);
  flowSync();
}

/* highlight the agents that are working right now */
function flowSync() {
  const a = S.live && S.live.analysis;
  const running = new Set(a ? a.stages.filter(s => s.state === "running").map(s => s.name) : []);
  document.querySelectorAll(".fl-node").forEach(g => g.classList.toggle("run", running.has(g.dataset.id)));
  const lab = document.getElementById("flowRun");
  if (lab) lab.textContent = running.size ? `now running: ${[...running].join(", ")} (${a.ticker})` : "";
}
