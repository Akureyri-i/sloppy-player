"""Local M2BERT features, with identity-preserving CP encoding and disk cache.

The network supplies contextual features, NOT calibrated voice probabilities.
Connection probabilities are supplied by the separately trained edge head.
No upstream scripts, remote model code, or unrestricted pickle are executed.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import pickle
import threading
from zipfile import BadZipFile

import numpy as np
from .cache import CACHE_ROOT, save_cache
from .i18n import tr
from .jobs import checkpoint

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / 'models' / 'm2bert'
CACHE_DIR = CACHE_ROOT / 'music_features'
CHECKPOINT_SHA256 = 'cf15015f8ed1bdf94983812ffb3229e51548b676dc5ceda76b91a09056b40f63'
# encoder.safetensors: the checkpoint's state_dict alone (tools/export_encoder_weights.py), same features.
WEIGHTS_SHA256 = '63b563f8f3eafed8c9819eb2a93b3753da092275d33f0125a715a0a9c20127ca'
ENCODER_VERSION = 'cp-identity-meter-overlap-v1'
_lock = threading.RLock()
# Separate from _lock: a song whose features are cached must not wait for a preload.
_load_lock = threading.Lock()
_backend = None


class ModelUnavailable(RuntimeError):
    pass


class _DataOnlyUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        raise ValueError(tr('error.model.vocab_code'))


def vocabulary(directory=MODEL_DIR):
    # Official dictionary contains only nested dicts, strings and integers.
    try:
        data = _DataOnlyUnpickler(io.BytesIO((directory/'CP.pkl').read_bytes())).load()
        vocab=data[0]
        if not isinstance(vocab,dict) or not all(isinstance(vocab[k],dict) for k in ('Bar','Position','Pitch','Duration')):
            raise ValueError(tr('error.model.vocab_shape'))
        return vocab
    except (pickle.UnpicklingError,EOFError,KeyError,IndexError,TypeError) as exc:
        raise ModelUnavailable(tr('error.model.vocab')) from exc


@dataclass
class EncodedNotes:
    ids: np.ndarray
    tokens: np.ndarray
    bars: np.ndarray
    positions: np.ndarray
    clipped: np.ndarray


def encode_notes(song, notes, vocab=None):
    """Quantize a *view* of every note; never delete, merge or mutate originals.

    Positions use the nearest of the official 16 onset bins, durations the
    nearest 1/8-quarter bin. Each meter region starts a new bar at its change.
    A partial final bar is retained once (upstream can duplicate it at changes).
    """
    vocab = vocabulary() if vocab is None else vocab
    ordered = sorted(notes, key=lambda n:(n.tick,n.pitch,n.id))
    meters = {0:(4,4)}
    meters.update({int(t):(int(a),int(b)) for t,a,b in song.meters})
    ticks = sorted(meters)
    widths = [song.ticks_per_beat*4*meters[t][0]/meters[t][1] for t in ticks]
    starts = [0]
    for i in range(1,len(ticks)):
        starts.append(starts[-1]+math.ceil((ticks[i]-ticks[i-1])/widths[i-1]))
    ids=[]; tokens=[]; bars=[]; positions=[]; clipped=[]
    previous_bar=None
    for n in ordered:
        checkpoint()
        region=bisect_right(ticks,n.tick)-1
        width=widths[region]
        local=(n.tick-ticks[region])/width
        bar=starts[region]+math.floor(local+1e-10)
        phase=max(0.,local-math.floor(local+1e-10))
        # np.argmin gives the lower index on an exact tie, as upstream does.
        pos=int(np.argmin(np.abs(np.arange(16)/16-phase)))
        duration=max(1,n.end_tick-n.tick)*4/width
        dur=int(np.argmin(np.abs(np.arange(1,65)/8-duration)))
        pitch=min(107,max(22,n.pitch))
        values=('Bar New' if bar!=previous_bar else 'Bar Continue',
                f'Position {pos+1}/16',f'Pitch {pitch}',f'Duration {dur}')
        tokens.append([vocab[k][value] for k,value in zip(('Bar','Position','Pitch','Duration'),values)])
        ids.append(n.id);bars.append(bar);positions.append(phase)
        clipped.append(pitch!=n.pitch);previous_bar=bar
    return EncodedNotes(np.asarray(ids,dtype=np.int64),
        np.asarray(tokens,dtype=np.int64).reshape(-1,4),np.asarray(bars,dtype=np.int64),
        np.asarray(positions,dtype=np.float32),np.asarray(clipped,dtype=bool))


def window_starts(count, length=1024, overlap=256):
    if not 0<=overlap<length:raise ValueError('invalid overlap')
    starts=[];start=0
    while start<count:
        starts.append(start)
        if start+length>=count:break
        start+=length-overlap
    return starts


def build_network(config, vocab):
    """Reproduce the published four-attribute embedding architecture.

    Key names intentionally match the official checkpoint for strict loading.
    """
    import torch
    from torch import nn
    from transformers import AutoModel

    class AttributeEmbedding(nn.Module):
        def __init__(self,count):
            super().__init__();self.lut=nn.Embedding(count,256)
        def forward(self,x):return self.lut(x)*16.

    class MusicEncoder(nn.Module):
        def __init__(self):
            super().__init__()
            self.note_bert1=AutoModel.from_config(config,attn_implementation='sdpa')
            self.word_emb=nn.ModuleList([AttributeEmbedding(len(vocab[k]))
                for k in ('Bar','Position','Pitch','Duration')])
            self.in_linear=nn.Linear(1024,768)
        def forward(self,ids):
            x=self.in_linear(torch.cat([layer(ids[...,i]) for i,layer in enumerate(self.word_emb)],dim=-1))
            return self.note_bert1(inputs_embeds=x,attention_mask=torch.ones(ids.shape[:2],device=ids.device),
                                   output_hidden_states=False).last_hidden_state
    return MusicEncoder()


class M2BertFeatures:
    def __init__(self,directory=MODEL_DIR,device=None):
        checkpoint(tr('progress.model'))
        import torch
        from transformers import ModernBertConfig
        directory=Path(directory)
        # The released weights-only file, else the official checkpoint (a source checkout).
        weights_path,expected=directory/'encoder.safetensors',WEIGHTS_SHA256
        if not weights_path.is_file():weights_path,expected=directory/'model_best.ckpt',CHECKPOINT_SHA256
        if not weights_path.is_file():raise ModelUnavailable(tr('error.model.weights'))
        with weights_path.open('rb') as stream:
            digest=hashlib.file_digest(stream,'sha256').hexdigest()
        if digest!=expected:raise ModelUnavailable(tr('error.model.checksum'))
        self.vocab=vocabulary(directory)
        self.fingerprint=CHECKPOINT_SHA256+hashlib.sha256((directory/'CP.pkl').read_bytes()).hexdigest()
        config=ModernBertConfig(**json.loads((directory/'config.json').read_text()))
        config.vocab_size=10;config.num_hidden_layers=12;config.max_position_embeddings=1024
        config.attention_dropout=.1;config.mlp_dropout=.1;config.pad_token_id=-1
        config.reference_compile=False
        self.device=device or ('cuda' if torch.cuda.is_available() else 'cpu')
        torch.set_num_threads(min(4,os.cpu_count() or 1))
        self.network=build_network(config,self.vocab)
        if weights_path.suffix=='.safetensors':
            from safetensors.torch import load_file
            state=load_file(str(weights_path))
        else:
            # Restricted torch loader: refuse arbitrary globals from model files.
            state=torch.load(weights_path,map_location='cpu',weights_only=True)['state_dict']
        self.network.load_state_dict(state,strict=True)
        del state
        self.network.to(self.device).eval()
        # No cancellation check here: a loaded model is kept for the next request
        # instead of being thrown away (loading takes far longer than inference).

    def run(self,ids):
        import torch
        with torch.inference_mode():
            return self.network(torch.as_tensor(ids,device=self.device))[0].float().cpu().numpy()

    def extract(self,encoded,length=1024,overlap=256):
        return extract(self,encoded,length,overlap)


class OnnxFeatures:
    """The encoder exported to ONNX (tools/export_onnx.py), run by ONNX Runtime on the CPU.
    What the player ships; PyTorch is then only needed to export and to train."""
    device='cpu'

    def __init__(self,directory=MODEL_DIR):
        checkpoint(tr('progress.model'))
        directory=Path(directory)
        if not (directory/'encoder.onnx').is_file():raise ModelUnavailable(tr('error.model.weights'))
        self.vocab=vocabulary(directory)
        self.session=onnx_session(directory/'encoder.onnx')

    def run(self,ids):
        return self.session.run(None,{'ids':ids.astype(np.int64)})[0][0]

    def extract(self,encoded,length=1024,overlap=256):
        return extract(self,encoded,length,overlap)


def onnx_session(path):
    """ONNX Runtime on the CPU with a fixed thread count: the same input gives the same bytes
    on every run, which the layout cache (keyed by link scores) relies on."""
    import onnxruntime as ort
    options=ort.SessionOptions()
    options.intra_op_num_threads=min(4,os.cpu_count() or 1);options.inter_op_num_threads=1
    return ort.InferenceSession(str(path),options,providers=['CPUExecutionProvider'])


def extract(backend,encoded,length=1024,overlap=256):
    """Features of every note: overlapping windows, each window's edges weighted down."""
    count=len(encoded.ids)
    total=np.zeros((count,768),np.float32);weight=np.zeros(count,np.float32)
    starts=window_starts(count,length,overlap)
    for number,start in enumerate(starts,1):
        checkpoint(tr('progress.context', done=number, total=len(starts)))
        end=min(count,start+length)
        result=backend.run(encoded.tokens[start:end][None])
        checkpoint()
        # Downweight each window's less-contextual edges; keep every note.
        j=np.arange(end-start)
        w=np.minimum(np.minimum(j+1,end-start-j),max(1,overlap//2)).astype(np.float32)
        total[start:end]+=result*w[:,None];weight[start:end]+=w
    if count:total/=weight[:,None]
    return total


def preload():
    """Load the encoder ahead of the first song (call from a background thread).

    Failures are left for the normal path to report when a song is arranged.
    """
    try:
        _model()
        return True
    except Exception:
        return False


def _model():
    global _backend
    with _load_lock:
        if _backend is None:
            try:_backend=OnnxFeatures() if onnx_encoder() else M2BertFeatures()
            except (ImportError,OSError,ValueError,RuntimeError) as exc:
                raise ModelUnavailable(str(exc)) from exc
        return _backend


def onnx_encoder():
    """The release runs the exported encoder; a source checkout without it uses PyTorch."""
    return (MODEL_DIR/'encoder.onnx').is_file()


def get_features(song,notes,use_cache=True):
    checkpoint(tr('progress.cache'))
    encoded=encode_notes(song,notes)
    payload=json.dumps({'encoder':ENCODER_VERSION,'weights':CHECKPOINT_SHA256,'runtime':'onnx' if onnx_encoder() else 'torch',
        'vocab':hashlib.sha256((MODEL_DIR/'CP.pkl').read_bytes()).hexdigest(),
        'config':hashlib.sha256((MODEL_DIR/'config.json').read_bytes()).hexdigest(),
        'meters':song.meters,'tpb':song.ticks_per_beat,
        'notes':[(n.id,n.tick,n.end_tick,n.pitch) for n in sorted(notes,key=lambda n:n.id)]},sort_keys=True).encode()
    key=hashlib.sha256(payload).hexdigest()
    path=CACHE_DIR/(key+'.npz')
    with _lock:
        checkpoint()
        if use_cache and path.exists():
            try:
                with np.load(path,allow_pickle=False) as data:
                    features=data['features']
                    if features.shape==(len(encoded.ids),768) and features.dtype==np.float32 and np.array_equal(data['ids'],encoded.ids) and np.isfinite(features).all():
                        return encoded,features,{'backend':'m2bert','cache_hit':True,'weights_sha256':CHECKPOINT_SHA256}
            except (OSError,ValueError,KeyError,EOFError,BadZipFile):pass
        backend=_model()
        checkpoint()
        features=backend.extract(encoded)
        if not np.isfinite(features).all():raise ModelUnavailable(tr('error.model.output'))
        checkpoint()
        if use_cache:
            save_cache(path,lambda f:np.savez_compressed(f,ids=encoded.ids,features=features),binary=True)
        return encoded,features,{'backend':'m2bert','cache_hit':False,'device':backend.device,
                                'weights_sha256':CHECKPOINT_SHA256}
