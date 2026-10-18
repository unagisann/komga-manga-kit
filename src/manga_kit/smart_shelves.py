"""Reviewed synchronization of four explicitly owned Komga shelves."""
from copy import deepcopy
import re
from uuid import uuid4

from .config import ConfigError
from .inventory import page_items
from .komga import write_version_supported
from .operations import digest
from .state import StateConflict, timestamp

DEFINITIONS = {"anime": ("collections", "今期アニメの原作"), "awards": ("collections", "受賞した漫画"),
               "new": ("collections", "新刊が出ている漫画"), "binge": ("readlists", "完結した漫画を一気読み")}


def snapshot(kind, value):
    if value is None:
        return None
    keys = ("name", "ordered", "seriesIds") if kind == "collections" else ("name", "ordered", "summary", "bookIds")
    result = {key: value[key] for key in keys}
    if kind == "collections" and not result["ordered"]:
        result["seriesIds"] = sorted(result["seriesIds"])
    return result


class SmartShelves:
    def __init__(self, settings, store, inventory, feeds, volumes, client):
        self.settings, self.store, self.inventory, self.feeds, self.volumes, self.client = settings, store, inventory, feeds, volumes, client

    def targets(self):
        rows = {row["id"]: row for row in self.inventory.cached()["series"]}
        anime = self.feeds.read("anime")
        rankings = self.store.cached_prefix("feed:ranking:")
        awards = [self.feeds.annotate(item, key) for key, value in rankings.items() for item in value["data"]["entries"]]
        values = {"anime": [item["series_id"] for item in (anime or {}).get("entries", []) if item.get("owned")],
                  "awards": [item["series_id"] for item in awards if item.get("owned")],
                  "new": [item["series_id"] for item in self.volumes.read() if item.get("new")],
                  "binge": [row["id"] for row in self.inventory.binge(10)]}
        available = {"anime": bool(anime and not anime.get("stale") and not anime.get("truncated")), "awards": bool(rankings),
                     "new": bool(self.store.cached_prefix("newvol:row:")), "binge": True}
        result = []
        for key, (kind, label) in DEFINITIONS.items():
            selected = [rows[identifier] for identifier in dict.fromkeys(values[key]) if identifier in rows]
            ids = ([book["komga_id"] for row in selected for book in row["books"] if book.get("komga_id")]
                   if kind == "readlists" else [row["komga_id"] for row in selected if row.get("komga_id")])
            result.append({"key": key, "kind": kind, "name": "Manga Kit · " + label, "ids": list(dict.fromkeys(ids)),
                           "available": available[key], "titles": [row["title"] for row in selected]})
        return result

    def prepare(self):
        if self.settings.demo or not self.settings.komga_url:
            raise ConfigError("スマート本棚は実ライブラリのKomga接続後に利用できます。")
        if not write_version_supported(self.client.get("/actuator/info")):
            raise ConfigError("検証済みのKomgaバージョンで利用してください。")
        existing = {kind: page_items(self.client, "/api/v1/" + kind) for kind in ("collections", "readlists")}
        targets = self.targets()
        for target in targets:
            ownership = self.store.cached("shelf:owned:" + target["key"])
            identifier = ownership["data"]["id"] if ownership else None
            before = next((value for value in existing[target["kind"]] if value["id"] == identifier), None)
            if identifier and before is None:
                raise StateConflict("管理している本棚が見つかりません。Komgaでの削除・権限を確認してください。")
            if before and before["name"] != target["name"]:
                raise StateConflict("管理している本棚の名前が変わっています。変更内容を確認してください。")
            if not identifier and any(item["name"] == target["name"] for item in existing[target["kind"]]):
                raise StateConflict("同名の本棚が既にあります。既存の本棚を自動で引き継ぐことはできません。")
            after = {"name": target["name"], "ordered": target["kind"] == "readlists",
                     "bookIds" if target["kind"] == "readlists" else "seriesIds": target["ids"] if target["kind"] == "readlists" else sorted(target["ids"])}
            if target["kind"] == "readlists":
                after["summary"] = "Manga Kitから同期した完結作品です。"
            target.update(id=identifier, before=snapshot(target["kind"], before), after=after,
                          skip=not target["available"] or not identifier and not target["ids"])
        plan = {"id": uuid4().hex, "created": timestamp(), "targets": targets, "komga_url": self.settings.komga_url}
        value = {"plan": plan, "digest": digest(plan), "status": "planned", "created_ids": {}, "updated": timestamp()}
        self.store.cache("shelf:plan:" + plan["id"], value)
        return value

    def inspect(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ConfigError("本棚の操作IDを確認してください。")
        value = self.store.cached("shelf:plan:" + identifier)
        if value is None:
            raise ConfigError("本棚の確認結果がありません。")
        return value["data"]

    def execute(self, identifier, confirm, *, restore=False):
        if self.settings.demo or not self.settings.execution_enabled or not self.settings.web_execution_enabled:
            raise ConfigError("Web実行を設定で有効にしてください。")
        with self.store.tool_lock("smart-shelves"), self.store.editing():
            operation = self.inspect(identifier)
            plan = operation["plan"]
            if not write_version_supported(self.client.get("/actuator/info")):
                raise ConfigError("検証済みのKomgaバージョンで利用してください。")
            if confirm != operation["digest"] or digest(plan) != operation["digest"] or plan["komga_url"] != self.settings.komga_url:
                raise StateConflict("確認した内容と接続先が一致しません。確認し直してください。")
            if restore and operation["status"] not in {"completed", "restoring"}:
                raise StateConflict("同期完了後に復元できます。")
            if not restore and operation["status"] not in {"planned", "running"}:
                raise StateConflict("この確認結果は実行済みです。")
            # Preflight all shelves before the first mutation; accept our own
            # already applied values when resuming an interrupted operation.
            pending = []
            existing = {kind: {value["id"]: value for value in page_items(self.client, "/api/v1/" + kind)}
                        for kind in ("collections", "readlists")}
            for target in plan["targets"]:
                if target["skip"]:
                    continue
                native_id = target["id"] or operation["created_ids"].get(target["key"])
                endpoint = "/api/v1/" + target["kind"]
                current = existing[target["kind"]].get(native_id)
                actual = snapshot(target["kind"], current)
                desired = target["before"] if restore else target["after"]
                expected = target["after"] if restore else target["before"]
                if actual == desired:
                    continue
                if actual != expected:
                    raise StateConflict("確認後に本棚が変更されました。既存の編集を保護するため中止しました。")
                if native_id is None:
                    all_items = existing[target["kind"]].values()
                    if any(item["name"] == target["name"] for item in all_items):
                        raise StateConflict("同名の本棚が作成されています。確認し直してください。")
                pending.append((target, endpoint, native_id, desired))
            operation["status"] = "restoring" if restore else "running"
            self.store.cache("shelf:plan:" + identifier, operation)
            for target, endpoint, native_id, desired in pending:
                if desired is None:
                    self.client.request("DELETE", endpoint + "/" + native_id)
                elif native_id:
                    self.client.request("PATCH", endpoint + "/" + native_id, desired)
                else:
                    created = self.client.request("POST", endpoint, desired)
                    if not isinstance(created, dict) or not isinstance(created.get("id"), str):
                        raise StateConflict("作成された本棚を確認できません。Komgaの本棚と処理履歴を確認してください。")
                    native_id = created["id"]
                    operation["created_ids"][target["key"]] = native_id
                    self.store.cache("shelf:plan:" + identifier, operation)
                if desired is not None and snapshot(target["kind"], self.client.get(endpoint + "/" + native_id)) != desired:
                    raise StateConflict("本棚の同期結果を検証できません。処理履歴を確認してください。")
                self.store.cache("shelf:owned:" + target["key"], {"id": target["id"] if restore else native_id})
            operation.update(status="restored" if restore else "completed", updated=timestamp())
            self.store.cache("shelf:plan:" + identifier, operation)
            return {"operation_id": identifier, "shelves": len(pending), "restored": restore}
