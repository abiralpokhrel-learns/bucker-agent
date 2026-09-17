"""Build a clean embeddable runtime; never copies a user profile or venv."""
import argparse
import hashlib
import json
import shutil
import subprocess
import tomllib
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'desktop/build/runtime'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-source', required=True)
    args = parser.parse_args()
    source = Path(args.hermes_source)
    meta = tomllib.loads((source / 'pyproject.toml').read_text(encoding='utf-8'))
    OUT.mkdir(parents=True, exist_ok=True)
    archive = OUT.parent / 'python-3.12.10-embed-amd64.zip'
    if not archive.exists() or not zipfile.is_zipfile(archive):
        subprocess.run(['curl', '--fail', '--location', '--max-time', '120', '--output', str(archive), 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip'], check=True)
    py = OUT / 'python'
    py.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        z.extractall(py)
    (py / 'python312._pth').write_text('python312.zip\n.\nLib/site-packages\n../hermes\n../gateway\nimport site\n', encoding='utf-8')
    deps = meta['project']['dependencies'] + meta['project']['optional-dependencies']['acp'] + ['jsonschema>=4.22', 'pip']
    req = OUT.parent / 'runtime-requirements.txt'
    req.write_text('\n'.join(deps), encoding='utf-8')
    subprocess.run(['uv', 'pip', 'install', '--python', str(py / 'python.exe'), '--target', str(py / 'Lib/site-packages'), '-r', str(req)], check=True)
    hermes = OUT / 'hermes'
    hermes.mkdir(exist_ok=True)
    # Explicit source/data allowlist. Never traverse the Hermes home directory.
    for name in ['agent','tools','hermes_cli','gateway','tui_gateway','cron','acp_adapter','plugins','providers']:
        shutil.copytree(source / name, hermes / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__','.env*','*.pyc','node_modules','*.log','auth.json','config.yaml'))
    for name in meta['tool']['setuptools']['py-modules']:
        shutil.copy2(source / (name + '.py'), hermes)
    for name in ['LICENSE','pyproject.toml']:
        shutil.copy2(source / name, hermes)
    gateway = OUT / 'gateway/bucker'
    gateway.mkdir(parents=True, exist_ok=True)
    for name in ['gateway','desktop_gateway']:
        shutil.copytree(ROOT / 'bucker' / name, gateway / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    for name in ['__init__.py','config.py']:
        shutil.copy2(ROOT / 'bucker' / name, gateway / name)
    shutil.copy2(ROOT / 'LICENSE', OUT / 'BUCKER-LICENSE')
    shutil.copy2(Path(__file__).with_name('launch.py'), OUT / 'launch.py')
    packages = subprocess.check_output([str(py/'python.exe'), '-c', 'import importlib.metadata,json; print(json.dumps(sorted((d.metadata["Name"],d.version) for d in importlib.metadata.distributions())))'], text=True)
    (OUT/'inventory.json').write_text(json.dumps({'python':'3.12.10','python_zip_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'hermes':meta['project']['version'],'packages':json.loads(packages)},indent=2),encoding='utf-8')
    print('Runtime ready:', OUT)


if __name__ == '__main__':
    main()
