import argparse
import json

from .arrange import arrange
from .export import export, write_midi, write_report
from .midi import read_song
from .i18n import start, tr
from .model import ARRANGERS, DENSITIES, MODES, Config
from .prefs import Preferences
from . import __version__


def main():
    preferences = Preferences()
    start(preferences.get_ui().get('language'), preferences.language_folder())
    parser = argparse.ArgumentParser(description='Sloppy Player '+__version__)
    parser.add_argument('--version',action='version',version=__version__)
    parser.add_argument('--analyze', metavar='SCORE', help=tr('cli.analyze'))
    parser.add_argument('--mode', choices=MODES, default='auto')
    parser.add_argument('--arranger',choices=ARRANGERS,default='model',help=tr('cli.arranger'))
    parser.add_argument('--density', choices=DENSITIES, default='balanced')
    parser.add_argument('--octave', type=int, default=None)
    parser.add_argument('--max-voices', type=int, default=None, help=tr('cli.max_voices'))
    parser.add_argument('--simplify', type=float, default=0, help=tr('cli.simplify'))
    parser.add_argument('--tuning-offset', type=int, default=1, help=tr('cli.tuning'))
    parser.add_argument('--gap-ms', type=float, default=0)
    parser.add_argument('--repeat-ms', type=float, default=0, help=tr('cli.repeat'))
    parser.add_argument('--speed', type=float, default=1)
    parser.add_argument('--hold-ms', type=float, default=25)
    parser.add_argument('--melody-source', help=tr('cli.source'))
    parser.add_argument('--bass-source')
    parser.add_argument('--export', metavar='FILE', help=tr('cli.export'))
    parser.add_argument('--export-midi')
    parser.add_argument('--report')
    parser.add_argument('--detailed', action='store_true')
    args = parser.parse_args()
    if not args.analyze:
        from .qt.controller import run
        run()
        return
    try:
        song = read_song(args.analyze)
        config = Config(mode=args.mode, arranger=args.arranger,density=args.density, octave=args.octave,
            max_voices=args.max_voices, simplify=args.simplify,tuning_offset=args.tuning_offset,
            speed=args.speed, hold=args.hold_ms/1000,gap=args.gap_ms/1000,repeat_interval=args.repeat_ms/1000,
            melody_source=args.melody_source, bass_source=args.bass_source)
        result = arrange(song, config)
        if args.export:
            export(result, args.export)
        if args.export_midi:
            write_midi(result, args.export_midi)
        if args.report:
            write_report(result, args.report, args.detailed)
        print(json.dumps(result.report(), ensure_ascii=True, indent=2))
    except (OSError, ValueError, EOFError) as exc:
        parser.exit(1, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
