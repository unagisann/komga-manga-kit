"""Functional tools pages; visual consolidation follows feature parity."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, current_app, jsonify, render_template, request, url_for

from .config import ConfigError
from .covers import small_image
from .discovery import discovery_view
from .feeds import CATEGORIES, DISCOVERY_FEEDS, season
from .japanese import search_key
from .komga import KomgaError
from .web import body_with_keys, book_cover, paginate, require_admin_access

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


def owned_links(entries):
    """Where an owned entry opens: Komga when it knows the series, otherwise the series page here."""
    inventory = service("inventory")
    rows = {row["id"]: row for row in inventory.cached()["series"]}
    links = {}
    for entry in entries:
        row = rows.get(entry.get("series_id")) if entry.get("owned") else None
        if row:
            read = inventory.read_url(row) if row.get("komga_id") else ""
            links[entry["id"]] = {"read": read if read.startswith(("http://", "https://")) else "",
                                  "series": url_for("tools.series", series_id=row["id"])}
    return links


@tools.get("/anime")
def anime():
    data = service("feeds").read("anime")
    entries = (data or {}).get("entries", [])
    counts = {"owned": sum(bool(item.get("owned")) for item in entries), "manga": sum(bool(item.get("has_manga")) for item in entries),
              "continuing": sum(bool(item.get("continuing")) for item in entries), "all": len(entries)}
    return render_template("anime.html", data=data, counts=counts, links=owned_links(entries),
                           source_key="feed:anime", current=season())


@tools.get("/discovery")
def discovery():
    source = request.args.get("source", "curated")
    if source not in {*DISCOVERY_FEEDS, "rakuten"}:
        raise ConfigError("発見の情報源を選んでください。")
    if source == "curated":
        data = service("feeds").read(source)
        return render_template("discovery.html", data=data, view=discovery_view(data, service("inventory")),
                               cover_url=book_cover, source_key="feed:curated")
    return render_template("tools_feed.html", title="漫画を見つける", kind=source, source_key="feed:" + source,
                           data=service("feeds").read(source), year=None, category=None)


@tools.post("/api/tools/discovery/refresh")
def refresh_discovery():
    require_admin_access()
    body_with_keys(set())
    return job_response(service("feeds").submit_discovery())


def ranking_years():
    return list(range(season()["year"], 2014, -1))


@tools.get("/tsugimanga")
def rankings():
    if "year" in request.args or "category" in request.args:
        # Earlier links carried the year and category as query parameters.
        from flask import redirect
        year, category = requested_year(), request.args.get("category", "comics")
        service("feeds").key("ranking", year, category)
        return redirect(url_for("tools.ranking_year", year=year, category=category))
    demo = current_app.config["MANGA_KIT"].demo
    cached = set(service("state").cached_keys("feed:ranking:"))
    available = {year: [category for category in CATEGORIES if demo or f"feed:ranking:{year}:{category}" in cached]
                 for year in ranking_years()}
    return render_template("rankings.html", years=ranking_years(), available=available, categories=CATEGORIES)


@tools.get("/tsugimanga/<int:year>/<category>")
def ranking_year(year, category):
    data = service("feeds").read("ranking", year, category)
    entries = (data or {}).get("entries", [])
    return render_template("ranking.html", data=data, year=year, category=category, categories=CATEGORIES,
                           years=ranking_years(), links=owned_links(entries),
                           ranked=[item for item in entries if item.get("rank")],
                           nominees=[item for item in entries if not item.get("rank")],
                           source_key=f"feed:ranking:{year}:{category}")


RECENT_RELEASE_DAYS = 14


def volume_sections(rows, today):
    """The existing Tools groups: released, pre-order only, up to date, needs review, previous values and errors."""
    from .inventory import number
    sections = {name: [] for name in ("new", "preorder", "current", "review", "stale", "errors")}
    recent = (today - timedelta(days=RECENT_RELEASE_DAYS)).isoformat()
    for row in rows:
        sources = row.get("sources", {})
        released = {name: source["latest"] for name, source in sources.items() if source.get("latest")}
        upcoming = sorted(({**item, "media": name} for name, source in sources.items() for item in source.get("upcoming", [])),
                          key=lambda item: (item.get("date") or "", number(item.get("volume")) or 0))
        local, latest = number(row.get("local_max")), number(row.get("latest"))
        paper, digital = (number((released.get(name) or {}).get("volume")) for name in ("paper", "digital"))
        store = next((item["url"] for item in [*released.values(), *upcoming] if item.get("url")), "")
        view = {**row, "paper": paper, "digital": digital, "preorder": upcoming[0] if upcoming else None, "store": store,
                "diff": latest - local if latest is not None and local is not None else None,
                "recent": bool(row.get("new") and any((item.get("date") or "") >= recent for item in released.values())),
                "review_count": sum(len(source.get("review", [])) for source in sources.values()),
                "incomplete": any(source.get("incomplete") for source in sources.values())}
        if row.get("stale"):
            sections["stale"].append(view)
        if row.get("errors") and latest is None and not row.get("stale"):
            sections["errors"].append(view)
        elif row.get("new"):
            sections["new"].append(view)
        elif upcoming:
            sections["preorder"].append(view)
        elif latest is not None and local is not None and latest >= local:
            sections["current"].append(view)
        else:
            sections["review"].append(view)
    sections["preorder"].sort(key=lambda view: (view["preorder"].get("date") or "", view["title"]))
    return sections


@tools.get("/new_volumes")
def new_volumes():
    rows = service("new_volumes").read()
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    ongoing = {row["id"] for row in service("inventory").cached()["series"] if row["status"] == "ongoing"}
    checked = max((row.get("updated") or "" for row in rows), default="")
    return render_template("new_volumes.html", sections=volume_sections(rows, today), total=len(rows), checked=checked,
                           unchecked=len(ongoing - {row["series_id"] for row in rows}), recent_days=RECENT_RELEASE_DAYS,
                           notification=service("state").cached("newvol:notification:last"))


@tools.get("/shelf/series/<series_id>")
def series(series_id):
    row = service("inventory").find(series_id)
    facts = service("state").cached("facts:" + series_id)
    books, paging = paginate(row["books"], 60)
    return render_template("series.html", series=row, read_url=service("inventory").read_url(row),
                           books=books, pagination=paging, book_urls={book["id"]: service("inventory").read_url(row, book) for book in books},
                           facts_revision=facts["revision"] if facts else 0)


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


@tools.get("/api/tools/books/<book_id>/thumbnail")
def book_thumbnail(book_id):
    # As in the existing Tools: prefer Komga's small thumbnail and let the browser keep covers for a day.
    # Callers add the book size to the URL, so a changed archive gets a new address.
    if current_app.config["MANGA_KIT"].demo:
        from flask import redirect
        return redirect(url_for("static", filename="mark.svg"))
    book = service("book_pages").find(book_id, local=False)
    image = None
    if book.get("komga_id"):
        try:
            image = service("komga").thumbnail(book["komga_id"])
        except KomgaError:
            if not book.get("path"):
                raise
    if image is None:
        # A local first page can be several megabytes; send a small JPEG when it decodes.
        data, mime = service("book_pages").image(book_id, 1)
        small = small_image(data, 360)
        image = (small, "image/jpeg") if small else (data, mime)
    return Response(image[0], mimetype=image[1], headers={"Cache-Control": "private, max-age=86400"})


@tools.get("/api/tools/books/<book_id>/page/<int:page>/preview")
def book_page_preview(book_id, page):
    """A small page image for the cover lists and the page editor; the caller puts the book size in the URL."""
    if current_app.config["MANGA_KIT"].demo:
        from flask import redirect
        return redirect(url_for("static", filename="mark.svg"))
    book = service("book_pages").find(book_id, local=False)
    if not book.get("path") and book.get("komga_id"):
        data, mime = service("komga").image(book["komga_id"], page)
    else:
        data, mime = service("book_pages").image(book_id, page)
    small = small_image(data, 240)
    return Response(small or data, mimetype="image/jpeg" if small else mime, headers={"Cache-Control": "private, max-age=86400"})


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
