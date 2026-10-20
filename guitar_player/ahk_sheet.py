"""Sheets for ml3czus-autoplayer (an AutoHotkey auto-player for the same Roblox guitar).

The format (ml3czus-autoplayer 1.3.2): a line "Key delay: N" (ms; "Pause delay: N"
likewise) is taken out of the sheet. Then each
`[...]` (keys pressed together), single key or `-` takes one key delay, `|` `/` `\\`
one pause delay; spaces and line breaks take no time. A fret key selects the fret
until the next one; a string key sounds that string at the selected fret. With
---START--- in the sheet (its "New Sheet Detector"), only what follows is played,
up to ---END---, and ---N--- sets the key delay to N ms (---RESET--- back).
`[` and `]` are the chord brackets, so the fret keys of frets 8 and 9 cannot be
written; single ones are skipped.
Lines starting with # are taken as notes about the song, not keys.

Written (write_sheet): "Key delay: 31", then one bar per line, one 31 ms step per
token; every note complete (fret key, then string key) inside `[...]`. 31 ms is just
under two 15.6 ms Windows timer ticks, so a step lasts about as long whether or not a
player's waits are rounded up to the tick.
Notes on fret 8 or 9 move to another string; the 吞音 guard is kept on the grid.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
import re

from .engine import END_GUARD
from .i18n import tr
from .midi import MeterMap, TempoMap
from .model import FRET_KEYS, GAME_SOUND_LENGTH, STRING_KEYS, Config, Note, Song, Source

KEY_DELAY, PAUSE_DELAY = 200, 100       # ms, the player's defaults
STEP = 31                               # ms per step of a written sheet
UNSENDABLE = (8, 9)                     # fret keys '[' and ']'
TOKEN = re.compile(r'\[[^\[\]]+\]|.')
TPB = 500                               # ticks per quarter at 120 bpm: one tick per ms


def _delay(value, default):
    return value if 0 <= value <= 500 else default


def read_sheet(text, path):
    """The sheet as a Song whose notes keep their strings and frets (Song.fingering)."""
    lines = [line.strip() for line in text.splitlines()]
    detector = '---START---' in lines
    key_delay = pause_delay = None
    body = []
    for line in lines:
        pause = re.search(r'(?i)pause\s*delay\D{0,10}?(\d+)', line)
        key = re.search(r'(?i)delay\D{0,10}?(\d+)', line)
        if pause and pause_delay is None:
            pause_delay = int(pause.group(1))
        elif key and key_delay is None and 'pause' not in line.lower():
            key_delay = int(key.group(1))
        elif not line.startswith('#'):
            body.append(line)
    key_delay = _delay(KEY_DELAY if key_delay is None else key_delay, KEY_DELAY)
    pause_delay = _delay(PAUSE_DELAY if pause_delay is None else pause_delay, PAUSE_DELAY)
    if detector:
        body = body[body.index('---START---')+1:]
    tuning = Config().tuning
    presses, skipped, unfretted = [], 0, 0
    now, fret, delay = 0., None, key_delay
    for line in body:
        if detector:
            if line == '---END---':
                break
            if line == '---RESET---':
                delay = key_delay
                continue
            zone = re.fullmatch(r'---(\d+)---', line)
            if zone:
                delay = int(zone.group(1))
                continue
        for token in TOKEN.findall(line):
            if not token.strip():
                continue
            if token in '|/\\':
                now += pause_delay
                continue
            if token == '-':
                now += delay
                continue
            if token in '[]':
                skipped += 1            # never sent by its player; no time either
                continue
            for key in (token[1:-1] if len(token) > 1 else token).lower():
                if key in FRET_KEYS:
                    fret = FRET_KEYS.index(key)
                elif key in STRING_KEYS:
                    unfretted += fret is None
                    presses.append((now, STRING_KEYS.index(key), fret or 0))
            now += delay
    return _song(presses, tuning, now, path, skipped, unfretted)


def _song(presses, tuning, total, path, skipped, unfretted):
    meters = MeterMap([], TPB)
    pitches = [tuning[s]+f for _, s, f in presses]
    next_same = {}
    ends = [0.]*len(presses)
    for i in range(len(presses)-1, -1, -1):
        at, pitch = presses[i][0], pitches[i]
        ends[i] = min(at+GAME_SOUND_LENGTH.get(pitch, 2.)*1000, next_same.get(pitch, float('inf')))
        next_same[pitch] = at
    notes, fingering = [], {}
    for i, (at, string, fret) in enumerate(presses):
        tick, end = round(at), max(round(at)+1, round(ends[i]))
        notes.append(Note(i, pitches[i], 100, at/1000, end/1000, tick, end, tick/TPB,
                          meters.strength(tick), '0:0', 0))
        fingering[i] = (string, fret)
    warnings = []
    if skipped:
        warnings.append(tr('warning.sheet_skipped', count=skipped))
    if unfretted:
        warnings.append(tr('warning.sheet_unfretted', count=unfretted))
    sources = [Source('0:0', 0, 0, tr('sheet.source'), len(notes), min(pitches), max(pitches))] if notes else []
    return Song(str(path), notes, sources, max([total/1000]+[n.end for n in notes]), TPB, warnings,
                fingering=fingering)


def placement(pitch, string, tuning):
    """(string, fret) for pitch avoiding frets 8 and 9: the given string if it can, else the nearest."""
    options = [(abs(s-string), s, pitch-tuning[s]) for s in range(6)
               if 0 <= pitch-tuning[s] < len(FRET_KEYS) and pitch-tuning[s] not in UNSENDABLE]
    _, s, f = min(options)
    return s, f


def write_sheet(arrangement, step_ms=STEP):
    """(sheet text, {'moved': notes moved off frets 8 / 9, 'guarded': notes moved for the 吞音 guard})."""
    a = arrangement
    config = a.config
    tuning = config.tuning
    notes = sorted(((f.voice.note.start/config.speed, f.voice.pitch, f.string, f.fret)
                    for s in a.strikes for f in s.notes), key=lambda n: (n[0], n[1]))
    steps = defaultdict(list)
    moved = guarded = 0
    last = {}
    for at, pitch, string, fret in notes:
        step = round(at*1000/step_ms)
        if fret in UNSENDABLE:
            string, fret = placement(pitch, string, tuning)
            moved += 1
        length = GAME_SOUND_LENGTH.get(pitch)
        if config.end_guard and pitch in last and length:
            # Keep a re-press of this pitch out of the moment its last sound ends (DECISIONS: 吞音).
            early = (last[pitch]*step_ms/1000+length-END_GUARD[0])*1000/step_ms
            late = (last[pitch]*step_ms/1000+length+END_GUARD[1])*1000/step_ms
            if early < step < late:
                options = [s for s in (int(early), int(late)+1) if s > last[pitch]]
                step = min(options, key=lambda s: abs(s-step))
                guarded += 1
        last[pitch] = step
        if (string, fret) not in steps[step]:
            steps[step].append((string, fret))
    lines = [f'Key delay: {step_ms}']
    if steps:
        bars = bar_steps(a.song, config.speed, step_ms)
        tokens = defaultdict(list)
        for step in range(max(steps)+1):
            keys = ''.join(FRET_KEYS[f]+STRING_KEYS[s] for s, f in sorted(steps.get(step, ()), key=lambda k: -k[0]))
            tokens[bisect_right(bars, step)].append(f'[{keys}]' if keys else '-')
        lines += [''.join(tokens[k]) for k in sorted(tokens)]
    return '\n'.join(lines)+'\n', dict(moved=moved, guarded=guarded)


def bar_steps(song, speed, step_ms=STEP):
    """Steps at which the song's bars start, for one bar per line."""
    tempos = TempoMap(song.tempos, song.ticks_per_beat)
    end = max((n.end_tick for n in song.notes), default=0)
    out, tick, k = [], 0, 0
    meters = sorted(song.meters)
    meter = (4, 4)
    while tick <= end:
        while k < len(meters) and meters[k][0] <= tick:
            meter = meters[k][1:]
            k += 1
        out.append(round(tempos.seconds_at(tick)/speed*1000/step_ms))
        tick += max(1, round(song.ticks_per_beat*4*meter[0]/meter[1]))
    return out[1:]
