"""Rakuten Kobo bibliographic candidates; ebook identifiers are never treated as ISBNs."""
import re

from .bibliography import edition_markers, product_readership
from .catalogs import CatalogError, DemoCatalog, PAGE_SIZE, caption_summary, series_reading, split_volume
from .config import ConfigError
from .credentials import secret
from .metadata import validate_fields
from .sources import PublicHTTP, safe_url

KOBO_ENDPOINT = "https://openapi.rakuten.co.jp/services/api/Kobo/EbookSearch/20170426"


class Kobo:
    id, label = "rakuten_kobo", "楽天Kobo"

    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.http = PublicHTTP(store, settings.rakuten.timeout_seconds)

    def configured(self):
        return (self.settings.tools.kobo_enabled and bool(secret(self.settings, self.settings.rakuten.application_id_env))
                and bool(secret(self.settings, self.settings.rakuten.access_key_env)))

    def search(self, query):
        if not self.configured():
            raise ConfigError("楽天Koboを有効化し、楽天のアプリIDとアクセスキーを登録してください。")
        if query.get("isbn"):
            raise ConfigError("KoboはISBNではなく作品名・著者で検索してください。")
        params = {name: value for name, value in query.items() if value}
        params.update(applicationId=secret(self.settings, self.settings.rakuten.application_id_env),
                      formatVersion=2, hits=PAGE_SIZE, koboGenreId="101904", sort="-releaseDate")
        headers = {"accessKey": secret(self.settings, self.settings.rakuten.access_key_env)}
        if self.settings.rakuten.origin:
            headers.update(Origin=self.settings.rakuten.origin.rstrip("/"), Referer=self.settings.rakuten.origin.rstrip("/") + "/")
        result = self.http.json(KOBO_ENDPOINT, params=params, headers=headers, provider="rakuten_books")
        return self.parse(result, query["page"])

    @staticmethod
    def parse(data, page):
        try:
            items = data.get("Items", data.get("items"))
            if (not isinstance(items, list) or len(items) > PAGE_SIZE or type(data["page"]) is not int or data["page"] != page
                    or type(data["count"]) is not int or not 0 <= data["count"] <= 100000000
                    or type(data["pageCount"]) is not int or not 0 <= data["pageCount"] <= 10000000):
                raise ValueError
            candidates, seen = [], set()
            for raw in items:
                item = raw.get("Item", raw)
                identifier = str(item["itemNumber"])
                if not re.fullmatch(r"[0-9]{1,30}", identifier) or identifier in seen:
                    raise ValueError
                seen.add(identifier)
                title = validate_fields({"title": item["title"]})["title"]
                core, volume = split_volume(title)
                label = item.get("seriesName") or ""
                if not isinstance(label, str) or len(label) > 300:
                    raise ValueError
                genres, origin = product_readership("", label, {})
                fields = validate_fields({"title": core, "reading": series_reading(item.get("titleKana") or ""),
                                          "author": item.get("author") or "", "publisher": item.get("publisherName") or "",
                                          "summary": caption_summary(item.get("itemCaption") or ""), "genres": genres})
                url = safe_url(item.get("itemUrl"), {"books.rakuten.co.jp"})
                if not url or not url.startswith("https://books.rakuten.co.jp/rk/"):
                    raise ValueError
                candidates.append({"id": identifier, "isbn": "", "title": title, "volume": volume,
                                   "fields": {key: value for key, value in fields.items() if value},
                                   "label": label, "size": "電子書籍", "editions": edition_markers(title, label),
                                   "readership_source": origin, "sales_date": str(item.get("salesDate") or "")[:40],
                                   "url": url, "media": "digital",
                                   "image": safe_url(item.get("largeImageUrl"), {"thumbnail.image.rakuten.co.jp"})})
            return {"candidates": candidates, "total": data["count"], "page": page, "page_count": data["pageCount"],
                    "has_next": page < data["pageCount"]}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CatalogError("Koboの書誌情報の形式を確認できませんでした。") from None


class DemoKobo(DemoCatalog):
    id, label = "demo_kobo", "架空の電子書籍"
