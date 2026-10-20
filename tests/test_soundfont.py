"""SF2 / SF3 sound banks in the listening preview (guitar_player.soundfont), with a
tiny bank built in code (tests/sf2_fixture.py). No audio device is opened."""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import mido
import numpy as np

from guitar_player import sample_preview
from guitar_player.sample_preview import CUSTOM, SamplePreview, TimbrePreview
from guitar_player.sampler import RATE, PackError
from guitar_player.soundfont import SoundFont, is_soundfont
from tests import sf2_fixture
from tests.test_sampler import FakeOutput

HAVE_TSF = importlib.util.find_spec('tinysoundfont') is not None


def on(t, note, channel=0, velocity=100):
    return (t, 1, mido.Message('note_on', channel=channel, note=note, velocity=velocity))


def off(t, note, channel=0):
    return (t, 0, mido.Message('note_off', channel=channel, note=note))


def pitch(audio):
    spectrum = np.abs(np.fft.rfft(audio[:, 0]*np.hanning(len(audio))))
    return np.argmax(spectrum)*RATE/len(audio)


def rms(audio):
    return float(np.sqrt((audio**2).mean())) if len(audio) else 0.


@unittest.skipUnless(HAVE_TSF, 'tinysoundfont not installed')
class SoundFontTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = sf2_fixture.write(folder/'bank.sf2')
        self.font = SoundFont(self.path)

    def test_presets_and_guitar_default(self):
        self.assertEqual(self.font.presets, [(0, 0, 'Test Piano'), (0, 25, 'Test Guitar')])
        self.assertEqual(self.font.preset, (0, 25))
        self.assertEqual(self.font.preset_name, 'Test Guitar')
        self.font.select((0, 0))
        self.assertEqual(self.font.preset, (0, 0))
        self.font.select((3, 7))
        self.assertEqual(self.font.preset, (0, 25))

    def test_first_preset_without_a_guitar(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = sf2_fixture.write(folder/'organ.sf2', presets=((0, 16, 'Organ'), (8, 4, 'Keys')))
        self.assertEqual(SoundFont(path).preset, (0, 16))

    def test_notes_play_their_pitch_loop_and_release(self):
        events = [on(.1, 69), off(1.1, 69), on(.1, 81, channel=3), off(.4, 81, channel=3)]
        r = self.font.renderer(events, int(3*RATE))
        audio = np.concatenate([r.block(f, 4096) for f in range(0, int(2*RATE), 4096)])
        self.assertLess(rms(audio[:int(.09*RATE)]), 1e-4)                   # nothing before the notes
        self.assertAlmostEqual(pitch(audio[int(.5*RATE):int(1.05*RATE)]), 441, delta=3)
        # The sample is 0.5 s long; the bank loops it while the key is held.
        self.assertGreater(rms(audio[int(.8*RATE):int(1.05*RATE)]), .5*rms(audio[int(.15*RATE):int(.35*RATE)]))
        self.assertLess(rms(audio[int(1.6*RATE):int(2*RATE)]), 1e-3)          # released
        both = audio[int(.15*RATE):int(.35*RATE)]
        spectrum = np.abs(np.fft.rfft(both[:, 0]))
        freqs = np.fft.rfftfreq(len(both), 1/RATE)
        self.assertGreater(spectrum[np.argmin(abs(freqs-882))], .2*spectrum.max())   # key 81, one octave up

    def test_seeking_restarts_held_notes_and_matches_continuous_play(self):
        events = [on(.1, 69), off(1.1, 69)]
        r = self.font.renderer(events, int(2*RATE))
        later = r.block(int(.6*RATE), 4096)                 # jump into the held note
        self.assertGreater(rms(later), .02)
        self.assertAlmostEqual(pitch(later), 441, delta=12)
        quiet = r.block(int(1.6*RATE), 4096)                # jump past its release
        self.assertLess(rms(quiet), 1e-3)
        a = self.font.renderer(events, int(2*RATE))
        first = np.concatenate([a.block(f, 1024) for f in range(0, 8192, 1024)])
        b = self.font.renderer(events, int(2*RATE))
        again = np.concatenate([b.block(f, 2048) for f in range(0, 8192, 2048)])
        np.testing.assert_allclose(first, again, atol=1e-6)   # block size does not change the sound

    def test_bad_files(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with self.assertRaises(PackError):
            SoundFont(folder/'missing.sf2')
        junk = folder/'junk.sf2'
        junk.write_bytes(b'not a soundfont')
        with self.assertRaises(PackError):
            SoundFont(junk)

    def test_preview_and_timbre_switch(self):
        self.assertTrue(is_soundfont(self.path) and is_soundfont('X.SF3') and not is_soundfont('a.sfz'))

        class Arrangement:
            duration = 1.
        events = [on(.2, 69), off(.8, 69)]
        with patch.object(sample_preview, 'reference_events', lambda a, original=False: events):
            preview = SamplePreview(self.path, output=FakeOutput)
            preview.play(Arrangement(), start=0.)
            preview.thread.join(5)
            self.assertEqual(preview.error, '')
            audio = np.concatenate(FakeOutput.instances[-1].frames)
            self.assertGreater(rms(audio[int(.3*RATE):int(.7*RATE)]), .02)
        timbre = TimbrePreview(midi=MagicMock(position=0.))
        self.assertIsNone(timbre.soundfont)
        timbre.set_timbre(CUSTOM, self.path)
        self.assertIsInstance(timbre.soundfont, SoundFont)
        timbre.set_preset((0, 0))
        self.assertEqual(timbre.soundfont.preset, (0, 0))


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(HAVE_TSF, 'tinysoundfont not installed')
class VolumeTests(unittest.TestCase):
    def setUp(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = sf2_fixture.write(folder/'bank.sf2')

    def test_auto_gain_brings_the_probe_to_the_game_level(self):
        preview = SamplePreview(self.path, output=FakeOutput)
        auto = preview.set_volume()
        level = sample_preview.loudness(preview.pack)
        self.assertAlmostEqual(level*10**(auto/20), sample_preview.GAME_LOUDNESS, delta=1e-4)
        self.assertAlmostEqual(preview.gain, 10**(auto/20))
        self.assertAlmostEqual(preview.set_volume(3.), auto)       # the automatic value is still reported
        self.assertAlmostEqual(preview.gain, 10**(3/20))           # but the hand-set volume is used

    def test_quiet_instrument_is_raised_within_range(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = sf2_fixture.write(folder/'quiet.sf2', amplitude=300)
        preview = SamplePreview(path, output=FakeOutput)
        self.assertEqual(preview.set_volume(), sample_preview.AUTO_RANGE[1])

    def test_probe_does_not_disturb_a_playing_renderer(self):
        font = SoundFont(self.path)
        events = [on(.1, 69), off(1.5, 69)]
        r = font.renderer(events, int(2*RATE))
        for f in range(0, int(.5*RATE), 2048):
            r.block(f, 2048)
        sample_preview._loudness.clear()
        sample_preview.loudness(font)              # uses the same synthesizer
        f = (int(.5*RATE)//2048+1)*2048
        after = r.block(f, 4096)
        self.assertGreater(rms(after), .02)
        self.assertAlmostEqual(pitch(after), 441, delta=12)

    def test_limiter_is_transparent_below_the_knee(self):
        quiet = np.array([[.5, -.79]], np.float32)
        np.testing.assert_array_equal(sample_preview.limit(quiet.copy()), quiet)
        loud = sample_preview.limit(np.array([[1.5, -3.]], np.float32))
        self.assertTrue(np.all(np.abs(loud) <= 1) and np.all(np.abs(loud) > sample_preview.KNEE))
