"""Explicit anime, award and discovery updates with library-scoped cached results."""
from datetime import datetime
from html.parser import HTMLParser
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from defusedxml.ElementTree import fromstring

from .catalogs import CatalogError
from .config import ConfigError
from .sources import PublicHTTP, plain_text, safe_url
from .state import timestamp

SEASONS = ("WINTER", "SPRING", "SUMMER", "FALL")
SEASON_LABELS = ("冬", "春", "夏", "秋")
DISCOVERY_FEEDS = {"manba": "https://magazine.manba.co.jp/feed/", "natalie": "https://natalie.mu/comic/feed/news",
                   "curated": "https://booklive.jp/bkmr/feed"}
CATEGORIES = {"comics": "コミックス部門", "web": "Webマンガ部門"}
ANIME_QUERY = """query KitSeason($season:MediaSeason,$year:Int,$status:MediaStatus,$page:Int) {
  Page(page:$page,perPage:50) { pageInfo { hasNextPage }
    media(type:ANIME,season:$season,seasonYear:$year,status:$status,sort:POPULARITY_DESC) {
      id title { native romaji english } source format status season seasonYear episodes
      coverImage { large } siteUrl popularity
      relations { edges { relationType node { type title { native romaji } } } }
    }
  }
}"""


def season(today=None):
    today = today or datetime.now(ZoneInfo("Asia/Tokyo")).date()
    index = (today.month - 1) // 3
    return {"season": SEASONS[index], "year": today.year, "label": f"{today.year}年{SEASON_LABELS[index]}"}


def feed_entries(content):
    try:
        root = fromstring(content, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        return root.findall(".//{*}item") or root.findall("{*}entry")
    except Exception:
        raise CatalogError("RSSの形式を確認できませんでした。前回の結果は保持しています。") from None


def entry_url(entry):
    link = entry.find("{*}link")
    return safe_url((link.get("href") or link.text or "").strip()) if link is not None else ""


def parse_rss(content):
    result = []
    for entry in feed_entries(content)[:30]:
        title = plain_text(entry.findtext("{*}title"), 200)
        url = entry_url(entry)
        if title and url:
            result.append({"id": hashlib.sha256(url.encode()).hexdigest()[:20], "title": title, "url": url,
                           "original_titles": list(dict.fromkeys(re.findall(r"[『「]([^』」]{2,100})[』」]", title))),
                           "published": plain_text(entry.findtext("{*}pubDate") or entry.findtext("{*}published"), 80)})
    if not result:
        raise CatalogError("RSSに紹介記事がありません。前回の結果は保持しています。")
    return result


class Introductions(HTMLParser):
    def __init__(self, url):
        super().__init__(); self.url = url; self.items = []; self.active = None; self.heading = False; self.hidden = 0

    def finish(self):
        if self.active and self.active.get("product_url"):
            title = plain_text(re.sub(r"\s+全[0-9０-９]+巻\s*$", "", "".join(self.active["heading"])), 160)
            if title:
                lines = [plain_text(line, 500) for line in "".join(self.active["text"]).splitlines()]
                author = next((match[1].strip() for line in lines if (match := re.fullmatch(r"著者[：:]\s*(.{1,80})", line))), "")
                teasers = [line for line in lines if len(line) >= 30 and not re.search(r"著者[：:]|試し読み|こちら|立ち読み|購入|配信中", line)]
                self.items.append({"id": hashlib.sha256((self.url + title).encode()).hexdigest()[:20],
                                   "title": title, "author": author,
                                   "url": self.url, "product_url": self.active["product_url"],
                                   "image": self.active.get("image", ""), "teaser": " ".join(teasers)[:110]})

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        values = dict(attrs)
        if tag == "h2":
            self.finish(); self.active = {"heading": [], "text": [], "product_url": ""}; self.heading = True
        if self.active:
            if tag in {"br", "p", "div"}:
                self.active["text"].append("\n")
            if tag == "a" and "/product/" in values.get("href", ""):
                self.active["product_url"] = safe_url(values["href"], {"booklive.jp"})
            if tag == "img":
                self.active["image"] = safe_url(values.get("src", ""), {"res.booklive.jp"})

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if self.active and tag in {"p", "div"}:
            self.active["text"].append("\n")
        if tag == "h2":
            self.heading = False

    def handle_data(self, value):
        if self.active and not self.hidden:
            self.active["heading" if self.heading else "text"].append(value)


def parse_curated(content):
    result = []
    for entry in feed_entries(content)[:30]:
        url = entry_url(entry)
        if not safe_url(url, {"booklive.jp"}):
            continue
        body = entry.findtext("{*}encoded") or entry.findtext("{*}content") or entry.findtext("{*}description") or ""
        parser = Introductions(url)
        parser.feed(body[:200000]); parser.finish()
        if len(parser.items) >= 3:
            result.extend(parser.items)
    unique = {item["id"]: item for item in result}
    if not unique:
        raise CatalogError("作品紹介記事を確認できませんでした。前回の結果は保持しています。")
    return list(unique.values())[:300]


def parse_ranking(content):
    match = re.search(rb'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', content, re.S)
    try:
        data = json.loads(match[1])["props"]["pageProps"]["data"]
        items = data["entries"]
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError
        values = []
        for index, item in enumerate(items):
            title = plain_text(item.get("title"), 200)
            rank = int(item.get("rank") or index + 1)
            if not title or not 1 <= rank <= 1000:
                raise ValueError
            values.append({"id": str(item.get("id") or rank), "title": title, "rank": rank,
                           "author": plain_text(item.get("author"), 200), "award": plain_text(item.get("award"), 100),
                           "image": safe_url(item.get("thumbnail"), {"img.tsugimanga.jp"}), "url": ""})
        return sorted(values, key=lambda item: item["rank"])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise CatalogError("受賞作品の形式を確認できませんでした。前回の結果は保持しています。") from None


class FeedsService:
    def __init__(self, settings, store, inventory, jobs):
        self.settings, self.store, self.inventory, self.jobs = settings, store, inventory, jobs
        self.http = PublicHTTP(store)

    @staticmethod
    def key(kind, year=None, category=None):
        if kind not in {"anime", "ranking", "manba", "natalie", "curated", "rakuten"}:
            raise ConfigError("取得する情報を選んでください。")
        if kind == "ranking":
            current_year = datetime.now(ZoneInfo("Asia/Tokyo")).year
            if type(year) is not int or not 2015 <= year <= current_year or not isinstance(category, str) or category not in CATEGORIES:
                raise ConfigError("ランキングの年と部門を確認してください。")
            return f"feed:ranking:{year}:{category}"
        return "feed:" + kind

    def read(self, kind, year=None, category=None):
        key = self.key(kind, year, category)
        value = self.store.cached(key)
        data = value["data"] if value else self.demo(kind, year, category) if self.settings.demo else None
        if data:
            data = {**data, "entries": [self.annotate(item, key) for item in data["entries"]]}
            data["owned_count"] = sum(item["owned"] for item in data["entries"])
            if kind == "anime":
                data["stale"] = (data.get("year"), data.get("season")) != (season()["year"], season()["season"])
                data["entries"].sort(key=lambda item: not item.get("owned"))
        return data

    def annotate(self, item, source):
        matched = self.inventory.match(item["title"], aliases=item.get("original_titles", []), author=item.get("author", ""), source=source, entry_id=item["id"])
        return {**item, **matched}

    def submit(self, kind, year=None, category=None):
        self.key(kind, year, category)
        return self.jobs.submit_task("source_refresh", lambda progress: self.refresh(kind, year, category, progress))

    def refresh(self, kind, year=None, category=None, progress=lambda *_: None):
        key = self.key(kind, year, category)
        enabled = self.settings.tools.anime_enabled if kind == "anime" else self.settings.tools.rankings_enabled if kind == "ranking" else self.settings.tools.discovery_enabled
        if not self.settings.demo and not enabled:
            raise ConfigError("この情報源を設定で有効にしてから更新してください。")
        if self.settings.demo:
            result = self.demo(kind, year, category)
        elif kind == "anime":
            result = self.anime(progress)
        elif kind == "ranking":
            url = f"https://tsugimanga.jp/winner/{year}/{category}"
            result = {"entries": parse_ranking(self.http.fetch(url, provider="tsugimanga")), "source_url": url,
                      "year": year, "category": category, "category_label": CATEGORIES[category]}
        elif kind == "rakuten":
            from .credentials import secret
            from .kobo import KOBO_ENDPOINT, Kobo
            if not secret(self.settings, self.settings.rakuten.application_id_env) or not secret(self.settings, self.settings.rakuten.access_key_env):
                raise ConfigError("楽天のアプリIDとアクセスキーを登録してください。")
            headers = {"accessKey": secret(self.settings, self.settings.rakuten.access_key_env)}
            if self.settings.rakuten.origin:
                headers.update(Origin=self.settings.rakuten.origin.rstrip("/"), Referer=self.settings.rakuten.origin.rstrip("/") + "/")
            raw = self.http.json(KOBO_ENDPOINT, params={"applicationId": secret(self.settings, self.settings.rakuten.application_id_env),
                "koboGenreId": "101904", "sort": "sales", "hits": 10, "page": 1, "formatVersion": 2},
                headers=headers, provider="rakuten_books")
            candidates = Kobo.parse(raw, 1)["candidates"]
            result = {"entries": [{"id": item["id"], "title": item["fields"]["title"], "author": item["fields"].get("author", ""),
                                   "url": item["url"], "image": item.get("image", ""), "rank": index + 1} for index, item in enumerate(candidates)]}
        else:
            result = {"entries": (parse_curated if kind == "curated" else parse_rss)(self.http.fetch(DISCOVERY_FEEDS[kind], provider=kind)),
                      "source_url": DISCOVERY_FEEDS[kind]}
        result = {**result, "kind": kind, "updated": timestamp(), "entries": [self.annotate(item, key) for item in result["entries"]]}
        self.store.cache(key, result)
        progress(len(result["entries"]), len(result["entries"]))
        return {"source": key, "items": len(result["entries"]), "owned": sum(item["owned"] for item in result["entries"])}

    def anime(self, progress):
        current = season()
        index = SEASONS.index(current["season"])
        previous = {"season": SEASONS[index - 1], "year": current["year"] - (index == 0)}
        items, seen, truncated = [], set(), False
        for period, status in ((current, None), (previous, "RELEASING")):
            for page in range(1, 5):
                data = self.http.json("https://graphql.anilist.co", method="POST", provider="anilist", body={"query": ANIME_QUERY,
                    "variables": {"season": period["season"], "year": period["year"], "status": status, "page": page}})
                try:
                    if data.get("errors"):
                        raise ValueError
                    result = data["data"]["Page"]
                    media = result["media"]
                    if not isinstance(media, list) or len(media) > 50 or type(result["pageInfo"]["hasNextPage"]) is not bool:
                        raise ValueError
                    for item in media:
                        identifier = item["id"]
                        if type(identifier) is not int or not 1 <= identifier <= 2**31 - 1:
                            raise ValueError
                        if identifier in seen or item.get("format") == "MUSIC":
                            continue
                        seen.add(identifier)
                        originals = [edge["node"]["title"].get("native") or edge["node"]["title"].get("romaji")
                                     for edge in (item.get("relations") or {}).get("edges", [])
                                     if edge.get("relationType") in {"ADAPTATION", "SOURCE"} and edge.get("node", {}).get("type") == "MANGA"]
                        originals = [plain_text(title, 200) for title in originals if title]
                        title = plain_text(item["title"].get("native") or item["title"].get("romaji"), 200)
                        if not title:
                            raise ValueError
                        items.append({"id": str(identifier), "title": title, "original_titles": originals,
                                      "has_manga": bool(originals) or item.get("source") in {"MANGA", "COMIC", "DOUJINSHI"},
                                      "continuing": status is not None, "source": str(item.get("source") or ""),
                                      "format": str(item.get("format") or ""), "episodes": item.get("episodes"),
                                      "image": safe_url((item.get("coverImage") or {}).get("large"), {"s4.anilist.co"}),
                                      "url": f"https://anilist.co/anime/{identifier}", "popularity": item.get("popularity") or 0})
                    progress(len(items), None)
                    if not result["pageInfo"]["hasNextPage"]:
                        break
                    if page == 4:
                        truncated = True
                except (KeyError, TypeError, ValueError, AttributeError):
                    raise CatalogError("AniListの作品情報の形式を確認できませんでした。") from None
        return {**current, "entries": items, "truncated": truncated}

    def demo(self, kind, year=None, category=None):
        rows = self.inventory.cached()["series"][:5]
        entries = [{"id": f"demo-{kind}-{index}", "title": row["title"], "author": row["author"],
                    "url": "", "rank": index + 1, "original_titles": [row["title"]], "has_manga": True,
                    "teaser": "架空の紹介記事です。", "continuing": index == 4} for index, row in enumerate(rows)]
        return {"kind": kind, "entries": entries, "updated": None, "demo": True, **season(), "year": year or season()["year"],
                "category": category, "category_label": CATEGORIES.get(category, "")}
