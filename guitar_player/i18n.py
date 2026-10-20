"""Interface text by key, from JSON language files.

guitar_player/locales/<code>.json holds the built-in languages; a file of the same
name in the user's language folder (prefs.Preferences.language_folder) overrides
one, or adds a language. Each file maps keys to text, plus "_meta": {"name": ...}
for the language menu. zh-CN is the source: it has every key. Any other language
falls back to English, then to zh-CN, key by key. Text with {name} placeholders is
filled by tr(key, name=...); an entry whose placeholders differ from zh-CN's is
ignored, so a translation slip cannot break a message.
"""
from __future__ import annotations

import json
import locale
from pathlib import Path
from string import Formatter
import sys

BUILTIN = Path(__file__).with_name('locales')
SOURCE, FALLBACK = 'zh-CN', 'en'
# System languages that read another file of the same language best.
ALIASES = {'zh-hk': 'zh-TW', 'zh-mo': 'zh-TW', 'zh-hant': 'zh-TW', 'zh-sg': 'zh-CN', 'zh-hans': 'zh-CN'}


def _read(path):
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _files(folder):
    """{code: [paths]}: the built-in file first, then the user's."""
    found = {}
    for base in (BUILTIN, folder):
        if base and Path(base).is_dir():
            for path in sorted(Path(base).glob('*.json')):
                found.setdefault(path.stem, []).append(path)
    return found


def available(folder=None):
    """{code: the language's own name} of every language file."""
    names = {}
    for code, paths in _files(folder).items():
        names[code] = code
        for path in paths:
            meta = _read(path).get('_meta')
            if isinstance(meta, dict) and isinstance(meta.get('name'), str):
                names[code] = meta['name']
    return names


def fields(text):
    return {name for _, name, _, _ in Formatter().parse(text) if name is not None}


def entries(code, folder=None):
    """The usable entries of one language: built-in, then the user's file over it."""
    texts = {}
    for path in _files(folder).get(code, ()):
        texts.update({k: v for k, v in _read(path).items() if not k.startswith('_') and isinstance(v, str)})
    return texts


def table(code, folder=None):
    source = entries(SOURCE, folder)
    merged = dict(source)
    for layer in () if code == SOURCE else dict.fromkeys((FALLBACK, code)):
        merged.update({k: v for k, v in entries(layer, folder).items()
                       if k in source and fields(v) == fields(source[k])})
    return merged


def system_language():
    """The system's interface language as 'zh-CN' / 'en-US' / ..., or ''."""
    name = ''
    if sys.platform == 'win32':
        import ctypes
        name = locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), '')
    else:
        name = (locale.getlocale()[0] or '')
    return name.split('.')[0].replace('_', '-')


def match(wanted, codes):
    """The language file for wanted: the same code, else the same language, else English."""
    lower = {c.lower(): c for c in codes}
    wanted = wanted.lower()
    for alias, code in ALIASES.items():
        if (wanted == alias or wanted.startswith(alias + '-')) and code in codes:
            wanted = code.lower()
    if wanted in lower:
        return lower[wanted]
    primary = wanted.split('-')[0].lower()
    for code in codes:
        if code.split('-')[0].lower() == primary:
            return code
    return FALLBACK if FALLBACK in codes else SOURCE


current = SOURCE
_texts = table(SOURCE)


def use(code, folder=None):
    global current, _texts
    current, _texts = code, table(code, folder)


def start(saved, folder=None):
    """At launch: the language chosen in the settings, else the system's."""
    codes = available(folder)
    use(saved if saved in codes else match(system_language(), codes), folder)
    return current


def tr(key, **params):
    text = _texts[key]
    return text.format(**params) if params else text
