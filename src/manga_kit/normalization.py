"""Incremental check, reviewed normalization, and journaled recovery."""
from dataclasses import asdict
import os
from pathlib import PurePosixPath
import shutil
import tempfile

from .archives import FORMATS, build_cbz, inspect_archive
from .config import ConfigError
from .komga import KomgaError
from .durability import durable_mkdir, sync_directory, sync_file
from .filesystem import LibraryFilesystem, file_hash
from .metadata import FIELDS
from .operations import OperationService, digest
from .organizer import plan_organization
from .state import StateConflict


def needs_rebuild(recipe):
    pages = [member for member in recipe["members"] if "page" in member]
    ordered = sorted(pages, key=lambda member: member["page"])
    return (recipe["format"] != "ZIP" or any(member["source"] != member["target"] for member in recipe["members"])
            or pages != ordered)


class NormalizationOperations(OperationService):
    kind = "normalize"

    def prepare(self, layout=None, selected=None, *, archives=None):
        if archives and self.settings.komga_url:
            from .managed_rebuild import supported
            supported(self)
        return super().prepare(layout, selected, archives=archives)

    @staticmethod
    def _remove(fs, path, expected):
        fs.verify(path, expected)
        with fs.parent(path) as (parent, name):
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                    expected["device"], expected["inode"], expected["size"], expected["mtime_ns"]):
                raise StateConflict("削除直前にファイルが変わりました。上書きせず中止しました。")
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)

    def execute(self, operation_id, confirmation):
        operation = self.inspect(operation_id)
        if self.settings.komga_url and any("archive" in entry for entry in operation["plan"]["entries"]):
            from .managed_rebuild import execute
            with self.store.tool_lock("managed-normalization"):
                return execute(self, operation_id, confirmation)
        # Pure moves retain the proven Komga hash/progress guard unchanged.
        if not any("archive" in entry for entry in self.inspect(operation_id)["plan"]["entries"]):
            return super().execute(operation_id, confirmation)
        with self.locked() as fs:
            operation = self._confirmed(operation_id, confirmation, fs)
            if operation["status"] != "planned" or self.settings.komga_url:
                raise StateConflict("再生成を実行できない状態です。操作記録を確認してください。")
            self._forward_check(fs, operation)
            entries = operation["plan"]["entries"]
            folder, backup = self._folder(operation_id), self._folder(operation_id) / "backup"
            required = sum(entry["snapshot"]["size"] for entry in entries)
            expanded = sum(entry.get("archive", {}).get("expanded_bytes", 0) * 2 for entry in entries)
            if shutil.disk_usage(folder).free < required + expanded + 1024 * 1024:
                raise ConfigError("バックアップ・CBZ生成の空き容量が不足しています。")
            if shutil.disk_usage(self.settings.library_root).free < expanded + 1024 * 1024:
                raise ConfigError("生成CBZをライブラリへ配置する空き容量が不足しています。")
            durable_mkdir(backup)
            if backup.is_symlink():
                raise StateConflict("バックアップの保存先が不正です。")
            for index, entry in enumerate(entries):
                path = backup / str(index)
                if path.exists():
                    if path.is_symlink() or file_hash(path) != entry["snapshot"]["sha256"]:
                        raise StateConflict("バックアップが一致しません。新しい計画を作成してください。")
                else:
                    fs.backup(entry["source"], path, entry["snapshot"])
                sync_file(path)
            sync_directory(backup)
            self._forward_check(fs, operation)
            job_id = self.store.create_job("normalize")
            operation.update(status="running", job_id=job_id, generated={})
            self._save(operation)
            self.store.update_job(job_id, status="running", total=len(entries))
            try:
                with tempfile.TemporaryDirectory(prefix="normalize-", dir=folder) as directory:
                    from pathlib import Path
                    stage = Path(directory)
                    # Every rewritten archive is built and verified before any source is removed.
                    for index, entry in enumerate(entries):
                        if "archive" not in entry:
                            continue
                        path = stage / f"{index}.cbz"
                        build_cbz(fs, entry["source"], entry["archive"], path)
                        path.chmod(entry["snapshot"]["mode"])
                        os.utime(path, ns=(entry["snapshot"]["mtime_ns"], entry["snapshot"]["mtime_ns"]))
                        sync_file(path)
                    sync_directory(stage)
                    self._forward_check(fs, operation)
                    with LibraryFilesystem(stage) as staged:
                        for index, entry in enumerate(entries):
                            operation["intent"] = index
                            self._save(operation)
                            if "archive" in entry:
                                generated = staged.snapshot(f"{index}.cbz")
                                if entry["source"] == entry["target"]:
                                    self._remove(fs, entry["source"], entry["snapshot"])
                                def record(value, key=str(index)):
                                    operation["generated"][key] = value
                                    self._save(operation)
                                self._copy_backup(fs, operation, entry["target"], stage / f"{index}.cbz", generated, record)
                                if entry["source"] != entry["target"]:
                                    self._remove(fs, entry["source"], entry["snapshot"])
                                operation["moved_count"] += 1
                            elif entry["source"] != entry["target"]:
                                fs.move(entry["source"], entry["target"], entry["snapshot"])
                                operation["moved_count"] += 1
                            operation.pop("intent", None)
                            self._save(operation)
                            self.store.update_job(job_id, current=index + 1)
                operation["edits_after"] = self.store.relocate_edits(operation["plan"]["groups"], operation["plan"]["edits"], operation_id)
                operation["status"] = "completed"
                self._save(operation)
                self.store.update_job(job_id, status="succeeded", result={"operation_id": operation_id,
                    "files": operation["moved_count"], "rewritten": len(operation["generated"]), "backup_bytes": required})
                return operation
            except Exception as error:
                operation["status"] = "failed"
                self._save(operation)
                self.store.update_job(job_id, status="failed", error="ノーマライズが中断しました。操作記録から復元できます。")
                raise StateConflict("ノーマライズを完了できませんでした。同じ操作の復元を実行してください。") from error

    def restore(self, operation_id, confirmation):
        operation = self.inspect(operation_id)
        if operation["status"] == "restored":
            if confirmation != operation["digest"]:
                raise ConfigError("確認値が一致しません。")
            return operation
        if self.settings.komga_url and any("archive" in entry for entry in operation["plan"]["entries"]):
            from .managed_rebuild import restore
            with self.store.tool_lock("managed-normalization"):
                return restore(self, operation_id, confirmation)
        if any("archive" in entry for entry in operation["plan"]["entries"]):
            with self.locked() as fs:
                operation = self._confirmed(operation_id, confirmation, fs)
                if operation["status"] not in {"running", "failed", "completed", "restoring"} or self.settings.komga_url:
                    raise StateConflict("この操作は復元できない状態です。")
                self.store.verify_edits(self.store.operation_edits(operation_id) or operation["plan"]["edits"])
                entries = operation["plan"]["entries"]
                # Check all backups and every original/generated file before removing anything.
                for index, entry in enumerate(entries):
                    backup = self._folder(operation_id) / "backup" / str(index)
                    if backup.parent.is_symlink() or file_hash(backup) != entry["snapshot"]["sha256"]:
                        raise StateConflict("バックアップを検証できませんでした。")
                    original = operation.get("recovered", {}).get(str(index), entry["snapshot"])
                    for path in {entry["source"], entry["target"]}:
                        actual = fs.existing(path)
                        if actual is None:
                            continue
                        allowed = [original] if path == entry["source"] else []
                        allowed.append(operation.get("generated", {}).get(str(index)) if "archive" in entry else entry["snapshot"])
                        if actual not in allowed:
                            raise StateConflict("生成物または復元先が変更されています。上書きせず中止しました。")
                operation["status"] = "restoring"
                self._save(operation)
                for index, entry in enumerate(entries):
                    if "archive" not in entry:
                        continue
                    actual = fs.existing(entry["target"])
                    generated = operation.get("generated", {}).get(str(index))
                    if generated is not None and actual == generated:
                        self._remove(fs, entry["target"], generated)
                    if fs.existing(entry["source"]) is None:
                        def record(value, key=str(index)):
                            operation.setdefault("recovered", {})[key] = value
                            self._save(operation)
                        self._copy_backup(fs, operation, entry["source"], self._folder(operation_id) / "backup" / str(index),
                                          entry["snapshot"], record)
        return super().restore(operation_id, confirmation)


class NormalizationService:
    def __init__(self, settings, library, store, jobs):
        self.settings, self.library, self.store, self.jobs = settings, library, store, jobs
        self.operations = NormalizationOperations(settings, store)

    def _context(self, series):
        return digest({key: getattr(series, key) for key in FIELDS})

    def check(self, progress):
        scanned = self.library.scan(refresh=True, reuse_metadata=True)
        rules = digest(asdict(self.settings.layout))
        previous = self.store.normalization_files()
        inventory, changed, recipes, archive_errors = {}, {}, {}, {}
        with LibraryFilesystem(self.settings.library_root) if not self.settings.demo else _demo_filesystem() as fs:
            for series in scanned.series:
                for book in series.books:
                    signature = fs.signature(book.path) if fs else {"demo": True}
                    value = {"signature": signature, "metadata": self._context(series), "rules": rules}
                    inventory[book.path] = value
                    if previous.get(book.path) != value:
                        changed[book.path] = "added" if book.path not in previous else "modified"
                        if fs and PurePosixPath(book.path).suffix.lower() in FORMATS:
                            try:
                                recipe = inspect_archive(fs, book.path, page_order="lexicographic-path")
                                if needs_rebuild(recipe):
                                    recipes[book.path] = recipe
                            except (ConfigError, StateConflict, OSError):
                                archive_errors[book.path] = "書庫を検証できません。破損・暗号化・ページ順・ComicInfo.xmlを確認してください。"
                    progress(len(inventory), scanned.stats()["books"])
            preview = plan_organization(scanned.series, self.settings.layout, None if self.settings.demo else self.settings.library_root, normalize=True)
            owners = {book.path: series for series in scanned.series for book in series.books}
            affected = {owners[path].id for path in changed}
            folders = [PurePosixPath(series.path) for series in scanned.series if series.id in affected and series.path != "."]
            affected.update(series.id for series in scanned.series
                            if any(PurePosixPath(series.path).is_relative_to(folder) for folder in folders))
            items = [item for item in preview["items"] if owners[item["source"]].id in affected]
            for item in items:
                item["change"] = changed.get(item["source"], "existing")
                item["series_id"] = owners[item["source"]].id
                if owners[item["source"]].path == ".":
                    item["issues"].append("作品フォルダの外にあります。作品ごとのフォルダへ入れてから確認してください。")
                if (fs and item["source"] not in changed and item["state"] == "ready"
                        and PurePosixPath(item["source"]).suffix.lower() in FORMATS):
                    try:
                        recipe = inspect_archive(fs, item["source"], page_order="lexicographic-path")
                        if needs_rebuild(recipe):
                            recipes[item["source"]] = recipe
                    except (ConfigError, StateConflict, OSError):
                        archive_errors[item["source"]] = "書庫を検証できません。内容を確認してください。"
                if item["source"] in archive_errors:
                    item["issues"].append(archive_errors[item["source"]])
                if item["source"] in recipes:
                    recipe = recipes[item["source"]]
                    pages = sorted((member for member in recipe["members"] if "page" in member), key=lambda member: member["page"])
                    item.update(pages=recipe["pages"], rebuild=True,
                                page_changes=[{"source": member["source"], "target": member["target"]} for member in pages[:8]])
                    if item["state"] == "unchanged":
                        item["state"] = "ready"
                if item["issues"]:
                    item["state"] = "blocked"
            blocked = {item["series_id"] for item in items if item["state"] == "blocked"}
            blocked_folders = [PurePosixPath(series.path) for series in scanned.series if series.id in blocked]
            for item in items:
                parent = PurePosixPath(owners[item["source"]].path)
                if item["state"] == "ready" and (item["series_id"] in blocked or any(folder.is_relative_to(parent) for folder in blocked_folders)):
                    item["state"] = "blocked"
                    item["issues"].append("同じ作品または入れ子の作品に要確認項目があります。作品全体を確認してください。")
                    blocked.add(item["series_id"])
            selected = {item["series_id"] for item in items if item["state"] == "ready"} - blocked
            operation, preparation_error = None, ""
            if selected and self.settings.execution_enabled and not self.settings.demo and not scanned.truncated:
                try:
                    active_recipes = {path: recipe for path, recipe in recipes.items() if owners[path].id in selected}
                    operation = self.operations.prepare(selected=sorted(selected), archives=active_recipes)
                    displayed = {item["source"]: item["target"] for item in items}
                    if any(entry["kind"] == "book" and displayed.get(entry["source"]) != entry["target"]
                           for entry in operation["plan"]["entries"]):
                        raise StateConflict("確認中に配置が変わりました。再確認してください。")
                    for path, value in inventory.items():
                        if fs.signature(path) != value["signature"]:
                            raise StateConflict("確認中にライブラリが変わりました。再確認してください。")
                except (ConfigError, StateConflict, KomgaError) as error:
                    preparation_error = str(error)
                    operation = None
        removed = sorted(set(previous) - set(inventory))
        if not scanned.truncated and not scanned.warnings:
            compliant = {item["source"]: inventory[item["source"]] for item in items if item["state"] == "unchanged"}
            self.store.remember_normalization(compliant, removed)
        return {"items": items, "inventory": inventory, "removed": removed, "truncated": scanned.truncated,
                "warnings": scanned.warnings, "layout": asdict(self.settings.layout), "preparation_error": preparation_error,
                "operation_id": operation["plan"]["id"] if operation else None, "digest": operation["digest"] if operation else None,
                "counts": {**{state: sum(item["state"] == state for item in items) for state in ("ready", "blocked", "unchanged")},
                           "added": sum(value == "added" for value in changed.values()), "modified": sum(value == "modified" for value in changed.values()),
                           "removed": len(removed), "scanned": len(inventory)}}

    def submit_check(self):
        return self.jobs.submit_task("normalization_check", self.check)

    def checked(self, job_id):
        job = self.store.job(job_id, include_result=True)
        if job is None or job["kind"] != "normalization_check" or job["status"] != "succeeded":
            raise ConfigError("完了した追加・変更分の確認結果を選んでください。")
        return job["result"]

    def execute(self, job_id, confirmation):
        result = self.checked(job_id)
        if not result["operation_id"] or confirmation != result["digest"]:
            raise StateConflict("実行できる計画を確認してから実行してください。")
        scanned = self.library.scan(refresh=True, reuse_metadata=True)
        current = {book.path: self._context(series) for series in scanned.series for book in series.books}
        if scanned.truncated or set(current) != set(result["inventory"]) or asdict(self.settings.layout) != result["layout"]:
            raise StateConflict("差分の確認後にライブラリまたはルールが変わりました。再確認してください。")
        with LibraryFilesystem(self.settings.library_root) as fs:
            for path, expected in result["inventory"].items():
                if fs.signature(path) != expected["signature"] or current[path] != expected["metadata"]:
                    raise StateConflict("差分の確認後に書庫または作品情報が変わりました。再確認してください。")
        operation = self.operations.execute(result["operation_id"], confirmation)
        normalized = self.library.scan(refresh=True, reuse_metadata=True)
        self._remember(result, operation, normalized)
        output = {"operation_id": operation["plan"]["id"], "status": operation["status"], "files": operation["moved_count"]}
        if getattr(self, "after_execute", None):
            try:
                output["post_import_job"] = self.after_execute()
            except (ConfigError, StateConflict):
                output["post_import_error"] = "ノーマライズは完了しました。取り込み後の処理は管理の自動更新から実行してください。"
        return output

    def submit_action(self, job_id, confirmation, *, restore=False):
        result = self.checked(job_id)
        if not result["operation_id"] or confirmation != result["digest"]:
            raise StateConflict("確認した操作を選んでください。")
        operation = self.operations.inspect(result["operation_id"])
        allowed = {"completed", "failed", "running", "restoring"} if restore else {"planned"}
        if operation["status"] not in allowed:
            raise StateConflict("この操作は既に処理済みか、復元が必要な状態です。")
        action = self.restore if restore else self.execute
        return self.jobs.submit_task("normalization_restore" if restore else "normalization_execute",
                                     lambda progress: action(job_id, confirmation))

    def _remember(self, result, operation, normalized):
        values, removed = {}, []
        contexts = {book.path: self._context(series) for series in normalized.series for book in series.books}
        pending = {item["source"] for item in result["items"] if item["state"] in {"ready", "blocked"}}
        with LibraryFilesystem(self.settings.library_root) as fs:
            for index, entry in enumerate(operation["plan"]["entries"]):
                if entry["kind"] != "book":
                    continue
                expected = operation.get("generated", {}).get(str(index), entry["snapshot"])
                fs.verify(entry["target"], expected)
                original = result["inventory"][entry["source"]]
                values[entry["target"]] = {**original, "signature": fs.signature(entry["target"]),
                                            "metadata": contexts[entry["target"]]}
                if entry["source"] != entry["target"]:
                    removed.append(entry["source"])
            for path, original in result["inventory"].items():
                if path in values or path in removed:
                    continue
                if path not in pending:
                    try:
                        if fs.signature(path) == original["signature"]:
                            values[path] = original
                    except FileNotFoundError:
                        pass
        self.store.remember_normalization(values, removed)

    def restore(self, job_id, confirmation):
        result = self.checked(job_id)
        if not result["operation_id"] or confirmation != result["digest"]:
            raise StateConflict("復元する操作を確認してください。")
        operation = self.operations.restore(result["operation_id"], confirmation)
        self.library.scan(refresh=True, reuse_metadata=True)
        return {"operation_id": operation["plan"]["id"], "status": operation["status"]}


def _demo_filesystem():
    from contextlib import nullcontext
    return nullcontext(None)
