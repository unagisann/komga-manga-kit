"""Per-account reading views through each reader's own Komga API credentials."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .config import ConfigError
from .inventory import page_items
from .komga import KomgaClient, KomgaError
from .state import timestamp


def progress_state(books, today=None):
    today = today or datetime.now(timezone.utc)
    started = [book for book in books if book.get("progress")]
    if not started:
        return None
    if all((book.get("progress") or {}).get("completed") for book in books):
        return "completed"
    dates = []
    for book in started:
        text = book["progress"].get("lastModified") or book["progress"].get("readDate")
        try:
            day = datetime.fromisoformat(text.replace("Z", "+00:00"))
            dates.append(day.replace(tzinfo=timezone.utc) if day.tzinfo is None else day)
        except (ValueError, TypeError, AttributeError):
            pass
    return "paused" if dates and max(dates) < today - timedelta(days=60) else "reading"


class ReadingService:
    def __init__(self, settings, store, inventory):
        self.settings, self.store, self.inventory = settings, store, inventory

    def accounts(self):
        return list(self.settings.tools.reading_accounts) or [{"name": "自分", "api_key_env": self.settings.api_key_env}]

    def read(self, name):
        if name not in {item["name"] for item in self.accounts()}:
            raise ConfigError("読書状況のアカウントを選んでください。")
        value = self.store.cached("reading:" + name)
        return value["data"] if value else None

    def refresh(self, progress=lambda *_: None):
        self.settings.tools.validate()
        settings = getattr(self.settings, "_current", self.settings)
        rows = self.inventory.cached()["series"]
        errors, updated = [], 0
        for index, account in enumerate(self.accounts()):
            try:
                if self.settings.demo:
                    entries = [{"series_id": row["id"], "title": row["title"], "author": row["author"], "state": "reading",
                                "books": [{**book, "progress": {"completed": False, "page": 1}} for book in row["books"][:1]]} for row in rows[:4]]
                else:
                    client = KomgaClient(replace(settings, api_key_env=account["api_key_env"]))
                    books = page_items(client, "/api/v1/books")
                    grouped = {}
                    native = {row["komga_id"]: row for row in rows if row.get("komga_id")}
                    local_books = {book.get("komga_id"): book for row in rows for book in row["books"] if book.get("komga_id")}
                    for book in books:
                        if book.get("deleted") or book.get("seriesId") not in native:
                            continue
                        grouped.setdefault(book["seriesId"], []).append({"id": local_books.get(book["id"], {}).get("id"),
                            "komga_id": book["id"], "volume": str(book.get("metadata", {}).get("number") or ""),
                            "pages": book.get("media", {}).get("pagesCount"), "progress": book.get("readProgress") or {},
                            "url": self.inventory.read_url(native[book["seriesId"]], {"komga_id": book["id"]})})
                    entries = []
                    for identifier, values in grouped.items():
                        state = progress_state(values)
                        if state:
                            entries.append({"series_id": native[identifier]["id"], "title": native[identifier]["title"],
                                            "author": native[identifier]["author"], "state": state, "books": values})
                self.store.cache("reading:" + account["name"], {"account": account["name"], "entries": entries,
                                                              "updated": timestamp(), "demo": self.settings.demo})
                updated += 1
            except (KomgaError, ConfigError) as error:
                errors.append({"account": account["name"], "error": str(error)})
            progress(index + 1, len(self.accounts()))
        self.store.cache("reading:last", {"updated": timestamp(), "errors": errors})
        return {"accounts": updated, "errors": errors}
