from dataclasses import asdict
import os
import ipaddress
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, render_template, request, url_for
from werkzeug.exceptions import NotFound

from .config import ConfigError
from .credentials import secret
from .auto_enrichment import PROVIDER_FIELDS, missing_fields
from .japanese import search_key
from .organizer import plan_organization
from .paths import map_komga_path

home = Blueprint("home", __name__)
library = Blueprint("library", __name__)
organize = Blueprint("organize", __name__)
metadata = Blueprint("metadata", __name__)
normalize = Blueprint("normalize", __name__)
management = Blueprint("management", __name__)
jobs = Blueprint("jobs", __name__)
settings = Blueprint("settings", __name__)
roadmap = Blueprint("roadmap", __name__)
api = Blueprint("api", __name__, url_prefix="/api")


def scan():
    return current_app.extensions["library"].scan()


def configuration():
    return current_app.config["MANGA_KIT"]


@normalize.get("/normalize", endpoint="index")
def normalization_index():
    store, service = current_app.extensions["state"], current_app.extensions["normalization"]
    identifier = request.args.get("check")
    job = store.job(identifier, include_result=True) if identifier else store.latest_normalization()
    if identifier and (job is None or job["kind"] != "normalization_check"):
        raise NotFound()
    operation = service.operations.inspect(job["result"]["operation_id"]) if job and job["result"] and job["result"].get("operation_id") else None
    action = store.job(request.args["action"], include_result=True) if request.args.get("action") else None
    if request.args.get("action") and (action is None or action["kind"] not in {"normalization_execute", "normalization_restore"}):
        raise NotFound()
    active = [entry for entry in store.jobs() if entry["kind"] in {"normalization_execute", "normalization_restore"}
              and entry["status"] in {"queued", "running"}]
    if active and action is None:
        action = active[0]
    items, paging = paginate(job["result"]["items"], size=20) if job and job["result"] else ([], None)
    scope = (job.get("result") or {}).get("scope") if job else None
    followup_id = (action.get("result") or {}).get("komga_followup") if action and action["status"] == "succeeded" else None
    followup_context = {"komga_followup": current_app.extensions["maintenance_ops"].followups.inspect(followup_id)} if followup_id else {}
    return render_template("normalize.html", check=job, operation=operation, action=action, busy=bool(active),
                           items=items, pagination=paging, target_libraries=service.targets.libraries(),
                           target_scope=scope or {"library": request.args.get("library", "all"),
                               "subpath": request.args.get("subpath", ""),
                               "limit": request.args.get("limit", configuration().max_files),
                               "recheck": request.args.get("recheck") == "true"}, **followup_context)


def require_normalization_execution():
    config = configuration()
    if config.demo or not config.execution_enabled or not config.web_execution_enabled:
        raise ConfigError("Web実行は無効です。execution.enabled と execution.web_enabled を有効にしてください。")
    require_admin_access()


def require_admin_access():
    try:
        local = ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        local = False
    hostname = urlsplit("//" + request.host).hostname
    if not current_app.config["ADMIN_AUTH_ENABLED"] and not (local and hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ConfigError("ローカル接続で実行するか、管理者認証を設定してください。")
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        raise ConfigError("同じ画面から差分を確認して実行してください。")


@api.post("/normalization/checks")
def normalization_check():
    body = body_with_keys({"library", "subpath", "limit", "recheck", "auto_execute"})
    automatic = body.pop("auto_execute", False)
    if type(automatic) is not bool:
        raise ConfigError("そのまま実行は true / false で指定してください。")
    service = current_app.extensions["normalization"]
    if automatic:
        require_normalization_execution()
        job_id, action_id = service.submit_auto(body)
        return jsonify(**current_app.extensions["state"].job(job_id), action_id=action_id), 202
    job_id = service.submit_check(body)
    return jsonify(current_app.extensions["state"].job(job_id)), 202


@api.post("/normalization/libraries/refresh")
def normalization_libraries():
    require_admin_access()
    body_with_keys(set())
    return jsonify(libraries=current_app.extensions["normalization"].targets.refresh())


@api.post("/normalization/checks/<job_id>/<action>")
def normalization_action(job_id, action):
    require_normalization_execution()
    if action not in {"execute", "restore"}:
        raise NotFound()
    body = body_with_keys({"confirm"}, {"confirm"})
    task = current_app.extensions["normalization"].submit_action(job_id, body["confirm"], restore=action == "restore")
    return jsonify(current_app.extensions["state"].job(task)), 202


def paginate(entries, size=60):
    try:
        page = int(request.args.get('page', '1'))
    except ValueError:
        raise ConfigError('ページは1以上の整数で指定してください。') from None
    if page < 1:
        raise ConfigError('ページは1以上の整数で指定してください。')
    total = len(entries)
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    start = (page - 1) * size
    args = {**(request.view_args or {}),
            **{key: request.args[key] for key in ('q', 'status', 'row', 'state', 'preview', 'provider', 'batch', 'check', 'action',
                                                 'year', 'category', 'source', 'bucket', 'author', 'publisher', 'label', 'demographic', 'decade', 'rating', 'sort') if key in request.args}}
    paging = {'page': page, 'pages': pages, 'total': total, 'start': start + 1 if total else 0,
              'end': min(start + size, total),
              'previous': url_for(request.endpoint, **args, page=page - 1) if page > 1 else None,
              'next': url_for(request.endpoint, **args, page=page + 1) if page < pages else None}
    return entries[start:start + size], paging


@home.get("/")
def index():
    return render_template("home.html", portal_links=current_app.extensions["menus"].shown("portal"))


@api.get("/portal/library")
def portal_library():
    import hashlib
    import random
    from datetime import date, datetime
    from zoneinfo import ZoneInfo
    from .inventory import number
    inventory = current_app.extensions["inventory"]

    def cover_url(row, book=None):
        return book_cover(book or next(iter((row or {}).get("books") or []), None))

    def local_time(value):
        try:
            return datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Tokyo"))
        except (TypeError, ValueError):
            return None

    def added(value):
        moment = local_time(value)
        return f"{moment.month}/{moment.day} 追加" if moment else ""

    common = {"cover_url": cover_url, "completed": inventory.binge(12)}
    if request.args.get("section") == "completed":
        return render_template("portal_library.html", section="completed", **common)
    result = scan()
    rows = {row["id"]: row for row in inventory.cached()["series"]}
    anime = current_app.extensions["feeds"].read("anime")
    anime_owned = [item for item in (anime or {}).get("entries", []) if item.get("owned") and item.get("series_id") in rows]
    pick = None
    if anime_owned:
        entry = random.choice(anime_owned[:5])
        row = rows[entry["series_id"]]
        pick = {"entry": entry, "row": row, "url": inventory.read_url(row)}
    new_volumes = []
    for item in current_app.extensions["new_volumes"].read():
        if not item.get("new"):
            continue
        store = next((source["latest"]["url"] for source in item.get("sources", {}).values()
                      if (source.get("latest") or {}).get("url")), "")
        latest, owned = number(item.get("latest")), number(item.get("local_max"))
        new_volumes.append({**item, "store": store, "diff": latest - owned if latest is not None and owned is not None else "?"})
    checked = local_time(max((item.get("updated") or "" for item in new_volumes), default=""))
    curated = current_app.extensions["feeds"].read("curated")
    picks = list((curated or {}).get("entries", []))
    picks.sort(key=lambda item: hashlib.sha256((date.today().isoformat() + item["id"]).encode()).hexdigest())
    return render_template("portal_library.html", scan=result, stats=result.stats(), rows=rows, added=added,
                           read_url=inventory.read_url, recent=inventory.recent(12), anime=anime,
                           anime_owned=anime_owned[:12], pick=pick, new_volumes=new_volumes,
                           checked=checked.strftime("%Y-%m-%d %H:%M") if checked else "",
                           picks=picks[:6], **common)


DEMOGRAPHICS = ("少年", "少女", "青年", "レディース")
LIBRARY_PAGE = 60
LIBRARY_SORTS = {"": "名前順", "rating": "評価が高い順", "new": "新しい順", "old": "古い順", "books": "巻数が多い順", "random": "ランダム"}


def book_cover(book):
    """Thumbnail address for a book, or None when the template draws a cover instead (demo books have no images)."""
    if configuration().demo or not book or not (book.get("path") or book.get("komga_id")):
        return None
    return url_for("tools.book_thumbnail", book_id=book["id"], v=book.get("size") or None)


def library_view():
    """The existing Tools library browser: readership, status, decade, author, publisher and sort, with
    facets counted over the whole library so choices never disappear, and 60 more series per page."""
    import hashlib
    from collections import Counter
    from datetime import date
    from .authors import author_candidates
    args = request.args
    query = args.get("q", "")[:200]
    status, demographic, decade, author, publisher, sorting = (args.get(key, "") for key in
                                                              ("status", "demographic", "decade", "author", "publisher", "sort"))
    if (status not in {"", "completed", "ongoing"} or demographic not in {"", *DEMOGRAPHICS, "不明"}
            or sorting not in LIBRARY_SORTS or decade and not (decade.isdigit() and len(decade) == 4)):
        raise ConfigError("蔵書の絞り込み条件を確認してください。")
    try:
        page = int(args.get("page", "1"))
    except ValueError:
        raise ConfigError("ページは1以上の整数で指定してください。") from None
    if page < 1:
        raise ConfigError("ページは1以上の整数で指定してください。")
    result = scan()
    inventory = current_app.extensions["inventory"]
    details = {item["id"]: item for item in inventory.cached()["series"]}
    ai_summaries = {(item["series_id"], item["fields"]["summary"]) for item in current_app.extensions["ai_assist"].proposals()
                    if item["fields"].get("summary")}
    cards = []
    for series in result.series:
        row = details.get(series.id)
        facts = (row or {}).get("facts", {})
        year, rating = facts.get("original_year"), facts.get("rating")
        try:
            authors = [item["name"] for item in author_candidates(series.author)] if series.author else []
        except ConfigError:
            authors = [series.author]
        read = inventory.read_url(row) if row else ""
        tooltip = series.summary[:300] + ("\n\n（AI生成のあらすじ）" if (series.id, series.summary) in ai_summaries else "")
        cards.append({"series": series, "authors": authors, "tooltip": tooltip if series.summary else "",
                      "demographic": facts.get("demographic") if facts.get("demographic") in DEMOGRAPHICS
                      else next((genre for genre in series.genres if genre in DEMOGRAPHICS), ""),
                      "decade": str(year // 10 * 10) if type(year) is int else "", "year": year if type(year) is int else None,
                      "rating": rating if type(rating) in {int, float} and rating > 0 else None,
                      "cover": book_cover(next(iter(row["books"]), None)) if row else None, "detail": row is not None,
                      # Without Komga the read link would only search this page again.
                      "read": read if read and not read.startswith("/library") else ""})
    key = search_key(query)
    matched = [card for card in cards
               if (not demographic or card["demographic"] == ("" if demographic == "不明" else demographic))
               and (not decade or card["decade"] == decade)
               and (not author or author in card["authors"])
               and (not publisher or card["series"].publisher == publisher)
               and (status != "completed" or card["series"].status == "completed")
               and (status != "ongoing" or card["series"].status != "completed")
               and (not key or key in search_key(" ".join([card["series"].title, card["series"].reading, card["series"].author,
                                                         card["series"].publisher, *card["series"].genres, *card["series"].tags])))]
    matched.sort(key=lambda card: card["series"].reading or card["series"].title)
    if sorting == "rating":
        matched.sort(key=lambda card: -(card["rating"] or 0))
    elif sorting == "new":
        matched.sort(key=lambda card: -(card["year"] or 0))
    elif sorting == "old":
        matched.sort(key=lambda card: card["year"] or 9999)
    elif sorting == "books":
        matched.sort(key=lambda card: -len(card["series"].books))
    elif sorting == "random":
        # A daily order keeps "more" pages consistent, unlike a fresh shuffle per request.
        matched.sort(key=lambda card: hashlib.sha256((date.today().isoformat() + card["series"].id).encode()).hexdigest())
    page = min(page, max(1, -(-len(matched) // LIBRARY_PAGE)))
    current = {"q": query, "status": status, "demographic": demographic, "decade": decade, "author": author,
               "publisher": publisher, "sort": sorting}

    def link(**changes):
        return url_for("library.index", **{name: value for name, value in {**current, **changes}.items() if value})

    authors = Counter(name for card in cards for name in card["authors"])
    publishers = Counter(card["series"].publisher for card in cards if card["series"].publisher)
    demographics = Counter(card["demographic"] for card in cards)
    facets = {"demographics": [(name, demographics[name]) for name in DEMOGRAPHICS if demographics[name]],
              "unknown": demographics[""],
              "decades": sorted(Counter(card["decade"] for card in cards if card["decade"]).items()),
              # Like the existing Tools: the busiest publishers, and only authors with several series.
              "publishers": sorted(dict(publishers.most_common(24) + ([(publisher, publishers[publisher])] if publisher else [])).items(),
                                   key=lambda item: -item[1]),
              "authors": sorted({name: count for name, count in authors.items() if count >= 2 or name == author}.items()),
              "author_total": len(authors), "total": len(cards),
              "with_metadata": sum(1 for card in cards if card["demographic"] or card["series"].publisher)}
    shown = min(page * LIBRARY_PAGE, len(matched))
    return {"scan": result, "cards": matched[:shown], "matched": len(matched), "shown": shown, "facets": facets,
            "current": current, "link": link, "sorts": LIBRARY_SORTS,
            "more": {"href": link(page=page + 1), "page": page + 1} if shown < len(matched) else None}


@library.get("/library", endpoint="index")
def library_index():
    return render_template("library.html", **library_view())


@api.get("/library/results")
def library_results():
    return render_template("library_results.html", **library_view())


@organize.get("/organize", endpoint="index")
def organize_index():
    config = configuration()
    store = current_app.extensions["state"]
    preview_id = request.args.get("preview")
    job = store.job(preview_id, include_result=True) if preview_id else store.latest_preview()
    if preview_id and (job is None or job["kind"] != "preview" or job["status"] != "succeeded"):
        raise NotFound()
    # A plain visit uses the configured rules. A result link restores the rules
    # used for that preview without saving them as application configuration.
    layout = asdict(config.layout)
    if job and preview_id:
        layout = asdict(config.layout.with_overrides(job["result"].get("layout", {})))
    if job and job["result"].get("layout") != layout:
        job = None
    context = plan_view(job["result"], preview_id=job["id"]) if job else {}
    return render_template("organize.html", layout=layout, preview_job=job, **context)


def filtered_plan_items(result, state="", query=""):
    if state not in {"", "all", "ready", "unchanged", "blocked"}:
        raise ConfigError("状態は all、ready、unchanged、blocked を指定してください。")
    key = search_key(query)
    return [item for item in result["items"]
            if (state in {"", "all"} or item["state"] == state)
            and (not key or key in search_key(" ".join([item["title"], item["source"], item["target"] or "", *item["issues"]])))]


def plan_view(result, *, preview_id=None):
    default = next((state for state in ("blocked", "ready", "unchanged") if result["counts"][state]), "all")
    state, query = request.args.get("state", default), request.args.get("q", "")
    items, paging = paginate(filtered_plan_items(result, state, query), size=20)
    args = {**(request.view_args or {}), "q": query}
    if preview_id:
        args["preview"] = preview_id
    # Keep the chosen state on page links even when it was initially implicit.
    for direction, step in (("previous", -1), ("next", 1)):
        if paging[direction]:
            paging[direction] = url_for(request.endpoint, **args, state=state, page=paging["page"] + step)
    return {"plan": {**result, "items": items}, "pagination": paging, "query": query, "selected_state": state,
            "plan_filters": [{"state": key, "label": label,
                              "count": len(result["items"]) if key == "all" else result["counts"][key],
                              "url": url_for(request.endpoint, **args, state=key)}
                             for key, label in (("blocked", "要確認"), ("ready", "変更予定"),
                                                ("unchanged", "変更なし"), ("all", "すべて"))]}


@metadata.get("/metadata", endpoint="index")
def metadata_index():
    result = scan()
    provider = request.args.get("provider", "rakuten_books")
    state, query = request.args.get("state", "missing"), request.args.get("q", "")
    if provider not in PROVIDER_FIELDS or state not in {"missing", "all", "unlinked", "links"}:
        raise ConfigError("補完する情報と表示条件を選んでください。")
    missing = {series.id: missing_fields(series.to_dict(), provider) for series in result.series}
    from .provenance import source_links
    links = current_app.extensions["state"].edits()
    entries = [series for series in result.series if (state != "missing" or missing[series.id])
               and (state != "unlinked" or provider not in source_links(links.get(series.id, {}).get("provenance", {})))
               and search_key(query) in search_key(series.title + series.author + series.reading)]
    entries, paging = paginate(entries)
    store = current_app.extensions["state"]
    batch_id = request.args.get("batch")
    batch = store.job(batch_id, include_result=True) if batch_id else None
    if batch_id and (batch is None or batch["kind"] != "enrichment"):
        raise NotFound()
    recent = [job for job in store.jobs() if job["kind"] == "enrichment"][:5]
    return render_template("metadata.html", entries=entries, missing=missing, scan=result, pagination=paging,
                           provider=provider, state=state, query=query,
                           catalogs=current_app.extensions["enrichment"].statuses(), batch=batch, recent=recent,
                           saved=store.operation_edits(batch_id, "enrichment") if batch else None)


@management.get("/manage", endpoint="index")
def management_index():
    return render_template("management.html")


@management.get("/manage/metadata", endpoint="metadata")
def manual_metadata_index():
    result = scan()
    query = request.args.get("q", "")
    entries = [series for series in result.series if search_key(query) in search_key(series.title + series.author)]
    entries, paging = paginate(entries)
    return render_template("metadata_manual.html", entries=entries, query=query, scan=result, pagination=paging)


@metadata.get("/metadata/<series_id>")
def metadata_detail(series_id):
    data = metadata_service().inspect(series_id)
    return render_template("metadata_detail.html", **data)


@metadata.get("/metadata/<series_id>/sources")
def metadata_sources(series_id):
    data = metadata_service().inspect(series_id)
    provider = request.args.get("provider", "rakuten_books")
    enrichment = current_app.extensions["enrichment"]
    data["provenance"] = data["provenances"].get(provider, {})
    return render_template("metadata_sources.html", **data, catalog=enrichment.status(provider), catalogs=enrichment.statuses(),
                           known_titles=enrichment.known_titles(series_id),
                           return_url=url_for("metadata.index", provider=provider, batch=request.args.get("batch")))


@jobs.get("/jobs", endpoint="index")
def jobs_index():
    return render_template("jobs.html", jobs=current_app.extensions["state"].jobs(),
                           events=current_app.extensions["state"].events())


@jobs.get("/jobs/<job_id>", endpoint="detail")
def job_detail(job_id):
    job = current_app.extensions["state"].job(job_id, include_result=True)
    if job is None:
        raise NotFound()
    context = plan_view(job["result"]) if job["kind"] == "preview" and job["result"] else {}
    if job["status"] == "succeeded" and (job["result"] or {}).get("komga_followup"):
        context["komga_followup"] = current_app.extensions["maintenance_ops"].followups.inspect(job["result"]["komga_followup"])
    return render_template("job_detail.html", job=job, **context)


@settings.get("/settings", endpoint="index")
def settings_index():
    config = configuration()
    return render_template("settings.html", key_configured=bool(secret(config, config.api_key_env)), layout=asdict(config.layout),
                           catalog=current_app.extensions["enrichment"].status(),
                           preferences=current_app.extensions["preferences"].view(),
                           menus=current_app.extensions["menus"].view())


@api.post("/menus")
def save_menus():
    require_admin_access()
    body = body_with_keys({"revision", "header", "portal"}, {"revision", "header", "portal"})
    revision = body.pop("revision")
    if type(revision) is not int or revision < 0:
        raise ConfigError("設定の版を確認してください。")
    return jsonify(current_app.extensions["menus"].save(body, revision))


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
                   api_key_configured=bool(secret(config, config.api_key_env)),
                   preview_only=config.demo or not config.web_execution_enabled,
                   execution_enabled=config.execution_enabled, web_execution_enabled=config.web_execution_enabled,
                   execution_interface="cli+web-normalization" if config.web_execution_enabled else "cli",
                   metadata_catalog=current_app.extensions["enrichment"].status(),
                   metadata_catalogs=current_app.extensions["enrichment"].statuses(),
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
    result = metadata_service().save(series_id, body.get("fields"), body["revision"], body["base_fingerprint"],
                                     reset=body.get("reset", False))
    return jsonify(saved_komga_followup(result, {series_id: result["changes"]},
                   f"manual:{series_id}:{result['revision']}", url_for("metadata.metadata_detail", series_id=series_id)))


@api.post("/metadata/<series_id>/search")
def metadata_search(series_id):
    body = body_with_keys({"title", "author", "isbn", "page", "provider"})
    return jsonify(current_app.extensions["enrichment"].search(series_id, **body))


@api.post("/metadata/<series_id>/source/preview")
def source_preview(series_id):
    body = body_with_keys({"search_id", "candidate_id", "fields", "unlink", "provider"})
    return jsonify(current_app.extensions["enrichment"].preview(series_id, **body))


@api.post("/metadata/<series_id>/source")
def source_save(series_id):
    body = body_with_keys({"search_id", "candidate_id", "fields", "unlink", "revision", "base_fingerprint", "provider"},
                          {"revision", "base_fingerprint"})
    result = current_app.extensions["enrichment"].save(series_id, **body)
    return jsonify(saved_komga_followup(result, {series_id: result["changes"]},
                   f"source:{series_id}:{result['revision']}", url_for("metadata.metadata_sources", series_id=series_id,
                   provider=body.get("provider", "rakuten_books"))))


@api.post("/enrichment/batches")
def enrichment_batch():
    body = body_with_keys({"series_ids", "provider", "link_only"}, {"series_ids"})
    job_id = current_app.extensions["auto_enrichment"].submit(**body)
    return jsonify(current_app.extensions["state"].job(job_id)), 202


@api.post("/enrichment/batches/<job_id>/apply")
def apply_enrichment_batch(job_id):
    body = body_with_keys({"confirm"}, {"confirm"})
    result = current_app.extensions["auto_enrichment"].apply(job_id, body["confirm"])
    batch = current_app.extensions["state"].job(job_id, include_result=True)["result"]
    changes = {row["series_id"]: row["preview"]["changes"] for row in batch["rows"] if row["state"] == "ready"}
    return jsonify(saved_komga_followup(result, changes, "enrichment:" + job_id,
                   url_for("metadata.index", batch=job_id, provider=batch["provider"])))


def saved_komga_followup(result, changes, source, back_url):
    identifier = current_app.extensions["maintenance_ops"].followups.create(source, changes, back_url)
    return {**result, "komga_followup": identifier}


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
        items = filtered_plan_items(result, request.args.get("state", ""), request.args.get("q", ""))
        result.update(items=items[offset:offset + limit], total_items=len(result["items"]),
                      filtered_items=len(items), limit=limit, offset=offset)
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
