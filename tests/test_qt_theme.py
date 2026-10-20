import unittest

from guitar_player.qt.theme import PALETTES, contrast, readable_on


class PaletteContrastTest(unittest.TestCase):
    def test_text_is_readable_on_every_surface(self):
        for p in PALETTES.values():
            for surface in p.surfaces:
                with self.subTest(palette=p.name, surface=surface):
                    self.assertGreaterEqual(contrast(p.text, surface), 7)
                    self.assertGreaterEqual(contrast(p.text2, surface), 4.5)

    def test_accent_button_label(self):
        for p in PALETTES.values():
            with self.subTest(palette=p.name):
                self.assertGreaterEqual(contrast(p.on_accent, p.accent), 4.5)

    def test_note_colours_stand_out_and_carry_labels(self):
        for p in PALETTES.values():
            for colour in p.notes:
                with self.subTest(palette=p.name, colour=colour):
                    # Non-text graphics: WCAG 1.4.11 asks for 3:1 against the chart.
                    self.assertGreaterEqual(contrast(colour, p.sunken), 3)
                    self.assertGreaterEqual(contrast(readable_on(colour, p), colour), 4.5)


if __name__ == '__main__':
    unittest.main()
