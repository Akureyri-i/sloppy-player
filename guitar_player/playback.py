"""Interruptible monotonic transport; all keyboard operations stay on one worker."""
from __future__ import annotations

import ctypes
import heapq
import os
import threading
import time
from dataclasses import dataclass

from .i18n import tr
from .model import Arrangement, FRET_KEYS, STRING_KEYS
from .timing import PlaybackTimer


def roblox_focused():
    if os.name != 'nt':
        return True
    from ctypes import wintypes
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(user.GetForegroundWindow(), ctypes.byref(pid))
    handle = kernel.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return False
    try:
        size = wintypes.DWORD(32768)
        buf = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return False
        return os.path.basename(buf.value).lower() in ('robloxplayerbeta.exe', 'robloxplayer.exe',
                                                     'robloxstudiobeta.exe')
    finally:
        kernel.CloseHandle(handle)


# Set-1 scan codes of the main-keyboard keys the game uses (physical positions).
# Sending key names is unsafe: the keyboard library resolves '-' to the numpad
# minus (scan code 74) first, which Roblox does not treat as the '-' fret key.
SCAN_CODES = {'e': 18, 'r': 19, 't': 20, 'y': 21, 'u': 22, 'p': 25, '-': 12, '=': 13, '[': 26, ']': 27,
              'f': 33, 'g': 34, 'h': 35, 'j': 36, 'k': 37, 'l': 38, ';': 39, "'": 40, 'm': 50, ',': 51,
              'z': 44, 'x': 45, 'c': 46, 'v': 47, 'b': 48, 'n': 49}


class ScanCodeKeys:
    """Backend adapter: press/release game keys by main-keyboard scan code."""

    def __init__(self, keyboard_module=None):
        if keyboard_module is None:
            import keyboard as keyboard_module
        self.keyboard = keyboard_module

    def press(self, key):
        self.keyboard.press(SCAN_CODES[key])

    def release(self, key):
        self.keyboard.release(SCAN_CODES[key])


class SendInputKeys:
    """Backend adapter: game keys in batches, one Windows SendInput call per batch, so
    the keys due at one moment reach the game together and in order (each key ~1 ms
    apart through ScanCodeKeys). Each event is the one
    ScanCodeKeys sends: the scan code with its virtual key, no extra flags."""

    def __init__(self):
        from ctypes import wintypes

        class KeyInput(ctypes.Structure):
            _fields_ = [('vk', wintypes.WORD), ('scan', wintypes.WORD), ('flags', wintypes.DWORD),
                        ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]

        class MouseInput(ctypes.Structure):          # the largest member: sets the size of INPUT
            _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('data', wintypes.DWORD),
                        ('flags', wintypes.DWORD), ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]

        class Union(ctypes.Union):
            _fields_ = [('key', KeyInput), ('mouse', MouseInput)]

        class Input(ctypes.Structure):
            _fields_ = [('type', wintypes.DWORD), ('u', Union)]

        self.Input, self.KeyInput = Input, KeyInput
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT
        self.vk = {key: self.user32.MapVirtualKeyW(code, 1) for key, code in SCAN_CODES.items()}   # MAPVK_VSC_TO_VK

    def send(self, events):
        """events: [(key, down)], sent in this order by one call."""
        inputs = (self.Input*len(events))()
        for item, (key, down) in zip(inputs, events):
            item.type = 1                                                            # INPUT_KEYBOARD
            item.u.key = self.KeyInput(self.vk[key], SCAN_CODES[key], 0 if down else 2, 0, 0)    # 2: KEYUP
        if self.user32.SendInput(len(events), inputs, ctypes.sizeof(self.Input)) != len(events):
            raise ctypes.WinError(ctypes.get_last_error())

    def press(self, key):
        self.send([(key, True)])

    def release(self, key):
        self.send([(key, False)])


class KeyboardDriver:
    """Key presses for the player.

    batch: (a backend with send(), SendInputKeys) presses and releases wait in `pending`
    until flush(); the player flushes whenever it is about to wait, so every key due at one
    moment goes out in one call. In game it sounds right but is not faster (each key event
    still costs Windows ~0.3-0.6 ms).
    late_fret_release: a fret selection is only the key-down; the player lifts the fret key
    with the string key, a hold later, so a note costs two key events at its moment instead
    of three. A fret key still down from an earlier note is lifted first, so every note
    still sends a complete fret press."""

    def __init__(self, backend=None, batch=False, late_fret_release=False):
        if backend is None:
            backend = ScanCodeKeys()
        self.backend = backend
        self.held = set()
        self.current_fret = None
        self.pending = [] if batch else None
        self.late_fret_release = late_fret_release

    def select_fret(self, key):
        # Send a complete selection for EVERY note, even on the same fret.
        # A key-up completes the tap; it does not cancel the latched fret.
        if self.late_fret_release:
            self.release(key)
            self.press(key)
        else:
            self.press(key)
            self.release(key)
        self.current_fret = key

    def _send(self, key, down):
        if self.pending is None:
            (self.backend.press if down else self.backend.release)(key)
        else:
            self.pending.append((key, down))

    def flush(self):
        if self.pending:
            events, self.pending = self.pending, []
            self.backend.send(events)

    def press(self, key):
        if key not in self.held:
            # Track before the API call so partial injection failures are released.
            self.held.add(key)
            self._send(key, True)

    def release(self, key):
        if key in self.held:
            self._send(key, False)
            self.held.discard(key)

    def release_all(self):
        self.current_fret = None
        errors = []
        for key in sorted(self.held, key=lambda key: (key not in STRING_KEYS, key)):
            try:
                self.release(key)
            except Exception as exc:
                errors.append(exc)
        try:
            self.flush()
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise RuntimeError(tr('error.release', error=errors[0]))


@dataclass(frozen=True)
class TransportState:
    status: str
    position: float
    duration: float
    skipped: int
    error: str


class Player:
    def __init__(self, driver=None, focus_check=roblox_focused, clock=time.perf_counter):
        self.driver = driver
        self.focus_check = focus_check
        self.clock = clock
        self.thread = None
        self.condition = threading.Condition()
        self._stop = False
        self._pause = False
        self._status = 'stopped'
        self._position = 0.
        self._duration = 0.
        self._skipped = 0
        self._error = ''
        self._origin = 0.
        self._focus_guard = True

    @property
    def state(self):
        with self.condition:
            return TransportState(self._status, self._position, self._duration,
                                  self._skipped, self._error)

    def play(self, arrangement: Arrangement, start=0., end=None, countdown=3., focus_guard=True):
        with self.condition:
            if self.thread and self.thread.is_alive():
                return False
            if not arrangement.strikes:
                return False
            self._stop = self._pause = False
            self._skipped = 0
            self._error = ''
            self._position = start
            self._duration = arrangement.duration
            self._status = 'countdown' if countdown else 'playing'
            self._focus_guard = focus_guard
            self.thread = threading.Thread(target=self._run,
                args=(arrangement, start, end, countdown), daemon=True, name='guitar-playback')
            self.thread.start()
            return True

    def pause_resume(self):
        with self.condition:
            if not self.thread or not self.thread.is_alive():
                return
            self._pause = not self._pause
            self.condition.notify_all()

    def stop(self, timeout=.5):
        with self.condition:
            self._stop = True
            self._pause = False
            self.condition.notify_all()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout)
        return not (self.thread and self.thread.is_alive())

    def _wait(self, target, active=False):
        """Return false when interrupted. A partial strike is never resumed.

        The wait itself is time.sleep in steps of at most 5 ms (stop and pause are seen
        within that). On Windows time.sleep uses a high-resolution waitable timer, which
        stays precise when Windows coarsens the timer of a process whose window is hidden
        (the player behind Roblox): a lock or event wait then wakes only every 15.6 ms."""
        while True:
            with self.condition:
                if self._stop:
                    return False
                now = self.clock()
                self._position = max(0., now - self._origin)
                if self._pause:
                    self.driver.release_all()
                    paused = now
                    self._status = 'paused'
                    while self._pause and not self._stop:
                        self.condition.wait(.05)
                    self._origin += self.clock() - paused
                    if self._stop:
                        return False
                    self._status = 'playing'
                    if active:
                        return False
                    continue
                left = self._origin + target - now
                if left <= 0:
                    return True
            self.driver.flush()          # everything due before this wait goes out together
            time.sleep(min(.005, left))

    def _ensure_focus(self):
        if not self._focus_guard or self.focus_check():
            return True
        with self.condition:
            self._pause = True
            self._status = 'paused'
        return False

    def _run(self, arrangement, start, end, countdown):
        try:
            with PlaybackTimer():
                self._play_events(arrangement, start, end, countdown)
        except Exception as exc:
            with self.condition:
                self._error = str(exc)
                self._status = 'error'
        finally:
            if self.driver:
                try:
                    self.driver.release_all()
                except Exception as exc:
                    with self.condition:
                        self._error = str(exc)
                        self._status = 'error'
            with self.condition:
                if self._stop and self._status != 'error':
                    self._status = 'stopped'

    def _play_events(self, arrangement, start, end, countdown):
        if self.driver is None:
            self.driver = KeyboardDriver()
        self.driver.current_fret = None
        self._origin = self.clock() - start + countdown
        limit = min(arrangement.duration, end) if end is not None else arrangement.duration
        queue = []
        for strike in arrangement.strikes:
            if start <= strike.time < limit:
                for finger in strike.notes:
                    queue.append((strike.time, len(queue), strike, finger))
        heapq.heapify(queue)
        pending = []
        release_due, gap_until, pitch_until = {}, {}, {}
        serial = 0
        while (queue or pending) and not self._stop:
            target = float('inf')
            if queue:
                scheduled, ident, strike, finger = queue[0]
                key = STRING_KEYS[finger.string]
                target = max(scheduled,
                    release_due.get(key, -1)+arrangement.config.gap,
                    gap_until.get(key, -1), pitch_until.get(finger.voice.pitch, -1))
                # A busy string must not block independent notes on other strings.
                if target > scheduled + 1e-9:
                    heapq.heapreplace(queue, (target, ident, strike, finger))
                    continue
            releasing = bool(pending and pending[0][0] <= target)
            if releasing:
                target = pending[0][0]
            if not self._wait(target, active=True):
                if self._stop:
                    break
                pending.clear(); release_due.clear()
                now = self.clock()-self._origin
                gap_until = {k: now+arrangement.config.gap for k in STRING_KEYS}
                continue
            if releasing:
                _, _, key = heapq.heappop(pending)
                self.driver.release(key)
                release_due.pop(key, None)
                gap_until[key] = self.clock()-self._origin+arrangement.config.gap
                continue
            with self.condition:
                self._status = 'playing'
            if not self._ensure_focus():
                continue
            if self.clock()-self._origin-strike.time > max(.05, arrangement.config.max_delay):
                self._skipped += 1
                heapq.heappop(queue)
                continue
            if self._stop or self._pause:
                continue
            self.driver.select_fret(FRET_KEYS[finger.fret])
            if self._stop or self._pause or not self._ensure_focus():
                continue
            key = STRING_KEYS[finger.string]
            self.driver.press(key)
            now = self.clock()-self._origin
            due = now+strike.hold
            release_due[key] = due
            pitch_until[finger.voice.pitch] = now+arrangement.config.repeat_interval
            heapq.heappush(pending, (due, serial, key))
            serial += 1
            if self.driver.late_fret_release:
                heapq.heappush(pending, (due, serial, FRET_KEYS[finger.fret]))
                serial += 1
            heapq.heappop(queue)
        if not self._stop:
            self._wait(limit)
        with self.condition:
            self._status = 'stopped' if self._stop else 'finished'
            if not self._stop:
                self._position = limit


class EdgeHotkeys:
    """Polling rising edges: holding F2 must not repeatedly toggle pause."""
    def __init__(self):
        self.held = set()

    def update(self, pressed):
        pressed = set(pressed)
        rising = pressed - self.held
        self.held = pressed
        return rising
