"""Validated configuration; media paths never depend on package location."""
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import re
from string import Formatter
from urllib.parse import urlsplit

import yaml


class ConfigError(ValueError):
    pass


def validate_template(template: str, allowed: set[str], *, path: bool = False) -> None:
    if not isinstance(template, str) or not template.strip() or len(template) > 300:
        raise ConfigError("命名テンプレートは1〜300文字で指定してください。")
    try:
        for literal, name, spec, conversion in Formatter().parse(template):
            if name is None:
                continue
            if name not in allowed or conversion:
                raise ConfigError(f"使用できない変数です: {name}")
            if spec and (name != "volume" or not re.fullmatch(r"0?[1-9][0-9]?", spec)):
                raise ConfigError("書式指定は巻数の桁数（例: {volume:02}）のみ使えます。")
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if "\\" in template or ("/" in template and not path):
        raise ConfigError("ファイル名に階層区切りは使えません。")
    if path and (template.startswith("/") or any(p in ("", ".", "..") for p in template.split("/"))):
        raise ConfigError("階層はルート内の相対パスで指定してください。")


@dataclass(frozen=True)
class Layout:
    ongoing_folder: str = "連載中"
    completed_folder: str = "完結"
    unknown_folder: str = "未分類"
    series: str = "[{author}] {title}"
    book: str = "{title} 第{volume:02}巻{extension}"
    ongoing: str = "{status}/{series}"
    completed: str = "{status}/{kana_row}/{series}"
    unknown: str = "{status}/{series}"

    def validate(self) -> "Layout":
        folders = [self.ongoing_folder, self.completed_folder, self.unknown_folder]
        for folder in folders:
            if (not isinstance(folder, str) or not folder.strip() or folder in (".", "..")
                    or any(c in folder for c in '/\\<>:"|?*') or any(ord(c) < 32 for c in folder)
                    or folder.endswith((".", " "))):
                raise ConfigError("状態フォルダ名は空でない単一のフォルダ名で指定してください。")
        if len({f.casefold() for f in folders}) != 3:
            raise ConfigError("連載中・完結・未分類には異なるフォルダ名を指定してください。")
        validate_template(self.series, {"title", "author"})
        validate_template(self.book, {"title", "volume", "extension"})
        if "{extension}" not in self.book:
            raise ConfigError("巻ファイル名には {extension} が必要です。")
        for name in ("ongoing", "completed", "unknown"):
            validate_template(getattr(self, name), {"status", "series", "author", "kana_row", "title"}, path=True)
        return self

    def with_overrides(self, values: dict) -> "Layout":
        allowed = set(self.__dataclass_fields__)
        if not isinstance(values, dict) or set(values) - allowed:
            raise ConfigError("未知の整理設定があります。")
        return replace(self, **values).validate()

    def folder(self, status: str) -> str:
        return getattr(self, f"{status}_folder")


@dataclass(frozen=True)
class Settings:
    name: str = "Komga Manga Kit"
    demo: bool = True
    library_root: Path = field(default_factory=lambda: Path.cwd() / "var" / "sample-library")
    max_files: int = 20000
    layout: Layout = field(default_factory=Layout)
    komga_url: str = ""
    api_key_env: str = "KOMGA_API_KEY"
    timeout_seconds: int = 10


def load_settings(path: str | Path | None = None) -> Settings:
    configured = path or os.getenv("MANGA_KIT_CONFIG")
    default = Path.cwd() / "config.yaml"
    source = Path(configured) if configured else default
    source = source.expanduser().resolve()
    raw = {}
    if configured and not source.is_file():
        raise ConfigError(f"設定ファイルがありません: {source}")
    if source.is_file():
        try:
            loaded = yaml.safe_load(source.read_text(encoding="utf-8"))
            raw = {} if loaded is None else loaded
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError("設定ファイルを読み込めません。") from exc
    if not isinstance(raw, dict) or set(raw) - {"app", "library", "layout", "komga"}:
        raise ConfigError("設定は app / library / layout / komga のマッピングで指定してください。")
    sections = {name: raw.get(name, {}) for name in ("app", "library", "layout", "komga")}
    if any(not isinstance(value, dict) for value in sections.values()):
        raise ConfigError("設定の各セクションはマッピングで指定してください。")
    valid_keys = {"app": {"name", "demo"}, "library": {"root", "max_files"},
                  "layout": {"status_folders", "series", "book", "ongoing", "completed", "unknown"},
                  "komga": {"base_url", "api_key_env", "timeout_seconds"}}
    for section, value in sections.items():
        if set(value) - valid_keys[section]:
            raise ConfigError(f"{section} に未知の設定項目があります。")
    app, lib, layout, komga = (sections[n] for n in ("app", "library", "layout", "komga"))
    folders = layout.pop("status_folders", {})
    if not isinstance(folders, dict) or set(folders) - {"ongoing", "completed", "unknown"}:
        raise ConfigError("status_folders が不正です。")
    layout.update({f"{key}_folder": value for key, value in folders.items()})
    root_value = os.getenv("MANGA_KIT_LIBRARY_ROOT") or lib.get("root", "./var/sample-library")
    if not isinstance(root_value, str) or not root_value.strip():
        raise ConfigError("ライブラリの保存先は空でないパスで指定してください。")
    root = Path(root_value).expanduser()
    if not root.is_absolute():
        root = source.parent / root
    demo = app.get("demo", True)
    if "MANGA_KIT_DEMO" in os.environ:
        value = os.environ["MANGA_KIT_DEMO"].lower()
        if value not in {"true", "false", "1", "0"}:
            raise ConfigError("MANGA_KIT_DEMO は true / false で指定してください。")
        demo = value in {"true", "1"}
    name = app.get("name", "Komga Manga Kit")
    limit, timeout = lib.get("max_files", 20000), komga.get("timeout_seconds", 10)
    if not isinstance(demo, bool) or not isinstance(name, str) or not 1 <= len(name) <= 80:
        raise ConfigError("app.name / app.demo が不正です。")
    if type(limit) is not int or not 1 <= limit <= 100000 or type(timeout) is not int or not 1 <= timeout <= 60:
        raise ConfigError("ファイル上限は1〜100000、タイムアウトは1〜60秒で指定してください。")
    url = os.getenv("KOMGA_URL") or komga.get("base_url", "")
    if not isinstance(url, str):
        raise ConfigError("Komga URLは文字列で指定してください。")
    try:
        parsed = urlsplit(url)
        parsed.port
    except ValueError as exc:
        raise ConfigError("Komga URLが不正です。") from exc
    if url and (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ConfigError("Komga URLは認証情報を含まないHTTP(S) URLで指定してください。")
    key_env = komga.get("api_key_env", "KOMGA_API_KEY")
    if not isinstance(key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key_env):
        raise ConfigError("APIキーの環境変数名が不正です。")
    return Settings(name, demo, root.resolve(), limit, Layout(**layout).validate(), url.rstrip("/"), key_env, timeout)
