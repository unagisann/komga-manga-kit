"""Functional tools pages; visual consolidation follows feature parity."""
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, current_app, jsonify, render_template, request, url_for

from .config import ConfigError
from .feeds import CATEGORIES, DISCOVERY_FEEDS, season
from .japanese import search_key
from .web import body_with_keys, paginate, require_admin_access

tools = Blueprint("tools", __name__)


def service(name):
    return current_app.extensions[name]


def job_response(identifier):
    return jsonify(service("state").job(identifier)), 202


def requested_year():
    try:
        return int(request.args.get("year", season()["year"]))
    except ValueError:
        raise ConfigError("年は整数で指定してください。") from None


@tools.get("/anime")
def anime():
    data = service("feeds").read("anime")
    return render_template("tools_feed.html", title="今期アニメと持っている原作", kind="anime", data=data,
                           source_key="feed:anime", period=season(), year=None, category=None)


@tools.get("/discovery")
def discovery():
    source = request.args.get("source", "curated")
    if source not in {*DISCOVERY_FEEDS, "rakuten"}:
        raise ConfigError("発見の情報源を選んでください。")
    return render_template("tools_feed.html", title="漫画を見つける", kind=source, source_key="feed:" + source,
                           data=service("feeds").read(source), year=None, category=None)


@tools.get("/tsugimanga")
def rankings():
    year, category = requested_year(), request.args.get("category", "comics")
    data = service("feeds").read("ranking", year, category)
    return render_template("tools_feed.html", title="次にくるマンガ大賞", kind="ranking", data=data,
                           source_key=f"feed:ranking:{year}:{category}", year=year, category=category,
                           years=range(season()["year"], 2014, -1), categories=CATEGORIES)


@tools.get("/tsugimanga/<int:year>/<category>")
def ranking_year(year, category):
    from flask import redirect
    service("feeds").key("ranking", year, category)
    return redirect(url_for("tools.rankings", year=year, category=category))


@tools.get("/new_volumes")
def new_volumes():
    rows = service("new_volumes").read()
    state = request.args.get("state", "all")
    if state not in {"all", "new", "review", "preorder"}:
        raise ConfigError("表示する新刊の状態を選んでください。")
    if state == "new":
        rows = [row for row in rows if row.get("new")]
    elif state == "review":
        rows = [row for row in rows if row.get("errors") or any(source.get("review") or source.get("incomplete") for source in row.get("sources", {}).values())]
    elif state == "preorder":
        rows = [row for row in rows if any(source.get("upcoming") for source in row.get("sources", {}).values())]
    rows, paging = paginate(rows, size=30)
    return render_template("new_volumes.html", rows=rows, pagination=paging, state=state,
                           notification=service("state").cached("newvol:notification:last"))


@tools.get("/shelf/series/<series_id>")
def series(series_id):
    row = service("inventory").find(series_id)
    facts = service("state").cached("facts:" + series_id)
    books, paging = paginate(row["books"], 60)
    return render_template("series.html", series=row, read_url=service("inventory").read_url(row),
                           books=books, pagination=paging, book_urls={book["id"]: service("inventory").read_url(row, book) for book in books},
                           facts_revision=facts["revision"] if facts else 0)


@tools.get("/reading")
def reading():
    require_admin_access()
    reading_service = service("reading")
    accounts = reading_service.accounts()
    name = request.args.get("account", accounts[0]["name"])
    data = reading_service.read(name)
    state = request.args.get("state", "reading")
    if state not in {"reading", "paused", "completed", "all"}:
        raise ConfigError("読書状況を選んでください。")
    rows = [item for item in (data or {}).get("entries", []) if state == "all" or item["state"] == state]
    rows, paging = paginate(rows, 30)
    last = service("state").cached("reading:last")
    return render_template("reading.html", data=data, rows=rows, accounts=accounts, account=name, state=state,
                           pagination=paging, errors=last["data"]["errors"] if last else [])


@tools.post("/api/tools/reading/refresh")
def reading_refresh():
    require_admin_access(); body_with_keys(set())
    return job_response(service("jobs").submit_task("reading_refresh", service("reading").refresh))


@tools.get("/api/tools/books/<book_id>/page/<int:page>")
def book_page(book_id, page):
    if current_app.config["MANGA_KIT"].demo:
        from flask import redirect
        return redirect(url_for("static", filename="mark.svg"))
    book = service("book_pages").find(book_id, local=False)
    if not book.get("path") and book.get("komga_id"):
        data, mime = service("komga").image(book["komga_id"], page)
    else:
        data, mime = service("book_pages").image(book_id, page)
    return Response(data, mimetype=mime)


@tools.post("/api/tools/series/<series_id>/facts")
def save_facts(series_id):
    require_admin_access()
    body = body_with_keys({"revision", "label", "magazine", "demographic", "original_year", "rating"}, {"revision"})
    service("inventory").find(series_id)
    if type(body["revision"]) is not int or body["revision"] < 0:
        raise ConfigError("編集前の版を指定してください。")
    facts = {}
    for key in ("label", "magazine", "demographic"):
        value = body.get(key, "")
        if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
            raise ConfigError("書誌情報は200文字以内の文字列で指定してください。")
        facts[key] = value.strip()
    year, rating = body.get("original_year"), body.get("rating")
    if year is not None and (type(year) is not int or not 1000 <= year <= season()["year"]):
        raise ConfigError("連載開始年を正しい年で指定してください。")
    if rating is not None and (type(rating) not in {int, float} or not 0 <= rating <= 5):
        raise ConfigError("評価は0から5の数値で指定してください。")
    facts.update(original_year=year, rating=rating)
    revision = service("state").cache("facts:" + series_id, facts, revision=body["revision"])
    service("inventory").update_facts(series_id, facts)
    return jsonify(saved=True, revision=revision)


@tools.post("/api/tools/inventory/refresh")
def refresh_inventory():
    require_admin_access(); body_with_keys(set())
    return job_response(service("jobs").submit_task("inventory_refresh", service("inventory").refresh))


@tools.post("/api/tools/feeds/<kind>/refresh")
def refresh_feed(kind):
    require_admin_access()
    body = body_with_keys({"year", "category"})
    return job_response(service("feeds").submit(kind, body.get("year"), body.get("category")))


@tools.post("/api/tools/rankings/refresh-all")
def refresh_rankings():
    require_admin_access(); body_with_keys(set())
    feeds = service("feeds")
    def update(progress):
        count, errors = 0, []
        targets = [(year, category) for year in range(2015, season()["year"] + 1) for category in CATEGORIES]
        for index, (year, category) in enumerate(targets):
            try:
                feeds.refresh("ranking", year, category)
                count += 1
            except ConfigError as error:
                errors.append({"year": year, "category": category, "error": str(error)})
            except Exception as error:
                from .catalogs import CatalogError
                if not isinstance(error, CatalogError) or error.retry_after:
                    raise
                errors.append({"year": year, "category": category, "error": str(error)})
            progress(index + 1, len(targets))
        return {"items": count, "errors": errors, "source": "feed:ranking"}
    return job_response(service("jobs").submit_task("source_refresh", update))


@tools.post("/api/tools/new-volumes/check")
def check_volumes():
    require_admin_access()
    body = body_with_keys({"force", "new_only", "series_ids"})
    return job_response(service("new_volumes").submit(**body))


@tools.post("/api/tools/new-volumes/<series_id>/policy")
def volume_policy(series_id):
    require_admin_access()
    body = body_with_keys({"digital"}, {"digital"})
    service("new_volumes").policy(series_id, body["digital"])
    return jsonify(saved=True)


@tools.get("/api/tools/owned")
def owned():
    query = request.args.get("q", "")
    if len(query) > 200:
        raise ConfigError("検索語は200文字以内で指定してください。")
    rows = [row for row in service("inventory").cached()["series"] if search_key(query) in search_key(row["title"] + row["author"])]
    return jsonify(items=[{"id": row["id"], "title": row["title"], "author": row["author"]} for row in rows[:20]], total=len(rows))


@tools.post("/api/tools/matches")
def match_override():
    require_admin_access()
    body = body_with_keys({"source", "entry_id", "series_id"}, {"source", "entry_id", "series_id"})
    if not isinstance(body["source"], str) or not body["source"].startswith("feed:"):
        raise ConfigError("情報源の作品を選んでください。")
    cached = service("state").cached(body["source"])
    if cached is None and current_app.config["MANGA_KIT"].demo:
        parts = body["source"].split(":")
        data = service("feeds").demo(parts[1], int(parts[2]) if len(parts) > 2 else None, parts[3] if len(parts) > 3 else None)
    else:
        data = cached["data"] if cached else {}
    if str(body["entry_id"]) not in {item["id"] for item in data.get("entries", [])}:
        raise ConfigError("作品が取得結果にありません。結果を更新してください。")
    service("inventory").match_override(body["source"], str(body["entry_id"]), body["series_id"])
    return jsonify(saved=True)
