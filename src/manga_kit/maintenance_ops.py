"""Web review adapters for existing journaled move and Komga metadata services."""
from pathlib import PurePosixPath

from .config import ConfigError
from .komga_batch import KomgaBatchService
from .operations import OperationService
from .state import timestamp


class MaintenanceOperations:
    def __init__(self, settings, store, inventory, client):
        self.settings, self.store, self.inventory = settings, store, inventory
        self.moves = OperationService(settings, store, client)
        self.batch = KomgaBatchService(settings, store, client)

    def candidates(self):
        rows = self.inventory.cached()["series"]
        return [row for row in rows if row.get("local_id") and row["status"] == "completed"
                and PurePosixPath(row["path"]).parts[0] != self.settings.layout.completed_folder]

    def prepare(self, kind, **body):
        if kind == "completion":
            identifiers = body.get("series_ids")
            allowed = {row["id"] for row in self.candidates()}
            if (set(body) != {"series_ids"} or not isinstance(identifiers, list) or not identifiers
                    or any(not isinstance(value, str) for value in identifiers) or set(identifiers) - allowed):
                raise ConfigError("完結にした作品から移動する対象を選んでください。")
            operation = self.moves.prepare(selected=identifiers)
        elif kind == "komga":
            if set(body) - {"mode", "series_ids", "fields", "overwrite", "replace_locked", "replace_lists"}:
                raise ConfigError("Komga反映の項目を確認してください。")
            operation = self.batch.prepare(**body)
        else:
            raise ConfigError("操作の種類を指定してください。")
        self.store.cache("maintenance:operation:" + operation["plan"]["id"], {"kind": kind, "created": timestamp()})
        return operation

    def inspect(self, identifier):
        record = self.store.cached("maintenance:operation:" + identifier)
        if record is None:
            raise ConfigError("この管理画面で確認した操作を指定してください。")
        return (self.moves if record["data"]["kind"] == "completion" else self.batch).inspect(identifier)

    def execute(self, identifier, confirm, *, restore=False):
        record = self.store.cached("maintenance:operation:" + identifier)
        if record is None:
            raise ConfigError("確認した操作を指定してください。")
        service = self.moves if record["data"]["kind"] == "completion" else self.batch
        operation = service.restore(identifier, confirm) if restore else service.execute(identifier, confirm)
        self.inventory.refresh()
        return {"operation_id": identifier, "kind": record["data"]["kind"], "status": operation["status"], "files": operation.get("moved_count", 0),
                "series": len(operation["plan"].get("targets", operation["plan"].get("groups", [])))}
