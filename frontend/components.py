"""Sidebar widgets: mood toggle tiles, a valence/energy map and a status card."""
from __future__ import annotations

from typing import Any, Mapping

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import (QButtonGroup, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                             QVBoxLayout, QWidget)

TEXT = "#F2F2F5"
MUTED = "rgba(242, 242, 245, 158)"

BASE_QSS = f"""
QWidget#SidebarPanel {{ background: transparent; }}
QLabel {{ color: {TEXT}; background: transparent; }}
QLabel#Title {{ font-size: 17px; font-weight: 600; }}
QLabel#Subtitle, QLabel#Muted {{ color: {MUTED}; font-size: 11px; }}
QLabel#Status {{ font-size: 12px; }}
QWidget#Card {{
    background: rgba(255, 255, 255, 18);
    border: 1px solid rgba(255, 255, 255, 23);
    border-radius: 12px;
}}
QPushButton#Pill {{
    color: {TEXT};
    background: rgba(255, 255, 255, 20);
    border: 1px solid rgba(255, 255, 255, 26);
    border-radius: 14px;
    padding: 6px 14px;
    font-size: 12px;
}}
QPushButton#Pill:hover {{ background: rgba(255, 255, 255, 36); }}
QPushButton#Pill:checked {{ background: rgba(255, 255, 255, 217); color: #1B1B1F; }}
QLabel#Account {{ font-size: 11px; font-weight: 600; }}
QPushButton#Link {{
    color: {MUTED};
    background: transparent;
    border: none;
    padding: 0;
    font-size: 11px;
    text-decoration: underline;
}}
QPushButton#Link:hover {{ color: {TEXT}; }}
"""


# What the sidebar says when the playlist session isn't actively shaping the queue.
SESSION_NOTES = {
    "waiting": "Start playing something in Spotify to begin",
    "user_paused": "Spotify is paused - press play to continue",
    "user_took_over": "You started other music - click New session to start again",
    "not_premium": "This account doesn't have Spotify Premium",
}


def tile_qss(color: str) -> str:
    c = QColor(color)
    rgb = f"{c.red()}, {c.green()}, {c.blue()}"
    return f"""
QPushButton {{
    color: {TEXT};
    text-align: left;
    padding: 12px 14px;
    background: rgba(255, 255, 255, 18);
    border: 1px solid rgba(255, 255, 255, 23);
    border-radius: 14px;
    font-size: 13px;
}}
QPushButton:hover {{ background: rgba(255, 255, 255, 33); }}
QPushButton:checked {{
    background: rgba({rgb}, 224);
    border: 1px solid rgba({rgb}, 255);
    color: white;
    font-weight: 600;
}}
"""


class MoodTile(QPushButton):
    def __init__(self, key: str, mood: Mapping[str, Any], parent: QWidget | None = None):
        super().__init__(f"{mood.get('glyph', '•')}\n{mood.get('label', key)}", parent)
        self.key = key
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(72)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(f"valence {mood['valence']:.2f} · energy {mood['energy']:.2f}")
        self.setStyleSheet(tile_qss(mood.get("color", "#5B8DEF")))


class MoodMap(QWidget):
    """Valence (x) vs energy (y) with the current mood, the target and the gap."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.mood: tuple[float, float] | None = None
        self.target: tuple[float, float] | None = None
        self.color = QColor("#5B8DEF")

    def set_state(self, mood, target, color: str) -> None:
        self.mood = tuple(mood) if mood else None
        self.target = tuple(target) if target else None
        self.color = QColor(color)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height()) - 22
        area = QRectF((self.width() - side) / 2 + 8, 4, side, side)

        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(QColor(255, 255, 255, 12))
        p.drawRoundedRect(area, 8, 8)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        for k in range(1, 4):
            x = area.left() + area.width() * k / 4
            y = area.top() + area.height() * k / 4
            p.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
            p.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))

        p.setPen(QColor(255, 255, 255, 140))
        f = QFont(self.font())
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.drawText(QRectF(area.left(), area.bottom() + 2, area.width(), 14),
                   Qt.AlignmentFlag.AlignCenter, "valence →")
        p.save()
        p.translate(area.left() - 4, area.center().y())
        p.rotate(-90)
        p.drawText(QRectF(-area.height() / 2, -12, area.height(), 12), Qt.AlignmentFlag.AlignCenter, "energy →")
        p.restore()

        def to_px(pt):
            return QPointF(area.left() + pt[0] * area.width(), area.bottom() - pt[1] * area.height())

        if self.target:
            t = to_px(self.target)
            glow = QColor(self.color)
            glow.setAlpha(60)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(t, 14, 14)
            p.setBrush(self.color)
            p.drawEllipse(t, 6, 6)
        if self.mood:
            m = to_px(self.mood)
            if self.target:
                p.setPen(QPen(QColor(255, 255, 255, 120), 1.2, Qt.PenStyle.DashLine))
                p.drawLine(m, to_px(self.target))
            p.setPen(QPen(QColor(20, 20, 24), 1.5))
            p.setBrush(QColor("white"))
            p.drawEllipse(m, 5, 5)


class Card(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


class SidebarPanel(QWidget):
    moodSelected = pyqtSignal(str)
    sessionStartRequested = pyqtSignal()
    sessionEndRequested = pyqtSignal()
    sessionNewRequested = pyqtSignal()
    quitRequested = pyqtSignal()
    logoutRequested = pyqtSignal()

    def __init__(self, moods: Mapping[str, Mapping[str, Any]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("SidebarPanel")
        self.setStyleSheet(BASE_QSS)
        self.moods = moods

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(2)
        title = QLabel("Ambient Mood")
        title.setObjectName("Title")
        self.subtitle = QLabel("Pick where you want to end up")
        self.subtitle.setObjectName("Subtitle")
        heading.addWidget(title)
        heading.addWidget(self.subtitle)
        header.addLayout(heading, 1)

        # Top-right corner: the Spotify account in use, and Log out.
        account_box = QVBoxLayout()
        account_box.setSpacing(2)
        self.account_label = QLabel("")
        self.account_label.setObjectName("Account")
        self.account_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.logout = QPushButton("Log out")
        self.logout.setObjectName("Link")
        self.logout.setCursor(Qt.CursorShape.PointingHandCursor)
        self.logout.clicked.connect(self.logoutRequested.emit)
        account_box.addWidget(self.account_label, 0, Qt.AlignmentFlag.AlignRight)
        account_box.addWidget(self.logout, 0, Qt.AlignmentFlag.AlignRight)
        header.addLayout(account_box)
        root.addLayout(header)
        self.set_account(None)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.tiles: dict[str, MoodTile] = {}
        for i, (key, mood) in enumerate(moods.items()):
            tile = MoodTile(key, mood)
            tile.clicked.connect(lambda _checked, k=key: self.moodSelected.emit(k))
            self.group.addButton(tile)
            self.tiles[key] = tile
            grid.addWidget(tile, i // 2, i % 2)
        root.addLayout(grid)

        map_card = Card()
        mc = QVBoxLayout(map_card)
        mc.setContentsMargins(10, 10, 10, 6)
        self.map = MoodMap()
        mc.addWidget(self.map)
        root.addWidget(map_card)

        status_card = Card()
        sc = QVBoxLayout(status_card)
        sc.setContentsMargins(14, 10, 14, 10)
        sc.setSpacing(4)
        self.now_playing = QLabel("Nothing playing")
        self.now_playing.setObjectName("Status")
        self.now_playing.setWordWrap(True)
        self.details = QLabel("Waiting for the daemon…")
        self.details.setObjectName("Muted")
        self.details.setWordWrap(True)
        sc.addWidget(self.now_playing)
        sc.addWidget(self.details)
        root.addWidget(status_card)

        # Session buttons: "Start session" when none runs; "End session" and "New session" during one.
        footer = QHBoxLayout()
        self.start_session = QPushButton("Start session")
        self.end_session = QPushButton("End session")
        self.new_session = QPushButton("New session")
        for button, signal in ((self.start_session, self.sessionStartRequested),
                               (self.end_session, self.sessionEndRequested),
                               (self.new_session, self.sessionNewRequested)):
            button.setObjectName("Pill")
            button.clicked.connect(signal.emit)
            footer.addWidget(button)
        self.end_session.setToolTip("End the session: songs go back to your queue, the session playlist "
                                    "and the song pool are deleted")
        self.new_session.setToolTip("End this session and start a new one with a fresh song pool")
        quit_btn = QPushButton("Quit")
        quit_btn.setObjectName("Pill")
        quit_btn.clicked.connect(self.quitRequested.emit)
        footer.addStretch(1)
        footer.addWidget(quit_btn)
        root.addLayout(footer)
        self.set_session(False)

    def set_account(self, account) -> None:
        """Show the Spotify account in use (an Account or None)."""
        if account is None:
            self.account_label.setText("")
            self.account_label.setToolTip("")
            self.logout.hide()
            return
        name = account.display_name if len(account.display_name) <= 18 else account.display_name[:17] + "…"
        self.account_label.setText(f"<span style='color:#1DB954'>●</span> {name}")
        self.account_label.setToolTip(f"Spotify account: {account.display_name} (Premium)")
        self.logout.show()

    def set_selected(self, key: str) -> None:
        if key in self.tiles and not self.tiles[key].isChecked():
            self.tiles[key].setChecked(True)

    def set_session(self, active: bool) -> None:
        """Show "Start session" when no session runs, "End session" + "New session" during one."""
        self.start_session.setVisible(not active)
        self.end_session.setVisible(active)
        self.new_session.setVisible(active)

    def show_state(self, state: Mapping[str, Any] | None, target_key: str) -> None:
        mood_cfg = self.moods.get(target_key, {})
        target = (mood_cfg.get("valence"), mood_cfg.get("energy")) if mood_cfg else None
        mood = state.get("mood") if state else None
        self.map.set_state(mood, target, mood_cfg.get("color", "#5B8DEF"))
        if not state:
            self.now_playing.setText("Nothing playing")
            self.details.setText("Daemon not running")
            return
        self.now_playing.setText(state.get("track") or "Nothing playing")
        if state.get("paused"):
            self.details.setText("No session - click Start session to begin")
            return
        session = state.get("session")
        if session and session.get("status") in SESSION_NOTES:
            self.details.setText(SESSION_NOTES[session["status"]])
            return
        rec = state.get("recommender")
        if session and session.get("preparing"):
            ready = f" ({rec['useful']}/{rec['ready_songs']} ready)" if rec else ""
            langs = f" in {', '.join(rec['languages'])}" if rec and rec.get("languages") else ""
            self.details.setText(f"Preparing songs for you{langs}{ready} - nothing is added until then")
            return
        parts = []
        if state.get("deviation") is not None:
            parts.append(f"{state['deviation']:.2f} from target")
        if state.get("expected_steps"):
            parts.append(f"~{state['expected_steps']:.0f} songs to go")
        if state.get("kelvin"):
            parts.append(f"{state['kelvin']:.0f}K")
        if session and session.get("status") == "managing":
            parts.append(f"{session.get('queue_size', 0)} songs ahead")
            changes = []
            if session.get("last_added"):
                changes.append(f"+{len(session['last_added'])}")
            if session.get("last_removed"):
                changes.append(f"-{len(session['last_removed'])}")
            if changes:
                parts.append(" ".join(changes) + " last update")
        elif state.get("last_bridge"):
            parts.append(f"queued: {state['last_bridge']}")
        self.details.setText(" · ".join(parts) or "Listening…")
