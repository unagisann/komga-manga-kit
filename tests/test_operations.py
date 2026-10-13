from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from manga_kit import create_app
from manga_kit.config import ConfigError, Settings, load_settings
from manga_kit.filesystem import LibraryFilesystem
from manga_kit.operations import OperationService, digest
from manga_kit.state import StateConflict, StateStore


@unittest.skipUnless(os.name == 'posix', 'Execution currently supports POSIX')
class OperationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.library = self.root / 'library'
        self.series_folder = self.library / '連載中' / '[作者] 作品'
        self.series_folder.mkdir(parents=True)
        for n in (1, 2):
            with zipfile.ZipFile(self.series_folder / f'原稿 第{n}巻.cbz', 'w') as out:
                out.writestr('ComicInfo.xml', f'<ComicInfo><Series>作品</Series><Writer>作者</Writer><Number>{n}</Number></ComicInfo>')
                out.writestr('0001.png', b'fictitious fixture')
        (self.series_folder / 'series.json').write_text(json.dumps({'reading': 'サクヒン', 'status': 'completed'}, ensure_ascii=False))
        self.settings = Settings(demo=False, library_root=self.library, data_root=self.root / 'state', execution_enabled=True)
        self.service = OperationService(self.settings)

    def files(self):
        return {p.relative_to(self.library).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns,
                   p.stat().st_mode & 0o777) for p in self.library.rglob('*') if p.is_file()}

    def prepare(self):
        op = self.service.prepare()
        return op, op['plan']['id'], op['digest']

    def execute(self):
        op, operation_id, confirmation = self.prepare()
        self.service.execute(operation_id, confirmation)
        return op, operation_id, confirmation

    def test_prepare_does_not_modify_library_and_disabled_demo_are_rejected(self):
        before = self.files()
        self.prepare()
        self.assertEqual(before, self.files())
        for settings in (replace(self.settings, execution_enabled=False), replace(self.settings, demo=True)):
            with self.assertRaises(ConfigError):
                OperationService(settings).prepare()

    def test_move_sidecar_edits_and_restore_preserve_every_byte_mode_and_time(self):
        before = self.files()
        entry = self.service.library.scan().series[0]
        self.service.store.save_edit(entry.id, {'author': '編集した著者'}, 0, {})
        op, operation_id, confirmation = self.execute()
        result = self.service.library.scan(refresh=True).series[0]
        self.assertEqual(result.author, '編集した著者')
        self.assertTrue(result.path.startswith('完結/さ/'))
        self.assertEqual(len(self.files()), 3)
        self.assertTrue(all((self.library / item['target']).exists() for item in op['plan']['entries']))
        self.service.restore(operation_id, confirmation)
        self.assertEqual(before, self.files())
        self.assertEqual(self.service.library.scan(refresh=True).series[0].author, '編集した著者')
        self.assertEqual(self.service.inspect(operation_id)['status'], 'restored')
        self.assertEqual([j['status'] for j in self.service.store.jobs()], ['succeeded', 'succeeded'])

    def test_confirmation_and_modified_manifest_are_rejected(self):
        before = self.files()
        op, operation_id, confirmation = self.prepare()
        with self.assertRaises(ConfigError):
            self.service.execute(operation_id, 'wrong')
        path = self.service._folder(operation_id) / 'plan.json'
        saved = json.loads(path.read_text())
        saved['entries'][0]['target'] = '../escape.cbz'
        path.write_text(json.dumps(saved))
        with self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def test_source_change_after_preview_is_rejected_before_any_move(self):
        op, operation_id, confirmation = self.prepare()
        source = self.library / op['plan']['entries'][0]['source']
        source.write_bytes(source.read_bytes() + b'changed')
        before = self.files()
        with self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def test_late_target_collision_is_never_overwritten(self):
        op, operation_id, confirmation = self.prepare()
        target = self.library / op['plan']['entries'][0]['target']
        target.parent.mkdir(parents=True)
        target.write_bytes(b'existing target')
        before = self.files()
        with self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def test_symlink_destination_cannot_escape_library(self):
        _, operation_id, confirmation = self.prepare()
        outside = self.root / 'outside'
        outside.mkdir()
        (self.library / '完結').symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, ConfigError, StateConflict)):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(len(list(self.series_folder.glob('*.cbz'))), 2)

    def test_source_replacement_with_same_bytes_is_rejected(self):
        op, operation_id, confirmation = self.prepare()
        source = self.library / op['plan']['entries'][0]['source']
        temporary = source.with_suffix('.temporary')
        shutil.copy2(source, temporary)
        os.replace(temporary, source)
        with self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)

    def test_root_replacement_is_rejected(self):
        _, operation_id, confirmation = self.prepare()
        old = self.root / 'old-library'
        self.library.rename(old)
        shutil.copytree(old, self.library)
        with self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)

    def test_partial_failure_can_be_restored_after_reopening_service(self):
        before = self.files()
        _, operation_id, confirmation = self.prepare()
        original, calls = LibraryFilesystem.move, []
        def fail_second(fs, source, target, expected):
            calls.append(source)
            if len(calls) == 2:
                raise OSError('simulated device failure')
            return original(fs, source, target, expected)
        with patch.object(LibraryFilesystem, 'move', new=fail_second), self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(self.service.inspect(operation_id)['status'], 'failed')
        reopened = OperationService(self.settings)
        reopened.restore(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def test_interrupted_link_unlink_step_can_be_recovered(self):
        before = self.files()
        _, operation_id, confirmation = self.prepare()
        def interrupt(fs, source, target, expected):
            with fs.parent(source) as (src_fd, src), fs.parent(target, create=True) as (dst_fd, dst):
                os.link(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
            raise KeyboardInterrupt()
        with patch.object(LibraryFilesystem, 'move', new=interrupt), self.assertRaises(KeyboardInterrupt):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(self.service.inspect(operation_id)['status'], 'running')
        OperationService(self.settings).restore(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def test_missing_destination_is_recovered_from_verified_backup(self):
        before = self.files()
        op, operation_id, confirmation = self.execute()
        (self.library / op['plan']['entries'][0]['target']).unlink()
        self.service.restore(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def test_restore_can_resume_after_backup_recovery_was_published(self):
        before = self.files()
        op, operation_id, confirmation = self.execute()
        (self.library / op['plan']['entries'][0]['target']).unlink()
        original = LibraryFilesystem.recover_backup
        def interrupt(fs, *args, **kwargs):
            original(fs, *args, **kwargs)
            raise KeyboardInterrupt()
        with patch.object(LibraryFilesystem, 'recover_backup', new=interrupt), self.assertRaises(KeyboardInterrupt):
            self.service.restore(operation_id, confirmation)
        self.assertEqual(self.service.inspect(operation_id)['status'], 'restoring')
        OperationService(self.settings).restore(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def test_corrupt_backup_or_changed_destination_prevents_all_restore_writes(self):
        op, operation_id, confirmation = self.execute()
        backup = self.service._folder(operation_id) / 'backup' / '0'
        original = backup.read_bytes()
        backup.write_bytes(b'corrupt')
        before = self.files()
        with self.assertRaises(StateConflict):
            self.service.restore(operation_id, confirmation)
        self.assertEqual(before, self.files())
        backup.write_bytes(original)
        target = self.library / op['plan']['entries'][0]['target']
        target.write_bytes(b'edited after organization')
        before = self.files()
        with self.assertRaises(StateConflict):
            self.service.restore(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def test_case_equivalent_destination_is_rejected(self):
        fs = LibraryFilesystem(self.library)
        self.addCleanup(fs.close)
        file = self.series_folder / 'Existing.CBZ'
        file.write_bytes(b'existing')
        with self.assertRaises(StateConflict):
            fs.ensure_absent(file.relative_to(self.library).as_posix().lower().replace('[作者]', '[作者]'))

    def test_no_backup_space_or_backup_io_failure_leaves_all_sources(self):
        before = self.files()
        _, operation_id, confirmation = self.prepare()
        with patch('manga_kit.operations.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)), self.assertRaises(ConfigError):
            self.service.execute(operation_id, confirmation)
        with patch.object(LibraryFilesystem, 'backup', side_effect=OSError('no space')), self.assertRaises(OSError):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def test_prepared_edits_cannot_be_changed_or_saved_during_execution(self):
        op, operation_id, confirmation = self.prepare()
        source_id = op['plan']['groups'][0]['source_id']
        app = create_app(settings=self.settings)
        self.addCleanup(app.extensions['jobs'].shutdown)
        with self.service.locked(), self.assertRaises(StateConflict):
            data = app.extensions['metadata'].preview(source_id, {'reading': 'サクラ'})
            app.extensions['metadata'].save(source_id, {'reading': 'サクラ'}, data['revision'], data['base_fingerprint'])
        self.service.store.save_edit(source_id, {'reading': 'サクラ'}, 0, {})
        with self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)

    def test_journal_recovers_edit_commit_before_manifest_save(self):
        before = self.files()
        _, operation_id, confirmation = self.prepare()
        original = self.service._save
        def fail_completed(operation):
            if operation['status'] == 'completed':
                raise OSError('journal write failed after edit commit')
            return original(operation)
        with patch.object(self.service, '_save', side_effect=fail_completed), self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)
        OperationService(self.settings).restore(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def test_execution_jobs_render_in_shared_history(self):
        _, operation_id, confirmation = self.execute()
        self.service.restore(operation_id, confirmation)
        app = create_app(settings=self.settings)
        self.addCleanup(app.extensions['jobs'].shutdown)
        client = app.test_client()
        self.assertEqual(client.get('/jobs').status_code, 200)
        for job in self.service.store.jobs():
            self.assertEqual(client.get('/jobs/' + job['id']).status_code, 200)

    def test_execution_setting_requires_boolean_and_default_is_off(self):
        config = self.root / 'config.yaml'
        config.write_text('execution:\n  enabled: maybe\n')
        with self.assertRaises(ConfigError):
            load_settings(config)
        self.assertFalse(Settings().execution_enabled)

    def test_legacy_dev2_plan_without_kind_still_executes_and_restores(self):
        before = self.files()
        operation, operation_id, _ = self.prepare()
        operation['plan'].pop('kind')
        operation['digest'] = digest(operation['plan'])
        (self.service._folder(operation_id) / 'plan.json').write_text(json.dumps(operation['plan']))
        self.service._save(operation)
        self.service.execute(operation_id, operation['digest'])
        self.service.restore(operation_id, operation['digest'])
        self.assertEqual(before, self.files())

    def test_failed_directory_sync_retains_original_at_destination(self):
        op, _, _ = self.prepare()
        entry = op['plan']['entries'][0]
        fs = LibraryFilesystem(self.library)
        self.addCleanup(fs.close)
        original = os.fsync
        def fail_after_unlink(fd):
            if not (self.library / entry['source']).exists():
                raise OSError('source directory sync failed')
            return original(fd)
        with patch('manga_kit.filesystem.os.fsync', side_effect=fail_after_unlink), self.assertRaises(OSError):
            fs.move(entry['source'], entry['target'], entry['snapshot'])
        self.assertFalse((self.library / entry['source']).exists())
        fs.verify(entry['target'], entry['snapshot'])

    def test_symlink_backup_is_rejected_without_restore_changes(self):
        _, operation_id, confirmation = self.execute()
        backup = self.service._folder(operation_id) / 'backup' / '0'
        external = self.root / 'external-backup'
        backup.rename(external)
        backup.symlink_to(external)
        before = self.files()
        with self.assertRaises(OSError):
            self.service.restore(operation_id, confirmation)
        self.assertEqual(self.files(), before)

    def test_schema_one_upgrade_preserves_existing_edits_and_jobs(self):
        store = self.service.store
        store.save_edit('existing', {'reading': 'さくら'}, 0, {})
        job_id = store.create_job('scan')
        with store.connection() as conn:
            conn.execute('DROP TABLE operation_edits')
            conn.execute('PRAGMA user_version=1')
        upgraded = StateStore(self.settings)
        self.assertEqual(upgraded.edits()['existing']['fields'], {'reading': 'さくら'})
        self.assertEqual(upgraded.job(job_id)['kind'], 'scan')
        with upgraded.connection() as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 3)
        self.assertIsNone(upgraded.operation_edits('unknown'))


if __name__ == '__main__':
    unittest.main()
