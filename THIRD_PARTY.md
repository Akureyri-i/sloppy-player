# Third-party work

Sloppy Player's own code is licensed under GPL-3.0-or-later (see [LICENSE](LICENSE)). This file lists the work it uses, under the terms of each. It does not grant any rights beyond theirs.

## Model files shipped with the program

**M2BERT encoder** (`models/m2bert/encoder.safetensors` in the release, `CP.pkl`)
Jun-You Wang, Li Su. *Improving BERT for symbolic music understanding using token denoising and pianoroll prediction.* ISMIR 2025.
Code: <https://github.com/york135/M2BERT> · Weights: <https://github.com/york135/M2BERT_files>
The official repositories carry no license file. The author, Jun-You Wang, confirmed in writing that the model and code may be used and redistributed without license restrictions, asking only that the paper be cited and the work credited. This permission is not an open-source license and does not extend to anything else listed here. `encoder.safetensors` is the `state_dict` of the official `model_best.ckpt` (SHA-256 `cf15015f…40f63`), written by `tools/export_encoder_weights.py`; the optimizer state is left out.

**ModernBERT configuration** (`models/m2bert/config.json`)
Warner et al. *Smarter, Better, Faster, Longer: A Modern Bidirectional Encoder for Fast, Memory Efficient, and Long Context Finetuning and Inference.* 2024. <https://huggingface.co/answerdotai/ModernBERT-base>, Apache-2.0.

**Voice-link model** (`models/m2bert/voice_edges.*`, trained by this project)
A small head over frozen M2BERT features, trained on the Bach chorales encoded in the music21 9.7.1 corpus (Cuthbert & Ariza, *music21: A Toolkit for Computer-Aided Musicology and Symbolic Music Data*, ISMIR 2010). The corpus terms are in `models/m2bert/source_notices/`. The scores themselves are not distributed.

**Voice-role model** (`models/m2bert/voice_roles.*`, trained by this project)
Trained on POP909 (Wang et al., *POP909: A Pop-song Dataset for Music Arrangement Generation*, ISMIR 2020; <https://github.com/music-x-lab/POP909-Dataset>). MIT License, Copyright (c) 2020 Music X Lab. The dataset is not distributed.

Both trained models are licensed with the program (GPL-3.0-or-later). See the [model card](models/m2bert/MODEL_CARD.md).

## Data used for statistics and evaluation (not distributed)

- **AnimeTAB** (Zhou, Ju, Xie, arXiv:2210.03027; <https://github.com/amamiya-yuuko/AnimeTAB>), CC BY-NC. *Guitar style*'s texture targets (notes per beat and per onset, the register of melody and bass, fret span) are medians computed from its 403 songs. They come from non-commercial data; check this before any commercial use.
- **PDMX** (Long et al., arXiv:2409.10831; <https://zenodo.org/records/15571083>), CC BY 4.0: piano scores with lyrics, used to check the melody line.
- **MERT-v1-95M** (Li et al., *MERT: Acoustic Music Understanding Model with Large-Scale Self-supervised Training*, ICLR 2024), CC BY-NC 4.0: audio similarity used to compare arrangements and tune three weights of *Guitar style*.
- **FluidR3_GM** sound font (Frank Wen), MIT: piano sound for those comparisons.

## Methods

- A. Uitdenbogerd, J. Zobel. *Melodic matching techniques for large music databases.* ACM Multimedia 1999. The top note of an onset as the melody candidate ("skyline"), and the rule used without the model.
- E. Nakamura, S. Sagayama. *Automatic piano reduction from ensemble scores based on merged-output hidden Markov model.* ICMC 2015. The cost of moving a note by an octave.
- Y.-W. Hsiao, L. Su. *Learning note-to-note affinity for voice segregation and melody line identification of symbolic music data.* ISMIR 2021. E. Karystinaios, F. Foscarin, G. Widmer. *Musical voice separation as link prediction.* IJCAI 2023. Voices as scored links between notes.
- K. Kosta et al. *A deep learning method for melody extraction from a polyphonic symbolic music representation.* ISMIR 2022. Pitch and neighbouring intervals as note features.

## Formats and references

- **ml3czus-autoplayer** by @ml3czus_ (<https://github.com/ml3czus/ml3czus-autoplayer>): the sheet format read and written by Sloppy Player (format of version 1.3.2). No code from it is included.
- **MIDIPlusPlus** by Zephkek (<https://github.com/Zephkek/MIDIPlusPlus>, GPL-3.0): its key-sending code was read as a reference. No code from it is included.
- **MusicXML** (W3C Music Notation Community Group): the format of MusicXML import and export.

## Software in the Windows release

The release folder contains these packages as built by PyInstaller; their license texts are in `licenses/` next to `SloppyPlayer.exe`.

| Package | License |
| --- | --- |
| Python 3.12 | PSF License |
| PySide6 / Qt 6 (Core, Gui, Widgets) | LGPL-3.0. The Qt libraries are separate DLLs in the folder and can be replaced. Qt source: <https://code.qt.io> |
| OR-Tools | Apache-2.0 |
| PyTorch (CPU) | BSD-3-Clause |
| Transformers, tokenizers, huggingface_hub, safetensors | Apache-2.0 |
| NumPy | BSD-3-Clause |
| pandas (used by OR-Tools) | BSD-3-Clause |
| protobuf | BSD-3-Clause |
| mido, keyboard | MIT |
| tinysoundfont (TinySoundFont, stb_vorbis) | MIT |
| sympy, mpmath, networkx, Jinja2 (used by PyTorch) | BSD |
| PyInstaller bootloader | GPL-2.0 with a bootloader exception |

Exact versions are in `requirements.txt`, `requirements-model.txt` and the `licenses/` folder names.
