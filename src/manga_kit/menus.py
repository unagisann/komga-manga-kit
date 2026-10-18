"""Header and portal menus the user arranges: order and visibility over the registered items."""
from .config import ConfigError

# Settings live under 管理, so the header always keeps a way back to them.
LOCKED = {"header": {"management.index"}, "portal": set()}


class Menus:
    KEY = "menus"

    def __init__(self, store, header, portal):
        self.store, self.items = store, {"header": list(header), "portal": list(portal)}
        self._revision, self._saved = -1, {}

    def saved(self):
        revision = self.store.cached_revision(self.KEY)
        if revision != self._revision:
            cached = self.store.cached(self.KEY)
            self._saved, self._revision = (cached["data"] if cached else {}), revision
        return self._saved

    def rows(self, name):
        """Every registered item in the saved order. Items the user has not arranged yet keep their
        registered place after the arranged ones, and their default visibility."""
        saved = self.saved().get(name, {})
        order = {identifier: index for index, identifier in enumerate(saved.get("order", []))}
        hidden = set(saved.get("hidden", []))
        ranked = sorted(enumerate(self.items[name]), key=lambda pair: order.get(pair[1].id, len(order) + pair[0]))
        rows = []
        for _, item in ranked:
            locked = item.id in LOCKED[name]
            arranged = item.id in order or item.id in hidden
            rows.append({"item": item, "id": item.id, "locked": locked,
                         "shown": locked or (item.id not in hidden if arranged else item.shown)})
        return rows

    def shown(self, name):
        return [row["item"] for row in self.rows(name) if row["shown"]]

    def view(self):
        self.saved()
        return {"revision": max(self._revision, 0),
                **{name: [{"id": row["id"], "label": row["item"].label, "icon": row["item"].icon,
                           "shown": row["shown"], "locked": row["locked"]} for row in self.rows(name)]
                   for name in self.items}}

    def save(self, values, revision):
        """values: {"header": [{"id", "shown"}...], "portal": [...]} in display order. Empty lists restore the defaults."""
        if not isinstance(values, dict) or set(values) != set(self.items):
            raise ConfigError("ヘッダーとポータルのメニューを指定してください。")
        data = {}
        for name, rows in values.items():
            known = {item.id for item in self.items[name]}
            if (not isinstance(rows, list) or len(rows) > len(known)
                    or any(not isinstance(row, dict) or set(row) != {"id", "shown"} or row["id"] not in known
                           or type(row["shown"]) is not bool for row in rows)):
                raise ConfigError("メニューの項目を確認してください。")
            identifiers = [row["id"] for row in rows]
            if len(set(identifiers)) != len(identifiers):
                raise ConfigError("メニューの項目が重複しています。")
            if any(row["id"] in LOCKED[name] and not row["shown"] for row in rows):
                raise ConfigError("「管理」はヘッダーから外せません。設定画面へ戻れるように常に表示します。")
            data[name] = {"order": identifiers, "hidden": [row["id"] for row in rows if not row["shown"]]}
        self.store.cache(self.KEY, data, revision=revision)
        return self.view()
