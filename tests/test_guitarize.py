"""吉他化: chord skeleton, melody line, playable subsets, the mode itself and old-setting migration."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from guitar_player import guitarize, harmony
from guitar_player.arrange import arrange
from guitar_player.i18n import tr
from guitar_player.model import MENU_MODES, Config
from guitar_player.prefs import Preferences
from guitar_player.roles import onset_groups
from tests.test_core import note, song, validate


class HarmonyTests(unittest.TestCase):
    def test_triads_and_functions(self):
        notes = ([note(i, p, 0., 1.) for i, p in enumerate([48, 52, 55, 60])]          # C major
                 + [note(4+i, p, 1., 1.) for i, p in enumerate([45, 48, 52, 57])])    # A minor
        found = harmony.chords(notes)
        self.assertEqual([(c.root, c.quality) for c in found], [(0, 'maj'), (9, 'min')])
        f = harmony.functions(notes, found)
        self.assertEqual(list(f[:4]), [harmony.ROOT, harmony.THIRD, harmony.FIFTH, harmony.ROOT])


class LineTests(unittest.TestCase):
    def test_melody_line_follows_the_likely_and_smooth_notes(self):
        notes = [note(0, 72, 0.), note(1, 48, 0.), note(2, 74, .5), note(3, 50, .5), note(4, 76, 1.), note(5, 52, 1.)]
        melodic = np.array([.9, .1, .8, .2, .9, .1])
        line = guitarize.melody_line(notes, onset_groups(notes), melodic)
        self.assertEqual(sorted(line), [0, 2, 4])

    def test_gated_line_takes_the_top_note_unless_the_model_is_sure(self):
        # Onset 0: the model slightly prefers the inner note -> the top note is the melody.
        # Onset 1: the model is confident about the inner note -> it may replace the top note.
        # Onset 2: low P(melodic) everywhere, but the lean still keeps a melody note there.
        notes = [note(0, 76, 0.), note(1, 72, 0.), note(2, 79, 1.), note(3, 74, 1.), note(4, 77, 2.), note(5, 72, 2.)]
        melodic = np.array([.4, .6, .05, .95, .3, .1])
        line = guitarize.gated_line(notes, onset_groups(notes), melodic)
        self.assertEqual(sorted(line), [0, 3, 4])

    def test_dense_melody_thins_with_simplify_only(self):
        # A phrase of sixteenth notes (4 per beat) over 4 beats, then a held note after a rest.
        notes = [note(i, 60+(i % 5), i/4, .25) for i in range(16)]+[note(16, 72, 6., 2.)]
        melody = set(range(17))
        self.assertEqual(guitarize.thin_melody(notes, melody, 0.)[0], melody)        # 简化 0: the whole melody
        kept, cut = guitarize.thin_melody(notes, melody, 1.)
        self.assertEqual(kept | cut, melody)
        self.assertTrue({0, 16} <= kept)                                              # phrase starts stay
        beats = sorted(notes[i].beat for i in kept)
        cap = guitarize.melody_cap(1.)*guitarize.THIN_WINDOW
        self.assertTrue(all(sum(a <= b < a+guitarize.THIN_WINDOW for b in beats) <= cap for a in beats))
        half, _ = guitarize.thin_melody(notes, melody, .5)
        self.assertGreater(len(half), len(kept))                                      # more 简化, fewer notes

    def test_octave_placement_follows_the_melody_median(self):
        base = dict(chord_coverage=1., texture_distance=0., octave_moved=0.)
        near = guitarize.candidate_score(dict(base, melody_median=guitarize.MELODY_TARGET+1, octave_moved=1.))
        high = guitarize.candidate_score(dict(base, melody_median=guitarize.MELODY_TARGET+11))
        self.assertGreater(near, high)                  # even though the higher octave (as written) moves no notes

    def test_placements_need_distinct_strings_within_the_span(self):
        tuning = Config().tuning
        self.assertTrue(guitarize.placements([60, 64, 67], tuning))
        self.assertFalse(guitarize.placements([41, 84], tuning))              # 3+ octaves apart: no hand shape
        self.assertFalse(guitarize.placements([60]*7, tuning))

    def test_simplify_lowers_the_texture_limits(self):
        k0, b0 = guitarize.texture_limits(Config(simplify=0.))
        k1, b1 = guitarize.texture_limits(Config(simplify=1.))
        self.assertGreater(k0, k1)
        self.assertGreater(b0, b1)


class ModeTests(unittest.TestCase):
    def test_cached_features_without_pytorch_fall_back_to_the_rule(self):
        s = song([note(i*2+j, p, i*.5) for i in range(8) for j, p in enumerate((48+i % 3, 72+i % 4))])

        def no_torch(*_):
            raise ImportError("No module named 'torch'")
        features = lambda song_, notes: (mock.Mock(ids=np.array([n.id for n in notes])), np.zeros((len(notes), 768), np.float32), {})
        with mock.patch.object(guitarize, 'get_features', features), mock.patch.object(guitarize, 'predict_roles', no_torch):
            _, backend = guitarize.role_probabilities(s, s.notes)
        self.assertEqual(backend, 'rule')

    def piece(self):
        notes = []
        for bar in range(8):
            t = bar*1.
            notes += [note(len(notes), 72+bar % 3, t, .5, '0:0'), note(len(notes)+1, 64, t, .5, '0:0'),
                      note(len(notes)+2, 60, t, .5, '0:0'), note(len(notes)+3, 48, t, 1., '1:1'),
                      note(len(notes)+4, 55, t+.5, .5, '1:1'), note(len(notes)+5, 52, t+.5, .5, '1:1')]
        return song(notes)

    def test_guitar_mode_keeps_the_melody_and_thins_the_rest(self):
        s = self.piece()
        a = arrange(s, Config(mode='guitar', melody_source='0:0'))      # track hint: the rule, no model needed
        validate(a)
        info = a.stats['guitarize']
        self.assertEqual(info['melody_kept'], info['melody'])
        self.assertLess(a.stats['played_notes'], len(s.notes))
        self.assertIn('guitarize', a.stats['drop_reasons'])
        self.assertEqual(a.stats['recommendation']['chosen'], 'guitar')
        self.assertTrue(a.stats['uses_voice_extraction'])
        melody = {f.voice.note.id for st in a.strikes for f in st.notes if f.voice.role == 'melody'}
        self.assertLessEqual({n.id for n in s.notes if n.pitch >= 72}, melody)   # rests filled by the top note

    def test_menu_offers_three_modes(self):
        self.assertEqual([tr('mode.'+m) for m in MENU_MODES], ['还原', '吉他化', '原样演奏'])

    def test_old_saved_modes_migrate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'prefs.json'
            path.write_text(json.dumps({'a': {'mode': 'duet', 'hold': .03}, 'b': {'mode': 'melody'},
                                        'c': {'mode': 'full', 'speed': 1.5}}), encoding='utf-8')
            prefs = Preferences(path)
            self.assertEqual((prefs.get('a').mode, prefs.get('a').hold), ('guitar', .03))
            self.assertEqual(prefs.get('b').mode, 'guitar')
            self.assertEqual((prefs.get('c').mode, prefs.get('c').speed), ('auto', 1.5))


if __name__ == '__main__':
    unittest.main()
