"""Explicit, public manga classification queries; no account or media access."""
import json
import re
import unicodedata

import requests

from .anilist_ja import DEMOGRAPHICS, EXCLUDED_GENRES, EXCLUDED_TAG_CATEGORIES, GENRES, TAGS
from .catalogs import CatalogError, MAX_RESPONSE_BYTES, PAGE_SIZE, RateLimitedCatalog
from .config import ConfigError
from .japanese import search_key
from .metadata import classification_values
from .sample import demo_library

ANILIST_ENDPOINT = "https://graphql.anilist.co"
# AniList ranks how strongly a tag applies (0-100). Lower ranks were mostly noise
# in the existing KomgaTool measurements, so they are not proposed.
MIN_TAG_RANK = 60
ANILIST_QUERY = """query MangaKitClassifications($search: String!, $page: Int!, $perPage: Int!) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { currentPage hasNextPage }
    media(search: $search, type: MANGA, format_in: [MANGA, ONE_SHOT], sort: SEARCH_MATCH) {
      id type format title { native romaji english } countryOfOrigin startDate { year }
      genres tags { name rank category isAdult isGeneralSpoiler isMediaSpoiler }
    }
  }
}"""


def anilist_query(title="", page=1, **unsupported):
    if unsupported:
        raise ConfigError("AniListは作品名で検索してください。著者・ISBNは指定できません。")
    if not isinstance(title, str) or len(title) > 200 or any(ord(c) < 32 for c in title):
        raise ConfigError("作品名は200文字以内の文字列で指定してください。")
    title = unicodedata.normalize("NFKC", title).strip()
    if not title or len(title) > 200:
        raise ConfigError("検索する作品名を1〜200文字で指定してください。")
    if type(page) is not int or not 1 <= page <= 100:
        raise ConfigError("検索ページは1〜100で指定してください。")
    return {"title": title, "page": page}


class AniList(RateLimitedCatalog):
    id = "anilist"
    label = "AniList"
    interval_seconds = 2.1

    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def configured(self):
        return self.settings.enabled

    def search(self, query):
        if not self.configured():
            raise ConfigError("AniList検索を使うには anilist.enabled: true を設定してください。")
        self._reserve()
        try:
            with requests.Session() as session:
                with session.post(ANILIST_ENDPOINT, json={"query": ANILIST_QUERY, "variables": {
                        "search": query["title"], "page": query["page"], "perPage": PAGE_SIZE}},
                        headers={"Accept": "application/json"}, timeout=self.settings.timeout_seconds,
                        allow_redirects=False, stream=True) as response:
                    if response.status_code == 429:
                        retry = response.headers.get("Retry-After", "60")
                        seconds = min(3600, max(1, int(retry))) if re.fullmatch(r"[0-9]{1,6}", retry) else 60
                        self._cooldown(seconds)
                        raise CatalogError("AniListの利用制限に達しました。時間を空けて再検索してください。", retry_after=seconds)
                    if response.status_code != 200:
                        raise CatalogError("AniListへ接続できませんでした。時間を空けてお試しください。")
                    # Respect exhaustion on successful responses as well as HTTP 429.
                    if response.headers.get("X-RateLimit-Remaining") == "0":
                        self._cooldown(60)
                    content = bytearray()
                    for chunk in response.iter_content(chunk_size=16384):
                        content.extend(chunk)
                        if len(content) > MAX_RESPONSE_BYTES:
                            raise CatalogError("AniListの応答が上限を超えています。")
                    data = json.loads(content)
        except (requests.RequestException, ValueError, UnicodeError, RecursionError):
            raise CatalogError("AniListの応答を取得できませんでした。接続・検索条件を確認してください。") from None
        return self.parse(data, query["page"])

    @staticmethod
    def parse(data, page):
        try:
            if not isinstance(data, dict) or data.get("errors"):
                raise ValueError
            result = data["data"]["Page"]
            info, items = result["pageInfo"], result["media"]
            if (type(info["currentPage"]) is not int or info["currentPage"] != page
                    or type(info["hasNextPage"]) is not bool
                    or not isinstance(items, list) or len(items) > PAGE_SIZE):
                raise ValueError
            candidates, seen = [], set()
            for item in items:
                media_id = item["id"]
                if (type(media_id) is not int or not 1 <= media_id <= 2**31 - 1 or media_id in seen
                        or item["type"] != "MANGA" or item["format"] not in {"MANGA", "ONE_SHOT"}):
                    raise ValueError
                seen.add(media_id)
                titles = []
                for name in ("native", "romaji", "english"):
                    title = item["title"].get(name)
                    if title is None:
                        continue
                    if not isinstance(title, str) or len(title) > 300 or any(ord(c) < 32 for c in title):
                        raise ValueError
                    if title.strip() and title.strip() not in titles:
                        titles.append(title.strip())
                if not titles:
                    raise ValueError
                country = item.get("countryOfOrigin")
                start_date = item.get("startDate")
                if start_date is not None and not isinstance(start_date, dict):
                    raise ValueError
                year = start_date.get("year") if start_date is not None else None
                if (country is not None and (not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country))
                        or year is not None and (type(year) is not int or not 1 <= year <= 9999)):
                    raise ValueError
                genres = classification_values([] if item.get("genres") is None else item["genres"])
                tags = item.get("tags")
                if tags is None:
                    tags = []
                if not isinstance(tags, list) or len(tags) > 500:
                    raise ValueError
                demographics, safe_tags = [], []
                for tag in tags:
                    if not isinstance(tag, dict):
                        raise ValueError
                    for flag in ("isGeneralSpoiler", "isMediaSpoiler", "isAdult"):
                        if tag.get(flag) is not None and type(tag[flag]) is not bool:
                            raise ValueError
                    rank, category = tag.get("rank"), tag.get("category")
                    if (rank is not None and (type(rank) is not int or not 0 <= rank <= 100)
                            or category is not None and not isinstance(category, str)):
                        raise ValueError
                    # Missing/unknown flags and ranks are excluded together with
                    # spoilers, adult tags and weakly ranked tags.
                    if (tag.get("isGeneralSpoiler") is not False or tag.get("isMediaSpoiler") is not False
                            or tag.get("isAdult") is not False or category in EXCLUDED_TAG_CATEGORIES
                            or rank is None or rank < MIN_TAG_RANK):
                        continue
                    name = classification_values([tag["name"]])[0]
                    if name in DEMOGRAPHICS:
                        demographics.append(DEMOGRAPHICS[name])
                    else:
                        safe_tags.append(TAGS.get(name, name))
                genres = demographics + [GENRES.get(name, name) for name in genres if name not in EXCLUDED_GENRES]
                fields = {"genres": classification_values(genres), "tags": classification_values(safe_tags[:100])}
                candidates.append({"id": str(media_id), "title": titles[0], "alternate_titles": titles[1:],
                                   "country": country, "year": year, "format": item["format"], "fields": fields,
                                   "url": f"https://anilist.co/manga/{media_id}"})
            # AniList explicitly documents total and lastPage as inaccurate.
            return {"candidates": candidates, "total": None, "page": page, "page_count": None,
                    "has_next": info["hasNextPage"] and page < 100}
        except (ValueError, TypeError, KeyError, AttributeError):
            raise CatalogError("AniListの作品情報の形式を確認できませんでした。") from None


class DemoClassifications:
    id = "demo_anilist"
    label = "架空のタグ・ジャンル"

    def __init__(self, layout):
        self.layout = layout

    def configured(self):
        return True

    def search(self, query):
        candidates = [{"id": f"demo-tags-{index + 1}", "title": series.title, "alternate_titles": [],
                       "country": "JP", "year": 2020 + index % 5, "format": "MANGA",
                       "fields": {"genres": ["日常", "ファンタジー"], "tags": ["旅", "仲間"]}, "url": ""}
                      for index, series in enumerate(demo_library(self.layout).series)
                      if search_key(query["title"]) in search_key(series.title)]
        start = (query["page"] - 1) * PAGE_SIZE
        return {"candidates": candidates[start:start + PAGE_SIZE], "total": len(candidates), "page": query["page"],
                "page_count": (len(candidates) + PAGE_SIZE - 1) // PAGE_SIZE,
                "has_next": start + PAGE_SIZE < len(candidates)}
