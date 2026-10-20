"""A tiny SoundFont 2 file built in code, so tests need no third-party sound bank.

One looped 441 Hz sine sample (root key 69), one instrument, and presets
0:0 'Test Piano' and 0:25 'Test Guitar' that both use it.
"""
import math
import struct

RATE = 44100
PERIOD = 100                 # samples per cycle: 441 Hz at 44.1 kHz
LENGTH = RATE//2             # 0.5 s of sample data
LOOP = (LENGTH-50*PERIOD, LENGTH)


def chunk(tag, data):
    data = bytes(data)
    return tag+struct.pack('<I', len(data))+data+(b'\0' if len(data) % 2 else b'')


def listed(kind, *chunks):
    return chunk(b'LIST', kind+b''.join(chunks))


def name(text):
    return text.encode('ascii')[:19].ljust(20, b'\0')


def build(presets=((0, 0, 'Test Piano'), (0, 25, 'Test Guitar')), release_timecents=-1200, amplitude=12000):
    wave = [round(amplitude*math.sin(2*math.pi*i/PERIOD)) for i in range(LENGTH)]
    smpl = struct.pack(f'<{LENGTH+46}h', *(wave+[0]*46))
    phdr = b''.join(name(n)+struct.pack('<HHHIII', preset, bank, i, 0, 0, 0)
                    for i, (bank, preset, n) in enumerate(presets))
    phdr += name('EOP')+struct.pack('<HHHIII', 0, 0, len(presets), 0, 0, 0)
    pbag = b''.join(struct.pack('<HH', i, 0) for i in range(len(presets)+1))
    pgen = b''.join(struct.pack('<HH', 41, 0) for _ in presets)+struct.pack('<HH', 0, 0)
    inst = name('Test Sine')+struct.pack('<H', 0)+name('EOI')+struct.pack('<H', 1)
    ibag = struct.pack('<HH', 0, 0)+struct.pack('<HH', 3, 0)
    igen = (struct.pack('<Hh', 38, release_timecents)      # releaseVolEnv
            + struct.pack('<HH', 54, 1)                   # sampleModes: loop continuously
            + struct.pack('<HH', 53, 0)                   # sampleID (last)
            + struct.pack('<HH', 0, 0))
    shdr = (name('Sine')+struct.pack('<IIIIIBbHH', 0, LENGTH, LOOP[0], LOOP[1], RATE, 69, 0, 0, 1)
            + name('EOS')+struct.pack('<IIIIIBbHH', 0, 0, 0, 0, 0, 0, 0, 0, 0))
    body = b'sfbk'+listed(b'INFO', chunk(b'ifil', struct.pack('<HH', 2, 1)), chunk(b'isng', b'EMU8000\0'),
                          chunk(b'INAM', b'Test bank\0'))
    body += listed(b'sdta', chunk(b'smpl', smpl))
    body += listed(b'pdta', chunk(b'phdr', phdr), chunk(b'pbag', pbag), chunk(b'pmod', bytes(10)),
                   chunk(b'pgen', pgen), chunk(b'inst', inst), chunk(b'ibag', ibag), chunk(b'imod', bytes(10)),
                   chunk(b'igen', igen), chunk(b'shdr', shdr))
    return chunk(b'RIFF', body)


def write(path, **kwargs):
    with open(path, 'wb') as stream:
        stream.write(build(**kwargs))
    return path
