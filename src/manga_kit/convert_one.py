"""Portable single-book conversion. The source is never moved or replaced."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import stat
import tempfile

from .archives import build_cbz, inspect_archive
from .config import ConfigError
from .durability import sync_directory
from .operations import digest
from .state import StateConflict


class SingleSource:
    def __init__(self, path):
        self.path = Path(path).absolute()

    @contextmanager
    def open_read(self, relative):
        if self.path.is_symlink():
            raise ConfigError("元書庫にリンクは指定できません。")
        descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as content:
            if not stat.S_ISREG(os.fstat(content.fileno()).st_mode):
                raise ConfigError("通常ファイルの書庫を指定してください。")
            yield content

    def fingerprint(self):
        with self.open_read(self.path.name) as content:
            before = os.fstat(content.fileno())
            value = hashlib.file_digest(content, "sha256").hexdigest()
            after = os.fstat(content.fileno())
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise StateConflict("確認中に元書庫が変更されました。")
        return {"sha256": value, "size": after.st_size, "mtime_ns": after.st_mtime_ns}


def convert_one(source, destination, *, page_order="natural-path", enabled=False, confirm=None):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if destination.suffix.lower() != ".cbz" or destination.exists() or destination.is_symlink() or destination == source:
        raise ConfigError("出力先には存在しない別名のCBZを指定してください。")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise ConfigError("出力先のフォルダを作成してください。")
    fs = SingleSource(source)
    before = fs.fingerprint()
    archive = inspect_archive(fs, source.name, page_order=page_order)
    if fs.fingerprint() != before:
        raise StateConflict("確認中に元書庫が変更されました。")
    plan = {"source": str(source), "destination": str(destination), "before": before, "archive": archive}
    confirmation = digest(plan)
    if confirm is None:
        return {"plan": plan, "digest": confirmation, "preview": True}
    if not enabled or confirm != confirmation:
        raise ConfigError("実行を有効にし、確認したdigestを指定してください。")
    with tempfile.TemporaryDirectory(prefix=".manga-kit-convert-", dir=destination.parent) as directory:
        stage = Path(directory) / "book.cbz"
        build_cbz(fs, source.name, archive, stage)
        if fs.fingerprint() != before:
            raise StateConflict("生成中に元書庫が変更されました。出力せず中止しました。")
        try:
            os.link(stage, destination)
        except FileExistsError:
            raise StateConflict("出力先にファイルが作成されました。上書きせず中止しました。") from None
        except OSError:
            raise ConfigError("出力先は安全なハードリンクの作成に対応したファイルシステムを使用してください。") from None
        sync_directory(destination.parent)
    return {"source_retained": True, "destination": str(destination), "pages": archive["pages"], "digest": confirmation}
