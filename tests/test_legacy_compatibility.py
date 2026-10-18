"""Public, fictitious regression cases for behavior retained from KomgaTool.

No operational modules, configuration, archives or credentials are used.
"""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from manga_kit.archives import inspect_archive
from manga_kit.cli import main
from manga_kit.config import ConfigError, Settings
from manga_kit.conversion import ConversionService
from manga_kit.filesystem import LibraryFilesystem
from manga_kit.library import filename_volume, scan_library, volume_number
from manga_kit.operations import OperationService
from manga_kit.providers import ComicInfoProvider, EpubProvider
from archive_fixtures import cp932_zip, stored_rar

try:
    import libarchive
except (ImportError, OSError):
    libarchive = None


class FilenameCompatibilityTests(unittest.TestCase):
    def test_known_title_allows_attached_number_and_author_suffix(self):
        for name, title, expected in (
            ('架空作品1.zip', '架空作品', '1'),
            ('架空作品００２ - 著者.cbz', '架空作品', '2'),
            ('架空作品_1.5.cbz', '架空作品', '1.5'),
            ('作品86_01.zip', '作品86', '1'),
            ('架空作品v02.zip', '架空作品', '2'),
            ('BookV02.zip', 'Book', '2'),
            ('作品（ ０２ ） - 著者.zip', '作品', '2'),
        ):
            with self.subTest(name=name):
                self.assertEqual(filename_volume(name, title), expected)

    def test_numbers_and_markers_inside_known_title_are_not_volumes(self):
        for title in ('架空作品86', '架空作品（2024）', '架空作品 第3巻', '架空作品 v2'):
            with self.subTest(title=title):
                self.assertIsNone(filename_volume(title + '.zip', title))
                self.assertEqual(filename_volume(title + ' 第05巻.zip', title), '5')
                self.assertEqual(filename_volume(title + '_01.zip', title), '1')

    def test_parentheses_in_middle_and_unknown_bare_numbers_remain_unknown(self):
        for name in ('作品（2024）特別編.zip', '作品86.zip', '作品1 - 著者.zip', 'alive02.zip'):
            with self.subTest(name=name):
                self.assertIsNone(filename_volume(name))

    def test_japanese_adjacent_v_marker_and_decimal_are_supported(self):
        self.assertEqual(filename_volume('架空作品v1.5.zip'), '1.5')

    def test_metadata_exponents_and_lengths_cannot_expand_unbounded_volume_text(self):
        for value in ('1e-1000000000', '9' * 10000, 'NaN', 'Infinity', '-1'):
            with self.subTest(value=value[:30]):
                self.assertIsNone(volume_number(value))

    def test_accepted_decimal_digits_are_not_rounded_by_decimal_context(self):
        value = '0.12345678901234567890123456789012'
        self.assertEqual(volume_number(value), value)
        self.assertEqual(volume_number('-0'), '0')


class IsolatedCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.library, self.output = self.root / 'library', self.root / 'output'
        self.library.mkdir()
        self.output.mkdir()
        self.settings = Settings(demo=False, execution_enabled=True, library_root=self.library,
                                 data_root=self.root / 'state')

    def archive(self, name, members):
        path = self.library / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, 'w') as archive:
            for member, content in members:
                archive.writestr(member, content)
        return path

    def originals(self):
        return {p.relative_to(self.library).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(),
                p.stat().st_mtime_ns) for p in self.library.rglob('*') if p.is_file()}

    def epub(self, name='作品/原稿.epub', position='２', *, package=None, container=None):
        container = container or b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>'
        package = package or ('<package xmlns="http://www.idpf.org/2007/opf"><metadata>'
                              '<meta property="group-position">' + position + '</meta>'
                              '</metadata></package>')
        return self.archive(name, [('mimetype', b'application/epub+zip'),
                                   ('META-INF/container.xml', container), ('OPS/book.opf', package)])


class MetadataCompatibilityTests(IsolatedCompatibilityTests):
    def test_temporary_files_mac_metadata_and_non_regular_books_are_not_scanned(self):
        self.archive('作品/作品 第1巻.zip', [('page.png', b'page')])
        self.archive('作品/.tmp_normalize_作品 第2巻.cbz', [('page.png', b'temporary')])
        self.archive('作品/._作品 第1巻.zip', [('page.png', b'AppleDouble')])
        self.archive('__MACOSX/作品/作品 第1巻.zip', [('page.png', b'metadata')])
        if hasattr(os, 'mkfifo'):
            os.mkfifo(self.library / '作品' / '作品 第3巻.zip')
        result = scan_library(self.settings)
        self.assertEqual(result.stats()['books'], 1)
        self.assertEqual(result.series[0].books[0].volume, '1')

    def test_nested_namespaced_comicinfo_overrides_filename_and_reports_conflict(self):
        path = self.archive('作品/原稿 第1巻.zip', [
            ('wrapper/COMICINFO.XML', '<ComicInfo xmlns="urn:fixture"><Series>指定作品</Series>'
             '<Writer>架空著者</Writer><Number>２.５</Number><Genre>日常, 冒険</Genre></ComicInfo>'),
            ('wrapper/page1.png', b'original page')])
        before = self.originals()
        series = scan_library(self.settings).series[0]
        self.assertEqual((series.title, series.author), ('指定作品', '架空著者'))
        self.assertEqual(series.genres, ['日常', '冒険'])
        self.assertEqual((series.books[0].volume, series.books[0].source), ('2.5', 'comicinfo'))
        self.assertIn('巻数が一致しません', series.warnings[0])
        self.assertEqual(self.originals(), before)
        self.assertTrue(path.exists())

    def test_known_folder_title_remains_stable_when_comicinfo_changes_title(self):
        self.archive('架空作品86/架空作品861.zip', [('ComicInfo.xml', '<ComicInfo><Series>別表記</Series></ComicInfo>')])
        self.archive('架空作品86/架空作品862.zip', [('page1.png', b'page')])
        series = scan_library(self.settings).series[0]
        self.assertEqual([book.volume for book in series.books], ['1', '2'])

    def test_duplicate_or_conflicting_comicinfo_is_rejected_instead_of_arbitrary_selection(self):
        for members in (
            [('ComicInfo.xml', '<ComicInfo><Number>1</Number></ComicInfo>'),
             ('nested/ComicInfo.xml', '<ComicInfo><Number>2</Number></ComicInfo>')],
            [('ComicInfo.xml', '<ComicInfo><Number>1</Number><Number>2</Number></ComicInfo>')],
        ):
            with self.subTest(members=members):
                path = self.archive('作品/原稿 第3巻.zip', members)
                with self.assertRaises(ValueError):
                    ComicInfoProvider().inspect(path)
                result = scan_library(self.settings).series[0]
                self.assertEqual(result.books[0].volume, '3')
                self.assertTrue(result.warnings)

    def test_cp932_zip_metadata_and_declared_directory_size_are_checked(self):
        path = self.library / '原稿.zip'
        cp932_zip(path, [('表紙/ComicInfo.xml', b'<ComicInfo><Number>2</Number></ComicInfo>')])
        self.assertEqual(ComicInfoProvider().inspect(path).fields['volume'], '2')
        with patch('manga_kit.archives.MAX_CENTRAL_BYTES', 1), self.assertRaises(ConfigError):
            ComicInfoProvider().inspect(path)

    def test_metadata_total_read_is_bounded(self):
        path = self.archive('原稿.zip', [('ComicInfo.xml', b'<ComicInfo/>'),
                                         ('nested/ComicInfo.xml', b'<ComicInfo/>')])
        with patch('manga_kit.archives.MAX_XML_BYTES', 20), self.assertRaises(ConfigError):
            ComicInfoProvider().inspect(path)

    def test_invalid_embedded_volume_falls_back_with_warning(self):
        self.archive('作品/原稿 第3巻.zip', [('ComicInfo.xml', '<ComicInfo><Number>NaN</Number></ComicInfo>')])
        series = scan_library(self.settings).series[0]
        self.assertEqual(series.books[0].volume, '3')
        self.assertIn('対応範囲外', series.warnings[0])

    @unittest.skipUnless(libarchive, 'RAR/7z need libarchive')
    def test_rar4_rar5_and_7z_read_nested_comicinfo_without_extracting_pages(self):
        members = [('wrapper/ComicInfo.xml', b'<ComicInfo><Series>Fictitious</Series><Number>2</Number></ComicInfo>'),
                   ('wrapper/page.png', b'original page')]
        paths = []
        for version in (4, 5):
            path = self.library / f'book{version}.cbr'
            stored_rar(path, members, version)
            paths.append(path)
        seven = self.library / 'book.7z'
        with libarchive.file_writer(str(seven), '7zip') as archive:
            for name, data in members:
                archive.add_file_from_memory(name, len(data), data, permission=0o644)
        paths.append(seven)
        before = self.originals()
        for path in paths:
            with self.subTest(suffix=path.suffix):
                proposal = ComicInfoProvider().inspect(path)
                self.assertEqual(proposal.fields['volume'], '2')
        self.assertEqual(self.originals(), before)

    def test_epub_volume_is_read_from_opf_and_original_bytes_are_preserved(self):
        self.epub('作品/原稿 第1巻.epub')
        before = self.originals()
        series = scan_library(self.settings).series[0]
        self.assertEqual((series.books[0].volume, series.books[0].source), ('2', 'epub'))
        self.assertIn('巻数が一致しません', series.warnings[0])
        self.assertEqual(self.originals(), before)

    def test_epub_conflicting_positions_and_unsafe_opf_paths_are_not_guessed(self):
        cases = [
            dict(package='<package><meta property="group-position">1</meta><meta property="group-position">2</meta></package>'),
            dict(container='<container><rootfile full-path="../outside.opf"/></container>'),
            dict(container='<container><rootfile full-path="OPS/book.opf"/><rootfile full-path="other.opf"/></container>'),
            dict(container='<container><rootfile full-path="missing.opf"/></container>'),
            dict(container='<!DOCTYPE container><container/>'),
            dict(package='<!DOCTYPE package><package/>'),
        ]
        for case in cases:
            with self.subTest(case=case):
                path = self.epub(**case)
                with self.assertRaises((ValueError, ConfigError)):
                    EpubProvider().inspect(path)

    def test_epub_without_position_keeps_filename_volume(self):
        self.epub('作品/原稿 第3巻.epub', package='<package><metadata/></package>')
        series = scan_library(self.settings).series[0]
        self.assertEqual((series.books[0].volume, series.books[0].source), ('3', 'filename'))


@unittest.skipUnless(os.name == 'posix', 'CLI operations currently support POSIX')
class OperationCompatibilityTests(IsolatedCompatibilityTests):
    def test_attached_volume_and_epub_move_restore_preserve_bytes(self):
        self.archive('連載中/架空作品/架空作品1.zip', [('nested/ComicInfo.xml', '<ComicInfo><Number>1</Number></ComicInfo>')])
        self.epub('連載中/架空作品/原稿.epub', position='2')
        before = self.originals()
        service = OperationService(self.settings)
        operation = service.prepare()
        service.execute(operation['plan']['id'], operation['digest'])
        self.assertEqual([b.volume for b in service.library.scan(refresh=True).series[0].books], ['1', '2'])
        service.restore(operation['plan']['id'], operation['digest'])
        self.assertEqual(self.originals(), before)

    def test_nested_comicinfo_duplicate_volumes_block_move_before_changes(self):
        for n in (1, 2):
            self.archive(f'作品/原稿 第{n}巻.zip', [('nested/ComicInfo.xml', '<ComicInfo><Number>1</Number></ComicInfo>')])
        before = self.originals()
        with self.assertRaises(ConfigError):
            OperationService(self.settings).prepare()
        self.assertEqual(self.originals(), before)

    def test_cp932_conversion_preserves_decoded_names_pages_and_undo(self):
        path = self.library / '原稿.zip'
        cp932_zip(path, [('表紙/2.png', b'second'), ('表紙/1.png', b'first'), ('補足.txt', b'notes'),
                         ('表紙/ComicInfo.xml', b'<ComicInfo xmlns="urn:fixture"><PageCount>2</PageCount><Pages><Page Image="0"/></Pages></ComicInfo>')])
        before = self.originals()
        service = ConversionService(self.settings)
        operation = service.prepare(['原稿.zip'], self.output)
        self.assertEqual(operation['plan']['entries'][0]['archive']['members'][0]['source'], '表紙/2.png')
        service.execute(operation['plan']['id'], operation['digest'])
        with zipfile.ZipFile(self.output / '原稿.cbz') as archive:
            self.assertEqual(archive.read('0001.png'), b'first')
            self.assertEqual(archive.read('補足.txt'), b'notes')
        service.restore(operation['plan']['id'], operation['digest'])
        self.assertFalse((self.output / '原稿.cbz').exists())
        self.assertEqual(self.originals(), before)

    def test_both_page_orders_are_retained_from_plan_through_build_and_undo(self):
        self.archive('原稿.zip', [('page2.png', b'two'), ('page10.png', b'ten'), ('page1.png', b'one')])
        before = self.originals()
        service = ConversionService(self.settings)
        for order, second in (('natural-path', b'two'), ('lexicographic-path', b'ten')):
            with self.subTest(order=order):
                operation = service.prepare(['原稿.zip'], self.output, page_order=order)
                self.assertEqual(operation['plan']['entries'][0]['archive']['order'], order)
                service.execute(operation['plan']['id'], operation['digest'])
                with zipfile.ZipFile(self.output / '原稿.cbz') as archive:
                    self.assertEqual(archive.read('0002.png'), second)
                service.restore(operation['plan']['id'], operation['digest'])
                self.assertEqual(self.originals(), before)

    def test_invalid_page_order_does_not_create_a_plan_or_output(self):
        with self.assertRaises(ConfigError):
            ConversionService(self.settings).prepare(['missing.zip'], self.output, page_order='guess')
        self.assertEqual(list(self.output.iterdir()), [])

    def test_namespaced_comicinfo_still_rejects_invalid_page_references(self):
        self.archive('原稿.zip', [('page.png', b'page'), ('ComicInfo.xml',
            '<ComicInfo xmlns="urn:fixture"><Pages><Page Image="1"/></Pages></ComicInfo>')])
        with LibraryFilesystem(self.library) as fs, self.assertRaises(ConfigError):
            inspect_archive(fs, '原稿.zip')

    def test_cli_lexicographic_order_reaches_the_saved_plan(self):
        self.archive('原稿.zip', [('page2.png', b'two'), ('page10.png', b'ten')])
        config = self.root / 'test.yaml'
        config.write_text('app:\n  demo: false\nexecution:\n  enabled: true\nlibrary:\n  root: ./library\ndata:\n  root: ./state\n')
        args = ['manga-kit', '--config', str(config), 'prepare-conversion', '--book', '原稿.zip',
                '--output', str(self.output), '--page-order', 'lexicographic-path']
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch('sys.argv', args), redirect_stdout(output):
            self.assertEqual(main(), 0)
        operation = json.loads(output.getvalue())
        self.assertEqual(operation['plan']['entries'][0]['archive']['order'], 'lexicographic-path')


if __name__ == '__main__':
    unittest.main()
