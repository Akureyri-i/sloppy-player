"""Lossless note identity and deterministic MIDI timing; no keyboard imports."""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict, deque
from pathlib import Path

import mido

from .i18n import tr
from .model import Note, Song, Source
from .jobs import checkpoint


class TempoMap:
    def __init__(self, changes, ticks_per_beat):
        self.tpb = ticks_per_beat
        if ticks_per_beat <= 0:
            raise ValueError(tr('error.midi.smpte'))
        # Defaults are replaced by real events at tick zero, never value-sorted.
        merged = {0: 500000}
        for tick, tempo in changes:
            if tempo <= 0:
                raise ValueError(tr('error.midi.tempo'))
            merged[tick] = tempo
        self.ticks = sorted(merged)
        self.tempos = [merged[t] for t in self.ticks]
        self.seconds = [0.0]
        for i in range(1, len(self.ticks)):
            self.seconds.append(self.seconds[-1] + mido.tick2second(
                self.ticks[i] - self.ticks[i-1], self.tpb, self.tempos[i-1]))

    def seconds_at(self, tick):
        i = bisect_right(self.ticks, tick) - 1
        return self.seconds[i] + mido.tick2second(
            tick - self.ticks[i], self.tpb, self.tempos[i])


class MeterMap:
    def __init__(self, changes, tpb):
        merged = {0: (4, 4)}
        for tick, numerator, denominator in changes:
            if numerator <= 0 or denominator <= 0:
                raise ValueError(tr('error.midi.meter'))
            merged[tick] = (numerator, denominator)
        self.ticks = sorted(merged)
        self.values = [merged[t] for t in self.ticks]
        self.tpb = tpb

    def strength(self, tick):
        i = bisect_right(self.ticks, tick) - 1
        numerator, denominator = self.values[i]
        unit = self.tpb * 4 / denominator
        beat = (tick - self.ticks[i]) / unit
        if abs(beat - round(beat)) > .05:
            return .15
        pos = round(beat) % numerator
        if pos == 0:
            return 1.0
        if (numerator == 4 and pos == 2) or (
                numerator > 3 and numerator % 3 == 0 and pos % 3 == 0):
            return .7
        return .4


def read_midi(path, midi=None) -> Song:
    """midi: an already built mido.MidiFile for path (an imported MusicXML score)."""
    checkpoint(tr('progress.read'))
    midi = midi or mido.MidiFile(path)
    checkpoint()
    if midi.type == 2:
        raise ValueError(tr('error.midi.type2'))
    events = []
    track_names = {}
    for ti, track in enumerate(midi.tracks):
        checkpoint()
        tick = 0
        track_names[ti] = track.name.replace('\x00', '').strip() or f'Track {ti + 1}'
        for order, msg in enumerate(track):
            if msg.time < 0:
                raise ValueError(tr('error.midi.negative'))
            tick += msg.time
            events.append((tick, ti, order, msg))
    events.sort(key=lambda e: e[:3])
    tempos = TempoMap([(t, m.tempo) for t, _, _, m in events
                       if m.type == 'set_tempo'], midi.ticks_per_beat)
    meters = MeterMap([(t, m.numerator, m.denominator) for t, _, _, m in events
                       if m.type == 'time_signature'], midi.ticks_per_beat)
    end_tick = max((e[0] for e in events), default=0)
    duration = tempos.seconds_at(end_tick)
    active = defaultdict(deque)
    sustained = defaultdict(list)
    pedal = defaultdict(bool)
    finished = []
    warnings = []
    serial = 0
    missing = 0

    def finish(item, tick):
        ident, start, track, channel, pitch, velocity = item
        st = tempos.seconds_at(start)
        finished.append(Note(ident, pitch, velocity, st,
                             max(st + .01, tempos.seconds_at(tick)),
                             start, tick, start / midi.ticks_per_beat,
                             meters.strength(start), f'{track}:{channel}', channel))

    for tick, ti, _, msg in events:
        if msg.type == 'note_on' and msg.velocity:
            active[(ti, msg.channel, msg.note)].append(
                (serial, tick, ti, msg.channel, msg.note, msg.velocity))
            serial += 1
        elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
            key = (ti, msg.channel, msg.note)
            if active[key]:
                item = active[key].popleft()  # explicit FIFO for overlapping same notes
                if pedal[msg.channel]:
                    sustained[msg.channel].append(item)
                else:
                    finish(item, tick)
        elif msg.type == 'control_change':
            if msg.control == 121:
                pedal[msg.channel] = False
                for item in sustained.pop(msg.channel, []):
                    finish(item, tick)
            if msg.control == 64:
                down = msg.value >= 64
                if pedal[msg.channel] and not down:
                    for item in sustained.pop(msg.channel, []):
                        finish(item, tick)
                pedal[msg.channel] = down
            elif msg.control in (120, 123):
                for key in list(active):
                    if key[1] == msg.channel:
                        while active[key]:
                            item = active[key].popleft()
                            if msg.control == 123 and pedal[msg.channel]:
                                sustained[msg.channel].append(item)
                            else:
                                finish(item, tick)
                if msg.control == 120:
                    for item in sustained.pop(msg.channel, []):
                        finish(item, tick)
    for queue in active.values():
        for item in queue:
            finish(item, end_tick)
            missing += 1
    for items in sustained.values():
        for item in items:
            finish(item, end_tick)
    if missing:
        warnings.append(tr('warning.note_off', count=missing))
    finished.sort(key=lambda n: (n.start, n.id))
    by_source = defaultdict(list)
    for note in finished:
        by_source[note.source].append(note)
    sources = []
    for sid, notes in by_source.items():
        ti, ch = map(int, sid.split(':'))
        sources.append(Source(sid, ti, ch, track_names[ti], len(notes),
                              min(n.pitch for n in notes), max(n.pitch for n in notes)))
    sources.sort(key=lambda s: (s.track, s.channel))
    return Song(str(Path(path)), finished, sources,
                max(duration, max((n.end for n in finished), default=0)),
                midi.ticks_per_beat, warnings,
                tuple((t, *v) for t, v in zip(meters.ticks, meters.values)),
                tuple(zip(tempos.ticks, tempos.tempos)))


SCORE_SUFFIXES = ('.mid', '.midi', '.musicxml', '.mxl', '.xml', '.txt')


def read_song(path) -> Song:
    """Any score the player opens: MIDI, MusicXML, or an ml3czus-autoplayer sheet (.txt)."""
    suffix = Path(path).suffix.lower()
    if suffix in ('.musicxml', '.mxl', '.xml'):
        from .musicxml import read_musicxml
        return read_musicxml(path)
    if suffix == '.txt':
        from .ahk_sheet import read_sheet
        return read_sheet(Path(path).read_text(encoding='utf-8-sig', errors='replace'), path)
    return read_midi(path)
