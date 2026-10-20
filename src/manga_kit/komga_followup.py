"""Carry a completed local edit into a scoped, explicitly reviewed Komga plan."""
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import ConfigError

METADATA_FIELDS = ("title", "reading", "publisher", "summary", "genres", "tags")
LABELS = {"metadata": "作品情報", "authors": "巻の著者", "completion": "完結タグ・連載状態"}


class KomgaFollowups:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def create(self, source, changes, back_url):
        """Changes come from a successful save, never from a browser selection."""
        if self.settings.demo or not self.settings.komga_url:
            return None
        choices = []
        for mode in LABELS:
            selected, fields = set(), set()
            for series_id, items in changes.items():
                for item in items:
                    field = item["field"]
                    if (mode == "metadata" and field in METADATA_FIELDS
                            or mode == "authors" and field == "author"
                            or mode == "completion" and field == "status" and item.get("after") == "completed"):
                        selected.add(series_id)
                        fields.add(field)
            if selected:
                choices.append({"mode": mode, "label": LABELS[mode], "series_ids": sorted(selected),
                                "fields": [field for field in METADATA_FIELDS if field in fields] if mode == "metadata" else []})
        if not choices:
            return None
        value = {"choices": choices, "back_url": back_url}
        identifier = hashlib.sha256(json.dumps([source, value], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
        if not self.store.cached("komga:followup:" + identifier):
            self.store.cache("komga:followup:" + identifier, value)
        return identifier

    def inspect(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ConfigError("保存した処理からKomga反映を選んでください。")
        record = self.store.cached("komga:followup:" + identifier)
        if not record:
            raise ConfigError("保存した処理の反映対象が見つかりません。")
        value = {"id": identifier, **record["data"]}
        back = urlsplit(value["back_url"])
        query = dict(parse_qsl(back.query))
        query["followup"] = identifier
        value["return_url"] = urlunsplit(("", "", back.path, urlencode(query), back.fragment))
        return value

    def moved(self, source, series, target_ids, back_url):
        changes = {row.id: [{"field": field, "after": getattr(row, field)}
                           for field in (*METADATA_FIELDS, "author", "status") if getattr(row, field)]
                   for row in series if row.id in target_ids}
        return self.create(source, changes, back_url)

    def choice(self, identifier, mode=None):
        value = self.inspect(identifier)
        if mode is None:
            return value["choices"][0]
        choice = next((item for item in value["choices"] if item["mode"] == mode), None)
        if choice is None:
            raise ConfigError("保存した情報に含まれる反映内容を選んでください。")
        return choice

    def scope(self, identifier, body):
        choice = self.choice(identifier, body.get("mode"))
        if "series_ids" in body:
            raise ConfigError("保存した処理の対象作品は変更できません。")
        if "fields" in body and (not isinstance(body["fields"], list)
                                 or any(field not in choice["fields"] for field in body["fields"])):
            raise ConfigError("保存した項目から反映する情報を選んでください。")
        scoped = {**body, "mode": choice["mode"], "series_ids": choice["series_ids"]}
        if choice["mode"] == "metadata":
            scoped.setdefault("fields", choice["fields"])
        return scoped
