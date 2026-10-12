"""Trial reports (developer tool): the mood path dashboard saves each session here.

    reports/trials/<trial id>/
        report.html            the trial at a glance: summary, graphs, every step
        plane.png / .svg       the mood path on the valence x energy plane
        plane_details.png/.svg the same with waypoints, every pick and the grid
        gap.png / .svg         gap to the target over time
        valence_energy.png/.svg valence and energy over time, against the target
        summary.json           the measurements (see METRICS)
        trial.json             the session's raw trace events
    reports/trials/README.md   every trial in one table, with averages

The dashboard (tools/mood_trace_view.py) saves a session automatically when it ends, and on "Save trial".
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from shared.config_io import PROJECT_ROOT

REPORTS_DIR = PROJECT_ROOT / "reports" / "trials"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
TEXT_FILES = {".svg", ".json", ".html", ".md"}
BINARY_FILES = {".png"}

# summary.json key -> (column title, how to show it)
METRICS = [
    ("date", "Date", "{}"),
    ("target", "Target", "{}"),
    ("source", "Songs from", "{}"),
    ("minutes", "Minutes", "{:.1f}"),
    ("start_gap", "Gap at start", "{:.2f}"),
    ("end_gap", "Gap at end", "{:.2f}"),
    ("best_gap", "Best gap", "{:.2f}"),
    ("closed_pct", "Closed %", "{:.0f}"),
    ("min_to_0_1", "Min to gap ≤ 0.10", "{:.1f}"),
    ("min_to_0_05", "Min to gap ≤ 0.05", "{:.1f}"),
    ("picks_toward_pct", "Picks toward %", "{:.0f}"),
    ("setbacks", "Setbacks", "{}"),
    ("worst_setback", "Worst setback", "{:.2f}"),
    ("songs_played", "Songs played", "{}"),
    ("skip_pct", "Skipped %", "{:.0f}"),
    ("added", "Added", "{}"),
    ("removed", "Removed", "{}"),
]
AVERAGED = ["minutes", "closed_pct", "min_to_0_1", "min_to_0_05", "picks_toward_pct", "setbacks", "skip_pct",
            "end_gap"]


def save_trial(payload: dict, root: Path = REPORTS_DIR) -> Path:
    """Write one trial's folder from the dashboard's payload {id, summary, events, files: {name: text|base64}}."""
    trial_id = str(payload.get("id", ""))
    if not SAFE_NAME.match(trial_id):
        raise ValueError(f"bad trial id {trial_id!r}")
    folder = root / trial_id
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in (payload.get("files") or {}).items():
        ext = Path(name).suffix.lower()
        if not SAFE_NAME.match(name) or ext not in TEXT_FILES | BINARY_FILES:
            raise ValueError(f"bad file name {name!r}")
        if ext in BINARY_FILES:
            (folder / name).write_bytes(base64.b64decode(content))
        else:
            (folder / name).write_text(content, encoding="utf-8")
    (folder / "summary.json").write_text(json.dumps(payload.get("summary", {}), indent=2, ensure_ascii=False),
                                         encoding="utf-8")
    (folder / "trial.json").write_text(json.dumps(payload.get("events", []), ensure_ascii=False), encoding="utf-8")
    write_index(root)
    return folder


def saved_ids(root: Path = REPORTS_DIR) -> list[str]:
    return sorted(p.parent.name for p in root.glob("*/summary.json")) if root.exists() else []


def _cell(fmt: str, value) -> str:
    if value is None or value == "":
        return "–"
    try:
        return fmt.format(value)
    except (ValueError, TypeError):
        return str(value)


def write_index(root: Path = REPORTS_DIR) -> Path:
    """reports/trials/README.md: one row per trial, then the averages."""
    rows = []
    for trial_id in saved_ids(root):
        try:
            rows.append((trial_id, json.loads((root / trial_id / "summary.json").read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    lines = ["# Mood path trials", "",
             "Saved by the mood path dashboard (developer tool). Each trial's folder has `report.html` with the graphs.",
             "",
             "**Gap** = distance from the mood to the target (0 = there). **Closed %** = how much of the gap at the "
             "start was closed. **Picks toward %** = songs Moodly added that were closer to the target than the mood "
             "when it added them. **Setbacks** = times the gap grew by more than 0.05 between two steps.", ""]
    lines.append("| Trial | " + " | ".join(title for _, title, _ in METRICS) + " |")
    lines.append("|" + "---|" * (len(METRICS) + 1))
    for trial_id, s in rows:
        cells = [_cell(fmt, s.get(key)) for key, _, fmt in METRICS]
        lines.append(f"| [{trial_id}]({trial_id}/report.html) | " + " | ".join(cells) + " |")
    if rows:
        avg = []
        for key, _, fmt in METRICS:
            vals = [s[key] for _, s in rows if isinstance(s.get(key), (int, float))]
            avg.append(_cell(fmt, sum(vals) / len(vals)) if key in AVERAGED and vals else "")
        lines.append(f"| **Average of {len(rows)}** | " + " | ".join(avg) + " |")
    root.mkdir(parents=True, exist_ok=True)
    path = root / "README.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
