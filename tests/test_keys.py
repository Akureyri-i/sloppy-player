"""The exact physical keys sent to the game. Inspects mappings only; never sends."""
import sys
import unittest
from unittest.mock import MagicMock

from guitar_player.model import FRET_KEYS, STRING_KEYS
from guitar_player.playback import SCAN_CODES, KeyboardDriver, ScanCodeKeys


class KeyMappingTests(unittest.TestCase):
    def test_every_game_key_has_a_scan_code(self):
        self.assertEqual(set(SCAN_CODES), set(FRET_KEYS) | set(STRING_KEYS))
        self.assertEqual(len(set(SCAN_CODES.values())), len(SCAN_CODES))

    def test_minus_is_the_main_keyboard_key_not_the_numpad(self):
        self.assertEqual(SCAN_CODES['-'], 12)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows key tables')
    def test_codes_are_main_keyboard_keys_known_to_the_library(self):
        import keyboard
        from keyboard import _winkeyboard as windows
        windows.init()
        for key, code in SCAN_CODES.items():
            with self.subTest(key=key):
                self.assertIn(code, keyboard.key_to_scan_codes(key))
                # Numpad virtual keys are 0x60-0x6F (VK_NUMPAD0 .. VK_DIVIDE).
                self.assertFalse(0x60 <= windows.scan_code_to_vk[code] <= 0x6F)
        # The library's own first choice for '-' is the numpad key: the reason for SCAN_CODES.
        self.assertTrue(0x60 <= windows.scan_code_to_vk[keyboard.key_to_scan_codes('-')[0]] <= 0x6F)

    def test_driver_sends_scan_codes(self):
        module = MagicMock()
        driver = KeyboardDriver(ScanCodeKeys(module))
        driver.select_fret('-')
        driver.press('z')
        driver.release_all()
        self.assertEqual(module.press.call_args_list[0].args, (12,))
        self.assertEqual(module.release.call_args_list[0].args, (12,))
        self.assertEqual(module.press.call_args_list[1].args, (44,))
        self.assertEqual(module.release.call_args_list[-1].args, (44,))

    def test_default_driver_uses_scan_codes(self):
        self.assertIsInstance(KeyboardDriver().backend, ScanCodeKeys)


if __name__ == '__main__':
    unittest.main()
