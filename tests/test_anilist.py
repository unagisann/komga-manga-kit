from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import requests

from manga_kit import create_app
from manga_kit.anilist import ANILIST_ENDPOINT, AniList, anilist_query
from manga_kit.catalogs import CatalogError, MAX_RESPONSE_BYTES
from manga_kit.config import AniListSettings, ConfigError, Settings, load_settings
from manga_kit.library import LibraryService
from manga_kit.metadata import MetadataService, validate_fields
from manga_kit.operations import OperationService
from manga_kit.provenance import source_links
from manga_kit.sample import create_sample_library
from manga_kit.state import StateConflict, StateStore


def anilist_response():
    return {"data": {"Page": {"pageInfo": {"currentPage": 1, "hasNextPage": False}, "media": [{
        "id": 12345, "type": "MANGA", "format": "MANGA", "title": {
            "native": "架空の物語", "romaji": "Kakuu no Monogatari", "english": None},
        "countryOfOrigin": "JP", "startDate": {"year": 2024}, "genres": ["Fantasy", "Drama"],
        "tags": [{"name": "Travel", "rank": 80, "category": "Theme-Other", "isAdult": False,
                  "isGeneralSpoiler": False, "isMediaSpoiler": False},
                 {"name": "Secret Ending", "rank": 90, "isAdult": False, "isGeneralSpoiler": False, "isMediaSpoiler": True},
                 {"name": "General Secret", "rank": 90, "isAdult": False, "isGeneralSpoiler": True, "isMediaSpoiler": False},
                 {"name": "Unknown", "rank": 90, "isAdult": False, "isGeneralSpoiler": None, "isMediaSpoiler": False}],
        "description": "Unrequested data must not be retained", "siteUrl": "https://evil.test/"}]}}}


def response(data=None, *, status=200, headers=None, chunks=None):
    result = MagicMock()
    result.__enter__.return_value = result
    result.status_code = status
    result.headers = headers or {}
    result.iter_content.return_value = chunks if chunks is not None else [json.dumps(anilist_response() if data is None else data).encode()]
    return result


class AniListCatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(demo=False, library_root=self.root / 'library', data_root=self.root / 'state',
                                 anilist=AniListSettings(enabled=True))
        self.store = StateStore(self.settings)
        self.catalog = AniList(self.settings.anilist, self.store)

    def test_fixed_endpoint_variables_manga_filter_and_minimal_fields(self):
        with patch('requests.Session.post', return_value=response()) as post:
            result = self.catalog.search(anilist_query(title='架空の物語'))
        args, kwargs = post.call_args
        self.assertEqual(args[0], ANILIST_ENDPOINT)
        self.assertFalse(kwargs['allow_redirects'])
        self.assertTrue(kwargs['stream'])
        self.assertEqual(kwargs['timeout'], 10)
        self.assertEqual(kwargs['json']['variables'], {'search': '架空の物語', 'page': 1, 'perPage': 10})
        self.assertIn('type: MANGA, format_in: [MANGA, ONE_SHOT]', kwargs['json']['query'])
        self.assertNotIn('架空の物語', kwargs['json']['query'])
        self.assertNotIn('Authorization', kwargs['headers'])
        item = result['candidates'][0]
        self.assertEqual(item['url'], 'https://anilist.co/manga/12345')
        self.assertEqual(item['fields'], {'genres': ['ファンタジー', 'ドラマ'], 'tags': ['旅']})
        self.assertEqual(item['alternate_titles'], ['Kakuu no Monogatari'])
        self.assertNotIn('Unrequested', json.dumps(result))
        self.assertNotIn('Secret', json.dumps(result))
        self.assertNotIn('Unknown', json.dumps(result))
        self.assertNotIn('evil.test', json.dumps(result))

    def test_adult_weak_tags_are_excluded_and_labels_are_japanese(self):
        data = anilist_response()
        item = data['data']['Page']['media'][0]
        item['genres'] = ['Romance', 'Hentai', 'Slice of Life', 'Future Genre']
        def tag(name, rank=75, category='Theme-Other', adult=False):
            return {'name': name, 'rank': rank, 'category': category, 'isAdult': adult,
                    'isGeneralSpoiler': False, 'isMediaSpoiler': False}
        item['tags'] = [tag('Seinen', 90, 'Demographic'), tag('Isekai'), tag('Explicit Example', 95, adult=True),
                        tag('Content Example', 95, 'Sexual Content'), tag('Weak Example', 59), tag('Female Harem', 60),
                        tag('Brand New Tag', 70), {**tag('No Rank'), 'rank': None}, {**tag('No Adult Flag'), 'isAdult': None}]
        with patch('requests.Session.post', return_value=response(data)):
            fields = self.catalog.search(anilist_query(title='架空の物語'))['candidates'][0]['fields']
        # Demographics are genres like 少年・青年; unknown new names keep AniList spelling.
        self.assertEqual(fields, {'genres': ['青年', '恋愛', '日常', 'Future Genre'],
                                  'tags': ['異世界', 'ハーレム', 'Brand New Tag']})
        for bad in ({'rank': 101}, {'rank': '80'}, {'isAdult': 'no'}, {'category': 3}):
            item['tags'] = [{**tag('Isekai'), **bad}]
            with self.subTest(bad=bad), patch('requests.Session.post', return_value=response(data)), \
                    self.assertRaises(CatalogError):
                self.catalog.search(anilist_query(title='架空の物語'))

    def test_bundled_vocabulary_has_unique_non_latin_labels(self):
        from manga_kit.anilist_ja import DEMOGRAPHICS, GENRES, TAGS
        labels = [*GENRES.values(), *DEMOGRAPHICS.values(), *TAGS.values()]
        self.assertEqual(len(labels), len(set(labels)))
        # Komga lowercases genres and tags; Latin-only labels would change shape.
        self.assertEqual([label for label in labels if label.isascii()], [])
        self.assertNotIn('Hentai', GENRES)

    def test_optional_values_and_native_title_fallback(self):
        data = anilist_response()
        item = data['data']['Page']['media'][0]
        item.update(title={'native': None, 'romaji': 'Kakuu'}, startDate=None, countryOfOrigin=None, genres=None, tags=None, format='ONE_SHOT')
        candidate = AniList.parse(data, 1)['candidates'][0]
        self.assertEqual(candidate['title'], 'Kakuu')
        self.assertEqual(candidate['fields'], {'genres': [], 'tags': []})
        self.assertEqual(candidate['format'], 'ONE_SHOT')

    def test_pagination_uses_has_next_instead_of_inaccurate_totals(self):
        data = anilist_response()
        data['data']['Page']['pageInfo'].update(total=-1, lastPage=None, hasNextPage=True)
        result = AniList.parse(data, 1)
        self.assertTrue(result['has_next'])
        self.assertIsNone(result['total'])
        self.assertIsNone(result['page_count'])
        data['data']['Page']['pageInfo']['currentPage'] = 100
        self.assertFalse(AniList.parse(data, 100)['has_next'])
        data['data']['Page']['media'] = []
        data['data']['Page']['pageInfo']['hasNextPage'] = False
        self.assertEqual(AniList.parse(data, 100)['candidates'], [])

    def test_query_validation_and_normalization(self):
        self.assertEqual(anilist_query(title=' Ｋａｋｕｕ '), {'title': 'Kakuu', 'page': 1})
        for values in ({}, {'title': []}, {'title': 'x' * 201}, {'title': '\n作品'},
                       {'title': '作品', 'page': True}, {'title': '作品', 'page': 101},
                       {'title': '作品', 'author': ''}, {'title': '作品', 'isbn': ''}):
            with self.subTest(values=values), self.assertRaises(ConfigError):
                anilist_query(**values)

    def test_disabled_catalog_does_not_contact_network(self):
        catalog = AniList(AniListSettings(), self.store)
        with patch('requests.Session.post', side_effect=AssertionError('Network called')), self.assertRaises(ConfigError):
            catalog.search(anilist_query(title='作品'))

    def test_rate_limit_is_shared_across_instances_and_libraries(self):
        other_settings = replace(self.settings, library_root=self.root / 'other-library')
        other = AniList(other_settings.anilist, StateStore(other_settings))
        with patch('requests.Session.post', return_value=response()) as post:
            self.catalog.search(anilist_query(title='作品'))
            with self.assertRaises(CatalogError) as caught:
                other.search(anilist_query(title='別作品'))
        self.assertEqual(post.call_count, 1)
        self.assertGreaterEqual(caught.exception.retry_after, 1)
        with self.store.connection() as connection:
            self.assertGreater(connection.execute('SELECT next_allowed FROM metadata_requests').fetchone()[0], time.time() + 1)

    def test_429_waits_without_retry_and_hides_upstream_messages(self):
        with patch('requests.Session.post', return_value=response(status=429, headers={'Retry-After': '120'})) as post:
            with self.assertRaises(CatalogError) as caught:
                self.catalog.search(anilist_query(title='作品'))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(caught.exception.retry_after, 120)
        with patch('requests.Session.post', side_effect=AssertionError('Retried')), self.assertRaises(CatalogError):
            self.catalog.search(anilist_query(title='作品'))

    def test_successful_response_with_exhausted_quota_sets_cooldown(self):
        with patch('requests.Session.post', return_value=response(headers={'X-RateLimit-Remaining': '0'})):
            self.catalog.search(anilist_query(title='作品'))
        with self.store.connection() as connection:
            self.assertGreater(connection.execute('SELECT next_allowed FROM metadata_requests').fetchone()[0], time.time() + 59)

    def test_http_json_size_and_transport_failures_are_safe(self):
        for upstream in (response(status=301), response(status=403), response(status=500),
                         response(chunks=[b'not JSON']), response(chunks=[b'x' * (MAX_RESPONSE_BYTES + 1)]),
                         response(chunks=[b'[' * 2000]), requests.Timeout('private-network-details')):
            with self.subTest(upstream=type(upstream).__name__):
                with self.store.connection() as connection:
                    connection.execute('DELETE FROM metadata_requests')
                with patch('requests.Session.post', side_effect=upstream if isinstance(upstream, Exception) else None,
                           return_value=upstream), self.assertRaises(CatalogError) as caught:
                    self.catalog.search(anilist_query(title='作品'))
                self.assertNotIn('private-', str(caught.exception))

    def test_malformed_or_partial_results_are_rejected(self):
        invalid = [None, [], {}, {'errors': [{'message': 'private details'}], 'data': anilist_response()['data']}]
        for key, value in (('id', True), ('id', -1), ('type', 'ANIME'), ('format', 'NOVEL'),
                           ('title', {'native': ''}), ('title', {'native': 'x' * 301}), ('title', []),
                           ('genres', 'Fantasy'), ('genres', False), ('genres', [None]), ('tags', {}),
                           ('tags', [{'name': 'X', 'isGeneralSpoiler': 'no'}]),
                           ('countryOfOrigin', 'not-a-country'), ('startDate', []), ('startDate', {'year': True})):
            data = anilist_response()
            data['data']['Page']['media'][0][key] = value
            invalid.append(data)
        for key, value in (('hasNextPage', 1), ('currentPage', True), ('currentPage', 2)):
            data = anilist_response()
            data['data']['Page']['pageInfo'][key] = value
            invalid.append(data)
        for count in (2, 11):
            data = anilist_response()
            data['data']['Page']['media'] *= count
            invalid.append(data)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(CatalogError):
                AniList.parse(data, 1)

    def test_classification_validation_deduplicates_without_losing_order(self):
        self.assertEqual(validate_fields({'tags': [' Travel ', 'travel', '旅'], 'genres': []}),
                         {'tags': ['Travel', '旅'], 'genres': []})
        for value in ('tag', [None], [''], ['   '], ['bad\nvalue'], ['x' * 101], ['x'] * 101):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                validate_fields({'tags': value})

    def test_anilist_config_is_explicit_and_validated(self):
        path = self.root / 'config.yaml'
        for section in ('enabled: yesplease', 'timeout_seconds: false', 'timeout_seconds: 31',
                        'base_url: https://evil.test', 'api_key: secret'):
            path.write_text('anilist:\n  ' + section + '\n')
            with self.subTest(section=section), self.assertRaises(ConfigError):
                load_settings(path)
        path.write_text('anilist:\n  enabled: true\n  timeout_seconds: 5\n')
        self.assertEqual(load_settings(path).anilist, AniListSettings(True, 5))
        self.assertFalse(load_settings(self.root / 'config.yaml').rakuten.enabled)
        self.assertFalse(load_settings(Path(__file__).parents[1] / 'config.example.yaml').anilist.enabled)


class ClassificationIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(library_root=self.root / 'library', data_root=self.root / 'state')
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected network'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.app = create_app(settings=self.settings)
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.client = self.app.test_client()
        self.series = self.client.get('/api/library').json['series'][0]
        self.base = '/api/metadata/' + self.series['id']

    def proposal(self, provider='anilist', fields=None):
        response = self.client.post(self.base + '/search', json={'provider': provider, 'title': self.series['title']})
        self.assertEqual(response.status_code, 200, response.json)
        result = response.json
        candidate = result['candidates'][0]
        body = {'provider': provider, 'search_id': result['search_id'], 'candidate_id': candidate['id'],
                'fields': candidate['fields'] if fields is None else fields}
        preview = self.client.post(self.base + '/source/preview', json=body)
        self.assertEqual(preview.status_code, 200, preview.json)
        body.update(revision=preview.json['revision'], base_fingerprint=preview.json['base_fingerprint'])
        return body, result

    def save(self, provider='anilist', fields=None):
        body, _ = self.proposal(provider, fields)
        response = self.client.post(self.base + '/source', json=body)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_multiple_links_persist_after_restart_and_appear_in_pages_and_search(self):
        bibliography = self.save('rakuten_books')['provenance']
        saved = self.save()
        # The bibliography demo also proposes a genre. Replacing it
        # with AniList's classification transfers that field's attribution.
        bibliography['applied_fields'] = [field for field in bibliography['applied_fields'] if field != 'genres']
        self.assertEqual(saved['provenance'], bibliography)
        self.assertEqual(set(saved['provenances']), {'rakuten_books', 'anilist'})
        self.assertEqual(saved['provenances']['anilist']['provider'], 'demo_anilist')
        self.assertEqual(saved['series']['genres'], ['日常', 'ファンタジー'])
        other = create_app(settings=self.settings)
        self.addCleanup(other.extensions['jobs'].shutdown)
        self.assertEqual(other.test_client().get(self.base).json['provenances'], saved['provenances'])
        for path in ('/settings', '/metadata/' + self.series['id'], '/metadata/' + self.series['id'] + '/sources?provider=anilist'):
            html = self.client.get(path)
            self.assertEqual(html.status_code, 200)
            self.assertIn('AniList', html.get_data(as_text=True))
        html = self.client.get('/library?q=仲間').get_data(as_text=True)
        self.assertIn(self.series['title'], html)
        self.assertNotIn('該当する作品がありません', html)
        self.assertIn('架空のタグ・ジャンル', saved['series']['sources'])

    def test_bibliographic_updates_preserve_classification_link_and_values(self):
        tags = self.save()
        saved = self.save('rakuten_books', fields={'publisher': '手元の出版社'})
        self.assertEqual(saved['provenances']['anilist'], tags['provenances']['anilist'])
        self.assertEqual(saved['series']['tags'], tags['series']['tags'])

    def test_unlink_only_selected_source_and_reset_all(self):
        self.save('rakuten_books')
        saved = self.save()
        review = self.client.post(self.base + '/source/preview', json={'provider': 'anilist', 'unlink': True}).json
        unlinked = self.client.post(self.base + '/source', json={'provider': 'anilist', 'unlink': True,
            'revision': review['revision'], 'base_fingerprint': review['base_fingerprint']}).json
        self.assertEqual(set(unlinked['provenances']), {'rakuten_books'})
        self.assertEqual(unlinked['overrides'], saved['overrides'])
        self.save()
        review = self.client.post(self.base + '/preview', json={'reset': True}).json
        reset = self.client.post(self.base, json={'reset': True, 'revision': review['revision'],
            'base_fingerprint': review['base_fingerprint']}).json
        self.assertEqual(reset['provenances'], {})
        self.assertEqual(reset['overrides'], {})
        self.assertEqual(reset['series']['genres'], [])

    def test_manual_edits_and_empty_list_clear_only_changed_field_attribution(self):
        saved = self.save()
        body = {'fields': {'genres': []}, 'revision': saved['revision'], 'base_fingerprint': saved['base_fingerprint']}
        edited = self.client.post(self.base, json=body).json
        self.assertEqual(edited['series']['genres'], [])
        self.assertEqual(edited['provenances']['anilist']['applied_fields'], ['tags'])
        self.assertEqual(edited['series']['tags'], saved['series']['tags'])

    def test_unlinking_bibliography_preserves_classification_only_link(self):
        self.save('rakuten_books', fields={})
        saved = self.save(fields={})
        review = self.client.post(self.base + '/source/preview', json={'unlink': True}).json
        unlinked = self.client.post(self.base + '/source', json={'unlink': True, 'revision': review['revision'],
            'base_fingerprint': review['base_fingerprint']}).json
        self.assertEqual(unlinked['provenance'], {})
        self.assertEqual(unlinked['provenances'], {'anilist': saved['provenances']['anilist']})
        self.assertEqual(unlinked['overrides'], {})
        html = self.client.get('/metadata/' + self.series['id']).get_data(as_text=True)
        self.assertNotIn('id="metadata-reset" disabled', html)

    def test_adjusted_values_are_reviewed_and_unselected_values_remain(self):
        body, _ = self.proposal(fields={'genres': ['自分のジャンル']})
        review = self.client.post(self.base + '/source/preview', json={k: v for k, v in body.items()
                                  if k not in {'revision', 'base_fingerprint'}}).json
        self.assertEqual(review['provenance']['adjusted_fields'], ['genres'])
        self.assertEqual(review['changes'], [{'field': 'genres', 'before': [], 'after': ['自分のジャンル']}])
        saved = self.client.post(self.base + '/source', json=body).json
        self.assertEqual(saved['series']['tags'], [])
        self.assertEqual(self.app.extensions['state'].events()[0]['detail']['source_provider'], 'anilist')

    def test_cache_is_provider_scoped_and_cross_source_candidates_are_rejected(self):
        _, first = self.proposal()
        cached = self.client.post(self.base + '/search', json={'provider': 'anilist', 'title': self.series['title']}).json
        self.assertTrue(cached['cached'])
        self.assertEqual(cached['search_id'], first['search_id'])
        _, bibliography = self.proposal('rakuten_books', fields={})
        self.assertNotEqual(bibliography['search_id'], first['search_id'])
        response = self.client.post(self.base + '/source/preview', json={'search_id': first['search_id'],
            'candidate_id': first['candidates'][0]['id'], 'fields': {}})
        self.assertEqual(response.status_code, 409)
        with self.app.extensions['state'].connection() as connection:
            connection.execute('UPDATE metadata_searches SET expires=0')
        response = self.client.post(self.base + '/source/preview', json={'provider': 'anilist', 'search_id': first['search_id'],
            'candidate_id': first['candidates'][0]['id'], 'fields': {}})
        self.assertEqual(response.status_code, 409)

    def test_untrusted_provider_fields_and_cross_origin_are_rejected(self):
        body, _ = self.proposal()
        for extra, status in (({'fields': {'status': 'completed'}}, 400), ({'provider': 'fake'}, 400),
                              ({'provider': None}, 400), ({'fields': {'tags': 'invalid'}}, 400)):
            candidate = {key: value for key, value in {**body, **extra}.items() if key not in {'revision', 'base_fingerprint'}}
            self.assertEqual(self.client.post(self.base + '/source/preview', json=candidate).status_code, status)
        self.assertEqual(self.client.post(self.base + '/search', json={'provider': 'anilist', 'title': '作品'},
                                         headers={'Origin': 'https://evil.test'}).status_code, 403)
        self.assertEqual(self.client.get('/metadata/' + self.series['id'] + '/sources?provider=unknown').status_code, 400)

    def test_stale_revision_and_other_series_candidates_cannot_be_saved(self):
        body, _ = self.proposal()
        other = self.client.get('/api/library').json['series'][1]
        self.assertEqual(self.client.post('/api/metadata/' + other['id'] + '/source', json=body).status_code, 409)
        self.save('rakuten_books', fields={})
        self.assertEqual(self.client.post(self.base + '/source', json=body).status_code, 409)

    def test_live_disabled_anilist_and_initial_pages_do_not_use_network(self):
        live = replace(self.settings, demo=False)
        create_sample_library(live.library_root, live.layout)
        app = create_app(settings=live)
        self.addCleanup(app.extensions['jobs'].shutdown)
        client = app.test_client()
        series_id = client.get('/api/library').json['series'][0]['id']
        path = '/api/metadata/' + series_id + '/search'
        self.assertEqual(client.get('/metadata/' + series_id + '/sources?provider=anilist').status_code, 200)
        self.assertEqual(client.post(path, json={'provider': 'anilist', 'title': '作品'}).status_code, 400)
        self.assertFalse(client.get('/api/settings').json['metadata_catalogs']['anilist']['configured'])

    def test_tags_and_both_sources_survive_move_restore_without_archive_changes(self):
        live = replace(self.settings, demo=False, execution_enabled=True)
        create_sample_library(live.library_root, live.layout)
        store = StateStore(live)
        library = LibraryService(live, store=store)
        metadata = MetadataService(library, store)
        series = library.scan().series[0]
        links = {'rakuten_books': {'provider': 'rakuten_books', 'item_id': 'example', 'applied_fields': ['reading']},
                 'anilist': {'provider': 'anilist', 'item_id': '12345', 'applied_fields': ['genres', 'tags']}}
        before = {path.relative_to(live.library_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in live.library_root.rglob('*') if path.is_file()}
        store.save_edit(series.id, {'genres': ['Fantasy'], 'tags': ['Travel']}, 0, {}, provenance=links)
        operations = OperationService(live)
        operation = operations.prepare(layout={'series': '{title}'}, selected=[series.id])
        info = metadata.inspect(series.id)
        with operations.locked(), self.assertRaises(StateConflict):
            metadata.save(series.id, {'tags': []}, info['revision'], info['base_fingerprint'])
        operations.execute(operation['plan']['id'], operation['digest'])
        target = operation['plan']['groups'][0]['target_id']
        self.assertEqual(source_links(operations.store.edits()[target]['provenance']), links)
        operations.restore(operation['plan']['id'], operation['digest'])
        self.assertEqual(metadata.inspect(series.id)['provenances'], links)
        self.assertEqual(metadata.inspect(series.id)['series']['tags'], ['Travel'])
        after = {path.relative_to(live.library_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in live.library_root.rglob('*') if path.is_file()}
        self.assertEqual(after, before)

    def test_sidecar_and_comicinfo_classifications_can_be_overridden_and_reset(self):
        live = replace(self.settings, demo=False)
        directory = live.library_root / '作品'
        directory.mkdir(parents=True)
        with zipfile.ZipFile(directory / '作品 第1巻.cbz', 'w') as archive:
            archive.writestr('ComicInfo.xml', '<ComicInfo><Series>作品</Series><Genre>Fantasy, Drama</Genre><Tags>Travel, travel</Tags></ComicInfo>')
        store = StateStore(live)
        library = LibraryService(live, store=store)
        metadata = MetadataService(library, store)
        series = library.scan().series[0]
        self.assertEqual(series.genres, ['Fantasy', 'Drama'])
        self.assertEqual(series.tags, ['Travel'])
        (directory / 'series.json').write_text(json.dumps({'genres': ['日常'], 'tags': []}))
        info = metadata.preview(series.id, {'genres': [], 'tags': ['手動']}, refresh=True)
        saved = metadata.save(series.id, {'genres': [], 'tags': ['手動']}, info['revision'], info['base_fingerprint'])
        self.assertEqual(saved['original']['genres'], ['日常'])
        self.assertEqual(saved['series']['genres'], [])
        reset = metadata.save(series.id, None, saved['revision'], saved['base_fingerprint'], reset=True)
        self.assertEqual(reset['series']['genres'], ['日常'])
        self.assertEqual(reset['series']['tags'], [])


if __name__ == '__main__':
    unittest.main()
