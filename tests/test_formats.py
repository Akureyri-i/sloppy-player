"""Score formats: ml3czus-autoplayer sheets, MusicXML import / export, MIDI on the score's beats."""
from collections import Counter, defaultdict
import importlib.util
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from guitar_player.ahk_sheet import STEP, read_sheet, write_sheet
from guitar_player.arrange import arrange
from guitar_player.export import export
from guitar_player.midi import read_midi, read_song
from guitar_player.model import FRET_KEYS, STRING_KEYS, Config, Note, Song, Source
from guitar_player.musicxml import read_score

TUNING = Config().tuning
ROOT = Path(__file__).resolve().parents[1]


def beat_note(i, pitch, beat, beats, tpb=480, bpm=120):
    spb = 60/bpm
    return Note(i, pitch, 90, beat*spb, (beat+beats)*spb, round(beat*tpb), round((beat+beats)*tpb), beat, 1., '0:0', 0)


def beat_song(notes, meters=((0, 4, 4),), tempos=((0, 500000),)):
    return Song('beats.mid', notes, [Source('0:0', 0, 0, 'Piano', len(notes), 40, 90)],
                max(n.end for n in notes), 480, meters=meters, tempos=tempos)


class SheetReadingTests(unittest.TestCase):
    def test_keys_steps_and_delays_as_its_player_does(self):
        text = '# Title: test\n# Key delay: 50\nPause delay: 80\n[pvyx]-|y[c]Z [[e]\n'
        song = read_sheet(text, 'test.txt')
        got = [(round(n.start*1000), song.fingering[n.id]) for n in song.notes]
        # p+v, y+x together (a step); '-' a step; '|' a pause; 'y' alone selects fret 3 (a step),
        # then [c] plays it; uppercase Z on fret 3; '[' is never sent; [e] selects fret 0 only.
        self.assertEqual(got, [(0, (3, 5)), (0, (1, 3)), (230, (2, 3)), (280, (0, 3))])
        self.assertEqual([n.pitch for n in song.notes], [TUNING[3]+5, TUNING[1]+3, TUNING[2]+3, TUNING[0]+3])
        self.assertEqual(len(song.warnings), 1)                  # the skipped '['

    def test_defaults_and_out_of_range_delays(self):
        song = read_sheet('Key delay: 900\n[ez]-[ez]', 's.txt')
        self.assertEqual([round(n.start*1000) for n in song.notes], [0, 400])

    def test_detector_zones(self):
        text = 'Key delay: 40\n[ez]\n---START---\n[ez]-\n---100---\n[rz]-\n---RESET---\n[tz]\n---END---\n[yz]'
        song = read_sheet(text, 's.txt')
        self.assertEqual([(round(n.start*1000), song.fingering[n.id][1]) for n in song.notes],
                         [(0, 0), (80, 1), (280, 2)])


class SheetPlayingTests(unittest.TestCase):
    def test_raw_mode_keeps_the_sheets_strings_and_frets(self):
        # Two notes on the x string at once (frets 3 and 5) and a chord the arranger would place differently.
        song = read_sheet('Key delay: 100\n[yxpx]--[;c]', 's.txt')
        a = arrange(song, Config(mode='raw', arranger='legacy'))
        played = sorted((round(s.time*1000), f.string, f.fret) for s in a.strikes for f in s.notes)
        self.assertEqual(played, [(0, 1, 3), (25, 1, 5), (300, 2, 16)])
        again = arrange(song, Config(mode='auto', arranger='legacy'))
        self.assertEqual(again.stats['played_notes'], 3)
        self.assertNotEqual({(f.string, f.fret) for s in again.strikes for f in s.notes}, {(1, 3), (1, 5), (2, 16)})


class SheetWritingTests(unittest.TestCase):
    def arranged(self, notes, **config):
        return arrange(beat_song(notes), Config(mode='raw', arranger='legacy', **config))

    def test_round_trip_on_the_grid(self):
        a = self.arranged([beat_note(0, 60, 0, 1), beat_note(1, 64, 0, 1), beat_note(2, 67, .5, 1),
                           beat_note(3, 62, 2.37, .5)], end_guard=False)
        text, info = write_sheet(a)
        self.assertTrue(text.startswith(f'Key delay: {STEP}\n'))
        self.assertNotIn('#', text)
        back = read_sheet(text, 'x.txt')
        self.assertEqual(sorted((round(n.start*1000), n.pitch) for n in back.notes),
                         sorted((round(t*1000/STEP)*STEP, p) for t, p in ((0, 60), (0, 64), (.25, 67), (1.185, 62))))

    def test_frets_8_and_9_move_to_another_string(self):
        song = beat_song([beat_note(0, TUNING[0]+8, 0, 1), beat_note(1, TUNING[5]+9, 1, 1)])
        song.fingering = {0: (0, 8), 1: (5, 9)}
        a = arrange(song, Config(mode='raw', arranger='legacy'))
        self.assertEqual(sorted((f.string, f.fret) for s in a.strikes for f in s.notes), [(0, 8), (5, 9)])
        text, info = write_sheet(a)
        self.assertEqual(info['moved'], 2)
        back = read_sheet(text, 'x.txt')
        self.assertEqual(sorted(n.pitch for n in back.notes), [TUNING[0]+8, TUNING[5]+9])
        self.assertFalse({8, 9} & {f for _, f in back.fingering.values()})

    def test_end_guard_on_the_grid(self):
        from guitar_player.model import GAME_SOUND_LENGTH
        length = GAME_SOUND_LENGTH[60]
        # The second press lands where the first sound ends by itself: the guard moves it off.
        a = self.arranged([beat_note(0, 60, 0, .5), beat_note(1, 60, length*2, .5)])
        _, info = write_sheet(a)
        self.assertEqual(info['guarded'], 1)
        _, info = write_sheet(self.arranged([beat_note(0, 60, 0, .5), beat_note(1, 60, length*2, .5)], end_guard=False))
        self.assertEqual(info['guarded'], 0)


class MusicXMLExportTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def song(self):
        notes = [beat_note(0, 52, 0, 4), beat_note(1, 64, 0, 1), beat_note(2, 67, 1, 1/3), beat_note(3, 69, 4/3, 1/3),
                 beat_note(4, 71, 5/3, 1/3), beat_note(5, 72, 2, .25), beat_note(6, 74, 2.25, .75), beat_note(7, 76, 3, 2),
                 beat_note(8, 48, 6, 1)]
        return beat_song(notes, meters=((0, 4, 4), (2400, 3, 4)), tempos=((0, 500000), (1920, 400000)))

    def test_round_trip_and_bar_structure(self):
        a = arrange(self.song(), Config(mode='raw', arranger='legacy', speed=2))
        path = self.dir/'a.musicxml'
        export(a, path)
        score = read_score(path)
        self.assertEqual(sorted((round(n.beat*24), n.pitch) for n in score.notes),
                         sorted((round(f.voice.note.beat*24), f.voice.pitch) for s in a.strikes for f in s.notes))
        self.assertEqual(next(n.beats for n in score.notes if n.pitch == 76), 2)        # tied over the bar line
        self.assertEqual(score.tempos, [(0., 240.), (4., 300.)])                         # times the speed
        self.assertEqual(score.meters, [(0., 4, 4), (5., 3, 4)])
        root = ET.parse(path).getroot()
        self.assertEqual(len(root.findall('.//time-modification')), 6)                   # the triplet, twice
        frets = {(n.findtext('notations/technical/string'), n.findtext('notations/technical/fret'))
                 for n in root.iter('note') if n.findtext('staff') == '2' and n.find('pitch') is not None}
        self.assertEqual(frets, {(str(6-f.string), str(f.fret)) for s in a.strikes for f in s.notes})
        for measure in root.iter('measure'):
            # 4/4, then a bar cut short by the change to 3/4 at beat 5.
            length = {'1': 4, '2': 1}.get(measure.get('number'), 3)*24
            voices = defaultdict(int)
            for note in measure.iter('note'):
                if note.find('chord') is None:
                    voices[note.findtext('voice')] += int(note.findtext('duration'))
            self.assertTrue(all(v == length for v in voices.values()), (measure.get('number'), dict(voices)))

    @unittest.skipUnless(importlib.util.find_spec('lxml') and (ROOT/'data'/'musicxml-schema'/'musicxml.xsd').is_file(),
                         'needs lxml and the MusicXML schema in data/musicxml-schema/')
    def test_valid_against_the_official_schema(self):
        from tools.validate_musicxml import errors
        for mode in ('raw', 'guitar'):
            a = arrange(self.song(), Config(mode=mode, arranger='legacy'))
            for name in ('a.musicxml', 'a.mxl'):
                export(a, self.dir/name)
                self.assertEqual(errors(self.dir/name), [], (mode, name))
        # The check does catch a mistake: <voice> before <duration>.
        tree = ET.parse(self.dir/'a.musicxml')
        note = next(tree.getroot().iter('note'))
        voice = note.find('voice')
        note.remove(voice)
        note.insert(0, voice)
        tree.write(self.dir/'b.musicxml', encoding='utf-8')
        self.assertTrue(errors(self.dir/'b.musicxml'))

    def test_compressed_file_and_tab_staff_read_once(self):
        a = arrange(self.song(), Config(mode='raw', arranger='legacy'))
        path = self.dir/'a.mxl'
        export(a, path)
        song = read_song(path)
        self.assertEqual(len(song.sources), 1)          # the tab staff repeats the notes: skipped
        self.assertEqual(Counter(n.pitch for n in song.notes), Counter(f.voice.pitch for s in a.strikes for f in s.notes))


class MidiExportTests(unittest.TestCase):
    def test_score_beats_meter_and_tempo(self):
        notes = [beat_note(0, 60, 0, 1), beat_note(1, 64, 0, 1), beat_note(2, 67, 3.5, .5)]
        a = arrange(beat_song(notes, meters=((0, 3, 4),), tempos=((0, 600000),)),
                    Config(mode='raw', arranger='legacy', speed=1.5))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'a.mid'
            export(a, path)
            back = read_midi(path)
        self.assertEqual(back.meters, ((0, 3, 4),))
        self.assertEqual(back.tempos, ((0, 400000),))
        self.assertEqual(sorted((n.tick, n.pitch) for n in back.notes), [(0, 60), (0, 64), (1680, 67)])
        self.assertEqual(len(back.sources), 3)                   # one track per string used


if __name__ == '__main__':
    unittest.main()
