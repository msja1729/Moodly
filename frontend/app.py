"""Tray icon + frosted-glass sidebar whose colour follows the mood.

    python -m frontend.app              # starts and stops the daemon itself
    python -m frontend.app --no-daemon  # development: run `python -m backend.daemon` yourself

Clicking the tray icon toggles a frameless sidebar anchored above the
taskbar. On Windows 10/11 the window gets the Acrylic blur-behind material
via SetWindowCompositionAttribute (plus rounded corners on Windows 11); on
other platforms alike the window paints the glass of the reference design (Glass design UI.jpg) over a blurred
snapshot of what's behind it (taken as the sidebar opens): a tint from
the current mood that glides to each new colour, a dark diagonal band, a soft
highlight and a thin bright border. Mood clicks write config.json directly
(atomic replace), which the daemon picks up. The sidebar re-reads
data/state.json the moment the daemon replaces it (file watcher), so the song,
progress and mood dot change together. Moving the pointer over the sidebar
leaves soft waves in the glass, behind all text (see also the row effects in
components.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QEvent, QFileSystemWatcher, QPointF, QRectF, Qt, QTimer, QUrl
from PyQt6.QtGui import (QAction, QColor, QCursor, QDesktopServices, QGuiApplication, QIcon, QLinearGradient,
                         QPainter, QPainterPath, QPen, QPixmap, QRadialGradient)
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

from frontend.components import DEFAULT_TINT, SidebarPanel, blend
from shared.config_io import (APP_HOME, DEFAULT_CONFIG_PATH, FROZEN, PROJECT_ROOT, atomic_write_json,
                              ensure_app_home, load_config, resolve_path, set_paused, set_song_source,
                              set_target_mood, start_new_session)

log = logging.getLogger("ui")

SIDEBAR_SIZE = (330, 680)
RADIUS = 24


def blur_pixmap(pm: QPixmap, radius: int = 14) -> QPixmap:
    """A soft blur: shrink a lot, then scale back up smoothly (in two steps, so it's even)."""
    if pm.isNull():
        return pm
    img = pm.toImage()
    w, h = img.width(), img.height()
    small = img.scaled(max(1, w // (radius // 2)), max(1, h // (radius // 2)), Qt.AspectRatioMode.IgnoreAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    tiny = small.scaled(max(1, small.width() // 2), max(1, small.height() // 2), Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
    up = tiny.scaled(small.width(), small.height(), Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)
    out = QPixmap.fromImage(up.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation))
    out.setDevicePixelRatio(pm.devicePixelRatio())
    return out


class GlassWindow(QWidget):
    """Frameless, translucent tool window that hosts the sidebar panel and paints its glass."""

    GLIDE_MS = 900  # a new tint is reached over about this long

    def __init__(self, panel: SidebarPanel):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("Ambient Mood")
        self.resize(*SIDEBAR_SIZE)
        self.panel = panel
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(panel)
        self.backdrop: QPixmap | None = None  # blurred snapshot of the screen behind the sidebar
        self.hidden_at = 0.0
        self.tint = QColor(DEFAULT_TINT)
        self._from = QColor(self.tint)
        self._to = QColor(self.tint)
        self._glide_start = 0.0
        self._glide = QTimer(self)
        self._glide.timeout.connect(self._glide_step)
        panel.tintChanged.connect(self.set_tint)
        # Waves the pointer leaves in the glass: [x, y, born (monotonic s), strength 0..1].
        self.waves: list[list[float]] = []
        self._last_wave: QPointF | None = None
        self._pointer = QTimer(self)  # runs only while the sidebar is open
        self._pointer.timeout.connect(self._follow_pointer)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._pointer.start(16)

    def hideEvent(self, event) -> None:
        self._pointer.stop()
        self.waves.clear()
        self._last_wave = None
        super().hideEvent(event)

    def _follow_pointer(self) -> None:
        self.track(QPointF(self.mapFromGlobal(QCursor.pos())))

    WAVE_STEP = 38.0    # a new wave every this many pixels of pointer movement
    WAVE_LIFE = 1.6     # seconds a wave takes to spread out and fade
    WAVE_REACH = 150.0  # px a wave spreads in its lifetime
    MAX_WAVES = 14

    def track(self, pos: QPointF, now: float | None = None) -> None:
        """One step: the pointer at `pos` (window coordinates) leaves waves as it moves; old ones fade."""
        now = time.monotonic() if now is None else now
        inside = QRectF(self.rect()).contains(pos)
        if inside:
            if self._last_wave is None:
                self._last_wave = QPointF(pos)
            moved = math.dist((pos.x(), pos.y()), (self._last_wave.x(), self._last_wave.y()))
            if moved >= self.WAVE_STEP:
                strength = min(1.0, 0.45 + moved / 120)  # faster movement, stronger waves
                self.waves.append([pos.x(), pos.y(), now, strength])
                self.waves = self.waves[-self.MAX_WAVES:]
                self._last_wave = QPointF(pos)
        else:
            self._last_wave = None
        alive = [w for w in self.waves if now - w[2] < self.WAVE_LIFE]
        if alive or self.waves:
            self.waves = alive
            self.update()

    def _paint_waves(self, p: QPainter, path: QPainterPath, now: float) -> None:
        """Each wave is a ring: a darker inner edge and a lighter outer edge, like a fold in the glass."""
        light = self.tint.lighter(175)
        for x, y, born, strength in self.waves:
            t = (now - born) / self.WAVE_LIFE
            if not 0 <= t < 1:
                continue
            r = 10 + self.WAVE_REACH * (1 - (1 - t) ** 2)  # quick at first, then slower
            a = strength * (1 - t) ** 1.5
            width = 26 + 22 * t  # wide, soft rings read as folds rather than outlines
            outer = r + width
            g = QRadialGradient(QPointF(x, y), outer)
            g.setColorAt(max(0.0, (r - width) / outer), QColor(0, 0, 0, 0))
            g.setColorAt(max(0.0, (r - width * 0.4) / outer), QColor(6, 2, 8, round(30 * a)))
            g.setColorAt(r / outer, QColor(light.red(), light.green(), light.blue(), round(40 * a)))
            g.setColorAt(min(1.0, (r + width * 0.6) / outer), QColor(255, 255, 255, round(12 * a)))
            g.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillPath(path, g)

    def set_tint(self, color: QColor) -> None:
        """Glide from the current tint to `color`."""
        if color == self._to:
            return
        self._from, self._to = QColor(self.tint), QColor(color)
        self._glide_start = time.monotonic()
        if self.isVisible():
            self._glide.start(16)
        else:
            self.tint = QColor(color)

    def _glide_step(self) -> None:
        t = min(1.0, (time.monotonic() - self._glide_start) * 1000 / self.GLIDE_MS)
        t = t * t * (3 - 2 * t)  # ease in and out
        self.tint = blend(self._from, self._to, t)
        self.update()
        if t >= 1.0:
            self._glide.stop()

    def rounded_path(self) -> QPainterPath:
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)
        return path

    def capture_backdrop(self) -> None:
        """Snapshot and blur the screen where the sidebar is about to appear (call while it's hidden).

        A static snapshot is enough for a flyout. On macOS without the Screen Recording permission
        it holds only the wallpaper, which still reads as frosted glass.
        """
        self.backdrop = None
        screen = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        sg, g = screen.geometry(), self.geometry()
        try:
            shot = screen.grabWindow(0, g.x() - sg.x(), g.y() - sg.y(), g.width(), g.height())
        except Exception as e:  # never let cosmetics break the app
            log.debug("backdrop capture failed: %s", e)
            return
        if not shot.isNull():
            self.backdrop = blur_pixmap(shot)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = self.rounded_path()
        p.setClipPath(path)
        w, h = float(self.width()), float(self.height())
        # 0. What's behind the sidebar, blurred: the glass is thin, so it shows through.
        if self.backdrop is not None:
            p.drawPixmap(self.rect(), self.backdrop)
        dense = 0 if self.backdrop is not None else 95  # no snapshot: a denser glass keeps text readable

        # 1. A light tint over a faint dark veil: deeper at the top, clearer towards the bottom.
        base = QLinearGradient(0, 0, 0, h)
        top, mid, low = self.tint.darker(280), self.tint.darker(130), QColor(self.tint)
        top.setAlpha(125 + dense)
        mid.setAlpha(80 + dense)
        low.setAlpha(70 + dense)
        base.setColorAt(0.0, top)
        base.setColorAt(0.45, mid)
        base.setColorAt(1.0, low)
        p.fillPath(path, base)

        # 2. The dark diagonal bands that make it read as glass (as in the reference), lighter now.
        for start, end, stops in (((0.0, 0.05), (1.0, 0.45), (0.0, 0.42, 0.60, 0.80)),
                                  ((0.0, 0.55), (1.0, 0.95), (0.55, 0.78, 0.90, 1.0))):
            band = QLinearGradient(w * start[0], h * start[1], w * end[0], h * end[1])
            band.setColorAt(stops[0], QColor(8, 4, 8, 0))
            band.setColorAt(stops[1], QColor(8, 4, 8, 80))
            band.setColorAt(stops[2], QColor(8, 4, 8, 40))
            band.setColorAt(stops[3], QColor(8, 4, 8, 0))
            p.fillPath(path, band)

        # 3. A soft highlight where light would catch the glass.
        glow = QRadialGradient(QPointF(w * 0.2, h * 0.55), w * 0.9)
        glow.setColorAt(0.0, QColor(255, 255, 255, 34))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, glow)

        # 4. Waves left by the pointer, in the glass (all text is drawn on top by the panel).
        if self.waves:
            self._paint_waves(p, path, time.monotonic())

        # 5. A thin bright edge, slightly tinted.
        p.setClipping(False)
        edge = QLinearGradient(0, 0, w, h)
        edge.setColorAt(0.0, QColor(255, 255, 255, 120))
        edge.setColorAt(0.5, blend(QColor(255, 255, 255, 40), self.tint, 0.5))
        edge.setColorAt(1.0, QColor(255, 255, 255, 90))
        p.setPen(QPen(edge, 1.2))
        p.drawPath(path)

    def event(self, e) -> bool:
        if e.type() == QEvent.Type.WindowDeactivate and self.isVisible():
            self.hidden_at = time.monotonic()
            self.hide()  # behave like a flyout: click away to dismiss
        return super().event(e)

    def place(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        g = screen.availableGeometry()
        # Next to the tray icon: the taskbar (bottom) on Windows, the menu bar (top) on macOS.
        y = g.top() + 12 if sys.platform == "darwin" else g.bottom() - self.height() - 12
        self.move(g.right() - self.width() - 12, y)


def mood_icon(color: str) -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(color)
    p.setPen(Qt.PenStyle.NoPen)
    halo = QColor(c)
    halo.setAlpha(90)
    p.setBrush(halo)
    p.drawEllipse(4, 4, 56, 56)
    p.setBrush(c)
    p.drawEllipse(14, 14, 36, 36)
    p.setBrush(QColor(255, 255, 255, 220))
    p.drawEllipse(26, 26, 12, 12)
    p.end()
    return QIcon(pm)


class MoodTrayApp:
    def __init__(self, config_path: Path | str = DEFAULT_CONFIG_PATH, state_path: Path | str | None = None,
                 poll_ms: int = 1500, account=None, store=None,
                 choose_account: Callable[[object], object | None] | None = None, daemon=None,
                 dashboard_url: str | None = None):
        self.config_path = Path(config_path)
        self.account = account
        self.store = store
        self.daemon = daemon  # DaemonProcess, or None when the daemon is run separately
        # Shows the account window and returns the chosen Account (None = user quit).
        self.choose_account = choose_account or choose_spotify_account
        self.cfg = load_config(self.config_path)
        self.state_path = Path(state_path) if state_path else resolve_path(
            self.cfg.get("paths", {}).get("state", "data/state.json"))

        self.panel = SidebarPanel(self.cfg["moods"])
        self.window = GlassWindow(self.panel)
        self.panel.moodSelected.connect(self.select_mood)
        self.panel.sessionStartRequested.connect(self.start_session)
        self.panel.sessionEndRequested.connect(self.end_session)
        self.panel.sessionNewRequested.connect(self.new_session)
        self.panel.quitRequested.connect(QApplication.quit)
        self.panel.logoutRequested.connect(self.log_out)
        self.panel.playerCommand.connect(self.player_command)
        self.panel.songSourceSelected.connect(self.select_source)
        self.panel.set_account(account)

        self.tray = QSystemTrayIcon()
        self.tray.setToolTip("Ambient Mood")
        menu = QMenu()
        open_action = QAction("Open", menu)
        open_action.triggered.connect(self.show_sidebar)
        self.account_action = QAction("", menu)
        self.account_action.setEnabled(False)
        logout_action = QAction("Log out", menu)
        logout_action.triggered.connect(self.log_out)
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(QApplication.quit)
        menu.addAction(open_action)
        if dashboard_url:  # development only (see dev_dashboard())
            dash_action = QAction("Mood path dashboard (testing)", menu)
            dash_action.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(dashboard_url)))
            menu.addAction(dash_action)
            self.panel.set_dashboard(dashboard_url)
        menu.addSeparator()
        menu.addAction(self.account_action)
        menu.addAction(logout_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)

        self.refresh()
        self.tray.show()

        self.timer = QTimer()  # a fallback; the file watcher below catches changes at once
        self.timer.timeout.connect(self.refresh)
        self.timer.start(poll_ms)

        # The daemon replaces data/state.json atomically (a rename), so watch its folder and the file.
        self.watcher = QFileSystemWatcher()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.watcher.addPath(str(self.state_path.parent))
        self.watcher.directoryChanged.connect(self._state_touched)
        self.watcher.fileChanged.connect(self._state_touched)
        self._debounce = QTimer()
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self.refresh)
        self._state_touched()

    # ----------------------------------------------------------- actions
    def select_mood(self, key: str) -> float:
        """Write the new target to config.json. Returns the elapsed milliseconds."""
        started = time.perf_counter()
        self.cfg = set_target_mood(key, self.config_path)
        elapsed = (time.perf_counter() - started) * 1000
        log.info("target mood -> %s (%.1f ms)", key, elapsed)
        self.refresh(reload=False)
        return elapsed

    def select_source(self, source: dict) -> None:
        """Where songs come from; the daemon rebuilds the song pool when it sees the change."""
        self.cfg = set_song_source(source, self.config_path)
        log.info("songs from -> %s", source.get("name") or source.get("kind"))
        self.refresh(reload=False)

    def player_command(self, cmd: str) -> None:
        """Play/pause/next: the daemon (watching data/player.cmd) tells Spotify and updates the state."""
        try:
            atomic_write_json(self.state_path.parent / "player.cmd", {"cmd": cmd, "at": time.time()})
        except OSError as e:
            log.warning("could not send %s to the daemon: %s", cmd, e)

    # A session = the daemon managing the music. config "paused" = no session; "session_seq" = new session.
    def start_session(self) -> None:
        self.cfg = set_paused(False, self.config_path)
        self.refresh(reload=False)

    def end_session(self) -> None:
        self.cfg = set_paused(True, self.config_path)
        self.refresh(reload=False)

    def new_session(self) -> None:
        self.cfg = start_new_session(self.config_path)
        self.refresh(reload=False)

    def log_out(self) -> None:
        """Forget the active Spotify account, then let the user choose another one."""
        from backend.spotify.auth import AccountStore

        store = self.store or AccountStore()
        if self.daemon is not None:
            self.daemon.stop()  # end the session while the account's token still works
        gone = store.log_out()
        log.info("logged out of Spotify account %s", gone.display_name if gone else "?")
        self.window.hide()
        self.set_account(None)
        account = self.choose_account(store)
        if account is None:
            QApplication.quit()
            return
        self.set_account(account)
        if self.daemon is not None:
            self.daemon.start()

    def set_account(self, account) -> None:
        self.account = account
        self.panel.set_account(account)
        self.refresh(reload=False)

    def on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.toggle_sidebar()

    def toggle_sidebar(self) -> None:
        if self.window.isVisible():
            self.window.hide()
        elif time.monotonic() - self.window.hidden_at > 0.3:
            # (a click on the tray icon itself deactivates the flyout first;
            # don't immediately reopen it in that case)
            self.show_sidebar()

    def show_sidebar(self) -> None:
        self.refresh()
        self.window.place()
        self.window.capture_backdrop()  # while still hidden: what's behind it
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    # ----------------------------------------------------------- state
    def _state_touched(self, _path: str = "") -> None:
        """The daemon wrote new state: show it right away (several events per write are merged)."""
        if self.state_path.exists() and str(self.state_path) not in self.watcher.files():
            self.watcher.addPath(str(self.state_path))  # re-arm: an atomic replace drops the old watch
        self._debounce.start(30)

    def read_state(self) -> dict | None:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return None
        # Stale state means the daemon isn't running.
        return state if time.time() - state.get("updated", 0) < 300 else None

    def refresh(self, reload: bool = True) -> None:
        if self.daemon is not None:
            self.daemon.check()
        if reload:
            try:
                self.cfg = load_config(self.config_path)
            except Exception:
                pass  # keep the last good config
        key = self.cfg["target_mood"]
        mood = self.cfg["moods"][key]
        self.panel.set_selected(key)
        self.panel.set_session(not self.cfg.get("paused"))
        self.panel.set_source(self.cfg.get("song_source"))
        self.panel.show_state(self.read_state(), key)
        self.tray.setIcon(mood_icon(mood.get("color", "#5B8DEF")))
        who = f" ({self.account.display_name})" if self.account else ""
        self.tray.setToolTip(f"Ambient Mood - {mood.get('label', key)}{who}")
        self.account_action.setText(f"Spotify: {self.account.display_name}" if self.account
                                    else "Spotify: not signed in")


def watch_quit_requests(quit_file: Path, interval_ms: int = 1000) -> QTimer:
    """Quit normally when `quit_file` appears.

    The uninstaller (and an installer upgrading a running copy) creates it, so
    the app ends the session and restores the display instead of being killed.
    During the account window the main loop isn't running yet, and Qt 6's
    quit() does nothing then, so open dialogs are closed first (main() then
    returns as if the user closed the window).
    """
    quit_file.unlink(missing_ok=True)  # left over from an uninstall that was cancelled

    def check():
        if quit_file.exists():
            log.info("quit requested by %s", quit_file)
            quit_file.unlink(missing_ok=True)
            while (modal := QApplication.activeModalWidget()) is not None:
                modal.reject() if isinstance(modal, QDialog) else modal.close()
                if QApplication.activeModalWidget() is modal:
                    break  # refused to close; don't spin
            QApplication.quit()

    timer = QTimer()
    timer.timeout.connect(check)
    timer.start(interval_ms)
    return timer


def dev_dashboard() -> str | None:
    """Development only: with AMBIENT_MOOD_TRACE set (tools/dev_dashboard.ps1 / .sh) and running from
    source, serve the mood path dashboard and return its link. Never in the installed app."""
    trace = os.environ.get("AMBIENT_MOOD_TRACE")
    if FROZEN or not trace:
        return None
    try:
        from tools.mood_trace_view import start_in_background
    except ImportError:
        return None
    url = start_in_background(Path(trace))
    log.info("mood path dashboard: %s", url)
    return url


def choose_spotify_account(store=None):
    """Show the Spotify account window. Returns the chosen Account, or None if the user quit."""
    from backend.spotify.auth import AccountStore
    from frontend.onboarding import SpotifyAccountDialog

    dialog = SpotifyAccountDialog(store or AccountStore())
    return dialog.account if dialog.exec() == SpotifyAccountDialog.DialogCode.Accepted else None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ambient Mood tray app")
    p.add_argument("--no-daemon", action="store_true", help="don't start the daemon (run it yourself)")
    args, _qt_args = p.parse_known_args(argv)

    ensure_app_home()
    log_target = {}
    if FROZEN:  # no console in the installed app
        (APP_HOME / "data").mkdir(parents=True, exist_ok=True)
        log_target = {"filename": str(APP_HOME / "data" / "app.log"), "encoding": "utf-8"}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S",
                        **log_target)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        log.error("no system tray available on this desktop")
        return 1
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")  # optional development overrides
    quit_watch = watch_quit_requests(APP_HOME / "data" / "app.quit")  # noqa: F841 (kept alive)
    set_paused(True, DEFAULT_CONFIG_PATH)  # no session until the user clicks "Start session"
    account = choose_spotify_account()
    if account is None:
        return 0
    daemon = None
    if not args.no_daemon:
        from frontend.daemon_process import DaemonProcess

        daemon = DaemonProcess(DEFAULT_CONFIG_PATH)
        daemon.start()
        app.aboutToQuit.connect(daemon.stop)
    tray_app = MoodTrayApp(account=account, daemon=daemon, dashboard_url=dev_dashboard())
    tray_app.tray.showMessage("Ambient Mood", "Running in the tray - click the icon to choose a mood.",
                              QSystemTrayIcon.MessageIcon.Information, 3000)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
