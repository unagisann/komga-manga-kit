from copy import deepcopy
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

import requests

from manga_kit import create_app
from manga_kit.catalogs import CatalogError, MAX_RESPONSE_BYTES, RakutenBooks, search_query
from manga_kit.config import ConfigError, RakutenSettings, Settings, load_settings
from manga_kit.enrichment import EnrichmentService
from manga_kit.library import LibraryService
from manga_kit.metadata import MetadataService
from manga_kit.operations import OperationService
from manga_kit.sample import create_sample_library
from manga_kit.state import StateConflict, StateStore


def response_data():
    return {'count': 1, 'page': 1, 'pageCount': 1, 'Items': [
        {'title': '架空の物語（1）', 'titleKana': 'カクウノモノガタリ 1', 'author': '架空作者',
         'publisherName': 'テスト出版', 'isbn': '9780000000019',
         'itemUrl': 'https://books.rakuten.co.jp/rb/1234567/?tracking=discard',
         'itemCaption': 'シリーズ累計100万部突破！\n架空の町に暮らす少女が、失われた物語の断片を集めながら仲間と出会い、町の秘密に近づいていく長編ファンタジー。\n【電子限定特典付き】',
         'salesDate': '2024年04月01日', 'booksGenreId': '001001001', 'itemPrice': 1000}]}


def http_response(data=None, status=200, chunks=None):
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code = status
    response.headers = {}
    response.iter_content.return_value = chunks if chunks is not None else [json.dumps(data or response_data()).encode()]
    return response


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(demo=False, library_root=self.root / 'library', data_root=self.root / 'state',
                                 rakuten=RakutenSettings(enabled=True, origin='https://manga.example.com'))
        self.store = StateStore(self.settings)
        self.catalog = RakutenBooks(self.settings.rakuten, self.store)
        env = patch.dict(os.environ, {'RAKUTEN_APPLICATION_ID': 'private-application-id', 'RAKUTEN_ACCESS_KEY': 'private-access-key'}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_current_endpoint_headers_limits_and_bibliographic_fields(self):
        with patch('requests.Session.get', return_value=http_response()) as get:
            result = self.catalog.search(search_query(title='架空の物語', author='架空作者'))
        args, kwargs = get.call_args
        self.assertEqual(args[0], 'https://openapi.rakuten.co.jp/services/api/BooksBook/Search/20170404')
        self.assertFalse(kwargs['allow_redirects'])
        self.assertTrue(kwargs['stream'])
        self.assertEqual(kwargs['headers']['accessKey'], 'private-access-key')
        self.assertEqual(kwargs['headers']['Origin'], 'https://manga.example.com')
        self.assertEqual(kwargs['params']['applicationId'], 'private-application-id')
        self.assertNotIn('accessKey', kwargs['params'])
        self.assertEqual(kwargs['params']['hits'], 10)
        # Genre 001001 covers manga in every format, including manga bunko.
        self.assertEqual(kwargs['params']['booksGenreId'], '001001')
        self.assertNotIn('size', kwargs['params'])
        self.assertIn('itemCaption', kwargs['params']['elements'])
        self.assertNotIn('itemPrice', kwargs['params']['elements'])
        item = result['candidates'][0]
        self.assertEqual(item['title'], '架空の物語（1）')
        self.assertEqual((item['volume'], item['sales_date']), ('1', '2024年04月01日'))
        self.assertEqual(item['fields']['title'], '架空の物語')
        self.assertEqual(item['fields']['reading'], 'かくうのものがたり')
        self.assertEqual(item['fields']['genres'], ['少年'])
        self.assertTrue(item['fields']['summary'].startswith('架空の町に暮らす少女が'))
        self.assertEqual(item['url'], 'https://books.rakuten.co.jp/rb/1234567/')
        self.assertEqual(set(item['fields']), {'title', 'reading', 'author', 'publisher', 'summary', 'genres'})
        self.assertNotIn('private-', json.dumps(result))
        self.assertNotIn('累計', json.dumps(result, ensure_ascii=False))
        self.assertNotIn('特典', json.dumps(result, ensure_ascii=False))
        self.assertNotIn('itemPrice', json.dumps(result))

    def test_product_titles_captions_and_genres_become_series_candidates(self):
        from manga_kit.catalogs import caption_summary, readership, series_reading, split_volume
        for title, expected in [('架空の物語（12）', ('架空の物語', '12')), ('架空の物語！（3）', ('架空の物語！', '3')),
                                ('架空の物語　7', ('架空の物語', '7')), ('架空の物語 第４巻', ('架空の物語', '4')),
                                ('架空の物語 20 (架空コミックス)', ('架空の物語', '20')), ('架空13', ('架空13', None)),
                                ('架空13 2', ('架空13', '2')), ('架空の物語 （特装版）', ('架空の物語', None)),
                                ('（1）', ('（1）', None))]:
            with self.subTest(title=title):
                self.assertEqual(split_volume(title), expected)
        self.assertEqual(series_reading('カクウ ノ モノガタリ 3'), 'カクウノモノガタリ')
        self.assertEqual(series_reading('KAKUU ノ MONOGATARI'), 'KAKUU ノ MONOGATARI')
        story = '架空の町に暮らす少女が、失われた物語の断片を集めながら仲間と出会い、町の秘密に近づいていく。'
        self.assertEqual(caption_summary('大好評発売中！\n' + story + '\n※電子版には特典が付きます'), story)
        self.assertEqual(caption_summary('完全版で登場！全10巻'), '')
        self.assertTrue(len(caption_summary(story * 20)) <= 401)
        self.assertEqual(readership('001001003/001001003001'), ['青年'])
        self.assertEqual(readership('001001001/001001002'), [])
        self.assertEqual(readership('001001006'), [])
        for key, value in (('booksGenreId', '001001;drop'), ('salesDate', 7), ('itemCaption', 'x' * 20001)):
            data = response_data()
            data['Items'][0][key] = value
            with self.subTest(key=key), self.assertRaises(CatalogError):
                RakutenBooks.parse(data, 1)

    def test_subtitles_and_unknown_parentheses_survive_volume_and_label_cleanup(self):
        from manga_kit.catalogs import series_reading, split_volume
        for title in ('架空作品（続編）', '架空作品（仮）', '架空作品（Side Story）',
                      '架空作品（文庫の物語）', '架空作品（架空コミックス）', '架空作品 2 （続編）'):
            with self.subTest(title=title):
                self.assertEqual(split_volume(title), (title, None))
        for title in ('架空作品（続編） 2', '架空作品（続編） 第２巻 （架空コミックス）',
                      '架空作品（続編）（2） （架空コミックス） （特装版）'):
            with self.subTest(title=title):
                self.assertEqual(split_volume(title), ('架空作品（続編）', '2'))
        self.assertEqual(series_reading('カクウ サクヒン（ゾクヘン）'), 'カクウサクヒン（ゾクヘン）')
        data = response_data()
        data['Items'][0].update(title='架空作品（続編）', titleKana='カクウサクヒン（ゾクヘン）')
        candidate = RakutenBooks.parse(data, 1)['candidates'][0]
        self.assertEqual(candidate['fields']['title'], '架空作品（続編）')
        self.assertEqual(candidate['fields']['reading'], 'かくうさくひん(ぞくへん)')

    def test_disabled_or_missing_credentials_never_contact_provider(self):
        with patch('requests.Session.get', side_effect=AssertionError('Network called')):
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigError):
                self.catalog.search(search_query(title='作品'))
            disabled = RakutenBooks(RakutenSettings(), self.store)
            with self.assertRaises(ConfigError):
                disabled.search(search_query(title='作品'))

    def test_isbn_normalization_priority_and_invalid_searches(self):
        self.assertEqual(search_query(title='ignored', author='ignored', isbn='９７８-０-０００００００１-９'),
                         {'title': '', 'author': '', 'isbn': '9780000000019', 'page': 1})
        for values in ({}, {'title': []}, {'title': 'x' * 201}, {'title': '\n'}, {'isbn': '9780000000010'},
                       {'title': '作品', 'page': True}, {'title': '作品', 'page': 101}, {'isbn': 'abc'}):
            with self.subTest(values=values), self.assertRaises(ConfigError):
                search_query(**values)

    def test_rate_limit_is_shared_by_service_instances(self):
        with patch('requests.Session.get', return_value=http_response()) as get:
            self.catalog.search(search_query(title='作品'))
            other = RakutenBooks(self.settings.rakuten, StateStore(self.settings))
            with self.assertRaises(CatalogError) as caught:
                other.search(search_query(title='別の作品'))
        self.assertGreaterEqual(caught.exception.retry_after, 1)
        self.assertEqual(get.call_count, 1)

    def test_provider_429_sets_persistent_cooldown_without_retry(self):
        response = http_response(status=429)
        response.headers['Retry-After'] = '120'
        with patch('requests.Session.get', return_value=response) as get, self.assertRaises(CatalogError) as caught:
            self.catalog.search(search_query(title='作品'))
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertEqual(get.call_count, 1)
        with self.store.connection() as connection:
            until = connection.execute('SELECT next_allowed FROM metadata_requests').fetchone()[0]
        self.assertGreater(until, time.time() + 100)

    def test_http_errors_redirects_timeouts_and_payload_limits_are_safe(self):
        for value in (http_response(status=302), http_response(status=401), http_response(status=503),
                      http_response(chunks=[b'{invalid']), http_response(chunks=[b'[' * 2000 + b']' * 2000]),
                      http_response(chunks=[b'x' * (MAX_RESPONSE_BYTES + 1)]),
                      requests.Timeout('private-access-key and private-application-id')):
            with self.subTest(value=type(value).__name__):
                with self.store.connection() as connection:
                    connection.execute('DELETE FROM metadata_requests')
                with patch('requests.Session.get', side_effect=value if isinstance(value, Exception) else None,
                           return_value=value), self.assertRaises(CatalogError) as caught:
                    self.catalog.search(search_query(title='作品'))
                self.assertNotIn('private-', str(caught.exception))

    def test_404_is_empty_result(self):
        with patch('requests.Session.get', return_value=http_response(status=404)):
            self.assertEqual(self.catalog.search(search_query(title='作品'))['candidates'], [])

    def test_malformed_records_and_unsafe_links_are_rejected(self):
        invalid = [None, [], {'Items': []}]
        for key, value in [('title', ''), ('titleKana', []), ('author', 'x' * 301), ('isbn', 'bad'), ('isbn', '9780000000010'),
                           ('itemUrl', 'javascript:alert(1)'), ('itemUrl', 'https://books.rakuten.co.jp.evil.test/rb/1/'),
                           ('itemUrl', 'https://books.rakuten.co.jp@evil.test/rb/1/')]:
            data = response_data()
            data['Items'][0][key] = value
            invalid.append(data)
        data = response_data()
        data['Items'] *= 11
        invalid.append(data)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(CatalogError):
                RakutenBooks.parse(data, 1)

    def test_configuration_rejects_secret_values_and_header_injection(self):
        path = self.root / 'config.yaml'
        for section in ('enabled: yesplease', 'application_id: secret', 'timeout_seconds: 0',
                        'origin: "https://example.com/?secret=1"', 'origin: "https://user:secret@example.com"',
                        'origin: "https://example.com/\\nHeader"', 'access_key_env: private-key'):
            path.write_text('rakuten:\n  ' + section + '\n')
            with self.subTest(section=section), self.assertRaises(ConfigError):
                load_settings(path)
        path.write_text('rakuten:\n  enabled: true\n  origin: https://manga.example.com\n')
        self.assertTrue(load_settings(path).rakuten.enabled)


class EnrichmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(demo=False, library_root=self.root / 'library', data_root=self.root / 'state', execution_enabled=True)
        create_sample_library(self.settings.library_root, self.settings.layout)
        self.store = StateStore(self.settings)
        self.library = LibraryService(self.settings, store=self.store)
        self.metadata = MetadataService(self.library, self.store)
        self.service = EnrichmentService(self.settings, self.metadata, self.store)
        self.provider = Mock(id='rakuten_books', label='楽天Books')
        self.provider.configured.return_value = True
        self.provider.search.side_effect = lambda query: deepcopy(RakutenBooks.parse(response_data(), 1))
        self.service.provider = self.provider
        self.series = self.library.scan().series[0]

    def files(self):
        return {p.relative_to(self.settings.library_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.settings.library_root.rglob('*') if p.is_file()}

    def proposal(self, fields=None):
        result = self.service.search(self.series.id, title=self.series.title)
        body = {'search_id': result['search_id'], 'candidate_id': result['candidates'][0]['id'],
                'fields': {'reading': 'カクウノモノガタリ', 'publisher': 'テスト出版'} if fields is None else fields}
        preview = self.service.preview(self.series.id, **body)
        body.update(revision=preview['revision'], base_fingerprint=preview['base_fingerprint'])
        return body, preview

    def test_selected_fields_provenance_and_history_persist_without_media_changes(self):
        before = self.files()
        body, preview = self.proposal()
        # The series reading already omits 楽天's volume number, so nothing was adjusted.
        self.assertEqual(preview['provenance']['adjusted_fields'], [])
        _, adjusted = self.proposal({'publisher': '調整した出版'})
        self.assertEqual(adjusted['provenance']['adjusted_fields'], ['publisher'])
        self.assertEqual(self.store.edits(), {})
        saved = self.service.save(self.series.id, **body)
        self.assertEqual(saved['series']['title'], self.series.title)
        self.assertEqual(saved['series']['reading'], 'かくうのものがたり')
        self.assertEqual(saved['provenance']['item_id'], '9780000000019')
        self.assertEqual(self.files(), before)
        reopened = StateStore(self.settings)
        self.assertEqual(reopened.edits()[self.series.id]['provenance'], saved['provenance'])
        self.assertEqual(reopened.events()[0]['detail']['source_action'], 'linked')
        self.assertIn('楽天Books', saved['series']['sources'])

    def test_cache_reused_after_restart_and_isolated_by_series_and_library(self):
        first = self.service.search(self.series.id, title='作品')
        other = EnrichmentService(self.settings, self.metadata, StateStore(self.settings))
        other.provider = self.provider
        second = other.search(self.series.id, title='作品')
        self.assertTrue(second['cached'])
        self.assertEqual(first['search_id'], second['search_id'])
        self.assertEqual(self.provider.search.call_count, 1)
        other_series = self.library.scan().series[1]
        body = {'search_id': first['search_id'], 'candidate_id': first['candidates'][0]['id'], 'fields': {}}
        with self.assertRaises(StateConflict):
            self.service.preview(other_series.id, **body)
        demo = replace(self.settings, demo=True)
        demo_store = StateStore(demo)
        demo_metadata = MetadataService(LibraryService(demo, store=demo_store), demo_store)
        demo_service = EnrichmentService(demo, demo_metadata, demo_store)
        demo_service.provider = self.provider
        with self.assertRaises(StateConflict):
            demo_service.preview(self.series.id, **body)

    def test_old_search_cache_is_refetched_and_old_candidate_cannot_be_adopted(self):
        first = self.service.search(self.series.id, title='作品')
        old_key = hashlib.sha256(json.dumps([self.provider.id, first['query']], sort_keys=True).encode()).hexdigest()
        with self.store.connection() as connection:
            connection.execute('UPDATE metadata_searches SET cache_key=? WHERE id=?', (old_key, first['search_id']))
        body = {'search_id': first['search_id'], 'candidate_id': first['candidates'][0]['id'], 'fields': {}}
        with self.assertRaises(StateConflict):
            self.service.preview(self.series.id, **body)
        refreshed = self.service.search(self.series.id, title='作品')
        self.assertFalse(refreshed['cached'])
        self.assertNotEqual(refreshed['search_id'], first['search_id'])
        self.assertEqual(self.provider.search.call_count, 2)
        self.assertEqual(self.store.edits(), {})

    def test_expired_candidate_cannot_be_reviewed_or_saved_and_search_refreshes(self):
        body, _ = self.proposal()
        with self.store.connection() as connection:
            connection.execute('UPDATE metadata_searches SET expires=0')
        with self.assertRaises(StateConflict):
            self.service.save(self.series.id, **body)
        refreshed = self.service.search(self.series.id, title=self.series.title)
        self.assertNotEqual(refreshed['search_id'], body['search_id'])
        self.assertEqual(self.provider.search.call_count, 2)

    def test_cache_is_bounded(self):
        with patch('manga_kit.enrichment.MAX_SEARCHES', 3):
            ids = [self.service.search(self.series.id, title=f'作品{index}')['search_id'] for index in range(5)]
        with self.store.connection() as connection:
            rows = connection.execute('SELECT id FROM metadata_searches').fetchall()
        self.assertEqual({row[0] for row in rows}, set(ids[-3:]))

    def test_forged_candidate_and_fields_cannot_change_source_attribution(self):
        body, _ = self.proposal()
        for values in ({'candidate_id': 'missing'}, {'fields': {'tags': ['Not offered by this provider']}},
                       {'fields': {'status': 'completed'}}, {'fields': []}, {'search_id': '../outside'}, {'unlink': 'yes'}):
            with self.subTest(values=values), self.assertRaises(ConfigError):
                self.service.preview(self.series.id, **{key: value for key, value in {**body, **values}.items()
                                                       if key not in {'revision', 'base_fingerprint'}})

    def test_revision_conflict_and_library_change_require_new_review(self):
        body, _ = self.proposal()
        info = self.metadata.inspect(self.series.id)
        self.metadata.save(self.series.id, {'author': '手動の著者'}, info['revision'], info['base_fingerprint'])
        with self.assertRaises(StateConflict):
            self.service.save(self.series.id, **body)
        body, _ = self.proposal()
        sidecar = self.settings.library_root / self.series.path / 'series.json'
        content = json.loads(sidecar.read_text())
        content['publisher'] = '別の出版社'
        sidecar.write_text(json.dumps(content))
        with self.assertRaises(StateConflict):
            self.service.save(self.series.id, **body)

    def test_manual_changes_remove_only_changed_field_attribution(self):
        body, _ = self.proposal()
        saved = self.service.save(self.series.id, **body)
        saved = self.metadata.save(self.series.id, {'publisher': '自分で修正', 'reading': saved['series']['reading']},
                                   saved['revision'], saved['base_fingerprint'])
        self.assertEqual(saved['provenance']['applied_fields'], ['reading'])
        self.assertEqual(saved['provenance']['item_id'], '9780000000019')

    def test_unlink_keeps_edits_and_reset_removes_both(self):
        body, _ = self.proposal()
        saved = self.service.save(self.series.id, **body)
        review = self.service.preview(self.series.id, unlink=True)
        unlinked = self.service.save(self.series.id, review['revision'], review['base_fingerprint'], unlink=True)
        self.assertEqual(unlinked['provenance'], {})
        self.assertEqual(unlinked['overrides'], saved['overrides'])
        self.assertEqual(self.store.events()[0]['detail']['source_action'], 'unlinked')
        body, _ = self.proposal()
        saved = self.service.save(self.series.id, **body)
        reset = self.metadata.save(self.series.id, None, saved['revision'], saved['base_fingerprint'], reset=True)
        self.assertEqual(reset['provenance'], {})
        self.assertEqual(reset['overrides'], {})

    def test_link_only_and_replacement_do_not_overwrite_unselected_fields(self):
        body, _ = self.proposal(fields={})
        saved = self.service.save(self.series.id, **body)
        self.assertEqual(saved['overrides'], {})
        self.assertEqual(saved['provenance']['applied_fields'], [])
        self.assertEqual(saved['series']['title'], self.series.title)
        data = response_data()
        data['Items'][0]['isbn'] = '9780000000026'
        self.provider.search.side_effect = lambda query: RakutenBooks.parse(data, 1)
        result = self.service.search(self.series.id, title='別の候補')
        values = dict(search_id=result['search_id'], candidate_id='9780000000026', fields={})
        preview = self.service.preview(self.series.id, **values)
        saved = self.service.save(self.series.id, preview['revision'], preview['base_fingerprint'], **values)
        self.assertEqual(saved['provenance']['item_id'], '9780000000026')
        self.assertEqual(saved['overrides'], {})

    def test_move_and_restore_keep_provenance_and_reject_edits_during_operation(self):
        body, _ = self.proposal()
        saved = self.service.save(self.series.id, **body)
        operations = OperationService(self.settings)
        operation = operations.prepare(layout={'series': '{title}'}, selected=[self.series.id])
        with operations.locked(), self.assertRaises(StateConflict):
            self.service.save(self.series.id, **body)
        operations.execute(operation['plan']['id'], operation['digest'])
        target_id = operation['plan']['groups'][0]['target_id']
        self.assertEqual(operations.store.edits()[target_id]['provenance'], saved['provenance'])
        operations.restore(operation['plan']['id'], operation['digest'])
        self.assertEqual(operations.store.edits()[self.series.id]['provenance'], saved['provenance'])

    def test_source_change_invalidates_prepared_move(self):
        operations = OperationService(self.settings)
        operation = operations.prepare(selected=[self.series.id])
        body, _ = self.proposal(fields={})
        self.service.save(self.series.id, **body)
        before = self.files()
        with self.assertRaises(StateConflict):
            operations.execute(operation['plan']['id'], operation['digest'])
        self.assertEqual(self.files(), before)

    @unittest.skipUnless(os.name == 'posix' and Path('/proc/self/fd').is_dir(), 'Linux SIGKILL verification')
    def test_source_provenance_survives_kill_after_move_database_commit(self):
        before = self.files()
        body, _ = self.proposal()
        saved = self.service.save(self.series.id, **body)
        operations = OperationService(self.settings)
        operation = operations.prepare(layout={'series': '{title}'}, selected=[self.series.id])
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('durability_worker.py')),
            str(self.root), 'organize', 'execute', operation['plan']['id'], operation['digest'], 'edits'],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        reopened = OperationService(self.settings)
        reopened.restore(operation['plan']['id'], operation['digest'])
        self.assertEqual(reopened.store.edits()[self.series.id]['provenance'], saved['provenance'])
        self.assertEqual(self.files(), before)

    def test_existing_link_only_at_move_destination_is_not_overwritten(self):
        body, _ = self.proposal(fields={})
        saved = self.service.save(self.series.id, **body)
        operations = OperationService(self.settings)
        operation = operations.prepare(layout={'series': '{title}'}, selected=[self.series.id])
        target = operation['plan']['groups'][0]['target_id']
        self.store.save_edit(target, {}, 0, {}, provenance=saved['provenance'])
        before = self.files()
        with self.assertRaises(StateConflict):
            operations.prepare(layout={'series': '{title}'}, selected=[self.series.id])
        self.assertEqual(self.store.edits()[target]['provenance'], saved['provenance'])
        self.assertEqual(self.files(), before)

    def test_actual_schema_two_database_migrates_without_losing_edits(self):
        alternate = replace(self.settings, data_root=self.root / 'old-state')
        alternate.data_root.mkdir()
        with closing(sqlite3.connect(alternate.data_root / 'state.sqlite3')) as connection, connection:
            connection.execute('CREATE TABLE edits(namespace TEXT NOT NULL,series_id TEXT NOT NULL,fields TEXT NOT NULL,revision INTEGER NOT NULL,updated TEXT NOT NULL,PRIMARY KEY(namespace,series_id))')
            connection.execute('INSERT INTO edits VALUES(?,?,?,?,?)', (self.store.namespace, self.series.id, '{"reading":"old"}', 7, 'previous'))
            connection.execute('PRAGMA user_version=2')
        upgraded = StateStore(alternate)
        self.assertEqual(upgraded.edits()[self.series.id], {'fields': {'reading': 'old'}, 'revision': 7})
        with upgraded.connection() as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 3)


class EnrichmentApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(data_root=self.root / 'state', library_root=self.root / 'library')
        env = patch.dict(os.environ, {'RAKUTEN_APPLICATION_ID': 'private-id', 'RAKUTEN_ACCESS_KEY': 'private-key'}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        network = patch('requests.Session.get', side_effect=AssertionError('Unexpected external request'))
        network.start()
        self.addCleanup(network.stop)
        self.app = create_app(settings=self.settings)
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.client = self.app.test_client()
        self.series = next(item for item in self.client.get('/api/library').json['series'] if item['title'] == '月を待つ駅')
        self.base = '/api/metadata/' + self.series['id']

    def test_demo_search_review_save_and_restart_without_network(self):
        result = self.client.post(self.base + '/search', json={'title': '月を待つ駅'}).json
        self.assertEqual(result['provider'], 'demo')
        self.assertEqual(len(result['candidates']), 1)
        candidate = result['candidates'][0]
        body = {'search_id': result['search_id'], 'candidate_id': candidate['id'],
                'fields': {'reading': candidate['fields']['reading'], 'author': candidate['fields']['author']}}
        preview = self.client.post(self.base + '/source/preview', json=body).json
        body.update(revision=preview['revision'], base_fingerprint=preview['base_fingerprint'])
        saved = self.client.post(self.base + '/source', json=body)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json['series']['kana_row'], 'た')
        self.assertEqual(saved.json['provenance']['provider'], 'demo')
        other = create_app(settings=self.settings)
        self.addCleanup(other.extensions['jobs'].shutdown)
        self.assertEqual(other.test_client().get(self.base).json['provenance'], saved.json['provenance'])

    def test_unknown_series_cross_origin_and_untrusted_json_are_rejected(self):
        self.assertEqual(self.client.post('/api/metadata/missing/search', json={'title': '作品'}).status_code, 404)
        self.assertEqual(self.client.post(self.base + '/search', json={'title': '作品'}, headers={'Origin': 'https://evil.test'}).status_code, 403)
        for suffix, body in (('/search', {'url': 'https://evil.test'}), ('/source', {'provider': 'forged'}),
                             ('/source/preview', {'fields': {}, 'search_id': 'bad', 'candidate_id': 'x'})):
            self.assertEqual(self.client.post(self.base + suffix, json=body).status_code, 400)

    def test_pages_do_not_leak_credentials_and_provider_markup_is_escaped(self):
        for path in ('/settings', '/metadata', '/metadata/' + self.series['id'], '/metadata/' + self.series['id'] + '/sources', '/api/settings'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('private-id', response.get_data(as_text=True))
            self.assertNotIn('private-key', response.get_data(as_text=True))
        info = self.client.get(self.base).json
        self.client.post(self.base, json={'fields': {'title': '<script>alert(1)</script>'}, 'revision': info['revision'], 'base_fingerprint': info['base_fingerprint']})
        html = self.client.get('/metadata/' + self.series['id'] + '/sources').get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('Supported by Rakuten Developers', html)

    def test_live_unconfigured_search_never_uses_demo_or_network(self):
        settings = replace(self.settings, demo=False)
        create_sample_library(settings.library_root, settings.layout)
        app = create_app(settings=settings)
        self.addCleanup(app.extensions['jobs'].shutdown)
        client = app.test_client()
        series_id = client.get('/api/library').json['series'][0]['id']
        response = client.post(f'/api/metadata/{series_id}/search', json={'title': '作品'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('認証情報', response.json['error'])


if __name__ == '__main__':
    unittest.main()
