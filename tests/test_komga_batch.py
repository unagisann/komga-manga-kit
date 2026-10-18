from collections import Counter
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from manga_kit import create_app
from manga_kit.cli import main
from manga_kit.config import ConfigError, PathMapping, Settings
from manga_kit.filesystem import LibraryFilesystem
from manga_kit.komga import KomgaError
from manga_kit.komga_batch import KomgaBatchService
from manga_kit.komga_import import KomgaReadingImportService, reading_from_title_sort
from manga_kit.komga_metadata import KomgaMetadataService
from manga_kit.library import scan_library
from manga_kit.organizer import plan_organization
from manga_kit.state import StateConflict


def metadata(title, **values):
    result = {'title': title, 'titleSort': title, 'summary': '', 'publisher': '', 'genres': [], 'tags': [],
              'status': 'ONGOING', 'language': 'ja'}
    result.update(values)
    for key in list(result):
        result.setdefault(key + 'Lock', False)
    return result


class FakeBatchKomga:
    """Komga 1.29 list/search/PATCH behaviour with small pages and injected failures."""

    def __init__(self, data):
        self.data, self.patches, self.lists = data, [], Counter()
        self.fail_on = self.lose_on = None
        self.sent = 0

    def get(self, path):
        if path == '/actuator/info':
            return {'build': {'version': '1.29.1'}}
        if path == '/api/v1/libraries':
            return deepcopy(self.data['libraries'])
        if path.startswith('/api/v1/series/'):
            return deepcopy(self.data['series'][path.rsplit('/', 1)[1]])
        raise AssertionError(path)

    def request(self, method, path, body=None, **params):
        if method == 'POST' and path in {'/api/v1/books/list', '/api/v1/series/list'}:
            if params['page'] == 0:
                self.lists[path] += 1
            conditions = body['condition']['allOf']
            values = list(self.data['books'].values() if path.endswith('books/list') else self.data['series'].values())
            for condition in conditions:
                if 'libraryId' in condition:
                    values = [item for item in values if item['libraryId'] == condition['libraryId']['value']]
                if 'seriesId' in condition:
                    values = [item for item in values if item['seriesId'] == condition['seriesId']['value']]
                if 'deleted' in condition:
                    values = [item for item in values if not item.get('deleted')]
            # Two items per page exercises pagination.
            page = values[params['page'] * 2:(params['page'] + 1) * 2]
            return {'content': deepcopy(page), 'last': (params['page'] + 1) * 2 >= len(values)}
        if method == 'PATCH':
            self.sent += 1
            if self.sent == self.fail_on:
                raise KomgaError('injected failure before the write')
            kind, identifier = path.split('/')[3:5]
            target = self.data['series' if kind == 'series' else 'books'][identifier]['metadata']
            target.update(deepcopy(body))
            self.patches.append((path, deepcopy(body)))
            if self.sent == self.lose_on:
                raise KomgaError('response lost after the write')
            return None
        raise AssertionError((method, path))


@unittest.skipUnless(os.name == 'posix', 'Confirmed CLI operations support POSIX')
class BatchFixture(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / 'library'
        layout = {'連載中/[架空一郎] 作品A': (2, {'reading': 'サクヒンエー', 'publisher': '架空出版', 'summary': '架空のあらすじ'}),
                  '連載中/[架空二郎ｘ架空三郎] 作品B': (2, None),
                  '完結/[架空四郎] 作品C': (1, {'publisher': '完結出版'}),
                  '連載中/作品D': (1, None)}
        for folder, (count, sidecar) in layout.items():
            directory = self.library / folder
            directory.mkdir(parents=True)
            title = folder.rsplit(' ', 1)[-1].rsplit('/', 1)[-1]
            for volume in range(1, count + 1):
                with zipfile.ZipFile(directory / f'{title} 第{volume}巻.cbz', 'w') as archive:
                    archive.writestr('ComicInfo.xml', f'<ComicInfo><Number>{volume}</Number></ComicInfo>')
                    archive.writestr('page.png', f'fictitious {folder} {volume}')
            if sidecar:
                (directory / 'series.json').write_text(json.dumps(sidecar, ensure_ascii=False))
        self.settings = Settings(demo=False, library_root=self.library, data_root=self.root / 'state', execution_enabled=True,
                                 komga_url='http://127.0.0.1:25602', path_mappings=(PathMapping('/data', self.library),))
        remote = {'作品A': ('series-a', metadata('作品A', summary='Komgaのあらすじ')),
                  '作品B': ('series-b', metadata('作品B', titleSort='サクヒンビー 作品B', publisher='Komga出版')),
                  '作品C': ('series-c', metadata('作品C', tags=['連載中', '架空分類']))}
        self.data = {'libraries': [{'id': 'lib', 'root': 'file:///data', 'hashFiles': True, 'emptyTrashAfterScan': False,
                                    'scanOnStartup': False, 'scanInterval': 'DISABLED', 'convertToCbz': False,
                                    'repairExtensions': False}], 'series': {}, 'books': {}}
        for folder in layout:
            title = folder.rsplit(' ', 1)[-1].rsplit('/', 1)[-1]
            if title not in remote:
                continue
            series_id, values = remote[title]
            self.data['series'][series_id] = {'id': series_id, 'libraryId': 'lib', 'url': 'file:///data/' + folder,
                                              'deleted': False, 'metadata': values}
            for index, book in enumerate(sorted((self.library / folder).glob('*.cbz'))):
                book_id = f'{series_id}-book-{index}'
                self.data['books'][book_id] = {
                    'id': book_id, 'libraryId': 'lib', 'seriesId': series_id, 'deleted': False,
                    'url': 'file:///data/' + book.relative_to(self.library).as_posix(), 'fileHash': f'hash-{book_id}',
                    'sizeBytes': book.stat().st_size, 'media': {'status': 'READY'},
                    'metadata': {'title': book.stem, 'authors': [], 'authorsLock': False}}
        self.client = FakeBatchKomga(self.data)
        self.service = KomgaBatchService(self.settings, client=self.client)
        self.series = {entry.title: entry for entry in self.service.library.scan(refresh=True).series}

    def run_op(self, service, operation, action):
        return getattr(service, action)(operation['plan']['id'], operation['digest'])

    def files(self):
        return {path: path.read_bytes() for path in self.library.rglob('*') if path.is_file()}

    def no_content_hashing(self):
        return patch.object(LibraryFilesystem, 'snapshot', side_effect=AssertionError('archives must not be hashed'))


class KomgaBatchTests(BatchFixture):
    def test_prepare_matches_paths_without_hashing_lists_once_and_fills_only_empty_values(self):
        before = self.files()
        with self.no_content_hashing():
            operation = self.service.prepare('metadata', fields=['reading', 'publisher', 'summary'])
        plan = operation['plan']
        self.assertEqual(self.client.lists, Counter({'/api/v1/books/list': 1, '/api/v1/series/list': 1}))
        self.assertEqual([target['series_title'] for target in plan['targets']], ['作品A', '作品C'])
        first = plan['targets'][0]
        # titleSort equal to the title counts as empty; an existing summary is kept.
        self.assertEqual(first['after']['titleSort'], 'さくひんえー')
        self.assertEqual(first['after']['publisher'], '架空出版')
        self.assertEqual(first['after']['summary'], 'Komgaのあらすじ')
        self.assertEqual({change['field'] for change in first['changes']}, {'reading', 'publisher'})
        self.assertEqual(first['komga']['series_id'], 'series-a')
        self.assertEqual(plan['skipped'], [{'series_id': self.series['作品D'].id, 'series_title': '作品D',
                                            'reason': 'Komgaに登録されていない巻があります。Komgaのスキャンを確認してください。'}])
        self.assertEqual(plan['counts'], {'unchanged': 1, 'targets': 2, 'skipped': 1})
        self.assertEqual(self.client.patches, [])
        self.assertEqual(self.files(), before)

    def test_overwrite_and_locks_are_explicit(self):
        operation = self.service.prepare('metadata', fields=['summary'], overwrite=True)
        self.assertEqual(operation['plan']['targets'][0]['after']['summary'], '架空のあらすじ')
        self.data['series']['series-a']['metadata']['summaryLock'] = True
        with self.assertRaises(ConfigError):
            self.service.prepare('metadata', fields=['summary'], overwrite=True)
        locked = self.service.prepare('metadata', fields=['summary', 'publisher'], overwrite=True, replace_locked=True)
        self.assertEqual(locked['plan']['targets'][0]['after']['summary'], '架空のあらすじ')
        for kwargs in ({'fields': None}, {'fields': ['status']}, {'fields': ['summary', 'summary']}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ConfigError):
                self.service.prepare('metadata', **kwargs)
        with self.assertRaises(ConfigError):
            self.service.prepare('authors', fields=['summary'])

    def test_execute_restore_and_history_without_hashing(self):
        books = deepcopy(self.data['books'])
        original = deepcopy(self.data['series'])
        operation = self.service.prepare('metadata', fields=['reading', 'publisher'])
        self.client.lists.clear()
        with self.no_content_hashing():
            self.run_op(self.service, operation, 'execute')
        self.assertEqual(self.data['series']['series-a']['metadata']['titleSort'], 'さくひんえー')
        self.assertTrue(self.data['series']['series-a']['metadata']['titleSortLock'])
        self.assertEqual(self.data['series']['series-c']['metadata']['publisher'], '完結出版')
        # One listing before and one after the writes, regardless of the number of series.
        self.assertEqual(self.client.lists, Counter({'/api/v1/books/list': 2, '/api/v1/series/list': 2}))
        self.assertEqual(self.data['books'], books)
        self.assertEqual(self.service.inspect(operation['plan']['id'])['attempted'], [0, 1])
        with self.no_content_hashing():
            self.run_op(self.service, operation, 'restore')
        self.assertEqual(self.data['series'], original)
        jobs = self.service.store.jobs()
        self.assertEqual([(job['kind'], job['status']) for job in jobs],
                         [('komga_batch_restore', 'succeeded'), ('komga_batch', 'succeeded')])
        result = self.service.store.job(jobs[1]['id'], include_result=True)['result']
        self.assertEqual((result['series'], result['mode']), (2, 'metadata'))
        with self.assertRaises(StateConflict):
            self.run_op(self.service, operation, 'execute')

    def test_failure_on_second_series_restores_only_the_sent_series(self):
        original = deepcopy(self.data['series'])
        operation = self.service.prepare('metadata', fields=['publisher'])
        self.client.fail_on = 2
        with self.assertRaises(StateConflict):
            self.run_op(self.service, operation, 'execute')
        state = self.service.inspect(operation['plan']['id'])
        self.assertEqual((state['status'], state['attempted']), ('failed', [0, 1]))
        self.assertEqual(self.data['series']['series-a']['metadata']['publisher'], '架空出版')
        self.assertEqual(self.data['series']['series-c'], original['series-c'])
        self.client.fail_on = None
        self.run_op(self.service, operation, 'restore')
        self.assertEqual(self.data['series'], original)
        self.assertEqual(len(self.client.patches), 2)

    def test_lost_response_is_not_retried_and_both_series_restore_in_reverse_order(self):
        original = deepcopy(self.data['series'])
        operation = self.service.prepare('metadata', fields=['publisher'])
        self.client.lose_on = 2
        with self.assertRaises(StateConflict):
            self.run_op(self.service, operation, 'execute')
        self.assertEqual(self.data['series']['series-c']['metadata']['publisher'], '完結出版')
        self.client.lose_on = None
        self.run_op(self.service, operation, 'restore')
        self.assertEqual(self.data['series'], original)
        self.assertEqual([path for path, _ in self.client.patches[-2:]],
                         ['/api/v1/series/series-c/metadata', '/api/v1/series/series-a/metadata'])

    def test_changes_after_planning_stop_before_any_write(self):
        for change in ('remote', 'archive', 'edit', 'book'):
            with self.subTest(change=change):
                operation = self.service.prepare('metadata', fields=['publisher'])
                if change == 'remote':
                    self.data['series']['series-c']['metadata']['publisher'] = '他の人が編集'
                elif change == 'archive':
                    book = next((self.library / '完結/[架空四郎] 作品C').glob('*.cbz'))
                    os.utime(book, ns=(1, 1))
                elif change == 'edit':
                    self.service.store.save_edit(self.series['作品C'].id, {'publisher': '編集'}, 0, {})
                else:
                    self.data['books']['series-c-book-0']['fileHash'] = 'reanalysed'
                with self.assertRaises(StateConflict):
                    self.run_op(self.service, operation, 'execute')
                self.assertEqual(self.client.patches, [])
                self.setUp()

    def test_restore_refuses_values_edited_after_apply(self):
        operation = self.service.prepare('metadata', fields=['publisher'])
        self.run_op(self.service, operation, 'execute')
        self.data['series']['series-a']['metadata']['publisher'] = '反映後に編集'
        with self.assertRaises(StateConflict):
            self.run_op(self.service, operation, 'restore')
        self.assertEqual(self.data['series']['series-c']['metadata']['publisher'], '完結出版')

    def test_lost_newest_journal_allows_recovery_of_every_series(self):
        original = deepcopy(self.data['series'])
        operation = self.service.prepare('metadata', fields=['publisher'])
        self.run_op(self.service, operation, 'execute')
        folder = self.service._folder(operation['plan']['id'])
        (folder / 'manifest.json').write_text('{broken')
        self.run_op(self.service, operation, 'restore')
        self.assertEqual(self.data['series'], original)

    def test_size_mismatch_partial_series_and_unanalysed_books_are_skipped(self):
        self.data['books']['series-a-book-0']['sizeBytes'] += 1
        self.data['books']['series-c-book-0']['media']['status'] = 'OUTDATED'
        self.data['books']['extra'] = dict(self.data['books']['series-b-book-0'], id='extra',
                                           url='file:///data/連載中/[架空二郎ｘ架空三郎] 作品B/別の巻.cbz')
        with self.assertRaises(ConfigError) as raised:
            self.service.prepare('metadata', fields=['publisher'], overwrite=True)
        self.assertIn('サイズが異なります', str(raised.exception))

    def test_completion_targets_only_completed_series(self):
        operation = self.service.prepare('completion')
        target, = operation['plan']['targets']
        self.assertEqual(target['series_title'], '作品C')
        self.assertEqual(target['after']['tags'], ['完結', '架空分類'])
        self.assertEqual((target['after']['status'], target['after']['statusLock']), ('ENDED', True))
        selected = self.service.prepare('completion', [self.series['作品A'].id, self.series['作品C'].id])
        self.assertEqual(selected['plan']['skipped'][0]['reason'], 'このツールで完結にしていません。')
        self.run_op(self.service, operation, 'execute')
        self.assertEqual(self.data['series']['series-c']['metadata']['status'], 'ENDED')

    def test_authors_from_local_names_merge_per_book_and_restore(self):
        self.data['books']['series-b-book-1']['metadata']['authors'] = [{'name': '既存の作者', 'role': 'editor'}]
        original = deepcopy(self.data['books'])
        operation = self.service.prepare('authors')
        targets = {target['series_title']: target for target in operation['plan']['targets']}
        self.assertEqual(targets['作品B']['authors'], [{'name': '架空二郎', 'role': 'writer'}, {'name': '架空三郎', 'role': 'writer'}])
        self.assertEqual(len(targets['作品B']['books']), 2)
        self.run_op(self.service, operation, 'execute')
        self.assertEqual(self.data['books']['series-b-book-1']['metadata']['authors'], [
            {'name': '既存の作者', 'role': 'editor'}, {'name': '架空二郎', 'role': 'writer'}, {'name': '架空三郎', 'role': 'writer'}])
        self.assertTrue(self.data['books']['series-b-book-1']['metadata']['authorsLock'])
        self.run_op(self.service, operation, 'restore')
        self.assertEqual(self.data['books'], original)

    def test_history_renders_batch_results_escaped_and_web_cannot_start_them(self):
        self.data['series']['series-c']['metadata']['title'] = '作品C'
        (self.library / '完結/[架空四郎] 作品C/series.json').write_text(json.dumps({'publisher': '<script>架空</script>'}))
        operation = self.service.prepare('metadata', fields=['publisher'])
        self.run_op(self.service, operation, 'execute')
        app = create_app(settings=self.settings)
        self.addCleanup(app.extensions['jobs'].shutdown)
        client = app.test_client()
        job = self.service.store.jobs()[0]
        html = client.get('/jobs/' + job['id']).get_data(as_text=True)
        self.assertIn('Komgaへの一括反映', html)
        self.assertIn('2作品の作品情報をKomgaへ反映しました', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>架空', html)
        self.assertEqual(client.post('/api/jobs', json={'kind': 'komga_batch'}).status_code, 400)

    def test_cli_prints_reviewable_summary_without_file_signatures(self):
        config = self.root / 'config.yaml'
        config.write_text(json.dumps({'app': {'demo': False}, 'library': {'root': str(self.library)},
            'data': {'root': str(self.root / 'state')}, 'execution': {'enabled': True},
            'komga': {'base_url': 'http://127.0.0.1:25602', 'path_mappings': [{'komga': '/data', 'local': str(self.library)}]}}))

        def command(arguments):
            output = io.StringIO()
            with patch('manga_kit.operations.KomgaClient', return_value=self.client), \
                    patch('sys.argv', ['manga-kit', '--config', str(config), *arguments]), redirect_stdout(output):
                self.assertEqual(main(), 0)
            return json.loads(output.getvalue())

        summary = command(['prepare-komga-batch', '--mode', 'metadata', '--all', '--field', 'publisher'])
        self.assertNotIn('signature', json.dumps(summary))
        self.assertEqual([target['series_title'] for target in summary['targets']], ['作品A', '作品C'])
        self.assertEqual(summary['targets'][0]['komga_series_id'], 'series-a')
        executed = command(['execute', summary['operation_id'], '--confirm', summary['digest']])
        self.assertEqual((executed['status'], executed['attempted']), ('completed', 2))
        restored = command(['restore', summary['operation_id'], '--confirm', summary['digest']])
        self.assertEqual(restored['status'], 'restored')


class SingleSeriesTests(BatchFixture):
    def test_komga_id_is_resolved_from_paths_and_books_are_listed_once(self):
        service = KomgaMetadataService(self.settings, client=self.client)
        with LibraryFilesystem(self.library) as fs:
            for book in self.data['books'].values():
                book['fileHash'] = fs.snapshot(book['url'].removeprefix('file:///data/'))['komga_hash']
        original = self.client.request

        def legacy(method, path, body=None, **params):
            # The single-series guard lists all books and the trash without a library filter.
            if path == '/api/v1/books/list' and 'deleted' in body['condition']:
                self.client.lists[path + (' trash' if body['condition']['deleted']['operator'] == 'isTrue' else '')] += 1
                values = [] if body['condition']['deleted']['operator'] == 'isTrue' else list(self.data['books'].values())
                for book in values:
                    book.setdefault('readProgress', None)
                return {'content': deepcopy(values), 'last': True}
            return original(method, path, body, **params)

        with patch.object(self.client, 'request', side_effect=legacy):
            operation = service.prepare(self.series['作品C'].id, None, ['publisher'])
        self.assertEqual(operation['plan']['komga']['series_id'], 'series-c')
        self.assertEqual(self.client.lists, Counter({'/api/v1/books/list': 1, '/api/v1/books/list trash': 1}))


class ReadingImportTests(BatchFixture):
    def test_reading_from_title_sort(self):
        cases = [('サクヒンビー 作品B', '作品B', 'サクヒンビー'), ('ｻｸﾋﾝ 作品', '作品', 'サクヒン'),
                 ('ソウソウ ノ フリーレン', '葬送のフリーレン', 'ソウソウノフリーレン'), ('作品B', '作品B', ''),
                 ('ハイキュー!!', 'ハイキュー!!', ''), ('Sakuhin', '作品', ''), ('ー 作品', '作品', ''), (None, '作品', '')]
        for title_sort, title, expected in cases:
            with self.subTest(title_sort=title_sort):
                self.assertEqual(reading_from_title_sort(title_sort, title), expected)

    def test_import_fills_only_missing_readings_and_restores_in_one_step(self):
        service = KomgaReadingImportService(self.settings, client=self.client)
        before = self.files()
        with self.no_content_hashing():
            operation = service.prepare()
        target, = operation['plan']['targets']
        self.assertEqual((target['series_title'], target['reading'], target['row']), ('作品B', 'さくひんびー', 'さ'))
        self.assertEqual(operation['plan']['counts'], {'reading_set': 1, 'no_reading': 1, 'targets': 1, 'skipped': 1})
        self.run_op(service, operation, 'execute')
        edits = service.store.edits()[self.series['作品B'].id]
        self.assertEqual(edits['fields'], {'reading': 'さくひんびー'})
        self.assertEqual(edits['provenance']['komga']['item_id'], 'series-b')
        scanned = service.library.scan(refresh=True)
        imported = next(entry for entry in scanned.series if entry.title == '作品B')
        self.assertIn('Komga', imported.sources)
        self.assertEqual(plan_organization([imported], self.settings.layout)['counts']['blocked'], 0)
        self.assertEqual(self.client.patches, [])
        self.assertEqual(self.files(), before)
        self.run_op(service, operation, 'restore')
        self.assertEqual(service.store.edits()[self.series['作品B'].id]['fields'], {})
        with self.assertRaises(StateConflict):
            self.run_op(service, operation, 'execute')

    def test_edits_after_planning_or_import_are_never_overwritten(self):
        service = KomgaReadingImportService(self.settings, client=self.client)
        operation = service.prepare([self.series['作品B'].id])
        service.store.save_edit(self.series['作品B'].id, {'reading': 'てどり'}, 0, {})
        with self.assertRaises(StateConflict):
            self.run_op(service, operation, 'execute')
        self.assertEqual(service.store.edits()[self.series['作品B'].id]['fields'], {'reading': 'てどり'})
        service.store.save_edit(self.series['作品B'].id, {}, 1, {})
        operation = service.prepare([self.series['作品B'].id])
        self.run_op(service, operation, 'execute')
        service.store.save_edit(self.series['作品B'].id, {'reading': 'あとから'}, 3, {})
        with self.assertRaises(StateConflict):
            self.run_op(service, operation, 'restore')
        self.assertEqual(service.store.edits()[self.series['作品B'].id]['fields'], {'reading': 'あとから'})


class ScanLimitTests(unittest.TestCase):
    def test_default_limit_and_near_limit_warning(self):
        self.assertEqual(Settings().max_files, 50000)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(9):
                with zipfile.ZipFile(root / f'作品 第{index + 1}巻.cbz', 'w') as archive:
                    archive.writestr('page.png', b'x')
            result = scan_library(Settings(demo=False, library_root=root, max_files=10))
            self.assertFalse(result.truncated)
            self.assertIn('90%', result.warnings[-1])
            self.assertEqual(scan_library(Settings(demo=False, library_root=root, max_files=100)).warnings, [])


if __name__ == '__main__':
    unittest.main()
