"""Tray icon + frosted-glass sidebar whose colour follows the mood.

    python -m frontend.app              # starts and stops the daemon itself
    python -m frontend.app --no-daemon  # development: run `python -m backend.daemon` yourself

Clicking the tray icon toggles a frameless sidebar anchored above the
taskbar. On Windows 10/11 the window gets the Acrylic blur-behind material
via SetWindowCompositionAttribute (plus rounded corners on Windows 11); on
other platforms a translucent tint is painted instead. Over it the window
paints the glass of the reference design (Glass design UI.jpg): a tint from
the current mood that glides to each new colour, a dark diagonal band, a soft
highlight and a thin bright border. Mood clicks write config.json directly
(atomic replace), which the daemon picks up. The sidebar re-reads
data/state.json the moment the daemon replaces it (file watcher), so the song,
progress and mood dot change together.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import QEvent, QFileSystemWatcher, QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (QAction, QColor, QGuiApplication, QIcon, QLinearGradient, QPainter, QPainterPath, QPen,
                         QPixmap, QRadialGradient)
from PyQt6.QtWidgets import QApplication, QDialog, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

from frontend.components import DEFAULT_TINT, SidebarPanel, blend
from shared.config_io import (APP_HOME, DEFAULT_CONFIG_PATH, FROZEN, PROJECT_ROOT, ensure_app_home, load_config,
                              resolve_path, set_paused, set_target_mood, start_new_session)

log = logging.getLogger("ui")

SIDEBAR_SIZE = (330, 680)
RADIUS = 24


def enable_acrylic(hwnd: int, tint_abgr: int = 0x40140A10) -> bool:
    """Windows 10/11 Acrylic blur behind a frameless window. Returns success."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class ACCENT_POLICY(ctypes.Structure):
            _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                        ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]

        class WINCOMPATTRDATA(ctypes.Structure):
            _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]

        accent = ACCENT_POLICY(4, 2, tint_abgr, 0)  # ACCENT_ENABLE_ACRYLICBLURBEHIND
        data = WINCOMPATTRDATA(19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p),  # WCA_ACCENT_POLICY
                               ctypes.sizeof(accent))
        user32 = ctypes.windll.user32
        user32.SetWindowCompositionAttribute.argtypes = [wintypes.HWND, ctypes.c_void_p]
        ok = bool(user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data)))

        # Windows 11: ask DWM for rounded corners (ignored on Windows 10).
        corner = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(corner),
                                                   ctypes.sizeof(corner))
        return ok
    except Exception as e:  # never let cosmetics break the app
        log.debug("acrylic unavailable: %s", e)
        return False


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
        self.acrylic = False
        self.hidden_at = 0.0
        self.tint = QColor(DEFAULT_TINT)
        self._from = QColor(self.tint)
        self._to = QColor(self.tint)
        self._glide_start = 0.0
        self._glide = QTimer(self)
        self._glide.timeout.connect(self._glide_step)
        panel.tintChanged.connect(self.set_tint)
        panel.closeRequested.connect(self.hide)

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

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self.acrylic:
            self.acrylic = enable_acrylic(int(self.winId()))

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, RADIUS, RADIUS)
        p.setClipPath(path)
        w, h = r.width(), r.height()
        dense = 0 if self.acrylic else 40  # without the OS blur behind, a slightly denser glass

        # 1. Tinted glass: deep at the top, brighter towards the bottom.
        base = QLinearGradient(0, 0, 0, h)
        top, mid, low = self.tint.darker(300), self.tint.darker(125), QColor(self.tint)
        top.setAlpha(215 + dense // 2)
        mid.setAlpha(175 + dense)
        low.setAlpha(170 + dense)
        base.setColorAt(0.0, top)
        base.setColorAt(0.45, mid)
        base.setColorAt(1.0, low)
        p.fillPath(path, base)

        # 2. The dark diagonal bands that make it read as glass (as in the reference).
        for start, end, stops in (((0.0, 0.05), (1.0, 0.45), (0.0, 0.42, 0.60, 0.80)),
                                  ((0.0, 0.55), (1.0, 0.95), (0.55, 0.78, 0.90, 1.0))):
            band = QLinearGradient(w * start[0], h * start[1], w * end[0], h * end[1])
            band.setColorAt(stops[0], QColor(8, 4, 8, 0))
            band.setColorAt(stops[1], QColor(8, 4, 8, 120))
            band.setColorAt(stops[2], QColor(8, 4, 8, 60))
            band.setColorAt(stops[3], QColor(8, 4, 8, 0))
            p.fillPath(path, band)

        # 3. A soft highlight where light would catch the glass.
        glow = QRadialGradient(QPointF(w * 0.2, h * 0.55), w * 0.9)
        glow.setColorAt(0.0, QColor(255, 255, 255, 34))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(path, glow)

        # 4. A thin bright edge, slightly tinted.
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
                 choose_account: Callable[[object], object | None] | None = None, daemon=None):
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
    tray_app = MoodTrayApp(account=account, daemon=daemon)
    tray_app.tray.showMessage("Ambient Mood", "Running in the tray - click the icon to choose a mood.",
                              QSystemTrayIcon.MessageIcon.Information, 3000)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
