from dataclasses import replace
import json
from pathlib import Path
import time
import unittest
from unittest.mock import MagicMock, patch

from guitar_player.arrange import arrange
from guitar_player.engine import continuity_lines, structural_stats
from guitar_player.export import reference_events
from guitar_player.model import Config, Fingering, Strike, VoiceNote
from guitar_player.playback import KeyboardDriver, Player
from guitar_player.timing import PlaybackTimer
from tests.test_core import FakeKeyboard, note, song, validate


def outputs(a):
    return {f.voice.note.id:(s.time,f.voice.pitch,f.string,f.fret)
            for s in a.strikes for f in s.notes}


class ContentTests(unittest.TestCase):
    def test_defaults_never_invoke_melody_or_bass_extraction(self):
        s=song([note(i,p,i*.05,source=f'{i%2}:{i%2}') for i,p in enumerate([60,72,64,55,68,74])])
        with patch('guitar_player.guitarize.guitarize',side_effect=AssertionError('guitarize called')), \
             patch('guitar_player.guitarize.role_probabilities',side_effect=AssertionError('role guessing called')):
            for mode in ('raw','full','auto'):
                a=arrange(s,Config(mode=mode));validate(a)
                self.assertEqual(a.stats['played_notes'],len(s.notes))
                self.assertFalse(a.stats['uses_voice_extraction'])
                self.assertFalse(a.melody_ids)

    def test_quiet_middle_voice_and_lower_arpeggio_are_not_removed(self):
        notes=[]
        for i in range(8):
            notes.extend([note(i*3,48+i%3,i*.2,source='0:0',velocity=100),
                          note(i*3+1,64+i%4,i*.2,source='1:1',velocity=25),
                          note(i*3+2,76+i%3,i*.2,source='2:2',velocity=110)])
        a=arrange(song(notes));validate(a)
        self.assertEqual(a.stats['played_notes'],24)
        self.assertEqual(a.stats['retention'],1)
        self.assertNotIn('arrangement',a.stats['drop_reasons'])
        self.assertNotIn('duet',str(a.stats['recommendation']['scores']))
        self.assertNotIn('melody',str(a.stats['recommendation']['scores']))

    def test_role_settings_and_names_do_not_bias_auto(self):
        s=song([note(i,p,i*.2,source=f'{i%2}:{i%2}') for i,p in enumerate([50,80,52,78,54,76])])
        a=arrange(s)
        s.sources=[replace(source,name='melody' if i else 'bass') for i,source in enumerate(s.sources)]
        b=arrange(s,Config(melody_source='invalid ignored role',bass_source='0:0'))
        self.assertEqual(outputs(a),outputs(b))

    def test_octave_doubling_survives_fold_collision(self):
        a=arrange(song([note(i,p) for i,p in enumerate([40,52,64])]),Config(mode='full',octave=0))
        validate(a)
        self.assertEqual(a.stats['played_notes'],3)
        pitches=[outputs(a)[i][1] for i in range(3)]
        self.assertEqual(pitches,sorted(set(pitches)))

    def test_polyphonic_lines_preserve_both_directions_at_boundary(self):
        ns=[]
        for i,(lo,hi) in enumerate(zip([52,50,48],[82,84,85])):
            ns.extend([note(i*2,lo,i*.3,.4),note(i*2+1,hi,i*.3,.4)])
        a=arrange(song(ns),Config(mode='full',octave=0))
        out=outputs(a)
        self.assertEqual(a.stats['played_notes'],6)
        self.assertEqual([out[i*2][1] for i in range(3)],[52,50,48])
        upper=[out[i*2+1][1] for i in range(3)]
        self.assertEqual([upper[1]-upper[0],upper[2]-upper[1]],[2,1])

    def test_user_simplification_preserves_onsets_and_is_optional(self):
        ns=[note(i*6+j,p,i*.5) for i in range(4) for j,p in enumerate([48,52,55,60,64,67])]
        complete=arrange(song(ns))
        simplified=arrange(song(ns),Config(simplify=1))
        self.assertEqual(complete.stats['played_notes'],24)
        self.assertLess(simplified.stats['played_notes'],24)
        self.assertEqual(simplified.stats['onset_retention'],1)
        self.assertGreater(simplified.stats['drop_reasons']['user_simplification'],0)
        validate(simplified)


class PhysicalTests(unittest.TestCase):
    def test_calibration_matches_all_recorded_reference_pitches(self):
        path=Path(__file__).resolve().parent/'fixtures/guitar_calibration.json'
        observed=json.loads(path.read_text(encoding='utf-8'))['baseline']
        self.assertTrue(all(round(n['observed_midi'])==n['script_pitch']+Config().tuning_offset for n in observed))
        cfg=Config(mode='raw')
        a=arrange(song([note(i,p,i*.2) for i,p in enumerate(cfg.tuning)]),cfg)
        self.assertEqual([(outputs(a)[i][2],outputs(a)[i][3]) for i in range(6)],[(i,0) for i in range(6)])
        self.assertEqual(cfg.bounds,(41,84))
        legacy=arrange(song([note(0,40)]),replace(cfg,tuning_offset=0))
        self.assertEqual(outputs(legacy)[0][2:],(0,0))

    def test_more_than_six_notes_are_scheduled_without_voice_cap(self):
        ns=[note(i,p) for i,p in enumerate([48,50,52,55,57,60,62,64,67,69,72,76])]
        a=arrange(song(ns),Config(mode='raw'))
        validate(a)
        self.assertEqual(a.stats['played_notes'],12)
        self.assertEqual(a.stats['rolled_groups_over_six'],1)
        self.assertLessEqual(a.stats['max_delay_ms'],80)

    def test_scarce_string_low_notes_are_rolled_not_silently_dropped(self):
        a=arrange(song([note(0,41),note(1,42),note(2,46),note(3,84)]),Config(mode='raw'))
        validate(a)
        self.assertEqual(a.stats['played_notes'],4)
        self.assertNotEqual(outputs(a)[0][0],outputs(a)[1][0])

    def test_no_hard_50ms_cooldown(self):
        for interval in (.02,.035):
            cfg=Config(mode='raw',hold=.01,gap=0,repeat_interval=0)
            a=arrange(song([note(i,41,i*interval,.1) for i in range(8)]),cfg)
            validate(a)
            self.assertEqual(a.stats['played_notes'],8)
            self.assertAlmostEqual(a.stats['max_delay_ms'],0)
        self.assertEqual(Config().repeat_interval,0)

    def test_optional_repeat_interval_applies_across_strings(self):
        a=arrange(song([note(i,65,i*.02,.2) for i in range(3)]),
                  Config(mode='raw',hold=.01,repeat_interval=.05))
        validate(a)
        times=sorted(s.time for s in a.strikes)
        self.assertEqual(len(times),3)
        self.assertTrue(all(b-a>=.05-1e-9 for a,b in zip(times,times[1:])))

    def test_reference_does_not_cut_old_pitch_on_string_reuse(self):
        a=arrange(song([note(0,41,0,1),note(1,42,.12,1)]),Config(mode='raw'))
        ons=[(t,m.note) for t,_,m in reference_events(a) if m.type=='note_on']
        offs={m.note:t for t,_,m in reference_events(a) if m.type=='note_off'}
        self.assertEqual(ons,[(0,41),(.12,42)])
        self.assertGreater(offs[41],.12)
        self.assertAlmostEqual(offs[41],1.)

    def test_busy_string_does_not_block_an_independent_runtime_note(self):
        class SlowBackend(FakeKeyboard):
            def press(self,key):
                super().press(key)
                if key=='z':time.sleep(.018)
        cfg=Config(mode='raw',hold=.04,max_delay=.2)
        a=arrange(song([note(0,41,.02,.2),note(1,42,.06,.2),note(2,65,.065,.2)]),cfg)
        backend=SlowBackend();p=Player(KeyboardDriver(backend),focus_check=lambda:True)
        p.play(a,countdown=0)
        p.thread.join(2)
        self.assertEqual(p.state.status,'finished')
        keys=[key for action,key,_ in backend.events if action=='press' and key in 'zxcvbn']
        self.assertEqual(keys,['z','n','z'])
        self.assertFalse(backend.held)


class TimerTests(unittest.TestCase):
    def test_resolution_is_restored_even_after_playback_error(self):
        api=MagicMock()
        api.timeBeginPeriod.return_value=0
        with patch('guitar_player.timing.os.name','nt'), \
             patch('guitar_player.timing.ctypes.WinDLL',return_value=api,create=True):
            with self.assertRaisesRegex(RuntimeError,'playback failed'):
                with PlaybackTimer():
                    raise RuntimeError('playback failed')
        api.timeBeginPeriod.assert_called_once_with(1)
        api.timeEndPeriod.assert_called_once_with(1)

    def test_failed_request_does_not_release_unacquired_resolution(self):
        api=MagicMock()
        api.timeBeginPeriod.return_value=97
        with patch('guitar_player.timing.os.name','nt'), \
             patch('guitar_player.timing.ctypes.WinDLL',return_value=api,create=True):
            with PlaybackTimer():pass
        api.timeBeginPeriod.assert_called_once_with(1)
        api.timeEndPeriod.assert_not_called()


if __name__=='__main__':unittest.main()
