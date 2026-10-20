"""Listening preview with a sound pack (SFZ, or SF2 / SF3 via soundfont.py), and
the switch between timbres.

Same interface as preview.MidiPreview (play / stop / playing / position /
error), so the windows do not care which timbre is active. Audio goes out
through the Windows waveOut API via ctypes: no extra dependency, and the
device reports exactly how many samples it has played.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import threading

import mido
import numpy as np

from .export import reference_events
from .i18n import tr
from .preview import MidiPreview
from .sampler import RATE, Pack, PackError, Renderer, plan
from .soundfont import SoundFont, is_soundfont

ROOT = Path(__file__).resolve().parents[1]
GAME_PACK = ROOT/'data'/'game_audio'/'sfz'/'roblox_guitar.sfz'
BLOCK = 2048            # frames per waveOut buffer (~43 ms)
BUFFERS = 4
TAIL = 4.               # seconds the last notes may ring past the end of the piece
DEFAULT, CUSTOM = 'default', 'custom'
# Loudness matching: the same probe notes through every pack, compared with the game pack.
PROBE = (48, 55, 60, 64, 67, 72)      # one per string, low to high
PROBE_STEP, PROBE_HOLD = .6, .5
# RMS of the probe through a pack recorded from the game. A constant, because that pack
# is a game asset and is not distributed.
GAME_LOUDNESS = .0393
AUTO_RANGE = (-12., 18.)              # dB the automatic match may apply
VOLUME_RANGE = (-24., 24.)            # dB the user may set by hand
KNEE = .8                             # soft limiter: untouched below, eased towards 1 above


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [('wFormatTag', wintypes.WORD), ('nChannels', wintypes.WORD), ('nSamplesPerSec', wintypes.DWORD),
                ('nAvgBytesPerSec', wintypes.DWORD), ('nBlockAlign', wintypes.WORD),
                ('wBitsPerSample', wintypes.WORD), ('cbSize', wintypes.WORD)]


class WAVEHDR(ctypes.Structure):
    pass


WAVEHDR._fields_ = [('lpData', ctypes.c_void_p), ('dwBufferLength', wintypes.DWORD), ('dwBytesRecorded', wintypes.DWORD),
                    ('dwUser', ctypes.c_size_t), ('dwFlags', wintypes.DWORD), ('dwLoops', wintypes.DWORD),
                    ('lpNext', ctypes.POINTER(WAVEHDR)), ('reserved', ctypes.c_size_t)]


class MMTIME(ctypes.Structure):
    _fields_ = [('wType', wintypes.UINT), ('u', wintypes.DWORD), ('pad', wintypes.DWORD)]


WHDR_DONE, TIME_SAMPLES, WAVE_MAPPER = 1, 2, 0xFFFFFFFF


class WaveOut:
    """16-bit stereo PCM stream on the default output device."""

    def __init__(self, rate=RATE):
        if os.name != 'nt':
            raise RuntimeError(tr('error.audio.windows'))
        self.api = ctypes.WinDLL('winmm')
        self.handle = ctypes.c_void_p()
        fmt = WAVEFORMATEX(1, 2, rate, rate*4, 4, 16, 0)
        error = self.api.waveOutOpen(ctypes.byref(self.handle), WAVE_MAPPER, ctypes.byref(fmt), 0, 0, 0)
        if error:
            raise RuntimeError(tr('error.audio.open', code=error))
        self.headers = [WAVEHDR() for _ in range(BUFFERS)]
        self.buffers = [ctypes.create_string_buffer(BLOCK*4) for _ in range(BUFFERS)]
        self.queued = [False]*BUFFERS

    def free(self):
        """Indices of buffers that may be filled now."""
        out = []
        for i, header in enumerate(self.headers):
            if self.queued[i] and header.dwFlags & WHDR_DONE:
                self.api.waveOutUnprepareHeader(self.handle, ctypes.byref(header), ctypes.sizeof(header))
                self.queued[i] = False
            if not self.queued[i]:
                out.append(i)
        return out

    def write(self, i, frames):
        data = (np.clip(frames, -1, 1)*32767).astype('<i2').tobytes()
        ctypes.memmove(self.buffers[i], data, len(data))
        header = self.headers[i]
        header.lpData = ctypes.cast(self.buffers[i], ctypes.c_void_p)
        header.dwBufferLength = len(data)
        header.dwFlags = 0
        self.api.waveOutPrepareHeader(self.handle, ctypes.byref(header), ctypes.sizeof(header))
        error = self.api.waveOutWrite(self.handle, ctypes.byref(header), ctypes.sizeof(header))
        if error:
            raise RuntimeError(tr('error.audio.write', code=error))
        self.queued[i] = True

    def played(self):
        """Samples the device has actually played since opening."""
        t = MMTIME(TIME_SAMPLES, 0, 0)
        self.api.waveOutGetPosition(self.handle, ctypes.byref(t), ctypes.sizeof(t))
        return t.u

    def close(self):
        self.api.waveOutReset(self.handle)
        for i, header in enumerate(self.headers):
            if self.queued[i]:
                self.api.waveOutUnprepareHeader(self.handle, ctypes.byref(header), ctypes.sizeof(header))
        self.api.waveOutClose(self.handle)


_packs = {}
_packs_lock = threading.Lock()


def load_pack(path):
    """Packs are cached per file and modification time: samples load once."""
    path = Path(path)
    try:
        key = (path.resolve(), path.stat().st_mtime)
    except OSError as exc:
        raise PackError(tr('error.pack.missing', path=path)) from exc
    with _packs_lock:
        if key not in _packs:
            _packs[key] = SoundFont(path) if is_soundfont(path) else Pack(path)
        return _packs[key]


def probe_events():
    events = []
    for i, key in enumerate(PROBE):
        t = i*PROBE_STEP
        events += [(t, 1, mido.Message('note_on', channel=i % 6, note=key, velocity=100)),
                   (t+PROBE_HOLD, 0, mido.Message('note_off', channel=i % 6, note=key))]
    return events


_loudness = {}


def loudness(pack):
    """RMS of the probe notes (each note's first PROBE_HOLD s) through a pack and,
    for SF2 / SF3, its current instrument. Cached per pack file and instrument."""
    preset = getattr(pack, 'preset', None)
    key = (id(pack), preset)
    if key not in _loudness:
        length = int(len(PROBE)*PROBE_STEP*RATE)
        events = probe_events()
        renderer = pack.renderer(events, length) if isinstance(pack, SoundFont) else Renderer(plan(events, pack), length)
        audio = renderer.block(0, length)
        parts = [audio[int(i*PROBE_STEP*RATE):int((i*PROBE_STEP+PROBE_HOLD)*RATE)] for i in range(len(PROBE))]
        _loudness[key] = float(np.sqrt(np.mean([np.mean(p**2) for p in parts])))
    return _loudness[key]


def auto_gain_db(pack):
    """dB that brings a pack (instrument) to the game pack's loudness."""
    level = loudness(pack)
    if level <= 0:
        return AUTO_RANGE[1]
    return float(np.clip(20*np.log10(GAME_LOUDNESS/level), *AUTO_RANGE))


def limit(frames):
    """Soft limiter: samples under KNEE pass unchanged, louder ones bend towards 1."""
    over = np.abs(frames) > KNEE
    if over.any():
        x = frames[over]
        frames[over] = np.sign(x)*(KNEE+(1-KNEE)*np.tanh((np.abs(x)-KNEE)/(1-KNEE)))
    return frames


class SamplePreview:
    def __init__(self, pack_path, output=WaveOut):
        self.pack = load_pack(pack_path)
        self.output = output
        self.thread = None
        self.cancel = threading.Event()
        self.error = ''
        self._position = self._start = self._end = 0.
        self._played = None
        self._plans = {}
        self.gain = 1.            # linear; set_volume

    def set_volume(self, db=None):
        """Gain in dB relative to the pack as recorded; None = the automatic loudness match.
        Returns the automatic value, so the window can show it either way."""
        auto_db = auto_gain_db(self.pack)
        self.gain = 10**((auto_db if db is None else db)/20)
        return auto_db

    @property
    def position(self):
        played = self._played
        if played is None:
            return self._position
        return min(self._end, self._start+played()/RATE)

    @property
    def playing(self):
        return bool(self.thread and self.thread.is_alive())

    def stop(self):
        self._position = self.position
        self.cancel.set()
        if self.thread:
            self.thread.join(1.)

    def renderer(self, arrangement, original):
        key = (id(arrangement), original)
        if key not in self._plans:
            events, length = reference_events(arrangement, original), int((arrangement.duration+TAIL)*RATE)
            if isinstance(self.pack, SoundFont):
                self._plans = {key: self.pack.renderer(events, length)}
            else:
                self._plans = {key: Renderer(plan(events, self.pack), length)}
        return self._plans[key]

    def set_preset(self, preset):
        """SF2 / SF3: the instrument (bank, preset) to play; ignored for SFZ."""
        if isinstance(self.pack, SoundFont):
            self.stop()
            self.pack.select(preset)
            self._plans = {}

    def play(self, arrangement, start=0., duration=None, original=False):
        self.stop()
        if self.playing:
            raise RuntimeError(tr('error.preview.busy'))
        self.error = ''
        start = max(0., min(float(start), arrangement.duration))
        self._start = self._position = start
        self._end = arrangement.duration if duration is None else min(arrangement.duration, start+duration)
        renderer = self.renderer(arrangement, original)
        self.cancel.clear()
        self.thread = threading.Thread(target=self._run, args=(renderer,), daemon=True, name='sample-preview')
        self.thread.start()

    def _run(self, renderer):
        out = None
        try:
            out = self.output()
            frame = int(self._start*RATE)
            end = int(self._end*RATE)
            self._played = out.played
            while not self.cancel.is_set():
                if frame < end:
                    for i in out.free():
                        out.write(i, limit(renderer.block(frame, BLOCK)*self.gain))
                        frame += BLOCK
                        if frame >= end:
                            break
                elif out.played() >= end-int(self._start*RATE):
                    break
                self.cancel.wait(.005)
        except Exception as exc:
            self.error = str(exc)
        finally:
            self._position = self.position
            self._played = None
            if out is not None:
                out.close()


class TimbrePreview:
    """The listening preview the windows use; swaps between the Windows MIDI
    synthesizer (default) and a sample pack (custom) without them noticing."""

    def __init__(self, kind=DEFAULT, pack_path=None, midi=None, sample_factory=SamplePreview):
        self.midi = midi or MidiPreview()
        self.sample_factory = sample_factory
        self.kind, self.pack_path = DEFAULT, None
        self.current = self.midi
        if kind == CUSTOM and pack_path:
            self.set_timbre(CUSTOM, pack_path)

    def set_timbre(self, kind, pack_path=None):
        """Raises PackError if the pack cannot be used; then nothing changes."""
        if kind == CUSTOM:
            backend = self.sample_factory(pack_path)
        else:
            backend = self.midi
        self.current.stop()
        backend._position = self.current.position
        self.current, self.kind = backend, kind
        self.pack_path = Path(pack_path) if kind == CUSTOM else None

    def set_preset(self, preset):
        if self.kind == CUSTOM and hasattr(self.current, 'set_preset'):
            self.current.set_preset(preset)

    def set_volume(self, db=None):
        """Custom timbre only; returns the automatic value in dB, or None."""
        if self.kind == CUSTOM and hasattr(self.current, 'set_volume'):
            return self.current.set_volume(db)
        return None

    @property
    def soundfont(self):
        """The SF2 / SF3 bank in use, or None (default timbre or an SFZ pack)."""
        pack = getattr(self.current, 'pack', None) if self.kind == CUSTOM else None
        return pack if isinstance(pack, SoundFont) else None

    # The preview interface, forwarded.
    @property
    def position(self):
        return self.current.position

    @property
    def playing(self):
        return self.current.playing

    @property
    def error(self):
        return self.current.error

    def play(self, *args, **kwargs):
        return self.current.play(*args, **kwargs)

    def stop(self):
        return self.current.stop()

