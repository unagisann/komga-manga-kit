"""Bounded HTTP and text handling for explicitly requested public information."""
from html.parser import HTMLParser
import json
import re
import time
from urllib.parse import urlsplit

import requests

from .catalogs import CatalogError

MAX_SOURCE_BYTES = 2 * 1024 * 1024


def safe_url(value, hosts=None):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        return ""
    try:
        parsed = urlsplit(value)
        parsed.port
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or hosts and parsed.hostname not in hosts):
            return ""
    except ValueError:
        return ""
    return value


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "br", "div", "li", "h2", "h3"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value, maximum=400):
    parser = PlainText()
    parser.feed(str(value or "")[:50000])
    return re.sub(r"\s+", " ", "".join(parser.parts)).strip()[:maximum]


class PublicHTTP:
    def __init__(self, store, timeout=15, session=None):
        self.store, self.timeout, self.session = store, timeout, session

    def pace(self, provider, interval=1.1):
        while True:
            now = time.time()
            with self.store.connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT next_allowed FROM metadata_requests WHERE provider=?", (provider,)).fetchone()
                delay = max(0, row[0] - now) if row else 0
                if delay <= 0:
                    connection.execute("INSERT INTO metadata_requests VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET next_allowed=excluded.next_allowed",
                                       (provider, now + interval))
                    return
            if delay > 3:
                raise CatalogError("取得先が待機中です。時間を空けて再実行してください。", retry_after=min(3600, int(delay) + 1))
            time.sleep(delay + .01)

    def fetch(self, url, *, method="GET", params=None, body=None, headers=None, provider="public", maximum=MAX_SOURCE_BYTES):
        if not safe_url(url):
            raise CatalogError("取得先URLの形式を確認してください。")
        self.pace(provider, 2.1 if provider == "anilist" else 1.1)
        session = self.session or requests.Session()
        try:
            with session.request(method, url, params=params, json=body,
                                 headers={"User-Agent": "KomgaMangaKit/0.1", "Accept": "application/json, application/xml, text/html", **(headers or {})},
                                 timeout=self.timeout, stream=True, allow_redirects=False) as response:
                if response.status_code == 429:
                    value = response.headers.get("Retry-After", "60")
                    delay = min(3600, max(1, int(value))) if re.fullmatch(r"[0-9]{1,6}", value) else 60
                    with self.store.connection() as connection:
                        connection.execute("UPDATE metadata_requests SET next_allowed=? WHERE provider=?", (time.time() + delay, provider))
                    raise CatalogError("取得先の利用制限に達しました。時間を空けて再実行してください。", retry_after=delay)
                if response.status_code == 404:
                    raise CatalogError("指定した情報はまだ公開されていません。")
                if response.status_code != 200:
                    raise CatalogError("取得先から正常な応答を受け取れませんでした。接続・設定を確認してください。")
                result = bytearray()
                for block in response.iter_content(16384):
                    result.extend(block)
                    if len(result) > maximum:
                        raise CatalogError("取得先の応答が上限を超えています。")
                return bytes(result)
        except requests.RequestException:
            raise CatalogError("情報を取得できませんでした。接続と認証設定を確認してください。") from None
        finally:
            if self.session is None:
                session.close()

    def json(self, url, **kwargs):
        try:
            return json.loads(self.fetch(url, **kwargs))
        except (ValueError, UnicodeError, RecursionError):
            raise CatalogError("取得先のデータ形式を確認できませんでした。") from None
