"""Same-origin install metadata with an offline document, never a library cache."""
from flask import Blueprint, Response, current_app, jsonify, render_template, url_for

pwa = Blueprint("pwa", __name__)


@pwa.get("/manifest.webmanifest")
def manifest():
    return jsonify(name=current_app.config["MANGA_KIT"].name, short_name="Manga Kit", lang="ja", start_url="/", scope="/",
                   display="standalone", background_color="#F0EEE6", theme_color="#F0EEE6",
                   icons=[{"src": url_for("static", filename="mark.svg"), "sizes": "any", "type": "image/svg+xml", "purpose": "any"}])


@pwa.get("/offline")
def offline():
    return render_template("offline.html")


@pwa.get("/sw.js")
def worker():
    from pathlib import Path
    return Response((Path(current_app.static_folder) / "sw.js").read_text(), mimetype="application/javascript")
