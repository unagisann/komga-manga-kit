"""Small read-only Komga client with no global credentials."""
import os
import requests

from .config import Settings


class KomgaError(RuntimeError):
    pass


class KomgaClient:
    def __init__(self, settings: Settings, session=None):
        self.settings = settings
        self.session = session or requests.Session()

    def get(self, endpoint: str, **params):
        if not self.settings.komga_url:
            raise KomgaError("Komgaの接続先が未設定です。config.yaml または KOMGA_URL を設定してください。")
        key = os.getenv(self.settings.api_key_env, "")
        if not key:
            raise KomgaError("Komga APIキーが未設定です。設定した環境変数へ保存してください。")
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

    def series(self, page=0, size=50):
        result = self.get("/api/v1/series", page=page, size=size)
        if (not isinstance(result, dict) or not isinstance(result.get("content"), list)
                or any(not isinstance(entry, dict) for entry in result["content"])):
            raise KomgaError("Komgaの作品応答形式が想定と異なります。")
        return result
