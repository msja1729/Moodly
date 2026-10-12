"""Sidebar widgets: a tall frosted-glass panel whose colour follows the user's mood.

After the reference design (Glass design UI.jpg). Top to bottom:

    ● ● ●                 window dots (red hides the sidebar)
    Ambient Mood          title, then a fading divider
    NOW PLAYING           song, artist, a live progress bar
    [mood map]            current mood (white) -> target (tinted), status line
    MOODS                 rows: glyph + label. The target is a lighter glass pill with a
                          white bar on the left edge and a chevron; a dot marks the mood
                          closest to how the music feels now
    SESSION               Start session, or End / New session
    (avatar) name  ⇥ ⏻    the Spotify account in use, Log out, Quit

The glass itself (tint, dark diagonal band, border) is painted by the window
(frontend.app.GlassWindow) in `mood_color(current mood)`, which glides as the
mood moves.
"""
from __future__ import annotations

import math
import time
from typing import Any, Mapping

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPen, QRadialGradient
from PyQt6.QtWidgets import (QAbstractButton, QButtonGroup, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout,
                             QWidget)

TEXT = "#F7F2F4"
MUTED = "rgba(247, 242, 244, 150)"

BASE_QSS = f"""
QWidget#SidebarPanel {{ background: transparent; }}
QLabel {{ color: {TEXT}; background: transparent; }}
QLabel#Title {{ font-size: 25px; font-weight: 700; }}
QLabel#Section {{ color: rgba(247, 242, 244, 175); font-size: 10px; font-weight: 600; }}
QLabel#Song {{ font-size: 13px; font-weight: 600; }}
QLabel#Muted {{ color: {MUTED}; font-size: 11px; }}
QLabel#Account {{ font-size: 12px; font-weight: 600; }}
"""

# What the sidebar says when the playlist session isn't actively shaping the queue.
SESSION_NOTES = {
    "waiting": "Start playing something in Spotify to begin",
    "user_paused": "Spotify is paused - press play to continue",
    "user_took_over": "You started other music - click New session to start again",
    "not_premium": "This account doesn't have Spotify Premium",
}

# Hue (degrees) at the four corners of the mood space, by direction from its centre:
# happy+energetic = amber, tense = rose, sad+calm = violet, content+calm = teal.
_HUE_ANCHORS = [(45.0, 36.0), (135.0, 345.0), (225.0, 265.0), (315.0, 168.0)]
DEFAULT_TINT = QColor("#C9566E")  # rose, like the reference design


def _lerp_hue(a: float, b: float, t: float) -> float:
    d = (b - a + 540) % 360 - 180  # shortest way round the colour wheel
    return (a + d * t) % 360


def mood_color(point) -> QColor:
    """The glass tint for a mood point (valence, energy): hue by direction, richness by distance from neutral."""
    if not point:
        return QColor(DEFAULT_TINT)
    dv, de = point[0] - 0.5, point[1] - 0.5
    # Corners are 90 degrees apart starting at 45: find the two around this direction and blend.
    offset = (math.degrees(math.atan2(de, dv)) - 45.0) % 360
    i = int(offset // 90)
    hue = _lerp_hue(_HUE_ANCHORS[i][1], _HUE_ANCHORS[(i + 1) % 4][1], (offset - 90 * i) / 90)
    strength = min(1.0, math.hypot(dv, de) / 0.5)
    c = QColor.fromHsvF(hue / 360, 0.42 + 0.38 * strength, 0.66)
    return QColor(c.red(), c.green(), c.blue())  # plain 8-bit RGB, so equal colours compare equal


def blend(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t), round(a.alpha() + (b.alpha() - a.alpha()) * t))


# --------------------------------------------------------------------------- small pieces

class WindowDots(QWidget):
    """The three window dots; the red one hides the sidebar."""
    closeRequested = pyqtSignal()
    COLORS = ("#FF5F57", "#FEBC2E", "#28C840")

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(54, 14)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Hide")

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        for i, c in enumerate(self.COLORS):
            p.setBrush(QColor(c))
            p.drawEllipse(QPointF(6 + i * 18, 7), 5, 5)

    def mousePressEvent(self, event) -> None:
        if event.position().x() < 14:
            self.closeRequested.emit()


class Divider(QWidget):
    """A thin line that fades out at both ends."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(9)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        g = QLinearGradient(0, 0, self.width(), 0)
        g.setColorAt(0.0, QColor(255, 255, 255, 0))
        g.setColorAt(0.5, QColor(255, 255, 255, 80))
        g.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(QPen(g, 1))
        p.drawLine(0, 4, self.width(), 4)


def section_label(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("Section")
    f = label.font()
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4)
    label.setFont(f)
    return label


class ProgressBar(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(4)
        self.fraction = 0.0

    def set_fraction(self, f: float) -> None:
        f = min(max(f, 0.0), 1.0)
        if abs(f - self.fraction) > 1e-4:
            self.fraction = f
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        p.setBrush(QColor(255, 255, 255, 38))
        p.drawRoundedRect(r, 2, 2)
        if self.fraction > 0:
            p.setBrush(QColor(255, 255, 255, 215))
            p.drawRoundedRect(QRectF(r.left(), r.top(), r.width() * self.fraction, r.height()), 2, 2)


class GlassRow(QAbstractButton):
    """A menu row: glyph + label. Checked = lighter glass pill, a white bar on the panel edge, a chevron."""

    INSET = 14  # the pill starts this far from the panel edge; the bar sits in this gap

    def __init__(self, glyph: str, text: str, checkable: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.glyph = glyph
        self.setText(text)
        self.setCheckable(checkable)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(35)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.marked = False  # a small dot at the right
        self.accent = QColor(DEFAULT_TINT)
        self._hover = False

    def sizeHint(self) -> QSize:
        return QSize(200, 35)

    def set_marked(self, marked: bool) -> None:
        if marked != self.marked:
            self.marked = marked
            self.update()

    def set_accent(self, color: QColor) -> None:
        self.accent = QColor(color)
        self.update()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pill = QRectF(self.INSET, 2, self.width() - 2 * self.INSET, self.height() - 4)
        on = self.isChecked()
        if on or self._hover:
            g = QLinearGradient(pill.topLeft(), pill.topRight())
            a = QColor(255, 255, 255, 46 if on else 20)
            b = blend(QColor(255, 255, 255, 18 if on else 8), self.accent, 0.25)
            g.setColorAt(0, a)
            g.setColorAt(1, b)
            p.setPen(QPen(QColor(255, 255, 255, 40 if on else 18), 1))
            p.setBrush(g)
            p.drawRoundedRect(pill, 11, 11)
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawRoundedRect(QRectF(0, pill.top() + 6, 3, pill.height() - 12), 1.5, 1.5)

        text = QColor(255, 255, 255, 250 if on else 185)
        p.setPen(text)
        f = QFont(self.font())
        f.setPointSizeF(12.5)
        p.setFont(f)
        p.drawText(QRectF(pill.left() + 10, pill.top(), 24, pill.height()), Qt.AlignmentFlag.AlignCenter, self.glyph)
        f.setPointSizeF(10.5)
        f.setWeight(QFont.Weight.DemiBold if on else QFont.Weight.Normal)
        p.setFont(f)
        p.drawText(QRectF(pill.left() + 44, pill.top(), pill.width() - 80, pill.height()),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        right = pill.right() - 18
        if on:
            p.setPen(QPen(QColor(255, 255, 255, 230), 1.6))
            p.drawLine(QPointF(right - 2, pill.center().y() - 4), QPointF(right + 2, pill.center().y()))
            p.drawLine(QPointF(right + 2, pill.center().y()), QPointF(right - 2, pill.center().y() + 4))
            right -= 16
        if self.marked:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 235))
            p.drawEllipse(QPointF(right, pill.center().y()), 3.5, 3.5)


class MoodTile(GlassRow):
    """A target mood (a checkable row)."""

    def __init__(self, key: str, mood: Mapping[str, Any], parent: QWidget | None = None):
        super().__init__(mood.get("glyph", "•"), mood.get("label", key), checkable=True, parent=parent)
        self.key = key
        self.point = (mood["valence"], mood["energy"])
        self.setToolTip(f"valence {mood['valence']:.2f} · energy {mood['energy']:.2f}")


class MoodMap(QWidget):
    """Valence (x) vs energy (y): the current mood (white), the target (tinted) and the gap."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(112)
        self.mood: tuple[float, float] | None = None
        self.target: tuple[float, float] | None = None
        self.color = QColor(DEFAULT_TINT)

    def set_state(self, mood, target, color) -> None:
        self.mood = tuple(mood) if mood else None
        self.target = tuple(target) if target else None
        self.color = QColor(color)
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(1, 1, -1, -14)
        p.setPen(QPen(QColor(255, 255, 255, 46), 1))
        p.setBrush(QColor(255, 255, 255, 16))
        p.drawRoundedRect(area, 12, 12)
        p.setPen(QPen(QColor(255, 255, 255, 20), 1))
        for k in range(1, 4):
            x = area.left() + area.width() * k / 4
            y = area.top() + area.height() * k / 4
            p.drawLine(QPointF(x, area.top() + 4), QPointF(x, area.bottom() - 4))
            p.drawLine(QPointF(area.left() + 4, y), QPointF(area.right() - 4, y))
        p.setPen(QColor(255, 255, 255, 120))
        f = QFont(self.font())
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.drawText(QRectF(area.left(), area.bottom() + 1, area.width(), 13),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "sad")
        p.drawText(QRectF(area.left(), area.bottom() + 1, area.width(), 13),
                   Qt.AlignmentFlag.AlignCenter, "valence →   ·   energy ↑")
        p.drawText(QRectF(area.left(), area.bottom() + 1, area.width(), 13),
                   Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, "happy")

        def to_px(pt):
            m = 10
            return QPointF(area.left() + m + pt[0] * (area.width() - 2 * m),
                           area.bottom() - m - pt[1] * (area.height() - 2 * m))

        if self.target:
            t = to_px(self.target)
            glow = QRadialGradient(t, 18)
            c = QColor(self.color)
            c.setAlpha(150)
            glow.setColorAt(0, c)
            glow.setColorAt(1, QColor(c.red(), c.green(), c.blue(), 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(t, 18, 18)
            p.setPen(QPen(QColor(255, 255, 255, 200), 1.4))
            p.setBrush(self.color)
            p.drawEllipse(t, 5.5, 5.5)
        if self.mood:
            m = to_px(self.mood)
            if self.target:
                p.setPen(QPen(QColor(255, 255, 255, 110), 1.2, Qt.PenStyle.DashLine))
                p.drawLine(m, to_px(self.target))
            halo = QRadialGradient(m, 12)
            halo.setColorAt(0, QColor(255, 255, 255, 120))
            halo.setColorAt(1, QColor(255, 255, 255, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(halo)
            p.drawEllipse(m, 12, 12)
            p.setBrush(QColor("white"))
            p.drawEllipse(m, 4.5, 4.5)


class IconButton(QAbstractButton):
    """A small round glass button with a glyph (Log out, Quit in the account row)."""

    def __init__(self, glyph: str, tip: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.glyph = glyph
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setFixedSize(30, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = False

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(255, 255, 255, 60), 1))
        p.setBrush(QColor(255, 255, 255, 46 if self._hover else 20))
        p.drawEllipse(QRectF(1, 1, self.width() - 2, self.height() - 2))
        p.setPen(QColor(255, 255, 255, 230))
        f = QFont(self.font())
        f.setPointSizeF(10.5)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.glyph)


class Avatar(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(38, 38)
        self.initial = "?"
        self.accent = QColor(DEFAULT_TINT)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = QLinearGradient(0, 0, self.width(), self.height())
        g.setColorAt(0, QColor(30, 22, 30, 235))
        g.setColorAt(1, blend(QColor(30, 22, 30), self.accent, 0.55))
        p.setPen(QPen(QColor(255, 255, 255, 70), 1))
        p.setBrush(g)
        p.drawEllipse(QRectF(1, 1, self.width() - 2, self.height() - 2))
        p.setPen(QColor(255, 255, 255, 235))
        f = QFont(self.font())
        f.setPointSizeF(12)
        f.setWeight(QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.initial)


# --------------------------------------------------------------------------- the panel

class SidebarPanel(QWidget):
    moodSelected = pyqtSignal(str)
    sessionStartRequested = pyqtSignal()
    sessionEndRequested = pyqtSignal()
    sessionNewRequested = pyqtSignal()
    quitRequested = pyqtSignal()
    logoutRequested = pyqtSignal()
    closeRequested = pyqtSignal()
    tintChanged = pyqtSignal(QColor)

    def __init__(self, moods: Mapping[str, Mapping[str, Any]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("SidebarPanel")
        self.setStyleSheet(BASE_QSS)
        self.moods = moods
        self.tint = QColor(DEFAULT_TINT)
        self._playback: tuple[float, float, bool, float] | None = None  # progress_ms, duration_ms, playing, at

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 16, 0, 16)
        root.setSpacing(0)

        def padded(widget: QWidget, top: int = 0, bottom: int = 0, left: bool = False) -> None:
            box = QHBoxLayout()
            box.setContentsMargins(22, top, 22, bottom)
            box.addWidget(widget, 0, Qt.AlignmentFlag.AlignLeft) if left else box.addWidget(widget)
            if left:
                box.addStretch(1)
            root.addLayout(box)

        self.dots = WindowDots()
        self.dots.closeRequested.connect(self.closeRequested.emit)
        padded(self.dots, 0, 12, left=True)
        title = QLabel("Ambient Mood")
        title.setObjectName("Title")
        padded(title, 0, 6)
        padded(Divider(), 0, 8)

        # Now playing.
        padded(section_label("Now playing"), 0, 6)
        self.now_playing = QLabel("Nothing playing")
        self.now_playing.setObjectName("Song")
        self.artist = QLabel("")
        self.artist.setObjectName("Muted")
        padded(self.now_playing)
        padded(self.artist, 1, 7)
        self.progress = ProgressBar()
        padded(self.progress, 0, 10)
        self.map = MoodMap()
        padded(self.map, 0, 4)
        self.details = QLabel("Waiting for the daemon…")
        self.details.setObjectName("Muted")
        self.details.setWordWrap(True)
        self.details.setMinimumHeight(30)
        self.details.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        padded(self.details, 0, 4)
        padded(Divider(), 0, 4)

        # Target moods.
        padded(section_label("Moods"), 0, 4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.tiles: dict[str, MoodTile] = {}
        for key, mood in moods.items():
            tile = MoodTile(key, mood)
            tile.clicked.connect(lambda _checked=False, k=key: self.moodSelected.emit(k))
            self.group.addButton(tile)
            self.tiles[key] = tile
            root.addWidget(tile)
        root.addSpacing(2)
        padded(Divider(), 0, 4)

        # Session controls and the account.
        padded(section_label("Session"), 0, 4)
        self.start_session = GlassRow("▶", "Start session")
        self.end_session = GlassRow("■", "End session")
        self.new_session = GlassRow("↻", "New session")
        self.logout = IconButton("⇥", "Log out")
        self.quit = IconButton("⏻", "Quit")
        self.start_session.setToolTip("Start shaping your music toward the chosen mood")
        self.end_session.setToolTip("End the session: songs go back to your queue, the session playlist "
                                    "and the song pool are deleted")
        self.new_session.setToolTip("End this session and start a new one with a fresh song pool")
        for row, signal in ((self.start_session, self.sessionStartRequested),
                            (self.end_session, self.sessionEndRequested),
                            (self.new_session, self.sessionNewRequested)):
            row.clicked.connect(signal.emit)
            root.addWidget(row)
        self.logout.clicked.connect(self.logoutRequested.emit)
        self.quit.clicked.connect(self.quitRequested.emit)
        self.rows = [*self.tiles.values(), self.start_session, self.end_session, self.new_session]
        root.addStretch(1)
        padded(Divider(), 2, 8)

        account = QHBoxLayout()
        account.setContentsMargins(22, 0, 22, 0)
        account.setSpacing(12)
        self.avatar = Avatar()
        names = QVBoxLayout()
        names.setSpacing(0)
        self.account_label = QLabel("")
        self.account_label.setObjectName("Account")
        self.account_plan = QLabel("")
        self.account_plan.setObjectName("Muted")
        names.addWidget(self.account_label)
        names.addWidget(self.account_plan)
        account.addWidget(self.avatar)
        account.addLayout(names, 1)
        account.addWidget(self.logout)
        account.addWidget(self.quit)
        root.addLayout(account)

        self.set_session(False)
        self.set_account(None)
        self._clock = QTimer(self)  # moves the progress bar between daemon updates
        self._clock.timeout.connect(self._tick)
        self._clock.start(250)

    # ------------------------------------------------------------- setters
    def set_account(self, account) -> None:
        """Show the Spotify account in use (an Account or None)."""
        if account is None:
            self.account_label.setText("Not signed in")
            self.account_label.setToolTip("")
            self.account_plan.setText("")
            self.avatar.initial = "?"
            self.logout.hide()
        else:
            name = account.display_name if len(account.display_name) <= 22 else account.display_name[:21] + "…"
            self.account_label.setText(name)
            self.account_label.setToolTip(f"Spotify account: {account.display_name}")
            self.account_plan.setText("Spotify Premium")
            self.avatar.initial = (account.display_name[:1] or "?").upper()
            self.logout.show()
        self.avatar.update()

    def set_selected(self, key: str) -> None:
        if key in self.tiles and not self.tiles[key].isChecked():
            self.tiles[key].setChecked(True)

    def set_session(self, active: bool) -> None:
        """Show "Start session" when no session runs, "End session" + "New session" during one."""
        self.start_session.setVisible(not active)
        self.end_session.setVisible(active)
        self.new_session.setVisible(active)

    def set_tint(self, color: QColor) -> None:
        if color != self.tint:
            self.tint = QColor(color)
            for row in self.rows:
                row.set_accent(self.tint)
            self.avatar.accent = self.tint
            self.avatar.update()
            self.tintChanged.emit(self.tint)

    # ------------------------------------------------------------- state from the daemon
    def show_state(self, state: Mapping[str, Any] | None, target_key: str) -> None:
        mood_cfg = self.moods.get(target_key, {})
        target = (mood_cfg.get("valence"), mood_cfg.get("energy")) if mood_cfg else None
        mood = state.get("mood") if state else None
        # The glass shows how the music feels now; before that is known, the chosen target.
        self.set_tint(mood_color(mood or target))
        self.map.set_state(mood, target, mood_color(target).lighter(125))
        nearest = min(self.tiles, key=lambda k: math.dist(self.tiles[k].point, mood)) if mood else None
        for k, tile in self.tiles.items():
            tile.set_marked(k == nearest)
        self._show_track(state)
        if not state:
            self.details.setText("Daemon not running")
            return
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

    def _show_track(self, state) -> None:
        name = (state or {}).get("track_name")
        if name:
            self._set_elided(self.now_playing, name)
            self._set_elided(self.artist, state.get("track_artists") or "")
        else:
            self.now_playing.setText((state or {}).get("track") or "Nothing playing")
            self.artist.setText("")
        if state and state.get("duration_ms"):
            self._playback = (float(state.get("progress_ms") or 0), float(state["duration_ms"]),
                              bool(state.get("is_playing")), float(state.get("observed_at") or time.time()))
        else:
            self._playback = None
        self._tick()

    def _set_elided(self, label: QLabel, text: str) -> None:
        width = max(self.width() - 50, 200)
        label.setText(QFontMetrics(label.font()).elidedText(text, Qt.TextElideMode.ElideRight, width))
        label.setToolTip(text)

    def _tick(self) -> None:
        if self._playback is None:
            self.progress.set_fraction(0.0)
            return
        progress, duration, playing, at = self._playback
        if playing:
            progress += (time.time() - at) * 1000
        self.progress.set_fraction(progress / duration if duration else 0.0)
