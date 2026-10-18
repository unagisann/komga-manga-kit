"""Reviewed completion updates preserve KomgaTool's existing metadata rules."""
from copy import deepcopy
from dataclasses import replace
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

from manga_kit import create_app
from manga_kit.cli import main
from manga_kit.config import ConfigError
from manga_kit.komga import KomgaError
from manga_kit.komga_metadata import KomgaMetadataService
from manga_kit.state import StateConflict
from test_komga_metadata import FakeMetadataKomga, KomgaMetadataFixture


class CompletionTests(KomgaMetadataFixture):
    def setUp(self):
        super().setUp()
        self.data['series']['metadata'].update(status='ONGOING', statusLock=False,
                                              tags=['保管用', '連載中'], tagsLock=False)
        self.service.store.save_edit(self.series.id, {'status': 'completed'}, 0, {})

    def completion(self, **options):
        return self.service.prepare_completion(self.series.id, 'remote-series', **options)

    def test_preview_retains_unrelated_tags_and_persists_original_values_without_patch(self):
        files = self.files()
        operation = self.completion()
        plan = operation['plan']
        self.assertEqual(plan['mode'], 'completion')
        self.assertEqual(plan['fields'], ['status', 'tags'])
        self.assertEqual(plan['before'], {'status': 'ONGOING', 'statusLock': False,
                                         'tags': ['保管用', '連載中'], 'tagsLock': False})
        self.assertEqual(plan['after'], {'status': 'ENDED', 'statusLock': True,
                                        'tags': ['保管用', '完結'], 'tagsLock': True})
        self.assertEqual(plan['completion'], {'ongoing_tag': '連載中', 'completed_tag': '完結'})
        self.assertEqual(len(plan['changes']), 2)
        self.assertEqual(self.client.patches, [])
        self.assertEqual(self.files(), files)

    def test_local_status_must_be_explicitly_completed(self):
        for status in ('ongoing', 'unknown'):
            edit = self.service.store.edits()[self.series.id]
            self.service.store.save_edit(self.series.id, {'status': status}, edit['revision'], {})
            with self.subTest(status=status), self.assertRaises(ConfigError):
                self.completion()
        self.assertEqual(self.client.patches, [])

    def test_existing_completed_tag_is_not_duplicated(self):
        self.data['series']['metadata']['tags'] = ['完結', '連載中', 'sf']
        operation = self.completion()
        self.assertEqual(operation['plan']['after']['tags'], ['sf', '完結'])

    def test_hiatus_and_abandoned_status_and_locks_are_preserved(self):
        for status in ('HIATUS', 'ABANDONED', 'ENDED'):
            for locked in (False, True):
                self.data['series']['metadata'].update(status=status, statusLock=locked)
                with self.subTest(status=status, locked=locked):
                    operation = self.completion()
                    self.assertEqual(operation['plan']['after']['status'], status)
                    self.assertEqual(operation['plan']['after']['statusLock'], locked)
                    self.assertEqual([change['field'] for change in operation['plan']['changes']], ['tags'])
                    self.execute(operation)
                    self.restore(operation)
                    self.assertEqual(self.data['series']['metadata']['statusLock'], locked)

    def test_custom_tags_are_reviewed_with_komga_lowercase_normalization(self):
        self.data['series']['metadata']['tags'] = ['in progress', 'sf']
        operation = self.completion(ongoing_tag='IN PROGRESS', completed_tag='DONE')
        self.assertEqual(operation['plan']['after']['tags'], ['done', 'sf'])
        self.execute(operation)
        self.restore(operation)
        self.assertEqual(self.data['series']['metadata']['tags'], ['in progress', 'sf'])

    def test_invalid_or_equivalent_tag_names_are_rejected(self):
        for options in ({'ongoing_tag': ''}, {'completed_tag': ' '}, {'ongoing_tag': None},
                        {'completed_tag': 'x' * 101}, {'completed_tag': 'bad\nname'},
                        {'ongoing_tag': 'same', 'completed_tag': 'SAME'},
                        {'ongoing_tag': 'Straße', 'completed_tag': 'STRASSE'}):
            with self.subTest(options=options), self.assertRaises(ConfigError):
                self.completion(**options)
        self.assertEqual(self.client.patches, [])

    def test_unknown_empty_or_malformed_status_and_locks_are_rejected(self):
        for values in ({'status': ''}, {'status': None}, {'status': 'UNKNOWN'},
                       {'statusLock': 1}, {'tagsLock': None}, {'tags': [None]}):
            original = deepcopy(self.data['series']['metadata'])
            self.data['series']['metadata'].update(values)
            with self.subTest(values=values), self.assertRaises(KomgaError):
                self.completion()
            self.data['series']['metadata'] = original
        self.assertEqual(self.client.patches, [])

    def test_locked_tags_or_ongoing_status_need_explicit_override(self):
        for field in ('tagsLock', 'statusLock'):
            self.data['series']['metadata'][field] = True
            with self.subTest(field=field), self.assertRaises(StateConflict):
                self.completion()
            operation = self.completion(replace_locked=True)
            self.execute(operation)
            self.restore(operation)
            self.assertTrue(self.data['series']['metadata'][field])
            self.data['series']['metadata'][field] = False

    def test_already_synced_status_and_tags_are_rejected_as_no_change(self):
        self.data['series']['metadata'].update(status='ENDED', statusLock=False, tags=['完結'], tagsLock=True)
        with self.assertRaises(ConfigError):
            self.completion()

    def test_only_tag_lock_change_is_reviewed_without_locking_retained_status(self):
        self.data['series']['metadata'].update(status='ENDED', statusLock=False, tags=['完結'], tagsLock=False)
        operation = self.completion()
        self.assertEqual(operation['plan']['changes'], [{'field': 'tags', 'komga_field': 'tags',
            'before': ['完結'], 'after': ['完結'], 'before_lock': False, 'after_lock': True}])
        self.execute(operation)
        self.assertFalse(self.data['series']['metadata']['statusLock'])

    def test_apply_restore_preserve_files_progress_other_metadata_and_local_edits(self):
        files, books = self.files(), deepcopy(self.data['books'])
        metadata, edits = deepcopy(self.data['series']['metadata']), self.service.store.edits()
        operation = self.completion()
        self.execute(operation)
        self.assertEqual(self.data['series']['metadata']['status'], 'ENDED')
        for field in ('title', 'titleSort', 'publisher', 'summary', 'genres', 'language'):
            self.assertEqual(self.data['series']['metadata'][field], metadata[field])
            self.assertEqual(self.data['series']['metadata'][field + 'Lock'], metadata[field + 'Lock'])
        self.assertEqual(self.data['books'], books)
        self.restore(operation)
        self.assertEqual(self.data['series']['metadata'], metadata)
        self.assertEqual(self.service.store.edits(), edits)
        self.assertEqual(self.files(), files)

    def test_later_unrelated_edits_reading_progress_and_local_state_are_preserved(self):
        operation = self.completion()
        self.execute(operation)
        self.data['series']['metadata'].update(summary='後からの編集', summaryLock=True)
        self.data['books'][0]['readProgress']['page'] = 9
        edit = self.service.store.edits()[self.series.id]
        self.service.store.save_edit(self.series.id, {'status': 'ongoing'}, edit['revision'], {})
        self.restore(operation)
        self.assertEqual(self.data['series']['metadata']['summary'], '後からの編集')
        self.assertEqual(self.data['books'][0]['readProgress']['page'], 9)
        self.assertEqual(self.service.store.edits()[self.series.id]['fields']['status'], 'ongoing')

    def test_changed_status_tags_or_locks_before_apply_prevent_patch(self):
        for values in ({'status': 'HIATUS'}, {'statusLock': True}, {'tags': ['別の分類']}, {'tagsLock': True}):
            operation = self.completion()
            original = deepcopy(self.data['series']['metadata'])
            self.data['series']['metadata'].update(values)
            with self.subTest(values=values), self.assertRaises(StateConflict):
                self.execute(operation)
            self.data['series']['metadata'] = original
        self.assertEqual(self.client.patches, [])

    def test_changed_local_completed_state_after_review_prevents_apply(self):
        operation = self.completion()
        edit = self.service.store.edits()[self.series.id]
        self.service.store.save_edit(self.series.id, {'status': 'ongoing'}, edit['revision'], {})
        with self.assertRaises(StateConflict):
            self.execute(operation)
        self.assertEqual(self.client.patches, [])

    def test_changed_status_or_tags_after_apply_prevents_restore(self):
        for values in ({'status': 'ABANDONED'}, {'tags': ['完結', '後から追加']}, {'statusLock': False}):
            operation = self.completion()
            self.execute(operation)
            expected = deepcopy(self.data['series']['metadata'])
            count = len(self.client.patches)
            self.data['series']['metadata'].update(values)
            with self.subTest(values=values), self.assertRaises(StateConflict):
                self.restore(operation)
            self.assertEqual(len(self.client.patches), count)
            self.data['series']['metadata'] = expected
            self.restore(operation)

    def test_wrong_remote_series_and_changed_book_identity_prevent_apply(self):
        with self.assertRaises(StateConflict):
            self.service.prepare_completion(self.series.id, 'wrong-series')
        operation = self.completion()
        self.data['books'][0]['id'] = 'different-book'
        with self.assertRaises(StateConflict):
            self.execute(operation)
        self.assertEqual(self.client.patches, [])

    def test_disabled_demo_unconfigured_execution_and_unsupported_version_never_patch(self):
        for settings in (replace(self.settings, execution_enabled=False), replace(self.settings, demo=True),
                         replace(self.settings, komga_url=''), replace(self.settings, path_mappings=())):
            with self.subTest(settings=settings), self.assertRaises(ConfigError):
                KomgaMetadataService(settings, client=self.client).prepare_completion(self.series.id, 'remote-series')
        self.data['version'] = 'unsupported'
        with self.assertRaises(ConfigError):
            self.completion()
        self.assertEqual(self.client.patches, [])

    def test_lost_response_is_not_resent_and_both_fields_can_be_restored(self):
        operation = self.completion()
        original = deepcopy(self.data['series']['metadata'])
        request = self.client.request
        def lost(method, path, body=None, **params):
            result = request(method, path, body, **params)
            if method == 'PATCH':
                raise KomgaError('simulated response loss')
            return result
        with patch.object(self.client, 'request', side_effect=lost), self.assertRaises(StateConflict):
            self.execute(operation)
        self.assertEqual(len(self.client.patches), 1)
        with self.assertRaises(StateConflict):
            self.execute(operation)
        self.restore(operation)
        self.assertEqual(self.data['series']['metadata'], original)

    def test_journal_failure_does_not_send_patch(self):
        operation = self.completion()
        with patch.object(self.service, '_save', side_effect=OSError('sync failure')), self.assertRaises(OSError):
            self.execute(operation)
        self.assertEqual(self.client.patches, [])

    def test_partial_remote_result_is_not_overwritten_during_restore(self):
        operation = self.completion()
        request = self.client.request
        def partial(method, path, body=None, **params):
            if method == 'PATCH':
                return request(method, path, {'tags': body['tags'], 'tagsLock': body['tagsLock']}, **params)
            return request(method, path, body, **params)
        with patch.object(self.client, 'request', side_effect=partial), self.assertRaises(StateConflict):
            self.execute(operation)
        with self.assertRaises(StateConflict):
            self.restore(operation)
        self.assertEqual(len(self.client.patches), 1)

    def test_previous_checkpoint_can_restore_completion_and_reject_reexecution(self):
        operation = self.completion()
        self.execute(operation)
        (self.service._folder(operation['plan']['id']) / 'manifest.json').unlink()
        with self.assertRaises(StateConflict):
            self.execute(operation)
        self.restore(operation)
        self.assertEqual(self.data['series']['metadata']['status'], 'ONGOING')
        self.assertEqual(self.data['series']['metadata']['tags'], ['保管用', '連載中'])

    def test_history_shows_completion_and_japanese_status_values_without_web_execution(self):
        operation = self.completion(completed_tag='<script>done</script>')
        self.execute(operation)
        app = create_app(settings=self.settings)
        self.addCleanup(app.extensions['jobs'].shutdown)
        client = app.test_client()
        job = self.service.store.jobs()[0]
        page = client.get('/jobs/' + job['id'])
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn('Komga完結情報', html)
        self.assertIn('連載中', html)
        self.assertNotIn('ONGOING', html)
        self.assertIn('&lt;script&gt;done&lt;/script&gt;', html)
        self.assertEqual(client.post('/api/komga/completion', json={}).status_code, 404)
        self.assertEqual(client.post('/api/jobs', json={'kind': 'komga_completion'}).status_code, 400)

    def test_cli_prepares_and_routes_completion_apply_and_restore(self):
        config = self.root / 'config.yaml'
        config.write_text('app:\n  demo: false\nlibrary:\n  root: library\ndata:\n  root: state\nexecution:\n  enabled: true\nkomga:\n  base_url: http://127.0.0.1:25601\n  path_mappings:\n    - komga: /data\n      local: library\n')
        def command(args):
            output = io.StringIO()
            with patch('sys.argv', ['manga-kit', '--config', str(config), *args]), patch('sys.stdout', output), \
                    patch('manga_kit.operations.KomgaClient', return_value=self.client):
                self.assertEqual(main(), 0)
            return json.loads(output.getvalue())
        operation = command(['prepare-completion', '--series', self.series.id, '--komga-series', 'remote-series',
                             '--ongoing-tag', '連載中', '--completed-tag', '読了候補'])
        self.assertEqual(operation['plan']['after']['tags'], ['保管用', '読了候補'])
        self.assertEqual(command(['execute', operation['plan']['id'], '--confirm', operation['digest']])['status'], 'completed')
        self.assertEqual(command(['operation', operation['plan']['id']])['plan']['mode'], 'completion')
        self.assertEqual(command(['restore', operation['plan']['id'], '--confirm', operation['digest']])['status'], 'restored')

    @unittest.skipUnless(Path('/proc/self/fd').is_dir(), 'Linux SIGKILL verification')
    def test_killed_before_after_and_during_restore_preserves_completion_checkpoint(self):
        for boundary in ('before', 'after'):
            operation = self.completion()
            original = deepcopy(self.data['series']['metadata'])
            remote = self.root / 'fake-remote.json'
            remote.write_text(json.dumps(self.data))
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('komga_metadata_worker.py')),
                str(self.root), operation['plan']['id'], operation['digest'], 'execute', boundary],
                capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
            self.data = json.loads(remote.read_text())
            self.client = FakeMetadataKomga(self.data)
            self.service = KomgaMetadataService(self.settings, client=self.client)
            self.restore(operation)
            self.assertEqual(self.data['series']['metadata'], original)
        operation = self.completion()
        self.execute(operation)
        remote.write_text(json.dumps(self.data))
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('komga_metadata_worker.py')),
            str(self.root), operation['plan']['id'], operation['digest'], 'restore', 'after'],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        self.data = json.loads(remote.read_text())
        self.client = FakeMetadataKomga(self.data)
        self.service = KomgaMetadataService(self.settings, client=self.client)
        self.restore(operation)
        self.assertEqual(self.client.patches, [])
        self.assertEqual(self.data['series']['metadata']['status'], 'ONGOING')
        self.assertEqual(self.service.inspect(operation['plan']['id'])['status'], 'restored')


if __name__ == '__main__':
    unittest.main()
