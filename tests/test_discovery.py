from copy import deepcopy
from dataclasses import replace
from datetime import date
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from manga_kit import create_app
from manga_kit.config import Settings, ToolsSettings
from manga_kit.discovery import discovery_view
from manga_kit.feeds import parse_curated
from manga_kit.sample import _png


def article(title, slug, *, completed=False, published='Tue, 13 Oct 2026 23:30:00 +0000'):
    body = ''.join(
        f'<h2>架空の作品{index}' + (' 全１２巻' if completed else '') + '</h2>'
        f'<p><a href="https://booklive.jp/product/index/title_id/{index}">'
        f'<img src="https://res.booklive.jp/{index}/001/thumbnail/2L.jpg"></a>'
        f'<strong>出会いと冒険を描く短い紹介文{index}</strong><br>著者：架空作者<br>'
        '<a href="https://booklive.jp/bviewer/s/">試し読み</a></p>'
        for index in range(3))
    return (f'<item><title>{title}</title><link>https://booklive.jp/bkmr/{slug}</link>'
            f'<pubDate>{published}</pubDate><content:encoded><![CDATA[{body}]]></content:encoded></item>')


def curated_feed():
    items = [article('ある作品好きに刺さる漫画3選｜紹介', 'recommend-like-example'),
             article('2026年9月に完結した漫画3選', 'comic_completed_202609', completed=True),
             article('新刊の紹介3選', 'comic_new_202609')]
    return ('<rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>' +
            ''.join(items) + '</channel></rss>').encode()


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(data_root=self.root / 'state', library_root=self.root / 'library')
        self.app = self.make_app(self.settings)
        self.client = self.app.test_client()

    def make_app(self, settings):
        app = create_app(settings=settings)
        app.testing = True
        self.addCleanup(app.extensions['jobs'].shutdown)
        self.addCleanup(app.extensions['automation'].shutdown)
        return app

    def wait_job(self, app, identifier):
        for _ in range(300):
            job = app.extensions['state'].job(identifier, include_result=True)
            if job['status'] not in {'queued', 'running'}:
                return job
            time.sleep(.01)
        self.fail('Discovery refresh did not finish')

    def test_public_articles_keep_theme_date_volume_cover_and_short_introduction(self):
        entries = parse_curated(curated_feed())
        self.assertEqual(len(entries), 6)
        self.assertEqual({entry['article_kind'] for entry in entries}, {'themes', 'completed'})
        self.assertEqual(entries[0]['article_title'], 'ある作品好きに刺さる漫画3選')
        self.assertEqual(entries[0]['published'], '2026-10-14')
        self.assertEqual(entries[0]['author'], '架空作者')
        self.assertEqual(entries[0]['teaser'], '出会いと冒険を描く短い紹介文0')
        self.assertTrue(entries[0]['image'].startswith('https://res.booklive.jp/'))
        self.assertTrue(entries[0]['product_url'].startswith('https://booklive.jp/product/'))
        self.assertEqual(entries[3]['total_volumes'], '１２')
        self.assertEqual(entries[3]['title'], '架空の作品0')

    def test_daily_recommendations_are_stable_unique_and_mix_owned_and_missing(self):
        inventory = self.app.extensions['inventory']
        data = self.app.extensions['feeds'].read('curated')
        first = discovery_view(data, inventory, today=date(2026, 10, 18))
        again = discovery_view(data, inventory, today=date(2026, 10, 18))
        self.assertEqual([item['id'] for item in first['daily']], [item['id'] for item in again['daily']])
        self.assertEqual(len(first['daily']), 5)
        self.assertEqual(first['daily_owned'], 3)
        self.assertEqual(len({item['title'] for item in first['daily']}), 5)
        self.assertEqual(len(first['groups']['themes']), 2)
        self.assertEqual(len(first['groups']['completed']), 2)
        days = {tuple(item['id'] for item in discovery_view(data, inventory, today=date(2026, 10, day))['daily'])
                for day in range(18, 25)}
        self.assertGreater(len(days), 1)
        self.assertTrue(all(not item['read_url'] for item in first['daily']))

    def test_existing_cache_without_article_metadata_and_small_pools_still_render(self):
        inventory = self.app.extensions['inventory']
        original = self.app.extensions['feeds'].read('curated')['entries'][0]
        item = {key: value for key, value in original.items() if key not in {'article_title', 'article_kind', 'published'}}
        data = {'entries': [item]}
        before = deepcopy(data)
        view = discovery_view(data, inventory, today=date(2026, 10, 18))
        self.assertEqual(len(view['daily']), 1)
        self.assertEqual(view['groups']['themes'][0]['title'], '紹介された作品')
        self.assertEqual(data, before)
        self.assertEqual(discovery_view(None, inventory)['daily'], [])

    def test_page_renders_three_shelves_filters_and_links_without_external_calls(self):
        with patch('requests.Session.request', side_effect=AssertionError('Unexpected network')):
            response = self.client.get('/discovery')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for label in ('漫画を発見', '今日のおすすめ', 'テーマから探す', '最近完結した漫画',
                      '記事と蔵書を更新', '作品名・著者で絞り込み', '所持判定を修正'):
            self.assertIn(label, html)
        self.assertIn('data-discovery-filter="owned"', html)
        self.assertIn('data-discovery-filter="missing"', html)
        self.assertNotIn('aria-label="発見の情報源"', html)
        self.assertNotIn('>Komgaで読む</a>', html)
        self.assertEqual(self.app.extensions['state'].jobs(), [])
        for path in ('/discovery?source=manba', '/discovery?source=natalie', '/discovery?source=rakuten'):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get('/discovery?source=forged').status_code, 400)

    def test_owned_komga_link_uses_local_thumbnail_and_external_article_links_are_safe(self):
        inventory = self.app.extensions['inventory']
        cached = deepcopy(inventory.cached())
        row = cached['series'][0]
        row['komga_id'] = 'test-series'
        self.app.extensions['state'].cache('inventory', cached)
        settings = replace(self.settings, demo=False, komga_url='http://komga.example/komga')
        app = self.make_app(settings)
        app.extensions['state'].cache('inventory', cached)
        entries = [{**parse_curated(curated_feed())[0], 'title': row['title'], 'author': row['author']}]
        app.extensions['state'].cache('feed:curated', {'entries': entries})
        with patch('requests.Session.request', side_effect=AssertionError('Unexpected network')):
            html = app.test_client().get('/discovery').get_data(as_text=True)
        self.assertIn('href="http://komga.example/komga/series/test-series"', html)
        self.assertIn('/thumbnail?v=', html)
        self.assertIn('data-cover-fallback="https://res.booklive.jp/', html)
        self.assertIn('rel="noopener noreferrer"', html)
        self.assertIn('紹介記事 ↗', html)
        self.assertIn('作品情報 ↗', html)
        view = discovery_view({'entries': [{**entries[0], 'url': 'javascript:alert(1)',
                                           'product_url': 'https://other.example/book', 'image': 'https://other.example/image'}]}, inventory)
        self.assertEqual((view['entries'][0]['url'], view['entries'][0]['product_url'], view['entries'][0]['image']), ('', '', ''))

    def test_disabled_discovery_update_does_not_refresh_the_inventory(self):
        app = self.make_app(replace(self.settings, demo=False))
        with patch.object(app.extensions['inventory'], 'refresh') as refresh:
            response = app.test_client().post('/api/tools/discovery/refresh', json={})
        self.assertEqual(response.status_code, 400)
        refresh.assert_not_called()
        self.assertEqual(app.extensions['state'].jobs(), [])
        html = app.test_client().get('/discovery').get_data(as_text=True)
        self.assertIn('紹介作品をまだ取得していません', html)
        self.assertIn('発見の情報取得は無効です', html)

    def test_explicit_refresh_updates_inventory_before_articles_and_preserves_previous_on_failure(self):
        settings = replace(self.settings, demo=False, tools=ToolsSettings(discovery_enabled=True))
        app = self.make_app(settings)
        inventory, feeds = app.extensions['inventory'], app.extensions['feeds']
        order = []
        with (patch.object(inventory, 'refresh', side_effect=lambda: order.append('inventory')),
              patch.object(feeds.http, 'fetch', side_effect=lambda *args, **kwargs: (order.append('articles'), curated_feed())[1])):
            response = app.test_client().post('/api/tools/discovery/refresh', json={})
            self.assertEqual(response.status_code, 202)
            job = self.wait_job(app, response.json['id'])
        self.assertEqual(job['status'], 'succeeded', job.get('error'))
        self.assertEqual(order, ['inventory', 'articles'])
        self.assertEqual(job['result']['items'], 6)
        previous = app.extensions['state'].cached('feed:curated')
        with patch.object(inventory, 'refresh'), patch.object(feeds.http, 'fetch', return_value=b'broken feed'):
            response = app.test_client().post('/api/tools/discovery/refresh', json={})
            failed = self.wait_job(app, response.json['id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(app.extensions['state'].cached('feed:curated'), previous)
        self.assertEqual(app.test_client().post('/api/tools/discovery/refresh', json={'force': True}).status_code, 400)
        self.assertEqual(app.test_client().post('/api/tools/discovery/refresh', json={},
                         headers={'Origin': 'https://foreign.example'}).status_code, 403)

    def test_non_demo_refresh_scans_books_matches_renders_covers_and_persists_corrections(self):
        folder = self.settings.library_root / '連載中' / '架空の作品0'
        folder.mkdir(parents=True)
        path = folder / '架空の作品0 第1巻.cbz'
        image = _png()
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('ComicInfo.xml', '<ComicInfo><Series>架空の作品0</Series>'
                             '<Writer>架空作者</Writer><Number>1</Number></ComicInfo>')
            archive.writestr('0001.png', image)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        settings = replace(self.settings, demo=False, tools=ToolsSettings(discovery_enabled=True))
        app = self.make_app(settings)
        client = app.test_client()
        with patch.object(app.extensions['feeds'].http, 'fetch', return_value=curated_feed()):
            response = client.post('/api/tools/discovery/refresh', json={})
            self.assertEqual(response.status_code, 202)
            job = self.wait_job(app, response.json['id'])
        self.assertEqual(job['status'], 'succeeded', job.get('error'))
        self.assertEqual(job['result']['items'], 6)
        self.assertEqual(job['result']['owned'], 2)
        row = app.extensions['inventory'].cached()['series'][0]
        html = client.get('/discovery').get_data(as_text=True)
        self.assertIn('href="/shelf/series/' + row['id'] + '"', html)
        cover = client.get('/api/tools/books/' + row['books'][0]['id'] + '/thumbnail')
        self.assertEqual(cover.status_code, 200)
        # Local first pages are sent as a small JPEG rather than the original image.
        self.assertEqual(cover.mimetype, 'image/jpeg')
        self.assertTrue(cover.data.startswith(b'\xff\xd8'))
        missing = next(item for item in app.extensions['feeds'].read('curated')['entries'] if not item['owned'])
        saved = client.post('/api/tools/matches', json={'source': 'feed:curated',
                            'entry_id': missing['id'], 'series_id': row['id']})
        self.assertEqual(saved.status_code, 200)
        restarted = self.make_app(settings)
        data = restarted.extensions['feeds'].read('curated')
        self.assertTrue(next(item for item in data['entries'] if item['id'] == missing['id'])['owned'])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
