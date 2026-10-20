"""Voice roles for 吉他化: melody / secondary melody (bridge) / accompaniment per note.

A small head over frozen M2BERT features, trained on POP909's MELODY / BRIDGE /
PIANO tracks (tools/train_role_head.py). Without the model, a rule gives the
same output: the top note of the highest track's onset groups is melody.
"""
from __future__ import annotations

from functools import lru_cache
import json
import math
from pathlib import Path

import numpy as np

from .i18n import tr
from .music_model import MODEL_DIR, ModelUnavailable

ROLE_VERSION = 'voice-role-v2'      # v2: bidirectional GRU over the song's notes
ROLES = ('melody', 'bridge', 'accompaniment')
GROUP_TOLERANCE = .03       # s: notes starting this close form one onset group
GEOMETRY = 14


def onset_groups(notes):
    """Indices of notes (sorted by start) grouped by near-equal start."""
    order = sorted(range(len(notes)), key=lambda i: (notes[i].start, -notes[i].pitch))
    groups, current = [], []
    for i in order:
        if current and notes[i].start-notes[current[0]].start > GROUP_TOLERANCE:
            groups.append(current)
            current = []
        current.append(i)
    if current:
        groups.append(current)
    return groups


def geometric_features(notes):
    """Per-note context that does not depend on tracks, velocities or names."""
    n = len(notes)
    out = np.zeros((n, GEOMETRY), np.float32)
    starts = np.array([x.start for x in notes])
    pitches = np.array([x.pitch for x in notes], np.float32)
    tpb = next((x.tick/x.beat for x in notes if x.beat > 0 and x.tick > 0), 480.)
    for group in onset_groups(notes):
        ranked = sorted(group, key=lambda i: -notes[i].pitch)
        size = len(group)
        mean = float(np.mean([notes[i].pitch for i in group]))
        for rank, i in enumerate(ranked):
            x = notes[i]
            beats = max(.01, (x.end_tick-x.tick)/tpb)
            out[i, :8] = [(x.pitch-60)/24, rank/max(1, size-1), (size-1-rank)/max(1, size-1), min(size, 8)/8,
                          float(rank == 0), float(rank == size-1), math.log2(min(8, beats))/3, (x.pitch-mean)/12]
            out[i, 8] = (x.beat % 1)
            out[i, 9] = float(x.beat % 1 < .02)
    # Local context: the note against everything sounding within +-1 beat.
    order = np.argsort(starts, kind='stable')
    sorted_starts = starts[order]
    for i, x in enumerate(notes):
        a, b = np.searchsorted(sorted_starts, [x.start-.6, x.start+.6])
        near = pitches[order[a:b]]
        out[i, 10] = (x.pitch-np.median(near))/12
        out[i, 11] = (x.pitch-near.max())/12
        out[i, 12] = float(x.pitch >= near.max())
        out[i, 13] = min(len(near), 24)/24
    return out


def build_role_head(use_model=True):
    import torch
    from torch import nn
    from torch.nn import functional as F

    class RoleHead(nn.Module):
        """Per note: projected M2BERT features + geometry, then a bidirectional GRU
        over the song's notes in onset order (melody is a property of the line)."""
        def __init__(self):
            super().__init__()
            self.use_model = use_model
            if use_model:
                self.projection = nn.Linear(768, 64, bias=False)
            self.inner = nn.Sequential(nn.Linear((64 if use_model else 0)+GEOMETRY, 64), nn.GELU())
            self.sequence = nn.GRU(64, 64, batch_first=True, bidirectional=True)
            self.out = nn.Sequential(nn.Linear(192, 64), nn.GELU(), nn.Linear(64, len(ROLES)))

        def forward(self, features, geometry):          # [batch, notes, ...]
            if self.use_model:
                geometry = torch.cat([self.projection(F.layer_norm(features, (768,))), geometry], dim=-1)
            x = self.inner(geometry)
            s, _ = self.sequence(x)
            return self.out(torch.cat([s, x], dim=-1))
    return RoleHead()


@lru_cache(maxsize=2)
def load_role_head(path_string):
    from safetensors.torch import load_file
    path = Path(path_string)
    if not path.is_file():
        raise ModelUnavailable(tr('error.roles.missing'))
    head = build_role_head()
    head.load_state_dict(load_file(path), strict=True)
    head.eval()
    metadata = json.loads(path.with_suffix('.json').read_text(encoding='utf8'))
    if metadata['role_version'] != ROLE_VERSION:
        raise ModelUnavailable(tr('error.roles.version'))
    return head, metadata


def predict_roles(notes, features, path=MODEL_DIR/'voice_roles.safetensors'):
    """[len(notes), 3] probabilities (ROLES order); notes in onset order, row-aligned with features."""
    import torch
    head, metadata = load_role_head(str(path))
    with torch.inference_mode():
        logits = head(torch.from_numpy(features)[None], torch.from_numpy(geometric_features(notes))[None])[0]
        return torch.softmax(logits/metadata['temperature'], dim=-1).numpy()


def rule_roles(notes, preferred=None):
    """Without the model (or with a melody track chosen in the settings): in each onset group
    the top note of that track (default: the highest-sounding one) is melody."""
    by_source = {}
    for x in notes:
        by_source.setdefault(x.source, []).append(x.pitch)
    upper = preferred if preferred in by_source else max(by_source, key=lambda s: np.mean(by_source[s]))
    out = np.tile(np.array([0., 0., 1.], np.float32), (len(notes), 1))
    for group in onset_groups(notes):
        top = [i for i in group if notes[i].source == upper] or group
        best = max(top, key=lambda i: notes[i].pitch)
        out[best] = [1., 0., 0.]
    return out
