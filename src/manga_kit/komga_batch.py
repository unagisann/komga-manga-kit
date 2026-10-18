"""Many-series Komga writes, identified by file attributes and journaled per series.

Archives are not read: size, inode and times identify them locally, and Komga's
path, size and analysed hash identify its records. Moves keep full content checks.
"""
from collections import Counter
from copy import deepcopy
from pathlib import PurePosixPath
from urllib.parse import quote
from uuid import uuid4

from .authors import author_candidates, author_values
from .completion import completed_metadata, completion_policy
from .config import ConfigError
from .durability import durable_mkdir
from .komga import KomgaError
from .komga_metadata import FIELD_MAP, planned_metadata, selected_metadata
from .metadata import apply_edits, fingerprint, validate_fields
from .operations import OperationService, digest
from .state import StateConflict, timestamp

MODES = ("metadata", "completion", "authors")
PAGE_SIZE = 500
MAX_TARGETS = 5000
MAX_BOOK_PATCHES = 50000


def remote_fields(plan, target):
    return ["status", "tags"] if plan["mode"] == "completion" else [FIELD_MAP[field] for field in target["fields"]]


def book_authors(book):
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


def batch_summary(operation):
    """Reviewable plan without per-file signatures, for CLI output."""
    plan = operation["plan"]
    targets = [{key: target[key] for key in ("series_id", "series_title", "series_path") if key in target}
               | {"komga_series_id": target.get("komga_series_id", target.get("komga", {}).get("series_id"))}
               | {key: target[key] for key in ("changes", "authors", "title_sort", "reading", "row") if key in target}
               for target in plan["targets"]]
    return {"operation_id": plan["id"], "digest": operation["digest"], "kind": plan["kind"], "mode": plan.get("mode"),
            "status": operation["status"], "attempted": len(operation.get("attempted", [])), "targets": targets,
            "counts": plan["counts"], "skipped": plan["skipped"]}


class KomgaBatchService(OperationService):
    kind = "komga_batch"

    @property
    def client(self):
        return self.guard.client

    def _mappings(self):
        return [{"komga": item.komga, "local": str(item.local)} for item in self.settings.path_mappings]

    def _list(self, endpoint, condition):
        result, page = [], 0
        while True:
            response = self.client.request("POST", endpoint, {"condition": condition}, size=PAGE_SIZE, page=page)
            if (not isinstance(response, dict) or not isinstance(response.get("content"), list)
                    or any(not isinstance(item, dict) for item in response["content"])):
                raise KomgaError("Komgaの一覧の応答形式が不正です。")
            result.extend(response["content"])
            if not response["content"] or response.get("last", len(result) >= response.get("totalElements", len(result))):
                return result
            if len(result) > 200000:
                raise ConfigError("Komgaの一覧を安全に取得できませんでした。")
            page += 1

    def _condition(self, library_id, series_id=None):
        conditions = [{"libraryId": {"operator": "is", "value": library_id}}, {"deleted": {"operator": "isFalse"}}]
        if series_id is not None:
            conditions.append({"seriesId": {"operator": "is", "value": series_id}})
        return {"allOf": conditions}

    def _index(self):
        """One listing of the library's books and series, keyed by local path and ID."""
        try:
            library = self.guard.library()
            books, series = {}, {}
            for book in self._list("/api/v1/books/list", self._condition(library["id"])):
                path = self.guard.relative(book["url"])
                if (path is None or path in books or not isinstance(book["id"], str) or not book["id"]
                        or not isinstance(book["seriesId"], str)):
                    raise KomgaError("Komgaの書籍のID・パス対応を一意に確認できません。")
                books[path] = book
            for item in self._list("/api/v1/series/list", self._condition(library["id"])):
                if not isinstance(item["id"], str) or item["id"] in series or not isinstance(item["metadata"], dict):
                    raise KomgaError("Komgaの作品の応答形式が不正です。")
                series[item["id"]] = item
        except (KeyError, TypeError, AttributeError):
            raise KomgaError("Komgaの書籍・作品の応答形式を確認できません。") from None
        return {"library_id": library["id"], "books": books, "series": series,
                "counts": Counter(book["seriesId"] for book in books.values())}

    def _identity(self, index, series, signatures):
        """Match a local series by path, book set and sizes; return (identity, remote series) or (None, reason)."""
        remote_books = {}
        for book in series.books:
            remote = index["books"].get(book.path)
            if remote is None:
                return None, "Komgaに登録されていない巻があります。Komgaのスキャンを確認してください。"
            media = remote.get("media")
            if (not isinstance(media, dict) or media.get("status") != "READY"
                    or not isinstance(remote.get("fileHash"), str) or not remote["fileHash"]):
                return None, "Komgaで解析・ハッシュ計算が終わっていない巻があります。"
            if remote.get("sizeBytes") != signatures[book.path]["size"]:
                return None, "Komgaの記録と書庫のサイズが異なります。Komgaを再スキャンしてください。"
            remote_books[book.path] = remote
        series_ids = {remote["seriesId"] for remote in remote_books.values()}
        if len(series_ids) != 1:
            return None, "巻が複数のKomga作品に分かれています。"
        remote_id = next(iter(series_ids))
        remote_series = index["series"].get(remote_id)
        if remote_series is None or self.guard.relative(remote_series.get("url", "")) != series.path:
            return None, "Komga作品の保存先がローカルの作品フォルダと一致しません。"
        if index["counts"][remote_id] != len(remote_books):
            return None, "Komga作品にローカルにない巻があります。"
        return {"series_id": remote_id, "books": {path: {"id": book["id"], "hash": book["fileHash"], "size": book["sizeBytes"]}
                                                  for path, book in remote_books.items()}}, remote_series

    def _locked(self, before, after, fields, replace_locked):
        locked = [field for field in fields if before[field + "Lock"] and before[field] != after[field]]
        if locked and not replace_locked:
            raise StateConflict(f"Komgaでロック済みの項目（{', '.join(locked)}）があります。--replace-locked で含めます。")

    def _change(self, plan, series, remote, index, identity):
        options, mode = plan["options"], plan["mode"]
        if mode == "authors":
            if not series.author:
                raise ConfigError("ローカルの著者が未設定です。")
            authors = author_candidates(series.author)
            if not authors:
                raise ConfigError("ローカルの著者表記から著者を判定できません。")
            books, changes = [], []
            for path in sorted(identity["books"]):
                before = book_authors(index["books"][path])
                values = deepcopy(authors) if options["replace_lists"] else deepcopy(before["authors"])
                if not options["replace_lists"]:
                    values += [deepcopy(item) for item in authors if item not in values]
                if len(values) > 100:
                    raise ConfigError("反映後の著者一覧が100件を超えます。")
                after = {"authors": values, "authorsLock": True}
                if before["authorsLock"] and before["authors"] != values and not options["replace_locked"]:
                    raise StateConflict("Komgaで著者がロック済みの巻があります。--replace-locked で含めます。")
                if before != after:
                    books.append({"path": path, "id": identity["books"][path]["id"], "before": before, "after": after})
                    changes.append({"field": "authors", "book_path": path, "before": before["authors"], "after": values,
                                    "before_lock": before["authorsLock"], "after_lock": True})
            return {"authors": authors, "books": books, "changes": changes} if books else None
        metadata = remote["metadata"]
        if mode == "completion":
            fields, targets = None, ["status", "tags"]
            before = selected_metadata(metadata, targets)
            after = completed_metadata(before, plan["completion"])
        else:
            values = validate_fields({field: getattr(series, field) for field in options["fields"]})
            # An empty local value never clears Komga; use the single-series command for that.
            fields = [field for field in options["fields"] if values[field]]
            if not fields:
                return None
            targets = [FIELD_MAP[field] for field in fields]
            before = selected_metadata(metadata, targets)
            after = planned_metadata(before, values, fields, replace_lists=options["replace_lists"],
                                     fill_only=not options["overwrite"], komga_title=str(metadata.get("title", "")))
        self._locked(before, after, targets, options["replace_locked"])
        local = {remote: field for field, remote in FIELD_MAP.items()} | {"status": "status"}
        changes = [{"field": local[field], "komga_field": field, "before": before[field], "after": after[field],
                    "before_lock": before[field + "Lock"], "after_lock": after[field + "Lock"]} for field in targets
                   if before[field] != after[field] or before[field + "Lock"] != after[field + "Lock"]]
        if not changes:
            return None
        return {**({"fields": fields} if fields is not None else {}), "before": before, "after": after, "changes": changes}

    def _scan(self):
        scanned = self.library.scan(refresh=True, edited=False, reuse_metadata=True)
        if scanned.truncated:
            raise ConfigError("スキャン上限に達した状態ではKomgaへ反映できません。")
        return scanned

    def _selection(self, scanned, series_ids):
        if series_ids is None:
            return list(scanned.series)
        if (not isinstance(series_ids, list) or not series_ids or len(set(series_ids)) != len(series_ids)
                or any(not isinstance(item, str) for item in series_ids)):
            raise ConfigError("対象の作品IDを重複なく指定してください。")
        known = {entry.id: entry for entry in scanned.series}
        if set(series_ids) - known.keys():
            raise ConfigError("指定した作品が見つかりません。")
        return [entry for entry in scanned.series if entry.id in set(series_ids)]

    def _entries(self, fs, series):
        entries = [{"source": book.path, "kind": "book", "signature": fs.signature(book.path)} for book in series.books]
        sidecar = str(PurePosixPath(series.path) / "series.json")
        if signature := fs.existing_signature(sidecar):
            entries.append({"source": sidecar, "kind": "sidecar", "signature": signature})
        return entries

    def prepare(self, mode, series_ids=None, *, fields=None, overwrite=False, replace_locked=False, replace_lists=False,
                ongoing_tag="連載中", completed_tag="完結"):
        if mode not in MODES:
            raise ConfigError("mode は metadata / completion / authors から選んでください。")
        if any(type(flag) is not bool for flag in (overwrite, replace_locked, replace_lists)):
            raise ConfigError("指定は true / false で指定してください。")
        if mode == "metadata":
            if (not isinstance(fields, list) or not fields or len(fields) != len(set(fields))
                    or any(field not in FIELD_MAP for field in fields)):
                raise ConfigError("反映する項目は title / reading / publisher / summary / genres / tags から重複なく選んでください。")
        elif fields:
            raise ConfigError("--field は metadata の一括反映だけで指定できます。")
        if mode != "metadata" and overwrite:
            raise ConfigError("--overwrite は metadata の一括反映だけで指定できます。")
        plan = {"id": uuid4().hex, "kind": self.kind, "mode": mode, "namespace": self.store.namespace, "created": timestamp(),
                "komga_url": self.settings.komga_url, "path_mappings": self._mappings(),
                "options": {"fields": fields, "overwrite": overwrite, "replace_locked": replace_locked,
                            "replace_lists": replace_lists}}
        if mode == "completion":
            plan["completion"] = completion_policy(ongoing_tag, completed_tag)
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
                if mode == "completion" and series.status != "completed":
                    if series_ids is not None:
                        skipped.append({"series_id": base.id, "series_title": series.title,
                                        "reason": "このツールで完結にしていません。"})
                    continue
                entries = self._entries(fs, base)
                identity, remote = self._identity(index, base, {entry["source"]: entry["signature"] for entry in entries})
                if identity is None:
                    skipped.append({"series_id": base.id, "series_title": series.title, "reason": remote})
                    continue
                try:
                    change = self._change(plan, series, remote, index, identity)
                except (ConfigError, KomgaError, StateConflict) as exc:
                    skipped.append({"series_id": base.id, "series_title": series.title, "reason": str(exc)})
                    continue
                if change is None:
                    counts["unchanged"] += 1
                    continue
                targets.append({"series_id": base.id, "series_path": base.path, "series_title": series.title,
                                "base_fingerprint": fingerprint(base),
                                "edit": edits.get(base.id, {"fields": {}, "revision": 0}),
                                "entries": entries, "komga": identity, **change})
            counts.update(targets=len(targets), skipped=len(skipped))
            if not targets:
                reasons = Counter(item["reason"] for item in skipped)
                detail = "".join(f" {reason}（{count}件）" for reason, count in reasons.most_common(3))
                raise ConfigError("Komgaへ反映する変更はありません。" + detail)
            if len(targets) > MAX_TARGETS or sum(len(target.get("books", [])) for target in targets) > MAX_BOOK_PATCHES:
                raise ConfigError(f"1計画は{MAX_TARGETS}作品・{MAX_BOOK_PATCHES}巻までです。--series で分けてください。")
            plan.update(root_identity=fs.identity, library_id=index["library_id"], targets=targets,
                        skipped=skipped, counts=dict(counts))
            operation = {"plan": plan, "digest": digest(plan), "status": "planned", "attempted": []}
            durable_mkdir(self._folder(plan["id"]))
            self._save(operation)
            return operation

    # Checks shared by execute and restore.

    def _local_check(self, fs, plan, *, local_edits):
        if self._mappings() != plan["path_mappings"]:
            raise StateConflict("計画後にKomgaのパス対応が変わっています。")
        for target in plan["targets"]:
            for entry in target["entries"]:
                if fs.existing_signature(entry["source"]) != entry["signature"]:
                    raise StateConflict(f"計画後に書庫が変更されています（{target['series_title']}）。計画を作り直してください。")
        if local_edits:
            self.store.verify_edits({target["series_id"]: target["edit"] for target in plan["targets"]})
            bases = {entry.id: entry for entry in self._scan().series}
            for target in plan["targets"]:
                base = bases.get(target["series_id"])
                if base is None or fingerprint(base) != target["base_fingerprint"]:
                    raise StateConflict(f"計画後に書庫の作品情報が変わっています（{target['series_title']}）。")

    def _remote_check(self, index, plan, target):
        identity = target["komga"]
        if index["library_id"] != plan["library_id"] or index["counts"][identity["series_id"]] != len(identity["books"]):
            raise StateConflict(f"Komgaの作品・書籍の対応が変わっています（{target['series_title']}）。")
        for path, expected in identity["books"].items():
            book = index["books"].get(path)
            if (book is None or book["id"] != expected["id"] or book.get("fileHash") != expected["hash"]
                    or book.get("sizeBytes") != expected["size"] or book["seriesId"] != identity["series_id"]):
                raise StateConflict(f"Komgaの書籍IDやハッシュが変わっています（{target['series_title']}）。")
        series = index["series"].get(identity["series_id"])
        if series is None or self.guard.relative(series.get("url", "")) != target["series_path"]:
            raise StateConflict(f"Komga作品の保存先が変わっています（{target['series_title']}）。")

    def _values(self, plan, target, *, after=False):
        if plan["mode"] == "authors":
            return {book["path"]: book["after" if after else "before"] for book in target["books"]}
        return target["after" if after else "before"]

    def _current(self, plan, target, metadata=None, books=None):
        if plan["mode"] == "authors":
            return {book["path"]: book_authors(books[book["path"]]) for book in target["books"]}
        return selected_metadata(metadata, remote_fields(plan, target))

    def _indexed(self, index, plan, target):
        return self._current(plan, target, index["series"][target["komga"]["series_id"]]["metadata"], index["books"])

    def _apply(self, plan, target, *, restore):
        """Write one series after re-reading it; Komga has no conditional PATCH."""
        expected, other = self._values(plan, target, after=not restore), self._values(plan, target, after=restore)
        series_id = target["komga"]["series_id"]
        if plan["mode"] == "authors":
            books = {}
            for book in self._list("/api/v1/books/list", self._condition(plan["library_id"], series_id)):
                books[self.guard.relative(book.get("url", ""))] = book
            for planned in target["books"]:
                book = books.get(planned["path"])
                if book is None or book.get("id") != planned["id"]:
                    raise StateConflict(f"Komgaの巻IDが変わっています（{target['series_title']}）。")
                current = book_authors(book)
                if current == expected[planned["path"]]:
                    continue
                if current != other[planned["path"]]:
                    raise StateConflict(f"送信直前にKomgaの著者が変わっています（{target['series_title']}）。")
                self.client.request("PATCH", "/api/v1/books/" + quote(planned["id"], safe="") + "/metadata",
                                    expected[planned["path"]])
            return
        series = self.client.get("/api/v1/series/" + quote(series_id, safe=""))
        if (not isinstance(series, dict) or series.get("id") != series_id or series.get("libraryId") != plan["library_id"]
                or series.get("deleted") is not False or self.guard.relative(series.get("url", "")) != target["series_path"]):
            raise StateConflict(f"Komga作品のID・保存先を確認できません（{target['series_title']}）。")
        current = self._current(plan, target, series.get("metadata"))
        if current == expected:
            return
        if current != other:
            raise StateConflict(f"送信直前にKomgaの作品情報が変わっています（{target['series_title']}）。")
        self.client.request("PATCH", "/api/v1/series/" + quote(series_id, safe="") + "/metadata", expected)

    def _run(self, operation, *, restore=False):
        plan, targets = operation["plan"], operation["plan"]["targets"]
        attempted = set(operation["attempted"])
        order = [index for index in reversed(range(len(targets))) if index in attempted] if restore else range(len(targets))
        job_id = self.store.create_job(self.kind + ("_restore" if restore else ""))
        operation.update(status="restoring" if restore else "running")
        operation["restore_job_id" if restore else "job_id"] = job_id
        self._save(operation)
        self.store.update_job(job_id, status="running", total=len(order))
        try:
            for count, index in enumerate(order, 1):
                if not restore:
                    # Record the intent durably before any request for this series.
                    operation["attempted"].append(index)
                    self._save(operation)
                self._apply(plan, targets[index], restore=restore)
                self.store.update_job(job_id, current=count)
            final = self._index()
            for target in targets:
                self._remote_check(final, plan, target)
                if self._indexed(final, plan, target) != self._values(plan, target, after=not restore):
                    raise StateConflict(f"Komgaへの反映結果を確認できません（{target['series_title']}）。")
            operation["status"] = "restored" if restore else "completed"
            self._save(operation)
            rows = [{"series_title": targets[index]["series_title"], "series_path": targets[index]["series_path"],
                     "changes": [{**change, "before": change["after"], "after": change["before"],
                                  "before_lock": change["after_lock"], "after_lock": change["before_lock"]}
                                 for change in targets[index]["changes"]] if restore else targets[index]["changes"]}
                    for index in order]
            self.store.update_job(job_id, status="succeeded", result={"operation_id": plan["id"], "mode": plan["mode"],
                "series": len(rows), "books": sum(len(targets[index].get("books", [])) for index in order),
                "targets": rows})
            return operation
        except Exception as exc:
            operation["status"] = "failed"
            self._save(operation)
            self.store.update_job(job_id, status="failed", error="Komgaへの一括操作を完了できませんでした。操作記録を確認して復元してください。")
            raise StateConflict("Komgaへの一括操作を完了できませんでした。再送せず、操作IDから復元してください。") from exc

    def execute(self, operation_id, confirmation):
        with self.locked() as fs:
            operation = self._confirmed(operation_id, confirmation, fs)
            if operation.get("_fallback"):
                raise StateConflict("現行の操作記録を読み取れません。直前の記録を使って先に復元してください。")
            if operation["status"] != "planned":
                raise StateConflict("この計画は実行済み、または復元が必要な状態です。")
            plan = operation["plan"]
            self._local_check(fs, plan, local_edits=True)
            index = self._index()
            for target in plan["targets"]:
                self._remote_check(index, plan, target)
                if self._indexed(index, plan, target) != self._values(plan, target):
                    raise StateConflict(f"計画後にKomgaの対象項目が編集されています（{target['series_title']}）。計画を作り直してください。")
            return self._run(operation)

    def restore(self, operation_id, confirmation):
        with self.locked() as fs:
            operation = self._confirmed(operation_id, confirmation, fs)
            recovery = bool(operation.get("_fallback") or operation.get("recovery_from_fallback"))
            if (operation["status"] not in {"completed", "failed", "running", "restoring"}
                    and not (operation["status"] == "planned" and recovery)):
                raise StateConflict("この操作は復元の対象ではありません。")
            plan = operation["plan"]
            self._local_check(fs, plan, local_edits=False)
            index = self._index()
            attempted = set(operation["attempted"])
            for number, target in enumerate(plan["targets"]):
                self._remote_check(index, plan, target)
                current, before, after = (self._indexed(index, plan, target), self._values(plan, target),
                                          self._values(plan, target, after=True))
                sent = recovery or number in attempted
                if plan["mode"] == "authors":
                    unexpected = any(current[path] not in ((before[path], after[path]) if sent else (before[path],))
                                     for path in current)
                else:
                    unexpected = current not in ((before, after) if sent else (before,))
                if unexpected:
                    raise StateConflict(f"反映後にKomgaの対象項目が編集されています（{target['series_title']}）。上書きせず中止しました。")
            if recovery:
                # The newest journal may be lost; any series could have been sent.
                operation["recovery_from_fallback"] = True
                operation["attempted"] = list(range(len(plan["targets"])))
            return self._run(operation, restore=True)
