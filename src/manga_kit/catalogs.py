"""Explicit bibliographic searches; no media retrieval or automatic matching."""
import json
import os
import re
import time
import unicodedata
from urllib.parse import urlsplit

import requests

from .config import ConfigError
from .japanese import hiragana, search_key
from .metadata import validate_fields
from .sample import demo_library

PAGE_SIZE = 10
MAX_RESPONSE_BYTES = 512 * 1024
RAKUTEN_ENDPOINT = "https://openapi.rakuten.co.jp/services/api/BooksBook/Search/20170404"


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


class RakutenBooks:
    id = "rakuten_books"
    label = "楽天Books"

    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def configured(self):
        return bool(self.settings.enabled and os.getenv(self.settings.application_id_env)
                    and os.getenv(self.settings.access_key_env))

    def _reserve(self):
        now = time.time()
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT next_allowed FROM metadata_requests WHERE provider=?", (self.id,)).fetchone()
            if row and row[0] > now:
                raise CatalogError("検索間隔を空けて、もう一度お試しください。", retry_after=max(1, int(row[0] - now) + 1))
            connection.execute("INSERT INTO metadata_requests VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET next_allowed=excluded.next_allowed",
                               (self.id, now + 1.1))

    def _cooldown(self, seconds):
        with self.store.connection() as connection:
            connection.execute("UPDATE metadata_requests SET next_allowed=MAX(next_allowed,?) WHERE provider=?",
                               (time.time() + seconds, self.id))

    def search(self, query):
        if not self.configured():
            raise ConfigError("楽天Books検索を使うには、rakuten.enabled とアプリID・アクセスキーの環境変数を設定してください。")
        application_id, key = (os.environ[self.settings.application_id_env], os.environ[self.settings.access_key_env])
        if any(not value.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in value) or len(value) > 4096
               for value in (application_id, key)):
            raise ConfigError("楽天の認証情報の形式を確認してください。")
        params = {name: value for name, value in query.items() if value}
        params.update(applicationId=application_id, format="json", formatVersion=2, hits=PAGE_SIZE,
                      size=9, outOfStockFlag=1, elements="count,page,pageCount,title,titleKana,author,publisherName,isbn,itemUrl")
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
                fields = {target: item.get(source, "") for source, target in (
                    ("title", "title"), ("titleKana", "reading"), ("author", "author"), ("publisherName", "publisher"))}
                fields = validate_fields(fields)
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
                candidates.append({"id": isbn, "isbn": isbn, "title": fields["title"], "fields": fields,
                                   "url": "https://books.rakuten.co.jp" + parsed.path})
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
            if (not query["isbn"] and search_key(query["title"]) in search_key(fields["title"])
                    and search_key(query["author"]) in search_key(fields["author"])):
                candidates.append({"id": f"demo-{index + 1}", "isbn": "", "title": series.title, "fields": fields, "url": ""})
        start = (query["page"] - 1) * PAGE_SIZE
        return {"candidates": candidates[start:start + PAGE_SIZE], "total": len(candidates), "page": query["page"],
                "page_count": (len(candidates) + PAGE_SIZE - 1) // PAGE_SIZE}
