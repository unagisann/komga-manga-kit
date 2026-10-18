"""Web review adapters for existing journaled move and Komga metadata services."""
from pathlib import PurePosixPath

from .config import ConfigError
from .komga_batch import KomgaBatchService
from .operations import OperationService
from .state import timestamp


# Moves recorded before the page was renamed used the kind "completion".
MOVE_KINDS = {"move", "completion"}


class MaintenanceOperations:
    def __init__(self, settings, store, inventory, client):
        self.settings, self.store, self.inventory = settings, store, inventory
        self.moves = OperationService(settings, store, client)
        self.batch = KomgaBatchService(settings, store, client)

    def candidates(self):
        """Series whose folder differs from where the organize rules place them; completed series are one case."""
        from collections import defaultdict
        from .organizer import plan_organization
        series = self.inventory.library.scan().series
        preview = plan_organization(series, self.settings.layout, None, context=series)
        items = defaultdict(list)
        for item in preview["items"]:
            items[str(PurePosixPath(item["source"]).parent)].append(item)
        rows = []
        for entry in series:
            own = items.get(entry.path, [])
            targets = {str(PurePosixPath(item["target"]).parent) for item in own if item.get("target")}
            issues = sorted({issue for item in own for issue in item.get("issues", [])})
            target = next(iter(targets)) if len(targets) == 1 else ""
            if issues or target and target != entry.path:
                rows.append({"id": entry.id, "title": entry.title, "author": entry.author, "status": entry.status,
                             "source": entry.path, "target": target, "issues": issues})
        return rows

    def prepare(self, kind, **body):
        if kind == "move":
            identifiers = body.get("series_ids")
            allowed = {row["id"] for row in self.candidates() if not row["issues"]}
            if (set(body) != {"series_ids"} or not isinstance(identifiers, list) or not identifiers
                    or any(not isinstance(value, str) for value in identifiers) or set(identifiers) - allowed):
                raise ConfigError("整理ルールの場所と違う作品から、移動する対象を選んでください。")
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
        return (self.moves if record["data"]["kind"] in MOVE_KINDS else self.batch).inspect(identifier)

    def execute(self, identifier, confirm, *, restore=False):
        record = self.store.cached("maintenance:operation:" + identifier)
        if record is None:
            raise ConfigError("確認した操作を指定してください。")
        service = self.moves if record["data"]["kind"] in MOVE_KINDS else self.batch
        operation = service.restore(identifier, confirm) if restore else service.execute(identifier, confirm)
        self.inventory.refresh()
        return {"operation_id": identifier, "kind": record["data"]["kind"], "status": operation["status"], "files": operation.get("moved_count", 0),
                "series": len(operation["plan"].get("targets", operation["plan"].get("groups", [])))}
