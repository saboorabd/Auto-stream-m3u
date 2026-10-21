"""
Lightning-fast Stream server for Render (No Playwright/Chrome dependency).
Instant M3U playlist generation with ClearKey & Cookie support.
"""

import os
import re
import json
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from pathlib import Path
from flask import Flask, Response, jsonify, request, redirect, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from cors import install_cors

try:
    from curl_cffi import requests as http_client
    HTTP_IMPERSONATE = "chrome"
except ImportError:
    import requests as http_client
    HTTP_IMPERSONATE = None

BASE_DIR = Path(__file__).resolve().parent
CFG_PATH = BASE_DIR / "config.json"
CFG = json.loads(CFG_PATH.read_text(encoding="utf-8")) if CFG_PATH.exists() else {}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
REFERER = "https://jjtvxweb.pages.dev/"

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
install_cors(app)

# Fallback stream URL with active token parameters
DEFAULT_STREAM = CFG.get("stream_url") or "https://jiotvpllive.cdn.jio.com/bpk-tv/ZeeThiraiHD_BTS/WDVLive/index.mpd?__hdnea__=st=1792502910~exp=1792506510~acl=/bpk-tv/ZeeThiraiHD_BTS/WDVLive/*~hmac=fe0f6800c84c29f75c42f8e05457c3cb05c413d77e50daed218a52b53f06e7f5"
ACTIVE_STREAM = DEFAULT_STREAM

ACCESS_KEY = os.environ.get("ACCESS_KEY", "").strip()

def key_ok():
    return not ACCESS_KEY or request.args.get("key", "") == ACCESS_KEY

@app.post("/api/target")
def api_target():
    global ACTIVE_STREAM
    body = request.get_json(silent=True) or {}
    value = str(body.get("page_url") or body.get("stream_url") or "").strip()
    if not value:
        return jsonify({"error": "Provide a stream URL"}), 400
    ACTIVE_STREAM = value
    return jsonify({"ok": True, "url": value}), 200

@app.get("/api/stream")
def api_stream():
    return jsonify({
        "url": ACTIVE_STREAM,
        "proxy_url": "/proxy?url=" + quote(ACTIVE_STREAM, safe=""),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })

@app.get("/api/status")
def api_status():
    return jsonify({
        "url": ACTIVE_STREAM,
        "live_mode": "direct",
        "http_client": "curl_cffi" if HTTP_IMPERSONATE else "requests",
        "stream_kind": "dash" if ".mpd" in ACTIVE_STREAM.lower() else "hls"
    })

@app.get("/proxy")
def proxy():
    target = request.args.get("url", "").strip()
    if not target:
        return jsonify({"error": "Missing URL"}), 400
    
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": REFERER,
        "Origin": "https://jjtvxweb.pages.dev"
    }
    kwargs = {"headers": headers, "stream": True, "timeout": (10, 30), "allow_redirects": True}
    if HTTP_IMPERSONATE:
        kwargs["impersonate"] = HTTP_IMPERSONATE

    try:
        resp = http_client.get(target, **kwargs)
    except Exception as exc:
        return jsonify({"error": f"Upstream failed: {exc}"}), 502

    def generate():
        try:
            for chunk in resp.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    out_headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-cache"
    }
    for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
        if name in resp.headers:
            out_headers[name] = resp.headers[name]

    return Response(generate(), status=resp.status_code, headers=out_headers)

@app.get("/playlist.m3u")
def playlist_m3u():
    if not key_ok():
        return Response("Forbidden", status=403)
    
    parsed_url = urlparse(ACTIVE_STREAM)
    query_string = parsed_url.query

    lines = [
        "#EXTM3U",
        '#EXTINF:-1 tvg-id="zeethirai" tvg-name="Zee Thirai HD" tvg-logo="https://jiotvimages.cdn.jio.com/dare_images/images/ZeeThiraiHD.png" group-title="Movies" http-user-agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36" http-referrer="https://jjtvxweb.pages.dev/" referrer="https://jjtvxweb.pages.dev/",Zee Thirai HD',
        '#KODIPROP:inputstream.adaptive.license_type=clearkey',
        '#KODIPROP:inputstream.adaptive.license_key={"58d0716748ee52f4a55cf7baa6e072bb":"8fc185a1015f026fec82236bb0e096f4","c2d762acbea256c0bd67b92d59f69757":"46c59697457af3a687426a4691393774"}',
        '#EXTVLCOPT:http-referrer=https://jjtvxweb.pages.dev/',
        '#EXTVLCOPT:http-origin=https://jjtvxweb.pages.dev',
        '#EXTVLCOPT:http-user-agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"',
    ]
    if query_string:
        lines.append(f'#EXTHTTP:{{"cookie":"{query_string}"}}')

    lines.append(ACTIVE_STREAM)
    return Response("\n".join(lines) + "\n",
                    content_type="audio/x-mpegurl; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})

@app.get("/live.m3u8")
def live_m3u8():
    if not key_ok():
        return Response("Forbidden", status=403)
    resp = redirect(ACTIVE_STREAM, code=302)
    resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp

@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
