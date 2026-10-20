"""Timing settings for the playback thread, scoped to a performance and always restored.

Entered on the playback thread (playback.Player._run):
- the interpreter's switch interval drops to SWITCH_INTERVAL. Python runs one thread at a
  time; with the default 5 ms a playback thread that wakes while the interface draws could
  wait up to ~8 ms for its turn. With 0.5 ms the late key presses measured with the real
  window drawing went from at most ~8 ms to ~1.4 ms.
- that thread runs at THREAD_PRIORITY_TIME_CRITICAL, so other programs (the game) yield the
  CPU to it.
- Windows timer resolution 1 ms. Windows 11 may ignore a process's timer resolution request
  while its windows are hidden (the player behind Roblox); the power-throttling setting
  below asks it to honour the request anyway. Playback's own waits do not depend on it
  (playback.Player._wait)."""
import ctypes
from ctypes import wintypes
import os
import sys

SWITCH_INTERVAL = .0005                         # s
PROCESS_POWER_THROTTLING = 4                    # PROCESS_INFORMATION_CLASS ProcessPowerThrottling
IGNORE_TIMER_RESOLUTION = 0x4                   # PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
THREAD_PRIORITY_TIME_CRITICAL = 15


class ThrottlingState(ctypes.Structure):
    _fields_ = [('version', ctypes.c_ulong), ('control', ctypes.c_ulong), ('state', ctypes.c_ulong)]


def honour_timer_resolution():
    """Control the timer-resolution throttling and switch it off (state 0); False where the
    system does not know the setting (before Windows 11)."""
    kernel = ctypes.WinDLL('kernel32')
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    setting = ThrottlingState(1, IGNORE_TIMER_RESOLUTION, 0)
    return bool(kernel.SetProcessInformation(kernel.GetCurrentProcess(), PROCESS_POWER_THROTTLING,
                                             ctypes.byref(setting), ctypes.sizeof(setting)))


class PlaybackTimer:
    def __enter__(self):
        self.api = self.priority = None
        self.switch = sys.getswitchinterval()
        sys.setswitchinterval(SWITCH_INTERVAL)
        if os.name == 'nt':
            kernel = ctypes.WinDLL('kernel32')
            kernel.GetCurrentThread.restype = wintypes.HANDLE
            kernel.GetThreadPriority.argtypes = [wintypes.HANDLE]
            kernel.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
            self.thread = kernel.GetCurrentThread()
            self.priority = kernel.GetThreadPriority(self.thread)
            kernel.SetThreadPriority(self.thread, THREAD_PRIORITY_TIME_CRITICAL)
            self.kernel = kernel
            try:
                honour_timer_resolution()
                api = ctypes.WinDLL('winmm')
                api.timeBeginPeriod.argtypes = [ctypes.c_uint]
                api.timeEndPeriod.argtypes = [ctypes.c_uint]
                if api.timeBeginPeriod(1) == 0:
                    self.api = api
            except OSError:
                pass
        return self

    def __exit__(self, *exc):
        if self.api is not None:
            self.api.timeEndPeriod(1)
        if self.priority is not None:
            self.kernel.SetThreadPriority(self.thread, self.priority)
        sys.setswitchinterval(self.switch)
        return False
