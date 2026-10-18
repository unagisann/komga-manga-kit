"""Differential paper/digital release checks, with explicit identity and date boundaries."""
from datetime import date, datetime, timedelta
import re
import time
from zoneinfo import ZoneInfo

from .authors import author_candidates
from .bibliography import edition_markers
from .catalogs import CatalogError, DemoCatalog, RakutenBooks, search_query
from .config import ConfigError
from .japanese import search_key
from .kobo import DemoKobo, Kobo
from .inventory import number
from .operations import digest
from .state import StateConflict, timestamp

NOISE = re.compile(r"分冊|分売|単話|話売り|合本|セット|公式ガイド|ファンブック|画集|アンソロジー|小説版")


def release_date(value):
    text = str(value or "").strip()
    match = re.fullmatch(r"([0-9]{4})(?:年|[-/])([0-9]{1,2})(?:月|[-/])([0-9]{1,2})日?", text)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def analyze_candidates(row, candidates, today, *, linked=False):
    titles = {search_key(title) for title in (row["title"], *row.get("aliases", []))}
    own_authors = {search_key(item["name"]) for item in author_candidates(row.get("author", ""))}
    editions = set(edition_markers(row["title"]))
    released, upcoming, review = [], [], []
    for item in candidates:
        fields = item["fields"]
        volume = number(item.get("volume"))
        if volume is None or search_key(fields.get("title", "")) not in titles or NOISE.search(item["title"]):
            continue
        authors = {search_key(value["name"]) for value in author_candidates(fields.get("author", ""))}
        day = release_date(item.get("sales_date"))
        evidence = bool(own_authors and authors and own_authors == authors) or linked and not (own_authors and authors and own_authors != authors)
        value = {"id": item["id"], "title": item["title"], "volume": str(volume),
                 "date": day.isoformat() if day else None, "url": item["url"]}
        if not evidence or set(item.get("editions", [])) != editions or day is None:
            review.append(value)
        elif day > today:
            upcoming.append(value)
        else:
            released.append(value)
    return {"latest": max(released, key=lambda item: number(item["volume"]), default=None),
            "upcoming": sorted(upcoming, key=lambda item: (item["date"], number(item["volume"]))), "review": review[:20]}


class NewVolumeService:
    def __init__(self, settings, store, inventory, jobs):
        self.settings, self.store, self.inventory, self.jobs = settings, store, inventory, jobs
        self.providers = {"paper": DemoCatalog(settings.layout) if settings.demo else RakutenBooks(settings.rakuten, store, settings.tools.credentials_file),
                          "digital": DemoKobo(settings.layout) if settings.demo else Kobo(settings, store)}

    def read(self):
        values = self.store.cached_prefix("newvol:row:")
        return sorted((row["data"] for row in values.values()), key=lambda row: (not row.get("new"), row["title"]))

    def submit(self, *, force=False, new_only=False, series_ids=None):
        if type(force) is not bool or type(new_only) is not bool:
            raise ConfigError("更新方法を確認してください。")
        rows = [row for row in self.inventory.cached()["series"] if row["status"] == "ongoing"]
        if series_ids is not None:
            if (not isinstance(series_ids, list) or not 1 <= len(series_ids) <= 5000 or any(not isinstance(value, str) for value in series_ids)
                    or set(series_ids) - {row["id"] for row in rows}):
                raise ConfigError("新刊チェックの対象作品を確認してください。")
            rows = [row for row in rows if row["id"] in series_ids]
        return self.jobs.submit_task("new_volume_check", lambda progress: self.check(rows, force=force, new_only=new_only, progress=progress))

    def search(self, provider, title):
        catalog = self.providers[provider]
        if provider == "paper" and not self.settings.demo:
            catalog.settings = self.settings.rakuten
        if not catalog.configured():
            raise ConfigError("書誌サービスの設定がありません。")
        candidates, incomplete = [], False
        for page in range(1, 4):
            query = {**search_query(title=title, page=page), "sort": "-releaseDate"}
            for attempt in range(2):
                try:
                    result = catalog.search(query)
                    break
                except CatalogError as error:
                    if attempt or not error.retry_after or error.retry_after > 3:
                        raise
                    time.sleep(error.retry_after + .01)
            candidates.extend(result["candidates"])
            pages = result.get("page_count") or 1
            if page >= pages:
                break
            incomplete = page == 3
        return candidates, incomplete

    def check(self, rows, *, force=False, new_only=False, progress=lambda *_: None, today=None):
        today = today or datetime.now(ZoneInfo("Asia/Tokyo")).date()
        if not self.settings.demo and not self.settings.rakuten.enabled and not self.settings.tools.kobo_enabled:
            raise ConfigError("新刊を確認する楽天Booksまたは楽天Koboを有効にしてください。")
        edits = self.store.edits()
        checked, skipped, failures, limited = 0, 0, 0, False
        for index, row in enumerate(rows):
            key = "newvol:row:" + row["id"]
            previous = self.store.cached(key)
            prior = previous["data"] if previous else None
            maximum = max((number(book.get("volume")) or 0 for book in row["books"]), default=0)
            policy = self.store.cached("newvol:policy:" + row["id"])
            digital = policy["data"].get("digital", True) if policy else True
            link = edits.get(row.get("local_id"), {}).get("provenance", {})
            from .provenance import source_links
            links = source_links(link)
            identity = digest({**{key: row.get(key) for key in ("title", "author", "aliases")}, "digital": digital,
                               "paper_enabled": self.settings.demo or self.settings.rakuten.enabled,
                               "digital_enabled": self.settings.demo or self.settings.tools.kobo_enabled,
                               "links": {key: value.get("item_id") for key, value in links.items() if key in {"rakuten_books", "rakuten_kobo"}}})
            if limited:
                failures += 1
                result = {**(prior or {"series_id": row["id"], "title": row["title"], "latest": None, "sources": {}}),
                          "local_max": str(maximum), "errors": ["取得先の利用制限により、この作品は未更新です。"], "stale": True}
                result["new"] = bool(number(result.get("latest")) and number(result["latest"]) > maximum)
                self.store.cache(key, result)
                progress(index + 1, len(rows))
                continue
            if prior and not force and not prior.get("errors") and prior.get("identity") == identity:
                next_day = min((release["date"] for source in prior.get("sources", {}).values() for release in source.get("upcoming", []) if release.get("date")), default=None)
                last = release_date(prior.get("checked_day"))
                if new_only or last and today < last + timedelta(days=7) and (next_day is None or next_day > today.isoformat()):
                    updated = {**prior, "local_max": str(maximum), "new": bool(number(prior.get("latest")) and number(prior["latest"]) > maximum)}
                    self.store.cache(key, updated)
                    skipped += 1; progress(index + 1, len(rows)); continue
            result = {"series_id": row["id"], "title": row["title"], "local_max": str(maximum), "latest": None,
                      "sources": {}, "errors": [], "new": False, "checked_day": today.isoformat(), "updated": timestamp()}
            result["identity"] = identity
            for source in ("paper", "digital"):
                if source == "paper" and not self.settings.demo and not self.settings.rakuten.enabled:
                    continue
                if source == "digital" and (not digital or not self.settings.demo and not self.settings.tools.kobo_enabled):
                    continue
                try:
                    if self.settings.demo:
                        latest = maximum + 1
                        result["sources"][source] = {"latest": {"id": "demo", "title": row["title"], "volume": str(latest),
                            "date": today.isoformat(), "url": ""}, "upcoming": [], "review": [], "incomplete": False}
                    else:
                        items, incomplete = self.search(source, row["title"])
                        linked = ("rakuten_books" if source == "paper" else "rakuten_kobo") in links
                        result["sources"][source] = {**analyze_candidates(row, items, today, linked=linked), "incomplete": incomplete}
                except (CatalogError, ConfigError) as error:
                    result["errors"].append(str(error))
                    if isinstance(error, CatalogError) and error.retry_after:
                        limited = error.retry_after > 3
            latest = [source["latest"] for source in result["sources"].values() if source.get("latest")]
            if latest:
                result["latest"] = str(max(number(item["volume"]) for item in latest))
                result["new"] = number(result["latest"]) > maximum
            if result["errors"] and not latest:
                failures += 1
                if prior:
                    result = {**prior, "errors": result["errors"], "local_max": str(maximum), "stale": True,
                              "new": bool(number(prior.get("latest")) and number(prior["latest"]) > maximum)}
            self.store.cache(key, result)
            checked += 1
            progress(index + 1, len(rows))
        from .notifications import Notifications
        try:
            notification = Notifications(self.settings, self.store).send(self.read())
        except (ConfigError, StateConflict) as error:
            notification = {"sent": 0, "error": str(error)}
        self.store.cache("newvol:notification:last", {**notification, "updated": timestamp()})
        return {"checked": checked, "skipped": skipped, "errors": failures, "limited": limited, "notification": notification,
                "new": sum(row.get("new", False) for row in self.read())}

    def policy(self, series_id, digital):
        self.inventory.find(series_id)
        if type(digital) is not bool:
            raise ConfigError("電子版の対象設定を確認してください。")
        self.store.cache("newvol:policy:" + series_id, {"digital": digital})
