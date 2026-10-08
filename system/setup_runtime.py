"""Install this project's verified Windows x64 runtime, without changing system Python.

Run with an existing Python that has pip: py -3 system/setup_runtime.py
Archives are pinned and checksum-verified against official sources.
"""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import urllib.request
import zipfile

SYSTEM=Path(__file__).resolve().parent
ROOT=SYSTEM.parent
PACKAGES=[
 ('https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip','sha256','d297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15'),
 ('https://www.sqlite.org/2026/sqlite-dll-win-x64-3530400.zip','sha3_256','deddee963c810d1eeac3ce5e15c7c41da21a1c54d7a39cf54fbf577d2f50de3a'),
]


def checked_download(url, algorithm, expected):
    target=ROOT/'cache'/url.rsplit('/',1)[-1]
    target.parent.mkdir(parents=True,exist_ok=True)
    if not target.exists() or hashlib.new(algorithm,target.read_bytes()).hexdigest()!=expected:
        with urllib.request.urlopen(url,timeout=60) as response:
            data=response.read()
        if hashlib.new(algorithm,data).hexdigest()!=expected:
            raise RuntimeError('Official archive checksum mismatch: '+url)
        target.write_bytes(data)
    return target


def main():
    if sys.platform!='win32': raise SystemExit('This bootstrap is Windows x64 only; other systems use Python + patched SQLite + requirements.txt.')
    runtime=SYSTEM/'runtime/python'
    runtime.mkdir(parents=True,exist_ok=True)
    python_package=checked_download(*PACKAGES[0])
    with zipfile.ZipFile(python_package) as z: z.extractall(runtime)
    sqlite_package=checked_download(*PACKAGES[1])
    with zipfile.ZipFile(sqlite_package) as z:
        member=next(n for n in z.namelist() if n.lower().endswith('sqlite3.dll'))
        (runtime/'sqlite3.dll').write_bytes(z.read(member))
    (runtime/'python314._pth').write_text('python314.zip\n.\n..\\..\nLib/site-packages\nimport site\n',encoding='utf-8')
    executable=runtime/'python.exe'
    subprocess.run([str(executable),'-X','utf8','-c',"import sqlite3, personal_system; assert sqlite3.sqlite_version=='3.53.4'; c=sqlite3.connect(':memory:'); c.execute('create virtual table ft using fts5(a, tokenize=trigram)'); print('Python runtime + project imports + SQLite 3.53.4 + FTS5: OK')"],check=True)
    requirements=SYSTEM/'requirements.txt'
    if requirements.is_file():
        subprocess.run([sys.executable,'-m','pip','--python',str(executable),'install','--index-url','https://pypi.org/simple','--target',str(runtime/'Lib/site-packages'),'-r',str(requirements)],check=True)
    (runtime/'provenance.json').write_text(json.dumps({'sources':[{'url':u,'algorithm':a,'digest':h} for u,a,h in PACKAGES], 'python':'3.14.7','sqlite':'3.53.4','note':'Project-local official DLL replacement; system Python unchanged.'},indent=2)+'\n',encoding='utf-8')


if __name__=='__main__': main()
