"""Standard-library entry point: a missing dependency must not fail silently."""
import sys


def failure_text(exc):
    missing = isinstance(exc, ModuleNotFoundError)
    try:
        from guitar_player.i18n import start, tr
        from guitar_player.prefs import Preferences
        preferences = Preferences()
        start(preferences.get_ui().get('language'), preferences.language_folder())
        return tr('launch.missing', name=exc.name) if missing else tr('launch.failed', error=exc)
    except Exception:
        # The package itself does not import: show the error as it is.
        return str(exc)


def main():
    try:
        from guitar_player.__main__ import main as run
        run()
    except Exception as exc:
        message = failure_text(exc)
        if sys.stderr:
            print(message, file=sys.stderr)
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,message,'Sloppy Player',0x10)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
