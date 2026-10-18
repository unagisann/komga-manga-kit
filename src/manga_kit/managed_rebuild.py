"""Two-stage Komga normalization: hash-preserving moves, then in-place rebuilds."""
from dataclasses import asdict, replace
from pathlib import PurePosixPath
import os
import time
from urllib.parse import quote
from uuid import uuid4

from .config import ConfigError
from .durability import durable_mkdir
from .filesystem import file_hash
from .komga_moves import locked_metadata, read_progress
from .operations import OperationService, digest
from .state import StateConflict, timestamp

VERSIONS = {"1.29.1"}


def supported(owner):
    info = owner.guard.client.get("/actuator/info")
    if info.get("build", {}).get("version") not in VERSIONS:
        raise ConfigError("Komga連携中の書庫再生成は、検証済みのKomga 1.29.1で利用してください。")
    owner.guard.library()


def child_service(owner):
    from .normalization import NormalizationOperations
    owner.settings.tools.validate()
    settings = getattr(owner.settings, "_current", owner.settings)
    return NormalizationOperations(replace(settings, komga_url=""), owner.store)


def current_native(owner, operation, *, baseline=None):
    supported(owner)
    entries = operation["plan"]["entries"]
    mapped = {owner.guard.relative(book["url"]): book for book in owner.guard.books()}
    result = {"books": {}, "library_id": operation["plan"]["komga"]["library_id"], "series": {}}
    for entry in entries:
        if entry["kind"] != "book":
            continue
        path = entry["target"]
        book = mapped.get(path)
        if not book or book["libraryId"] != result["library_id"]:
            raise StateConflict("Komgaの再生成対象を確認できません。スキャンと操作記録を確認してください。")
        if baseline and book["id"] != baseline["books"][path]["id"]:
            raise StateConflict("再生成対象のKomga書籍IDが変わっています。上書きせず中止しました。")
        result["books"][path] = {"id": book["id"], "hash": book.get("fileHash"), "progress": read_progress(book),
                                 "locked": locked_metadata(book["metadata"])}
        parent = str(PurePosixPath(path).parent)
        if parent not in result["series"]:
            series = owner.guard.client.get("/api/v1/series/" + quote(book["seriesId"], safe=""))
            result["series"][parent] = {"id": series["id"], "locked": locked_metadata(series["metadata"])}
    return result


def verify_native(owner, baseline, hashes, *, ready):
    owner.guard.client.request("POST", "/api/v1/libraries/" + quote(baseline["library_id"], safe="") + "/scan")
    deadline = time.monotonic() + 90
    while True:
        mapped = {owner.guard.relative(book["url"]): book for book in owner.guard.books()}
        valid = True
        for path, expected in baseline["books"].items():
            actual = mapped.get(path)
            if (not actual or actual["id"] != expected["id"] or actual.get("fileHash") != hashes[path]
                    or read_progress(actual) != expected["progress"] or locked_metadata(actual["metadata"]) != expected["locked"]
                    or ready and actual.get("media", {}).get("status") != "READY"):
                valid = False
                break
        if valid:
            for expected in baseline["series"].values():
                series = owner.guard.client.get("/api/v1/series/" + quote(expected["id"], safe=""))
                if locked_metadata(series["metadata"]) != expected["locked"]:
                    valid = False
                    break
        if valid:
            return
        if time.monotonic() >= deadline:
            raise StateConflict("再生成後のKomga書籍ID・既読情報・ロック済み情報を検証できません。操作記録から復元してください。")
        time.sleep(.5)


def prepare_child(owner, operation):
    child = child_service(owner)
    with child.locked() as fs:
        entries = [{"source": entry["target"], "target": entry["target"], "kind": "book",
                    "snapshot": fs.snapshot(entry["target"]), "archive": entry["archive"]}
                   for entry in operation["plan"]["entries"] if "archive" in entry]
        plan = {"id": uuid4().hex, "kind": "normalize", "purpose": "managed_rebuild_stage", "namespace": owner.store.namespace,
                "created": timestamp(), "root_identity": fs.identity, "komga_url": "", "layout": asdict(owner.settings.layout),
                "entries": entries, "groups": [], "edits": {}, "komga": None}
        result = {"plan": plan, "digest": digest(plan), "status": "planned", "moved_count": 0, "edits_after": None}
        durable_mkdir(child._folder(plan["id"]))
        child._save(result)
    return child, result


def execute(owner, identifier, confirmation):
    supported(owner)
    operation = owner.inspect(identifier)
    if operation["status"] != "planned":
        raise StateConflict("未実行のノーマライズ計画を確認してください。")
    # This stage leaves every byte unchanged. Komga can transfer all users'
    # progress through its native hash-based restoration before hashes change.
    operation = OperationService.execute(owner, identifier, confirmation)
    try:
        operation["managed_native"] = current_native(owner, operation)
        operation["status"] = "running"
        operation["stage"] = "moved"
        owner._save(operation)
        child, prepared = prepare_child(owner, operation)
        operation["rewrite_operation_id"] = prepared["plan"]["id"]
        operation["stage"] = "rebuilding"
        owner._save(operation)
        rebuilt = child.execute(prepared["plan"]["id"], prepared["digest"])
        generated_by_path = {entry["target"]: rebuilt["generated"][str(index)] for index, entry in enumerate(rebuilt["plan"]["entries"])}
        operation["generated"] = {str(index): generated_by_path[entry["target"]] for index, entry in enumerate(operation["plan"]["entries"])
                                  if entry["target"] in generated_by_path}
        owner._save(operation)
        hashes = {entry["target"]: entry["snapshot"]["komga_hash"] for entry in operation["plan"]["entries"] if entry["kind"] == "book"}
        for index, entry in enumerate(rebuilt["plan"]["entries"]):
            hashes[entry["target"]] = rebuilt["generated"][str(index)]["komga_hash"]
        verify_native(owner, operation["managed_native"], hashes, ready=True)
        operation.update(stage="completed", status="completed", moved_count=sum(
            entry["source"] != entry["target"] or "archive" in entry for entry in operation["plan"]["entries"]))
        owner._save(operation)
        return operation
    except Exception:
        operation["status"] = "failed"
        owner._save(operation)
        raise


def restore(owner, identifier, confirmation):
    operation = owner.inspect(identifier)
    if confirmation != operation["digest"]:
        raise ConfigError("確認値が一致しません。")
    if not operation.get("rewrite_operation_id"):
        return OperationService.restore(owner, identifier, confirmation)
    supported(owner)
    child = child_service(owner)
    child_id = operation["rewrite_operation_id"]
    rewritten = child.inspect(child_id)
    by_target = {entry["source"]: (index, entry) for index, entry in enumerate(rewritten["plan"]["entries"])}
    with owner.locked() as fs:
        operation = owner._confirmed(identifier, confirmation, fs)
        if operation["status"] == "restored":
            return operation
        owner.store.verify_edits(owner.store.operation_edits(identifier, "restore") or operation["edits_after"] or operation["plan"]["edits"])
        # Validate all parent backups and all live files before child restoration.
        for index, entry in enumerate(operation["plan"]["entries"]):
            backup = owner._folder(identifier) / "backup" / str(index)
            if backup.parent.is_symlink() or file_hash(backup) != entry["snapshot"]["sha256"]:
                raise StateConflict("元書庫のバックアップを検証できません。")
            allowed = [entry["snapshot"]]
            if entry["target"] in by_target:
                child_index, child_entry = by_target[entry["target"]]
                allowed.extend([rewritten.get("generated", {}).get(str(child_index)), rewritten.get("recovered", {}).get(str(child_index))])
            for path in {entry["source"], entry["target"]}:
                actual = fs.existing(path)
                if actual is not None and actual not in allowed:
                    raise StateConflict("生成物・元の保存先が変更されています。上書きせず中止しました。")
        if operation.get("stage") == "restoring_moves":
            latest = operation["restore_native"]
        else:
            latest = current_native(owner, operation, baseline=operation["managed_native"])
            operation["restore_native"] = latest
            owner._save(operation)
    # The child has its own journal and the same filesystem lock. Do not hold
    # the parent lock while invoking its independently resumable restoration.
    if rewritten["status"] not in {"planned", "restored"}:
        rewritten = child.restore(child_id, rewritten["digest"])
    if operation.get("stage") != "restoring_moves":
        hashes = {entry["target"]: entry["snapshot"]["komga_hash"] for entry in operation["plan"]["entries"] if entry["kind"] == "book"}
        verify_native(owner, latest, hashes, ready=False)
    with owner.locked() as fs:
        operation = owner._confirmed(identifier, confirmation, fs)
        native = owner.guard.restore_snapshot(operation["plan"]["komga"], operation["plan"]["entries"])
        operation.update(status="restoring", stage="restoring_moves", restore_moves_native=native)
        owner._save(operation)
        expected = {}
        for index, entry in enumerate(operation["plan"]["entries"]):
            value = entry["snapshot"]
            if entry["target"] in by_target:
                child_index, child_entry = by_target[entry["target"]]
                value = rewritten.get("recovered", {}).get(str(child_index), child_entry["snapshot"])
            expected[str(index)] = value
            for path in {entry["source"], entry["target"]}:
                actual = fs.existing(path)
                if actual is not None and actual != value:
                    raise StateConflict("復元する書庫が変更されています。上書きせず中止しました。")
            if fs.existing(entry["source"]) is None and fs.existing(entry["target"]) is None:
                raise StateConflict("復元する元書庫が見つかりません。操作記録とバックアップを確認してください。")
        for index, entry in enumerate(operation["plan"]["entries"]):
            if entry["source"] == entry["target"]:
                continue
            source, target = fs.existing(entry["source"]), fs.existing(entry["target"])
            if source is None:
                fs.move(entry["target"], entry["source"], expected[str(index)])
            elif target is not None:
                with fs.parent(entry["target"]) as (parent, name):
                    fs.verify(entry["target"], expected[str(index)])
                    os.unlink(name, dir_fd=parent); os.fsync(parent)
        owner.guard.verify_after(native, operation["plan"]["entries"], operation["plan"]["groups"], restore=True)
        owner.store.relocate_edits(operation["plan"]["groups"], operation["edits_after"], identifier, restore=True)
        operation.update(status="restored", stage="restored")
        owner._save(operation)
        return operation
