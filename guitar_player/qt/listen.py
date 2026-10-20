"""Listening window: check an arrangement by ear before playing it in the game.

It keeps its own position, separate from the main window's start point, and
remembers where you stopped for each song while the program is open.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ..i18n import tr
from ..sample_preview import CUSTOM, DEFAULT as DEFAULT_TIMBRE
from .common import ElidedLabel, IconButton, label, tabular_font
from .panel import ARRANGED, KEYBOARD, ORIGINAL, ScorePanel
from .scoreview import KEYS, NOTES, clock
from .widgets import Segmented, app_icon
from .winstyle import tint_title_bar


class ListenWindow(QWidget):
    closed = Signal()
    started = Signal()
    timbre_chosen = Signal(str)       # sample_preview.DEFAULT or CUSTOM

    def __init__(self, audio, palette, parent=None):
        super().__init__(parent, Qt.Window)
        self.setObjectName('Root')
        self.setWindowTitle(tr('listen.title'))
        self.setWindowIcon(app_icon())
        self.resize(940, 600)
        self.setMinimumSize(720, 480)
        self.audio = audio
        self.p = palette
        self.arrangement = None
        self.position = 0.
        self._was_playing = False
        self._resume_after_seek = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        self.title = ElidedLabel('', 'section')
        layout.addWidget(self.title)
        self.panel = ScorePanel(palette, show_source=True)
        transport = self.panel.transport
        self.play_button = IconButton(tr('listen.play'), 'primary', 'play')
        self.restart_button = IconButton(tr('listen.restart'), icon='restart')
        transport.addWidget(self.play_button)
        transport.addWidget(self.restart_button)
        transport.addStretch(1)
        transport.addWidget(label(tr('listen.timbre'), 'secondary'))
        self.timbre = Segmented({DEFAULT_TIMBRE: tr('timbre.default'), CUSTOM: tr('timbre.custom')})
        self.timbre.changed.connect(self.timbre_chosen.emit)
        transport.addWidget(self.timbre)
        transport.addSpacing(12)
        self.message = label('', 'secondary')
        transport.addWidget(self.message)
        transport.addSpacing(12)
        self.time_label = label('0:00 / 0:00', 'secondary')
        self.time_label.setFont(tabular_font())
        transport.addWidget(self.time_label)
        layout.addWidget(self.panel, 1)
        self.buttons = (self.play_button, self.restart_button)

        self.timer = QTimer(self, interval=30, timeout=self._tick)
        self.seek_timer = QTimer(self, singleShot=True, interval=160, timeout=self._resume)
        self.play_button.clicked.connect(self.toggle)
        self.restart_button.clicked.connect(lambda: self.seek(0.))
        self.panel.seek.connect(self.seek)
        self.panel.source_changed.connect(self._source_changed)
        self.apply_palette(palette)

    # -- appearance -----------------------------------------------------
    def apply_palette(self, p):
        self.p = p
        for widget in self.buttons:
            widget.set_palette(p)
        self.panel.set_palette(p)
        tint_title_bar(self, p)

    def showEvent(self, event):
        super().showEvent(event)
        tint_title_bar(self, self.p)

    # -- content --------------------------------------------------------
    def set_arrangement(self, arrangement, original, arranged, marks, title, position=0.):
        self.stop()
        self.arrangement = arrangement
        self.title.set_full_text(title)
        # Played as written: the original and the arrangement sound the same.
        same = not marks.notes
        if same:
            self.panel.source_switch.set_value(ARRANGED)
        self.panel.source_switch.setVisible(not same)
        self.panel.set_arrangement(arrangement, original, arranged, marks)
        self._source_changed(self.panel.source())
        self.set_position(min(position, arrangement.duration))

    def set_position(self, position):
        self.position = max(0., position)
        self.panel.set_position(self.position)
        duration = self.arrangement.duration if self.arrangement else 0
        self.time_label.setText(f'{clock(self.position)} / {clock(duration)}')

    # -- transport --------------------------------------------------------
    @property
    def playing(self):
        return self.audio.playing

    def toggle(self):
        if self.playing:
            self.stop()
        else:
            self.play()

    def play(self):
        if not self.arrangement:
            return
        start = 0. if self.position >= self.arrangement.duration-.05 else self.position
        try:
            self.audio.play(self.arrangement, start=start, original=self.panel.source() == ORIGINAL)
        except Exception as exc:
            self.message.setText(tr('listen.failed', error=exc))
            return
        self.message.setText('')
        self._was_playing = True
        self.timer.start()
        self._set_play_button(True)
        self.started.emit()

    def stop(self):
        """Stop sound and keep the place, so pressing play continues from here."""
        self.seek_timer.stop()
        self._resume_after_seek = False
        if self.audio.playing:
            self.audio.stop()
            self.set_position(self.audio.position)
        self._was_playing = False
        self.timer.stop()
        self._set_play_button(False)

    def seek(self, position):
        if not self.arrangement:
            return
        resume = self.playing or self._resume_after_seek
        if self.audio.playing:
            self.audio.stop()
        self.set_position(min(position, self.arrangement.duration))
        if resume:
            # Dragging sends many seeks; restart the sound once the drag settles.
            self._resume_after_seek = True
            self.seek_timer.start()

    def _resume(self):
        if self._resume_after_seek:
            self._resume_after_seek = False
            self.play()

    def _tick(self):
        if self.audio.playing:
            self.set_position(self.audio.position)
            return
        if self._resume_after_seek:
            return
        # Reached the end or failed.
        self.set_position(self.audio.position)
        self.timer.stop()
        self._was_playing = False
        self._set_play_button(False)
        if self.audio.error:
            self.message.setText(tr('listen.failed', error=self.audio.error))

    def _set_play_button(self, playing):
        self.play_button.setText(' '+tr('listen.pause' if playing else 'listen.play'))
        self.play_button.set_icon('pause' if playing else 'play')
        self.play_button.set_palette(self.p)

    def _source_changed(self, source):
        # The original score has no string/key data; keep its views to notes.
        original = source == ORIGINAL
        for view in (KEYS, KEYBOARD):
            self.panel.view_switch.buttons[view].setEnabled(not original)
        if original and self.panel.view() != NOTES:
            self.panel.set_view(NOTES)
        if self.playing:
            self.stop()
            self.play()

    # -- keyboard and closing ---------------------------------------------
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space:
            self.toggle()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        self.stop()
        self.closed.emit()
        super().closeEvent(event)
