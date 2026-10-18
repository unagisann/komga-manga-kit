"""Management actions for the existing Tools feature set."""
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

from .credentials import save_credentials
from .web import body_with_keys, require_admin_access

maintenance = Blueprint("maintenance", __name__)


@maintenance.get("/settings/tools")
def settings():
    # Connections and sources now share the single settings page.
    return redirect(url_for("settings.index", _anchor="komga"))


@maintenance.post("/api/tools/settings")
def save_settings():
    require_admin_access()
    body = body_with_keys({"revision", "komga_url", "rakuten", "anilist", "tools"}, {"revision"})
    revision = body.pop("revision")
    if type(revision) is not int or revision < 0:
        from .config import ConfigError
        raise ConfigError("設定の版を確認してください。")
    return jsonify(current_app.extensions["preferences"].save(body, revision))


@maintenance.post("/api/tools/credentials")
def credentials():
    require_admin_access()
    body = body_with_keys({"values"}, {"values"})
    return jsonify(configured=save_credentials(current_app.config["MANGA_KIT"], body["values"]))


@maintenance.get("/smart-shelves")
def shelves():
    service = current_app.extensions["smart_shelves"]
    identifier = request.args.get("plan")
    return render_template("smart_shelves.html", targets=service.targets(), operation=service.inspect(identifier) if identifier else None)


@maintenance.post("/api/tools/shelves/preview")
def shelves_preview():
    require_admin_access(); body_with_keys(set())
    return jsonify(current_app.extensions["smart_shelves"].prepare())


@maintenance.post("/api/tools/shelves/<identifier>/<action>")
def shelves_action(identifier, action):
    from .config import ConfigError
    if action not in {"execute", "restore"}:
        raise ConfigError("同期または復元を指定してください。")
    require_admin_access()
    body = body_with_keys({"confirm"}, {"confirm"})
    service = current_app.extensions["smart_shelves"]
    job = current_app.extensions["jobs"].submit_task("shelf_" + action,
        lambda progress: service.execute(identifier, body["confirm"], restore=action == "restore"))
    return jsonify(current_app.extensions["state"].job(job)), 202


@maintenance.get("/ai")
def ai():
    from .japanese import search_key
    query = request.args.get("q", "")[:200]
    rows = [row for row in current_app.extensions["inventory"].cached()["series"] if row.get("local_id")
            and search_key(query) in search_key(row["title"] + row["author"])]
    return render_template("ai.html", rows=rows[:100], query=query, proposals=current_app.extensions["ai_assist"].proposals()[:60],
                           audit=current_app.extensions["ai_assist"].audit()[:100],
                           statuses=current_app.extensions["state"].cached_prefix("ai:status:"))


@maintenance.post("/api/tools/ai/models")
def ai_models():
    require_admin_access(); body_with_keys(set())
    return jsonify(models=current_app.extensions["ai_assist"].models())


@maintenance.post("/api/tools/ai/generate")
def ai_generate():
    require_admin_access()
    body = body_with_keys({"series_id", "fields", "web_search"}, {"series_id", "fields"})
    service = current_app.extensions["ai_assist"]
    job = current_app.extensions["jobs"].submit_task("ai_generate", lambda progress: service.generate(**body))
    return jsonify(current_app.extensions["state"].job(job)), 202


@maintenance.post("/api/tools/ai/<identifier>/<action>")
def ai_action(identifier, action):
    from .config import ConfigError
    if action not in {"save", "restore"}:
        raise ConfigError("保存または復元を指定してください。")
    require_admin_access()
    body = body_with_keys({"confirm", "fields"}, {"confirm"})
    return jsonify(current_app.extensions["ai_assist"].save(identifier, body["confirm"], body.get("fields"), restore=action == "restore"))


@maintenance.get("/pages")
def page_books():
    from .japanese import search_key
    from .web import paginate
    query = request.args.get("q", "")[:200]
    rows = [{"series": row, "book": book} for row in current_app.extensions["inventory"].cached()["series"]
            if search_key(query) in search_key(row["title"] + row["author"]) for book in row["books"] if book.get("path")]
    rows, pagination = paginate(rows, 40)
    return render_template("page_books.html", rows=rows, pagination=pagination, query=query,
                           marks=current_app.extensions["state"].cached_prefix("pages:mark:"))


@maintenance.get("/pages/<book_id>")
def edit_pages(book_id):
    from .config import ConfigError
    if current_app.config["MANGA_KIT"].demo:
        raise ConfigError("ページの編集は実ライブラリの書庫で利用できます。")
    editor = current_app.extensions["page_editor"]
    identifier = request.args.get("plan")
    operation = editor.inspect(identifier) if identifier else None
    if operation and operation["plan"].get("purpose") != "page_edit":
        raise ConfigError("ページ修正の確認結果を指定してください。")
    record = current_app.extensions["state"].cached("pages:operation:" + identifier) if identifier else None
    if operation and (record is None or record["data"]["book_id"] != book_id):
        raise ConfigError("この本のページ修正を選んでください。")
    review = editor.review(book_id) if operation is None or operation["status"] == "planned" else {
        "book": {"id": book_id, "volume": record["data"].get("volume", "")}, "pages": [], "fingerprint": ""}
    return render_template("page_editor.html", **review, operation=operation)


@maintenance.post("/api/tools/pages/<book_id>/preview")
def pages_preview(book_id):
    require_admin_access()
    request.max_content_length = 128 * 1024
    body = body_with_keys({"selected_pages", "fingerprint"}, {"selected_pages", "fingerprint"})
    return jsonify(current_app.extensions["page_editor"].prepare_book(book_id, **body))


@maintenance.post("/api/tools/pages/<book_id>/mark")
def pages_mark(book_id):
    require_admin_access()
    body = body_with_keys({"status"}, {"status"})
    current_app.extensions["page_editor"].mark(book_id, body["status"])
    return jsonify(saved=True)


@maintenance.post("/api/tools/page-operations/<identifier>/<action>")
def pages_action(identifier, action):
    from .config import ConfigError
    from .web import require_normalization_execution
    require_normalization_execution()
    if action not in {"execute", "restore"}:
        raise ConfigError("実行または復元を指定してください。")
    body = body_with_keys({"confirm"}, {"confirm"})
    editor = current_app.extensions["page_editor"]
    inventory = current_app.extensions["inventory"]
    operation = editor.inspect(identifier)
    if operation["plan"].get("purpose") != "page_edit":
        raise ConfigError("ページ修正の確認結果を指定してください。")
    def run(progress):
        result = getattr(editor, action)(identifier, body["confirm"])
        inventory.refresh()
        return {"operation_id": identifier, "files": result["moved_count"], "status": result["status"]}
    job = current_app.extensions["jobs"].submit_task("pages_" + action, run)
    return jsonify(current_app.extensions["state"].job(job)), 202


@maintenance.get("/pages/trash")
def page_trash():
    return render_template("page_trash.html", rows=current_app.extensions["page_editor"].trash())


@maintenance.post("/api/tools/page-operations/<identifier>/purge")
def pages_purge(identifier):
    from .web import require_normalization_execution
    require_normalization_execution()
    body = body_with_keys({"confirm"}, {"confirm"})
    return jsonify(current_app.extensions["page_editor"].purge(identifier, body["confirm"]))


@maintenance.get("/automation")
def automation():
    from .automation import STEPS
    store = current_app.extensions["state"]
    runs = sorted((value["data"] for value in store.cached_prefix("automation:run:").values()), key=lambda value: value["created"], reverse=True)
    return render_template("automation.html", config=current_app.extensions["automation"].config(), steps=STEPS, runs=runs[:50],
                           operations=[value["data"] for value in store.cached_prefix("automation:operation:").values()],
                           last_error=store.cached("automation:last_error"))


@maintenance.post("/api/tools/automation/settings")
def automation_settings():
    require_admin_access()
    body = body_with_keys({"revision", "enabled", "time", "steps", "post_import", "auto_apply"},
                          {"revision", "enabled", "time", "steps", "post_import", "auto_apply"})
    revision = body.pop("revision")
    if type(revision) is not int or revision < 0:
        from .config import ConfigError
        raise ConfigError("設定の版を確認してください。")
    return jsonify(current_app.extensions["automation"].save(body, revision))


@maintenance.post("/api/tools/automation/run")
def automation_run():
    require_admin_access()
    body = body_with_keys({"identifier"})
    job = current_app.extensions["automation"].submit(identifier=body.get("identifier"))
    return jsonify(current_app.extensions["state"].job(job)), 202


@maintenance.get("/completion")
def completion():
    service = current_app.extensions["maintenance_ops"]
    identifier = request.args.get("plan")
    return render_template("maintenance_operations.html", kind="completion", rows=service.candidates(),
                           operation=service.inspect(identifier) if identifier else None)


@maintenance.get("/komga-sync")
def komga_sync():
    service = current_app.extensions["maintenance_ops"]
    identifier = request.args.get("plan")
    return render_template("maintenance_operations.html", kind="komga", rows=[],
                           operation=service.inspect(identifier) if identifier else None)


@maintenance.post("/api/tools/maintenance/<kind>/preview")
def maintenance_preview(kind):
    require_admin_access()
    body = body_with_keys({"mode", "series_ids", "fields", "overwrite", "replace_locked", "replace_lists"})
    service = current_app.extensions["maintenance_ops"]
    job = current_app.extensions["jobs"].submit_task("maintenance_preview", lambda progress: service.prepare(kind, **body))
    return jsonify(current_app.extensions["state"].job(job)), 202


@maintenance.post("/api/tools/maintenance/<identifier>/<action>")
def maintenance_action(identifier, action):
    from .config import ConfigError
    from .web import require_normalization_execution
    require_normalization_execution()
    if action not in {"execute", "restore"}:
        raise ConfigError("実行または復元を指定してください。")
    body = body_with_keys({"confirm"}, {"confirm"})
    service = current_app.extensions["maintenance_ops"]
    job = current_app.extensions["jobs"].submit_task("maintenance_" + action,
        lambda progress: service.execute(identifier, body["confirm"], restore=action == "restore"))
    return jsonify(current_app.extensions["state"].job(job)), 202
