from dataclasses import asdict
import os

from flask import Blueprint, current_app, jsonify, render_template, request

from .config import ConfigError
from .japanese import ROWS, search_key
from .organizer import plan_organization

home = Blueprint("home", __name__)
library = Blueprint("library", __name__)
organize = Blueprint("organize", __name__)
metadata = Blueprint("metadata", __name__)
settings = Blueprint("settings", __name__)
roadmap = Blueprint("roadmap", __name__)
api = Blueprint("api", __name__, url_prefix="/api")


def scan():
    return current_app.extensions["library"].scan()


def configuration():
    return current_app.config["MANGA_KIT"]


@home.get("/")
def index():
    result = scan()
    return render_template("home.html", stats=result.stats(), entries=result.series[:4], scan=result)


@library.get("/library", endpoint="index")
def library_index():
    query, status, row = request.args.get("q", ""), request.args.get("status", ""), request.args.get("row", "")
    result = scan()
    entries = [s for s in result.series if (not query or search_key(query) in search_key(s.title + s.author + s.reading))
               and (not status or s.status == status) and (not row or s.row == row)]
    return render_template("library.html", entries=entries, total=len(result.series), query=query, status=status,
                           row=row, rows=list(ROWS) + ["その他"], scan=result)


@organize.get("/organize", endpoint="index")
def organize_index():
    result = scan()
    config = configuration()
    plan = plan_organization(result.series, config.layout, None if config.demo else config.library_root)
    return render_template("organize.html", plan=plan, layout=asdict(config.layout), scan=result)


@metadata.get("/metadata", endpoint="index")
def metadata_index():
    result = scan()
    return render_template("metadata.html", entries=result.series, stats=result.stats(), scan=result)


@settings.get("/settings", endpoint="index")
def settings_index():
    config = configuration()
    return render_template("settings.html", key_configured=bool(os.getenv(config.api_key_env)), layout=asdict(config.layout))


@roadmap.get("/roadmap", endpoint="index")
def roadmap_index():
    return render_template("roadmap.html")


@api.get("/library")
def api_library():
    result = scan()
    return jsonify(series=[s.to_dict() for s in result.series], stats=result.stats(),
                   warnings=result.warnings, truncated=result.truncated, demo=configuration().demo)


@api.post("/library/refresh")
def refresh_library():
    current_app.extensions["library"].scan(refresh=True)
    return api_library()


@api.post("/organize/preview")
def preview():
    body = request.get_json()
    if not isinstance(body, dict) or set(body) - {"layout"}:
        raise ConfigError("layout の設定をJSONで指定してください。")
    config = configuration()
    layout = config.layout.with_overrides(body.get("layout", {}))
    result = scan()
    plan = plan_organization(result.series, layout, None if config.demo else config.library_root)
    return jsonify(**plan, truncated=result.truncated, warnings=result.warnings)


@api.get("/settings")
def api_settings():
    config = configuration()
    return jsonify(name=config.name, demo=config.demo, library_root=str(config.library_root),
                   max_files=config.max_files, layout=asdict(config.layout), komga_url=config.komga_url,
                   api_key_configured=bool(os.getenv(config.api_key_env)), preview_only=True)


@api.post("/komga/test")
def test_komga():
    return jsonify(current_app.extensions["komga"].connection())


@api.get("/komga/series")
def komga_series():
    try:
        page, size = int(request.args.get("page", "0")), int(request.args.get("size", "50"))
        if page < 0 or not 1 <= size <= 100:
            raise ValueError
    except ValueError as exc:
        raise ConfigError("page は0以上、size は1〜100で指定してください。") from exc
    response = current_app.extensions["komga"].series(page, size)
    # Do not proxy unrestricted metadata or paths from Komga to the UI.
    content = [{"id": s.get("id"), "name": s.get("name"), "books_count": s.get("booksCount", 0),
                "title": (s.get("metadata") or {}).get("title", "")} for s in response["content"]]
    return jsonify(content=content, total_elements=response.get("totalElements", len(content)))
