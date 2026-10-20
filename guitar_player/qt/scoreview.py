"""Score views: a full-song minimap that doubles as the seek bar, and a
zoomed detail view around the playhead (notes, or the keys to be pressed).

The minimap renders once into a cached pixmap per size/palette/score; during
a live resize it stretches the old pixmap and re-renders after the drag
settles. The detail view only paints notes inside its time window.
"""
from __future__ import annotations

from bisect import bisect_right

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..i18n import tr
from ..model import FRET_KEYS, STRING_KEYS
from ..visual import VisualNote, VisualScore
from .theme import font_families, mix, readable_on

NOTES, KEYS = 'notes', 'keys'
MOVED, DROPPED = 0, 1   # lanes of change marks


def change_marks(arrangement):
    """Where the arrangement departs from the score: the original pitch of notes
    moved by octaves, and notes left out. Empty when played as written."""
    speed = arrangement.config.speed
    played = {f.voice.note.id: f.voice.pitch for s in arrangement.strikes for f in s.notes}
    marks = []
    for n in arrangement.selected:
        if n.id in played:
            if played[n.id] != n.pitch:
                marks.append(VisualNote(n.start/speed, n.end/speed, n.pitch, n.velocity, MOVED))
        elif n.id in arrangement.dropped:
            marks.append(VisualNote(n.start/speed, n.end/speed, n.pitch, n.velocity, DROPPED))
    return VisualScore(marks, arrangement.duration)


def font(size, weight=QFont.Normal):
    f = QFont(font_families())
    f.setPointSizeF(size)
    f.setWeight(weight)
    return f


def clock(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds//60}:{seconds%60:02d}'


def note_name(pitch):
    return ('C', 'C♯', 'D', 'D♯', 'E', 'F', 'F♯', 'G', 'G♯', 'A', 'A♯', 'B')[pitch % 12]+str(pitch//12-1)


class Minimap(QWidget):
    """Whole piece at a glance; click or drag to move the playhead.
    Ctrl+wheel or dragging the window box's edges changes the detail range."""
    seek = Signal(float)
    zoom = Signal(float)          # factor: >1 shows less time
    edge = Signal(str, float)     # 'left' / 'right', time the edge was dragged to
    EDGE = 6                      # px around a box edge that grab it

    def __init__(self, palette, parent=None):
        super().__init__(parent)
        self.p = palette
        self.score = VisualScore()
        self.position = 0.
        self.window = (0., 0.)
        self._cache = None
        self._cache_key = None
        self._render_timer = QTimer(self, singleShot=True, interval=120, timeout=self._render)
        self.setMinimumHeight(44)
        self.setFixedHeight(52)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self._dragging_edge = None

    def set_palette(self, palette):
        self.p = palette
        self._cache_key = None
        self._render()

    def set_score(self, score):
        self.score = score
        self._cache_key = None
        self._render()

    def set_position(self, position, window):
        self.position, self.window = position, window
        hint = tr('view.minimap_hint') if window[1] > window[0] else ''
        if self.toolTip() != hint:
            self.setToolTip(hint)
        self.update()

    def _key(self):
        return id(self.score), self.p.name, self.width(), self.height(), self.devicePixelRatioF()

    def _render(self):
        if self.width() < 2 or self.height() < 2:
            return
        ratio = self.devicePixelRatioF()
        pixmap = QPixmap(int(self.width()*ratio), int(self.height()*ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.transparent)
        score, p = self.score, self.p
        if score.notes and score.duration > 0:
            painter = QPainter(pixmap)
            low, high = score.low-1, score.high+1
            w, h = self.width(), self.height()-6
            row = h/(high-low+1)
            colours = [QColor(c) for c in p.notes]
            for c in colours:
                c.setAlphaF(.8)
            painter.setPen(Qt.NoPen)
            scale = w/score.duration
            for n in score.notes:
                painter.setBrush(colours[n.lane % len(colours)])
                x = n.start*scale
                painter.drawRect(QRectF(x, 3+(high-n.pitch)*row, max(1., (n.end-n.start)*scale), max(1., row)))
            painter.end()
        self._cache, self._cache_key = pixmap, self._key()
        self.update()

    def resizeEvent(self, event):
        self._render_timer.start()
        super().resizeEvent(event)

    def paintEvent(self, _):
        p = self.p
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        frame = QRectF(.5, .5, self.width()-1, self.height()-1)
        painter.setPen(QPen(QColor(p.border), 1))
        painter.setBrush(QColor(p.sunken))
        painter.drawRoundedRect(frame, 6, 6)
        if self._cache is not None:
            painter.drawPixmap(self.rect(), self._cache)
        if not self.score.notes or self.score.duration <= 0:
            return
        scale = self.width()/self.score.duration
        left, right = self.window
        if right > left:
            view = QRectF(left*scale, 1, max(6., (right-left)*scale), self.height()-2)
            fill = QColor(p.accent)
            fill.setAlphaF(.12)
            painter.setBrush(fill)
            painter.setPen(QPen(QColor(p.accent), 1.2))
            painter.drawRoundedRect(view, 4, 4)
        x = self.position*scale
        painter.setPen(QPen(QColor(p.accent), 2))
        painter.drawLine(QPointF(x, 1), QPointF(x, self.height()-1))

    def _time(self, x):
        return min(self.score.duration, max(0., x/self.width()*self.score.duration))

    def _seek_to(self, x):
        if self.score.duration > 0:
            self.seek.emit(self._time(x))

    def _edge_at(self, x):
        left, right = self.window
        if right <= left or self.score.duration <= 0:
            return None
        scale = self.width()/self.score.duration
        # Prefer the right edge: the left one is often pinned at 0:00.
        for side, at in (('right', right*scale), ('left', left*scale)):
            if abs(x-at) <= self.EDGE:
                return side
        return None

    def mousePressEvent(self, event):
        self._dragging_edge = self._edge_at(event.position().x())
        if not self._dragging_edge:
            self._seek_to(event.position().x())

    def mouseMoveEvent(self, event):
        x = event.position().x()
        if event.buttons() & Qt.LeftButton:
            if self._dragging_edge:
                self.edge.emit(self._dragging_edge, self._time(x))
            else:
                self._seek_to(x)
            return
        self.setCursor(Qt.SizeHorCursor if self._edge_at(x) else Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        self._dragging_edge = None

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier and self.window[1] > self.window[0]:
            self.zoom.emit(1.2 if event.angleDelta().y() > 0 else 1/1.2)
            event.accept()
        else:
            event.ignore()


class DetailView(QWidget):
    """A few seconds around the playhead: piano-roll notes or per-string keys."""
    seek = Signal(float)
    window_changed = Signal(float, float)

    AXIS = 22        # time labels under the plot
    GUTTER = 44      # pitch / string labels on the left
    LEAD = .25       # playhead sits a quarter of the way in

    def __init__(self, palette, parent=None):
        super().__init__(parent)
        self.p = palette
        self.mode = NOTES
        self.notes_score = VisualScore()
        self.keys_score = VisualScore()
        self.bounds = (41, 84)
        self.show_range = False
        self.marks = VisualScore()
        self.position = 0.
        self.zooms = {NOTES: 1., KEYS: 1.}   # each view keeps its own range
        self._lane_starts = {}
        self._key_pps = 120.
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # -- data -----------------------------------------------------------
    def set_palette(self, palette):
        self.p = palette
        self.update()

    def set_scores(self, notes_score, keys_score, bounds=(41, 84), show_range=False, marks=None):
        self.notes_score, self.keys_score = notes_score, keys_score
        self.bounds, self.show_range = bounds, show_range
        self.marks = marks or VisualScore()
        lanes = {}
        for n in keys_score.notes:
            lanes.setdefault(n.lane, []).append(n.start)
        self._lane_starts = lanes
        # Pick a scale where typical same-string repeats leave room for a key label.
        gaps = sorted(b-a for starts in lanes.values() for a, b in zip(starts, starts[1:]) if b-a > .005)
        quick = gaps[len(gaps)//4] if gaps else .25
        self._key_pps = min(420., max(70., 30/quick))
        self.update()
        self._emit_window()

    def set_mode(self, mode):
        self.mode = mode
        self.update()
        self._emit_window()

    def set_position(self, position):
        self.position = max(0., position)
        self.update()
        self._emit_window()

    # -- geometry -------------------------------------------------------
    def _score(self):
        return self.keys_score if self.mode == KEYS else self.notes_score

    def _plot(self):
        return QRectF(self.GUTTER, 6, max(10., self.width()-self.GUTTER-10), max(10., self.height()-self.AXIS-8))

    MIN_SPAN = 1.

    def _base_span(self):
        """Seconds shown at zoom 1: about 10 s of notes, or keys spaced to fit labels."""
        plot = self._plot()
        return 10. if self.mode != KEYS else plot.width()/self._key_pps

    def _max_span(self):
        return max(2*self.MIN_SPAN, self._score().duration)

    def seconds_visible(self):
        span = self._base_span()/self.zooms[self.mode]
        return min(self._max_span(), max(self.MIN_SPAN, span))

    def set_span(self, span):
        span = min(self._max_span(), max(self.MIN_SPAN, span))
        self.zooms[self.mode] = self._base_span()/span
        self.update()
        self._emit_window()

    def zoom_by(self, factor):
        self.set_span(self.seconds_visible()/factor)

    def drag_edge(self, side, t):
        """Resize the range so the given window edge lands on time t."""
        position, lead = self.position, self.LEAD
        if side == 'right':
            span = (t-position)/(1-lead)
            if position-lead*span <= 0:
                span = t   # window pinned at 0:00
        else:
            if t >= position-.05:
                return
            span = (position-t)/lead
        if span > 0:
            self.set_span(span)

    def time_window(self):
        span = self.seconds_visible()
        left = max(0., self.position-span*self.LEAD)
        return left, left+span

    def _emit_window(self):
        self.window_changed.emit(*self.time_window())

    def _x(self, t, left, span, plot):
        return plot.left()+(t-left)/span*plot.width()

    # -- painting -------------------------------------------------------
    def paintEvent(self, _):
        p = self.p
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        plot = self._plot()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(p.sunken))
        painter.drawRoundedRect(QRectF(plot.left()-4, plot.top()-4, plot.width()+8, plot.height()+8), 8, 8)
        score = self._score()
        if not score.notes:
            painter.setPen(QColor(p.text2))
            painter.setFont(font(10.5))
            text = tr('view.keys_empty') if self.mode == KEYS and self.notes_score.notes else tr('view.empty')
            painter.drawText(plot, Qt.AlignCenter, text)
            return
        left, right = self.time_window()
        span = right-left
        self._draw_time_grid(painter, plot, left, span)
        painter.save()
        painter.setClipRect(plot.adjusted(-2, -2, 2, 2))
        if self.mode == KEYS:
            self._draw_keys(painter, plot, left, span)
        else:
            self._draw_notes(painter, plot, left, span)
        painter.restore()
        if self.mode == KEYS:
            self._draw_string_labels(painter, plot)
        else:
            self._draw_pitch_labels(painter, plot)
        x = self._x(self.position, left, span, plot)
        painter.setPen(QPen(QColor(p.accent), 2))
        painter.drawLine(QPointF(x, plot.top()-4), QPointF(x, plot.bottom()+4))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(p.accent))
        painter.drawPolygon(QPolygonF([QPointF(x-5, plot.top()-6), QPointF(x+5, plot.top()-6), QPointF(x, plot.top())]))

    def _draw_time_grid(self, painter, plot, left, span):
        p = self.p
        step = next((s for s in (.5, 1, 2, 5, 10, 15, 30, 60) if s/span*plot.width() >= 80), 120)
        first = int(left/step+.999)*step
        grid = QColor(p.border)
        painter.setFont(font(8.5))
        t = first
        while t <= left+span:
            x = self._x(t, left, span, plot)
            painter.setPen(QPen(grid, 1))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(QColor(p.text2))
            label = clock(t) if step >= 1 else f'{clock(t)}.{int(t*10) % 10}'
            painter.drawText(QRectF(x-40, plot.bottom()+6, 80, self.AXIS-4), Qt.AlignHCenter | Qt.AlignTop, label)
            t += step

    def _pitch_range(self):
        score = self.notes_score
        low, high = score.low-2, score.high+2
        if self.marks.notes:
            low, high = min(low, self.marks.low-2), max(high, self.marks.high+2)
        if self.show_range:
            low, high = min(low, self.bounds[0]-2), max(high, self.bounds[1]+2)
        if high-low < 24:
            mid = (high+low)//2
            low, high = mid-12, mid+12
        return low, high

    def _draw_notes(self, painter, plot, left, span):
        p = self.p
        low, high = self._pitch_range()
        row = plot.height()/(high-low+1)
        y_of = lambda pitch: plot.bottom()-(pitch-low+1)*row
        if self.show_range:
            # Outside the guitar's range: shaded, so the user sees what must move.
            shade = QColor(p.text)
            shade.setAlphaF(.06 if not p.dark else .09)
            painter.setPen(Qt.NoPen)
            painter.setBrush(shade)
            top_edge, bottom_edge = y_of(self.bounds[1]), y_of(self.bounds[0])+row
            painter.drawRect(QRectF(plot.left(), plot.top(), plot.width(), top_edge-plot.top()))
            painter.drawRect(QRectF(plot.left(), bottom_edge, plot.width(), plot.bottom()-bottom_edge))
        painter.setPen(QPen(QColor(p.border), 1))
        for pitch in range(low, high+1):
            if pitch % 12 == 0:
                y = y_of(pitch)+row
                painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        colours = [QColor(c) for c in p.notes]
        past = [QColor(mix(c, p.sunken, .6)) for c in p.notes]
        height = max(2., min(row*.82, 14.))
        radius = min(3., height/2)
        self._draw_marks(painter, plot, left, span, y_of, row, height, radius)
        painter.setPen(Qt.NoPen)
        for n in self.notes_score.between(left, left+span):
            x0, x1 = self._x(n.start, left, span, plot), self._x(n.end, left, span, plot)
            done = n.end <= self.position
            painter.setBrush((past if done else colours)[n.lane % len(colours)])
            y = y_of(n.pitch)+(row-height)/2
            painter.drawRoundedRect(QRectF(x0, y, max(2., x1-x0-1), height), radius, radius)

    def _draw_marks(self, painter, plot, left, span, y_of, row, height, radius):
        """Hollow outlines behind the notes: solid = original pitch of a moved
        note, dashed = a note that is not played."""
        if not self.marks.notes:
            return
        ink = QColor(self.p.text2)
        ink.setAlphaF(.7)
        painter.setBrush(Qt.NoBrush)
        for n in self.marks.between(left, left+span):
            x0, x1 = self._x(n.start, left, span, plot), self._x(n.end, left, span, plot)
            pen = QPen(ink, 1.2, Qt.DashLine if n.lane == DROPPED else Qt.SolidLine)
            painter.setPen(pen)
            y = y_of(n.pitch)+(row-height)/2
            painter.drawRoundedRect(QRectF(x0+.6, y+.6, max(2., x1-x0-1.2), height-1.2), radius, radius)

    def _draw_pitch_labels(self, painter, plot):
        p = self.p
        low, high = self._pitch_range()
        row = plot.height()/(high-low+1)
        painter.setFont(font(8.5))
        painter.setPen(QColor(p.text2))
        for pitch in range(low, high+1):
            if pitch % 12 == 0:
                y = plot.bottom()-(pitch-low+1)*row+row
                painter.drawText(QRectF(0, y-9, self.GUTTER-8, 18), Qt.AlignRight | Qt.AlignVCenter, note_name(pitch))

    def _lane_y(self, plot, lane):
        step = plot.height()/len(STRING_KEYS)
        return plot.top()+(len(STRING_KEYS)-1-lane)*step, step

    def _draw_keys(self, painter, plot, left, span):
        p = self.p
        painter.setPen(QPen(QColor(p.border), 1))
        for lane in range(len(STRING_KEYS)):
            top, step = self._lane_y(plot, lane)
            painter.drawLine(QPointF(plot.left(), top+step/2), QPointF(plot.right(), top+step/2))
        label_font = font(9, QFont.DemiBold)
        metrics = QFontMetricsF(label_font)
        painter.setFont(label_font)
        for n in self.keys_score.between(left, left+span):
            if n.fret is None:
                continue
            top, step = self._lane_y(plot, n.lane)
            starts = self._lane_starts.get(n.lane, ())
            i = bisect_right(starts, n.start+1e-6)
            limit = self._x(starts[i], left, span, plot)-2 if i < len(starts) else plot.right()
            x0 = self._x(n.start, left, span, plot)
            x1 = max(x0+4, min(self._x(n.end, left, span, plot), limit))
            x1 = max(x1, min(x0+22, limit))
            height = min(step*.66, 26.)
            chip = QRectF(x0, top+(step-height)/2, max(3., x1-x0), height)
            colour = p.notes[n.lane % len(p.notes)]
            done = n.start < self.position-1e-6
            fill = mix(colour, p.sunken, .6) if done else colour
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(fill))
            painter.drawRoundedRect(chip, min(5., height/2), min(5., height/2))
            label = FRET_KEYS[n.fret].upper()
            if chip.width() >= metrics.horizontalAdvance(label)+8:
                painter.setPen(QColor(readable_on(fill, p)))
                painter.drawText(chip, Qt.AlignCenter, label)

    def _draw_string_labels(self, painter, plot):
        p = self.p
        painter.setFont(font(9, QFont.DemiBold))
        for lane, key in enumerate(STRING_KEYS):
            top, step = self._lane_y(plot, lane)
            box = QRectF(self.GUTTER-34, top+step/2-11, 24, 22)
            painter.setPen(QPen(QColor(p.border), 1))
            painter.setBrush(QColor(p.surface))
            painter.drawRoundedRect(box, 5, 5)
            painter.setPen(QColor(p.text))
            painter.drawText(box, Qt.AlignCenter, key.upper())

    # -- input ----------------------------------------------------------
    def mousePressEvent(self, event):
        if not self._score().notes:
            return
        plot = self._plot()
        left, right = self.time_window()
        t = left+(event.position().x()-plot.left())/plot.width()*(right-left)
        self.seek.emit(min(self._score().duration, max(0., t)))

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            self.zoom_by(1.2 if event.angleDelta().y() > 0 else 1/1.2)
            event.accept()
        else:
            event.ignore()
