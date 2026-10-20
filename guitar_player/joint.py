"""Joint voice-link / octave optimization with vertical and motif constraints.

Bounded overlapping CP-SAT windows are a deterministic approximation, not a
claim of global optimality. Link decisions remain variables inside the solver.
No note deletion or guessed melody labels exist in this module.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass, replace
import math
import time
import hashlib
import marshal
import json
from pathlib import Path
import threading

import numpy as np

from .arrange import group_notes
from .i18n import tr
from .music_model import ModelUnavailable, encode_notes, get_features
from .voice_edges import candidate_pairs, geometric_features, geometric_probabilities, predict_edges
from .cache import CACHE_ROOT, save_cache
from .jobs import checkpoint

_optimization_lock=threading.RLock()


def cached_optimize(structure,preferred,offset,bounds):
    """Cache only musical placement; changing speed/hold still reschedules input."""
    digest=hashlib.sha256(marshal.dumps(__loader__.get_code(__name__)))   # this module's code; also in a frozen build
    digest.update(json.dumps([offset,bounds,[(n.id,n.pitch,n.start,n.end,n.beat,n.beat_strength,preferred[n.id])
        for n in structure.notes],structure.motifs],separators=(',',':')).encode())
    digest.update(structure.pairs.tobytes());digest.update(structure.scores.tobytes())
    digest.update(structure.boundaries.tobytes())
    key=digest.hexdigest();folder=CACHE_ROOT/'joint_plans'
    target=folder/(key+'.json')
    with _optimization_lock:
        checkpoint(tr('progress.registers'))
        if target.exists():
            try:
                data=json.loads(target.read_text(encoding='utf8'))
                mapping={int(k):v for k,v in data['mapping'].items()}
                if len(mapping)==len(structure.notes) and all(n.id in mapping and type(mapping[n.id]) is int and bounds[0]<=mapping[n.id]<=bounds[1]
                    and (mapping[n.id]-n.pitch)%12==0 for n in structure.notes):
                    return mapping,{**data['info'],'plan_cache_hit':True}
            except (OSError,ValueError,KeyError,TypeError,AttributeError):pass
        mapping,info=optimize(structure,preferred,offset,bounds)
        checkpoint()
        save_cache(target,lambda stream:json.dump({'mapping':mapping,'info':info},stream))
        return mapping,{**info,'plan_cache_hit':False}


@dataclass
class Structure:
    notes: list
    groups: list[list[int]]
    pairs: np.ndarray
    scores: np.ndarray
    boundaries: np.ndarray
    motifs: list[tuple[int,int]]
    metadata: dict


def repeated_motifs(notes,groups):
    """Align repeated four-onset patterns, including uniformly transposed ones.

    Use original pitch intervals and score rhythm, not the already folded result.
    Constant ostinato chords are excluded; links are soft, never forced equality.
    """
    seen={};links=set()
    for start in range(max(0,len(groups)-3)):
        checkpoint()
        selected=groups[start:start+4]
        first=notes[selected[0][0]]
        if notes[selected[-1][0]].beat-first.beat<.5:continue
        pitches={notes[i].pitch for group in selected for i in group}
        if len(pitches)<3:continue
        signature=tuple((round((notes[group[0]].beat-first.beat)*8),
            tuple(notes[i].pitch-first.pitch for i in group)) for group in selected)
        if signature in seen:
            old=seen[signature]
            if old+4<=start:
                for a,b in zip(groups[old:old+4],selected):
                    links.update(zip(a,b))
        else:seen[signature]=start
    return sorted(links)


_structures=OrderedDict()
_structures_lock=threading.Lock()


def analyze_structure(song,notes,backend='model'):
    """Speed-independent analysis, kept in memory for the last few songs.

    Changing speed, simplification or key timing reuses it; only a different
    song, track selection or engine computes it again.
    """
    key=(id(song),tuple(sorted(n.id for n in notes)),backend)
    with _structures_lock:
        entry=_structures.get(key)
        if entry is not None and entry[0] is song:
            _structures.move_to_end(key)
            structure=entry[1]
            return replace(structure,metadata={**structure.metadata,'structure_reused':True})
    structure=_analyze_structure(song,notes,backend)
    with _structures_lock:
        # Holding the song object keeps id(song) from being reused while cached.
        _structures[key]=(song,structure)
        while len(_structures)>4:_structures.popitem(last=False)
    return structure


def _analyze_structure(song,notes,backend='model'):
    checkpoint(tr('progress.structure'))
    started=time.perf_counter();metadata={'requested_backend':backend}
    ns=sorted(notes,key=lambda n:(n.start,n.pitch,n.id))
    if backend=='model' and ns:
        try:
            encoded,h,info=get_features(song,ns)
            index={n.id:n for n in ns};ns=[index[int(i)] for i in encoded.ids]
            pairs,p,head_info=predict_edges(ns,h)
            geometry=geometric_probabilities(geometric_features(ns,pairs))
            # Held-out calibration is on Bach, not this user's MIDI domain.
            # Blend with local timing/pitch evidence instead of trusting it fully.
            reliability=np.where(encoded.clipped,.0,.65)
            mixing=np.minimum(reliability[pairs[:,0]],reliability[pairs[:,1]])
            scores=p*mixing+geometry*(1-mixing)
            metadata.update(info);metadata.update(head_info)
            metadata.update(model_used=True,model_weight=.65,clipped_analysis_notes=int(encoded.clipped.sum()),
                score_interpretation='soft link scores; not calibrated probabilities on user repertoire')
        except (ModelUnavailable,ImportError,OSError,ValueError,RuntimeError,KeyError,TypeError) as exc:
            metadata.update(model_used=False,fallback_reason=str(exc),backend='joint')
            pairs=candidate_pairs(ns);scores=geometric_probabilities(geometric_features(ns,pairs))
    else:
        pairs=candidate_pairs(ns);scores=geometric_probabilities(geometric_features(ns,pairs))
        metadata.update(model_used=False,backend='joint')
    original_edges=len(pairs)
    incoming=defaultdict(list);outgoing=defaultdict(list)
    for index,((a,b),score) in enumerate(zip(pairs,scores)):
        incoming[int(b)].append((float(score),index));outgoing[int(a)].append((float(score),index))
    # Retain competing hypotheses on both sides; avoid a Cartesian explosion.
    keep=sorted({i for groups_ in (incoming,outgoing) for choices in groups_.values()
                 for _,i in sorted(choices,reverse=True)[:3]})
    pairs=pairs[keep];scores=scores[keep]
    by_id={n.id:i for i,n in enumerate(ns)}
    groups=[[by_id[n.id] for n in group] for group in group_notes(ns)]
    boundaries=np.zeros(len(ns),np.float32)
    for gi,group in enumerate(groups):
        if not gi:boundaries[group]=1.;continue
        previous=groups[gi-1];a=ns[previous[-1]];b=ns[group[0]]
        rest=b.start-max(ns[i].end for i in previous)
        gap=b.beat-a.beat
        boundary=min(.85,max(0,rest)*1.5+max(0,gap-1)*.15)
        if b.beat_strength>=.9 and gap>=.5:boundary=max(boundary,.2)
        boundaries[group]=boundary
    motifs=repeated_motifs(ns,groups)
    metadata.update(candidate_links=len(pairs),unpruned_candidate_links=original_edges,motif_links=len(motifs),analysis_seconds=time.perf_counter()-started,
                    boundary_method='score rests, onset spacing and metrical accents',motif_method='original four-onset interval/rhythm patterns')
    return Structure(ns,groups,pairs,scores,boundaries,motifs,metadata)


def _vertical_cost(a,b,ka,kb):
    before=b.pitch-a.pitch;after=before+12*(kb-ka)
    if before==0:return abs(after)*4
    return abs(after-before)*.12+(before*after<0)*9+(after==0)*90+(abs(before)>=12 and abs(after)<5)*5


def best_links(count,edges):
    """Highest-gain set of links (a,b) with each a and each b used at most once; edges
    [(a,b,gain)] over 0..count-1, only positive gains count. Min-cost flow on integers."""
    from ortools.graph.python import min_cost_flow
    flow=min_cost_flow.SimpleMinCostFlow();sink=2*count+1;arcs=[]
    for a in range(count):
        flow.add_arc_with_capacity_and_unit_cost(0,1+a,1,0)
        flow.add_arc_with_capacity_and_unit_cost(1+count+a,sink,1,0)
    for a,b,gain in edges:
        if gain>0:arcs.append((a,b,flow.add_arc_with_capacity_and_unit_cost(1+a,1+count+b,1,-round(gain*1e6))))
    flow.add_arc_with_capacity_and_unit_cost(0,sink,count,0)     # links are optional
    flow.set_node_supply(0,count);flow.set_node_supply(sink,-count)
    if flow.solve()!=flow.OPTIMAL:raise RuntimeError('link matching failed')
    return {(a,b) for a,b,arc in arcs if flow.flow(arc)}


def optimize(structure,preferred,offset,bounds,window_notes=144,overlap_notes=48,passes=2):
    from ortools.sat.python import cp_model
    ns=structure.notes
    if not ns:return {},dict(windows=0,solver_statuses={})
    started=time.perf_counter()
    domains=[list(range(math.ceil((bounds[0]-n.pitch)/12),math.floor((bounds[1]-n.pitch)/12)+1)) for n in ns]
    current=[min(ds,key=lambda k:abs(n.pitch+12*k-preferred[n.id])) for n,ds in zip(ns,domains)]
    for group in structure.groups:
        unisons={}
        for i in group:
            current[i]=unisons.setdefault(ns[i].pitch,current[i])
    # Build windows on complete onset groups; overlap sees the same surrounding voices.
    windows=[];first=0
    while first<len(structure.groups):
        end=first;count=0
        while end<len(structure.groups) and (count<window_notes or end==first):
            count+=len(structure.groups[end]);end+=1
        windows.append((first,end))
        if end==len(structure.groups):break
        next_start=end;seen=0
        while next_start>first+1 and seen<overlap_notes:
            next_start-=1;seen+=len(structure.groups[next_start])
        first=next_start
    scale=100;statuses=defaultdict(int);chosen_links=set();objective_changes=[]
    for sweep in range(passes):
        order=windows if sweep%2==0 else list(reversed(windows))
        for window_number,(gstart,gend) in enumerate(order,1):
            checkpoint(tr('progress.joint',done=sweep*len(windows)+window_number,total=passes*len(windows)))
            indices=[i for g in structure.groups[gstart:gend] for i in g];inside=set(indices)
            local_index={i:j for j,i in enumerate(indices)}
            local_edges=[(int(a),int(b),float(score)) for (a,b),score in zip(structure.pairs,structure.scores)
                         if int(a) in inside and int(b) in inside and score>=.08]
            def edge_cost(a,b,ka,kb,score):
                original=ns[b].pitch-ns[a].pitch;after=original+12*(kb-ka)
                smooth=(abs(after-original)*.65+(ka!=kb)*4+(original*after<0)*9)*(1-.7*float(structure.boundaries[b]))
                return smooth-math.log(max(.02,score))*3
            # A complete feasible warm start; these links remain solver variables.
            warm_links={(indices[r],indices[c]) for r,c in best_links(len(indices),[(local_index[a],local_index[b],
                14*(1-.75*float(structure.boundaries[b]))-edge_cost(a,b,current[a],current[b],score)) for a,b,score in local_edges])}
            # Coarse coordinated proposals move entire possible voice paths. CP-SAT
            # then refines both links and pitches, rather than spending its budget
            # discovering a many-note octave move one Boolean variable at a time.
            vertical=[];unisons=[]
            for group in structure.groups[gstart:gend]:
                same=defaultdict(list)
                for i in group:same[ns[i].pitch].append(i)
                unisons.extend(g for g in same.values() if len(g)>1)
                for ai,a in enumerate(group):
                    for bi,b in enumerate(group[ai+1:],start=ai+1):
                        if len(group)>16 and bi>ai+2 and ns[a].pitch%12!=ns[b].pitch%12:continue
                        if ns[a].pitch!=ns[b].pitch:vertical.append((a,b))
            incident=defaultdict(list)
            for a,b in vertical:incident[a].append(b);incident[b].append(a)
            motif_incident=defaultdict(list)
            for a,b in structure.motifs:
                if a in inside:motif_incident[a].append(b)
                if b in inside:motif_incident[b].append(a)
            boundary_edges=[(int(a),int(b),float(p)) for (a,b),p in zip(structure.pairs,structure.scores)
                            if (int(a) in inside)!=(int(b) in inside) and p>=.7]
            edge_lookup={(a,b):p for a,b,p in local_edges}
            destinations={b for _,b,_ in local_edges}
            def matching(values,ignore_octaves=False):
                edges=[(local_index[a],local_index[b],14*(1-.75*float(structure.boundaries[b]))
                        -(-math.log(max(.02,p))*3 if ignore_octaves else edge_cost(a,b,values[a],values[b],p))) for a,b,p in local_edges]
                return {(indices[r],indices[c]) for r,c in best_links(len(indices),edges)}
            def energy(values,links):
                assigned={b for _,b in links}
                return (sum(abs(12*values[i]-offset)*.13 for i in indices)
                    +sum(_vertical_cost(ns[a],ns[b],values[a],values[b]) for a,b in vertical)
                    +sum(edge_cost(a,b,values[a],values[b],edge_lookup[a,b]) for a,b in links)
                    +sum(14*(1-.75*float(structure.boundaries[b])) for b in destinations-assigned)
                    +sum(3*abs(values[a]-values[b]) for a,b in structure.motifs if a in inside or b in inside)
                    +sum(.8*p*abs(values[a]-values[b]) for a,b,p in boundary_edges))
            initial_energy=energy(current,warm_links)
            best_energy=initial_energy
            for proposal_round in range(3):
                links=matching(current,ignore_octaves=proposal_round==0)
                next_node=dict(links);has_previous={b for _,b in links};paths=[]
                for first_node in indices:
                    if first_node in has_previous:continue
                    path=[first_node]
                    while path[-1] in next_node:path.append(next_node[path[-1]])
                    paths.append(path)
                for path in paths:
                    checkpoint()
                    if len(path)<2:continue
                    layers=[]
                    for step,i in enumerate(path):
                        layer={}
                        for k in domains[i]:
                            unary=abs(12*k-offset)*.13
                            for other in incident[i]:
                                if ns[i].pitch<ns[other].pitch:unary+=_vertical_cost(ns[i],ns[other],k,current[other])
                                else:unary+=_vertical_cost(ns[other],ns[i],current[other],k)
                            unary+=sum(3*abs(k-current[o]) for o in motif_incident[i])
                            if step:
                                a=path[step-1]
                                value,prev=min((cost+edge_cost(a,i,pk,k,edge_lookup[a,i]),pk)
                                               for pk,(cost,_) in layers[-1].items())
                                layer[k]=(unary+value,prev)
                            else:layer[k]=(unary,None)
                        layers.append(layer)
                    candidate=current.copy();k=min(layers[-1],key=lambda k:layers[-1][k][0])
                    for step in range(len(path)-1,-1,-1):candidate[path[step]]=k;k=layers[step][k][1]
                    for group in unisons:
                        changed=[i for i in group if candidate[i]!=current[i]]
                        if changed:
                            for i in group:candidate[i]=candidate[changed[0]]
                    candidate_links=matching(candidate);value=energy(candidate,candidate_links)
                    if value<best_energy-1e-6:
                        current=candidate;warm_links=candidate_links;best_energy=value
            warm_incoming={b for _,b in warm_links}
            model=cp_model.CpModel();ks={};terms=[]
            for i in indices:
                ks[i]=model.new_int_var_from_domain(cp_model.Domain.from_values(domains[i]),f'k{i}')
                model.add_hint(ks[i],current[i])
                c=model.new_int_var(0,10000,f'u{i}')
                model.add_allowed_assignments([ks[i],c],[(k,round(scale*(abs(ns[i].pitch+12*k-(ns[i].pitch+offset))*.13))) for k in domains[i]])
                model.add_hint(c,round(scale*abs(12*current[i]-offset)*.13))
                terms.append(c)
            incoming=defaultdict(list);outgoing=defaultdict(list);link_vars=[]
            for a,b,score in local_edges:
                y=model.new_bool_var(f'link{a}_{b}')
                incoming[b].append(y);outgoing[a].append(y)
                c=model.new_int_var(0,100000,f'edge{a}_{b}')
                rows=[]
                for ka in domains[a]:
                    for kb in domains[b]:
                        rows.extend([(ka,kb,0,0),(ka,kb,1,round(scale*edge_cost(a,b,ka,kb,score)))])
                model.add_allowed_assignments([ks[a],ks[b],y,c],rows);terms.append(c)
                chosen=(a,b) in warm_links
                model.add_hint(y,int(chosen));model.add_hint(c,round(scale*edge_cost(a,b,current[a],current[b],score)) if chosen else 0)
                link_vars.append((a,b,y))
            for b,ys in incoming.items():
                new=model.new_bool_var(f'new{b}');model.add(sum(ys)+new==1)
                model.add_hint(new,int(b not in warm_incoming))
                terms.append(round(scale*14*(1-.75*float(structure.boundaries[b])))*new)
            for ys in outgoing.values():model.add(sum(ys)<=1)
            for group in structure.groups[gstart:gend]:
                for ai,a in enumerate(group):
                    for b in group[ai+1:]:
                        # Huge orchestral onsets: adjacent register and same-PC pairs.
                        if len(group)>16 and group.index(b)>ai+2 and ns[a].pitch%12!=ns[b].pitch%12:continue
                        if ns[a].pitch==ns[b].pitch:
                            model.add(ks[a]==ks[b]);continue
                        c=model.new_int_var(0,100000,f'v{a}_{b}')
                        model.add_allowed_assignments([ks[a],ks[b],c],[(ka,kb,round(scale*_vertical_cost(ns[a],ns[b],ka,kb)))
                            for ka in domains[a] for kb in domains[b]])
                        model.add_hint(c,round(scale*_vertical_cost(ns[a],ns[b],current[a],current[b])))
                        terms.append(c)
            # Soft anchors communicate repeated-theme choices across windows/sweeps.
            for a,b in structure.motifs:
                if a not in inside and b not in inside:continue
                left=ks[a] if a in inside else current[a]
                right=ks[b] if b in inside else current[b]
                distance=model.new_int_var(0,20,f'motif{a}_{b}')
                model.add_abs_equality(distance,left-right);terms.append(300*distance)
                model.add_hint(distance,abs(current[a]-current[b]))
            # Across-window continuity is soft; it cannot force an impossible pitch.
            for (a,b),score in zip(structure.pairs,structure.scores):
                a=int(a);b=int(b)
                if (a in inside)==(b in inside) or score<.7:continue
                left=ks[a] if a in inside else current[a]
                right=ks[b] if b in inside else current[b]
                distance=model.new_int_var(0,20,f'boundary{a}_{b}')
                model.add_abs_equality(distance,left-right);terms.append(round(80*float(score))*distance)
                model.add_hint(distance,abs(current[a]-current[b]))
            model.minimize(sum(terms))
            solver=cp_model.CpSolver();solver.parameters.num_search_workers=1
            solver.parameters.random_seed=8721;solver.parameters.max_deterministic_time=.08
            solver.parameters.cp_model_presolve=False
            solver.parameters.max_time_in_seconds=2.
            status=solver.solve(model);name=solver.status_name(status);statuses[name]+=1
            checkpoint()
            if status in (cp_model.OPTIMAL,cp_model.FEASIBLE):
                for i in indices:current[i]=solver.value(ks[i])
                chosen_links.difference_update({(a,b) for a,b in chosen_links if a in inside and b in inside})
                chosen_links.update((a,b) for a,b,y in link_vars if solver.value(y))
                objective_changes.append(initial_energy-float(solver.objective_value)/scale)
    mapping={n.id:n.pitch+12*k for n,k in zip(ns,current)}
    # Overlapping windows may choose different successors for the same note.
    # Reconcile the reported graph to disjoint paths using final-pitch costs.
    scores={(int(a),int(b)):float(p) for (a,b),p in zip(structure.pairs,structure.scores)}
    def link_value(edge):
        a,b=edge;before=ns[b].pitch-ns[a].pitch;after=before+12*(current[b]-current[a])
        smooth=(abs(after-before)*.65+(current[a]!=current[b])*4+(before*after<0)*9)*(1-.7*float(structure.boundaries[b]))
        return 14*(1-.75*float(structure.boundaries[b]))-smooth+math.log(max(.02,scores[a,b]))*3
    consistent=set();used_from=set();used_to=set()
    for a,b in sorted(chosen_links,key=lambda edge:(-link_value(edge),edge)):
        if a not in used_from and b not in used_to:
            consistent.add((a,b));used_from.add(a);used_to.add(b)
    reconciled=len(chosen_links)-len(consistent)
    return mapping,dict(windows=len(windows)*passes,solver_statuses=dict(statuses),
        selected_links=[(ns[a].id,ns[b].id) for a,b in sorted(consistent)],
        reconciled_overlap_links=reconciled,
        window_objective_improvement=sum(objective_changes),
        joint_seconds=time.perf_counter()-started,motif_octave_disagreements=sum(current[a]!=current[b] for a,b in structure.motifs),
        approximation='two overlapping forward/backward CP-SAT sweeps; not global optimality')
