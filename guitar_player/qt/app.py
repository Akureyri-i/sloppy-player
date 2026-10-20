"""QApplication setup shared by the player and the screenshot tool."""
import sys

from PySide6.QtCore import QLibraryInfo, QLocale, Qt, QTranslator
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import QApplication

from .theme import font_families
from .winstyle import set_app_id


def application(argv=None):
    app = QApplication.instance()
    if app:
        return app
    set_app_id()
    # Qt 6 is per-monitor DPI aware; pass fractional scales (125 %, 150 %) through unrounded.
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName('Sloppy Player')
    font = QFont(font_families())
    font.setPointSizeF(10)
    font.setHintingPreference(QFont.PreferNoHinting)
    app.setFont(font)
    app.setStyle('Fusion')
    return app


def install_translation(app, code):
    """Qt's own texts (message box buttons and the like) in the interface language,
    from the translations that ship with PySide6."""
    translator = QTranslator(app)
    if translator.load(QLocale(code.replace('-', '_')), 'qtbase', '_',
                       QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)
