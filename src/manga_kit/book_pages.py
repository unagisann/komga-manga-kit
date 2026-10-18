"""Bounded image reads from locally owned books, without extraction."""
from pathlib import PurePosixPath

from .archives import IMAGES, MAX_ENTRIES, name_key, reader, safe_name
from .config import ConfigError
from .filesystem import LibraryFilesystem

MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp", ".avif": "image/avif", ".bmp": "image/bmp"}
MAX_IMAGE = 25 * 1024 * 1024


class BookPages:
    def __init__(self, settings, inventory):
        self.settings, self.inventory = settings, inventory

    def find(self, identifier, *, local=True):
        for series in self.inventory.cached()["series"]:
            for book in series["books"]:
                if book["id"] == identifier:
                    if local and not book.get("path"):
                        raise ConfigError("この本はローカルの保存先と対応付けられていません。")
                    return book
        raise ConfigError("本が見つかりません。蔵書情報を更新してください。")

    def names(self, identifier):
        book = self.find(identifier)
        pages, names = [], set()
        with LibraryFilesystem(self.settings.library_root) as fs, fs.open_read(book["path"]) as content:
            with reader(content, PurePosixPath(book["path"]).suffix.lower()) as (_, _, entries):
                for index, (raw, size, directory, _) in enumerate(entries, 1):
                    if index > MAX_ENTRIES:
                        raise ConfigError("書庫内の項目数が上限を超えています。")
                    name = safe_name(raw, directory)
                    key = name_key(name)
                    if key in names:
                        raise ConfigError("書庫内に重複した名前があります。")
                    names.add(key)
                    if not directory and PurePosixPath(name).suffix.lower() in IMAGES:
                        pages.append({"name": name, "size": size})
        return sorted(pages, key=lambda item: item["name"])

    def image(self, identifier, page):
        if type(page) is not int or not 1 <= page <= MAX_ENTRIES:
            raise ConfigError("ページ番号が不正です。")
        pages = self.names(identifier)
        if page > len(pages):
            raise ConfigError("ページが見つかりません。")
        chosen = pages[page - 1]
        if not 0 < chosen["size"] <= MAX_IMAGE:
            raise ConfigError("画像サイズがプレビューの上限を超えています。")
        suffix = PurePosixPath(chosen["name"]).suffix.lower()
        if suffix not in MIME:
            raise ConfigError("この画像形式はブラウザーでのプレビューに対応していません。")
        book = self.find(identifier)
        with LibraryFilesystem(self.settings.library_root) as fs, fs.open_read(book["path"]) as content:
            with reader(content, PurePosixPath(book["path"]).suffix.lower()) as (_, _, entries):
                for raw, size, directory, blocks in entries:
                    if not directory and raw == chosen["name"]:
                        data = bytearray()
                        for block in blocks:
                            if len(data) + len(block) > min(size, MAX_IMAGE):
                                raise ConfigError("画像のサイズが書庫の記録と一致しません。")
                            data.extend(block)
                        if len(data) != chosen["size"]:
                            raise ConfigError("画像が変更されました。再確認してください。")
                        return bytes(data), MIME[suffix]
        raise ConfigError("画像が変更されました。再確認してください。")
