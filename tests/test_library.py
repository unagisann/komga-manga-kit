from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from manga_kit.config import ConfigError, Layout, Settings, load_settings
from manga_kit.japanese import hiragana, kana_row, search_key
from manga_kit.library import filename_volume, scan_library
from manga_kit.models import Book, Series
from manga_kit.organizer import component, plan_organization
from manga_kit.providers import ComicInfoProvider
from manga_kit.sample import create_sample_library


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def config(self, content):
        path = self.root / "config.yaml"
        path.write_text(content, encoding="utf-8")
        return load_settings(path)

    def archive(self, path, xml=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("0001.png", b"fixture")
            if xml is not None:
                archive.writestr("ComicInfo.xml", xml)


class ConfigurationTests(IsolatedTest):
    def test_paths_resolve_against_configuration(self):
        config = self.config("app:\n  demo: false\nlibrary:\n  root: ./books\n")
        self.assertEqual(config.library_root, self.root / "books")
        self.assertFalse(config.demo)

    def test_environment_overrides_yaml_without_embedding_keys(self):
        with patch.dict(os.environ, {"MANGA_KIT_DEMO": "false", "MANGA_KIT_LIBRARY_ROOT": str(self.root / "custom"),
                                     "KOMGA_URL": "https://example.test/komga", "KOMGA_API_KEY": "secret"}):
            config = self.config("app:\n  demo: true\n")
        self.assertFalse(config.demo)
        self.assertEqual(config.library_root, self.root / "custom")
        self.assertEqual(config.komga_url, "https://example.test/komga")
        self.assertNotIn("secret", repr(config))

    def test_explicit_missing_configuration_is_an_error(self):
        with self.assertRaises(ConfigError):
            load_settings(self.root / "missing.yaml")

    def test_invalid_sections_types_and_unknown_keys(self):
        for content in ["- bad", "[]", "false", "0", "app: []", "app:\n  demo: maybe", "library:\n  root: []",
                        "library:\n  max_files: true", "komga:\n  base_url: ftp://host",
                        "komga:\n  base_url: http://user:secret@host", "komga:\n  base_url: http://[invalid",
                        "app:\n  typo: 1", "layout:\n  status_folders:\n    completed: []", "extra: 1"]:
            with self.subTest(content=content), self.assertRaises(ConfigError):
                self.config(content)

    def test_invalid_templates_and_traversal(self):
        for changes in [{"completed": "../{series}"}, {"completed": "/{series}"},
                        {"series": "{title.__class__}"}, {"series": "{title!r}"},
                        {"series": "{title:1000000}"}, {"series": "{oops}"}, {"series": "{title"},
                        {"book": "{title}.cbz"}, {"ongoing_folder": "a/b"}, {"ongoing_folder": ".."},
                        {"completed_folder": "連載中"}]:
            with self.subTest(changes=changes), self.assertRaises(ConfigError):
                Layout().with_overrides(changes)


class JapaneseTests(unittest.TestCase):
    def test_kana_normalization_and_rows(self):
        self.assertEqual(hiragana("ｶﾞｯｺｳ"), "がっこう")
        self.assertEqual(kana_row(" ガラス"), "か")
        self.assertEqual(kana_row("ヴァイオリン"), "あ")
        self.assertEqual(kana_row("雨あがり"), "その他")
        self.assertEqual(search_key("アメ アガリ"), search_key("ｱﾒｱｶﾞﾘ"))

    def test_volume_parsing_preserves_decimal_and_zero(self):
        self.assertEqual(filename_volume("作品 第０２巻.cbz"), "2")
        self.assertEqual(filename_volume("作品 v1.5.cbz"), "1.5")
        self.assertEqual(filename_volume("作品（0）.zip"), "0")
        self.assertIsNone(filename_volume("作品.cbz"))

    def test_padded_underscore_suffix_and_explicit_marker_precedence(self):
        for name, expected in (("sample_series_01.zip", "1"), ("作品_００２.cbz", "2"),
                               ("作品_120.zip", "120"), ("作品_00.zip", "0"),
                               ("作品 第3巻_01.zip", "3"), ("作品 v2_01.zip", "2")):
            with self.subTest(name=name):
                self.assertEqual(filename_volume(name), expected)

    def test_ambiguous_numeric_names_remain_unknown(self):
        for name in ("作品_1.zip", "作品_2024.zip", "作品_2024_01.zip", "2024_01.zip",
                     "作品_01_02.zip", "作品86_01.zip", "作品_01.5.zip", "作品_01_extra.zip", "作品 01.zip"):
            with self.subTest(name=name):
                self.assertIsNone(filename_volume(name))


class ScanTests(IsolatedTest):
    def test_generated_library_is_read_and_preview_leaves_every_byte_unchanged(self):
        layout = Layout(ongoing_folder="Ongoing", completed_folder="Completed", unknown_folder="Unknown")
        self.assertEqual(create_sample_library(self.root, layout), 22)
        before = {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in self.root.rglob("*") if p.is_file()}
        result = scan_library(Settings(demo=False, library_root=self.root, layout=layout))
        self.assertEqual(result.stats()["series"], 8)
        self.assertEqual(result.stats()["books"], 22)
        self.assertEqual(result.stats()["completed"], 3)
        self.assertEqual(result.stats()["needs_metadata"], 1)
        self.assertFalse(result.warnings)
        self.assertTrue(all(not s.warnings for s in result.series))
        plan = plan_organization(result.series, layout, self.root)
        self.assertEqual(plan["counts"]["ready"], 22)
        after = {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_comicinfo_then_sidecar_override(self):
        folder = self.root / "連載中" / "[作者] ファイル名"
        self.archive(folder / "第01巻.cbz", "<ComicInfo><Series>XML作品</Series><Writer>XML著者</Writer><Number>３</Number></ComicInfo>")
        (folder / "series.json").write_text(json.dumps({"title": "指定作品", "reading": "シテイサクヒン", "status": "completed"}), encoding="utf-8")
        series = scan_library(Settings(demo=False, library_root=self.root)).series[0]
        self.assertEqual(series.title, "指定作品")
        self.assertEqual(series.author, "XML著者")
        self.assertEqual(series.status, "completed")
        self.assertEqual(series.row, "さ")
        self.assertEqual(series.books[0].volume, "3")

    def test_komga_mylar_series_json_is_read_without_changing_status(self):
        folder = self.root / "連載中" / "[作者] ファイル名"
        self.archive(folder / "第01巻.cbz", "<ComicInfo><Series>XML作品</Series><Number>1</Number></ComicInfo>")
        mylar = {"version": "1.0.2", "metadata": {
            "type": "comicSeries", "publisher": "架空出版", "imprint": None, "name": "架空の作品", "comicid": "1",
            "year": 2020, "description_text": "架空のあらすじ。", "description_formatted": None, "volume": 1,
            "booktype": "Print", "age_rating": None, "ComicImage": "", "total_issues": 3, "publication_run": "",
            "status": "Ended"}}
        (folder / "series.json").write_text(json.dumps(mylar, ensure_ascii=False), encoding="utf-8")
        series = scan_library(Settings(demo=False, library_root=self.root)).series[0]
        self.assertEqual((series.title, series.publisher, series.summary), ("架空の作品", "架空出版", "架空のあらすじ。"))
        self.assertEqual(series.status, "ongoing")
        self.assertEqual(series.warnings, [])
        # Komga distinguishes later volumes of the same name by year.
        mylar["metadata"].update(volume=2, description_formatted="整形済みのあらすじ。")
        (folder / "series.json").write_text(json.dumps(mylar, ensure_ascii=False), encoding="utf-8")
        series = scan_library(Settings(demo=False, library_root=self.root)).series[0]
        self.assertEqual((series.title, series.summary), ("架空の作品 (2020)", "整形済みのあらすじ。"))
        for broken in ({"name": ""}, {"name": 3}, {"year": "2020"}, {"publisher": ["架空"]}):
            (folder / "series.json").write_text(json.dumps({"metadata": {**mylar["metadata"], **broken}}), encoding="utf-8")
            with self.subTest(broken=broken):
                series = scan_library(Settings(demo=False, library_root=self.root)).series[0]
                self.assertEqual(series.title, "XML作品")
                self.assertTrue(series.warnings)

    def test_corrupt_archive_does_not_hide_other_books(self):
        self.archive(self.root / "作品" / "第01巻.cbz")
        (self.root / "作品" / "第02巻.cbz").write_bytes(b"not a zip")
        result = scan_library(Settings(demo=False, library_root=self.root))
        self.assertEqual(len(result.series[0].books), 2)
        self.assertEqual(len(result.series[0].warnings), 1)

    def test_symlink_books_and_directories_are_ignored(self):
        self.archive(self.root / "作品" / "第01巻.cbz")
        (self.root / "リンク").symlink_to(self.root / "作品", target_is_directory=True)
        (self.root / "作品" / "第02巻.cbz").symlink_to(self.root / "作品" / "第01巻.cbz")
        result = scan_library(Settings(demo=False, library_root=self.root))
        self.assertEqual(result.stats()["books"], 1)

    def test_scan_limit_is_visible(self):
        create_sample_library(self.root, Layout())
        result = scan_library(Settings(demo=False, library_root=self.root, max_files=2))
        self.assertTrue(result.truncated)
        self.assertEqual(result.stats()["books"], 2)

    def test_empty_missing_root_is_visible(self):
        result = scan_library(Settings(demo=False, library_root=self.root / "missing"))
        self.assertFalse(result.series)
        self.assertEqual(len(result.warnings), 1)

    def test_sample_refuses_overwrite(self):
        (self.root / "keep.txt").write_text("keep")
        with self.assertRaises(ValueError):
            create_sample_library(self.root, Layout())
        self.assertEqual((self.root / "keep.txt").read_text(), "keep")

    def test_xml_entities_and_oversized_metadata_are_rejected(self):
        payloads = [b'<!DOCTYPE x [<!ENTITY x "bad">]><ComicInfo><Series>&x;</Series></ComicInfo>',
                    '<!DOCTYPE x [<!ENTITY x "bad">]><ComicInfo><Series>&x;</Series></ComicInfo>'.encode('utf-16'),
                    b" " * (256 * 1024 + 1)]
        for i, payload in enumerate(payloads):
            path = self.root / f"{i}.cbz"
            self.archive(path, payload)
            with self.subTest(i=i), self.assertRaises(Exception):
                ComicInfoProvider().inspect(path)


class OrganizationTests(IsolatedTest):
    def series(self, **changes):
        result = Series("old", "作品", "作者", "サクヒン", "completed", books=[Book("old/1.cbz", "1")])
        return replace(result, **changes)

    def test_custom_author_and_flat_hierarchy(self):
        layout = Layout(series="{title}", completed="{author}/{series}", book="{title} v{volume:03}{extension}")
        target = plan_organization([self.series()], layout)["items"][0]["target"]
        self.assertEqual(target, "作者/作品/作品 v001.cbz")
        target = plan_organization([self.series()], replace(layout, completed="{series}"))["items"][0]["target"]
        self.assertEqual(target, "作品/作品 v001.cbz")

    def test_decimal_volume_is_preserved(self):
        series = self.series(books=[Book("old/1.5.cbz", "1.5")])
        target = plan_organization([series], Layout())["items"][0]["target"]
        self.assertIn("第01.5巻.cbz", target)

    def test_volume_width_pads_whole_part_so_decimals_sort_between_neighbors(self):
        cases = [("{title} 第{volume:02}巻{extension}", "1.5", "作品 第01.5巻.cbz"),
                 ("{title} 第{volume:02}巻{extension}", "3", "作品 第03巻.cbz"),
                 ("{title} 第{volume:02}巻{extension}", "0", "作品 第00巻.cbz"),
                 ("{title} 第{volume:02}巻{extension}", "12.25", "作品 第12.25巻.cbz"),
                 ("{title} v{volume:03}{extension}", "2.5", "作品 v002.5.cbz"),
                 ("{title} {volume}{extension}", "1.5", "作品 1.5.cbz")]
        for template, volume, filename in cases:
            with self.subTest(template=template, volume=volume):
                series = self.series(books=[Book("old/x.cbz", volume)])
                target = plan_organization([series], Layout(book=template))["items"][0]["target"]
                self.assertEqual(Path(target).name, filename)
        names = {v: Path(plan_organization([self.series(books=[Book("old/x.cbz", v)])], Layout())["items"][0]["target"]).name
                 for v in ("1", "1.5", "2", "9", "10")}
        # Same as the existing tool: 1.5 stays next to 1, never between 9 and 10.
        self.assertEqual(sorted(names.values())[2:], [names["2"], names["9"], names["10"]])

    def test_unknown_kana_row_blocks_new_placement_until_reading_is_set(self):
        series = self.series(title="漢字作品", reading="")
        plan = plan_organization([series], Layout())
        self.assertEqual(plan["counts"]["blocked"], 1)
        self.assertIn("読みが未設定", plan["items"][0]["issues"][0])
        self.assertIn("/その他/", plan["items"][0]["target"])
        for changes, row in (({"reading": "かんじさくひん"}, "か"), ({"title": "カタカナ作品", "reading": ""}, "か"),
                             ({"reading": "ABC"}, "その他")):
            with self.subTest(changes=changes):
                plan = plan_organization([replace(series, **changes)], Layout())
                self.assertEqual(plan["counts"]["ready"], 1)
                self.assertTrue(plan["items"][0]["target"].startswith(f"完結/{row}/"))
        # Ongoing series and an existing その他 folder do not need a new row decision.
        self.assertEqual(plan_organization([replace(series, status="ongoing")], Layout())["counts"]["blocked"], 0)
        placed = replace(series, path="完結/その他/[作者] 漢字作品", books=[Book("完結/その他/[作者] 漢字作品/1.cbz", "1")])
        self.assertEqual(plan_organization([placed], Layout())["counts"], {"ready": 1, "unchanged": 0, "blocked": 0})

    def test_nested_series_keep_their_place_under_the_planned_parent(self):
        parent = self.series(path="連載中/[作者] 作品", books=[Book("連載中/[作者] 作品/1.cbz", "1")])
        child = Series("連載中/[作者] 作品/外伝", "外伝", "", "", "completed", books=[Book("連載中/[作者] 作品/外伝/1.cbz", "1")])
        grandchild = Series("連載中/[作者] 作品/外伝/番外", "番外", "", "", "unknown",
                            books=[Book("連載中/[作者] 作品/外伝/番外/1.cbz", "1")])
        targets = {item["source"]: item["target"] for item in
                   plan_organization([grandchild, child, parent], Layout())["items"]}
        self.assertEqual(targets["連載中/[作者] 作品/1.cbz"], "完結/さ/[作者] 作品/作品 第01巻.cbz")
        self.assertEqual(targets["連載中/[作者] 作品/外伝/1.cbz"], "完結/さ/[作者] 作品/外伝/外伝 第01巻.cbz")
        self.assertEqual(targets["連載中/[作者] 作品/外伝/番外/1.cbz"], "完結/さ/[作者] 作品/外伝/番外/番外 第01巻.cbz")
        # Without its parent in the plan, a nested series is only renamed in place.
        plan = plan_organization([child], Layout(), context=[parent, child])
        self.assertEqual(plan["items"][0]["target"], "連載中/[作者] 作品/外伝/外伝 第01巻.cbz")
        self.assertEqual(plan["counts"]["blocked"], 0)

    def test_missing_volume_blocks_only_that_book(self):
        series = self.series(books=[Book("old/unknown.cbz"), Book("old/2.cbz", "2")])
        plan = plan_organization([series], Layout())
        self.assertEqual(plan["counts"], {"ready": 1, "unchanged": 0, "blocked": 1})

    def test_duplicate_targets_are_blocked_including_case_variants(self):
        a = self.series(title="Book", books=[Book("a/1.cbz", "1")])
        b = self.series(title="book", books=[Book("b/1.cbz", "1")])
        plan = plan_organization([a, b], Layout())
        self.assertEqual(plan["counts"]["blocked"], 2)

    def test_existing_target_blocks_without_overwriting(self):
        series = self.series()
        target = self.root / plan_organization([series], Layout())["items"][0]["target"]
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing")
        plan = plan_organization([series], Layout(), self.root)
        self.assertEqual(plan["counts"]["blocked"], 1)
        self.assertEqual(target.read_bytes(), b"existing")

    def test_unchanged_is_distinct_from_ready(self):
        series = self.series()
        target = plan_organization([series], Layout())["items"][0]["target"]
        series.books[0].path = target
        plan = plan_organization([series], Layout())
        self.assertEqual(plan["counts"]["unchanged"], 1)

    def test_symlink_destination_outside_root_is_blocked(self):
        library = self.root / "library"
        library.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (library / "完結").symlink_to(outside, target_is_directory=True)
        plan = plan_organization([self.series()], Layout(), library)
        self.assertEqual(plan["counts"]["blocked"], 1)

    def test_title_cannot_become_path_or_reserved_filename(self):
        self.assertEqual(component("../../作品"), "..＿..＿作品")
        self.assertEqual(component("CON"), "_CON")
        series = self.series(title="../../作品", author="/作者")
        target = plan_organization([series], Layout())["items"][0]["target"]
        self.assertNotIn("..", Path(target).parts)
        self.assertFalse(Path(target).is_absolute())


if __name__ == "__main__":
    unittest.main()
