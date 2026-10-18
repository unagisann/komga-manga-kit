"""Per-book author changes with a durable intent before every external PATCH."""
from copy import deepcopy
from pathlib import PurePosixPath
import re
from urllib.parse import quote
from uuid import uuid4

from .authors import author_values
from .config import ConfigError
from .durability import durable_mkdir
from .komga import KomgaError
from .komga_metadata import KomgaMetadataService
from .metadata import MetadataService, fingerprint
from .operations import digest
from .state import StateConflict, timestamp


class KomgaAuthorsService(KomgaMetadataService):
    kind = "komga_authors"

    def _book(self, path, identity):
        expected = identity["books"][path]
        book = self.client.get("/api/v1/books/" + quote(expected["id"], safe=""))
        if (not isinstance(book, dict) or book.get("id") != expected["id"]
                or book.get("seriesId") != identity["series_id"] or book.get("libraryId") != identity["library_id"]
                or self.guard.relative(book.get("url", "")) != path or book.get("fileHash") != expected["hash"]
                or book.get("deleted") is not False or not isinstance(book.get("media"), dict)
                or book["media"].get("status") != "READY"):
            raise StateConflict("Komgaの巻ID・パス・ハッシュの対応が変わっています。")
        metadata = book.get("metadata")
        if not isinstance(metadata, dict) or type(metadata.get("authorsLock")) is not bool:
            raise KomgaError("Komgaの著者情報・ロックの形式が不正です。")
        try:
            authors = author_values(metadata.get("authors"), deduplicate=False)
        except ConfigError:
            raise KomgaError("Komgaの著者一覧を保持可能な形式で取得できません。") from None
        if authors != metadata["authors"]:
            raise KomgaError("Komgaの著者表記をそのまま復元できません。正規化を確認してください。")
        return {"authors": authors, "authorsLock": metadata["authorsLock"]}

    def prepare(self, series_id, remote_id, authors, *, selected=None, replace_lists=False, replace_locked=False):
        authors = author_values(authors)
        if not authors and not replace_lists:
            raise ConfigError("著者を指定してください。空一覧による削除には --replace-lists が必要です。")
        if type(replace_lists) is not bool or type(replace_locked) is not bool:
            raise ConfigError("置き換えの指定は true / false で指定してください。")
        if remote_id is not None and (not isinstance(remote_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", remote_id)):
            raise ConfigError("Komga作品IDの形式が不正です。省略するとパス対応から特定します。")
        if selected is not None and (not isinstance(selected, list) or not selected
                or any(not isinstance(path, str) for path in selected) or len(set(selected)) != len(selected)):
            raise ConfigError("対象巻は重複のない相対パスで指定してください。")
        with self.locked() as fs:
            if not self.settings.komga_url or not self.settings.path_mappings:
                raise ConfigError("Komgaの接続先とパス対応を設定してください。")
            if self.library.scan(refresh=True).truncated:
                raise ConfigError("スキャン上限に達した状態では反映できません。")
            try:
                base, series, edit = MetadataService(self.library, self.store).series(series_id)
            except KeyError:
                raise ConfigError("対象のローカル作品が見つかりません。") from None
            paths = {book.path for book in series.books}
            if selected is not None and set(selected) - paths:
                raise ConfigError("指定した巻は対象作品にありません。")
            if len(paths if selected is None else selected) > 50:
                raise ConfigError("著者反映は1計画50巻までです。--book で分けて指定してください。")
            entries = [{"source": path, "kind": "book", "snapshot": fs.snapshot(path)} for path in sorted(paths)]
            sidecar = str(PurePosixPath(series.path) / "series.json")
            if snapshot := fs.existing(sidecar):
                entries.append({"source": sidecar, "kind": "sidecar", "snapshot": snapshot})
            identity, _ = self._target(entries, series.path, remote_id)
            books = []
            for path in sorted(paths if selected is None else selected):
                before = self._book(path, identity)
                values = deepcopy(authors) if replace_lists else deepcopy(before["authors"])
                if not replace_lists:
                    for item in authors:
                        if item not in values:
                            values.append(deepcopy(item))
                if len(values) > 100:
                    raise ConfigError("反映後の著者一覧が100件を超えます。")
                after = {"authors": values, "authorsLock": True}
                if before["authorsLock"] and before["authors"] != values and not replace_locked:
                    raise StateConflict("ロック済みの巻の著者を変更するには --replace-locked が必要です。")
                if before != after:
                    books.append({"path": path, "id": identity["books"][path]["id"], "before": before, "after": after})
            if not books:
                raise ConfigError("著者一覧とロックは反映済みです。変更はありません。")
            plan = {"id": uuid4().hex, "kind": self.kind, "namespace": self.store.namespace, "created": timestamp(),
                    "root_identity": fs.identity, "komga_url": self.settings.komga_url, "path_mappings": self._mappings(),
                    "series_id": series.id, "series_path": series.path, "series_title": series.title,
                    "base_fingerprint": fingerprint(base), "edits": {series.id: edit}, "entries": entries,
                    "komga": identity, "books": books, "authors": authors, "replace_lists": replace_lists,
                    "replace_locked": replace_locked}
            operation = {"plan": plan, "digest": digest(plan), "status": "planned", "attempted": []}
            durable_mkdir(self._folder(plan["id"]))
            self._save(operation)
            return operation

    def _check(self, fs, operation, *, local_edits=False):
        self._check_identity(fs, operation, local_edits=local_edits)
        plan = operation["plan"]
        return {book["path"]: self._book(book["path"], plan["komga"]) for book in plan["books"]}

    def _patch_book(self, book, values):
        self.client.request("PATCH", "/api/v1/books/" + quote(book["id"], safe="") + "/metadata", values)

    def _restore_check(self, operation, current):
        recovery = operation.get("_fallback") or operation.get("recovery_from_fallback")
        for book in operation["plan"]["books"]:
            value = current[book["path"]]
            if value not in (book["before"], book["after"]):
                raise StateConflict("反映後に巻の著者・ロックが編集されています。上書きせず中止しました。")
            if not recovery and book["path"] not in operation["attempted"] and value != book["before"]:
                raise StateConflict("未送信の巻が編集されています。復元では変更しません。")

    def _run_books(self, operation, fs, *, restore=False):
        plan = operation["plan"]
        current = self._check(fs, operation, local_edits=not restore)
        if restore:
            self._restore_check(operation, current)
        elif current != {book["path"]: book["before"] for book in plan["books"]}:
            raise StateConflict("計画後に巻の著者・ロックが変わっています。")
        if operation.get("_fallback"):
            operation["recovery_from_fallback"] = True
        job_id = self.store.create_job(self.kind + ("_restore" if restore else ""))
        operation.update(status="restoring" if restore else "running")
        operation["restore_job_id" if restore else "job_id"] = job_id
        self._save(operation)
        self.store.update_job(job_id, status="running", total=len(plan["books"]))
        expected = deepcopy(current)
        try:
            for index, book in enumerate(reversed(plan["books"]) if restore else plan["books"]):
                if self._check(fs, operation, local_edits=not restore) != expected:
                    raise StateConflict("確認後に対象の巻が編集されています。")
                target = book["before"] if restore else book["after"]
                if expected[book["path"]] != target:
                    if not restore:
                        operation["attempted"].append(book["path"])
                    operation["intent"] = {"action": "restore" if restore else "apply", "book": book["path"]}
                    self._save(operation)  # No external write before this durable intent.
                    if self._check(fs, operation, local_edits=not restore) != expected:
                        raise StateConflict("送信直前に対象の巻が編集されています。")
                    self._patch_book(book, target)
                    expected[book["path"]] = target
                    if self._check(fs, operation) != expected:
                        raise StateConflict("巻の著者の反映結果を確認できません。")
                operation.pop("intent", None)
                self._save(operation)
                self.store.update_job(job_id, current=index + 1)
            operation["status"] = "restored" if restore else "completed"
            self._save(operation)
            changes = [{"field": "authors", "book_path": book["path"],
                        "before": book["after" if restore else "before"]["authors"],
                        "after": book["before" if restore else "after"]["authors"],
                        "before_lock": book["after" if restore else "before"]["authorsLock"],
                        "after_lock": book["before" if restore else "after"]["authorsLock"]} for book in plan["books"]]
            self.store.update_job(job_id, status="succeeded", current=len(plan["books"]), result={
                "operation_id": plan["id"], "series_title": plan["series_title"], "books": len(plan["books"]), "changes": changes})
            return operation
        except Exception as exc:
            operation["status"] = "failed"
            self._save(operation)
            self.store.update_job(job_id, status="failed", error="著者反映を完了できませんでした。再送せず操作IDから復元してください。")
            raise StateConflict("著者反映を完了できませんでした。操作記録と各巻を確認して復元してください。") from exc

    def execute(self, operation_id, confirmation):
        with self.locked() as fs:
            operation = self._confirmed(operation_id, confirmation, fs)
            if operation.get("_fallback") or operation["status"] != "planned":
                raise StateConflict("この計画は再実行できません。先に操作記録から復元してください。")
            return self._run_books(operation, fs)

    def restore(self, operation_id, confirmation):
        with self.locked() as fs:
            operation = self._confirmed(operation_id, confirmation, fs)
            if operation["status"] not in {"completed", "failed", "running", "restoring"} and not (
                    operation["status"] == "planned" and operation.get("_fallback")):
                raise StateConflict("この著者操作は復元の対象ではありません。")
            return self._run_books(operation, fs, restore=True)
