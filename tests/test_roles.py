"""Voice roles (guitar_player.roles): onset groups, geometry, rule fallback, head round trip."""
import json
import tempfile
import importlib.util
import unittest
from pathlib import Path

import numpy as np

from guitar_player import roles
from tests.test_core import note


def chord(i, start, pitches, source='0:0'):
    return [note(i+k, p, start, .5, source) for k, p in enumerate(pitches)]


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.notes = (chord(0, 0., [48, 64, 72], '1:0') + chord(3, .5, [50, 65, 74], '1:0')
                      + [note(6, 40, .01, 1., '2:1')])

    def test_onset_groups_join_near_starts(self):
        groups = roles.onset_groups(self.notes)
        self.assertEqual([sorted(g) for g in groups], [[0, 1, 2, 6], [3, 4, 5]])

    def test_geometry_marks_the_top_and_bottom(self):
        g = roles.geometric_features(self.notes)
        self.assertEqual(g.shape, (7, roles.GEOMETRY))
        self.assertEqual((g[2, 4], g[2, 5]), (1., 0.))          # 72: top of its group
        self.assertEqual((g[6, 4], g[6, 5]), (0., 1.))          # 40: bottom
        self.assertTrue(np.isfinite(g).all())

    def test_rule_takes_the_top_of_the_highest_track(self):
        p = roles.rule_roles(self.notes)
        self.assertEqual([i for i in range(7) if p[i, 0] == 1], [2, 5])
        self.assertTrue(np.allclose(p.sum(1), 1))

    @unittest.skipUnless(all(importlib.util.find_spec(m) for m in ('torch', 'onnx', 'onnxruntime')),
                         'needs PyTorch, onnx and ONNX Runtime')
    def test_head_round_trip(self):
        import torch
        from safetensors.torch import save_file
        torch.manual_seed(0)
        head = roles.build_role_head()
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = folder/'voice_roles.safetensors'
        save_file({k: v.contiguous() for k, v in head.state_dict().items()}, str(path))
        path.with_suffix('.json').write_text(json.dumps({'role_version': roles.ROLE_VERSION, 'temperature': 1.5}))
        features = np.random.default_rng(0).normal(size=(7, 768)).astype(np.float32)
        with torch.inference_mode():
            torch.onnx.export(head, (torch.randn(1, 7, 768), torch.randn(1, 7, roles.GEOMETRY)), str(path.with_suffix('.onnx')),
                              input_names=['features', 'geometry'], output_names=['logits'],
                              dynamic_axes={'features': {1: 'notes'}, 'geometry': {1: 'notes'}, 'logits': {1: 'notes'}},
                              opset_version=17, dynamo=False)
        p = roles.predict_roles(self.notes, features, path.with_suffix('.onnx'))
        self.assertEqual(p.shape, (7, 3))
        self.assertTrue(np.allclose(p.sum(1), 1, atol=1e-5))


if __name__ == '__main__':
    unittest.main()
