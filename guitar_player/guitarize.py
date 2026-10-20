"""吉他化: keep a guitar-solo skeleton of a piano score.

1. Roles: melody / bridge / accompaniment per note (roles.py; rule fallback).
2. Melody line: one note per onset group, Viterbi over P(melody)+P(bridge) with a
   cost for leaps; with the model, only the top note of a group is a candidate unless
   the model is confident about another (gated_line); placed by whole phrases
   (arrange.fold_voice) around MIDI 63.
3. Bass line: the lowest other note of each group, placed around MIDI 49.
4. Values for the rest: chord function (harmony.py), beat strength, length;
   octave doublings of a kept pitch class count less.
   With 简化 above 0, a dense melody is thinned to melody_cap notes per beat (thin_melody);
   a thinned note's onset goes with it, except the bass.
5. Per onset group, every subset a real guitar could hold (one note per string,
   frets within SPAN, at most K notes, always the melody note); CP-SAT picks one
   per group, maximising value with a soft limit on notes per beat.

Targets come from real guitar arrangements: statistics of the 403 songs of AnimeTAB
(CC BY-NC data; credited in THIRD_PARTY.md).
"""
from __future__ import annotations

from collections import defaultdict
from itertools import combinations
import math

import numpy as np

from . import harmony
from .arrange import fold_voice
from .music_model import ModelUnavailable, get_features
from .roles import onset_groups, predict_roles, rule_roles

# AnimeTAB, song medians: 4.4 notes/s, 2.6 notes/beat (IQR 2.2-3.4), 1.52 notes per
# onset, 64% single notes, top note MIDI 62, lowest MIDI 49, fret span p90 2, strings p90 ~4.
NOTES_PER_BEAT = 2.6
# Melody: human tabs of the same songs put the whole melody an octave or two down so that its
# median is nearest MIDI 63 (36 songs; chosen on 18, checked on the other 18).
MELODY_TARGET, BASS_TARGET = 63, 49
SPAN = 3                    # frets between notes held together (measured p90 2; the game has no open strings)
MAX_NOTES = 4
LEAP = .9                   # melody line: cost per octave of leap
MOVE = math.log(.8/.1)/4    # moving a note by an octave (not-changed 0.8 : octave 0.1, Nakamura & Sagayama)
WINDOW = 16.                # beats per optimisation window
CROWD = .4                  # value lost for each note added to a group beside the melody (guitar texture is mostly single notes)
OVER = 2.                   # value lost per note above the per-beat budget
# CROWD, OVER, BASS_VALUE: a grid search; the best MERT audio similarity to the original among
# settings whose texture stays inside the AnimeTAB interquartile range.
BASS_VALUE, BRIDGE_VALUE = 2.5, 3.
FUNCTION_VALUE = {harmony.ROOT: 2., harmony.THIRD: 1.8, harmony.SEVENTH: 1., harmony.FIFTH: .8, harmony.OTHER: .4}
MELODY, BRIDGE, BASS, CONTENT = 'melody', 'bridge', 'bass', 'content'
# Melody line on solo piano (checked on PDMX piano scores with lyrics): the top note of an onset is the
# candidate and the model decides whether the onset has a melody note, leaning LEAN (log-odds)
# toward yes; another note only when its P(melody or bridge) is CONFIDENT above the top note's.
LEAN = 1.5
# Dense melodies: human tabs of the same songs thin a melody to about 2 notes per beat
# where the piano is dense; which notes go is not predictable from the notes. With 简化 above 0
# the melody may hold at most melody_cap(simplify) notes per beat within any THIN_WINDOW beats;
# the least audible notes go first (short, weak beat), phrase starts and contour turns stay.
THIN_WINDOW = 2.
CAP_FREE, CAP_TIGHT = 4., 1.          # notes per beat at 简化 just above 0 and at 100%
CONFIDENT = .7                # chosen on half the PDMX lyric scores and same-song tabs; the other halves agree


def role_probabilities(song, notes, melody_source=None):
    """[len(notes), 3] in the order of `notes`, and the backend used."""
    if melody_source:
        return rule_roles(notes, preferred=melody_source), 'track'
    try:
        encoded, features, _ = get_features(song, notes)
        by_id = {n.id: k for k, n in enumerate(notes)}
        ordered = [notes[by_id[int(i)]] for i in encoded.ids]
        p = predict_roles(ordered, features)
        out = np.zeros_like(p)
        out[[by_id[int(i)] for i in encoded.ids]] = p
        return out, 'model'
    except ModelUnavailable:   # no model installed: the rule still gives a usable line
        return rule_roles(notes), 'rule'


def gated_line(notes, groups, melodic):
    """melody_line over the top note of each group (plus any note the model is CONFIDENT about),
    with the group's highest P(melodic) as the top note's, leaning LEAN toward a melody note."""
    candidates, weight = [], melodic.copy()
    for g in groups:
        top = max(g, key=lambda i: notes[i].pitch)
        others = [i for i in g if i != top and melodic[i]-melodic[top] >= CONFIDENT]
        if not others:
            weight[top] = max(melodic[i] for i in g)
        candidates.append([top]+others)
    odds = weight/np.maximum(1e-6, 1-weight)*math.exp(LEAN)
    return melody_line(notes, candidates, odds/(1+odds))


def melody_cap(simplify):
    """Melody notes per beat allowed at this 简化 setting; None at 0 (the whole melody)."""
    return None if simplify <= 0 else CAP_TIGHT+(CAP_FREE-CAP_TIGHT)*(1-simplify)**2


def thin_melody(notes, melody, simplify):
    """Melody indices left after the cap, and the ones taken out. In the most crowded window
    the least audible note goes (weak beat, short; a turn of the contour counts more), until no
    window holds more than the cap; phrase starts always stay."""
    cap = melody_cap(simplify)
    line = sorted(melody, key=lambda i: notes[i].beat)
    if cap is None or len(line) < 3:
        return set(line), set()
    beats = [notes[i].beat for i in line]
    kept, removed, stuck = list(range(len(line))), set(), set()
    while True:
        worst, lo = None, 0
        for hi in range(len(kept)):                    # windows ending at each kept note
            while beats[kept[hi]]-beats[kept[lo]] >= THIN_WINDOW:
                lo += 1
            if hi-lo+1 > cap*THIN_WINDOW and (lo, hi) not in stuck and (worst is None or hi-lo > worst[1]-worst[0]):
                worst = (lo, hi)
        if worst is None:
            break
        options = []
        for j in range(worst[0], worst[1]+1):
            if not j or beats[kept[j]]-beats[kept[j-1]] >= 1.:          # a phrase start
                continue
            p, a = notes[line[kept[j]]].pitch, notes[line[kept[j-1]]].pitch
            b = notes[line[kept[j+1]]].pitch if j+1 < len(kept) else p
            gap = beats[kept[j+1]]-beats[kept[j]] if j+1 < len(kept) else 2.
            importance = notes[line[kept[j]]].beat_strength+min(gap, 2.)/2+.5*((p-a)*(p-b) > 0)
            options.append((importance, j))
        if not options:
            stuck.add(worst)
            continue
        removed.add(kept.pop(min(options)[1]))
        stuck.clear()
    return {line[k] for k in kept}, {line[k] for k in removed}


def melody_line(notes, groups, melodic):
    """Viterbi: per group one note (or none); emission log P(melodic), leaps cost."""
    states, scores, back = [], [], []
    for g in groups:
        cands = [None]+list(g)
        emit = [math.log(max(1e-3, 1-max(melodic[i] for i in g)))]+[math.log(max(1e-3, melodic[i])) for i in g]
        if not states:
            score, ptr = emit, [0]*len(cands)
        else:
            prev, prev_score = states[-1], scores[-1]
            score, ptr = [], []
            for c, e in zip(cands, emit):
                best, arg = -1e18, 0
                for k, (p, s) in enumerate(zip(prev, prev_score)):
                    cost = 0. if c is None or p is None else LEAP*abs(notes[c].pitch-notes[p].pitch)/12
                    if s-cost > best:
                        best, arg = s-cost, k
                score.append(best+e)
                ptr.append(arg)
        states.append(cands)
        scores.append(score)
        back.append(ptr)
    k = int(np.argmax(scores[-1])) if scores else 0
    line = []
    for t in range(len(states)-1, -1, -1):
        if states[t][k] is not None:
            line.append(states[t][k])
        k = back[t][k]
    return set(line)


def placements(pitches, tuning, span=SPAN, frets=20):
    """Whether these pitches fit on the strings: one per string, frets within `span`."""
    options = [[(s, p-tuning[s]) for s in range(6) if 0 <= p-tuning[s] < frets] for p in pitches]

    def search(k, used, lo, hi):
        if k == len(options):
            return True
        for s, f in options[k]:
            if s not in used and max(hi, f)-min(lo, f) <= span:
                if search(k+1, used | {s}, min(lo, f), max(hi, f)):
                    return True
        return False
    return search(0, frozenset(), 99, -1)


_analysis = {}


def analysis(song, notes, melody_source):
    """Everything that does not depend on the octave placement; the candidates of
    one song share it (only the latest song is kept)."""
    key = (id(song), tuple(n.id for n in notes), melody_source)
    if key not in _analysis:
        probs, backend = role_probabilities(song, notes, melody_source)
        groups = onset_groups(notes)
        melodic = probs[:, 0]+probs[:, 1]
        # The model's line on solo piano; a track the user named or the rule keep their own top notes.
        melody = gated_line(notes, groups, melodic) if backend == 'model' else melody_line(notes, groups, melodic)
        bass = set()
        for g in groups:
            rest = [i for i in g if i not in melody]
            if rest:
                low = min(rest, key=lambda i: notes[i].pitch)
                top = max((notes[i].pitch for i in g if i in melody), default=999)
                if notes[low].pitch <= top-5:
                    bass.add(low)
        chords = harmony.chords(notes)
        _analysis.clear()
        _analysis[key] = (probs, backend, groups, melody, bass, chords, harmony.functions(notes, chords))
    return _analysis[key]


def guitarize(song, notes, config, offset):
    """{note id: played pitch}, {note id: role}, {dropped id: 'guitarize' | 'range'}, info."""
    if not notes:
        return {}, {}, {}, {}
    lo, hi = config.bounds
    probs, backend, groups, whole_melody, bass, chords, function = analysis(song, notes, config.melody_source)
    melody, thinned = thin_melody(notes, whole_melody, config.simplify)
    # A thinned melody note takes its onset with it, the bass excepted (what human arrangers do,
    # and the next accompaniment note up would otherwise sound like the melody).
    silenced = {i for g in groups if thinned.intersection(g) for i in g if i not in bass}
    mapping = fold_voice([notes[i] for i in sorted(melody, key=lambda i: notes[i].start)], 'complete', offset,
                         MELODY_TARGET, bounds=config.bounds)
    melody_pitch = {notes[i].id: mapping.get(notes[i].id) for i in melody}
    ceilings = {}
    for g in groups:
        top = max((melody_pitch[notes[i].id] for i in g if i in melody and melody_pitch[notes[i].id]), default=hi)
        for i in g:
            ceilings[notes[i].id] = top-3
    mapping.update(fold_voice([notes[i] for i in sorted(bass, key=lambda i: notes[i].start)], 'complete', offset,
                              BASS_TARGET, ceilings, config.bounds))
    roles, values, pitch_options = {}, {}, {}
    for i, n in enumerate(notes):
        if i in melody or i in thinned:
            roles[n.id] = MELODY
        elif i in bass:
            roles[n.id] = BASS
        elif probs[i, 1] > .5:
            roles[n.id] = BRIDGE
        else:
            roles[n.id] = CONTENT
        if n.id in mapping:
            pitch_options[n.id] = [(mapping[n.id], 0.)]
        else:
            p = n.pitch+offset
            pitch_options[n.id] = [(q, MOVE*abs(q-p)/12) for q in (p, p-12, p+12) if lo <= q <= hi][:2]
        if i in silenced:
            pitch_options[n.id] = []
        value = (BRIDGE_VALUE if roles[n.id] == BRIDGE else BASS_VALUE if roles[n.id] == BASS else FUNCTION_VALUE[int(function[i])])
        value += .5*min(1., n.beat_strength)+.3*min(2., n.duration)/2
        values[n.id] = value
    kept, dropped = choose(notes, groups, melody, roles, values, pitch_options, config)
    for n in notes:
        if n.id not in kept and n.id not in dropped:
            dropped[n.id] = 'guitarize'
    for i in silenced:
        dropped[notes[i].id] = 'guitarize'
    info = dict(role_backend=backend, melody=len(melody), melody_thinned=len(thinned), bass=len(bass), chords=len(chords),
                kept=len(kept), melody_kept=sum(notes[i].id in kept for i in melody),
                bass_kept=sum(notes[i].id in kept for i in bass), **measure(notes, kept, melody, chords))
    return kept, roles, dropped, info


def measure(notes, kept, melody, chords):
    """Objective qualities of a 吉他化 result (no listening): melody register (share in 52-76, median),
    chord skeleton coverage, texture against the AnimeTAB medians, octave moves."""
    melody_pitches = [kept[notes[i].id] for i in melody if notes[i].id in kept]
    sweet = float(np.mean([52 <= p <= 76 for p in melody_pitches])) if melody_pitches else 0.
    middle = float(np.median(melody_pitches)) if melody_pitches else MELODY_TARGET+24.
    kept_notes = [n for n in notes if n.id in kept]
    covered = total = 0
    for c in chords:
        if c.root is None:
            continue
        total += 1
        pcs = {kept[n.id] % 12 for n in kept_notes if c.start <= n.beat < c.end}
        covered += bool(pcs & set(c.tones[:2]))
    groups = defaultdict(int)
    for n in kept_notes:
        groups[round(n.start, 2)] += 1
    beats = max(1., max((n.beat for n in kept_notes), default=0)-min((n.beat for n in kept_notes), default=0)+1)
    per_beat = len(kept_notes)/beats
    group_mean = float(np.mean(list(groups.values()))) if groups else 0.
    moved = sum(kept[n.id] != n.pitch for n in kept_notes)
    return dict(melody_in_register=round(sweet, 4), melody_median=middle, chord_coverage=round(covered/max(1, total), 4),
                notes_per_beat=round(per_beat, 3), group_mean=round(group_mean, 3),
                texture_distance=round(abs(per_beat-NOTES_PER_BEAT)/NOTES_PER_BEAT+abs(group_mean-1.52)/1.52, 4),
                octave_moved=round(moved/max(1, len(kept_notes)), 4))


def candidate_score(info):
    """Higher is better; compares octave placements of one song. The melody's median against
    MELODY_TARGET dominates (40 per octave away): the whole-piece octave human arrangers choose."""
    return (-40*abs(info['melody_median']-MELODY_TARGET)/12+30*info['chord_coverage']-20*info['texture_distance']
            - 10*info['octave_moved'])


def texture_limits(config):
    """(notes per onset, notes per beat) for the 简化 slider (0 = guitar median)."""
    k = MAX_NOTES if config.simplify < .34 else 3 if config.simplify < .67 else 2
    return k, NOTES_PER_BEAT*(1-.6*config.simplify)


def choose(notes, groups, melody, roles, values, pitch_options, config):
    """Pick one playable subset per onset group (CP-SAT, per window of WINDOW beats)."""
    from ortools.sat.python import cp_model
    k_max, per_beat = texture_limits(config)
    tuning = config.tuning
    options = []         # per group: [(value, {id: pitch})]
    dropped = {}
    for g in groups:
        must = [notes[i] for i in g if i in melody and pitch_options[notes[i].id]]
        free = [notes[i] for i in g if i not in melody and pitch_options[notes[i].id]]
        for i in g:
            if not pitch_options[notes[i].id]:
                dropped[notes[i].id] = 'range'
        free.sort(key=lambda n: -values[n.id])
        free = free[:8]                       # the most valuable few are enough to choose from
        group_options = []
        for size in range(0, k_max-len(must)+1):
            for extra in combinations(free, size):
                chosen = must+list(extra)
                for pitches in _pitch_choices(chosen, pitch_options):
                    ps = list(pitches.values())
                    if len(set(ps)) < len(ps) or not placements(ps, tuning):
                        continue
                    value = sum(values[n.id]-CROWD for n in extra)-sum(c for n in chosen for q, c in pitch_options[n.id]
                                                                  if q == pitches[n.id])
                    seen = set()
                    for n in sorted(chosen, key=lambda n: -values[n.id] if n not in must else -99):
                        pc = pitches[n.id] % 12
                        if pc in seen and n not in must:
                            value -= .6*values[n.id]
                        seen.add(pc)
                    group_options.append((value, pitches))
                    break
        if not group_options:
            group_options = [(0., {})]
        options.append(group_options)
    kept = {}
    beats = [notes[g[0]].beat for g in groups]
    start = 0
    while start < len(groups):
        end = start
        while end < len(groups) and beats[end] < beats[start]+WINDOW:
            end += 1
        model = cp_model.CpModel()
        pick = [[model.NewBoolVar('') for _ in options[g]] for g in range(start, end)]
        for row in pick:
            model.AddExactlyOne(row)
        terms = []
        per_window = defaultdict(list)
        for row, g in zip(pick, range(start, end)):
            for var, (value, pitches) in zip(row, options[g]):
                terms.append(int(round(value*100))*var)
                per_window[int(beats[g]-beats[start])].append(len(pitches)*var)
        for counts in per_window.values():
            excess = model.NewIntVar(0, 50, '')
            model.Add(sum(counts) <= int(math.ceil(per_beat))+excess)
            terms.append(-int(OVER*100)*excess)
        model.Maximize(sum(terms))
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = .5
        solver.parameters.num_workers = 4
        status = solver.Solve(model)
        for row, g in zip(pick, range(start, end)):
            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                choice = next(k for k, var in enumerate(row) if solver.Value(var))
            else:
                choice = max(range(len(options[g])), key=lambda k: options[g][k][0])
            kept.update(options[g][choice][1])
        start = end
    return kept, dropped


def _pitch_choices(chosen, pitch_options):
    """Each note's pitch options, cheapest first; at most a few combinations."""
    combos = [{}]
    for n in chosen:
        combos = [{**c, n.id: q} for c in combos for q, _ in pitch_options[n.id]][:8]
    return combos
