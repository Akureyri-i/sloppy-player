"""A small SFZ sample player for the listening preview (no audio device here).

Supported SFZ subset, enough for the recorded game pack and simple user packs:
headers <control> <global> <group> <region>; opcodes sample, default_path,
key, lokey, hikey, pitch_keycenter, lochan, hichan, lovel, hivel, volume,
amp_veltrack, loop_mode (one_shot honours no note-off), ampeg_release, group,
off_by, transpose, tune. Unknown opcodes are ignored. Keys with no region are
played by pitch-shifting the nearest region, so a sparse pack still covers
every note.

Rendering is planned once (which sample, when, until when) and then mixed in
blocks on demand, so seeking and long pieces cost nothing up front.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import re
import struct

import numpy as np

from .i18n import tr

RATE = 48000
NOTE_NAMES = {'c': 0, 'd': 2, 'e': 4, 'f': 5, 'g': 7, 'a': 9, 'b': 11}
RELEASE_FADE = .006     # seconds; a cut voice (off_by) fades this fast


class PackError(ValueError):
    """The sound pack cannot be read; the message is shown to the user."""


def parse_key(value):
    """'60', 'c4', 'C#4', 'db3' -> MIDI number (c4 = 60)."""
    value = value.strip().lower()
    if re.fullmatch(r'-?\d+', value):
        return int(value)
    match = re.fullmatch(r'([a-g])([#b]?)(-?\d+)', value)
    if not match:
        raise PackError(tr('error.pack.note_name', name=value))
    name, accidental, octave = match.groups()
    return NOTE_NAMES[name]+{'#': 1, 'b': -1, '': 0}[accidental]+12*(int(octave)+1)


@dataclass
class Region:
    sample: Path
    lokey: int = 0
    hikey: int = 127
    keycenter: int = 60
    lochan: int = 1
    hichan: int = 16
    lovel: int = 1
    hivel: int = 127
    volume: float = 0.            # dB
    amp_veltrack: float = 100.    # %
    one_shot: bool = False
    release: float = .03          # s, applied at note-off unless one-shot
    group: int = 0
    off_by: int = 0
    tune: float = 0.              # cents, transpose folded in

    def matches(self, channel, key, velocity, any_channel=False):
        return (self.lokey <= key <= self.hikey and self.lovel <= velocity <= self.hivel
                and (any_channel or self.lochan <= channel+1 <= self.hichan))


def _opcodes(text):
    """Yield (header, {opcode: value}) in file order. Values may contain spaces for sample=."""
    text = re.sub(r'//[^\n]*', '', text)
    for match in re.finditer(r'<(\w+)>([^<]*)', text):
        header, body = match.group(1).lower(), match.group(2)
        values = {}
        # sample= runs until the next opcode, so paths with spaces survive.
        for m in re.finditer(r'(\w+)=(.*?)(?=\s+\w+=|\s*$)', body.strip(), re.S):
            values[m.group(1).lower()] = m.group(2).strip()
        yield header, values


def parse_sfz(path):
    path = Path(path)
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as exc:
        raise PackError(tr('error.pack.read', error=exc)) from exc
    default_path, global_, group, regions = '', {}, {}, []
    for header, values in _opcodes(text):
        if header == 'control':
            default_path = values.get('default_path', default_path)
        elif header == 'global':
            global_, group = values, {}
        elif header in ('group', 'master'):
            group = values
        elif header == 'region':
            opcodes = {**global_, **group, **values}
            if 'sample' not in opcodes:
                continue
            regions.append(_region(path.parent, default_path, opcodes))
    if not regions:
        raise PackError(tr('error.pack.no_region'))
    return regions


def _region(folder, default_path, op):
    sample = (folder/default_path/op['sample'].replace('\\', '/')).resolve()
    region = Region(sample=sample)
    if 'key' in op:
        k = parse_key(op['key'])
        region.lokey = region.hikey = region.keycenter = k
    if 'lokey' in op:
        region.lokey = parse_key(op['lokey'])
    if 'hikey' in op:
        region.hikey = parse_key(op['hikey'])
    if 'pitch_keycenter' in op:
        region.keycenter = parse_key(op['pitch_keycenter'])
    for name in ('lochan', 'hichan', 'lovel', 'hivel', 'group', 'off_by'):
        if name in op:
            setattr(region, name, int(float(op[name])))
    for name, attr in (('volume', 'volume'), ('amp_veltrack', 'amp_veltrack'), ('ampeg_release', 'release')):
        if name in op:
            setattr(region, attr, float(op[name]))
    region.one_shot = op.get('loop_mode', '') == 'one_shot'
    region.tune = float(op.get('tune', 0))+100*float(op.get('transpose', 0))
    return region


def read_wav(path):
    """(rate, float32 [frames, channels]) of a PCM (8/16/24/32-bit) or float WAV file."""
    raw = Path(path).read_bytes()
    if raw[:4] != b'RIFF' or raw[8:12] != b'WAVE':
        raise ValueError('not a WAV file')
    chunks, pos = {}, 12
    while pos+8 <= len(raw):
        size = int.from_bytes(raw[pos+4:pos+8], 'little')
        chunks.setdefault(raw[pos:pos+4], raw[pos+8:pos+8+size])
        pos += 8+size+(size & 1)
    if b'fmt ' not in chunks or b'data' not in chunks:
        raise ValueError('no fmt or data chunk')
    fmt, data = chunks[b'fmt '], chunks[b'data']
    kind, channels, rate = struct.unpack('<HHI', fmt[:8])
    width = struct.unpack('<H', fmt[14:16])[0]//8
    if kind == 0xFFFE:                      # WAVE_FORMAT_EXTENSIBLE: the sub-format's tag
        kind = struct.unpack('<H', fmt[24:26])[0]
    if (kind, width) not in ((1, 1), (1, 2), (1, 3), (1, 4), (3, 4), (3, 8)):
        raise ValueError(f'unsupported WAV format {kind}, {8*width}-bit')
    data = data[:len(data)//(width*channels)*width*channels]
    if kind == 3:
        samples = np.frombuffer(data, f'<f{width}').astype(np.float32)
    elif width == 1:
        samples = (np.frombuffer(data, np.uint8).astype(np.float32)-128)/128
    elif width == 3:
        b = np.frombuffer(data, np.uint8).reshape(-1, 3).astype(np.uint32)
        samples = ((b[:, 0] << 8) | (b[:, 1] << 16) | (b[:, 2] << 24)).view(np.int32).astype(np.float32)/(2**31-1)
    else:
        samples = np.frombuffer(data, f'<i{width}').astype(np.float32)/(2**(8*width-1)-1)
    return rate, samples.reshape(-1, channels)


def load_wav(path, rate=RATE):
    """Stereo float32 at `rate`."""
    try:
        source_rate, data = read_wav(path)
    except (OSError, ValueError) as exc:
        raise PackError(tr('error.pack.sample', name=Path(path).name, error=exc)) from exc
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    data = data[:, :2]
    if source_rate != rate:
        data = stretch(data, source_rate/rate)
    return data


def shift(data, semitones):
    """Pitch-shift by resampling (changes length, like a classic sampler)."""
    if abs(semitones) < 1e-6:
        return data
    return stretch(data, 2**(semitones/12))


def stretch(data, ratio):
    """Read `data` at `ratio` source frames per output frame, linearly interpolated;
    len(data)/ratio frames, the last frame held past the end."""
    positions = np.arange(round(len(data)/ratio))*ratio
    index = positions.astype(int)
    frac = (positions-index)[:, None].astype(np.float32)
    data = np.concatenate([data, data[-1:]])
    return (data[index]*(1-frac)+data[index+1]*frac).astype(np.float32)


class Pack:
    """Regions plus their audio, loaded lazily and cached per (sample, shift)."""

    def __init__(self, path, rate=RATE):
        self.path, self.rate = Path(path), rate
        self.regions = parse_sfz(self.path)
        self._audio = {}
        missing = [r.sample.name for r in self.regions if not r.sample.is_file()]
        if missing:
            raise PackError(tr('error.pack.samples_missing', count=len(missing), example=missing[0]))

    def region_for(self, channel, key, velocity):
        """(region, semitone shift) or (None, 0)."""
        for any_channel in (False, True):
            for r in self.regions:
                if r.matches(channel, key, velocity, any_channel):
                    return r, key-r.keycenter+r.tune/100
        nearest = min(self.regions, key=lambda r: min(abs(key-r.lokey), abs(key-r.hikey)))
        return nearest, key-nearest.keycenter+nearest.tune/100

    def audio(self, region, semitones):
        key = (region.sample, round(semitones, 3))
        if key not in self._audio:
            base = self._audio.get((region.sample, 0.)) if semitones else None
            if base is None:
                base = load_wav(region.sample, self.rate)
                self._audio[(region.sample, 0.)] = base
            self._audio[key] = shift(base, semitones)
        return self._audio[key]


@dataclass
class Voice:
    start: int            # sample index in the piece
    stop: int             # where it ends (sample end, release or cut)
    fade: int             # samples of fade-out ending at `stop` (0 = natural end)
    audio: np.ndarray = field(repr=False)
    gain: float = 1.
    region: Region | None = field(default=None, repr=False)


def plan(events, pack, rate=RATE):
    """events: (seconds, priority, mido message) as from export.reference_events.
    Returns voices sorted by start, with one-shot, release and off_by applied."""
    voices, held, chokable = [], {}, defaultdict(list)   # off_by -> voices it can stop
    for seconds, _, msg in sorted(events, key=lambda e: (e[0], e[1])):
        at = int(round(seconds*rate))
        if msg.type == 'note_on' and msg.velocity > 0:
            region, semitones = pack.region_for(msg.channel, msg.note, msg.velocity)
            if region is None:
                continue
            if region.group:
                # Choke: a note in group G stops sounding voices with off_by == G.
                still = []
                for v in chokable.get(region.group, ()):
                    if v.start <= at < v.stop:
                        fade = int(RELEASE_FADE*rate)
                        v.stop, v.fade = min(v.stop, at+fade), min(fade, max(1, at+fade-v.start))
                    elif at < v.start:
                        still.append(v)
                chokable[region.group] = still
            audio = pack.audio(region, semitones)
            veltrack = region.amp_veltrack/100
            gain = 10**(region.volume/20)*((msg.velocity/127)**2*veltrack+(1-veltrack))
            voice = Voice(at, at+len(audio), 0, audio, gain, region)
            voices.append(voice)
            if region.off_by:
                chokable[region.off_by].append(voice)
            held.setdefault((msg.channel, msg.note), []).append(voice)
        elif msg.type in ('note_off', 'note_on'):
            for voice in held.pop((msg.channel, msg.note), []):
                if voice.region.one_shot:
                    continue
                fade = int(voice.region.release*rate)
                if at+fade < voice.stop:
                    voice.stop, voice.fade = at+fade, fade
    voices.sort(key=lambda v: v.start)
    return voices


class Renderer:
    """Mixes planned voices into any block of the piece."""

    def __init__(self, voices, length, rate=RATE):
        self.voices, self.rate = voices, rate
        self.length = length
        self.starts = [v.start for v in voices]
        # Longest voice bounds how far back a block must look for voices still sounding.
        self.reach = max((v.stop-v.start for v in voices), default=0)

    def block(self, begin, frames):
        out = np.zeros((frames, 2), np.float32)
        end = begin+frames
        first = bisect_right(self.starts, begin-self.reach-1)
        last = bisect_right(self.starts, end)
        for v in self.voices[first:last]:
            if v.stop <= begin or v.start >= end:
                continue
            a, b = max(begin, v.start), min(end, v.stop)
            chunk = v.audio[a-v.start:b-v.start]*v.gain
            if v.fade:
                fade_start = v.stop-v.fade
                if b > fade_start:
                    ramp = np.clip((v.stop-np.arange(a, b))/v.fade, 0, 1).astype(np.float32)
                    chunk = chunk*ramp[:, None]
            out[a-begin:b-begin] += chunk
        np.clip(out, -1, 1, out=out)
        return out
