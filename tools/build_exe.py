"""Build the Windows release: a portable folder with SloppyPlayer.exe, zipped.

Run from the repository root in a build environment that has PyInstaller and ONNX
Runtime, with the exported model files in models/m2bert/ (tools/setup_model.py --install;
see README, Build from source):

  python tools/build_exe.py [--documents <folder with README.md, LICENSE, ...>]

Writes dist/SloppyPlayer-<version>-win64.zip. The Qt libraries stay separate DLLs in
the folder, so they can be replaced (LGPL). The license texts of every bundled
package are copied to licenses/.
"""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from guitar_player import __version__  # noqa: E402

MODEL_FILES = ('encoder.onnx', 'CP.pkl', 'config.json', 'voice_edges.onnx', 'voice_edges.json', 'voice_roles.onnx',
               'voice_roles.json')
# Not used by the player; keep them out even when installed in the build environment.
# The models run on ONNX Runtime; PyTorch and Transformers are only for exporting and training.
EXCLUDED = ('torch', 'transformers', 'safetensors', 'tokenizers', 'huggingface_hub', 'sympy', 'networkx', 'onnx',
            'scipy', 'pyaudio', 'tkinter', 'matplotlib', 'IPython', 'pytest', 'lxml', 'soundcard', 'PySide6.QtNetwork',
            'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtPdf', 'PySide6.QtOpenGL')
# The READMEs are for the repository page (their images are not in the zip).
DOCUMENTS = ('LICENSE', 'THIRD_PARTY.md', 'USER_GUIDE.md', 'USER_GUIDE.zh-CN.md', 'CHANGELOG.md')


def write_icon(path, app=None):
    """A Windows .ico with the logo at 16-256 px, each stored as PNG."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtWidgets import QApplication
    from guitar_player.qt.widgets import app_icon
    app = app or QApplication.instance() or QApplication([])   # pixmaps need an application
    icon, images = app_icon(), []
    for size in (16, 24, 32, 48, 64, 128, 256):
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        icon.pixmap(size, size).save(buffer, 'PNG')
        images.append((size, bytes(data)))
    header = struct.pack('<HHH', 0, 1, len(images))
    offset, entries = 6+16*len(images), b''
    for size, png in images:
        entries += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    path.write_bytes(header+entries+b''.join(png for _, png in images))


def bundled_distributions(internal, pyz_toc):
    """Installed distributions with any module in the build: a folder in _internal or a
    module in the PYZ archive (pure-Python packages)."""
    present = {p.name.split('.')[0].lower() for p in internal.iterdir()}
    present |= {m.split('.')[0].lower() for m in re.findall(r"\('([\w.]+)',", pyz_toc.read_text(encoding='utf-8'))}
    found = []
    for dist in importlib.metadata.distributions():
        tops = (dist.read_text('top_level.txt') or '').split()
        tops = tops or {Path(str(f)).parts[0].removesuffix('.py') for f in dist.files or ()
                        if str(f).endswith('.py') and '.dist-info' not in str(f)}
        if any(t.lower() in present for t in tops):
            found.append(dist)
    return sorted(found, key=lambda d: d.metadata['Name'].lower())


def license_of(dist):
    m = dist.metadata
    classifiers = [c.split('::')[-1].strip() for c in m.get_all('Classifier') or () if c.startswith('License')]
    return m.get('License-Expression') or ', '.join(classifiers) or (m.get('License') or '').partition('\n')[0][:80]


def copy_licenses(dists, target):
    """licenses/<name-version>/ with each package's license files, and an index."""
    target.mkdir()
    index = ['Packages in this build, their licenses, and the license files they ship.', '']
    for dist in dists:
        name = f"{dist.metadata['Name']}-{dist.version}"
        index.append(f'{name}: {license_of(dist)}')
        for f in dist.files or ():
            if any(k in f.name.upper() for k in ('LICENSE', 'LICENCE', 'COPYING', 'NOTICE', 'AUTHORS')):
                source = Path(dist.locate_file(f))
                if source.is_file():
                    (target/name).mkdir(exist_ok=True)
                    shutil.copy(source, target/name/source.name)
    index += ['', 'Python 3.12: PSF License (python312.dll and the standard library).',
              'Qt 6 (in PySide6): LGPL-3.0; the Qt DLLs in _internal/PySide6 can be replaced.',
              'PyInstaller bootloader (SloppyPlayer.exe): GPL-2.0 with the bootloader exception.']
    (target/'README.txt').write_text('\n'.join(index)+'\n', encoding='utf-8')


def self_check(exe, work):
    """Run the built program on a score wider than the guitar and require the model to take part."""
    import mido
    song = mido.MidiFile()
    track = mido.MidiTrack()
    song.tracks.append(track)
    for k in range(48):
        pitch = 30+(k*7) % 66                 # 30..95: needs octave moves, so the model runs
        track += [mido.Message('note_on', note=pitch, velocity=80, time=0 if k == 0 else 120),
                  mido.Message('note_off', note=pitch, velocity=0, time=240)]
    song.save(work/'check.mid')
    env = dict(os.environ, APPDATA=str(work/'appdata'), LOCALAPPDATA=str(work/'localappdata'))
    subprocess.run([str(exe), '--analyze', str(work/'check.mid'), '--report', str(work/'check.json')], env=env, check=True)
    stats = json.loads((work/'check.json').read_text(encoding='utf-8'))['stats']
    joint = stats.get('joint') or {}
    if not joint.get('model_used'):
        raise SystemExit(f"The built program arranged without the model: {joint.get('fallback_reason')}")
    print('self-check: the model ran in the built program')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=ROOT/'dist')
    parser.add_argument('--documents', type=Path, default=ROOT)
    args = parser.parse_args()
    import PyInstaller.__main__
    name = f'SloppyPlayer-{__version__}-win64'
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        write_icon(work/'icon.ico')
        options = ['--noconfirm', '--clean', '--onedir', '--windowed', '--name', 'SloppyPlayer',
                   '--icon', str(work/'icon.ico'), '--distpath', str(work/'dist'), '--workpath', str(work/'build'),
                   '--specpath', str(work), '--paths', str(ROOT), '--collect-binaries', 'ortools',
                   '--add-data', f"{ROOT/'guitar_player'/'locales'};guitar_player/locales",
                   '--add-data', f"{ROOT/'guitar_player'/'qt'/'logo.svg'};guitar_player/qt"]
        for module in EXCLUDED:
            options += ['--exclude-module', module]
        for file in MODEL_FILES:
            options += ['--add-data', f"{ROOT/'models'/'m2bert'/file};models/m2bert"]
        # Another Python's CUDA libraries on PATH would be collected too; the CPU build needs none.
        os.environ['PATH'] = os.pathsep.join(p for p in os.environ['PATH'].split(os.pathsep)
                                             if 'nvidia' not in p.lower() and 'cuda' not in p.lower())
        PyInstaller.__main__.run(options + [str(ROOT/'launch.py')])
        folder = work/'dist'/'SloppyPlayer'
        self_check(folder/'SloppyPlayer.exe', work)
        copy_licenses(bundled_distributions(folder/'_internal', work/'build'/'SloppyPlayer'/'PYZ-00.toc'), folder/'licenses')
        for document in DOCUMENTS:
            if (args.documents/document).is_file():
                shutil.copy(args.documents/document, folder/document)
        args.out.mkdir(parents=True, exist_ok=True)
        archive = args.out/f'{name}.zip'
        with ZipFile(archive, 'w', ZIP_DEFLATED, compresslevel=9) as z:
            for path in sorted(folder.rglob('*')):
                if path.is_file():
                    z.write(path, f'{name}/{path.relative_to(folder).as_posix()}')
        size = sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())
        print(f'{archive.name}: {archive.stat().st_size/2**20:.0f} MiB zipped, {size/2**20:.0f} MiB unpacked')


if __name__ == '__main__':
    main()
