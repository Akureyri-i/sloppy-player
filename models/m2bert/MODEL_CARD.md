# Models

Sloppy Player uses a pre-trained music encoder and two small models trained for this project. The models only give soft evidence to the arranger; the game's range and key rules are hard constraints, and nothing is deleted because a model calls it "accompaniment".

## M2BERT encoder

- Jun-You Wang, Li Su. *Improving BERT for symbolic music understanding using token denoising and pianoroll prediction.* ISMIR 2025. Code <https://github.com/york135/M2BERT>, weights <https://github.com/york135/M2BERT_files>.
- Official checkpoint `M2BERT_pretrained_model/model_best.ckpt`, 749,258,690 bytes, SHA-256 `cf15015f8ed1bdf94983812ffb3229e51548b676dc5ceda76b91a09056b40f63`. The release carries `encoder.onnx`, the network exported to ONNX (`tools/export_onnx.py`) and run with ONNX Runtime; its features match PyTorch's to within 1e-4 (float rounding), and the two small heads are exported the same way.
- Vocabulary `CP.pkl` (SHA-256 `a84a4aeb14201af952acffa46d7dd04f8780a398d76af043f2f057a5c431885d`) and the ModernBERT-base `config.json` (SHA-256 `1609d59e627c33eaed524b4f01e546d42e84190a079a5a5ded84b212c41c324f`), set to M2BERT's 12 layers, 768 dimensions and four-attribute embedding. All weight keys are checked strictly.
- Frozen: not fine-tuned. Notes are read through a quantised view (bar, 16 positions per bar, 64 durations, pitch 22–107) in 1,024-note windows overlapping by 256; the original note times and pitches are kept separately.
- Distributed with the author's permission; see [THIRD_PARTY.md](../../THIRD_PARTY.md).

## Voice-link model (`voice_edges.*`)

Scores how likely one note continues the same voice as another.

- Training data: the Bach chorales in the music21 9.7.1 corpus, with the four voices as labels. 401 scores, 93,986 notes; 305 for training, 57 for validation, 39 for testing, split by BWV work family (duplicates and transpositions grouped together).
- Input: all voices merged into one anonymous track. A 768→32 projection of the frozen features plus geometric features (intervals, timing) feed a small network; temperature fitted on the validation set.

| 39 test scores | Geometric features only | M2BERT + geometric |
| --- | ---: | ---: |
| Next note, top-1 | 85.79% | 96.14% |
| Link F1 (threshold 0.5) | 83.49% | 94.70% |
| Brier score | 0.01671 | 0.00555 |

This measures voice linking on Bach chorales, not melody identification or how good an arrangement sounds on other music.

In the arranger the scores are mixed 0.65 / 0.35 with geometric evidence, then an optimiser (OR-Tools CP-SAT, overlapping windows of about 144 notes) chooses links and whole-octave moves together, weighing interval changes, voice crossings, crowding, phrase boundaries and repeated motifs. The result is a good local solution, not a proven global optimum.

## Voice-role model (`voice_roles.*`)

Gives each note a probability of melody, secondary melody or accompaniment; used by *Guitar style* only.

- Training data: POP909 (MELODY / BRIDGE / PIANO tracks merged into one track), split 8:1:1 by song.
- Frozen M2BERT features plus geometric features, bidirectional GRU, three classes.
- Test set: accuracy 93.2%, melody F1 0.895. On solo piano the arranger takes the top note of each onset as the melody candidate and asks the model only whether the onset has a melody note, unless the model is very sure about another note.

## Reproducing

The training data is not in this repository. Put the music21 9.7.1 wheel in `data/training/packages/` and POP909 in `data/training/pop909/POP909/`, install the model (`tools/setup_model.py --install`), then:

```
python tools/prepare_voice_corpus.py
python tools/train_voice_edges.py --epochs 35
python tools/train_role_head.py
```

These overwrite the model files in `models/m2bert/`; run them in a separate copy. Training results are written to `data/training/`.
