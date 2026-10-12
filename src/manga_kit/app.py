"""Flask app factory; separate app instances own their services and settings."""
import hmac
import os
from urllib.parse import urlsplit

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from . import __version__
from .config import ConfigError, load_settings
from .features import built_in_features
from .komga import KomgaClient, KomgaError
from .library import LibraryService
from .providers import ComicInfoProvider


def create_app(config_path=None, *, settings=None, features=None):
    settings = settings or load_settings(config_path)
    settings.layout.validate()
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=16 * 1024, MANGA_KIT=settings)
    providers = [ComicInfoProvider()]
    app.extensions.update(library=LibraryService(settings, providers), komga=KomgaClient(settings), providers=providers)
    registry = list(built_in_features() if features is None else features)
    if len({feature.id for feature in registry}) != len(registry):
        raise ConfigError("機能IDが重複しています。")
    for feature in registry:
        app.register_blueprint(feature.blueprint)
    from .web import api
    app.register_blueprint(api)
    admin_token = os.getenv("MANGA_KIT_ADMIN_TOKEN", "")

    @app.before_request
    def guard():
        if request.path == "/healthz":
            return None
        if admin_token:
            auth = request.authorization
            if (auth is None or auth.type.lower() != "basic" or auth.username != "admin"
                    or not hmac.compare_digest((auth.password or "").encode(), admin_token.encode())):
                return "認証が必要です。", 401, {"WWW-Authenticate": 'Basic realm="Manga Kit", charset="UTF-8"'}
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                return jsonify(error="別のサイトからのリクエストは受け付けません。"), 403

    @app.context_processor
    def context():
        return {"settings": settings, "features": registry, "version": __version__,
                "status_labels": {"ongoing": "連載中", "completed": "完結", "unknown": "未分類"},
                "active_feature": (request.endpoint or "").split(".")[0]}

    @app.get("/healthz")
    def health():
        return jsonify(status="ok", version=__version__)

    @app.errorhandler(ConfigError)
    def config_error(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(KomgaError)
    def komga_error(error):
        return jsonify(error=str(error), connected=False), 502

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return jsonify(error="リクエストを処理できませんでした。", status=error.code), error.code
        from flask import render_template
        return render_template("error.html", code=error.code, message="ページが見つからないか、リクエストを処理できませんでした。"), error.code

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self'; "
                                                      "img-src 'self' data:; object-src 'none'; base-uri 'self'; "
                                                      "frame-ancestors 'none'; form-action 'self'")
        response.headers["Cache-Control"] = "public, max-age=3600" if request.path.startswith("/static/") else "no-store"
        return response

    return app
