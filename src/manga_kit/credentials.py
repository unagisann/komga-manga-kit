"""Optional private credential file; environment variables always take precedence."""
import json
import os
import stat
from pathlib import Path
import tempfile

from .config import ConfigError
from .durability import durable_mkdir, sync_directory


def credential_values(path):
    if not path:
        return {}
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return {}
    try:
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ConfigError("認証情報の保存先には通常ファイルを指定してください。")
            content = source.read(65537)
        values = json.loads(content)
        if len(content) > 65536 or not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in values.items()):
            raise ValueError
        return values
    except (ValueError, UnicodeError):
        raise ConfigError("認証情報の保存ファイルの形式を確認してください。") from None


def credential_names(settings):
    return {settings.api_key_env, settings.rakuten.application_id_env, settings.rakuten.access_key_env,
            settings.tools.ai_key_env, settings.tools.notify_url_env}


def secret(settings, name, typed=""):
    # A value typed but not saved yet stands in for the file only: the environment still wins, as after saving.
    return os.getenv(name, "") or typed or credential_values(settings.tools.credentials_file).get(name, "")


def credential_sources(settings):
    """Where each value in use comes from: "env", "file" or "" when unset. Never the values themselves."""
    saved = credential_values(settings.tools.credentials_file)
    return {name: "env" if os.getenv(name, "") else "file" if saved.get(name) else "" for name in credential_names(settings)}


def check_credentials(settings, values):
    if (not isinstance(values, dict) or set(values) - credential_names(settings) or any(not isinstance(v, str) or len(v) > 4096
            or any(ord(c) < 32 for c in v) for v in values.values())):
        raise ConfigError("登録できる認証情報の項目と形式を確認してください。")


def save_credentials(settings, values):
    check_credentials(settings, values)
    if not settings.tools.credentials_file:
        raise ConfigError("Webから登録する場合は tools.credentials_file に専用の保存先を設定してください。")
    target = Path(settings.tools.credentials_file)
    durable_mkdir(target.parent)
    if target.is_symlink():
        raise ConfigError("認証情報の保存先にリンクは使えません。")
    saved = credential_values(target)
    saved.update(values)
    descriptor, temporary = tempfile.mkstemp(prefix=".credentials-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(saved, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        sync_directory(target.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {name: bool(source) for name, source in credential_sources(settings).items()}
