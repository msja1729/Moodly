"""Trial reports saved by the mood path dashboard (tools/trial_reports.py)."""
import base64
import json

import pytest

from tools import trial_reports


def payload(trial_id, closed, minutes=10.0):
    return {"id": trial_id, "events": [{"kind": "cycle"}],
            "summary": {"date": "today", "target": "Energized", "source": "Liked Songs", "minutes": minutes,
                        "closed_pct": closed, "end_gap": 0.05, "setbacks": 1},
            "files": {"plane.png": base64.b64encode(b"\x89PNG fake").decode(), "plane.svg": "<svg/>",
                      "report.html": "<html></html>"}}


def test_a_trial_folder_and_the_table_of_all_trials(tmp_path):
    folder = trial_reports.save_trial(payload("2026-10-12_1600_energized_liked-songs", 90.0), tmp_path)
    assert (folder / "plane.png").read_bytes() == b"\x89PNG fake"
    assert (folder / "plane.svg").read_text() == "<svg/>" and (folder / "report.html").exists()
    assert json.loads((folder / "summary.json").read_text())["closed_pct"] == 90.0
    assert json.loads((folder / "trial.json").read_text()) == [{"kind": "cycle"}]
    trial_reports.save_trial(payload("2026-10-12_1700_calm_for-you", 70.0, 20.0), tmp_path)
    table = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "[2026-10-12_1600_energized_liked-songs](2026-10-12_1600_energized_liked-songs/report.html)" in table
    assert "**Average of 2**" in table and "| 80 |" in table and "| 15.0 |" in table  # closed % and minutes
    assert trial_reports.saved_ids(tmp_path) == ["2026-10-12_1600_energized_liked-songs", "2026-10-12_1700_calm_for-you"]


@pytest.mark.parametrize("bad", [{"id": "../escape"}, {"id": "ok", "files": {"../x.png": ""}},
                                 {"id": "ok", "files": {"run.exe": ""}}])
def test_names_that_could_escape_the_folder_are_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        trial_reports.save_trial(bad, tmp_path)
