"""Colour tokens, contrast checks and the Qt style sheet for the five palettes.

Every text token must reach WCAG AA (4.5:1) on every surface it can sit on;
tests/test_qt_theme.py enforces this, so a palette edit cannot quietly
reintroduce unreadable grey text.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile

from .. import i18n


def font_families():
    """Segoe UI for Latin and Cyrillic; the CJK face that matches the interface language."""
    cjk = ('Microsoft JhengHei UI', 'Microsoft YaHei UI') if i18n.current == 'zh-TW' else ('Microsoft YaHei UI',)
    return ['Segoe UI Variable Text', 'Segoe UI', *cjk]


@dataclass(frozen=True)
class Palette:
    name: str        # id; shown as tr('theme.<name>')
    dark: bool
    bg: str          # window background
    surface: str     # cards and side panel
    sunken: str      # chart background inside a card
    border: str
    text: str
    text2: str       # secondary text: captions, labels
    accent: str
    accent_hover: str
    on_accent: str
    notes: tuple[str, ...]

    @property
    def surfaces(self):
        return self.bg, self.surface, self.sunken


PALETTES = {p.name: p for p in (
    # Light: neutral paper with an indigo accent; the default.
    Palette('frost', False, bg='#f2f3f7', surface='#ffffff', sunken='#f7f8fb', border='#dcdfe8',
            text='#151922', text2='#4b5262', accent='#4a4ed3', accent_hover='#3c3fb5', on_accent='#ffffff',
            notes=('#4a4ed3', '#0d7f72', '#bb5a0e', '#bf3a74', '#1d6fb0', '#687a10')),
    # Light: warm rice paper with a cinnabar accent.
    Palette('paper', False, bg='#f3eee5', surface='#fffcf6', sunken='#f8f4ec', border='#e2d9c9',
            text='#221b13', text2='#594a39', accent='#b0411d', accent_hover='#923618', on_accent='#ffffff',
            notes=('#b0411d', '#2d7656', '#8a6710', '#3c64a3', '#9a3d6c', '#6656a6')),
    # Dark: graphite, cool and quiet.
    Palette('night', True, bg='#121317', surface='#1a1c22', sunken='#0e0f12', border='#2c2f38',
            text='#eceef3', text2='#a7acba', accent='#8f93f8', accent_hover='#a8abfa', on_accent='#15164a',
            notes=('#8f93f8', '#4fd1c0', '#f5a95b', '#f07fb0', '#6cb6f5', '#b9d36a')),
    # Dark: stage-lit wood, amber accent.
    Palette('amber', True, bg='#17130f', surface='#211b15', sunken='#120f0b', border='#3a3027',
            text='#f4ece2', text2='#c2b29f', accent='#f0a63c', accent_hover='#f5b964', on_accent='#2a1700',
            notes=('#f0a63c', '#e5795f', '#7fc4a8', '#d9c25e', '#8fb2e8', '#d68fc0')),
    # Dark: deep navy with rhythm-game neon.
    Palette('neon', True, bg='#0d1020', surface='#151a30', sunken='#0a0c19', border='#283056',
            text='#eaf0ff', text2='#a9b3d6', accent='#3ee0d0', accent_hover='#6ae8dc', on_accent='#032b28',
            notes=('#3ee0d0', '#ff5fa2', '#9a88ff', '#ffd166', '#5fb3ff', '#7ee081')),
)}
DEFAULT = 'frost'


def palette(name):
    return PALETTES.get(name, PALETTES[DEFAULT])


def _channel(value):
    value /= 255
    return value/12.92 if value <= .03928 else ((value+.055)/1.055)**2.4


def luminance(colour):
    colour = colour.lstrip('#')
    r, g, b = (_channel(int(colour[i:i+2], 16)) for i in (0, 2, 4))
    return .2126*r+.7152*g+.0722*b


def contrast(a, b):
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high+.05)/(low+.05)


def readable_on(fill, p):
    """Label colour for text drawn on a coloured block (fret keys on note chips)."""
    dark, light = ('#0b0f16', '#ffffff')
    return dark if contrast(fill, dark) >= contrast(fill, light) else light


def mix(a, b, amount):
    """Blend colour a toward b; used for hover, pressed and dimmed tints."""
    a, b = a.lstrip('#'), b.lstrip('#')
    parts = (round(int(a[i:i+2], 16)*(1-amount)+int(b[i:i+2], 16)*amount) for i in (0, 2, 4))
    return '#'+''.join(f'{v:02x}' for v in parts)


def _check_image(colour):
    """Tick mark for check boxes; style sheets can only take images from files."""
    folder = Path(tempfile.gettempdir())/'midi-guitar-player'
    folder.mkdir(exist_ok=True)
    path = folder/f'check-{colour.lstrip("#")}.svg'
    if not path.exists():
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
                        f'<path d="M3.5 8.5l3 3 6-7" fill="none" stroke="{colour}" stroke-width="2" '
                        'stroke-linecap="round" stroke-linejoin="round"/></svg>', encoding='utf-8')
    return path.as_posix()


def style_sheet(p: Palette) -> str:
    hover = mix(p.surface, p.accent, .08)
    check = _check_image(p.on_accent)
    pressed = mix(p.surface, p.accent, .16)
    disabled_text = mix(p.text2, p.surface, .35)
    return f"""
* {{ color: {p.text}; }}
QMainWindow, QDialog, #Root {{ background: {p.bg}; }}
QLabel {{ background: transparent; }}
QLabel[role="secondary"] {{ color: {p.text2}; }}
QLabel[role="title"] {{ font-size: 20pt; font-weight: 600; }}
QLabel[role="section"] {{ font-size: 10.5pt; font-weight: 600; }}
QLabel[role="value"] {{ color: {p.text}; font-weight: 600; }}
#SideScroll, #SideContent {{ background: transparent; }}
#Card, #SidePanel {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 12px; }}
#StatusBar {{ background: {p.bg}; border-top: 1px solid {p.border}; }}
QPushButton {{
    background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px;
    padding: 7px 16px; min-height: 22px;
}}
QPushButton:hover {{ background: {hover}; }}
QPushButton:pressed {{ background: {pressed}; }}
QPushButton:disabled {{ color: {disabled_text}; }}
QPushButton[kind="primary"] {{
    background: {p.accent}; color: {p.on_accent}; border: none; font-weight: 600; padding: 8px 22px;
}}
QPushButton[kind="primary"]:hover {{ background: {p.accent_hover}; }}
QPushButton[kind="primary"]:disabled {{ background: {p.border}; color: {p.text2}; }}
QPushButton[kind="ghost"] {{ background: transparent; border: none; padding: 6px 10px; }}
QPushButton[kind="ghost"]:hover {{ background: {hover}; }}
#Segmented {{ background: {p.sunken}; border: 1px solid {p.border}; border-radius: 8px; }}
#Segmented QPushButton {{
    background: transparent; border: none; border-radius: 6px; padding: 4px 14px; min-height: 18px;
    color: {p.text2};
}}
#Segmented QPushButton:hover {{ color: {p.text}; }}
#Segmented QPushButton:checked {{ background: {p.surface}; color: {p.text}; font-weight: 600; border: 1px solid {p.border}; }}
QComboBox {{
    background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px; padding: 6px 12px; min-height: 22px;
    combobox-popup: 0;
}}
QComboBox:hover {{ border-color: {p.accent}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBoxPrivateContainer {{ background: {p.surface}; border: 1px solid {p.border}; }}
QComboBox QAbstractItemView {{
    background: {p.surface}; border: none; outline: none; padding: 4px;
}}
QComboBox QAbstractItemView::item {{ min-height: 32px; padding: 0 10px; border-radius: 6px; color: {p.text}; }}
QComboBox QAbstractItemView::item:hover {{ background: {hover}; }}
QComboBox QAbstractItemView::item:selected {{ background: {pressed}; color: {p.text}; }}
QSlider:horizontal {{ min-height: 26px; }}
QSlider::groove:horizontal {{ height: 4px; background: {p.border}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {p.surface}; border: 2px solid {p.accent}; width: 14px; height: 14px; margin: -7px 0; border-radius: 9px;
}}
QSlider::handle:horizontal:hover {{ background: {hover}; }}
QDoubleSpinBox {{
    background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px; padding: 4px 8px; min-height: 22px;
}}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: none; }}
QLineEdit {{
    background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px; padding: 6px 10px; min-height: 22px;
    color: {p.text}; selection-background-color: {pressed}; selection-color: {p.text};
}}
QLineEdit:focus {{ border-color: {p.accent}; }}
QProgressBar {{ background: {p.border}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 3px; }}
QSpinBox, QPlainTextEdit, QListWidget {{
    background: {p.surface}; border: 1px solid {p.border}; border-radius: 8px; padding: 4px 8px;
}}
QSpinBox {{ min-height: 22px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}
QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {{ border-color: {p.accent}; }}
QListWidget {{ padding: 4px; outline: none; }}
QListWidget::item {{ min-height: 28px; padding: 0 4px; border-radius: 6px; }}
QListWidget::item:hover {{ background: {hover}; }}
QListWidget::item:selected {{ background: transparent; color: {p.text}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator, QListWidget::indicator {{
    width: 16px; height: 16px; border: 1.5px solid {p.text2}; border-radius: 4px; background: {p.surface};
}}
QCheckBox::indicator:checked, QListWidget::indicator:checked {{
    background: {p.accent}; border-color: {p.accent}; image: url({check});
}}
QCheckBox:disabled, QComboBox:disabled {{ color: {disabled_text}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.border}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p.text2}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.border}; border-radius: 3px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {p.text2}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ height: 0; width: 0; background: none; }}
QMessageBox {{ background: {p.bg}; }}
QMenu {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.border}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: {hover}; }}
QPlainTextEdit#Sheet {{ font-family: Consolas, "Cascadia Mono", monospace; }}
QToolTip {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.border}; padding: 4px 8px; }}
"""
