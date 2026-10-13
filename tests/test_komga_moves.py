from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from manga_kit.config import ConfigError, PathMapping, Settings
from manga_kit.komga import KomgaClient, KomgaError
from manga_kit.komga_moves import KomgaMoveGuard
from manga_kit.state import StateConflict


class MoveGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = Settings(demo=False, library_root=self.root, komga_url='http://localhost:25601',
            path_mappings=(PathMapping('/data', self.root),))
        self.library = {'id': 'library', 'root': 'file:///data', 'hashFiles': True, 'emptyTrashAfterScan': False,
                        'scanOnStartup': False, 'scanInterval': 'DISABLED', 'convertToCbz': False, 'repairExtensions': False}
        self.book = {'id': 'old-book', 'libraryId': 'library', 'seriesId': 'old-series', 'url': 'file:///data/series/old.cbz',
                     'fileHash': 'hash', 'media': {'status': 'READY'}, 'metadata': {'title': 'locked title', 'titleLock': True},
                     'readProgress': {'page': 2, 'completed': False, 'readDate': '2026-01-01T00:00:00Z'}}
        self.active, self.deleted = [self.book], []
        self.entries = [{'source': 'series/old.cbz', 'target': 'completed/new.cbz', 'kind': 'book',
                         'snapshot': {'komga_hash': 'hash'}}]
        self.groups = [{'source': 'series', 'target': 'completed'}]
        self.client = Mock()
        self.client.get.side_effect = self.get
        self.client.request.side_effect = self.request
        self.guard = KomgaMoveGuard(self.settings, self.client)

    def get(self, path):
        if path == '/actuator/info':
            return {'build': {'version': '1.29.0'}}
        if path == '/api/v1/libraries':
            return [self.library]
        if path.startswith('/api/v1/series/'):
            return {'metadata': {'titleSort': 'さくひん', 'titleSortLock': True}}
        raise AssertionError(path)

    def request(self, method, path, body=None, **params):
        if path.endswith('/scan'):
            return None
        self.assertEqual((method, path), ('POST', '/api/v1/books/list'))
        values = self.deleted if body['condition']['deleted']['operator'] == 'isTrue' else self.active
        return {'content': values, 'last': True}

    def snapshot(self):
        return self.guard.snapshot(self.entries, self.groups)

    def test_unconfigured_komga_never_contacts_network(self):
        guard = KomgaMoveGuard(replace(self.settings, komga_url=''), self.client)
        self.assertIsNone(guard.snapshot(self.entries, self.groups))
        self.client.get.assert_not_called()

    def test_unknown_version_and_unsafe_library_flags_are_rejected(self):
        with patch.object(self.client, 'get', return_value={'build': {'version': 'other'}}), self.assertRaises(ConfigError):
            self.snapshot()
        for key, value in {'hashFiles': False, 'emptyTrashAfterScan': True, 'scanOnStartup': True,
                           'scanInterval': 'EVERY_HOUR', 'convertToCbz': True, 'repairExtensions': True}.items():
            with self.subTest(key=key), patch.dict(self.library, {key: value}), self.assertRaises(ConfigError):
                self.snapshot()

    def test_hash_must_be_current_and_analysis_complete(self):
        for change, error in [({'fileHash': ''}, ConfigError), ({'media': {'status': 'OUTDATED'}}, ConfigError),
                              ({'fileHash': 'outdated'}, StateConflict)]:
            with self.subTest(change=change), patch.dict(self.book, change), self.assertRaises(error):
                self.snapshot()

    def test_identical_content_in_another_library_or_trash_is_rejected(self):
        duplicate = dict(self.book, id='another-book', libraryId='another-library')
        self.active.append(duplicate)
        with self.assertRaises(ConfigError):
            self.snapshot()
        self.active.pop()
        self.deleted.append(duplicate)
        with self.assertRaises(ConfigError):
            self.snapshot()

    def test_partial_series_and_mismatched_library_root_are_rejected(self):
        self.active.append(dict(self.book, id='extra', url='file:///data/series/extra.cbz', fileHash='another-hash'))
        with self.assertRaises(ConfigError):
            self.snapshot()
        self.active.pop()
        self.library['root'] = 'file:///data/subfolder'
        with self.assertRaises(ConfigError):
            self.snapshot()

    def test_new_book_and_series_ids_preserve_progress_and_locked_metadata(self):
        snapshot = self.snapshot()
        self.book.update(id='new-book', seriesId='new-series', url='file:///data/completed/new.cbz')
        self.guard.verify_after(snapshot, self.entries, self.groups)
        self.assertEqual(self.client.request.call_args_list[-2].args[:2], ('POST', '/api/v1/libraries/library/scan'))

    def test_progress_loss_is_reported_after_explicit_scan(self):
        snapshot = self.snapshot()
        self.book.update(url='file:///data/completed/new.cbz', readProgress=None)
        with patch('manga_kit.komga_moves.time.monotonic', side_effect=[0, 91]), self.assertRaises(StateConflict):
            self.guard.verify_after(snapshot, self.entries, self.groups)

    def test_restore_captures_latest_progress_and_rejects_ambiguous_hash(self):
        snapshot = self.snapshot()
        self.book.update(url='file:///data/completed/new.cbz')
        self.book['readProgress']['page'] = 3
        latest = self.guard.restore_snapshot(snapshot, self.entries)
        self.assertEqual(latest['books']['series/old.cbz']['progress']['page'], 3)
        self.assertEqual(snapshot['books']['series/old.cbz']['progress']['page'], 2)
        self.deleted.append(deepcopy(self.book))
        with self.assertRaises(StateConflict):
            self.guard.restore_snapshot(snapshot, self.entries)

    def test_restore_also_checks_automatic_conversion_flags(self):
        snapshot = self.snapshot()
        for key in ('convertToCbz', 'repairExtensions'):
            with self.subTest(key=key), patch.dict(self.library, {key: True}), self.assertRaises(ConfigError):
                self.guard.restore_snapshot(snapshot, self.entries)

    def test_book_query_pagination_and_malformed_response(self):
        self.client.request.side_effect = [ {'content': [self.book], 'last': False}, {'content': [], 'last': True} ]
        self.assertEqual(self.guard.books(), [self.book])
        self.assertEqual(self.client.request.call_args.kwargs['page'], 1)
        self.client.request.side_effect = None
        self.client.request.return_value = {'content': None}
        with self.assertRaises(KomgaError):
            self.guard.books()

    def test_scan_request_keeps_key_private_and_never_follows_redirects(self):
        session = Mock()
        response = session.request.return_value
        response.status_code, response.content = 202, b''
        client = KomgaClient(self.settings, session)
        with patch.dict(os.environ, {'KOMGA_API_KEY': 'fixture-key'}):
            self.assertIsNone(client.request('POST', '/api/v1/libraries/library/scan'))
            self.assertFalse(session.request.call_args.kwargs['allow_redirects'])
            self.assertEqual(session.request.call_args.kwargs['headers']['X-API-Key'], 'fixture-key')
            for status in (302, 401, 500):
                response.status_code = status
                with self.assertRaises(KomgaError) as caught:
                    client.request('POST', '/api/v1/libraries/library/scan')
                self.assertNotIn('fixture-key', str(caught.exception))
            session.request.side_effect = requests.Timeout('fixture-key')
            with self.assertRaises(KomgaError) as caught:
                client.request('POST', '/api/v1/libraries/library/scan')
            self.assertNotIn('fixture-key', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
