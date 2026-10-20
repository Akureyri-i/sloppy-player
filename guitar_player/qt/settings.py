"""Settings window: appearance, arrangement details, key timing.

Widgets only; controller.py fills them and reacts to changes, so every
change applies at once and there is no OK/Cancel step.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QDialog, QFormLayout, QGridLayout, QHBoxLayout, QPlainTextEdit,
                               QSizePolicy, QSlider, QSpinBox, QStackedLayout, QVBoxLayout, QWidget)

from ..i18n import tr
from ..model import ARRANGERS
from ..sample_preview import CUSTOM, DEFAULT as DEFAULT_TIMBRE, VOLUME_RANGE
from .common import ComboBox, ElidedLabel, IconButton, label
from .scoreview import font
from .theme import PALETTES
from .widgets import Segmented, app_icon
from .winstyle import tint_title_bar

PAGES = ('appearance', 'arrangement', 'keys', 'listening')


class Swatch(QWidget):
    """A palette preview: background, card, accent and note colours. Clickable."""
    clicked = Signal()

    def __init__(self, palette):
        super().__init__()
        self.palette_ = palette
        self.checked = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(150, 104)

    def isChecked(self):
        return self.checked

    def setChecked(self, checked):
        self.checked = checked
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, _):
        p = self.palette_
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        outer = QRectF(1.5, 1.5, self.width()-3, self.height()-3)
        painter.setBrush(QColor(p.bg))
        painter.setPen(QPen(QColor(p.accent if self.isChecked() else p.border), 3 if self.isChecked() else 1))
        painter.drawRoundedRect(outer, 10, 10)
        card = QRectF(12, 12, self.width()-24, 46)
        painter.setPen(QPen(QColor(p.border), 1))
        painter.setBrush(QColor(p.surface))
        painter.drawRoundedRect(card, 6, 6)
        for i, colour in enumerate(p.notes):
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(colour))
            painter.drawRoundedRect(QRectF(20+i*9, 20+(i % 3)*9, 24, 6), 3, 3)
        painter.setBrush(QColor(p.accent))
        painter.drawRoundedRect(QRectF(card.right()-38, card.bottom()-16, 30, 10), 4, 4)
        painter.setPen(QColor(p.text))
        painter.setFont(font(10))
        painter.drawText(QRectF(12, 64, self.width()-24, 28), Qt.AlignLeft | Qt.AlignVCenter, tr('theme.'+p.name))


class SettingsWindow(QDialog):
    theme_chosen = Signal(str)
    timbre_chosen = Signal(str)          # sample_preview.DEFAULT or CUSTOM
    language_chosen = Signal(str)        # a language file's code
    pack_requested = Signal()            # the user wants to pick an .sfz file
    volume_changed = Signal(float)       # dB set by hand for this pack / instrument

    def __init__(self, palette, parent=None):
        super().__init__(parent)
        self.setObjectName('Root')
        self.setWindowTitle(tr('settings.title'))
        self.setWindowIcon(app_icon())
        self.setModal(False)
        self.resize(640, 520)
        self.p = palette
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(16)
        self.tabs = Segmented({page: tr('settings.page.'+page) for page in PAGES})
        layout.addWidget(self.tabs, 0, Qt.AlignLeft)
        self.pages = QStackedLayout()
        layout.addLayout(self.pages, 1)
        self.pages.addWidget(self._appearance())
        self.pages.addWidget(self._arrangement())
        self.pages.addWidget(self._keys())
        self.pages.addWidget(self._listening())
        self.tabs.changed.connect(lambda name: self.pages.setCurrentIndex(PAGES.index(name)))
        self.apply_palette(palette)

    def _page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        return page, layout

    def _form(self):
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        return form

    def _appearance(self):
        page, layout = self._page()
        layout.addWidget(label(tr('settings.colours'), 'section'))
        grid = QGridLayout()
        grid.setSpacing(12)
        grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.swatches = {}
        for i, p in enumerate(PALETTES.values()):
            swatch = Swatch(p)
            swatch.clicked.connect(lambda name=p.name: self.theme_chosen.emit(name))
            grid.addWidget(swatch, i//3, i % 3)
            self.swatches[p.name] = swatch
        layout.addLayout(grid)
        layout.addSpacing(8)
        layout.addWidget(label(tr('settings.language'), 'section'))
        row = QHBoxLayout()
        self.language = ComboBox(self.p, [])
        self.language.setMinimumWidth(200)
        self.language.activated.connect(lambda i: self.language_chosen.emit(self.language.itemData(i)))
        row.addWidget(self.language)
        self.language_note = label(tr('settings.language_restart'), 'secondary')
        self.restart_button = IconButton(tr('settings.restart'), icon='restart')
        row.addWidget(self.language_note)
        row.addWidget(self.restart_button)
        row.addStretch(1)
        self.show_restart(False)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def set_languages(self, names, chosen):
        """names: {code: the language's own name}."""
        self.language.clear()
        for code, name in sorted(names.items(), key=lambda item: item[1].casefold()):
            self.language.addItem(name, code)
        self.language.setCurrentIndex(max(0, self.language.findData(chosen)))

    def show_restart(self, shown):
        self.language_note.setVisible(shown)
        self.restart_button.setVisible(shown)

    def _arrangement(self):
        page, layout = self._page()
        form = self._form()
        self.engine = ComboBox(self.p, [])
        for arranger in ARRANGERS:
            self.engine.addItem(tr('arranger.'+arranger), arranger)
        self.octave = ComboBox(self.p, [tr('settings.auto')]+[f'{i:+d}' if i else '0' for i in range(-5, 6)])
        self.voices = ComboBox(self.p, [tr('settings.unlimited')]+[str(i) for i in range(1, 25)])
        self.melody = ComboBox(self.p, [tr('settings.auto')])
        self.melody.setToolTip(tr('settings.melody_tip'))
        self.drums = QCheckBox(tr('settings.drums'))
        form.addRow(tr('settings.engine'), self.engine)
        form.addRow(tr('settings.octave'), self.octave)
        form.addRow(tr('settings.voices'), self.voices)
        form.addRow(tr('settings.melody'), self.melody)
        form.addRow('', self.drums)
        layout.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(label(tr('settings.details'), 'section'))
        row.addStretch(1)
        self.report_button = IconButton(tr('settings.export_report'), icon='export')
        row.addWidget(self.report_button)
        layout.addLayout(row)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        self.combos = (self.engine, self.octave, self.voices, self.melody)
        return page

    def _spin(self, low, high, suffix=''):
        box = QSpinBox()
        box.setRange(low, high)
        box.setSuffix(suffix)
        # At least 120 px, wider when the suffix or "unlimited" needs it.
        box.setMinimumWidth(120)
        box.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        box.setKeyboardTracking(False)
        return box

    def _keys(self):
        page, layout = self._page()
        form = self._form()
        self.hold = self._spin(5, 100, ' ms')
        self.gap = self._spin(0, 50, ' ms')
        self.delay = self._spin(0, 250, ' ms')
        self.repeat = self._spin(0, 250, ' ms')
        self.repeat.setSpecialValueText(tr('settings.unlimited'))
        self.tuning = self._spin(-12, 12, ' '+tr('settings.semitones'))
        form.addRow(tr('settings.hold'), self.hold)
        form.addRow(tr('settings.gap'), self.gap)
        form.addRow(tr('settings.delay'), self.delay)
        form.addRow(tr('settings.repeat'), self.repeat)
        form.addRow(tr('settings.tuning'), self.tuning)
        layout.addLayout(form)
        layout.addSpacing(6)
        self.end_guard = QCheckBox(tr('settings.end_guard'))
        self.end_guard.setToolTip(tr('settings.end_guard_tip'))
        layout.addWidget(self.end_guard)
        self.guard = QCheckBox(tr('settings.focus_guard'))
        self.hotkeys = QCheckBox(tr('settings.hotkeys'))
        layout.addWidget(self.guard)
        layout.addWidget(self.hotkeys)
        layout.addSpacing(6)
        row = QHBoxLayout()
        self.calibrate_button = IconButton(tr('settings.calibrate'), icon='play')
        row.addWidget(self.calibrate_button)
        row.addWidget(label(tr('settings.calibrate_tip'), 'secondary'))
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        self.spins = (self.hold, self.gap, self.delay, self.repeat, self.tuning)
        return page

    def _listening(self):
        page, layout = self._page()
        layout.addWidget(label(tr('settings.timbre'), 'section'))
        self.timbre = Segmented({DEFAULT_TIMBRE: tr('timbre.default'), CUSTOM: tr('timbre.custom')})
        self.timbre.changed.connect(self.timbre_chosen.emit)
        layout.addWidget(self.timbre, 0, Qt.AlignLeft)
        layout.addWidget(label(tr('settings.timbre_tip'), 'secondary'))
        layout.addSpacing(8)
        layout.addWidget(label(tr('settings.pack'), 'section'))
        row = QHBoxLayout()
        self.pack_path = ElidedLabel('', 'secondary')
        row.addWidget(self.pack_path, 1)
        self.pack_button = IconButton(tr('settings.choose_pack'), icon='open')
        self.pack_button.clicked.connect(self.pack_requested.emit)
        row.addWidget(self.pack_button)
        layout.addLayout(row)
        self.pack_status = label('', 'secondary')
        self.pack_status.setWordWrap(True)
        layout.addWidget(self.pack_status)
        # SF2 / SF3 banks hold many instruments; SFZ packs have no choice to make.
        self.instrument_row = QWidget()
        row = QHBoxLayout(self.instrument_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(label(tr('settings.instrument'), 'section'))
        self.instrument = ComboBox(self.p, [])
        self.instrument.setMinimumWidth(280)
        row.addWidget(self.instrument)
        row.addStretch(1)
        self.instrument_row.hide()
        layout.addWidget(self.instrument_row)
        # Volume: automatic match to the game pack's loudness, plus the user's own trim.
        self.volume_row = QWidget()
        column = QVBoxLayout(self.volume_row)
        column.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(label(tr('settings.volume'), 'section'))
        # Half-dB steps. It always shows the volume in use, the automatic match included.
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(int(VOLUME_RANGE[0]*2), int(VOLUME_RANGE[1]*2))
        self.volume.setTracking(False)          # applied and saved once the handle is let go
        self.volume.setPageStep(6)
        self.volume.setFixedWidth(240)
        self.volume_value = label('0 dB', 'value')
        self.volume_value.setMinimumWidth(64)
        self.volume.sliderMoved.connect(lambda v: self._show_db(v/2))
        self.volume.valueChanged.connect(self._volume_set)
        row.addWidget(self.volume)
        row.addWidget(self.volume_value)
        row.addStretch(1)
        column.addLayout(row)
        self.auto_volume = QCheckBox(tr('settings.auto_volume'))
        column.addWidget(self.auto_volume)
        self.volume_row.hide()
        layout.addWidget(self.volume_row)
        layout.addStretch(1)
        return page

    def _show_db(self, db):
        self.volume_value.setText(f'{db:+.1f} dB' if abs(db) >= .05 else '0 dB')

    def _volume_set(self, value):
        self._show_db(value/2)
        self.volume_changed.emit(value/2)

    def show_volume(self, db, auto):
        """db: the volume in use (dB), or None when the default (MIDI) timbre is in use;
        auto: whether that is the automatic match."""
        self.volume_row.setVisible(db is not None)
        if db is None:
            return
        for widget in (self.volume, self.auto_volume):
            widget.blockSignals(True)
        self.volume.setValue(round(db*2))
        self.auto_volume.setChecked(auto)
        for widget in (self.volume, self.auto_volume):
            widget.blockSignals(False)
        self._show_db(db)

    def show_timbre(self, kind, path, status, presets=None, chosen=None):
        """presets: [(bank, preset, name)] of an SF2 / SF3 bank, else None."""
        self.timbre.set_value(kind)
        self.pack_path.set_full_text(str(path) if path else tr('settings.no_pack'))
        self.pack_status.setText(status)
        self.instrument.blockSignals(True)
        self.instrument.clear()
        for bank, preset, name in presets or ():
            self.instrument.addItem(f'{bank:03d}:{preset:03d}  {name}', (bank, preset))
            if (bank, preset) == chosen:
                self.instrument.setCurrentIndex(self.instrument.count()-1)
        self.instrument.blockSignals(False)
        self.instrument_row.setVisible(bool(presets))

    def apply_palette(self, p):
        self.p = p
        for combo in (*self.combos, self.instrument, self.language):
            combo.set_palette(p)
        for widget in (self.report_button, self.calibrate_button, self.pack_button, self.restart_button):
            widget.set_palette(p)
        for name, swatch in self.swatches.items():
            swatch.setChecked(name == p.name)
            swatch.update()
        tint_title_bar(self, p)

    def showEvent(self, event):
        super().showEvent(event)
        tint_title_bar(self, self.p)
