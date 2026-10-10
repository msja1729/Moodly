"""UI: tray icon, frameless glass sidebar and mood click events (pytest-qt)."""
import json
import time

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QSystemTrayIcon

from frontend.app import MoodTrayApp, mood_icon
from frontend.components import MoodMap, SidebarPanel
from shared.config_io import load_config


@pytest.fixture
def tray_app(qtbot, tmp_config):
    app = MoodTrayApp(tmp_config, tmp_config.parent / "state.json", poll_ms=60_000)
    qtbot.addWidget(app.window)
    yield app
    app.timer.stop()
    app.tray.hide()


def test_tray_icon_launches(tray_app):
    if not QSystemTrayIcon.isSystemTrayAvailable():
        pytest.skip("no system tray on this desktop")
    assert tray_app.tray.isVisible()
    assert not tray_app.tray.icon().isNull()
    assert "Focused" in tray_app.tray.toolTip()


def test_tray_click_opens_frameless_glass_sidebar(qtbot, tray_app):
    assert not tray_app.window.isVisible()
    tray_app.on_tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
    qtbot.waitExposed(tray_app.window)
    w = tray_app.window
    assert w.isVisible()
    assert w.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert w.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    # Clicking the tray again closes it.
    tray_app.on_tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
    assert not w.isVisible()


def test_context_menu_has_open_account_log_out_and_quit(tray_app):
    labels = [a.text() for a in tray_app.menu.actions() if not a.isSeparator()]
    assert labels == ["Open", "Spotify: not signed in", "Log out", "Quit"]


@pytest.mark.parametrize("mood", ["calm", "energized", "wind_down", "focused"])
def test_mood_click_updates_config_under_100ms(qtbot, tray_app, tmp_config, mood):
    tray_app.show_sidebar()
    qtbot.waitExposed(tray_app.window)
    tile = tray_app.panel.tiles[mood]

    started = time.perf_counter()
    qtbot.mouseClick(tile, Qt.MouseButton.LeftButton)
    elapsed = time.perf_counter() - started

    cfg = json.loads(tmp_config.read_text(encoding="utf-8"))
    expected = cfg["moods"][mood]
    assert cfg["target_mood"] == mood
    assert cfg["target"] == {"valence": expected["valence"], "energy": expected["energy"]}
    assert elapsed < 0.1, f"click -> config.json took {elapsed * 1000:.0f} ms"
    assert tile.isChecked()
    assert sum(t.isChecked() for t in tray_app.panel.tiles.values()) == 1


def test_select_mood_reports_write_time(tray_app, tmp_config):
    assert tray_app.select_mood("calm") < 100
    assert load_config(tmp_config)["target_mood"] == "calm"


def test_pause_toggle_writes_config(qtbot, tray_app, tmp_config):
    tray_app.show_sidebar()
    qtbot.mouseClick(tray_app.panel.pause, Qt.MouseButton.LeftButton)
    assert load_config(tmp_config)["paused"] is True
    qtbot.mouseClick(tray_app.panel.pause, Qt.MouseButton.LeftButton)
    assert load_config(tmp_config)["paused"] is False


def test_external_config_change_reflected(tray_app, tmp_config):
    from shared.config_io import set_target_mood

    set_target_mood("energized", tmp_config)
    tray_app.refresh()
    assert tray_app.panel.tiles["energized"].isChecked()
    assert "Energized" in tray_app.tray.toolTip()


def test_daemon_state_shown(tray_app, tmp_config):
    state = {"updated": time.time(), "mood": [0.8, 0.8], "target_mood": "focused", "target": [0.5, 0.45],
             "deviation": 0.47, "expected_steps": 4.0, "track": "Band - Song", "last_bridge": "A - B",
             "kelvin": 6100, "paused": False}
    (tmp_config.parent / "state.json").write_text(json.dumps(state), encoding="utf-8")
    tray_app.refresh()
    assert tray_app.panel.now_playing.text() == "Band - Song"
    assert "0.47 from target" in tray_app.panel.details.text()
    assert tray_app.panel.map.mood == (0.8, 0.8)


def test_stale_state_means_daemon_not_running(tray_app, tmp_config):
    (tmp_config.parent / "state.json").write_text(json.dumps({"updated": 0, "track": "old"}), encoding="utf-8")
    tray_app.refresh()
    assert tray_app.panel.details.text() == "Daemon not running"


def test_components_render(qtbot, tmp_config):
    panel = SidebarPanel(load_config(tmp_config)["moods"])
    qtbot.addWidget(panel)
    panel.resize(340, 520)
    panel.map.set_state((0.2, 0.9), (0.5, 0.45), "#5B8DEF")
    assert not panel.grab().isNull()
    assert not mood_icon("#9B7FE6").isNull()
    assert isinstance(panel.map, MoodMap)


def _session_state(status, **session):
    return {"updated": time.time(), "mood": [0.7, 0.6], "target_mood": "focused", "target": [0.5, 0.45],
            "deviation": 0.25, "expected_steps": 3.0, "track": "Band - Song", "kelvin": 6000, "paused": False,
            "mode": "session", "session": {"status": status, "queue_size": 14, "last_added": [],
                                           "last_removed": [], **session}}


def test_session_progress_shown(tray_app, tmp_config):
    state = _session_state("managing", last_added=["A - One", "B - Two"], last_removed=["C - Far"])
    (tmp_config.parent / "state.json").write_text(json.dumps(state), encoding="utf-8")
    tray_app.refresh()
    text = tray_app.panel.details.text()
    assert "0.25 from target" in text and "14 songs ahead" in text and "+2 -1 last update" in text


@pytest.mark.parametrize("status, words", [("waiting", "Start playing"), ("user_paused", "paused"),
                                           ("user_took_over", "other music"), ("not_premium", "Premium")])
def test_session_notes_shown(tray_app, tmp_config, status, words):
    (tmp_config.parent / "state.json").write_text(json.dumps(_session_state(status)), encoding="utf-8")
    tray_app.refresh()
    assert words in tray_app.panel.details.text()


def test_quit_file_quits_the_app(qtbot, tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QApplication

    from frontend.app import watch_quit_requests

    quit_file = tmp_path / "app.quit"
    quit_file.touch()  # stale file from a cancelled uninstall: removed, not obeyed
    calls = []
    monkeypatch.setattr(QApplication, "quit", lambda: calls.append(1))
    timer = watch_quit_requests(quit_file, interval_ms=20)
    assert not quit_file.exists()
    qtbot.wait(100)
    assert calls == []

    quit_file.touch()
    qtbot.waitUntil(lambda: calls == [1], timeout=2000)
    assert not quit_file.exists()
    timer.stop()


def test_quit_file_closes_an_open_dialog(qtbot, tmp_path, monkeypatch):
    # The account window runs before the main loop, where Qt 6's quit() alone does nothing.
    from PyQt6.QtWidgets import QApplication, QDialog

    from frontend.app import watch_quit_requests

    monkeypatch.setattr(QApplication, "quit", lambda: None)
    dialog = QDialog()
    qtbot.addWidget(dialog)
    dialog.open()  # modal
    qtbot.waitUntil(lambda: QApplication.activeModalWidget() is dialog, timeout=2000)
    quit_file = tmp_path / "app.quit"
    timer = watch_quit_requests(quit_file, interval_ms=20)
    quit_file.touch()
    qtbot.waitUntil(lambda: not dialog.isVisible(), timeout=2000)
    assert dialog.result() == QDialog.DialogCode.Rejected
    timer.stop()
