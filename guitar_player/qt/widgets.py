"""Small reusable controls: segmented switch, keycap, app icon."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QPushButton, QWidget

from .theme import font_families

LOGO = Path(__file__).with_name('logo.svg')


class Segmented(QFrame):
    """Mutually exclusive options {value: shown text}; emits the chosen value."""
    changed = Signal(str)

    def __init__(self, options, current=None, parent=None):
        super().__init__(parent)
        self.setObjectName('Segmented')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        self.group = QButtonGroup(self)
        self.buttons = {}
        for option, text in options.items():
            button = QPushButton(text)
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            # Wide enough for the checked state's semibold text (style sheet: padding 14px, border 1px).
            bold = QFont(button.font())
            bold.setWeight(QFont.DemiBold)
            button.setMinimumWidth(QFontMetrics(bold).horizontalAdvance(text) + 30)
            self.group.addButton(button)
            layout.addWidget(button)
            self.buttons[option] = button
        self.set_value(current or next(iter(options)))
        self.group.buttonClicked.connect(lambda button: self.changed.emit(self.value()))

    def value(self):
        return next((option for option, button in self.buttons.items() if button.isChecked()), '')

    def set_value(self, option):
        if option in self.buttons:
            self.buttons[option].setChecked(True)


class Keycap(QWidget):
    """Display-only rounded key with a shallow lower edge."""

    def __init__(self, text, palette, parent=None):
        super().__init__(parent)
        self.text = text
        self.palette_ = palette
        self.setFixedSize(34, 30)

    def set_palette(self, palette):
        self.palette_ = palette
        self.update()

    def paintEvent(self, _):
        p = self.palette_
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        body = QRectF(.5, .5, self.width()-1, self.height()-1)
        painter.setPen(QColor(p.border))
        painter.setBrush(QColor(p.border))
        painter.drawRoundedRect(body, 6, 6)
        face = QRectF(.5, .5, self.width()-1, self.height()-4)
        painter.setBrush(QColor(p.surface))
        painter.drawRoundedRect(face, 6, 6)
        font = QFont(font_families())
        font.setPointSizeF(9)
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(p.accent))
        painter.drawText(face, Qt.AlignCenter, self.text)


def app_icon():
    """The logo (logo.svg), rendered at each icon size so it stays sharp."""
    renderer = QSvgRenderer(str(LOGO))
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap)
    return icon
