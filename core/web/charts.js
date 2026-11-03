/* Tiny SVG line chart: lineChart(el, {labels, series, height, fmt, hlines, markers}) */
const SVGNS = "http://www.w3.org/2000/svg";

function lineChart(el, o) {
  const labels = o.labels || [];
  const n = labels.length;
  const H = o.height || 220, W = Math.max(el.clientWidth, 280);
  const m = { l: 56, r: 10, t: 8, b: 22 };
  const fmt = o.fmt || (v => v.toLocaleString(undefined, { maximumFractionDigits: 2 }));
  el.innerHTML = "";
  el.classList.add("chart");
  if (n < 2) { el.innerHTML = '<div class="empty">Not enough history yet to draw a chart.</div>'; return; }

  const vals = [];
  o.series.forEach(s => s.data.forEach(v => { if (v != null && isFinite(v)) vals.push(v); }));
  (o.hlines || []).forEach(h => vals.push(h.y));
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (o.zero) lo = Math.min(lo, 0);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;

  const x = i => m.l + (W - m.l - m.r) * i / (n - 1);
  const y = v => m.t + (H - m.t - m.b) * (1 - (v - lo) / (hi - lo));
  const svg = document.createElementNS(SVGNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("height", H);
  const add = (tag, attrs, parent = svg) => {
    const e = document.createElementNS(SVGNS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    parent.appendChild(e);
    return e;
  };
  const text = (t, attrs) => { const e = add("text", { fill: "#7b8a82", "font-size": 11, "font-family": "inherit", ...attrs }); e.textContent = t; };

  for (let k = 0; k <= 4; k++) {
    const v = lo + (hi - lo) * k / 4;
    add("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v), stroke: "#1a2620", "stroke-dasharray": "2 4" });
    text(fmt(v), { x: m.l - 8, y: y(v) + 4, "text-anchor": "end" });
  }
  const ticks = Math.min(5, n);
  for (let k = 0; k < ticks; k++) {
    const i = Math.round((n - 1) * k / (ticks - 1));
    text(labels[i].slice(2), { x: x(i), y: H - 6, "text-anchor": k === 0 ? "start" : k === ticks - 1 ? "end" : "middle" });
  }
  (o.hlines || []).forEach(h => add("line", { x1: m.l, x2: W - m.r, y1: y(h.y), y2: y(h.y), stroke: h.color || "#4a5850", "stroke-dasharray": "5 4" }));

  o.series.forEach(s => {
    let d = "", pen = false, first = null, last = null;
    s.data.forEach((v, i) => {
      if (v == null || !isFinite(v)) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true; if (first === null) first = i; last = i;
    });
    if (!d) return;
    if (s.area) add("path", { d: `${d}L${x(last)},${H - m.b}L${x(first)},${H - m.b}Z`, fill: s.color, opacity: 0.1 });
    add("path", { d, fill: "none", stroke: s.color, "stroke-width": s.width || 1.6, "stroke-dasharray": s.dash || "", "stroke-linejoin": "round", opacity: s.opacity || 1 });
  });

  (o.markers || []).forEach(mk => {
    const i = labels.indexOf(mk.label);
    if (i < 0) return;
    const g = add("circle", { cx: x(i), cy: y(mk.y), r: 5, fill: mk.color, stroke: "#090d0b", "stroke-width": 2 });
    add("title", {}, g).textContent = mk.title || "";
  });

  const cross = add("line", { y1: m.t, y2: H - m.b, stroke: "#35493f", opacity: 0 });
  const tip = document.createElement("div");
  tip.className = "tt"; tip.style.display = "none";
  const hit = add("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" });
  hit.addEventListener("mousemove", ev => {
    const r = svg.getBoundingClientRect();
    const px = (ev.clientX - r.left) * (W / r.width);
    const i = Math.max(0, Math.min(n - 1, Math.round((px - m.l) / (W - m.l - m.r) * (n - 1))));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("opacity", 1);
    tip.innerHTML = `<span class="d">${labels[i]}</span>` + o.series.filter(s => s.data[i] != null && !s.hideTip)
      .map(s => `<br><span style="color:${s.color}">${s.name}</span> ${fmt(s.data[i])}`).join("");
    tip.style.display = "block";
    const left = x(i) * (r.width / W);
    tip.style.left = (left > r.width / 2 ? left - tip.offsetWidth - 12 : left + 12) + "px";
    tip.style.top = "8px";
  });
  hit.addEventListener("mouseleave", () => { cross.setAttribute("opacity", 0); tip.style.display = "none"; });

  const legend = o.series.filter(s => s.name && !s.hideLegend)
    .map(s => `<span><i style="background:${s.color}"></i>${s.name}</span>`).join("");
  if (legend) el.insertAdjacentHTML("beforeend", `<div class="legend">${legend}</div>`);
  el.appendChild(svg);
  el.appendChild(tip);
}
