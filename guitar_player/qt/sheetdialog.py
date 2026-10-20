"""Text box for ml3czus-autoplayer sheets: paste one in to open it, or copy the
arrangement out as one (the sheet box of that player takes it as it is)."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog, QHBoxLayout, QPlainTextEdit, QVBoxLayout

from ..i18n import tr
from .common import IconButton, label
from .widgets import app_icon
from .winstyle import tint_title_bar


class SheetDialog(QDialog):
    opened = Signal(str)              # pasted sheet text, to open
    save_requested = Signal()         # save the shown sheet as a .txt file

    def __init__(self, palette, parent=None, text=None, note=''):
        """text: the sheet to show for copying; None: an empty box to paste one into."""
        super().__init__(parent)
        self.setObjectName('Root')
        self.p = palette
        self.pasting = text is None
        self.setWindowTitle(tr('sheet.paste_title' if self.pasting else 'sheet.export_title'))
        self.setWindowIcon(app_icon())
        self.resize(720, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(12)
        hint = label(tr('sheet.paste_hint' if self.pasting else 'sheet.export_hint'), 'secondary')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        if note:
            info = label(note, 'secondary')
            info.setWordWrap(True)
            layout.addWidget(info)
        self.text = QPlainTextEdit(objectName='Sheet')
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        if not self.pasting:
            self.text.setPlainText(text)
            self.text.setReadOnly(True)
        layout.addWidget(self.text, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        if self.pasting:
            self.open_button = IconButton(tr('sheet.open'), 'primary', 'open')
            self.open_button.clicked.connect(self._open)
            self.text.textChanged.connect(lambda: self.open_button.setEnabled(bool(self.text.toPlainText().strip())))
            self.open_button.setEnabled(False)
            self.buttons = [self.open_button]
        else:
            self.copy_button = IconButton(tr('sheet.copy'), 'primary', 'copy')
            self.copy_button.clicked.connect(self.copy)
            self.save_button = IconButton(tr('sheet.save'), icon='export')
            self.save_button.clicked.connect(self.save_requested.emit)
            self.buttons = [self.copy_button, self.save_button]
        for button in self.buttons:
            row.addWidget(button)
        close = IconButton(tr('sheet.close'))
        close.clicked.connect(self.close)
        self.buttons.append(close)
        row.addWidget(close)
        layout.addLayout(row)
        for button in self.buttons:
            button.set_palette(palette)

    def copy(self):
        QGuiApplication.clipboard().setText(self.text.toPlainText())
        self.copy_button.setText(' '+tr('sheet.copied'))

    def _open(self):
        self.opened.emit(self.text.toPlainText())
        self.close()

    def showEvent(self, event):
        super().showEvent(event)
        tint_title_bar(self, self.p)
