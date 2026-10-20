"""Book moves to the organize-rule location, and settings kept after reading status was removed."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from manga_kit import create_app
from manga_kit.config import Settings, load_settings


def write_book(path, series, writer):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('0001.png', b'page')
        archive.writestr('ComicInfo.xml', f'<ComicInfo><Series>{series}</Series><Writer>{writer}</Writer><Number>1</Number></ComicInfo>')


class MoveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        library = self.root / 'library'
        write_book(library / '連載中' / '[架空作者] 正しい場所' / '正しい場所 第01巻.cbz', '正しい場所', '架空作者')
        write_book(library / '連載中' / '置き場所違い' / '置き場所違い 第01巻.cbz', '置き場所違い', '架空作者')
        env = patch.dict(os.environ, {}, clear=True); env.start(); self.addCleanup(env.stop)
        self.settings = Settings(demo=False, library_root=library, data_root=self.root / 'state',
                                 execution_enabled=True, web_execution_enabled=True)
        self.app = create_app(settings=self.settings); self.app.testing = True
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.client = self.app.test_client()
        self.series = {series.title: series.id for series in self.app.extensions['library'].scan().series}

    def wait(self, response):
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        for _ in range(400):
            job = self.app.extensions['state'].job(response.json['id'], include_result=True)
            if job['status'] not in {'queued', 'running'}:
                return job
            time.sleep(.05)
        self.fail('job did not finish')

    def test_moves_list_series_away_from_their_rule_location_and_plan_only_those(self):
        rows = self.app.extensions['maintenance_ops'].candidates()
        self.assertEqual([(row['title'], row['source'], row['target']) for row in rows],
                         [('置き場所違い', '連載中/置き場所違い', '連載中/[架空作者] 置き場所違い')])
        html = self.client.get('/moves').get_data(as_text=True)
        self.assertIn('連載中/[架空作者] 置き場所違い', html)
        self.assertNotIn('[架空作者] 正しい場所', html)
        self.assertIn('連載中 <span class="count">1</span>', html)
        self.assertNotIn('置き場所違い', self.client.get('/moves?status=completed').get_data(as_text=True).split('<form id="tool-move"')[1])
        self.assertEqual(self.client.get('/moves?status=bad').status_code, 400)
        refused = self.wait(self.client.post('/api/tools/maintenance/move/preview', json={'series_ids': [self.series['正しい場所']]}))
        self.assertEqual(refused['status'], 'failed')
        planned = self.wait(self.client.post('/api/tools/maintenance/move/preview', json={'series_ids': [self.series['置き場所違い']]}))
        self.assertEqual(planned['status'], 'succeeded', planned.get('error'))
        review = self.client.get('/moves?plan=' + planned['result']['plan']['id']).get_data(as_text=True)
        self.assertIn('確認した移動', review)
        self.assertIn('<code>連載中/置き場所違い</code> → <code>連載中/[架空作者] 置き場所違い</code>', review)

    def test_reading_status_is_gone_but_its_old_settings_still_load(self):
        self.assertEqual(self.client.get('/reading').status_code, 404)
        config = self.root / 'config.yaml'
        config.write_text('app:\n  demo: true\ndata:\n  root: ./state\ntools:\n  anime_enabled: true\n'
                          '  reading_accounts:\n    - name: 自分\n      api_key_env: KOMGA_READER_KEY\n', encoding='utf-8')
        self.assertTrue(load_settings(config).tools.anime_enabled)
        store = self.app.extensions['state']
        store.cache('preferences', {'tools': {'discovery_enabled': True, 'reading_accounts': [{'name': '自分', 'api_key_env': 'KOMGA_READER_KEY'}]}})
        other = create_app(settings=self.settings); self.addCleanup(other.extensions['jobs'].shutdown)
        self.assertTrue(other.config['MANGA_KIT'].tools.discovery_enabled)
        self.assertEqual(other.test_client().get('/settings').status_code, 200)
        automation = self.app.extensions['automation']
        for steps, expected in ((['reading', 'inventory'], ['inventory']), (['reading'], ['inventory'])):
            with self.subTest(steps=steps):
                store.cache('automation:config', {'enabled': False, 'time': '04:00', 'steps': steps, 'auto_apply': False})
                self.assertEqual(automation.config()['steps'], expected)


if __name__ == '__main__':
    unittest.main()
