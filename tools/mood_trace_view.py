"""Live plot of the mood trace (developer tool, not shipped).

    python -m tools.mood_trace_view [--trace data/mood_trace.jsonl] [--port 8765]

Start the app with AMBIENT_MOOD_TRACE set to the same file, then open
http://127.0.0.1:8765 . The page re-reads the trace every second.

Easier: tools/dev_dashboard.ps1 (Windows) or tools/dev_dashboard.sh (macOS)
starts the app with the trace on; the app then serves this page itself and
shows a "Mood path dashboard" link in the sidebar and the tray menu.
"""
from __future__ import annotations

import argparse
import json
import threading
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


def start_in_background(trace: Path, port: int = 8765) -> str:
    """Serve the dashboard from a background thread; returns its URL. If the port is taken
    (a viewer is already running), that one is used."""
    url = f"http://127.0.0.1:{port}/"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(Path(trace)))
    except OSError:
        return url
    threading.Thread(target=server.serve_forever, name="mood-dashboard", daemon=True).start()
    return url


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
        --you:#ffffff; --target:#ff5d7a; --now:#c9a7ff; --pick:#56d68c; --rem:#ff8a5c; --path:#ffcf5a; --way:#5fb3ff; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:14px/1.5 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif; }
header { display:flex; flex-wrap:wrap; gap:10px 18px; align-items:center; padding:12px 18px; border-bottom:1px solid var(--line); }
header h1 { font-size:17px; margin:0; font-weight:700; }
header label { color:var(--muted); font-size:13px; display:flex; gap:6px; align-items:center; cursor:pointer; }
header .meta { color:var(--muted); font-size:12px; margin-left:auto; }
select { background:var(--panel); color:var(--text); border:1px solid var(--line); border-radius:6px; padding:3px 6px; }
main { display:grid; grid-template-columns:minmax(360px, 640px) minmax(320px, 1fr); gap:16px; padding:16px 18px; }
@media (max-width: 980px) { main { grid-template-columns:1fr; } }
.card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px; min-width:0; }
.card h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); margin:0 0 10px; font-weight:700; }
.col { display:flex; flex-direction:column; gap:16px; min-width:0; }
svg { display:block; width:100%; height:auto; }
.now { display:grid; grid-template-columns:auto 1fr; gap:4px 14px; align-items:baseline; }
.now .k { color:var(--muted); font-size:12.5px; }
.now .v { font-weight:600; }
.big { font-size:20px; font-weight:700; line-height:1.3; margin:0 0 10px; }
.pt { font-variant-numeric:tabular-nums; color:var(--muted); font-weight:500; font-size:12.5px; }
.bar { height:8px; border-radius:4px; background:#262b36; overflow:hidden; margin:10px 0 2px; }
.bar i { display:block; height:100%; background:linear-gradient(90deg, #ffcf5a, #56d68c); }
ol.next { list-style:none; margin:0; padding:0; }
ol.next li { display:grid; grid-template-columns:22px 1fr auto; gap:10px; align-items:center; padding:7px 0; border-bottom:1px solid var(--line); }
ol.next li:last-child { border-bottom:0; }
.num { width:22px; height:22px; border-radius:50%; display:grid; place-items:center; font-size:11.5px; font-weight:700; background:#262b36; }
.num.now { background:var(--now); color:#1b1325; }
.name { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.name small { display:block; color:var(--muted); font-size:11.5px; }
.tag { font-size:11px; padding:2px 8px; border-radius:10px; white-space:nowrap; }
.tag.pick { background:#1d3a2a; color:var(--pick); } .tag.yours { background:#262b36; color:var(--muted); }
.dir { font-size:11.5px; white-space:nowrap; } .dir.good { color:var(--pick); } .dir.bad { color:var(--rem); }
.feed { max-height:520px; overflow:auto; }
.ev { padding:9px 0; border-bottom:1px solid var(--line); }
.ev:last-child { border-bottom:0; }
.ev time { color:var(--muted); font-size:12px; margin-right:8px; font-variant-numeric:tabular-nums; }
.ev ul { margin:4px 0 0; padding-left:18px; } .ev li { margin:1px 0; }
.ev .add { color:var(--pick); } .ev .rem { color:var(--rem); }
details summary { cursor:pointer; color:var(--rem); }
.legend { display:flex; flex-wrap:wrap; gap:6px 16px; color:var(--muted); font-size:12px; margin-top:8px; }
.legend i { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:5px; vertical-align:-1px; }
.adv { display:none; } body.details .adv { display:block; } body.details .adv.flex { display:flex; }
#tip { position:fixed; pointer-events:none; background:#0b0d11ee; border:1px solid var(--line); border-radius:8px; padding:7px 9px;
       font-size:12.5px; max-width:340px; display:none; z-index:10; white-space:pre-line; }
.empty { color:var(--muted); padding:30px 10px; text-align:center; }
</style></head>
<body>
<header>
  <h1>Mood path</h1>
  <label>Session <select id="sess"></select></label>
  <label><input type="checkbox" id="showDetails"> Show details (waypoints, all picks, charts)</label>
  <span class="meta" id="meta"></span>
</header>
<main>
  <div class="col">
    <div class="card">
      <h2>Where your music is</h2>
      <svg id="plane" viewBox="0 0 640 600"></svg>
      <div class="legend">
        <span><i style="background:var(--you)"></i>your mood now</span>
        <span><i style="background:var(--target)"></i>target</span>
        <span><i style="background:var(--path)"></i>how the mood moved</span>
        <span><i style="background:var(--now)"></i>song playing now</span>
        <span><i style="background:var(--pick)"></i>songs Moodly picked (numbers = order in the queue)</span>
        <span class="adv"><i style="background:var(--way)"></i>waypoint (the next step it aimed for)</span>
      </div>
    </div>
    <div class="card"><h2>Gap to the target over time</h2><svg id="dist" viewBox="0 0 600 170"></svg></div>
    <div class="card adv"><h2>Valence (green) and energy (blue) over time · dashed: target</h2><svg id="axes" viewBox="0 0 600 170"></svg></div>
  </div>
  <div class="col">
    <div class="card" id="nowCard"><h2>Right now</h2><div class="empty">Waiting for the first session step…</div></div>
    <div class="card"><h2>Up next</h2><div id="next"></div></div>
    <div class="card"><h2>What Moodly did (newest first)</h2><div class="feed" id="feed"></div></div>
  </div>
</main>
<div id="tip"></div>
<script>
const $ = id => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
let DATA = {events: [], moods: {}}, chosen = null, lastKey = "";
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
const text = (parent, attrs, s) => { const t = el(parent, "text", attrs); t.textContent = s; return t; };
const pt = p => p ? `(${p[0].toFixed(2)}, ${p[1].toFixed(2)})` : "–";
const dist = (a, b) => (a && b) ? Math.hypot(a[0] - b[0], a[1] - b[1]) : null;
const clock = t => new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"})[c]);
const short = (s, n = 26) => s.length > n ? s.slice(0, n - 1) + "…" : s;
const songTitle = full => full.includes(" - ") ? full.slice(full.indexOf(" - ") + 3) : full;
const fullName = w => `${w.artists} - ${w.name}`;
const same = n => n.toLowerCase();  // Spotify spells some titles with different capitals in different places
function targetName(t) {
  for (const m of Object.values(DATA.moods)) if (t && Math.abs(m.point[0] - t[0]) < 1e-6 && Math.abs(m.point[1] - t[1]) < 1e-6) return m.label;
  return "the target";
}
function feel(from, to) { // plain words for a move
  const dv = to[0] - from[0], de = to[1] - from[1], w = [];
  if (Math.abs(dv) >= 0.02) w.push(dv > 0 ? "happier" : "sadder");
  if (Math.abs(de) >= 0.02) w.push(de > 0 ? "more energetic" : "calmer");
  return w.join(" and ") || "about the same";
}

// Sessions: a cycle with reason "start" begins one, an "end" closes it.
function sessions(events) {
  const out = []; let cur = null;
  for (const e of events) {
    if (!cur || (e.kind === "cycle" && e.reason === "start" && cur.events.length)) { cur = {events: []}; out.push(cur); }
    cur.events.push(e);
    if (e.kind === "end") cur = null;
  }
  return out.filter(s => s.events.some(e => e.kind === "cycle"));
}

// Everything the page shows, worked out once per update.
function analyse(evs) {
  const steps = []; let target = null;
  for (const e of evs) {
    if (e.target) target = e.target;
    if ((e.kind === "cycle" || e.kind === "song") && e.mood) steps.push({...e, target: e.target || target, d: dist(e.mood, e.target || target)});
  }
  const cycles = steps.filter(s => s.kind === "cycle");
  const picked = new Map();  // name -> {point, t}
  cycles.forEach(c => c.added.forEach(a => picked.set(same(a.name), {name: a.name, point: a.point, t: c.t, why: a.why})));
  const lastCycle = cycles[cycles.length - 1];
  const lastSong = steps.filter(s => s.kind === "song").pop();
  // Now playing: the latest song change, or the first song of the latest cycle.
  let now = null;
  if (lastCycle && lastCycle.window.length) now = {name: fullName(lastCycle.window[0]), point: lastCycle.window[0].point, t: lastCycle.t};
  if (lastSong && (!lastCycle || lastSong.t >= lastCycle.t)) now = {name: lastSong.name, point: lastSong.point, t: lastSong.t};
  // Up next: the latest cycle's queue after the song playing now (each song once).
  let upNext = [];
  if (lastCycle) {
    const names = lastCycle.window.map(fullName);
    const at = now ? names.indexOf(now.name) : 0;
    const seen = new Set(now ? [now.name] : []);
    for (const w of lastCycle.window.slice(Math.max(0, at) + 1)) {
      const n = fullName(w);
      if (seen.has(n)) continue;
      seen.add(n); upNext.push({...w, full: n, picked: picked.has(same(n))});
      if (upNext.length >= 8) break;
    }
  }
  const last = steps[steps.length - 1];
  const key = s => s && s.target ? s.target.join(",") : "";
  const first = last ? (steps.find(s => key(s) === key(last)) || steps[0]) : null;
  return {steps, cycles, picked, now, upNext, last, first, ended: evs.some(e => e.kind === "end")};
}

function drawPlane(A) {
  const svg = $("plane"); svg.innerHTML = "";
  const P = 44, S = 530, X = v => P + v * S, Y = v => 14 + (1 - v) * S;
  const details = document.body.classList.contains("details");
  el(svg, "rect", {x: X(0), y: Y(1), width: S, height: S, fill: "#12151b", stroke: "#2a2f3a", rx: 6});
  const quads = [[0.5, 1, "happy · energetic"], [0, 1, "tense · angry"], [0, 0.5, "sad · low"], [0.5, 0.5, "calm · content"]];
  for (const [vx, vy, name] of quads) text(svg, {x: X(vx) + 10, y: Y(vy) + 20, fill: "#4f5666", "font-size": 13}, name);
  el(svg, "line", {x1: X(0.5), y1: Y(0), x2: X(0.5), y2: Y(1), stroke: "#2f3542"});
  el(svg, "line", {x1: X(0), y1: Y(0.5), x2: X(1), y2: Y(0.5), stroke: "#2f3542"});
  if (details) for (let i = 1; i < 10; i++) {
    el(svg, "line", {x1: X(i / 10), y1: Y(0), x2: X(i / 10), y2: Y(1), stroke: "#1d2129"});
    el(svg, "line", {x1: X(0), y1: Y(i / 10), x2: X(1), y2: Y(i / 10), stroke: "#1d2129"});
  }
  for (const v of [0, 0.5, 1]) {
    text(svg, {x: X(v), y: Y(0) + 17, fill: "#7d8496", "font-size": 12, "text-anchor": "middle"}, v.toFixed(1));
    text(svg, {x: X(0) - 8, y: Y(v) + 4, fill: "#7d8496", "font-size": 12, "text-anchor": "end"}, v.toFixed(1));
  }
  text(svg, {x: X(0.5), y: Y(0) + 34, fill: "#9aa1b2", "font-size": 13, "text-anchor": "middle"}, "sad  ←  valence  →  happy");
  text(svg, {x: 13, y: Y(0.5), fill: "#9aa1b2", "font-size": 13, "text-anchor": "middle", transform: `rotate(-90 13 ${Y(0.5)})`}, "calm  ←  energy  →  energetic");
  if (!A.last) return;
  const target = A.last.target;

  // Waypoints (details only).
  if (details) A.cycles.forEach(c => { if (!c.waypoint) return;
    const x = X(c.waypoint[0]), y = Y(c.waypoint[1]);
    el(svg, "path", {d: `M${x} ${y - 5} L${x + 5} ${y} L${x} ${y + 5} L${x - 5} ${y} Z`, fill: "#5fb3ff", opacity: .75},
       `${clock(c.t)}: aimed for ${pt(c.waypoint)} from mood ${pt(c.mood)}`); });
  // Every song picked this session: faint (all of them only in details).
  const upNames = new Set(A.upNext.map(u => same(u.full)));
  if (details) A.picked.forEach((p, name) => { if (upNames.has(name)) return;
    el(svg, "circle", {cx: X(p.point[0]), cy: Y(p.point[1]), r: 3, fill: "#56d68c", opacity: .35}, `Picked at ${clock(p.t)}\n${p.name}\n${pt(p.point)}`); });

  // Target with "almost there" rings.
  const tx = X(target[0]), ty = Y(target[1]);
  for (const r of [0.05, 0.1]) el(svg, "circle", {cx: tx, cy: ty, r: r * S, fill: "none", stroke: "#ff5d7a", "stroke-dasharray": "3 5", opacity: .45});
  const star = []; for (let k = 0; k < 10; k++) { const a = -Math.PI / 2 + k * Math.PI / 5, rr = k % 2 ? 5 : 12; star.push(`${tx + rr * Math.cos(a)},${ty + rr * Math.sin(a)}`); }
  el(svg, "polygon", {points: star.join(" "), fill: "#ff5d7a"}, `Target: ${targetName(target)} ${pt(target)}`);
  text(svg, {x: tx + 15, y: ty - 10, fill: "#ff8fa3", "font-size": 14, "font-weight": 700}, `Target: ${targetName(target)}`);

  // The path of the mood: one smooth line, fading in from the start.
  const path = A.steps.filter(s => s.mood);
  if (path.length > 1) {
    const d = path.map((s, i) => `${i ? "L" : "M"}${X(s.mood[0])} ${Y(s.mood[1])}`).join(" ");
    el(svg, "path", {d, fill: "none", stroke: "#ffcf5a", "stroke-width": 2.5, "stroke-linejoin": "round", opacity: .75});
    path.forEach((s, i) => el(svg, "circle", {cx: X(s.mood[0]), cy: Y(s.mood[1]), r: 3.2, fill: "#ffcf5a", opacity: .35 + .65 * i / (path.length - 1)},
      `${clock(s.t)} · ${s.kind === "song" ? "song changed: " + s.name : "Moodly updated the queue"}\nmood ${pt(s.mood)} · gap ${s.d.toFixed(2)}`));
    const s0 = path[0];
    text(svg, {x: X(s0.mood[0]) - 8, y: Y(s0.mood[1]) + 20, fill: "#b9a36a", "font-size": 12, "text-anchor": "end"}, "start");
  }

  // Up next: numbered dots (green = Moodly picked it).
  A.upNext.slice(0, 6).forEach((u, i) => { if (!u.point) return;
    const x = X(u.point[0]), y = Y(u.point[1]);
    el(svg, "circle", {cx: x, cy: y, r: 9, fill: u.picked ? "#56d68c" : "#3a4150", stroke: "#0f1115", "stroke-width": 1.5},
       `Up next #${i + 1}${u.picked ? " · picked by Moodly" : ""}\n${u.full}\n${pt(u.point)}`);
    text(svg, {x, y: y + 4, fill: u.picked ? "#0f2a1a" : "#e8e9ee", "font-size": 11, "font-weight": 700, "text-anchor": "middle", "pointer-events": "none"}, i + 1);
  });
  // Song playing now.
  if (A.now && A.now.point) {
    const x = X(A.now.point[0]), y = Y(A.now.point[1]);
    el(svg, "circle", {cx: x, cy: y, r: 10, fill: "none", stroke: "#c9a7ff", "stroke-width": 3}, `Playing now\n${A.now.name}\n${pt(A.now.point)}`);
    text(svg, {x: x + 14, y: y + 18, fill: "#c9a7ff", "font-size": 13, "font-weight": 600}, `♪ ${short(songTitle(A.now.name))}`);
  }
  // You are here.
  const m = A.last.mood, mx = X(m[0]), my = Y(m[1]);
  el(svg, "circle", {cx: mx, cy: my, r: 18, fill: "#ffffff", opacity: .12});
  el(svg, "circle", {cx: mx, cy: my, r: 8, fill: "#ffffff", stroke: "#0f1115", "stroke-width": 2}, `Your mood now ${pt(m)}\n${A.last.d.toFixed(2)} from ${targetName(target)}`);
  el(svg, "line", {x1: mx, y1: my, x2: tx, y2: ty, stroke: "#ffffff", "stroke-dasharray": "4 4", opacity: .35});
  text(svg, {x: mx + 14, y: my - 12, fill: "#ffffff", "font-size": 14, "font-weight": 700}, "You are here");
}

function drawNow(A) {
  const box = $("nowCard");
  if (!A.last) { box.innerHTML = '<h2>Right now</h2><div class="empty">Waiting for the first session step…</div>'; return; }
  const t = A.last.target, name = targetName(t), gap = A.last.d, start = A.first.d;
  const done = start > 0 ? Math.max(0, Math.min(1, 1 - gap / start)) : 1;
  const status = A.ended ? "Session ended." : gap <= 0.05 ? "You're there - Moodly is just keeping the queue full."
    : gap <= 0.12 ? "Almost there." : "On the way.";
  const moved = A.first ? feel(A.first.mood, A.last.mood) : "";
  box.innerHTML = `<h2>Right now</h2>
    <p class="big">${esc(status)} Your music is <b>${gap.toFixed(2)}</b> away from ${esc(name)}.</p>
    <div class="now">
      <span class="k">Playing</span><span class="v">${A.now ? "♪ " + esc(A.now.name) + ` <span class="pt">${pt(A.now.point)}</span>` : "–"}</span>
      <span class="k">Mood now</span><span class="v">${pt(A.last.mood)}</span>
      <span class="k">Target</span><span class="v">${esc(name)} ${pt(t)}</span>
      <span class="k">Since target set</span><span class="v">${esc(moved)} · gap ${start.toFixed(2)} → ${gap.toFixed(2)}</span>
    </div>
    <div class="bar"><i style="width:${Math.round(done * 100)}%"></i></div>
    <span class="pt">${Math.round(done * 100)}% of the way</span>`;
}

function drawNext(A) {
  const box = $("next");
  if (!A.upNext.length) { box.innerHTML = '<div class="empty">No queue seen yet.</div>'; return; }
  const mood = A.last.mood, target = A.last.target, gapNow = dist(mood, target);
  box.innerHTML = '<ol class="next">' + A.upNext.map((u, i) => {
    let dir = "";
    if (u.point) { const g = dist(u.point, target);
      dir = g < gapNow - 0.01 ? '<span class="dir good">pulls toward target</span>' : g > gapNow + 0.01 ? '<span class="dir bad">pulls away</span>' : '<span class="dir">holds</span>'; }
    return `<li><span class="num">${i + 1}</span>
      <span class="name">${esc(songTitle(u.full))}<small>${esc(u.artists)} · <span class="pt">${pt(u.point)}</span> · ${dir}</small></span>
      <span class="tag ${u.picked ? "pick" : "yours"}">${u.picked ? "picked by Moodly" : "your queue"}</span></li>`;
  }).join("") + "</ol>";
}

function drawFeed(A) {
  const items = A.steps.slice().reverse().map(s => {
    if (s.kind === "song") return `<div class="ev"><time>${clock(s.t)}</time>♪ Now playing <b>${esc(songTitle(s.name))}</b>
      <span class="pt">${pt(s.point)}</span> - mood now ${pt(s.mood)}</div>`;
    const lines = [];
    if (s.preparing) lines.push("Preparing your song pool - only measuring the mood for now.");
    if (s.reason === "target_change") lines.push(`Target changed to <b>${esc(targetName(s.target))}</b> ${pt(s.target)}.`);
    if (s.waypoint && s.added.length) lines.push(`Aimed one step ${feel(s.mood, s.waypoint)} (towards ${pt(s.waypoint)}) and added:`);
    else if (s.added.length) lines.push("Added to keep the music going:");
    const adds = s.added.map(a => `<li class="add">+ ${esc(a.name)} <span class="pt">${pt(a.point)}</span>${a.why === "plan" ? "" : " · keep-going fill"}</li>`).join("");
    const rems = s.removed.length ? `<details><summary>removed ${s.removed.length} song${s.removed.length > 1 ? "s" : ""}</summary><ul>` +
      s.removed.map(r => `<li class="rem">− ${esc(r)}</li>`).join("") + "</ul></details>" : "";
    if (s.needs_more_songs) lines.push("No good song near the next step yet - waiting for the pool to grow.");
    if (!lines.length && !adds && !rems) lines.push("Checked the queue - no change needed.");
    return `<div class="ev"><time>${clock(s.t)}</time>${lines.join(" ")} <span class="pt">mood ${pt(s.mood)} · gap ${s.d.toFixed(2)}</span>
      ${adds ? "<ul>" + adds + "</ul>" : ""}${rems}</div>`;
  });
  $("feed").innerHTML = items.join("") || '<div class="empty">Nothing yet.</div>';
}

function drawSeries(svgId, steps, series, yMax) {
  const svg = $(svgId); svg.innerHTML = "";
  const L = 40, R = 590, T = 10, B = 140, W = R - L, H = B - T;
  if (!steps.length) { text(svg, {x: 300, y: 80, fill: "#7d8496", "text-anchor": "middle"}, "no data yet"); return; }
  const t0 = steps[0].t, t1 = Math.max(steps[steps.length - 1].t, t0 + 1);
  const X = t => L + (t - t0) / (t1 - t0) * W, Y = v => B - v / yMax * H;
  for (let i = 0; i <= 2; i++) { const v = yMax * i / 2;
    el(svg, "line", {x1: L, y1: Y(v), x2: R, y2: Y(v), stroke: "#262b36"});
    text(svg, {x: L - 6, y: Y(v) + 4, fill: "#7d8496", "font-size": 11, "text-anchor": "end"}, v.toFixed(2)); }
  for (let i = 0; i <= 4; i++) { const t = t0 + (t1 - t0) * i / 4;
    text(svg, {x: X(t), y: B + 18, fill: "#7d8496", "font-size": 11, "text-anchor": "middle"}, `${((t - t0) / 60).toFixed(1)} min`); }
  for (const {get, color, dash} of series) {
    const pts = steps.filter(s => get(s) != null);
    if (!pts.length) continue;
    el(svg, "polyline", {points: pts.map(s => `${X(s.t)},${Y(get(s))}`).join(" "), fill: "none", stroke: color,
                         "stroke-width": dash ? 1.2 : 2.4, "stroke-dasharray": dash ? "5 4" : "", "stroke-linejoin": "round"});
    if (!dash) pts.forEach(s => el(svg, "circle", {cx: X(s.t), cy: Y(get(s)), r: 2.6, fill: color},
      `${clock(s.t)} · ${s.kind === "song" ? "song changed" : "queue updated"}\n${get(s).toFixed(3)}`));
  }
}

function render() {
  const all = sessions(DATA.events);
  const sel = $("sess");
  sel.innerHTML = all.map((s, i) => `<option value="${i}">#${i + 1} · ${clock(s.events[0].t)}${s.events.some(e => e.kind === "end") ? "" : " (live)"}</option>`).join("");
  const idx = chosen == null ? all.length - 1 : Math.min(chosen, all.length - 1);
  if (all.length) sel.value = idx;
  const A = analyse((all[idx] || {events: []}).events);
  $("meta").textContent = `updated ${new Date().toLocaleTimeString()}`;
  drawPlane(A); drawNow(A); drawNext(A); drawFeed(A);
  // The gap chart starts when the current target was set.
  const since = A.first ? A.steps.filter(s => s.t >= A.first.t) : [];
  drawSeries("dist", since, [{get: s => s.d, color: "#ffcf5a"}], Math.max(0.2, ...since.map(s => s.d || 0)) * 1.1);
  drawSeries("axes", A.steps, [{get: s => s.mood[0], color: "#56d68c"}, {get: s => s.mood[1], color: "#5fb3ff"},
    {get: s => s.target && s.target[0], color: "#56d68c", dash: true}, {get: s => s.target && s.target[1], color: "#5fb3ff", dash: true}], 1);
}

async function poll() {
  try {
    const r = await fetch("trace.json", {cache: "no-store"});
    DATA = await r.json();
    const key = DATA.events.length + ":" + (DATA.events.length ? DATA.events[DATA.events.length - 1].t : "");
    if (key !== lastKey) { lastKey = key; render(); }
  } catch (e) { $("meta").textContent = "viewer offline: " + e; }
}
$("sess").addEventListener("change", e => { const all = sessions(DATA.events); chosen = +e.target.value === all.length - 1 ? null : +e.target.value; render(); });
$("showDetails").addEventListener("change", e => { document.body.classList.toggle("details", e.target.checked); render(); });
poll(); setInterval(poll, 1000);
</script>
</body></html>
"""

if __name__ == "__main__":
    main()
