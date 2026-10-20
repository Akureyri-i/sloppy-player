"""Keys of one moment sent as one batch (SendInputKeys), in order, without any real input."""
import ctypes
import time
import unittest

from guitar_player.arrange import arrange
from guitar_player.model import FRET_KEYS, Config
from guitar_player.playback import SCAN_CODES, KeyboardDriver, Player, SendInputKeys
from tests.test_core import note, song


class Batches:
    def __init__(self):
        self.batches = []

    def send(self, events):
        self.batches.append((time.perf_counter(), list(events)))


class DriverTests(unittest.TestCase):
    def test_keys_wait_for_flush_and_keep_their_order(self):
        backend = Batches()
        driver = KeyboardDriver(backend, batch=True)
        driver.select_fret('p')
        driver.press('v')
        driver.select_fret('y')
        driver.press('x')
        self.assertEqual(backend.batches, [])
        driver.flush()
        self.assertEqual(backend.batches[0][1], [('p', True), ('p', False), ('v', True),
                                                  ('y', True), ('y', False), ('x', True)])
        driver.release_all()                     # releases go out at once, strings first
        self.assertEqual(backend.batches[1][1], [('v', False), ('x', False)])
        self.assertEqual(driver.held, set())


class PlayerTests(unittest.TestCase):
    def test_a_chord_goes_out_in_one_batch(self):
        a = arrange(song([note(0, 60, 0.1, .5), note(1, 64, 0.1, .5), note(2, 67, 0.1, .5), note(3, 72, .3, .2)]),
                    Config(mode='raw', end_guard=False))
        backend = Batches()
        player = Player(KeyboardDriver(backend, batch=True), focus_check=lambda: True)
        player.play(a, countdown=0.)
        player.thread.join(5)
        presses = [events for _, events in backend.batches if any(down and k in 'zxcvbn' for k, down in events)]
        self.assertEqual([sum(down and k in 'zxcvbn' for k, down in p) for p in presses], [3, 1])
        chord = presses[0]                 # each note: fret down, fret up, string down
        self.assertEqual(len(chord), 9)
        self.assertTrue(all(chord[i][0] not in 'zxcvbn' and chord[i][1] and chord[i+1] == (chord[i][0], False)
                            and chord[i+2][0] in 'zxcvbn' and chord[i+2][1] for i in range(0, 9, 3)))
        self.assertEqual(player.driver.held, set())


class Keys:
    def __init__(self):
        self.events = []

    def press(self, key):
        self.events.append((time.perf_counter(), key, True))

    def release(self, key):
        self.events.append((time.perf_counter(), key, False))


class LateFretReleaseTests(unittest.TestCase):
    def test_a_note_is_fret_down_and_string_down_and_both_lift_a_hold_later(self):
        a = arrange(song([note(0, 60, 0.1, .5), note(1, 64, 0.1, .5), note(2, 67, .3, .2)]),
                    Config(mode='raw', end_guard=False))
        keys = Keys()
        player = Player(KeyboardDriver(keys, late_fret_release=True), focus_check=lambda: True)
        player.play(a, countdown=0.)
        player.thread.join(5)
        order = [(k, down) for _, k, down in keys.events]
        first_chord = order[:4]                              # two notes: fret down, string down each
        self.assertEqual([down for _, down in first_chord], [True]*4)
        self.assertEqual([k in 'zxcvbn' for k, _ in first_chord], [False, True, False, True])
        times = {}
        for t, k, down in keys.events:
            times.setdefault((k, down), t)
        for f in a.strikes[0].notes:
            fret = FRET_KEYS[f.fret]
            self.assertGreater(times[(fret, False)]-times[(fret, True)], .02)    # lifted a hold later
        self.assertEqual(player.driver.held, set())

    def test_a_fret_still_down_is_lifted_before_it_is_pressed_again(self):
        keys = Keys()
        driver = KeyboardDriver(keys, late_fret_release=True)
        driver.select_fret('p')
        driver.press('v')
        driver.select_fret('p')
        driver.press('c')
        self.assertEqual([(k, d) for _, k, d in keys.events],
                         [('p', True), ('v', True), ('p', False), ('p', True), ('c', True)])


class SendInputTests(unittest.TestCase):
    def test_records_are_what_the_keyboard_library_sends(self):
        keys = SendInputKeys()
        self.assertEqual(ctypes.sizeof(keys.Input), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        sent = []

        class User32:
            def SendInput(self, count, inputs, size):
                sent.append([(inputs[i].type, inputs[i].u.key.vk, inputs[i].u.key.scan, inputs[i].u.key.flags)
                             for i in range(count)])
                return count
        keys.user32 = User32()
        keys.send([('-', True), ('-', False), ('c', True)])
        minus = ctypes.windll.user32.MapVirtualKeyW(SCAN_CODES['-'], 1)
        self.assertEqual(minus, 0xBD)                   # the main-keyboard minus, not the keypad one
        self.assertEqual(sent, [[(1, minus, 12, 0), (1, minus, 12, 2), (1, keys.vk['c'], 46, 0)]])


if __name__ == '__main__':
    unittest.main()
