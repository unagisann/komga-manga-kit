"""Descriptor-relative file operations: no symlinks, no overwrite, one filesystem."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import PurePosixPath
import re
import stat
import unicodedata
from uuid import uuid4
import xxhash

from .config import ConfigError
from .durability import sync_directory
from .state import StateConflict


def file_hash(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise StateConflict("バックアップは通常ファイルである必要があります。")
        return hashlib.file_digest(source, "sha256").hexdigest()


class LibraryFilesystem:
    def __init__(self, root):
        if os.name != "posix":
            raise ConfigError("整理の実行はPOSIX環境で利用してください。Windowsではプレビューを利用できます。")
        self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.identity = {"device": os.fstat(self.fd).st_dev, "inode": os.fstat(self.fd).st_ino}

    def close(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def parent(self, relative, create=False):
        if not isinstance(relative, str):
            raise ConfigError("操作対象のパスが不正です。")
        path = PurePosixPath(relative)
        if (not relative or path.is_absolute()
                or any(part in {"", ".", ".."} for part in relative.split("/"))):
            raise ConfigError("操作対象はライブラリ内の相対パスで指定してください。")
        fd = os.dup(self.fd)
        try:
            for part in path.parts[:-1]:
                if create:
                    key = unicodedata.normalize("NFKC", part).casefold()
                    if any(name != part and unicodedata.normalize("NFKC", name).casefold() == key for name in os.listdir(fd)):
                        raise StateConflict("整理先に表記だけが異なるフォルダがあります。")
                    try:
                        os.mkdir(part, mode=0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                if os.fstat(child).st_dev != self.identity["device"]:
                    os.close(child)
                    raise ConfigError("別のファイルシステムへの整理は未対応です。")
                if create:
                    try:
                        os.fsync(child)
                        os.fsync(fd)
                    except BaseException:
                        os.close(child)
                        raise
                os.close(fd)
                fd = child
            yield fd, path.name
        finally:
            os.close(fd)

    def ensure_absent(self, relative):
        try:
            with self.parent(relative) as (parent, name):
                key = unicodedata.normalize("NFKC", name).casefold()
                if any(unicodedata.normalize("NFKC", existing).casefold() == key for existing in os.listdir(parent)):
                    raise StateConflict("整理先または復元先に同じ名前・表記違いのファイルがあります。")
        except FileNotFoundError:
            pass

    @contextmanager
    def open_read(self, relative):
        with self.parent(relative) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb", buffering=0) as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_dev != self.identity["device"]:
                    raise ConfigError("通常ファイル以外・別ファイルシステムは読み取れません。")
                yield source

    def snapshot(self, relative):
        with self.parent(relative) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_dev != self.identity["device"]:
                    raise ConfigError("通常ファイル以外・別ファイルシステムは整理できません。")
                digest = hashlib.sha256()
                native_hash = xxhash.xxh3_128(seed=0)
                while chunk := os.read(fd, 1024 * 1024):
                    digest.update(chunk)
                    native_hash.update(chunk)
                after = os.fstat(fd)
                if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise StateConflict("読み取り中にファイルが変更されました。再確認してください。")
                return {"sha256": digest.hexdigest(), "komga_hash": native_hash.hexdigest(), "size": after.st_size, "inode": after.st_ino,
                        "device": after.st_dev, "mtime_ns": after.st_mtime_ns, "mode": stat.S_IMODE(after.st_mode)}
            finally:
                os.close(fd)

    def existing(self, relative):
        try:
            return self.snapshot(relative)
        except FileNotFoundError:
            return None

    def verify(self, relative, expected):
        if self.snapshot(relative) != expected:
            raise StateConflict("確認後にファイルが変更または差し替えられました。プレビューを作り直してください。")

    def move(self, source, target, expected):
        self.verify(source, expected)
        self.ensure_absent(target)
        with self.parent(source) as (src_fd, src_name), self.parent(target, create=True) as (dst_fd, dst_name):
            os.link(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd, follow_symlinks=False)
            try:
                self.verify(target, expected)
                self.verify(source, expected)
                with self.open_read(target) as content:
                    os.fsync(content.fileno())
                # Persist the new name before removing the old name.
                os.fsync(dst_fd)
                os.unlink(src_name, dir_fd=src_fd)
                os.fsync(src_fd)
            except BaseException:
                # Delete only the link created by this call, never a substituted file.
                # After source unlink, the destination is the remaining original.
                try:
                    original = os.stat(src_name, dir_fd=src_fd, follow_symlinks=False)
                    current = os.stat(dst_name, dir_fd=dst_fd, follow_symlinks=False)
                    if all((value.st_dev, value.st_ino) == (expected["device"], expected["inode"])
                           for value in (original, current)):
                        os.unlink(dst_name, dir_fd=dst_fd)
                        os.fsync(dst_fd)
                except OSError:
                    pass
                raise

    def backup(self, relative, destination, expected):
        self.verify(relative, expected)
        with self.parent(relative) as (parent, name):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                with destination.open("xb") as out:
                    os.chmod(destination, 0o600)
                    while chunk := os.read(fd, 1024 * 1024):
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
            finally:
                os.close(fd)
        self.verify(relative, expected)
        if file_hash(destination) != expected["sha256"]:
            raise StateConflict("バックアップの内容を検証できませんでした。")
        sync_directory(destination.parent)

    def recover_backup(self, relative, backup, expected, before_publish=None, on_create=None, temporary_prefix=".manga-kit-restore-"):
        self.ensure_absent(relative)
        if file_hash(backup) != expected["sha256"]:
            raise StateConflict("バックアップが変更されています。復元を中止しました。")
        with self.parent(relative, create=True) as (parent, name):
            if not re.fullmatch(r"\.manga-kit-restore-(?:[0-9a-f]{32}-)?", temporary_prefix):
                raise ConfigError("復元用の一時ファイル名が不正です。")
            temporary = temporary_prefix + uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                if on_create is not None:
                    os.fsync(fd)
                    os.fsync(parent)
                    created = os.fstat(fd)
                    on_create(str(PurePosixPath(relative).parent / temporary),
                              {"device": created.st_dev, "inode": created.st_ino})
                copied_hash = hashlib.sha256()
                with backup.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        copied_hash.update(chunk)
                        view = memoryview(chunk)
                        while view:
                            view = view[os.write(fd, view):]
                if copied_hash.hexdigest() != expected["sha256"]:
                    raise StateConflict("コピー中にバックアップが変わりました。復元を中止しました。")
                os.fchmod(fd, expected["mode"])
                os.utime(fd, ns=(expected["mtime_ns"], expected["mtime_ns"]))
                os.fsync(fd)
                if before_publish is not None:
                    created = os.fstat(fd)
                    before_publish(dict(expected, inode=created.st_ino, device=created.st_dev,
                                        mtime_ns=created.st_mtime_ns, mode=stat.S_IMODE(created.st_mode)))
                os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.fsync(parent)
            finally:
                os.close(fd)
                os.unlink(temporary, dir_fd=parent)
                os.fsync(parent)
