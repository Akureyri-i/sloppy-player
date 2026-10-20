from dataclasses import replace
import json
from pathlib import Path
import tempfile
import importlib.util
import unittest
from unittest.mock import patch

import numpy as np

from guitar_player.arrange import arrange
from guitar_player.joint import analyze_structure,optimize,repeated_motifs
from guitar_player.model import Config
from guitar_player.music_model import encode_notes,window_starts,ModelUnavailable,get_features
from guitar_player.voice_edges import candidate_pairs,geometric_features,predict_edges
from tests.test_core import note,song,validate,midi_song
import mido


class EncodingTests(unittest.TestCase):
    def test_fast_repeats_keep_identity_and_exact_times(self):
        ns=[note(i,60,i*.02) for i in range(8)]
        s=song(ns);before=[(n.id,n.start,n.end,n.tick,n.end_tick) for n in ns]
        e=encode_notes(s,ns)
        self.assertEqual(len(e.ids),8);self.assertEqual(set(e.ids),set(range(8)))
        self.assertLess(len(set(map(tuple,e.tokens))),8)
        self.assertEqual(before,[(n.id,n.start,n.end,n.tick,n.end_tick) for n in ns])

    def test_meter_change_encodes_each_note_once_and_preserves_ticks(self):
        s=midi_song([[mido.MetaMessage('time_signature',numerator=3,denominator=4),
            mido.Message('note_on',note=60,velocity=80,time=0),mido.Message('note_off',note=60,time=480),
            mido.MetaMessage('time_signature',numerator=4,denominator=4,time=960),
            mido.Message('note_on',note=62,velocity=80,time=0),mido.Message('note_off',note=62,time=480)]])
        self.assertEqual(s.meters,((0,3,4),(1440,4,4)))
        e=encode_notes(s,s.notes)
        self.assertEqual(e.bars.tolist(),[0,1]);self.assertEqual(e.positions.tolist(),[0.,0.])
        self.assertEqual([n.tick for n in s.notes],[0,1440])

    def test_short_out_of_vocabulary_notes_are_retained(self):
        s=song([replace(note(17,0),end_tick=1),note(44,127,.02)])
        e=encode_notes(s,s.notes)
        self.assertEqual(e.ids.tolist(),[17,44]);self.assertTrue(e.clipped.all())
        self.assertEqual([n.pitch for n in s.notes],[0,127])

    def test_windows_cover_every_note_and_overlap(self):
        for count in (0,1,1024,1025,2884):
            coverage=np.zeros(count,int)
            for start in window_starts(count):coverage[start:min(count,start+1024)]+=1
            self.assertTrue((coverage>0).all())
            if count>1024:self.assertGreater(coverage.max(),1)

    def test_sources_are_not_model_voice_labels(self):
        ns=[note(i,60+i,i*.2,source=f'{i%2}:{i%2}') for i in range(6)]
        e=encode_notes(song(ns),ns)
        merged=[replace(n,source='0:0',channel=0) for n in ns]
        np.testing.assert_array_equal(e.tokens,encode_notes(song(merged),merged).tokens)


class JointTests(unittest.TestCase):
    def test_joint_can_move_in_range_prefix_to_protect_phrase(self):
        ns=[note(i,p,i*.25) for i,p in enumerate([80,82,84,85])]
        structure=analyze_structure(song(ns),ns,'joint')
        out,info=optimize(structure,{0:80,1:82,2:84,3:73},0,(41,84))
        self.assertEqual([out[i] for i in range(4)],[68,70,72,73])
        self.assertGreater(info['window_objective_improvement'],0)

    def test_connections_are_decisions_not_fixed_nearest_neighbors(self):
        ns=[note(i*2+j,p,i*.25) for i in range(4) for j,p in enumerate([60+i*2,80+i*2])]
        s=song(ns);a=analyze_structure(s,ns,'joint')
        preferred={n.id:n.pitch if n.pitch<=84 else n.pitch-12 for n in ns}
        def solve(cross):
            a.scores=np.asarray([.98 if b//2==a0//2+1 and ((a0%2)!=(b%2))==cross else .01 for a0,b in a.pairs])
            return optimize(a,preferred,0,(41,84))
        straight,sa=solve(False);cross,sb=solve(True)
        self.assertNotEqual(sa['selected_links'],sb['selected_links'])
        self.assertNotEqual(straight,cross)
        for result,info in ((straight,sa),(cross,sb)):
            self.assertEqual(set(result),{n.id for n in ns})
            self.assertTrue(all((result[n.id]-n.pitch)%12==0 for n in ns))

    def test_repeated_theme_links_use_original_intervals_and_rhythm(self):
        ns=[note(i,p,i*.25) for i,p in enumerate([60,62,65,64,72,74,77,76])]
        a=analyze_structure(song(ns),ns,'joint')
        self.assertTrue({(0,4),(1,5),(2,6),(3,7)}<=set(a.motifs))

    def test_repeated_theme_can_change_octave_layout(self):
        ns=[note(i,p,(i%4)*.25+(5 if i>=4 else 0)) for i,p in enumerate([68,70,73,72,80,82,85,84])]
        structure=analyze_structure(song(ns),ns,'joint')
        preferred={n.id:n.pitch if n.pitch<=84 else n.pitch-12 for n in ns}
        linked,_=optimize(structure,preferred,0,(41,84))
        structure.motifs=[]
        separate,_=optimize(structure,preferred,0,(41,84))
        self.assertNotEqual(linked,separate)
        self.assertEqual([linked[i]-ns[i].pitch for i in range(4)],
                         [linked[i]-ns[i].pitch for i in range(4,8)])

    def test_phrase_boundary_relaxes_continuity(self):
        ns=[note(i,p,i*.25) for i,p in enumerate([80,82,84,85])]
        structure=analyze_structure(song(ns),ns,'joint')
        preferred={0:80,1:82,2:84,3:73}
        connected,_=optimize(structure,preferred,0,(41,84))
        structure.boundaries[3]=1.
        separated,_=optimize(structure,preferred,0,(41,84))
        self.assertNotEqual(connected,separated)

    def test_overlapping_windows_report_valid_disjoint_paths(self):
        ns=[note(i*2+j,p,i*.25) for i in range(20) for j,p in enumerate([50+i%4,72+i%5])]
        a=analyze_structure(song(ns),ns,'joint')
        out,info=optimize(a,{n.id:n.pitch for n in ns},0,(41,84),window_notes=16,overlap_notes=8)
        links=info['selected_links']
        self.assertEqual(len({a for a,b in links}),len(links))
        self.assertEqual(len({b for a,b in links}),len(links))
        self.assertEqual(len(out),len(ns))

    def test_unavailable_model_is_explicit_not_silent_success(self):
        s=song([note(i,p,i*.2) for i,p in enumerate([40,64,86])])
        with patch('guitar_player.joint.get_features',side_effect=ModelUnavailable('test missing weights')):
            a=arrange(s,Config(arranger='model'));validate(a)
        self.assertFalse(a.stats['analysis']['model_used'])
        self.assertIn('test missing weights',a.stats['analysis']['fallback_reason'])
        self.assertTrue(any('test missing weights' in w for w in a.warnings))

    def test_auto_can_reject_a_worse_joint_layout(self):
        s=song([note(i,p,i*.25) for i,p in enumerate([80,82,84,85,84,82,80])])
        poor={n.id: n.pitch-12 if n.id%2 or n.pitch>84 else n.pitch for n in s.notes}
        with patch('guitar_player.joint.cached_optimize',return_value=(poor,{})):
            a=arrange(s,Config(arranger='joint'))
        self.assertTrue(a.stats['recommendation']['candidate'].startswith('baseline:'))
        self.assertEqual(a.stats['recommendation']['layout_backend'],'legacy')
        validate(a)


class RealModelTests(unittest.TestCase):
    @unittest.skipUnless(Path('models/m2bert/voice_edges.safetensors').exists() and importlib.util.find_spec('torch'),
                         'trained head or PyTorch not installed')
    def test_cached_features_use_stable_scores_after_restart(self):
        import torch
        from guitar_player.voice_edges import load_edge_head
        ns=[note(i*2+j,p,i*.25) for i in range(8) for j,p in enumerate([50+i%4,75+i%5])]
        h=np.random.default_rng(8721).normal(size=(len(ns),768)).astype(np.float32)
        original=torch.get_num_threads()
        try:
            torch.set_num_threads(1);load_edge_head.cache_clear()
            _,a,_=predict_edges(ns,h)
            torch.set_num_threads(8);load_edge_head.cache_clear()
            _,b,_=predict_edges(ns,h)
            np.testing.assert_array_equal(a,b)
        finally:
            torch.set_num_threads(original);load_edge_head.cache_clear()

    @unittest.skipUnless(Path('models/m2bert/encoder.safetensors').exists() and Path('models/m2bert/model_best.ckpt').exists(),
                         'both encoder files needed')
    def test_weights_only_encoder_matches_the_checkpoint(self):
        import shutil,tempfile
        from guitar_player import music_model
        s=song([note(i*2+j,p,i*.25) for i in range(8) for j,p in enumerate([50+i%4,75+i%5])])
        encoded=music_model.encode_notes(s,s.notes)
        with tempfile.TemporaryDirectory() as folder:
            for name in ('CP.pkl','config.json','model_best.ckpt'):shutil.copy(music_model.MODEL_DIR/name,Path(folder)/name)
            official=music_model.M2BertFeatures(directory=folder,device='cpu').extract(encoded)
        np.testing.assert_array_equal(music_model.M2BertFeatures(device='cpu').extract(encoded),official)

    @unittest.skipUnless(Path('models/m2bert/model_best.ckpt').exists() or Path('models/m2bert/encoder.safetensors').exists(),
                         'local model not installed')
    def test_real_model_features_and_trained_head(self):
        s=song([note(i*2+j,p,i*.25) for i in range(8) for j,p in enumerate([50+i%4,75+i%5])])
        e,h,info=get_features(s,s.notes)
        self.assertEqual(h.shape,(16,768));self.assertTrue(np.isfinite(h).all())
        self.assertGreater(float(np.std(h)),.01)
        index={n.id:n for n in s.notes};ns=[index[int(i)] for i in e.ids]
        pairs,p,metadata=predict_edges(ns,h)
        self.assertEqual(len(p),len(pairs));self.assertTrue(((p>=0)&(p<=1)).all())
        self.assertGreater(float(p.max()-p.min()),.05)
        # Explicit full adaptation: auto would play this in-range fixture as written.
        a=arrange(s,Config(mode='full',arranger='model'));validate(a)
        self.assertTrue(a.stats['joint']['model_used'])
        self.assertTrue(a.stats['joint']['head_sha256'])

    def test_voice_training_splits_are_disjoint_by_work_family(self):
        path=Path('data/training/voice_corpus.json')
        if not path.exists():self.skipTest('training sources not installed')
        rows=json.loads(path.read_text(encoding='utf8'))['pieces']
        families={name:{r['split_family'] for r in rows if r['split']==name} for name in ('train','valid','test')}
        self.assertFalse(families['train']&families['valid']);self.assertFalse(families['train']&families['test'])
        self.assertFalse(families['valid']&families['test'])
        self.assertEqual(len({r['signature'] for r in rows}),len(rows))


if __name__=='__main__':unittest.main()


class SpeedPathTests(unittest.TestCase):
    def test_structure_is_reused_for_the_same_song_and_tracks(self):
        from guitar_player.joint import analyze_structure
        s=song([note(i,90+i%5,i*.25) for i in range(12)])
        first=analyze_structure(s,s.notes,'joint')
        again=analyze_structure(s,list(reversed(s.notes)),'joint')
        self.assertTrue(again.metadata['structure_reused'])
        self.assertNotIn('structure_reused',first.metadata)
        self.assertIs(again.pairs,first.pairs)
        other=analyze_structure(s,s.notes[:6],'joint')
        self.assertNotIn('structure_reused',other.metadata)

    def test_cancelling_during_model_load_keeps_the_model(self):
        import threading
        from guitar_player import music_model as music
        from guitar_player.jobs import Cancelled, task_context
        cancel=threading.Event()

        class SlowModel:
            device='cpu'
            def __init__(self):cancel.set()   # the user changes a setting while loading
            def extract(self,encoded):raise AssertionError('should stop before inference')
        s=song([note(i,60+i,i*.25) for i in range(4)])
        with tempfile.TemporaryDirectory() as directory,patch.object(music,'CACHE_DIR',Path(directory)),\
                patch.object(music,'_backend',None),patch.object(music,'M2BertFeatures',SlowModel):
            with task_context(cancel,lambda stage:None),self.assertRaises(Cancelled):
                music.get_features(s,s.notes,use_cache=False)
            self.assertIsInstance(music._backend,SlowModel)


class PrepareTests(unittest.TestCase):
    def test_prepare_does_the_work_that_any_speed_reuses(self):
        import guitar_player.joint as joint
        from guitar_player.engine import prepare
        s=song([note(i,(97+i*7)%30+80,i*.31,source=f'0:{i%3}') for i in range(40)])
        calls=[]
        real=joint.optimize
        def counted(*args,**kwargs):
            calls.append(1);return real(*args,**kwargs)
        with tempfile.TemporaryDirectory() as directory,patch.object(joint,'optimize',counted),\
                patch.object(joint,'CACHE_ROOT',Path(directory)):
            prepare(s,Config(mode='full',arranger='joint'))
            self.assertEqual(len(calls),1)
            for speed in (1.5,.75):
                a=arrange(s,Config(mode='full',arranger='joint',speed=speed));validate(a)
                self.assertTrue(a.stats['joint']['plan_cache_hit'])
            self.assertEqual(len(calls),1)
