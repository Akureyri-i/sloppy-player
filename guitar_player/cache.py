"""Disposable caches must never turn a successful arrangement into a failure."""
import os
from pathlib import Path
import tempfile

# Analysis caches: per user, outside the program folder (which may not be writable).
CACHE_ROOT = Path(os.environ.get('LOCALAPPDATA') or os.environ.get('XDG_CACHE_HOME') or Path.home()/'.cache')/'SloppyPlayer'


def save_cache(path, writer, binary=False):
    path = Path(path)
    name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        options = {} if binary else {'encoding': 'utf8'}
        with tempfile.NamedTemporaryFile('wb' if binary else 'w', dir=path.parent,
                                         delete=False, suffix='.tmp', **options) as stream:
            name = stream.name
            writer(stream)
        os.replace(name, path)
        return True
    except OSError:
        return False
    finally:
        if name:
            try:
                Path(name).unlink(missing_ok=True)
            except OSError:
                pass
