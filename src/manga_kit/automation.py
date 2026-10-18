"""App-owned scheduled refreshes and a resumable post-import pipeline."""
from datetime import datetime
from contextlib import contextmanager
import os
import re
import time
from threading import Event, Thread
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import ConfigError
from .catalogs import CatalogError
from .komga import KomgaError
from .state import StateConflict, timestamp
from .durability import durable_mkdir

STEPS = {"scan": "Komgaスキャン", "inventory": "蔵書情報", "bibliography": "書誌・読み・著者の補完",
         "classification": "タグ・ジャンル補完", "anime": "今期アニメ", "new_volumes": "新刊チェック",
         "discovery": "漫画の発見", "rankings": "今年のマンガ大賞", "shelves": "スマート本棚"}
DEFAULTS = {"enabled": False, "time": "04:00", "steps": ["inventory"], "post_import": False, "auto_apply": False}


class Automation:
    def __init__(self, settings, store, jobs, services):
        self.settings, self.store, self.jobs, self.services = settings, store, jobs, services
        self._stop, self._thread = Event(), None

    @contextmanager
    def locked(self):
        import fcntl
        folder = self.settings.data_root / "automation"
        durable_mkdir(folder)
        descriptor = os.open(folder / (self.store.namespace + ".lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StateConflict("別の自動更新が動いています。完了後に再実行してください。") from None
            yield
        finally:
            os.close(descriptor)

    def config(self):
        value = self.store.cached("automation:config")
        data = dict(value["data"]) if value else dict(DEFAULTS)
        # Steps removed from the application (such as reading status) are skipped, not failed.
        data["steps"] = [step for step in data["steps"] if step in STEPS] or list(DEFAULTS["steps"])
        return {"revision": value["revision"] if value else 0, **data}

    def save(self, data, revision):
        if (not isinstance(data, dict) or set(data) != set(DEFAULTS) or any(type(data[key]) is not bool for key in ("enabled", "post_import", "auto_apply"))
                or not isinstance(data["time"], str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", data["time"])
                or not isinstance(data["steps"], list) or not data["steps"] or any(not isinstance(step, str) for step in data["steps"])
                or set(data["steps"]) - STEPS.keys() or len(set(data["steps"])) != len(data["steps"])):
            raise ConfigError("自動更新の時刻・ステップ・実行方法を確認してください。")
        if data["auto_apply"] and (self.settings.demo or not self.settings.execution_enabled or not self.settings.web_execution_enabled):
            raise ConfigError("自動反映にはWeb実行を設定で有効にしてください。")
        self.store.cache("automation:config", data, revision=revision)
        if data["enabled"]:
            self.start()
        return self.config()

    def submit(self, *, identifier=None, after_import=False):
        if identifier is not None and (not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier)):
            raise ConfigError("再開する処理のIDを確認してください。")
        config = self.config()
        if after_import and not config["post_import"]:
            return None
        if identifier:
            value = self.store.cached("automation:run:" + identifier)
            if not value:
                raise ConfigError("再開する処理が見つかりません。")
            run = value["data"]
            if run["status"] == "completed":
                raise ConfigError("この処理は完了しています。")
        else:
            run = {"id": uuid4().hex, "config": config, "created": timestamp(), "status": "queued", "steps": {}, "after_import": after_import}
            self.store.cache("automation:run:" + run["id"], run)
        return self.jobs.submit_task("automation", lambda progress: self.run(run["id"], progress))

    def enrich(self, provider, auto_apply):
        metadata = self.services["metadata"]
        enrichment = self.services["auto_enrichment"]
        ids = [row.id for row in metadata.library.scan().series]
        results = []
        for start in range(0, len(ids), 60):
            identifier = self.store.create_job("enrichment")
            self.store.update_job(identifier, status="running")
            try:
                result = enrichment.collect(ids[start:start + 60], provider, lambda current, total: self.store.update_job(identifier, current=current, total=total))
                self.store.update_job(identifier, status="succeeded", result=result)
                saved = enrichment.apply(identifier, result["digest"]) if auto_apply and result["counts"]["ready"] else None
                results.append({"job_id": identifier, "counts": result["counts"], "saved": saved})
            except Exception:
                self.store.update_job(identifier, status="failed", error="補完を完了できませんでした。情報源の設定を確認してください。")
                raise
        return {"provider": provider, "batches": results}

    def step(self, key, config):
        services, auto_apply = self.services, config["auto_apply"]
        if auto_apply and (not self.settings.execution_enabled or not self.settings.web_execution_enabled or self.settings.demo):
            raise ConfigError("自動反映の実行設定が無効になっています。")
        if key == "scan":
            if not self.settings.komga_url or self.settings.demo:
                return {"skipped": "Komga未接続"}
            from .paths import map_komga_path
            libraries = services["komga"].get("/api/v1/libraries")
            selected = []
            for library in libraries:
                try:
                    mapped = map_komga_path(library["root"], self.settings.path_mappings, self.settings.library_root)
                except (KeyError, ConfigError):
                    continue
                if mapped is not None:
                    selected.append(library["id"])
            if not selected:
                raise ConfigError("スキャン対象のKomgaライブラリとパス対応を確認してください。")
            for identifier in selected:
                services["komga"].request("POST", "/api/v1/libraries/" + quote(identifier, safe="") + "/scan")
            from .inventory import page_items
            scanned = services["metadata"].library.scan(refresh=True, reuse_metadata=True)
            if scanned.truncated or scanned.warnings:
                raise ConfigError("取り込み対象を全件確認できません。保存先とスキャン上限を確認してください。")
            expected = {book.path: book.size for row in scanned.series for book in row.books}
            deadline, stable = time.monotonic() + 90, 0
            while time.monotonic() < deadline:
                active = {}
                for book in page_items(services["komga"], "/api/v1/books"):
                    if book.get("deleted") or book.get("libraryId") not in selected:
                        continue
                    try:
                        path = map_komga_path(book["url"], self.settings.path_mappings, self.settings.library_root)
                    except ConfigError:
                        continue
                    if path in expected:
                        active[path] = book
                valid = all(path in active and active[path].get("sizeBytes") == size and active[path].get("fileHash")
                            and active[path].get("media", {}).get("status") == "READY" for path, size in expected.items())
                stable = stable + 1 if valid else 0
                if stable >= 2:
                    return {"libraries": len(selected), "books": len(expected), "analyzed": True}
                time.sleep(.5)
            raise ConfigError("Komgaのスキャン・解析が完了していません。完了後に処理を再開してください。")
        if key == "inventory":
            return services["inventory"].refresh()
        if key in {"bibliography", "classification"}:
            if auto_apply and self.settings.komga_url:
                from .komga_batch import KomgaBatchService
                pending = self.store.cached_prefix("automation:operation:" + config.get("run_id", "") + ":")
                for item in pending.values():
                    previous = KomgaBatchService(self.settings, self.store, services["komga"]).inspect(item["data"]["operation_id"])
                    if previous["status"] not in {"completed", "restored"}:
                        raise StateConflict("中断したKomga反映が残っています。下の操作リンクから確認・実行または復元してから再開してください。")
            providers = [provider for provider in ("rakuten_books", "rakuten_kobo", "mangaupdates") if services["enrichment"].status(provider)["configured"]] if key == "bibliography" else ["anilist"]
            results = [self.enrich(provider, auto_apply) for provider in providers if services["enrichment"].status(provider)["configured"]]
            if auto_apply and self.settings.komga_url:
                from .komga_batch import KomgaBatchService
                batch = KomgaBatchService(self.settings, self.store, services["komga"])
                for mode in ("metadata", "authors") if key == "bibliography" else ("metadata",):
                    try:
                        operation = batch.prepare(mode, **({"fields": ["reading", "publisher", "summary", "genres", "tags"]} if mode == "metadata" else {}))
                    except ConfigError as error:
                        if not str(error).startswith("Komgaへ反映する変更はありません。"):
                            raise
                    else:
                        identifier = operation["plan"]["id"]
                        self.store.cache("maintenance:operation:" + identifier, {"kind": "komga", "created": timestamp()})
                        self.store.cache("automation:operation:" + config.get("run_id", "") + ":" + identifier,
                                         {"operation_id": identifier, "run_id": config.get("run_id", ""), "mode": mode, "created": timestamp()})
                        batch.execute(operation["plan"]["id"], operation["digest"])
                        results.append({"operation_id": operation["plan"]["id"], "mode": mode})
            return {"results": results}
        if key == "anime":
            return services["feeds"].refresh("anime")
        if key == "new_volumes":
            return services["new_volumes"].check([row for row in services["inventory"].cached()["series"] if row["status"] == "ongoing"])
        if key == "discovery":
            return {"sources": [services["feeds"].refresh(kind) for kind in ("curated", "manba", "natalie", "rakuten")]}
        if key == "rankings":
            from .feeds import CATEGORIES, season
            return {"sources": [services["feeds"].refresh("ranking", season()["year"], category) for category in CATEGORIES]}
        if key == "shelves":
            operation = services["smart_shelves"].prepare()
            if auto_apply:
                return services["smart_shelves"].execute(operation["plan"]["id"], operation["digest"])
            return {"operation_id": operation["plan"]["id"], "review_required": True}
        raise ConfigError("自動更新のステップが見つかりません。")

    def run(self, identifier, progress):
        run = self.store.cached("automation:run:" + identifier)["data"]
        # Serialize across Web workers; no host timers or production scripts.
        with self.locked():
            run["status"] = "running"
            self.store.cache("automation:run:" + identifier, run)
            for index, key in enumerate(run["config"]["steps"]):
                if run["steps"].get(key, {}).get("status") != "completed":
                    run["steps"][key] = {"status": "running", "updated": timestamp()}
                    self.store.cache("automation:run:" + identifier, run)
                    try:
                        result = self.step(key, {**run["config"], "run_id": identifier})
                        run["steps"][key] = {"status": "completed", "result": result, "updated": timestamp()}
                    except (ConfigError, CatalogError, KomgaError, StateConflict) as error:
                        run["steps"][key] = {"status": "failed", "error": str(error), "updated": timestamp()}
                        run["status"] = "failed"
                        self.store.cache("automation:run:" + identifier, run)
                        raise
                    self.store.cache("automation:run:" + identifier, run)
                progress(index + 1, len(run["config"]["steps"]))
            run["status"] = "completed"
            self.store.cache("automation:run:" + identifier, run)
            return {"run_id": identifier, "steps": len(run["steps"])}

    def due(self, now=None):
        now = now or datetime.now(ZoneInfo("Asia/Tokyo"))
        config = self.config()
        if not config["enabled"] or now.strftime("%H:%M") < config["time"]:
            return None
        try:
            self.store.cache("automation:claim:" + now.strftime("%Y-%m-%d"), {"claimed": timestamp()}, revision=0)
        except StateConflict:
            return None
        try:
            return self.submit()
        except (ConfigError, StateConflict) as error:
            self.store.cache("automation:last_error", {"error": str(error), "updated": timestamp()})
            return None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def loop():
            while not self._stop.wait(20):
                try:
                    self.due()
                except Exception:
                    self.store.cache("automation:last_error", {"error": "自動更新を開始できませんでした。設定・保存先を確認してください。", "updated": timestamp()})
        self._thread = Thread(target=loop, daemon=True, name="manga-kit-scheduler")
        self._thread.start()

    def shutdown(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
