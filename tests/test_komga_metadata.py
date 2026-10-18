from copy import deepcopy
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from manga_kit import create_app
from manga_kit.cli import main
from manga_kit.config import ConfigError, PathMapping, Settings
from manga_kit.filesystem import LibraryFilesystem
from manga_kit.komga import KomgaError
from manga_kit.komga_metadata import KomgaMetadataService
from manga_kit.metadata import MetadataService
from manga_kit.operations import OperationService
from manga_kit.state import StateConflict


class FakeMetadataKomga:
    def __init__(self, data):
        self.data, self.patches = data, []

    def get(self, path):
        if path == '/actuator/info':
            return {'build': {'version': self.data.get('version', '1.29.0')}}
        if path == '/api/v1/libraries':
            return deepcopy(self.data['libraries'])
        if path == '/api/v1/series/remote-series':
            return deepcopy(self.data['series'])
        raise AssertionError(path)

    def request(self, method, path, body=None, **params):
        if (method, path) == ('POST', '/api/v1/books/list'):
            values = self.data['books'] if body['condition']['deleted']['operator'] == 'isFalse' else []
            return {'content': deepcopy(values), 'last': True}
        if (method, path) == ('PATCH', '/api/v1/series/remote-series/metadata'):
            self.patches.append(deepcopy(body))
            self.data['series']['metadata'].update(deepcopy(body))
            return None
        raise AssertionError((method, path, body))


@unittest.skipUnless(os.name == 'posix', 'Confirmed CLI operations support POSIX')
class KomgaMetadataFixture(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / 'library' / '連載中' / '作品'
        self.folder.mkdir(parents=True)
        for volume in (1, 2):
            with zipfile.ZipFile(self.folder / f'作品 第{volume}巻.cbz', 'w') as archive:
                archive.writestr('ComicInfo.xml', f'<ComicInfo><Series>作品</Series><Number>{volume}</Number></ComicInfo>')
                archive.writestr('page.png', f'fictitious page {volume}')
        (self.folder / 'series.json').write_text(json.dumps({'reading': 'サクヒン', 'publisher': '補完出版',
            'summary': '保存したあらすじ', 'genres': ['Fantasy'], 'tags': ['旅']}))
        self.settings = Settings(demo=False, library_root=self.root / 'library', data_root=self.root / 'state',
            execution_enabled=True, komga_url='http://127.0.0.1:25601',
            path_mappings=(PathMapping('/data', self.root / 'library'),))
        self.data = {'libraries': [{'id': 'lib', 'root': 'file:///data', 'hashFiles': True, 'emptyTrashAfterScan': False,
            'scanOnStartup': False, 'scanInterval': 'DISABLED', 'convertToCbz': False, 'repairExtensions': False}],
            'series': {'id': 'remote-series', 'libraryId': 'lib', 'url': 'file:///data/連載中/作品', 'deleted': False,
                'metadata': {'title': '元の作品', 'titleLock': False, 'titleSort': 'もとのさくひん', 'titleSortLock': False,
                             'summary': '元のあらすじ', 'summaryLock': False, 'publisher': '元の出版', 'publisherLock': False,
                             'genres': ['drama'], 'genresLock': False, 'tags': ['保管用'], 'tagsLock': False,
                             'status': 'HIATUS', 'statusLock': True, 'language': 'ja', 'languageLock': True}}, 'books': []}
        with LibraryFilesystem(self.settings.library_root) as fs:
            for index, book in enumerate(sorted(self.folder.glob('*.cbz'))):
                relative = book.relative_to(self.settings.library_root).as_posix()
                self.data['books'].append({'id': f'book-{index}', 'libraryId': 'lib', 'seriesId': 'remote-series',
                    'url': 'file:///data/' + relative, 'fileHash': fs.snapshot(relative)['komga_hash'],
                    'media': {'status': 'READY'}, 'metadata': {'title': f'元の巻{index}', 'titleLock': True},
                    'readProgress': {'page': index + 1, 'completed': False, 'readDate': '2026-10-15T00:00:00Z'}})
        self.client = FakeMetadataKomga(self.data)
        self.service = KomgaMetadataService(self.settings, client=self.client)
        self.series = self.service.library.scan().series[0]

    def plan(self, fields=None, **options):
        return self.service.prepare(self.series.id, 'remote-series', fields or ['reading', 'publisher', 'genres', 'tags'], **options)

    def execute(self, operation):
        return self.service.execute(operation['plan']['id'], operation['digest'])

    def restore(self, operation):
        return self.service.restore(operation['plan']['id'], operation['digest'])

    def files(self):
        return {path.relative_to(self.settings.library_root).as_posix():
                (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns, path.stat().st_mode)
                for path in self.settings.library_root.rglob('*') if path.is_file()}


class KomgaMetadataTests(KomgaMetadataFixture):
    def test_prepare_contains_reviewable_diff_original_values_and_no_patch(self):
        before = self.files()
        operation = self.plan()
        self.assertEqual(operation['status'], 'planned')
        self.assertEqual(operation['plan']['before']['titleSort'], 'もとのさくひん')
        self.assertEqual(operation['plan']['after']['titleSort'], 'さくひん')
        self.assertEqual(operation['plan']['after']['tags'], ['保管用', '旅'])
        self.assertEqual(len(operation['plan']['changes']), 4)
        self.assertEqual(self.client.patches, [])
        self.assertEqual(self.files(), before)
        journal = self.service._folder(operation['plan']['id']) / 'plan.json'
        self.assertEqual(journal.stat().st_mode & 0o777, 0o600)

    def test_apply_restore_preserve_files_edits_books_progress_and_other_fields(self):
        before, metadata, books = self.files(), deepcopy(self.data['series']['metadata']), deepcopy(self.data['books'])
        self.service.store.save_edit(self.series.id, {'tags': ['旅', '仲間']}, 0, {}, provenance={
            'anilist': {'provider': 'anilist', 'item_id': 'example', 'applied_fields': ['tags']}})
        edits = self.service.store.edits()
        operation = self.plan(['title', 'reading', 'publisher', 'summary', 'genres', 'tags'])
        self.execute(operation)
        self.assertEqual(self.data['series']['metadata']['tags'], ['仲間', '保管用', '旅'])
        self.assertEqual(self.data['series']['metadata']['title'], '作品')
        self.assertTrue(self.data['series']['metadata']['titleLock'])
        self.assertEqual(self.data['series']['metadata']['status'], 'HIATUS')
        self.assertEqual(self.data['books'], books)
        self.restore(operation)
        self.assertEqual(self.data['series']['metadata'], metadata)
        self.assertEqual(self.data['books'], books)
        self.assertEqual(self.service.store.edits(), edits)
        self.assertEqual(self.files(), before)
        self.assertEqual([job['status'] for job in self.service.store.jobs()], ['succeeded', 'succeeded'])

    def test_list_replacement_is_explicit_and_can_clear_a_list(self):
        operation = self.plan(['tags'], replace_lists=True)
        self.assertEqual(operation['plan']['after']['tags'], ['旅'])
        self.execute(operation)
        self.restore(operation)
        info = MetadataService(self.service.library, self.service.store).inspect(self.series.id)
        self.service.store.save_edit(self.series.id, {'tags': []}, info['revision'], {})
        operation = self.plan(['tags'], replace_lists=True)
        self.assertEqual(operation['plan']['after']['tags'], [])
        self.execute(operation)
        self.assertEqual(self.data['series']['metadata']['tags'], [])

    def test_merge_previews_komga_lowercase_normalization_and_preserves_existing_tags(self):
        self.data['series']['metadata']['tags'] = ['travel', '保管用']
        self.service.store.save_edit(self.series.id, {'tags': ['TRAVEL', '旅']}, 0, {})
        operation = self.plan(['genres', 'tags'])
        self.assertEqual(operation['plan']['after']['tags'], ['travel', '保管用', '旅'])
        self.assertEqual(operation['plan']['after']['genres'], ['drama', 'fantasy'])
        self.assertEqual(self.service.store.edits()[self.series.id]['fields']['tags'], ['TRAVEL', '旅'])

    def test_locked_value_requires_explicit_override_and_restore_keeps_original_lock(self):
        self.data['series']['metadata']['publisherLock'] = True
        with self.assertRaises(StateConflict):
            self.plan(['publisher'])
        operation = self.plan(['publisher'], replace_locked=True)
        self.execute(operation)
        self.restore(operation)
        self.assertEqual(self.data['series']['metadata']['publisher'], '元の出版')
        self.assertTrue(self.data['series']['metadata']['publisherLock'])

    def test_no_change_is_rejected_but_lock_only_change_is_reviewed(self):
        self.data['series']['metadata'].update(publisher='補完出版', publisherLock=False)
        operation = self.plan(['publisher'])
        self.assertEqual(operation['plan']['changes'][0]['before'], operation['plan']['changes'][0]['after'])
        self.execute(operation)
        with self.assertRaises(ConfigError):
            self.plan(['publisher'])

    def test_invalid_fields_ids_and_unset_reading_are_rejected(self):
        for fields in ([], ['author'], ['status'], ['publisher', 'publisher'], [None], 'publisher'):
            with self.subTest(fields=fields), self.assertRaises(ConfigError):
                self.service.prepare(self.series.id, 'remote-series', fields)
        with self.assertRaises(ConfigError):
            self.service.prepare(self.series.id, '../outside', ['publisher'])
        with self.assertRaises(ConfigError):
            self.service.prepare('missing', 'remote-series', ['publisher'])
        self.service.store.save_edit(self.series.id, {'reading': ''}, 0, {})
        with self.assertRaises(ConfigError):
            self.plan(['reading'])

    def test_disabled_demo_unconfigured_and_missing_mappings_never_patch(self):
        for settings in (replace(self.settings, execution_enabled=False), replace(self.settings, demo=True),
                         replace(self.settings, komga_url=''), replace(self.settings, path_mappings=())):
            with self.subTest(settings=settings), self.assertRaises(ConfigError):
                KomgaMetadataService(settings, client=self.client).prepare(self.series.id, 'remote-series', ['publisher'])
        self.assertEqual(self.client.patches, [])

    def test_wrong_remote_target_path_deleted_series_and_ids_are_rejected(self):
        with self.assertRaises(StateConflict):
            self.service.prepare(self.series.id, 'wrong-series', ['publisher'])
        original = deepcopy(self.data['series'])
        for values in ({'id': 'wrong'}, {'libraryId': 'other'}, {'url': 'file:///data/other'}, {'deleted': True}):
            self.data['series'].update(values)
            with self.subTest(values=values), self.assertRaises(StateConflict):
                self.plan()
            self.data['series'] = deepcopy(original)
        self.assertEqual(self.client.patches, [])

    def test_version_library_flags_and_hash_changes_are_rejected(self):
        self.data['version'] = 'other'
        with self.assertRaises(ConfigError):
            self.plan()
        self.data['version'] = '1.29.0'
        self.data['libraries'][0]['scanOnStartup'] = True
        with self.assertRaises(ConfigError):
            self.plan()
        self.data['libraries'][0]['scanOnStartup'] = False
        self.data['books'][0]['fileHash'] = 'stale'
        with self.assertRaises(StateConflict):
            self.plan()

    def test_bad_confirmation_and_wrong_operation_engine_never_patch(self):
        operation = self.plan()
        with self.assertRaises(ConfigError):
            self.service.execute(operation['plan']['id'], 'wrong')
        with self.assertRaises(ConfigError):
            OperationService(self.settings, client=self.client).execute(operation['plan']['id'], operation['digest'])
        self.assertEqual(self.client.patches, [])

    def test_local_edit_after_review_prevents_apply(self):
        operation = self.plan()
        self.service.store.save_edit(self.series.id, {'publisher': '別の補完'}, 0, {})
        with self.assertRaises(StateConflict):
            self.execute(operation)
        self.assertEqual(self.client.patches, [])

    def test_local_sidecar_or_archive_change_after_review_prevents_apply(self):
        for path in (self.folder / 'series.json', next(self.folder.glob('*.cbz'))):
            operation = self.plan()
            original = path.read_bytes()
            with path.open('ab') as output:
                output.write(b' ')
            with self.subTest(path=path.name), self.assertRaises((StateConflict, ConfigError)):
                self.execute(operation)
            path.write_bytes(original)
        self.assertEqual(self.client.patches, [])

    def test_remote_value_or_lock_changed_after_review_prevents_apply(self):
        for values in ({'publisher': '別の編集'}, {'publisherLock': True}):
            operation = self.plan(['publisher'])
            original = deepcopy(self.data['series']['metadata'])
            self.data['series']['metadata'].update(values)
            with self.subTest(values=values), self.assertRaises(StateConflict):
                self.execute(operation)
            self.data['series']['metadata'] = original
        self.assertEqual(self.client.patches, [])

    def test_changed_book_id_and_path_mapping_prevent_apply(self):
        operation = self.plan()
        self.data['books'][0]['id'] = 'replacement-book'
        with self.assertRaises(StateConflict):
            self.execute(operation)
        settings = replace(self.settings, path_mappings=(PathMapping('/different', self.settings.library_root),))
        with self.assertRaises(StateConflict):
            KomgaMetadataService(settings, client=self.client).execute(operation['plan']['id'], operation['digest'])
        self.assertEqual(self.client.patches, [])

    def test_unrelated_remote_edits_and_new_reading_progress_are_preserved(self):
        operation = self.plan(['publisher'])
        self.data['series']['metadata'].update(summary='別のあらすじ', summaryLock=True)
        self.data['books'][0]['readProgress']['page'] = 3
        self.execute(operation)
        self.data['series']['metadata']['language'] = 'en'
        self.data['books'][0]['readProgress']['page'] = 4
        self.service.store.save_edit(self.series.id, {'publisher': '新しいローカル編集'}, 0, {})
        self.restore(operation)
        self.assertEqual(self.data['series']['metadata']['summary'], '別のあらすじ')
        self.assertEqual(self.data['series']['metadata']['language'], 'en')
        self.assertEqual(self.data['books'][0]['readProgress']['page'], 4)
        self.assertEqual(self.service.store.edits()[self.series.id]['fields']['publisher'], '新しいローカル編集')

    def test_restore_refuses_overwriting_later_selected_field_changes(self):
        operation = self.plan()
        self.execute(operation)
        count = len(self.client.patches)
        self.data['series']['metadata']['tags'].append('後から追加')
        with self.assertRaises(StateConflict):
            self.restore(operation)
        self.assertEqual(len(self.client.patches), count)
        self.assertIn('後から追加', self.data['series']['metadata']['tags'])

    def test_lost_patch_response_is_not_retried_and_can_be_restored(self):
        operation = self.plan()
        request = self.client.request
        def lost(method, path, body=None, **params):
            result = request(method, path, body, **params)
            if method == 'PATCH':
                raise KomgaError('simulated lost response')
            return result
        with patch.object(self.client, 'request', side_effect=lost), self.assertRaises(StateConflict):
            self.execute(operation)
        self.assertEqual(len(self.client.patches), 1)
        self.restore(operation)
        self.assertEqual(self.data['series']['metadata']['publisher'], '元の出版')

    def test_failed_patch_without_changes_restores_without_sending_another_patch(self):
        operation = self.plan()
        with patch.object(self.service, '_patch', side_effect=KomgaError('No response')), self.assertRaises(StateConflict):
            self.execute(operation)
        self.restore(operation)
        self.assertEqual(self.client.patches, [])

    def test_post_write_verification_detects_unexpected_response(self):
        operation = self.plan(['publisher'])
        request = self.client.request
        def incomplete(method, path, body=None, **params):
            if method == 'PATCH':
                self.client.patches.append(body)
                return None
            return request(method, path, body, **params)
        with patch.object(self.client, 'request', side_effect=incomplete), self.assertRaises(StateConflict):
            self.execute(operation)
        self.assertEqual(self.service.inspect(operation['plan']['id'])['status'], 'failed')
        self.restore(operation)

    def test_journal_sync_failure_prevents_patch(self):
        operation = self.plan()
        with patch.object(self.service, '_save', side_effect=OSError('sync failed')), self.assertRaises(OSError):
            self.execute(operation)
        self.assertEqual(self.client.patches, [])

    def test_missing_current_journal_uses_previous_checkpoint_only_for_restore(self):
        operation = self.plan(['publisher'])
        self.execute(operation)
        (self.service._folder(operation['plan']['id']) / 'manifest.json').unlink()
        self.assertTrue(self.service.inspect(operation['plan']['id'])['_fallback'])
        with self.assertRaises(StateConflict):
            self.execute(operation)
        self.restore(operation)
        self.assertEqual(self.data['series']['metadata']['publisher'], '元の出版')

    def test_previous_planned_checkpoint_can_undo_an_uncertain_patch(self):
        operation = self.plan(['publisher'])
        # The durable running journal is lost after PATCH, leaving the earlier plan.
        self.service._patch(operation['plan'], operation['plan']['after'])
        folder = self.service._folder(operation['plan']['id'])
        (folder / 'manifest.json').rename(folder / 'manifest.previous.json')
        self.assertEqual(self.service.inspect(operation['plan']['id'])['status'], 'planned')
        with self.assertRaises(StateConflict):
            self.execute(operation)
        self.restore(operation)
        self.assertEqual(self.data['series']['metadata']['publisher'], '元の出版')

    def test_edit_lock_and_rescan_limit_are_respected(self):
        with self.service.locked(), self.assertRaises(StateConflict):
            self.plan()
        with patch.object(self.service.library, 'scan', return_value=type('Scan', (), {'truncated': True})()), self.assertRaises(ConfigError):
            self.plan()

    def test_malformed_remote_metadata_is_rejected_without_patch(self):
        for values in ({'publisher': None}, {'publisherLock': 1}, {'tags': ['tag', 2]}, {'titleSort': ''}):
            original = deepcopy(self.data['series']['metadata'])
            self.data['series']['metadata'].update(values)
            with self.subTest(values=values), self.assertRaises(KomgaError):
                self.plan()
            self.data['series']['metadata'] = original
        self.assertEqual(self.client.patches, [])

    def test_history_displays_series_diff_and_web_has_no_execution_endpoint(self):
        operation = self.plan()
        self.execute(operation)
        app = create_app(settings=self.settings)
        self.addCleanup(app.extensions['jobs'].shutdown)
        client = app.test_client()
        job = self.service.store.jobs()[0]
        html = client.get('/jobs/' + job['id'])
        self.assertEqual(html.status_code, 200)
        self.assertIn('Komga', html.get_data(as_text=True))
        self.assertIn('補完出版', html.get_data(as_text=True))
        self.assertEqual(client.post('/api/komga/metadata', json={}).status_code, 404)
        self.assertEqual(client.post('/api/jobs', json={'kind': 'komga_metadata'}).status_code, 400)

    def test_cli_routes_prepare_apply_inspect_and_restore_to_metadata_engine(self):
        config = self.root / 'config.yaml'
        config.write_text('app:\n  demo: false\nlibrary:\n  root: library\ndata:\n  root: state\nexecution:\n  enabled: true\nkomga:\n  base_url: http://127.0.0.1:25601\n  path_mappings:\n    - komga: /data\n      local: library\n')
        def command(args):
            output = io.StringIO()
            with patch('sys.argv', ['manga-kit', '--config', str(config), *args]), patch('sys.stdout', output), \
                    patch('manga_kit.operations.KomgaClient', return_value=self.client):
                self.assertEqual(main(), 0)
            return json.loads(output.getvalue())
        operation = command(['prepare-metadata', '--series', self.series.id, '--komga-series', 'remote-series', '--field', 'publisher'])
        self.assertEqual(command(['execute', operation['plan']['id'], '--confirm', operation['digest']])['status'], 'completed')
        self.assertEqual(command(['operation', operation['plan']['id']])['plan']['kind'], 'komga_metadata')
        self.assertEqual(command(['restore', operation['plan']['id'], '--confirm', operation['digest']])['status'], 'restored')

    @unittest.skipUnless(Path('/proc/self/fd').is_dir(), 'Linux SIGKILL verification')
    def test_killed_before_or_after_patch_restores_from_persisted_journal(self):
        for boundary in ('before', 'after'):
            operation = self.plan(['publisher'])
            remote = self.root / 'fake-remote.json'
            remote.write_text(json.dumps(self.data))
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('komga_metadata_worker.py')),
                str(self.root), operation['plan']['id'], operation['digest'], 'execute', boundary], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
            self.data = json.loads(remote.read_text())
            self.client = FakeMetadataKomga(self.data)
            self.service = KomgaMetadataService(self.settings, client=self.client)
            self.assertEqual(self.service.inspect(operation['plan']['id'])['status'], 'running')
            self.restore(operation)
            self.assertEqual(self.data['series']['metadata']['publisher'], '元の出版')

    @unittest.skipUnless(Path('/proc/self/fd').is_dir(), 'Linux SIGKILL verification')
    def test_restore_killed_after_patch_can_retry_without_overwriting(self):
        operation = self.plan(['publisher'])
        self.execute(operation)
        remote = self.root / 'fake-remote.json'
        remote.write_text(json.dumps(self.data))
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('komga_metadata_worker.py')),
            str(self.root), operation['plan']['id'], operation['digest'], 'restore', 'after'], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        client = FakeMetadataKomga(json.loads(remote.read_text()))
        service = KomgaMetadataService(self.settings, client=client)
        service.restore(operation['plan']['id'], operation['digest'])
        self.assertEqual(client.patches, [])
        self.assertEqual(service.inspect(operation['plan']['id'])['status'], 'restored')


if __name__ == '__main__':
    unittest.main()
