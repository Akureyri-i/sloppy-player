"""PlaybackTimer: the playback thread's timing settings hold during a performance and are restored after."""
import ctypes
import os
import sys
import threading
import unittest

from guitar_player.timing import SWITCH_INTERVAL, THREAD_PRIORITY_TIME_CRITICAL, PlaybackTimer


def thread_priority():
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32')
    kernel.GetCurrentThread.restype = wintypes.HANDLE
    kernel.GetThreadPriority.argtypes = [wintypes.HANDLE]
    return kernel.GetThreadPriority(kernel.GetCurrentThread())


class PlaybackTimerTests(unittest.TestCase):
    def test_switch_interval_and_priority_are_scoped(self):
        seen = {}

        def run():
            before = sys.getswitchinterval()
            priority = thread_priority() if os.name == 'nt' else None
            with PlaybackTimer():
                seen['inside'] = sys.getswitchinterval()
                if os.name == 'nt':
                    seen['priority'] = thread_priority()
            seen['after'] = sys.getswitchinterval() == before
            if os.name == 'nt':
                seen['priority_after'] = thread_priority() == priority
        thread = threading.Thread(target=run)
        thread.start()
        thread.join(5)
        self.assertAlmostEqual(seen['inside'], SWITCH_INTERVAL)
        self.assertTrue(seen['after'])
        if os.name == 'nt':
            self.assertEqual(seen['priority'], THREAD_PRIORITY_TIME_CRITICAL)
            self.assertTrue(seen['priority_after'])


if __name__ == '__main__':
    unittest.main()
