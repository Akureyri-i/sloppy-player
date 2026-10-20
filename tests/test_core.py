import io
import json
from pathlib import Path
import tempfile
import time
import unittest

import mido

from guitar_player.arrange import (arrange, assign_strings, extract_melody,
                                  fold_voice, octave_candidates)
from guitar_player.export import write_midi, write_report
from guitar_player.midi import MeterMap, TempoMap, read_midi
from guitar_player.model import Config, Note, Song, Source, VoiceNote, TUNING, FRET_KEYS, STRING_KEYS
from guitar_player.playback import EdgeHotkeys, KeyboardDriver, Player
from guitar_player.prefs import Preferences


def note(i, pitch, start=0., duration=.4, source='0:0', velocity=80):
    return Note(i,pitch,velocity,start,start+duration,round(start*960),
                round((start+duration)*960),start*2,1.,source,int(source.split(':')[1]))


def song(notes):
    sources = []
    for sid in sorted({n.source for n in notes}):
        ns = [n for n in notes if n.source == sid]
        track,ch = map(int,sid.split(':'))
        sources.append(Source(sid,track,ch,'Track',len(ns),min(n.pitch for n in ns),max(n.pitch for n in ns)))
    return Song('fixture.mid',sorted(notes,key=lambda n:n.start),sources,
                max((n.end for n in notes),default=0),480)


def midi_song(tracks, midi_type=1):
    midi=mido.MidiFile(type=midi_type,ticks_per_beat=480)
    midi.tracks.extend(mido.MidiTrack(track) for track in tracks)
    data=io.BytesIO(); midi.save(file=data); data.seek(0)
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'fixture.mid'; path.write_bytes(data.read())
        return read_midi(path)


def validate(a):
    ids=set(); available=[0.]*6; pitch_due={}
    for s in a.strikes:
        assert s.notes
        assert len({f.fret for f in s.notes}) == 1
        assert len({f.string for f in s.notes}) == len(s.notes)
        assert s.time >= s.intended
        assert s.time-s.intended <= a.config.max_delay+1e-7
        for f in s.notes:
            assert s.time>=available[f.string]-1e-7
            available[f.string]=s.time+s.hold+a.config.gap
            assert 0<=f.string<6 and 0<=f.fret<20
            assert a.config.tuning[f.string]+f.fret == f.voice.pitch
            assert s.time >= pitch_due.get(f.voice.pitch,0)-1e-7
            pitch_due[f.voice.pitch]=s.time+a.config.repeat_interval
            assert (f.voice.pitch-f.voice.note.pitch)%12 == 0
            assert f.voice.note.id not in ids
            ids.add(f.voice.note.id)
    assert not ids.intersection(a.dropped)
    assert ids.union(a.dropped) == {n.id for n in a.selected}
    assert len(ids) == a.stats['played_notes']


class MidiTests(unittest.TestCase):
    def test_initial_tempo_and_changes(self):
        tempo=TempoMap([(0,400000),(480,600000),(960,300000)],480)
        self.assertAlmostEqual(tempo.seconds_at(480),.4)
        self.assertAlmostEqual(tempo.seconds_at(960),1.)
        self.assertAlmostEqual(tempo.seconds_at(1440),1.3)

    def test_initial_three_four_and_change(self):
        meter=MeterMap([(0,3,4),(2880,6,8)],480)
        self.assertEqual(meter.strength(1440),1.)
        self.assertEqual(meter.strength(2880+720),.7)

    def test_same_tick_changes_use_event_order(self):
        t=TempoMap([(0,800000),(0,400000)],480)
        self.assertAlmostEqual(t.seconds_at(480),.4)

    def test_channels_keep_independent_durations(self):
        s=midi_song([[mido.Message('note_on',channel=0,note=60,velocity=80),
            mido.Message('note_on',channel=1,note=60,velocity=80),
            mido.Message('note_off',channel=0,note=60,time=96),
            mido.Message('note_off',channel=1,note=60,time=864)]])
        self.assertEqual([n.duration for n in s.notes],[.1,1.])
        self.assertEqual(len({n.id for n in s.notes}),2)

    def test_overlapping_same_pitch_fifo(self):
        s=midi_song([[mido.Message('note_on',note=60,velocity=80),
            mido.Message('note_on',note=60,velocity=90,time=96),
            mido.Message('note_off',note=60,time=96),
            mido.Message('note_off',note=60,time=96)]])
        self.assertAlmostEqual(s.notes[0].duration,.2)
        self.assertAlmostEqual(s.notes[1].duration,.2)

    def test_track_identity_same_channel(self):
        s=midi_song([[mido.Message('note_on',note=60,velocity=80),mido.Message('note_off',note=60,time=480)],
                     [mido.Message('note_on',note=60,velocity=80),mido.Message('note_off',note=60,time=960)]])
        self.assertEqual([n.duration for n in s.notes],[.5,1])

    def test_sustain_and_velocity_zero(self):
        s=midi_song([[mido.Message('control_change',control=64,value=127),
            mido.Message('note_on',note=60,velocity=80),
            mido.Message('note_on',note=60,velocity=0,time=480),
            mido.Message('control_change',control=64,value=0,time=480)]])
        self.assertEqual(len(s.notes),1)
        self.assertEqual(s.notes[0].duration,1)

    def test_unclosed_note_warning(self):
        s=midi_song([[mido.Message('note_on',note=60,velocity=80),
            mido.MetaMessage('end_of_track',time=480)]])
        self.assertTrue(s.warnings)
        self.assertEqual(s.notes[0].duration,.5)

    def test_type_two_clear_error(self):
        with self.assertRaisesRegex(ValueError,'Type 2'):
            midi_song([[]],2)


class ArrangementTests(unittest.TestCase):
    def test_whole_chord_matching_reserves_high_string(self):
        vs=[VoiceNote(note(i,p),p,'harmony',20) for i,p in enumerate([64,83])]
        fs=assign_strings(vs)
        self.assertEqual(len(fs),2)
        self.assertEqual(len({f.string for f in fs}),2)
        self.assertEqual(next(f.string for f in fs if f.voice.pitch==83),5)

    def test_impossible_low_chord_drops_instead_of_reusing(self):
        fs=assign_strings([VoiceNote(note(i,p),p,'harmony',20) for i,p in enumerate([40,41])])
        self.assertEqual(len(fs),1)

    def test_repeated_notes_can_use_another_available_string(self):
        voice=VoiceNote(note(0,64),64,'melody',100)
        result=assign_strings([voice],occupied={5:.035},at=0)
        self.assertNotEqual(result[0].string,5)

    def test_register_order_preserved_without_melody_labels(self):
        s=song([note(0,82,0,.6,'0:0'),note(1,84,.5,.6,'0:0'),
                note(2,76,0,.6,'1:1'),note(3,77,.5,.6,'1:1')])
        cfg=Config(mode='full',octave=0,melody_source='0:0',bass_source='1:1')
        full=arrange(s,cfg)
        self.assertEqual(full.stats['register_inversion_rate'],0)
        self.assertEqual(full.stats['played_notes'],4)
        from dataclasses import replace
        neutral=arrange(s,replace(cfg,melody_source=None,bass_source=None))
        self.assertEqual(full.strikes,neutral.strikes)

    def test_octave_doubling_keeps_melody(self):
        s=song([note(0,48,velocity=110),note(1,72,velocity=60)])
        a=arrange(s,Config(mode='guitar',octave=0,melody_source='0:0'))
        self.assertEqual({f.voice.pitch for st in a.strikes for f in st.notes},{48,72})
        self.assertIn(1,a.melody_ids)
        validate(a)

    def test_phrase_shift_preserves_ascending_boundary(self):
        ns=[note(i,p,i*.5,.6) for i,p in enumerate([82,84,85])]
        mapped=fold_voice(ns,'complete',base=0)
        pitches=[mapped[n.id] for n in ns]
        self.assertEqual([pitches[i+1]-pitches[i] for i in range(2)],[2,1])
        self.assertLess(pitches[0],82)

    def test_conservative_candidates_never_exceed_octave(self):
        for p in range(128):
            self.assertTrue(all(abs(c-p)<=12 for c in octave_candidates(p,'conservative')))

    def test_raw_preserves_pitch_and_repeated_notes(self):
        a=arrange(song([note(0,60),note(1,60,.04)]),Config(mode='raw'))
        self.assertEqual(a.stats['played_notes'],2)
        self.assertEqual(a.stats['shifts_semitones'],{0:2})
        validate(a)

    def test_sustained_melody_does_not_follow_lower_ornaments(self):
        ns=[note(0,72,0,1),note(1,60,.25,.1),note(2,62,.5,.1),note(3,74,1,.5)]
        self.assertEqual([n.id for n in extract_melody(ns)],[0,3])

    def test_selected_sources_and_drums(self):
        s=song([note(0,60,source='0:0'),note(1,72,source='1:1'),note(2,40,source='2:9')])
        a=arrange(s,Config(mode='raw',sources=('0:0','2:9')))
        self.assertEqual([n.id for n in a.selected],[0])
        with self.assertRaises(ValueError):
            arrange(s,Config(mode='guitar',melody_source='1:1',sources=('0:0',)))

    def test_empty_song_and_selection(self):
        for cfg in (Config(),Config(mode='raw')):
            a=arrange(song([]),cfg); validate(a)
            self.assertEqual(a.stats['played_notes'],0)
        a=arrange(song([note(0,60)]),Config(sources=()))
        self.assertEqual(a.stats['selected_notes'],0)

    def test_dense_notes_report_physical_losses(self):
        # These five low pitches can only use Z; overload that actual resource.
        a=arrange(song([note(i,41+i%5,i*.005) for i in range(100)]),Config(mode='raw'))
        validate(a)
        self.assertGreater(a.stats['drop_reasons'].get('input_capacity',0),0)
        self.assertLessEqual(a.stats['max_delay_ms'],83.001)

    def test_same_fret_groups_share_one_strike(self):
        a=arrange(song([note(i,p) for i,p in enumerate([41,46,51])]),Config(mode='raw'))
        self.assertEqual(len(a.strikes),1)
        self.assertEqual(len(a.strikes[0].notes),3)

    def test_auto_preserves_ready_guitar(self):
        s=song([note(i,p,i*.5) for i,p in enumerate([60,62,64,65,67])])
        a=arrange(s)
        self.assertEqual(a.config.mode,'auto')
        self.assertEqual(a.stats['recommendation']['chosen'],'raw')
        # Already in range: played as written, without running the model or comparing layouts.
        self.assertTrue(a.stats['recommendation']['in_range'])
        self.assertEqual(a.stats['recommendation']['scores'],{})
        self.assertNotIn('analysis',a.stats)
        self.assertEqual(a.stats['retention'],1)
        validate(a)

    def test_auto_adapts_out_of_range_lead(self):
        s=song([note(i,p,i*.5) for i,p in enumerate([84,86,88,89,91])])
        a=arrange(s)
        self.assertNotEqual(a.stats['recommendation']['chosen'],'raw')
        self.assertEqual(a.stats['retention'],1)

    def test_auto_does_not_ignore_explicit_octave(self):
        a=arrange(song([note(0,60)]),Config(octave=1))
        self.assertNotEqual(a.stats['recommendation']['chosen'],'raw')
        self.assertEqual(next(f.voice.pitch for s in a.strikes for f in s.notes),72)

    def test_random_arrangements_obey_physical_invariants(self):
        import random
        rng=random.Random(719)
        for case in range(8):
            ns=[note(i,rng.randrange(20,110),round(rng.random()*2,2),rng.uniform(.05,.8),
                     f'{i%3}:{i%3}',rng.randrange(30,128)) for i in range(45)]
            for mode in ('raw','full','guitar','auto'):
                validate(arrange(song(ns),Config(mode=mode,density=('conservative','balanced','complete')[case%3])))

    def test_speed_and_export_round_trip(self):
        a=arrange(song([note(0,60,1),note(1,64,2)]),Config(mode='raw',speed=.5))
        self.assertAlmostEqual(a.strikes[0].time,2.)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'out.mid'; write_midi(a,path)
            read=read_midi(path)
            self.assertEqual([n.pitch for n in read.notes],[60,64])
            for n,st in zip(read.notes,a.strikes):
                self.assertAlmostEqual(n.start,st.time,delta=.001)
            report=Path(temp)/'report.json'; write_report(a,report,True)
            self.assertEqual(json.loads(report.read_text(encoding='utf-8'))['stats']['played_notes'],2)

    def test_invalid_settings(self):
        for kwargs in ({'max_voices':129},{'hold':0},{'speed':0},{'density':'unknown'},
                       {'simplify':1.1},{'tuning_offset':1.5},{'repeat_interval':-.01}):
            with self.assertRaises(ValueError): Config(**kwargs)


class FakeKeyboard:
    def __init__(self, fail=None):
        self.events=[]; self.held=set(); self.fail=fail
    def press(self,key):
        self.events.append(('press',key,time.perf_counter())); self.held.add(key)
        if key==self.fail: raise RuntimeError('injected failure')
    def release(self,key):
        self.events.append(('release',key,time.perf_counter())); self.held.discard(key)


class PlaybackTests(unittest.TestCase):
    def wait_for(self, predicate, seconds=2):
        deadline=time.perf_counter()+seconds
        while not predicate() and time.perf_counter()<deadline: time.sleep(.002)
        self.assertTrue(predicate())

    def plan(self, count=2, hold=.015):
        return arrange(song([note(i,60+i,.03+i*.08,.05) for i in range(count)]),
                       Config(mode='raw',hold=hold,gap=.004))

    def test_normal_completion_no_early_presses_or_held_keys(self):
        backend=FakeKeyboard(); p=Player(KeyboardDriver(backend),focus_check=lambda:True)
        plan=self.plan(); before=time.perf_counter()
        self.assertTrue(p.play(plan,countdown=0))
        self.wait_for(lambda:p.state.status=='finished')
        self.assertFalse(backend.held)
        self.assertGreaterEqual(backend.events[0][2]-before,.029)
        self.assertAlmostEqual(p.state.position,plan.duration)

    def test_exception_releases_partially_pressed_keys(self):
        backend=FakeKeyboard(fail='b'); p=Player(KeyboardDriver(backend),focus_check=lambda:True)
        p.play(self.plan(1),countdown=0)
        self.wait_for(lambda:p.state.status=='error')
        self.assertFalse(backend.held)

    def test_stop_during_hold(self):
        backend=FakeKeyboard(); p=Player(KeyboardDriver(backend),focus_check=lambda:True)
        p.play(self.plan(2,.1),countdown=0)
        self.wait_for(lambda:bool(backend.held.intersection(STRING_KEYS)))
        self.assertTrue(p.stop())
        self.assertFalse(backend.held)
        self.assertEqual(p.state.status,'stopped')

    def test_pause_during_hold_releases_and_does_not_retrigger(self):
        backend=FakeKeyboard(); p=Player(KeyboardDriver(backend),focus_check=lambda:True)
        p.play(self.plan(2,.06),countdown=0)
        self.wait_for(lambda:bool(backend.held.intersection(STRING_KEYS)))
        p.pause_resume(); self.wait_for(lambda:p.state.status=='paused')
        self.assertFalse(backend.held)
        count=len(backend.events); time.sleep(.05)
        self.assertEqual(count,len(backend.events))
        p.pause_resume(); self.wait_for(lambda:p.state.status=='finished')
        self.assertFalse(backend.held)

    def test_focus_loss_freezes_before_any_input(self):
        focused=[False]; backend=FakeKeyboard()
        p=Player(KeyboardDriver(backend),focus_check=lambda:focused[0])
        p.play(self.plan(1),countdown=0)
        self.wait_for(lambda:p.state.status=='paused')
        self.assertEqual(backend.events,[])
        focused[0]=True; p.pause_resume()
        self.wait_for(lambda:p.state.status=='finished')
        self.assertTrue(backend.events)

    def test_snapshot_and_reentrant_play(self):
        p=Player(KeyboardDriver(FakeKeyboard()),focus_check=lambda:True)
        self.assertTrue(p.play(self.plan(),countdown=.2))
        self.assertFalse(p.play(self.plan(),countdown=0))
        self.assertTrue(p.stop())

    def test_held_hotkey_fires_once(self):
        edge=EdgeHotkeys()
        self.assertEqual(edge.update(['f2']),{'f2'})
        self.assertEqual(edge.update(['f2']),set())
        edge.update([])
        self.assertEqual(edge.update(['f2']),{'f2'})

    def test_fret_taps_do_not_release_strings_and_are_not_omitted(self):
        backend=FakeKeyboard();driver=KeyboardDriver(backend)
        driver.select_fret('e');driver.press('z')
        driver.select_fret('e');driver.press('c')
        self.assertEqual(backend.held,{'z','c'})
        driver.select_fret('t');driver.press('v')
        self.assertEqual(backend.held,{'z','c','v'})
        self.assertEqual([(a,k) for a,k,_ in backend.events if k in FRET_KEYS],
                         [('press','e'),('release','e'),('press','e'),('release','e'),('press','t'),('release','t')])
        driver.release_all();self.assertFalse(backend.held)

    def test_simultaneous_different_frets_do_not_wait_for_hold(self):
        backend=FakeKeyboard();p=Player(KeyboardDriver(backend),focus_check=lambda:True)
        a=arrange(song([note(0,64,.03,.05),note(1,83,.03,.05)]),Config(mode='raw',hold=.08))
        self.assertEqual([s.time for s in a.strikes],[.03,.03])
        p.play(a,countdown=0);self.wait_for(lambda:p.state.status=='finished')
        presses=[t for action,key,t in backend.events if action=='press' and key in STRING_KEYS]
        releases=[t for action,key,t in backend.events if action=='release' and key in STRING_KEYS]
        self.assertEqual(len(presses),2)
        self.assertLess(max(presses),min(releases))
        self.assertEqual(len([1 for action,key,_ in backend.events if action=='press' and key in FRET_KEYS]),2)

    def test_all_six_strings_can_trigger_together(self):
        a=arrange(song([note(i,p,.03,.05) for i,p in enumerate(Config().tuning)]),Config(mode='raw'))
        self.assertEqual(a.stats['played_notes'],6)
        self.assertEqual({s.time for s in a.strikes},{.03})
        backend=FakeKeyboard();p=Player(KeyboardDriver(backend),focus_check=lambda:True)
        p.play(a,countdown=0);self.wait_for(lambda:p.state.status=='finished')
        self.assertEqual(len([1 for action,key,_ in backend.events if action=='press' and key=='e']),6)
        self.assertFalse(backend.held)

    def test_preferences_and_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Preferences(Path(temp)/'prefs.json')
            c=Config(sources=('0:0',),mode='auto')
            p.save('x',c); self.assertEqual(p.get('x'),c)
            p.path.write_text('{broken')
            self.assertIsNone(p.get('x'))


if __name__=='__main__': unittest.main()
