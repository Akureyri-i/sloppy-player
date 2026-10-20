"""改善吞音现象 (Config.end_guard): same-pitch re-presses kept out of the moment the
previous note of that pitch ends by itself (engine.avoid_end_retrigger)."""
import json
import tempfile
import unittest
from pathlib import Path

from guitar_player.arrange import arrange
from guitar_player.engine import END_GUARD, avoid_end_retrigger
from guitar_player.model import GAME_SOUND_LENGTH, NOTE_MIN, NOTE_MAX, Config, Fingering, Strike, VoiceNote
from guitar_player.prefs import Preferences
from tests.test_core import note, song

L66 = GAME_SOUND_LENGTH[66]


def strike(i, t, pitch, string, config=Config()):
    f = Fingering(VoiceNote(note(i, pitch, t), pitch, 'content', 1.), string, pitch-config.tuning[string])
    return Strike(t, t, (f,), config.hold)


def times(strikes, pitch):
    return [s.time for s in strikes for f in s.notes if f.voice.pitch == pitch]


class RuleTests(unittest.TestCase):
    def test_every_game_pitch_has_a_length(self):
        self.assertEqual(sorted(GAME_SOUND_LENGTH), list(range(NOTE_MIN, NOTE_MAX+1)))

    def test_moves_to_the_nearer_safe_edge(self):
        c = Config()
        for offset, expected in ((.03, L66+END_GUARD[1]), (0., L66-END_GUARD[0]), (-.01, L66-END_GUARD[0])):
            out, info = avoid_end_retrigger([strike(0, 0., 66, 3), strike(1, L66+offset, 66, 3)], c)
            self.assertAlmostEqual(times(out, 66)[1], expected, places=6)
            self.assertEqual((info['moved'], info['unavoidable']), (1, 0))

    def test_leaves_safe_presses_alone(self):
        c = Config()
        for offset in (-END_GUARD[0]-.005, END_GUARD[1]+.005, -.5, .4):
            strikes = [strike(0, 0., 66, 3), strike(1, L66+offset, 66, 3)]
            out, info = avoid_end_retrigger(strikes, c)
            self.assertEqual(out, strikes)
            self.assertEqual(info['moved'], 0)

    def test_other_string_and_other_pitches(self):
        c = Config()
        # Same pitch on another string is the same game sample: still moved.
        out, info = avoid_end_retrigger([strike(0, 0., 66, 3), strike(1, L66+.02, 66, 2)], c)
        self.assertEqual(info['moved'], 1)
        # A different pitch at that moment is unaffected.
        strikes = [strike(0, 0., 66, 3), strike(1, L66+.02, 67, 3)]
        self.assertEqual(avoid_end_retrigger(strikes, c)[0], strikes)

    def test_only_the_note_leaves_its_chord(self):
        c = Config()
        a = strike(0, 0., 66, 3)
        chord_notes = (strike(1, L66+.03, 66, 3).notes[0], strike(2, L66+.03, 59, 1).notes[0])
        chord = Strike(L66+.03, L66+.03, chord_notes, c.hold)
        out, info = avoid_end_retrigger([a, chord], c)
        self.assertEqual(info['moved'], 1)
        self.assertEqual(times(out, 59), [L66+.03])
        self.assertAlmostEqual(times(out, 66)[1], L66+END_GUARD[1], places=6)
        self.assertEqual(sum(len(s.notes) for s in out), 3)

    def test_crowded_string_uses_the_other_edge_or_stays(self):
        c = Config()
        late = L66+END_GUARD[1]
        out, info = avoid_end_retrigger([strike(0, 0., 66, 3), strike(1, L66+.03, 66, 3), strike(2, late, 70, 3)], c)
        self.assertAlmostEqual(times(out, 66)[1], L66-END_GUARD[0], places=6)
        early = L66-END_GUARD[0]
        strikes = [strike(0, 0., 66, 3), strike(3, early, 68, 3), strike(1, L66+.03, 66, 3), strike(2, late, 70, 3)]
        out, info = avoid_end_retrigger(strikes, c)
        self.assertEqual((info['moved'], info['unavoidable']), (0, 1))
        self.assertEqual(times(out, 66), [0., L66+.03])

    def test_chains_use_the_moved_time(self):
        c = Config()
        second = L66+.03
        moved = L66+END_GUARD[1]
        # The third press is safe after the original second press, not after the moved one.
        third = second+L66+END_GUARD[1]+.005
        self.assertTrue(L66-END_GUARD[0] < third-moved < L66+END_GUARD[1])
        out, info = avoid_end_retrigger([strike(0, 0., 66, 3), strike(1, second, 66, 3), strike(2, third, 66, 3)], c)
        self.assertEqual(info['moved'], 2)
        t = times(out, 66)
        for a, b in zip(t, t[1:]):
            self.assertFalse(L66-END_GUARD[0]+1e-6 < b-a < L66+END_GUARD[1]-1e-6, t)


class ArrangementTests(unittest.TestCase):
    def piece(self):
        return song([note(0, 66, 0., .3), note(1, 66, L66+.03, .3), note(2, 59, L66+.03, .3), note(3, 64, 5., .3)])

    def test_on_by_default_and_reported(self):
        a = arrange(self.piece(), Config(mode='raw'))
        self.assertEqual(a.stats['end_guard']['moved'], 1)
        self.assertTrue(a.stats['end_guard']['enabled'])
        self.assertEqual(sum(len(s.notes) for s in a.strikes), 4)

    def test_switch_off(self):
        a = arrange(self.piece(), Config(mode='raw', end_guard=False))
        self.assertEqual(a.stats['end_guard'], dict(enabled=False, moved=0, max_shift_ms=0., unavoidable=0))
        self.assertIn(L66+.03, times(a.strikes, 66))

    def test_config_rejects_non_bool(self):
        with self.assertRaises(ValueError):
            Config(end_guard=1)

    def test_older_saved_settings_turn_it_on(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'prefs.json'
            prefs = Preferences(path)
            prefs.save('song', Config(hold=.03, end_guard=False))
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertFalse(prefs.get('song').end_guard)
            del data['song']['end_guard']
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertTrue(prefs.get('song').end_guard)
            self.assertEqual(prefs.get('song').hold, .03)


if __name__ == '__main__':
    unittest.main()
