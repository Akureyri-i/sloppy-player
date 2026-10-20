"""Main window layout. Behaviour lives in controller.py; this module only
builds widgets and exposes them.
"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (QDoubleSpinBox, QFrame, QHBoxLayout, QListWidget, QMainWindow, QMenu, QScrollArea,
                               QSlider, QVBoxLayout, QWidget)

from ..i18n import tr
from ..midi import SCORE_SUFFIXES
from ..model import MENU_MODES
from .common import ComboBox, ElidedLabel, IconButton, label, tabular_font
from .panel import ScorePanel
from .theme import DEFAULT, palette as find_palette
from .widgets import Keycap, app_icon
from .winstyle import tint_title_bar


class MainWindow(QMainWindow):
    file_dropped = Signal(str)
    closing = Signal()

    def __init__(self, theme=DEFAULT):
        super().__init__()
        self.setWindowTitle('Sloppy Player')
        self.setWindowIcon(app_icon())
        self.resize(1120, 720)
        self.setMinimumSize(900, 600)
        self.setAcceptDrops(True)
        self.p = find_palette(theme)
        self.buttons = []
        self._build()
        self.apply_palette(self.p)

    # -- layout ---------------------------------------------------------
    def _build(self):
        root = QWidget(objectName='Root')
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(24, 18, 24, 18)
        body.setSpacing(18)
        outer.addLayout(body, 1)

        main = QVBoxLayout()
        main.setSpacing(14)
        body.addLayout(main, 1)

        header = QHBoxLayout()
        header.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title = ElidedLabel(tr('main.no_song'), 'title')
        self.subtitle = label('', 'secondary')
        titles.addWidget(self.title)
        titles.addWidget(self.subtitle)
        header.addLayout(titles, 1)
        self.open_button = self._button(header, tr('main.open'), icon='open')
        self.paste_button = self._button(header, tr('main.paste'), icon='paste')
        self.settings_button = self._button(header, tr('main.settings'), icon='settings')
        main.addLayout(header)

        self.panel = ScorePanel(self.p)
        transport = self.panel.transport
        self.play_button = self._button(transport, tr('main.play'), 'primary', 'play')
        self.pause_button = self._button(transport, tr('main.pause'), icon='pause')
        self.stop_button = self._button(transport, tr('main.stop'), icon='stop')
        transport.addSpacing(10)
        self.listen_button = self._button(transport, tr('main.listen'), icon='listen')
        transport.addStretch(1)
        self.time_label = label('0:00 / 0:00', 'secondary')
        self.time_label.setFont(tabular_font())
        transport.addWidget(self.time_label)
        main.addWidget(self.panel, 1)

        body.addWidget(self._build_side_panel())

        status = QFrame(objectName='StatusBar')
        status.setFixedHeight(46)
        status_layout = QHBoxLayout(status)
        status_layout.setContentsMargins(24, 0, 24, 0)
        status_layout.setSpacing(6)
        self.status = label('', 'secondary')
        status_layout.addWidget(self.status, 1)
        self.keycaps = []
        for key, text in (('F1', tr('main.key_play')), ('F2', tr('main.key_pause')), ('F3', tr('main.key_stop'))):
            cap = Keycap(key, self.p)
            self.keycaps.append(cap)
            status_layout.addSpacing(10)
            status_layout.addWidget(cap)
            status_layout.addWidget(label(text, 'secondary'))
        outer.addWidget(status)

    def _build_side_panel(self):
        side = QFrame(objectName='SidePanel')
        side.setFixedWidth(292)
        outer = QVBoxLayout(side)
        outer.setContentsMargins(0, 8, 0, 18)
        outer.setSpacing(8)
        # Settings scroll in a short window; the export button stays visible.
        scroll = QScrollArea(objectName='SideScroll')
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget(objectName='SideContent')
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 10, 14, 4)
        layout.setSpacing(8)
        layout.addWidget(label(tr('main.mode'), 'section'))
        self.mode_combo = ComboBox(self.p, [])
        for mode in MENU_MODES:
            self.mode_combo.addItem(tr('mode.'+mode), mode)
        layout.addWidget(self.mode_combo)
        self.summary = label('', 'secondary')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        layout.addSpacing(16)

        self.simplify_value = label('0%', 'value')
        self.simplify_slider = QSlider(Qt.Horizontal)
        self.simplify_slider.setRange(0, 20)
        self._slider_block(layout, tr('main.simplify'), self.simplify_value, self.simplify_slider,
                           (tr('main.simplify_none'), tr('main.simplify_most')))
        layout.addSpacing(12)

        self.speed_box = QDoubleSpinBox()
        self.speed_box.setRange(.25, 4.)
        self.speed_box.setSingleStep(.05)
        self.speed_box.setDecimals(2)
        self.speed_box.setSuffix('×')
        self.speed_box.setValue(1.)
        self.speed_box.setAlignment(Qt.AlignRight)
        self.speed_box.setFixedWidth(86)
        self.speed_box.setKeyboardTracking(False)
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(25, 400)
        self.speed_slider.setValue(100)
        self._slider_block(layout, tr('main.speed'), self.speed_box, self.speed_slider, ('0.25×', '4×'))
        layout.addSpacing(12)

        layout.addWidget(label(tr('main.tracks'), 'section'))
        self.track_list = QListWidget(objectName='TrackList')
        self.track_list.setFixedHeight(40)
        layout.addWidget(self.track_list)
        layout.addStretch(1)

        buttons = QVBoxLayout()
        buttons.setContentsMargins(18, 0, 18, 0)
        buttons.setSpacing(8)
        self.restore_button = self._button(buttons, tr('main.restore'))
        self.restore_button.hide()
        self.export_button = self._button(buttons, tr('main.export'), icon='export')
        self.export_menu = QMenu(self)
        self.export_midi = self.export_menu.addAction(tr('export.menu_midi'))
        self.export_musicxml = self.export_menu.addAction(tr('export.menu_musicxml'))
        self.export_sheet = self.export_menu.addAction(tr('export.menu_sheet'))
        self.export_button.clicked.connect(
            lambda: self.export_menu.exec(self.export_button.mapToGlobal(QPoint(0, self.export_button.height()))))
        outer.addLayout(buttons)
        return side

    def _slider_block(self, layout, title, value_widget, slider, ends):
        row = QHBoxLayout()
        row.addWidget(label(title, 'section'))
        row.addStretch(1)
        row.addWidget(value_widget)
        layout.addLayout(row)
        layout.addWidget(slider)
        scale = QHBoxLayout()
        scale.addWidget(label(ends[0], 'secondary'))
        scale.addStretch(1)
        scale.addWidget(label(ends[1], 'secondary'))
        layout.addLayout(scale)

    def _button(self, layout, text, kind=None, icon=None):
        widget = IconButton(text, kind, icon)
        layout.addWidget(widget)
        self.buttons.append(widget)
        return widget

    # -- appearance -----------------------------------------------------
    def apply_palette(self, p):
        self.p = p
        for widget in self.buttons:
            widget.set_palette(p)
        for cap in self.keycaps:
            cap.set_palette(p)
        self.mode_combo.set_palette(p)
        self.panel.set_palette(p)
        tint_title_bar(self, p)

    def showEvent(self, event):
        super().showEvent(event)
        tint_title_bar(self, self.p)

    # -- drag and drop ----------------------------------------------------
    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if urls and urls[0].toLocalFile().lower().endswith(SCORE_SUFFIXES):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.file_dropped.emit(event.mimeData().urls()[0].toLocalFile())

    def closeEvent(self, event):
        self.closing.emit()
        super().closeEvent(event)
