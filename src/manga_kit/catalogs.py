"""Explicit bibliographic searches; no media retrieval or automatic matching."""
import json
from decimal import Decimal
import os
import re
import time
import unicodedata
from urllib.parse import urlsplit

import requests

from .config import ConfigError
from .credentials import credential_values
from .bibliography import edition_markers, product_readership
from .japanese import hiragana, search_key
from .metadata import validate_fields
from .sample import demo_library

PAGE_SIZE = 10
MAX_RESPONSE_BYTES = 512 * 1024
RAKUTEN_ENDPOINT = "https://openapi.rakuten.co.jp/services/api/BooksBook/Search/20170404"
# 漫画（コミック）. Unlike the comic format (size=9), it also covers manga bunko.
RAKUTEN_COMICS_GENRE = "001001"
# Comic sub-genres that name the readership, as Japanese bookstores classify it.
RAKUTEN_DEMOGRAPHICS = {"001001001": "少年", "001001002": "少女", "001001003": "青年", "001001004": "レディース"}
SUMMARY_LIMIT = 400
# Lines of a product caption that advertise the edition rather than the story.
_PROMOTION = re.compile(
    r"累計|万部|部突破|大好評|発売中|重版|好評連載|電子版|電子書籍|電子限定|デジタル版|紙\+電子|試し読み|"
    r"特典|描き下ろし|カバーイラスト|巻末|収録内容|同時収録|初登場|文庫化|新装版|愛蔵版|完全版|合本版|セット|刊行|"
    r"連載開始|前代未聞|待望の|ついに.{0,6}(発売|登場|開始)|^※|^■|^▼|^【|^<|アニメ化|アニメ放送|コミックス発売|"
    r"収録|再現|ファン必携|お届け|ボリューム|[0-9]+\s*(P|ページ)|第\s*[0-9]+\s*巻\s*!?\s*$")
_EDITION_ONLY = re.compile(r"完全版|新装版|愛蔵版|文庫版|カラー版|全\s*[0-9]+\s*巻")
# Patterns accept full- and half-width forms so the original spelling is returned.
_BRACKET_SUFFIX = re.compile(r"\s*[(（]([^()（）]+)[)）]\s*$")
_EDITION_NOTE = re.compile(r"特装版|限定版|通常版|新装版|愛蔵版|完全版|文庫版|カラー版")
_LABEL_NOTE = re.compile(r"コミックス?|comics|\bKC\b|文庫|新書", re.IGNORECASE)
# A trailing volume needs a space, bracket or 第 before it; ゴルゴ13 keeps its number.
_VOLUME_SUFFIX = re.compile(r"(?:\s+第?|第|\s*[(（【]\s*第?)\s*([0-9０-９]{1,4}(?:[.．][0-9０-９]{1,3})?)\s*[)）】]?\s*巻?\s*$")


def split_volume(title):
    """Return (series title, volume) from a product title such as 作品（3） or 作品 3 (レーベル)."""
    text = title.strip()
    while note := _BRACKET_SUFFIX.search(text):
        prefix = text[:note.start()].strip()
        label = unicodedata.normalize("NFKC", note[1]).strip()
        # A subtitle is part of the work's identity. Only explicit editions,
        # or label-like notes following a volume, may be removed automatically.
        if not prefix or not (_EDITION_NOTE.fullmatch(label)
                              or _LABEL_NOTE.search(label) and _VOLUME_SUFFIX.search(prefix)):
            break
        text = prefix
    match = _VOLUME_SUFFIX.search(text)
    if not match or not text[:match.start()].strip():
        return text, None
    return text[:match.start()].strip(), format(Decimal(unicodedata.normalize("NFKC", match[1])).normalize(), "f")


def series_reading(kana):
    text, _ = split_volume(kana)
    # 楽天's titleKana separates words; a series reading is one sortable word.
    return re.sub(r"(?<=[^\x00-\x7f])\s+(?=[^\x00-\x7f])", "", text)


def caption_summary(caption):
    """Keep the story part of a product caption, or nothing when only promotion remains."""
    lines = [line.strip() for line in re.split(r"[\r\n]+", caption) if line.strip()]
    kept = [line for line in lines if not _PROMOTION.search(unicodedata.normalize("NFKC", line))]
    body = re.sub(r"\s+", " ", " ".join(kept)).strip()
    if len(body) > SUMMARY_LIMIT:
        cut = body[:SUMMARY_LIMIT]
        end = max(cut.rfind(mark) for mark in "。！？!?")
        body = cut[:end + 1] if end > SUMMARY_LIMIT // 2 else cut + "…"
    if len(body) < 40 or _EDITION_ONLY.search(unicodedata.normalize("NFKC", body)) and len(body) < 60:
        return ""
    return body


def readership(genre_ids):
    found = {name for genre in genre_ids.split("/") for prefix, name in RAKUTEN_DEMOGRAPHICS.items()
             if genre.startswith(prefix)}
    return [found.pop()] if len(found) == 1 else []


class CatalogError(RuntimeError):
    def __init__(self, message, *, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def search_query(title="", author="", isbn="", page=1):
    result = {}
    for key, value in (("title", title), ("author", author), ("isbn", isbn)):
        if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
            raise ConfigError("検索語は200文字以内の文字列で指定してください。")
        result[key] = unicodedata.normalize("NFKC", value).strip()
    if type(page) is not int or not 1 <= page <= 100:
        raise ConfigError("検索ページは1〜100で指定してください。")
    result["page"] = page
    if result["isbn"]:
        isbn = re.sub(r"[\s-]", "", result["isbn"])
        if (not re.fullmatch(r"97[89][0-9]{10}", isbn)
                or sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(isbn)) % 10):
            raise ConfigError("ISBNは正しい13桁で指定してください。ハイフンは省略できます。")
        result.update(isbn=isbn, title="", author="")
    elif not result["title"] and not result["author"]:
        raise ConfigError("作品名・著者・ISBNのいずれかを入力してください。")
    return result


class RateLimitedCatalog:
    interval_seconds = 1.1

    def _reserve(self):
        now = time.time()
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT next_allowed FROM metadata_requests WHERE provider=?", (self.id,)).fetchone()
            if row and row[0] > now:
                raise CatalogError("検索間隔を空けて、もう一度お試しください。", retry_after=max(1, int(row[0] - now) + 1))
            connection.execute("INSERT INTO metadata_requests VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET next_allowed=excluded.next_allowed",
                               (self.id, now + self.interval_seconds))

    def _cooldown(self, seconds):
        with self.store.connection() as connection:
            connection.execute("UPDATE metadata_requests SET next_allowed=MAX(next_allowed,?) WHERE provider=?",
                               (time.time() + seconds, self.id))


class RakutenBooks(RateLimitedCatalog):
    id = "rakuten_books"
    label = "楽天Books"

    def __init__(self, settings, store, credentials_file=""):
        self.settings, self.store, self.credentials_file = settings, store, credentials_file

    def _credential(self, name):
        return os.getenv(name, "") or credential_values(self.credentials_file).get(name, "")

    def configured(self):
        return bool(self.settings.enabled and self._credential(self.settings.application_id_env)
                    and self._credential(self.settings.access_key_env))

    def search(self, query):
        if not self.configured():
            raise ConfigError("楽天Books検索を使うには、rakuten.enabled とアプリID・アクセスキーの環境変数を設定してください。")
        application_id, key = (self._credential(self.settings.application_id_env), self._credential(self.settings.access_key_env))
        if any(not value.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in value) or len(value) > 4096
               for value in (application_id, key)):
            raise ConfigError("楽天の認証情報の形式を確認してください。")
        params = {name: value for name, value in query.items() if value}
        params.update(applicationId=application_id, format="json", formatVersion=2, hits=PAGE_SIZE,
                      booksGenreId=RAKUTEN_COMICS_GENRE, outOfStockFlag=1,
                      elements="count,page,pageCount,title,titleKana,author,publisherName,isbn,itemUrl,"
                               "itemCaption,salesDate,booksGenreId,seriesName,size")
        headers = {"accessKey": key, "Accept": "application/json"}
        if self.settings.origin:
            headers.update(Origin=self.settings.origin.rstrip("/"), Referer=self.settings.origin.rstrip("/") + "/")
        self._reserve()
        try:
            with requests.Session() as session:
                with session.get(RAKUTEN_ENDPOINT, params=params, headers=headers, timeout=self.settings.timeout_seconds,
                                 allow_redirects=False, stream=True) as response:
                    if response.status_code == 429:
                        retry = response.headers.get("Retry-After", "60")
                        seconds = min(3600, max(1, int(retry))) if re.fullmatch(r"[0-9]{1,6}", retry) else 60
                        self._cooldown(seconds)
                        raise CatalogError("楽天Booksの利用制限に達しました。時間を空けて再検索してください。", retry_after=seconds)
                    if response.status_code == 404:
                        return {"candidates": [], "total": 0, "page": query["page"], "page_count": 0}
                    if response.status_code in {400, 401, 403}:
                        raise CatalogError("楽天Booksの検索を受け付けてもらえませんでした。検索条件・認証情報・アプリのアクセス設定を確認してください。")
                    if response.status_code != 200:
                        raise CatalogError("楽天Booksへ接続できませんでした。時間を空けてお試しください。")
                    content = bytearray()
                    for chunk in response.iter_content(chunk_size=16384):
                        content.extend(chunk)
                        if len(content) > MAX_RESPONSE_BYTES:
                            raise CatalogError("楽天Booksの応答が上限を超えています。")
                    data = json.loads(content)
        except (requests.RequestException, ValueError, UnicodeError, RecursionError):
            raise CatalogError("楽天Booksの応答を取得できませんでした。接続・検索条件を確認してください。") from None
        return self.parse(data, query["page"])

    @staticmethod
    def parse(data, page):
        try:
            if not isinstance(data, dict):
                raise ValueError
            items = data.get("Items", data.get("items"))
            if not isinstance(items, list) or len(items) > PAGE_SIZE:
                raise ValueError
            for name, limit in (("count", 10**9), ("pageCount", 100), ("page", 100)):
                if type(data.get(name)) is not int or not 0 <= data[name] <= limit:
                    raise ValueError
            if data["page"] != page:
                raise ValueError
            candidates, seen = [], set()
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError
                raw = {key: item.get(key, "") for key in ("title", "titleKana", "itemCaption", "salesDate", "booksGenreId")}
                if (any(not isinstance(value, str) for value in raw.values()) or len(raw["itemCaption"]) > 20000
                        or len(raw["salesDate"]) > 40 or any(ord(c) < 32 for c in raw["salesDate"])
                        or raw["booksGenreId"] and not re.fullmatch(r"[0-9]{3,30}(?:/[0-9]{3,30}){0,20}", raw["booksGenreId"])):
                    raise ValueError
                product = validate_fields({"title": raw["title"]})["title"]
                label, size = item.get("seriesName", ""), item.get("size", "")
                if (not isinstance(label, str) or len(label) > 300 or any(ord(c) < 32 for c in label)
                        or not isinstance(size, str) or len(size) > 40 or any(ord(c) < 32 for c in size)):
                    raise ValueError
                label, size = label.strip(), size.strip()
                audience, audience_source = product_readership(raw["booksGenreId"], label, RAKUTEN_DEMOGRAPHICS)
                title, volume = split_volume(product)
                fields = validate_fields({"title": title, "reading": series_reading(raw["titleKana"]),
                                          "author": item.get("author", ""), "publisher": item.get("publisherName", ""),
                                          "summary": caption_summary(raw["itemCaption"]),
                                          "genres": audience})
                fields = {name: value for name, value in fields.items() if value}
                isbn = item.get("isbn")
                if not isinstance(isbn, str) or not re.fullmatch(r"97[89][0-9]{10}", isbn) or isbn in seen:
                    raise ValueError
                search_query(isbn=isbn)
                url = item.get("itemUrl")
                if not isinstance(url, str) or len(url) > 2048:
                    raise ValueError
                parsed = urlsplit(url)
                if (parsed.scheme != "https" or parsed.netloc != "books.rakuten.co.jp"
                        or not re.fullmatch(r"/rb/[0-9]+/", parsed.path)):
                    raise ValueError
                seen.add(isbn)
                candidates.append({"id": isbn, "isbn": isbn, "title": product, "fields": fields, "volume": volume,
                                   "label": label, "size": size, "editions": edition_markers(product, label, size),
                                   "readership_source": audience_source,
                                   "sales_date": raw["salesDate"], "url": "https://books.rakuten.co.jp" + parsed.path})
            return {"candidates": candidates, "total": data["count"], "page": page, "page_count": data["pageCount"]}
        except (ValueError, TypeError, KeyError):
            raise CatalogError("楽天Booksの書誌情報の形式を確認できませんでした。") from None


class DemoCatalog:
    id = "demo"
    label = "架空の書誌データ"

    def __init__(self, layout):
        self.layout = layout

    def configured(self):
        return True

    def search(self, query):
        candidates = []
        for index, series in enumerate(demo_library(self.layout).series):
            fields = {"title": series.title, "reading": hiragana(series.reading or "ツキヲマツエキ"),
                      "author": series.author or "宵野はる", "publisher": series.publisher}
            if series.summary:
                fields["summary"] = series.summary
            fields["genres"] = ["日常"]
            if (not query["isbn"] and search_key(query["title"]) in search_key(fields["title"])
                    and search_key(query["author"]) in search_key(fields["author"])):
                candidates.append({"id": f"demo-{index + 1}", "isbn": "", "title": series.title, "fields": fields, "url": ""})
        start = (query["page"] - 1) * PAGE_SIZE
        return {"candidates": candidates[start:start + PAGE_SIZE], "total": len(candidates), "page": query["page"],
                "page_count": (len(candidates) + PAGE_SIZE - 1) // PAGE_SIZE}
