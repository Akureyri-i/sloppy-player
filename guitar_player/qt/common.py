"""Small widget helpers shared by the main, listening and settings windows."""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QComboBox, QLabel, QListView, QPushButton, QSizePolicy

from .theme import font_families
from .winstyle import round_corners

ICON_FONTS = ('Segoe Fluent Icons', 'Segoe MDL2 Assets')
GLYPHS = {'play': '', 'pause': '', 'stop': '', 'listen': '',
          'open': '', 'settings': '', 'export': '', 'restart': '',
          'copy': '', 'paste': ''}


def glyph_icon(name, colour, size=16):
    """Icon-font glyph rendered at device resolution, so it stays sharp at 150 %."""
    icon = QIcon()
    for ratio in (1, 1.25, 1.5, 2):
        pixmap = QPixmap(int(size*ratio), int(size*ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.TextAntialiasing)
        f = QFont(list(ICON_FONTS))
        f.setPixelSize(size-2)
        painter.setFont(f)
        painter.setPen(QColor(colour))
        painter.drawText(QRect(0, 0, size, size), Qt.AlignCenter, GLYPHS[name])
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def label(text='', role=None):
    widget = QLabel(text)
    if role:
        widget.setProperty('role', role)
    return widget


def tabular_font(size=10):
    f = QFont(font_families())
    f.setPointSizeF(size)
    f.setFeature(QFont.Tag('tnum'), 1)
    return f


class IconButton(QPushButton):
    """Push button whose icon follows the palette (white on the primary button)."""

    def __init__(self, text, kind=None, icon=None):
        super().__init__(' '+text if icon else text)
        self.setCursor(Qt.PointingHandCursor)
        if kind:
            self.setProperty('kind', kind)
        self.icon_name = icon
        self.setIconSize(QSize(16, 16))

    def set_icon(self, name):
        self.icon_name = name

    def set_palette(self, p):
        if self.icon_name:
            colour = p.on_accent if self.property('kind') == 'primary' else p.text
            self.setIcon(glyph_icon(self.icon_name, colour))


class ComboBox(QComboBox):
    """Style sheets hide Fusion's arrow; draw a chevron in the palette colour."""

    def __init__(self, palette, items=()):
        super().__init__()
        self.p = palette
        self.setCursor(Qt.PointingHandCursor)
        # A plain list view lets the style sheet pad and round each item.
        self.setView(QListView())
        self.addItems(list(items))

    def set_palette(self, p):
        self.p = p
        self.update()

    def showPopup(self):
        super().showPopup()
        popup = self.view().window()
        popup.setMinimumWidth(self.width())
        round_corners(popup)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(self.p.text2), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        x, y = self.width()-20, self.height()/2
        path = QPainterPath()
        path.moveTo(x-4.5, y-2)
        path.lineTo(x, y+2.5)
        path.lineTo(x+4.5, y-2)
        painter.drawPath(path)


class ElidedLabel(QLabel):
    """One line; cut once at the end with an ellipsis, full text in the tooltip."""

    def __init__(self, text='', role=None):
        super().__init__()
        if role:
            self.setProperty('role', role)
        self.full = ''
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.set_full_text(text)

    def set_full_text(self, text):
        self.full = text
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, event):
        self._elide()
        super().resizeEvent(event)

    def _elide(self):
        self.setText(QFontMetrics(self.font()).elidedText(self.full, Qt.ElideRight, max(20, self.width())))
