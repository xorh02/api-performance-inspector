"use strict";

// All values come from api/summary. Text is always set via textContent, never innerHTML.
const SVG_NS = "http://www.w3.org/2000/svg";
const state = { data: null, sortKey: "requests", sortDir: -1, timer: null };
const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, text) {
  const node = tag.startsWith("svg:")
    ? document.createElementNS(SVG_NS, tag.slice(4))
    : document.createElement(tag);
  // Styles go through CSSOM: the CSP forbids inline style attributes.
  for (const [k, v] of Object.entries(attrs)) k === "style" ? (node.style.cssText = v) : node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

const fmtMs = (v) => (v === null || v === undefined ? "–" : v >= 100 ? v.toFixed(0) : v.toFixed(1));
const fmtPct = (v) => (v * 100).toFixed(v > 0 && v < 0.01 ? 2 : 1) + "%";
const fmtInt = (v) => v.toLocaleString();
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function fmtTime(unix, bucketSeconds) {
  const d = new Date(unix * 1000);
  const opts = bucketSeconds >= 3600
    ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
    : { hour: "2-digit", minute: "2-digit", ...(bucketSeconds < 60 ? { second: "2-digit" } : {}) };
  return d.toLocaleString(undefined, opts);
}

async function load() {
  const windowSeconds = $("window").value;
  $("export-json").href = `api/export?format=json&window=${windowSeconds}`;
  $("export-csv").href = `api/export?format=csv&window=${windowSeconds}`;
  try {
    const response = await fetch(`api/summary?window=${windowSeconds}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    $("error-banner").hidden = true;
    render();
  } catch (err) {
    const banner = $("error-banner");
    banner.textContent = `Could not load metrics (${err.message}). Is the dashboard server still running?`;
    banner.hidden = false;
    $("status-line").textContent = "Disconnected";
  }
}

function render() {
  const d = state.data;
  const o = d.overview;
  $("status-line").textContent = `Updated ${new Date(d.generated_at).toLocaleTimeString()} · ${fmtInt(o.requests)} requests in window`;
  renderCollector(d.collector);
  const limit = d.sample_limit;
  $("limit-note").hidden = !(limit && limit.reached);
  if (limit && limit.reached) {
    $("limit-note").textContent = `Showing the newest ${fmtInt(limit.max_samples)} measurements. Older requests in this window are not included; for SQLite, raise --max-samples on the dashboard command.`;
  }
  const empty = o.requests === 0;
  $("empty").hidden = !empty;
  $("content").hidden = empty;
  if (empty) return;

  $("ov-requests").textContent = fmtInt(o.requests);
  $("ov-rps").textContent = `${o.requests_per_second} req/s (window average)`;
  $("ov-p50").textContent = fmtMs(o.p50_ms);
  $("ov-p95").textContent = fmtMs(o.p95_ms);
  $("ov-p99").textContent = fmtMs(o.p99_ms);
  $("ov-error-rate").textContent = fmtPct(o.error_rate);
  $("ov-error-rate").classList.toggle("bad", o.error_rate > 0);
  $("ov-errors").textContent = `${fmtInt(o.errors)} errors (5xx)`;
  $("ov-slow").textContent = fmtInt(o.slow_requests);
  $("ov-slow-pct").textContent = `${fmtPct(o.slow_requests / o.requests)} of requests`;

  const ts = d.timeseries;
  $("bucket-label").textContent = `requests per ${ts.bucket_seconds}s bucket`;
  lineChart($("chart-volume"), ts, [
    { key: "requests", label: "Requests", color: "--series-1", fmt: fmtInt },
    { key: "errors", label: "Errors (5xx)", color: "--series-2", fmt: fmtInt },
  ]);
  lineChart($("chart-latency"), ts, [
    { key: "p50_ms", label: "p50", color: "--series-1", fmt: (v) => `${fmtMs(v)} ms` },
    { key: "p95_ms", label: "p95", color: "--series-2", fmt: (v) => `${fmtMs(v)} ms` },
  ]);
  renderSlowest(d.endpoints);
  renderErrors(d);
  renderTable();
}

function renderCollector(c) {
  const footer = $("collector");
  if (!c) {
    footer.textContent = "Reading from a stored database; live collector diagnostics are available when the dashboard is mounted in the application.";
    return;
  }
  const parts = [`Storage: ${c.storage}`, `recorded since start: ${fmtInt(c.recorded)}`];
  if (c.storage === "memory") parts.push(`retained: ${fmtInt(c.retained_samples)}`);
  if (c.storage === "sqlite") parts.push(`dropped: ${fmtInt(c.dropped)}`, `failed writes: ${fmtInt(c.failed_writes)}`);
  if (c.routes_overflowed) parts.push(`route limit reached (${fmtInt(c.routes_overflowed)} grouped as <other>)`);
  if (c.last_error) parts.push(`last storage error: ${c.last_error}`);
  footer.textContent = parts.join(" · ");
}

function renderSlowest(endpoints) {
  const list = $("slowest");
  list.replaceChildren();
  const ranked = endpoints.filter((e) => e.p95_ms !== null).sort((a, b) => b.p95_ms - a.p95_ms).slice(0, 8);
  const max = Math.max(...ranked.map((e) => e.p95_ms), 1e-9);
  for (const e of ranked) {
    const name = el("span", { class: "name", title: `${e.method} ${e.route}` }, `${e.method} ${e.route} `);
    name.append(el("span", { class: "count" }, `(${fmtInt(e.requests)} req)`));
    const track = el("span", { class: "track" });
    track.append(el("span", { class: "fill", style: `width:${Math.max((e.p95_ms / max) * 75, 1)}%` }));
    track.append(el("span", { class: "val" }, fmtMs(e.p95_ms)));
    const li = el("li");
    li.append(name, track);
    list.append(li);
  }
}

const STATUS_COLORS = { "1xx": "--muted", "2xx": "--good", "3xx": "--accent", "4xx": "--warning", "5xx": "--critical" };

function renderErrors(d) {
  const box = $("status-classes");
  box.replaceChildren();
  for (const [cls, count] of Object.entries(d.status_classes)) {
    const chip = el("span", { class: "status-chip" });
    chip.append(el("i", { style: `background:var(${STATUS_COLORS[cls] || "--muted"})` }));
    chip.append(document.createTextNode(`${cls}: ${fmtInt(count)} (${fmtPct(count / d.overview.requests)})`));
    box.append(chip);
  }
  const tbody = $("error-routes").querySelector("tbody");
  tbody.replaceChildren();
  const failing = d.endpoints.filter((e) => e.errors > 0).sort((a, b) => b.errors - a.errors).slice(0, 8);
  if (!failing.length) {
    const row = el("tr");
    row.append(el("td", { colspan: "3", class: "muted" }, "No 5xx responses or unhandled exceptions in this window."));
    tbody.append(row);
  }
  for (const e of failing) {
    const row = el("tr");
    row.append(el("td", { class: "route" }, `${e.method} ${e.route}`));
    row.append(el("td", { class: "num" }, fmtInt(e.errors)));
    row.append(el("td", { class: "num bad" }, fmtPct(e.error_rate)));
    tbody.append(row);
  }
}

function renderTable() {
  const { sortKey, sortDir } = state;
  const rows = [...state.data.endpoints].sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    if (x === y) return 0;
    if (x === null) return 1;
    if (y === null) return -1;
    return (x < y ? -1 : 1) * sortDir;
  });
  for (const th of document.querySelectorAll("#endpoints th")) {
    th.setAttribute("aria-sort", th.dataset.key === sortKey ? (sortDir > 0 ? "ascending" : "descending") : "none");
  }
  const tbody = $("endpoints").querySelector("tbody");
  tbody.replaceChildren();
  for (const e of rows) {
    const tr = el("tr");
    tr.append(
      el("td", { class: "method" }, e.method),
      el("td", { class: "route" }, e.route),
      el("td", { class: "num" }, fmtInt(e.requests)),
      el("td", { class: "num" }, fmtMs(e.mean_ms)),
      el("td", { class: "num" }, fmtMs(e.p50_ms)),
      el("td", { class: "num" }, fmtMs(e.p95_ms)),
      el("td", { class: "num" }, fmtMs(e.p99_ms)),
      el("td", { class: "num" }, fmtMs(e.max_ms)),
      el("td", { class: "num" }, fmtInt(e.slow_requests)),
      el("td", { class: "num" + (e.error_rate > 0 ? " bad" : "") }, fmtPct(e.error_rate)),
    );
    tbody.append(tr);
  }
}

// Minimal SVG line chart with a crosshair tooltip. Null values leave gaps.
function lineChart(container, ts, series) {
  container.replaceChildren();
  const points = ts.points;
  const width = container.clientWidth || 600;
  const height = 220;
  const m = { top: 8, right: 12, bottom: 40, left: 48 };
  const w = width - m.left - m.right;
  const h = height - m.top - m.bottom;
  const values = series.flatMap((s) => points.map((p) => p[s.key])).filter((v) => v !== null);
  const yMax = niceMax(Math.max(...values, 0));
  const x = (i) => m.left + (points.length > 1 ? (i / (points.length - 1)) * w : w / 2);
  const y = (v) => m.top + h - (v / yMax) * h;

  const svg = el("svg:svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": series.map((s) => s.label).join(" and ") + " over time" });
  const axis = el("svg:g", { class: "axis" });
  for (let i = 0; i <= 4; i++) {
    const v = (yMax / 4) * i;
    axis.append(el("svg:line", { class: "gridline", x1: m.left, x2: m.left + w, y1: y(v), y2: y(v) }));
    axis.append(el("svg:text", { x: m.left - 6, y: y(v) + 4, "text-anchor": "end" }, v >= 100 ? v.toFixed(0) : +v.toFixed(2)));
  }
  const ticks = Math.min(5, points.length);
  for (let t = 0; t < ticks; t++) {
    const i = ticks === 1 ? 0 : Math.round((t / (ticks - 1)) * (points.length - 1));
    const anchor = t === 0 ? "start" : t === ticks - 1 ? "end" : "middle";
    axis.append(el("svg:text", { x: x(i), y: m.top + h + 16, "text-anchor": anchor }, fmtTime(points[i].start_unix, ts.bucket_seconds)));
  }
  svg.append(axis);

  for (const s of series) {
    let d = "";
    let pen = false;
    points.forEach((p, i) => {
      const v = p[s.key];
      if (v === null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      pen = true;
    });
    svg.append(el("svg:path", { d, fill: "none", stroke: cssVar(s.color), "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    // A value with no neighbours draws no line segment, so mark it with a dot.
    points.forEach((p, i) => {
      const isolated = (i === 0 || points[i - 1][s.key] === null) && (i === points.length - 1 || points[i + 1][s.key] === null);
      if (p[s.key] !== null && isolated) svg.append(el("svg:circle", { cx: x(i), cy: y(p[s.key]), r: 3, fill: cssVar(s.color) }));
    });
  }

  const cursor = el("svg:line", { y1: m.top, y2: m.top + h, stroke: cssVar("--muted"), "stroke-dasharray": "3 3", visibility: "hidden" });
  const dots = series.map((s) => el("svg:circle", { r: 4, fill: cssVar(s.color), stroke: cssVar("--surface"), "stroke-width": 2, visibility: "hidden" }));
  svg.append(cursor, ...dots);
  const overlay = el("svg:rect", { x: m.left, y: m.top, width: w, height: h, fill: "transparent" });
  svg.append(overlay);
  container.append(svg);

  const tip = el("div", { class: "tooltip", hidden: "" });
  container.append(tip);
  overlay.addEventListener("mousemove", (event) => {
    const rect = svg.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * width;
    const i = Math.max(0, Math.min(points.length - 1, Math.round(((px - m.left) / w) * (points.length - 1))));
    const p = points[i];
    cursor.setAttribute("x1", x(i));
    cursor.setAttribute("x2", x(i));
    cursor.setAttribute("visibility", "visible");
    series.forEach((s, k) => {
      const v = p[s.key];
      dots[k].setAttribute("visibility", v === null ? "hidden" : "visible");
      if (v !== null) { dots[k].setAttribute("cx", x(i)); dots[k].setAttribute("cy", y(v)); }
    });
    tip.replaceChildren(el("strong", {}, fmtTime(p.start_unix, ts.bucket_seconds)));
    for (const s of series) tip.append(el("div", {}, `${s.label}: ${p[s.key] === null ? "no data" : s.fmt(p[s.key])}`));
    tip.hidden = false;
    const left = (x(i) / width) * rect.width;
    tip.style.left = `${Math.min(left + 12, rect.width - tip.offsetWidth)}px`;
    tip.style.top = "8px";
  });
  overlay.addEventListener("mouseleave", () => {
    tip.hidden = true;
    cursor.setAttribute("visibility", "hidden");
    dots.forEach((dot) => dot.setAttribute("visibility", "hidden"));
  });

  const legend = el("div", { class: "legend" });
  for (const s of series) {
    const item = el("span");
    item.append(el("i", { style: `background:${cssVar(s.color)}` }), document.createTextNode(s.label));
    legend.append(item);
  }
  container.append(legend);
}

function niceMax(v) {
  if (v <= 0) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  for (const step of [1, 2, 2.5, 5, 10]) if (step * exp >= v) return step * exp;
  return 10 * exp;
}

document.addEventListener("DOMContentLoaded", () => {
  // Allow linking to a window, e.g. ?window=900.
  const requested = new URLSearchParams(location.search).get("window");
  if ([...$("window").options].some((o) => o.value === requested)) $("window").value = requested;
  $("refresh").addEventListener("click", load);
  $("window").addEventListener("change", load);
  $("auto-refresh").addEventListener("change", (event) => {
    clearInterval(state.timer);
    state.timer = event.target.checked ? setInterval(load, 10000) : null;
  });
  for (const th of document.querySelectorAll("#endpoints th")) {
    th.addEventListener("click", () => {
      const key = th.dataset.key;
      state.sortDir = state.sortKey === key ? -state.sortDir : (key === "route" || key === "method" ? 1 : -1);
      state.sortKey = key;
      if (state.data) renderTable();
    });
  }
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => state.data && render(), 150);
  });
  load();
});
