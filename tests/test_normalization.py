from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

from manga_kit import create_app
from manga_kit.config import ConfigError, PathMapping, Settings, load_settings
from manga_kit.normalization_scope import NormalizationTargets
from manga_kit.state import StateConflict
from archive_fixtures import stored_rar

try:
    import libarchive
except (ImportError, OSError):
    libarchive = None


@unittest.skipUnless(os.name == 'posix', 'POSIX filesystem transactions')
class NormalizationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / 'library' / '連載中' / '[架空作者] 架空作品'
        self.folder.mkdir(parents=True)
        self.xml = b'<ComicInfo><Series>fiction</Series><Number>1</Number></ComicInfo>'
        for volume in (1, 2):
            self.archive(self.folder / f'元の名前 第{volume}巻.zip', volume)
        (self.folder / 'series.json').write_text(json.dumps({'title': '架空作品', 'author': '架空作者', 'reading': 'かくうさくひん'}))
        self.settings = Settings(demo=False, library_root=self.root / 'library', data_root=self.root / 'state',
                                 execution_enabled=True, web_execution_enabled=True)
        self.app = create_app(settings=self.settings)
        self.addCleanup(self.app.extensions['jobs'].shutdown)
        self.service = self.app.extensions['normalization']
        self.client, self.store = self.app.test_client(), self.app.extensions['state']

    def archive(self, path, volume=1, *, canonical=False):
        xml = self.xml.replace(b'<Number>1', f'<Number>{volume}'.encode())
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('ComicInfo.xml', xml)
            for number in ((1, 2, 3) if canonical else (2, 10, 1)):
                archive.writestr(f'{number:04}.avif' if canonical else f'page{number}.avif', f'original image {volume} {number}'.encode())

    def files(self):
        return {path.relative_to(self.settings.library_root).as_posix():
                (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns, path.stat().st_mode & 0o777)
                for path in self.settings.library_root.rglob('*') if path.is_file()}

    def wait_job(self, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.store.job(job_id, include_result=True)
            if job['status'] not in {'queued', 'running'}:
                return job
            time.sleep(0.01)
        self.fail('Normalization worker did not finish')

    def check(self, options=None):
        response = self.client.post('/api/normalization/checks', json=options or {})
        self.assertEqual(response.status_code, 202, response.json)
        job = self.wait_job(response.json['id'])
        self.assertEqual(job['status'], 'succeeded', job)
        return job

    def action(self, job, action='execute'):
        response = self.client.post(f'/api/normalization/checks/{job["id"]}/{action}', json={'confirm': job['result']['digest']})
        self.assertEqual(response.status_code, 202, response.json)
        return self.wait_job(response.json['id'])

    def automatic(self, options=None):
        response = self.client.post('/api/normalization/checks', json={**(options or {}), 'auto_execute': True})
        self.assertEqual(response.status_code, 202, response.json)
        action = self.wait_job(response.json['action_id'])
        return self.store.job(response.json['id'], include_result=True), action

    def test_duplicate_automatic_requests_share_check_and_execution_then_restore_originals(self):
        before = self.files()
        entered, release = Event(), Event()
        def blocking(progress):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('fixture timed out')
            return {}
        self.app.extensions['jobs'].submit_task('fixture', blocking)
        self.assertTrue(entered.wait(3))
        try:
            first = self.client.post('/api/normalization/checks', json={'auto_execute': True})
            second = self.client.post('/api/normalization/checks', json={'auto_execute': True})
            self.assertEqual((first.status_code, second.status_code), (202, 202))
            self.assertEqual((first.json['id'], first.json['action_id']), (second.json['id'], second.json['action_id']))
            self.assertEqual(len(self.store.jobs()), 3)
            self.assertEqual(self.files(), before)
        finally:
            release.set()
        action = self.wait_job(first.json['action_id'])
        self.assertEqual(action['status'], 'succeeded', action)
        checked = self.store.job(first.json['id'], include_result=True)
        self.assertEqual(checked['status'], 'succeeded')
        self.assertEqual(self.action(checked, 'restore')['status'], 'succeeded')
        self.assertEqual(self.files(), before)

    def test_duplicate_confirmed_execution_runs_once_while_other_work_waits(self):
        before, checked = self.files(), self.check()
        entered, release = Event(), Event()
        original = self.service.operations.execute
        def executing(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError('fixture timed out')
            return original(*args, **kwargs)
        path = f'/api/normalization/checks/{checked["id"]}/execute'
        body = {'confirm': checked['result']['digest']}
        with patch.object(self.service.operations, 'execute', side_effect=executing) as execution:
            try:
                first = self.client.post(path, json=body)
                self.assertTrue(entered.wait(3))
                second = self.client.post(path, json=body)
                self.assertEqual((first.status_code, second.status_code), (202, 202))
                self.assertEqual(first.json['id'], second.json['id'])
                self.assertEqual(self.files(), before)
            finally:
                release.set()
            self.assertEqual(self.wait_job(first.json['id'])['status'], 'succeeded')
            self.assertEqual(execution.call_count, 1)
        self.assertEqual(self.action(checked, 'restore')['status'], 'succeeded')
        self.assertEqual(self.files(), before)

    def test_target_form_keeps_options_closed_and_automatic_execution_unchecked(self):
        with (patch.object(self.app.extensions['library'], 'scan', side_effect=AssertionError('Implicit scan')),
              patch('requests.Session.request', side_effect=AssertionError('Implicit network'))):
            html = self.client.get('/normalize').get_data(as_text=True)
        self.assertIn('value="local-ongoing"', html)
        self.assertIn('name="subpath"', html)
        self.assertIn('<details class="normalize-options" id="normalization-options">', html)
        self.assertIn('id="normalization-auto">', html)
        self.assertNotIn('id="normalization-auto" checked', html)
        self.assertIn('差分を確認', html)

    def test_scoped_execution_and_restore_leave_other_libraries_unprocessed(self):
        other = self.settings.library_root / '完結' / '[別作者] 別作品'
        other.mkdir(parents=True)
        self.archive(other / '別作品 第1巻.zip')
        (other / 'series.json').write_text(json.dumps({'title': '別作品', 'author': '別作者', 'reading': 'べつさくひん'}))
        original = self.files()
        calls = []
        from manga_kit.normalization import inspect_archive
        def inspected(fs, path, *args, **kwargs):
            calls.append(path)
            return inspect_archive(fs, path, *args, **kwargs)
        with patch('manga_kit.normalization.inspect_archive', side_effect=inspected):
            checked = self.check({'library': 'local-ongoing', 'subpath': self.folder.name})
        self.assertEqual(checked['result']['counts']['scanned'], 2)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(path.startswith('連載中/') for path in calls))
        self.assertEqual(self.action(checked)['status'], 'succeeded')
        self.assertEqual(self.files()['完結/[別作者] 別作品/別作品 第1巻.zip'], original['完結/[別作者] 別作品/別作品 第1巻.zip'])
        pending = self.check({'library': 'local-completed'})
        self.assertEqual(pending['result']['counts']['added'], 1)
        self.assertEqual(self.action(checked, 'restore')['status'], 'succeeded')
        self.assertEqual(self.files(), original)

    def test_scope_does_not_hide_collisions_with_another_series(self):
        duplicate = self.folder.with_name('重複候補')
        duplicate.mkdir()
        self.archive(duplicate / '重複 第1巻.zip')
        (duplicate / 'series.json').write_bytes((self.folder / 'series.json').read_bytes())
        before = self.files()
        checked, automatic = self.automatic({'library': 'local-ongoing', 'subpath': self.folder.name})
        self.assertGreater(checked['result']['counts']['blocked'], 0)
        self.assertTrue(automatic['result']['skipped'])
        self.assertEqual(self.files(), before)

    def test_invalid_targets_limits_and_symlinks_are_rejected_before_queueing(self):
        escape = self.settings.library_root / '連載中' / '外へのリンク'
        escape.symlink_to(self.root)
        invalid = [{'library': 'missing'}, {'library': 'all', 'subpath': '連載中'},
                   *({'library': 'local-ongoing', 'subpath': path} for path in (
                       '../state', '/etc', 'C:/books', 'a/../b', 'a//b', 'a/./b', 'a\\b',
                       '外へのリンク', self.folder.name + '/元の名前 第1巻.zip', 'missing', '\x00')),
                   *({'limit': value} for value in (0, -1, True, 1.5, '2', self.settings.max_files + 1)),
                   {'recheck': 'yes'}, {'auto_execute': 'yes'}]
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.client.post('/api/normalization/checks', json=body).status_code, 400)
        self.assertEqual(self.store.jobs(), [])

    def test_limit_prevents_partial_execution_and_displays_the_required_scope_count(self):
        before = self.files()
        checked, automatic = self.automatic({'limit': 1})
        self.assertEqual(checked['status'], 'succeeded')
        self.assertTrue(checked['result']['limit_reached'])
        self.assertEqual(checked['result']['scope_count'], 2)
        self.assertIsNone(checked['result']['operation_id'])
        self.assertTrue(automatic['result']['skipped'])
        self.assertEqual(self.files(), before)
        html = self.client.get('/normalize?check=' + checked['id'] + '&action=' + automatic['id']).get_data(as_text=True)
        self.assertIn('対象は2冊で、確認上限1冊', html)
        self.assertNotIn('ノーマライズが必要な追加・変更はありません。', html)
        self.assertIn('差分を確認', self.client.get('/jobs/' + automatic['id']).get_data(as_text=True))

    def test_auto_execution_uses_the_checked_plan_and_can_be_restored(self):
        before = self.files()
        checked, automatic = self.automatic({'library': 'local-ongoing', 'subpath': self.folder.name})
        self.assertEqual(checked['status'], 'succeeded')
        self.assertEqual(automatic['status'], 'succeeded', automatic.get('error'))
        self.assertFalse(checked['result']['auto_stop_reason'])
        self.assertEqual(automatic['result']['check_id'], checked['id'])
        self.assertTrue((self.folder / '架空作品 第01巻.cbz').exists())
        self.assertNotEqual(self.files(), before)
        self.assertEqual(self.action(checked, 'restore')['status'], 'succeeded')
        self.assertEqual(self.files(), before)

    def test_auto_execution_stops_for_broken_archives_and_for_no_changes(self):
        path = self.folder / '元の名前 第1巻.zip'
        source = path.read_bytes()
        path.write_bytes(b'broken archive')
        before = self.files()
        checked, automatic = self.automatic()
        self.assertGreater(checked['result']['counts']['blocked'], 0)
        self.assertTrue(automatic['result']['skipped'])
        self.assertEqual(self.files(), before)
        path.write_bytes(source)
        checked, automatic = self.automatic()
        self.assertEqual(automatic['status'], 'succeeded', automatic.get('error'))
        before = self.files()
        checked, automatic = self.automatic()
        self.assertTrue(automatic['result']['skipped'])
        self.assertEqual(checked['result']['counts']['ready'], 0)
        self.assertEqual(self.files(), before)

    def test_auto_execution_requires_enabled_web_execution_and_same_origin(self):
        for config in (replace(self.settings, execution_enabled=False, web_execution_enabled=False),
                       replace(self.settings, web_execution_enabled=False), replace(self.settings, demo=True)):
            app = create_app(settings=config)
            self.addCleanup(app.extensions['jobs'].shutdown)
            self.assertEqual(app.test_client().post('/api/normalization/checks', json={'auto_execute': True}).status_code, 400)
        for kwargs in ({'base_url': 'http://evil.test'}, {'environ_overrides': {'REMOTE_ADDR': '192.0.2.1'}}):
            self.assertEqual(self.client.post('/api/normalization/checks', json={'auto_execute': True}, **kwargs).status_code, 400)
        self.assertEqual(self.client.post('/api/normalization/checks', json={'auto_execute': True},
                         headers={'Origin': 'https://evil.test'}).status_code, 403)
        self.assertEqual(self.store.jobs(), [])

    def test_recheck_reads_verified_archives_without_rebuilding_canonical_books(self):
        checked = self.check()
        self.assertEqual(self.action(checked)['status'], 'succeeded')
        from manga_kit.normalization import inspect_archive
        with patch('manga_kit.normalization.inspect_archive', wraps=inspect_archive) as inspected:
            again = self.check({'recheck': True})
        self.assertEqual(inspected.call_count, 2)
        self.assertEqual(again['result']['counts']['ready'], 0)
        self.assertEqual(again['result']['counts']['unchanged'], 2)

    def test_changed_scope_bindings_after_check_stop_execution(self):
        checked = self.check({'library': 'local-ongoing'})
        self.service.targets.settings = replace(self.settings, path_mappings=(PathMapping('/data', self.settings.library_root),))
        before = self.files()
        self.assertEqual(self.action(checked)['status'], 'failed')
        self.assertEqual(self.files(), before)

    def test_registered_komga_libraries_are_mapped_cached_and_revalidated(self):
        client = Mock()
        settings = replace(self.settings, komga_url='http://komga.test',
                           path_mappings=(PathMapping('/data', self.settings.library_root),))
        targets = NormalizationTargets(settings, self.store, client)
        client.get.return_value = [{'id': 'ongoing', 'name': '登録した漫画', 'root': '/data/連載中'},
                                   {'id': 'outside', 'name': '対象外', 'root': '/database/private'}]
        libraries = targets.refresh()
        self.assertTrue(libraries[0]['available'])
        self.assertFalse(libraries[1]['available'])
        self.assertIsNone(libraries[1]['path'])
        self.assertEqual(targets.resolve()['roots'], ['連載中'])
        selected = targets.resolve({'library': 'komga-ongoing', 'subpath': self.folder.name})
        client.get.assert_called_once_with('/api/v1/libraries')
        self.assertEqual(targets.libraries(), libraries)
        with self.assertRaises(ConfigError):
            targets.resolve({'library': 'komga-outside'})
        client.get.return_value = [{'id': 'ongoing', 'name': '変更後', 'root': '/data'}]
        targets.refresh()
        with self.assertRaises(StateConflict):
            targets.verify(selected)

    def test_check_does_not_change_books_and_displays_real_cbz_and_page_changes(self):
        before = self.files()
        job = self.check()
        self.assertEqual(self.files(), before)
        self.assertEqual(job['result']['counts']['added'], 2)
        self.assertEqual(job['result']['counts']['ready'], 2)
        self.assertTrue(all(row['target'].endswith('.cbz') and row['rebuild'] for row in job['result']['items']))
        html = self.client.get('/normalize?check=' + job['id']).get_data(as_text=True)
        self.assertIn('この内容で実行', html)
        self.assertIn('3ページを辞書順', html)
        self.assertEqual([(page['source'], page['target']) for page in job['result']['items'][0]['page_changes']],
                         [('page1.avif', '0001.avif'), ('page10.avif', '0002.avif'), ('page2.avif', '0003.avif')])

    def test_execute_preserves_images_xml_and_second_check_reports_zero(self):
        job = self.check()
        finished = self.action(job)
        self.assertEqual(finished['status'], 'succeeded', finished)
        target = self.folder / '架空作品 第01巻.cbz'
        self.assertTrue(target.is_file())
        self.assertFalse((self.folder / '元の名前 第1巻.zip').exists())
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(archive.namelist(), ['0001.avif', '0002.avif', '0003.avif', 'ComicInfo.xml'])
            self.assertEqual(archive.read('ComicInfo.xml'), self.xml)
            self.assertEqual([archive.read(f'{i:04}.avif') for i in (1, 2, 3)],
                             [f'original image 1 {i}'.encode() for i in (1, 10, 2)])
        again = self.check()
        self.assertEqual(again['result']['counts']['added'], 0)
        self.assertEqual(again['result']['counts']['modified'], 0)
        self.assertEqual(again['result']['items'], [])

    def test_restore_preserves_original_bytes_times_modes_and_manual_edits(self):
        metadata = self.app.extensions['metadata']
        entry = self.app.extensions['library'].scan().series[0]
        current = metadata.inspect(entry.id)
        metadata.save(entry.id, {'publisher': '架空出版'}, current['revision'], current['base_fingerprint'])
        before = self.files()
        job = self.check()
        self.assertEqual(self.action(job)['status'], 'succeeded')
        restored = self.action(job, 'restore')
        self.assertEqual(restored['status'], 'succeeded', restored)
        self.assertEqual(self.files(), before)
        self.assertEqual(metadata.inspect(entry.id)['series']['publisher'], '架空出版')

    def test_new_book_is_detected_but_existing_books_are_not_rebuilt(self):
        first = self.check()
        self.assertEqual(self.action(first)['status'], 'succeeded')
        old = self.folder / '架空作品 第01巻.cbz'
        before = old.stat(), old.read_bytes()
        self.archive(self.folder / '追加 第3巻.zip', 3)
        added = self.check()
        self.assertEqual(added['result']['counts']['added'], 1)
        changed = [row for row in added['result']['items'] if row['state'] == 'ready']
        self.assertEqual(len(changed), 1)
        self.assertEqual(self.action(added)['status'], 'succeeded')
        self.assertEqual((old.stat().st_ino, old.stat().st_mtime_ns, old.read_bytes()),
                         (before[0].st_ino, before[0].st_mtime_ns, before[1]))

    def test_changed_book_new_book_or_metadata_after_check_prevent_execution(self):
        job = self.check()
        self.archive(self.folder / '追加 第3巻.zip', 3)
        failed = self.action(job)
        self.assertEqual(failed['status'], 'failed')
        self.assertTrue((self.folder / '元の名前 第1巻.zip').exists())
        (self.folder / '追加 第3巻.zip').unlink()
        job = self.check()
        path = self.folder / '元の名前 第1巻.zip'
        path.write_bytes(path.read_bytes() + b'changed')
        self.assertEqual(self.action(job)['status'], 'failed')
        self.assertFalse((self.folder / '架空作品 第01巻.cbz').exists())

    def test_generated_file_changes_prevent_restore_before_any_removal(self):
        job = self.check()
        self.action(job)
        path = self.folder / '架空作品 第02巻.cbz'
        path.write_bytes(path.read_bytes() + b'changed after execution')
        before = self.files()
        restored = self.action(job, 'restore')
        self.assertEqual(restored['status'], 'failed')
        self.assertEqual(self.files(), before)

    def test_failure_after_first_publication_can_restore_all_books(self):
        before = self.files()
        job = self.check()
        original = self.service.operations._copy_backup
        calls = []
        def publish(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise OSError('simulated publication failure')
            return original(*args, **kwargs)
        with patch.object(self.service.operations, '_copy_backup', side_effect=publish):
            self.assertEqual(self.action(job)['status'], 'failed')
        self.assertEqual(self.action(job, 'restore')['status'], 'succeeded')
        self.assertEqual(self.files(), before)

    def test_in_place_cbz_rebuild_and_restore(self):
        for path in self.folder.glob('*.zip'):
            path.unlink()
        self.archive(self.folder / '架空作品 第01巻.cbz')
        before = self.files()
        job = self.check()
        self.assertEqual(job['result']['counts']['ready'], 1)
        self.assertEqual(self.action(job)['status'], 'succeeded')
        self.assertEqual(self.action(job, 'restore')['status'], 'succeeded')
        self.assertEqual(self.files(), before)

    def test_sigkill_after_original_removal_recovers_from_durable_backup(self):
        for path in self.folder.glob('*.zip'):
            path.unlink()
        self.archive(self.folder / '架空作品 第01巻.cbz')
        before = self.files()
        job = self.check()
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('normalization_worker.py')),
                                 str(self.root), job['result']['operation_id'], job['result']['digest']],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        self.assertEqual(self.action(job, 'restore')['status'], 'succeeded')
        self.assertEqual(self.files(), before)

    def test_collision_unknown_volume_and_outside_series_are_not_executed(self):
        self.archive(self.folder / '重複 第1巻.zip', 1)
        job = self.check()
        self.assertGreater(job['result']['counts']['blocked'], 0)
        self.assertIsNone(job['result']['operation_id'])
        (self.folder / '重複 第1巻.zip').unlink()
        with zipfile.ZipFile(self.folder / '巻数不明.zip', 'w') as archive:
            archive.writestr('0001.png', b'unknown volume')
        job = self.check()
        self.assertIsNone(job['result']['operation_id'])
        self.archive(self.settings.library_root / 'outside 第3巻.zip', 3)
        job = self.check()
        self.assertTrue(any('作品フォルダの外' in issue for row in job['result']['items'] for issue in row['issues']))

    def test_komga_content_rebuild_requires_verified_version_without_source_changes(self):
        original = self.files()
        guarded = create_app(settings=replace(self.settings, komga_url='http://127.0.0.1:25699'))
        self.addCleanup(guarded.extensions['jobs'].shutdown)
        client = guarded.extensions['normalization'].operations.guard.client
        with patch.object(client, 'get', return_value={'build': {'version': 'untested'}}):
            result = guarded.extensions['normalization'].check(lambda *_: None)
        self.assertIsNone(result['operation_id'])
        self.assertIn('1.29.1', result['preparation_error'])
        self.assertEqual(self.files(), original)

    def test_default_preview_disabled_and_wrong_confirmation_or_remote_hosts_are_rejected(self):
        job = self.check()
        disabled = create_app(settings=replace(self.settings, web_execution_enabled=False))
        self.addCleanup(disabled.extensions['jobs'].shutdown)
        url = f'/api/normalization/checks/{job["id"]}/execute'
        body = {'confirm': job['result']['digest']}
        self.assertEqual(disabled.test_client().post(url, json=body).status_code, 400)
        self.assertEqual(self.client.post(url, json={'confirm': 'wrong'}).status_code, 409)
        self.assertEqual(self.client.post(url, json=body, base_url='http://evil.test').status_code, 400)
        self.assertEqual(self.client.post(url, json=body, headers={'Origin': 'https://evil.test'}).status_code, 403)
        self.assertEqual(self.client.post(url, json=body, environ_overrides={'REMOTE_ADDR': '192.0.2.1'}).status_code, 400)

    def test_page_open_does_not_scan_and_completed_check_persists_after_restart(self):
        with patch.object(self.app.extensions['library'], 'scan', side_effect=AssertionError('Implicit scan')):
            self.assertEqual(self.client.get('/').status_code, 200)
            self.assertEqual(self.client.get('/normalize').status_code, 200)
        job = self.check()
        restarted = create_app(settings=self.settings)
        self.addCleanup(restarted.extensions['jobs'].shutdown)
        self.assertIn('元の名前', restarted.test_client().get('/normalize').get_data(as_text=True))
        self.assertEqual(self.client.get('/normalize?check=missing').status_code, 404)

    def test_web_execution_configuration_is_explicit(self):
        path = self.root / 'config.yaml'
        path.write_text('execution:\n  enabled: true\n  web_enabled: true\n')
        self.assertTrue(load_settings(path).web_execution_enabled)
        path.write_text('execution:\n  enabled: false\n  web_enabled: true\n')
        with self.assertRaises(ConfigError):
            load_settings(path)

    @unittest.skipUnless(libarchive, 'RAR/7z need libarchive')
    def test_rar4_rar5_and_7z_normalize_and_restore_original_archives(self):
        for path in self.folder.glob('*.zip'):
            path.unlink()
        for volume, suffix in ((1, '.rar'), (2, '.cbr'), (3, '.7z')):
            path = self.folder / f'追加 第{volume}巻{suffix}'
            members = [('page2.png', b'second'), ('page1.png', b'first')]
            if suffix == '.7z':
                with libarchive.file_writer(str(path), '7zip') as archive:
                    for name, value in members:
                        archive.add_file_from_memory(name, len(value), value, permission=0o644)
            else:
                stored_rar(path, members, version=4 if volume == 1 else 5)
        before = self.files()
        checked = self.check()
        self.assertEqual(checked['result']['counts']['ready'], 3)
        self.assertEqual(self.action(checked)['status'], 'succeeded')
        for volume in (1, 2, 3):
            with zipfile.ZipFile(self.folder / f'架空作品 第{volume:02}巻.cbz') as archive:
                self.assertEqual([archive.read(name) for name in archive.namelist()], [b'first', b'second'])
        self.assertEqual(self.action(checked, 'restore')['status'], 'succeeded')
        self.assertEqual(self.files(), before)

    def test_verified_unchanged_archives_are_not_reopened_by_the_next_check(self):
        checked = self.check()
        self.assertEqual(self.action(checked)['status'], 'succeeded')
        with patch('manga_kit.normalization.inspect_archive', side_effect=AssertionError('Unnecessary archive read')):
            again = self.check()
        self.assertEqual(again['result']['items'], [])

    def test_folder_only_metadata_is_remembered_after_naming_without_false_changes(self):
        for path in self.folder.iterdir():
            path.unlink()
        renamed = self.folder.with_name('架空作品')
        self.folder.rename(renamed)
        self.folder = renamed
        for volume in (1, 2):
            with zipfile.ZipFile(self.folder / f'追加 第{volume}巻.zip', 'w') as archive:
                archive.writestr('0001.avif', f'original page {volume}'.encode())
        before = self.files()
        checked = self.check()
        self.assertEqual(self.action(checked)['status'], 'succeeded')
        self.assertTrue((self.folder.with_name('[著者未設定] 架空作品') / '架空作品 第01巻.cbz').is_file())
        again = self.check()
        self.assertEqual(again['result']['counts']['added'], 0)
        self.assertEqual(again['result']['counts']['modified'], 0)
        self.assertEqual(again['result']['items'], [])
        self.assertEqual(self.action(checked, 'restore')['status'], 'succeeded')
        self.assertEqual(self.files(), before)

    def test_insufficient_library_space_leaves_every_source_in_place(self):
        checked = self.check()
        before = self.files()
        real_usage = __import__('shutil').disk_usage
        def disk_usage(path):
            usage = real_usage(path)
            return usage._replace(free=0) if Path(path) == self.settings.library_root else usage
        with patch('manga_kit.normalization.shutil.disk_usage', side_effect=disk_usage):
            result = self.action(checked)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('空き容量', result['error'])
        self.assertEqual(self.files(), before)

    def test_all_diff_pages_and_recovery_history_remain_accessible(self):
        for volume in range(3, 24):
            self.archive(self.folder / f'追加 第{volume}巻.zip', volume)
        checked = self.check()
        first = self.client.get('/normalize?check=' + checked['id']).get_data(as_text=True)
        second = self.client.get('/normalize?check=' + checked['id'] + '&page=2').get_data(as_text=True)
        self.assertEqual(first.count('class="plan-item ready"'), 20)
        self.assertEqual(second.count('class="plan-item ready"'), 3)
        self.assertIn('全23冊の変更前後', first)
        self.assertIn('check=' + checked['id'], second)
        history = self.client.get('/jobs/' + checked['id']).get_data(as_text=True)
        self.assertIn('/normalize?check=' + checked['id'], history)


if __name__ == '__main__':
    unittest.main()
