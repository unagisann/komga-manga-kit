"""Validated, non-secret Web preferences shared by workers through application state."""
from dataclasses import asdict, replace
import os
import time
from urllib.parse import urlsplit

from .config import ConfigError
from .credentials import secret

TOOLS_FIELDS = {"kobo_enabled", "anime_enabled", "discovery_enabled", "rankings_enabled", "mangaupdates_enabled",
                "ai_enabled", "notify_enabled", "ai_provider", "ai_url", "ai_model", "public_komga_url", "reading_accounts"}


class Preferences:
    def __init__(self, settings, store):
        self.base, self.store, self._current = settings, store, settings
        self._revision, self._checked = -1, 0
        self.reload()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        self.reload()
        return getattr(self._current, name)

    def build(self, data):
        if not isinstance(data, dict) or set(data) - {"komga_url", "rakuten", "anilist", "tools"}:
            raise ConfigError("接続・情報源の設定項目を確認してください。")
        tools = data.get("tools", {})
        rakuten, anilist = data.get("rakuten", {}), data.get("anilist", {})
        if (not isinstance(tools, dict) or set(tools) - TOOLS_FIELDS
                or not isinstance(rakuten, dict) or set(rakuten) - {"enabled", "origin"}
                or not isinstance(anilist, dict) or set(anilist) - {"enabled"}):
            raise ConfigError("情報源の設定項目を確認してください。")
        url = os.getenv("KOMGA_URL") or data.get("komga_url", self.base.komga_url)
        if not isinstance(url, str):
            raise ConfigError("Komgaの接続先を文字列で指定してください。")
        try:
            parsed = urlsplit(url); parsed.port
        except ValueError:
            raise ConfigError("Komgaの接続先URLを確認してください。") from None
        if url and (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or any(ord(c) < 33 for c in url)):
            raise ConfigError("Komgaの接続先は認証情報を含まないHTTP(S) URLで指定してください。")
        return replace(self.base, komga_url=url.rstrip("/"), tools=replace(self.base.tools, **tools),
                       rakuten=replace(self.base.rakuten, **rakuten), anilist=replace(self.base.anilist, **anilist)).validate_storage()

    def reload(self):
        if time.monotonic() - self._checked < .5:
            return
        self._checked = time.monotonic()
        revision = self.store.cached_revision("preferences")
        if revision != self._revision:
            cached = self.store.cached("preferences")
            self._current = self.build(cached["data"]) if cached else self.base
            self._revision = revision

    def save(self, values, revision):
        self.build(values)
        self.store.cache("preferences", values, revision=revision)
        self._checked = 0
        self.reload()
        return self.view()

    def view(self):
        self.reload()
        settings = self._current
        names = {settings.api_key_env, settings.rakuten.application_id_env, settings.rakuten.access_key_env,
                 settings.tools.ai_key_env, settings.tools.notify_url_env}
        names.update(account["api_key_env"] for account in settings.tools.reading_accounts)
        return {"revision": self._revision, "komga_url": settings.komga_url,
                "rakuten": {name: getattr(settings.rakuten, name) for name in ("enabled", "origin")},
                "anilist": {"enabled": settings.anilist.enabled},
                "tools": {name: getattr(settings.tools, name) for name in TOOLS_FIELDS},
                "credentials": {name: bool(secret(settings, name)) for name in names},
                "credential_file_enabled": bool(settings.tools.credentials_file)}
