"""
CORS for the API, so the player can run from GitHub Pages.
Set ALLOWED_ORIGIN in Render (Environment) to your Pages origin, e.g.
    https://yourname.github.io
Leave it empty to allow same-origin only (the Render-hosted player still works).
"""

import os

from flask import request

ALLOWED = [o.strip() for o in os.environ.get("ALLOWED_ORIGIN", "").split(",") if o.strip()]


def install_cors(app):
    @app.before_request
    def preflight():
        if request.method == "OPTIONS":
            return ("", 204)

    @app.after_request
    def add_cors(resp):
        origin = request.headers.get("Origin", "")
        if origin and origin in ALLOWED:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Range"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range, Accept-Ranges"
        return resp
