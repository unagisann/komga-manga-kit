from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from manga_kit import create_app
from manga_kit.config import Settings


class ManagementUITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(data_root=Path(temporary.name) / 'state')
        self.app = create_app(settings=self.settings)
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.client = self.app.test_client()

    def test_main_navigation_groups_support_functions_under_management(self):
        destinations = ('/', '/library', '/discovery', '/new_volumes', '/anime',
                        '/tsugimanga', '/moves', '/pages', '/manage')
        paths = {path: path for path in destinations}
        paths.update({path: '/manage' for path in ('/normalize', '/metadata', '/organize', '/jobs',
                                                 '/settings', '/roadmap', '/manage/metadata',
                                                 '/komga-sync', '/ai', '/smart-shelves', '/automation')})
        paths['/pages/trash'] = '/pages'
        for path, selected in paths.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                nav = html.split('aria-label="メインナビゲーション">')[1].split('</nav>')[0]
                for href in destinations:
                    self.assertIn('href="' + href + '"', nav)
                for href in ('/shelf', '/reading', '/normalize', '/metadata', '/organize', '/jobs', '/settings', '/roadmap'):
                    self.assertNotIn('href="' + href + '"', nav)
                self.assertIn('href="' + selected + '" class="active"', nav)
                self.assertEqual(nav.count('aria-current="page"'), 1)
                self.assertNotIn('aria-label="ポータルメニュー"', html)
                self.assertNotIn('aria-label="管理メニュー"', html)

    def test_management_pages_share_tabs_for_maintenance_settings_automation_and_history(self):
        job = self.client.post('/api/jobs', json={'kind': 'scan'}).json
        for path, tab, current in [('/manage', '/manage', 'page'), ('/normalize', '/manage', 'true'), ('/pages/trash', '/manage', 'true'),
                                   ('/manage/metadata', '/manage', 'true'), ('/settings', '/settings', 'page'),
                                   ('/organize', '/settings', 'true'), ('/automation', '/automation', 'page'),
                                   ('/jobs', '/jobs', 'page'), ('/jobs/' + job['id'], '/jobs', 'true')]:
            with self.subTest(path=path):
                html = self.client.get(path).get_data(as_text=True)
                tabs = html.split('aria-label="管理の切り替え">')[1].split('</nav>')[0]
                self.assertEqual(tabs.count('<a '), 4)
                self.assertIn(f'href="{tab}" class="active" aria-current="{current}"', tabs)
                self.assertEqual(tabs.count('class="active"'), 1)
        for path in ('/', '/library', '/discovery', '/roadmap'):
            with self.subTest(path=path):
                self.assertNotIn('管理の切り替え', self.client.get(path).get_data(as_text=True))
        moved = self.client.get('/settings/tools')
        self.assertEqual((moved.status_code, moved.headers['Location']), (302, '/settings#komga'))
        renamed = self.client.get('/completion?plan=abc')
        self.assertEqual((renamed.status_code, renamed.headers['Location']), (302, '/moves?plan=abc'))

    def test_maintenance_tab_lists_only_tasks_and_history_only_records(self):
        html = self.client.get('/manage').get_data(as_text=True)
        body = html.split('aria-label="管理の切り替え">')[1].split('</nav>', 1)[1]
        for href in ('/normalize', '/metadata', '/manage/metadata', '/ai', '/pages', '/moves', '/komga-sync', '/smart-shelves'):
            self.assertIn('href="' + href + '"', body)
        self.assertIn('id="refresh-library"', body)
        for href in ('/settings', '/organize', '/jobs', '/automation'):
            self.assertNotIn('href="' + href + '"', body.split('<footer')[0])
        history = self.client.get('/jobs').get_data(as_text=True)
        self.assertNotIn('data-job-kind', history)
        settings = self.client.get('/settings').get_data(as_text=True)
        for marker in ('id="menu-settings"', 'id="tools-settings"', 'data-credential="KOMGA_API_KEY"', 'id="test-komga"', 'href="/organize"', 'AniList'):
            self.assertIn(marker, settings)

    def cards(self):
        html = self.client.get('/manage').get_data(as_text=True)
        return {part.split('</h3>')[0]: part.split('</h3>')[1].split('<span class="text-link">')[0].split('<button')[0]
                for part in html.split('<h3>')[1:]}

    def test_maintenance_cards_show_saved_progress_without_scanning_on_open(self):
        store, library = self.app.extensions['state'], self.app.extensions['library']
        with (patch.object(library, 'scan', side_effect=AssertionError('scan on open')),
              patch('requests.Session.request', side_effect=AssertionError('network on open'))):
            cards = self.cards()
        self.assertIn('まだ実行していません', cards['追加した漫画を整理'])
        self.assertNotIn('情報が不足', cards['補完・個別確認'])
        self.assertNotIn('management-state', cards['手動修正'])
        self.assertEqual(store.jobs(), [])
        check = store.create_job('normalization_check')
        store.update_job(check, status='succeeded', result={'items': [], 'counts': {'ready': 3, 'blocked': 1, 'unchanged': 5}})
        moved = store.create_job('maintenance_execute'); store.update_job(moved, status='failed', result={'kind': 'move'})
        synced = store.create_job('maintenance_execute'); store.update_job(synced, status='succeeded', result={'kind': 'komga'})
        enrichment = store.create_job('enrichment'); store.update_job(enrichment, status='running')
        store.cache('covers:screening', {'books': {'a': {'bucket': 'mono_color'}, 'b': {'bucket': 'ok'}, 'c': {'bucket': 'broken'}}})
        store.cache('pages:mark:c', {'status': 'done'})
        library.scan()
        cards = self.cards()
        self.assertIn('未実行の差分 4件', cards['追加した漫画を整理'])
        self.assertIn('処理中', cards['補完・個別確認'])
        self.assertIn('要確認 1冊', cards['表紙・ページ修正'])
        self.assertIn('・失敗', cards['書籍移動'])
        self.assertIn('前回', cards['作品情報をまとめて反映'])
        self.assertNotIn('・失敗', cards['作品情報をまとめて反映'])
        self.assertIn('まだ実行していません', cards['スマート本棚の同期'])
        # Once nothing runs, the missing-field count comes from the last scan, as the enrichment list counts it.
        store.update_job(enrichment, status='succeeded', result={})
        self.assertRegex(self.cards()['補完・個別確認'], r'情報が不足 \d+作品')
        time.sleep(0.01)
        store.update_job(store.create_job('normalization_execute'), status='succeeded', result={})
        self.assertNotIn('未実行の差分', self.cards()['追加した漫画を整理'])

    def test_settings_keep_portal_topics_apart_and_fold_what_switches_off(self):
        def section(name):
            html = self.client.get('/settings').get_data(as_text=True)
            return html.split(f'settings-block" id="{name}"')[1].split('</section>')[0]
        for name in ('anime_enabled', 'discovery_enabled', 'rankings_enabled'):
            self.assertIn(f'name="{name}"', section('topics'))
            self.assertNotIn(f'name="{name}"', section('sources'))
        contents = self.client.get('/settings').get_data(as_text=True).split('class="settings-toc"')[1].split('</nav>')[0]
        self.assertLess(contents.index('href="#sources"'), contents.index('href="#topics"'))
        self.assertIn('data-when="rakuten_enabled kobo_enabled" hidden', section('sources'))
        self.assertIn('data-when="ai_enabled" hidden', section('ai'))
        self.assertIn('data-when="notify_enabled" hidden', section('notify'))
        saved = self.client.post('/api/tools/settings', json={'revision': 0, 'tools': {'ai_enabled': True, 'kobo_enabled': True}})
        self.assertEqual(saved.status_code, 200)
        self.assertIn('data-when="rakuten_enabled kobo_enabled">', section('sources'))
        self.assertIn('data-when="ai_enabled">', section('ai'))

    def test_header_and_portal_menus_follow_the_saved_order_and_visibility(self):
        view = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('data-revision="0"', view.split('id="menu-settings"')[1][:40])
        header = [{'id': 'management.index', 'shown': True}, {'id': 'library.index', 'shown': True},
                  {'id': 'jobs.index', 'shown': True}, {'id': 'tools.discovery', 'shown': False}]
        portal = [{'id': 'jobs', 'shown': True}, {'id': 'library', 'shown': True}, {'id': 'komga', 'shown': False}]
        saved = self.client.post('/api/menus', json={'revision': 0, 'header': header, 'portal': portal})
        self.assertEqual(saved.status_code, 200)
        nav = self.client.get('/library').get_data(as_text=True).split('aria-label="メインナビゲーション">')[1].split('</nav>')[0]
        self.assertLess(nav.index('href="/manage"'), nav.index('href="/library"'))
        self.assertLess(nav.index('href="/library"'), nav.index('href="/jobs"'))
        self.assertNotIn('href="/discovery"', nav)
        # Items the user has not arranged keep their default place and visibility.
        self.assertIn('href="/anime"', nav)
        self.assertNotIn('href="/normalize"', nav)
        quick = self.client.get('/').get_data(as_text=True).split('aria-label="主な機能">')[1].split('</nav>')[0]
        self.assertLess(quick.index('href="/jobs"'), quick.index('href="/library"'))
        self.assertNotIn('Komgaとつなぐ', quick)
        self.assertIn('href="/discovery"', quick)
        for body in ({'revision': 1, 'header': [{'id': 'management.index', 'shown': False}], 'portal': []},
                     {'revision': 1, 'header': [{'id': 'missing.index', 'shown': True}], 'portal': []},
                     {'revision': 1, 'header': [{'id': 'library.index', 'shown': True}] * 2, 'portal': []},
                     {'revision': 1, 'header': []}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post('/api/menus', json=body).status_code, 400)
        self.assertEqual(self.client.post('/api/menus', json={'revision': 0, 'header': [], 'portal': []}).status_code, 409)
        self.assertEqual(self.client.post('/api/menus', json={'revision': 1, 'header': [], 'portal': []}).status_code, 200)
        nav = self.client.get('/library').get_data(as_text=True).split('aria-label="メインナビゲーション">')[1].split('</nav>')[0]
        self.assertIn('href="/discovery"', nav)
        self.assertNotIn('href="/jobs"', nav)
        self.assertIn('Komgaとつなぐ', self.client.get('/').get_data(as_text=True))

    def test_management_landing_links_to_each_task_and_manual_corrections_are_secondary(self):
        html = self.client.get('/manage').get_data(as_text=True)
        for href in ('/normalize', '/metadata', '/settings', '/jobs', '/roadmap', '/manage/metadata'):
            self.assertIn('href="' + href + '"', html)
        manual = self.client.get('/manage/metadata?q=月を待つ駅').get_data(as_text=True)
        self.assertIn('月を待つ駅', manual)
        self.assertNotIn('雨あがりの喫茶室', manual)
        series = next(series for series in self.app.extensions['library'].scan().series if series.title == '月を待つ駅')
        detail = self.client.get('/metadata/' + series.id).get_data(as_text=True)
        self.assertIn('href="/manage/metadata"', detail)
        self.assertIn('href="/manage" class="active"', detail)

    def test_opening_pages_never_fetches_or_saves_enrichment(self):
        with (patch('requests.Session.get', side_effect=AssertionError('Unexpected network')),
              patch('requests.Session.post', side_effect=AssertionError('Unexpected network'))):
            for path in ('/metadata', '/metadata?provider=anilist', '/manage', '/manage/metadata', '/settings'):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.app.extensions['state'].edits(), {})
        self.assertEqual(self.app.extensions['state'].jobs(), [])

    def test_auto_enrichment_starts_from_missing_fields_and_not_an_editor(self):
        html = self.client.get('/metadata').get_data(as_text=True)
        self.assertIn('選んだ作品の候補を自動取得', html)
        self.assertIn('不足している情報', html)
        self.assertNotIn('id="metadata-form"', html)
        self.assertNotIn('provider-card', html)
        self.assertNotIn('作品が確認待ち', html)
        for query in ('provider=forged', 'state=bad', 'page=bad'):
            self.assertEqual(self.client.get('/metadata?' + query).status_code, 400)


if __name__ == '__main__':
    unittest.main()
