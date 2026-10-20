"""Train the voice-role head (melody / bridge / accompaniment) on POP909.

POP909 (MIT, data/training/pop909/POP909/<nnn>/<nnn>.mid) names its tracks
MELODY, BRIDGE and PIANO; each note's label is its track. The model never sees
tracks: M2BERT encodes all notes as one stream, and roles.geometric_features
uses no track, velocity or name. Split by song number: 8 train, 1 valid, 1 test
in every 10. Frozen M2BERT features; a geometry-only head is trained on the same
split as a control.

  .venv/Scripts/python.exe tools/train_role_head.py            features + training
  .venv/Scripts/python.exe tools/train_role_head.py --reuse-features

Writes models/m2bert/voice_roles.safetensors + .json and data/training/role_training.json.
Features (float16, ~2 GB) go to .cache/role_features/ (rebuildable).
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import torch
from torch.nn import functional as F
from safetensors.torch import save_file

from guitar_player.midi import read_midi
from guitar_player.music_model import CHECKPOINT_SHA256, MODEL_DIR, get_features
from guitar_player.roles import ROLE_VERSION, ROLES, build_role_head, geometric_features

ROOT = Path(__file__).resolve().parents[1]
POP909 = ROOT/'data'/'training'/'pop909'/'POP909'
FEATURES = ROOT/'.cache'/'role_features'
LABEL = {'MELODY': 0, 'BRIDGE': 1, 'PIANO': 2}
SEED = 9091


def split_of(number):
    return 'test' if number % 10 == 9 else 'valid' if number % 10 == 8 else 'train'


def prepare():
    FEATURES.mkdir(parents=True, exist_ok=True)
    parts = {name: dict(h=[], g=[], y=[], songs=[]) for name in ('train', 'valid', 'test')}
    folders = sorted(p for p in POP909.iterdir() if p.is_dir())
    for k, folder in enumerate(folders, 1):
        song = read_midi(str(folder/f'{folder.name}.mid'))
        track = {s.id: s.name.strip().upper() for s in song.sources}
        notes = [n for n in song.notes if track.get(n.source) in LABEL]
        encoded, h, _ = get_features(song, notes, use_cache=False)
        by_id = {n.id: n for n in notes}
        ordered = [by_id[int(i)] for i in encoded.ids]
        part = parts[split_of(int(folder.name))]
        part['h'].append(h.astype(np.float16))
        part['g'].append(geometric_features(ordered))
        part['y'].append(np.array([LABEL[track[n.source]] for n in ordered], np.int64))
        part['songs'].append(folder.name)
        if k % 50 == 0 or k == len(folders):
            print('features', k, '/', len(folders), flush=True)
    for name, part in parts.items():
        for key in ('h', 'g', 'y'):
            np.save(FEATURES/f'{name}_{key}.npy', np.concatenate(part[key]))
        (FEATURES/f'{name}.json').write_text(json.dumps({'songs': part['songs']}), encoding='utf8')


def song_lengths(songs):
    """Labelled notes per song, in the saved order (to cut the stacked arrays back into songs)."""
    lengths = []
    for number in songs:
        song = read_midi(str(POP909/number/f'{number}.mid'))
        track = {s.id: s.name.strip().upper() for s in song.sources}
        lengths.append(sum(track.get(n.source) in LABEL for n in song.notes))
    return lengths


def load(device):
    data = {}
    for name in ('train', 'valid', 'test'):
        d = {key: torch.from_numpy(np.load(FEATURES/f'{name}_{key}.npy')) for key in ('h', 'g', 'y')}
        meta = json.loads((FEATURES/f'{name}.json').read_text(encoding='utf8'))
        if 'lengths' not in meta:
            meta['lengths'] = song_lengths(meta['songs'])
            (FEATURES/f'{name}.json').write_text(json.dumps(meta), encoding='utf8')
        assert sum(meta['lengths']) == len(d['y'])
        bounds = np.cumsum([0]+meta['lengths'])
        data[name] = dict(songs=meta['songs'], y=d['y'].to(device),
                          parts=[(d['h'][a:b].to(device).float(), d['g'][a:b].to(device), d['y'][a:b].to(device))
                                 for a, b in zip(bounds, bounds[1:])])
    return data


def batches(parts, size, shuffle):
    """Padded batches of whole songs: (features, geometry, labels with -1 padding)."""
    order = torch.randperm(len(parts)).tolist() if shuffle else list(range(len(parts)))
    for k in range(0, len(order), size):
        chosen = [parts[i] for i in order[k:k+size]]
        h = torch.nn.utils.rnn.pad_sequence([c[0] for c in chosen], batch_first=True)
        g = torch.nn.utils.rnn.pad_sequence([c[1] for c in chosen], batch_first=True)
        y = torch.nn.utils.rnn.pad_sequence([c[2] for c in chosen], batch_first=True, padding_value=-1)
        yield h, g, y


def metrics(logits, y, temperature=1.):
    p = torch.softmax(logits/temperature, dim=-1)
    pred = p.argmax(-1)
    out = dict(accuracy=float((pred == y).float().mean()),
               nll=float(F.cross_entropy(logits/temperature, y)))
    confusion = torch.zeros(3, 3, dtype=torch.long)
    for t in range(3):
        for q in range(3):
            confusion[t, q] = int(((y == t) & (pred == q)).sum())
    out['confusion'] = confusion.tolist()          # rows: truth, columns: prediction
    for k, role in enumerate(ROLES):
        tp = confusion[k, k].item()
        precision = tp/max(1, confusion[:, k].sum().item())
        recall = tp/max(1, confusion[k, :].sum().item())
        out[role] = dict(precision=precision, recall=recall, f1=2*precision*recall/max(1e-9, precision+recall))
    # Melody vs the rest, the distinction 吉他化 depends on most.
    out['melody_vs_rest_accuracy'] = float(((pred == 0) == (y == 0)).float().mean())
    return out


def fit(data, use_model, epochs, device):
    torch.manual_seed(SEED)
    head = build_role_head(use_model).to(device)
    optimizer = torch.optim.AdamW(head.parameters(), lr=.001, weight_decay=.01)
    counts = torch.bincount(data['train']['y'], minlength=3).float()
    weights = (counts.sum()/(3*counts)).to(device)

    def logits_of(name):
        head.eval()
        with torch.inference_mode():
            return torch.cat([head(h[None], g[None])[0] for h, g, _ in data[name]['parts']])

    best, best_loss, history = None, float('inf'), []
    for epoch in range(epochs):
        head.train()
        for h, g, y in batches(data['train']['parts'], 8, True):
            logits = head(h, g)
            loss = F.cross_entropy(logits.reshape(-1, 3), y.reshape(-1), weight=weights, ignore_index=-1)
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(head.parameters(), 1.)
            optimizer.step()
        m = metrics(logits_of('valid'), data['valid']['y'])
        history.append(dict(epoch=epoch+1, nll=m['nll'], accuracy=m['accuracy'], melody_f1=m['melody']['f1']))
        if m['nll'] < best_loss:
            best_loss, best = m['nll'], deepcopy(head.state_dict())
        print('model' if use_model else 'geometry', history[-1], flush=True)
    head.load_state_dict(best)
    valid = logits_of('valid')
    temperature = float(min(np.geomspace(.5, 3., 61),
                            key=lambda t: float(F.cross_entropy(valid/t, data['valid']['y']))))
    result = dict(role_version=ROLE_VERSION, roles=list(ROLES), temperature=temperature, epochs=epochs, seed=SEED,
                  training_domain='POP909 MELODY / BRIDGE / PIANO tracks, merged anonymous stream; whole songs in onset order',
                  frozen_encoder_sha256=CHECKPOINT_SHA256 if use_model else None,
                  selection='lowest validation NLL; temperature on validation only', history=history,
                  splits={name: dict(metrics(logits_of(name), data[name]['y'], temperature),
                                     songs=len(data[name]['songs']), notes=len(data[name]['y']))
                          for name in ('train', 'valid', 'test')})
    return head, result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--reuse-features', action='store_true')
    parser.add_argument('--epochs', type=int, default=40)
    args = parser.parse_args()
    if not args.reuse_features:
        prepare()
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    data = load(device)
    results = {}
    for name, use_model in (('geometry', False), ('m2bert', True)):
        head, results[name] = fit(data, use_model, args.epochs, device)
        print(name, 'TEST', json.dumps(results[name]['splits']['test']), flush=True)
    save_file({k: v.cpu().contiguous() for k, v in head.state_dict().items()}, str(MODEL_DIR/'voice_roles.safetensors'))
    (MODEL_DIR/'voice_roles.json').write_text(json.dumps(results['m2bert'], indent=2), encoding='utf8')
    out = ROOT/'data'/'training'
    out.mkdir(parents=True, exist_ok=True)
    (out/'role_training.json').write_text(json.dumps(results, indent=2), encoding='utf8')


if __name__ == '__main__':
    main()
