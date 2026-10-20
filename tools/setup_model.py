"""Install/check the local model without executing downloaded repository code."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
FILES={
 'model_best.ckpt':('https://media.githubusercontent.com/media/york135/M2BERT_files/main/M2BERT_pretrained_model/model_best.ckpt',
                    'cf15015f8ed1bdf94983812ffb3229e51548b676dc5ceda76b91a09056b40f63'),
 'CP.pkl':('https://raw.githubusercontent.com/york135/M2BERT/main/data_creation/prepare_data/dict/CP.pkl',
           'a84a4aeb14201af952acffa46d7dd04f8780a398d76af043f2f057a5c431885d'),
 'config.json':('https://huggingface.co/answerdotai/ModernBERT-base/resolve/main/config.json',
                '1609d59e627c33eaed524b4f01e546d42e84190a079a5a5ded84b212c41c324f')}


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def download(url,target,expected):
    """Only replace a model after the complete download passes its checksum."""
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    temporary=None
    try:
        digest=hashlib.sha256();size=0;last=0
        with urllib.request.urlopen(url,timeout=90) as source, tempfile.NamedTemporaryFile(
                'wb',dir=target.parent,delete=False,suffix='.part') as output:
            temporary=Path(output.name)
            while block:=source.read(1024*1024):
                output.write(block);digest.update(block);size+=len(block)
                if size-last>=16*1024*1024:
                    print(f'  {size//(1024*1024)} MiB',flush=True);last=size
        if digest.hexdigest()!=expected:raise ValueError('Checksum mismatch: '+target.name)
        os.replace(temporary,target)
    finally:
        if temporary:temporary.unlink(missing_ok=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--install',action='store_true')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--report',help='Optional local installation report')
    args=parser.parse_args();folder=ROOT/'models'/'m2bert'
    if args.install:
        # PyTorch (CPU) and Transformers only convert the official checkpoint to ONNX here;
        # the player itself runs the models with ONNX Runtime (requirements-model.txt).
        subprocess.run([sys.executable,'-m','pip','install','torch==2.8.0','--index-url','https://download.pytorch.org/whl/cpu'],check=True)
        subprocess.run([sys.executable,'-m','pip','install','-r',str(ROOT/'requirements.txt'),'-r',str(ROOT/'requirements-model.txt'),
                        '-r',str(ROOT/'requirements-export.txt')],check=True)
    for name,(url,expected) in FILES.items():
        target=folder/name
        if target.exists() and sha(target)==expected:continue
        if args.check:raise SystemExit(f'Missing or invalid: {name}')
        print('Downloading',name,flush=True)
        download(url,target,expected)
    for name in ('voice_edges.onnx','voice_edges.json','voice_roles.onnx','voice_roles.json'):
        if not (folder/name).is_file():raise SystemExit('Missing project-trained voice head: '+name)
    if not (folder/'encoder.onnx').is_file():
        if args.check:raise SystemExit('Missing: encoder.onnx (run tools/setup_model.py --install)')
        subprocess.run([sys.executable,str(ROOT/'tools'/'export_onnx.py'),'--check'],check=True)
    from guitar_player.music_model import OnnxFeatures
    from guitar_player.voice_edges import edge_session
    from guitar_player.roles import role_session
    OnnxFeatures();_,_,head_hash=edge_session(str(folder/'voice_edges.onnx'));role_session(str(folder/'voice_roles.onnx'))
    result=dict(weights={name:sha(folder/name) for name in FILES},encoder_onnx_sha256=sha(folder/'encoder.onnx'),
                head_sha256=head_hash,role_head_sha256=sha(folder/'voice_roles.onnx'),
                packages={name:importlib.metadata.version(name) for name in ('onnxruntime','numpy','ortools')})
    if args.report:
        target=Path(args.report);target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,RuntimeError,ImportError,subprocess.CalledProcessError) as exc:
        raise SystemExit('Model setup failed; rerun after resolving the error: '+str(exc))
