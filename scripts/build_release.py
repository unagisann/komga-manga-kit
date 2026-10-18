"""Build reviewable artifacts and reject runtime data or private material."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile

ROOT=Path(__file__).resolve().parents[1]
ROOT_FILES={'LICENSE','README.md','README.en.md','THIRD_PARTY_NOTICES.md','requirements.lock','pyproject.toml',
    'MANIFEST.in','config.example.yaml','compose.yaml','compose.local.example.yaml','.env.example',
    'Dockerfile','.dockerignore','.gitignore','PKG-INFO','setup.cfg'}
SOURCE_DIRS={'src','tests','scripts','docs','licenses','.github'}
BANNED_EXTENSIONS={'.cbz','.zip','.epub','.rar','.cbr','.7z','.db','.sqlite','.sqlite3','.pyc'}
PRIVATE=re.compile(r'/(?:home|Users|srv)/[^/\s]+/|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[A-Z0-9]{16}')


def inspect_members(members, *, source=False):
    count=0
    for name,data in members:
        path=PurePosixPath(name)
        if source:
            path=PurePosixPath(*path.parts[1:])
        if not str(path) or path.is_absolute() or '..' in path.parts:
            raise ValueError(f'Unsafe artifact path: {name}')
        if path.suffix.lower() in BANNED_EXTENSIONS or any(part in {'.env','config.yaml','compose.local.yaml','.venv','.git','var','library','__pycache__'} for part in path.parts):
            raise ValueError(f'Runtime/private file in artifact: {name}')
        if source and not (path.parts[0] in SOURCE_DIRS or path.as_posix() in ROOT_FILES):
            raise ValueError(f'Unexpected source artifact file: {name}')
        if not source and not (path.parts[0]=='manga_kit' or path.parts[0].endswith('.dist-info')):
            raise ValueError(f'Unexpected wheel file: {name}')
        if len(data)>4*1024*1024:
            raise ValueError(f'Oversized artifact member: {name}')
        if PRIVATE.search(data.decode('utf-8',errors='replace')):
            raise ValueError(f'Private path/token in artifact: {name}')
        count+=1
    return count


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'dist')
    args=parser.parse_args()
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    subprocess.run([sys.executable,'-m','build','--outdir',str(output)],cwd=ROOT,check=True)
    version=tomllib.loads((ROOT/'pyproject.toml').read_text())['project']['version']
    artifacts=[output/f'komga_manga_kit-{version}-py3-none-any.whl',output/f'komga_manga_kit-{version}.tar.gz']
    report={'version':version,'artifacts':[]}
    for artifact in artifacts:
        if artifact.suffix=='.whl':
            with zipfile.ZipFile(artifact) as archive:
                names=archive.namelist()
                for required in ('manga_kit/komga_authors.py', 'manga_kit/authors.py',
                                 'manga_kit/templates/roadmap.html', 'manga_kit/static/app.js'):
                    if required not in names:
                        raise ValueError(f'Missing wheel member: {required}')
                for required in ('THIRD_PARTY_NOTICES.md', 'licenses/MPL-2.0.txt',
                                 'licenses/CC0-1.0.txt', 'licenses/xxhash-library-LICENSE.txt'):
                    if not any(name.endswith('/licenses/' + required) for name in names):
                        raise ValueError(f'Missing wheel license: {required}')
                count=inspect_members((name,archive.read(name)) for name in names if not name.endswith('/'))
        else:
            with tarfile.open(artifact,'r:gz') as archive:
                members=archive.getmembers()
                if any(item.issym() or item.islnk() for item in members):
                    raise ValueError('Source artifact contains a link')
                names={PurePosixPath(*PurePosixPath(item.name).parts[1:]).as_posix() for item in members}
                for required in ('.env.example', 'compose.local.example.yaml', 'config.example.yaml',
                                 'docs/OPERATIONS.md', 'docs/SUPPORT.md', 'docs/AUTHORS.md',
                                 'docs/RELEASE_NOTES.md', 'scripts/release_smoke.py', 'requirements.lock'):
                    if required not in names:
                        raise ValueError(f'Missing source member: {required}')
                count=inspect_members(((item.name,archive.extractfile(item).read()) for item in members if item.isfile()),source=True)
        report['artifacts'].append({'file':artifact.name,'sha256':hashlib.sha256(artifact.read_bytes()).hexdigest(),
                                    'bytes':artifact.stat().st_size,'members':count})
    (output/'release-manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    (output/'SHA256SUMS').write_text(''.join(f"{item['sha256']}  {item['file']}\n" for item in report['artifacts']))
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
