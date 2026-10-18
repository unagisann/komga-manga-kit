from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from manga_kit import create_app
from manga_kit.bibliography import edition_markers, edition_title
from manga_kit.catalogs import CatalogError, RakutenBooks
from manga_kit.config import Settings
from manga_kit.enrichment import EnrichmentService
from manga_kit.library import LibraryService
from manga_kit.matching import compare_candidate
from manga_kit.metadata import MetadataService
from manga_kit.sample import create_sample_library
from manga_kit.state import StateStore
from test_enrichment import response_data


def local(title='架空の物語', author='架空作者', publisher='テスト出版', links=None):
    return {'series': {'title': title, 'author': author, 'publisher': publisher, 'path': title},
            'provenances': links or {}}


def book(**changes):
    data = response_data()
    data['Items'][0].update(changes)
    return RakutenBooks.parse(data, 1)['candidates'][0]


class CandidateMatchingTests(unittest.TestCase):
    def test_exact_identity_keeps_publisher_separate_from_label(self):
        candidate = book(seriesName='架空少年コミックス')
        self.assertEqual(candidate['label'], '架空少年コミックス')
        self.assertEqual(candidate['fields']['publisher'], 'テスト出版')
        match = compare_candidate(local(), candidate)
        self.assertEqual(match['status'], 'consistent')
        self.assertEqual(match['evidence'], ['作品名が一致', '著者が一致', '出版社が一致'])

    def test_same_title_different_creators_and_reprint_publishers_need_review(self):
        for changes, warning in (({'author': '別の作者'}, '著者'), ({'publisherName': '再刊出版'}, '出版社')):
            with self.subTest(changes=changes):
                match = compare_candidate(local(), book(**changes))
                self.assertEqual(match['status'], 'review')
                self.assertTrue(any(warning in text for text in match['warnings']))

    def test_short_titles_subtitles_and_related_books_are_not_substring_matches(self):
        for title in ('架空', '架空の物語（続編）', '架空の物語 公式ガイド'):
            match = compare_candidate(local(), book(title=title))
            self.assertEqual(match['status'], 'review')
            self.assertIn('作品名・確認済みの別名が一致しません', match['warnings'][0])
        short = compare_candidate(local(title='月'), book(title='月（1）'))
        self.assertTrue(any('短い作品名' in text for text in short['warnings']))

    def test_confirmed_source_aliases_and_candidate_alternate_titles_are_used(self):
        linked = {'anilist': {'identity': {'titles': ['架空の物語', 'Kakuu Monogatari']}}}
        match = compare_candidate(local(links=linked), book(title='KAKUU MONOGATARI（1）'))
        self.assertEqual(match['status'], 'consistent')
        self.assertIn('確認済みの別名が一致', match['evidence'])
        candidate = book(title='別の表記（1）')
        candidate['alternate_titles'] = ['架空の物語']
        self.assertEqual(compare_candidate(local(), candidate)['status'], 'consistent')
        # A query typed by the user is not evidence of an alias.
        self.assertEqual(compare_candidate(local(), book(title='Kakuu Monogatari（1）'))['status'], 'review')

    def test_multiple_authors_compare_names_without_losing_middle_dots_or_roles(self):
        candidate = book(author='作画: 田中乙 / 原作: 星野・甲 / 他')
        match = compare_candidate(local(author='原作: 星野・甲 × 作画: 田中乙'), candidate)
        self.assertIn('著者が一致', match['evidence'])
        candidate['fields']['author'] = '星野・甲'
        self.assertTrue(any('著者の一覧' in text for text in compare_candidate(
            local(author='原作: 星野・甲 × 作画: 田中乙'), candidate)['warnings']))

    def test_editions_are_visible_and_never_treated_as_regular_editions(self):
        for title, changes, marker in (
                ('架空の物語（1） （特装版）', {}, '特装版'),
                ('架空の物語 新装版 1', {}, '新装版'),
                ('架空の物語（1）', {'size': '文庫'}, '文庫版'),
                ('架空の物語（1）', {'seriesName': '架空文庫'}, '文庫版')):
            with self.subTest(title=title, changes=changes):
                candidate = book(title=title, **changes)
                self.assertIn(marker, candidate['editions'])
                self.assertTrue(any('版の表記' in text for text in compare_candidate(local(), candidate)['warnings']))
        candidate = book(title='架空の物語（新装版） 1')
        match = compare_candidate(local(title='架空の物語（新装版）'), candidate)
        self.assertIn('版の表記が一致', match['evidence'])
        self.assertEqual(edition_title('架空の物語（続編）'), '架空の物語（続編）')
        self.assertEqual(edition_title('完全版の物語'), '完全版の物語')
        self.assertEqual(edition_markers('完全版の物語'), [])

    def test_ambiguous_search_results_show_why_even_when_one_candidate_matches(self):
        candidate = book()
        for changes in ({'author': '別の作者'}, {'publisherName': '再刊出版'},
                        {'seriesName': '別レーベル'}, {'title': '架空の物語（1） （限定版）'}):
            other = book(isbn='9780000000026', **changes)
            match = compare_candidate(local(), candidate, [candidate, other])
            self.assertEqual(match['status'], 'review')
            self.assertTrue(any('同名で' in text for text in match['warnings']))
        same_edition = book(isbn='9780000000026', title='架空の物語（2）')
        self.assertEqual(compare_candidate(local(), candidate, [candidate, same_edition])['status'], 'consistent')

    def test_label_readership_is_conservative_and_genre_disagreement_has_no_proposal(self):
        for label, expected in (('架空少年コミックス', ['少年']), ('架空少女コミック', ['少女']),
                                ('架空青年漫画', ['青年']), ('架空レディースコミックス', ['レディース']),
                                ('架空コミックス', []), ('少年の冒険コミックス', []),
                                ('少年コミックス／少女コミックス', [])):
            candidate = book(seriesName=label, booksGenreId='001001006')
            self.assertEqual(candidate['fields'].get('genres', []), expected)
            if expected:
                self.assertEqual(candidate['readership_source'], 'label')
        conflict = book(seriesName='架空少女コミックス', booksGenreId='001001001')
        self.assertNotIn('genres', conflict['fields'])
        self.assertEqual(conflict['readership_source'], 'conflict')
        self.assertTrue(any('読者層' in text for text in compare_candidate(local(), conflict)['warnings']))
        # Multiple explicit label audiences are ambiguous even if the genre has one.
        ambiguous = book(seriesName='少年コミックス／少女コミックス', booksGenreId='001001001')
        self.assertNotIn('genres', ambiguous['fields'])

    def test_optional_label_and_format_are_bounded_and_typed(self):
        for field, value in (('seriesName', []), ('seriesName', 'x' * 301), ('seriesName', 'bad\nlabel'),
                             ('size', 2), ('size', 'x' * 41)):
            with self.subTest(field=field, value=value), self.assertRaises(CatalogError):
                book(**{field: value})


class MatchingIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(demo=False, library_root=self.root / 'library', data_root=self.root / 'state',
                                 execution_enabled=True)
        create_sample_library(self.settings.library_root, self.settings.layout)
        self.store = StateStore(self.settings)
        self.library = LibraryService(self.settings, store=self.store)
        self.metadata = MetadataService(self.library, self.store)
        self.service = EnrichmentService(self.settings, self.metadata, self.store)
        self.catalog = Mock(id='rakuten_books', label='楽天Books')
        self.catalog.configured.return_value = True
        self.catalog.search.side_effect = lambda query: deepcopy(RakutenBooks.parse(response_data(), 1))
        self.service.provider = self.catalog
        self.series = self.library.scan().series[0]

    def test_cached_candidates_recompare_current_edits_and_preview_keeps_review_reasons(self):
        first = self.service.search(self.series.id, title='架空の物語')
        current = self.metadata.inspect(self.series.id)
        self.metadata.save(self.series.id, {'title': '架空の物語', 'author': '架空作者', 'publisher': 'テスト出版'},
                           current['revision'], current['base_fingerprint'])
        second = self.service.search(self.series.id, title='架空の物語')
        self.assertTrue(second['cached'])
        self.assertEqual(self.catalog.search.call_count, 1)
        self.assertNotEqual(first['candidates'][0]['match'], second['candidates'][0]['match'])
        self.assertEqual(second['candidates'][0]['match']['status'], 'consistent')
        current = self.metadata.inspect(self.series.id)
        self.metadata.save(self.series.id, {'author': '別の作者'}, current['revision'], current['base_fingerprint'])
        preview = self.service.preview(self.series.id, search_id=first['search_id'],
                                       candidate_id=first['candidates'][0]['id'], fields={})
        self.assertTrue(any('著者の一覧' in text for text in preview['match']['warnings']))
        self.assertEqual(preview['match'], preview['provenance']['match'])

    def test_reviewed_aliases_persist_across_restart_and_unlink_without_media_changes(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.settings.library_root.rglob('*') if p.is_file()}
        tags = Mock(id='anilist', label='AniList')
        tags.configured.return_value = True
        tags.search.return_value = {'candidates': [{'id': '99', 'title': self.series.title,
            'alternate_titles': ['Kakuu Monogatari', '<架空の別名>'], 'fields': {'tags': ['旅']},
            'url': 'https://anilist.co/manga/99'}], 'total': 1, 'page': 1, 'page_count': 1}
        self.service.tag_provider = tags
        result = self.service.search(self.series.id, provider='anilist', title=self.series.title)
        body = {'provider': 'anilist', 'search_id': result['search_id'], 'candidate_id': '99', 'fields': {}}
        preview = self.service.preview(self.series.id, **body)
        self.service.save(self.series.id, preview['revision'], preview['base_fingerprint'], **body)
        reopened = StateStore(self.settings)
        service = EnrichmentService(self.settings, MetadataService(LibraryService(self.settings, store=reopened), reopened), reopened)
        self.assertIn('Kakuu Monogatari', service.known_titles(self.series.id))
        with patch('requests.Session.get', side_effect=AssertionError('Unexpected network')):
            app = create_app(settings=self.settings)
            self.addCleanup(app.extensions['jobs'].shutdown)
            response = app.test_client().get(f'/metadata/{self.series.id}/sources')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('確認済みの別名', html)
        self.assertIn('Kakuu Monogatari', html)
        self.assertIn('&lt;架空の別名&gt;', html)
        self.assertNotIn('<架空の別名>', html)
        unlink = service.preview(self.series.id, provider='anilist', unlink=True)
        service.save(self.series.id, unlink['revision'], unlink['base_fingerprint'], provider='anilist', unlink=True)
        self.assertNotIn('Kakuu Monogatari', service.known_titles(self.series.id))
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_saved_source_retains_product_edition_label_and_readership_basis(self):
        data = response_data()
        data['Items'][0].update(title='架空の物語（1） （特装版）', seriesName='架空少年コミックス', booksGenreId='001001006')
        self.catalog.search.side_effect = lambda query: RakutenBooks.parse(data, 1)
        result = self.service.search(self.series.id, title='架空の物語')
        body = {'search_id': result['search_id'], 'candidate_id': result['candidates'][0]['id'], 'fields': {'genres': ['少年']}}
        preview = self.service.preview(self.series.id, **body)
        saved = self.service.save(self.series.id, preview['revision'], preview['base_fingerprint'], **body)
        identity = saved['provenance']['identity']
        self.assertEqual(identity['label'], '架空少年コミックス')
        self.assertEqual(identity['editions'], ['特装版'])
        self.assertEqual(identity['product_title'], '架空の物語（1） （特装版）')
        self.assertEqual(identity['readership_source'], 'label')
        self.assertEqual(self.store.events()[0]['detail']['provenance']['identity'], identity)

    def test_preview_compares_sidecar_changes_after_its_required_refresh(self):
        result = self.service.search(self.series.id, title='架空の物語')
        sidecar = self.settings.library_root / self.series.path / 'series.json'
        content = json.loads(sidecar.read_text())
        content.update(title='架空の物語', author='架空作者', publisher='テスト出版')
        sidecar.write_text(json.dumps(content))
        preview = self.service.preview(self.series.id, search_id=result['search_id'],
                                       candidate_id=result['candidates'][0]['id'], fields={})
        self.assertEqual(preview['match']['status'], 'consistent')


if __name__ == '__main__':
    unittest.main()
