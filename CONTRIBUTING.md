# Contributing to Sloppy Player

Thanks for helping. Bug reports, arrangement reports with the score attached, translations and pull requests are all welcome. Questions and ideas go to [Discussions](https://github.com/Akureyri-i/sloppy-player/discussions).

Sloppy Player is a vibe-coded project (see the README). Contributions written with AI tools are welcome on the same terms as any other: you are responsible for what you submit and for checking that it works.

## Setting up

Python 3.12, 64-bit, on Windows.

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python launch.py
```

This runs the player without the model; arrangements then use the rule-based joint optimiser. For the model (the setup converts the official checkpoint to ONNX and installs PyTorch for that step; the player runs it with ONNX Runtime):

```
.venv\Scripts\python -m pip install -r requirements-model.txt
.venv\Scripts\python tools/setup_model.py --install
```

## Tests

```
.venv\Scripts\python -m unittest discover -s tests
```

Tests that need the model are skipped without it. GitHub runs the same tests on every pull request. None of the tests send keys to the game: playback is tested with a mock keyboard.

## Translations

Interface texts are in `guitar_player/locales/<language>.json`, one entry per key. `zh-CN.json` is the source and has every key; any other language falls back to English, then to `zh-CN`, key by key.

- To fix a text, edit the language's file.
- To add a language, copy `en.json` to `<code>.json` (for example `fr.json`), set `"_meta": {"name": "Français"}`, and translate the values.
- Keep placeholders such as `{count}` or `{name}` exactly as they are: an entry whose placeholders differ from `zh-CN` is ignored.
- Keep texts short: the window is small and the buttons are narrow.

You can try a language file without changing the program: put it in `%APPDATA%\SloppyPlayer\languages\`, choose it in Settings → Appearance and restart.

## Pull requests

- Keep changes focused, and write code that reads like the code around it. Please don't reformat files you are not changing.
- New interface text needs a key in `zh-CN.json` and `en.json`; code calls `tr('key')` and never contains the text itself.
- Changes to how the player presses keys (timing, key order, hold times) affect what the game hears. Explain how you checked them, ideally with a recording in game.
- Changes to arranging: describe which scores you compared before and after, and attach them if their license allows.

## Releases

Releases are built with `tools/build_exe.py` (see the README, Build from source) and attached to GitHub Releases with their SHA-256 checksums.

## License

By contributing you agree that your contribution is licensed under the GNU General Public License v3.0 or later, like the rest of the project.
