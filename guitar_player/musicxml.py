"""MusicXML scores (.musicxml / .xml / .mxl): reading as a Song, writing an arrangement.

Reading: the notes in playing order (repeats, endings, D.S. / D.C., To Coda and Fine,
marks taken from the first part), sounding pitch (transposing instruments), ties
merged, grace and cue notes left out. A tab staff is skipped when its part also has
a standard staff (the same notes twice). Each part and staff becomes a track, as in
MuseScore's own MIDI export. Notes also carry whether they are sung (lyrics, melismas).

Writing (write_musicxml): one guitar part, standard notation (treble clef an octave
down) over a tab staff tuned to the game's strings, the melody (or the top note of each
onset) in one voice and everything else in another. Times are the score's beats, not
the key timing; the tempo is the score's times the playing speed.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

import mido

from . import __version__
from .i18n import tr
from .midi import read_midi

STEPS = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
JUMPS = ('segno', 'dalsegno', 'dacapo', 'tocoda', 'coda', 'fine')
TICKS = 480


# -- reading ----------------------------------------------------------------
def playback(bars):
    """Measure indices in playing order: repeats with endings, D.S. / D.C. (taken once, without repeats,
    final endings only), To Coda and Fine on that pass."""
    endings, bracket = [()]*len(bars), None
    for k, b in enumerate(bars):
        if b['ending']:
            bracket = b['ending']
        if bracket:
            endings[k] = bracket
            if b['ending_stop']:
                bracket = None
    final, k = set(), 0           # in each run of ending measures, those of the highest ending
    while k < len(bars):
        run = k
        while run < len(bars) and endings[run]:
            run += 1
        last = max((n for e in endings[k:run] for n in e), default=0)
        final.update(j for j in range(k, run) if last in endings[j])
        k = run+1
    segno = next((k for k, b in enumerate(bars) if b['segno']), 0)
    order, i, start, turn, taken, again, closing = [], 0, 0, 1, {}, False, False
    while i < len(bars):
        b = bars[i]
        if endings[i] and (turn not in endings[i] if not again else i not in final):
            i += 1
            continue
        if b['forward'] and i != start or closing and not endings[i]:
            start, turn, closing = i, 1, False
        order.append(i)
        if again and b['fine']:
            break
        if again and b['tocoda']:
            i = next((k for k in range(i+1, len(bars)) if bars[k]['coda']), len(bars))
            continue
        if b['backward'] and not again and taken.get(i, 0) < b['backward']-1:
            taken[i] = taken.get(i, 0)+1
            i, turn = start, turn+1
            continue
        if b['backward']:
            start, closing = i+1, True      # the endings that follow still count this pass
        if (b['dalsegno'] or b['dacapo']) and not again:
            again = True
            i = start = segno if b['dalsegno'] else 0
            continue
        i += 1
    return order


@dataclass
class ScoreNote:
    beat: float             # quarter notes from the start, in playing order
    beats: float
    pitch: int              # sounding
    part: int
    staff: int
    voice: str
    lyric: bool


@dataclass
class Score:
    path: str
    parts: list[dict]       # name, program (0-based GM, None if not given), staves
    notes: list[ScoreNote]
    tempos: list[tuple[float, float]]           # (beat, bpm)
    meters: list[tuple[float, int, int]]        # (beat, numerator, denominator)


def root_of(path):
    path = Path(path)
    if path.suffix.lower() != '.mxl':
        root = ET.parse(path).getroot()
    else:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if 'META-INF/container.xml' in names:
                container = ET.fromstring(z.read('META-INF/container.xml'))
                name = next(e.get('full-path') for e in container.iter() if e.tag.endswith('rootfile'))
            else:
                name = next(n for n in names if n.endswith('.xml') and not n.startswith('META-INF'))
            root = ET.fromstring(z.read(name))
    if root.tag != 'score-partwise':
        raise ValueError(tr('error.musicxml.kind'))
    return root


def part_bars(part):
    """Per measure: notes [start, beats, pitch, staff, voice, lyric kind, ties], length, marks, tempo / meter
    events; and the part's tab staves."""
    bars, divisions, transpose, tabs, staves = [], 1., 0, set(), {1}
    for measure in part.iter('measure'):
        cursor, last_start, length, notes, events = 0., 0., 0., [], []
        marks = dict(forward=False, backward=0, ending=(), ending_stop=False, **{k: False for k in JUMPS})
        for item in measure:
            tag = item.tag
            if tag == 'attributes':
                if item.find('divisions') is not None:
                    divisions = float(item.findtext('divisions'))
                t = item.find('transpose')
                if t is not None:
                    transpose = int(float(t.findtext('chromatic') or 0))+12*int(float(t.findtext('octave-change') or 0))
                time = item.find('time')
                if time is not None and time.findtext('beats') and time.findtext('beats').isdigit():
                    events.append(('meter', cursor, (int(time.findtext('beats')), int(time.findtext('beat-type')))))
                for clef in item.findall('clef'):
                    staff = int(clef.get('number') or 1)
                    staves.add(staff)
                    (tabs.add if clef.findtext('sign') == 'TAB' else tabs.discard)(staff)
            elif tag == 'backup':
                cursor -= float(item.findtext('duration'))/divisions
            elif tag == 'forward':
                cursor += float(item.findtext('duration'))/divisions
            elif tag == 'barline':
                repeat, ending = item.find('repeat'), item.find('ending')
                if repeat is not None and repeat.get('direction') == 'forward':
                    marks['forward'] = True
                elif repeat is not None:
                    marks['backward'] = int(repeat.get('times') or 2)
                if ending is not None and ending.get('type') == 'start':
                    marks['ending'] = tuple(int(x) for x in (ending.get('number') or '1').replace(' ', '').split(',')
                                            if x.isdigit())
                elif ending is not None:
                    marks['ending_stop'] = True
            elif tag in ('direction', 'sound'):
                for sound in ([item] if tag == 'sound' else item.iter('sound')):
                    for k in JUMPS:
                        marks[k] |= sound.get(k) is not None
                    if sound.get('tempo'):
                        events.append(('tempo', cursor, float(sound.get('tempo'))))
            elif tag == 'note':
                if item.find('grace') is not None or item.find('cue') is not None:
                    continue
                duration = float(item.findtext('duration') or 0)/divisions
                start = last_start if item.find('chord') is not None else cursor
                if item.find('chord') is None:
                    last_start = cursor
                    cursor += duration
                length = max(length, cursor)
                if item.find('pitch') is None:
                    continue
                pitch = (int(item.findtext('pitch/octave'))+1)*12+STEPS[item.findtext('pitch/step')] \
                    + round(float(item.findtext('pitch/alter') or 0))+transpose
                # Lyric kinds: 'start' a syllable held over the next notes (<extend/> or type start),
                # 'text' a plain syllable, 'stop' the last note of a melisma, 'extend' inside one.
                kind = None
                for lyric in item.findall('lyric'):
                    extend = lyric.find('extend')
                    if lyric.find('text') is not None:
                        kind = 'start' if extend is not None and extend.get('type') in (None, 'start') else 'text'
                    elif extend is not None:
                        kind = 'stop' if extend.get('type') == 'stop' else 'extend'
                ties = {t.get('type') for t in item.findall('tie')}
                notes.append([start, duration, pitch, int(item.findtext('staff') or 1), item.findtext('voice') or '1',
                              kind, ties])
            length = max(length, cursor)
        # A syllable belongs to the whole chord it stands under (exporters put it on any one note).
        chord = {}
        for n in notes:
            if n[5]:
                chord[(n[0], n[3], n[4])] = n[5]
        for n in notes:
            n[5] = n[5] or chord.get((n[0], n[3], n[4]))
        bars.append(dict(notes=notes, length=length, events=events, **marks))
    # A tab staff beside a standard one repeats its notes.
    return bars, (tabs if staves-tabs else set())


def read_score(path):
    root = root_of(path)
    names = {}
    for sp in root.iter('score-part'):
        program = sp.findtext('midi-instrument/midi-program')
        names[sp.get('id')] = dict(name=(sp.findtext('part-name') or '').strip(),
                                   program=int(program)-1 if program and program.isdigit() else None)
    parts = list(root.iter('part'))
    read = [part_bars(p) for p in parts]
    bars = [b for b, _ in read]
    count = min((len(b) for b in bars), default=0)
    lengths = [max(b[k]['length'] for b in bars) for k in range(count)]
    order = playback([dict(bars[0][k], length=lengths[k]) for k in range(count)]) if count else []
    notes, tempos, meters = [], [], []
    for pi, (part_bars_, skipped) in enumerate(read):
        open_ties, melisma, offset = {}, set(), 0.
        for k in order:
            bar = part_bars_[k]
            if pi == 0:
                for kind, at, value in bar['events']:
                    if kind == 'tempo':
                        tempos.append((offset+at, value))
                    else:
                        meters.append((offset+at, *value))
            for start, beats, pitch, staff, voice, kind, ties in bar['notes']:
                if staff in skipped:
                    continue
                key = (staff, voice, pitch)
                if 'stop' in ties and key in open_ties:
                    notes[open_ties[key]].beats += beats
                    if 'start' not in ties:
                        del open_ties[key]
                    continue
                lyric = kind is not None or (staff, voice) in melisma
                if kind == 'start':
                    melisma.add((staff, voice))
                elif kind in ('text', 'stop'):
                    melisma.discard((staff, voice))
                notes.append(ScoreNote(offset+start, beats, pitch, pi, staff, voice, lyric))
                if 'start' in ties:
                    open_ties[key] = len(notes)-1
            offset += lengths[k]
    notes.sort(key=lambda n: (n.beat, -n.pitch))
    return Score(str(path), [names.get(p.get('id'), dict(name='', program=None)) for p in parts], notes,
                 tempos or [(0., 120.)], meters)


def score_midi(score):
    """The score as a MIDI file: one track per part and staff, like MuseScore's own export."""
    midi = mido.MidiFile(ticks_per_beat=TICKS, charset='utf-8')
    conductor = [(round(b*TICKS), mido.MetaMessage('set_tempo', tempo=mido.bpm2tempo(bpm))) for b, bpm in score.tempos]
    conductor += [(round(b*TICKS), mido.MetaMessage('time_signature', numerator=n, denominator=d))
                  for b, n, d in score.meters]
    tracks = sorted({(n.part, n.staff) for n in score.notes})
    staves = defaultdict(set)
    for part, staff in tracks:
        staves[part].add(staff)
    for ti, (part, staff) in enumerate(tracks or [(0, 1)]):
        channel = ti % 16 if ti % 16 != 9 else 15
        events = list(conductor) if ti == 0 else []
        program = score.parts[part]['program'] if score.parts else None
        if program is not None:
            events.append((0, mido.Message('program_change', channel=channel, program=program)))
        for n in score.notes:
            if (n.part, n.staff) == (part, staff):
                a, b = round(n.beat*TICKS), round((n.beat+n.beats)*TICKS)
                events.append((a, mido.Message('note_on', channel=channel, note=n.pitch, velocity=80)))
                events.append((max(a+1, b), mido.Message('note_off', channel=channel, note=n.pitch, velocity=0)))
        events.sort(key=lambda e: (e[0], e[1].type != 'note_off'))
        name = (score.parts[part]['name'] if score.parts else '') or tr('musicxml.part', number=part+1)
        if len(staves[part]) > 1:
            name = f'{name} {staff}'
        track = mido.MidiTrack([mido.MetaMessage('track_name', name=name)])
        tick = 0
        for at, msg in events:
            track.append(msg.copy(time=at-tick))
            tick = at
        midi.tracks.append(track)
    return midi


def read_musicxml(path):
    return read_midi(path, score_midi(read_score(path)))


# -- writing ----------------------------------------------------------------
DIVISIONS = 24           # per quarter note: 32nds (3) and triplet 16ths (4) are whole numbers
NAMES = ('C', 'C', 'D', 'D', 'E', 'F', 'F', 'G', 'G', 'A', 'A', 'B')
SHARPS = {1, 3, 6, 8, 10}
WHOLE_BEATS = {6: ('whole', 1), 4: ('whole', 0), 3: ('half', 1), 2: ('half', 0), 1: ('quarter', 0)}
BINARY = {18: ('eighth', 1), 12: ('eighth', 0), 9: ('16th', 1), 6: ('16th', 0), 3: ('32nd', 0)}
TRIPLET = {16: ('quarter', 0), 12: ('eighth', 1), 8: ('eighth', 0), 4: ('16th', 0)}    # inside a 3:2 beat


def snap_points(points):
    """{position in quarter notes: position in divisions}, each quarter note on a 32nd grid or,
    when its points fit it clearly better, a triplet 16th grid; and {quarter: grid step}."""
    by_beat = defaultdict(list)
    for x in points:
        by_beat[int(x+1e-9)].append(x)
    out, steps = {}, {}
    for beat, xs in by_beat.items():
        binary = sum(abs(x*8-round(x*8)) for x in xs)/8
        triplet = sum(abs(x*6-round(x*6)) for x in xs)/6
        grid = 6 if triplet < binary*.5-1e-9 else 8
        steps[beat] = DIVISIONS//grid
        for x in xs:
            out[x] = round(x*grid)*DIVISIONS//grid
    return out, steps


def pieces(start, end, triplet_beats):
    """Notated pieces (start, length, type, dots, triplet) of start..end (divisions from the bar
    line): whole beats from a beat line, otherwise up to the next beat line."""
    out, pos = [], start
    while pos < end:
        if pos % DIVISIONS == 0 and end-pos >= DIVISIONS:
            beats = next(b for b in WHOLE_BEATS if b*DIVISIONS <= end-pos)
            out.append((pos, beats*DIVISIONS, *WHOLE_BEATS[beats], False))
            pos += beats*DIVISIONS
            continue
        beat = pos//DIVISIONS
        table = TRIPLET if beat in triplet_beats else BINARY
        length = next(n for n in table if n <= min(end, (beat+1)*DIVISIONS)-pos)
        out.append((pos, length, *table[length], beat in triplet_beats))
        pos += length
    return out


def voice_events(chords):
    """{onset: [(end, pitch, string, fret)]} -> [(start, end, [(pitch, string, fret)])]: each chord
    lasts as long as its longest note, at most until the voice's next onset."""
    onsets = sorted(chords)
    out = []
    for i, at in enumerate(onsets):
        end = max(e for e, *_ in chords[at])
        if i+1 < len(onsets):
            end = min(end, onsets[i+1])
        out.append((at, end, sorted({(p, s, f) for _, p, s, f in chords[at]})))
    return out


def bar_lines(song, last):
    """[(start, length, (numerator, denominator) or None when unchanged)] in divisions, up to last."""
    changes = sorted((round(t/song.ticks_per_beat*DIVISIONS), n, d) for t, n, d in song.meters)
    out, at, meter, shown, k = [], 0, (4, 4), None, 0
    while at < last or not out:
        while k < len(changes) and changes[k][0] <= at:
            meter = changes[k][1:]
            k += 1
        length = meter[0]*4*DIVISIONS//meter[1]
        if k < len(changes) and changes[k][0] < at+length:
            length = changes[k][0]-at          # a meter change inside a bar starts a new bar
        out.append((at, length, meter if meter != shown else None))
        shown = meter
        at += length
    return out


def write_musicxml(arrangement, path):
    a = arrangement
    song, config = a.song, a.config
    tpb = song.ticks_per_beat
    played = [(f.voice.note, f.voice.pitch, f.string, f.fret) for s in a.strikes for f in s.notes]
    snap, steps = snap_points({x for n, *_ in played for x in (n.tick/tpb, n.end_tick/tpb)})
    # Upper voice: 吉他化's melody line, otherwise the top note of each onset.
    top = defaultdict(int)
    for n, p, *_ in played:
        top[snap[n.tick/tpb]] = max(top[snap[n.tick/tpb]], p)
    voices = (defaultdict(list), defaultdict(list))
    for n, p, s, f in played:
        at = snap[n.tick/tpb]
        upper = n.id in a.melody_ids if a.melody_ids else p == top[at]
        end = max(snap[n.end_tick/tpb], at+steps[at//DIVISIONS])
        voices[0 if upper else 1][at].append((end, p, s, f))
    events = [voice_events(v) for v in voices]
    last = max((b for v in events for _, b, _ in v), default=DIVISIONS)

    root = ET.Element('score-partwise', version='4.0')
    ET.SubElement(ET.SubElement(root, 'work'), 'work-title').text = Path(song.path).stem
    encoding = ET.SubElement(ET.SubElement(root, 'identification'), 'encoding')
    ET.SubElement(encoding, 'software').text = f'Sloppy Player {__version__}'
    ET.SubElement(encoding, 'encoding-date').text = date.today().isoformat()
    score_part = ET.SubElement(ET.SubElement(root, 'part-list'), 'score-part', id='P1')
    ET.SubElement(score_part, 'part-name').text = tr('musicxml.guitar')
    instrument = ET.SubElement(score_part, 'midi-instrument', id='P1-I1')
    ET.SubElement(instrument, 'midi-channel').text = '1'
    ET.SubElement(instrument, 'midi-program').text = '25'
    part = ET.SubElement(root, 'part', id='P1')
    tempos = sorted((round(t/tpb*DIVISIONS), 60e6/us*config.speed) for t, us in song.tempos)
    shown_bpm = None
    for number, (start, length, meter) in enumerate(bar_lines(song, last), 1):
        measure = ET.SubElement(part, 'measure', number=str(number))
        if number == 1 or meter:
            _attributes(measure, number == 1, meter, config.tuning)
        for at, bpm in tempos:
            if start <= at < start+length:
                shown = shown_bpm is None or abs(bpm-shown_bpm) >= shown_bpm*.05
                shown_bpm = bpm if shown else shown_bpm
                _tempo(measure, at-start, bpm, shown)
        written = 0
        for staff in (1, 2):
            for v, events_ in enumerate(events):
                # Cut to the bar; a chord that crosses a bar line is tied over it.
                inside = [(max(x, start), min(y, start+length), notes, x < start, y > start+length)
                          for x, y, notes in events_ if x < start+length and y > start]
                if v and not inside:
                    continue                    # no accompaniment here: no rests for it either
                if written:
                    ET.SubElement(ET.SubElement(measure, 'backup'), 'duration').text = str(length)
                _voice(measure, inside, start, length, staff, v)
                written += 1
    ET.indent(root, space=' ')
    text = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
            '"http://www.musicxml.org/dtds/partwise.dtd">\n' + ET.tostring(root, encoding='unicode') + '\n')
    if Path(path).suffix.lower() != '.mxl':
        Path(path).write_text(text, encoding='utf-8')
        return
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:     # compressed MusicXML
        z.writestr('mimetype', 'application/vnd.recordare.musicxml', compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/container.xml',
                   '<?xml version="1.0" encoding="UTF-8"?>\n<container><rootfiles>'
                   '<rootfile full-path="score.musicxml" media-type="application/vnd.recordare.musicxml+xml"/>'
                   '</rootfiles></container>\n')
        z.writestr('score.musicxml', text)


def _pitch(parent, pitch, tag='pitch', prefix=''):
    element = ET.SubElement(parent, tag)
    ET.SubElement(element, prefix+'step').text = NAMES[pitch % 12]
    if pitch % 12 in SHARPS:
        ET.SubElement(element, prefix+'alter').text = '1'
    ET.SubElement(element, prefix+'octave').text = str(pitch//12-1)
    return element


def _attributes(measure, first, meter, tuning):
    attributes = ET.SubElement(measure, 'attributes')
    if first:
        ET.SubElement(attributes, 'divisions').text = str(DIVISIONS)
        ET.SubElement(ET.SubElement(attributes, 'key'), 'fifths').text = '0'
    if meter:
        time = ET.SubElement(attributes, 'time')
        ET.SubElement(time, 'beats').text = str(meter[0])
        ET.SubElement(time, 'beat-type').text = str(meter[1])
    if first:
        ET.SubElement(attributes, 'staves').text = '2'
        clef = ET.SubElement(attributes, 'clef', number='1')
        ET.SubElement(clef, 'sign').text = 'G'
        ET.SubElement(clef, 'line').text = '2'
        ET.SubElement(clef, 'clef-octave-change').text = '-1'
        clef = ET.SubElement(attributes, 'clef', number='2')
        ET.SubElement(clef, 'sign').text = 'TAB'
        ET.SubElement(clef, 'line').text = '5'
        details = ET.SubElement(attributes, 'staff-details', number='2')
        ET.SubElement(details, 'staff-lines').text = '6'
        for line, pitch in enumerate(tuning, 1):          # line 1 is the lowest string
            tuned = _pitch(details, pitch, 'staff-tuning', 'tuning-')
            tuned.set('line', str(line))


def _tempo(measure, offset, bpm, shown):
    direction = ET.SubElement(measure, 'direction', placement='above')
    metronome = ET.SubElement(ET.SubElement(direction, 'direction-type'), 'metronome')
    if not shown:
        metronome.set('print-object', 'no')     # small tempo drifts: playback only
    ET.SubElement(metronome, 'beat-unit').text = 'quarter'
    ET.SubElement(metronome, 'per-minute').text = str(round(bpm))
    if offset:
        ET.SubElement(direction, 'offset').text = str(offset)
    ET.SubElement(direction, 'staff').text = '1'
    ET.SubElement(direction, 'sound', tempo=f'{bpm:.2f}'.rstrip('0').rstrip('.'))


def _voice(measure, events, start, length, staff, v):
    """One voice of one staff in one bar: its chords (cut to the bar, tied over) and the rests between."""
    number = str(v+1+4*(staff-1))
    spans, cursor = [], start
    for x, y, notes, carried_in, carried_out in events:
        if x > cursor:
            spans.append((cursor, x, None, False, False))
        spans.append((x, y, notes, carried_in, carried_out))
        cursor = y
    if cursor < start+length:
        spans.append((cursor, start+length, None, False, False))
    if not events:
        note = ET.SubElement(measure, 'note')
        ET.SubElement(note, 'rest', measure='yes')
        ET.SubElement(note, 'duration').text = str(length)
        ET.SubElement(note, 'voice').text = number
        ET.SubElement(note, 'staff').text = str(staff)
        return
    # A beat is written in triplets when one of this voice's edges inside it is off the 32nd grid.
    triplets = {(x-start)//DIVISIONS for a, b, *_ in spans for x in (a, b) if (x-start) % 3}
    laid = [(a, b, notes, cin, cout, piece) for a, b, notes, cin, cout in spans
            for piece in pieces(a-start, b-start, triplets)]
    in_beat = defaultdict(list)
    for k, (*_, piece) in enumerate(laid):
        if piece[4]:
            in_beat[piece[0]//DIVISIONS].append(k)
    for k, (a, b, notes, carried_in, carried_out, (pos, size, kind, dots, triplet)) in enumerate(laid):
        group = in_beat[pos//DIVISIONS]
        tied_in = notes is not None and (start+pos > a or carried_in)
        tied_out = notes is not None and (start+pos+size < b or carried_out)
        for i, chord_note in enumerate(notes or [None]):
            note = ET.SubElement(measure, 'note')
            if i:
                ET.SubElement(note, 'chord')
            if chord_note is None:
                ET.SubElement(note, 'rest')
            else:
                _pitch(note, chord_note[0])
            ET.SubElement(note, 'duration').text = str(size)
            if tied_in:
                ET.SubElement(note, 'tie', type='stop')
            if tied_out:
                ET.SubElement(note, 'tie', type='start')
            ET.SubElement(note, 'voice').text = number
            ET.SubElement(note, 'type').text = kind
            for _ in range(dots):
                ET.SubElement(note, 'dot')
            if triplet:
                modification = ET.SubElement(note, 'time-modification')
                ET.SubElement(modification, 'actual-notes').text = '3'
                ET.SubElement(modification, 'normal-notes').text = '2'
            if chord_note is not None and staff == 1:
                ET.SubElement(note, 'stem').text = 'up' if v == 0 else 'down'
            ET.SubElement(note, 'staff').text = str(staff)
            marks = [('tied', kind_) for kind_, on in (('stop', tied_in), ('start', tied_out)) if on]
            if triplet and not i:
                marks += [('tuplet', kind_) for kind_, on in (('start', k == group[0]), ('stop', k == group[-1])) if on]
            if marks or (chord_note is not None and staff == 2):
                notations = ET.SubElement(note, 'notations')
                for tag, kind_ in marks:
                    ET.SubElement(notations, tag, type=kind_)
                if chord_note is not None and staff == 2:
                    technical = ET.SubElement(notations, 'technical')
                    ET.SubElement(technical, 'string').text = str(6-chord_note[1])
                    ET.SubElement(technical, 'fret').text = str(chord_note[2])

