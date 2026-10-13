import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from manga_kit.config import Settings
from manga_kit.conversion import ConversionService
from manga_kit.durability import durable_mkdir
from manga_kit.filesystem import LibraryFilesystem
from manga_kit.operations import OperationService
from manga_kit.state import StateConflict, StateStore


@unittest.skipUnless(os.name == 'posix' and Path('/proc/self/fd').is_dir(), 'Linux persistence and SIGKILL verification')
class DurabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / 'library'
        folder = self.library / '連載中' / '[作者] 作品'
        folder.mkdir(parents=True)
        for volume in (1, 2):
            with zipfile.ZipFile(folder / f'元 第{volume}巻.cbz', 'w') as archive:
                archive.writestr('ComicInfo.xml', f'<ComicInfo><Series>作品</Series><Writer>作者</Writer><Number>{volume}</Number></ComicInfo>')
                archive.writestr('page10.png', b'fictitious tenth page')
                archive.writestr('page2.png', b'fictitious second page')
        (folder / 'series.json').write_text(json.dumps({'reading': 'サクヒン', 'status': 'completed'}))
        self.settings = Settings(demo=False, library_root=self.library, data_root=self.root / 'state', execution_enabled=True)
        self.service = OperationService(self.settings)
        self.output = self.root / 'output'
        self.output.mkdir()

    def files(self):
        return {p.relative_to(self.library).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.library.rglob('*') if p.is_file()}

    def plan(self):
        operation = self.service.prepare()
        return operation, operation['plan']['id'], operation['digest']

    def worker(self, engine, action, operation_id, confirmation, boundary):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('durability_worker.py')),
            str(self.root), engine, action, operation_id, confirmation, boundary],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)

    def test_destination_is_reachable_in_a_model_that_loses_unsynced_directory_entries(self):
        """Discard uncommitted names at every source unlink, rather than just counting fsync calls."""
        committed, content = {}, set()
        actual_sync, actual_unlink = os.fsync, os.unlink
        def sync(fd):
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode):
                path = Path(os.readlink(f'/proc/self/fd/{fd}'))
                committed[path] = {name: os.stat(name, dir_fd=fd, follow_symlinks=False).st_ino for name in os.listdir(fd)}
            else:
                content.add(info.st_ino)
            return actual_sync(fd)
        with LibraryFilesystem(self.library) as fs:
            source = '連載中/[作者] 作品/元 第1巻.cbz'
            target = 'new/deeper/book.cbz'
            expected = fs.snapshot(source)
            def unlink(name, *, dir_fd):
                if name == '元 第1巻.cbz':
                    directory = self.library / 'new' / 'deeper'
                    self.assertEqual(committed[directory]['book.cbz'], expected['inode'])
                    self.assertIn(expected['inode'], content)
                    self.assertEqual(committed[self.library]['new'], (self.library / 'new').stat().st_ino)
                    self.assertEqual(committed[self.library / 'new']['deeper'], directory.stat().st_ino)
                return actual_unlink(name, dir_fd=dir_fd)
            with patch('os.fsync', side_effect=sync), patch('os.unlink', side_effect=unlink):
                fs.move(source, target, expected)

    def test_backup_directory_sync_failure_prevents_every_source_move(self):
        before = self.files()
        _, operation_id, confirmation = self.plan()
        actual_sync = os.fsync
        def fail(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                path = Path(os.readlink(f'/proc/self/fd/{fd}'))
                if path.name == 'backup' and os.listdir(fd):
                    raise OSError('backup directory sync failed')
            return actual_sync(fd)
        with patch('os.fsync', side_effect=fail), patch.object(LibraryFilesystem, 'move', side_effect=AssertionError('move began too early')), self.assertRaises(OSError):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def test_move_destination_sync_failure_retains_source(self):
        before = self.files()
        operation, operation_id, confirmation = self.plan()
        target = self.library / operation['plan']['entries'][0]['target']
        actual_sync = os.fsync
        def fail(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode) and target.exists() and Path(os.readlink(f'/proc/self/fd/{fd}')) == target.parent:
                raise OSError('destination directory sync failed')
            return actual_sync(fd)
        with patch('os.fsync', side_effect=fail), self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(before, self.files())
        self.service.restore(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def test_sigkill_after_move_link_can_restore_originals(self):
        before = self.files()
        _, operation_id, confirmation = self.plan()
        self.worker('organize', 'execute', operation_id, confirmation, 'link')
        OperationService(self.settings).restore(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def test_sigkill_after_source_unlink_can_restore_originals(self):
        before = self.files()
        _, operation_id, confirmation = self.plan()
        self.worker('organize', 'execute', operation_id, confirmation, 'unlink')
        OperationService(self.settings).restore(operation_id, confirmation)
        self.assertEqual(before, self.files())

    def restore_crash(self, boundary):
        before = self.files()
        operation, operation_id, confirmation = self.plan()
        self.service.execute(operation_id, confirmation)
        (self.library / operation['plan']['entries'][0]['target']).unlink()
        self.worker('organize', 'restore', operation_id, confirmation, boundary)
        self.assertTrue(list(self.library.rglob('.manga-kit-restore-*')))
        if boundary == 'copy':
            temporary = next(self.library.rglob('.manga-kit-restore-*'))
            self.assertGreater(temporary.stat().st_size, 0)
            self.assertLess(temporary.stat().st_size, operation['plan']['entries'][0]['snapshot']['size'])
        reopened = OperationService(self.settings)
        reopened.restore(operation_id, confirmation)
        self.assertEqual(before, self.files())
        self.assertEqual(list(self.library.rglob('.manga-kit-restore-*')), [])
        self.assertEqual(reopened.inspect(operation_id)['status'], 'restored')

    def test_sigkill_during_backup_copy_cleans_partial_file_and_retries(self):
        self.restore_crash('temporary')

    def test_sigkill_after_partial_backup_write_can_retry(self):
        self.restore_crash('copy')

    def test_sigkill_after_backup_publish_cleans_extra_link_and_retries(self):
        self.restore_crash('publish')

    def conversion_crash(self, boundary):
        before = self.files()
        source = next(self.library.rglob('*.cbz')).relative_to(self.library).as_posix()
        service = ConversionService(self.settings)
        operation = service.prepare([source], self.output)
        self.worker('conversion', 'execute', operation['plan']['id'], operation['digest'], boundary)
        self.assertTrue(list(self.output.rglob('.manga-kit-restore-*')))
        if boundary == 'copy':
            temporary = next(self.output.rglob('.manga-kit-restore-*'))
            self.assertGreater(temporary.stat().st_size, 0)
            staged = next(service._folder(operation['plan']['id']).glob('cbz-*/0.cbz'))
            expected = staged.stat().st_size
            self.assertLess(temporary.stat().st_size, expected)
        service.restore(operation['plan']['id'], operation['digest'])
        self.assertEqual(before, self.files())
        self.assertEqual([p for p in self.output.rglob('*') if p.is_file()], [])

    def test_sigkill_during_cbz_copy_can_undo_without_touching_originals(self):
        self.conversion_crash('temporary')

    def test_sigkill_after_partial_cbz_write_can_undo(self):
        self.conversion_crash('copy')

    def test_sigkill_after_cbz_publish_can_undo_without_touching_originals(self):
        self.conversion_crash('publish')

    def test_replaced_temporary_file_is_never_deleted(self):
        operation, operation_id, confirmation = self.plan()
        self.service.execute(operation_id, confirmation)
        (self.library / operation['plan']['entries'][0]['target']).unlink()
        self.worker('organize', 'restore', operation_id, confirmation, 'temporary')
        temporary = next(self.library.rglob('.manga-kit-restore-*'))
        replacement = self.root / 'replacement'
        replacement.write_bytes(b'externally replaced file')
        os.replace(replacement, temporary)
        with self.assertRaises(StateConflict):
            OperationService(self.settings).restore(operation_id, confirmation)
        self.assertEqual(temporary.read_bytes(), b'externally replaced file')

    def test_new_journal_directory_and_its_parents_are_persisted(self):
        path = self.root / 'new-state' / 'operations' / 'namespace'
        synced = set()
        actual_sync = os.fsync
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                synced.add(Path(os.readlink(f'/proc/self/fd/{fd}')))
            return actual_sync(fd)
        with patch('os.fsync', side_effect=sync):
            durable_mkdir(path)
        self.assertTrue({path, path.parent, path.parent.parent, self.root}.issubset(synced))

    def test_sqlite_uses_full_synchronization_on_every_connection(self):
        store = StateStore(self.settings)
        for _ in range(2):
            with store.connection() as connection:
                self.assertEqual(connection.execute('PRAGMA synchronous').fetchone()[0], 2)
                self.assertEqual(connection.execute('PRAGMA journal_mode').fetchone()[0], 'wal')

    def test_sigkill_after_edit_commit_restores_files_and_manual_metadata(self):
        before = self.files()
        series = self.service.library.scan().series[0]
        self.service.store.save_edit(series.id, {'author': '復元を検証する著者'}, 0, {})
        _, operation_id, confirmation = self.plan()
        self.worker('organize', 'execute', operation_id, confirmation, 'edits')
        reopened = OperationService(self.settings)
        reopened.restore(operation_id, confirmation)
        self.assertEqual(before, self.files())
        self.assertEqual(reopened.library.scan(refresh=True).series[0].author, '復元を検証する著者')

    def test_previous_checkpoint_recovers_missing_or_unreadable_current_manifest(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                before = self.files()
                _, operation_id, confirmation = self.plan()
                self.service.execute(operation_id, confirmation)
                current = self.service._folder(operation_id) / 'manifest.json'
                if corrupt:
                    current.write_text('{interrupted journal')
                else:
                    current.unlink()
                self.assertTrue(self.service.inspect(operation_id)['_fallback'])
                self.service.restore(operation_id, confirmation)
                self.assertEqual(before, self.files())
                self.assertEqual(self.service.inspect(operation_id)['status'], 'restored')

    def test_failed_checkpoint_sync_aborts_before_source_changes(self):
        before = self.files()
        _, operation_id, confirmation = self.plan()
        actual = os.replace
        def fail(source, destination):
            if str(destination).endswith('manifest.previous.json'):
                raise OSError('previous checkpoint rename failed')
            return actual(source, destination)
        with patch('os.replace', side_effect=fail), self.assertRaises(OSError):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(before, self.files())


if __name__ == '__main__':
    unittest.main()
