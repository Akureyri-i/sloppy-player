import tempfile
import unittest
from unittest.mock import MagicMock,patch
from pathlib import Path

import mido

from guitar_player.arrange import arrange
from guitar_player.export import reference_events
from guitar_player.model import Config
from guitar_player.visual import VisualScore, VisualNote
from guitar_player.prefs import Preferences
from guitar_player.preview import MidiPreview,events_from_position
from tests.test_core import note,song,validate


class OverviewTests(unittest.TestCase):
    def test_seek_keeps_long_notes_and_excludes_ended_notes(self):
        score=VisualScore([VisualNote(0,100,40,90),VisualNote(1,2,60,60),VisualNote(10,11,70,100)])
        self.assertEqual([n.pitch for n in score.between(10,10.1)],[40,70])
        energy=score.spectrum(10)
        self.assertEqual(energy[40],90);self.assertEqual(energy[60],0);self.assertEqual(energy[70],100)
        self.assertEqual(score.spectrum(100),[0.]*128)

    def test_overlapping_same_pitch_accumulates_energy(self):
        score=VisualScore([VisualNote(0,1,60,60),VisualNote(0,1,60,80)])
        self.assertEqual(score.spectrum(.5)[60],100.)

    def test_full_score_envelope_does_not_truncate_late_notes(self):
        score=VisualScore([VisualNote(0,.1,60,40),VisualNote(99,100,60,127)],100)
        envelope=score.envelope(10)
        self.assertEqual(envelope[0],40);self.assertEqual(envelope[-1],127)

    def test_output_visualization_matches_reference_midi_envelopes(self):
        a=arrange(song([note(0,65,0,1),note(1,65,.2,.5),note(2,69,.1,2)]),Config(mode='raw',arranger='legacy',speed=2))
        active={};expected=[]
        for t,_,msg in reference_events(a):
            key=(msg.channel,msg.note)
            if msg.type=='note_on':active[key]=(t,msg.velocity)
            elif key in active:
                start,velocity=active.pop(key);expected.append((start,t,msg.note,velocity,msg.channel))
        score=VisualScore.from_arrangement(a)
        self.assertEqual(sorted(expected),sorted((n.start,n.end,n.pitch,n.velocity,n.lane) for n in score.notes))
        original=VisualScore.from_song(a.song,a.config.speed,a.selected)
        self.assertEqual(original.duration,a.song.duration/2)

    def test_speed_limits_keep_execution_constraints(self):
        s=song([note(i,60+i%6,i*.04) for i in range(18)])
        for speed in (.25,1.37,4.):validate(arrange(s,Config(mode='full',arranger='legacy',speed=speed)))
        for speed in (0,float('nan'),float('inf'),4.01):
            with self.assertRaises(ValueError):Config(speed=speed)


class UiPreferencesTests(unittest.TestCase):
    def test_ui_preferences_do_not_replace_per_song_config(self):
        with tempfile.TemporaryDirectory() as directory:
            prefs=Preferences(Path(directory)/'settings.json');config=Config(speed=1.37)
            prefs.save('song',config);prefs.save_ui(theme='午夜',view='力度轮廓',source='原谱')
            self.assertEqual(prefs.get('song'),config)
            prefs.save('second',Config());self.assertEqual(prefs.get_ui()['theme'],'午夜')
            self.assertIsNone(prefs.get('__ui__'))

    def test_corrupt_ui_settings_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'prefs.json';path.write_text('{"__ui__": [42]}')
            self.assertEqual(Preferences(path).get_ui(),{})


class PreviewSeekTests(unittest.TestCase):
    def test_seek_reconstructs_sustained_notes_without_replaying_past_attacks(self):
        events=[(0,1,mido.Message('note_on',note=60,velocity=80)),
                (.1,1,mido.Message('note_on',note=64,velocity=90)),
                (.2,0,mido.Message('note_off',note=64)),
                (2,0,mido.Message('note_off',note=60))]
        held,future=events_from_position(events,1.)
        self.assertEqual([n.note for n in held],[60]);self.assertEqual([e[0] for e in future],[2])
        self.assertEqual(events_from_position(events,3),( [],[]))

    def test_no_duplicate_attack_at_exact_seek_boundary(self):
        events=[(0,1,mido.Message('note_on',note=60,velocity=80)),
                (1,0,mido.Message('note_off',note=60)),
                (1,1,mido.Message('note_on',note=60,velocity=90))]
        held,future=events_from_position(events,1)
        self.assertFalse(held);self.assertEqual(len(future),2)

    def test_synth_output_seek_position_and_resource_cleanup(self):
        a=arrange(song([note(0,60,0,.2)]),Config(mode='raw',arranger='legacy'))
        synth=MagicMock()
        for name in ('midiOutOpen','midiOutShortMsg','midiOutReset','midiOutClose'):
            getattr(synth,name).return_value=0
        preview=MidiPreview()
        with patch('guitar_player.preview.os.name','nt'),patch('guitar_player.preview.ctypes.WinDLL',return_value=synth,create=True):
            preview.play(a,start=.1);preview.thread.join(2)
        self.assertFalse(preview.playing);self.assertFalse(preview.error)
        self.assertAlmostEqual(preview.position,a.duration,places=2)
        messages=[call.args[1] for call in synth.midiOutShortMsg.call_args_list]
        self.assertTrue(any((m&0xf0)==0x90 and ((m>>8)&127)==60 for m in messages))
        synth.midiOutReset.assert_called_once();synth.midiOutClose.assert_called_once()


if __name__=='__main__':unittest.main()
