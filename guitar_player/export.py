"""Exports of an arrangement (MIDI, and through export() MusicXML and ml3czus sheets),
the inspectable analysis report, and the reference sound events for listening."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

import mido

from .i18n import tr
from .model import FRET_KEYS, STRING_KEYS


def write_report(arrangement, path, detailed=False):
    report = arrangement.report()
    report['config'] = asdict(arrangement.config)
    if detailed:
        report['strikes'] = [dict(time=s.time, intended=s.intended, hold=s.hold,
            notes=[dict(source_id=f.voice.note.id, original=f.voice.note.pitch,
                        pitch=f.voice.pitch, role=f.voice.role, string=f.string,
                        fret=f.fret, keys=[FRET_KEYS[f.fret], STRING_KEYS[f.string]])
                   for f in s.notes]) for s in arrangement.strikes]
        report['dropped'] = arrangement.dropped
    with open(path, 'w', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)


def reference_events(arrangement, original=False):
    """(seconds, priority, message), with note-offs before retriggers.

    Output durations are a reference sound envelope, not a claim about the
    Roblox instrument's decay. Different pitches may ring on the same string;
    only a retrigger of the same channel/pitch clips its reference envelope.
    """
    events = []
    if original:
        for note in arrangement.selected:
            channel = note.channel
            events.append((note.start / arrangement.config.speed, 1,
                mido.Message('note_on', channel=channel, note=note.pitch, velocity=note.velocity)))
            events.append((note.end / arrangement.config.speed, 0,
                mido.Message('note_off', channel=channel, note=note.pitch)))
    else:
        by_string = [[] for _ in range(6)]
        for strike in arrangement.strikes:
            for f in strike.notes:
                by_string[f.string].append((strike.time, f))
        for channel, ns in enumerate(by_string):
            next_pitch = {}
            for start, finger in reversed(ns):
                end = min(arrangement.duration,
                          start + min(finger.voice.note.duration / arrangement.config.speed, 3.))
                if finger.voice.pitch in next_pitch:
                    end = min(end, next_pitch[finger.voice.pitch])
                next_pitch[finger.voice.pitch] = start
                end = max(end, start + .001)
                events.append((start, 1, mido.Message('note_on', channel=channel,
                    note=finger.voice.pitch, velocity=finger.voice.note.velocity)))
                events.append((end, 0, mido.Message('note_off', channel=channel,
                    note=finger.voice.pitch)))
    return sorted(events, key=lambda e: (e[0], e[1]))


def _track(events):
    """[(tick, message)] -> MidiTrack with delta times; note-offs before note-ons at a tick."""
    track, tick = mido.MidiTrack(), 0
    for at, msg in sorted(events, key=lambda e: (e[0], e[1].type == 'note_on')):
        track.append(msg.copy(time=at-tick))
        tick = at
    return track


def write_midi(arrangement, path):
    """The arrangement on the score's beats: its bars, its tempo times the playing speed, the
    pitches played in the game, one track and channel per string (nylon guitar). The key
    timing (chords pressed in turns, delayed onsets, the 吞音 guard) is the performance's,
    not the score's, and is left out."""
    a = arrangement
    song = a.song
    midi = mido.MidiFile(type=1, ticks_per_beat=song.ticks_per_beat, charset='utf-8')
    conductor = [(0, mido.MetaMessage('track_name', name=Path(song.path).stem))]
    conductor += [(t, mido.MetaMessage('set_tempo', tempo=round(us/a.config.speed))) for t, us in song.tempos]
    conductor += [(t, mido.MetaMessage('time_signature', numerator=n, denominator=d)) for t, n, d in song.meters]
    midi.tracks.append(_track(conductor))
    by_string = [[] for _ in range(6)]
    for strike in a.strikes:
        for f in strike.notes:
            by_string[f.string].append((f.voice.note.tick, f.voice.note.end_tick, f.voice.pitch, f.voice.note.velocity))
    for string, notes in enumerate(by_string):
        events = [(0, mido.MetaMessage('track_name', name=tr('export.string', number=6-string))),
                  (0, mido.Message('program_change', channel=string, program=24))]
        next_start = {}
        for start, end, pitch, velocity in sorted(notes, reverse=True):
            # A repeated pitch on this channel ends the previous one.
            end = max(start+1, min(end, next_start.get(pitch, end)))
            next_start[pitch] = start
            events.append((start, mido.Message('note_on', channel=string, note=pitch, velocity=velocity)))
            events.append((end, mido.Message('note_off', channel=string, note=pitch)))
        midi.tracks.append(_track(events))
    midi.save(path)


FORMATS = {'midi': '.mid', 'musicxml': '.musicxml', 'sheet': '.txt'}


def export(arrangement, path):
    """Write the arrangement in the format of path's extension (FORMATS)."""
    suffix = Path(path).suffix.lower()
    if suffix in ('.musicxml', '.xml', '.mxl'):
        from .musicxml import write_musicxml
        write_musicxml(arrangement, path)
    elif suffix == '.txt':
        from .ahk_sheet import write_sheet
        Path(path).write_text(write_sheet(arrangement)[0], encoding='utf-8')
    else:
        write_midi(arrangement, path)
