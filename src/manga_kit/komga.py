"""Komga requests with private credentials and sanitized upstream errors."""
import os
import requests
from .credentials import secret

from .config import Settings

# Writes depend on Komga's hash-based restore and metadata semantics. Add a
# version only after the isolated real-server integration suite passes on it.
WRITE_VERSIONS = ("1.29.0", "1.29.1")
WRITE_VERSIONS_LABEL = " / ".join(WRITE_VERSIONS)


class KomgaError(RuntimeError):
    pass


def write_version_supported(info) -> bool:
    build = info.get("build") if isinstance(info, dict) else None
    return isinstance(build, dict) and build.get("version") in WRITE_VERSIONS


class KomgaClient:
    def __init__(self, settings: Settings, session=None, api_key=""):
        self.settings = settings
        self.session = session or requests.Session()
        # Only for checking a key typed in the settings form before it is saved.
        self.api_key = api_key

    def _key(self):
        return secret(self.settings, self.settings.api_key_env, self.api_key)

    def get(self, endpoint: str, **params):
        if not self.settings.komga_url:
            raise KomgaError("Komgaの接続先が未設定です。config.yaml または KOMGA_URL を設定してください。")
        key = self._key()
        if not key:
            raise KomgaError("Komga APIキーが未設定です。環境設定のKomgaで登録するか、環境変数へ設定してください。")
        try:
            response = self.session.get(self.settings.komga_url + endpoint, params=params,
                                        headers={"X-API-Key": key, "Accept": "application/json"},
                                        timeout=self.settings.timeout_seconds, allow_redirects=False)
            if response.status_code in {401, 403}:
                raise KomgaError("Komgaの認証に失敗しました。APIキーと権限を確認してください。")
            if 300 <= response.status_code < 400:
                raise KomgaError("Komgaがリダイレクトを返しました。最終的な接続先URLを設定してください。")
            response.raise_for_status()
            return response.json()
        except requests.Timeout as exc:
            raise KomgaError("Komgaへの接続がタイムアウトしました。") from exc
        except (requests.RequestException, ValueError) as exc:
            # Never surface an upstream body, exception URL, or API key.
            raise KomgaError("Komgaから正常な応答を取得できませんでした。接続先を確認してください。") from exc

    def connection(self) -> dict:
        libraries = self.get("/api/v1/libraries")
        if not isinstance(libraries, list) or any(not isinstance(lib, dict) for lib in libraries):
            raise KomgaError("Komgaのライブラリ応答形式が想定と異なります。")
        return {"connected": True, "library_count": len(libraries),
                "libraries": [{"id": lib.get("id"), "name": lib.get("name")} for lib in libraries]}

    def image(self, identifier, page):
        from urllib.parse import quote
        return self._image("/api/v1/books/" + quote(identifier, safe="") + "/pages/" + str(page),
                           {"zero_based": "false"}, "ページ画像")

    def thumbnail(self, identifier):
        """Komga's generated cover, far smaller than the first page itself."""
        from urllib.parse import quote
        return self._image("/api/v1/books/" + quote(identifier, safe="") + "/thumbnail", {}, "表紙画像")

    def _image(self, endpoint, params, label):
        if not self.settings.komga_url or not self._key():
            raise KomgaError("Komgaの接続先とAPIキーを設定してください。")
        try:
            with self.session.get(self.settings.komga_url + endpoint, params=params,
                    headers={"X-API-Key": self._key()},
                    timeout=self.settings.timeout_seconds, stream=True, allow_redirects=False) as response:
                mime = response.headers.get("Content-Type", "").split(";")[0]
                if response.status_code != 200 or mime not in {"image/jpeg", "image/png", "image/gif", "image/webp", "image/avif", "image/bmp"}:
                    raise KomgaError(f"Komgaの{label}を確認できませんでした。画像形式と権限を確認してください。")
                content = bytearray()
                for block in response.iter_content(65536):
                    content.extend(block)
                    if len(content) > 25 * 1024 * 1024:
                        raise KomgaError(f"{label}がプレビューの上限を超えています。")
                return bytes(content), mime
        except requests.RequestException:
            raise KomgaError(f"Komgaの{label}を取得できませんでした。") from None

    def series(self, page=0, size=50):
        result = self.get("/api/v1/series", page=page, size=size)
        if (not isinstance(result, dict) or not isinstance(result.get("content"), list)
                or any(not isinstance(entry, dict) for entry in result["content"])):
            raise KomgaError("Komgaの作品応答形式が想定と異なります。")
        return result

    def request(self, method, endpoint, body=None, **params):
        if method == "GET":
            return self.get(endpoint, **params)
        if not self.settings.komga_url or not self._key():
            raise KomgaError("Komgaの接続先とAPIキーを設定してください。")
        try:
            response = self.session.request(method, self.settings.komga_url + endpoint, params=params, json=body,
                headers={"X-API-Key": self._key(), "Accept": "application/json"},
                timeout=self.settings.timeout_seconds, allow_redirects=False)
            if not 200 <= response.status_code < 300:
                raise KomgaError("Komgaの操作に失敗しました。接続先と権限を確認してください。")
            return response.json() if response.content else None
        except (requests.RequestException, ValueError) as exc:
            raise KomgaError("Komgaの操作を完了できませんでした。状態を確認してください。") from exc
