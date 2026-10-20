"""Write the M2BERT encoder's inference weights as safetensors.

The official model_best.ckpt also holds the optimizer state and the masked-LM
head from pre-training (about two thirds of its 749 MB); the player only needs
`state_dict`. The output loads into the same network with strict key checks and
gives the same features. model_best.ckpt is left untouched.

  python tools/export_encoder_weights.py [--source models/m2bert/model_best.ckpt] [--out models/m2bert/encoder.safetensors]
Prints the output's SHA-256 (guitar_player.music_model.WEIGHTS_SHA256).
"""
import argparse
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guitar_player.music_model import CHECKPOINT_SHA256, MODEL_DIR  # noqa: E402


def main():
    import torch
    from safetensors.torch import save_file
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=MODEL_DIR/'model_best.ckpt')
    parser.add_argument('--out', type=Path, default=MODEL_DIR/'encoder.safetensors')
    args = parser.parse_args()
    with args.source.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != CHECKPOINT_SHA256:
            raise SystemExit(f'{args.source} is not the official M2BERT checkpoint')
    state = torch.load(args.source, map_location='cpu', weights_only=True)['state_dict']
    save_file({k: v.contiguous().clone() for k, v in state.items()}, str(args.out),
              metadata={'source': 'M2BERT model_best.ckpt (state_dict)', 'source_sha256': CHECKPOINT_SHA256})
    with args.out.open('rb') as stream:
        print(args.out.name, args.out.stat().st_size, hashlib.file_digest(stream, 'sha256').hexdigest())


if __name__ == '__main__':
    main()
