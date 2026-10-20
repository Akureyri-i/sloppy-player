"""Behaviour of the Qt player: loading, arranging, the game transport, hotkeys,
preferences, and the listening and settings windows.

Game transport: when idle, the main window's position is where F1 starts
(default 0:00). F2 pauses in place; dragging during a performance moves the
paused position. Continuing is immediate when Roblox is in front, otherwise
it counts down 3 seconds. F3 and the end of a piece return to 0:00. The
listening window keeps its own position.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import math
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QFileDialog, QListWidgetItem, QMessageBox

from .. import i18n, music_model
from ..arrange import arrange
from ..engine import prepare, selected_notes
from ..ahk_sheet import read_sheet, write_sheet
from ..export import FORMATS, export, write_report
from ..jobs import LatestWorker, checkpoint
from ..midi import read_song
from ..i18n import tr
from ..model import MENU_MODES, Config, Note, Song, Source
from ..visual import VisualScore
from ..playback import EdgeHotkeys, Player, roblox_focused
from ..prefs import Preferences, file_hash
from ..sample_preview import CUSTOM, DEFAULT as DEFAULT_TIMBRE, GAME_PACK, PackError, TimbrePreview
from .listen import ListenWindow
from .panel import VIEWS
from .scoreview import change_marks, clock
from .settings import SettingsWindow
from .sheetdialog import SheetDialog
from .theme import DEFAULT, palette as find_palette, style_sheet
from .window import MainWindow

COUNTDOWN = 3.
REBUILD_DELAY = 300   # ms; sliders and spin boxes settle before re-arranging
# Fields "恢复默认" resets. Speed and key timing are kept: they are about the
# performance, not about how the piece is arranged.
ARRANGEMENT_FIELDS = ('mode', 'arranger', 'density', 'octave', 'max_voices', 'simplify', 'sources',
                      'melody_source', 'bass_source', 'include_drums')
ACTIVE = ('countdown', 'playing', 'paused')
# Why notes are missing (engine / guitarize drop reasons): tr('missing.<reason>') in the
# summary, tr('drop.<reason>') in the analysis details. Mode tips: tr('mode_tip.<mode>');
# the 简化 slider: tr('simplify_tip.guitar') for 吉他化, which is closest to human guitar tabs
# of the same songs at about 50%, tr('simplify_tip.auto') otherwise.


def end_guard_text(info):
    if not info or not info.get('enabled'):
        return tr('details.end_guard_off')
    if info['moved']:
        text = tr('details.end_guard_max', count=info['moved'], ms=f'{info["max_shift_ms"]:.0f}')
    else:
        text = tr('details.end_guard', count=0)
    if info.get('unavoidable'):
        text += tr('details.end_guard_unavoidable', count=info['unavoidable'])
    return text


def parse_preset(text):
    """'bank:preset' as saved in the preferences -> (bank, preset), else None."""
    try:
        bank, preset = (int(part) for part in text.split(':'))
    except ValueError:
        return None
    return (bank, preset) if 0 <= bank <= 128 and 0 <= preset <= 127 else None


def song_title(path):
    return Path(path).stem.replace('_', ' ').strip()


class Controller(QObject):
    def __init__(self, preferences=None, player=None, audio=None, global_hotkeys=None,
                 focus_check=roblox_focused, arrange_fn=arrange, analyze_fn=prepare,
                 preload_model=True):
        super().__init__()
        self.preferences = preferences or Preferences()
        self.player = player or Player()
        self.audio = audio or TimbrePreview()
        self.focus_check = focus_check
        self.arrange_fn = arrange_fn
        self.analyze_fn = analyze_fn
        ui = self.preferences.get_ui()
        self.p = find_palette(ui.get('theme', DEFAULT))
        self.guard = ui.get('guard', 'on') != 'off'
        # Custom timbre: the chosen pack, else a locally recorded game pack if there is one.
        saved_pack = ui.get('timbre_path')
        self.pack_path = Path(saved_pack) if saved_pack else (GAME_PACK if GAME_PACK.is_file() else None)
        self.pack_preset = parse_preset(ui.get('timbre_preset', ''))     # SF2 / SF3 instrument
        self.auto_db = None                     # automatic loudness match of the custom pack, dB
        self.timbre_note = ''
        if ui.get('timbre') == CUSTOM and hasattr(self.audio, 'set_timbre'):
            self._apply_timbre(CUSTOM, quiet=True)
        self.global_hotkeys = ui.get('hotkeys', 'on') != 'off' if global_hotkeys is None else global_hotkeys
        self.language = ui.get('language', '')   # '' until chosen: the system's language
        self.restart_requested = False
        self.config = Config()
        self.song = self.arrangement = self.digest = None
        self.original = self.arranged = self.marks = None
        self.generation = 0
        self.busy = False
        self.start = 0.                 # the game start point, in arranged seconds
        self.listen_positions = {}      # digest -> listening position
        self.listen = self.settings = self.sheet_dialog = None
        self._last_dir = ''
        self._last_status = 'stopped'
        self._countdown_from = None
        self._calibrating = False
        self._held = None               # paused at a dragged position; the player thread is stopped
        self._resuming = False          # the current countdown continues a performance
        self._shown_error = ''
        self._loading = False
        self._analysis = None           # running analysis: key and worker token
        self._analyzed = set()          # analysis keys done for the current song
        self._analysis_count = 0

        self.messages = queue.Queue()
        self.worker = LatestWorker(self.messages)
        self.hotkeys = EdgeHotkeys()
        QApplication.instance().setStyleSheet(style_sheet(self.p))
        self.window = w = MainWindow(self.p.name)
        w.panel.set_view(ui.get('view') if ui.get('view') in VIEWS else VIEWS[0])
        self._connect()
        self._apply_config_to_ui()
        self._set_status('')
        self._update_controls()
        self.rebuild_timer = QTimer(self, singleShot=True, interval=REBUILD_DELAY, timeout=self.rebuild)
        self.timer = QTimer(self, interval=25, timeout=self._tick)
        self.timer.start()
        if preload_model:
            # Loading the encoder takes ~45 s; start while the user picks a song.
            threading.Thread(target=music_model.preload, daemon=True, name='model-preload').start()

    # -- wiring -----------------------------------------------------------
    def _connect(self):
        w = self.window
        w.open_button.clicked.connect(lambda: self.load_file())
        w.paste_button.clicked.connect(self.paste_sheet)
        w.settings_button.clicked.connect(self.open_settings)
        w.play_button.clicked.connect(self.play)
        w.pause_button.clicked.connect(self.pause_resume)
        w.stop_button.clicked.connect(lambda: self.stop(cancel_analysis=True))
        w.listen_button.clicked.connect(self.open_listen)
        w.export_midi.triggered.connect(lambda: self.export_file('midi'))
        w.export_musicxml.triggered.connect(lambda: self.export_file('musicxml'))
        w.export_sheet.triggered.connect(self.show_sheet)
        w.restore_button.clicked.connect(self.restore_defaults)
        w.file_dropped.connect(self.load_file)
        w.closing.connect(self.close)
        w.panel.seek.connect(self._seek)
        w.panel.view_changed.connect(lambda _: self._save_ui())
        for i, key in enumerate(MENU_MODES):
            w.mode_combo.setItemData(i, tr('mode_tip.'+key), Qt.ToolTipRole)
        w.mode_combo.activated.connect(lambda _: self._edit(mode=w.mode_combo.currentData()))
        w.simplify_slider.valueChanged.connect(self._simplify_moved)
        w.speed_slider.valueChanged.connect(lambda v: self._speed_set(v/100))
        w.speed_box.valueChanged.connect(self._speed_set)
        w.track_list.itemChanged.connect(self._tracks_changed)
        # Window-only shortcuts, used when global hotkeys are off.
        self.shortcuts = [QShortcut(QKeySequence(key), w, activated=action) for key, action in
                          (('F1', self.play), ('F2', self.pause_resume),
                           ('F3', lambda: self.stop(cancel_analysis=True)))]
        self._sync_shortcuts()

    def _sync_shortcuts(self):
        for shortcut in self.shortcuts:
            shortcut.setEnabled(not self.global_hotkeys)

    def show(self):
        self.window.show()

    # -- appearance -------------------------------------------------------
    def set_theme(self, name):
        self.p = find_palette(name)
        QApplication.instance().setStyleSheet(style_sheet(self.p))
        for window in (self.window, self.listen, self.settings):
            if window:
                window.apply_palette(self.p)
        self._save_ui()

    def _save_ui(self):
        try:
            timbre = getattr(self.audio, 'kind', DEFAULT_TIMBRE)
            self.preferences.save_ui(theme=self.p.name, view=self.window.panel.view(),
                                     timbre=timbre, timbre_path=str(self.pack_path) if self.pack_path else '',
                                     timbre_preset='%d:%d' % self.pack_preset if self.pack_preset else '',
                                     guard='on' if self.guard else 'off',
                                     hotkeys='on' if self.global_hotkeys else 'off', language=self.language)
        except OSError:
            pass

    # -- status -----------------------------------------------------------
    def _set_status(self, text):
        if self.window.status.text() != text:
            self.window.status.setText(text)

    def _error(self, title, text):
        QMessageBox.warning(self.window, title, text)

    # -- configuration ----------------------------------------------------
    def _edit(self, **changes):
        try:
            config = replace(self.config, **changes)
        except ValueError as exc:
            self._error(tr('error.title.settings'), str(exc))
            self._apply_config_to_ui()
            return
        if config == self.config:
            return
        self.config = config
        self._update_restore()
        if self.song:
            self._invalidate()
            self.rebuild_timer.start()

    def _simplify_moved(self, value):
        self.window.simplify_value.setText(f'{value/20:.0%}')
        self._edit(simplify=value/20)

    def _speed_set(self, value):
        value = round(min(4., max(.25, value)), 2)
        w = self.window
        for widget, target in ((w.speed_slider, round(value*100)), (w.speed_box, value)):
            if widget.value() != target:
                widget.blockSignals(True)
                widget.setValue(target)
                widget.blockSignals(False)
        self._edit(speed=value)

    def _tracks_changed(self, _item=None):
        if not self.song:
            return
        items = [self.window.track_list.item(i) for i in range(self.window.track_list.count())]
        chosen = tuple(item.data(Qt.UserRole) for item in items if item.checkState() == Qt.Checked)
        all_ids = tuple(s.id for s in self.song.sources if s.channel != 9 or self.config.include_drums)
        if not chosen:
            # Keep at least one track; an empty selection has nothing to play.
            self._fill_tracks()
            return
        self._edit(sources=None if set(chosen) == set(all_ids) else chosen)

    def _apply_config_to_ui(self):
        w, c = self.window, self.config
        widgets = (w.mode_combo, w.simplify_slider, w.speed_slider, w.speed_box)
        for widget in widgets:
            widget.blockSignals(True)
        w.mode_combo.setCurrentIndex(w.mode_combo.findData(c.mode))
        w.simplify_slider.setValue(round(c.simplify*20))
        w.simplify_slider.setToolTip(tr('simplify_tip.guitar' if c.mode == 'guitar' else 'simplify_tip.auto'))
        w.simplify_value.setText(f'{c.simplify:.0%}')
        w.speed_slider.setValue(round(c.speed*100))
        w.speed_box.setValue(c.speed)
        for widget in widgets:
            widget.blockSignals(False)
        self._fill_tracks()
        self._update_restore()
        if self.settings:
            self._fill_settings()

    def _fill_tracks(self):
        tracks = self.window.track_list
        tracks.blockSignals(True)
        tracks.clear()
        for source in (self.song.sources if self.song else ()):
            item = QListWidgetItem(source.label)
            item.setData(Qt.UserRole, source.id)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            on = (source.id in self.config.sources if self.config.sources is not None
                  else source.channel != 9 or self.config.include_drums)
            item.setCheckState(Qt.Checked if on else Qt.Unchecked)
            tracks.addItem(item)
        tracks.blockSignals(False)
        # As tall as its rows (up to eight), so a two-track song leaves no empty box.
        rows = max(1, min(8, tracks.count()))
        tracks.setFixedHeight(rows*tracks.sizeHintForRow(0)+14 if tracks.count() else 40)

    def _update_restore(self):
        default = Config()
        changed = any(getattr(self.config, f) != getattr(default, f) for f in ARRANGEMENT_FIELDS)
        self.window.restore_button.setVisible(changed and self.song is not None)

    def restore_defaults(self):
        default = Config()
        self.config = replace(self.config, **{f: getattr(default, f) for f in ARRANGEMENT_FIELDS})
        self._apply_config_to_ui()
        if self.song:
            self._invalidate()
            self.rebuild()

    # -- loading and arranging ----------------------------------------------
    def load_file(self, path=None):
        if not path:
            path, _ = QFileDialog.getOpenFileName(self.window, tr('dialog.open_score'), self._last_dir,
                                                  tr('dialog.score_filter'))
        if not path:
            return
        self._last_dir = str(Path(path).parent)
        preferences = self.preferences

        def work():
            song = read_song(path)
            checkpoint()
            digest = file_hash(path)
            return song, digest, preferences.get(digest) or Config()
        self._load(song_title(path), work)

    def paste_sheet(self):
        """A box to paste an ml3czus-autoplayer sheet into; it opens like a file."""
        dialog = self.sheet_dialog = SheetDialog(self.p, self.window)
        dialog.opened.connect(self.load_text)
        dialog.show()

    def load_text(self, text):
        title = tr('sheet.pasted_title')
        preferences = self.preferences

        def work():
            song = read_sheet(text, title)
            digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
            return song, digest, preferences.get(digest) or Config()
        self._load(title, work)

    def _load(self, title, work):
        self._remember_listen()
        self.stop()
        self.rebuild_timer.stop()
        self.song = self.digest = None
        self._analysis = None
        self._analyzed = set()
        self._invalidate()
        self.generation += 1
        token = self.generation
        self.start = 0.
        self._loading = True
        w = self.window
        w.title.set_full_text(title)
        w.subtitle.setText('')
        w.summary.setText('')
        self._set_status(tr('status.reading'))
        self.busy = True
        self.worker.submit('loaded', token, work)
        self._update_controls()

    def _invalidate(self):
        """The shown arrangement no longer matches the settings.

        A running analysis (structure + octave optimisation, engine.prepare) is
        left alone when the new settings need the same one (speed,
        simplification, key timing); it finishes and the final step then uses
        the latest settings.
        """
        if not (self._analysis and self._analysis['key'] == self._analysis_key(self.config)):
            self.worker.cancel()
            self._analysis = None
        self.generation += 1
        if self._performing():
            self.stop()
        if self.listen:
            self.listen.stop()
        self.busy = self.song is not None
        if self.song:
            self.window.summary.setText(tr('status.arranging'))
            self._set_status(tr('status.arranging'))
        self._update_controls()

    def _analysis_key(self, config):
        """What the speed-independent analysis depends on, or None if none is needed."""
        if not self.song or config.arranger == 'legacy' or config.mode not in ('auto', 'full'):
            return None
        try:
            notes = selected_notes(self.song, config)
        except ValueError:
            return None
        low, high = config.bounds
        if config.mode == 'auto' and config.octave in (None, 0) and all(low <= n.pitch <= high for n in notes):
            return None   # played as written; see engine.recommend
        return tuple(sorted(n.id for n in notes)), config.arranger, config.bounds, config.octave

    def rebuild(self):
        self.rebuild_timer.stop()
        if not self.song or self.worker.closed:
            return
        key = self._analysis_key(self.config)
        if key is not None and key not in self._analyzed:
            if self._analysis and self._analysis['key'] == key:
                return   # already running; the arrangement follows when it is done
            self._analysis_count += 1
            token, song, config = ('analysis', self._analysis_count), self.song, self.config
            self._analysis = dict(key=key, token=token)
            self.busy = True
            self.worker.submit('analyzed', token,
                               lambda: self.analyze_fn(song, config))
            self._update_controls()
            return
        self._analysis = None
        self.generation += 1
        token, song, config = self.generation, self.song, self.config
        self.busy = True
        self.worker.submit('arranged', token, lambda: self.arrange_fn(song, config))
        self._update_controls()

    def _loaded(self, data):
        self.song, self.digest, config = data
        self._loading = False
        self.config = config
        w = self.window
        w.subtitle.setText(tr('main.subtitle', tracks=len(self.song.sources), duration=clock(self.song.duration/config.speed)))
        self._apply_config_to_ui()
        self.original = VisualScore.from_song(self.song, config.speed)
        self.window.panel.set_arrangement(None, self.original, VisualScore())
        self.rebuild()

    def _arranged(self, arrangement):
        old = self.arrangement
        self.arrangement = a = arrangement
        self.busy = False
        if old is not None and old.song is a.song and old.config.speed != a.config.speed:
            self.start *= old.config.speed/a.config.speed
        self.start = min(self.start, a.duration)
        self.original = VisualScore.from_song(a.song, a.config.speed, a.selected)
        self.arranged = VisualScore.from_arrangement(a)
        self.marks = change_marks(a)
        w = self.window
        w.panel.set_arrangement(a, self.original, self.arranged, self.marks)
        w.title.set_full_text(song_title(a.song.path))
        w.subtitle.setText(tr('main.subtitle', tracks=len(a.song.sources), duration=clock(a.duration)))
        w.summary.setText(self._summary(a))
        try:
            self.preferences.save(self.digest, a.config)
        except OSError:
            pass
        if self.listen:
            self._send_to_listen(old)
        if self.settings:
            self._fill_details()
        self._set_status(tr('status.ready' if a.strikes else 'status.nothing'))
        self._update_controls()

    @staticmethod
    def _summary(a):
        """What will be played, and why notes are missing if any are."""
        stats = a.stats
        rec = stats.get('recommendation') or {}
        lines = []
        if rec.get('in_range'):
            lines.append(tr('summary.in_range'))
        elif rec:
            lines.append(tr('summary.chosen', mode=tr('mode.'+rec['chosen'])))
        played, total = stats['played_notes'], stats['selected_notes']
        lines.append(tr('summary.all', total=f'{total:,}') if played == total
                     else tr('summary.played', played=f'{played:,}', total=f'{total:,}'))
        moved = sum(count for shift, count in stats['shifts_semitones'].items() if shift)
        if moved:
            lines.append(tr('summary.moved', count=f'{moved:,}'))
        for reason, count in stats['drop_reasons'].items():
            lines.append(tr('missing.'+reason, count=f'{count:,}'))
        analysis = stats.get('analysis', stats.get('joint', {}))
        if analysis.get('fallback_reason'):
            lines.append(tr('summary.fallback'))
        return '\n'.join(lines)

    # -- the game transport -------------------------------------------------
    def _performing(self, state=None):
        return (state or self.player.state).status in ACTIVE or self._held is not None

    def _seek(self, position):
        """Idle: choose where F1 starts. During a performance: pause there."""
        if not self.arrangement or self._calibrating:
            return
        position = min(position, self.arrangement.duration)
        if self.player.state.status in ACTIVE:
            # Release every key and keep the new place; F2 continues from it.
            self.player.stop()
            self._countdown_from = None
            self._resuming = False
            self._held = position
            self._set_status(tr('status.paused'))
        elif self._held is not None:
            self._held = position
        else:
            self.start = position

    def play(self):
        if not self.arrangement or self.busy or self._performing():
            return
        if self.listen:
            self.listen.stop()
        self._shown_error = ''
        start = 0. if self.start >= self.arrangement.duration-.01 else self.start
        if self.player.play(self.arrangement, start=start, countdown=COUNTDOWN, focus_guard=self.guard):
            self._resuming = False
            self._countdown_from = time.monotonic()

    def pause_resume(self):
        state = self.player.state
        if self._held is not None:
            self._resume_from(self._held)
        elif state.status == 'playing' or self._calibrating:
            self.player.pause_resume()
        elif state.status == 'paused':
            if self.focus_check():
                self.player.pause_resume()
            else:
                # Pressed in this window: give time to switch back to the game.
                self.player.stop()
                self._resume_from(state.position)

    def _resume_from(self, position):
        """Continue at once when Roblox is in front, otherwise after a countdown."""
        countdown = 0. if self.focus_check() else COUNTDOWN
        if self.listen:
            self.listen.stop()
        if self.player.play(self.arrangement, start=position, countdown=countdown, focus_guard=self.guard):
            self._held = None
            self._resuming = bool(countdown)
            self._countdown_from = time.monotonic()

    def stop(self, cancel_analysis=False, rewind=True):
        if cancel_analysis and self.busy and not self._performing():
            self.worker.cancel()
            self._analysis = None
            self.rebuild_timer.stop()
            self.generation += 1
            self.busy = False
            if self._loading:
                self._loading = False
                self.window.title.set_full_text(tr('main.no_song'))
            self.window.summary.setText(tr('summary.cancelled') if self.song else '')
            self._set_status(tr('status.cancelled'))
        was_active = self._performing()
        self.player.stop()
        self._held = None
        self._resuming = False
        self._countdown_from = None
        if rewind:
            self.start = 0.
        if was_active:
            self._set_status(tr('status.stopped') if not self._calibrating else '')
        self._calibrating = False
        self._update_controls()

    def _tick(self):
        self._drain_messages()
        state = self.player.state
        active = state.status in ACTIVE
        w = self.window
        if self._held is not None:
            shown = self._held
        elif active and not self._calibrating:
            shown = state.position
        else:
            shown = self.start
        w.panel.set_position(shown)
        duration = self.arrangement.duration if self.arrangement else 0
        w.time_label.setText(f'{clock(shown)} / {clock(duration)}')
        if state.status != self._last_status or state.status in ('countdown', 'paused'):
            self._transport_status(state)
        self._last_status = state.status
        if state.error and state.error != self._shown_error:
            self._shown_error = state.error
            self._error(tr('error.title.play'), state.error)
        self._update_controls(state)
        self._poll_hotkeys()

    def _transport_status(self, state):
        if state.status == 'countdown':
            left = COUNTDOWN-(time.monotonic()-(self._countdown_from or time.monotonic()))
            self._set_status(tr('status.countdown_resume' if self._resuming else 'status.countdown_start',
                                seconds=max(1, math.ceil(left))))
        elif state.status == 'playing':
            self._resuming = False
            self._set_status(tr('status.calibrating' if self._calibrating else 'status.playing'))
        elif state.status == 'paused':
            if self.guard and not self.focus_check():
                self._set_status(tr('status.paused_focus'))
            else:
                self._set_status(tr('status.paused'))
        elif state.status == 'finished' and self._last_status in ACTIVE:
            self.start = 0.
            if self._calibrating:
                self._set_status(tr('status.calibration_done'))
            elif state.skipped:
                self._set_status(tr('status.finished_skipped', count=state.skipped))
            else:
                self._set_status(tr('status.finished'))
            self._calibrating = False

    def _update_controls(self, state=None):
        state = state or self.player.state
        w = self.window
        active = self._performing(state)
        held = state.status == 'paused' or self._held is not None
        ready = self.arrangement is not None and not self.busy
        for widget, enabled in ((w.play_button, ready and not active),
                                (w.pause_button, state.status == 'playing' or held),
                                (w.stop_button, active or self.busy),
                                (w.listen_button, ready),
                                (w.export_button, ready),
                                (w.mode_combo, self.song is not None)):
            if widget.isEnabled() != enabled:
                widget.setEnabled(enabled)
        text = ' '+tr('main.resume' if held else 'main.pause')
        if w.pause_button.text() != text:
            w.pause_button.setText(text)

    def _poll_hotkeys(self):
        if not self.global_hotkeys:
            return
        try:
            import keyboard
            edges = self.hotkeys.update(k for k in ('f1', 'f2', 'f3') if keyboard.is_pressed(k))
        except Exception:
            self.global_hotkeys = False
            self._sync_shortcuts()
            self._set_status(tr('status.no_hotkeys'))
            return
        if 'f3' in edges:
            self.stop(cancel_analysis=True)
        elif 'f2' in edges:
            self.pause_resume()
        elif 'f1' in edges:
            self.play()

    def _drain_messages(self):
        while True:
            try:
                kind, token, data = self.messages.get_nowait()
            except queue.Empty:
                return
            analysis = self._analysis is not None and token == self._analysis['token']
            if token != self.generation and not analysis:
                continue
            if kind == 'analyzed':
                self._analyzed.add(self._analysis['key'])
                self._analysis = None
                self.rebuild()
            elif kind == 'progress':
                self._set_status(data)
            elif kind == 'loaded':
                self._loaded(data)
            elif kind == 'arranged':
                self._arranged(data)
            else:
                self.busy = False
                self._analysis = None
                self._loading = False
                self._set_status('')
                self.window.summary.setText('')
                self._update_controls()
                self._error(tr('error.title.load'), data)

    # -- listening window ---------------------------------------------------
    def open_listen(self):
        if not self.arrangement or self.busy:
            return
        if self.listen is None:
            self.listen = ListenWindow(self.audio, self.p)
            self.listen.timbre_chosen.connect(self.set_timbre)
            self.listen.timbre.set_value(getattr(self.audio, 'kind', DEFAULT_TIMBRE))
            self.listen.closed.connect(self._listen_closed)
            self.listen.started.connect(self._listen_started)
            self._send_to_listen(None)
        self.listen.show()
        self.listen.raise_()
        self.listen.activateWindow()

    def _send_to_listen(self, old):
        position = self.listen_positions.get(self.digest, 0.)
        if self.listen.arrangement is not None and self.listen.arrangement.song is self.arrangement.song:
            position = self.listen.position
            if old is not None and old.config.speed != self.arrangement.config.speed:
                position *= old.config.speed/self.arrangement.config.speed
        self.listen.set_arrangement(self.arrangement, self.original, self.arranged, self.marks,
                                    song_title(self.arrangement.song.path), position)

    def _listen_started(self):
        # One sound source at a time: listening never overlaps a game performance.
        if self._performing():
            self.stop()

    def _remember_listen(self):
        if self.listen and self.digest:
            self.listen_positions[self.digest] = self.listen.position

    def _listen_closed(self):
        self._remember_listen()
        self.listen.deleteLater()
        self.listen = None

    # -- settings window ----------------------------------------------------
    def open_settings(self):
        if self.settings is None:
            s = self.settings = SettingsWindow(self.p, self.window)
            s.theme_chosen.connect(self.set_theme)
            s.set_languages(i18n.available(self.preferences.language_folder()), self.language or i18n.current)
            s.language_chosen.connect(self._language_chosen)
            s.restart_button.clicked.connect(self.restart)
            s.engine.activated.connect(lambda _: self._edit(arranger=s.engine.currentData()))
            s.octave.activated.connect(lambda _: self._edit(
                octave=None if s.octave.currentIndex() == 0 else int(s.octave.currentText())))
            s.voices.activated.connect(lambda _: self._edit(
                max_voices=None if s.voices.currentIndex() == 0 else int(s.voices.currentText())))
            s.melody.activated.connect(lambda _: self._edit(melody_source=s.melody.currentData()))
            s.drums.toggled.connect(lambda on: self._edit(include_drums=on, sources=None))
            s.hold.valueChanged.connect(lambda v: self._edit(hold=v/1000))
            s.gap.valueChanged.connect(lambda v: self._edit(gap=v/1000))
            s.delay.valueChanged.connect(lambda v: self._edit(max_delay=v/1000))
            s.repeat.valueChanged.connect(lambda v: self._edit(repeat_interval=v/1000))
            s.end_guard.toggled.connect(lambda on: self._edit(end_guard=on))
            s.tuning.valueChanged.connect(lambda v: self._edit(tuning_offset=v))
            s.guard.toggled.connect(self._guard_toggled)
            s.hotkeys.toggled.connect(self._hotkeys_toggled)
            s.calibrate_button.clicked.connect(self.calibrate)
            s.timbre_chosen.connect(self.set_timbre)
            s.pack_requested.connect(self.choose_pack)
            s.instrument.activated.connect(lambda i: self.set_preset(s.instrument.itemData(i)))
            s.volume_changed.connect(self.set_volume)
            s.auto_volume.toggled.connect(self.set_auto_volume)
            self._show_timbre()
            s.report_button.clicked.connect(self.export_report)
            s.finished.connect(self._settings_closed)
            self._fill_settings()
            self._fill_details()
        self.settings.show()
        self.settings.raise_()
        self.settings.activateWindow()

    # -- listening timbre -----------------------------------------------------
    def _apply_timbre(self, kind, quiet=False):
        """Switch the preview's timbre; on a bad pack keep the old one and say why."""
        if kind == CUSTOM and not self.pack_path:
            self.timbre_note = tr('timbre.no_pack')
            if not quiet:
                self._error(tr('error.title.timbre'), tr('error.choose_pack'))
            return False
        try:
            self.audio.set_timbre(kind, self.pack_path if kind == CUSTOM else None)
            font = getattr(self.audio, 'soundfont', None)
            if font is not None:
                self.audio.set_preset(self.pack_preset)
                self.pack_preset = font.preset          # an unknown saved preset fell back
            self._apply_volume()
        except PackError as exc:
            self.timbre_note = str(exc)
            if not quiet:
                self._error(tr('error.title.timbre'), str(exc))
            return False
        self.timbre_note = ''
        return True

    def set_timbre(self, kind):
        listen = self.listen
        was_playing = bool(listen and listen.playing)
        if listen:
            listen.stop()
        self._apply_timbre(kind)
        if was_playing:
            listen.play()
        self._show_timbre()
        self._save_ui()

    def choose_pack(self):
        start = str(self.pack_path.parent if self.pack_path else Path.home())
        path, _ = QFileDialog.getOpenFileName(self.settings or self.window, tr('dialog.choose_pack'), start,
                                              tr('dialog.pack_filter'))
        if not path:
            return
        previous = self.pack_path, self.pack_preset
        self.pack_path, self.pack_preset = Path(path), None    # a new bank starts on its guitar
        if not self._apply_timbre(CUSTOM):
            self.pack_path, self.pack_preset = previous
        self._show_timbre()
        self._save_ui()

    def _show_timbre(self):
        kind = getattr(self.audio, 'kind', DEFAULT_TIMBRE)
        font = getattr(self.audio, 'soundfont', None)
        if self.listen:
            self.listen.timbre.set_value(kind)
            self.listen.timbre.setToolTip(str(self.pack_path) if kind == CUSTOM and self.pack_path else tr('timbre.midi'))
        if self.settings:
            if self.timbre_note:
                status = tr('timbre.unusable', reason=self.timbre_note)
            elif kind == CUSTOM and font:
                status = tr('timbre.presets', count=len(font.presets))
            elif kind == CUSTOM:
                status = tr('timbre.regions', count=len(self.audio.current.pack.regions))
            else:
                status = ''
            self.settings.show_timbre(kind, self.pack_path, status, font.presets if font else None,
                                      font.preset if font else None)
            custom = kind == CUSTOM and not self.timbre_note
            manual = self.manual_db()
            shown = manual if manual is not None else self.auto_db
            self.settings.show_volume(shown if custom and shown is not None else None, manual is None)

    # -- listening volume -------------------------------------------------------
    def volume_key(self):
        """Trims are remembered per pack, and per instrument of an SF2 / SF3 bank."""
        font = getattr(self.audio, 'soundfont', None)
        key = str(self.pack_path) if self.pack_path else ''
        return f'{key}|{font.preset[0]}:{font.preset[1]}' if font else key

    def manual_db(self):
        """The volume set by hand for this pack / instrument, or None (automatic match)."""
        return self.preferences.get_volumes().get(self.volume_key())

    def _apply_volume(self):
        if hasattr(self.audio, 'set_volume'):
            self.auto_db = self.audio.set_volume(self.manual_db())

    def _save_volume(self, db):
        try:
            self.preferences.save_volume(self.volume_key(), db)
        except OSError:
            pass
        self._apply_volume()               # takes effect from the next block: no restart
        self._show_timbre()

    def set_volume(self, db):
        """Moving the slider sets the volume by hand: the automatic match is off for it."""
        self._save_volume(float(db))

    def set_auto_volume(self, on):
        """On: back to the automatic value. Off: keep the volume as it is, now set by hand."""
        self._save_volume(None if on else (self.auto_db or 0.))

    def set_preset(self, preset):
        """The SF2 / SF3 instrument chosen in 设置 → 试听."""
        listen = self.listen
        was_playing = bool(listen and listen.playing)
        if listen:
            listen.stop()
        self.pack_preset = tuple(preset)
        self.audio.set_preset(self.pack_preset)
        self._apply_volume()
        if was_playing:
            listen.play()
        self._show_timbre()
        self._save_ui()

    def _settings_closed(self):
        self.settings.deleteLater()
        self.settings = None

    def _fill_settings(self):
        s, c = self.settings, self.config
        widgets = (*s.combos, *s.spins, s.drums, s.end_guard, s.guard, s.hotkeys)
        for widget in widgets:
            widget.blockSignals(True)
        s.engine.setCurrentIndex(s.engine.findData(c.arranger))
        s.octave.setCurrentIndex(0 if c.octave is None else c.octave+6)
        s.voices.setCurrentIndex(0 if c.max_voices is None else min(c.max_voices, 24))
        sources = self.song.sources if self.song else ()
        s.melody.clear()
        s.melody.addItem(tr('settings.auto'), None)
        for source in sources:
            s.melody.addItem(source.label, source.id)
        s.melody.setCurrentIndex(max(0, s.melody.findData(c.melody_source)))
        s.melody.setEnabled(c.mode == 'guitar')
        s.drums.setChecked(c.include_drums)
        s.hold.setValue(round(c.hold*1000))
        s.gap.setValue(round(c.gap*1000))
        s.delay.setValue(round(c.max_delay*1000))
        s.repeat.setValue(round(c.repeat_interval*1000))
        s.tuning.setValue(c.tuning_offset)
        s.end_guard.setChecked(c.end_guard)
        s.guard.setChecked(self.guard)
        s.hotkeys.setChecked(self.global_hotkeys)
        for widget in widgets:
            widget.blockSignals(False)

    def _fill_details(self):
        a = self.arrangement
        if not self.settings:
            return
        if not a:
            self.settings.details.setPlainText('')
            return
        s = a.stats
        comma = tr('details.separator')
        shifts = comma.join(tr('details.shift', shift=f'{k:+d}', count=v) for k, v in sorted(s['shifts_semitones'].items()))
        dropped = comma.join(tr('details.drop', reason=tr('drop.'+k), count=v) for k, v in s['drop_reasons'].items())
        lines = [tr('details.notes', selected=s['selected_notes'], played=s['played_notes']),
                 tr('details.onsets', share=f'{s["onset_retention"]:.1%}'),
                 tr('details.delay', mean=f'{s["mean_delay_ms"]:.1f}', max=f'{s["max_delay_ms"]:.1f}'),
                 tr('details.rolled', count=s['rolled_groups_over_six']),
                 end_guard_text(s.get('end_guard')),
                 tr('details.shifts', shifts=shifts),
                 tr('details.dropped', dropped=dropped or tr('details.none'))]
        rec = s.get('recommendation')
        if rec:
            lines += ['', tr('details.scores')] + [f'  {k}  {v}' for k, v in sorted(rec['scores'].items(), key=lambda kv: -kv[1])]
            lines.append(tr('details.chosen', candidate=rec['candidate']))
        analysis = s.get('analysis', s.get('joint', {}))
        if analysis:
            lines += ['', tr('details.model_used', answer=tr('details.yes' if analysis.get('model_used') else 'details.no')),
                      tr('details.links', count=analysis.get('candidate_links', 0)),
                      tr('details.motifs', count=analysis.get('motif_links', 0))]
            if analysis.get('fallback_reason'):
                lines.append(tr('details.fallback', reason=analysis['fallback_reason']))
        if a.warnings:
            lines += ['', *a.warnings]
        self.settings.details.setPlainText('\n'.join(lines))

    def _language_chosen(self, code):
        """Saved now; the interface changes at the next start."""
        self.language = code
        self._save_ui()
        self.settings.show_restart(code != i18n.current)

    def restart(self):
        self.restart_requested = True
        self.window.close()

    def _guard_toggled(self, on):
        self.guard = on
        self._save_ui()

    def _hotkeys_toggled(self, on):
        self.global_hotkeys = on
        self.hotkeys.update(())
        self._sync_shortcuts()
        self._save_ui()

    def calibrate(self):
        self.stop()
        try:
            config = replace(self.config, mode='raw', sources=None, melody_source=None, bass_source=None,
                             speed=1, octave=0, max_voices=None, simplify=0)
            bases = config.tuning
            pitches = [(i+1, p) for i, p in enumerate(bases)]+[(7, bases[1]), (7, bases[2]), (7, bases[3]),
                                                               (8, bases[5]), (8, config.bounds[1])]
            notes = [Note(i, p, 100, t, t+.4, t*480, int((t+.4)*480), t, 1, '0:0', 0)
                     for i, (t, p) in enumerate(pitches)]
            song = Song('calibration', notes, [Source('0:0', 0, 0, tr('calibration.track'), len(notes), *config.bounds)], 9, 480)
            plan = arrange(song, config)
        except Exception as exc:
            self._error(tr('error.title.calibration'), str(exc))
            return
        QMessageBox.information(self.settings or self.window, tr('calibration.title'), tr('calibration.message'))
        if self.listen:
            self.listen.stop()
        self._calibrating = self.player.play(plan, countdown=COUNTDOWN, focus_guard=self.guard)
        self._countdown_from = time.monotonic()

    # -- export -------------------------------------------------------------
    def export_file(self, kind):
        """kind: a key of export.FORMATS; the file's extension decides the format."""
        if not self.arrangement:
            return
        default = str(Path(self._last_dir or '.')/(Path(self.song.path).stem+'_guitar'+FORMATS[kind]))
        path, _ = QFileDialog.getSaveFileName(self.sheet_dialog if kind == 'sheet' else self.window,
                                              tr('dialog.export_'+kind), default, tr('dialog.filter_'+kind))
        if path:
            try:
                export(self.arrangement, path)
                self._set_status(tr('status.exported', name=Path(path).name))
            except Exception as exc:
                self._error(tr('error.title.export'), str(exc))

    def show_sheet(self):
        """The arrangement as an ml3czus-autoplayer sheet, to copy or save."""
        if not self.arrangement:
            return
        text, info = write_sheet(self.arrangement)
        note = tr('sheet.moved', count=info['moved']) if info['moved'] else ''
        dialog = self.sheet_dialog = SheetDialog(self.p, self.window, text, note)
        dialog.save_requested.connect(lambda: self.export_file('sheet'))
        dialog.show()

    def export_report(self):
        if not self.arrangement:
            return
        default = str(Path(self._last_dir or '.')/(Path(self.song.path).stem+'_report.json'))
        path, _ = QFileDialog.getSaveFileName(self.settings or self.window, tr('dialog.export_report'), default, 'JSON (*.json)')
        if path:
            try:
                write_report(self.arrangement, path, True)
            except Exception as exc:
                self._error(tr('error.title.export'), str(exc))

    # -- shutdown -------------------------------------------------------------
    def close(self):
        self.timer.stop()
        self.rebuild_timer.stop()
        self.worker.close()
        if self.listen:
            self.listen.close()
        self.audio.stop()
        self.player.stop()
        if self.settings:
            self.settings.close()
        if self.sheet_dialog:
            self.sheet_dialog.close()


def run():
    from .app import application, install_translation
    preferences = Preferences()
    i18n.start(preferences.get_ui().get('language'), preferences.language_folder())
    app = application()
    install_translation(app, i18n.current)
    controller = Controller(preferences)
    controller.show()
    app.exec()
    if controller.player.thread and controller.player.thread.is_alive():
        controller.player.stop(timeout=1.)
    if controller.restart_requested:
        # The same command line (launch.py, or python -m guitar_player), after this
        # process has released its keys and hotkeys.
        subprocess.Popen([sys.executable, *sys.orig_argv[1:]])

