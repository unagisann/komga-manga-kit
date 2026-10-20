"""Flask app factory; separate app instances own their services and settings."""
import hmac
import hashlib
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, url_for
from werkzeug.exceptions import HTTPException

from . import __version__
from .config import ConfigError, load_settings
from .features import MANAGEMENT_TABS, built_in_features, management_tab, navigation_items, portal_links
from .catalogs import CatalogError
from .enrichment import EnrichmentService
from .auto_enrichment import AutoEnrichmentService
from .normalization import NormalizationService
from .komga import KomgaClient, KomgaError
from .library import LibraryService
from .jobs import JobService
from .metadata import MetadataService
from .providers import ComicInfoProvider
from .state import StateConflict, StateStore
from .inventory import InventoryService
from .feeds import FeedsService
from .new_volumes import NewVolumeService
from .book_pages import BookPages
from .preferences import Preferences
from .smart_shelves import SmartShelves
from .ai_assist import AIAssist
from .page_editor import PageEditor
from .automation import Automation
from .maintenance_ops import MaintenanceOperations


def create_app(config_path=None, *, settings=None, features=None):
    settings = settings or load_settings(config_path)
    settings.layout.validate()
    app = Flask(__name__)
    asset_versions = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()[:12]
                      for path in Path(app.static_folder).iterdir() if path.is_file()}

    @app.template_global()
    def asset_url(filename):
        return url_for("static", filename=filename, v=asset_versions.get(filename, ""))

    app.config.update(MAX_CONTENT_LENGTH=16 * 1024, MANGA_KIT=settings)
    providers = [ComicInfoProvider()]
    store = StateStore(settings)
    store.recover()
    settings = Preferences(settings, store)
    app.config["MANGA_KIT"] = settings
    app.extensions["preferences"] = settings
    library = LibraryService(settings, providers, store)
    app.extensions.update(library=library, komga=KomgaClient(settings), providers=providers, state=store,
                          jobs=JobService(library, store), metadata=MetadataService(library, store))
    library.background_refresh = app.extensions["jobs"].refresh_cached
    app.extensions["enrichment"] = EnrichmentService(settings, app.extensions["metadata"], store)
    app.extensions["auto_enrichment"] = AutoEnrichmentService(app.extensions["enrichment"], app.extensions["jobs"])
    app.extensions["normalization"] = NormalizationService(settings, library, store, app.extensions["jobs"])
    inventory = InventoryService(settings, library, store, app.extensions["komga"])
    app.extensions.update(inventory=inventory, feeds=FeedsService(settings, store, inventory, app.extensions["jobs"]),
                          new_volumes=NewVolumeService(settings, store, inventory, app.extensions["jobs"]),
                          book_pages=BookPages(settings, inventory))
    app.extensions["ai_assist"] = AIAssist(settings, store, app.extensions["metadata"])
    app.extensions["page_editor"] = PageEditor(settings, store, inventory, app.extensions["book_pages"])
    from .covers import CoverScreening
    app.extensions["covers"] = CoverScreening(settings, store, inventory, app.extensions["jobs"])
    app.extensions["maintenance_ops"] = MaintenanceOperations(settings, store, inventory, app.extensions["komga"])
    app.extensions["smart_shelves"] = SmartShelves(settings, store, inventory, app.extensions["feeds"],
                                                 app.extensions["new_volumes"], app.extensions["komga"])
    automation = Automation(settings, store, app.extensions["jobs"], app.extensions)
    app.extensions["automation"] = automation
    app.extensions["normalization"].after_execute = automation.submit_import
    if automation.config()["enabled"]:
        automation.start()
    import atexit
    atexit.register(automation.shutdown)
    registry = list(built_in_features() if features is None else features)
    from .menus import Menus
    menus = app.extensions["menus"] = Menus(store, navigation_items(registry), portal_links(registry))
    if len({feature.id for feature in registry}) != len(registry):
        raise ConfigError("機能IDが重複しています。")
    for feature in registry:
        app.register_blueprint(feature.blueprint)
    from .web import api
    app.register_blueprint(api)
    from .pwa import pwa
    app.register_blueprint(pwa)
    admin_token = os.getenv("MANGA_KIT_ADMIN_TOKEN", "")
    app.config["ADMIN_AUTH_ENABLED"] = bool(admin_token)

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
        from .provenance import PROVIDER_LABELS
        section = (request.endpoint or "").split(".")[0]
        feature = next((feature for feature in registry if feature.id == section), None)
        active = (feature.parent or feature.id) if feature else section
        if request.endpoint == "metadata.metadata_detail":
            active = "management"
        navigation = menus.shown("header")
        selected = next((item.endpoint for item in navigation
                         if request.endpoint == item.endpoint or request.endpoint in item.related_endpoints), None)
        if selected is None:
            selected = next((item.endpoint for item in registry if item.id == active), None)
        tab = management_tab(request.endpoint)
        followup_id = request.args.get("followup") if request.endpoint != "maintenance.komga_sync" else None
        followup = app.extensions["maintenance_ops"].followups.inspect(followup_id) if followup_id else None
        return {"settings": settings, "features": registry, "navigation": navigation,
                "komga_followup": followup,
                "active_navigation": selected, "version": __version__,
                "management_tabs": MANAGEMENT_TABS if tab else (), "management_tab": tab,
                "metadata_labels": {"title": "作品名", "author": "著者", "reading": "読み", "publisher": "出版社",
                                    "status": "状態", "summary": "あらすじ", "genres": "ジャンル", "tags": "タグ", "authors": "著者・役割"},
                "provider_labels": PROVIDER_LABELS,
                "status_labels": {"ongoing": "連載中", "completed": "完結", "unknown": "未分類"},
                "komga_status_labels": {"ONGOING": "連載中", "ENDED": "完結", "HIATUS": "休載", "ABANDONED": "打ち切り"},
                "job_labels": {"scan_refresh": "蔵書の表示更新", "pages_preview": "ページ修正の差分確認", "shelf_preview": "スマート本棚の差分確認", "automation": "自動更新・取り込み後の処理", "cover_screening": "表紙の確認", "ai_generate": "AIの提案取得", "shelf_execute": "スマート本棚の同期", "shelf_restore": "スマート本棚の復元", "pages_execute": "表紙・ページの修正", "pages_restore": "ページ修正の復元", "maintenance_preview": "管理操作の差分確認", "maintenance_execute": "管理操作の実行", "maintenance_restore": "管理操作の復元", "source_refresh": "情報源の更新", "inventory_refresh": "蔵書情報の更新", "new_volume_check": "新刊チェック", "normalization_check": "追加・変更分の確認", "normalization_execute": "取り込み整理の実行", "normalize": "取り込み整理", "normalization_restore": "取り込み整理の復元", "normalization_baseline": "現状を整理済みとして登録", "normalization_baseline_restore": "整理済みの登録の取り消し", "enrichment": "自動補完の候補取得", "scan": "蔵書スキャン", "preview": "整理プレビュー", "organize": "整理の実行", "restore": "整理の復元",
                               "conversion": "CBZ生成", "conversion_restore": "CBZ生成の取り消し",
                               "komga_metadata": "Komga作品情報の反映", "komga_metadata_restore": "Komga作品情報の復元",
                               "komga_authors": "Komga著者情報の反映", "komga_authors_restore": "Komga著者情報の復元",
                               "komga_batch": "Komgaへの一括反映", "komga_batch_restore": "Komgaへの一括反映の復元",
                               "komga_reading_import": "Komgaの読みの取り込み", "komga_reading_import_restore": "読みの取り込みの取り消し"},
                "job_status_labels": {"queued": "待機中", "running": "処理中", "succeeded": "完了",
                                      "failed": "失敗", "interrupted": "中断"},
                "batch_mode_labels": {"metadata": "作品情報", "completion": "完結情報", "authors": "巻の著者", "reading": "読み"},
                "event_labels": {"metadata_enrichment": "不足情報の一括補完", "metadata_edit": "作品情報の編集", "metadata_relocated": "編集情報の引き継ぎ", "metadata_import": "読みの一括取り込み",
                                 "job_created": "処理の受付", "job_finished": "処理の終了"},
                "active_feature": active}

    @app.get("/healthz")
    def health():
        return jsonify(status="ok", version=__version__)

    @app.errorhandler(ConfigError)
    def config_error(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(KomgaError)
    def komga_error(error):
        return jsonify(error=str(error), connected=False), 502

    @app.errorhandler(CatalogError)
    def catalog_error(error):
        response = jsonify(error=str(error))
        response.status_code = 429 if error.retry_after else 502
        if error.retry_after:
            response.headers["Retry-After"] = str(error.retry_after)
        return response

    @app.errorhandler(StateConflict)
    def state_conflict(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(OSError)
    @app.errorhandler(sqlite3.Error)
    def storage_error(error):
        return jsonify(error="保存先を利用できません。実行データの場所・空き容量・アクセス権を確認してください。"), 503

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
                                                      "img-src 'self' data: https://s4.anilist.co https://img.tsugimanga.jp https://res.booklive.jp https://thumbnail.image.rakuten.co.jp; object-src 'none'; base-uri 'self'; "
                                                      "frame-ancestors 'none'; form-action 'self'")
        if request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=3600"
        elif "Cache-Control" not in response.headers:
            # Only views that opt in (book covers) may be cached; everything else can hold private data.
            response.headers["Cache-Control"] = "no-store"
        return response

    return app
