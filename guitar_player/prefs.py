from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .model import OLD_MODES, Config

UI_KEYS = ('theme', 'view', 'source', 'guard', 'hotkeys', 'timbre', 'timbre_path', 'timbre_preset', 'language')
# Settings saved by earlier development builds stored themes and views by their Chinese names.
OLD_UI = {'theme': {'霜白': 'frost', '宣纸': 'paper', '夜幕': 'night', '琥珀': 'amber', '霓虹': 'neon'},
          'view': {'音符': 'notes', '按键': 'keys', '键盘': 'keyboard'}}


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()


class Preferences:
    def __init__(self, path=None):
        base = Path(os.environ.get('APPDATA') or os.environ.get('XDG_CONFIG_HOME') or Path.home()/'.config')
        self.path = Path(path) if path else base / 'SloppyPlayer' / 'v16.json'

    def language_folder(self):
        """User language files (i18n): added languages, or overrides of built-in ones."""
        return self.path.parent / 'languages'

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key):
        entry = self.load().get(key)
        if not isinstance(entry, dict):
            return None
        try:
            if entry.get('mode') in OLD_MODES:
                entry['mode'] = OLD_MODES[entry['mode']]
            if entry.get('sources') is not None:
                if not isinstance(entry['sources'],list):return None
                entry['sources'] = tuple(entry['sources'])
            return Config(**entry)
        except (ValueError, TypeError):
            return None

    def save(self, key, config):
        data = self.load()
        data[key] = asdict(config)
        self._write(data)

    def get_ui(self):
        entry = self.load().get('__ui__', {})
        if not isinstance(entry, dict):return {}
        return {key:OLD_UI.get(key,{}).get(value,value) for key,value in entry.items()
                if key in UI_KEYS and isinstance(value,str)}

    def save_ui(self, **settings):
        data = self.load()
        data['__ui__'] = {key:value for key,value in settings.items()
                          if key in UI_KEYS and isinstance(value,str)}
        self._write(data)

    def get_volumes(self):
        """{pack or 'pack|bank:preset': dB set by hand} for the custom listening timbre.
        A pack or instrument that is not listed uses the automatic loudness match."""
        entry = self.load().get('__volume__', {})
        if not isinstance(entry, dict):
            return {}
        return {k: float(v) for k, v in entry.items() if isinstance(k, str) and isinstance(v, (int, float))}

    def save_volume(self, key, db):
        """db: the hand-set volume, or None to go back to the automatic match."""
        data = self.load()
        volumes = data.get('__volume__') if isinstance(data.get('__volume__'), dict) else {}
        if db is None:
            volumes.pop(key, None)
        else:
            volumes[key] = float(db)
        data['__volume__'] = volumes
        self._write(data)

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        name = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.path.parent,
                                             delete=False, suffix='.tmp') as stream:
                name = stream.name
                json.dump(data, stream, ensure_ascii=False, indent=2)
            os.replace(name, self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)
