"""Qt front end behaviour with a mock keyboard backend and fake audio.

No window is shown, no key reaches any game, and preferences go to a
temporary file instead of %APPDATA%.
"""
import importlib.util
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from guitar_player.arrange import arrange
from guitar_player.model import Config
from guitar_player.playback import KeyboardDriver, Player
from guitar_player.prefs import Preferences
from guitar_player.qt import controller as controller_module
from guitar_player.qt.app import application
from guitar_player.qt.controller import Controller
from guitar_player.qt.panel import KEYBOARD
from guitar_player.qt.scoreview import KEYS, NOTES
from tests.test_core import note, song


class FakeAudio:
    def __init__(self):
        self.calls = []
        self.playing = False
        self.position = 0.
        self.error = ''

    def play(self, arrangement, start=0., duration=None, original=False):
        self.calls.append(('play', start, original))
        self.playing, self.position = True, start

    def stop(self):
        self.calls.append(('stop',))
        self.playing = False


def piece(seconds=8):
    notes = [note(i, 52+(i % 12), start=i*.25, duration=.2) for i in range(int(seconds*4))]
    return song(notes)


class ControllerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = application()

    def setUp(self):
        self._countdown = controller_module.COUNTDOWN
        controller_module.COUNTDOWN = 0.
        self.folder = tempfile.TemporaryDirectory()
        self.backend = MagicMock(spec=['press', 'release'])     # a one-key-per-call backend
        self.audio = FakeAudio()
        self.c = Controller(preferences=Preferences(Path(self.folder.name)/'prefs.json'),
                            player=Player(driver=KeyboardDriver(self.backend), focus_check=lambda: True),
                            audio=self.audio, global_hotkeys=False, focus_check=lambda: True,
                            arrange_fn=lambda s, c: arrange(s, replace(c, arranger='legacy')),
                            analyze_fn=self.fake_analysis, preload_model=False)
        self.analysis_calls = []
        self.song = piece()
        self.c.song, self.c.digest = self.song, 'digest'
        self.c._arranged(arrange(self.song, Config(arranger='legacy')))

    def fake_analysis(self, song, config):
        from guitar_player.engine import selected_notes
        self.analysis_calls.append(len(selected_notes(song, config)))
        time.sleep(.4)
        return None

    def tearDown(self):
        self.c.close()
        self.c.window.deleteLater()
        controller_module.COUNTDOWN = self._countdown
        self.folder.cleanup()

    def pump(self, seconds):
        end = time.perf_counter()+seconds
        while time.perf_counter() < end:
            self.app.processEvents()
            self.c._tick()
            time.sleep(.01)

    def test_loading_a_file_arranges_it(self):
        import mido
        midi = mido.MidiFile(ticks_per_beat=480)
        track = mido.MidiTrack()
        midi.tracks.append(track)
        for pitch in (60, 64, 67, 72):
            track.append(mido.Message('note_on', note=pitch, velocity=80, time=0))
            track.append(mido.Message('note_off', note=pitch, velocity=0, time=240))
        path = Path(self.folder.name)/'tiny.mid'
        midi.save(path)
        # A second request while the first is loading must win.
        self.c.load_file(str(Path(self.folder.name)/'missing.mid'))
        self.c.load_file(str(path))
        end = time.perf_counter()+30
        while time.perf_counter() < end and (self.c.busy or self.c.arrangement is None
                                             or self.c.arrangement.song.path != str(path)):
            self.pump(.05)
        self.assertEqual(self.c.arrangement.song.path, str(path))
        self.assertEqual(self.c.window.title.full, 'tiny')
        self.assertEqual(self.c.window.status.text(), '就绪')
        self.assertTrue(self.c.window.play_button.isEnabled())

    def test_simplify_tip_follows_the_mode(self):
        self.c.config = replace(self.c.config, mode='guitar')
        self.c._apply_config_to_ui()
        self.assertIn('50%', self.c.window.simplify_slider.toolTip())
        self.c.config = replace(self.c.config, mode='auto')
        self.c._apply_config_to_ui()
        self.assertIn('每个起音都保留', self.c.window.simplify_slider.toolTip())

    def wait_until_idle(self, limit=10):
        end = time.perf_counter()+limit
        while time.perf_counter() < end and self.c.busy:
            self.pump(.02)

    def high_piece(self):
        # Above the guitar's range on two tracks: needs the speed-independent analysis.
        return song([note(i, 90+(i % 5), start=i*.25, duration=.2, source=f'0:{i % 2}') for i in range(24)])

    def test_speed_change_does_not_restart_the_analysis(self):
        self.c.song = self.high_piece()
        self.c._analyzed = set()
        self.c.rebuild()
        self.pump(.1)
        self.c._edit(speed=1.5)
        self.c.rebuild()
        self.wait_until_idle()
        self.assertEqual(len(self.analysis_calls), 1)
        self.assertEqual(self.c.arrangement.config.speed, 1.5)
        # Later speed changes skip the analysis entirely.
        self.c._edit(speed=.8)
        self.c.rebuild()
        self.wait_until_idle()
        self.assertEqual(len(self.analysis_calls), 1)
        self.assertEqual(self.c.arrangement.config.speed, .8)

    def test_track_change_restarts_the_analysis(self):
        self.c.song = self.high_piece()
        self.c._analyzed = set()
        self.c.rebuild()
        self.pump(.1)
        self.c._edit(sources=('0:0',))
        self.c.rebuild()
        self.wait_until_idle()
        self.assertEqual(self.analysis_calls[-1], 12)
        self.assertEqual(self.c.arrangement.config.sources, ('0:0',))

    def test_in_range_song_needs_no_analysis(self):
        self.c.rebuild()
        self.wait_until_idle()
        self.assertEqual(self.analysis_calls, [])
        self.assertIn('按原样演奏', self.c.window.summary.text())

    def test_stop_returns_to_the_beginning(self):
        self.c._seek(2.)
        self.c.play()
        self.pump(.6)
        self.assertEqual(self.c.player.state.status, 'playing')
        self.assertGreater(self.c.window.panel.position, 2.3)
        self.c.stop()
        self.pump(.05)
        self.assertEqual(self.c.window.panel.position, 0.)
        self.assertEqual(self.c.start, 0.)
        self.assertTrue(self.backend.press.called)
        self.assertEqual(self.c.window.status.text(), '已停止')

    def test_end_of_piece_returns_to_the_beginning(self):
        short = song([note(i, 60+i, start=i*.1, duration=.1) for i in range(4)])
        self.c._arranged(arrange(short, Config(arranger='legacy')))
        self.c._seek(.2)
        self.c.play()
        self.pump(.9)
        self.assertEqual(self.c.window.status.text(), '演奏结束')
        self.assertEqual(self.c.window.panel.position, 0.)

    def test_pause_keeps_the_place(self):
        self.c.play()
        self.pump(.5)
        self.c.player.pause_resume()
        self.pump(.15)
        paused_at = self.c.window.panel.position
        self.pump(.3)
        self.assertEqual(self.c.player.state.status, 'paused')
        self.assertAlmostEqual(self.c.window.panel.position, paused_at, places=2)
        self.assertGreater(paused_at, .3)
        self.assertEqual(self.c.start, 0.)

    def test_dragging_during_a_performance_pauses_at_the_new_place(self):
        self.c.play()
        self.pump(.3)
        self.c.pause_resume()
        self.pump(.1)
        self.assertEqual(self.c.player.state.status, 'paused')
        self.c._seek(5.)
        self.pump(.1)
        self.assertEqual(self.c.window.panel.position, 5.)
        self.assertEqual(self.c.window.pause_button.text(), ' 继续')
        self.assertFalse(self.c.window.play_button.isEnabled())
        self.assertEqual(self.c.window.status.text(), '已暂停，按 F2 继续')
        self.assertEqual(self.c.start, 0.)
        # Roblox in front (F2 in the game): continue at once from the new place.
        self.c.pause_resume()
        self.pump(.3)
        self.assertEqual(self.c.player.state.status, 'playing')
        self.assertGreater(self.c.window.panel.position, 5.2)
        self.assertLess(self.c.window.panel.position, 5.6)

    def test_dragging_while_playing_holds_the_new_place(self):
        self.c.play()
        self.pump(.2)
        self.c._seek(6.)
        self.pump(.1)
        self.assertNotEqual(self.c.player.state.status, 'playing')
        self.assertEqual(self.c.window.panel.position, 6.)
        self.c.stop()
        self.assertEqual(self.c.window.panel.seekable, True)
        self.assertEqual(self.c.start, 0.)

    def test_continue_from_this_window_counts_down(self):
        controller_module.COUNTDOWN = .6
        self.c.focus_check = lambda: False   # this window is in front, not Roblox
        self.c.play()
        self.pump(.9)
        self.c.pause_resume()
        self.pump(.1)
        paused_at = self.c.window.panel.position
        self.c.pause_resume()
        self.pump(.05)
        self.assertEqual(self.c.player.state.status, 'countdown')
        self.assertIn('秒后继续', self.c.window.status.text())
        self.pump(.8)
        self.assertEqual(self.c.player.state.status, 'playing')
        self.assertGreater(self.c.window.panel.position, paused_at)
        self.assertLess(self.c.window.panel.position, paused_at+.6)

    def test_minimap_zoom_and_edges_change_the_detail_range(self):
        panel = self.c.window.panel
        panel.resize(900, 500)
        detail = panel.detail
        self.c.start = 4.
        self.pump(.05)
        before = detail.seconds_visible()
        panel.minimap.zoom.emit(2.)
        self.assertAlmostEqual(detail.seconds_visible(), before/2)
        panel.minimap.zoom.emit(1e-3)
        self.assertAlmostEqual(detail.seconds_visible(), self.c.arrangement.duration)
        panel.minimap.zoom.emit(1e3)
        self.assertAlmostEqual(detail.seconds_visible(), 1.)
        panel.minimap.edge.emit('right', 7.)
        self.assertAlmostEqual(detail.time_window()[1], 7.)
        # Each view keeps its own range.
        notes_span = detail.seconds_visible()
        panel.set_view(KEYS)
        panel.minimap.zoom.emit(1.5)
        panel.set_view(NOTES)
        self.assertAlmostEqual(detail.seconds_visible(), notes_span)

    def test_listening_keeps_its_own_position(self):
        self.c._seek(1.)
        self.c.open_listen()
        listen = self.c.listen
        listen.seek(5.)
        listen.play()
        self.assertEqual(self.audio.calls[-1], ('play', 5., False))
        self.pump(.05)
        self.assertEqual(self.c.start, 1.)
        self.assertEqual(self.c.window.panel.position, 1.)
        # Starting the game performance silences the listening window.
        self.c.play()
        self.assertFalse(self.audio.playing)
        self.c.stop()
        listen.set_position(6.)
        listen.close()
        self.app.processEvents()
        self.c.open_listen()
        self.assertEqual(self.c.listen.position, 6.)

    def test_speed_change_keeps_the_same_musical_start(self):
        self.c._seek(4.)
        self.c._arranged(arrange(self.song, Config(arranger='legacy', speed=2.)))
        self.assertAlmostEqual(self.c.start, 2.)

    def test_restore_defaults_only_when_arrangement_changed(self):
        self.assertTrue(self.c.window.restore_button.isHidden())
        self.c._edit(simplify=.5)
        self.assertFalse(self.c.window.restore_button.isHidden())
        self.c._edit(speed=1.5)
        self.c.restore_defaults()
        self.assertTrue(self.c.window.restore_button.isHidden())
        self.assertEqual(self.c.config.speed, 1.5)

    def test_window_shortcuts_follow_the_global_hotkey_setting(self):
        self.assertTrue(all(s.isEnabled() for s in self.c.shortcuts))
        self.c._hotkeys_toggled(True)
        self.assertFalse(any(s.isEnabled() for s in self.c.shortcuts))
        self.c.global_hotkeys = False

    def test_theme_and_view_are_saved_to_the_given_file(self):
        self.c.window.panel.set_view(KEYBOARD)
        self.c.set_theme('amber')
        saved = self.c.preferences.get_ui()
        self.assertEqual((saved['theme'], saved['view']), ('amber', KEYBOARD))
        self.assertTrue(str(self.c.preferences.path).startswith(self.folder.name))
        self.assertNotIn(os.environ.get('APPDATA', '<none>'), str(self.c.preferences.path))

    def test_settings_window_reflects_config(self):
        self.c._edit(hold=.03)
        self.c.open_settings()
        self.assertEqual(self.c.settings.hold.value(), 30)
        self.c.settings.tuning.setValue(2)
        self.assertEqual(self.c.config.tuning_offset, 2)
        self.assertIn('原谱', self.c.settings.details.toPlainText())
        self.c.settings.close()

    def test_end_guard_switch_in_settings(self):
        self.c.open_settings()
        box = self.c.settings.end_guard
        self.assertEqual(box.text(), '改善吞音现象（实验性）')
        self.assertTrue(box.isChecked())
        self.assertIn('改善吞音现象', self.c.settings.details.toPlainText())
        box.setChecked(False)
        self.assertFalse(self.c.config.end_guard)
        self.c.settings.close()

    def test_language_choice_is_saved_and_applies_after_a_restart(self):
        folder = self.c.preferences.language_folder()
        folder.mkdir()
        (folder/'de.json').write_text('{"_meta": {"name": "Deutsch"}, "main.open": "MIDI öffnen"}', encoding='utf-8')
        self.c.open_settings()
        s = self.c.settings
        self.assertEqual([s.language.itemData(i) for i in range(s.language.count())], ['id', 'de', 'en', 'es', 'pt-BR', 'ru', 'zh-CN', 'zh-TW'])   # by own name
        self.assertEqual(s.language.currentData(), 'zh-CN')
        self.assertFalse(s.restart_button.isVisibleTo(s))
        s.language_chosen.emit('de')
        self.assertEqual(self.c.preferences.get_ui()['language'], 'de')
        self.assertTrue(s.restart_button.isVisibleTo(s))
        self.assertEqual(self.c.window.open_button.text().strip(), '打开乐谱')   # unchanged until restarted
        s.language_chosen.emit('zh-CN')
        self.assertFalse(s.restart_button.isVisibleTo(s))
        s.restart_button.click()
        self.assertTrue(self.c.restart_requested)
        self.assertFalse(self.c.window.isVisible())

    def test_pasted_sheet_opens_and_the_arrangement_copies_out_as_one(self):
        self.c.paste_sheet()
        self.c.sheet_dialog.text.setPlainText('Key delay: 100\n[pvyx]--[;c]')
        self.c.sheet_dialog.open_button.click()
        self.c.config = replace(self.c.config, mode='raw')
        deadline = time.perf_counter()+10
        while (self.c.busy or not self.c.song or not self.c.song.fingering) and time.perf_counter() < deadline:
            self.pump(.05)
        self.assertEqual(self.c.window.title.full, '粘贴的乐谱')
        self.assertEqual(len(self.c.song.notes), 3)
        self.c.show_sheet()
        dialog = self.c.sheet_dialog
        self.assertTrue(dialog.text.toPlainText().startswith('Key delay: 31\n'))
        dialog.copy()
        self.assertEqual(self.app.clipboard().text(), dialog.text.toPlainText())
        target = Path(self.folder.name)/'sheet.txt'
        with patch.object(controller_module.QFileDialog, 'getSaveFileName', return_value=(str(target), '')):
            dialog.save_requested.emit()
        self.assertEqual(target.read_text(encoding='utf-8'), dialog.text.toPlainText())
        dialog.close()

    def test_export_menu_writes_midi_and_musicxml(self):
        for kind, name in (('midi', 'a.mid'), ('musicxml', 'a.musicxml')):
            target = Path(self.folder.name)/name
            with patch.object(controller_module.QFileDialog, 'getSaveFileName', return_value=(str(target), '')):
                getattr(self.c.window, 'export_'+kind).trigger()
            self.assertTrue(target.stat().st_size > 0)
        self.assertIn('a.musicxml', self.c.window.status.text())

    def test_settings_saved_with_chinese_names_still_load(self):
        self.c.preferences._write({'__ui__': {'theme': '琥珀', 'view': '按键', 'timbre': 'default'}})
        self.assertEqual(self.c.preferences.get_ui(), {'theme': 'amber', 'view': KEYS, 'timbre': 'default'})


if __name__ == '__main__':
    unittest.main()


class PanelLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = application()

    def test_view_switch_keeps_its_size_and_place_in_every_view(self):
        from PySide6.QtCore import Qt
        from guitar_player.qt.panel import VIEWS, ScorePanel
        from guitar_player.qt.theme import DEFAULT, palette
        panel = ScorePanel(palette(DEFAULT))
        panel.setAttribute(Qt.WA_DontShowOnScreen)
        panel.resize(1100, 500)
        panel.show()
        self.addCleanup(panel.deleteLater)
        a = arrange(piece(), Config(arranger='legacy'))
        from guitar_player.visual import VisualScore
        panel.set_arrangement(a, VisualScore.from_song(a.song, 1., a.selected), VisualScore.from_arrangement(a))
        places = set()
        for view in VIEWS:
            panel.set_view(view)
            self.app.processEvents()
            places.add(panel.view_switch.geometry().getRect())
        self.assertEqual(len(places), 1, places)


class FakeSamplePreview(FakeAudio):
    def __init__(self, path):
        if 'bad' in str(path):
            from guitar_player.sampler import PackError
            raise PackError('音色包缺少样本')
        super().__init__()
        self.pack = type('Pack', (), {'regions': [1]*120})()


class TimbreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = application()

    def make(self, folder):
        from guitar_player.sample_preview import TimbrePreview
        c = Controller(preferences=Preferences(Path(folder)/'prefs.json'),
                       player=Player(driver=KeyboardDriver(MagicMock()), focus_check=lambda: True),
                       audio=TimbrePreview(midi=FakeAudio(), sample_factory=FakeSamplePreview),
                       global_hotkeys=False, focus_check=lambda: True, preload_model=False)
        c._error = MagicMock()
        self.addCleanup(lambda: (c.close(), c.window.deleteLater()))
        return c

    def test_choice_is_remembered_and_shown(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        c = self.make(folder)
        c.pack_path = Path(folder)/'game.sfz'
        c.song, c.digest = piece(), 'digest'
        c._arranged(arrange(c.song, Config(arranger='legacy')))
        c.open_listen()
        c.open_settings()
        c.set_timbre('custom')
        self.assertEqual(c.audio.kind, 'custom')
        self.assertEqual(c.listen.timbre.value(), 'custom')
        self.assertEqual(c.settings.timbre.value(), 'custom')
        self.assertIn('120', c.settings.pack_status.text())
        c.settings.close()
        again = self.make(folder)
        self.assertEqual(again.audio.kind, 'custom')
        self.assertEqual(again.pack_path, Path(folder)/'game.sfz')

    def test_soundfont_bank_offers_its_instruments(self):
        from guitar_player import soundfont
        from guitar_player.sample_preview import TimbrePreview
        from tests import sf2_fixture
        if importlib.util.find_spec('tinysoundfont') is None:
            self.skipTest('tinysoundfont not installed')
        folder = self.enterContext(tempfile.TemporaryDirectory())
        bank = sf2_fixture.write(Path(folder)/'bank.sf2')
        c = Controller(preferences=Preferences(Path(folder)/'prefs.json'),
                       player=Player(driver=KeyboardDriver(MagicMock()), focus_check=lambda: True),
                       audio=TimbrePreview(midi=FakeAudio()), global_hotkeys=False,
                       focus_check=lambda: True, preload_model=False)
        c._error = MagicMock()
        self.addCleanup(lambda: (c.close(), c.window.deleteLater()))
        c.open_settings()
        self.assertTrue(c.settings.instrument_row.isHidden())
        with patch('guitar_player.qt.controller.QFileDialog.getOpenFileName', return_value=(str(bank), '')):
            c.choose_pack()
        self.assertEqual(c.audio.kind, 'custom')
        self.assertIsInstance(c.audio.soundfont, soundfont.SoundFont)
        box = c.settings.instrument
        self.assertFalse(c.settings.instrument_row.isHidden())
        self.assertEqual(box.count(), 2)
        self.assertEqual(box.currentData(), (0, 25))                 # the bank's guitar by default
        self.assertIn('2 种乐器', c.settings.pack_status.text())
        box.setCurrentIndex(0)
        box.activated.emit(0)
        self.assertEqual(c.audio.soundfont.preset, (0, 0))
        self.assertEqual(c.preferences.get_ui()['timbre_preset'], '0:0')
        c.settings.close()
        again = Controller(preferences=Preferences(Path(folder)/'prefs.json'),
                           player=Player(driver=KeyboardDriver(MagicMock()), focus_check=lambda: True),
                           audio=TimbrePreview(midi=FakeAudio()), global_hotkeys=False,
                           focus_check=lambda: True, preload_model=False)
        self.addCleanup(lambda: (again.close(), again.window.deleteLater()))
        self.assertEqual((again.audio.kind, again.audio.soundfont.preset), ('custom', (0, 0)))

    def test_volume_is_matched_and_trimmed_per_instrument(self):
        from guitar_player.sample_preview import TimbrePreview
        from tests import sf2_fixture
        if importlib.util.find_spec('tinysoundfont') is None:
            self.skipTest('tinysoundfont not installed')
        folder = self.enterContext(tempfile.TemporaryDirectory())
        bank = sf2_fixture.write(Path(folder)/'bank.sf2')
        c = Controller(preferences=Preferences(Path(folder)/'prefs.json'),
                       player=Player(driver=KeyboardDriver(MagicMock()), focus_check=lambda: True),
                       audio=TimbrePreview(midi=FakeAudio()), global_hotkeys=False,
                       focus_check=lambda: True, preload_model=False)
        c._error = MagicMock()
        self.addCleanup(lambda: (c.close(), c.window.deleteLater()))
        c.open_settings()
        s = c.settings
        self.assertTrue(s.volume_row.isHidden())                    # default MIDI timbre: no trim
        with patch('guitar_player.qt.controller.QFileDialog.getOpenFileName', return_value=(str(bank), '')):
            c.choose_pack()
        self.assertFalse(s.volume_row.isHidden())
        auto = c.auto_db
        # The slider shows the volume in use: the automatic match.
        self.assertTrue(s.auto_volume.isChecked())
        self.assertEqual(s.volume.value(), round(auto*2))
        self.assertEqual(s.volume_value.text(), f'{auto:+.1f} dB')
        self.assertAlmostEqual(c.audio.current.gain, 10**(auto/20))
        # Moving it sets the volume by hand: the automatic match is unticked.
        s.volume.setValue(round(auto*2)+2)
        self.assertFalse(s.auto_volume.isChecked())
        manual = round(auto*2)/2+1
        self.assertAlmostEqual(c.audio.current.gain, 10**(manual/20))
        self.assertEqual(c.preferences.get_volumes()[f'{bank}|0:25'], manual)
        # Each instrument keeps its own: another one is still automatic.
        c.set_preset((0, 0))
        self.assertTrue(s.auto_volume.isChecked())
        c.set_preset((0, 25))
        self.assertFalse(s.auto_volume.isChecked())
        self.assertEqual(s.volume.value(), round(manual*2))
        # Ticking it again goes back to the automatic value.
        s.auto_volume.setChecked(True)
        self.assertEqual(s.volume.value(), round(auto*2))
        self.assertAlmostEqual(c.audio.current.gain, 10**(auto/20))
        self.assertNotIn(f'{bank}|0:25', c.preferences.get_volumes())
        # Unticking keeps the volume where it is, now held by hand.
        s.auto_volume.setChecked(False)
        self.assertAlmostEqual(c.audio.current.gain, 10**(auto/20))
        self.assertAlmostEqual(c.preferences.get_volumes()[f'{bank}|0:25'], auto)
        s.close()

    def test_bad_pack_keeps_the_default_and_says_why(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        c = self.make(folder)
        c.pack_path = Path(folder)/'bad.sfz'
        c.set_timbre('custom')
        self.assertEqual(c.audio.kind, 'default')
        c._error.assert_called_once()
        self.assertIn('缺少样本', c._error.call_args.args[1])
