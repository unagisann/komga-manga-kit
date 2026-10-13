"""Reviewable metadata edits stored in application state, never in manga archives."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json

from .config import ConfigError
from .japanese import hiragana
from .models import ScanResult
from .state import StateConflict

FIELDS = {"title": 300, "author": 300, "reading": 300, "status": 20, "summary": 8000, "publisher": 300}


def validate_fields(fields):
    if not isinstance(fields, dict) or not fields or set(fields) - FIELDS.keys():
        raise ConfigError("作品名・著者・読み・状態・あらすじ・出版社を指定してください。")
    result = {}
    for key, value in fields.items():
        if not isinstance(value, str) or len(value) > FIELDS[key] or any(ord(c) < 32 and c not in "\n\t" for c in value):
            raise ConfigError(f"{key} の値または文字数が不正です。")
        value = value.strip()
        if key == "title" and not value:
            raise ConfigError("作品名は空にできません。")
        if key == "status" and value not in {"ongoing", "completed", "unknown"}:
            raise ConfigError("状態は ongoing / completed / unknown で指定してください。")
        if key == "reading":
            value = hiragana(value)
        result[key] = value
    return result


def fingerprint(series):
    return hashlib.sha256(json.dumps(asdict(series), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def apply_edits(result, edits):
    result = deepcopy(result)
    for series in result.series:
        fields = edits.get(series.id, {}).get("fields", {})
        for key, value in fields.items():
            if key in FIELDS:
                setattr(series, key, value)
        if fields:
            series.sources.append("manual")
    return result


class MetadataService:
    def __init__(self, library, store):
        self.library, self.store = library, store

    def series(self, series_id, *, refresh=False):
        result = self.library.scan(refresh=refresh, edited=False)
        series = next((series for series in result.series if series.id == series_id), None)
        if series is None:
            raise KeyError(series_id)
        edit = self.store.edits().get(series_id, {"fields": {}, "revision": 0})
        effective = apply_edits(ScanResult([series]), {series_id: edit}).series[0]
        return series, effective, edit

    def inspect(self, series_id):
        base, current, edit = self.series(series_id)
        return {"series": current.to_dict(), "original": {key: getattr(base, key) for key in FIELDS},
                "overrides": edit["fields"], "revision": edit["revision"], "base_fingerprint": fingerprint(base)}

    def preview(self, series_id, fields=None, *, reset=False, refresh=False):
        fields = {} if reset else validate_fields(fields)
        base, current, edit = self.series(series_id, refresh=refresh)
        overrides = {} if reset else {**edit["fields"], **fields}
        overrides = {key: value for key, value in overrides.items() if value != getattr(base, key)}
        after = apply_edits(ScanResult([base]), {series_id: {"fields": overrides}}).series[0]
        changes = [{"field": key, "before": getattr(current, key), "after": getattr(after, key)}
                   for key in FIELDS if getattr(current, key) != getattr(after, key)]
        return {"series_id": series_id, "revision": edit["revision"], "base_fingerprint": fingerprint(base),
                "changes": changes, "before_row": current.row, "after_row": after.row,
                "fields": overrides, "reset": reset, "source": "manual"}

    def save(self, series_id, fields, revision, base_fingerprint, *, reset=False):
        with self.store.editing():
            return self._save(series_id, fields, revision, base_fingerprint, reset=reset)

    def _save(self, series_id, fields, revision, base_fingerprint, *, reset=False):
        if type(revision) is not int or revision < 0 or not isinstance(base_fingerprint, str):
            raise ConfigError("保存前に差分プレビューを取得してください。")
        preview = self.preview(series_id, fields, reset=reset, refresh=True)
        if preview["base_fingerprint"] != base_fingerprint:
            raise StateConflict("書庫の情報が更新されました。差分を再確認してください。")
        updated = self.store.save_edit(series_id, preview["fields"], revision,
                                       {"changes": preview["changes"], "reset": reset})
        return {**self.inspect(series_id), "revision": updated}
