"""Exercise installed wheels in fresh environments using fictitious books only.

The optional previous wheel must be an explicitly selected, trusted build. All
files, databases, backups and environments are disposable; no user config loads.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import venv


def snapshot(root):
    return {p.relative_to(root).as_posix(): [hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mtime_ns, p.stat().st_mode & 0o777]
            for p in root.rglob('*') if p.is_file()}


def phase(name, root):
    # Imported only in the clean environment, from the installed wheel.
    from manga_kit import __version__, create_app
    from manga_kit.config import ConfigError, load_settings
    from manga_kit.library import LibraryService
    from manga_kit.operations import OperationService
    from manga_kit.sample import create_sample_library
    from manga_kit.state import StateConflict, StateStore

    config = root / 'config.yaml'
    if name == 'seed':
        config.write_text(json.dumps({'app': {'demo': False},
            'library': {'root': str(root / 'library')}, 'data': {'root': str(root / 'state')},
            'execution': {'enabled': True}}))
    settings = load_settings(config)
    store = StateStore(settings)
    service = OperationService(settings, store)
    library = LibraryService(settings, store=store)
    evidence = root / 'evidence.json'
    if name == 'seed':
        create_sample_library(settings.library_root, settings.layout)
        entry = library.scan().series[0]
        provenance = {'provider': 'demo', 'item_id': 'release-fixture',
                      'url': 'https://example.invalid/book', 'retrieved_at': '2026-10-16T00:00:00Z',
                      'fields': ['summary']}
        store.save_edit(entry.id, {'summary': '公開前の架空編集', 'tags': ['架空タグ']}, 0,
                        {'release_fixture': True}, provenance=provenance)
        with store.connection() as conn:
            conn.execute('INSERT INTO metadata_searches VALUES(?,?,?,?,?,?)',
                ('release-fixture', store.namespace, entry.id, 'fixture-query', time.time()+3600,
                 json.dumps({'provider': 'demo', 'candidates': []})))
        original = snapshot(settings.library_root)
        operation = service.prepare(selected=[entry.id])
        service.execute(operation['plan']['id'], operation['digest'])
        saved = {'version_before': __version__, 'original': original,
                 'operation': operation['plan']['id'], 'digest': operation['digest'],
                 'edits': store.edits(), 'jobs': [job['id'] for job in store.jobs()],
                 'events': store.events(), 'applied': snapshot(settings.library_root)}
        evidence.write_text(json.dumps(saved, ensure_ascii=False))
    else:
        saved = json.loads(evidence.read_text())
        assert store.edits() == saved['edits'], 'Edits or provenance lost'
        assert {job['id'] for job in store.jobs()} >= set(saved['jobs']), 'History lost'
        assert store.events() == saved['events'], 'Events lost'
        with store.connection() as conn:
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert conn.execute('SELECT COUNT(*) FROM metadata_searches WHERE namespace=?',
                                (store.namespace,)).fetchone()[0] == 1
        assert snapshot(settings.library_root) == saved['applied'], 'File data, time or mode changed'
        assert service.inspect(saved['operation'])['status'] == 'completed'
        if name == 'upgraded':
            service.restore(saved['operation'], saved['digest'])
            assert snapshot(settings.library_root) == saved['original']
            entry = next(s for s in library.scan(refresh=True).series if s.summary == '公開前の架空編集')
            operation = service.prepare(selected=[entry.id])
            service.execute(operation['plan']['id'], operation['digest'])
            service.restore(operation['plan']['id'], operation['digest'])
            assert snapshot(settings.library_root) == saved['original']
        elif name == 'recovered':
            # An old manifest must never be rebound to a newly created root/inode.
            try:
                service.restore(saved['operation'], saved['digest'])
            except StateConflict:
                pass
            else:
                raise AssertionError('Old plan accepted after physical root replacement')
    app = create_app(settings=settings)
    try:
        client = app.test_client()
        assert client.get('/healthz').status_code == 200
        assert client.get('/api/library').json['stats']['books'] == 22
        assert client.post('/api/organize/execute', json={}).status_code == 404
    finally:
        app.extensions['jobs'].shutdown()
    # Defaults continue to disallow CLI writes and external providers.
    from manga_kit.config import Settings
    defaults = Settings(library_root=root / 'empty', data_root=root / 'default-state')
    assert defaults.demo and not defaults.execution_enabled
    assert not defaults.rakuten.enabled and not defaults.anilist.enabled
    try:
        OperationService(defaults).prepare()
    except ConfigError:
        pass
    else:
        raise AssertionError('Default execution enabled')
    print(json.dumps({'phase': name, 'version': __version__, 'books': 22,
                      'integrity': 'ok'}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path)
    parser.add_argument('--previous-wheel', type=Path)
    parser.add_argument('--output', type=Path, default=Path('release-smoke.json'))
    parser.add_argument('--phase', choices=['seed', 'upgraded', 'recovered'], help=argparse.SUPPRESS)
    parser.add_argument('--root', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.phase:
        phase(args.phase, args.root)
        return
    if not args.wheel:
        parser.error('--wheel is required')
    wheel = args.wheel.resolve(strict=True)
    previous = args.previous_wheel.resolve(strict=True) if args.previous_wheel else None
    script = Path(__file__).resolve()
    lock = script.parents[1] / 'requirements.lock'
    env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'
           and not key.startswith(('MANGA_KIT_', 'KOMGA_', 'RAKUTEN_', 'ANILIST_'))}
    report = {'wheel': wheel.name, 'previous_wheel': previous.name if previous else None, 'checks': []}
    with tempfile.TemporaryDirectory(prefix='manga-kit-release-smoke-') as directory:
        work = Path(directory)

        def command(argv):
            result = subprocess.run([str(value) for value in argv], cwd=work, env=env,
                                    text=True, capture_output=True)
            if result.returncode:
                raise RuntimeError(f'{argv[0]} failed:\n{result.stdout}\n{result.stderr}')
            return result.stdout

        def install(name, artifact):
            location = work / name
            venv.EnvBuilder(with_pip=True).create(location)
            python = location / 'bin/python'
            command([python, '-m', 'pip', 'install', '-r', lock])
            command([python, '-m', 'pip', 'install', '--no-deps', artifact])
            command([python, '-m', 'pip', 'check'])
            return python

        def check(python, name, root):
            output = command([python, script, '--phase', name, '--root', root])
            value = json.loads(output.strip().splitlines()[-1])
            report['checks'].append(value)
            print(json.dumps(value), flush=True)

        fresh = install('fresh-venv', wheel)
        fresh_root = work / 'fresh'
        fresh_root.mkdir()
        check(fresh, 'seed', fresh_root)
        check(fresh, 'upgraded', fresh_root)
        if previous:
            upgrade = install('upgrade-venv', previous)
            upgrade_root = work / 'upgrade'
            upgrade_root.mkdir()
            check(upgrade, 'seed', upgrade_root)
            # No app processes are running. Keep config, whole state and library together.
            backup = work / 'backup'
            shutil.copytree(upgrade_root, backup, copy_function=shutil.copy2)
            command([upgrade, '-m', 'pip', 'install', '--no-deps', '--upgrade', wheel])
            command([upgrade, '-m', 'pip', 'check'])
            check(upgrade, 'upgraded', upgrade_root)
            displaced = work / 'displaced'
            upgrade_root.rename(displaced)  # Retain old inode to make replacement deterministic.
            shutil.copytree(backup, upgrade_root, copy_function=shutil.copy2)
            check(upgrade, 'recovered', upgrade_root)
            # Roll back the application together with the pre-update data snapshot.
            old = work / 'rollback-venv'
            old_python = install(old.name, previous)
            check(old_python, 'recovered', upgrade_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(f'Passed; report: {args.output}')


if __name__ == '__main__':
    main()
