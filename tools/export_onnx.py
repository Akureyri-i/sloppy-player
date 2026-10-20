"""Export the M2BERT encoder and the two trained heads to ONNX, for ONNX Runtime.

The player runs the models with ONNX Runtime; PyTorch and Transformers are only needed
here and for training. Needs the official checkpoint (or encoder.safetensors) in
models/m2bert/, PyTorch, Transformers and onnx.

  python tools/export_onnx.py [--heads] [--check]
Writes models/m2bert/encoder.onnx; with --heads also voice_edges.onnx and voice_roles.onnx
(after training them). --check then compares ONNX Runtime with PyTorch on random inputs
of several lengths and fails if the outputs differ by more than float rounding.
"""
import argparse
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guitar_player.music_model import MODEL_DIR, M2BertFeatures  # noqa: E402
from guitar_player.roles import GEOMETRY, load_role_head  # noqa: E402
from guitar_player.voice_edges import load_edge_head  # noqa: E402

OPSET = 17
EDGE_GEOMETRY = 16


def random_ids(vocab, length, rng):
    sizes = [len(vocab[k]) for k in ('Bar', 'Position', 'Pitch', 'Duration')]
    return np.stack([rng.integers(0, s, length) for s in sizes], axis=-1)[None].astype(np.int64)


def export(folder=MODEL_DIR, heads=False):
    import torch
    encoder = M2BertFeatures(directory=folder, device='cpu')
    rng = np.random.default_rng(0)
    with torch.inference_mode():
        torch.onnx.export(encoder.network, (torch.from_numpy(random_ids(encoder.vocab, 300, rng)),),
                          str(folder/'encoder.onnx'), input_names=['ids'], output_names=['features'],
                          dynamic_axes={'ids': {1: 'notes'}, 'features': {1: 'notes'}}, opset_version=OPSET, dynamo=False)
        if not heads:
            return encoder
        edges, _, _ = load_edge_head(str(folder/'voice_edges.safetensors'))
        n = 50
        torch.onnx.export(edges, (torch.randn(n, 768), torch.randn(n, 768), torch.randn(n, EDGE_GEOMETRY)),
                          str(folder/'voice_edges.onnx'), input_names=['left', 'right', 'geometry'], output_names=['logits'],
                          dynamic_axes={k: {0: 'pairs'} for k in ('left', 'right', 'geometry', 'logits')},
                          opset_version=OPSET, dynamo=False)
        roles, _ = load_role_head(str(folder/'voice_roles.safetensors'))
        torch.onnx.export(roles, (torch.randn(1, n, 768), torch.randn(1, n, GEOMETRY)),
                          str(folder/'voice_roles.onnx'), input_names=['features', 'geometry'], output_names=['logits'],
                          dynamic_axes={'features': {1: 'notes'}, 'geometry': {1: 'notes'}, 'logits': {1: 'notes'}},
                          opset_version=OPSET, dynamo=False)
    return encoder


def check(encoder, folder=MODEL_DIR):
    import onnxruntime as ort
    import torch
    options = ort.SessionOptions()
    rng = np.random.default_rng(1)
    worst = {}
    session = ort.InferenceSession(str(folder/'encoder.onnx'), options, providers=['CPUExecutionProvider'])
    for length in (1, 57, 300, 700, 1024):
        ids = random_ids(encoder.vocab, length, rng)
        with torch.inference_mode():
            expected = encoder.network(torch.from_numpy(ids)).numpy()
        got = session.run(None, {'ids': ids})[0]
        worst[f'encoder {length}'] = float(np.abs(got-expected).max())
    edges, _, _ = load_edge_head(str(folder/'voice_edges.safetensors'))
    session = ort.InferenceSession(str(folder/'voice_edges.onnx'), options, providers=['CPUExecutionProvider'])
    for n in (1, 4096):
        x = [rng.normal(size=s).astype(np.float32) for s in ((n, 768), (n, 768), (n, EDGE_GEOMETRY))]
        with torch.inference_mode():
            expected = edges(*map(torch.from_numpy, x)).numpy()
        worst[f'edges {n}'] = float(np.abs(session.run(None, dict(zip(('left', 'right', 'geometry'), x)))[0]-expected).max())
    roles, _ = load_role_head(str(folder/'voice_roles.safetensors'))
    session = ort.InferenceSession(str(folder/'voice_roles.onnx'), options, providers=['CPUExecutionProvider'])
    for n in (1, 2000):
        x = [rng.normal(size=s).astype(np.float32) for s in ((1, n, 768), (1, n, GEOMETRY))]
        with torch.inference_mode():
            expected = roles(*map(torch.from_numpy, x)).numpy()
        worst[f'roles {n}'] = float(np.abs(session.run(None, dict(zip(('features', 'geometry'), x)))[0]-expected).max())
    for name, value in worst.items():
        print(f'{name:14} max |ONNX - PyTorch| = {value:.2e}')
    if max(worst.values()) > 1e-3:
        raise SystemExit('ONNX outputs differ from PyTorch')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--heads', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    encoder = export(heads=args.heads)
    for name in ('encoder.onnx',)+(('voice_edges.onnx', 'voice_roles.onnx') if args.heads else ()):
        print(name, f'{(MODEL_DIR/name).stat().st_size/2**20:.1f} MiB')
    if args.check:
        check(encoder)


if __name__ == '__main__':
    main()
