"""Map Komga filesystem paths using component boundaries, never string prefixes."""
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from urllib.parse import unquote, urlsplit

from .config import ConfigError


def remote_path(value):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ConfigError("Komgaパスは空でない絶対パスで指定してください。")
    if value.startswith("file:"):
        parsed = urlsplit(value)
        if parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
            raise ConfigError("Komgaのfile URLが不正です。")
        value = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
    kind = PureWindowsPath if re.match(r"^[A-Za-z]:[/\\]", value) or value.startswith("\\\\") else PurePosixPath
    path = kind(value)
    if not path.is_absolute() or ".." in path.parts or any(ord(c) < 32 for c in str(path)):
        raise ConfigError("Komgaパスは階層の逸脱がない絶対パスで指定してください。")
    return path


def map_komga_path(value: str, mappings, library_root: Path) -> str | None:
    path = remote_path(value)
    for mapping in sorted(mappings, key=lambda m: len(remote_path(m.komga).parts), reverse=True):
        prefix = remote_path(mapping.komga)
        if type(path) is not type(prefix) or not path.is_relative_to(prefix):
            continue
        local = mapping.local.joinpath(*path.relative_to(prefix).parts).resolve()
        if not local.is_relative_to(library_root.resolve()):
            raise ConfigError("対応先が漫画ライブラリの外を指しています。")
        return local.relative_to(library_root.resolve()).as_posix()
    return None
