"""Build labelled voice connections from music21's Bach SATB score corpus.

Uses part identities in authored MusicXML, never our player's inferred voices.
All model inputs are merged to one anonymous track. BWV family and exact
transposition-invariant duplicates are kept in the same dataset split.
"""
from collections import Counter, defaultdict
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def read_score(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        container=ET.fromstring(z.read('META-INF/container.xml'))
        name=container.find('.//{*}rootfile').attrib['full-path']
        root=ET.fromstring(z.read(name))
    roles={p.attrib['id']:p.findtext('part-name','').lower() for p in root.findall('part-list/score-part')}
    wanted={'soprano':0,'alto':1,'tenor':2,'bass':3}
    parts=[p for p in root.findall('part') if roles[p.attrib['id']] in wanted]
    if len(parts)!=4:raise ValueError('requires four explicitly named SATB parts')
    local=[];lengths=defaultdict(float);meters={};ties={}
    for part in parts:
        voice=wanted[roles[part.attrib['id']]];division=1
        for mi,measure in enumerate(part.findall('measure')):
            cursor=previous=0.;end=0.
            for element in measure:
                if element.tag=='attributes':
                    division=int(element.findtext('divisions',str(division)))
                    tm=element.find('time')
                    if tm is not None:meters[mi]=(int(tm.findtext('beats')),int(tm.findtext('beat-type')))
                elif element.tag in ('backup','forward'):
                    cursor+=int(element.findtext('duration'))/division*(1 if element.tag=='forward' else -1)
                    end=max(end,cursor)
                elif element.tag=='note':
                    duration=int(element.findtext('duration','0'))/division
                    if element.find('grace') is not None:continue
                    onset=previous if element.find('chord') is not None else cursor
                    pitch=element.find('pitch')
                    if pitch is not None:
                        pc={'C':0,'D':2,'E':4,'F':5,'G':7,'A':9,'B':11}[pitch.findtext('step')]
                        value=(int(pitch.findtext('octave'))+1)*12+pc+int(pitch.findtext('alter','0'))
                        local.append([mi,onset,duration,value,voice,[t.attrib['type'] for t in element.findall('tie')],
                                      element.find('notations/fermata') is not None])
                    if element.find('chord') is None:previous=cursor;cursor+=duration
                    end=max(end,onset+duration,cursor)
            lengths[mi]=max(lengths[mi],end)
    starts={};position=0.
    for mi in sorted(lengths):starts[mi]=position;position+=lengths[mi]
    notes=[]
    for mi,start,dur,pitch,voice,tie,fermata in sorted(local,key=lambda x:(x[0],x[1],x[4],x[3])):
        onset=starts[mi]+start;end=onset+dur;key=(voice,pitch)
        if 'stop' in tie and key in ties and abs(notes[ties[key]][1]-onset)<1e-5:
            notes[ties[key]][1]=end;notes[ties[key]][4]|=fermata
        else:
            notes.append([onset,end,pitch,voice,bool(fermata)])
            if 'start' in tie:ties[key]=len(notes)-1
        if 'stop' in tie and 'start' not in tie:ties.pop(key,None)
    notes.sort(key=lambda x:(x[0],x[2],x[3]))
    for voice in range(4):
        ns=[n for n in notes if n[3]==voice]
        if not ns:raise ValueError('empty part')
        if any(a[1]>b[0]+1e-6 for a,b in zip(ns,ns[1:])):raise ValueError('polyphonic part')
    return notes,[(round(starts[i]*480),*v) for i,v in sorted(meters.items())]


def main():
    package=next(Path('data/training/packages').glob('music21-9.7.1*.whl'))
    records=[];errors=[]
    with zipfile.ZipFile(package) as z:
        for name in sorted(z.namelist()):
            if '/corpus/bach/' not in name or not name.endswith('.mxl'):continue
            try:
                notes,meters=read_score(z.read(name))
                if len(notes)<64:continue
                family=re.match(r'bwv\d+',Path(name).stem).group(0)
                base=min(n[2] for n in notes)
                signature=hashlib.sha256(json.dumps([(n[0],n[1],n[2]-base,n[3]) for n in notes]).encode()).hexdigest()
                records.append(dict(name=Path(name).stem,family=family,signature=signature,notes=notes,meters=meters))
            except (ValueError,AttributeError,KeyError) as exc:errors.append(dict(file=name,error=str(exc)))
    # Union duplicate families before assigning splits, then discard exact copies.
    parent={r['family']:r['family'] for r in records}
    def find(x):
        while parent[x]!=x:x=parent[x]
        return x
    owners={}
    for r in records:
        if r['signature'] in owners:
            a,b=sorted((find(r['family']),find(owners[r['signature']])))
            parent[b]=a
        owners[r['signature']]=r['family']
    seen=set();unique=[]
    for r in records:
        if r['signature'] in seen:continue
        seen.add(r['signature']);family=find(r['family'])
        bucket=int(hashlib.sha256(family.encode()).hexdigest()[:8],16)%10
        r['split']='test' if bucket==0 else 'valid' if bucket==1 else 'train'
        r['split_family']=family;unique.append(r)
    result=dict(source='music21 9.7.1 bundled Bach MusicXML SATB parts',
        source_package_sha256=hashlib.sha256(package.read_bytes()).hexdigest(),
        input_tracks='all collapsed; labels are NOT model inputs',
        scope='Bach four-part voice continuation; not a guarantee for piano arrangements',
        pieces=unique,excluded=errors)
    target=Path('data/training/voice_corpus.json');target.write_text(json.dumps(result,ensure_ascii=False),encoding='utf8')
    print('pieces',len(unique),'notes',sum(len(r['notes']) for r in unique),'splits',dict(Counter(r['split'] for r in unique)),flush=True)
    print('excluded',len(errors),'duplicates',len(records)-len(unique),flush=True)


if __name__=='__main__':main()
