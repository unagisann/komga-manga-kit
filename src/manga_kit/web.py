from dataclasses import asdict
import os

from flask import Blueprint, current_app, jsonify, render_template, request
from werkzeug.exceptions import NotFound

from .config import ConfigError
from .japanese import ROWS, search_key
from .organizer import plan_organization
from .paths import map_komga_path

home = Blueprint("home", __name__)
library = Blueprint("library", __name__)
organize = Blueprint("organize", __name__)
metadata = Blueprint("metadata", __name__)
jobs = Blueprint("jobs", __name__)
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


@metadata.get("/metadata/<series_id>")
def metadata_detail(series_id):
    data = metadata_service().inspect(series_id)
    return render_template("metadata_detail.html", **data)


@metadata.get("/metadata/<series_id>/sources")
def metadata_sources(series_id):
    data = metadata_service().inspect(series_id)
    return render_template("metadata_sources.html", **data, catalog=current_app.extensions["enrichment"].status())


@jobs.get("/jobs", endpoint="index")
def jobs_index():
    return render_template("jobs.html", jobs=current_app.extensions["state"].jobs(),
                           events=current_app.extensions["state"].events())


@jobs.get("/jobs/<job_id>", endpoint="detail")
def job_detail(job_id):
    job = current_app.extensions["state"].job(job_id, include_result=True)
    if job is None:
        raise NotFound()
    total = 0
    if job["result"] and "items" in job["result"]:
        total = len(job["result"]["items"])
        job["result"]["items"] = job["result"]["items"][:200]
    return render_template("job_detail.html", job=job, total_items=total)


@settings.get("/settings", endpoint="index")
def settings_index():
    config = configuration()
    return render_template("settings.html", key_configured=bool(os.getenv(config.api_key_env)), layout=asdict(config.layout),
                           catalog=current_app.extensions["enrichment"].status())


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
                   api_key_configured=bool(os.getenv(config.api_key_env)), preview_only=True,
                   execution_enabled=config.execution_enabled, execution_interface="cli",
                   metadata_catalog=current_app.extensions["enrichment"].status(),
                   data_root=str(config.data_root), path_mappings=[{"komga": m.komga, "local": str(m.local)}
                                                                  for m in config.path_mappings])


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


def body_with_keys(allowed, required=()):
    body = request.get_json()
    if not isinstance(body, dict) or set(body) - set(allowed) or set(required) - set(body):
        raise ConfigError("JSONの設定項目が不正です。")
    return body


def metadata_service():
    return current_app.extensions["metadata"]


@metadata.errorhandler(KeyError)
@api.errorhandler(KeyError)
def missing_record(error):
    return jsonify(error="作品または処理が見つかりません。"), 404


@api.get("/metadata/<series_id>")
def api_metadata(series_id):
    return jsonify(metadata_service().inspect(series_id))


@api.post("/metadata/<series_id>/preview")
def metadata_preview(series_id):
    body = body_with_keys({"fields", "reset"})
    if type(body.get("reset", False)) is not bool:
        raise ConfigError("reset は true / false で指定してください。")
    return jsonify(metadata_service().preview(series_id, body.get("fields"), reset=body.get("reset", False), refresh=True))


@api.post("/metadata/<series_id>")
def metadata_save(series_id):
    body = body_with_keys({"fields", "reset", "revision", "base_fingerprint"}, {"revision", "base_fingerprint"})
    if type(body.get("reset", False)) is not bool:
        raise ConfigError("reset は true / false で指定してください。")
    return jsonify(metadata_service().save(series_id, body.get("fields"), body["revision"], body["base_fingerprint"],
                                          reset=body.get("reset", False)))


@api.post("/metadata/<series_id>/search")
def metadata_search(series_id):
    body = body_with_keys({"title", "author", "isbn", "page"})
    return jsonify(current_app.extensions["enrichment"].search(series_id, **body))


@api.post("/metadata/<series_id>/source/preview")
def source_preview(series_id):
    body = body_with_keys({"search_id", "candidate_id", "fields", "unlink"})
    return jsonify(current_app.extensions["enrichment"].preview(series_id, **body))


@api.post("/metadata/<series_id>/source")
def source_save(series_id):
    body = body_with_keys({"search_id", "candidate_id", "fields", "unlink", "revision", "base_fingerprint"},
                          {"revision", "base_fingerprint"})
    return jsonify(current_app.extensions["enrichment"].save(series_id, **body))


def pagination():
    try:
        limit, offset = int(request.args.get("limit", "50")), int(request.args.get("offset", "0"))
        if not 1 <= limit <= 200 or not 0 <= offset <= 100000:
            raise ValueError
    except ValueError as exc:
        raise ConfigError("limit は1〜200、offset は0〜100000で指定してください。") from exc
    return limit, offset


@api.get("/jobs")
def list_jobs():
    limit, offset = pagination()
    return jsonify(jobs=current_app.extensions["state"].jobs(limit, offset))


@api.get("/history")
def list_history():
    limit, offset = pagination()
    return jsonify(events=current_app.extensions["state"].events(limit, offset))


@api.post("/jobs")
def submit_job():
    body = body_with_keys({"kind", "layout"}, {"kind"})
    if not isinstance(body.get("layout", {}), dict):
        raise ConfigError("layout はマッピングで指定してください。")
    job_id = current_app.extensions["jobs"].submit(body["kind"], body.get("layout"))
    return jsonify(current_app.extensions["state"].job(job_id)), 202


@api.get("/jobs/<job_id>")
def get_job(job_id):
    job = current_app.extensions["state"].job(job_id)
    if job is None:
        raise NotFound()
    return jsonify(job)


@api.get("/jobs/<job_id>/result")
def get_job_result(job_id):
    job = current_app.extensions["state"].job(job_id, include_result=True)
    if job is None:
        raise NotFound()
    if job["status"] != "succeeded":
        return jsonify(error="処理が正常に完了していません。"), 409
    result = dict(job["result"])
    if "items" in result:
        limit, offset = pagination()
        items = result["items"]
        result.update(items=items[offset:offset + limit], total_items=len(items), limit=limit, offset=offset)
    return jsonify(result)


@api.post("/komga/match")
def match_komga():
    body = body_with_keys({"page", "size"})
    page, size = body.get("page", 0), body.get("size", 50)
    if type(page) is not int or page < 0 or type(size) is not int or not 1 <= size <= 100:
        raise ConfigError("page は0以上、size は1〜100で指定してください。")
    config = configuration()
    if config.demo or not config.path_mappings:
        raise ConfigError("実ライブラリとKomgaのパス対応付けを設定してください。")
    entries = {entry.path: entry for entry in scan().series}
    response = current_app.extensions["komga"].series(page, size)
    matches = []
    for entry in response["content"]:
        try:
            relative = map_komga_path(entry.get("url", ""), config.path_mappings, config.library_root)
        except ConfigError:
            relative = None
        local = entries.get(relative)
        matches.append({"komga_id": entry.get("id"), "name": entry.get("name"),
                        "state": "matched" if local else "missing_local_series" if relative is not None else "unmapped",
                        "local_series_id": local.id if local else None, "title": local.title if local else ""})
    return jsonify(matches=matches, page=page, total_elements=response.get("totalElements", len(matches)))
