"""Confirmed organization transactions with durable journals and verified backups."""
from contextlib import contextmanager
from collections import defaultdict
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import shutil
import stat
import unicodedata
from uuid import uuid4

from .config import ConfigError
from .filesystem import LibraryFilesystem, file_hash
from .durability import durable_mkdir, sync_directory, sync_file
from .komga import KomgaClient, KomgaError
from .komga_moves import KomgaMoveGuard
from .library import LibraryService
from .state import StateConflict, StateStore, timestamp
from .organizer import plan_organization


def series_id(path):
    return hashlib.sha256(path.encode()).hexdigest()[:16]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class OperationService:
    kind = "organize"
    def __init__(self, settings, store=None, client=None):
        self.settings = settings.validate_storage()
        self.store = store or StateStore(settings)
        self.library = LibraryService(settings, store=self.store)
        self.guard = KomgaMoveGuard(settings, client or KomgaClient(settings))
        self.directory = settings.data_root / "operations" / self.store.namespace

    @contextmanager
    def locked(self):
        if not self.settings.execution_enabled or self.settings.demo:
            raise ConfigError("実ライブラリで execution.enabled: true を設定してから利用してください。")
        if os.name != "posix":
            raise ConfigError("整理の実行はPOSIX環境に限ります。")
        import fcntl
        durable_mkdir(self.directory)
        if not self.directory.resolve().is_relative_to(self.settings.data_root.resolve()):
            raise ConfigError("操作履歴の保存先が実行データ外を指しています。")
        fd = os.open(self.directory / "lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fs = None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StateConflict("別の整理処理が実行中です。完了後に再実行してください。") from exc
            fs = LibraryFilesystem(self.settings.library_root)
            yield fs
        finally:
            if fs is not None:
                fs.close()
            os.close(fd)

    def _folder(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ConfigError("操作IDが不正です。")
        folder = self.directory / operation_id
        if folder.is_symlink() or not folder.resolve().is_relative_to(self.directory.resolve()):
            raise ConfigError("操作履歴のパスが不正です。")
        return folder

    def _save(self, operation):
        folder = self._folder(operation["plan"]["id"])
        plan_file = folder / "plan.json"
        if not plan_file.exists():
            with plan_file.open("x") as output:
                os.chmod(plan_file, 0o600)
                json.dump(operation["plan"], output, ensure_ascii=False, indent=2)
                output.flush()
                os.fsync(output.fileno())
        current = folder / "manifest.json"
        if current.exists() and not operation.get("_fallback"):
            # Keep the last committed state reachable even if the new rename
            # has not reached disk when power is lost.
            previous = folder / ("manifest-previous-" + uuid4().hex)
            os.link(current, previous, follow_symlinks=False)
            os.replace(previous, folder / "manifest.previous.json")
            sync_directory(folder)
        temporary = folder / ("manifest-" + uuid4().hex)
        with temporary.open("x") as output:
            os.chmod(temporary, 0o600)
            json.dump({key: value for key, value in operation.items() if key not in {"plan", "_fallback"}}, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, folder / "manifest.json")
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        # The operation directory itself must also survive a reboot.
        sync_directory(folder.parent)
        operation.pop("_fallback", None)

    def _copy_backup(self, fs, operation, relative, backup, expected, record):
        prefix = ".manga-kit-restore-" + operation["plan"]["id"] + "-"
        def created(path, identity):
            operation.setdefault("temporary_files", {})[path] = identity
            self._save(operation)
        fs.recover_backup(relative, backup, expected, before_publish=record,
                          on_create=created, temporary_prefix=prefix)
        self._cleanup_temporary(fs, operation)

    def _cleanup_temporary(self, fs, operation):
        pending = operation.get("temporary_files", {})
        field = "target" if self.kind == "conversion" else "source"
        parents = {PurePosixPath(entry[field]).parent for entry in operation["plan"]["entries"]}
        prefix = ".manga-kit-restore-" + operation["plan"]["id"] + "-"
        for path, expected in pending.items():
            parsed = PurePosixPath(path)
            if parsed.parent not in parents or not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}", parsed.name):
                raise StateConflict("一時ファイルの記録が操作の対象と一致しません。")
            with fs.parent(path) as (fd, name):
                try:
                    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if (not stat.S_ISREG(current.st_mode)
                        or (current.st_dev, current.st_ino) != (expected["device"], expected["inode"])):
                    raise StateConflict("一時ファイルが差し替えられています。削除せず中止しました。")
        for path in pending:
            with fs.parent(path) as (fd, name):
                try:
                    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    expected = pending[path]
                    if (current.st_dev, current.st_ino) != (expected["device"], expected["inode"]):
                        raise StateConflict("一時ファイルが差し替えられています。")
                    os.unlink(name, dir_fd=fd)
                except FileNotFoundError:
                    pass
                os.fsync(fd)
        if pending:
            operation["temporary_files"] = {}
            self._save(operation)

    def inspect(self, operation_id):
        try:
            folder = self._folder(operation_id)
            operation = None
            for filename in ("manifest.json", "manifest.previous.json"):
                try:
                    value = json.loads((folder / filename).read_text())
                    if not isinstance(value, dict):
                        raise ValueError("Invalid manifest")
                    operation = value
                    if filename != "manifest.json":
                        operation["_fallback"] = True
                    break
                except (OSError, ValueError):
                    continue
            if operation is None:
                raise ValueError("No readable manifest")
            if "plan" not in operation:
                operation["plan"] = json.loads((self._folder(operation_id) / "plan.json").read_text())
        except (OSError, ValueError) as exc:
            raise ConfigError("操作履歴を読み取れません。") from exc
        if (operation.get("digest") != digest(operation["plan"]) or operation["plan"]["id"] != operation_id
                or operation["plan"]["namespace"] != self.store.namespace
                or operation["plan"]["komga_url"] != self.settings.komga_url):
            raise StateConflict("操作履歴または接続設定が変わっています。再確認してください。")
        return operation

    @staticmethod
    def _empty_target(fs, group):
        if group["source"] == group["target"]:
            return
        try:
            probe = str(PurePosixPath(group["target"]) / "__manga_kit_probe__")
            with fs.parent(probe) as (fd, _):
                if os.listdir(fd):
                    raise StateConflict("整理先の作品フォルダに既存の内容があります。作品の統合は未対応です。")
        except FileNotFoundError:
            pass

    def prepare(self, layout=None, selected=None):
        if layout is not None and not isinstance(layout, dict):
            raise ConfigError("整理ルールはマッピングで指定してください。")
        rules = self.settings.layout.with_overrides(layout or {})
        with self.locked() as fs:
            scanned = self.library.scan(refresh=True)
            if scanned.truncated:
                raise ConfigError("スキャン上限に達した状態では整理を実行できません。")
            series = scanned.series
            if selected:
                if set(selected) - {entry.id for entry in series}:
                    raise ConfigError("指定した作品が見つかりません。")
                series = [entry for entry in series if entry.id in selected]
            preview = plan_organization(series, rules, self.settings.library_root)
            if preview["counts"]["blocked"] or not preview["counts"]["ready"]:
                raise ConfigError("要確認項目がない、変更を含むプレビューを作成してください。")
            entries, groups, destinations = [], [], set()
            by_parent = defaultdict(list)
            for item in preview["items"]:
                by_parent[str(PurePosixPath(item["source"]).parent)].append(item)
            for entry in series:
                items = by_parent[entry.path]
                parents = {str(PurePosixPath(item["target"]).parent) for item in items}
                if len(parents) != 1:
                    raise ConfigError("作品を複数のフォルダへ分割することはできません。")
                target = next(iter(parents))
                key = unicodedata.normalize("NFKC", target).casefold()
                if key in destinations:
                    raise ConfigError("複数作品を同じフォルダへ統合することはできません。")
                destinations.add(key)
                group = {"source": entry.path, "target": target, "source_id": entry.id, "target_id": series_id(target)}
                self._empty_target(fs, group)
                groups.append(group)
                for item in items:
                    entries.append({"source": item["source"], "target": item["target"], "kind": "book",
                                    "snapshot": fs.snapshot(item["source"])})
                sidecar = str(PurePosixPath(entry.path) / "series.json")
                existing = fs.existing(sidecar)
                if existing:
                    entries.append({"source": sidecar, "target": str(PurePosixPath(target) / "series.json"),
                                    "kind": "sidecar", "snapshot": existing})
            edits = self.store.edits()
            expected = {key: edits.get(key, {"fields": {}, "revision": 0}) for group in groups
                        for key in (group["source_id"], group["target_id"])}
            for group in groups:
                if group["source_id"] != group["target_id"] and expected[group["target_id"]]["fields"]:
                    raise StateConflict("整理先の作品に保存済みの編集があります。先に確認してください。")
            native = self.guard.snapshot(entries, groups)
            plan = {"id": uuid4().hex, "kind": self.kind, "namespace": self.store.namespace, "created": timestamp(),
                    "root_identity": fs.identity, "komga_url": self.settings.komga_url, "layout": asdict(rules),
                    "entries": entries, "groups": groups, "edits": expected, "komga": native}
            operation = {"plan": plan, "digest": digest(plan), "status": "planned", "moved_count": 0, "edits_after": None}
            durable_mkdir(self._folder(plan["id"]))
            self._save(operation)
            return operation

    def _confirmed(self, operation_id, confirmation, fs):
        operation = self.inspect(operation_id)
        if confirmation != operation["digest"]:
            raise ConfigError("確認した計画の digest を --confirm に指定してください。")
        if operation["plan"].get("kind", "organize") != self.kind:
            raise ConfigError("操作の種類に合った実行・復元処理を使用してください。")
        if fs.identity != operation["plan"]["root_identity"]:
            raise StateConflict("ライブラリのルートが差し替えられています。")
        return operation

    def _forward_check(self, fs, operation):
        plan = operation["plan"]
        self.store.verify_edits(plan["edits"])
        for group in plan["groups"]:
            self._empty_target(fs, group)
        for entry in plan["entries"]:
            fs.verify(entry["source"], entry["snapshot"])
            if entry["source"] != entry["target"]:
                fs.ensure_absent(entry["target"])
        if self.guard.snapshot(plan["entries"], plan["groups"]) != plan["komga"]:
            raise StateConflict("確認後にKomgaの書籍・既読情報が更新されています。")

    def execute(self, operation_id, confirmation):
        with self.locked() as fs:
            operation = self._confirmed(operation_id, confirmation, fs)
            if operation["status"] != "planned":
                raise StateConflict("この計画は実行済み、または復元が必要な状態です。")
            self._forward_check(fs, operation)
            entries, groups = operation["plan"]["entries"], operation["plan"]["groups"]
            backup = self._folder(operation_id) / "backup"
            required = sum(entry["snapshot"]["size"] for entry in entries)
            if shutil.disk_usage(self.directory).free < required + 1024 * 1024:
                raise ConfigError("全対象をバックアップするための空き容量がありません。")
            durable_mkdir(backup)
            if backup.is_symlink():
                raise ConfigError("バックアップの保存先が不正です。")
            for index, entry in enumerate(entries):
                target = backup / str(index)
                if target.exists():
                    if target.is_symlink() or file_hash(target) != entry["snapshot"]["sha256"]:
                        raise StateConflict("不完全なバックアップがあります。元ファイルを確認し、新しい計画を作成してください。")
                else:
                    fs.backup(entry["source"], target, entry["snapshot"])
                # Also flush reused backups from older/retried operations.
                sync_file(target)
            sync_directory(backup)
            self._forward_check(fs, operation)
            job_id = self.store.create_job("organize")
            operation.update(status="running", job_id=job_id)
            self._save(operation)
            self.store.update_job(job_id, status="running", total=len(entries))
            try:
                for index, entry in enumerate(entries):
                    if entry["source"] != entry["target"]:
                        operation["intent"] = index
                        self._save(operation)
                        fs.move(entry["source"], entry["target"], entry["snapshot"])
                        operation["moved_count"] += 1
                        operation.pop("intent", None)
                        self._save(operation)
                    self.store.update_job(job_id, current=index + 1)
                self.guard.verify_after(operation["plan"]["komga"], entries, groups)
                operation["edits_after"] = self.store.relocate_edits(groups, operation["plan"]["edits"], operation_id)
                operation["status"] = "completed"
                self._save(operation)
                self.store.update_job(job_id, status="succeeded", result={"operation_id": operation_id,
                    "files": operation["moved_count"], "backup_bytes": required, "komga_verified": bool(operation["plan"]["komga"])})
                return operation
            except Exception as exc:
                operation["status"] = "failed"
                self._save(operation)
                self.store.update_job(job_id, status="failed", error="整理を完了できませんでした。操作記録を確認して復元してください。")
                raise StateConflict("整理を完了できませんでした。操作IDとバックアップを使って復元してください。") from exc

    def restore(self, operation_id, confirmation):
        with self.locked() as fs:
            operation = self._confirmed(operation_id, confirmation, fs)
            if operation["status"] not in {"completed", "failed", "running", "restoring"}:
                raise StateConflict("この操作は復元の対象ではありません。")
            plan, locations = operation["plan"], []
            operation["edits_after"] = self.store.operation_edits(operation_id) or operation["edits_after"]
            restored_edits = self.store.operation_edits(operation_id, "restore")
            if restored_edits:
                operation["status"] = "restored"
                self._save(operation)
                return operation
            self.store.verify_edits(operation["edits_after"] or plan["edits"])
            for index, entry in enumerate(plan["entries"]):
                source, target = fs.existing(entry["source"]), fs.existing(entry["target"])
                if source is None:
                    fs.ensure_absent(entry["source"])
                for value, expected in ((source, operation.get("recovered", {}).get(str(index), entry["snapshot"])),
                                        (target, operation.get("recovered", {}).get(str(index), entry["snapshot"])
                                         if entry["source"] == entry["target"] else entry["snapshot"])):
                    if value is not None and value != expected:
                        raise StateConflict("復元先の衝突、または移動後のファイル変更があります。上書きせず中止しました。")
                backup = self._folder(operation_id) / "backup" / str(index)
                if backup.parent.is_symlink() or file_hash(backup) != entry["snapshot"]["sha256"]:
                    raise StateConflict("バックアップの検証に失敗しました。")
                locations.append((source, target, backup))
            native = self.guard.restore_snapshot(plan["komga"], plan["entries"])
            self._cleanup_temporary(fs, operation)
            operation["restore_native"] = native
            job_id = self.store.create_job("restore")
            operation.update(status="restoring", restore_job_id=job_id)
            self._save(operation)
            self.store.update_job(job_id, status="running", total=len(locations))
            try:
                for index, (entry, (source, target, backup)) in enumerate(zip(plan["entries"], locations)):
                    def record_recovery(value):
                        # Persist the new inode before publishing its original path.
                        # A crash after link creation can then be retried safely.
                        operation.setdefault("recovered", {})[str(index)] = value
                        self._save(operation)
                    if entry["source"] == entry["target"]:
                        if source is None:
                            self._copy_backup(fs, operation, entry["source"], backup, entry["snapshot"], record_recovery)
                    elif source is None:
                        if target:
                            fs.move(entry["target"], entry["source"], entry["snapshot"])
                        else:
                            self._copy_backup(fs, operation, entry["source"], backup, entry["snapshot"], record_recovery)
                    elif target:
                        # An interrupted link/unlink step can leave both names of the same inode.
                        with fs.parent(entry["target"]) as (fd, name):
                            fs.verify(entry["target"], entry["snapshot"])
                            os.unlink(name, dir_fd=fd)
                            os.fsync(fd)
                    self.store.update_job(job_id, current=index + 1)
                self.guard.verify_after(native, plan["entries"], plan["groups"], restore=True)
                if operation["edits_after"]:
                    self.store.relocate_edits(plan["groups"], operation["edits_after"], operation_id, restore=True)
                operation["status"] = "restored"
                self._save(operation)
                self.store.update_job(job_id, status="succeeded", result={"operation_id": operation_id, "files": len(locations)})
                return operation
            except Exception as exc:
                operation["status"] = "failed"
                self._save(operation)
                self.store.update_job(job_id, status="failed", error="復元を完了できませんでした。バックアップと操作記録を確認してください。")
                raise StateConflict("復元を完了できませんでした。操作記録とバックアップを確認してください。") from exc
