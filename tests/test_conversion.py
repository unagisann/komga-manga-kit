from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import struct
import zlib
import unittest
from unittest.mock import patch
import zipfile

from manga_kit import create_app
from manga_kit.archives import build_cbz, inspect_archive
from manga_kit.config import ConfigError, Settings
from manga_kit.conversion import ConversionService
from manga_kit.filesystem import LibraryFilesystem
from manga_kit.operations import OperationService
from manga_kit.state import StateConflict
from archive_fixtures import stored_rar

try:
    import libarchive
except (ImportError, OSError):
    libarchive = None


@unittest.skipUnless(os.name == 'posix', 'CLI conversion currently supports POSIX')
class ArchiveFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.library, self.output = self.root / 'library', self.root / 'converted'
        self.library.mkdir()
        self.output.mkdir()
        self.metadata = b'<?xml version="1.0"?><ComicInfo><Series>Fictitious</Series><Number>1</Number><PageCount>3</PageCount><Pages><Page Image="0" Type="FrontCover"/><Page Image="2" DoublePage="True"/></Pages></ComicInfo>'
        self.members = [('wrapper/page10.png', b'tenth page'), ('wrapper/page2.png', b'second page'),
                        ('wrapper/page1.png', b'first page'), ('wrapper/ComicInfo.xml', self.metadata),
                        ('notes/readme.txt', b'original supplementary data')]
        self.relative = '作品 第1巻.zip'
        self.write_zip(self.library / self.relative, self.members)
        self.settings = Settings(demo=False, library_root=self.library, data_root=self.root / 'state', execution_enabled=True)
        self.service = ConversionService(self.settings)

    def write_zip(self, path, members):
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.comment = b'original archive comment'
            for name, content in members:
                archive.writestr(name, content)

    def inspect(self, relative=None):
        with LibraryFilesystem(self.library) as fs:
            return inspect_archive(fs, relative or self.relative)

    def originals(self):
        return {p.relative_to(self.library).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                for p in self.library.rglob('*') if p.is_file()}

    def prepare(self, paths=None):
        op = self.service.prepare(paths or [self.relative], self.output)
        return op, op['plan']['id'], op['digest']

    def execute(self, paths=None):
        operation, operation_id, confirmation = self.prepare(paths)
        self.service.execute(operation_id, confirmation)
        return operation, operation_id, confirmation


class ConversionTests(ArchiveFixture):
    def test_plan_is_read_only_and_shows_natural_page_mapping(self):
        before = self.originals()
        operation, _, _ = self.prepare()
        self.assertEqual(self.originals(), before)
        self.assertEqual(list(self.output.iterdir()), [])
        archive = operation['plan']['entries'][0]['archive']
        self.assertEqual(archive['pages'], 3)
        pages = sorted((m['page'], m['source'], m['target']) for m in archive['members'] if 'page' in m)
        self.assertEqual([p[1] for p in pages], ['wrapper/page1.png', 'wrapper/page2.png', 'wrapper/page10.png'])
        self.assertEqual([p[2] for p in pages], ['0001.png', '0002.png', '0003.png'])

    def test_generated_cbz_preserves_pages_comicinfo_other_files_and_comment(self):
        before = self.originals()
        operation, operation_id, confirmation = self.execute()
        path = self.output / operation['plan']['entries'][0]['target']
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.namelist(), ['0001.png', '0002.png', '0003.png', 'ComicInfo.xml', 'notes/readme.txt'])
            self.assertEqual(archive.read('0001.png'), b'first page')
            self.assertEqual(archive.read('0002.png'), b'second page')
            self.assertEqual(archive.read('0003.png'), b'tenth page')
            self.assertEqual(archive.read('ComicInfo.xml'), self.metadata)
            self.assertEqual(archive.read('notes/readme.txt'), b'original supplementary data')
            self.assertEqual(archive.comment, b'original archive comment')
        self.assertEqual(self.originals(), before)
        self.service.restore(operation_id, confirmation)
        self.assertFalse(path.exists())
        self.assertEqual(self.originals(), before)
        self.assertEqual(self.service.inspect(operation_id)['status'], 'restored')

    @unittest.skipUnless(libarchive, 'RAR/7z need the libarchive shared library')
    def test_original_stored_rar4_rar5_and_7z_fixtures_convert(self):
        for version in (4, 5):
            path = self.library / f'rar{version}.cbr'
            stored_rar(path, self.members, version)
        seven = self.library / 'book.7z'
        with libarchive.file_writer(str(seven), '7zip') as archive:
            for name, data in self.members:
                archive.add_file_from_memory(name, len(data), data, permission=0o644)
        before = self.originals()
        operation, operation_id, confirmation = self.execute(['rar4.cbr', 'rar5.cbr', 'book.7z'])
        for entry in operation['plan']['entries']:
            with zipfile.ZipFile(self.output / entry['target']) as archive:
                self.assertEqual(archive.read('0002.png'), b'second page')
                self.assertEqual(archive.read('ComicInfo.xml'), self.metadata)
        self.assertEqual(before, self.originals())
        self.service.restore(operation_id, confirmation)
        self.assertEqual(before, self.originals())

    def test_cbz_can_be_normalized_to_another_root(self):
        cbz = self.library / 'book.cbz'
        self.write_zip(cbz, self.members)
        self.execute(['book.cbz'])
        self.assertTrue(cbz.exists())
        self.assertTrue((self.output / 'book.cbz').exists())

    def test_bad_confirmation_and_wrong_operation_service_are_rejected(self):
        _, operation_id, confirmation = self.prepare()
        with self.assertRaises(ConfigError):
            self.service.execute(operation_id, 'wrong')
        with self.assertRaises(ConfigError):
            OperationService(self.settings).execute(operation_id, confirmation)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_disabled_demo_overlapping_output_and_missing_output_are_rejected(self):
        for settings in (replace(self.settings, execution_enabled=False), replace(self.settings, demo=True)):
            with self.assertRaises(ConfigError):
                ConversionService(settings).prepare([self.relative], self.output)
        for output in (self.library, self.library / 'inside', self.root, self.settings.data_root, self.root / 'missing'):
            with self.subTest(output=output), self.assertRaises(ConfigError):
                self.service.prepare([self.relative], output)

    def test_duplicate_targets_and_file_folder_target_conflicts_are_rejected(self):
        self.write_zip(self.library / '作品 第1巻.cbz', self.members)
        with self.assertRaises(ConfigError):
            self.prepare([self.relative, '作品 第1巻.cbz'])
        self.write_zip(self.library / 'folder.zip', self.members)
        self.write_zip(self.library / 'folder.cbz' / 'child.zip', self.members)
        with self.assertRaises(ConfigError):
            self.prepare(['folder.zip', 'folder.cbz/child.zip'])

    def test_late_collision_and_changed_source_leave_every_file_untouched(self):
        operation, operation_id, confirmation = self.prepare()
        target = self.output / operation['plan']['entries'][0]['target']
        target.write_bytes(b'keep existing file')
        with self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(target.read_bytes(), b'keep existing file')
        target.unlink()
        source = self.library / self.relative
        source.write_bytes(source.read_bytes() + b'changed')
        before = self.originals()
        with self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(self.originals(), before)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_output_root_replacement_and_symlink_escape_are_rejected(self):
        _, operation_id, confirmation = self.prepare()
        self.output.rename(self.root / 'old-output')
        self.output.mkdir()
        with self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)
        self.write_zip(self.library / 'subfolder' / 'book.zip', self.members)
        _, operation_id, confirmation = self.prepare(['subfolder/book.zip'])
        outside = self.root / 'outside'
        outside.mkdir()
        (self.output / 'subfolder').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(list(outside.iterdir()), [])

    def test_corrupt_or_replaced_output_blocks_all_undo_deletions(self):
        self.write_zip(self.library / 'second.zip', self.members)
        operation, operation_id, confirmation = self.execute([self.relative, 'second.zip'])
        target = self.output / operation['plan']['entries'][1]['target']
        replacement = self.root / 'replacement'
        shutil.copy2(target, replacement)
        os.replace(replacement, target)
        before = {p.name: p.read_bytes() for p in self.output.iterdir()}
        with self.assertRaises(StateConflict):
            self.service.restore(operation_id, confirmation)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir()})

    def test_interrupted_publication_can_be_undone_after_reopening(self):
        before = self.originals()
        _, operation_id, confirmation = self.prepare()
        original = LibraryFilesystem.recover_backup
        def interrupt(fs, *args, **kwargs):
            original(fs, *args, **kwargs)
            raise KeyboardInterrupt()
        with patch.object(LibraryFilesystem, 'recover_backup', new=interrupt), self.assertRaises(KeyboardInterrupt):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(self.service.inspect(operation_id)['status'], 'running')
        ConversionService(self.settings).restore(operation_id, confirmation)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(self.originals(), before)

    def test_build_failure_publishes_nothing_and_sources_stay_intact(self):
        before = self.originals()
        _, operation_id, confirmation = self.prepare()
        with patch('manga_kit.conversion.build_cbz', side_effect=OSError('simulated full disk')), self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(before, self.originals())
        self.service.restore(operation_id, confirmation)

    def test_partial_publication_failure_undo_removes_only_owned_outputs(self):
        self.write_zip(self.library / 'second.zip', self.members)
        before = self.originals()
        _, operation_id, confirmation = self.prepare([self.relative, 'second.zip'])
        original = LibraryFilesystem.recover_backup
        calls = []
        def fail_second(fs, *args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                raise OSError('publication failed')
            return original(fs, *args, **kwargs)
        with patch.object(LibraryFilesystem, 'recover_backup', new=fail_second), self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(len(list(self.output.glob('*.cbz'))), 1)
        self.service.restore(operation_id, confirmation)
        self.assertEqual(list(self.output.glob('*.cbz')), [])
        self.assertEqual(self.originals(), before)

    def test_interrupted_undo_can_resume_after_one_generated_file_was_deleted(self):
        self.write_zip(self.library / 'second.zip', self.members)
        before = self.originals()
        _, operation_id, confirmation = self.execute([self.relative, 'second.zip'])
        original = os.unlink
        def interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt()
        with patch('manga_kit.conversion.os.unlink', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.service.restore(operation_id, confirmation)
        self.assertEqual(self.service.inspect(operation_id)['status'], 'restoring')
        ConversionService(self.settings).restore(operation_id, confirmation)
        self.assertEqual(list(self.output.glob('*.cbz')), [])
        self.assertEqual(self.originals(), before)

    def test_corrupt_source_backup_blocks_undo_before_any_deletion(self):
        _, operation_id, confirmation = self.execute()
        (self.service._folder(operation_id) / 'backup' / '0').write_bytes(b'corrupted backup')
        before = [path.read_bytes() for path in self.output.glob('*.cbz')]
        with self.assertRaises(StateConflict):
            self.service.restore(operation_id, confirmation)
        self.assertEqual([path.read_bytes() for path in self.output.glob('*.cbz')], before)

    def test_low_disk_space_leaves_sources_and_output_untouched(self):
        before = self.originals()
        _, operation_id, confirmation = self.prepare()
        with patch('manga_kit.conversion.shutil.disk_usage', return_value=shutil._ntuple_diskusage(100, 100, 0)), self.assertRaises(ConfigError):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(before, self.originals())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_undo_preserves_later_changes_to_originals(self):
        _, operation_id, confirmation = self.execute()
        source = self.library / self.relative
        source.write_bytes(b'new source deliberately replaced by owner')
        self.service.restore(operation_id, confirmation)
        self.assertEqual(source.read_bytes(), b'new source deliberately replaced by owner')

    def test_undo_uses_actual_output_timestamp_on_a_coarser_filesystem(self):
        original = os.utime
        def rounded(path, **kwargs):
            if isinstance(path, int):
                kwargs['ns'] = tuple(value // 1000000000 * 1000000000 for value in kwargs['ns'])
            original(path, **kwargs)
        with patch('manga_kit.filesystem.os.utime', side_effect=rounded):
            _, operation_id, confirmation = self.execute()
        self.service.restore(operation_id, confirmation)
        self.assertEqual(list(self.output.glob('*.cbz')), [])

    def test_komga_is_not_contacted_and_manual_edits_remain_at_original(self):
        configured = replace(self.settings, komga_url='http://localhost:25601')
        service = ConversionService(configured)
        series = service.library.scan().series[0]
        service.store.save_edit(series.id, {'author': 'manually reviewed author'}, 0, {})
        before = service.store.edits()
        with patch('manga_kit.komga.KomgaClient.get', side_effect=AssertionError('no calls allowed')):
            operation = service.prepare([self.relative], self.output)
            service.execute(operation['plan']['id'], operation['digest'])
            service.restore(operation['plan']['id'], operation['digest'])
        self.assertEqual(service.store.edits(), before)

    def test_job_history_and_detail_render_for_conversion_and_undo(self):
        _, operation_id, confirmation = self.execute()
        self.service.restore(operation_id, confirmation)
        app = create_app(settings=self.settings)
        self.addCleanup(app.extensions['jobs'].shutdown)
        client = app.test_client()
        self.assertEqual(client.get('/jobs').status_code, 200)
        for job in self.service.store.jobs():
            self.assertEqual(client.get('/jobs/' + job['id']).status_code, 200)


class ArchiveValidationTests(ArchiveFixture):
    def test_zip_directory_is_bounded_before_member_allocation(self):
        with patch('manga_kit.archives.MAX_CENTRAL_BYTES', 10), self.assertRaises(ConfigError):
            self.inspect()

    def test_zip64_directory_is_parsed_and_bounded(self):
        path = self.library / self.relative
        data = path.read_bytes()
        index = data.rindex(b'PK\x05\x06')
        fields = list(struct.unpack('<4s4H2IH', data[index:index + 22]))
        end64 = struct.pack('<4sQ2H2I4Q', b'PK\x06\x06', 44, 45, 45, 0, 0,
                            fields[4], fields[4], fields[5], fields[6])
        locator = struct.pack('<4sIQI', b'PK\x06\x07', 0, index, 1)
        fields[3] = fields[4] = 65535
        fields[5] = fields[6] = 0xffffffff
        path.write_bytes(data[:index] + end64 + locator + struct.pack('<4s4H2IH', *fields) + data[index + 22:])
        self.assertEqual(self.inspect()['pages'], 3)
        with patch('manga_kit.archives.MAX_CENTRAL_BYTES', 10), self.assertRaises(ConfigError):
            self.inspect()

    @unittest.skipUnless(libarchive, 'RAR needs libarchive')
    def test_split_rar4_and_rar5_are_rejected_even_with_complete_files(self):
        for version in (4, 5):
            path = self.library / f'split{version}.rar'
            stored_rar(path, self.members, version)
            data = bytearray(path.read_bytes())
            if version == 4:
                data[10] |= 1
                data[7:9] = struct.pack('<H', zlib.crc32(data[9:20]) & 0xffff)
            else:
                data[15] |= 1
                data[8:12] = struct.pack('<I', zlib.crc32(data[12:16]))
            path.write_bytes(data)
            with self.subTest(version=version), self.assertRaisesRegex(ConfigError, '分割'):
                self.inspect(path.name)

    def test_dangerous_names_duplicate_names_links_and_hidden_pages_are_rejected(self):
        for names in (['../page1.png'], ['/page1.png'], ['C:/page1.png'], ['foo\\page1.png'],
                      ['pages/．．/page1.png'], ['page1.png', 'PAGE1.PNG'], ['.hidden/page1.png'], ['__MACOSX/page1.png']):
            with self.subTest(names=names):
                self.write_zip(self.library / self.relative, [(name, b'page') for name in names])
                with self.assertRaises(ConfigError):
                    self.inspect()
        info = zipfile.ZipInfo('page1.png')
        info.create_system, info.external_attr = 3, 0o120777 << 16
        with zipfile.ZipFile(self.library / self.relative, 'w') as archive:
            archive.writestr(info, b'../outside')
        with self.assertRaises(ConfigError):
            self.inspect()

    def test_metadata_conflicts_dtd_and_page_reference_errors_are_rejected(self):
        for xml in (b'<!DOCTYPE ComicInfo><ComicInfo/>', b'<ComicInfo><PageCount>9</PageCount></ComicInfo>',
                    b'<ComicInfo><Pages><Page Image="3"/></Pages></ComicInfo>', b'<broken', b'<other/>',
                    b'<ComicInfo><Pages><Page Image="0"/><Page Image="0"/></Pages></ComicInfo>'):
            self.write_zip(self.library / self.relative, [('page1.png', b'page'), ('ComicInfo.xml', xml)])
            with self.subTest(xml=xml), self.assertRaises(ConfigError):
                self.inspect()
        self.write_zip(self.library / self.relative, [('page1.png', b'page'), ('ComicInfo.xml', b'<ComicInfo/>'),
                                                     ('nested/ComicInfo.xml', b'<ComicInfo/>')])
        with self.assertRaises(ConfigError):
            self.inspect()

    def test_size_count_limits_empty_archives_and_unsupported_formats_are_rejected(self):
        for setting, limit in (('MAX_ENTRIES', 2), ('MAX_ENTRY_BYTES', 4), ('MAX_TOTAL_BYTES', 10), ('MAX_XML_BYTES', 2)):
            with self.subTest(setting=setting), patch('manga_kit.archives.' + setting, limit), self.assertRaises(ConfigError):
                self.inspect()
        self.write_zip(self.library / self.relative, [('readme.txt', b'no images')])
        with self.assertRaises(ConfigError):
            self.inspect()
        epub = self.library / 'book.epub'
        self.write_zip(epub, [('page1.png', b'page')])
        with self.assertRaises(ConfigError):
            self.inspect('book.epub')

    def test_bad_crc_and_encrypted_zip_are_rejected(self):
        path = self.library / self.relative
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('page1.png', b'original-page-unique')
        content = path.read_bytes()
        path.write_bytes(content.replace(b'original-page-unique', b'modified-page-unique', 1))
        with self.assertRaises(ConfigError):
            self.inspect()
        encrypted = bytearray(content)
        encrypted[6] |= 1
        offset = encrypted.index(b'PK\x01\x02')
        encrypted[offset + 8] |= 1
        path.write_bytes(encrypted)
        with self.assertRaises(ConfigError):
            self.inspect()

    def test_normalized_page_folder_collision_is_rejected(self):
        self.write_zip(self.library / self.relative, [('page1.png', b'page'), ('0001.png/readme.txt', b'extra')])
        with self.assertRaises(ConfigError):
            self.inspect()

    def test_build_is_repeatable_and_every_member_hash_is_verified(self):
        with LibraryFilesystem(self.library) as fs:
            expected = inspect_archive(fs, self.relative)
            one, two = self.root / 'one.cbz', self.root / 'two.cbz'
            build_cbz(fs, self.relative, expected, one)
            build_cbz(fs, self.relative, expected, two)
        self.assertEqual(one.read_bytes(), two.read_bytes())


if __name__ == '__main__':
    unittest.main()
