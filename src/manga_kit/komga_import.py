"""Readings already curated in Komga's titleSort, imported as local edits in one batch."""
from collections import Counter
import re
import unicodedata
from uuid import uuid4

from .config import ConfigError
from .durability import durable_mkdir
from .japanese import kana_row
from .komga_batch import KomgaBatchService
from .metadata import apply_edits, validate_fields
from .operations import digest
from .provenance import source_links, stored_links
from .state import StateConflict, timestamp

# The leading kana of a titleSort. The existing KomgaTool writes "カナ 元の題名";
# a titleSort written only in kana is used as a whole.
_KANA = re.compile(r"[ぁ-ゖゝゞァ-ヺ・ーヽヾ\s]+")


def reading_from_title_sort(title_sort, title):
    if not isinstance(title_sort, str) or not isinstance(title, str):
        return ""
    text = unicodedata.normalize("NFKC", title_sort).strip()
    # Komga keeps titleSort equal to the title until someone sets it; that is not a reading.
    if not text or text.casefold() == unicodedata.normalize("NFKC", title).strip().casefold():
        return ""
    match = _KANA.match(text)
    reading = re.sub(r"\s+", "", match[0]).strip("・") if match else ""
    return reading if reading.strip("ー") else ""


class KomgaReadingImportService(KomgaBatchService):
    kind = "komga_reading_import"

    def prepare(self, series_ids=None):
        plan = {"id": uuid4().hex, "kind": self.kind, "mode": "reading", "namespace": self.store.namespace,
                "created": timestamp(), "komga_url": self.settings.komga_url, "path_mappings": self._mappings()}
        with self.locked() as fs:
            if not self.settings.komga_url or not self.settings.path_mappings:
                raise ConfigError("Komgaの接続先・管理者APIキー・パス対応を設定してください。")
            scanned = self._scan()
            edits = self.store.edits()
            effective = {entry.id: entry for entry in apply_edits(scanned, edits).series}
            chosen = self._selection(scanned, series_ids)
            index = self._index()
            targets, skipped, counts = [], [], Counter()
            for base in chosen:
                series = effective[base.id]
                # Manual edits, series.json and catalog readings are never replaced.
                if series.reading.strip():
                    counts["reading_set"] += 1
                    continue
                identity, remote = self._identity(index, base, {book.path: fs.signature(book.path) for book in base.books})
                if identity is None:
                    skipped.append({"series_id": base.id, "series_title": series.title, "reason": remote})
                    continue
                metadata = remote["metadata"]
                reading = reading_from_title_sort(metadata.get("titleSort"), metadata.get("title", ""))
                if not reading:
                    counts["no_reading"] += 1
                    continue
                reading = validate_fields({"reading": reading})["reading"]
                targets.append({"series_id": base.id, "series_path": base.path, "series_title": series.title,
                                "komga_series_id": identity["series_id"], "title_sort": metadata["titleSort"],
                                "reading": reading, "row": kana_row(reading),
                                "edit": edits.get(base.id, {"fields": {}, "revision": 0})})
            counts.update(targets=len(targets), skipped=len(skipped))
            if not targets:
                raise ConfigError("取り込める読みはありません。Komgaの並び順が作品名のまま、または読みが設定済みです。")
            plan.update(root_identity=fs.identity, library_id=index["library_id"], targets=targets,
                        skipped=skipped, counts=dict(counts))
            operation = {"plan": plan, "digest": digest(plan), "status": "planned", "attempted": []}
            durable_mkdir(self._folder(plan["id"]))
            self._save(operation)
            return operation

    def _changes(self, plan, imported=None):
        changes = []
        for target in plan["targets"]:
            before = target["edit"]
            if imported is not None:
                changes.append({"series_id": target["series_id"], "expected": imported[target["series_id"]], "edit": before})
                continue
            links = source_links(before.get("provenance", {}))
            links["komga"] = {"provider": "komga", "item_id": target["komga_series_id"], "url": "",
                              "retrieved_at": plan["created"], "applied_fields": ["reading"], "adjusted_fields": []}
            changes.append({"series_id": target["series_id"], "expected": before,
                            "edit": {"fields": {**before["fields"], "reading": target["reading"]},
                                     "provenance": stored_links(links)}})
        return changes

    def _finish(self, operation, restore):
        plan = operation["plan"]
        job_id = self.store.create_job(self.kind + ("_restore" if restore else ""))
        operation.update(status="restoring" if restore else "running")
        operation["restore_job_id" if restore else "job_id"] = job_id
        self._save(operation)
        self.store.update_job(job_id, status="running", total=len(plan["targets"]))
        try:
            if restore:
                imported = self.store.operation_edits(plan["id"], "import")
                # Nothing was saved if the import transaction never committed.
                if imported is not None:
                    self.store.apply_operation_edits(plan["id"], "import_restore", self._changes(plan, imported))
            else:
                self.store.apply_operation_edits(plan["id"], "import", self._changes(plan))
            operation["status"] = "restored" if restore else "completed"
            self._save(operation)
            rows = [{"series_title": target["series_title"], "series_path": target["series_path"],
                     "changes": [{"field": "reading", "before": target["reading"] if restore else "",
                                  "after": "" if restore else target["reading"]}]} for target in plan["targets"]]
            self.store.update_job(job_id, status="succeeded", current=len(rows),
                                  result={"operation_id": plan["id"], "mode": "reading", "series": len(rows), "targets": rows})
            return operation
        except Exception as exc:
            operation["status"] = "failed"
            self._save(operation)
            self.store.update_job(job_id, status="failed", error="読みの取り込みを完了できませんでした。操作記録を確認してください。")
            raise StateConflict("読みの取り込みを完了できませんでした。操作IDから状態を確認してください。") from exc

    def execute(self, operation_id, confirmation):
        with self.locked() as fs:
            operation = self._confirmed(operation_id, confirmation, fs)
            if operation.get("_fallback"):
                raise StateConflict("現行の操作記録を読み取れません。直前の記録を使って先に復元してください。")
            if operation["status"] != "planned":
                raise StateConflict("この計画は実行済み、または復元が必要な状態です。")
            effective = {entry.id: entry for entry in apply_edits(self._scan(), self.store.edits()).series}
            for target in operation["plan"]["targets"]:
                series = effective.get(target["series_id"])
                if series is None or series.path != target["series_path"]:
                    raise StateConflict(f"計画後に作品が移動・削除されています（{target['series_title']}）。")
                if series.reading.strip():
                    raise StateConflict(f"計画後に読みが設定されています（{target['series_title']}）。計画を作り直してください。")
            return self._finish(operation, restore=False)

    def restore(self, operation_id, confirmation):
        with self.locked() as fs:
            operation = self._confirmed(operation_id, confirmation, fs)
            if (operation["status"] not in {"completed", "failed", "running", "restoring"}
                    and not (operation["status"] == "planned" and operation.get("_fallback"))):
                raise StateConflict("この操作は復元の対象ではありません。")
            return self._finish(operation, restore=True)
