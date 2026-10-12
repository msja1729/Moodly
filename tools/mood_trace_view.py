"""Live plot of the mood trace (developer tool, not shipped).

    python -m tools.mood_trace_view [--trace data/mood_trace.jsonl] [--port 8765]

Start the app with AMBIENT_MOOD_TRACE set to the same file, then open
http://127.0.0.1:8765 . The page re-reads the trace every 2 seconds.
"""
from __future__ import annotations

import argparse
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from shared.config_io import APP_HOME, load_config

DEFAULT_TRACE = APP_HOME / "data" / "mood_trace.jsonl"


def read_events(path: Path) -> list[dict]:
    events = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            pass  # a line still being written
    return events


def moods() -> dict:
    try:
        return {k: {"label": m.get("label", k), "point": [m["valence"], m["energy"]], "color": m.get("color")}
                for k, m in load_config()["moods"].items()}
    except Exception:
        return {}


def handler_for(trace: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/trace.json"):
                body = json.dumps({"events": read_events(trace), "moods": moods(), "file": str(trace)}).encode()
                kind = "application/json"
            elif self.path in ("/", "/index.html"):
                body, kind = PAGE.encode("utf-8"), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trace", type=Path, default=DEFAULT_TRACE)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(args.trace))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"mood trace viewer: {url}  (trace: {args.trace})", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    server.serve_forever()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mood Path Trace</title>
<style>
:root { --bg:#0f1115; --panel:#171a21; --line:#2a2f3a; --text:#e8e9ee; --muted:#9aa1b2;
        --mood:#ffcf5a; --target:#ff5d7a; --way:#5fb3ff; --add:#56d68c; --rem:#ff8a5c; --win:#c9a7ff; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:13px/1.45 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif; }
header { display:flex; flex-wrap:wrap; gap:12px 20px; align-items:center; padding:12px 18px; border-bottom:1px solid var(--line); }
header h1 { font-size:16px; margin:0; font-weight:700; }
header .meta { color:var(--muted); font-size:12px; }
header label { color:var(--muted); font-size:12px; display:flex; gap:5px; align-items:center; cursor:pointer; }
select { background:var(--panel); color:var(--text); border:1px solid var(--line); border-radius:6px; padding:3px 6px; }
main { display:grid; grid-template-columns:minmax(380px, 700px) minmax(320px, 1fr); gap:16px; padding:16px 18px; }
@media (max-width: 980px) { main { grid-template-columns:1fr; } }
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:12px; min-width:0; }
.card h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:0 0 8px; font-weight:700; }
svg { display:block; width:100%; height:auto; }
.stats { display:grid; grid-template-columns:repeat(auto-fill, minmax(130px, 1fr)); gap:8px; }
.stat { background:#1d2129; border-radius:8px; padding:8px 10px; }
.stat b { display:block; font-size:17px; font-variant-numeric:tabular-nums; }
.stat span { color:var(--muted); font-size:11px; }
.side { display:flex; flex-direction:column; gap:16px; min-width:0; }
.legend { display:flex; flex-wrap:wrap; gap:6px 14px; color:var(--muted); font-size:11.5px; margin-top:8px; }
.legend i { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:5px; vertical-align:-1px; }
table { width:100%; border-collapse:collapse; font-size:12px; font-variant-numeric:tabular-nums; }
th, td { text-align:left; padding:5px 6px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; position:sticky; top:0; background:var(--panel); }
tr.song td { color:var(--muted); }
tr:hover td { background:#1d2129; }
.log { max-height:440px; overflow:auto; }
.add { color:var(--add); } .rem { color:var(--rem); } .tag { font-size:10.5px; padding:1px 6px; border-radius:9px; background:#262b36; color:var(--muted); }
#tip { position:fixed; pointer-events:none; background:#0b0d11ee; border:1px solid var(--line); border-radius:8px; padding:7px 9px;
       font-size:12px; max-width:340px; display:none; z-index:10; white-space:pre-line; }
.empty { color:var(--muted); padding:40px; text-align:center; }
</style></head>
<body>
<header>
  <h1>Mood path trace</h1>
  <label>Session <select id="sess"></select></label>
  <label><input type="checkbox" id="showWay" checked> waypoints</label>
  <label><input type="checkbox" id="showAdd" checked> songs added</label>
  <label><input type="checkbox" id="showWin"> current queue songs</label>
  <label><input type="checkbox" id="showNum" checked> step numbers</label>
  <label><input type="checkbox" id="showSong" checked> song-change steps</label>
  <span class="meta" id="meta"></span>
</header>
<main>
  <div class="card">
    <h2>Mood plane: valence (x) × energy (y)</h2>
    <svg id="plane" viewBox="0 0 640 640"></svg>
    <div class="legend">
      <span><i style="background:var(--mood)"></i>mood after a cycle (queue average)</span>
      <span><i style="background:#8a8f9c"></i>mood after a song change</span>
      <span><i style="background:var(--target)"></i>target</span>
      <span><i style="background:var(--way)"></i>waypoint (Markov step aimed for)</span>
      <span><i style="background:var(--add)"></i>song added</span>
      <span><i style="background:var(--win)"></i>song in the queue now</span>
    </div>
  </div>
  <div class="side">
    <div class="card"><h2>Now</h2><div class="stats" id="stats"></div></div>
    <div class="card"><h2>Distance to target over time</h2><svg id="dist" viewBox="0 0 600 190"></svg></div>
    <div class="card"><h2>Valence and energy over time (dashed: target)</h2><svg id="axes" viewBox="0 0 600 190"></svg></div>
    <div class="card"><h2>Steps (newest first)</h2><div class="log"><table id="log"></table></div></div>
  </div>
</main>
<div id="tip"></div>
<script>
const $ = id => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
let DATA = {events: [], moods: {}}, chosen = null, lastLen = -1;
const tip = $("tip");

function el(parent, tag, attrs, tipText) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (tipText) {
    e.style.cursor = "pointer";
    e.addEventListener("mousemove", ev => { tip.textContent = tipText; tip.style.display = "block";
      tip.style.left = Math.min(ev.clientX + 14, innerWidth - 350) + "px"; tip.style.top = (ev.clientY + 14) + "px"; });
    e.addEventListener("mouseleave", () => tip.style.display = "none");
  }
  parent.appendChild(e); return e;
}
const f2 = v => v == null ? "–" : v.toFixed(3);
const pt = p => p ? `(${p[0].toFixed(3)}, ${p[1].toFixed(3)})` : "–";
const dist = (a, b) => (a && b) ? Math.hypot(a[0] - b[0], a[1] - b[1]) : null;
const clock = t => new Date(t * 1000).toLocaleTimeString();

// Split the trace into sessions: a cycle with reason "start" (or an "end") begins a new one.
function sessions(events) {
  const out = []; let cur = null;
  for (const e of events) {
    if (!cur || (e.kind === "cycle" && e.reason === "start" && cur.events.length)) { cur = {events: []}; out.push(cur); }
    cur.events.push(e);
    if (e.kind === "end") cur = null;
  }
  return out.filter(s => s.events.some(e => e.kind === "cycle"));
}

function steps(evs) {
  // Every mood point in order: cycles and song changes.
  const out = []; let target = null, n = 0;
  for (const e of evs) {
    if (e.target) target = e.target;
    if ((e.kind === "cycle" || e.kind === "song") && e.mood)
      out.push({...e, target: e.target || target, n: ++n, d: dist(e.mood, e.target || target)});
  }
  return out;
}

function lerpColor(t) { // old (dim violet) -> new (bright gold)
  const a = [110, 90, 200], b = [255, 207, 90];
  return `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * t)).join(",")})`;
}

function drawPlane(st) {
  const svg = $("plane"); svg.innerHTML = "";
  const P = 50, S = 560, X = v => P + v * S, Y = v => P + (1 - v) * S;
  el(svg, "rect", {x: P, y: P, width: S, height: S, fill: "#12151b", stroke: "#2a2f3a"});
  // Quadrant tints and names.
  const quads = [[0.5, 0.5, "#3a2a1a", "happy · energetic"], [0, 0.5, "#3a1a24", "tense · angry"],
                 [0, 0, "#1a2433", "sad · low"], [0.5, 0, "#183028", "calm · content"]];
  for (const [vx, vy, c, name] of quads) {
    el(svg, "rect", {x: X(vx), y: Y(vy + 0.5), width: S / 2, height: S / 2, fill: c, opacity: 0.35});
    el(svg, "text", {x: X(vx) + 8, y: Y(vy + 0.5) + 16, fill: "#5d6474", "font-size": 11}).textContent = name;
  }
  // 10×10 Markov grid, labelled axes.
  for (let i = 0; i <= 10; i++) {
    const v = i / 10, major = i % 5 === 0;
    el(svg, "line", {x1: X(v), y1: Y(0), x2: X(v), y2: Y(1), stroke: major ? "#3a4150" : "#222731", "stroke-width": major ? 1.2 : 1});
    el(svg, "line", {x1: X(0), y1: Y(v), x2: X(1), y2: Y(v), stroke: major ? "#3a4150" : "#222731", "stroke-width": major ? 1.2 : 1});
    el(svg, "text", {x: X(v), y: Y(0) + 16, fill: "#7d8496", "font-size": 10.5, "text-anchor": "middle"}).textContent = v.toFixed(1);
    el(svg, "text", {x: X(0) - 8, y: Y(v) + 4, fill: "#7d8496", "font-size": 10.5, "text-anchor": "end"}).textContent = v.toFixed(1);
  }
  el(svg, "text", {x: X(0.5), y: Y(0) + 36, fill: "#9aa1b2", "font-size": 12, "text-anchor": "middle"}).textContent = "valence →";
  const ylab = el(svg, "text", {x: 16, y: Y(0.5), fill: "#9aa1b2", "font-size": 12, "text-anchor": "middle",
                   transform: `rotate(-90 16 ${Y(0.5)})`}); ylab.textContent = "energy →";
  // Mood presets.
  for (const [k, m] of Object.entries(DATA.moods)) {
    el(svg, "circle", {cx: X(m.point[0]), cy: Y(m.point[1]), r: 4, fill: "none", stroke: m.color || "#888", "stroke-width": 1.5},
       `${m.label} preset ${pt(m.point)}`);
    el(svg, "text", {x: X(m.point[0]) + 7, y: Y(m.point[1]) - 6, fill: m.color || "#888", "font-size": 10.5, opacity: .85}).textContent = m.label;
  }
  if (!st.length) return;
  const last = st[st.length - 1];
  const cycles = st.filter(s => s.kind === "cycle");
  const show = id => $(id).checked;

  // Current queue songs (window of the last cycle).
  const lastCycle = cycles[cycles.length - 1];
  if (show("showWin") && lastCycle) lastCycle.window.forEach((w, i) => {
    if (!w.point) return;
    el(svg, "circle", {cx: X(w.point[0]), cy: Y(w.point[1]), r: i === 0 ? 6 : 4, fill: "none", stroke: "#c9a7ff",
                       "stroke-width": i === 0 ? 2 : 1.2, opacity: .85},
       `${i === 0 ? "Now playing" : "Queue #" + i}${w.removable ? "" : " (fixed)"}\n${w.artists} – ${w.name}\npoint ${pt(w.point)}`);
  });
  // Waypoints and added songs per cycle.
  cycles.forEach(c => {
    if (show("showWay") && c.waypoint) {
      el(svg, "line", {x1: X(c.mood[0]), y1: Y(c.mood[1]), x2: X(c.waypoint[0]), y2: Y(c.waypoint[1]),
                       stroke: "#5fb3ff", "stroke-width": 1, "stroke-dasharray": "3 3", opacity: .55});
      const x = X(c.waypoint[0]), y = Y(c.waypoint[1]);
      el(svg, "path", {d: `M${x} ${y - 5} L${x + 5} ${y} L${x} ${y + 5} L${x - 5} ${y} Z`, fill: "#5fb3ff", opacity: .8},
         `Step ${c.n} waypoint ${pt(c.waypoint)}\ndirection ${c.direction || "–"}${c.settled ? " (settled)" : ""}\nfrom mood ${pt(c.mood)}`);
    }
    if (show("showAdd")) c.added.forEach(a => {
      const from = c.waypoint || c.mood;
      el(svg, "line", {x1: X(from[0]), y1: Y(from[1]), x2: X(a.point[0]), y2: Y(a.point[1]), stroke: "#56d68c",
                       "stroke-width": .8, opacity: .35});
      el(svg, "circle", {cx: X(a.point[0]), cy: Y(a.point[1]), r: 3.2, fill: a.why === "plan" ? "#56d68c" : "#d6c556", opacity: .9},
         `Step ${c.n}: added (${a.why})\n${a.name}\npoint ${pt(a.point)}\nposition ${a.position == null ? "end" : a.position}` +
         (c.waypoint ? `\n${f2(dist(a.point, c.waypoint))} from the waypoint` : ""));
    });
  });
  // Targets (every distinct one; the latest is big).
  const targets = [...new Map(st.filter(s => s.target).map(s => [s.target.join(","), s.target])).values()];
  targets.forEach((t, i) => {
    const big = i === targets.length - 1, x = X(t[0]), y = Y(t[1]), r = big ? 11 : 7, pts = [];
    for (let k = 0; k < 10; k++) { const a = -Math.PI / 2 + k * Math.PI / 5, rr = k % 2 ? r * .45 : r; pts.push(`${x + rr * Math.cos(a)},${y + rr * Math.sin(a)}`); }
    el(svg, "polygon", {points: pts.join(" "), fill: big ? "#ff5d7a" : "none", stroke: "#ff5d7a", "stroke-width": 1.5, opacity: big ? 1 : .6},
       `${big ? "Target" : "Earlier target"} ${pt(t)}`);
    if (big) for (const rad of [0.05, 0.1, 0.2]) el(svg, "circle", {cx: x, cy: y, r: rad * S, fill: "none", stroke: "#ff5d7a",
                                                    "stroke-width": .8, "stroke-dasharray": "2 4", opacity: .45});
  });
  // The mood path.
  const path = st.filter(s => show("showSong") || s.kind === "cycle");
  for (let i = 1; i < path.length; i++) {
    const a = path[i - 1].mood, b = path[i].mood, t = i / Math.max(1, path.length - 1);
    const long = Math.hypot(X(b[0]) - X(a[0]), Y(b[1]) - Y(a[1])) > 14;  // arrows only where they fit
    el(svg, "line", {x1: X(a[0]), y1: Y(a[1]), x2: X(b[0]), y2: Y(b[1]), stroke: lerpColor(t), "stroke-width": 2.2,
                     "marker-end": long ? "url(#arrow)" : "", opacity: .9});
  }
  const defs = el(svg, "defs", {});
  const mk = el(defs, "marker", {id: "arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 5, markerHeight: 5, orient: "auto-start-reverse"});
  el(mk, "path", {d: "M0 0 L10 5 L0 10 z", fill: "#ffcf5a", opacity: .8});
  path.forEach((s, i) => {
    const t = i / Math.max(1, path.length - 1), isLast = s === last, song = s.kind === "song";
    const text = `Step ${s.n} · ${clock(s.t)} · ${song ? "song change" : "cycle (" + s.reason + ")"}\n` +
      `mood ${pt(s.mood)}\ndistance to target ${f2(s.d)}` +
      (song ? `\nnow playing: ${s.name}${s.point ? " " + pt(s.point) : ""}` :
        `\nwaypoint ${pt(s.waypoint)}\nadded ${s.added.length}, removed ${s.removed.length}, queue ${s.queue_size}` +
        (s.preparing ? "\npreparing pool (measure only)" : "") + (s.needs_more_songs ? "\nwaiting for more songs near this step" : ""));
    el(svg, "circle", {cx: X(s.mood[0]), cy: Y(s.mood[1]), r: isLast ? 8 : song ? 3.5 : 5,
                       fill: song ? "#8a8f9c" : lerpColor(t), stroke: isLast ? "#fff" : "#0f1115", "stroke-width": isLast ? 2.5 : 1}, text);
    if (show("showNum") && !song)
      el(svg, "text", {x: X(s.mood[0]) + 7, y: Y(s.mood[1]) + 12, fill: "#cfd3dc", "font-size": 10}).textContent = s.n;
  });
}

function drawSeries(svgId, st, series, yMax, yLabel) {
  const svg = $(svgId); svg.innerHTML = "";
  const L = 42, R = 590, T = 10, B = 160, W = R - L, H = B - T;
  if (st.length < 1) { el(svg, "text", {x: 300, y: 95, fill: "#7d8496", "text-anchor": "middle"}).textContent = "no data yet"; return; }
  const t0 = st[0].t, t1 = Math.max(st[st.length - 1].t, t0 + 1);
  const X = t => L + (t - t0) / (t1 - t0) * W, Y = v => B - v / yMax * H;
  for (let i = 0; i <= 4; i++) {
    const v = yMax * i / 4;
    el(svg, "line", {x1: L, y1: Y(v), x2: R, y2: Y(v), stroke: "#262b36"});
    el(svg, "text", {x: L - 6, y: Y(v) + 4, fill: "#7d8496", "font-size": 10, "text-anchor": "end"}).textContent = v.toFixed(2);
  }
  const mins = (t1 - t0) / 60;
  for (let i = 0; i <= 5; i++) {
    const t = t0 + (t1 - t0) * i / 5;
    el(svg, "text", {x: X(t), y: B + 16, fill: "#7d8496", "font-size": 10, "text-anchor": "middle"}).textContent =
      mins < 3 ? `${Math.round(t - t0)}s` : `${((t - t0) / 60).toFixed(1)}m`;
  }
  // Song changes as vertical ticks.
  st.filter(s => s.kind === "song").forEach(s => el(svg, "line", {x1: X(s.t), y1: T, x2: X(s.t), y2: B, stroke: "#3a4150", "stroke-dasharray": "2 3"},
                                                    `song change ${clock(s.t)}\n${s.name}`));
  for (const {get, color, dash, name} of series) {
    const pts = st.filter(s => get(s) != null);
    if (!pts.length) continue;
    el(svg, "polyline", {points: pts.map(s => `${X(s.t)},${Y(get(s))}`).join(" "), fill: "none", stroke: color,
                         "stroke-width": dash ? 1.2 : 2, "stroke-dasharray": dash ? "5 4" : "", opacity: .95});
    if (!dash) pts.forEach(s => el(svg, "circle", {cx: X(s.t), cy: Y(get(s)), r: s.kind === "song" ? 2.2 : 3, fill: color},
                                   `Step ${s.n} · ${clock(s.t)}\n${name} ${f2(get(s))}`));
  }
  el(svg, "text", {x: L, y: B + 30, fill: "#7d8496", "font-size": 10.5}).textContent = yLabel;
}

function drawStats(st, sess) {
  const box = $("stats");
  if (!st.length) { box.innerHTML = '<div class="empty">Waiting for the first cycle…</div>'; return; }
  const last = st[st.length - 1], first = st[0];
  const cycles = st.filter(s => s.kind === "cycle");
  const added = cycles.reduce((n, c) => n + c.added.length, 0), removed = cycles.reduce((n, c) => n + c.removed.length, 0);
  const songs = st.filter(s => s.kind === "song").length;
  const ended = sess.events.some(e => e.kind === "end");
  const lastC = cycles[cycles.length - 1], lastW = cycles.filter(c => c.waypoint).pop();
  const items = [
    ["mood now", pt(last.mood)], ["target", pt(last.target)], ["distance now", f2(last.d)],
    ["distance at start", f2(first.d)], ["closed", first.d ? `${Math.round((1 - last.d / first.d) * 100)}%` : "–"],
    ["latest waypoint", pt(lastW && lastW.waypoint)], ["cycles", cycles.length], ["song changes", songs],
    ["songs added", added], ["songs removed", removed], ["queue ahead", lastC ? lastC.queue_size : "–"],
    ["elapsed", `${((last.t - first.t) / 60).toFixed(1)} min`],
    ["state", ended ? "ended" : lastC && lastC.preparing ? "preparing pool" : lastC && lastC.needs_more_songs ? "waiting for songs" : "running"],
  ];
  box.innerHTML = items.map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");
}

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]); }

function drawLog(st) {
  const rows = st.slice().reverse().map(s => {
    if (s.kind === "song") return `<tr class="song"><td>${s.n}</td><td>${clock(s.t)}</td><td><span class="tag">song</span></td>
      <td>${pt(s.mood)}</td><td>${f2(s.d)}</td><td>–</td><td>▶ ${esc(s.name)}</td></tr>`;
    const adds = s.added.map(a => `<div class="add">+ ${esc(a.name)} ${pt(a.point)}${a.why === "plan" ? "" : " · keep going"}</div>`).join("");
    const rems = s.removed.map(r => `<div class="rem">− ${esc(r)}</div>`).join("");
    const notes = (s.preparing ? '<div class="tag">preparing</div>' : "") + (s.needs_more_songs ? '<div class="tag">waiting for songs</div>' : "");
    return `<tr><td>${s.n}</td><td>${clock(s.t)}</td><td><span class="tag">${esc(s.reason)}</span></td>
      <td>${pt(s.mood)}</td><td>${f2(s.d)}</td><td>${pt(s.waypoint)}</td><td>${adds}${rems}${notes || (adds || rems ? "" : "no change")}</td></tr>`;
  }).join("");
  $("log").innerHTML = `<tr><th>#</th><th>time</th><th>kind</th><th>mood</th><th>dist</th><th>waypoint</th><th>changes</th></tr>${rows}`;
}

function render() {
  const all = sessions(DATA.events);
  const sel = $("sess"), keep = sel.value;
  sel.innerHTML = all.map((s, i) => `<option value="${i}">#${i + 1} · ${clock(s.events[0].t)}${s.events.some(e => e.kind === "end") ? "" : " (live)"}</option>`).join("");
  let idx = chosen == null ? all.length - 1 : Math.min(chosen, all.length - 1);
  if (all.length) sel.value = idx;
  const sess = all[idx] || {events: []};
  const st = steps(sess.events);
  $("meta").textContent = `${DATA.events.length} events · ${DATA.file || ""} · updated ${new Date().toLocaleTimeString()}`;
  drawPlane(st);
  drawStats(st, sess);
  drawSeries("dist", st, [{get: s => s.d, color: "#ffcf5a", name: "distance"}], Math.max(0.2, ...st.map(s => s.d || 0)) * 1.1,
             "Euclidean distance from the mood to the target (lower = closer)");
  drawSeries("axes", st, [
    {get: s => s.mood[0], color: "#56d68c", name: "valence"}, {get: s => s.mood[1], color: "#5fb3ff", name: "energy"},
    {get: s => s.target && s.target[0], color: "#56d68c", dash: true, name: "target valence"},
    {get: s => s.target && s.target[1], color: "#5fb3ff", dash: true, name: "target energy"}], 1, "green: valence · blue: energy");
  drawLog(st);
}

async function poll() {
  try {
    const r = await fetch("trace.json", {cache: "no-store"});
    DATA = await r.json();
    if (DATA.events.length !== lastLen) { lastLen = DATA.events.length; render(); }
  } catch (e) { $("meta").textContent = "viewer offline: " + e; }
}
$("sess").addEventListener("change", e => { const all = sessions(DATA.events); chosen = +e.target.value === all.length - 1 ? null : +e.target.value; render(); });
for (const id of ["showWay", "showAdd", "showWin", "showNum", "showSong"]) $(id).addEventListener("change", render);
poll(); setInterval(poll, 2000);
</script>
</body></html>
"""

if __name__ == "__main__":
    main()
