"""Library-scoped search cache, explicit bibliographic links and reviewed edits."""
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from uuid import uuid4

from .catalogs import DemoCatalog, RakutenBooks, search_query
from .config import ConfigError
from .metadata import validate_fields
from .state import StateConflict, timestamp

CACHE_SECONDS = 3600
MAX_SEARCHES = 100


class EnrichmentService:
    def __init__(self, settings, metadata, store):
        self.metadata, self.store = metadata, store
        self.provider = DemoCatalog(settings.layout) if settings.demo else RakutenBooks(settings.rakuten, store)

    def status(self):
        return {"provider": self.provider.id, "label": self.provider.label,
                "configured": self.provider.configured(), "cache_seconds": CACHE_SECONDS}

    def search(self, series_id, **values):
        self.metadata.inspect(series_id)
        query = search_query(**values)
        if not self.provider.configured():
            raise ConfigError("楽天Booksの設定がありません。設定画面で認証情報の登録状況を確認してください。")
        key = hashlib.sha256(json.dumps([self.provider.id, query], sort_keys=True).encode()).hexdigest()
        now = time.time()
        with self.store.connection() as connection:
            connection.execute("DELETE FROM metadata_searches WHERE expires<=?", (now,))
            row = connection.execute("SELECT id,data FROM metadata_searches WHERE namespace=? AND series_id=? AND cache_key=? AND expires>? ORDER BY expires DESC LIMIT 1",
                                     (self.store.namespace, series_id, key, now)).fetchone()
        if row:
            return {**json.loads(row["data"]), "search_id": row["id"], "cached": True}
        result = self.provider.search(query)
        now = time.time()
        result.update(provider=self.provider.id, label=self.provider.label, retrieved_at=timestamp(),
                      expires_at=datetime.fromtimestamp(now + CACHE_SECONDS, timezone.utc).isoformat(), query=query)
        search_id = uuid4().hex
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO metadata_searches VALUES(?,?,?,?,?,?)", (search_id, self.store.namespace, series_id, key,
                                                                                   now + CACHE_SECONDS, json.dumps(result, ensure_ascii=False)))
            connection.execute("DELETE FROM metadata_searches WHERE namespace=? AND id NOT IN "
                               "(SELECT id FROM metadata_searches WHERE namespace=? ORDER BY expires DESC,id DESC LIMIT ?)",
                               (self.store.namespace, self.store.namespace, MAX_SEARCHES))
        return {**result, "search_id": search_id, "cached": False}

    def _candidate(self, series_id, search_id, candidate_id):
        if not isinstance(search_id, str) or not re.fullmatch(r"[0-9a-f]{32}", search_id) or not isinstance(candidate_id, str):
            raise ConfigError("検索結果から候補を選んでください。")
        with self.store.connection() as connection:
            row = connection.execute("SELECT data FROM metadata_searches WHERE id=? AND namespace=? AND series_id=? AND expires>?",
                                     (search_id, self.store.namespace, series_id, time.time())).fetchone()
        if row is None:
            raise StateConflict("検索結果が期限切れか、別の作品の候補です。検索し直してください。")
        result = json.loads(row["data"])
        if result["provider"] != self.provider.id:
            raise StateConflict("情報源の設定が変わりました。検索し直してください。")
        candidate = next((item for item in result["candidates"] if item["id"] == candidate_id), None)
        if candidate is None:
            raise ConfigError("選んだ候補が検索結果にありません。")
        return result, candidate

    def preview(self, series_id, *, search_id=None, candidate_id=None, fields=None, unlink=False):
        current = self.metadata.inspect(series_id)
        if type(unlink) is not bool:
            raise ConfigError("unlink は true / false で指定してください。")
        if unlink:
            if search_id is not None or candidate_id is not None or fields is not None:
                raise ConfigError("対応付けの解除と補完は同時に指定できません。")
            if not current["provenance"]:
                raise StateConflict("解除する対応付けがありません。")
            selected, provenance = {}, {}
        else:
            result, candidate = self._candidate(series_id, search_id, candidate_id)
            if not isinstance(fields, dict) or set(fields) - candidate["fields"].keys():
                raise ConfigError("この候補にある項目から、採用する情報を選んでください。")
            selected = validate_fields(fields) if fields else {}
            provenance = {"provider": result["provider"], "item_id": candidate["id"], "url": candidate["url"],
                          "retrieved_at": result["retrieved_at"], "applied_fields": list(selected),
                          "adjusted_fields": [key for key, value in selected.items() if value != candidate["fields"][key]]}
        preview = self.metadata.preview(series_id, selected or {"title": current["series"]["title"]}, refresh=True)
        return {**preview, "provenance": provenance, "previous_provenance": current["provenance"], "unlink": unlink,
                "selected_fields": selected}

    def save(self, series_id, revision, base_fingerprint, **values):
        # Hold the library edit lock across candidate checks and the atomic edit/link save.
        with self.store.editing():
            preview = self.preview(series_id, **values)
            provenance = preview["provenance"]
            if provenance:
                provenance["reviewed_at"] = timestamp()
            fields = preview["selected_fields"] or {"title": self.metadata.inspect(series_id)["series"]["title"]}
            return self.metadata._save(series_id, fields, revision, base_fingerprint, provenance=provenance)
