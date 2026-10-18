from dataclasses import asdict, replace
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from manga_kit import create_app
from manga_kit.config import Settings
from manga_kit.models import Book, Series
from manga_kit.organizer import plan_organization
from manga_kit.state import StateStore


class OrganizationUITests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.settings = Settings(data_root=Path(temporary.name) / 'state')
        self.app = create_app(settings=self.settings)
        self.app.testing = True
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.client, self.store = self.app.test_client(), self.app.extensions['state']

    def record(self, *, layout=None):
        items = [{'source': f'現在/架空作品/第{i:02}巻.cbz', 'target': f'整理先/第{i:02}巻.cbz',
                  'title': '架空作品カモメ' if i == 61 else '架空作品', 'state': state,
                  'issues': ['巻数を確認してください。'] if state == 'blocked' else []}
                 for i, state in enumerate(['ready'] * 61 + ['blocked'] * 3 + ['unchanged'] * 2, 1)]
        result = {'items': items, 'counts': {'ready': 61, 'blocked': 3, 'unchanged': 2},
                  'layout': asdict(self.settings.layout.with_overrides(layout or {})),
                  'preview_only': True, 'warnings': [], 'truncated': False}
        job_id = self.store.create_job('preview')
        self.store.update_job(job_id, status='succeeded', result=result)
        return job_id, result

    def wait(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.store.job(job_id)
            if job['status'] not in {'queued', 'running'}:
                self.assertEqual(job['status'], 'succeeded')
                return job
            time.sleep(.01)
        self.fail('preview did not finish')

    def test_opening_organize_does_not_scan_or_plan_and_starts_with_collapsed_rules(self):
        with patch.object(self.app.extensions['library'], 'scan', side_effect=AssertionError('must not scan')), \
                patch('manga_kit.web.plan_organization', side_effect=AssertionError('must not plan')):
            response = self.client.get('/organize')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('配置を確認', html)
        self.assertNotIn('<article class="plan-item', html)
        self.assertIn('<details class="rule-panel layout-details">', html)

    def test_saved_results_prioritize_issues_without_rescanning(self):
        job_id, _ = self.record()
        with patch.object(self.app.extensions['library'], 'scan', side_effect=AssertionError('must not scan')):
            html = self.client.get('/organize').get_data(as_text=True)
        self.assertEqual(html.count('<article class="plan-item blocked"'), 3)
        self.assertNotIn('<article class="plan-item ready"', html)
        self.assertIn('前回の確認:', html)
        self.assertIn(f'preview={job_id}', html)
        self.assertIn('61</strong>', html)

    def test_every_item_is_accessible_in_pages_of_twenty_on_both_screens(self):
        job_id, result = self.record()
        for route in (f'/organize?preview={job_id}&state=all', f'/jobs/{job_id}?state=all'):
            for page, size in ((1, 20), (2, 20), (3, 20), (4, 6)):
                with self.subTest(route=route, page=page):
                    html = self.client.get(route + f'&page={page}').get_data(as_text=True)
                    self.assertEqual(html.count('<article class="plan-item '), size)
                    self.assertNotIn('先頭200件', html)
                    self.assertIn(f'state=all', html)
            html = self.client.get(route + '&page=4').get_data(as_text=True)
            self.assertIn(result['items'][-1]['source'], html)
        self.assertEqual(len(self.store.job(job_id, include_result=True)['result']['items']), 66)

    def test_search_is_normalized_and_covers_items_beyond_first_page(self):
        job_id, _ = self.record()
        for route in (f'/organize?preview={job_id}', f'/jobs/{job_id}'):
            html = self.client.get(route + ('&' if '?' in route else '?') + 'state=all&q=ｶﾓﾒ').get_data(as_text=True)
            self.assertEqual(html.count('<article class="plan-item '), 1)
            self.assertIn('第61巻.cbz', html)
        result = self.client.get(f'/api/jobs/{job_id}/result?state=all&q=ｶﾓﾒ').json
        self.assertEqual((result['total_items'], result['filtered_items'], len(result['items'])), (66, 1, 1))
        result = self.client.get(f'/api/jobs/{job_id}/result?state=blocked&limit=2&offset=1').json
        self.assertEqual((result['filtered_items'], len(result['items'])), (3, 2))
        self.assertEqual(result['counts'], {'ready': 61, 'blocked': 3, 'unchanged': 2})

    def test_result_links_preserve_layout_without_changing_configuration(self):
        job_id, _ = self.record(layout={'completed': '{series}'})
        self.assertNotIn('前回の確認:', self.client.get('/organize').get_data(as_text=True))
        html = self.client.get(f'/organize?preview={job_id}&state=all').get_data(as_text=True)
        self.assertIn('name="completed" value="{series}"', html)
        self.assertIn(f'preview={job_id}', html)
        self.assertEqual(self.app.config['MANGA_KIT'].layout.completed, '{status}/{kana_row}/{series}')

    def test_invalid_result_filters_and_foreign_jobs_are_rejected(self):
        job_id, _ = self.record()
        for route in (f'/organize?preview={job_id}', f'/jobs/{job_id}', f'/api/jobs/{job_id}/result'):
            self.assertEqual(self.client.get(route + '&state=other' if '?' in route else route + '?state=other').status_code, 400)
        for page in ('0', '-1', 'bad'):
            self.assertEqual(self.client.get(f'/organize?preview={job_id}&page={page}').status_code, 400)
        unfinished = self.store.create_job('preview')
        scan = self.store.create_job('scan')
        self.store.update_job(scan, status='succeeded', result={})
        for identifier in ('missing', unfinished, scan):
            self.assertEqual(self.client.get(f'/organize?preview={identifier}').status_code, 404)
        other = StateStore(replace(self.settings, library_root=self.settings.data_root.parent / 'other'))
        other_job = other.create_job('preview')
        self.store.update_job(unfinished, status='interrupted')
        self.assertIsNone(other.latest_preview())
        self.assertEqual(self.client.get(f'/organize?preview={other_job}').status_code, 404)

    def test_pagination_never_hides_collisions_from_global_counts(self):
        # Two different works collide across a display-page boundary.
        first = Series(path='a', title='架空作品', books=[Book(path=f'a/{i}.cbz', volume=str(i)) for i in range(1, 22)])
        second = Series(path='b', title='架空作品', books=[Book(path='b/1.cbz', volume='1')])
        result = plan_organization([first, second], self.settings.layout)
        job_id = self.store.create_job('preview')
        self.store.update_job(job_id, status='succeeded', result={**result, 'layout': asdict(self.settings.layout)})
        html = self.client.get(f'/organize?preview={job_id}').get_data(as_text=True)
        self.assertEqual(result['counts']['blocked'], 2)
        self.assertEqual(html.count('<article class="plan-item blocked"'), 2)
        self.assertIn('複数のファイルが同じ整理先になります。', html)

    def test_saved_preview_survives_restart_and_unsafe_markup_is_escaped(self):
        job_id, result = self.record()
        result['items'][61]['title'] = '<script>alert(1)</script>'
        self.store.update_job(job_id, result=result)
        restarted = create_app(settings=self.settings)
        self.addCleanup(restarted.extensions['jobs'].shutdown)
        html = restarted.test_client().get('/organize').get_data(as_text=True)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)

