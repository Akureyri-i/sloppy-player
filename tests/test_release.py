import hashlib
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from guitar_player.cache import save_cache
from guitar_player.jobs import Cancelled, LatestWorker, checkpoint, task_context
from guitar_player.joint import analyze_structure, cached_optimize
from guitar_player.midi import MeterMap
from guitar_player.model import Config
from guitar_player.prefs import Preferences
from tests.test_core import note, song
from tools.setup_model import download


class WorkerTests(unittest.TestCase):
    def test_cancel_before_cold_model_load(self):
        from guitar_player.music_model import M2BertFeatures
        cancel=threading.Event();cancel.set()
        with task_context(cancel,lambda _:None):
            with self.assertRaises(Cancelled):M2BertFeatures()

    def test_superseded_jobs_stop_and_only_latest_pending_job_runs(self):
        messages=queue.Queue();worker=LatestWorker(messages)
        started=threading.Event();release=threading.Event();visited=[]
        def slow():
            started.set();release.wait(2);checkpoint();visited.append('old')
        try:
            worker.submit('arranged',1,slow)
            self.assertTrue(started.wait(2))
            worker.submit('arranged',2,lambda:visited.append('middle'))
            worker.submit('arranged',3,lambda:'latest')
            release.set()
            self.assertEqual(messages.get(timeout=2),('arranged',3,'latest'))
            self.assertEqual(visited,[])
            self.assertTrue(messages.empty())
        finally:release.set();worker.close();worker.thread.join(2)

    def test_error_recovery_progress_and_shutdown(self):
        messages=queue.Queue();worker=LatestWorker(messages)
        def fail():
            checkpoint('reading');raise ValueError('broken')
        worker.submit('loaded',1,fail)
        self.assertEqual(messages.get(timeout=2),('progress',1,'reading'))
        self.assertEqual(messages.get(timeout=2),('error',1,'broken'))
        worker.submit('loaded',2,lambda:'ok')
        self.assertEqual(messages.get(timeout=2),('loaded',2,'ok'))
        worker.close();worker.thread.join(2)
        self.assertFalse(worker.thread.is_alive())

    def test_cancellation_does_not_trigger_model_fallback(self):
        s=song([note(0,60)])
        with patch('guitar_player.joint.get_features',side_effect=Cancelled),patch('guitar_player.joint.candidate_pairs') as fallback:
            with self.assertRaises(Cancelled):analyze_structure(s,s.notes)
            fallback.assert_not_called()

    def test_cancelled_context_never_leaks_to_next_operation(self):
        cancel=threading.Event();cancel.set()
        with task_context(cancel,lambda _:None):
            with self.assertRaises(Cancelled):checkpoint()
        checkpoint()


class FailureTests(unittest.TestCase):
    def test_cache_replace_failure_keeps_existing_file_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'cache.json';path.write_text('old')
            with patch('guitar_player.cache.os.replace',side_effect=PermissionError):
                self.assertFalse(save_cache(path,lambda f:f.write('new')))
            self.assertEqual(path.read_text(),'old')
            self.assertEqual(list(Path(directory).iterdir()),[path])

    def test_readonly_feature_cache_still_returns_model_features(self):
        import guitar_player.music_model as music
        from types import SimpleNamespace
        s=song([note(0,60),note(1,62,.2)])
        features=np.ones((2,768),np.float32)
        backend=SimpleNamespace(device='cpu',extract=lambda _:features)
        with tempfile.TemporaryDirectory() as directory,patch.object(music,'CACHE_DIR',Path(directory)),patch.object(music,'_backend',backend):
            with patch('guitar_player.cache.os.replace',side_effect=PermissionError):
                _,actual,metadata=music.get_features(s,s.notes)
            np.testing.assert_array_equal(actual,features)
            self.assertEqual(metadata['backend'],'m2bert')
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_broken_compressed_feature_cache_is_rebuilt(self):
        import guitar_player.music_model as music
        from types import SimpleNamespace
        s=song([note(0,60)])
        backend=SimpleNamespace(device='cpu',extract=lambda _:np.ones((1,768),np.float32))
        with tempfile.TemporaryDirectory() as directory,patch.object(music,'CACHE_DIR',Path(directory)),patch.object(music,'_backend',backend):
            music.get_features(s,s.notes)
            path=next(Path(directory).glob('*.npz'));path.write_bytes(b'PK\x03\x04truncated')
            self.assertFalse(music.get_features(s,s.notes)[2]['cache_hit'])
            self.assertTrue(music.get_features(s,s.notes)[2]['cache_hit'])

    def test_layout_survives_cache_write_failure(self):
        s=song([note(901,35),note(902,89,.25)])
        structure=analyze_structure(s,s.notes,'joint')
        expected={901:47,902:77}
        with patch('guitar_player.joint.Path.exists',return_value=False),patch('guitar_player.joint.optimize',return_value=(expected,{})),patch('guitar_player.cache.os.replace',side_effect=PermissionError):
            self.assertEqual(cached_optimize(structure,expected,0,(41,84))[0],expected)

    def test_invalid_metadata_falls_back_without_losing_notes(self):
        s=song([note(0,60)])
        with patch('guitar_player.joint.get_features',side_effect=KeyError('metadata')):
            structure=analyze_structure(s,s.notes)
        self.assertFalse(structure.metadata['model_used'])
        self.assertEqual(structure.notes,s.notes)

    def test_invalid_meter_rejected_early(self):
        with self.assertRaisesRegex(ValueError,'拍号'):MeterMap([(0,0,4)],480)

    def test_invalid_preferences_use_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'prefs.json';prefs=Preferences(path)
            for entry in ({'octave':.5},{'max_voices':True},{'sources':'0:0'},{'mode':[]},{'speed':float('nan')}):
                path.write_text(json.dumps({'song':entry}))
                self.assertIsNone(prefs.get('song'),entry)
            prefs.save('song',Config(sources=('0:0',)))
            self.assertEqual(prefs.get('song'),Config(sources=('0:0',)))


class DownloadTests(unittest.TestCase):
    def test_bad_download_never_overwrites_working_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'model';path.write_bytes(b'good')
            with patch('tools.setup_model.urllib.request.urlopen',return_value=io.BytesIO(b'bad')):
                with self.assertRaises(ValueError):download('unused',path,hashlib.sha256(b'good').hexdigest())
            self.assertEqual(path.read_bytes(),b'good')
            self.assertEqual(list(Path(directory).iterdir()),[path])

    def test_interrupted_download_keeps_working_model(self):
        class Interrupted(io.BytesIO):
            def read(self,size=-1):raise OSError('network interrupted')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'model';path.write_bytes(b'good')
            with patch('tools.setup_model.urllib.request.urlopen',return_value=Interrupted()):
                with self.assertRaises(OSError):download('unused',path,'unused')
            self.assertEqual(path.read_bytes(),b'good')
            self.assertEqual(list(Path(directory).iterdir()),[path])

    def test_verified_download_replaces_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'model'
            with patch('tools.setup_model.urllib.request.urlopen',return_value=io.BytesIO(b'good')):
                download('unused',path,hashlib.sha256(b'good').hexdigest())
            self.assertEqual(path.read_bytes(),b'good')
            self.assertEqual(list(Path(directory).iterdir()),[path])


if __name__=='__main__':unittest.main()
