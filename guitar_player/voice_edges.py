"""Candidate voice links and a supervised head over frozen M2BERT features."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from .i18n import tr
from .music_model import MODEL_DIR, ModelUnavailable
from .jobs import checkpoint

EDGE_VERSION='voice-next-v1'


def candidate_pairs(notes, max_groups=6, max_beats=4., max_candidates=24):
    """Indices into onset-sorted notes; source/track labels never gate edges."""
    pairs=[]
    for i,a in enumerate(notes):
        checkpoint()
        candidates=[];groups=0;last=None
        for j in range(i+1,len(notes)):
            b=notes[j];gap=b.beat-a.beat
            if gap>max_beats+1e-8:break
            if b.start-a.start<=.002:continue
            if last is None or abs(b.start-last)>.002:
                groups+=1;last=b.start
            if groups>max_groups:break
            if abs(b.pitch-a.pitch)<=24:
                candidates.append((gap*.4+abs(b.pitch-a.pitch)/12,j))
        pairs.extend((i,j) for _,j in sorted(candidates)[:max_candidates])
    return np.asarray(pairs,dtype=np.int64).reshape(-1,2)


def geometric_features(notes,pairs):
    features=[]
    tpb=next((n.tick/n.beat for n in notes if n.beat>0 and n.tick>0),480.)
    for i,j in pairs:
        checkpoint()
        a,b=notes[i],notes[j]
        db=b.beat-a.beat
        # Durations in quarter beats, preserving tempo-independent score context.
        duration_a=max(.01,(a.end_tick-a.tick)/tpb)
        duration_b=max(.01,(b.end_tick-b.tick)/tpb)
        delta=b.pitch-a.pitch
        features.append([delta/12,abs(delta)/12,db/4,
            min(4,duration_a)/4,min(4,duration_b)/4,
            np.clip(db-duration_a,-4,4)/4,float(delta==0),
            (a.pitch-60)/24,(b.pitch-60)/24,
            math.sin(a.pitch*math.tau/12),math.cos(a.pitch*math.tau/12),
            math.sin(b.pitch*math.tau/12),math.cos(b.pitch*math.tau/12),
            float(abs(db-round(db))<.02),float(b.beat%1<.02),
            min(8,j-i)/8])
    return np.asarray(features,dtype=np.float32).reshape(-1,16)


def geometric_probabilities(features):
    if not len(features):return np.zeros(0,np.float32)
    # Explicit heuristic baseline; these are scores, not empirically calibrated probabilities.
    logits=2.2-2*features[:,1]-2*features[:,2]-3*np.abs(features[:,5])+.4*features[:,6]
    return 1/(1+np.exp(-logits))


def build_edge_head(use_model=True):
    import torch
    from torch import nn
    from torch.nn import functional as F

    class EdgeHead(nn.Module):
        def __init__(self):
            super().__init__();self.use_model=use_model
            if use_model:self.projection=nn.Linear(768,32,bias=False)
            self.classifier=nn.Sequential(nn.Linear(144 if use_model else 16,64),nn.GELU(),
                                          nn.Linear(64,32),nn.GELU(),nn.Linear(32,1))
        def forward(self,left,right,geometry):
            if self.use_model:
                a=self.projection(F.layer_norm(left,(768,)))
                b=self.projection(F.layer_norm(right,(768,)))
                geometry=torch.cat([a,b,torch.abs(a-b),a*b,geometry],dim=-1)
            return self.classifier(geometry).squeeze(-1)
    return EdgeHead()


def edge_metadata(path):
    """The head's JSON next to its weights, checked."""
    metadata=json.loads(Path(path).with_suffix('.json').read_text(encoding='utf8'))
    if metadata['edge_version']!=EDGE_VERSION:raise ModelUnavailable(tr('error.edges.version'))
    temperature=metadata.get('temperature')
    if not isinstance(temperature,(float,int)) or not math.isfinite(temperature) or temperature<=0:
        raise ModelUnavailable(tr('error.edges.temperature'))
    return metadata


@lru_cache(maxsize=2)
def load_edge_head(path_string):
    """The PyTorch head from its safetensors weights (training and ONNX export)."""
    from safetensors.torch import load_file
    from safetensors import SafetensorError
    path=Path(path_string)
    if not path.is_file():raise ModelUnavailable(tr('error.edges.missing'))
    head=build_edge_head()
    try:head.load_state_dict(load_file(path),strict=True)
    except SafetensorError as exc:raise ModelUnavailable(tr('error.edges.broken')) from exc
    head.eval()
    return head,edge_metadata(path),hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=2)
def edge_session(path_string):
    """The head exported to ONNX (tools/export_onnx.py), as the player runs it."""
    from .music_model import onnx_session
    path=Path(path_string)
    if not path.is_file():raise ModelUnavailable(tr('error.edges.missing'))
    metadata=edge_metadata(path)
    try:session=onnx_session(path)
    except (RuntimeError,ValueError) as exc:raise ModelUnavailable(tr('error.edges.broken')) from exc
    return session,metadata,hashlib.sha256(path.read_bytes()).hexdigest()


def predict_edges(notes,features,path=MODEL_DIR/'voice_edges.onnx'):
    checkpoint(tr('progress.edges'))
    pairs=candidate_pairs(notes);geometry=geometric_features(notes,pairs).astype(np.float32)
    session,metadata,digest=edge_session(str(path))
    predictions=[]
    for start in range(0,len(pairs),4096):
        p=pairs[start:start+4096]
        logits=session.run(None,{'left':features[p[:,0]],'right':features[p[:,1]],
                                 'geometry':geometry[start:start+4096]})[0]
        predictions.extend((1/(1+np.exp(-logits/metadata['temperature']))).tolist())
    if not np.isfinite(predictions).all():raise ModelUnavailable(tr('error.edges.output'))
    return pairs,np.asarray(predictions,np.float32),{'head_sha256':digest,
        'training_domain':metadata['training_domain'],'temperature':metadata['temperature']}
