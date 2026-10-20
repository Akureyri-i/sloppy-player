"""The score card used by both the main window and the listening window:
view switch, notes/keys/keyboard views and the minimap seek bar.
Each window owns its own panel, so their positions never affect each other.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QSizePolicy, QStackedLayout, QVBoxLayout, QWidget

from ..i18n import tr
from ..visual import VisualScore
from .keyboardview import KeyboardView
from .scoreview import KEYS, NOTES, DetailView, Minimap, font
from .widgets import Segmented

KEYBOARD = 'keyboard'
VIEWS = (NOTES, KEYS, KEYBOARD)     # shown as tr('view.<view>')
ORIGINAL, ARRANGED = 'original', 'arranged'


class MarkLegend(QWidget):
    """Key for the shading and change outlines in the notes view."""
    RANGE = (None, 'legend.range')
    MARKS = ((Qt.SolidLine, 'legend.moved'), (Qt.DashLine, 'legend.dropped'))

    def __init__(self, palette):
        super().__init__()
        self.p = palette
        self.items = (self.RANGE,)
        self.setFixedHeight(24)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # Hidden in the keys/keyboard views, it still holds the space, so the view
        # switch keeps the same size and place in every view.
        policy.setRetainSizeWhenHidden(True)
        self.setSizePolicy(policy)

    def set_marks(self, shown):
        self.items = (self.RANGE, *self.MARKS) if shown else (self.RANGE,)
        self.update()

    def set_palette(self, p):
        self.p = p
        self.update()

    def paintEvent(self, _):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(font(9))
        metrics = painter.fontMetrics()
        x = self.width()
        for style, key in reversed(self.items):
            text = tr(key)
            width = metrics.horizontalAdvance(text)
            if x-width-30 < 0:
                break   # narrow window: show only the items that fit
            x -= width
            painter.setPen(QColor(self.p.text2))
            painter.drawText(QRectF(x, 0, width, self.height()), Qt.AlignVCenter, text)
            x -= 30
            ink = QColor(self.p.text2)
            if style is None:
                # Same tint DetailView uses outside the playable range.
                ink = QColor(self.p.text)
                ink.setAlphaF(.06 if not self.p.dark else .09)
                painter.setPen(QPen(QColor(self.p.border), 1))
                painter.setBrush(ink)
                painter.drawRoundedRect(QRectF(x, self.height()/2-6, 24, 12), 3, 3)
            else:
                ink.setAlphaF(.7)
                painter.setPen(QPen(ink, 1.2, style))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(QRectF(x, self.height()/2-4, 24, 8), 3, 3)
            x -= 16


class ScorePanel(QFrame):
    seek = Signal(float)
    view_changed = Signal(str)
    source_changed = Signal(str)

    def __init__(self, palette, show_source=False, parent=None):
        super().__init__(parent, objectName='Card')
        self.p = palette
        self.show_source = show_source
        self.marks = VisualScore()
        self.position = 0.
        self.arrangement = None
        self.original = self.arranged = VisualScore()
        self.seekable = True
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        self.bar = QHBoxLayout()
        self.view_switch = Segmented({view: tr('view.'+view) for view in VIEWS})
        self.source_switch = Segmented({ARRANGED: tr('view.arranged'), ORIGINAL: tr('view.original')})
        self.bar.addWidget(self.view_switch)
        # The legend takes the free width and draws right-aligned.
        self.legend = MarkLegend(palette)
        self.bar.addWidget(self.legend, 1)
        self.bar.addWidget(self.source_switch)
        self.source_switch.setVisible(show_source)
        layout.addLayout(self.bar)
        self.detail = DetailView(palette)
        self.keyboard = KeyboardView(palette)
        self.views = QStackedLayout()
        self.views.addWidget(self.detail)
        self.views.addWidget(self.keyboard)
        layout.addLayout(self.views, 1)
        self.minimap = Minimap(palette)
        layout.addWidget(self.minimap)
        self.transport = QHBoxLayout()
        self.transport.setSpacing(8)
        layout.addLayout(self.transport)

        self.view_switch.changed.connect(self.set_view)
        self.source_switch.changed.connect(self._source_changed)
        self.minimap.seek.connect(self._seek)
        self.minimap.zoom.connect(self._zoom)
        self.minimap.edge.connect(self._edge)
        self.detail.seek.connect(self._seek)
        self.detail.window_changed.connect(self._window_changed)

    # -- state ----------------------------------------------------------
    def set_palette(self, p):
        self.p = p
        for widget in (self.detail, self.keyboard, self.minimap, self.legend):
            widget.set_palette(p)

    def set_arrangement(self, arrangement, original, arranged, marks=None):
        self.arrangement = arrangement
        self.original, self.arranged = original, arranged
        self.marks = marks or VisualScore()
        self._refresh()

    def clear(self):
        self.arrangement = None
        self.original = self.arranged = self.marks = VisualScore()
        self._refresh()

    def view(self):
        return self.view_switch.value()

    def source(self):
        return self.source_switch.value()

    def set_view(self, view):
        if view not in VIEWS:
            view = NOTES
        self.view_switch.set_value(view)
        if view == KEYBOARD:
            self.views.setCurrentWidget(self.keyboard)
        else:
            self.views.setCurrentWidget(self.detail)
            self.detail.set_mode(view)
        self._sync_minimap()
        self._sync_legend()
        self.view_changed.emit(view)

    def set_source(self, source):
        self.source_switch.set_value(source)
        self._refresh()

    def set_position(self, position):
        self.position = position
        self.detail.set_position(position)
        self.keyboard.set_position(position)
        self._sync_minimap()

    # -- internals ------------------------------------------------------
    def _source_changed(self, source):
        self._refresh()
        self.source_changed.emit(source)

    def _refresh(self):
        original = self.show_source and self.source() == ORIGINAL
        notes = self.original if original else self.arranged
        bounds = self.arrangement.config.bounds if self.arrangement else (41, 84)
        tuning = self.arrangement.config.tuning if self.arrangement else None
        self.detail.set_scores(notes, self.arranged, bounds, show_range=bool(notes.notes),
                               marks=None if original else self.marks)
        if tuning:
            self.keyboard.set_score(self.arranged, tuning)
        else:
            self.keyboard.set_score(self.arranged)
        self.minimap.set_score(notes)
        self._sync_minimap()
        self._sync_legend()

    def _sync_legend(self):
        original = self.show_source and self.source() == ORIGINAL
        self.legend.set_marks(bool(self.marks.notes) and not original)
        self.legend.setVisible(self.view() == NOTES and bool(self.arranged.notes or self.original.notes))

    def _window_changed(self, *_):
        self._sync_minimap()

    def _sync_minimap(self):
        window = self.detail.time_window() if self.views.currentWidget() is self.detail else (0., 0.)
        self.minimap.set_position(self.position, window)

    def _zoom(self, factor):
        if self.views.currentWidget() is self.detail:
            self.detail.zoom_by(factor)

    def _edge(self, side, t):
        if self.views.currentWidget() is self.detail:
            self.detail.drag_edge(side, t)

    def _seek(self, position):
        if self.seekable:
            self.seek.emit(position)
