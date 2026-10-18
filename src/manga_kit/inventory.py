"""Cached library/Komga identity for portal, discovery, shelves and reading tools."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import random
from urllib.parse import quote

from .authors import author_candidates
from .bibliography import edition_title
from .config import ConfigError
from .japanese import search_key
from .komga import KomgaError
from .paths import map_komga_path
from .provenance import source_links
from .state import timestamp


def number(value):
    try:
        value = Decimal(str(value))
        return value if value.is_finite() and 0 <= value <= 99999 else None
    except (InvalidOperation, TypeError):
        return None


def page_items(client, endpoint, **params):
    items, page = [], 0
    while page < 1000:
        result = client.get(endpoint, page=page, size=100, **params)
        if not isinstance(result, dict) or not isinstance(result.get("content"), list):
            raise KomgaError("Komgaの一覧の形式を確認できませんでした。")
        items.extend(result["content"])
        if result.get("last") is True or len(result["content"]) < 100:
            return items
        page += 1
    raise ConfigError("Komgaの一覧が上限を超えています。対象を絞ってください。")


class InventoryService:
    def __init__(self, settings, library, store, client):
        self.settings, self.library, self.store, self.client = settings, library, store, client
        self._snapshot, self._revision = None, -1

    def local(self):
        scanned = self.library.scan()
        edits, values = self.store.edits(), []
        previous = self.store.cached("inventory")
        prior = {book["path"]: book.get("created", "") for row in previous["data"]["series"] for book in row["books"]} if previous else {}
        for series in scanned.series:
            links = source_links(edits.get(series.id, {}).get("provenance", {}))
            identities = [link.get("identity", {}) for link in links.values()]
            facts = self.store.cached("facts:" + series.id)
            details = {**next((identity.get("facts", {}) for identity in identities if identity.get("facts")), {}),
                       **(facts["data"] if facts else {})}
            row = {"id": series.id, "local_id": series.id, "komga_id": None,
                   "title": series.title, "reading": series.reading, "author": series.author, "publisher": series.publisher,
                   "status": series.status, "summary": series.summary, "genres": series.genres, "tags": series.tags,
                   "row": series.row, "label": details.get("label") or next((identity.get("label") for identity in identities if identity.get("label")), ""),
                   "aliases": list(dict.fromkeys(title for identity in identities for title in identity.get("titles", []))),
                   "facts": details, "books": []}
            for book in series.books:
                row["books"].append({"id": hashlib.sha256(book.path.encode()).hexdigest()[:24], "komga_id": None,
                                     "path": book.path, "volume": book.volume, "size": book.size, "pages": None,
                                     "created": prior.get(book.path, ""), "progress": None})
            row["path"] = series.path
            values.append(row)
        return values, scanned

    def cached(self):
        revision = self.store.cached_revision("inventory")
        if self._snapshot is not None and revision == self._revision:
            return self._snapshot
        value = self.store.cached("inventory")
        if value:
            self._snapshot, self._revision = value["data"], value["revision"]
            return self._snapshot
        rows, scanned = self.local()
        self._snapshot = {"series": rows, "updated": None, "warnings": scanned.warnings, "truncated": scanned.truncated}
        self._revision = self.store.cache("inventory", self._snapshot)
        return self._snapshot

    def refresh(self, progress=lambda *_: None):
        self.library.scan(refresh=True, reuse_metadata=True)
        rows, scanned = self.local()
        if scanned.truncated or scanned.warnings:
            raise ConfigError("蔵書の全件を確認できませんでした。保存先・上限を確認してください。")
        by_path = {row["path"]: row for row in rows}
        native_series, native_books = [], []
        if self.settings.komga_url and not self.settings.demo:
            native_series = page_items(self.client, "/api/v1/series")
            native_books = page_items(self.client, "/api/v1/books")
        grouped = {}
        for book in native_books:
            if not book.get("deleted"):
                grouped.setdefault(book["seriesId"], []).append(book)
        now = timestamp()
        for index, series in enumerate(native_series):
            if series.get("deleted"):
                continue
            try:
                path = map_komga_path(series.get("url", ""), self.settings.path_mappings, self.settings.library_root)
            except ConfigError:
                path = None
            row = by_path.get(path)
            metadata = series.get("metadata", {})
            if row is None:
                row = {"id": "komga-" + series["id"], "local_id": None, "komga_id": series["id"],
                       "title": metadata.get("title") or series.get("name", ""), "reading": metadata.get("titleSort", ""),
                       "author": "", "publisher": metadata.get("publisher", ""),
                       "status": "completed" if metadata.get("status") == "ENDED" else "ongoing",
                       "summary": metadata.get("summary", ""), "genres": metadata.get("genres", []),
                       "tags": metadata.get("tags", []), "row": "その他", "label": "", "aliases": [], "facts": {}, "books": [], "path": None}
                rows.append(row)
            row["komga_id"] = series["id"]
            local_books = {book["path"]: book for book in row["books"]}
            if not row["author"]:
                names = [author["name"] for book in grouped.get(series["id"], []) for author in book.get("metadata", {}).get("authors", []) if author.get("name")]
                row["author"] = "・".join(dict.fromkeys(names))
            for book in grouped.get(series["id"], []):
                try:
                    relative = map_komga_path(book.get("url", ""), self.settings.path_mappings, self.settings.library_root)
                except ConfigError:
                    relative = None
                item = local_books.get(relative)
                if item is None:
                    item = {"id": "komga-" + book["id"], "path": None, "size": book.get("sizeBytes", 0), "komga_id": book["id"]}
                    row["books"].append(item)
                item.update(komga_id=book["id"], volume=str(book.get("metadata", {}).get("number") or ""),
                            pages=book.get("media", {}).get("pagesCount"), created=book.get("created") or now,
                            progress=book.get("readProgress"))
            progress(index + 1, len(native_series))
        for row in rows:
            for book in row["books"]:
                if not book.get("created"):
                    book["created"] = now
            row["books"].sort(key=lambda book: (number(book.get("volume")) is None, number(book.get("volume")) or 0, book["id"]))
        result = {"series": rows, "updated": now, "recent_source": "komga" if native_series else "observed",
                  "warnings": [], "truncated": False}
        self.store.cache("inventory", result)
        return {"series": len(rows), "books": sum(len(row["books"]) for row in rows), "updated": now}

    def find(self, identifier):
        row = next((item for item in self.cached()["series"] if item["id"] == identifier), None)
        if row is None:
            local, _ = self.local()
            row = next((item for item in local if item["id"] == identifier), None)
            if row is None:
                raise ConfigError("作品が見つかりません。蔵書情報を更新してください。")
            value = self.store.cached("inventory")
            if value:
                value["data"]["series"].append(row)
                self.store.cache("inventory", value["data"], revision=value["revision"])
        return row

    def update_facts(self, identifier, facts):
        cached = self.store.cached("inventory")
        if cached:
            for row in cached["data"]["series"]:
                if row["id"] == identifier:
                    row["facts"] = {**row.get("facts", {}), **facts}
                    row["label"] = facts.get("label", row.get("label", ""))
            self.store.cache("inventory", cached["data"], revision=cached["revision"])

    def match(self, title, *, aliases=(), author="", source="", entry_id=""):
        rows = self.cached()["series"]
        override = self.store.cached("match:" + source + ":" + str(entry_id)) if source and entry_id else None
        if override:
            identifier = override["data"].get("series_id")
            matches = [row for row in rows if row["id"] == identifier]
            return self.matched(matches[0], "手動確認") if len(matches) == 1 else {"owned": False, "state": "unowned", "series_id": None}
        keys = {search_key(edition_title(value)) for value in (title, *aliases) if value}
        candidates = [row for row in rows if keys & {search_key(edition_title(value)) for value in (row["title"], *row["aliases"])}]
        if author:
            names = {search_key(item["name"]) for item in author_candidates(author)}
            candidates = [row for row in candidates if not row["author"] or names & {search_key(item["name"]) for item in author_candidates(row["author"])}]
        if len(candidates) == 1 and min((len(key) for key in keys), default=0) > 2:
            return self.matched(candidates[0], "作品名・確認済み別名")
        return {"owned": False, "state": "review" if candidates else "unowned", "series_id": None,
                "candidates": [{"id": row["id"], "title": row["title"], "author": row["author"]} for row in candidates[:20]]}

    @staticmethod
    def matched(row, reason):
        return {"owned": True, "state": "owned", "series_id": row["id"], "komga_id": row["komga_id"], "matched_title": row["title"], "match_reason": reason}

    def match_override(self, source, entry_id, identifier):
        if identifier is not None:
            self.find(identifier)
        self.store.cache("match:" + source + ":" + str(entry_id), {"series_id": identifier})

    def read_url(self, row, book=None):
        base = (self.settings.tools.public_komga_url or self.settings.komga_url).rstrip("/")
        if base and book and book.get("komga_id"):
            return base + "/book/" + quote(book["komga_id"], safe="")
        if base and row.get("komga_id"):
            return base + "/series/" + quote(row["komga_id"], safe="")
        return "/library?q=" + quote(row["title"], safe="")

    def recent(self, limit=12):
        values = []
        for row in self.cached()["series"]:
            if row["status"] != "ongoing" or not row["books"]:
                continue
            latest = max(row["books"], key=lambda book: (number(book.get("volume")) or 0, book.get("created") or ""))
            created = max(book.get("created") or "" for book in row["books"])
            if created:
                values.append({"series": row, "book": latest, "created": created, "url": self.read_url(row, latest)})
        return sorted(values, key=lambda item: item["created"], reverse=True)[:limit]

    def binge(self, limit=12):
        rows = [row for row in self.cached()["series"] if row["status"] == "completed"]
        return random.sample(rows, min(limit, len(rows)))
