"""Windows built-in MIDI synthesizer preview, never injects keyboard input."""
from __future__ import annotations

import ctypes
import os
import threading
import time

from .export import reference_events
from .i18n import tr


class MidiPreview:
    def __init__(self):
        self.thread = None
        self.cancel = threading.Event()
        self.error = ''
        self._origin = None
        self._position = self._start = self._end = 0.

    @property
    def position(self):
        origin=self._origin
        return self._position if origin is None else min(self._end,max(self._start,time.perf_counter()-origin))

    @property
    def playing(self):
        return bool(self.thread and self.thread.is_alive())

    def stop(self):
        self._position=self.position
        self.cancel.set()
        if self.thread:
            self.thread.join(.5)

    def play(self, arrangement, start=0., duration=None, original=False):
        self.stop()
        if self.playing:
            raise RuntimeError(tr('error.preview.busy'))
        if os.name != 'nt':
            raise RuntimeError(tr('error.preview.windows'))
        self.error = ''
        start=max(0.,min(float(start),arrangement.duration))
        duration=arrangement.duration-start if duration is None else max(0.,float(duration))
        self._start=self._position=start
        self._end=min(arrangement.duration,start+duration)
        self._origin=None
        self.cancel.clear()
        self.thread = threading.Thread(target=self._run,
            args=(arrangement, start, duration, original), daemon=True, name='guitar-preview')
        self.thread.start()

    def _run(self, arrangement, start, duration, original):
        handle = ctypes.c_void_p()
        midi = ctypes.WinDLL('winmm')
        midi.midiOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint,
                                    ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint]
        midi.midiOutShortMsg.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        midi.midiOutReset.argtypes = [ctypes.c_void_p]
        midi.midiOutClose.argtypes = [ctypes.c_void_p]
        opened = False
        try:
            error = midi.midiOutOpen(ctypes.byref(handle), 0xFFFFFFFF, 0, 0, 0)
            if error:
                raise RuntimeError(tr('error.preview.open', code=error))
            opened = True
            for channel in range(16):
                if channel != 9:
                    midi.midiOutShortMsg(handle, (0xC0 | channel) | ((0 if original else 24) << 8))
            events=reference_events(arrangement,original)
            if self.cancel.is_set():return
            # Restore sustained notes when seeking into a phrase.
            sustained,remaining=events_from_position(events,start)
            for msg in sustained:
                result=midi.midiOutShortMsg(handle,sum(byte << (8*i) for i,byte in enumerate(msg.bytes())))
                if result:raise RuntimeError(tr('error.preview.send', code=result))
            origin = self._origin = time.perf_counter() - start
            for sec, _, msg in remaining:
                if sec >= start + duration:
                    break
                if self.cancel.wait(max(0, sec - (time.perf_counter() - origin))):
                    return
                data = msg.bytes()
                packed = sum(byte << (8*i) for i, byte in enumerate(data))
                result = midi.midiOutShortMsg(handle, packed)
                if result:
                    raise RuntimeError(tr('error.preview.send', code=result))
            self.cancel.wait(max(0, min(start+duration, arrangement.duration) -
                                 (time.perf_counter()-origin)))
        except Exception as exc:
            self.error = str(exc)
        finally:
            self._position=self.position
            self._origin=None
            if opened:
                midi.midiOutReset(handle)
                midi.midiOutClose(handle)


def events_from_position(events,start):
    """Return sounding notes to retrigger and future events, including exact-boundary offs."""
    active={};remaining=[]
    for sec,priority,msg in events:
        if sec>=start:
            if sec==start:active.pop((msg.channel,msg.note),None)
            remaining.append((sec,priority,msg));continue
        key=(msg.channel,msg.note)
        if msg.type=='note_on' and msg.velocity:active[key]=msg
        else:active.pop(key,None)
    return list(active.values()),remaining
