"""Presentation of public recommendations, with daily picks and article shelves."""
from datetime import datetime
from email.utils import parsedate_to_datetime
import hashlib
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .japanese import search_key
from .sources import plain_text, safe_url


def article_kind(title, url):
    path = urlsplit(url or "").path
    if re.search(r"comic_completed_\d{6}", path) or re.search(r"完結した(?:マンガ|漫画)", title):
        return "completed"
    if "recommend-like-" in path or "好きに刺さる" in title:
        return "themes"
    return ""


def article_date(value):
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            moment = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return ""
    if moment.tzinfo:
        moment = moment.astimezone(ZoneInfo("Asia/Tokyo"))
    return moment.date().isoformat()


def discovery_view(data, inventory, today=None):
    today = today or datetime.now(ZoneInfo("Asia/Tokyo")).date()
    rows = {row["id"]: row for row in inventory.cached()["series"]} if data else {}
    shelves = {"themes": {}, "completed": {}}
    entries = []
    for entry in (data or {}).get("entries", []):
        url = safe_url(entry.get("url"), {"booklive.jp"})
        title = plain_text(entry.get("article_title"), 200) or "紹介された作品"
        kind = entry.get("article_kind") or article_kind(title, url) or "themes"
        kind = kind if kind in shelves else "themes"
        identifier = hashlib.sha256((url or title).encode()).hexdigest()[:16]
        article = shelves[kind].setdefault(identifier, {
            "id": identifier, "title": title, "url": url,
            "published": article_date(entry.get("published")), "entries": [],
        })
        row = rows.get(entry.get("series_id")) if entry.get("owned") else None
        read_url = inventory.read_url(row) if row and row.get("komga_id") else ""
        item = {**entry, "url": url, "article_title": title,
                "product_url": safe_url(entry.get("product_url"), {"booklive.jp"}),
                "image": safe_url(entry.get("image"), {"res.booklive.jp"}),
                "book": next(iter((row or {}).get("books") or []), None),
                "read_url": read_url if read_url.startswith(("http://", "https://")) else ""}
        article["entries"].append(item)
        entries.append(item)

    # The same work can occur in several articles; recommend it only once per day.
    unique = {}
    for item in entries:
        identity = item.get("series_id") if item.get("owned") else None
        identity = identity or search_key(item["title"]) + ":" + search_key(item.get("author", ""))
        unique.setdefault(identity, item)
    candidates = sorted(unique.values(), key=lambda item: hashlib.sha256(
        (today.isoformat() + item["id"]).encode()).hexdigest())
    owned = [item for item in candidates if item.get("owned")]
    missing = [item for item in candidates if not item.get("owned")]
    daily = owned[:3] + missing[:2]
    if len(daily) < 5:
        daily += [item for item in candidates if item not in daily][:5 - len(daily)]
    return {"entries": entries, "daily": daily, "today": today,
            "daily_owned": sum(bool(item.get("owned")) for item in daily),
            "owned_count": sum(bool(item.get("owned")) for item in entries),
            "groups": {key: sorted(articles.values(), key=lambda article: article["published"], reverse=True)
                       for key, articles in shelves.items()}}
