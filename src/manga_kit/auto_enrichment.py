"""Collect missing-field proposals in the background, then save a reviewed batch."""
import hashlib
import json
import time

from .authors import author_candidates
from .catalogs import CatalogError
from .config import ConfigError
from .matching import candidate_identity
from .metadata import fingerprint
from .provenance import source_links, stored_links
from .state import StateConflict, timestamp

PROVIDER_FIELDS = {"rakuten_books": ("reading", "author", "publisher", "summary", "genres"),
                   "rakuten_kobo": ("reading", "author", "publisher", "summary", "genres"),
                   "mangaupdates": ("author", "publisher", "summary", "genres"),
                   "anilist": ("genres", "tags")}
MAX_BATCH_SERIES = 60


def missing_fields(series, provider):
    return [field for field in PROVIDER_FIELDS[provider] if not series.get(field)]


def _authors(text):
    return {item["name"].casefold() for item in author_candidates(text)}


def automatic_candidate(current, result, provider):
    """A title plus author/publisher evidence; uncertain matches stay for review."""
    if result.get("has_next") or result.get("total", 0) is not None and result["total"] > len(result["candidates"]):
        return None
    candidates = []
    linked_id = current["provenances"].get(provider, {}).get("item_id")
    for candidate in result["candidates"]:
        match = candidate["match"]
        if not any(text in match["evidence"] for text in ("作品名が一致", "確認済みの別名が一致")):
            continue
        # Missing author evidence is allowed only with another independent anchor.
        if any(not warning.startswith("比較できる著者情報") for warning in match["warnings"]):
            continue
        anchored = (candidate["id"] == linked_id or "著者が一致" in match["evidence"]
                    or "出版社が一致" in match["evidence"])
        # AniList has no author/publisher in its classification response. A single
        # exact native/alternate title is still shown for the batch diff review.
        if provider == "anilist" and len(result["candidates"]) == 1:
            anchored = True
        if anchored:
            candidates.append(candidate)
    if not candidates:
        return None
    identities = {(tuple(sorted(_authors(item["fields"].get("author", "")))), item["fields"].get("publisher", ""),
                   item.get("label", ""), tuple(item.get("editions", []))) for item in candidates}
    if len(identities) != 1:
        return None
    first_volumes = [item for item in candidates if item.get("volume") == "1"]
    if len(first_volumes) == 1:
        return first_volumes[0]
    return candidates[0] if len(candidates) == 1 else None


class AutoEnrichmentService:
    def __init__(self, enrichment, jobs):
        self.enrichment, self.jobs = enrichment, jobs
        self.metadata, self.store = enrichment.metadata, enrichment.store

    def submit(self, series_ids, provider="rakuten_books", link_only=False):
        if type(link_only) is not bool:
            raise ConfigError("対応付けの構築方法を確認してください。")
        if not isinstance(provider, str) or provider not in PROVIDER_FIELDS:
            raise ConfigError("補完する情報源を選んでください。")
        if (not isinstance(series_ids, list) or not 1 <= len(series_ids) <= MAX_BATCH_SERIES
                or any(not isinstance(value, str) for value in series_ids) or len(set(series_ids)) != len(series_ids)):
            raise ConfigError("補完する作品を1〜60作品、重複せず選んでください。")
        catalog = self.enrichment._provider(provider)
        if not catalog.configured():
            raise ConfigError("補完サービスが未設定です。管理の設定画面で有効にしてください。")
        actual = {series.id for series in self.metadata.library.scan().series}
        if set(series_ids) - actual:
            raise ConfigError("選んだ作品が見つかりません。作品一覧を更新してください。")
        return self.jobs.submit_task("enrichment", lambda progress: self.collect(series_ids, provider, progress, link_only=link_only),
                                     key=[provider, sorted(series_ids), link_only])

    def _pace(self, provider):
        with self.store.connection() as connection:
            row = connection.execute("SELECT next_allowed FROM metadata_requests WHERE provider=?",
                                     (self.enrichment._provider(provider).id,)).fetchone()
        delay = max(0, row[0] - time.time()) if row else 0
        if delay > 3:
            raise CatalogError("補完サービスが待機中です。時間を空けて再実行してください。", retry_after=int(delay) + 1)
        if delay:
            time.sleep(delay + 0.01)

    def collect(self, series_ids, provider, progress, *, link_only=False):
        rows, paused = [], False
        for index, series_id in enumerate(series_ids):
            current = self.metadata.inspect(series_id)
            row = {"series_id": series_id, "title": current["series"]["title"], "state": "unchanged",
                   "missing": missing_fields(current["series"], provider), "message": "この情報源で補う空欄はありません。"}
            if paused:
                row.update(state="error", message="サービスの制限により検索を中止しました。時間を空けて再実行してください。")
            elif row["missing"] or link_only:
                try:
                    self._pace(provider)
                    result = self.enrichment.search(series_id, provider=provider, title=current["series"]["title"])
                    row["search_id"] = result["search_id"]
                    candidate = automatic_candidate(current, result, provider)
                    if not result["candidates"]:
                        row.update(state="missing", message="候補が見つかりませんでした。別名で探すか、管理で手動修正できます。")
                    elif candidate is None:
                        row.update(state="review", message="作品や版を一つに絞れませんでした。候補を確認してください。")
                    else:
                        fields = {key: value for key, value in candidate["fields"].items() if key in row["missing"] and value}
                        if link_only:
                            fields = {}
                        if candidate.get("volume") not in ("1", None) or candidate.get("volume") is None and current["series"]["book_count"] > 1:
                            fields.pop("summary", None)
                        if fields or link_only:
                            preview = self.metadata.preview(series_id, fields or {"title": current["series"]["title"]})
                            expected = self.store.edits().get(series_id, {"fields": {}, "revision": 0})
                            if preview["revision"] != current["revision"] or preview["base_fingerprint"] != current["base_fingerprint"]:
                                raise StateConflict("取得中に作品情報が変わりました。再実行してください。")
                            if expected["revision"] != preview["revision"]:
                                raise StateConflict("取得中に作品情報が変わりました。再実行してください。")
                            row.update(state="ready", message="対応付けを保存できます。" if link_only else "不足項目を補完できます。", fields=fields, preview=preview,
                                       candidate_id=candidate["id"], candidate_title=candidate["title"], url=candidate["url"],
                                       match=candidate["match"], expected=expected)
                        else:
                            row.update(state="missing", message="この候補には不足項目の情報がありません。別の候補か手動修正を確認できます。")
                except (CatalogError, ConfigError, StateConflict) as error:
                    paused = isinstance(error, CatalogError) and bool(error.retry_after)
                    row.update(state="error", message=str(error))
            rows.append(row)
            progress(index + 1, len(series_ids))
        result = {"provider": provider, "label": self.enrichment.status(provider)["label"], "rows": rows, "link_only": link_only,
                  "counts": {state: sum(row["state"] == state for row in rows)
                             for state in ("ready", "review", "missing", "unchanged", "error")}}
        result["digest"] = hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return result

    def apply(self, job_id, confirm):
        job = self.store.job(job_id, include_result=True)
        if job is None or job["kind"] != "enrichment":
            raise ConfigError("自動補完の結果を選んでください。")
        if job["status"] != "succeeded" or not isinstance(confirm, str) or confirm != job["result"]["digest"]:
            raise StateConflict("完了した補完結果を確認してから保存してください。")
        result = job["result"]
        with self.store.editing():
            saved = self.store.operation_edits(job_id, "enrichment")
            if saved is not None:
                return {"saved": len(saved), "already_saved": True}
            bases = {series.id: series for series in self.metadata.library.scan(
                refresh=True, edited=False, reuse_metadata=True).series}
            changes = []
            for row in result["rows"]:
                if row["state"] != "ready":
                    continue
                base = bases.get(row["series_id"])
                if base is None or fingerprint(base) != row["preview"]["base_fingerprint"]:
                    raise StateConflict("候補の取得後に書庫の情報が変わりました。上書きせず中止しました。再実行してください。")
                source, candidate = self.enrichment._candidate(row["series_id"], row["search_id"], row["candidate_id"], result["provider"])
                link = {"provider": source["provider"], "item_id": candidate["id"], "url": candidate["url"],
                        "retrieved_at": source["retrieved_at"], "reviewed_at": timestamp(), "applied_fields": list(row["fields"]),
                        "adjusted_fields": [], "identity": candidate_identity(candidate), "match": row["match"]}
                links = source_links(row["expected"].get("provenance", {}))
                for provider, existing in links.items():
                    if provider != result["provider"]:
                        for key in ("applied_fields", "adjusted_fields"):
                            existing[key] = [field for field in existing.get(key, []) if field not in row["fields"]]
                links[result["provider"]] = link
                changes.append({"series_id": row["series_id"], "expected": row["expected"],
                                "edit": {"fields": row["preview"]["fields"], "provenance": stored_links(links)}})
            if not changes:
                raise ConfigError("まとめて保存できる補完候補がありません。")
            saved = self.store.apply_operation_edits(job_id, "enrichment", changes, event_kind="metadata_enrichment")
        return {"saved": len(saved), "already_saved": False}
