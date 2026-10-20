from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .i18n import tr

STRING_KEYS = ('z', 'x', 'c', 'v', 'b', 'n')
FRET_KEYS = ('e', 'r', 't', 'y', 'u', 'p', '-', '=', '[', ']',
             'f', 'g', 'h', 'j', 'k', 'l', ';', "'", 'm', ',')
TUNING = (40, 45, 50, 55, 59, 64)
# TUNING is standard guitar tuning; the game sounds one semitone above it (Config.tuning_offset).
NOTE_MIN, NOTE_MAX = 41, 84
# Seconds each game pitch sounds: one fixed sample per pitch, the same on every string
# (recorded from the game; longest measurement of each pitch).
GAME_SOUND_LENGTH = {
    41: 2.32, 42: 2.19, 43: 2.07, 44: 1.95, 45: 1.84, 46: 1.74, 47: 1.64, 48: 3.88,
    49: 3.66, 50: 3.46, 51: 3.26, 52: 3.08, 53: 2.91, 54: 2.74, 55: 2.32, 56: 2.19,
    57: 2.06, 58: 1.95, 59: 1.84, 60: 1.74, 61: 1.64, 62: 2.91, 63: 2.74, 64: 2.59,
    65: 2.44, 66: 2.31, 67: 2.18, 68: 2.06, 69: 2.30, 70: 2.17, 71: 2.05, 72: 1.94,
    73: 1.83, 74: 1.73, 75: 1.63, 76: 2.33, 77: 2.20, 78: 2.07, 79: 1.96, 80: 1.84,
    81: 1.73, 82: 1.63, 83: 1.54, 84: 1.46,
}
# Shown as tr('mode.<mode>'), tr('density.<density>'), tr('arranger.<arranger>').
MODES = ('auto', 'guitar', 'raw', 'full')
# What the window offers; 'full' (one complete layout, no comparison) stays for tools and the CLI.
MENU_MODES = ('auto', 'guitar', 'raw')
# Settings saved by earlier development builds: old modes become their successors.
OLD_MODES = {'duet': 'guitar', 'melody': 'guitar', 'full': 'auto'}
DENSITIES = ('conservative', 'balanced', 'complete')
ARRANGERS = ('model', 'joint', 'legacy')


@dataclass(frozen=True)
class Note:
    id: int
    pitch: int
    velocity: int
    start: float
    end: float
    tick: int
    end_tick: int
    beat: float
    beat_strength: float
    source: str
    channel: int

    @property
    def duration(self):
        return max(0.01, self.end - self.start)


@dataclass(frozen=True)
class Source:
    id: str
    track: int
    channel: int
    name: str
    count: int
    low: int
    high: int

    @property
    def label(self):
        return tr('source.drums' if self.channel == 9 else 'source.label', track=self.track+1,
                  channel=self.channel+1, name=self.name, count=self.count)


@dataclass
class Song:
    path: str
    notes: list[Note]
    sources: list[Source]
    duration: float
    ticks_per_beat: int
    warnings: list[str] = field(default_factory=list)
    # Tick-based meter map retained for model analysis; playback stays in seconds.
    meters: tuple[tuple[int, int, int], ...] = ((0, 4, 4),)
    # (tick, microseconds per quarter note), as in the MIDI file; for exports with bar lines.
    tempos: tuple[tuple[int, int], ...] = ((0, 500000),)
    # note id -> (string, fret) chosen by the score itself (an ml3czus sheet); 原样演奏 keeps it.
    fingering: dict[int, tuple[int, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    mode: Literal['auto', 'guitar', 'raw', 'full'] = 'auto'
    density: Literal['conservative', 'balanced', 'complete'] = 'balanced'
    # None means automatic. Manual values are octaves, not semitones.
    octave: int | None = None
    max_voices: int | None = None
    simplify: float = 0.0
    tuning_offset: int = 1
    sources: tuple[str, ...] | None = None
    melody_source: str | None = None
    bass_source: str | None = None
    include_drums: bool = False
    chord_window: float = 0.002
    hold: float = 0.025
    gap: float = 0.0
    # Optional user preference, not an inferred game cooldown.
    repeat_interval: float = 0.0
    max_delay: float = 0.080
    # Keep a same-pitch re-press out of the moment the previous note ends by itself,
    # where the game silences that pitch (engine.avoid_end_retrigger). UI: 改善吞音现象.
    end_guard: bool = True
    speed: float = 1.0
    arranger: Literal['legacy', 'joint', 'model'] = 'model'

    def __post_init__(self):
        for name in ('octave','max_voices','tuning_offset'):
            value=getattr(self,name)
            if value is not None and type(value) is not int:
                raise ValueError(tr('error.config.integer', name=name))
        if self.sources is not None and (not isinstance(self.sources,tuple) or
                any(not isinstance(s,str) for s in self.sources)):
            raise ValueError(tr('error.config.sources'))
        if self.arranger not in ARRANGERS:
            raise ValueError(tr('error.config.arranger'))
        if self.mode not in MODES or self.density not in DENSITIES:
            raise ValueError(tr('error.config.mode'))
        if self.max_voices is not None and not 1 <= self.max_voices <= 128:
            raise ValueError(tr('error.config.max_voices'))
        if not 0 <= self.simplify <= 1:
            raise ValueError(tr('error.config.simplify'))
        if type(self.tuning_offset) is not int or not -12 <= self.tuning_offset <= 12:
            raise ValueError(tr('error.config.tuning'))
        if not 0 <= self.repeat_interval <= .250:
            raise ValueError(tr('error.config.repeat'))
        if self.octave is not None and not -5 <= self.octave <= 5:
            raise ValueError(tr('error.config.octave'))
        if not 0.005 <= self.hold <= 0.100:
            raise ValueError(tr('error.config.hold'))
        if not 0 <= self.gap <= 0.050:
            raise ValueError(tr('error.config.gap'))
        if not 0 <= self.max_delay <= 0.250 or not 0.25 <= self.speed <= 4:
            raise ValueError(tr('error.config.delay_speed'))
        if type(self.end_guard) is not bool:
            raise ValueError(tr('error.config.end_guard'))
        if not 0 <= self.chord_window <= 0.030:
            raise ValueError(tr('error.config.chord_window'))

    @property
    def tuning(self):
        return tuple(p+self.tuning_offset for p in TUNING)

    @property
    def bounds(self):
        return self.tuning[0], self.tuning[-1]+19


@dataclass(frozen=True)
class VoiceNote:
    note: Note
    pitch: int
    role: str
    weight: float


@dataclass(frozen=True)
class Fingering:
    voice: VoiceNote
    string: int
    fret: int


@dataclass(frozen=True)
class Strike:
    # time is the string's key-down time, not fret preparation time.
    time: float
    intended: float
    notes: tuple[Fingering, ...]
    hold: float


@dataclass
class Arrangement:
    song: Song
    config: Config
    strikes: list[Strike]
    selected: list[Note]
    melody_ids: set[int]
    bass_ids: set[int]
    dropped: dict[int, str]
    warnings: list[str]
    stats: dict

    @property
    def duration(self):
        last = max((s.time + s.hold for s in self.strikes), default=0)
        return max(self.song.duration / self.config.speed, last)

    def report(self):
        return {'file': self.song.path, 'mode': self.config.mode,
                'stats': self.stats, 'warnings': self.warnings}
