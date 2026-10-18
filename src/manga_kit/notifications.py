"""Opt-in new-release webhook with successful-delivery deduplication."""
import requests

from .config import ConfigError
from .credentials import secret
from .operations import digest
from .sources import safe_url
from .state import timestamp


class Notifications:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def send(self, rows):
        if self.settings.demo or not self.settings.tools.notify_enabled:
            return {"sent": 0}
        with self.store.tool_lock("notifications"):
            return self._send(rows)

    def _send(self, rows):
        url = secret(self.settings, self.settings.tools.notify_url_env)
        if not safe_url(url):
            raise ConfigError("通知先のHTTPS Webhook URLを登録してください。")
        pending = []
        for row in rows:
            if not row.get("new") or row.get("errors") or row.get("stale"):
                continue
            key = "notification:" + digest([row["series_id"], row["latest"]])
            if self.store.cached(key) is None:
                pending.append((key, row))
        count = 0
        for key, row in pending:
            text = f"新刊: {row['title']} · 最新 {row['latest']}巻（所持 {row['local_max']}巻）"
            try:
                with requests.Session() as session, session.post(url, json={"content": text}, timeout=15, allow_redirects=False, stream=True) as response:
                    if not 200 <= response.status_code < 300:
                        raise ConfigError("新刊通知を送信できませんでした。通知先・権限・利用上限を確認してください。")
            except requests.RequestException:
                raise ConfigError("新刊通知を送信できませんでした。通知先の接続を確認してください。") from None
            self.store.cache(key, {"sent": timestamp()})
            count += 1
        return {"sent": count}
