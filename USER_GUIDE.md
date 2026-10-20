# Sloppy Player user guide

[简体中文](USER_GUIDE.zh-CN.md)

## Playing a piece

1. **Open score** (or drag a file onto the window): MIDI (.mid, .midi), MusicXML (.musicxml, .xml, .mxl) or an ml3czus-autoplayer sheet (.txt). A sheet can also be pasted in with **Paste sheet**. Wait until the status bar says *Ready*.
2. To hear it first, click **Listen**. The listening window has its own position; switch between *Arranged* and *Original*; Space plays and pauses.
3. Click **Play** or press **F1**, then switch to Roblox within 3 seconds and hold the guitar.
4. **F2** pauses and resumes; **F3** stops and goes back to the start. Playback also pauses itself when Roblox is not in front (Settings → Keys).

The overview under the score is also the position bar. When stopped, drag it to choose where the next performance starts. During a performance (or while paused), dragging moves there and stays paused: F2 in the game continues at once; **Resume** in the player counts down 3 seconds first.

Settings are remembered per piece. **Restore defaults** appears after you change the arrangement settings; it leaves speed and key timing alone.

## Arrangement

| Arrangement | What it does |
| --- | --- |
| **Faithful** (default) | Keeps as much of the score as possible. A piece already inside the guitar's range is played as written. Otherwise notes are moved by whole octaves: candidate layouts are compared by how much they keep, intervals, spacing between voices and timing. |
| **Guitar style** | Keeps a solo-guitar skeleton: the melody, then the bass, the root and third of each chord, then the rest, with a texture close to real guitar arrangements. The piece moves to the register guitar arrangements usually use (often an octave below a piano score). |
| **As written** | Changes no pitch. Notes outside the game's range are not played; the same pitch twice at one moment is played once. |

**Simplify** thins the music. In *Faithful* and *As written* it keeps every onset and thins chords evenly across the register. In *Guitar style*, above 0 it also thins the melody in dense passages (short notes on weak beats first); about 50% is closest to human guitar arrangements.

*Faithful* never guesses a melody and then deletes the accompaniment; only *Guitar style* uses melody detection. On solo piano the top note of each onset is the melody candidate, and a model decides whether there is a melody note there. You can name the melody track yourself in Settings → Arrangement.

**Speed** goes from 0.25× to 4×. **Tracks** chooses which tracks take part; percussion is left out unless you include it in Settings.

The side panel tells you how many notes are played, how many moved by octaves, and why any notes are missing.

### The first analysis

A piece that is not already inside the guitar's range is analysed with the local model and an optimiser the first time it is opened. This takes from a few seconds to a few minutes for long, dense pieces. The model loads in the background when the program starts. You can keep working meanwhile: changing speed, simplify or key timing does not interrupt the analysis, and afterwards only the key schedule is redone. Changing tracks starts a new analysis. F3 or Stop cancels it. Results are cached in `%LOCALAPPDATA%\SloppyPlayer`.

## Views

- **Notes**: the notes around the playing position. Grey areas are outside the guitar's range; a solid outline marks where a moved note was originally, a dashed outline a note that is not played.
- **Keys**: one row per string, with the fret key on each note.
- **Keyboard**: the real keyboard, showing the selected fret key and the string keys just pressed.

Ctrl + mouse wheel (on the view or the overview), or dragging the edges of the box in the overview, sets the time range from one second to the whole piece.

## The game's keys

- String keys, low to high: **Z X C V B N**.
- Fret keys: `E R T Y U P - = [ ] F G H J K L ; ' M ,` (fret 0 to 19). M is a fret key; the highest string key is N.
- Range: MIDI 41 to 84.
- Every note sends its fret key (down, up) and then its string key, even when the fret does not change. Changing fret does not release other strings or wait.
- Strings are released independently. Defaults: string key held 25 ms, no extra gap between presses on the same string or of the same note.
- More than six notes at once are pressed in quick groups within the allowed onset delay (80 ms by default), so they may sound like a fast strum.

Settings → Keys:

- **Pitch calibration** (default +1 semitone) matches the key map to the pitches the game actually plays. It does not transpose the piece.
- **Reduce swallowed notes (experimental)**, on by default. If a pitch is pressed again just as its previous sound ends, the game may silence it, and keep silencing that pitch until there is a pause longer than the sound. With this option the few notes that would land at that moment are moved slightly, usually by less than 50 ms; only that note moves. Analysis details show how many were moved.
- **Play calibration** plays the six open strings so you can check the pitch in game.
- **Respond to F1 / F2 / F3 in other windows too**: the global hotkeys. When they are off, the keys work only in the player window.

## Listening

The **Sound** menu in the listening window, and Settings → Listening, choose between:

- **Default**: the Windows MIDI synthesizer.
- **Custom**: a sound pack you choose (.sfz, .sf2 or .sf3). SF2 / SF3 banks usually contain many instruments; pick one under *Instrument* (a guitar is chosen if there is one).

For custom sound packs there is a **Volume** slider and **Auto volume**. With auto volume on, the slider shows the level that matches the reference loudness; dragging the slider turns it off, ticking it again returns to the automatic level. Each pack and instrument remembers its own setting. Very loud peaks are softened.

The listening window shows the music, not what the game will sound like: the game has its own samples and note lengths.

## Export

**Export…** in the side panel:

- **MIDI**: on the score's bars, time signatures and tempo (multiplied by the current speed), with the pitches the game will play, one track per string.
- **MusicXML (notation + tab)**: a guitar score with standard notation over a tab staff for the game's strings; the melody in one voice and everything else in another, aligned to 32nd notes or triplets. Saved as .musicxml or compressed .mxl.
- **ml3czus auto-play sheet**: opens a text box. **Copy all** and paste it into the ml3czus-autoplayer sheet box, or **Save as .txt**. Each step is 31 ms. The fret keys for frets 8 and 9 (`[` and `]`) cannot be written in this format because they are its chord brackets, so those notes move to another string; the box says how many.

The small timing adjustments made for the game (chords pressed in groups, delayed onsets, reduced swallowed notes) are part of playing and are not written into MIDI or MusicXML.

## ml3czus-autoplayer sheets

Sheets are read as the format defines them (`Key delay`, `[...]`, `-`, `|` and so on). A single `[` or `]` is a bracket, not a key, so it is skipped and the side panel says so. With *As written* the sheet's own strings and frets are played (if one string is pressed twice in one step, the second press follows shortly after); the other arrangements arrange the notes again.

## Other settings

- **Appearance**: colours (Frost, Rice paper, Night, Amber, Neon) and language. A new language takes effect after a restart. On first start the program follows the Windows language.
- **Arrangement**: the arrangement engine (the model-assisted joint arrangement is the default; the others are for comparison), octave shift, maximum notes per chord, melody track, analysis details and the analysis report.

Settings are stored in `%APPDATA%\SloppyPlayer`. To add or correct a translation, put a language file (for example `fr.json`, in the format of the files in `guitar_player/locales/`) in `%APPDATA%\SloppyPlayer\languages\`.

## Supported files

MIDI type 0 and 1 with tempo and time signature changes and the sustain pedal. MIDI type 2 and SMPTE timing are refused with a message. MusicXML score-partwise, including repeats, endings, D.S. / D.C., Coda and Fine; a tab staff is skipped when the part also has standard notation.
