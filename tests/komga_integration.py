"""Opt-in real Komga 1.29.0 integration; never uses an existing user's library."""
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

import requests

from manga_kit.config import PathMapping, Settings
from manga_kit.komga import KomgaClient
from manga_kit.operations import OperationService
from manga_kit.conversion import ConversionService
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
        self.assertEqual(info['build']['version'], '1.29.0')
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

    def wait_books(self, library_id=None):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            response = self.call(self.admin, 'GET', '/api/v1/books?library_id=' + (library_id or self.native_library['id']) + '&size=100')
            content = response['content']
            if len(content) == 2 and all(book.get('fileHash') and book['media']['status'] == 'READY' for book in content):
                return sorted(content, key=lambda book: book['metadata']['numberSort'])
            time.sleep(.5)
        self.fail('Book analysis and hashing did not finish')

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


if __name__ == '__main__':
    unittest.main()
