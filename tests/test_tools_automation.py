from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from manga_kit import create_app
from manga_kit.automation import Automation, DEFAULTS
from manga_kit.catalogs import split_volume
from manga_kit.config import ConfigError, Settings, ToolsSettings
from manga_kit.convert_one import convert_one
from manga_kit.mangaupdates import MangaUpdates
from manga_kit.notifications import Notifications
from manga_kit.sample import create_sample_library
from manga_kit.state import StateConflict


class MoreToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(library_root=self.root / 'library', data_root=self.root / 'state')
        self.app = create_app(settings=self.settings)
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.addCleanup(self.app.extensions['automation'].shutdown)
        self.store = self.app.extensions['state']

    def test_automation_is_disabled_by_default_and_claims_each_day_once_across_instances(self):
        service = self.app.extensions['automation']
        self.assertIsNone(service.due())
        service.save({**DEFAULTS, 'enabled': True, 'time': '03:00'}, 0)
        other = Automation(self.settings, self.store, self.app.extensions['jobs'], self.app.extensions)
        self.addCleanup(other.shutdown)
        now = datetime(2026, 10, 18, 3, 1, tzinfo=ZoneInfo('Asia/Tokyo'))
        self.assertIsNotNone(service.due(now))
        self.assertIsNone(other.due(now))
        self.assertIsNone(service.due(now.replace(hour=2)))
        self.assertIsNotNone(other.due(now.replace(day=19)))

    def test_pipeline_resumes_only_incomplete_steps_and_preserves_errors(self):
        service = self.app.extensions['automation']
        identifier = 'a'*32
        run = {'id': identifier, 'status': 'queued', 'config': {**DEFAULTS, 'steps': ['inventory', 'anime']}, 'steps': {}}
        self.store.cache('automation:run:'+identifier, run)
        def step(key, config):
            if key == 'anime':
                raise ConfigError('取得先が待機中です。')
            return {'series': 1}
        with patch.object(service, 'step', side_effect=step):
            with self.assertRaises(ConfigError):
                service.run(identifier, lambda *_: None)
        failed = self.store.cached('automation:run:'+identifier)['data']
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['steps']['inventory']['status'], 'completed')
        with patch.object(service, 'step', return_value={'items': 1}) as method:
            service.run(identifier, lambda *_: None)
        self.assertEqual(method.call_count, 1)
        self.assertEqual(method.call_args.args[0], 'anime')
        self.assertEqual(self.store.cached('automation:run:'+identifier)['data']['status'], 'completed')

    def test_automatic_apply_requires_explicit_execution_and_settings_validation(self):
        service = self.app.extensions['automation']
        for data in [{**DEFAULTS, 'time': '25:00'}, {**DEFAULTS, 'steps': ['download']}, {**DEFAULTS, 'steps': [{}]}, {**DEFAULTS, 'auto_apply': True}]:
            with self.assertRaises(ConfigError):
                service.save(data, 0)
        with self.assertRaises(ConfigError):
            service.submit(identifier={})

    def test_import_followup_choices_are_checked_remembered_and_carried_over(self):
        service = self.app.extensions['automation']
        options = {option['key']: option for option in service.import_options()}
        self.assertIn('Komga', options['scan']['reason'])
        self.assertEqual((options['bibliography']['reason'], options['classification']['reason']), ('', ''))
        self.assertEqual(service.import_steps(), [])
        # An old "after import" switch carries over the steps that still apply, and leaves the daily settings.
        self.store.cache('automation:config', {'enabled': False, 'time': '04:00', 'steps': ['inventory', 'classification', 'bibliography'],
                                               'post_import': True, 'auto_apply': False})
        self.assertEqual(service.import_steps(), ['bibliography', 'classification'])
        self.assertNotIn('post_import', service.config())
        for steps in ('bibliography', ['download'], ['bibliography', 'bibliography'], [1], ['scan']):
            with self.subTest(steps=steps), self.assertRaises(ConfigError):
                service.choose_import(steps)
        self.assertEqual(service.choose_import(['classification', 'bibliography']), ['bibliography', 'classification'])
        self.assertEqual(service.choose_import([]), [])
        self.assertEqual(service.import_steps(), [])

    def test_steps_after_an_import_cover_only_the_series_it_placed(self):
        service = self.app.extensions['automation']
        series = [row.id for row in self.app.extensions['library'].scan().series]
        placed = series[:1]
        collected = {'counts': {'ready': 0, 'review': 0}, 'digest': 'x'}
        with patch.object(self.app.extensions['auto_enrichment'], 'collect', return_value=collected) as collect:
            service.step('classification', {'auto_apply': False, 'run_id': 'r', 'series': placed})
            self.assertEqual(collect.call_args.args[0], placed)
            service.step('classification', {'auto_apply': False, 'run_id': 'r'})
            self.assertEqual(collect.call_args.args[0], series)
        with patch.object(service, 'step', return_value={'done': True}) as step:
            submitted = service.submit_import(['classification'], placed)
            record = self.store.cached('automation:run:' + submitted['run_id'])['data']
            self.assertEqual((record['config']['steps'], record['series'], record['after_import']), (['classification'], placed, True))
            for _ in range(500):
                if self.store.job(submitted['job_id'])['status'] not in {'queued', 'running'}:
                    break
                time.sleep(0.01)
        self.assertEqual(self.store.job(submitted['job_id'])['status'], 'succeeded')
        self.assertEqual((step.call_args.args[0], step.call_args.args[1]['series']), ('classification', placed))
        self.assertEqual(self.store.cached('automation:run:' + submitted['run_id'])['data']['status'], 'completed')

    def test_failed_remote_operation_blocks_pipeline_retry_until_reviewed(self):
        run_id = 'b'*32
        self.store.cache('automation:operation:'+run_id+':'+('c'*32), {'operation_id': 'c'*32})
        settings = replace(self.settings, demo=False, komga_url='http://isolated.invalid', execution_enabled=True, web_execution_enabled=True)
        service = Automation(settings, self.store, self.app.extensions['jobs'], self.app.extensions)
        with patch('manga_kit.komga_batch.KomgaBatchService') as batch:
            batch.return_value.inspect.return_value = {'status': 'failed'}
            with self.assertRaises(StateConflict):
                service.step('bibliography', {'auto_apply': True, 'run_id': run_id})
            batch.return_value.prepare.assert_not_called()

    def test_notifications_are_opt_in_deduplicated_and_retry_failed_deliveries(self):
        settings = replace(self.settings, demo=False, tools=ToolsSettings(notify_enabled=True))
        rows = [{'series_id': 'series1', 'title': '架空の物語', 'latest': '2', 'local_max': '1', 'new': True}]
        service = Notifications(settings, self.store)
        response = MagicMock(); response.__enter__.return_value = response; response.status_code = 204
        session = MagicMock(); session.__enter__.return_value = session; session.post.return_value = response
        with patch.dict(os.environ, {'MANGA_KIT_NOTIFY_URL': 'https://example.invalid/private-token'}), patch('requests.Session', return_value=session):
            self.assertEqual(service.send(rows)['sent'], 1)
            self.assertEqual(service.send(rows)['sent'], 0)
            self.assertEqual(session.post.call_count, 1)
            rows[0]['latest'] = '3'; response.status_code = 500
            with self.assertRaises(ConfigError) as error:
                service.send(rows)
            self.assertNotIn('private-token', str(error.exception))
            response.status_code = 204
            self.assertEqual(service.send(rows)['sent'], 1)

    def test_portable_single_conversion_requires_preview_digest_preserves_source_and_refuses_overwrite(self):
        create_sample_library(self.settings.library_root, self.settings.layout)
        source = next(self.settings.library_root.rglob('*.cbz'))
        original = source.read_bytes()
        target = self.root / 'converted.cbz'
        preview = convert_one(source, target)
        self.assertFalse(target.exists())
        with self.assertRaises(ConfigError):
            convert_one(source, target, confirm=preview['digest'])
        result = convert_one(source, target, enabled=True, confirm=preview['digest'])
        self.assertTrue(result['source_retained'])
        self.assertEqual(source.read_bytes(), original)
        generated = target.read_bytes()
        with self.assertRaises(ConfigError):
            convert_one(source, target, enabled=True, confirm=preview['digest'])
        self.assertEqual(target.read_bytes(), generated)

    def test_mangaupdates_completion_aliases_and_original_year_are_bibliographic(self):
        candidate = MangaUpdates.parse({'series_id': 1234, 'title': 'Fictitious Work', 'associated': [{'title': '架空の作品'}],
            'url': 'https://www.mangaupdates.com/series/fictitious', 'description': 'An English plot.', 'completed': True,
            'authors': [{'name': '架空作者'}], 'publishers': [{'publisher_name': '架空出版', 'type': 'Original'}],
            'year': '1998', 'bayesian_rating': 8, 'publications': [{'publication_name': '架空雑誌'}]})
        self.assertEqual(candidate['fields']['status'], 'completed')
        self.assertNotIn('summary', candidate['fields'])
        self.assertEqual(candidate['alternate_titles'], ['架空の作品'])
        self.assertEqual(candidate['facts']['original_year'], 1998)
        self.assertEqual(candidate['facts']['rating'], 4)
        self.assertEqual(split_volume('架空の作品（５．５）'), ('架空の作品', '5.5'))

    def test_management_forms_and_pwa_routes_render_without_network(self):
        client = self.app.test_client()
        with patch('requests.Session.request', side_effect=AssertionError('implicit network')):
            for path in ['/ai', '/smart-shelves', '/pages', '/pages/trash', '/moves', '/komga-sync', '/automation', '/manifest.webmanifest', '/sw.js', '/offline']:
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 200)
        self.assertEqual(client.get('/manifest.webmanifest').json['start_url'], '/')
        self.assertNotIn('/api/', client.get('/sw.js').get_data(as_text=True))

    def test_ai_result_validates_model_data_and_saves_only_reviewed_fields(self):
        ai = self.app.extensions['ai_assist']; row = self.app.extensions['library'].scan().series[0]
        self.app.extensions['preferences']._current = replace(self.settings, demo=False, tools=ToolsSettings(ai_enabled=True, ai_model='configured-model', ai_url='https://example.invalid/v1'))
        result = {'title': row.title, 'author': row.author, 'summary': '架空の町で出会った仲間たちが、新しい暮らしを始めます。', 'evidence': {'summary': '第1巻の導入'}}
        with patch.object(ai, 'request', return_value={'choices': [{'message': {'content': json.dumps(result)}}]}) as upstream:
            proposal = ai.generate(row.id, ['summary'])
        prompt = upstream.call_args.args[2]['messages'][0]['content']
        self.assertNotIn(str(self.settings.library_root), prompt)
        self.assertEqual(proposal['fields'], {'summary': result['summary']})
        self.assertEqual(self.app.extensions['metadata'].inspect(row.id)['series']['summary'], row.summary)
        with patch.object(ai, 'request', return_value={'choices': [{'message': {'content': '{"summary": ["bad"]}'}}]}):
            with self.assertRaises(ConfigError):
                ai.generate(row.id, ['summary'])


if __name__ == '__main__':
    unittest.main()
