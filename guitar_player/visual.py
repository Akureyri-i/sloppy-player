"""Toolkit-free note data for the score views: MIDI-derived, not audio.

VisualScore keeps notes sorted by start with a max-end interval tree, so a
time window (including long notes that began earlier) is found quickly.
"""
from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class VisualNote:
    start: float
    end: float
    pitch: int
    velocity: int
    lane: int = 0
    fret: int | None = None


class VisualScore:
    def __init__(self, notes=(), duration=0.):
        self.notes = sorted(notes, key=lambda n:n.start)
        self.duration = max(float(duration), max((n.end for n in self.notes),default=0.))
        self.starts = [n.start for n in self.notes]
        self.low = min((n.pitch for n in self.notes),default=41)
        self.high = max((n.pitch for n in self.notes),default=84)
        # Max-end interval tree: long sustained notes must not be missed by seek.
        self._size = 1 << max(0,(len(self.notes)-1).bit_length())
        self._ends = [0.] * (2*self._size)
        for i,n in enumerate(self.notes):self._ends[self._size+i]=n.end
        for i in range(self._size-1,0,-1):self._ends[i]=max(self._ends[2*i],self._ends[2*i+1])

    @classmethod
    def from_song(cls, song, speed=1., selected=None):
        lanes={s.id:i for i,s in enumerate(song.sources)}
        return cls((VisualNote(n.start/speed,n.end/speed,n.pitch,n.velocity,lanes.get(n.source,0))
                    for n in (song.notes if selected is None else selected)),song.duration/speed)

    @classmethod
    def from_arrangement(cls, arrangement):
        # Match reference_events: same string/pitch retriggers cut the old envelope.
        result=[];next_pitch={}
        duration=arrangement.duration
        for strike in reversed(arrangement.strikes):
            for finger in reversed(strike.notes):
                key=(finger.string,finger.voice.pitch)
                end=min(duration,strike.time+min(finger.voice.note.duration/arrangement.config.speed,3.))
                end=min(end,next_pitch.get(key,end))
                result.append(VisualNote(strike.time,max(end,strike.time+.001),finger.voice.pitch,
                                         finger.voice.note.velocity,finger.string,finger.fret))
                next_pitch[key]=strike.time
        return cls(result,duration)

    def between(self, left, right):
        """Notes overlapping [left,right), including notes starting before left."""
        limit=bisect_left(self.starts,right)
        result=[]
        def visit(node,lo,hi):
            if lo>=limit or self._ends[node]<=left:return
            if hi-lo==1:
                if lo<len(self.notes):result.append(self.notes[lo])
                return
            mid=(lo+hi)//2
            visit(2*node,lo,mid);visit(2*node+1,mid,hi)
        if limit:visit(1,0,self._size)
        return result

    def spectrum(self, position):
        energy=[0.] * 128
        for n in self.between(position,position+1e-8):
            if n.start<=position<n.end:energy[n.pitch]+=n.velocity*n.velocity
        return [math.sqrt(v) for v in energy]

    def envelope(self, bins):
        result=[0]*bins
        if self.duration<=0:return result
        for n in self.notes:
            i=min(bins-1,max(0,int(n.start/self.duration*bins)))
            result[i]=max(result[i],n.velocity)
        return result
