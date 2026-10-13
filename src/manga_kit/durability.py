"""Explicit persistence of files and directory entries; errors are not ignored."""
import os
from pathlib import Path
import stat

from .config import ConfigError


def sync_directory(path):
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ConfigError("同期対象は通常ファイルである必要があります。")
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_mkdir(path, mode=0o700):
    path = Path(path)
    if os.name != "posix":
        path.mkdir(parents=True, exist_ok=True, mode=mode)
        return
    if not path.exists():
        durable_mkdir(path.parent, mode)
        try:
            path.mkdir(mode=mode)
        except FileExistsError:
            pass
    sync_directory(path)
    # Mount points already exist in the parent filesystem. Flush their mounted
    # contents without trying to sync a container's read-only root.
    if path != path.parent and path.stat().st_dev == path.parent.stat().st_dev:
        sync_directory(path.parent)
