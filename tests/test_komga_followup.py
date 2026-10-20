from copy import deepcopy
from dataclasses import replace
import time
from unittest.mock import patch

from manga_kit import create_app
from manga_kit.config import ConfigError
from manga_kit.komga_followup import KomgaFollowups
from manga_kit.models import Series
from test_komga_batch import BatchFixture


class KomgaFollowupTests(BatchFixture):
    def setUp(self):
        super().setUp()
        self.app = create_app(settings=replace(self.settings, web_execution_enabled=True))
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.app.extensions['maintenance_ops'].batch = self.service
        self.web = self.app.test_client()
        self.followups = self.app.extensions['maintenance_ops'].followups
        self.selected = self.series['作品A'].id

    def save(self, fields):
        preview = self.web.post(f'/api/metadata/{self.selected}/preview', json={'fields': fields}).json
        response = self.web.post(f'/api/metadata/{self.selected}', json={
            'fields': fields, 'revision': preview['revision'], 'base_fingerprint': preview['base_fingerprint']})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def wait_job(self, response):
        self.assertEqual(response.status_code, 202, response.json)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = self.app.extensions['state'].job(response.json['id'], include_result=True)
            if job['status'] not in {'queued', 'running'}:
                return job
            time.sleep(.01)
        self.fail('Follow-up task did not finish')

    def test_save_leads_to_scoped_preview_confirmed_write_and_restore(self):
        files, remote = self.files(), deepcopy(self.data)
        saved = self.save({'summary': '保存後にKomgaへ送るあらすじ'})
        identifier = saved['komga_followup']
        self.assertEqual(self.client.patches, [])
        self.assertEqual(self.followups.choice(identifier)['series_ids'], [self.selected])
        self.assertEqual(self.followups.choice(identifier)['fields'], ['summary'])
        self.assertEqual(self.followups.inspect(identifier)['return_url'], f'/metadata/{self.selected}?followup={identifier}')
        with patch('requests.Session.request', side_effect=AssertionError('Unrequested network')):
            html = self.web.get(f'/metadata/{self.selected}?followup={identifier}').get_data(as_text=True)
        self.assertIn('Komgaにも反映しますか？', html)
        self.assertIn('作品情報の差分を確認（1作品）', html)
        self.assertEqual(self.app.extensions['state'].jobs(), [])
        page = self.web.get(f'/komga-sync?followup={identifier}').get_data(as_text=True)
        self.assertIn('この処理の内容をKomgaへ反映', page)
        self.assertNotIn('全作品の差分を確認', page)
        self.assertNotIn('value="publisher"', page)
        self.assertIn('value="summary"', page)
        job = self.wait_job(self.web.post('/api/tools/maintenance/komga/preview', json={
            'followup': identifier, 'mode': 'metadata', 'overwrite': True}))
        self.assertEqual(job['status'], 'succeeded', job)
        operation = job['result']
        self.assertEqual([row['series_id'] for row in operation['plan']['targets']], [self.selected])
        self.assertEqual(operation['plan']['options']['fields'], ['summary'])
        self.assertEqual(self.data, remote)
        self.assertEqual(self.files(), files)
        plan = operation['plan']['id']
        page = self.web.get(f'/komga-sync?followup={identifier}&plan={plan}').get_data(as_text=True)
        self.assertLess(page.index('id="komga-changes"'), page.index('id="tool-komga-sync"'))
        with patch.object(self.app.extensions['inventory'], 'refresh', return_value={}):
            executed = self.wait_job(self.web.post(f'/api/tools/maintenance/{plan}/execute', json={'confirm': operation['digest']}))
            self.assertEqual(executed['status'], 'succeeded', executed)
            self.assertEqual(self.data['series']['series-a']['metadata']['summary'], '保存後にKomgaへ送るあらすじ')
            self.assertEqual(self.data['series']['series-b'], remote['series']['series-b'])
            restored = self.wait_job(self.web.post(f'/api/tools/maintenance/{plan}/restore', json={'confirm': operation['digest']}))
            self.assertEqual(restored['status'], 'succeeded', restored)
        self.assertEqual(self.data, remote)
        self.assertEqual(self.files(), files)

    def test_followup_rejects_wider_targets_fields_and_modes_before_komga_access(self):
        identifier = self.save({'reading': 'あたらしいよみ'})['komga_followup']
        for extra in ({'series_ids': [self.series['作品B'].id]}, {'fields': ['publisher']}, {'mode': 'completion'}):
            with self.subTest(extra=extra):
                job = self.wait_job(self.web.post('/api/tools/maintenance/komga/preview', json={
                    'followup': identifier, 'mode': 'metadata', **extra}))
                self.assertEqual(job['status'], 'failed', job)
        self.assertEqual(self.client.lists, {})
        self.assertEqual(self.client.patches, [])

    def test_source_only_changes_and_disconnected_or_demo_modes_do_not_prompt(self):
        self.assertIsNone(self.followups.create('unchanged', {self.selected: []}, '/metadata'))
        self.assertIsNone(self.save({'summary': self.series['作品A'].summary})['komga_followup'])
        for settings in (replace(self.settings, demo=True), replace(self.settings, komga_url='')):
            disconnected = KomgaFollowups(settings, self.followups.store)
            self.assertIsNone(disconnected.create('no-connection', {self.selected: [{'field': 'reading', 'after': 'よみ'}]}, '/metadata'))

    def test_completed_and_author_changes_keep_their_own_targets(self):
        identifier = self.followups.create('several', {
            self.selected: [{'field': 'summary', 'after': '要約'}, {'field': 'author', 'after': '作者'}],
            self.series['作品C'].id: [{'field': 'status', 'after': 'completed'}],
            self.series['作品B'].id: [{'field': 'status', 'after': 'ongoing'}]}, '/metadata')
        self.assertEqual(self.followups.choice(identifier, 'metadata')['series_ids'], [self.selected])
        self.assertEqual(self.followups.choice(identifier, 'authors')['series_ids'], [self.selected])
        self.assertEqual(self.followups.choice(identifier, 'completion')['series_ids'], [self.series['作品C'].id])
        self.assertEqual(self.web.get('/komga-sync?followup=' + identifier + '&mode=completion').status_code, 200)

    def test_moved_series_uses_only_the_new_ids(self):
        source = Series('連載中/旧作品', '旧作品', status='completed')
        target = Series('完結/新作品', '新作品', reading='しんさくひん', status='completed')
        identifier = self.followups.moved('move:fiction', [source, target], {target.id}, '/moves?plan=fiction')
        self.assertEqual(self.followups.choice(identifier, 'completion')['series_ids'], [target.id])
        self.assertEqual(self.followups.choice(identifier, 'metadata')['series_ids'], [target.id])
        self.assertNotIn(source.id, str(self.followups.inspect(identifier)))

    def test_receipts_survive_restart_and_are_isolated_by_library(self):
        identifier = self.save({'summary': '保存するあらすじ'})['komga_followup']
        same = KomgaFollowups(self.settings, self.app.extensions['state'])
        self.assertEqual(same.inspect(identifier)['id'], identifier)
        other = create_app(settings=replace(self.settings, demo=True))
        self.addCleanup(other.extensions['jobs'].shutdown)
        with self.assertRaises(ConfigError):
            other.extensions['maintenance_ops'].followups.inspect(identifier)

