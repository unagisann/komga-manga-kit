from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from manga_kit import create_app
from manga_kit.config import ConfigError, PathMapping, Settings, load_settings
from manga_kit.paths import map_komga_path, remote_path
from manga_kit.sample import create_sample_library
from manga_kit.state import StateStore


class StateTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = Settings(demo=False, library_root=self.root / 'library', data_root=self.root / 'state')
        create_sample_library(self.settings.library_root, self.settings.layout)
        self.app = self.new_app()
        self.client = self.app.test_client()
        self.series = self.client.get('/api/library').json['series'][0]
        self.series_id = self.series['id']

    def new_app(self, settings=None):
        app = create_app(settings=settings or self.settings)
        app.testing = True
        self.addCleanup(app.extensions['jobs'].shutdown)
        return app

    def preview(self, fields=None, reset=False):
        payload = {'reset': True} if reset else {'fields': fields}
        result = self.client.post(f'/api/metadata/{self.series_id}/preview', json=payload)
        self.assertEqual(result.status_code, 200)
        return result.json

    def save(self, fields, preview):
        return self.client.post(f'/api/metadata/{self.series_id}', json={
            'fields': fields, 'revision': preview['revision'], 'base_fingerprint': preview['base_fingerprint']})

    def wait_job(self, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.client.get(f'/api/jobs/{job_id}').json
            if job['status'] not in {'queued', 'running'}:
                return job
            time.sleep(0.01)
        self.fail('Job did not finish')

    def hashes(self):
        return {str(p.relative_to(self.settings.library_root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.settings.library_root.rglob('*') if p.is_file()}

    def test_metadata_preview_save_and_restart_leave_manga_unchanged(self):
        before = self.hashes()
        preview = self.preview({'reading': 'ｶﾞｯｺｳ', 'author': '確認済みの著者'})
        self.assertEqual(preview['after_row'], 'か')
        self.assertEqual(self.client.get(f'/api/metadata/{self.series_id}').json['overrides'], {})
        saved = self.save({'reading': 'ｶﾞｯｺｳ', 'author': '確認済みの著者'}, preview)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json['series']['reading'], 'がっこう')
        restored = self.new_app().test_client()
        result = restored.get(f'/api/metadata/{self.series_id}').json
        self.assertEqual(result['series']['author'], '確認済みの著者')
        self.assertEqual(result['revision'], 1)
        self.assertIn('確認済みの著者', restored.get('/library?q=確認済み').get_data(as_text=True))
        plan = restored.post('/api/organize/preview', json={}).json
        for item in plan['items']:
            if item['source'].startswith(self.series['path'] + '/'):
                self.assertIn('確認済みの著者', item['target'])
        self.assertEqual(before, self.hashes())
        self.assertEqual(len(restored.get('/api/history').json['events']), 1)

    def test_simultaneous_edit_rejects_stale_revision_without_overwriting(self):
        first = self.preview({'reading': 'カモメ'})
        stale = self.preview({'reading': 'サクラ'})
        self.assertEqual(self.save({'reading': 'カモメ'}, first).status_code, 200)
        self.assertEqual(self.save({'reading': 'サクラ'}, stale).status_code, 409)
        self.assertEqual(self.client.get(f'/api/metadata/{self.series_id}').json['series']['reading'], 'かもめ')
        self.assertEqual(len(self.client.get('/api/history').json['events']), 1)

    def test_changed_sidecar_requires_new_review(self):
        preview = self.preview({'reading': 'サクラ'})
        sidecar = self.settings.library_root / self.series['path'] / 'series.json'
        data = json.loads(sidecar.read_text())
        data['author'] = '書庫側で編集された著者'
        sidecar.write_text(json.dumps(data, ensure_ascii=False))
        self.assertEqual(self.save({'reading': 'サクラ'}, preview).status_code, 409)
        self.assertEqual(self.client.get(f'/api/metadata/{self.series_id}').json['overrides'], {})

    def test_reset_restores_scanned_metadata_and_preserves_audit(self):
        fields = {'reading': 'サクラ', 'summary': ''}
        self.assertEqual(self.save(fields, self.preview(fields)).status_code, 200)
        preview = self.preview(reset=True)
        result = self.client.post(f'/api/metadata/{self.series_id}', json={
            'reset': True, 'revision': preview['revision'], 'base_fingerprint': preview['base_fingerprint']})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json['overrides'], {})
        self.assertEqual(result.json['series']['reading'], self.series['reading'])
        history = self.client.get('/api/history').json['events']
        self.assertEqual(len(history), 2)
        self.assertTrue(history[0]['detail']['reset'])

    def test_unchanged_form_fields_do_not_mask_future_sidecar_changes(self):
        fields = {key: self.series[key] for key in ('title', 'author', 'reading', 'status', 'summary', 'publisher')}
        fields['reading'] = 'サクラ'
        saved = self.save(fields, self.preview(fields))
        self.assertEqual(set(saved.json['overrides']), {'reading'})

    def test_demo_and_different_library_states_are_isolated(self):
        fields = {'reading': 'サクラ'}
        self.assertEqual(self.save(fields, self.preview(fields)).status_code, 200)
        demo = self.new_app(replace(self.settings, demo=True)).test_client()
        self.assertEqual(demo.get('/api/history').json['events'], [])
        self.assertEqual(demo.get(f'/api/metadata/{self.series_id}').json['overrides'], {})
        other_root = self.root / 'other-library'
        create_sample_library(other_root, self.settings.layout)
        other = self.new_app(replace(self.settings, library_root=other_root)).test_client()
        self.assertEqual(other.get(f'/api/metadata/{self.series_id}').json['overrides'], {})

    def test_metadata_validation_missing_records_and_cross_site(self):
        for fields in ({'id': 'x'}, {'reading': []}, {'title': ''}, {'status': 'bad'},
                       {'reading': 'bad\x00'}, {'summary': 'x' * 8001}, {}, []):
            with self.subTest(fields=fields):
                self.assertEqual(self.client.post(f'/api/metadata/{self.series_id}/preview',
                                                 json={'fields': fields}).status_code, 400)
        self.assertEqual(self.client.get('/api/metadata/missing').status_code, 404)
        self.assertEqual(self.client.post(f'/api/metadata/{self.series_id}', json={'fields': {'reading': 'ア'}}).status_code, 400)
        self.assertEqual(self.client.post(f'/api/metadata/{self.series_id}/preview', json={'reset': 'true'}).status_code, 400)
        self.assertEqual(self.client.post(f'/api/metadata/{self.series_id}/preview', json={'fields': {'reading': 'ア'}},
                                         headers={'Origin': 'https://elsewhere.test'}).status_code, 403)

    def test_preview_job_result_persists_and_is_paginated_without_media_changes(self):
        before = self.hashes()
        response = self.client.post('/api/jobs', json={'kind': 'preview', 'layout': {'completed': '{series}'}})
        self.assertEqual(response.status_code, 202)
        job_id = response.json['id']
        job = self.wait_job(job_id)
        self.assertEqual((job['status'], job['current'], job['total']), ('succeeded', 22, 22))
        restored = self.new_app().test_client()
        result = restored.get(f'/api/jobs/{job_id}/result?limit=2&offset=1').json
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(result['total_items'], 22)
        self.assertTrue(result['preview_only'])
        self.assertEqual(result['layout']['completed'], '{series}')
        self.assertEqual(restored.get('/api/jobs').json['jobs'][0]['id'], job_id)
        self.assertEqual(before, self.hashes())

    def test_job_progress_is_visible_while_worker_is_running(self):
        entered, release = Event(), Event()
        original = self.app.extensions['library'].scan
        def slow_scan(*args, **kwargs):
            kwargs['progress'](3)
            entered.set()
            if not release.wait(5):
                raise RuntimeError('fixture timed out')
            return original(*args, **kwargs)
        with patch.object(self.app.extensions['library'], 'scan', side_effect=slow_scan):
            response = self.client.post('/api/jobs', json={'kind': 'scan'})
            try:
                self.assertTrue(entered.wait(3))
                status = self.client.get('/api/jobs/' + response.json['id']).json
                self.assertEqual((status['status'], status['current']), ('running', 3))
                self.assertEqual(self.client.get('/api/jobs/' + response.json['id'] + '/result').status_code, 409)
            finally:
                release.set()
            self.assertEqual(self.wait_job(response.json['id'])['status'], 'succeeded')

    def test_failed_job_records_safe_error_and_can_be_followed_by_new_job(self):
        with patch.object(self.app.extensions['library'], 'scan', side_effect=OSError('secret-token/private-path')):
            response = self.client.post('/api/jobs', json={'kind': 'scan'})
            result = self.wait_job(response.json['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('secret-token', json.dumps(result))
        next_job = self.client.post('/api/jobs', json={'kind': 'scan'})
        self.assertEqual(self.wait_job(next_job.json['id'])['status'], 'succeeded')

    def test_invalid_jobs_pagination_and_missing_jobs(self):
        for payload in ({'kind': 'move'}, {'kind': []}, {'kind': 'scan', 'layout': []},
                        {'kind': 'preview', 'layout': {'completed': '../{series}'}}, {}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post('/api/jobs', json=payload).status_code, 400)
        for url in ('/api/jobs?limit=0', '/api/jobs?offset=-1', '/api/history?limit=201', '/api/jobs/missing'):
            self.assertIn(self.client.get(url).status_code, {400, 404})

    def test_dead_process_and_reused_pid_jobs_are_marked_interrupted(self):
        store = self.app.extensions['state']
        dead = store.create_job('scan')
        reused = store.create_job('preview')
        alive = store.create_job('scan')
        with store.connection() as conn:
            conn.execute('UPDATE jobs SET owner=? WHERE id=?', (json.dumps({'pid': 2147483647, 'start': None}), dead))
            conn.execute('UPDATE jobs SET owner=? WHERE id=?', (json.dumps({'pid': os.getpid(), 'start': 'prior-process'}), reused))
            conn.execute('UPDATE jobs SET owner=? WHERE id=?', (json.dumps({'pid': os.getpid(), 'start': 'current-process'}), alive))
        with patch('manga_kit.state.process_start', return_value='current-process'):
            self.new_app()
        self.assertEqual(store.job(dead)['status'], 'interrupted')
        self.assertEqual(store.job(reused)['status'], 'interrupted')
        self.assertEqual(store.job(alive)['status'], 'queued')
        store.update_job(alive, status='succeeded', result={})

    def test_new_pages_render_and_unsafe_markup_is_escaped(self):
        fields = {'title': '<script>alert(1)</script>'}
        self.assertEqual(self.save(fields, self.preview(fields)).status_code, 200)
        for url in ('/metadata', f'/metadata/{self.series_id}', '/jobs', '/settings'):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('<script>alert(1)</script>', response.get_data(as_text=True))
        job = self.client.post('/api/jobs', json={'kind': 'preview'}).json
        self.wait_job(job['id'])
        self.assertEqual(self.client.get('/jobs/' + job['id']).status_code, 200)

    def test_non_posix_recovery_never_signals_processes(self):
        store = self.app.extensions['state']
        store.create_job('scan')
        with patch('manga_kit.state.os.name', 'nt'), patch('manga_kit.state.os.kill') as kill:
            store.recover()
        kill.assert_not_called()

    def test_komga_mapping_returns_only_matched_identity_without_remote_paths(self):
        settings = replace(self.settings, path_mappings=(PathMapping('/data/manga', self.settings.library_root),))
        app = self.new_app(settings)
        client = app.test_client()
        with patch.object(app.extensions['komga'], 'series', return_value={'content': [
            {'id': 'matched', 'name': 'Komga作品', 'url': '/data/manga/' + self.series['path']},
            {'id': 'missing', 'name': '別作品', 'url': '/data/manga/absent'},
            {'id': 'unmapped', 'name': '対象外', 'url': '/private/environment/secret'}], 'totalElements': 3}):
            response = client.post('/api/komga/match', json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['state'] for item in response.json['matches']], ['matched', 'missing_local_series', 'unmapped'])
        self.assertEqual(response.json['matches'][0]['local_series_id'], self.series_id)
        self.assertNotIn('/private/environment', response.get_data(as_text=True))
        self.assertEqual(self.client.post('/api/komga/match', json={}).status_code, 400)


class PathAndConfigurationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.library = self.root / 'library'
        self.library.mkdir()

    def test_boundaries_longest_prefix_and_file_url_decode(self):
        mappings = (PathMapping('/data/manga', self.library), PathMapping('/data/manga/special', self.library / '別棚'))
        self.assertEqual(map_komga_path('/data/manga/special/作品', mappings, self.library), '別棚/作品')
        self.assertEqual(map_komga_path('file:///data/manga/%E4%BD%9C%E5%93%81%20A', mappings, self.library), '作品 A')
        self.assertIsNone(map_komga_path('/data/manga-other/作品', mappings, self.library))

    def test_windows_drive_paths_are_component_based_and_case_insensitive(self):
        mappings = (PathMapping(r'D:\Manga', self.library),)
        self.assertEqual(map_komga_path(r'd:\manga\作品\第01巻.cbz', mappings, self.library), '作品/第01巻.cbz')
        self.assertEqual(map_komga_path('file:///D:/Manga/%E4%BD%9C%E5%93%81', mappings, self.library), '作品')
        self.assertIsNone(map_komga_path(r'D:\Manga-other\作品', mappings, self.library))

    def test_traversal_and_symlink_escape_are_rejected(self):
        for path in ('relative/path', '/data/../private', 'file://elsewhere/data', 'file:///data/%2E%2E/private'):
            with self.subTest(path=path), self.assertRaises(ConfigError):
                remote_path(path)
        outside = self.root / 'outside'
        outside.mkdir()
        (self.library / 'link').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ConfigError):
            map_komga_path('/data/link/file', (PathMapping('/data', self.library),), self.library)

    def test_runtime_path_config_environment_and_mapping_validation(self):
        config = self.root / 'config.yaml'
        config.write_text('library:\n  root: ./library\ndata:\n  root: ./state\nkomga:\n  path_mappings:\n    - komga: /data/manga\n      local: ./library\n')
        with patch.dict(os.environ, {}, clear=True):
            settings = load_settings(config)
        self.assertEqual(settings.data_root, self.root / 'state')
        self.assertEqual(settings.path_mappings[0].local, self.library)
        with patch.dict(os.environ, {'MANGA_KIT_DATA_ROOT': str(self.root / 'custom-state')}, clear=True):
            self.assertEqual(load_settings(config).data_root, self.root / 'custom-state')
        for content in ('data:\n  root: ./library/state\nlibrary:\n  root: ./library',
                        'data:\n  root: []', 'komga:\n  path_mappings: false',
                        'komga:\n  path_mappings:\n    - komga: relative\n      local: ./library',
                        'komga:\n  path_mappings:\n    - komga: /data\n      local: ./outside',
                        'komga:\n  path_mappings:\n    - komga: /data\n      local: ./library\n    - komga: /data/\n      local: ./library'):
            config.write_text(content)
            with self.subTest(content=content), patch.dict(os.environ, {}, clear=True), self.assertRaises(ConfigError):
                load_settings(config)

    def test_database_symlink_and_unsupported_schema_are_rejected(self):
        settings = Settings(library_root=self.library, data_root=self.root / 'state')
        settings.data_root.mkdir()
        target = self.root / 'private.db'
        target.write_bytes(b'private')
        database = settings.data_root / 'state.sqlite3'
        database.symlink_to(target)
        with self.assertRaises(ConfigError):
            StateStore(settings).jobs()
        self.assertEqual(target.read_bytes(), b'private')
        database.unlink()
        store = StateStore(settings)
        with store.connection() as conn:
            conn.execute('PRAGMA user_version=99')
        with self.assertRaises(ConfigError):
            StateStore(settings).jobs()


if __name__ == '__main__':
    unittest.main()
