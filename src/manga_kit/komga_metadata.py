"""Confirmed, journaled series-metadata patches and conflict-aware undo."""
from copy import deepcopy
from pathlib import PurePosixPath
import re
from urllib.parse import quote
from uuid import uuid4

from .config import ConfigError
from .completion import completed_metadata, completion_policy
from .durability import durable_mkdir
from .komga import KomgaError
from .metadata import MetadataService, fingerprint, validate_fields
from .operations import OperationService, digest
from .state import StateConflict, timestamp

FIELD_MAP = {"title": "title", "reading": "titleSort", "publisher": "publisher", "summary": "summary",
             "genres": "genres", "tags": "tags"}
COLLECTIONS = {"genres", "tags"}
PLAN_FIELD_MAP = {**FIELD_MAP, "status": "status"}


def selected_metadata(metadata, fields):
    """Compare Komga sets by contents while retaining every original spelling."""
    result = {}
    if not isinstance(metadata, dict):
        raise KomgaError("Komgaの作品情報の形式が不正です。")
    for field in fields:
        value, lock = metadata.get(field), metadata.get(field + "Lock")
        if type(lock) is not bool:
            raise KomgaError("Komgaの作品情報のロックを確認できません。")
        if field in COLLECTIONS:
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise KomgaError("Komgaのタグ・ジャンルの形式が不正です。")
            value = sorted(set(value))
        elif not isinstance(value, str) or field in {"title", "titleSort"} and not value.strip():
            raise KomgaError("Komgaの作品情報の値を確認できません。")
        result[field], result[field + "Lock"] = value, lock
    return result


def planned_metadata(before, values, fields, *, replace_lists=False, fill_only=False, komga_title=""):
    """Komga values after applying local fields; every applied field is locked.

    With fill_only, a non-empty Komga value is kept (a default titleSort equal to
    the title counts as empty), as the existing KomgaTool writeback does.
    """
    after = deepcopy(before)
    for field in fields:
        target, value = FIELD_MAP[field], values[field]
        if field in COLLECTIONS:
            # Komga 1.29 stores these sets in lowercase (lowerNotBlank).
            value = [item.lower() for item in value]
            if not replace_lists:
                value = before[target] + value
            value = sorted(set(value))
        elif fill_only and before[target].strip() and not (
                target == "titleSort" and before[target].strip().casefold() == komga_title.strip().casefold()):
            continue
        after[target], after[target + "Lock"] = value, True
    return after


class KomgaMetadataService(OperationService):
    kind = "komga_metadata"

    @property
    def client(self):
        return self.guard.client

    def _mappings(self):
        return [{"komga": item.komga, "local": str(item.local)} for item in self.settings.path_mappings]

    def _target(self, entries, path, remote_id):
        try:
            return self._read_target(entries, path, remote_id)
        except (KeyError, TypeError, AttributeError, IndexError):
            raise KomgaError("Komgaの作品・書籍の応答形式を確認できません。") from None

    def _read_target(self, entries, path, remote_id):
        if not self.settings.komga_url:
            raise ConfigError("Komgaの接続先・管理者APIキー・パス対応を設定してください。")
        active = self.guard.books()
        native = self.guard.snapshot(entries, [{"source": path}], books=active)
        series_ids = {book["series_id"] for book in native["books"].values()}
        if remote_id is None:
            # The verified book paths identify the Komga series; no ID needs to be typed.
            if len(series_ids) != 1:
                raise StateConflict("ローカル書庫に対応するKomga作品を1件に特定できません。")
            remote_id = next(iter(series_ids))
        if series_ids != {remote_id}:
            raise StateConflict("指定したKomga作品とローカル書庫の対応が一致しません。")
        response = self.client.get("/api/v1/series/" + quote(remote_id, safe=""))
        if (not isinstance(response, dict) or response.get("id") != remote_id
                or response.get("libraryId") != native["library_id"] or response.get("deleted") is not False
                or self.guard.relative(response.get("url", "")) != path):
            raise StateConflict("Komga作品のID・保存先・ライブラリの対応を確認できません。")
        books = [book for book in active if book.get("seriesId") == remote_id]
        identities = {}
        for book in books:
            relative = self.guard.relative(book.get("url", ""))
            expected = native["books"].get(relative)
            if (expected is None or book.get("fileHash") != expected["hash"] or relative in identities
                    or not isinstance(book.get("id"), str) or not book["id"]):
                raise StateConflict("Komga書籍の対応が確認中に変わりました。")
            identities[relative] = {"id": book["id"], "hash": expected["hash"]}
        if set(identities) != set(native["books"]) or len({book["id"] for book in identities.values()}) != len(identities):
            raise StateConflict("Komga作品の書籍構成が確認中に変わりました。")
        return {"library_id": native["library_id"], "series_id": remote_id, "books": identities}, response.get("metadata")

    def prepare(self, series_id, remote_id, fields, *, replace_locked=False, replace_lists=False):
        if (not isinstance(fields, list) or not fields or any(not isinstance(field, str) or field not in FIELD_MAP for field in fields)
                or len(fields) != len(set(fields))):
            raise ConfigError("反映する項目は title / reading / publisher / summary / genres / tags から重複なく選んでください。")
        return self._prepare(series_id, remote_id, fields, replace_locked=replace_locked, replace_lists=replace_lists)

    def prepare_completion(self, series_id, remote_id, *, ongoing_tag="連載中", completed_tag="完結", replace_locked=False):
        policy = completion_policy(ongoing_tag, completed_tag)
        return self._prepare(series_id, remote_id, ["status", "tags"], replace_locked=replace_locked, completion=policy)

    def _prepare(self, series_id, remote_id, fields, *, replace_locked=False, replace_lists=False, completion=None):
        if remote_id is not None and (not isinstance(remote_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", remote_id)):
            raise ConfigError("Komga作品IDの形式が不正です。省略するとパス対応から特定します。")
        if type(replace_locked) is not bool or type(replace_lists) is not bool:
            raise ConfigError("置き換えの指定は true / false で指定してください。")
        with self.locked() as fs:
            if not self.settings.komga_url or not self.settings.path_mappings:
                raise ConfigError("Komgaの接続先・管理者APIキー・パス対応を設定してください。")
            scanned = self.library.scan(refresh=True)
            if scanned.truncated:
                raise ConfigError("スキャン上限に達した状態ではKomgaへ反映できません。")
            try:
                base, series, edit = MetadataService(self.library, self.store).series(series_id)
            except KeyError:
                raise ConfigError("対象のローカル作品が見つかりません。") from None
            if completion is not None and series.status != "completed":
                raise ConfigError("このツールで作品の状態を完結にしてから、Komgaの完結情報を計画してください。")
            values = {} if completion is not None else validate_fields({field: getattr(series, field) for field in fields})
            if "reading" in values and not values["reading"]:
                raise ConfigError("読みが未設定です。読みを保存してから反映してください。")
            entries = [{"source": book.path, "kind": "book", "snapshot": fs.snapshot(book.path)} for book in series.books]
            sidecar = str(PurePosixPath(series.path) / "series.json")
            if snapshot := fs.existing(sidecar):
                entries.append({"source": sidecar, "kind": "sidecar", "snapshot": snapshot})
            identity, metadata = self._target(entries, series.path, remote_id)
            remote_fields = [PLAN_FIELD_MAP[field] for field in fields]
            before = selected_metadata(metadata, remote_fields)
            after = (completed_metadata(before, completion) if completion is not None
                     else planned_metadata(before, values, fields, replace_lists=replace_lists))
            for target in remote_fields:
                if before[target + "Lock"] and before[target] != after[target] and not replace_locked:
                    raise StateConflict(f"Komgaの {target} はロック済みです。変更を確認し --replace-locked を指定して計画し直してください。")
            changes = [{"field": field, "komga_field": PLAN_FIELD_MAP[field], "before": before[PLAN_FIELD_MAP[field]],
                        "after": after[PLAN_FIELD_MAP[field]], "before_lock": before[PLAN_FIELD_MAP[field] + "Lock"],
                        "after_lock": after[PLAN_FIELD_MAP[field] + "Lock"]}
                       for field in fields if before[PLAN_FIELD_MAP[field]] != after[PLAN_FIELD_MAP[field]]
                       or before[PLAN_FIELD_MAP[field] + "Lock"] != after[PLAN_FIELD_MAP[field] + "Lock"]]
            if not changes:
                raise ConfigError("選んだ作品情報とロックはKomgaに反映済みです。変更はありません。")
            plan = {"id": uuid4().hex, "kind": self.kind, "namespace": self.store.namespace, "created": timestamp(),
                    "root_identity": fs.identity, "komga_url": self.settings.komga_url, "path_mappings": self._mappings(),
                    "series_id": series.id, "series_path": series.path, "series_title": series.title,
                    "base_fingerprint": fingerprint(base), "edits": {series.id: edit}, "entries": entries,
                    "komga": identity, "before": before, "after": after, "fields": fields, "changes": changes,
                    "provenances": MetadataService(self.library, self.store).inspect(series.id)["provenances"],
                    "replace_locked": replace_locked, "replace_lists": replace_lists,
                    "mode": "completion" if completion is not None else "metadata"}
            if completion is not None:
                plan["completion"] = completion
            operation = {"plan": plan, "digest": digest(plan), "status": "planned"}
            durable_mkdir(self._folder(plan["id"]))
            self._save(operation)
            return operation

    def _check_identity(self, fs, operation, *, local_edits=False):
        plan = operation["plan"]
        if self._mappings() != plan["path_mappings"]:
            raise StateConflict("計画後にKomgaのパス対応が変わっています。")
        for entry in plan["entries"]:
            fs.verify(entry["source"], entry["snapshot"])
        if local_edits:
            self.store.verify_edits(plan["edits"])
            try:
                base, _, _ = MetadataService(self.library, self.store).series(plan["series_id"], refresh=True)
            except KeyError:
                raise StateConflict("計画後にローカル作品が変わっています。") from None
            if fingerprint(base) != plan["base_fingerprint"]:
                raise StateConflict("計画後に書庫の作品情報が変わっています。差分を確認し直してください。")
        identity, metadata = self._target(plan["entries"], plan["series_path"], plan["komga"]["series_id"])
        if identity != plan["komga"]:
            raise StateConflict("Komgaの作品・書籍IDや対応が変わっています。上書きせず中止しました。")
        return metadata

    def _check(self, fs, operation, *, local_edits=False):
        metadata = self._check_identity(fs, operation, local_edits=local_edits)
        return selected_metadata(metadata, [PLAN_FIELD_MAP[field] for field in operation["plan"]["fields"]])

    def _patch(self, plan, values):
        self.client.request("PATCH", "/api/v1/series/" + quote(plan["komga"]["series_id"], safe="") + "/metadata", values)

    def _run(self, operation, fs, *, restore=False):
        plan = operation["plan"]
        kind = self.kind + ("_restore" if restore else "")
        job_id = self.store.create_job(kind)
        operation.update(status="restoring" if restore else "running", intent="restore" if restore else "apply")
        operation["restore_job_id" if restore else "job_id"] = job_id
        self._save(operation)  # Persist both values and intent before sending the one PATCH.
        self.store.update_job(job_id, status="running", total=1)
        try:
            expected = plan["before"] if restore else plan["after"]
            # Check again after the journal is durable; Komga has no conditional PATCH.
            current = self._check(fs, operation, local_edits=not restore)
            needs_patch = True
            if restore:
                if current not in (plan["before"], plan["after"]):
                    raise StateConflict("反映後にKomgaの対象項目が編集されています。復元を中止しました。")
                needs_patch = current != plan["before"]
            elif current != plan["before"]:
                raise StateConflict("確認後にKomgaの対象項目が編集されています。")
            if needs_patch:
                self._patch(plan, expected)
            if self._check(fs, operation) != expected:
                raise StateConflict("Komgaへの反映結果を確認できません。操作記録から復元してください。")
            operation.update(status="restored" if restore else "completed")
            operation.pop("intent", None)
            self._save(operation)
            changes = [{**change, "before": change["after"], "after": change["before"],
                        "before_lock": change["after_lock"], "after_lock": change["before_lock"]}
                       for change in plan["changes"]] if restore else plan["changes"]
            self.store.update_job(job_id, status="succeeded", current=1, result={"operation_id": plan["id"],
                "series": 1, "series_title": plan["series_title"], "changes": changes,
                "mode": plan.get("mode", "metadata")})
            return operation
        except Exception as exc:
            operation["status"] = "failed"
            self._save(operation)
            self.store.update_job(job_id, status="failed", error="Komgaの作品情報の操作を完了できませんでした。操作記録を確認して復元してください。")
            raise StateConflict("Komgaの作品情報の操作を完了できませんでした。再送せず、操作IDから復元してください。") from exc

    def execute(self, operation_id, confirmation):
        with self.locked() as fs:
            operation = self._confirmed(operation_id, confirmation, fs)
            if operation.get("_fallback"):
                raise StateConflict("現行の操作記録を読み取れません。直前の記録を使って先に復元してください。")
            if operation["status"] != "planned":
                raise StateConflict("この計画は実行済み、または復元が必要な状態です。")
            if self._check(fs, operation, local_edits=True) != operation["plan"]["before"]:
                raise StateConflict("計画後にKomgaの対象項目が編集されています。差分を確認し直してください。")
            return self._run(operation, fs)

    def restore(self, operation_id, confirmation):
        with self.locked() as fs:
            operation = self._confirmed(operation_id, confirmation, fs)
            if (operation["status"] not in {"completed", "failed", "running", "restoring"}
                    and not (operation["status"] == "planned" and operation.get("_fallback"))):
                raise StateConflict("この操作は復元の対象ではありません。")
            current = self._check(fs, operation)
            if current not in (operation["plan"]["before"], operation["plan"]["after"]):
                raise StateConflict("反映後にKomgaの対象項目が編集されています。上書きせず中止しました。")
            return self._run(operation, fs, restore=True)
