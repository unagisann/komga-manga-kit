from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch
from xml.etree.ElementTree import fromstring
import zipfile

from manga_kit import create_app
from manga_kit.ai_assist import summary_flags
from manga_kit.config import ConfigError, Settings
from manga_kit.smart_shelves import SmartShelves
from manga_kit.state import StateConflict


class ToolFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        folder = self.root / 'library' / '連載中' / '[架空作者] 架空の作品'
        folder.mkdir(parents=True)
        self.book = folder / '架空の作品 第01巻.cbz'
        with zipfile.ZipFile(self.book, 'w') as archive:
            for i in range(1, 4):
                archive.writestr(f'{i:04}.png', bytes([i])*32)
            archive.writestr('ComicInfo.xml', '<ComicInfo><Series>架空の作品</Series><Number>1</Number><Writer>架空作者</Writer><PageCount>3</PageCount><Pages><Page Image="0" Type="FrontCover"/><Page Image="1"/><Page Image="2"/></Pages></ComicInfo>')
            archive.writestr('notes.txt', 'keep')
        self.original = self.book.read_bytes()
        self.settings = Settings(demo=False, library_root=self.root / 'library', data_root=self.root / 'state', execution_enabled=True, web_execution_enabled=True)
        self.app = create_app(settings=self.settings); self.app.testing = True
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.store = self.app.extensions['state']
        self.inventory = self.app.extensions['inventory']
        self.row = self.inventory.cached()['series'][0]
        self.book_id = self.row['books'][0]['id']


class ToolOperationsTests(ToolFixture, unittest.TestCase):
    def test_page_preview_waits_and_deduplicates_without_changing_the_archive(self):
        editor = self.app.extensions['page_editor']
        review = editor.review(self.book_id)
        entered, release = Event(), Event()
        def blocking(progress):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('fixture timed out')
            return {}
        self.app.extensions['jobs'].submit_task('fixture', blocking)
        self.assertTrue(entered.wait(3))
        client = self.app.test_client()
        try:
            body = {'selected_pages': list(reversed(review['pages'])), 'fingerprint': review['fingerprint']}
            first = client.post(f'/api/tools/pages/{self.book_id}/preview/jobs', json=body)
            second = client.post(f'/api/tools/pages/{self.book_id}/preview/jobs', json=body)
            self.assertEqual((first.status_code, second.status_code), (202, 202))
            self.assertEqual(first.json['id'], second.json['id'])
            self.assertEqual(self.store.job(first.json['id'])['status'], 'queued')
            self.assertEqual(self.book.read_bytes(), self.original)
            self.assertEqual(self.store.cached_prefix('pages:operation:'), {})
        finally:
            release.set()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = self.store.job(first.json['id'], include_result=True)
            if job['status'] not in {'queued', 'running'}:
                break
            time.sleep(.01)
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(self.book.read_bytes(), self.original)
        self.assertEqual(len(self.store.cached_prefix('pages:operation:')), 1)
        html = client.get('/jobs/'+job['id']).get_data(as_text=True)
        self.assertIn(f'/pages/{self.book_id}?plan={job["result"]["plan"]["id"]}', html)

    def test_book_thumbnail_prefers_komga_and_lets_the_browser_keep_covers(self):
        from manga_kit.komga import KomgaError
        client = self.app.test_client()
        response = client.get('/api/tools/books/' + self.book_id + '/thumbnail')
        self.assertEqual((response.status_code, response.data), (200, bytes([1]) * 32))
        self.assertEqual(response.headers['Cache-Control'], 'private, max-age=86400')
        self.assertEqual(client.get('/api/tools/books/' + self.book_id + '/page/1').headers['Cache-Control'], 'no-store')
        self.assertIn('/api/tools/books/%s/thumbnail?v=%d' % (self.book_id, self.book.stat().st_size),
                      client.get('/shelf/series/' + self.row['id']).get_data(as_text=True))
        komga = self.app.extensions['komga'] = Mock()
        managed = {**self.row['books'][0], 'komga_id': 'K1'}
        with patch.object(self.app.extensions['book_pages'], 'find', return_value=managed):
            komga.thumbnail.return_value = (b'small', 'image/jpeg')
            self.assertEqual(client.get('/api/tools/books/' + self.book_id + '/thumbnail').data, b'small')
            komga.thumbnail.assert_called_with('K1')
            komga.thumbnail.side_effect = KomgaError('unavailable')
            self.assertEqual(client.get('/api/tools/books/' + self.book_id + '/thumbnail').data, bytes([1]) * 32)
            managed['path'] = None
            failed = client.get('/api/tools/books/' + self.book_id + '/thumbnail')
            self.assertEqual((failed.status_code, failed.headers['Cache-Control']), (502, 'no-store'))

    def test_page_reorder_delete_relinks_xml_and_restores_exact_archive(self):
        editor = self.app.extensions['page_editor']
        review = editor.review(self.book_id)
        operation = editor.prepare_book(self.book_id, [review['pages'][2], review['pages'][0]], fingerprint=review['fingerprint'])
        self.assertEqual(self.book.read_bytes(), self.original)
        editor.execute(operation['plan']['id'], operation['digest'])
        with zipfile.ZipFile(self.book) as archive:
            self.assertEqual(archive.read('0001.png'), bytes([3])*32)
            self.assertEqual(archive.read('0002.png'), bytes([1])*32)
            self.assertNotIn('0003.png', archive.namelist())
            self.assertEqual(archive.read('notes.txt'), b'keep')
            xml = fromstring(archive.read('ComicInfo.xml'))
            self.assertEqual(xml.findtext('PageCount'), '2')
            refs = {page.attrib['Image']: page.attrib.get('Type') for page in xml.findall('Pages/Page')}
            self.assertEqual(refs, {'1': 'Story', '0': 'FrontCover'})
        client = self.app.test_client()
        self.assertEqual(client.get('/pages/'+self.book_id+'?plan='+operation['plan']['id']).status_code, 200)
        editor.restore(operation['plan']['id'], operation['digest'])
        self.assertEqual(self.book.read_bytes(), self.original)

    def test_page_changes_reject_stale_review_empty_pages_and_symlink(self):
        editor = self.app.extensions['page_editor']; review = editor.review(self.book_id)
        for selection in [[], ['missing.png'], [review['pages'][0]]*2]:
            with self.assertRaises(ConfigError):
                editor.prepare_book(self.book_id, selection, fingerprint=review['fingerprint'])
        with zipfile.ZipFile(self.book, 'a') as archive:
            archive.comment = b'changed'
        with self.assertRaises(StateConflict):
            editor.prepare_book(self.book_id, review['pages'], fingerprint=review['fingerprint'])
        self.book.unlink(); self.book.symlink_to(self.root / 'outside')
        with self.assertRaises(OSError):
            self.app.extensions['book_pages'].names(self.book_id)

    def test_managed_page_deletion_is_rejected_before_network_or_source_changes(self):
        editor = self.app.extensions['page_editor']; review = editor.review(self.book_id)
        self.app.extensions['preferences']._current = replace(self.settings, komga_url='http://localhost:25600')
        with self.assertRaises(ConfigError):
            editor.prepare_book(self.book_id, review['pages'][:2], fingerprint=review['fingerprint'])
        self.assertEqual(self.book.read_bytes(), self.original)

    def test_page_backup_purge_is_explicit_and_preserves_current_book(self):
        editor = self.app.extensions['page_editor']; review = editor.review(self.book_id)
        operation = editor.prepare_book(self.book_id, review['pages'][:2], fingerprint=review['fingerprint'])
        with self.assertRaises(StateConflict):
            editor.purge(operation['plan']['id'], operation['digest']+':purge')
        editor.execute(operation['plan']['id'], operation['digest'])
        generated = self.book.read_bytes()
        with self.assertRaises(ConfigError):
            editor.purge(operation['plan']['id'], operation['digest'])
        editor.purge(operation['plan']['id'], operation['digest']+':purge')
        self.assertEqual(self.book.read_bytes(), generated)
        with self.assertRaises(ConfigError):
            editor.restore(operation['plan']['id'], operation['digest'])

    def test_ai_suggestions_save_once_and_undo_without_touching_archive(self):
        app = create_app(settings=replace(self.settings, demo=True)); self.addCleanup(app.extensions['jobs'].shutdown)
        ai = app.extensions['ai_assist']; row = app.extensions['library'].scan().series[0]
        proposal = ai.generate(row.id, ['demographic'])
        before = app.extensions['metadata'].inspect(row.id)
        ai.save(proposal['id'], proposal['digest'], proposal['fields'])
        saved = app.extensions['metadata'].inspect(row.id)
        self.assertIn('青年', saved['series']['genres'])
        self.assertTrue(ai.save(proposal['id'], proposal['digest'], proposal['fields'])['already_saved'])
        ai.save(proposal['id'], proposal['digest'], None, restore=True)
        restored = app.extensions['metadata'].inspect(row.id)
        self.assertEqual(restored['overrides'], before['overrides'])
        self.assertEqual(self.book.read_bytes(), self.original)
        self.assertIn('別の作品と同じあらすじです', summary_flags('架空の物語です。', ['架空の物語です。']))

    def test_ai_undo_preserves_later_manual_edits_by_refusing_overwrite(self):
        app = create_app(settings=replace(self.settings, demo=True)); self.addCleanup(app.extensions['jobs'].shutdown)
        ai = app.extensions['ai_assist']; row = app.extensions['library'].scan().series[0]
        proposal = ai.generate(row.id, ['demographic']); ai.save(proposal['id'], proposal['digest'], proposal['fields'])
        meta = app.extensions['metadata']; current = meta.inspect(row.id)
        meta.save(row.id, {'title': row.title + '変更'}, current['revision'], current['base_fingerprint'])
        with self.assertRaises(StateConflict):
            ai.save(proposal['id'], proposal['digest'], None, restore=True)


class FakeKomga:
    def __init__(self):
        self.shelves = {'collections': {'unrelated': {'id': 'unrelated', 'name': '個人の本棚', 'ordered': False, 'seriesIds': ['private']}}, 'readlists': {}}
        self.writes = []

    def get(self, endpoint, **params):
        if endpoint == '/actuator/info':
            return {'build': {'version': '1.29.1'}}
        kind, *identifier = endpoint.removeprefix('/api/v1/').split('/')
        return deepcopy(self.shelves[kind][identifier[0]]) if identifier else {'content': list(deepcopy(self.shelves[kind]).values()), 'last': True}

    def request(self, method, endpoint, body=None):
        self.writes.append((method, endpoint, deepcopy(body)))
        kind, *identifier = endpoint.removeprefix('/api/v1/').split('/')
        if method == 'POST':
            native_id = kind + str(len(self.shelves[kind]))
            result = {'id': native_id, **deepcopy(body)}; self.shelves[kind][native_id] = result
            return deepcopy(result)
        if method == 'DELETE':
            del self.shelves[kind][identifier[0]]
        else:
            self.shelves[kind][identifier[0]].update(deepcopy(body))


class SmartShelfTests(ToolFixture, unittest.TestCase):
    def service(self):
        self.inventory.refresh()
        data = self.store.cached('inventory')['data']; data['series'][0]['komga_id'] = 'owned-series'
        data['series'][0]['books'][0]['komga_id'] = 'owned-book'
        data['series'][0]['status'] = 'completed'; self.store.cache('inventory', data)
        self.store.cache('feed:anime', {'entries': [{'id': 'anime1', 'title': self.row['title']}], 'year': 2026, 'season': 'FALL'})
        self.client = FakeKomga()
        return SmartShelves(replace(self.settings, komga_url='http://isolated.example'), self.store, self.inventory,
                           self.app.extensions['feeds'], self.app.extensions['new_volumes'], self.client)

    def test_sync_and_undo_only_changes_owned_shelves(self):
        service = self.service(); before = deepcopy(self.client.shelves['collections']['unrelated'])
        operation = service.prepare(); self.assertEqual(self.client.writes, [])
        result = service.execute(operation['plan']['id'], operation['digest'])
        self.assertGreater(result['shelves'], 0)
        self.assertEqual(self.client.shelves['collections']['unrelated'], before)
        service.execute(operation['plan']['id'], operation['digest'], restore=True)
        self.assertEqual(self.client.shelves['collections'], {'unrelated': before})
        self.assertEqual(self.client.shelves['readlists'], {})

    def test_personal_same_name_is_never_adopted_and_remote_edit_aborts_whole_sync(self):
        service = self.service()
        self.client.shelves['collections']['unrelated']['name'] = 'Manga Kit · 今期アニメの原作'
        with self.assertRaises(StateConflict):
            service.prepare()
        self.assertEqual(self.client.writes, [])
        self.client.shelves['collections']['unrelated']['name'] = '個人の本棚'
        operation = service.prepare(); service.execute(operation['plan']['id'], operation['digest'])
        native = next(item for item in self.client.shelves['collections'].values() if item['id'] != 'unrelated')
        native['seriesIds'].append('manual-edit')
        count = len(self.client.writes)
        with self.assertRaises(StateConflict):
            service.execute(operation['plan']['id'], operation['digest'], restore=True)
        self.assertEqual(len(self.client.writes), count)


if __name__ == '__main__':
    unittest.main()
