"""MusicXML reading: staves, voices, lyrics with melismas, ties, repeats, transposition, MIDI export."""
import tempfile
import unittest
from pathlib import Path

from guitar_player.midi import read_midi
from guitar_player import musicxml

SCORE = """<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="3.1">
 <part-list>
  <score-part id="P1"><part-name>Piano</part-name><midi-instrument id="I1"><midi-program>1</midi-program></midi-instrument></score-part>
  <score-part id="P2"><part-name>Clarinet</part-name></score-part>
 </part-list>
 <part id="P1">
  <measure number="1">
   <attributes><divisions>2</divisions><time><beats>2</beats><beat-type>4</beat-type></time><staves>2</staves></attributes>
   <direction><sound tempo="90"/></direction>
   <barline location="left"><repeat direction="forward"/></barline>
   <note><pitch><step>E</step><octave>5</octave></pitch><duration>2</duration><voice>1</voice><staff>1</staff>
    <lyric><syllable>single</syllable><text>la</text><extend type="start"/></lyric></note>
   <note><chord/><pitch><step>C</step><octave>5</octave></pitch><duration>2</duration><voice>1</voice><staff>1</staff></note>
   <note><pitch><step>D</step><octave>5</octave></pitch><duration>2</duration><voice>1</voice><staff>1</staff>
    <tie type="start"/></note>
   <backup><duration>4</duration></backup>
   <note><pitch><step>C</step><octave>3</octave></pitch><duration>4</duration><voice>5</voice><staff>2</staff></note>
   <barline location="right"><repeat direction="backward"/></barline>
  </measure>
  <measure number="2">
   <note><pitch><step>D</step><octave>5</octave></pitch><duration>2</duration><voice>1</voice><staff>1</staff>
    <tie type="stop"/></note>
   <note><pitch><step>F</step><alter>1</alter><octave>5</octave></pitch><duration>2</duration><voice>1</voice><staff>1</staff>
    <lyric><syllable>single</syllable><text>lu</text></lyric></note>
  </measure>
 </part>
 <part id="P2">
  <measure number="1">
   <attributes><divisions>1</divisions><transpose><diatonic>-1</diatonic><chromatic>-2</chromatic></transpose></attributes>
   <note><pitch><step>D</step><octave>5</octave></pitch><duration>2</duration><voice>1</voice></note>
  </measure>
  <measure number="2">
   <note><rest/><duration>2</duration><voice>1</voice></note>
  </measure>
 </part>
</score-partwise>
"""


class MusicXMLTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)/'score.musicxml'
        self.path.write_text(SCORE, encoding='utf-8')

    def tearDown(self):
        self.dir.cleanup()

    def test_notes_in_playing_order(self):
        score = musicxml.read_score(self.path)
        piano = [(n.beat, n.beats, n.pitch, n.staff, n.lyric) for n in score.notes if n.part == 0]
        once = [(0., 1., 76, 1, True), (0., 1., 72, 1, True), (0., 2., 48, 2, False), (1., 1., 74, 1, True)]
        # The repeat plays measure 1 twice; the tie into measure 2 lengthens the second D.
        self.assertEqual(sorted(piano), sorted(once+[(2., 1., 76, 1, True), (2., 1., 72, 1, True), (2., 2., 48, 2, False),
                                                      (3., 2., 74, 1, True), (5., 1., 78, 1, True)]))
        clarinet = [(n.beat, n.pitch) for n in score.notes if n.part == 1]
        self.assertEqual(clarinet, [(0., 72), (2., 72)])        # written D5 sounds C5
        self.assertEqual(score.tempos, [(0., 90.), (2., 90.)])
        self.assertEqual(score.parts[0], dict(name='Piano', program=0))

    def test_midi_keeps_hands_apart(self):
        score = musicxml.read_score(self.path)
        path = Path(self.dir.name)/'score.mid'
        musicxml.score_midi(score).save(path)
        song = read_midi(str(path))
        self.assertEqual(len(song.sources), 3)                  # piano right hand, left hand, clarinet
        self.assertEqual(sorted(n.pitch for n in song.notes), sorted(n.pitch for n in score.notes))
        self.assertAlmostEqual(song.duration, 6*60/90, places=2)


if __name__ == '__main__':
    unittest.main()
