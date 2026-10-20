"""Windows-only window chrome: tint the native title bar to match the palette.

Uses the documented DWM attributes (Windows 11 22000+) instead of a frameless
window, so snapping, resizing and the system menu keep working. Older Windows
silently keeps the default title bar.
"""
import ctypes
import sys

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUNDSMALL = 3


def _colorref(colour):
    colour = colour.lstrip('#')
    r, g, b = (int(colour[i:i+2], 16) for i in (0, 2, 4))
    return ctypes.c_uint32(r | g << 8 | b << 16)


def tint_title_bar(widget, p):
    if sys.platform != 'win32':
        return False
    try:
        hwnd = ctypes.c_void_p(int(widget.winId()))
        set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
        dark = ctypes.c_int(1 if p.dark else 0)
        set_attribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(dark), ctypes.sizeof(dark))
        for attribute, colour in ((DWMWA_CAPTION_COLOR, p.bg), (DWMWA_TEXT_COLOR, p.text),
                                  (DWMWA_BORDER_COLOR, p.border)):
            value = _colorref(colour)
            set_attribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
        return True
    except (OSError, AttributeError):
        return False


def round_corners(widget):
    """Native Windows 11 rounded corners and shadow for popups (drop-down lists)."""
    if sys.platform != 'win32':
        return False
    try:
        value = ctypes.c_int(DWMWCP_ROUNDSMALL)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(int(widget.winId())),
            DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(value), ctypes.sizeof(value))
        return True
    except (OSError, AttributeError):
        return False


def set_app_id(app_id='SloppyPlayer'):
    """Stable taskbar grouping and icon for the process."""
    if sys.platform == 'win32':
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except (OSError, AttributeError):
            pass
