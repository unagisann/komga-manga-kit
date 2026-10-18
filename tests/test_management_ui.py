from pathlib import Path
import tempfile
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
                        '/tsugimanga', '/completion', '/pages', '/manage')
        paths = {path: path for path in destinations}
        paths.update({path: '/manage' for path in ('/normalize', '/metadata', '/organize', '/jobs',
                                                 '/settings', '/roadmap', '/manage/metadata', '/settings/tools',
                                                 '/komga-sync', '/ai', '/smart-shelves', '/automation')})
        paths['/pages/trash'] = '/pages'
        paths['/reading'] = '/'
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

    def test_management_landing_links_to_each_task_and_manual_corrections_are_secondary(self):
        html = self.client.get('/manage').get_data(as_text=True)
        for href in ('/normalize', '/metadata', '/organize', '/jobs', '/settings', '/roadmap', '/manage/metadata'):
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
