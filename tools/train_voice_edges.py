"""Train/validate/test a voice-successor head on authored SATB identities.

Frozen actual M2BERT features; test works never select weights or temperature.
Also trains a geometry-only head on the identical split as a diagnostic control.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import torch
from torch.nn import functional as F
from safetensors.torch import save_file

from guitar_player.model import Note,Song
from guitar_player.music_model import get_features,MODEL_DIR,CHECKPOINT_SHA256
from guitar_player.voice_edges import candidate_pairs,geometric_features,build_edge_head,EDGE_VERSION


def as_song(record):
    notes=[Note(i,int(p),80,float(a)/2,float(b)/2,round(a*480),round(b*480),float(a),.4,'0:0',0)
           for i,(a,b,p,_,_) in enumerate(record['notes'])]
    return Song(record['name'],notes,[],max(n.end for n in notes),480,
                meters=tuple(tuple(x) for x in record['meters']))


def prepare(records):
    splits={name:dict(h=[],pairs=[],g=[],y=[],query=[],works=[],positive_total=0,positive_covered=0) for name in ('train','valid','test')}
    for index,record in enumerate(records):
        s=as_song(record);encoded,h,_=get_features(s,s.notes)
        ns=[s.notes[int(i)] for i in encoded.ids]
        voice=[record['notes'][int(i)][3] for i in encoded.ids]
        pairs=candidate_pairs(ns);g=geometric_features(ns,pairs)
        successors={};last={}
        for j,v in enumerate(voice):
            if v in last:successors[last[v]]=j
            last[v]=j
        y=np.asarray([successors.get(int(i))==int(j) for i,j in pairs],np.float32)
        d=splits[record['split']];base=sum(len(a) for a in d['h'])
        d['h'].append(h);d['pairs'].append(pairs+base);d['g'].append(g);d['y'].append(y)
        d['query'].append(pairs[:,0]+base);d['works'].append(record['name'])
        d['positive_total']+=len(successors);d['positive_covered']+=int(y.sum())
        if (index+1)%20==0 or index+1==len(records):print('features',index+1,'/',len(records),flush=True)
    root=Path('data/training/voice_features');root.mkdir(exist_ok=True)
    for name,d in splits.items():
        np.savez_compressed(root/(name+'.npz'),**{k:np.concatenate(d[k]) for k in ('h','pairs','g','y','query')})
        (root/(name+'.json')).write_text(json.dumps({k:d[k] for k in ('works','positive_total','positive_covered')},indent=2),encoding='utf8')


def metrics(logits,y,queries,temperature=1):
    p=1/(1+np.exp(-np.clip(logits/temperature,-40,40)))
    positive=p>=.5;truth=y>.5
    tp=int((positive&truth).sum());fp=int((positive&~truth).sum());fn=int((~positive&truth).sum())
    # Top-1 continuation among queries with an observed true successor.
    eligible={};best={}
    for q,label,value in zip(queries,y,p):
        eligible[int(q)]=eligible.get(int(q),False) or bool(label)
        if q not in best or value>best[q][0]:best[q]=(value,bool(label))
    n=sum(eligible.values());correct=sum(best[q][1] for q in best if eligible[q])
    ece=0.
    for low in np.arange(0,1,.1):
        mask=(p>=low)&(p<low+.1)
        if mask.any():ece+=mask.mean()*abs(float(p[mask].mean()-y[mask].mean()))
    return dict(precision=tp/max(1,tp+fp),recall=tp/max(1,tp+fn),f1=2*tp/max(1,2*tp+fp+fn),
        successor_top1=correct/max(1,n),queries=n,brier=float(np.mean((p-y)**2)),ece=ece,
        nll=float(np.mean(np.logaddexp(0,logits/temperature)-y*logits/temperature)))


def fit(use_model,epochs):
    torch.manual_seed(8721);np.random.seed(8721);torch.set_num_threads(4)
    device='cuda' if torch.cuda.is_available() else 'cpu';data={}
    for name in ('train','valid','test'):
        with np.load(Path('data/training/voice_features')/(name+'.npz'),allow_pickle=False) as z:
            data[name]={k:torch.from_numpy(z[k]).to(device) for k in z.files}
    head=build_edge_head(use_model).to(device)
    optimizer=torch.optim.AdamW(head.parameters(),lr=.0007,weight_decay=.01)
    def evaluate(name):
        d=data[name];chunks=[];head.eval()
        with torch.inference_mode():
            for start in range(0,len(d['y']),8192):
                p=d['pairs'][start:start+8192]
                chunks.append(head(d['h'][p[:,0]],d['h'][p[:,1]],d['g'][start:start+8192]).cpu().numpy())
        return np.concatenate(chunks)
    best=None;best_loss=float('inf');history=[];d=data['train']
    for epoch in range(epochs):
        head.train();order=torch.randperm(len(d['y']),device=device)
        for batch in order.split(2048):
            p=d['pairs'][batch];logits=head(d['h'][p[:,0]],d['h'][p[:,1]],d['g'][batch])
            loss=F.binary_cross_entropy_with_logits(logits,d['y'][batch])
            optimizer.zero_grad();loss.backward();optimizer.step()
        logits=evaluate('valid');m=metrics(logits,data['valid']['y'].cpu().numpy(),data['valid']['query'].cpu().numpy())
        history.append(m)
        if m['nll']<best_loss:best_loss=m['nll'];best=deepcopy(head.state_dict())
        if (epoch+1)%5==0:print('model' if use_model else 'geometry','epoch',epoch+1,m,flush=True)
    head.load_state_dict(best)
    valid_logits=evaluate('valid');vy=data['valid']['y'].cpu().numpy()
    temperatures=np.geomspace(.5,3.,81)
    temperature=float(min(temperatures,key=lambda t:np.mean(np.logaddexp(0,valid_logits/t)-vy*valid_logits/t)))
    result={'edge_version':EDGE_VERSION,'training_domain':'authored Bach SATB voice successors; merged anonymous track',
        'frozen_encoder_sha256':CHECKPOINT_SHA256 if use_model else None,'temperature':temperature,
        'epochs':epochs,'seed':8721,'selection':'lowest validation BCE; temperature on validation only',
        'history':history,'splits':{}}
    for name in ('train','valid','test'):
        result['splits'][name]=metrics(evaluate(name),data[name]['y'].cpu().numpy(),data[name]['query'].cpu().numpy(),temperature)
        result['splits'][name].update(json.loads((Path('data/training/voice_features')/(name+'.json')).read_text()))
    stem='voice_edges' if use_model else 'geometry_edges'
    save_file({k:v.cpu().contiguous() for k,v in head.state_dict().items()},str(MODEL_DIR/(stem+'.safetensors')))
    (MODEL_DIR/(stem+'.json')).write_text(json.dumps(result,indent=2),encoding='utf8')
    print(stem,'TEST',result['splits']['test'],flush=True)
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--features-only',action='store_true')
    parser.add_argument('--reuse-features',action='store_true');parser.add_argument('--epochs',type=int,default=35)
    args=parser.parse_args();records=json.loads(Path('data/training/voice_corpus.json').read_text(encoding='utf8'))['pieces']
    if not args.reuse_features:prepare(records)
    if args.features_only:return
    results={name:fit(use_model,args.epochs) for name,use_model in [('geometry',False),('m2bert',True)]}
    Path('data/training/voice_training.json').write_text(json.dumps(results,indent=2),encoding='utf8')


if __name__=='__main__':main()
