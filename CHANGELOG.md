# Changelog

## 1.1.0

- Smaller download: the model now runs on ONNX Runtime instead of PyTorch (zip 436 → 308 MB, 841 → 473 MB unpacked). Arrangements are the same.
- Guitar style no longer fails when cached analysis exists but PyTorch is not installed (running from source).

## 1.0.0

First release.

- Opens MIDI, MusicXML (.musicxml, .xml, .mxl) and ml3czus-autoplayer sheets; sheets can also be pasted in.
- Three arrangements: Faithful (default), Guitar style and As written, with Simplify, Speed and track selection.
- Model-assisted arrangement with M2BERT, running on the CPU.
- Listening window with the Windows MIDI synthesizer or SFZ / SF2 / SF3 sound packs.
- Export to MIDI, MusicXML (notation and tab) and ml3czus-autoplayer sheets.
- F1 / F2 / F3 hotkeys, automatic pause when Roblox is not in front.
- Interface in English, Español, Português (Brasil), Русский, Bahasa Indonesia, 简体中文 and 繁體中文.
