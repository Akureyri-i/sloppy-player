"""Chord skeleton per beat, for 吉他化's "keep the root and third" rule.

Template matching on duration-weighted pitch classes (the lowest sounding note
counts extra), smoothed with a Viterbi pass that charges for every chord change.
Checked against POP909's chord annotations.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Chord quality -> intervals above the root, in order root, third, fifth, seventh.
TEMPLATES = {'maj': (0, 4, 7), 'min': (0, 3, 7), '7': (0, 4, 7, 10), 'min7': (0, 3, 7, 10),
             'maj7': (0, 4, 7, 11), 'sus2': (0, 2, 7), 'sus4': (0, 5, 7), 'dim': (0, 3, 6), 'aug': (0, 4, 8)}
PRIOR = {'maj': 0., 'min': 0., '7': -.04, 'min7': -.04, 'maj7': -.06, 'sus2': -.08, 'sus4': -.08,
         'dim': -.12, 'aug': -.15}
OUTSIDE = .6          # score lost per unit of weight on pitch classes outside the chord
BASS = .15            # bonus when the lowest note is the root
CHANGE = .12          # cost of changing chord between neighbouring beats
QUIET = .05           # beats with less total weight than this have no chord
ROOT, THIRD, FIFTH, SEVENTH, OTHER = range(5)


@dataclass(frozen=True)
class Chord:
    start: float               # beats
    end: float
    root: int | None           # pitch class; None = no chord
    quality: str

    @property
    def tones(self):
        return tuple((self.root+i) % 12 for i in TEMPLATES[self.quality]) if self.root is not None else ()


def _candidates():
    out = []
    for quality, intervals in TEMPLATES.items():
        for root in range(12):
            mask = np.zeros(12)
            mask[[(root+i) % 12 for i in intervals]] = 1
            out.append((root, quality, mask))
    return out


CANDIDATES = _candidates()


def beat_weights(notes, step=1.):
    """(segment starts in beats, [segments, 12] weights, [segments] bass pitch class or -1)."""
    tpb = next((n.tick/n.beat for n in notes if n.beat > 0 and n.tick > 0), 480.)
    first = np.floor(min(n.beat for n in notes)/step)*step
    last = max(n.beat+(n.end_tick-n.tick)/tpb for n in notes)
    starts = np.arange(first, last, step)
    weights = np.zeros((len(starts), 12))
    lowest = np.full(len(starts), 999)
    for n in notes:
        end = n.beat+max(.05, (n.end_tick-n.tick)/tpb)
        a, b = int((n.beat-first)//step), int(min(len(starts)-1, (end-first-1e-9)//step))
        for k in range(a, b+1):
            overlap = min(end, starts[k]+step)-max(n.beat, starts[k])
            weights[k, n.pitch % 12] += max(0., overlap)/step
            lowest[k] = min(lowest[k], n.pitch)
    bass = np.where(lowest < 999, lowest % 12, -1)
    for k, pc in enumerate(bass):
        if pc >= 0:
            weights[k, pc] += .5
    return starts, weights, bass


def chords(notes, step=1.):
    """Chords per segment of `step` beats, merged where the same chord continues."""
    if not notes:
        return []
    starts, weights, bass = beat_weights(notes, step)
    totals = weights.sum(1)
    norm = weights/np.maximum(totals, 1e-9)[:, None]
    masks = np.array([m for _, _, m in CANDIDATES])
    scores = norm@masks.T - OUTSIDE*(norm@(1-masks).T)
    scores += np.array([PRIOR[q] for _, q, _ in CANDIDATES])
    scores += BASS*(bass[:, None] == np.array([r for r, _, _ in CANDIDATES])[None])
    scores = np.concatenate([scores, np.where(totals < QUIET, 1., -1.)[:, None]], axis=1)   # last = no chord
    # Viterbi: stay free, change costs CHANGE.
    best = scores[0].copy()
    back = np.zeros(scores.shape, int)
    for k in range(1, len(scores)):
        stay = best
        jump = best.max()-CHANGE
        back[k] = np.where(stay >= jump, np.arange(scores.shape[1]), best.argmax())
        best = np.maximum(stay, jump)+scores[k]
    path = [int(best.argmax())]
    for k in range(len(scores)-1, 0, -1):
        path.append(back[k, path[-1]])
    path.reverse()
    out = []
    for k, c in enumerate(path):
        root, quality = (CANDIDATES[c][0], CANDIDATES[c][1]) if c < len(CANDIDATES) else (None, 'maj')
        if out and out[-1].root == root and out[-1].quality == quality:
            out[-1] = Chord(out[-1].start, starts[k]+step, root, quality)
        else:
            out.append(Chord(float(starts[k]), float(starts[k]+step), root, quality))
    return out


def chord_at(chords_, beat):
    lo, hi = 0, len(chords_)
    while lo < hi:
        mid = (lo+hi)//2
        if chords_[mid].end <= beat:
            lo = mid+1
        else:
            hi = mid
    return chords_[min(lo, len(chords_)-1)] if chords_ else None


def functions(notes, chords_):
    """Per note: ROOT, THIRD, FIFTH, SEVENTH or OTHER in the chord at its onset."""
    out = np.full(len(notes), OTHER)
    for i, n in enumerate(notes):
        c = chord_at(chords_, n.beat)
        if c is not None and c.root is not None:
            tones = c.tones
            pc = n.pitch % 12
            if pc in tones:
                out[i] = (ROOT, THIRD, FIFTH, SEVENTH)[tones.index(pc)]
    return out
