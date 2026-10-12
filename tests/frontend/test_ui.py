"""UI: tray icon, frameless glass sidebar and mood click events (pytest-qt)."""
import json
import time

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
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


def test_session_buttons_write_config(qtbot, tray_app, tmp_config):
    panel = tray_app.panel
    tray_app.end_session()  # as at launch: no session yet
    tray_app.show_sidebar()
    assert panel.start_session.isVisible() and not panel.end_session.isVisible()
    qtbot.mouseClick(panel.start_session, Qt.MouseButton.LeftButton)
    assert load_config(tmp_config)["paused"] is False
    assert panel.end_session.isVisible() and panel.new_session.isVisible() and not panel.start_session.isVisible()

    seq = load_config(tmp_config).get("session_seq", 0)
    qtbot.mouseClick(panel.new_session, Qt.MouseButton.LeftButton)
    cfg = load_config(tmp_config)
    assert cfg["session_seq"] == seq + 1 and cfg["paused"] is False  # the daemon ends + restarts on this

    qtbot.mouseClick(panel.end_session, Qt.MouseButton.LeftButton)
    assert load_config(tmp_config)["paused"] is True
    assert panel.start_session.isVisible() and not panel.end_session.isVisible()


def test_preparing_and_no_session_messages(qtbot):
    panel = SidebarPanel({"focused": {"label": "Focused", "valence": 0.5, "energy": 0.45}})
    qtbot.addWidget(panel)
    panel.show_state({"track": "A - B", "paused": True}, "focused")
    assert "Start session" in panel.details.text()
    panel.show_state({"track": "A - B", "paused": False,
                      "session": {"status": "managing", "preparing": True},
                      "recommender": {"useful": 7, "ready_songs": 15, "languages": ["Telugu", "Hindi"]}}, "focused")
    assert "Preparing" in panel.details.text() and "7/15" in panel.details.text() and "Telugu" in panel.details.text()


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


def test_state_file_change_shows_at_once(qtbot, tmp_config):
    # A long fallback timer: only the file watcher can make this pass in time.
    app = MoodTrayApp(tmp_config, tmp_config.parent / "state.json", poll_ms=60_000)
    qtbot.addWidget(app.window)
    state = {"updated": time.time(), "mood": [0.3, 0.2], "track": "X - Y", "track_name": "Kesariya",
             "track_artists": "Pritam, Arijit Singh", "progress_ms": 60_000, "duration_ms": 240_000,
             "is_playing": True, "observed_at": time.time(), "paused": False}
    from shared.config_io import atomic_write_json
    atomic_write_json(tmp_config.parent / "state.json", state)
    qtbot.waitUntil(lambda: app.panel.now_playing.text() == "Kesariya", timeout=1000)
    assert app.panel.artist.text() == "Pritam, Arijit Singh"
    assert 0.24 < app.panel.progress.fraction < 0.3
    assert app.panel.map.mood == (0.3, 0.2)
    app.timer.stop()
    app.tray.hide()


def test_glass_tint_follows_the_mood(qtbot, tmp_config):
    from frontend.components import mood_color

    hues = {name: mood_color(pt).hsvHue() for name, pt in
            {"happy+energetic": (0.9, 0.9), "tense": (0.1, 0.9), "sad": (0.1, 0.1), "calm": (0.9, 0.1)}.items()}
    assert len(set(hues.values())) == 4                           # four corners, four colours
    assert 20 <= hues["happy+energetic"] <= 50                    # amber
    assert 150 <= hues["calm"] <= 185                             # teal
    assert mood_color((0.5, 0.5)).hsvSaturationF() < mood_color((0.95, 0.95)).hsvSaturationF()

    app = MoodTrayApp(tmp_config, tmp_config.parent / "state.json", poll_ms=60_000)
    qtbot.addWidget(app.window)
    app.show_sidebar()
    qtbot.waitExposed(app.window)
    from shared.config_io import atomic_write_json
    atomic_write_json(tmp_config.parent / "state.json", {"updated": time.time(), "mood": [0.9, 0.1], "paused": False})
    target = mood_color((0.9, 0.1))
    qtbot.waitUntil(lambda: app.window.tint == target, timeout=3000)  # the daemon's new mood -> glided there
    assert app.panel.tiles["calm"].marked                             # the closest mood gets the dot
    assert not app.window.grab().isNull()
    app.timer.stop()
    app.tray.hide()


def test_glass_blurs_what_is_behind(qtbot, tray_app):
    from PyQt6.QtGui import QColor, QImage, QPixmap

    from frontend.app import blur_pixmap

    img = QImage(200, 200, QImage.Format.Format_RGB32)
    for y in range(200):
        for x in range(200):
            img.setPixelColor(x, y, QColor("white") if (x // 10 + y // 10) % 2 else QColor("black"))  # sharp checkers
    blurred = blur_pixmap(QPixmap.fromImage(img)).toImage()
    assert blurred.size() == img.size()
    grey = blurred.pixelColor(100, 100).value()
    assert 40 < grey < 215  # sharp black/white squares became a soft mix

    tray_app.show_sidebar()  # snapshots the screen behind before showing (null on headless test screens is fine)
    qtbot.waitExposed(tray_app.window)
    assert tray_app.window.backdrop is None or not tray_app.window.backdrop.isNull()
    assert not tray_app.window.grab().isNull()


# ----------------------------------------------------------------- pointer effects

def test_pointer_leaves_waves_in_the_glass(qtbot, tray_app):
    from PyQt6.QtCore import QPointF

    w = tray_app.window
    tray_app.show_sidebar()
    qtbot.waitExposed(w)
    w._pointer.stop()  # drive it by hand
    t = 100.0
    w.track(QPointF(100, 300), t)
    assert w.waves == []                      # standing still: no waves
    for i in range(1, 6):                      # move 40 px at a time (a wave every 38 px)
        w.track(QPointF(100 + 40 * i, 300), t + 0.05 * i)
    assert len(w.waves) == 5
    assert all(0.4 < wave[3] <= 1.0 for wave in w.waves)
    assert not w.grab().isNull()             # paints with waves alive
    w.track(QPointF(-50, -50), t + 0.3 + w.WAVE_LIFE)  # pointer gone and the waves have faded
    assert w.waves == []
    for i in range(40):                        # a long, fast stroke keeps only the newest waves
        w.track(QPointF(10 + (i % 2) * 300, 300), t + 10 + 0.01 * i)
    assert len(w.waves) == w.MAX_WAVES
    w.hide()
    assert not w._pointer.isActive() and w.waves == []  # nothing runs while the sidebar is closed


def test_row_hover_ripple_and_sliding_selection(qtbot, tray_app):
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QEnterEvent

    tray_app.show_sidebar()
    qtbot.waitExposed(tray_app.window)
    panel = tray_app.panel
    row = panel.tiles["calm"]
    row.enterEvent(QEnterEvent(QPointF(40, 15), QPointF(40, 15), QPointF(40, 15)))
    qtbot.waitUntil(lambda: row.hover > 0.99, timeout=1000)  # the pill fades in
    row.leaveEvent(QEvent(QEvent.Type.Leave))
    qtbot.waitUntil(lambda: row.hover < 0.01, timeout=1000)  # and out

    qtbot.mousePress(row, Qt.MouseButton.LeftButton, pos=QPointF(60, 15).toPoint())
    assert row._ripple_anim.state() == row._ripple_anim.State.Running
    qtbot.waitUntil(lambda: row.ripple > 0.99, timeout=1000)
    qtbot.mouseRelease(row, Qt.MouseButton.LeftButton, pos=QPointF(60, 15).toPoint())

    tray_app.timer.stop()  # keep the app's refresh from re-selecting the configured mood meanwhile
    tray_app._debounce.stop()
    tray_app.watcher.blockSignals(True)
    qtbot.waitUntil(lambda: not panel.selection.isNull(), timeout=1000)
    panel.set_selected("focused")
    qtbot.waitUntil(lambda: panel.selection == panel._selection_target(), timeout=1000)
    start = panel.selection.top()
    panel.set_selected("wind_down")      # slides from Focused down to Wind Down
    target = panel._selection_target()
    qtbot.wait(60)
    assert start < panel.selection.top() < target.top()  # on its way
    qtbot.waitUntil(lambda: panel.selection == target, timeout=1000)


def test_mood_map_dot_breathes_only_while_shown(qtbot):
    from frontend.components import MoodMap

    m = MoodMap()
    qtbot.addWidget(m)
    m.resize(280, 112)
    m.set_state((0.3, 0.3), (0.6, 0.25), "#4FC1A6")
    m.show()
    qtbot.waitExposed(m)
    assert m._pulse.isActive()
    assert m.cursor().shape() == Qt.CursorShape.ArrowCursor  # no crosshair
    assert not m.grab().isNull()
    m.hide()
    assert not m._pulse.isActive()


def test_player_buttons_send_commands_and_follow_at_once(qtbot, tmp_config):
    app = MoodTrayApp(tmp_config, tmp_config.parent / "state.json", poll_ms=60_000)
    qtbot.addWidget(app.window)
    panel = app.panel
    assert not panel.play_pause.isEnabled()  # nothing playing yet
    state = {"track_name": "Kesariya", "track_artists": "Pritam", "progress_ms": 60_000, "duration_ms": 240_000,
             "is_playing": True, "observed_at": time.time(), "paused": True}
    panel.show_state(state, "focused")
    assert panel.play_pause.kind == "pause" and panel.time.text() == "1:00 / 4:00"
    cmd = tmp_config.parent / "player.cmd"
    panel.play_pause.click()
    assert json.loads(cmd.read_text(encoding="utf-8"))["cmd"] == "pause"
    assert panel.play_pause.kind == "play"  # at once, before Spotify answers
    frozen = panel.progress.fraction
    panel.show_state(state, "focused")  # an older state (still playing) arrives: the click wins
    assert panel.play_pause.kind == "play"
    time.sleep(0.05)
    panel._tick()
    assert panel.progress.fraction == frozen
    panel.show_state({**state, "is_playing": False, "observed_at": time.time()}, "focused")
    assert panel.play_pause.kind == "play" and panel._pending_play is None
    panel.next_song.click()
    assert json.loads(cmd.read_text(encoding="utf-8"))["cmd"] == "next"
    app.timer.stop()
    app.tray.hide()


def test_cover_art_shows_the_album_image(qtbot):
    from PyQt6.QtGui import QImage
    panel = SidebarPanel({"focused": {"label": "Focused", "valence": 0.5, "energy": 0.45}})
    qtbot.addWidget(panel)
    img = QImage(300, 300, QImage.Format.Format_RGB32)
    img.fill(QColor("#336699"))
    panel.covers._store("https://i.scdn.co/image/abc", img)  # as if downloaded
    panel.show_state({"track_name": "Song", "track_image": "https://i.scdn.co/image/abc", "duration_ms": 1000,
                      "progress_ms": 0, "is_playing": False, "observed_at": time.time()}, "focused")
    assert panel.cover.image is not None and panel.cover.image.pixelColor(10, 10) == QColor("#336699")
    panel.show_state(None, "focused")
    assert panel.cover.image is None  # placeholder
