"""Confirmed CBZ copies; originals and Komga records are never replaced."""
import os
from pathlib import Path, PurePosixPath
import shutil
from uuid import uuid4

from .archives import build_cbz, inspect_archive, name_key
from .config import ConfigError
from .filesystem import LibraryFilesystem, file_hash
from .durability import durable_mkdir, sync_directory, sync_file
from .operations import OperationService, digest
from .state import StateConflict, timestamp


class ConversionService(OperationService):
    kind = "conversion"

    def output_path(self, root):
        path = Path(root).expanduser().resolve()
        for existing in (self.settings.library_root.resolve(), self.settings.data_root.resolve()):
            if path.is_relative_to(existing) or existing.is_relative_to(path):
                raise ConfigError("変換先は元ライブラリ・実行データと重ならない別のフォルダにしてください。")
        if not path.is_dir():
            raise ConfigError("変換先のフォルダを作成してから指定してください。")
        return path

    def prepare(self, selected, output):
        if not selected or len(selected) > self.settings.max_files:
            raise ConfigError("変換する書庫の相対パスを --book で指定してください。")
        root = self.output_path(output)
        with self.locked() as fs, LibraryFilesystem(root) as destination:
            entries, targets = [], set()
            for relative in selected:
                snapshot = fs.snapshot(relative)
                target = str(PurePosixPath(relative).with_suffix(".cbz"))
                key = name_key(target)
                if key in targets:
                    raise ConfigError("複数の書庫が同じCBZ名になります。別の計画で変換してください。")
                targets.add(key)
                destination.ensure_absent(target)
                archive = inspect_archive(fs, relative)
                fs.verify(relative, snapshot)
                entries.append({"source": relative, "target": target, "snapshot": snapshot, "archive": archive})
            if any(name_key(str(parent)) in targets for entry in entries for parent in PurePosixPath(entry["target"]).parents
                   if str(parent) != "."):
                raise ConfigError("変換先のCBZ名と作品フォルダが衝突します。")
            plan = {"id": uuid4().hex, "kind": self.kind, "namespace": self.store.namespace, "created": timestamp(),
                    "root_identity": fs.identity, "komga_url": self.settings.komga_url,
                    "output_root": str(root), "output_identity": destination.identity,
                    "entries": entries, "originals_retained": True}
            operation = {"plan": plan, "digest": digest(plan), "status": "planned", "generated": {}}
            durable_mkdir(self._folder(plan["id"]))
            self._save(operation)
            return operation

    def destination(self, plan):
        root = self.output_path(plan["output_root"])
        output = LibraryFilesystem(root)
        if output.identity != plan["output_identity"]:
            output.close()
            raise StateConflict("変換先のルートが差し替えられています。")
        return output

    @staticmethod
    def forward_check(fs, output, entries):
        for entry in entries:
            fs.verify(entry["source"], entry["snapshot"])
            output.ensure_absent(entry["target"])

    def execute(self, operation_id, confirmation):
        with self.locked() as fs:
            operation = self._confirmed(operation_id, confirmation, fs)
            if operation["status"] != "planned":
                raise StateConflict("この計画は実行済み、または生成物の取り消しが必要です。")
            with self.destination(operation["plan"]) as output:
                entries = operation["plan"]["entries"]
                self.forward_check(fs, output, entries)
                folder = self._folder(operation_id)
                required = sum(entry["snapshot"]["size"] for entry in entries)
                expanded = sum(entry["archive"]["estimated_output_bytes"] for entry in entries)
                work = max(entry["archive"]["expanded_bytes"] for entry in entries)
                if shutil.disk_usage(folder).free < required + expanded + work + 1024 * 1024:
                    raise ConfigError("元書庫のバックアップ・展開・CBZ生成の空き容量が不足しています。")
                if shutil.disk_usage(operation["plan"]["output_root"]).free < expanded + 1024 * 1024:
                    raise ConfigError("変換先の空き容量が不足しています。")
                if folder.stat().st_dev == output.identity["device"] and shutil.disk_usage(folder).free < required + expanded * 2 + work + 1024 * 1024:
                    raise ConfigError("共有ディスクのバックアップ・展開・出力の空き容量が不足しています。")
                backup, stage = folder / "backup", folder / ("cbz-" + uuid4().hex)
                durable_mkdir(backup)
                if backup.is_symlink():
                    raise ConfigError("バックアップの保存先が不正です。")
                for index, entry in enumerate(entries):
                    path = backup / str(index)
                    if path.exists():
                        if file_hash(path) != entry["snapshot"]["sha256"]:
                            raise StateConflict("不完全なバックアップがあります。元書庫から新しい計画を作成してください。")
                    else:
                        fs.backup(entry["source"], path, entry["snapshot"])
                    sync_file(path)
                sync_directory(backup)
                self.forward_check(fs, output, entries)
                durable_mkdir(stage)
                job_id = self.store.create_job("conversion")
                operation.update(status="running", job_id=job_id)
                self._save(operation)
                self.store.update_job(job_id, status="running", total=len(entries))
                try:
                    # Build and verify every archive before publishing the first one.
                    with LibraryFilesystem(stage) as staged:
                        for index, entry in enumerate(entries):
                            fs.verify(entry["source"], entry["snapshot"])
                            path = stage / f"{index}.cbz"
                            build_cbz(fs, entry["source"], entry["archive"], path)
                            fs.verify(entry["source"], entry["snapshot"])
                            path.chmod(entry["snapshot"]["mode"])
                            os.utime(path, ns=(entry["snapshot"]["mtime_ns"], entry["snapshot"]["mtime_ns"]))
                        self.forward_check(fs, output, entries)
                        for index, entry in enumerate(entries):
                            snapshot = staged.snapshot(f"{index}.cbz")
                            def record(value):
                                operation["generated"][str(index)] = value
                                self._save(operation)
                            self._copy_backup(output, operation, entry["target"], stage / f"{index}.cbz", snapshot, record)
                            self.store.update_job(job_id, current=index + 1)
                    operation["status"] = "completed"
                    self._save(operation)
                    self.store.update_job(job_id, status="succeeded", result={"operation_id": operation_id,
                        "files": len(entries), "pages": sum(entry["archive"]["pages"] for entry in entries),
                        "originals_retained": True, "backup_bytes": required})
                    return operation
                except Exception as exc:
                    operation["status"] = "failed"
                    self._save(operation)
                    self.store.update_job(job_id, status="failed", error="CBZ生成に失敗しました。元書庫は保持されています。操作IDから生成物を取り消せます。")
                    raise StateConflict("CBZ生成を完了できませんでした。元書庫を確認し、操作IDから生成物を取り消してください。") from exc
                finally:
                    shutil.rmtree(stage, ignore_errors=True)

    def restore(self, operation_id, confirmation):
        with self.locked() as fs:
            operation = self._confirmed(operation_id, confirmation, fs)
            if operation["status"] not in {"completed", "failed", "running", "restoring"}:
                raise StateConflict("この変換は生成物の取り消し対象ではありません。")
            with self.destination(operation["plan"]) as output:
                entries = operation["plan"]["entries"]
                for index, entry in enumerate(entries):
                    backup = self._folder(operation_id) / "backup" / str(index)
                    if backup.parent.is_symlink() or file_hash(backup) != entry["snapshot"]["sha256"]:
                        raise StateConflict("元書庫のバックアップを検証できませんでした。")
                    current = output.existing(entry["target"])
                    expected = operation["generated"].get(str(index))
                    if current is not None and current != expected:
                        raise StateConflict("生成物が変更または差し替えられています。削除せず中止しました。")
                self._cleanup_temporary(output, operation)
                job_id = self.store.create_job("conversion_restore")
                operation.update(status="restoring", restore_job_id=job_id)
                self._save(operation)
                self.store.update_job(job_id, status="running", total=len(entries))
                try:
                    for index, entry in enumerate(entries):
                        current = output.existing(entry["target"])
                        if current is not None:
                            output.verify(entry["target"], operation["generated"][str(index)])
                            with output.parent(entry["target"]) as (fd, name):
                                os.unlink(name, dir_fd=fd)
                                os.fsync(fd)
                        self.store.update_job(job_id, current=index + 1)
                    operation["status"] = "restored"
                    self._save(operation)
                    self.store.update_job(job_id, status="succeeded", result={"operation_id": operation_id,
                        "files": len(operation["generated"]), "originals_retained": True})
                    return operation
                except Exception as exc:
                    operation["status"] = "failed"
                    self._save(operation)
                    self.store.update_job(job_id, status="failed", error="生成物を取り消せませんでした。変更されたファイルを確認してください。")
                    raise StateConflict("生成物の取り消しを完了できませんでした。操作記録を確認してください。") from exc
