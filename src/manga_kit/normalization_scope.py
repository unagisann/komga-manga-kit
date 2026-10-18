"""Resolve normalization targets without changing the transaction root or layout."""
import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath

from .config import ConfigError
from .paths import map_komga_path
from .state import StateConflict


def within(path, roots):
    return any(PurePosixPath(path).is_relative_to(PurePosixPath(root)) for root in roots)


class NormalizationTargets:
    def __init__(self, settings, store, client):
        self.settings, self.store, self.client = settings, store, client

    def signature(self):
        value = {"root": str(self.settings.library_root.resolve()), "komga": self.settings.komga_url,
                 "mappings": [{"komga": item.komga, "local": str(item.local.resolve())}
                              for item in self.settings.path_mappings]}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def libraries(self):
        cached = self.store.cached("normalization:libraries")
        if self.settings.komga_url and cached and cached["data"].get("signature") == self.signature():
            return cached["data"]["libraries"]
        result = [{"id": "local", "label": "設定済みライブラリ", "path": ".", "available": True}]
        for status in ("ongoing", "completed", "unknown"):
            name = self.settings.layout.folder(status)
            path = self.settings.library_root / name
            if self.settings.demo or path.is_dir() and not path.is_symlink():
                result.append({"id": "local-" + status, "label": name, "path": name, "available": True})
        return result

    def refresh(self):
        if not self.settings.komga_url or self.settings.demo:
            return self.libraries()
        entries = self.client.get("/api/v1/libraries")
        if not isinstance(entries, list):
            raise ConfigError("Komgaの登録ライブラリを確認できませんでした。")
        libraries, seen = [], set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"] or entry["id"] in seen:
                raise ConfigError("Komgaの登録ライブラリを確認できませんでした。")
            seen.add(entry["id"])
            try:
                path = map_komga_path(entry.get("root", ""), self.settings.path_mappings, self.settings.library_root)
            except ConfigError:
                path = None
            libraries.append({"id": "komga-" + entry["id"], "label": str(entry.get("name") or "名称未設定")[:200],
                              "path": path, "available": path is not None})
        self.store.cache("normalization:libraries", {"signature": self.signature(), "libraries": libraries})
        return libraries

    def resolve(self, options=None):
        options = {} if options is None else options
        if not isinstance(options, dict) or set(options) - {"library", "subpath", "limit", "recheck"}:
            raise ConfigError("ノーマライズの対象設定を確認してください。")
        identifier, subpath = options.get("library", "all"), options.get("subpath", "")
        if not isinstance(identifier, str) or not isinstance(subpath, str) or len(subpath) > 1000:
            raise ConfigError("ライブラリとサブパスを確認してください。")
        subpath = subpath.strip()
        if subpath and (PurePosixPath(subpath).is_absolute() or PureWindowsPath(subpath).drive or "\\" in subpath
                        or any(part in {"", ".", ".."} for part in subpath.split("/"))
                        or any(ord(char) < 32 for char in subpath)):
            raise ConfigError("サブパスは選択したライブラリ内の相対フォルダで指定してください。")
        if identifier == "all" and subpath:
            raise ConfigError("サブパスを使う場合はライブラリを一つ選んでください。")
        limit, recheck = options.get("limit", self.settings.max_files), options.get("recheck", False)
        if type(limit) is not int or not 1 <= limit <= self.settings.max_files or type(recheck) is not bool:
            raise ConfigError(f"確認件数の上限は1〜{self.settings.max_files}、再確認は true / false で指定してください。")
        libraries = self.libraries()
        chosen = [item for item in libraries if item["available"] and (identifier == "all" or item["id"] == identifier)]
        if not chosen:
            raise ConfigError("対象のライブラリがありません。登録とパス対応を確認してください。")
        roots = sorted({str(PurePosixPath(item["path"]) / subpath) for item in chosen})
        if not self.settings.demo:
            base = self.settings.library_root.resolve()
            for root in roots:
                path = self.settings.library_root
                for part in PurePosixPath(root).parts:
                    path = path / part
                    if path.is_symlink():
                        raise ConfigError("サブパスにシンボリックリンクは指定できません。")
                if not path.is_dir() or not path.resolve().is_relative_to(base):
                    raise ConfigError("対象フォルダがライブラリ内に見つかりません。")
        return {"library": identifier, "label": "全体" if identifier == "all" else chosen[0]["label"],
                "subpath": subpath, "roots": roots, "limit": limit, "recheck": recheck,
                "signature": self.signature()}

    def verify(self, saved):
        if saved is None:
            return
        try:
            current = self.resolve({key: saved[key] for key in ("library", "subpath", "limit", "recheck")})
        except ConfigError as error:
            raise StateConflict("対象ライブラリ・サブパスが変わりました。差分を再確認してください。") from error
        if current["roots"] != saved["roots"] or current["signature"] != saved["signature"]:
            raise StateConflict("対象ライブラリ・パス対応が変わりました。差分を再確認してください。")
