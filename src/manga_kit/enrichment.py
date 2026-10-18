"""Library-scoped search cache, explicit bibliographic links and reviewed edits."""
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from uuid import uuid4

from .anilist import AniList, DemoClassifications, anilist_query
from .catalogs import DemoCatalog, RakutenBooks, search_query
from .kobo import DemoKobo, Kobo
from .mangaupdates import DemoMangaUpdates, MangaUpdates
from .config import ConfigError
from .metadata import validate_fields
from .matching import annotate_results, candidate_identity, compare_candidate, known_titles
from .state import StateConflict, timestamp

CACHE_SECONDS = 3600
MAX_SEARCHES = 100
SEARCH_CACHE_VERSION = 3


def search_cache_key(provider, query):
    return hashlib.sha256(json.dumps([provider, SEARCH_CACHE_VERSION, query], sort_keys=True).encode()).hexdigest()


class EnrichmentService:
    def __init__(self, settings, metadata, store):
        self.metadata, self.store = metadata, store
        self.provider = DemoCatalog(settings.layout) if settings.demo else RakutenBooks(settings.rakuten, store, settings.tools.credentials_file)
        self.tag_provider = DemoClassifications(settings.layout) if settings.demo else AniList(settings.anilist, store)
        self.kobo_provider = DemoKobo(settings.layout) if settings.demo else Kobo(settings, store)
        self.mu_provider = DemoMangaUpdates(settings.layout) if settings.demo else MangaUpdates(settings, store)

    def _provider(self, provider):
        settings = self.metadata.library.settings
        if not settings.demo:
            self.provider.settings = settings.rakuten
            self.tag_provider.settings = settings.anilist
        if provider == "rakuten_books":
            return self.provider
        if provider == "anilist":
            return self.tag_provider
        if provider == "rakuten_kobo":
            return self.kobo_provider
        if provider == "mangaupdates":
            return self.mu_provider
        raise ConfigError("情報源は rakuten_books / rakuten_kobo / mangaupdates / anilist から選んでください。")

    def status(self, provider="rakuten_books"):
        catalog = self._provider(provider)
        return {"provider": catalog.id, "key": provider, "label": catalog.label,
                "configured": catalog.configured(), "cache_seconds": CACHE_SECONDS}

    def statuses(self):
        return {key: self.status(key) for key in ("rakuten_books", "rakuten_kobo", "mangaupdates", "anilist")}

    def known_titles(self, series_id):
        return known_titles(self.metadata.inspect(series_id))

    def search(self, series_id, *, provider="rakuten_books", **values):
        current = self.metadata.inspect(series_id)
        catalog = self._provider(provider)
        query = anilist_query(**values) if provider == "anilist" else search_query(**values)
        if not catalog.configured():
            raise ConfigError(f"{catalog.label}の設定がありません。設定画面で有効化・認証情報の登録状況を確認してください。")
        key = search_cache_key(catalog.id, query)
        now = time.time()
        with self.store.connection() as connection:
            connection.execute("DELETE FROM metadata_searches WHERE expires<=?", (now,))
            row = connection.execute("SELECT id,data FROM metadata_searches WHERE namespace=? AND series_id=? AND cache_key=? AND expires>? ORDER BY expires DESC LIMIT 1",
                                     (self.store.namespace, series_id, key, now)).fetchone()
        if row:
            return annotate_results(current, {**json.loads(row["data"]), "search_id": row["id"], "cached": True})
        result = catalog.search(query)
        now = time.time()
        result.update(provider=catalog.id, label=catalog.label, retrieved_at=timestamp(),
                      expires_at=datetime.fromtimestamp(now + CACHE_SECONDS, timezone.utc).isoformat(), query=query)
        search_id = uuid4().hex
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO metadata_searches VALUES(?,?,?,?,?,?)", (search_id, self.store.namespace, series_id, key,
                                                                                   now + CACHE_SECONDS, json.dumps(result, ensure_ascii=False)))
            connection.execute("DELETE FROM metadata_searches WHERE namespace=? AND id NOT IN "
                               "(SELECT id FROM metadata_searches WHERE namespace=? ORDER BY expires DESC,id DESC LIMIT ?)",
                               (self.store.namespace, self.store.namespace, MAX_SEARCHES))
        return annotate_results(current, {**result, "search_id": search_id, "cached": False})

    def _candidate(self, series_id, search_id, candidate_id, provider):
        if not isinstance(search_id, str) or not re.fullmatch(r"[0-9a-f]{32}", search_id) or not isinstance(candidate_id, str):
            raise ConfigError("検索結果から候補を選んでください。")
        with self.store.connection() as connection:
            row = connection.execute("SELECT data,cache_key FROM metadata_searches WHERE id=? AND namespace=? AND series_id=? AND expires>?",
                                     (search_id, self.store.namespace, series_id, time.time())).fetchone()
        if row is None:
            raise StateConflict("検索結果が期限切れか、別の作品の候補です。検索し直してください。")
        result = json.loads(row["data"])
        if result["provider"] != self._provider(provider).id:
            raise StateConflict("情報源の設定が変わりました。検索し直してください。")
        if row["cache_key"] != search_cache_key(result["provider"], result.get("query")):
            raise StateConflict("検索結果の形式が変わりました。検索し直してください。")
        candidate = next((item for item in result["candidates"] if item["id"] == candidate_id), None)
        if candidate is None:
            raise ConfigError("選んだ候補が検索結果にありません。")
        return result, candidate

    def preview(self, series_id, *, search_id=None, candidate_id=None, fields=None, unlink=False, provider="rakuten_books"):
        self._provider(provider)
        current = self.metadata.inspect(series_id)
        if type(unlink) is not bool:
            raise ConfigError("unlink は true / false で指定してください。")
        if unlink:
            if search_id is not None or candidate_id is not None or fields is not None:
                raise ConfigError("対応付けの解除と補完は同時に指定できません。")
            if not current["provenances"].get(provider):
                raise StateConflict("解除する対応付けがありません。")
            selected, provenance, match = {}, {}, {}
        else:
            result, candidate = self._candidate(series_id, search_id, candidate_id, provider)
            if not isinstance(fields, dict) or set(fields) - candidate["fields"].keys():
                raise ConfigError("この候補にある項目から、採用する情報を選んでください。")
            selected = validate_fields(fields) if fields else {}
            provenance = {"provider": result["provider"], "item_id": candidate["id"], "url": candidate["url"],
                          "retrieved_at": result["retrieved_at"], "applied_fields": list(selected),
                          "adjusted_fields": [key for key, value in selected.items() if value != candidate["fields"][key]],
                          "identity": candidate_identity(candidate)}
        preview = self.metadata.preview(series_id, selected or {"title": current["series"]["title"]}, refresh=True)
        if not unlink:
            # Compare after the archive/sidecar refresh used by the diff, too.
            match = compare_candidate(self.metadata.inspect(series_id), candidate, result["candidates"])
            provenance["match"] = match
        return {**preview, "provenance": provenance, "previous_provenance": current["provenances"].get(provider, {}), "unlink": unlink,
                "selected_fields": selected, "match": match}

    def save(self, series_id, revision, base_fingerprint, **values):
        # Hold the library edit lock across candidate checks and the atomic edit/link save.
        with self.store.editing():
            preview = self.preview(series_id, **values)
            provenance = preview["provenance"]
            if provenance:
                provenance["reviewed_at"] = timestamp()
            fields = preview["selected_fields"] or {"title": self.metadata.inspect(series_id)["series"]["title"]}
            return self.metadata._save(series_id, fields, revision, base_fingerprint, provenance=provenance,
                                       source_provider=values.get("provider", "rakuten_books"))
