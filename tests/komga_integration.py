"""Opt-in real Komga integration for each verified write version; never uses an existing user's library."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import tempfile
import time
import unittest
from urllib.parse import urlsplit
from uuid import uuid4
import zipfile
from dataclasses import replace

import requests

from manga_kit import create_app
from manga_kit.config import PathMapping, Settings
from manga_kit.komga import WRITE_VERSIONS, KomgaClient, KomgaError
from manga_kit.operations import OperationService
from manga_kit.conversion import ConversionService
from manga_kit.komga_authors import KomgaAuthorsService
from manga_kit.komga_batch import KomgaBatchService
from manga_kit.komga_import import KomgaReadingImportService
from unittest.mock import patch
from manga_kit.komga_metadata import FIELD_MAP, KomgaMetadataService, selected_metadata
from manga_kit.paths import remote_path
from manga_kit.sample import _png
from manga_kit.state import StateConflict

CONFIRM = 'ISOLATED_TEST_SERVER'


@unittest.skipUnless(os.getenv('KOMGA_INTEGRATION_CONFIRM') == CONFIRM, 'Real Komga integration is opt-in')
class KomgaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.base = os.environ['KOMGA_INTEGRATION_URL'].rstrip('/')
        parsed = urlsplit(self.base)
        if parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or parsed.username or parsed.password:
            self.fail('Integration requires a loopback test server without URL credentials')
        mount = Path(os.environ['KOMGA_INTEGRATION_DIRECTORY']).resolve()
        if not mount.is_dir():
            self.fail('Create and mount the dedicated verification directory before running integration')
        self.run_id = uuid4().hex
        self.root = mount / self.run_id
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        self.admin = requests.Session()
        self.admin.trust_env = False
        self.admin.headers['X-API-Key'] = os.environ['KOMGA_TEST_API_KEY']
        self.addCleanup(self.admin.close)
        info = self.call(self.admin, 'GET', '/actuator/info')
        self.assertIn(info['build']['version'], WRITE_VERSIONS)
        self.folder = self.root / '連載中' / '検証用作品'
        self.folder.mkdir(parents=True)
        for volume in (1, 2):
            with zipfile.ZipFile(self.folder / f'元の名前 第{volume}巻.cbz', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('ComicInfo.xml', f'<ComicInfo><Series>検証用作品{self.run_id}</Series><Writer>架空作者</Writer><Number>{volume}</Number><PageCount>4</PageCount><Pages><Page Image="0" Type="FrontCover"/><Page Image="3" DoublePage="True"/></Pages></ComicInfo>')
                for page in (10, 2, 1, 3):
                    archive.writestr(f'page{page}.png', _png() + f'fictitious-page-{page}'.encode())
        (self.folder / 'series.json').write_text(json.dumps({'reading': 'ケンショウヨウ', 'status': 'completed'}))
        self.before_files = self.hashes()
        payload = {'name': 'manga-kit-integration-' + self.run_id, 'root': '/verification/' + self.run_id,
            'analyzeDimensions': True, 'convertToCbz': False, 'emptyTrashAfterScan': False, 'hashFiles': True,
            'hashKoreader': False, 'hashPages': False, 'importBarcodeIsbn': False, 'importComicInfoBook': True,
            'importComicInfoCollection': False, 'importComicInfoReadList': False, 'importComicInfoSeries': True,
            'importComicInfoSeriesAppendVolume': False, 'importEpubBook': False, 'importEpubSeries': False,
            'importLocalArtwork': False, 'importMylarSeries': False, 'repairExtensions': False, 'scanCbx': True,
            'scanDirectoryExclusions': [], 'scanEpub': False, 'scanForceModifiedTime': False,
            'scanInterval': 'DISABLED', 'scanOnStartup': False, 'scanPdf': False, 'seriesCover': 'FIRST'}
        self.native_library = self.call(self.admin, 'POST', '/api/v1/libraries', payload)
        self.library_payload = payload
        self.addCleanup(self.call, self.admin, 'DELETE', '/api/v1/libraries/' + self.native_library['id'])
        self.call(self.admin, 'POST', '/api/v1/libraries/' + self.native_library['id'] + '/scan')
        self.wait_books()
        self.readers = []
        for n in range(2):
            password = secrets.token_urlsafe(24)
            user = self.call(self.admin, 'POST', '/api/v2/users', {
                'email': f'manga-kit-{self.run_id}-{n}@example.invalid', 'password': password,
                'roles': ['PAGE_STREAMING'], 'sharedLibraries': {'all': False, 'libraryIds': [self.native_library['id']]}})
            self.addCleanup(self.call, self.admin, 'DELETE', '/api/v2/users/' + user['id'])
            session = requests.Session()
            session.trust_env = False
            session.auth = (user['email'], password)
            self.addCleanup(session.close)
            self.readers.append(session)
        settings = Settings(demo=False, library_root=self.root, data_root=Path(state.name), execution_enabled=True,
            komga_url=self.base, api_key_env='KOMGA_TEST_API_KEY',
            path_mappings=(PathMapping('/verification/' + self.run_id, self.root),))
        self.service = OperationService(settings, client=KomgaClient(settings, session=self.admin))

    def call(self, session, method, path, body=None):
        response = session.request(method, self.base + path, json=body, timeout=20, allow_redirects=False)
        self.assertTrue(200 <= response.status_code < 300, f'{method} {path}: HTTP {response.status_code}')
        return response.json() if response.content else None

    def wait_books(self, library_id=None, count=2):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            response = self.call(self.admin, 'POST', '/api/v1/books/list?size=100', {'condition': {'allOf': [
                {'libraryId': {'operator': 'is', 'value': library_id or self.native_library['id']}}, {'deleted': {'operator': 'isFalse'}}]}})
            content = response['content']
            if len(content) == count and all(book.get('fileHash') and book['media']['status'] == 'READY' for book in content):
                return sorted(content, key=lambda book: book['metadata']['numberSort'])
            time.sleep(.5)
        self.fail('Book analysis and hashing did not finish')

    def wait_series_metadata(self, series_id):
        # Komga creates a series from its folder name and applies ComicInfo series
        # metadata in a later refresh task; cold servers can expose the interim title.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            metadata = self.call(self.admin, 'GET', '/api/v1/series/' + series_id)['metadata']
            if metadata['title'] == '検証用作品' + self.run_id:
                return metadata
            time.sleep(.5)
        self.fail('Series metadata refresh did not finish')

    def hashes(self):
        return {path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob('*') if path.is_file()}

    def progress(self, books):
        result = []
        for reader in self.readers:
            values = []
            for book in books:
                value = self.call(reader, 'GET', '/api/v1/books/' + book['id']).get('readProgress')
                # Komga creates a new progress record after a path change. Its
                # creation timestamp changes; reading position/date must survive.
                values.append({key: value[key] for key in ('page', 'completed', 'readDate')} if value else None)
            result.append(values)
        return result

    def test_execute_collision_backup_restore_and_multi_user_progress(self):
        books = self.wait_books()
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'completed': True})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + books[1]['id'] + '/read-progress', {'page': 1})
        before = self.progress(books)
        self.call(self.admin, 'PATCH', '/api/v1/series/' + books[0]['seriesId'] + '/metadata',
                  {'titleSort': 'ケンショウヨウ', 'titleSortLock': True})
        source = self.service.library.scan().series[0]
        self.service.store.save_edit(source.id, {'author': '編集した架空作者'}, 0, {})
        operation = self.service.prepare()
        operation_id, confirmation = operation['plan']['id'], operation['digest']
        target = self.root / operation['plan']['entries'][0]['target']
        target.parent.mkdir(parents=True)
        target.write_bytes(b'existing file must never be overwritten')
        with self.assertRaises(StateConflict):
            self.service.execute(operation_id, confirmation)
        self.assertEqual(target.read_bytes(), b'existing file must never be overwritten')
        target.unlink()
        self.service.execute(operation_id, confirmation)
        moved = self.wait_books()
        self.assertEqual(self.progress(moved), before)
        self.assertEqual(self.service.library.scan(refresh=True).series[0].author, '編集した架空作者')
        series = self.call(self.admin, 'GET', '/api/v1/series/' + moved[0]['seriesId'])
        self.assertEqual(series['metadata']['titleSort'], 'ケンショウヨウ')
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + moved[0]['id'] + '/read-progress', {'page': 3})
        latest = self.progress(moved)
        (self.root / operation['plan']['entries'][0]['target']).unlink()
        self.service.restore(operation_id, confirmation)
        restored = self.wait_books()
        self.assertEqual(self.progress(restored), latest)
        self.assertEqual(self.hashes(), self.before_files)
        self.assertEqual(self.service.library.scan(refresh=True).series[0].author, '編集した架空作者')
        self.assertEqual(self.service.inspect(operation_id)['status'], 'restored')

    def test_web_normalization_zip_to_cbz_preserves_multi_user_progress_and_restores(self):
        folder = self.root / '連載中' / '[架空作者] 正規化検証'
        folder.mkdir()
        for volume in (1, 2):
            with zipfile.ZipFile(folder / f'追加した巻 第{volume}巻.zip', 'w') as archive:
                archive.writestr('ComicInfo.xml', f'<ComicInfo><Series>正規化検証{self.run_id}</Series><Number>{volume}</Number></ComicInfo>')
                for page in range(1, 5):
                    archive.writestr(f'{page:04}.png', _png() + f'normalization-{self.run_id}-{page}'.encode())
        (folder / 'series.json').write_text(json.dumps({'title': '正規化検証', 'reading': 'せいきかけんしょう'}))
        before_files = self.hashes()
        self.call(self.admin, 'POST', '/api/v1/libraries/' + self.native_library['id'] + '/scan')
        identity = lambda book: ('正規化検証' in book['url'], book['metadata']['numberSort'])
        books = sorted(self.wait_books(count=4), key=identity)
        normalized = next(book for book in books if '正規化検証' in book['url'])
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + normalized['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + normalized['id'] + '/read-progress', {'completed': True})
        before = self.progress(books)
        app = create_app(settings=replace(self.service.settings, web_execution_enabled=True))
        self.addCleanup(app.extensions['jobs'].shutdown)
        client, store = app.test_client(), app.extensions['state']
        def completed(job_id):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                job = store.job(job_id, include_result=True)
                if job['status'] not in {'queued', 'running'}:
                    self.assertEqual(job['status'], 'succeeded', job)
                    return job
                time.sleep(.1)
            self.fail('Web normalization did not finish')
        response = client.post('/api/normalization/checks', json={})
        self.assertEqual(response.status_code, 202)
        checked = completed(response.json['id'])
        # Byte-preserving moves and in-place rebuilds share the reviewed plan.
        self.assertEqual(checked['result']['counts']['ready'], 4)
        self.assertEqual(checked['result']['counts']['blocked'], 0)
        confirmation = {'confirm': checked['result']['digest']}
        endpoint = '/api/normalization/checks/' + checked['id']
        response = client.post(endpoint + '/execute', json=confirmation)
        self.assertEqual(response.status_code, 202)
        completed(response.json['id'])
        moved = sorted(self.wait_books(count=4), key=identity)
        self.assertEqual(self.progress(moved), before)
        self.assertTrue(all(book['url'].endswith('.cbz') for book in moved))
        normalized = next(book for book in moved if book['fileHash'] == normalized['fileHash'])
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + normalized['id'] + '/read-progress', {'page': 3})
        latest = self.progress(moved)
        response = client.post(endpoint + '/restore', json=confirmation)
        self.assertEqual(response.status_code, 202)
        completed(response.json['id'])
        restored = sorted(self.wait_books(count=4), key=identity)
        self.assertEqual(self.progress(restored), latest)
        self.assertEqual(self.hashes(), before_files)

    def wait_sidecar_thumbnail(self, kind, identifier):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            thumbnails = self.call(self.admin, 'GET', f'/api/v1/{kind}/{identifier}/thumbnails')
            if any(thumbnail['type'] == 'SIDECAR' for thumbnail in thumbnails):
                return thumbnails
            time.sleep(.5)
        self.fail(f'Komga did not import the {kind} sidecar artwork')

    def test_scoped_auto_normalization_keeps_other_series_and_two_user_progress(self):
        primary = self.wait_books()
        self.wait_series_metadata(primary[0]['seriesId'])
        other = self.root / '連載中' / '対象外の作品'
        other.mkdir()
        with zipfile.ZipFile(other / '対象外 第1巻.cbz', 'w') as archive:
            archive.writestr('ComicInfo.xml', '<ComicInfo><Series>対象外の作品</Series><Number>1</Number></ComicInfo>')
            archive.writestr('0001.png', _png())
        before_files = self.hashes()
        self.call(self.admin, 'POST', '/api/v1/libraries/' + self.native_library['id'] + '/scan')
        self.wait_books(count=3)
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + primary[0]['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + primary[1]['id'] + '/read-progress', {'completed': True})
        before = self.progress(primary)
        app = create_app(settings=replace(self.service.settings, web_execution_enabled=True))
        self.addCleanup(app.extensions['jobs'].shutdown)
        client, store = app.test_client(), app.extensions['state']
        self.assertEqual(client.post('/api/normalization/libraries/refresh', json={}).status_code, 200)
        response = client.post('/api/normalization/checks', json={
            'library': 'komga-' + self.native_library['id'], 'subpath': '連載中/検証用作品', 'auto_execute': True})
        self.assertEqual(response.status_code, 202, response.json)
        def completed(identifier):
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                job = store.job(identifier, include_result=True)
                if job['status'] not in {'queued', 'running'}:
                    self.assertEqual(job['status'], 'succeeded', job)
                    return job
                time.sleep(.1)
            self.fail('Scoped normalization did not finish')
        action = completed(response.json['action_id'])
        self.assertFalse(action['result'].get('skipped'), action)
        checked = completed(response.json['id'])
        self.assertEqual(checked['result']['counts']['scanned'], 2)
        all_books = self.wait_books(count=3)
        selected = sorted([book for book in all_books if '対象外の作品' not in book['url']],
                          key=lambda book: book['metadata']['numberSort'])
        self.assertEqual(len(selected), 2)
        self.assertEqual(self.progress(selected), before)
        operation = app.extensions['normalization'].operations.inspect(checked['result']['operation_id'])
        for book in selected:
            relative = app.extensions['normalization'].operations.guard.relative(book['url'])
            self.assertEqual(book['id'], operation['managed_native']['books'][relative]['id'])
        relative = '連載中/対象外の作品/対象外 第1巻.cbz'
        self.assertEqual(self.hashes()[relative], before_files[relative])
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + selected[0]['id'] + '/read-progress', {'page': 3})
        latest = self.progress(selected)
        restored = client.post('/api/normalization/checks/' + checked['id'] + '/restore', json={'confirm': checked['result']['digest']})
        self.assertEqual(restored.status_code, 202, restored.json)
        completed(restored.json['id'])
        self.assertEqual(self.hashes(), before_files)
        restored_books = sorted([book for book in self.wait_books(count=3) if '対象外の作品' not in book['url']],
                                key=lambda book: book['metadata']['numberSort'])
        self.assertEqual(self.progress(restored_books), latest)

    def test_nested_series_and_local_artwork_move_with_parent_and_restore(self):
        nested = self.folder / '外伝'
        nested.mkdir()
        with zipfile.ZipFile(nested / '外伝 第1巻.cbz', 'w') as archive:
            archive.writestr('ComicInfo.xml', '<ComicInfo><Number>1</Number></ComicInfo>')
            archive.writestr('page1.png', _png() + f'fictitious-nested-{self.run_id}'.encode())
        (self.folder / 'cover.png').write_bytes(_png() + b'fictitious-series-cover')
        (self.folder / '元の名前 第1巻.png').write_bytes(_png() + b'fictitious-book-cover')
        (self.folder / 'メモ.txt').write_text('stays in place')
        self.before_files = self.hashes()
        self.call(self.admin, 'PATCH', '/api/v1/libraries/' + self.native_library['id'],
                  dict(self.library_payload, importLocalArtwork=True))
        self.call(self.admin, 'POST', '/api/v1/libraries/' + self.native_library['id'] + '/scan')
        books = sorted(self.wait_books(count=3), key=lambda book: book['fileHash'])
        first = next(book for book in books if '/外伝/' not in book['url'] and book['metadata']['number'] == '1')
        nested_book = next(book for book in books if '/外伝/' in book['url'])
        self.wait_sidecar_thumbnail('series', first['seriesId'])
        self.wait_sidecar_thumbnail('books', first['id'])
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + first['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + nested_book['id'] + '/read-progress', {'completed': True})
        before = self.progress(books)
        series = self.service.library.scan(refresh=True).series
        parent = next(entry for entry in series if entry.path == '連載中/検証用作品')
        operation = self.service.prepare(selected=[parent.id])
        targets = {entry['source']: entry['target'] for entry in operation['plan']['entries']}
        destination = targets['連載中/検証用作品/元の名前 第1巻.cbz'].rsplit('/', 1)[0]
        self.assertEqual(targets['連載中/検証用作品/外伝/外伝 第1巻.cbz'], destination + '/外伝/外伝 第01巻.cbz')
        self.assertEqual(targets['連載中/検証用作品/cover.png'], destination + '/cover.png')
        self.service.execute(operation['plan']['id'], operation['digest'])
        moved = sorted(self.wait_books(count=3), key=lambda book: book['fileHash'])
        self.assertTrue(all('/連載中/' not in book['url'] for book in moved))
        moved_first = next(book for book in moved if book['fileHash'] == first['fileHash'])
        self.assertEqual(self.progress(moved), before)
        self.wait_sidecar_thumbnail('series', moved_first['seriesId'])
        self.wait_sidecar_thumbnail('books', moved_first['id'])
        self.assertEqual((self.root / '連載中/検証用作品/メモ.txt').read_text(), 'stays in place')
        self.service.restore(operation['plan']['id'], operation['digest'])
        restored = sorted(self.wait_books(count=3), key=lambda book: book['fileHash'])
        self.assertEqual(self.progress(restored), before)
        self.assertEqual(self.hashes(), self.before_files)

    def series_metadata(self, series_id):
        return {key: value for key, value in self.call(self.admin, 'GET', '/api/v1/series/' + series_id)['metadata'].items()
                if key not in {'lastModified', 'created'}}

    def book_metadata(self, book_id):
        return {key: value for key, value in self.call(self.admin, 'GET', '/api/v1/books/' + book_id)['metadata'].items()
                if key not in {'lastModified', 'created'}}

    def test_batch_writes_and_reading_import_across_series_preserve_progress(self):
        second = self.root / '連載中' / '[架空作者ｘ架空作画] 二作目'
        second.mkdir()
        for volume in (1, 2):
            with zipfile.ZipFile(second / f'二作目 第{volume}巻.cbz', 'w') as archive:
                archive.writestr('ComicInfo.xml', f'<ComicInfo><Series>二作目{self.run_id}</Series><Number>{volume}</Number></ComicInfo>')
                archive.writestr('page1.png', _png() + f'fictitious-second-{self.run_id}-{volume}'.encode())
        (second / 'series.json').write_text(json.dumps({'publisher': '架空出版'}, ensure_ascii=False))
        self.before_files = self.hashes()
        self.call(self.admin, 'POST', '/api/v1/libraries/' + self.native_library['id'] + '/scan')
        books = sorted(self.wait_books(count=4), key=lambda book: book['fileHash'])
        first_id = next(book['seriesId'] for book in books if '二作目' not in book['url'])
        second_id = next(book['seriesId'] for book in books if book['seriesId'] != first_id)
        self.wait_series_metadata(first_id)
        deadline = time.monotonic() + 90
        while self.series_metadata(second_id)['title'] != '二作目' + self.run_id:
            self.assertLess(time.monotonic(), deadline, 'second series metadata refresh did not finish')
            time.sleep(.5)
        # The existing KomgaTool stores "カナ 題名" in a locked titleSort.
        self.call(self.admin, 'PATCH', '/api/v1/series/' + second_id + '/metadata',
                  {'titleSort': 'ニサクメ 二作目' + self.run_id, 'titleSortLock': True})
        first_book = next(book for book in books if book['seriesId'] == first_id)
        second_book = next(book for book in books if book['seriesId'] == second_id)
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + first_book['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + second_book['id'] + '/read-progress', {'completed': True})
        progress = self.progress(books)
        series_before = {series_id: self.series_metadata(series_id) for series_id in (first_id, second_id)}
        books_before = {book['id']: self.book_metadata(book['id']) for book in books}
        client = KomgaClient(self.service.settings, session=self.admin)

        importer = KomgaReadingImportService(self.service.settings, self.service.store, client=client)
        imported = importer.prepare()
        self.assertEqual([(target['komga_series_id'], target['reading']) for target in imported['plan']['targets']],
                         [(second_id, 'にさくめ')])
        importer.execute(imported['plan']['id'], imported['digest'])
        readings = {entry.path: entry.reading for entry in importer.library.scan(refresh=True).series}
        self.assertEqual(readings['連載中/[架空作者ｘ架空作画] 二作目'], 'にさくめ')

        batch = KomgaBatchService(self.service.settings, self.service.store, client=client)
        for mode, options in (('metadata', {'fields': ['reading', 'publisher']}), ('authors', {}), ('completion', {})):
            with self.subTest(mode=mode):
                operation = batch.prepare(mode, **options)
                batch.execute(operation['plan']['id'], operation['digest'])
                if mode == 'metadata':
                    first, second_metadata = self.series_metadata(first_id), self.series_metadata(second_id)
                    self.assertEqual((first['titleSort'], first['titleSortLock']), ('けんしょうよう', True))
                    # Fill-only keeps the curated titleSort and fills the empty publisher.
                    self.assertEqual(second_metadata['titleSort'], 'ニサクメ 二作目' + self.run_id)
                    self.assertEqual((second_metadata['publisher'], second_metadata['publisherLock']), ('架空出版', True))
                elif mode == 'authors':
                    second_books = [book for book in books if book['seriesId'] == second_id]
                    for book in second_books:
                        names = [item['name'] for item in self.book_metadata(book['id'])['authors']]
                        self.assertEqual(names[-2:], ['架空作者', '架空作画'])
                else:
                    first = self.series_metadata(first_id)
                    self.assertEqual((first['status'], first['statusLock']), ('ENDED', True))
                    self.assertIn('完結', first['tags'])
                self.assertEqual(self.progress(books), progress)
                batch.restore(operation['plan']['id'], operation['digest'])
                self.assertEqual({series_id: self.series_metadata(series_id) for series_id in series_before}, series_before)
                self.assertEqual({book['id']: self.book_metadata(book['id']) for book in books}, books_before)
        importer.restore(imported['plan']['id'], imported['digest'])
        readings = {entry.path: entry.reading for entry in importer.library.scan(refresh=True).series}
        self.assertEqual(readings['連載中/[架空作者ｘ架空作画] 二作目'], '')
        self.assertEqual(self.progress(books), progress)
        self.assertEqual(self.hashes(), self.before_files)

    def test_converted_pages_metadata_and_original_reading_state(self):
        books = self.wait_books()
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'completed': True})
        before = self.progress(books)
        output = self.root.parent / (self.run_id + '-converted')
        output.mkdir()
        self.addCleanup(shutil.rmtree, output)
        service = ConversionService(self.service.settings, self.service.store)
        selected = sorted(path.relative_to(self.root).as_posix() for path in self.folder.glob('*.cbz'))
        operation = service.prepare(selected, output)
        service.execute(operation['plan']['id'], operation['digest'])
        self.assertEqual(self.hashes(), self.before_files)
        self.assertEqual([book['id'] for book in self.wait_books()], [book['id'] for book in books])
        self.assertEqual(self.progress(self.wait_books()), before)

        payload = dict(self.library_payload, root='/verification/' + output.name,
                       name='converted-integration-' + self.run_id)
        converted_library = self.call(self.admin, 'POST', '/api/v1/libraries', payload)
        self.addCleanup(self.call, self.admin, 'DELETE', '/api/v1/libraries/' + converted_library['id'])
        self.call(self.admin, 'POST', '/api/v1/libraries/' + converted_library['id'] + '/scan')
        converted = self.wait_books(converted_library['id'])
        for original_book, book in zip(books, converted):
            self.assertEqual(book['media']['pagesCount'], 4)
            self.assertEqual(book['metadata']['title'], original_book['metadata']['title'])
            self.assertIsNone(book.get('readProgress'))
            for index, page in enumerate((1, 2, 3, 10), 1):
                response = self.admin.get(self.base + '/api/v1/books/' + book['id'] + f'/pages/{index}?zero_based=false',
                                          timeout=20, allow_redirects=False)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, _png() + f'fictitious-page-{page}'.encode())
        for entry in operation['plan']['entries']:
            with zipfile.ZipFile(self.root / entry['source']) as original, zipfile.ZipFile(output / entry['target']) as normalized:
                self.assertEqual(original.read('ComicInfo.xml'), normalized.read('ComicInfo.xml'))
        service.restore(operation['plan']['id'], operation['digest'])
        self.assertEqual(list(output.rglob('*.cbz')), [])
        self.assertEqual(self.hashes(), self.before_files)
        self.assertEqual([book['id'] for book in self.wait_books()], [book['id'] for book in books])
        self.assertEqual(self.progress(self.wait_books()), before)

    def test_reviewed_series_metadata_apply_restore_preserves_multi_user_reading(self):
        books = self.wait_books()
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'completed': True})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + books[1]['id'] + '/read-progress', {'page': 1})
        endpoint = '/api/v1/series/' + books[0]['seriesId']
        self.call(self.admin, 'PATCH', endpoint + '/metadata', {'publisher': '元の架空出版', 'publisherLock': True,
            'tags': ['既存タグ'], 'tagsLock': False, 'language': 'ja', 'languageLock': True})
        before_metadata = self.call(self.admin, 'GET', endpoint)['metadata']
        before_progress = self.progress(books)
        service = KomgaMetadataService(self.service.settings, self.service.store,
            client=KomgaClient(self.service.settings, session=self.admin))
        source = service.library.scan().series[0]
        service.store.save_edit(source.id, {'reading': 'けんしょうよう', 'publisher': '補完した架空出版',
            'summary': '検証用の架空のあらすじ', 'genres': ['Fantasy'], 'tags': ['旅', '仲間']}, 0, {},
            provenance={'anilist': {'provider': 'anilist', 'item_id': 'fictitious', 'applied_fields': ['genres', 'tags']}})
        fields = ['title', 'reading', 'publisher', 'summary', 'genres', 'tags']
        with self.assertRaises(StateConflict):
            service.prepare(source.id, books[0]['seriesId'], fields)
        operation = service.prepare(source.id, books[0]['seriesId'], fields, replace_locked=True)
        service.execute(operation['plan']['id'], operation['digest'])
        current = self.call(self.admin, 'GET', endpoint)['metadata']
        self.assertEqual(selected_metadata(current, [FIELD_MAP[field] for field in fields]), operation['plan']['after'])
        self.assertEqual(set(current['tags']), {'既存タグ', '旅', '仲間'})
        self.assertEqual(current['language'], 'ja')
        self.assertTrue(current['languageLock'])
        self.assertEqual(self.progress(self.wait_books()), before_progress)
        self.assertEqual([book['id'] for book in self.wait_books()], [book['id'] for book in books])
        self.assertEqual(self.hashes(), self.before_files)
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'page': 3})
        latest_progress = self.progress(books)
        self.call(self.admin, 'PATCH', endpoint + '/metadata', {'tags': ['利用者が後から変更'], 'tagsLock': True})
        with self.assertRaises(StateConflict):
            service.restore(operation['plan']['id'], operation['digest'])
        self.assertEqual(self.call(self.admin, 'GET', endpoint)['metadata']['tags'], ['利用者が後から変更'])
        self.call(self.admin, 'PATCH', endpoint + '/metadata', {'tags': operation['plan']['after']['tags'], 'tagsLock': True})
        service.restore(operation['plan']['id'], operation['digest'])
        restored = self.call(self.admin, 'GET', endpoint)['metadata']
        for field, value in before_metadata.items():
            if field not in {'lastModified'}:
                self.assertEqual(restored[field], value, field)
        self.assertEqual(self.progress(self.wait_books()), latest_progress)
        self.assertEqual([book['id'] for book in self.wait_books()], [book['id'] for book in books])
        self.assertEqual(self.hashes(), self.before_files)

    def test_legacy_lexicographic_page_order_and_original_progress(self):
        books = self.wait_books()
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + books[1]['id'] + '/read-progress', {'completed': True})
        before_progress = self.progress(books)
        output = self.root.parent / (self.run_id + '-lexicographic')
        output.mkdir()
        self.addCleanup(shutil.rmtree, output)
        service = ConversionService(self.service.settings, self.service.store)
        selected = sorted(path.relative_to(self.root).as_posix() for path in self.folder.glob('*.cbz'))
        operation = service.prepare(selected, output, page_order='lexicographic-path')
        service.execute(operation['plan']['id'], operation['digest'])
        payload = dict(self.library_payload, root='/verification/' + output.name,
                       name='lexicographic-integration-' + self.run_id)
        converted_library = self.call(self.admin, 'POST', '/api/v1/libraries', payload)
        self.addCleanup(self.call, self.admin, 'DELETE', '/api/v1/libraries/' + converted_library['id'])
        self.call(self.admin, 'POST', '/api/v1/libraries/' + converted_library['id'] + '/scan')
        for book in self.wait_books(converted_library['id']):
            self.assertEqual(book['media']['pagesCount'], 4)
            for index, page in enumerate((1, 10, 2, 3), 1):
                response = self.admin.get(self.base + '/api/v1/books/' + book['id'] + f'/pages/{index}?zero_based=false',
                                          timeout=20, allow_redirects=False)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, _png() + f'fictitious-page-{page}'.encode())
        service.restore(operation['plan']['id'], operation['digest'])
        self.assertEqual(list(output.rglob('*.cbz')), [])
        self.assertEqual(self.hashes(), self.before_files)
        self.assertEqual(self.progress(self.wait_books()), before_progress)
        self.assertEqual([book['id'] for book in self.wait_books()], [book['id'] for book in books])

    def test_completion_after_move_restores_metadata_and_latest_multi_user_progress(self):
        books = self.wait_books()
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + books[1]['id'] + '/read-progress', {'completed': True})
        original_progress = self.progress(books)
        self.call(self.admin, 'PATCH', '/api/v1/series/' + books[0]['seriesId'] + '/metadata',
                  {'tags': ['連載中', '架空分類'], 'tagsLock': True, 'status': 'ONGOING', 'statusLock': False})
        movement = self.service.prepare()
        self.service.execute(movement['plan']['id'], movement['digest'])
        moved = self.wait_books()
        self.assertEqual(self.progress(moved), original_progress)
        endpoint = '/api/v1/series/' + moved[0]['seriesId']
        before = self.wait_series_metadata(moved[0]['seriesId'])
        service = KomgaMetadataService(self.service.settings, self.service.store,
            client=KomgaClient(self.service.settings, session=self.admin))
        source = service.library.scan(refresh=True).series[0]
        with self.assertRaises(StateConflict):
            service.prepare_completion(source.id, moved[0]['seriesId'])
        operation = service.prepare_completion(source.id, moved[0]['seriesId'], replace_locked=True)
        service.execute(operation['plan']['id'], operation['digest'])
        current = self.call(self.admin, 'GET', endpoint)['metadata']
        self.assertEqual(current['status'], 'ENDED')
        self.assertEqual(set(current['tags']), {'完結', '架空分類'})
        self.assertTrue(current['statusLock'])
        self.assertTrue(current['tagsLock'])
        self.assertEqual(self.progress(self.wait_books()), original_progress)
        self.assertEqual([book['id'] for book in self.wait_books()], [book['id'] for book in moved])
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + moved[0]['id'] + '/read-progress', {'page': 3})
        latest = self.progress(moved)
        self.call(self.admin, 'PATCH', endpoint + '/metadata', {'tags': ['完結', '後から追加'], 'tagsLock': True})
        with self.assertRaises(StateConflict):
            service.restore(operation['plan']['id'], operation['digest'])
        self.call(self.admin, 'PATCH', endpoint + '/metadata', {'tags': operation['plan']['after']['tags'], 'tagsLock': True})
        service.restore(operation['plan']['id'], operation['digest'])
        restored = self.call(self.admin, 'GET', endpoint)['metadata']
        for field, value in before.items():
            if field != 'lastModified':
                self.assertEqual(restored[field], value, field)
        self.assertEqual(self.progress(self.wait_books()), latest)
        self.service.restore(movement['plan']['id'], movement['digest'])
        self.assertEqual(self.hashes(), self.before_files)
        self.assertEqual(self.progress(self.wait_books()), latest)

    def test_per_book_authors_partial_failure_restore_and_latest_progress(self):
        books = self.wait_books()
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + books[1]['id'] + '/read-progress', {'completed': True})
        self.call(self.admin, 'PATCH', '/api/v1/books/' + books[0]['id'] + '/metadata', {'authorsLock': True})
        before = [self.call(self.admin, 'GET', '/api/v1/books/' + book['id'])['metadata'] for book in books]
        progress = self.progress(books)
        service = KomgaAuthorsService(self.service.settings, self.service.store,
            client=KomgaClient(self.service.settings, session=self.admin))
        source = service.library.scan(refresh=True).series[0]
        values = [{'name': '架空原作', 'role': 'writer'}, {'name': '架空・作画', 'role': 'penciller'}]
        with self.assertRaises(StateConflict):
            service.prepare(source.id, books[0]['seriesId'], values)
        selected = [service.guard.relative(books[1]['url'])]
        subset = service.prepare(source.id, books[0]['seriesId'], values, selected=selected)
        service.execute(subset['plan']['id'], subset['digest'])
        self.assertEqual(self.call(self.admin, 'GET', '/api/v1/books/' + books[0]['id'])['metadata'], before[0])
        service.restore(subset['plan']['id'], subset['digest'])
        operation = service.prepare(source.id, books[0]['seriesId'], values, replace_locked=True)
        actual = service._patch_book
        attempts = []
        def lose_second(book, payload):
            actual(book, payload)
            attempts.append(book['id'])
            if len(attempts) == 2:
                raise KomgaError('simulated lost second response')
        with patch.object(service, '_patch_book', side_effect=lose_second), self.assertRaises(StateConflict):
            service.execute(operation['plan']['id'], operation['digest'])
        self.assertEqual(len(attempts), 2)
        for book, metadata in zip(books, before):
            current = self.call(self.admin, 'GET', '/api/v1/books/' + book['id'])['metadata']
            self.assertEqual(current['authors'], metadata['authors'] + values)
            self.assertTrue(current['authorsLock'])
        self.assertEqual(self.progress(self.wait_books()), progress)
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'page': 3})
        latest = self.progress(books)
        self.call(self.admin, 'PATCH', '/api/v1/books/' + books[1]['id'] + '/metadata',
                  {'authors': [{'name': '後からの著者', 'role': 'writer'}]})
        with self.assertRaises(StateConflict):
            service.restore(operation['plan']['id'], operation['digest'])
        target = next(book for book in operation['plan']['books'] if book['id'] == books[1]['id'])
        self.call(self.admin, 'PATCH', '/api/v1/books/' + books[1]['id'] + '/metadata', target['after'])
        service.restore(operation['plan']['id'], operation['digest'])
        for book, metadata in zip(books, before):
            current = self.call(self.admin, 'GET', '/api/v1/books/' + book['id'])['metadata']
            for key, value in metadata.items():
                if key != 'lastModified':
                    self.assertEqual(current[key], value, key)
        self.assertEqual(self.progress(self.wait_books()), latest)
        self.assertEqual([book['id'] for book in self.wait_books()], [book['id'] for book in books])
        self.assertEqual(self.hashes(), self.before_files)

    def test_smart_shelves_and_two_reader_api_views_preserve_private_shelves_and_progress(self):
        from manga_kit.config import ToolsSettings
        from manga_kit.feeds import season
        books = self.wait_books()
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'page': 3})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'completed': True})
        before = self.progress(books)
        keys = [self.call(reader, 'POST', '/api/v2/users/me/api-keys', {'comment': 'isolated reader view'})['key'] for reader in self.readers]
        settings = replace(self.service.settings, web_execution_enabled=True, tools=ToolsSettings(reading_accounts=(
            {'name': '検証読者A', 'api_key_env': 'ISOLATED_READER_A'}, {'name': '検証読者B', 'api_key_env': 'ISOLATED_READER_B'})))
        with patch.dict(os.environ, {'ISOLATED_READER_A': keys[0], 'ISOLATED_READER_B': keys[1]}):
            app = create_app(settings=settings)
            self.addCleanup(app.extensions['jobs'].shutdown)
            inventory = app.extensions['inventory']; inventory.refresh()
            row = next(row for row in inventory.cached()['series'] if row['books'])
            app.extensions['state'].cache('feed:anime', {**season(), 'entries': [{'id': 'fictitious-anime', 'title': row['title']}]})
            personal = self.call(self.admin, 'POST', '/api/v1/collections', {'name': '個人の本棚-' + self.run_id, 'ordered': False, 'seriesIds': [books[0]['seriesId']]})
            self.addCleanup(self.call, self.admin, 'DELETE', '/api/v1/collections/' + personal['id'])
            shelves = app.extensions['smart_shelves']
            operation = shelves.prepare()
            try:
                shelves.execute(operation['plan']['id'], operation['digest'])
                self.assertEqual(self.call(self.admin, 'GET', '/api/v1/collections/' + personal['id']), personal)
                result = app.extensions['reading'].refresh()
                self.assertEqual(result['accounts'], 2, result)
                first = app.extensions['reading'].read('検証読者A')['entries'][0]
                second = app.extensions['reading'].read('検証読者B')['entries'][0]
                self.assertEqual(next(book['progress']['page'] for book in first['books'] if book['komga_id'] == books[0]['id']), 3)
                self.assertTrue(next(book['progress']['completed'] for book in second['books'] if book['komga_id'] == books[0]['id']))
                self.assertEqual(self.progress(books), before)
            finally:
                state = shelves.inspect(operation['plan']['id'])
                if state['status'] == 'completed':
                    shelves.execute(operation['plan']['id'], operation['digest'], restore=True)
            self.assertEqual(self.progress(books), before)
            self.assertEqual(self.hashes(), self.before_files)

    def test_managed_archive_rebuild_retains_two_user_progress_and_restores_originals(self):
        from manga_kit.normalization import NormalizationOperations
        books = self.wait_books()
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + books[1]['id'] + '/read-progress', {'completed': True})
        before = self.progress(books)
        self.call(self.admin, 'PATCH', '/api/v1/series/' + books[0]['seriesId'] + '/metadata', {'titleSort': 'ケンショウヨウ', 'titleSortLock': True})
        app = create_app(settings=replace(self.service.settings, web_execution_enabled=True))
        self.addCleanup(app.extensions['jobs'].shutdown)
        service = app.extensions['normalization']
        check = service.check(lambda *_: None)
        self.assertTrue(check['operation_id'], check.get('preparation_error'))
        self.assertTrue(any(row.get('rebuild') for row in check['items']))
        self.assertEqual(self.hashes(), self.before_files)
        operation = service.operations.execute(check['operation_id'], check['digest'])
        rebuilt = self.wait_books()
        self.assertEqual(self.progress(rebuilt), before)
        native = operation['managed_native']['books']
        for book in rebuilt:
            relative = service.operations.guard.relative(book['url'])
            self.assertEqual(book['id'], native[relative]['id'])
            with zipfile.ZipFile(self.root / relative) as archive:
                self.assertEqual([name for name in archive.namelist() if name.endswith('.png')], ['0001.png', '0002.png', '0003.png', '0004.png'])
                self.assertEqual(archive.read('0002.png'), _png() + b'fictitious-page-10')
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + rebuilt[0]['id'] + '/read-progress', {'page': 3})
        latest = self.progress(rebuilt)
        service.operations.restore(check['operation_id'], check['digest'])
        self.assertEqual(self.hashes(), self.before_files)
        self.assertEqual(self.progress(self.wait_books()), latest)

    def test_managed_rebuild_failure_after_source_removal_can_restore_progress(self):
        from manga_kit.normalization import NormalizationOperations
        books = self.wait_books()
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + books[1]['id'] + '/read-progress', {'completed': True})
        before = self.progress(books)
        app = create_app(settings=replace(self.service.settings, web_execution_enabled=True))
        self.addCleanup(app.extensions['jobs'].shutdown)
        service = app.extensions['normalization']; check = service.check(lambda *_: None)
        self.assertTrue(check['operation_id'], check.get('preparation_error'))
        original = NormalizationOperations._remove
        attempts = []
        def fail_after_remove(*args):
            original(*args)
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError('simulated failure in the replacement gap')
        with patch.object(NormalizationOperations, '_remove', side_effect=fail_after_remove):
            with self.assertRaises(StateConflict):
                service.operations.execute(check['operation_id'], check['digest'])
        service.operations.restore(check['operation_id'], check['digest'])
        self.assertEqual(self.hashes(), self.before_files)
        self.assertEqual(self.progress(self.wait_books()), before)

    def test_managed_rar_to_cbz_rebuild_and_restore_preserve_progress(self):
        from archive_fixtures import stored_rar
        for path in self.folder.glob('*.cbz'):
            with zipfile.ZipFile(path) as archive:
                members = [(name, archive.read(name)) for name in archive.namelist()]
            stored_rar(path.with_suffix('.rar'), members)
            path.unlink()
        self.call(self.admin, 'POST', '/api/v1/libraries/' + self.native_library['id'] + '/scan')
        deadline = time.monotonic() + 90
        while True:
            books = self.wait_books()
            if all(book['url'].endswith('.rar') for book in books):
                break
            self.assertLess(time.monotonic(), deadline, 'RAR scan did not finish')
            time.sleep(.5)
        original = self.hashes()
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + books[1]['id'] + '/read-progress', {'completed': True})
        before = self.progress(books)
        app = create_app(settings=replace(self.service.settings, web_execution_enabled=True))
        self.addCleanup(app.extensions['jobs'].shutdown)
        service = app.extensions['normalization']; check = service.check(lambda *_: None)
        self.assertTrue(check['operation_id'], check.get('preparation_error'))
        service.operations.execute(check['operation_id'], check['digest'])
        rebuilt = self.wait_books()
        self.assertTrue(all(book['url'].endswith('.cbz') for book in rebuilt))
        self.assertEqual(self.progress(rebuilt), before)
        service.operations.restore(check['operation_id'], check['digest'])
        self.assertEqual(self.hashes(), original)
        self.assertEqual(self.progress(self.wait_books()), before)

    def test_managed_cover_reorder_of_one_book_preserves_other_books_and_progress(self):
        books = self.wait_books()
        self.call(self.readers[0], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'page': 2})
        self.call(self.readers[1], 'PATCH', '/api/v1/books/' + books[0]['id'] + '/read-progress', {'completed': True})
        before = self.progress(books)
        app = create_app(settings=replace(self.service.settings, web_execution_enabled=True)); self.addCleanup(app.extensions['jobs'].shutdown)
        inventory = app.extensions['inventory']; inventory.refresh()
        book = next(book for row in inventory.cached()['series'] for book in row['books'] if book.get('komga_id') == books[0]['id'])
        editor = app.extensions['page_editor']; review = editor.review(book['id'])
        order = [review['pages'][-1], *review['pages'][:-1]]
        operation = editor.prepare_book(book['id'], order, fingerprint=review['fingerprint'])
        editor.execute(operation['plan']['id'], operation['digest'])
        after = self.wait_books()
        self.assertEqual([book['id'] for book in after], [book['id'] for book in books])
        self.assertEqual(self.progress(after), before)
        self.assertEqual(hashlib.sha256((self.root / app.extensions['page_editor'].guard.relative(books[1]['url'])).read_bytes()).hexdigest(),
                         self.before_files[app.extensions['page_editor'].guard.relative(books[1]['url'])])
        editor.restore(operation['plan']['id'], operation['digest'])
        self.assertEqual(self.hashes(), self.before_files)
        self.assertEqual(self.progress(self.wait_books()), before)


if __name__ == '__main__':
    unittest.main()
