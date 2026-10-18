from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from manga_kit import create_app
from manga_kit.config import ConfigError, Settings, ToolsSettings, load_settings
from manga_kit.credentials import save_credentials, secret
from manga_kit.feeds import parse_curated, parse_ranking, parse_rss, season
from manga_kit.kobo import Kobo
from manga_kit.new_volumes import analyze_candidates, release_date
from manga_kit.state import StateConflict, StateStore


class ToolsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = Settings(library_root=self.root / 'library', data_root=self.root / 'state',
                                 tools=ToolsSettings(credentials_file=str(self.root / 'config' / 'secrets.json')))
        env = patch.dict(os.environ, {}, clear=True); env.start(); self.addCleanup(env.stop)
        self.app = create_app(settings=self.settings); self.app.testing = True
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.client = self.app.test_client()
        self.store = self.app.extensions['state']

    def test_tools_pages_never_fetch_on_get_and_render_owned_links(self):
        with patch('requests.Session.request', side_effect=AssertionError('implicit network')):
            for path in ['/anime', '/discovery', '/discovery?source=natalie', '/tsugimanga', '/new_volumes', '/settings/tools']:
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 200)
            row = self.app.extensions['inventory'].cached()['series'][0]
            self.assertEqual(self.client.get('/shelf/series/' + row['id']).status_code, 200)
            self.assertEqual(self.client.get('/shelf').status_code, 404)
        self.assertEqual(self.store.jobs(), [])

    def test_tool_cache_is_scoped_versioned_and_optimistic(self):
        self.assertIsNone(self.store.cached('sample'))
        self.assertEqual(self.store.cache('sample', {'日本語': True}, revision=0), 1)
        with self.assertRaises(StateConflict):
            self.store.cache('sample', {}, revision=0)
        self.assertEqual(self.store.cached('sample')['data'], {'日本語': True})
        other = StateStore(replace(self.settings, library_root=self.root / 'other'))
        self.assertIsNone(other.cached('sample'))

    def test_private_credentials_environment_priority_and_no_public_echo(self):
        save_credentials(self.settings, {'KOMGA_API_KEY': 'secret-value'})
        self.assertEqual(stat.S_IMODE(Path(self.settings.tools.credentials_file).stat().st_mode), 0o600)
        self.assertEqual(secret(self.settings, 'KOMGA_API_KEY'), 'secret-value')
        with patch.dict(os.environ, {'KOMGA_API_KEY': 'from-env'}):
            self.assertEqual(secret(self.settings, 'KOMGA_API_KEY'), 'from-env')
        for path in ['/api/settings', '/settings', '/settings/tools']:
            self.assertNotIn('secret-value', self.client.get(path).get_data(as_text=True))
        with self.assertRaises(ConfigError):
            save_credentials(self.settings, {'OTHER_SECRET': 'no'})

    def test_web_preferences_validate_persist_and_take_effect_in_other_instances(self):
        response = self.client.post('/api/tools/settings', json={'revision': 0, 'tools': {'anime_enabled': True}})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.app.config['MANGA_KIT'].tools.anime_enabled)
        other = create_app(settings=self.settings); self.addCleanup(other.extensions['jobs'].shutdown)
        self.assertTrue(other.config['MANGA_KIT'].tools.anime_enabled)
        self.assertEqual(self.client.post('/api/tools/settings', json={'revision': 0, 'tools': {}}).status_code, 409)
        for values in [{'execution_enabled': True}, {'tools': {'credentials_file': '/tmp/no'}}, {'tools': {'anime_enabled': 1}}, {'komga_url': 'http://user:secret@example.org'}]:
            self.assertEqual(self.client.post('/api/tools/settings', json={'revision': 1, **values}).status_code, 400)

    def test_write_actions_refuse_remote_unprotected_clients_and_foreign_origins(self):
        self.assertEqual(self.client.post('/api/tools/inventory/refresh', json={}, environ_base={'REMOTE_ADDR': '192.0.2.1'}).status_code, 400)
        self.assertEqual(self.client.post('/api/tools/credentials', json={'values': {}}, headers={'Origin': 'https://evil.example'}).status_code, 403)

    def test_short_or_ambiguous_titles_need_manual_owned_match(self):
        inventory = self.app.extensions['inventory']
        rows = inventory.cached()['series']
        row = rows[0]
        self.assertTrue(inventory.match(row['title'])['owned'])
        row['title'] = '短'
        self.store.cache('inventory', {**inventory.cached(), 'series': rows})
        self.assertEqual(inventory.match('短')['state'], 'review')
        inventory.match_override('test', 'entry', row['id'])
        self.assertTrue(inventory.match('短', source='test', entry_id='entry')['owned'])
        inventory.match_override('test', 'entry', None)
        self.assertFalse(inventory.match('短', source='test', entry_id='entry')['owned'])

    def test_facts_revision_and_filters_use_original_year(self):
        row = self.app.extensions['inventory'].cached()['series'][0]
        url = '/api/tools/series/' + row['id'] + '/facts'
        value = {'revision': 0, 'label': '架空レーベル', 'magazine': '架空誌', 'demographic': '青年', 'original_year': 1998, 'rating': 4.5}
        self.assertEqual(self.client.post(url, json=value).status_code, 200)
        self.assertEqual(self.client.post(url, json=value).status_code, 409)
        html = self.client.get('/library?label=架空レーベル&decade=1990&rating=4').get_data(as_text=True)
        self.assertIn(row['title'], html)
        self.assertIn(' / 1 作品', html)
        for invalid in [True, [], '1998', 9999]:
            self.assertEqual(self.client.post(url, json={**value, 'revision': 1, 'original_year': invalid}).status_code, 400)

    def test_background_feed_update_and_history_render(self):
        result = self.client.post('/api/tools/feeds/anime/refresh', json={})
        self.assertEqual(result.status_code, 202)
        identifier = result.json['id']
        for _ in range(100):
            job = self.store.job(identifier, include_result=True)
            if job['status'] not in {'queued', 'running'}:
                break
            time.sleep(.01)
        self.assertEqual(job['status'], 'succeeded', job.get('error'))
        self.assertEqual(self.client.get('/jobs/' + identifier).status_code, 200)

    def test_kobo_identifier_is_not_isbn_and_html_is_removed(self):
        result = Kobo.parse({'page': 1, 'count': 1, 'pageCount': 1, 'Items': [{'itemNumber': '1234567890', 'title': '架空の物語（3）',
            'titleKana': 'カクウノモノガタリ 3', 'author': '架空作者', 'publisherName': '架空出版', 'itemUrl': 'https://books.rakuten.co.jp/rk/abc/',
            'itemCaption': '', 'salesDate': '2026年10月01日'}]}, 1)
        item = result['candidates'][0]
        self.assertEqual(item['isbn'], '')
        self.assertEqual(item['volume'], '3')
        self.assertEqual(item['fields']['title'], '架空の物語')

    def test_rss_atom_and_curated_parsers_have_distinct_article_and_work_results(self):
        rss = b'<rss><channel><item><title>News</title><link>https://example.org/story</link></item></channel></rss>'
        self.assertEqual(parse_rss(rss)[0]['title'], 'News')
        atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Atom</title><link href="https://example.org/atom"/></entry></feed>'
        self.assertEqual(parse_rss(atom)[0]['url'], 'https://example.org/atom')
        body = ''.join(f'<h2>架空の作品{i} 全3巻</h2><p>著者：架空作者</p><a href="https://booklive.jp/product/index/title_id/{i}">本</a>' for i in range(3))
        feed = '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item><title>記事</title><link>https://booklive.jp/bkmr/example</link><content:encoded><![CDATA['+body+']]></content:encoded></item></channel></rss>'
        self.assertEqual([item['title'] for item in parse_curated(feed.encode())], ['架空の作品0', '架空の作品1', '架空の作品2'])
        with self.assertRaises(Exception):
            parse_rss(b'<!DOCTYPE x [<!ENTITY secret SYSTEM "file:///etc/passwd">]><rss>&secret;</rss>')

    def test_ranking_parser_uses_structured_entries_not_unrelated_text(self):
        data = {'props': {'pageProps': {'data': {'entries': [{'id': 1, 'title': '<b>架空作品</b>', 'rank': 1, 'author': '作者'}]}}}}
        entries = parse_ranking(('<script id="__NEXT_DATA__" type="application/json">'+json.dumps(data)+'</script>').encode())
        self.assertEqual(entries[0]['title'], '架空作品')
        self.assertEqual(season(date(2026, 1, 1))['season'], 'WINTER')

    def test_release_checks_reject_related_editions_and_uncertain_dates(self):
        row = {'title': '架空の作品', 'author': '架空作者', 'aliases': []}
        def item(volume, day, title='架空の作品', author='架空作者', editions=None):
            return {'id': str(volume), 'title': title+'（'+str(volume)+'）', 'volume': str(volume), 'fields': {'title': title, 'author': author},
                    'sales_date': day, 'editions': editions or [], 'url': 'https://example.org/book'}
        candidates = [item(2, '2026年10月01日'), item(3, '2026年11月01日'), item(99, '2026年10月01日', '関連の作品'),
                      item(4, '2026年10月頃'), item(5, '2026年10月01日', editions=['完全版']), item(6, '2026年10月01日', author='別作者')]
        result = analyze_candidates(row, candidates, date(2026, 10, 18))
        self.assertEqual(result['latest']['volume'], '2')
        self.assertEqual(result['upcoming'][0]['volume'], '3')
        self.assertEqual(len(result['review']), 3)
        self.assertIsNone(release_date('2026年10月頃'))

    def test_new_volume_differential_cache_rechecks_on_identity_change(self):
        service = self.app.extensions['new_volumes']
        row = deepcopy(self.app.extensions['inventory'].cached()['series'][0]); row['status'] = 'ongoing'
        first = service.check([row], today=date(2026, 10, 18))
        self.assertEqual(first['checked'], 1)
        second = service.check([row], today=date(2026, 10, 19))
        self.assertEqual(second['skipped'], 1)
        row['author'] += '別名'
        self.assertEqual(service.check([row], today=date(2026, 10, 19))['checked'], 1)

    def test_link_only_build_saves_identity_without_changing_existing_metadata(self):
        metadata = self.app.extensions['metadata']
        row = self.app.extensions['library'].scan().series[0]
        current = metadata.inspect(row.id)
        fields = {key: current['series'][key] for key in ('title', 'reading', 'author', 'publisher', 'genres', 'summary')}
        before = deepcopy(fields)
        service = self.app.extensions['auto_enrichment']
        result = service.collect([row.id], 'rakuten_books', lambda *_: None, link_only=True)
        self.assertEqual(result['rows'][0]['state'], 'ready', result)
        self.assertEqual(result['rows'][0]['fields'], {})
        identifier = self.store.create_job('enrichment')
        self.store.update_job(identifier, status='succeeded', result=result)
        service.apply(identifier, result['digest'])
        after = metadata.inspect(row.id)
        self.assertEqual({key: after['series'][key] for key in before}, before)
        self.assertIn('rakuten_books', after['provenances'])


if __name__ == '__main__':
    unittest.main()
