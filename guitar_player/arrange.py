"""Voice extraction, phrase folding, whole-chord matching and physical planning."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from bisect import bisect_right
from statistics import median
from .jobs import checkpoint

from .model import (Arrangement, Config, Fingering, NOTE_MAX, NOTE_MIN, Note,
                    Song, Strike, TUNING, VoiceNote)


def group_notes(notes, window=.002):
    groups = []
    for note in sorted(notes, key=lambda n: (n.start, n.id)):
        checkpoint()
        if not groups or note.start - groups[-1][0].start > window + 1e-9:
            groups.append([])
        groups[-1].append(note)
    return groups


def choose_source(notes, sources, role):
    by_source = defaultdict(list)
    for n in notes:
        by_source[n.source].append(n)
    if not by_source:
        return None
    names = {s.id: s.name.casefold() for s in sources}
    total = len(notes)
    scored = []
    for sid, ns in by_source.items():
        pitches = sorted(n.pitch for n in ns)
        center = median(pitches)
        # A tiny high ornament track must not displace the main musical part.
        coverage = min(1, len(ns) / max(total * .20, 1))
        spread = max(n.start for n in ns) - min(n.start for n in ns)
        extent = spread / max(notes[-1].start - notes[0].start, 1)
        direction = 1 if role == 'melody' else -1
        hint = any(w in names.get(sid, '') for w in (
            ('melody', 'vocal', 'lead', '旋律', '主音') if role == 'melody'
            else ('bass', '低音', 'left hand')))
        score = direction * center / 12 + coverage * 2 + extent + hint * 3
        scored.append((score, len(ns), sid))
    return max(scored)[2]


def extract_melody(notes):
    """Bounded beam search, including rests while an upper voice is sustaining.

    This is a heuristic transcription, not a claim that the highest/loudest
    note is always the melody. Explicit source selection remains available.
    """
    groups = group_notes(notes)
    if not groups:
        return []
    center = sorted(n.pitch for n in notes)[int((len(notes) - 1) * .65)]
    # Keep a sounding upper anchor even if another candidate is briefly chosen;
    # otherwise a dense low run can earn enough rewards to steal the voice.
    # State: utility, previous note, persistent path, sounding upper anchor.
    states = [(0.0, None, None, None)]
    for group in groups:
        highest = max(n.pitch for n in group)
        candidates = sorted(group, key=lambda n: (
            n.pitch / 12 + min(n.duration, 1) * .6 + n.velocity / 300), reverse=True)[:6]
        next_states = []
        for score, prev, path, anchor in states:
            sounding = anchor is not None and min(anchor.end, anchor.start+2.) > group[0].start + .04
            if not sounding:
                anchor = None
            underneath = sounding and highest < anchor.pitch - 3
            next_states.append((score - (.03 if underneath else 1.1), prev, path, anchor))
            for n in candidates:
                value = 1.25 + min(n.duration, 1) * .35 + n.velocity / 400
                value -= (highest - n.pitch) * .13
                value -= max(0, center - n.pitch) * .055
                if prev:
                    gap = n.start - prev.start
                    value -= min(abs(n.pitch - prev.pitch), 24) * (.035 if gap < 1 else .012)
                    if sounding and n.pitch < anchor.pitch - 3:
                        value -= 1.65
                    if n.source != prev.source:
                        value -= .6
                new_anchor = anchor if anchor and anchor.pitch > n.pitch else n
                next_states.append((score + value, n, (n, path), new_anchor))
        # For identical previous notes only the best path can matter later.
        unique = {}
        for state in sorted(next_states, key=lambda s: s[0], reverse=True):
            key = (state[1].id if state[1] else None, state[3].id if state[3] else None)
            unique.setdefault(key, state)
            if len(unique) >= 12:
                break
        states = list(unique.values())
        # Keep score magnitudes bounded on hour-long files.
        peak = states[0][0]
        states = [(s - peak, n, p, a) for s, n, p, a in states]
    path = max(states, key=lambda s: s[0])[2]
    result = []
    while path:
        note, path = path
        result.append(note)
    return result[::-1]


def extract_bass(notes, melody_ids, density):
    result = []
    interval = {'conservative': 1., 'balanced': .75, 'complete': .5}[density]
    for group in group_notes(notes):
        available = [n for n in group if n.id not in melody_ids]
        if not available:
            continue
        n = min(available, key=lambda n: (n.pitch, -n.velocity))
        if not result:
            result.append(n)
            continue
        prev = result[-1]
        gap = n.beat - prev.beat
        changed = n.pitch != prev.pitch
        if gap >= interval - 1e-6 or (changed and gap >= .5 and n.beat_strength >= .4):
            result.append(n)
    return result


def best_offset(notes, target=None, bounds=(NOTE_MIN, NOTE_MAX)):
    if not notes:
        return 0
    def cost(offset):
        total = 0.
        for n in notes:
            p = n.pitch + offset
            distance = max(bounds[0] - p, p - bounds[1], 0)
            weight = .5 + n.velocity / 127 + min(n.duration, 2) * .25
            total += (distance * 3 + bool(distance) * 12) * weight
        total /= len(notes)
        total += abs(offset) * .015
        if target is not None:
            total += abs(median(n.pitch for n in notes) + offset - target) * .10
        return total
    return min(range(-60, 61, 12), key=lambda o: (cost(o), abs(o)))


def octave_candidates(pitch, density='complete', bounds=(NOTE_MIN, NOTE_MAX)):
    limit = {'conservative': 12, 'balanced': 24, 'complete': 120}[density]
    return sorted((p for p in range(bounds[0], bounds[1] + 1)
                   if (p - pitch) % 12 == 0 and abs(p - pitch) <= limit),
                  key=lambda p: (abs(p - pitch), p))


def fold_voice(notes, density, base=None, target=68, ceilings=None, bounds=(NOTE_MIN, NOTE_MAX)):
    """Optimize octave placement of entire phrases, including in-range notes."""
    if not notes:
        return {}
    phrases = []
    for note in notes:
        if not phrases or (note.start - phrases[-1][-1].end >= .35 or
                           note.start - phrases[-1][-1].start >= 2.5):
            phrases.append([])
        phrases[-1].append(note)
    mapping = {}
    for phrase in phrases:
        offset = base if base is not None else best_offset(phrase, target, bounds)
        # Missing candidates are explicitly dropped, never silently folded farther.
        seq = [(n, [p for p in octave_candidates(n.pitch + offset, density, bounds)
                    if ceilings is None or p <= ceilings.get(n.id, bounds[1])]) for n in phrase]
        seq = [(n, cs) for n, cs in seq if cs]
        if not seq:
            continue
        layers = []
        for i, (note, candidates) in enumerate(seq):
            layer = {}
            for p in candidates:
                unary = abs(p - (note.pitch + offset)) * .13 + abs(p - target) * .015
                if not i:
                    layer[p] = (unary, None)
                    continue
                prev_note = seq[i-1][0]
                original_interval = note.pitch - prev_note.pitch
                options = []
                for prev_pitch, (cost, _) in layers[-1].items():
                    interval = p - prev_pitch
                    changed_octave = interval != original_interval
                    reversed_direction = interval * original_interval < 0
                    transition = (abs(interval - original_interval) * .65 +
                                  changed_octave * 4 + reversed_direction * 9)
                    options.append((cost + unary + transition, prev_pitch))
                layer[p] = min(options)
            layers.append(layer)
        p = min(layers[-1], key=lambda p: layers[-1][p][0])
        for i in range(len(seq) - 1, -1, -1):
            mapping[seq[i][0].id] = p
            p = layers[i][p][1]
    return mapping


CHORDS = ({0, 4, 7}, {0, 3, 7}, {0, 4, 7, 10}, {0, 4, 7, 11},
          {0, 3, 7, 10}, {0, 2, 7}, {0, 5, 7}, {0, 3, 6})


def harmony_weights(notes):
    pcs = {n.pitch % 12 for n in notes}
    if len(pcs) < 3:
        return {}
    candidates = []
    for root in range(12):
        for template in CHORDS:
            actual = {(root + p) % 12 for p in template}
            score = len(pcs & actual) / len(pcs | actual)
            candidates.append((score, root, actual))
    score, root, actual = max(candidates, key=lambda x: x[0])
    if score < .7:
        return {}
    return {pc: (3 if pc == root else 2 if (pc-root) % 12 in (3, 4)
                 else 1) for pc in actual}


def assign_strings(voices, max_voices=6, previous=None, occupied=None, at=0., tuning=None):
    """Maximum-weight matching over all six strings, including a drop option.

    DP state is the used-string bitmask. Unlike greedy placement this finds
    64 on B/5 and 83 on N/19 together. No fallback ever reuses an occupied string.
    """
    previous = previous or {}
    occupied = occupied or {}
    tuning = tuning or Config().tuning
    max_voices = min(max_voices or 6, 6)
    states = {0: (0., ())}
    for voice in voices:
        options = [(s, voice.pitch - base) for s, base in enumerate(tuning)
                   if 0 <= voice.pitch - base < 20]
        updated = dict(states)
        for mask, (score, fingering) in states.items():
            if mask.bit_count() >= max_voices:
                continue
            for string, fret in options:
                if mask & (1 << string):
                    continue
                cost = (fret * .015 + abs(fret - previous.get(string, fret)) * .008 +
                        max(0, occupied.get(string, 0) - at) * 30)
                value = score + voice.weight - cost
                newmask = mask | (1 << string)
                if value > updated.get(newmask, (-float('inf'), ()))[0]:
                    updated[newmask] = (value, fingering + (Fingering(voice, string, fret),))
        states = updated
    return list(max(states.values(), key=lambda s: s[0])[1])


def arrange(song: Song, config: Config = Config()) -> Arrangement:
    from .engine import arrange_once, recommend, recommend_guitar
    if config.mode == 'auto':
        return recommend(song, config)
    if config.mode == 'guitar':
        return recommend_guitar(song, config)
    # 原样演奏 keeps the strings and frets a sheet chose; 还原 may arrange it anew.
    return arrange_once(song, config, fingering=song.fingering if config.mode == 'raw' else None)


def recommend(song, config):
    from .engine import recommend as choose_complete
    return choose_complete(song, config)
