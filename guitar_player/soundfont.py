"""SF2 / SF3 sound banks for the listening preview, through tinysoundfont (MIT,
a self-contained wheel of the TinySoundFont synthesizer).

The bank plays as the instrument it is: note-offs at the arranged note lengths
(export.reference_events), the MIDI velocity, the bank's own loops and
envelopes. Only the game's own pack (SFZ, sampler.py) imitates the game.

SoundFontRenderer offers the same block(begin, frames) as sampler.Renderer, so
sample_preview.SamplePreview plays either. The synthesizer is stateful: reading
blocks in order just runs it on; any other start resets it and presses again
the notes that would be sounding there (like preview.MidiPreview does).
"""
from __future__ import annotations

from bisect import bisect_left
from pathlib import Path
import threading

import numpy as np

from .i18n import tr
from .preview import events_from_position
from .sampler import RATE, PackError

SUFFIXES = ('.sf2', '.sf3')
GAIN_DB = -6.          # headroom: several strings at full velocity stay below clipping
SETTLE = 480           # frames a reset needs before the stopped voices are silent (measured ~5 ms)
CHANNELS = 16
GUITARS = range(24, 32)   # General MIDI programs 25-32: the guitars


def is_soundfont(path):
    return Path(path).suffix.lower() in SUFFIXES


class SoundFont:
    """One loaded bank and the preset the preview uses on every channel."""

    def __init__(self, path):
        try:
            import tinysoundfont
        except ImportError as exc:
            raise PackError(tr('error.pack.tinysoundfont')) from exc
        self.path = Path(path)
        if not self.path.is_file():
            raise PackError(tr('error.pack.missing', path=self.path))
        self.lock = threading.Lock()
        self.owner = None         # the renderer whose notes the synthesizer holds now
        self.synth = tinysoundfont.Synth(gain=GAIN_DB, samplerate=RATE)
        try:
            self.sfid = self.synth.sfload(str(self.path))
        except Exception as exc:
            raise PackError(tr('error.pack.read', error=exc)) from exc
        self.presets = []         # [(bank, preset, name)]
        for bank in range(129):
            for preset in range(128):
                name = self.synth.sfpreset_name(self.sfid, bank, preset)
                if name is not None:
                    self.presets.append((bank, preset, name))
        if not self.presets:
            raise PackError(tr('error.pack.no_preset'))
        self.preset = self.default_preset()

    def default_preset(self):
        """A General MIDI guitar from bank 0 if the bank has one, else its first preset."""
        guitars = [(b, p) for b, p, _ in self.presets if b == 0 and p in GUITARS]
        return guitars[0] if guitars else self.presets[0][:2]

    def select(self, preset):
        """preset: (bank, number); unknown ones fall back to the default."""
        known = {(b, p) for b, p, _ in self.presets}
        self.preset = tuple(preset) if preset and tuple(preset) in known else self.default_preset()

    @property
    def preset_name(self):
        return next(n for b, p, n in self.presets if (b, p) == self.preset)

    def reset(self):
        """Silence everything and put the chosen preset on every channel."""
        synth = self.synth
        synth.sounds_off()
        synth.generate(SETTLE)
        for channel in range(CHANNELS):
            synth.program_select(channel, self.sfid, *self.preset)
        return synth

    def renderer(self, events, length):
        return SoundFontRenderer(self, events, length)


class SoundFontRenderer:
    def __init__(self, font, events, length):
        self.font, self.length = font, length
        self.events = sorted(events, key=lambda e: (e[0], e[1]))
        self.secs = [sec for sec, _, _ in self.events]
        self.times = [int(round(sec*RATE)) for sec in self.secs]
        self.cursor, self.next = None, 0

    def _send(self, msg):
        if msg.type == 'note_on' and msg.velocity:
            self.font.synth.noteon(msg.channel, msg.note, msg.velocity)
        elif msg.type in ('note_on', 'note_off'):
            self.font.synth.noteoff(msg.channel, msg.note)

    def _seek(self, frame):
        self.font.reset()
        self.font.owner = self
        sustained, _ = events_from_position(self.events, frame/RATE)
        for msg in sustained:
            self._send(msg)
        self.next = bisect_left(self.secs, frame/RATE)    # the same split as events_from_position
        self.cursor = frame

    def block(self, begin, frames):
        with self.font.lock:
            if self.cursor != begin or self.font.owner is not self:
                self._seek(begin)            # another renderer (or a loudness probe) used the synth
            out = np.zeros((frames, 2), np.float32)
            done = 0
            while done < frames:
                while self.next < len(self.events) and self.times[self.next] <= self.cursor:
                    self._send(self.events[self.next][2])
                    self.next += 1
                step = frames-done
                if self.next < len(self.events):
                    step = min(step, self.times[self.next]-self.cursor)
                data = np.frombuffer(self.font.synth.generate(step), np.float32).reshape(-1, 2)
                out[done:done+step] = data
                done += step
                self.cursor += step
            return out
