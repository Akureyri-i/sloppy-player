"""Content-first octave placement and input scheduling, without melody guessing.

Structural lines are local continuity links, not labelled musical parts. Every
note participates; only explicit extraction modes classify melody or bass.
"""
from collections import Counter, defaultdict, deque
from dataclasses import replace
from statistics import median
from .i18n import tr
from .jobs import checkpoint

from .model import GAME_SOUND_LENGTH, Arrangement, Config, Fingering, Strike, VoiceNote
from .arrange import best_offset, fold_voice, group_notes, octave_candidates


def selected_notes(song, config):
    known = {s.id for s in song.sources}
    if config.sources is not None and not set(config.sources) <= known:
        raise ValueError(tr('error.track_missing'))
    notes = [n for n in song.notes if (config.include_drums or n.channel != 9)
             and (config.sources is None or n.source in config.sources)]
    # The melody track hint only matters to 吉他化.
    if config.mode == 'guitar' and config.melody_source is not None and config.melody_source not in {n.source for n in notes}:
        raise ValueError(tr('error.melody_track'))
    return notes


def continuity_lines(notes):
    """Link nearby notes within each source; never select or discard a part."""
    sources = defaultdict(list)
    for n in notes:
        sources[n.source].append(n)
    result = []
    for ns in sources.values():
        lines = []
        active = []
        for group in group_notes(ns):
            checkpoint()
            now = group[0].start
            active = [i for i in active
                      if now-lines[i][-1].start <= 2.5 and now-lines[i][-1].end <= .5]
            # Greedy local matching is only a continuity hint. Incorrect links
            # can alter octave preference, but cannot cause a note to disappear.
            edges = sorted((abs(n.pitch-lines[i][-1].pitch), i, j)
                           for i in active for j, n in enumerate(group))
            used_lines, used_notes = set(), set()
            for distance, i, j in edges:
                if i not in used_lines and j not in used_notes and distance <= 19:
                    lines[i].append(group[j]); used_lines.add(i); used_notes.add(j)
            for j,n in enumerate(group):
                if j not in used_notes:
                    active.append(len(lines));lines.append([n])
        result.extend(lines)
    return result


def chord_placement(group, preferred, offset, bounds):
    """Preserve octave doubling and vertical order when independently folded
    lines collide. Beam search is bounded; its output never removes notes."""
    ordered = sorted(group, key=lambda n: (n.pitch, n.id))
    values = [preferred[n.id] for n in ordered]
    distinct = [v for i,v in enumerate(values) if i == 0 or ordered[i].pitch != ordered[i-1].pitch]
    if len(set(distinct)) == len(distinct) and values == sorted(values):
        return {n.id: preferred[n.id] for n in group}
    states = [(0., (), 0)]
    for i,n in enumerate(ordered):
        candidates = octave_candidates(n.pitch+offset, bounds=bounds)
        expanded = []
        for score, pitches, mask in states:
            for p in candidates:
                cost = abs(p-preferred[n.id])*.6
                if pitches:
                    original = n.pitch-ordered[i-1].pitch
                    interval = p-pitches[-1]
                    cost += abs(original-interval)*.12 + (interval < 0)*9
                duplicate = bool(mask & (1 << (p-bounds[0])))
                # Original unisons may stay unisons; octave doubles should not.
                if duplicate and (not i or n.pitch != ordered[i-1].pitch):
                    cost += 24
                expanded.append((score+cost, pitches+(p,), mask | (1 << (p-bounds[0]))))
        states = sorted(expanded, key=lambda s:s[0])[:24]
    pitches = states[0][1]
    return {n.id:p for n,p in zip(ordered,pitches)}


def map_complete(notes, lines, offset, bounds):
    preferred = {}
    for line in lines:
        checkpoint()
        target = min(bounds[1], max(bounds[0], median(n.pitch for n in line)+offset))
        preferred.update(fold_voice(line, 'complete', offset, target, bounds=bounds))
    output = {}
    for group in group_notes(notes):
        checkpoint()
        output.update(chord_placement(group, preferred, offset, bounds))
    return output


def simplify_group(voices, config):
    """User-requested thinning only; preserve the onset and spread of registers."""
    count = len(voices)
    limit = max(1, round(count*(1-.75*config.simplify)))
    if config.max_voices is not None:
        limit = min(limit, config.max_voices)
    if limit >= count:
        return voices
    ordered = sorted(voices,key=lambda v:(v.note.pitch,v.note.id))
    # No assumed lead: evenly sample registers, including both boundaries.
    positions = [count//2] if limit == 1 else [round(i*(count-1)/(limit-1)) for i in range(limit)]
    return [ordered[i] for i in positions]


def free_slots(at, reservations, width, deadline):
    result = []
    cursor = at
    for start,end in sorted(reservations):
        while cursor+width <= start+1e-9 and cursor <= deadline+1e-9:
            result.append(cursor); cursor += width
        cursor = max(cursor,end)
        if cursor > deadline+1e-9:
            break
    while cursor <= deadline+1e-9:
        result.append(cursor); cursor += width
    return result


def slot_matching(voices, slots, config, pitch_due, fingering=None):
    """Maximum-cardinality minimum-cost note/slot matching, with residual edges.

    Multiple slots on a string are separate input opportunities, not a limit on
    its sounding voices. Augmenting paths reserve scarce strings for high/low
    notes and can move earlier choices instead of silently dropping a chord.
    fingering: {note id: (string, fret)} a note must keep.
    """
    count = len(voices)
    sink = 1+count+len(slots)
    graph = [[] for _ in range(sink+1)]
    choices = []

    def edge(a,b,cost):
        forward = [b, len(graph[b]), 1, cost]
        backward = [a, len(graph[a]), 0, -cost]
        graph[a].append(forward); graph[b].append(backward)
        return forward

    for i,v in enumerate(voices):
        edge(0,1+i,-1000*v.weight)
        intended = v.note.start/config.speed
        fixed = (fingering or {}).get(v.note.id)
        for j,(string,at) in enumerate(slots):
            fret = v.pitch-config.tuning[string]
            if fixed and string != fixed[0]:
                continue
            if not 0 <= fret < 20 or at > intended+config.max_delay+1e-9:
                continue
            if at+1e-9 < pitch_due.get(v.pitch,0):
                continue
            ref = edge(1+i,1+count+j,(at-intended)*100+fret*.002)
            choices.append((i,j,ref))
    for j in range(len(slots)):
        edge(1+count+j,sink,0)
    while True:
        distances = [float('inf')]*len(graph)
        distances[0]=0
        previous = [None]*len(graph)
        queue=deque([0]); queued={0}
        while queue:
            a=queue.popleft();queued.remove(a)
            for index,e in enumerate(graph[a]):
                b,_,capacity,cost=e
                value=distances[a]+cost
                if capacity and value < distances[b]-1e-10:
                    distances[b]=value;previous[b]=(a,index)
                    if b not in queued:queue.append(b);queued.add(b)
        if previous[sink] is None:
            break
        b=sink
        while b:
            a,index=previous[b];e=graph[a][index]
            e[2]-=1;graph[b][e[1]][2]+=1;b=a
    return [(voices[i],slots[j]) for i,j,e in choices if not e[2]]


def schedule(groups, config, dropped, fingering=None):
    reservations = [[] for _ in range(6)]
    pitch_due = {}
    strikes = []
    width = config.hold+config.gap
    for voices in groups:
        checkpoint()
        at = max(v.note.start for v in voices)/config.speed
        slots = []
        for string in range(6):
            reservations[string] = [(a,b) for a,b in reservations[string] if b > at+1e-9]
            slots.extend((string,t) for t in free_slots(at,reservations[string],width,at+config.max_delay))
        chosen = slot_matching(voices, slots, config, pitch_due, fingering)
        kept = {v.note.id for v,_ in chosen}
        for v in voices:
            if v.note.id not in kept:dropped[v.note.id]='input_capacity'
        packets = defaultdict(list)
        for v,(string,t) in chosen:
            reservations[string].append((t,t+width))
            pitch_due[v.pitch]=t+config.repeat_interval
            f=Fingering(v,string,v.pitch-config.tuning[string])
            # Keep intended time per note accurate even in near-simultaneous groups.
            packets[(t,v.note.start/config.speed,f.fret)].append(f)
        strikes.extend(Strike(t,intended,tuple(fs),config.hold)
                       for (t,intended,_),fs in packets.items())
    return sorted(strikes,key=lambda s:(s.time,s.intended,s.notes[0].voice.note.id))


# Re-pressing a pitch from its previous press + its sound length + 0 to + 50 ms can leave
# it silent in the game (measured; -20 and +65 ms were safe). The guard keeps 10 ms more
# on both sides for the game's timing jitter.
END_GUARD = (.030, .075)


def avoid_end_retrigger(strikes, config):
    """Move each note whose press would land where the previous note of its pitch ends
    by itself to the nearer safe edge; there the game would silence the pitch until it
    rests a full sound length (DECISIONS: 同音卡死). Only that note moves, as a strike of
    its own; one that cannot move without crowding its string stays (unavoidable)."""
    width = config.hold+config.gap
    notes = sorted(((s.time, k, s, f) for s in strikes for k, f in enumerate(s.notes)),
                   key=lambda n: (n[0], n[2].intended, n[1]))
    times = [t for t, *_ in notes]
    on_string = defaultdict(list)
    for i, (_, _, _, f) in enumerate(notes):
        on_string[f.string].append(i)
    by_pitch = defaultdict(list)
    for i, (_, _, _, f) in enumerate(notes):
        by_pitch[f.voice.pitch].append(i)
    moved, unavoidable, last = {}, 0, {}
    for i, (t, _, strike, f) in enumerate(notes):
        pitch, length = f.voice.pitch, GAME_SOUND_LENGTH.get(f.voice.pitch)
        previous = last.get(pitch)
        last[pitch] = i
        if previous is None or length is None:
            continue
        early, late = times[previous]+length-END_GUARD[0], times[previous]+length+END_GUARD[1]
        if not early < t < late:
            continue
        later = [times[j] for j in by_pitch[pitch] if j > i]
        room = max(width, config.repeat_interval)
        for target in sorted((early, late), key=lambda x: abs(x-t)):
            crowded = any(abs(times[j]-target) < width-1e-9 for j in on_string[f.string] if j != i)
            if not crowded and (not later or later[0]-target >= room-1e-9):
                times[i], moved[i] = target, target-t
                break
        else:
            unavoidable += 1
    if not moved:
        return strikes, dict(moved=0, max_shift_ms=0., unavoidable=unavoidable)
    kept = defaultdict(list)
    out = []
    for i, (_, k, strike, f) in enumerate(notes):
        if i in moved:
            out.append(Strike(times[i], strike.intended, (f,), strike.hold))
        else:
            kept[id(strike)].append((k, f))
    for strike in strikes:
        fs = kept.get(id(strike))
        if fs:
            out.append(strike if len(fs) == len(strike.notes) else replace(strike, notes=tuple(f for _, f in sorted(fs, key=lambda x: x[0]))))
    out.sort(key=lambda s: (s.time, s.intended, s.notes[0].voice.note.id))
    return out, dict(moved=len(moved), max_shift_ms=max(abs(v) for v in moved.values())*1000,
                     unavoidable=unavoidable)


def structural_stats(notes, lines, strikes, config):
    played = {f.voice.note.id:(s,f) for s in strikes for f in s.notes}
    errors=[];reversals=links=0
    for line in lines:
        for a,b in zip(line,line[1:]):
            if a.id not in played or b.id not in played:continue
            before=b.pitch-a.pitch
            after=played[b.id][1].voice.pitch-played[a.id][1].voice.pitch
            errors.append(abs(before-after)); reversals+=before*after<0;links+=1
    inversions=pairs=collapsed=0
    onset_kept=0; groups=group_notes(notes,config.chord_window)
    max_spread=0.; over_six=0
    for group in groups:
        kept=[n for n in group if n.id in played]
        onset_kept+=bool(kept)
        over_six+=len(kept)>6
        times=[played[n.id][0].time for n in kept]
        max_spread=max(max_spread,max(times,default=0)-min(times,default=0))
        for i,a in enumerate(kept):
            for b in kept[i+1:]:
                before=b.pitch-a.pitch
                if not before:continue
                after=played[b.id][1].voice.pitch-played[a.id][1].voice.pitch
                pairs+=1;inversions+=before*after<0
                collapsed+=abs(before)>=12 and abs(after)<5
    sources={sid:dict(selected=sum(n.source==sid for n in notes),
                     played=sum(f.voice.note.source==sid for _,f in played.values()))
             for sid in {n.source for n in notes}}
    return dict(continuity_interval_error=sum(errors)/max(1,len(errors)),
                continuity_reversals=reversals,continuity_links=links,
                register_inversion_rate=inversions/max(1,pairs),
                register_crowding_rate=collapsed/max(1,pairs),
                onset_retention=onset_kept/max(1,len(groups)),
                source_retention=sources,
                mean_source_retention=sum(v['played']/v['selected'] for v in sources.values())/max(1,len(sources)),
                rolled_groups_over_six=over_six,max_chord_spread_ms=max_spread*1000)


def arrange_once(song, config, base=None, prepared=None, fingering=None):
    """fingering: {note id: (string, fret)} to keep (原样演奏 of an ml3czus sheet); the
    pitch then follows the string's tuning."""
    notes,lines,structure = prepared if prepared is not None else (None,None,None)
    if notes is None:
        notes=selected_notes(song,config);lines=continuity_lines(notes)
    warnings=list(song.warnings)
    dropped={}
    melody=[];bass=[];melody_source=bass_source=None
    offset = config.octave*12 if config.octave is not None else (
        base if base is not None else best_offset(notes,bounds=config.bounds))
    roles={}
    joint_info={}
    guitar_info=None
    if config.mode in ('raw','full'):
        mapping = {n.id:n.pitch for n in notes if config.bounds[0]<=n.pitch<=config.bounds[1]} if config.mode=='raw' else map_complete(notes,lines,offset,config.bounds)
        for n in notes:
            if fingering and n.id in fingering:
                string,fret=fingering[n.id]
                mapping[n.id]=config.tuning[string]+fret
        if config.mode=='full' and config.arranger!='legacy':
            from .joint import analyze_structure,cached_optimize
            if structure is None:structure=analyze_structure(song,notes,config.arranger)
            mapping,optimization=cached_optimize(structure,mapping,offset,config.bounds)
            joint_info={**structure.metadata,**optimization}
            if joint_info.get('fallback_reason'):
                warnings.append(tr('warning.model_fallback',reason=joint_info['fallback_reason']))
    else:
        from .guitarize import guitarize
        mapping,roles,guitar_dropped,guitar_info=guitarize(song,notes,config,offset)
        dropped.update(guitar_dropped)
        melody=[n for n in notes if roles.get(n.id)=='melody']
        bass=[n for n in notes if roles.get(n.id)=='bass']
        melody_source=config.melody_source
        if guitar_info.get('role_backend')=='rule':
            warnings.append(tr('warning.no_role_model'))
    groups=[]
    for group in group_notes(notes,config.chord_window):
        unique={}
        for n in sorted(group,key=lambda n:(-n.velocity,n.id)):
            if n.id not in mapping:
                dropped.setdefault(n.id,'range')
                continue
            p=mapping[n.id]
            if p in unique:
                dropped[n.id]='unison'
                continue
            role=roles.get(n.id,'content')
            unique[p]=VoiceNote(n,p,role,{'melody':3.,'bass':2.,'bridge':1.5,'content':1.}[role])
        voices=list(unique.values())
        # 吉他化 already thinned the texture by the 简化 setting.
        kept=voices if config.mode=='guitar' else simplify_group(voices,config)
        kept_ids={v.note.id for v in kept}
        for v in voices:
            if v.note.id not in kept_ids:dropped[v.note.id]='user_simplification'
        if kept:groups.append(kept)
    strikes=schedule(groups,config,dropped,fingering)
    end_guard=dict(enabled=config.end_guard,moved=0,max_shift_ms=0.,unavoidable=0)
    if config.end_guard:
        strikes,result=avoid_end_retrigger(strikes,config)
        end_guard.update(result)
    played={f.voice.note.id:(s,f) for s in strikes for f in s.notes}
    delays=sorted(s.time-f.voice.note.start/config.speed for s in strikes for f in s.notes)
    shifts=Counter(f.voice.pitch-f.voice.note.pitch for s in strikes for f in s.notes)
    mid={n.id for n in melody};bid={n.id for n in bass}
    stats=dict(file_notes=len(song.notes),selected_notes=len(notes),filtered_notes=len(song.notes)-len(notes),
               played_notes=len(played),retention=len(played)/max(1,len(notes)),
               global_octave=0 if config.mode=='raw' else offset//12,
               shifts_semitones=dict(sorted(shifts.items())),drop_reasons=dict(Counter(dropped.values())),
               mean_delay_ms=sum(delays)/max(1,len(delays))*1000,
               p95_delay_ms=delays[min(len(delays)-1,int(len(delays)*.95))]*1000 if delays else 0,
               max_delay_ms=max(delays,default=0)*1000,
               melody_notes=len(mid),melody_played=len(mid & played.keys()),
               melody_retention=len(mid & played.keys())/max(1,len(mid)),
               bass_notes=len(bid),bass_played=len(bid & played.keys()),
               melody_source=melody_source,bass_source=bass_source,
               source_duration=song.duration,output_duration=max(song.duration/config.speed,max((s.time+s.hold for s in strikes),default=0)),
               tuning_offset=config.tuning_offset,playable_range=list(config.bounds),
               uses_voice_extraction=config.mode=='guitar')
    stats.update(structural_stats(notes,lines,strikes,config))
    stats['joint']=joint_info
    stats['end_guard']=end_guard
    if guitar_info is not None:
        stats['guitarize']=guitar_info
    if not notes:warnings.append(tr('warning.no_notes'))
    if stats['drop_reasons'].get('input_capacity'):
        warnings.append(tr('warning.input_capacity'))
    return Arrangement(song,config,strikes,notes,mid,bid,dropped,warnings,stats)


def prepare(song,config):
    """Run the speed-independent work of arranging: the voice-structure analysis
    and the joint octave optimisation for the offsets that arrange_once and
    recommend will use. Both are cached (in memory and on disk, cache.CACHE_ROOT),
    so arranging afterwards with any speed, simplification or key timing only
    reschedules the input. Does nothing when nothing would be optimised."""
    notes=selected_notes(song,config)
    if config.mode=='guitar' and notes:
        from .music_model import ModelUnavailable,get_features
        try:get_features(song,notes)        # warm the feature cache 吉他化 reads
        except ModelUnavailable:pass
        return
    if config.arranger=='legacy' or config.mode not in ('auto','full') or not notes:return
    if config.mode=='auto' and config.octave in (None,0) and all(config.bounds[0]<=n.pitch<=config.bounds[1] for n in notes):return
    from .joint import analyze_structure,cached_optimize
    structure=analyze_structure(song,notes,config.arranger)
    lines=continuity_lines(notes)
    offset=config.octave*12 if config.octave is not None else best_offset(notes,bounds=config.bounds)
    cached_optimize(structure,map_complete(notes,lines,offset,config.bounds),offset,config.bounds)


def recommend_guitar(song,config):
    """吉他化: a few whole-piece octave placements, scored on what makes it a guitar
    skeleton (melody register, chord coverage, texture), not on note retention."""
    checkpoint(tr('progress.compare_guitar'))
    notes=selected_notes(song,config)
    config=replace(config,mode='guitar')
    if not notes:
        return arrange_once(song,config)
    from .guitarize import MELODY_TARGET,analysis,candidate_score
    # Centred on the octave that puts the melody's median nearest MELODY_TARGET (what human
    # guitar arrangers do); the melody line does not depend on the octave and is cached.
    melody=analysis(song,notes,config.melody_source)[3]
    center=(12*round((MELODY_TARGET-median(notes[i].pitch for i in melody))/12) if melody
            else best_offset(notes,bounds=config.bounds))
    offsets=[config.octave*12] if config.octave is not None else sorted({max(-60,center-12),center,min(60,center+12)})
    options={f'guitar:{o//12:+d}':arrange_once(song,config,o) for o in offsets}
    scores={k:round(candidate_score(a.stats['guitarize']),4) for k,a in options.items()}
    chosen=max(scores,key=lambda k:(scores[k],-abs(options[k].stats['global_octave'])))
    best=options[chosen]
    best.stats['recommendation']=dict(chosen='guitar',candidate=chosen,layout_backend=best.stats['guitarize']['role_backend'],
        reason=tr('reason.guitar'),scores=scores,excluded={},
        candidates={k:a.stats['guitarize'] for k,a in options.items()})
    return best


def recommend(song,config):
    checkpoint(tr('progress.compare'))
    notes=selected_notes(song,config)
    if notes and config.octave in (None,0) and all(config.bounds[0]<=n.pitch<=config.bounds[1] for n in notes):
        # Already playable as written: nothing to arrange, so skip the model and
        # the candidate comparison and play the original pitches.
        best=arrange_once(song,replace(config,mode='raw'),prepared=(notes,continuity_lines(notes),None))
        best.stats['recommendation']=dict(chosen='raw',candidate='raw',layout_backend='raw',in_range=True,
            reason=tr('reason.in_range'),scores={},excluded={},candidates={})
        best.config=config
        return best
    structure=None
    if config.arranger!='legacy':
        from .joint import analyze_structure
        structure=analyze_structure(song,notes,config.arranger)
    prepared=(notes,continuity_lines(notes),structure)
    center=best_offset(notes,bounds=config.bounds)
    offsets=[config.octave*12] if config.octave is not None else (
        [center] if config.arranger!='legacy' else sorted({center,max(-60,center-12),min(60,center+12)}))
    options={}
    if config.octave in (None,0):
        options['raw']=arrange_once(song,replace(config,mode='raw'),prepared=prepared)
    for offset in offsets:
        checkpoint()
        options[f'full:{offset//12:+d}']=arrange_once(song,replace(config,mode='full'),offset,prepared)
    # A learned proposal must compete with the existing complete arrangements.
    # Keep their original offset search: the learned optimizer's best local
    # window solution need not beat the previous whole-piece layout.
    if config.arranger!='legacy':
        legacy_offsets=[config.octave*12] if config.octave is not None else sorted({center,max(-60,center-12),min(60,center+12)})
        for offset in legacy_offsets:
            checkpoint(tr('progress.compare'))
            options[f'baseline:{offset//12:+d}']=arrange_once(song,replace(config,mode='full',arranger='legacy'),offset,prepared)
    scores={}
    for key,a in options.items():
        s=a.stats
        alteration=sum(abs(k)/12*v for k,v in s['shifts_semitones'].items())/max(1,s['played_notes'])
        scores[key]=round(s['retention']*100+s['onset_retention']*15+s['mean_source_retention']*15
                         -min(20,s['continuity_interval_error']*1.5)
                         -s['register_inversion_rate']*12-s['register_crowding_rate']*5
                         -s['mean_delay_ms']*.08-s['p95_delay_ms']*.03-alteration*.3,4)
    # Do not trade a significant fraction of original notes for an attractive
    # interval score. This compares original content, never inferred melody.
    best_coverage=max(a.stats['retention'] for a in options.values())
    eligible=[k for k,a in options.items() if a.stats['retention']>=best_coverage-.005]
    chosen=max(eligible,key=lambda k:(scores[k],k=='raw',-abs(options[k].stats['global_octave'])))
    best=options[chosen]
    if structure is not None:
        best.stats['analysis']=structure.metadata
        warning=tr('warning.model_fallback',reason=structure.metadata.get('fallback_reason'))
        if structure.metadata.get('fallback_reason') and warning not in best.warnings:
            best.warnings.append(warning)
    best.stats['recommendation']=dict(chosen=best.config.mode,candidate=chosen,
        layout_backend='legacy' if chosen.startswith('baseline:') else ('raw' if chosen=='raw' else config.arranger),
        reason=tr('reason.auto'),
        scores=scores,excluded={k:tr('reason.excluded') for k in options if k not in eligible},
        candidates={k:{name:a.stats[name] for name in ('retention','onset_retention','mean_source_retention',
            'continuity_interval_error','register_inversion_rate','mean_delay_ms','global_octave')}
            for k,a in options.items()})
    best.config=config
    return best
