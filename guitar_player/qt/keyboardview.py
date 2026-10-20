"""Live keyboard: the physical keys the player is pressing at the playhead.

Fret keys are latched in the game, so the selected fret stays lit until the
next one; string keys flash and fade. Keys the game does not use are drawn
faintly for orientation only.
"""
from __future__ import annotations

from bisect import bisect_right

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..i18n import tr
from ..model import FRET_KEYS, STRING_KEYS, TUNING
from ..visual import VisualScore
from .scoreview import font, note_name
from .theme import contrast, mix

FADE = .35   # seconds a string press stays visible
ROWS = (
    (('`', 1), ('1', 1), ('2', 1), ('3', 1), ('4', 1), ('5', 1), ('6', 1), ('7', 1), ('8', 1), ('9', 1),
     ('0', 1), ('-', 1), ('=', 1), ('', 2)),
    (('', 1.5), ('q', 1), ('w', 1), ('e', 1), ('r', 1), ('t', 1), ('y', 1), ('u', 1), ('i', 1), ('o', 1),
     ('p', 1), ('[', 1), (']', 1), ('\\', 1.5)),
    (('', 1.75), ('a', 1), ('s', 1), ('d', 1), ('f', 1), ('g', 1), ('h', 1), ('j', 1), ('k', 1), ('l', 1),
     (';', 1), ("'", 1), ('', 2.25)),
    (('', 2.25), ('z', 1), ('x', 1), ('c', 1), ('v', 1), ('b', 1), ('n', 1), ('m', 1), (',', 1), ('.', 1),
     ('/', 1), ('', 2.75)),
)
WIDTH_UNITS = 15
IDLE_TINT = .14
_peaks = {}


def peak_tint(p):
    """Strongest lane-colour tint of a pressed string key that keeps the key's
    normal text readable (4.5:1 letter, 3:1 note name) for every string.
    The text colour then never changes, so it cannot lag behind the key."""
    if p.name not in _peaks:
        steps = [t/100 for t in range(int(IDLE_TINT*100), 81)]
        _peaks[p.name] = max(t for t in steps if all(
            contrast(p.text, mix(p.surface, c, t)) >= 4.5 and contrast(p.text2, mix(p.surface, c, t)) >= 3
            for c in p.notes))
    return _peaks[p.name]


class KeyboardView(QWidget):
    def __init__(self, palette, parent=None):
        super().__init__(parent)
        self.p = palette
        self.score = VisualScore()
        self.tuning = TUNING
        self.position = 0.
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_palette(self, palette):
        self.p = palette
        self.update()

    def set_score(self, score, tuning=TUNING):
        self.score, self.tuning = score, tuning
        self.update()

    def set_position(self, position):
        self.position = position
        self.update()

    def _state(self):
        """Latched fret, its age, and recent string presses with their age."""
        notes, t = self.score.notes, self.position
        i = bisect_right(self.score.starts, t+1e-6)-1
        fret = fret_age = None
        strings = {}
        j = i
        while j >= 0:
            n = notes[j]
            if fret is None and n.fret is not None:
                fret, fret_age = n.fret, t-n.start
            if t-n.start <= FADE and n.fret is not None:
                strings.setdefault(n.lane, t-n.start)
            elif fret is not None:
                break
            j -= 1
        return fret, fret_age, strings

    def paintEvent(self, _):
        p = self.p
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        area = QRectF(0, 0, self.width(), self.height())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(p.sunken))
        painter.drawRoundedRect(area, 8, 8)
        unit = min((area.width()-48)/WIDTH_UNITS, (area.height()-40)/len(ROWS)/1.2)
        # Taller than wide when there is room: the letter and its fret/note label both fit.
        row_h = min(unit*1.35, (area.height()-40)/len(ROWS))
        gap = max(4., unit*.09)
        left = (area.width()-unit*WIDTH_UNITS)/2
        top = (area.height()-row_h*len(ROWS))/2
        fret, fret_age, strings = self._state() if self.score.notes else (None, None, {})
        fret_of = {key: i for i, key in enumerate(FRET_KEYS)}
        lane_of = {key: i for i, key in enumerate(STRING_KEYS)}
        for r, row in enumerate(ROWS):
            x = left
            for key, width in row:
                rect = QRectF(x+gap/2, top+r*row_h+gap/2, width*unit-gap, row_h-gap)
                x += width*unit
                if key in lane_of:
                    lane = lane_of[key]
                    self._string_key(painter, rect, key, lane, strings.get(lane))
                elif key in fret_of:
                    age = fret_age if fret_of[key] == fret else None
                    self._fret_key(painter, rect, key, fret_of[key], age)
                else:
                    self._idle_key(painter, rect, key)

    def _face(self, painter, rect, fill, border, glow=None, glow_strength=1.):
        radius = rect.height()*.16
        if glow is not None and glow_strength > 0:
            halo = QColor(glow)
            for spread, alpha in ((7, .10), (4, .18)):
                halo.setAlphaF(alpha*glow_strength)
                painter.setPen(Qt.NoPen)
                painter.setBrush(halo)
                painter.drawRoundedRect(rect.adjusted(-spread, -spread, spread, spread), radius+spread, radius+spread)
        edge = QColor(mix(fill, '#000000', .18))
        painter.setPen(Qt.NoPen)
        painter.setBrush(edge)
        painter.drawRoundedRect(rect, radius, radius)
        painter.setBrush(QColor(fill))
        painter.setPen(QPen(QColor(border), 1))
        painter.drawRoundedRect(rect.adjusted(0, 0, 0, -rect.height()*.07), radius, radius)

    def _labels(self, painter, rect, key, sub, colour, sub_colour):
        face = rect.adjusted(0, 0, 0, -rect.height()*.07)
        h = face.height()
        letter = QRectF(face.left(), face.top()+h*.08, face.width(), h*.52)
        painter.setPen(QColor(colour))
        painter.setFont(font(max(9., min(rect.width(), h)*.22), QFont.DemiBold))
        painter.drawText(letter if sub else face, Qt.AlignCenter, key.upper())
        if sub:
            painter.setPen(QColor(sub_colour))
            size = max(7.5, min(rect.width(), h)*.12)
            # Shrink a long "fret 7" in other languages to fit the key face.
            wide = QFontMetricsF(font(size)).horizontalAdvance(sub)
            painter.setFont(font(size*min(1., face.width()*.88/wide)))
            painter.drawText(QRectF(face.left(), face.top()+h*.60, face.width(), h*.32), Qt.AlignCenter, sub)

    def _idle_key(self, painter, rect, key):
        p = self.p
        faint = QColor(p.border)
        painter.setPen(QPen(faint, 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, rect.height()*.16, rect.height()*.16)
        if key:
            label = QColor(p.text2)
            label.setAlphaF(.45)
            painter.setPen(label)
            painter.setFont(font(max(8., rect.height()*.18)))
            painter.drawText(rect, Qt.AlignCenter, key.upper())

    def _fret_key(self, painter, rect, key, fret, age):
        p = self.p
        if age is None:
            self._face(painter, rect, p.surface, p.border)
            self._labels(painter, rect, key, tr('view.fret', fret=fret), p.text, p.text2)
            return
        # The latched fret keeps the accent; a fresh selection glows brighter.
        flash = max(0., 1-age/FADE)
        self._face(painter, rect, p.accent, p.accent, glow=p.accent, glow_strength=min(1., .5+.5*flash))
        self._labels(painter, rect, key, tr('view.fret', fret=fret), p.on_accent, p.on_accent)

    def _string_key(self, painter, rect, key, lane, age):
        p = self.p
        colour = p.notes[lane % len(p.notes)]
        sub = note_name(self.tuning[lane])
        strength = 0. if age is None else max(0., 1-age/FADE)
        # Fill, border and glow fade on one curve; the text keeps its colour.
        fill = mix(p.surface, colour, IDLE_TINT+(peak_tint(p)-IDLE_TINT)*strength)
        border = mix(mix(p.border, colour, .5), colour, strength)
        self._face(painter, rect, fill, border, glow=colour, glow_strength=strength)
        if strength:
            # A solid lane-coloured edge keeps the press vivid on dark palettes.
            painter.setBrush(Qt.NoBrush)
            radius = rect.height()*.16
            edge = QColor(colour)
            edge.setAlphaF(strength)
            painter.setPen(QPen(edge, 2))
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -rect.height()*.07-1), radius, radius)
        self._labels(painter, rect, key, sub, p.text, p.text2)
