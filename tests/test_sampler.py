"""SFZ sampler, sample-pack preview and timbre switching. No audio device is used."""
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import wave

import mido
import numpy as np

from guitar_player import sample_preview
from guitar_player.sampler import RATE, Pack, PackError, Renderer, parse_key, parse_sfz, plan, read_wav
from guitar_player.sample_preview import CUSTOM, DEFAULT, GAME_PACK, SamplePreview, TimbrePreview


def write_wav(path, seconds=.5, value=.5, rate=RATE, channels=2):
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(channels)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes((np.full((int(seconds*rate), channels), value, np.float32)*32767).astype('<i2').tobytes())


def riff(kind, bits, channels, rate, frames, extensible=False):
    """A WAV file's bytes with a hand-written fmt chunk."""
    block = channels*bits//8
    fmt = struct.pack('<HHIIHH', 0xFFFE if extensible else kind, channels, rate, rate*block, block, bits)
    if extensible:
        fmt += struct.pack('<HHI', 22, bits, 0) + struct.pack('<H', kind) + bytes(14)
    body = b'WAVE' + b'fmt ' + struct.pack('<I', len(fmt)) + fmt + b'data' + struct.pack('<I', len(frames)) + frames
    return b'RIFF' + struct.pack('<I', len(body)) + body


def on(t, note, channel=0, velocity=100):
    return (t, 1, mido.Message('note_on', channel=channel, note=note, velocity=velocity))


def off(t, note, channel=0):
    return (t, 0, mido.Message('note_off', channel=channel, note=note))


class WavTests(unittest.TestCase):
    def read(self, data):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder/'a.wav').write_bytes(data)
        return read_wav(folder/'a.wav')

    def test_sample_formats(self):
        values = np.array([0, .5, -.5, .25], np.float32)
        cases = {
            'pcm8': riff(1, 8, 1, 22050, (values*127+128).round().astype(np.uint8).tobytes()),
            'pcm16': riff(1, 16, 2, 44100, np.repeat(values*32767, 2).round().astype('<i2').tobytes()),
            'pcm24': riff(1, 24, 1, 48000, b''.join(int(round(v*(2**23-1))).to_bytes(3, 'little', signed=True) for v in values)),
            'pcm32': riff(1, 32, 1, 48000, (values*(2**31-1)).round().astype('<i4').tobytes()),
            'float32': riff(3, 32, 1, 96000, values.astype('<f4').tobytes()),
            'extensible float': riff(3, 32, 1, 48000, values.astype('<f4').tobytes(), extensible=True),
        }
        for name, data in cases.items():
            rate, samples = self.read(data)
            self.assertEqual(rate, {'pcm8': 22050, 'pcm16': 44100, 'float32': 96000}.get(name, 48000), name)
            np.testing.assert_allclose(samples[:, 0], values, atol=.01, err_msg=name)

    def test_other_files_are_refused(self):
        with self.assertRaises(ValueError):
            self.read(b'not a wave file')
        with self.assertRaises(ValueError):
            self.read(riff(2, 4, 1, 48000, bytes(8)))      # ADPCM


class PackFixture(unittest.TestCase):
    def make_pack(self, text, samples=('a.wav',), **wav):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder/'samples').mkdir()
        for name in samples:
            write_wav(folder/'samples'/name, **wav)
        (folder/'pack.sfz').write_text(text, encoding='utf-8')
        return folder/'pack.sfz'


class ParseTests(PackFixture):
    def test_note_names(self):
        self.assertEqual([parse_key(k) for k in ('60', 'c4', 'C#4', 'db4', 'a-1')], [60, 60, 61, 61, 9])

    def test_headers_inherit_and_regions_override(self):
        path = self.make_pack('''// comment
<control> default_path=samples/
<global> loop_mode=one_shot amp_veltrack=0
<group> lochan=2 hichan=2 volume=-6
<region> sample=a b.wav key=c4 group=60 off_by=60
<region> sample=a b.wav lokey=62 hikey=64 pitch_keycenter=63 volume=0 loop_mode=no_loop''', samples=('a b.wav',))
        first, second = parse_sfz(path)
        self.assertEqual((first.lokey, first.hikey, first.keycenter, first.lochan, first.volume), (60, 60, 60, 2, -6.))
        self.assertTrue(first.one_shot and first.amp_veltrack == 0 and first.group == first.off_by == 60)
        self.assertEqual(first.sample.name, 'a b.wav')
        self.assertEqual((second.lokey, second.hikey, second.keycenter, second.volume, second.one_shot), (62, 64, 63, 0., False))

    def test_unusable_packs_say_why(self):
        with self.assertRaises(PackError):
            parse_sfz(self.make_pack('<global> volume=0'))
        with self.assertRaises(PackError):
            Pack(self.make_pack('<region> sample=missing.wav key=60'))


class MappingAndPlanTests(PackFixture):
    def pack(self):
        return Pack(self.make_pack('''<control> default_path=samples/
<global> amp_veltrack=0
<group> lochan=1 hichan=1
<region> sample=a.wav key=60 loop_mode=one_shot group=60 off_by=60
<group> lochan=2 hichan=2
<region> sample=a.wav key=60 loop_mode=one_shot group=60 off_by=60
<region> sample=a.wav key=64 ampeg_release=0.01''', seconds=1.))

    def test_channel_then_any_channel_then_nearest_with_shift(self):
        pack = self.pack()
        region, semis = pack.region_for(1, 60, 100)
        self.assertEqual((region.lochan, semis), (2, 0))
        region, semis = pack.region_for(9, 60, 100)          # no channel 10 region: any channel
        self.assertEqual((region.keycenter, semis), (60, 0))
        region, semis = pack.region_for(0, 62, 100)          # no region for 62: nearest, shifted
        self.assertIn(region.keycenter, (60, 64))
        self.assertEqual(region.keycenter+semis, 62)
        self.assertLess(len(pack.audio(*pack.region_for(0, 72, 100))), len(pack.audio(*pack.region_for(0, 60, 100))))

    def test_one_shot_ignores_note_off_and_release_shortens(self):
        pack = self.pack()
        voices = plan([on(0., 60), off(.1, 60), on(.0, 64, channel=1), off(.2, 64, channel=1)], pack)
        one_shot = next(v for v in voices if v.region.keycenter == 60)
        released = next(v for v in voices if v.region.keycenter == 64)
        self.assertEqual(one_shot.stop, RATE)                       # plays the whole 1 s sample
        self.assertEqual(released.stop, int(.2*RATE)+int(.01*RATE))  # note-off + 10 ms release

    def test_same_pitch_on_another_string_cuts_the_first(self):
        pack = self.pack()
        voices = plan([on(0., 60, channel=0), on(.3, 60, channel=1)], pack)
        first, second = voices
        self.assertLessEqual(first.stop, int(.3*RATE)+int(.006*RATE)+1)
        self.assertGreater(first.fade, 0)
        self.assertEqual(second.stop, int(.3*RATE)+RATE)

    def test_render_blocks_match_a_single_render(self):
        pack = self.pack()
        voices = plan([on(0., 60), on(.25, 64, channel=1), off(.6, 64, channel=1), on(.3, 60, channel=1)], pack)
        renderer = Renderer(voices, 2*RATE)
        whole = renderer.block(0, 2*RATE)
        pieces = np.concatenate([renderer.block(b, 2048) for b in range(0, 2*RATE, 2048)])[:2*RATE]
        self.assertTrue(np.allclose(whole, pieces, atol=1e-6))
        self.assertAlmostEqual(float(whole[100, 0]), .5, places=3)           # one note
        self.assertAlmostEqual(float(whole[int(.27*RATE), 0]), 1., places=3)  # two notes (clipped at 1)

    def test_resampled_and_mono_samples_become_stereo_at_48k(self):
        pack = Pack(self.make_pack('<region> sample=samples/a.wav key=60', seconds=.5, rate=22050, channels=1))
        audio = pack.audio(*pack.region_for(0, 60, 100))
        self.assertEqual(audio.shape, (int(.5*RATE), 2))


class FakeOutput:
    instances = []

    def __init__(self):
        self.frames, self.count = [], 0
        FakeOutput.instances.append(self)

    def free(self):
        return [0]

    def write(self, i, frames):
        self.frames.append(frames.copy())
        self.count += len(frames)

    def played(self):
        return self.count

    def close(self):
        pass


class PreviewTests(PackFixture):
    def test_sample_preview_streams_from_the_start_position(self):
        path = self.make_pack('<control> default_path=samples/\n<region> sample=a.wav key=60 loop_mode=one_shot', seconds=1.)

        class Arrangement:
            duration = 2.
        events = [on(1., 60)]
        with patch.object(sample_preview, 'reference_events', lambda a, original=False: events):
            preview = SamplePreview(path, output=FakeOutput)
            arrangement = Arrangement()
            preview.play(arrangement, start=.5)
            preview.thread.join(5)
        audio = np.concatenate(FakeOutput.instances[-1].frames)
        onset = int(np.flatnonzero(audio[:, 0] > .1)[0])
        self.assertAlmostEqual(onset/RATE, .5, delta=1/RATE)       # note at 1.0 s, playback from 0.5 s
        self.assertAlmostEqual(preview.position, 2., delta=.05)
        self.assertEqual(preview.error, '')

    def test_timbre_switch_keeps_old_backend_on_a_bad_pack(self):
        class Fake:
            def __init__(self, path=None):
                if path == 'bad':
                    raise PackError('坏的音色包')
                self.position, self.playing, self.error = 0., False, ''

            def stop(self):
                pass
        midi = Fake()
        timbre = TimbrePreview(midi=midi, sample_factory=Fake)
        self.assertEqual(timbre.kind, DEFAULT)
        with self.assertRaises(PackError):
            timbre.set_timbre(CUSTOM, 'bad')
        self.assertIs(timbre.current, midi)
        timbre.set_timbre(CUSTOM, 'good')
        self.assertEqual((timbre.kind, str(timbre.pack_path)), (CUSTOM, 'good'))
        timbre.set_timbre(DEFAULT)
        self.assertIs(timbre.current, midi)


@unittest.skipUnless(GAME_PACK.is_file(), 'local game pack not built')
class GamePackTests(unittest.TestCase):
    def test_every_string_and_fret_has_its_own_sample_and_cut_group(self):
        pack = sample_preview.load_pack(GAME_PACK)
        self.assertEqual(len(pack.regions), 120)
        for string, open_ in enumerate((40, 45, 50, 55, 59, 64)):
            for fret in range(20):
                region, semis = pack.region_for(string, open_+fret+1, 100)
                self.assertEqual((region.lochan, semis), (string+1, 0))
                self.assertEqual(region.group, region.off_by)
                self.assertEqual(region.group, open_+fret+1)
                self.assertTrue(region.one_shot)


if __name__ == '__main__':
    unittest.main()
