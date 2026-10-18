"""MangaUpdates public bibliography only; release/acquisition endpoints are unused."""
import math
import re

from .catalogs import CatalogError, DemoCatalog
from .config import ConfigError
from .metadata import validate_fields
from .sources import PublicHTTP, plain_text, safe_url


class MangaUpdates:
    id, label = "mangaupdates", "MangaUpdates"

    def __init__(self, settings, store):
        self.settings, self.http = settings, PublicHTTP(store)

    def configured(self):
        return self.settings.tools.mangaupdates_enabled

    def search(self, query):
        if not self.configured():
            raise ConfigError("MangaUpdatesを設定で有効にしてください。")
        if query.get("isbn") or not query.get("title"):
            raise ConfigError("MangaUpdatesは作品名で検索してください。")
        page = query["page"]
        raw = self.http.json("https://api.mangaupdates.com/v1/series/search", method="POST", provider=self.id,
                             body={"search": query["title"], "stype": "title", "type": ["Manga"], "page": page, "perpage": 5})
        try:
            if (not isinstance(raw["results"], list) or len(raw["results"]) > 5 or type(raw["total_hits"]) is not int
                    or not 0 <= raw["total_hits"] <= 1000000):
                raise ValueError
            values = []
            for record in raw["results"]:
                identifier = record["record"]["series_id"]
                if type(identifier) is not int or not 1 <= identifier < 2**63:
                    raise ValueError
                detail = self.http.json(f"https://api.mangaupdates.com/v1/series/{identifier}", provider=self.id)
                if detail["series_id"] != identifier:
                    raise ValueError
                values.append(self.parse(detail))
            return {"candidates": values, "page": page, "page_count": math.ceil(raw["total_hits"] / 5), "total": raw["total_hits"], "has_next": page * 5 < raw["total_hits"]}
        except (KeyError, ValueError, TypeError, AttributeError):
            raise CatalogError("MangaUpdatesの書誌情報の形式を確認できませんでした。") from None

    @staticmethod
    def parse(raw):
        title = plain_text(raw["title"], 300)
        summary = plain_text(raw.get("description"), 400)
        # Foreign-language descriptions remain on the source page for review.
        summary = summary if re.search(r"[ぁ-んァ-ヶ]", summary) else ""
        fields = {"title": title, "author": "・".join(dict.fromkeys(plain_text(author["name"], 100) for author in raw.get("authors", []))),
                  "publisher": next((plain_text(item["publisher_name"], 300) for item in raw.get("publishers", []) if item.get("type") == "Original"), ""),
                  "summary": summary, "genres": [plain_text(item["genre"], 100) for item in raw.get("genres", [])][:100]}
        if type(raw.get("completed")) is bool:
            fields["status"] = "completed" if raw["completed"] else "ongoing"
        year = raw.get("year", "")
        rating = raw.get("bayesian_rating")
        facts = {"original_year": int(year) if isinstance(year, str) and re.fullmatch(r"[12][0-9]{3}", year) else None,
                 "rating": round(rating / 2, 2) if type(rating) in {int, float} and 0 <= rating <= 10 else None,
                 "magazine": " / ".join(plain_text(item["publication_name"], 100) for item in raw.get("publications", []))[:200]}
        return {"id": str(raw["series_id"]), "title": title, "fields": {key: value for key, value in validate_fields(fields).items() if value},
                "alternate_titles": [plain_text(item["title"], 300) for item in raw.get("associated", [])][:100], "facts": facts,
                "url": safe_url(raw.get("url"), {"www.mangaupdates.com", "mangaupdates.com"}), "volume": None,
                "editions": [], "label": "", "isbn": "", "sales_date": "", "size": "作品情報"}


class DemoMangaUpdates(DemoCatalog):
    id, label = "demo_mangaupdates", "架空の作品情報"
