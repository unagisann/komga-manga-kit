"""Guard byte-preserving moves against the tested Komga hash-restore behavior."""
from collections import Counter
from copy import deepcopy
from pathlib import PurePosixPath
import time
from urllib.parse import quote

from .config import ConfigError
from .komga import WRITE_VERSIONS_LABEL, KomgaError, write_version_supported
from .paths import map_komga_path
from .state import StateConflict


def locked_metadata(metadata):
    result = {}
    for key, value in metadata.items():
        if key.endswith("Lock") and value is True:
            result[key] = True
            result[key[:-4]] = metadata.get(key[:-4])
    return result


def read_progress(book):
    value = book.get("readProgress")
    return {key: value.get(key) for key in ("page", "completed", "readDate")} if value else None


class KomgaMoveGuard:
    def __init__(self, settings, client):
        self.settings, self.client = settings, client

    def books(self, deleted=False):
        result, page = [], 0
        while True:
            response = self.client.request("POST", "/api/v1/books/list",
                {"condition": {"deleted": {"operator": "isTrue" if deleted else "isFalse"}}}, size=100, page=page)
            if not isinstance(response, dict) or not isinstance(response.get("content"), list):
                raise KomgaError("Komgaの書籍応答形式が不正です。")
            result.extend(response["content"])
            if response.get("last", len(result) >= response.get("totalElements", len(result))):
                return result
            if len(result) > 100000 or not response["content"]:
                raise ConfigError("Komgaの書籍一覧を安全に取得できませんでした。")
            page += 1

    def relative(self, url):
        try:
            return map_komga_path(url, self.settings.path_mappings, self.settings.library_root)
        except ConfigError:
            return None

    def library(self):
        """Return the Komga library at the configured root after the write preconditions."""
        if not write_version_supported(self.client.get("/actuator/info")):
            raise ConfigError(f"整理のKomga連携は実機検証済みのKomga {WRITE_VERSIONS_LABEL} に限ります。")
        libraries = self.client.get("/api/v1/libraries")
        matches = [lib for lib in libraries if self.relative(lib["root"]) == "."]
        if len(matches) != 1:
            raise ConfigError("ライブラリのルートと一致するKomgaのパス対応を1件設定してください。")
        lib = matches[0]
        if (not lib["hashFiles"] or lib["emptyTrashAfterScan"] or lib["scanOnStartup"]
                or lib["scanInterval"] != "DISABLED" or lib["convertToCbz"] or lib["repairExtensions"]):
            raise ConfigError("Komgaはハッシュ有効・定期/起動時スキャン無効・ゴミ箱自動削除無効・自動変換無効にしてください。")
        return lib

    def snapshot(self, entries, groups, *, books=None):
        if not self.settings.komga_url:
            return None
        lib = self.library()
        # Callers that also need the active list pass it in, so it is fetched once.
        all_books = self.books() if books is None else books
        hash_counts = Counter(book.get("fileHash") for book in all_books)
        trash_hashes = {book.get("fileHash") for book in self.books(deleted=True)}
        mapped = {}
        for book in all_books:
            if book["libraryId"] == lib["id"]:
                path = self.relative(book["url"])
                if path in mapped:
                    raise ConfigError("Komgaのパス対応が重複しています。")
                mapped[path] = book
        snapshot = {"library_id": lib["id"], "books": {}, "series": {}}
        for entry in entries:
            if entry["kind"] != "book":
                continue
            book = mapped.get(entry["source"])
            if book is None or not book.get("fileHash") or book["media"]["status"] != "READY":
                raise ConfigError("Komgaで書籍の解析とハッシュ計算を完了し、再スキャンしてください。")
            if book["fileHash"] != entry["snapshot"]["komga_hash"]:
                raise StateConflict("Komgaのハッシュと現在の書庫が一致しません。解析・再スキャン後に計画を作り直してください。")
            if hash_counts[book["fileHash"]] != 1 or book["fileHash"] in trash_hashes:
                raise ConfigError("Komgaに同一内容の書籍またはゴミ箱の候補があります。整理前に確認してください。")
            snapshot["books"][entry["source"]] = {"hash": book["fileHash"], "progress": read_progress(book),
                "locked": locked_metadata(book["metadata"]), "series_id": book["seriesId"]}
        for group in groups:
            selected = [snapshot["books"][entry["source"]] for entry in entries
                        if entry["kind"] == "book" and str(PurePosixPath(entry["source"]).parent) == group["source"]]
            if not selected:
                continue
            ids = {book["series_id"] for book in selected}
            if len(ids) != 1:
                raise ConfigError("ローカル作品とKomga作品の対応が不明確です。")
            series_id = next(iter(ids))
            if sum(book["seriesId"] == series_id for book in all_books) != len(selected):
                raise ConfigError("作品の一部だけを移動することはできません。")
            series = self.client.get("/api/v1/series/" + quote(series_id, safe=""))
            snapshot["series"][group["source"]] = locked_metadata(series["metadata"])
        return snapshot

    def restore_snapshot(self, snapshot, entries):
        if snapshot is None:
            return None
        if not write_version_supported(self.client.get("/actuator/info")):
            raise ConfigError(f"復元時も実機検証済みのKomga {WRITE_VERSIONS_LABEL} を使用してください。")
        lib = next((lib for lib in self.client.get("/api/v1/libraries") if lib["id"] == snapshot["library_id"]), None)
        if (not lib or self.relative(lib["root"]) != "." or not lib["hashFiles"]
                or lib["scanInterval"] != "DISABLED" or lib["scanOnStartup"] or lib["emptyTrashAfterScan"]
                or lib["convertToCbz"] or lib["repairExtensions"]):
            raise ConfigError("Komgaの保存先・ハッシュ・スキャン・ゴミ箱設定を元に戻してから復元してください。")
        books = self.books() + self.books(deleted=True)
        result = deepcopy(snapshot)
        for entry in entries:
            if entry["kind"] != "book":
                continue
            expected = snapshot["books"][entry["source"]]
            candidates = [book for book in books if book.get("fileHash") == expected["hash"]]
            if (len(candidates) != 1 or candidates[0]["libraryId"] != snapshot["library_id"]
                    or self.relative(candidates[0]["url"]) not in {entry["source"], entry["target"]}):
                raise StateConflict("復元するKomga書籍の対応が不明確です。上書きせず中止しました。")
            current = candidates[0]
            result["books"][entry["source"]].update(progress=read_progress(current),
                locked=locked_metadata(current["metadata"]))
            parent = str(PurePosixPath(entry["source"]).parent)
            if parent in snapshot["series"]:
                try:
                    series = self.client.get("/api/v1/series/" + quote(expected["series_id"], safe=""))
                except KomgaError:
                    series = self.client.get("/api/v1/series/" + quote(current["seriesId"], safe=""))
                result["series"][parent] = locked_metadata(series["metadata"])
        return result

    def verify_after(self, snapshot, entries, groups, *, restore=False):
        if snapshot is None:
            if self.settings.komga_url:
                raise StateConflict("計画時とKomgaの接続設定が異なります。")
            return
        self.client.request("POST", "/api/v1/libraries/" + quote(snapshot["library_id"], safe="") + "/scan")
        deadline = time.monotonic() + 90
        while True:
            mapped = {self.relative(book["url"]): book for book in self.books()
                      if book["libraryId"] == snapshot["library_id"]}
            valid, checked = True, {}
            for entry in entries:
                if entry["kind"] != "book":
                    continue
                path = entry["source"] if restore else entry["target"]
                before = snapshot["books"][entry["source"]]
                current = mapped.get(path)
                if (not current or current.get("fileHash") != before["hash"]
                        or read_progress(current) != before["progress"]
                        or locked_metadata(current["metadata"]) != before["locked"]):
                    valid = False
                    break
                checked[path] = current
            if valid:
                for group in groups:
                    source = group["source"]
                    expected = snapshot["series"].get(source)
                    if expected is None:
                        continue
                    prefix = source if restore else group["target"]
                    book = next(book for path, book in checked.items() if str(PurePosixPath(path).parent) == prefix)
                    series = self.client.get("/api/v1/series/" + quote(book["seriesId"], safe=""))
                    if locked_metadata(series["metadata"]) != expected:
                        valid = False
                        break
            if valid:
                return
            if time.monotonic() >= deadline:
                raise StateConflict("Komgaの既読情報・ロックしたメタデータの引き継ぎを確認できませんでした。")
            time.sleep(.5)
