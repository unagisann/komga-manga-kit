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
from manga_kit.authors import author_candidates, author_values
from manga_kit.cli import main
from manga_kit.config import ConfigError
from manga_kit.komga import KomgaError
from manga_kit.komga_authors import KomgaAuthorsService
from manga_kit.operations import OperationService
from manga_kit.state import StateConflict
from test_komga_metadata import FakeMetadataKomga, KomgaMetadataFixture


class FakeAuthorsKomga(FakeMetadataKomga):
    def get(self, path):
        if path.startswith('/api/v1/books/'):
            return deepcopy(next(book for book in self.data['books'] if path.endswith('/' + book['id'])))
        return super().get(path)

    def request(self, method, path, body=None, **params):
        if method == 'PATCH' and path.startswith('/api/v1/books/'):
            book = next(book for book in self.data['books'] if path == '/api/v1/books/' + book['id'] + '/metadata')
            self.patches.append({'id': book['id'], 'values': deepcopy(body)})
            book['metadata'].update(deepcopy(body))
            return None
        return super().request(method, path, body, **params)


class AuthorValueTests(unittest.TestCase):
    def test_candidate_roles_names_middle_dots_spaces_and_latin_x(self):
        self.assertEqual(author_candidates('原作: 架空一郎 × 作画: 架空・二郎 / Alex Smith、他'), [
            {'name':'架空一郎','role':'writer'}, {'name':'架空・二郎','role':'penciller'},
            {'name':'Alex Smith','role':'writer'}])
        self.assertEqual(author_candidates('漫画太郎'), [{'name':'漫画太郎','role':'writer'}])
        self.assertEqual(author_candidates('架空（所属） & 架空'), [{'name':'架空','role':'writer'}])

    def test_x_joins_japanese_names_but_stays_inside_latin_names(self):
        def names(text):
            return [item['name'] for item in author_candidates(text)]
        # Full-width x is the usual separator in [著者]作品名 folders.
        self.assertEqual(names('架空一郎ｘ架空二郎'), ['架空一郎', '架空二郎'])
        self.assertEqual(names('架空一郎Ｘ架空二郎ｘ架空三郎'), ['架空一郎', '架空二郎', '架空三郎'])
        self.assertEqual(names('ＯＮＥｘ架空二郎'), ['ONE', '架空二郎'])
        self.assertEqual(names('KAKUｘ架空'), ['KAKU', '架空'])
        self.assertEqual(names('架空ｘKAKU'), ['架空', 'KAKU'])
        self.assertEqual(names('架空一郎x架空二郎'), ['架空一郎', '架空二郎'])
        self.assertEqual(names('架空一郎 x 架空二郎'), ['架空一郎', '架空二郎'])
        # Latin letters, including full-width words, are never split.
        for text in ('Alex Smith', 'Max', 'ＡｌｅｘＳｍｉｔｈ', 'Alex架空', 'Xavier'):
            with self.subTest(text=text):
                self.assertEqual(len(names(text)), 1)
        self.assertEqual(names('ｘ架空'), ['架空'])

    def test_space_before_explicit_role_label_starts_a_new_name(self):
        self.assertEqual(author_candidates('原作:架空一郎 漫画:架空二郎'), [
            {'name': '架空一郎', 'role': 'writer'}, {'name': '架空二郎', 'role': 'penciller'}])
        self.assertEqual(author_candidates('架空 画家'), [{'name': '架空 画家', 'role': 'writer'}])
        self.assertEqual(author_candidates('漫画太郎'), [{'name': '漫画太郎', 'role': 'writer'}])

    def test_fullwidth_latin_x_is_kept_at_word_edges_and_next_to_spaces(self):
        cases = {'Ａｌｅｘ': 'Alex', 'Ｍａｘ': 'Max', 'Ｘａｖｉｅｒ': 'Xavier',
                 'Ａｌｅｘ Ｓｍｉｔｈ': 'Alex Smith', 'Ｍａｘ、他': 'Max',
                 'Maｘ': 'Max', 'Ｘavier': 'Xavier', 'ＡｌｅｘSmith': 'AlexSmith',
                 'ＯＮＥｘＫＡＫＵ': 'ONExKAKU'}
        for text, name in cases.items():
            with self.subTest(text=text):
                self.assertEqual(author_candidates(text), [{'name': name, 'role': 'writer'}])
        self.assertEqual(author_candidates('Ａｌｅｘ ｘ Ｍａｘ'),
                         [{'name': 'Alex', 'role': 'writer'}, {'name': 'Max', 'role': 'writer'}])

    def test_explicit_names_keep_punctuation_and_role_pairs(self):
        self.assertEqual(author_values([{'name':' A・B (Pen) ', 'role':'WRITER'},
                                       {'name':' A・B (Pen) ', 'role':'writer'},
                                       {'name':'A・B (Pen)', 'role':'penciller'}]), [
            {'name':'A・B (Pen)','role':'writer'}, {'name':'A・B (Pen)','role':'penciller'}])

    def test_placeholders_invalid_shapes_limits_and_control_characters(self):
        for values in (None, {}, [{'name':'他','role':'writer'}], [{'name':'unknown','role':'writer'}],
                       [{'name':'a','role':''}], [{'name':'a\nb','role':'writer'}],
                       [{'name':'a','role':'writer','extra':1}], [{'name':'a'*301,'role':'writer'}],
                       [{'name':'a','role':'writer'}]*101):
            with self.subTest(values=str(values)[:60]), self.assertRaises(ConfigError):
                author_values(values)


class AuthorsTests(KomgaMetadataFixture):
    def setUp(self):
        super().setUp()
        for book in self.data['books']:
            book['deleted'] = False
            book['metadata'].update(authors=[{'name':'元の校正者','role':'editor'}], authorsLock=False)
        self.client = FakeAuthorsKomga(self.data)
        self.service = KomgaAuthorsService(self.settings, client=self.client)
        self.authors = [{'name':'架空原作','role':'writer'}, {'name':'架空作画','role':'penciller'}]

    def plan(self, authors=None, **options):
        return self.service.prepare(self.series.id, 'remote-series', self.authors if authors is None else authors, **options)

    def values(self):
        return {book['id']:deepcopy(book['metadata']) for book in self.data['books']}

    def test_prepare_is_read_only_and_shows_each_book_original_values_locks_and_ids(self):
        files, values = self.files(), self.values()
        operation = self.plan()
        self.assertEqual(operation['plan']['kind'], 'komga_authors')
        self.assertEqual(len(operation['plan']['books']), 2)
        for book in operation['plan']['books']:
            self.assertEqual(book['before']['authors'], [{'name':'元の校正者','role':'editor'}])
            self.assertEqual(book['after']['authors'][1:], self.authors)
            self.assertEqual(book['id'], operation['plan']['komga']['books'][book['path']]['id'])
        self.assertEqual(self.client.patches, [])
        self.assertEqual(self.files(), files)
        self.assertEqual(self.values(), values)

    def test_apply_restore_preserve_files_edits_progress_and_unselected_metadata(self):
        before, files = self.values(), self.files()
        edit = self.service.store.edits()
        operation = self.plan()
        self.execute(operation)
        self.assertEqual(len(self.client.patches), 2)
        self.assertEqual(self.data['books'][0]['metadata']['authors'][1:], self.authors)
        self.data['books'][0]['readProgress']['page'] = 9
        self.data['books'][0]['metadata']['summary'] = '後からの別項目'
        self.restore(operation)
        for book in self.data['books']:
            self.assertEqual(book['metadata']['authors'], before[book['id']]['authors'])
            self.assertFalse(book['metadata']['authorsLock'])
        self.assertEqual(self.data['books'][0]['readProgress']['page'], 9)
        self.assertEqual(self.data['books'][0]['metadata']['summary'], '後からの別項目')
        self.assertEqual(self.files(), files)
        self.assertEqual(self.service.store.edits(), edit)

    def test_selected_book_only_and_explicit_replacement_or_clearing(self):
        selected = [self.data['books'][1]['url'].removeprefix('file:///data/')]
        operation = self.plan(selected=selected, replace_lists=True)
        self.execute(operation)
        self.assertEqual([item['id'] for item in self.client.patches], ['book-1'])
        self.assertEqual(self.data['books'][0]['metadata']['authors'], [{'name':'元の校正者','role':'editor'}])
        self.assertEqual(self.data['books'][1]['metadata']['authors'], self.authors)
        self.restore(operation)
        with self.assertRaises(ConfigError):
            self.plan([])
        operation = self.plan([], replace_lists=True)
        self.execute(operation)
        self.assertEqual(self.data['books'][0]['metadata']['authors'], [])
        self.restore(operation)

    def test_existing_duplicates_and_role_order_are_kept_in_merge_and_restore(self):
        original = [{'name':'旧著者','role':'writer'}]*2 + [{'name':'架空原作','role':'writer'}]
        self.data['books'][0]['metadata']['authors'] = deepcopy(original)
        operation = self.plan()
        self.execute(operation)
        self.assertEqual(self.data['books'][0]['metadata']['authors'], original + [self.authors[1]])
        self.restore(operation)
        self.assertEqual(self.data['books'][0]['metadata']['authors'], original)

    def test_locked_changes_require_override_and_restore_original_lock(self):
        self.data['books'][1]['metadata']['authorsLock'] = True
        with self.assertRaises(StateConflict):
            self.plan()
        self.assertEqual(self.client.patches, [])
        operation = self.plan(replace_locked=True)
        self.execute(operation)
        self.restore(operation)
        self.assertTrue(self.data['books'][1]['metadata']['authorsLock'])

    def test_lock_only_change_and_already_applied_noop(self):
        for book in self.data['books']:
            book['metadata']['authors'] = deepcopy(self.authors)
        operation = self.plan()
        self.execute(operation)
        with self.assertRaises(ConfigError):
            self.plan()
        self.restore(operation)

    def test_bad_selection_flags_ids_and_disabled_or_demo_execution(self):
        for selected in ([], ['missing'], ['a','a'], 'book', [None]):
            with self.subTest(selected=selected), self.assertRaises(ConfigError):
                self.plan(selected=selected)
        for options in ({'replace_lists':1}, {'replace_locked':'yes'}):
            with self.assertRaises(ConfigError):
                self.plan(**options)
        for config in (replace(self.settings, execution_enabled=False), replace(self.settings, demo=True),
                       replace(self.settings, komga_url=''), replace(self.settings, path_mappings=())):
            with self.assertRaises(ConfigError):
                KomgaAuthorsService(config, client=self.client).prepare(self.series.id,'remote-series',self.authors)
        with self.assertRaises(StateConflict):
            self.service.prepare(self.series.id,'wrong-series',self.authors)
        self.data['version']='unsupported'
        with self.assertRaises(ConfigError):
            self.plan()
        self.assertEqual(self.client.patches,[])

    def test_malformed_author_response_book_identity_or_status_are_rejected(self):
        for values in ({'authors':None}, {'authorsLock':1}, {'authors':[{'name':'a','role':'WRITER'}]}):
            before=deepcopy(self.data['books'][0]['metadata'])
            self.data['books'][0]['metadata'].update(values)
            with self.subTest(values=values), self.assertRaises(KomgaError):
                self.plan()
            self.data['books'][0]['metadata']=before
        get=self.client.get
        for values in ({'deleted':True}, {'fileHash':'other'}, {'media':None}, {'url':'file:///data/other'}):
            def changed(path):
                book=get(path)
                if path.startswith('/api/v1/books/'):
                    book.update(values)
                return book
            with patch.object(self.client,'get',side_effect=changed), self.assertRaises(StateConflict):
                self.plan()
        self.assertEqual(self.client.patches,[])

    def test_changed_local_edits_or_authors_prevent_first_patch(self):
        operation=self.plan()
        self.service.store.save_edit(self.series.id,{'author':'別の著者'},0,{})
        with self.assertRaises(StateConflict):
            self.execute(operation)
        operation=self.plan()
        self.data['books'][0]['metadata']['authorsLock']=True
        with self.assertRaises(StateConflict):
            self.execute(operation)
        self.assertEqual(self.client.patches,[])

    def test_changed_archive_or_remote_book_id_prevents_first_patch(self):
        operation = self.plan()
        source = self.settings.library_root / operation['plan']['entries'][0]['source']
        original = source.read_bytes()
        source.write_bytes(original + b'changed')
        with self.assertRaises(StateConflict):
            self.execute(operation)
        source.write_bytes(original)
        operation = self.plan()
        self.data['books'][0]['id'] = 'replacement-id'
        with self.assertRaises(StateConflict):
            self.execute(operation)
        self.assertEqual(self.client.patches, [])

    def test_more_than_fifty_books_requires_explicit_selection(self):
        from shutil import copyfile
        source = self.settings.library_root / self.series.books[0].path
        for number in range(3, 52):
            copyfile(source, source.parent / f'追加 第{number}巻.cbz')
        with self.assertRaisesRegex(ConfigError, '50巻'):
            self.plan()
        self.assertEqual(self.client.patches, [])

    def test_bad_confirmation_and_wrong_engine_do_not_write(self):
        operation=self.plan()
        with self.assertRaises(ConfigError):
            self.service.execute(operation['plan']['id'],'wrong')
        with self.assertRaises(ConfigError):
            OperationService(self.settings).execute(operation['plan']['id'],operation['digest'])
        self.assertEqual(self.client.patches,[])

    def test_second_patch_failure_preserves_attempts_and_restores_only_changed_book(self):
        operation=self.plan()
        original=self.values()
        actual=self.service._patch_book
        def fail(book,values):
            if book['id']=='book-1':
                raise KomgaError('No response before apply')
            return actual(book,values)
        with patch.object(self.service,'_patch_book',side_effect=fail), self.assertRaises(StateConflict):
            self.execute(operation)
        self.assertEqual(len(self.client.patches),1)
        self.assertEqual(len(self.service.inspect(operation['plan']['id'])['attempted']),2)
        with self.assertRaises(StateConflict):
            self.execute(operation)
        self.restore(operation)
        self.assertEqual(self.values(),original)
        self.assertEqual(len(self.client.patches),2)

    def test_lost_second_response_is_not_retried_and_both_books_restore_in_reverse_order(self):
        operation=self.plan()
        original=self.values()
        actual=self.service._patch_book
        def lost(book,values):
            actual(book,values)
            if book['id']=='book-1':
                raise KomgaError('lost response')
        with patch.object(self.service,'_patch_book',side_effect=lost), self.assertRaises(StateConflict):
            self.execute(operation)
        self.restore(operation)
        self.assertEqual([p['id'] for p in self.client.patches],['book-0','book-1','book-1','book-0'])
        self.assertEqual(self.values(),original)

    def test_restore_preflights_every_book_and_never_overwrites_later_changes(self):
        operation=self.plan()
        self.execute(operation)
        self.data['books'][1]['metadata']['authors'].append({'name':'後から追加','role':'writer'})
        with self.assertRaises(StateConflict):
            self.restore(operation)
        self.assertEqual(len(self.client.patches),2)

    def test_unattempted_book_even_matching_after_is_not_reverted(self):
        operation=self.plan()
        actual=self.service._patch_book
        def fail_after_first(book,values):
            actual(book,values)
            raise KomgaError('response lost on first')
        with patch.object(self.service,'_patch_book',side_effect=fail_after_first), self.assertRaises(StateConflict):
            self.execute(operation)
        self.data['books'][1]['metadata'].update(operation['plan']['books'][1]['after'])
        with self.assertRaises(StateConflict):
            self.restore(operation)
        self.assertEqual(len(self.client.patches),1)

    def test_partial_unexpected_author_result_is_not_overwritten(self):
        operation=self.plan()
        actual=self.service._patch_book
        def partial(book,values):
            actual(book,{'authors':values['authors']})
        with patch.object(self.service,'_patch_book',side_effect=partial), self.assertRaises(StateConflict):
            self.execute(operation)
        with self.assertRaises(StateConflict):
            self.restore(operation)
        self.assertEqual(len(self.client.patches),1)

    def test_journal_sync_failure_and_remote_change_during_sync_prevent_patch(self):
        operation=self.plan()
        with patch.object(self.service,'_save',side_effect=OSError('sync failure')), self.assertRaises(OSError):
            self.execute(operation)
        self.assertEqual(self.client.patches,[])
        save=self.service._save
        def change(op):
            save(op)
            if op.get('intent'):
                self.data['books'][0]['metadata']['authorsLock']=True
        with patch.object(self.service,'_save',side_effect=change), self.assertRaises(StateConflict):
            self.execute(operation)
        self.assertEqual(self.client.patches,[])

    def test_previous_checkpoint_recovers_and_cannot_execute(self):
        operation=self.plan()
        original=self.values()
        self.execute(operation)
        (self.service._folder(operation['plan']['id'])/'manifest.json').unlink()
        with self.assertRaises(StateConflict):
            self.execute(operation)
        self.restore(operation)
        self.assertEqual(self.values(),original)

    def test_history_renders_per_book_authors_and_has_no_web_write_endpoint(self):
        operation=self.plan([{'name':'<script>架空</script>','role':'writer'}])
        self.execute(operation)
        app=create_app(settings=self.settings)
        self.addCleanup(app.extensions['jobs'].shutdown)
        client=app.test_client()
        job=self.service.store.jobs()[0]
        response=client.get('/jobs/'+job['id'])
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertIn('著者・役割',html)
        self.assertIn('&lt;script&gt;',html)
        self.assertIn('作品 第1巻.cbz',html)
        self.assertEqual(client.post('/api/komga/authors',json={}).status_code,404)
        self.assertEqual(client.post('/api/jobs',json={'kind':'komga_authors'}).status_code,400)

    def test_cli_candidates_prepare_execute_inspect_and_restore(self):
        config=self.root/'config.yaml'
        config.write_text('app:\n  demo: false\nlibrary:\n  root: library\ndata:\n  root: state\nexecution:\n  enabled: true\nkomga:\n  base_url: http://127.0.0.1:25601\n  path_mappings:\n    - komga: /data\n      local: library\n')
        def command(args):
            out=io.StringIO()
            with patch('sys.argv',['manga-kit','--config',str(config),*args]),patch('sys.stdout',out),patch('manga_kit.operations.KomgaClient',return_value=self.client):
                self.assertEqual(main(),0)
            return json.loads(out.getvalue())
        self.assertTrue(command(['author-candidates','--series',self.series.id])['review_required'])
        operation=command(['prepare-authors','--series',self.series.id,'--komga-series','remote-series','--author','writer','架空原作'])
        self.assertEqual(command(['execute',operation['plan']['id'],'--confirm',operation['digest']])['status'],'completed')
        self.assertEqual(command(['operation',operation['plan']['id']])['plan']['kind'],'komga_authors')
        self.assertEqual(command(['restore',operation['plan']['id'],'--confirm',operation['digest']])['status'],'restored')

    @unittest.skipUnless(Path('/proc/self/fd').is_dir(),'Linux SIGKILL verification')
    def test_crash_before_after_each_patch_and_during_restore(self):
        for action,boundary in (('execute','before'),('execute','after-first'),('execute','after-second'),('restore','after-first')):
            operation=self.plan()
            original=self.values()
            if action=='restore':
                self.execute(operation)
            remote=self.root/'fake-authors.json'
            remote.write_text(json.dumps(self.data))
            process=subprocess.run([sys.executable,str(Path(__file__).with_name('authors_worker.py')),str(self.root),operation['plan']['id'],operation['digest'],action,boundary],capture_output=True,text=True,timeout=20)
            self.assertEqual(process.returncode,-signal.SIGKILL,process.stderr)
            self.data=json.loads(remote.read_text())
            self.client=FakeAuthorsKomga(self.data)
            self.service=KomgaAuthorsService(self.settings,client=self.client)
            self.restore(operation)
            self.assertEqual(self.values(),original)
            self.assertEqual(self.service.inspect(operation['plan']['id'])['status'],'restored')


if __name__=='__main__':
    unittest.main()
