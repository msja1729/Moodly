"""Sidebar widgets: a tall frosted-glass panel whose colour follows the user's mood.

After the reference design (Glass design UI.jpg). Top to bottom:

    Ambient Mood          title, then a fading divider
    NOW PLAYING           [cover] song, artist, play/pause + next buttons;
                          a live progress bar with the time;
                          status line with a small mood map at its side
                          (current mood white -> target tinted)
    MOODS                 rows: glyph + label. The target is a lighter glass pill with a
                          white bar on the left edge and a chevron; a dot marks the mood
                          closest to how the music feels now
    SESSION               Start session, or End / New session
    (avatar) name  ⇥ ⏻    the Spotify account in use, Log out, Quit

The glass itself (tint, dark diagonal band, border) is painted by the window
(frontend.app.GlassWindow) in `mood_color(current mood)`, which glides as the
mood moves.

Pointer effects (they only run while the pointer is over the sidebar):
    rows       the glass pill fades in on hover, a faint light follows the pointer
               inside it, the icon nudges right, a click sends a ripple out
    selection  the target's pill and edge bar slide to a newly chosen mood
    mood map   the current-mood dot pulses gently
(The waves the pointer leaves in the glass, behind all text, are painted by
frontend.app.GlassWindow.)
"""
from __future__ import annotations

import math
import threading
import time
import urllib.request
from typing import Any, Mapping

from PyQt6.QtCore import (QEasingCurve, QObject, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation,
                          pyqtSignal)
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                         QRadialGradient)
from PyQt6.QtWidgets import (QAbstractButton, QButtonGroup, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout,
                             QWidget)

TEXT = "#F7F2F4"
MUTED = "rgba(247, 242, 244, 175)"
# Modern UI fonts, first one found wins: Windows 11, Windows 10, macOS.
FONT_FAMILIES = ["Segoe UI Variable Text", "Segoe UI", ".AppleSystemUIFont", "Helvetica Neue"]


def ui_font(size: float = 10.5, weight: QFont.Weight = QFont.Weight.Medium) -> QFont:
    f = QFont()
    f.setFamilies(FONT_FAMILIES)
    f.setPointSizeF(size)
    f.setWeight(weight)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


BASE_QSS = f"""
QWidget#SidebarPanel {{ background: transparent; }}
QLabel {{ color: {TEXT}; background: transparent; }}
QLabel#Title {{ font-size: 26px; font-weight: 800; }}
QLabel#Section {{ color: rgba(247, 242, 244, 190); font-size: 10px; font-weight: 700; }}
QLabel#Song {{ font-size: 14px; font-weight: 700; }}
QLabel#Muted {{ color: {MUTED}; font-size: 11px; font-weight: 500; }}
QLabel#Account {{ font-size: 12px; font-weight: 700; }}
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


def fade(duration_ms: int, on_value, parent) -> QVariantAnimation:
    """A 0..1 animation (eased) that calls `on_value(t)` on every frame."""
    anim = QVariantAnimation(parent)
    anim.setDuration(duration_ms)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.valueChanged.connect(on_value)
    return anim


def blend(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * t), round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t), round(a.alpha() + (b.alpha() - a.alpha()) * t))


# --------------------------------------------------------------------------- small pieces

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
    """A menu row: glyph + label.

    Hover: a glass pill fades in, a soft light follows the pointer inside it, the icon nudges right.
    Click: a ripple of light spreads from the click point. The selected mood's pill and edge bar
    are drawn by the panel underneath (so they can slide between rows); the row adds the bold
    label, the chevron and the dot.
    """

    INSET = 14  # the pill starts this far from the panel edge; the edge bar sits in this gap

    def __init__(self, glyph: str, text: str, checkable: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.glyph = glyph
        self.setText(text)
        self.setCheckable(checkable)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setFixedHeight(35)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.marked = False  # a small dot at the right
        self.accent = QColor(DEFAULT_TINT)
        self.hover = 0.0     # 0..1, animated
        self.mouse: QPointF | None = None
        self.ripple = 1.0    # 0..1 while a ripple runs (1 = none)
        self.ripple_at = QPointF()
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(200)
        self._hover_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hover_anim.valueChanged.connect(self._set_hover)
        self._ripple_anim = fade(480, self._set_ripple, self)

    def sizeHint(self) -> QSize:
        return QSize(200, 35)

    def pill(self) -> QRectF:
        return QRectF(self.INSET, 2, self.width() - 2 * self.INSET, self.height() - 4)

    def set_marked(self, marked: bool) -> None:
        if marked != self.marked:
            self.marked = marked
            self.update()

    def set_accent(self, color: QColor) -> None:
        self.accent = QColor(color)
        self.update()

    # ------------------------------------------------------------- pointer effects
    def _set_hover(self, value) -> None:
        self.hover = float(value)
        self.update()

    def _set_ripple(self, value) -> None:
        self.ripple = float(value)
        self.update()

    def _animate_hover(self, to: float) -> None:
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self.hover)
        self._hover_anim.setEndValue(to)
        self._hover_anim.start()

    def enterEvent(self, event) -> None:
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.mouse = None
        self._animate_hover(0.0)
        super().leaveEvent(event)

    def mouseMoveEvent(self, event) -> None:
        self.mouse = event.position()
        self.update()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:
        self.ripple_at = event.position()
        self._ripple_anim.stop()
        self._ripple_anim.start()
        super().mousePressEvent(event)

    # ------------------------------------------------------------- painting
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pill = self.pill()
        on = self.isChecked()
        h = self.hover
        if h > 0.01 and not on:  # the hover pill (the selected one is drawn by the panel)
            g = QLinearGradient(pill.topLeft(), pill.topRight())
            g.setColorAt(0, QColor(255, 255, 255, round(24 * h)))
            g.setColorAt(1, blend(QColor(255, 255, 255, round(8 * h)), self.accent, 0.25))
            p.setPen(QPen(QColor(255, 255, 255, round(22 * h)), 1))
            p.setBrush(g)
            p.drawRoundedRect(pill, 11, 11)
        if h > 0.01 and self.mouse is not None:  # a soft light that follows the pointer inside the pill
            light = QRadialGradient(self.mouse, pill.height() * 2.2)
            light.setColorAt(0, QColor(255, 255, 255, round(26 * h)))
            light.setColorAt(1, QColor(255, 255, 255, 0))
            p.save()
            clip = QPainterPath()
            clip.addRoundedRect(pill, 11, 11)
            p.setClipPath(clip)
            p.fillRect(pill, light)
            p.restore()
        if self.ripple < 1.0:  # a ring of light spreading from the click
            p.save()
            clip = QPainterPath()
            clip.addRoundedRect(pill, 11, 11)
            p.setClipPath(clip)
            radius = pill.width() * 1.1 * self.ripple
            ring = QRadialGradient(self.ripple_at, max(radius, 1.0))
            alpha = round(110 * (1 - self.ripple))
            ring.setColorAt(0.0, QColor(255, 255, 255, alpha // 3))
            ring.setColorAt(0.85, QColor(255, 255, 255, alpha))
            ring.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(ring)
            p.drawEllipse(self.ripple_at, radius, radius)
            p.restore()

        nudge = 3.0 * h
        p.setPen(QColor(255, 255, 255, 250 if on else round(185 + 50 * h)))
        p.setFont(ui_font(12.5, QFont.Weight.Medium))
        p.drawText(QRectF(pill.left() + 10 + nudge, pill.top(), 24, pill.height()), Qt.AlignmentFlag.AlignCenter,
                   self.glyph)
        p.setFont(ui_font(10.5, QFont.Weight.Bold if on else QFont.Weight.DemiBold))
        p.drawText(QRectF(pill.left() + 44 + nudge * 0.5, pill.top(), pill.width() - 80, pill.height()),
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
    """Valence (x) vs energy (y): the current mood (white, gently pulsing), the target (tinted), the gap."""

    MARGIN = 8

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(96, 72)  # small, at the side of the status line
        self.setToolTip("Mood map: valence (sad → happy) across, energy up.\n"
                        "White: how your music feels now. Coloured: the target.")
        self.mood: tuple[float, float] | None = None
        self.target: tuple[float, float] | None = None
        self.color = QColor(DEFAULT_TINT)
        self._pulse = QTimer(self)  # the mood dot breathes while the sidebar is open
        self._pulse.timeout.connect(self.update)

    def set_state(self, mood, target, color) -> None:
        self.mood = tuple(mood) if mood else None
        self.target = tuple(target) if target else None
        self.color = QColor(color)
        self.update()

    def area(self) -> QRectF:
        return QRectF(self.rect()).adjusted(1, 1, -1, -1)

    def to_px(self, pt) -> QPointF:
        a, m = self.area(), self.MARGIN
        return QPointF(a.left() + m + pt[0] * (a.width() - 2 * m), a.bottom() - m - pt[1] * (a.height() - 2 * m))

    def to_point(self, px: QPointF) -> tuple[float, float]:
        a, m = self.area(), self.MARGIN
        v = (px.x() - a.left() - m) / (a.width() - 2 * m)
        e = (a.bottom() - m - px.y()) / (a.height() - 2 * m)
        return (min(max(v, 0.0), 1.0), min(max(e, 0.0), 1.0))

    def showEvent(self, event) -> None:
        self._pulse.start(40)
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._pulse.stop()
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = self.area()
        p.setPen(QPen(QColor(255, 255, 255, 46), 1))
        p.setBrush(QColor(255, 255, 255, 14))
        p.drawRoundedRect(area, 10, 10)
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        x, y = area.center().x(), area.center().y()  # the four quadrants
        p.drawLine(QPointF(x, area.top() + 4), QPointF(x, area.bottom() - 4))
        p.drawLine(QPointF(area.left() + 4, y), QPointF(area.right() - 4, y))

        if self.target:
            t = self.to_px(self.target)
            glow = QRadialGradient(t, 13)
            c = QColor(self.color)
            c.setAlpha(150)
            glow.setColorAt(0, c)
            glow.setColorAt(1, QColor(c.red(), c.green(), c.blue(), 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(t, 13, 13)
            p.setPen(QPen(QColor(255, 255, 255, 200), 1.2))
            p.setBrush(self.color)
            p.drawEllipse(t, 4.5, 4.5)
        if self.mood:
            m = self.to_px(self.mood)
            if self.target:
                p.setPen(QPen(QColor(255, 255, 255, 110), 1.2, Qt.PenStyle.DashLine))
                p.drawLine(m, self.to_px(self.target))
            breath = 0.5 + 0.5 * math.sin(time.monotonic() * 2 * math.pi / 1.8)  # one breath every 1.8 s
            r = 8 + 4 * breath
            halo = QRadialGradient(m, r)
            halo.setColorAt(0, QColor(255, 255, 255, round(90 + 60 * breath)))
            halo.setColorAt(1, QColor(255, 255, 255, 0))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(halo)
            p.drawEllipse(m, r, r)
            p.setBrush(QColor("white"))
            p.drawEllipse(m, 3.5, 3.5)



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
        p.setPen(QColor(255, 255, 255, 235))
        p.setFont(ui_font(10.5, QFont.Weight.DemiBold))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.glyph)


class CoverLoader(QObject):
    """Downloads album covers off the UI thread; keeps the last few in memory."""

    loaded = pyqtSignal(str, QImage)
    _done = pyqtSignal(str, QImage)
    KEEP = 24

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._cache: dict[str, QImage] = {}
        self._pending: set[str] = set()
        self._done.connect(self._store)

    def get(self, url: str) -> QImage | None:
        return self._cache.get(url)

    def fetch(self, url: str) -> None:
        if url in self._cache:
            self.loaded.emit(url, self._cache[url])
            return
        if url in self._pending or not url.startswith(("https://", "http://")):
            return
        self._pending.add(url)

        def run():
            img = QImage()
            try:
                with urllib.request.urlopen(url, timeout=8) as r:
                    img.loadFromData(r.read())
            except Exception:
                pass  # offline: the placeholder stays
            self._done.emit(url, img)  # queued to the UI thread

        threading.Thread(target=run, name="cover", daemon=True).start()

    def _store(self, url: str, img: QImage) -> None:
        self._pending.discard(url)
        if img.isNull():
            return
        self._cache[url] = img
        while len(self._cache) > self.KEEP:
            self._cache.pop(next(iter(self._cache)))
        self.loaded.emit(url, img)


class CoverArt(QWidget):
    """The album cover with rounded corners; cross-fades when the song changes."""

    SIZE = 64

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.image: QImage | None = None
        self.previous: QImage | None = None
        self.mix = 1.0
        self.accent = QColor(DEFAULT_TINT)
        self._fade = fade(320, self._set_mix, self)

    def _scaled(self, img: QImage | None) -> QImage | None:
        if img is None or img.isNull():
            return None
        px = round(self.SIZE * self.devicePixelRatioF())
        out = img.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                         Qt.TransformationMode.SmoothTransformation)
        out.setDevicePixelRatio(self.devicePixelRatioF())
        return out

    def set_image(self, img: QImage | None) -> None:
        img = self._scaled(img)
        if img is None and self.image is None:
            return
        if img is not None and self.image is not None and img.cacheKey() == self.image.cacheKey():
            return
        self.previous, self.image = self.image, img
        self._fade.stop()
        if self.isVisible():
            self.mix = 0.0
            self._fade.start()
        else:
            self.mix = 1.0
        self.update()

    def _set_mix(self, value) -> None:
        self.mix = float(value)
        self.update()

    def _draw(self, p: QPainter, img: QImage | None, opacity: float) -> None:
        if opacity <= 0:
            return
        p.setOpacity(opacity)
        r = QRectF(self.rect())
        if img is None:  # placeholder: tinted glass with a note
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0, QColor(255, 255, 255, 40))
            g.setColorAt(1, blend(QColor(255, 255, 255, 14), self.accent, 0.45))
            p.fillRect(r, g)
            p.setPen(QColor(255, 255, 255, 190))
            p.setFont(ui_font(20, QFont.Weight.DemiBold))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "♪")
        else:
            w, h = img.width() / img.devicePixelRatio(), img.height() / img.devicePixelRatio()
            p.drawImage(QRectF((r.width() - w) / 2, (r.height() - h) / 2, w, h), img)
        p.setOpacity(1.0)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)
        p.setClipPath(path)
        self._draw(p, self.previous, 1.0 - self.mix)
        self._draw(p, self.image, self.mix)
        p.setClipping(False)
        p.setPen(QPen(QColor(255, 255, 255, 70), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)


class MediaButton(QAbstractButton):
    """Round player button with a drawn icon: "play", "pause" or "next". The play/pause one is solid."""

    def __init__(self, kind: str, tip: str, solid: bool = False, size: int = 32, parent: QWidget | None = None):
        super().__init__(parent)
        self.kind = kind
        self.solid = solid
        self.setFixedSize(size, size)
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hover = False

    def set_kind(self, kind: str, tip: str) -> None:
        if kind != self.kind:
            self.kind = kind
            self.setToolTip(tip)
            self.setAccessibleName(tip)
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
        s = self.width()
        shrink = 1.5 if self.isDown() else 0.0  # a little press
        circle = QRectF(1 + shrink, 1 + shrink, s - 2 - 2 * shrink, s - 2 - 2 * shrink)
        enabled = self.isEnabled()
        if self.solid:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, (245 if self._hover else 225) if enabled else 90))
            icon = QColor(34, 24, 32)
        else:
            p.setPen(QPen(QColor(255, 255, 255, 60), 1))
            p.setBrush(QColor(255, 255, 255, 46 if self._hover and enabled else 20))
            icon = QColor(255, 255, 255, 235 if enabled else 110)
        p.drawEllipse(circle)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(icon)
        c, u = circle.center(), circle.width() / 32  # icon unit
        if self.kind == "pause":
            for dx in (-4.2, 1.6):
                p.drawRoundedRect(QRectF(c.x() + dx * u, c.y() - 6 * u, 2.8 * u, 12 * u), 1 * u, 1 * u)
        elif self.kind == "play":
            path = QPainterPath()
            path.moveTo(c.x() - 3.6 * u, c.y() - 6.5 * u)
            path.lineTo(c.x() + 6.4 * u, c.y())
            path.lineTo(c.x() - 3.6 * u, c.y() + 6.5 * u)
            path.closeSubpath()
            p.drawPath(path)
        else:  # next
            path = QPainterPath()
            path.moveTo(c.x() - 5.5 * u, c.y() - 5.5 * u)
            path.lineTo(c.x() + 2.8 * u, c.y())
            path.lineTo(c.x() - 5.5 * u, c.y() + 5.5 * u)
            path.closeSubpath()
            p.drawPath(path)
            p.drawRoundedRect(QRectF(c.x() + 3.4 * u, c.y() - 5.5 * u, 2.4 * u, 11 * u), 1 * u, 1 * u)


def clock_text(ms: float) -> str:
    s = max(0, int(ms // 1000))
    return f"{s // 60}:{s % 60:02d}"


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
        p.setFont(ui_font(12, QFont.Weight.Bold))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.initial)


# --------------------------------------------------------------------------- the panel

class SidebarPanel(QWidget):
    moodSelected = pyqtSignal(str)
    sessionStartRequested = pyqtSignal()
    sessionEndRequested = pyqtSignal()
    sessionNewRequested = pyqtSignal()
    quitRequested = pyqtSignal()
    logoutRequested = pyqtSignal()
    tintChanged = pyqtSignal(QColor)
    playerCommand = pyqtSignal(str)  # "play", "pause" or "next"

    def __init__(self, moods: Mapping[str, Mapping[str, Any]], parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("SidebarPanel")
        self.setFont(ui_font())
        self.setStyleSheet(BASE_QSS)
        self.moods = moods
        self.tint = QColor(DEFAULT_TINT)
        self._playback: tuple[float, float, bool, float] | None = None  # progress_ms, duration_ms, playing, at
        self._pending_play: tuple[bool, float] | None = None  # a play/pause click Spotify hasn't confirmed yet
        self._cover_url: str | None = None
        self.covers = CoverLoader(self)
        self.covers.loaded.connect(self._cover_loaded)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 22, 0, 16)
        root.setSpacing(0)

        def padded(widget: QWidget, top: int = 0, bottom: int = 0, left: bool = False) -> None:
            box = QHBoxLayout()
            box.setContentsMargins(22, top, 22, bottom)
            box.addWidget(widget, 0, Qt.AlignmentFlag.AlignLeft) if left else box.addWidget(widget)
            if left:
                box.addStretch(1)
            root.addLayout(box)

        title = QLabel("Ambient Mood")
        title.setObjectName("Title")
        padded(title, 0, 6)
        padded(Divider(), 0, 8)

        # Now playing.
        padded(section_label("Now playing"), 0, 8)
        self.cover = CoverArt()
        self.now_playing = QLabel("Nothing playing")
        self.now_playing.setObjectName("Song")
        self.artist = QLabel("")
        self.artist.setObjectName("Muted")
        self.play_pause = MediaButton("play", "Play", solid=True, size=32)
        self.next_song = MediaButton("next", "Next song", size=30)
        self.play_pause.clicked.connect(self._toggle_play)
        self.next_song.clicked.connect(lambda: self.playerCommand.emit("next"))
        info = QVBoxLayout()
        info.setSpacing(0)
        info.addWidget(self.now_playing)
        info.addWidget(self.artist)
        info.addSpacing(6)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.play_pause)
        buttons.addWidget(self.next_song)
        buttons.addStretch(1)
        info.addLayout(buttons)
        top = QHBoxLayout()
        top.setContentsMargins(22, 0, 22, 10)
        top.setSpacing(12)
        top.addWidget(self.cover, 0, Qt.AlignmentFlag.AlignTop)
        top.addLayout(info, 1)
        root.addLayout(top)

        self.progress = ProgressBar()
        self.time = QLabel("")
        self.time.setObjectName("Muted")
        self.time.setStyleSheet("font-size: 10px;")
        bar = QHBoxLayout()
        bar.setContentsMargins(22, 0, 22, 10)
        bar.setSpacing(10)
        bar.addWidget(self.progress, 1, Qt.AlignmentFlag.AlignVCenter)
        bar.addWidget(self.time)
        root.addLayout(bar)

        self.map = MoodMap()
        self.details = QLabel("Waiting for the daemon…")
        self.details.setObjectName("Muted")
        self.details.setWordWrap(True)
        self.details.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        status = QHBoxLayout()
        status.setContentsMargins(22, 0, 22, 8)
        status.setSpacing(12)
        status.addWidget(self.details, 1)
        status.addWidget(self.map, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(status)
        padded(Divider(), 0, 4)

        # Target moods.
        padded(section_label("Moods"), 0, 4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.buttonToggled.connect(lambda _b, checked: checked and self._move_selection(animate=True))
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
        # The target's glass pill + edge bar, drawn under the rows so it can slide between them.
        self.selection = QRectF()
        self._slide = QVariantAnimation(self)
        self._slide.setDuration(320)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide.valueChanged.connect(self._set_selection)
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
            self.cover.accent = self.tint
            self.cover.update()
            self.update()  # the selected pill is tinted too
            self.tintChanged.emit(self.tint)

    # ------------------------------------------------------------- the sliding selection
    def _selection_target(self) -> QRectF:
        tile = next((t for t in self.tiles.values() if t.isChecked()), None)
        if tile is None:
            return QRectF()
        return tile.pill().translated(QPointF(tile.geometry().topLeft()))

    def _set_selection(self, rect) -> None:
        self.selection = QRectF(rect)
        self.update()

    def _move_selection(self, animate: bool) -> None:
        target = self._selection_target()
        self._slide.stop()
        if animate and self.isVisible() and not self.selection.isNull() and not target.isNull():
            self._slide.setStartValue(QRectF(self.selection))
            self._slide.setEndValue(target)
            self._slide.start()
        else:
            self._set_selection(target)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        QTimer.singleShot(0, lambda: self._move_selection(animate=False))  # after the layout settles

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, lambda: self._move_selection(animate=False))

    def paintEvent(self, _event) -> None:
        if self.selection.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pill = self.selection
        g = QLinearGradient(pill.topLeft(), pill.topRight())
        g.setColorAt(0, QColor(255, 255, 255, 46))
        g.setColorAt(1, blend(QColor(255, 255, 255, 18), self.tint, 0.25))
        p.setPen(QPen(QColor(255, 255, 255, 40), 1))
        p.setBrush(g)
        p.drawRoundedRect(pill, 11, 11)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(255, 255, 255, 235))
        p.drawRoundedRect(QRectF(0, pill.top() + 6, 3, pill.height() - 12), 1.5, 1.5)

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
            playing = bool(state.get("is_playing"))
            observed = float(state.get("observed_at") or time.time())
            duration = float(state["duration_ms"])
            stale = False
            if self._pending_play is not None:
                clicked = self._pending_play[1]
                # Spotify hasn't caught up with the click yet: keep showing what the click did.
                stale = observed < clicked and time.time() - clicked < 3
                if not stale:
                    self._pending_play = None
            if not (stale and self._playback is not None and self._playback[1] == duration):
                if stale:
                    playing = self._pending_play[0]
                self._playback = (float(state.get("progress_ms") or 0), duration, playing, observed)
        else:
            self._playback = None
        self._show_cover((state or {}).get("track_image") if name else None)
        self._show_buttons()
        self._tick()

    def _show_cover(self, url: str | None) -> None:
        if url == self._cover_url:
            return
        self._cover_url = url
        if not url:
            self.cover.set_image(None)
            return
        cached = self.covers.get(url)
        if cached is not None:
            self.cover.set_image(cached)
        else:
            self.covers.fetch(url)  # the old cover stays until the new one arrives

    def _cover_loaded(self, url: str, img: QImage) -> None:
        if url == self._cover_url:
            self.cover.set_image(img)

    def _show_buttons(self) -> None:
        has_track = self._playback is not None
        playing = has_track and self._playback[2]
        self.play_pause.set_kind("pause" if playing else "play", "Pause" if playing else "Play")
        self.play_pause.setEnabled(has_track)
        self.next_song.setEnabled(has_track)

    def _toggle_play(self) -> None:
        if self._playback is None:
            return
        progress, duration, playing, at = self._playback
        now = time.time()
        if playing:
            progress += (now - at) * 1000  # freeze the bar where it is
        self._playback = (progress, duration, not playing, now)
        self._pending_play = (not playing, now)
        self._show_buttons()
        self._tick()
        self.playerCommand.emit("pause" if playing else "play")

    def _set_elided(self, label: QLabel, text: str) -> None:
        width = max(self.width() - 44 - CoverArt.SIZE - 12, 140)
        label.setText(QFontMetrics(label.font()).elidedText(text, Qt.TextElideMode.ElideRight, width))
        label.setToolTip(text)

    def _tick(self) -> None:
        if self._playback is None:
            self.progress.set_fraction(0.0)
            self.time.setText("")
            return
        progress, duration, playing, at = self._playback
        if playing:
            progress += (time.time() - at) * 1000
        progress = min(progress, duration)
        self.progress.set_fraction(progress / duration if duration else 0.0)
        self.time.setText(f"{clock_text(progress)} / {clock_text(duration)}")
