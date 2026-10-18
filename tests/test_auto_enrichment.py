from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from manga_kit import create_app
from manga_kit.catalogs import CatalogError, RakutenBooks
from manga_kit.config import Settings
from manga_kit.sample import create_sample_library
from test_enrichment import response_data


class AutoEnrichmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(demo=False, library_root=self.root / 'library', data_root=self.root / 'state')
        create_sample_library(self.settings.library_root, self.settings.layout)
        self.app = create_app(settings=self.settings)
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.client = self.app.test_client()
        self.store = self.app.extensions['state']
        self.entries = self.app.extensions['library'].scan().series
        self.by_title = {series.title: series for series in self.entries}
        self.catalog = Mock(id='rakuten_books', label='楽天Books')
        self.catalog.configured.return_value = True
        self.catalog.search.side_effect = self.result
        self.app.extensions['enrichment'].provider = self.catalog

    def result(self, query):
        series = self.by_title[query['title']]
        data = response_data()
        data['Items'][0].update(title=series.title + '（1）', titleKana=series.reading or 'ツキヲマツエキ',
                               author=series.author or '架空作者', publisherName=series.publisher,
                               seriesName='架空少年コミックス')
        return RakutenBooks.parse(data, 1)

    def run_batch(self, selected=None, provider='rakuten_books'):
        selected = selected or [self.entries[0].id, self.entries[1].id]
        response = self.client.post('/api/enrichment/batches', json={'series_ids': selected, 'provider': provider})
        self.assertEqual(response.status_code, 202, response.json)
        job_id = response.json['id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.store.job(job_id, include_result=True)
            if job['status'] not in {'queued', 'running'}:
                self.assertEqual(job['status'], 'succeeded', job)
                return job
            time.sleep(0.01)
        self.fail('Background enrichment did not finish')

    def apply(self, job, **extra):
        return self.client.post(f'/api/enrichment/batches/{job["id"]}/apply',
                                json={'confirm': job['result']['digest'], **extra})

    def files(self):
        return {p.relative_to(self.settings.library_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.settings.library_root.rglob('*') if p.is_file()}

    def test_collect_is_read_only_and_save_fills_only_missing_fields_once(self):
        before = self.files()
        job = self.run_batch()
        self.assertEqual(job['result']['counts']['ready'], 2)
        self.assertEqual(self.store.edits(), {})
        self.assertEqual(self.files(), before)
        response = self.apply(job)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['saved'], 2)
        for row in job['result']['rows']:
            current = self.client.get('/api/metadata/' + row['series_id']).json
            self.assertEqual(current['series']['genres'], ['少年'])
            self.assertEqual(current['series']['summary'], self.by_title[row['title']].summary)
            self.assertEqual(current['provenance']['identity']['label'], '架空少年コミックス')
        revisions = {key: value['revision'] for key, value in self.store.edits().items()}
        self.assertTrue(self.apply(job).json['already_saved'])
        self.assertEqual(revisions, {key: value['revision'] for key, value in self.store.edits().items()})
        self.assertEqual(self.files(), before)
        self.assertEqual(self.catalog.search.call_count, 2)

    def test_manual_edit_after_review_blocks_entire_save(self):
        job = self.run_batch()
        target = job['result']['rows'][1]['series_id']
        metadata = self.app.extensions['metadata']
        current = metadata.inspect(target)
        metadata.save(target, {'summary': 'あとから修正したあらすじ'}, current['revision'], current['base_fingerprint'])
        before = deepcopy(self.store.edits())
        self.assertEqual(self.apply(job).status_code, 409)
        self.assertEqual(self.store.edits(), before)

    def test_changed_sidecar_blocks_entire_save_without_partial_updates(self):
        job = self.run_batch()
        series = self.by_title[job['result']['rows'][1]['title']]
        sidecar = self.settings.library_root / series.path / 'series.json'
        data = json.loads(sidecar.read_text())
        data['publisher'] = '変更後の出版社'
        sidecar.write_text(json.dumps(data))
        self.assertEqual(self.apply(job).status_code, 409)
        self.assertEqual(self.store.edits(), {})

    def test_expired_candidates_require_new_collection_and_no_save(self):
        job = self.run_batch()
        with self.store.connection() as connection:
            connection.execute('UPDATE metadata_searches SET expires=0')
        self.assertEqual(self.apply(job).status_code, 409)
        self.assertEqual(self.store.edits(), {})

    def test_explicit_confirmation_and_server_owned_plan_are_required(self):
        job = self.run_batch()
        self.assertEqual(self.apply(job, confirm='wrong').status_code, 409)
        self.assertEqual(self.apply(job, fields={'title': 'forged'}).status_code, 400)
        self.assertEqual(self.store.edits(), {})

    def test_other_library_cannot_read_or_save_batch(self):
        job = self.run_batch()
        other = create_app(settings=replace(self.settings, library_root=self.root / 'other-library'))
        self.addCleanup(other.extensions['jobs'].shutdown)
        self.assertEqual(other.test_client().get('/metadata?batch=' + job['id']).status_code, 404)
        self.assertEqual(other.test_client().post(f'/api/enrichment/batches/{job["id"]}/apply',
                         json={'confirm': job['result']['digest']}).status_code, 400)

    def test_ambiguous_authors_editions_and_large_result_sets_are_left_for_review(self):
        original = self.result
        for variant in ('author', 'edition', 'many', 'subtitle'):
            def ambiguous(query):
                result = original(query)
                if variant == 'many':
                    result['total'] = 100
                elif variant == 'subtitle':
                    result['candidates'][0]['fields']['title'] += '（続編）'
                else:
                    other = deepcopy(result['candidates'][0])
                    other['id'] = '9780000000026'
                    if variant == 'author':
                        other['fields']['author'] = '別の作者'
                    else:
                        other['editions'] = ['特装版']
                    result['candidates'].append(other)
                    result['total'] = 2
                return result
            self.catalog.search.side_effect = ambiguous
            with self.store.connection() as connection:
                connection.execute('DELETE FROM metadata_searches')
            with self.subTest(variant=variant):
                job = self.run_batch([self.entries[0].id])
                self.assertEqual(job['result']['rows'][0]['state'], 'review')
                self.assertEqual(self.apply(job).status_code, 400)
        self.assertEqual(self.store.edits(), {})

    def test_later_volume_does_not_fill_series_summary(self):
        series = next(entry for entry in self.entries if entry.title == '月を待つ駅')
        def later(query):
            result = self.result(query)
            result['candidates'][0]['volume'] = '8'
            return result
        self.catalog.search.side_effect = later
        job = self.run_batch([series.id])
        row = job['result']['rows'][0]
        self.assertEqual(row['state'], 'ready')
        self.assertNotIn('summary', row['fields'])
        self.assertIn('reading', row['fields'])
        self.assertEqual(self.apply(job).status_code, 200)

    def test_429_stops_further_network_searches_without_retry(self):
        self.catalog.search.side_effect = CatalogError('サービスが待機中です。', retry_after=120)
        job = self.run_batch()
        self.assertEqual(job['result']['counts']['error'], 2)
        self.assertEqual(self.catalog.search.call_count, 1)

    def test_network_failure_does_not_discard_other_series_results(self):
        first = self.result({'title': self.entries[0].title})
        self.catalog.search.side_effect = [CatalogError('接続できません。'), first]
        job = self.run_batch([self.entries[1].id, self.entries[0].id])
        self.assertEqual(job['result']['counts']['error'], 1)
        self.assertEqual(job['result']['counts']['ready'], 1)

    def test_search_spacing_and_existing_cooldown_are_respected(self):
        with self.store.connection() as connection:
            connection.execute('INSERT INTO metadata_requests VALUES(?,?)', ('rakuten_books', time.time() + 1))
        with patch('manga_kit.auto_enrichment.time.sleep') as sleep:
            self.app.extensions['auto_enrichment']._pace('rakuten_books')
        self.assertGreater(sleep.call_args.args[0], 0.5)
        with self.store.connection() as connection:
            connection.execute('UPDATE metadata_requests SET next_allowed=?', (time.time() + 120,))
        job = self.run_batch()
        self.assertEqual(job['result']['counts']['error'], 2)
        self.catalog.search.assert_not_called()

    def test_unconfigured_invalid_and_cross_origin_requests_do_not_start_search(self):
        for body in ({'series_ids': []}, {'series_ids': [self.entries[0].id] * 2}, {'series_ids': ['missing']},
                     {'series_ids': ['x'] * 61}, {'series_ids': [None]}, {'series_ids': [self.entries[0].id], 'provider': []}):
            self.assertEqual(self.client.post('/api/enrichment/batches', json=body).status_code, 400)
        self.assertEqual(self.client.post('/api/enrichment/batches', json={'series_ids': [self.entries[0].id]},
                        headers={'Origin': 'https://evil.test'}).status_code, 403)
        self.catalog.configured.return_value = False
        self.assertEqual(self.client.post('/api/enrichment/batches', json={'series_ids': [self.entries[0].id]}).status_code, 400)
        self.catalog.search.assert_not_called()

    def test_result_survives_restart_and_history_returns_to_review_page(self):
        job = self.run_batch()
        other = create_app(settings=self.settings)
        self.addCleanup(other.extensions['jobs'].shutdown)
        response = other.test_client().get('/metadata?batch=' + job['id'])
        self.assertEqual(response.status_code, 200)
        self.assertIn('不足情報をまとめて保存', response.get_data(as_text=True))
        html = self.client.get('/jobs/' + job['id']).get_data(as_text=True)
        self.assertIn('補完結果を確認', html)
        self.assertIn('batch=' + job['id'], html)

    def test_anilist_proposals_preserve_bibliography_and_an_existing_source(self):
        metadata = self.app.extensions['metadata']
        current = metadata.inspect(self.entries[0].id)
        previous = {'provider': 'rakuten_books', 'item_id': 'saved-isbn', 'url': '', 'applied_fields': ['reading'],
                    'adjusted_fields': [], 'retrieved_at': '2026-10-17T00:00:00+00:00'}
        metadata.save(self.entries[0].id, {'title': current['series']['title']}, current['revision'],
                      current['base_fingerprint'], provenance=previous)
        tags = Mock(id='anilist', label='AniList')
        tags.configured.return_value = True
        tags.search.return_value = {'candidates': [{'id': '99', 'title': current['series']['title'],
            'alternate_titles': [], 'fields': {'genres': ['日常'], 'tags': ['旅']}, 'url': 'https://anilist.co/manga/99'}],
            'page': 1, 'page_count': None, 'total': None, 'has_next': False}
        self.app.extensions['enrichment'].tag_provider = tags
        job = self.run_batch([self.entries[0].id], provider='anilist')
        self.assertEqual(job['result']['counts']['ready'], 1)
        self.assertEqual(self.apply(job).status_code, 200)
        saved = metadata.inspect(self.entries[0].id)
        self.assertEqual(saved['series']['reading'], current['series']['reading'])
        self.assertEqual(saved['provenances']['rakuten_books'], previous)
        self.assertEqual(saved['provenances']['anilist']['item_id'], '99')

    def test_complete_series_are_skipped_without_search(self):
        metadata = self.app.extensions['metadata']
        current = metadata.inspect(self.entries[0].id)
        metadata.save(self.entries[0].id, {'genres': ['日常']}, current['revision'], current['base_fingerprint'])
        job = self.run_batch([self.entries[0].id])
        self.assertEqual(job['result']['counts']['unchanged'], 1)
        self.catalog.search.assert_not_called()

    def test_first_volume_is_selected_from_consistent_volumes(self):
        def volumes(query):
            result = self.result(query)
            later = deepcopy(result['candidates'][0])
            later.update(id='9780000000026', volume='2')
            result['candidates'].insert(0, later)
            result['total'] = 2
            return result
        self.catalog.search.side_effect = volumes
        job = self.run_batch([self.entries[0].id])
        self.assertEqual(job['result']['rows'][0]['candidate_id'], '9780000000019')

    def test_organizer_lock_prevents_batch_save(self):
        from manga_kit.operations import OperationService
        job = self.run_batch()
        with OperationService(replace(self.settings, execution_enabled=True)).locked():
            self.assertEqual(self.apply(job).status_code, 409)
        self.assertEqual(self.store.edits(), {})

    def test_unexpected_worker_errors_do_not_expose_upstream_messages(self):
        self.catalog.search.side_effect = RuntimeError('private-token-should-not-appear')
        response = self.client.post('/api/enrichment/batches', json={'series_ids': [self.entries[0].id]})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.store.job(response.json['id'], include_result=True)
            if job['status'] == 'failed':
                self.assertNotIn('private-token', json.dumps(job))
                break
            time.sleep(0.01)
        else:
            self.fail('Expected a safely reported worker failure')
        self.assertEqual(self.store.edits(), {})


if __name__ == '__main__':
    unittest.main()
