"""Single-book page/cover reviews using the proven normalization backup journal."""
from dataclasses import asdict
from pathlib import PurePosixPath
import shutil
from uuid import uuid4

from .archives import IMAGES, inspect_archive
from .config import ConfigError
from .durability import durable_mkdir, sync_directory
from .normalization import NormalizationOperations
from .operations import digest, series_id
from .state import StateConflict, timestamp


class PageEditor(NormalizationOperations):
    def __init__(self, settings, store, inventory, pages):
        super().__init__(settings, store)
        self.inventory, self.pages = inventory, pages

    def prepare_book(self, book_id, selected_pages, *, fingerprint):
        book = self.pages.find(book_id)
        with self.locked() as fs:
            before = fs.snapshot(book["path"])
            recipe = inspect_archive(fs, book["path"], page_order="lexicographic-path")
            if digest({"snapshot": before, "recipe": recipe}) != fingerprint:
                raise StateConflict("ページの確認後に書庫が変わりました。読み直してください。")
            names = {member["source"] for member in recipe["members"] if "page" in member}
            if (not isinstance(selected_pages, list) or not selected_pages or any(not isinstance(value, str) for value in selected_pages)
                    or len(set(selected_pages)) != len(selected_pages) or set(selected_pages) - names):
                raise ConfigError("残すページと順番を選んでください。全ページの削除はできません。")
            if self.settings.komga_url and len(selected_pages) != len(names):
                raise ConfigError("Komga連携中は表紙・並べ替えに対応しています。ページ数が変わる削除の既読位置移行は未対応です。")
            target = str(PurePosixPath(book["path"]).with_suffix(".cbz"))
            if target != book["path"]:
                fs.ensure_absent(target)
            parent = str(PurePosixPath(book["path"]).parent)
            identifier = series_id(parent)
            edits = self.store.edits()
            archive = {"base": recipe, "selected_pages": selected_pages, "expanded_bytes": recipe["expanded_bytes"],
                       "estimated_output_bytes": recipe["estimated_output_bytes"]}
            entries = [{"source": book["path"], "target": target, "kind": "book", "snapshot": before, "archive": archive}]
            groups = [] if self.settings.komga_url else [{"source": parent, "target": parent, "source_id": identifier, "target_id": identifier, "retained": []}]
            if self.settings.komga_url:
                from .managed_rebuild import supported
                supported(self)
            native = self.guard.snapshot(entries, groups)
            plan = {"id": uuid4().hex, "kind": "normalize", "purpose": "page_edit", "namespace": self.store.namespace,
                    "created": timestamp(), "root_identity": fs.identity, "komga_url": self.settings.komga_url, "layout": asdict(self.settings.layout),
                    "entries": entries, "groups": groups,
                    "edits": {identifier: edits.get(identifier, {"fields": {}, "revision": 0})} if groups else {}, "komga": native}
            operation = {"plan": plan, "digest": digest(plan), "status": "planned", "moved_count": 0, "edits_after": None}
            durable_mkdir(self._folder(plan["id"]))
            self._save(operation)
            self.store.cache("pages:operation:" + plan["id"], {"book_id": book_id, "source": book["path"], "created": timestamp(),
                              "volume": book.get("volume", ""),
                              "before": len(names), "after": len(selected_pages), "deleted": len(names) - len(selected_pages)})
            return operation

    def review(self, book_id):
        from .filesystem import LibraryFilesystem
        book = self.pages.find(book_id)
        with LibraryFilesystem(self.settings.library_root) as fs:
            before = fs.snapshot(book["path"])
            recipe = inspect_archive(fs, book["path"], page_order="lexicographic-path")
        names = [member["source"] for member in sorted((member for member in recipe["members"] if "page" in member), key=lambda member: member["page"])]
        return {"book": book, "pages": names, "fingerprint": digest({"snapshot": before, "recipe": recipe})}

    def mark(self, book_id, status):
        self.pages.find(book_id)
        if not isinstance(status, str) or status not in {"review", "hold", "done", "unmarked"}:
            raise ConfigError("確認の状態を選んでください。")
        self.store.cache("pages:mark:" + book_id, {"status": status})

    def restore(self, operation_id, confirmation):
        if self.inspect(operation_id).get("purged"):
            raise ConfigError("バックアップを完全削除したため復元できません。")
        return super().restore(operation_id, confirmation)

    def trash(self):
        rows = []
        for identifier, value in self.store.cached_prefix("pages:operation:").items():
            operation = self.inspect(identifier.removeprefix("pages:operation:"))
            rows.append({**value["data"], "id": operation["plan"]["id"], "digest": operation["digest"],
                         "status": operation["status"], "purged": operation.get("purged")})
        return sorted(rows, key=lambda row: row["created"], reverse=True)

    def purge(self, identifier, confirm):
        with self.locked():
            operation = self.inspect(identifier)
            if operation["plan"].get("purpose") != "page_edit" or confirm != operation["digest"] + ":purge":
                raise ConfigError("完全削除するバックアップを確認してください。")
            if operation["status"] not in {"completed", "restored"}:
                raise StateConflict("実行または復元が完了したバックアップだけを削除できます。")
            folders = [self._folder(identifier)]
            if operation.get("rewrite_operation_id"):
                from .managed_rebuild import child_service
                child = child_service(self)
                child_operation = child.inspect(operation["rewrite_operation_id"])
                if child_operation["plan"].get("purpose") != "managed_rebuild_stage":
                    raise StateConflict("再生成の操作記録が一致しません。")
                folders.append(child._folder(operation["rewrite_operation_id"]))
            for folder in folders:
                if (folder / "backup").is_symlink():
                    raise StateConflict("バックアップの保存先が不正です。")
            for folder in folders:
                backup = folder / "backup"
                if backup.exists():
                    shutil.rmtree(backup)
                    sync_directory(folder)
            operation["purged"] = timestamp()
            self._save(operation)
            return {"purged": True}
