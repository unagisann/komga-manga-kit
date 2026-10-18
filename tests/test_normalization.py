from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from manga_kit import create_app
from manga_kit.config import ConfigError, Settings, load_settings
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

    def check(self):
        response = self.client.post('/api/normalization/checks', json={})
        self.assertEqual(response.status_code, 202, response.json)
        job = self.wait_job(response.json['id'])
        self.assertEqual(job['status'], 'succeeded', job)
        return job

    def action(self, job, action='execute'):
        response = self.client.post(f'/api/normalization/checks/{job["id"]}/{action}', json={'confirm': job['result']['digest']})
        self.assertEqual(response.status_code, 202, response.json)
        return self.wait_job(response.json['id'])

    def test_check_does_not_change_books_and_displays_real_cbz_and_page_changes(self):
        before = self.files()
        job = self.check()
        self.assertEqual(self.files(), before)
        self.assertEqual(job['result']['counts']['added'], 2)
        self.assertEqual(job['result']['counts']['ready'], 2)
        self.assertTrue(all(row['target'].endswith('.cbz') and row['rebuild'] for row in job['result']['items']))
        html = self.client.get('/normalize?check=' + job['id']).get_data(as_text=True)
        self.assertIn('確認した内容でノーマライズを実行', html)
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
