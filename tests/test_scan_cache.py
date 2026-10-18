from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
import zipfile

from manga_kit import create_app
from manga_kit.config import Settings
from manga_kit.library import LibraryService
from manga_kit.jobs import JobService
from manga_kit.models import ScanResult, Series
from manga_kit.providers import ComicInfoProvider, MetadataProposal
from manga_kit.state import StateStore


class ScanCacheTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(demo=False, library_root=self.root / 'library', data_root=self.root / 'state')
        self.folder = self.settings.library_root / '連載中' / '架空作品'
        self.folder.mkdir(parents=True)
        self.write(1)
        self.write(2)
        self.store = StateStore(self.settings)
        self.library = LibraryService(self.settings, store=self.store)
        original = ComicInfoProvider.inspect
        self.reads = patch.object(ComicInfoProvider, 'inspect', autospec=True, side_effect=original).start()
        self.addCleanup(patch.stopall)

    def write(self, number, author='架空著者A'):
        path = self.folder / f'第{number}巻.cbz'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('ComicInfo.xml', f'<ComicInfo><Series>架空作品</Series><Writer>{author}</Writer><Number>{number}</Number></ComicInfo>')
            archive.writestr('page.png', b'fictitious page')
        return path

    def rescan(self):
        self.library._updated = 0
        return self.library.scan()

    def cache_count(self):
        with self.store.connection() as conn:
            return conn.execute('SELECT COUNT(*) FROM archive_metadata_cache WHERE namespace=?',
                                (self.store.namespace,)).fetchone()[0]

    def test_unchanged_archives_are_reused_after_expiry_and_process_restart(self):
        before = self.library.scan()
        self.assertEqual(self.reads.call_count, 2)
        self.assertEqual(self.cache_count(), 2)
        self.reads.reset_mock()
        self.assertEqual(self.rescan(), before)
        restarted = LibraryService(self.settings, store=StateStore(self.settings))
        self.assertEqual(restarted.scan(), before)
        self.assertEqual(self.reads.call_count, 0)

    def test_full_refresh_bypasses_persistent_cache_for_integrity_checks(self):
        self.library.scan()
        self.reads.reset_mock()
        self.library.scan(refresh=True)
        self.assertEqual(self.reads.call_count, 2)

    def test_preview_refreshes_current_files_without_reopening_unchanged_archives(self):
        self.library.scan()
        self.write(1, '架空著者B')
        (self.folder / '第2巻.cbz').unlink()
        self.write(3, '架空著者B')
        self.write(4, '架空著者B')
        (self.folder / 'series.json').write_text(json.dumps({'title': '更新後の架空作品'}))
        self.reads.reset_mock()
        jobs = JobService(self.library, self.store)
        jobs.submit('preview')
        jobs.shutdown()
        job = self.store.latest_preview()
        self.assertIsNotNone(job)
        self.assertEqual(job['total'], 3)
        self.assertEqual(self.reads.call_count, 3)
        self.assertTrue(all(item['title'] == '更新後の架空作品' for item in job['result']['items']))
        self.assertFalse(any('第2巻' in item['source'] for item in job['result']['items']))
        self.reads.reset_mock()
        jobs = JobService(self.library, self.store)
        jobs.submit('preview')
        jobs.shutdown()
        self.assertEqual(self.reads.call_count, 0)
        # Explicit scans and execution reviews still reopen all archives.
        jobs = JobService(self.library, self.store)
        jobs.submit('scan')
        jobs.shutdown()
        self.assertEqual(self.reads.call_count, 3)

    def test_archives_without_comicinfo_are_also_cached(self):
        with zipfile.ZipFile(self.folder / '第3巻.cbz', 'w') as archive:
            archive.writestr('page.png', b'fictitious page')
        self.assertEqual(self.library.scan().stats()['books'], 3)
        self.reads.reset_mock()
        self.assertEqual(self.rescan().stats()['books'], 3)
        self.assertEqual(self.reads.call_count, 0)

    def test_only_changed_archive_is_reparsed_even_if_size_and_mtime_match(self):
        self.library.scan()
        path = self.folder / '第1巻.cbz'
        before = path.stat()
        self.write(1, '架空著者B')
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_size, before.st_size)
        self.reads.reset_mock()
        self.assertEqual(self.rescan().series[0].author, '架空著者B')
        self.assertEqual(self.reads.call_count, 1)

    def test_replaced_inode_and_recreated_root_invalidate_their_entries(self):
        self.library.scan()
        path = self.folder / '第1巻.cbz'
        replacement = path.with_suffix('.replacement')
        shutil.copy2(path, replacement)
        replacement.replace(path)
        self.reads.reset_mock()
        self.rescan()
        self.assertEqual(self.reads.call_count, 1)
        displaced = self.root / 'old-library'
        self.settings.library_root.rename(displaced)
        shutil.copytree(displaced, self.settings.library_root)
        self.reads.reset_mock()
        restarted = LibraryService(self.settings, store=StateStore(self.settings))
        restarted.scan()
        self.assertEqual(self.reads.call_count, 2)

    def test_sidecars_and_local_edits_remain_current_without_reopening_archives(self):
        series = self.library.scan().series[0]
        (self.folder / 'series.json').write_text(json.dumps({'reading': 'サクヒン', 'publisher': '架空出版'}))
        self.store.save_edit(series.id, {'summary': '確認したあらすじ'}, 0, {})
        self.reads.reset_mock()
        result = self.rescan().series[0]
        self.assertEqual((result.reading, result.publisher, result.summary), ('サクヒン', '架空出版', '確認したあらすじ'))
        self.assertEqual(self.reads.call_count, 0)

    def test_additions_deletions_and_scan_limits_do_not_hide_books(self):
        self.library.scan()
        self.write(3)
        (self.folder / '第2巻.cbz').unlink()
        self.reads.reset_mock()
        self.assertEqual(self.rescan().stats()['books'], 2)
        self.assertEqual(self.reads.call_count, 1)
        self.assertEqual(self.cache_count(), 2)
        limited = LibraryService(replace(self.settings, max_files=1), store=self.store)
        self.assertTrue(limited.scan().truncated)
        self.assertEqual(self.cache_count(), 2)
        self.reads.reset_mock()
        self.assertEqual(self.rescan().stats()['books'], 2)
        self.assertEqual(self.reads.call_count, 0)

    def test_failed_or_unstable_reads_are_not_cached(self):
        (self.folder / '第2巻.cbz').write_bytes(b'corrupt archive')
        first = self.library.scan()
        self.assertEqual(len(first.series[0].warnings), 1)
        self.assertEqual(self.cache_count(), 1)
        self.reads.reset_mock()
        self.assertEqual(len(self.rescan().series[0].warnings), 1)
        self.assertEqual(self.reads.call_count, 1)
        self.write(2)
        original = self.reads.side_effect
        def mutate(provider, path):
            value = original(provider, path)
            if path.name == '第2巻.cbz':
                path.write_bytes(path.read_bytes() + b'changed during scan')
            return value
        self.reads.side_effect = mutate
        result = self.rescan()
        self.assertTrue(any('読み取り中' in value for value in result.series[0].warnings))
        self.assertEqual(self.cache_count(), 1)

    def test_corrupt_cache_and_parser_version_changes_rebuild_safely(self):
        self.library.scan()
        with self.store.connection() as conn:
            conn.execute("UPDATE archive_metadata_cache SET data='broken' WHERE path LIKE '%1巻%'")
        self.reads.reset_mock()
        self.rescan()
        self.assertEqual(self.reads.call_count, 1)
        with patch('manga_kit.scan_cache.CACHE_VERSION', 999):
            self.reads.reset_mock()
            self.rescan()
            self.assertEqual(self.reads.call_count, 2)

    def test_custom_providers_and_other_libraries_do_not_share_cached_results(self):
        self.library.scan()
        class DynamicProvider:
            def __init__(self):
                self.count = 0
            def inspect(self, path):
                self.count += 1
                return MetadataProposal('dynamic', {'author': str(self.count)})
        provider = DynamicProvider()
        custom = LibraryService(self.settings, providers=[provider], store=self.store)
        custom.scan()
        custom._updated = 0
        custom.scan()
        self.assertEqual(provider.count, 4)
        other_root = self.root / 'other-library'
        shutil.copytree(self.settings.library_root, other_root)
        other_settings = replace(self.settings, library_root=other_root)
        self.reads.reset_mock()
        LibraryService(other_settings, store=StateStore(other_settings)).scan()
        self.assertEqual(self.reads.call_count, 2)

    def test_cached_pages_remain_available_during_explicit_background_scan(self):
        app = create_app(settings=self.settings)
        self.addCleanup(app.extensions['jobs'].shutdown)
        client = app.test_client()
        self.assertEqual(client.get('/library').status_code, 200)
        entered, release = Event(), Event()
        original = self.reads.side_effect
        def slow(provider, path):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('fixture timed out')
            return original(provider, path)
        self.reads.side_effect = slow
        library = app.extensions['library']
        library._updated = 0
        with ThreadPoolExecutor(max_workers=2) as executor:
            scanning = executor.submit(library.scan, refresh=True)
            try:
                self.assertTrue(entered.wait(2))
                viewing = executor.submit(lambda: app.test_client().get('/library'))
                self.assertEqual(viewing.result(timeout=2).status_code, 200)
            finally:
                release.set()
            scanning.result(timeout=5)

    def test_expired_web_cache_refreshes_once_in_background_without_holding_pages(self):
        app = create_app(settings=self.settings)
        self.addCleanup(app.extensions['jobs'].shutdown)
        client = app.test_client()
        self.assertEqual(client.get('/library').status_code, 200)
        self.write(1, '架空著者B')
        entered, release = Event(), Event()
        original = self.reads.side_effect
        def slow(provider, path):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('fixture timed out')
            return original(provider, path)
        self.reads.reset_mock()
        self.reads.side_effect = slow
        app.extensions['library']._updated = 0
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                response = executor.submit(client.get, '/library').result(timeout=2)
                self.assertIn('架空著者A', response.get_data(as_text=True))
                self.assertTrue(entered.wait(2))
                for path in ('/metadata', '/library', '/'):
                    self.assertEqual(executor.submit(client.get, path).result(timeout=2).status_code, 200)
                self.assertEqual(self.reads.call_count, 1)
        finally:
            release.set()
        app.extensions['jobs']._refresh_future.result(timeout=5)
        self.assertIn('架空著者B', client.get('/library').get_data(as_text=True))
        self.assertEqual(self.reads.call_count, 1)

    def test_failed_background_refresh_retains_snapshot_and_limits_retries(self):
        app = create_app(settings=self.settings)
        self.addCleanup(app.extensions['jobs'].shutdown)
        client = app.test_client()
        client.get('/library')
        app.extensions['library']._updated = 0
        cache_class = 'manga_kit.library.ArchiveMetadataCache'
        with patch(cache_class, side_effect=OSError('private-path-secret')) as reads:
            with self.assertLogs('manga_kit.jobs', level='WARNING') as logs:
                self.assertEqual(client.get('/library').status_code, 200)
                app.extensions['jobs']._refresh_future.result(timeout=2)
            self.assertNotIn('private-path-secret', ''.join(logs.output))
            self.assertEqual(client.get('/library').status_code, 200)
            self.assertEqual(reads.call_count, 1)


class PaginationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        settings = Settings(data_root=Path(temporary.name) / 'state')
        self.app = create_app(settings=settings)
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.client = self.app.test_client()
        result = ScanResult([Series(f'作品{i:03}', f'架空{i:03}', status='ongoing') for i in range(125)])
        self.scan = patch.object(self.app.extensions['library'], 'scan', return_value=result).start()
        self.addCleanup(patch.stopall)

    def test_library_and_metadata_render_at_most_sixty_series_and_reach_the_last(self):
        for endpoint in ('/library', '/metadata'):
            first = self.client.get(endpoint).get_data(as_text=True)
            self.assertIn('架空059', first)
            self.assertNotIn('架空060', first)
        self.assertEqual(first.split('<tbody>')[1].split('</tbody>')[0].count('<tr>'), 60)
        last = self.client.get('/metadata?page=3').get_data(as_text=True)
        self.assertIn('架空124', last)
        self.assertNotIn('架空119', last)
        self.assertIn('3 / 3', last)
        # The library browser appends: a later page keeps the earlier series, as "more" does.
        more = self.client.get('/library?page=3').get_data(as_text=True)
        self.assertIn('架空000', more)
        self.assertIn('架空124', more)
        self.assertIn('125 件中 125 件を表示', more)
        self.assertNotIn('data-more=', more)

    def test_paging_retains_filters_handles_empty_results_and_rejects_invalid_pages(self):
        html = self.client.get('/library?q=架空&status=ongoing').get_data(as_text=True)
        self.assertIn('data-more="2"', html)
        self.assertIn('page=2', html)
        self.assertIn('status=ongoing', html)
        self.assertIn('架空060', self.client.get('/library?q=架空&status=ongoing&page=2').get_data(as_text=True))
        for page in ('0', '-1', 'bad'):
            self.assertEqual(self.client.get('/library?page=' + page).status_code, 400)
        self.assertIn('0 件中 0 件を表示', self.client.get('/library?q=missing').get_data(as_text=True))
        self.assertIn('架空124', self.client.get('/library?page=999999').get_data(as_text=True))
