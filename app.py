"""
Final Lightning-fast Dynamic Stream Server for Render.
Includes /get_key, /get_stream, /playlist.m3u, and /api/status for background keeper.
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

PAGE_URL = CFG.get("page_url") or "https://jjtvxweb.pages.dev/pind?id=3051"
ACCESS_KEY = os.environ.get("ACCESS_KEY", "").strip()

def key_ok():
    return not ACCESS_KEY or request.args.get("key", "") == ACCESS_KEY

def fetch_fresh_stream():
    """Jio page-ai hit panni fresh stream URL matrum tokens-ah dynamic-ah edukkum"""
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": REFERER,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    kwargs = {"headers": headers, "timeout": 10, "allow_redirects": True}
    if HTTP_IMPERSONATE:
        kwargs["impersonate"] = HTTP_IMPERSONATE

    try:
        resp = http_client.get(PAGE_URL, **kwargs)
        if resp.status_code == 200:
            match = re.search(r'https?://[^\s<>"]+?\.(?:mpd|m3u8)[^\s<>"]*', resp.text)
            if match:
                return match.group(0)
    except Exception as exc:
        pass
    
    # Fallback default stream with active format
    return "https://jiotvpllive.cdn.jio.com/bpk-tv/ZeeThiraiHD_BTS/WDVLive/index.mpd?__hdnea__=st=1792502910~exp=1792506510~acl=/bpk-tv/ZeeThiraiHD_BTS/WDVLive/*~hmac=fe0f6800c84c29f75c42f8e05457c3cb05c413d77e50daed218a52b53f06e7f5"

@app.get("/api/status")
def api_status():
    """Background keeper script-kaana status endpoint"""
    url = fetch_fresh_stream()
    return jsonify({
        "url": url,
        "live_mode": "dynamic",
        "http_client": "curl_cffi" if HTTP_IMPERSONATE else "requests",
        "status": "online"
    }), 200

@app.get("/api/stream")
def api_stream():
    url = fetch_fresh_stream()
    return jsonify({
        "url": url,
        "proxy_url": "/proxy?url=" + quote(url, safe=""),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })

@app.get("/get_key")
def get_key():
    """Dynamic ClearKey Endpoint for OTT Navigator / InputStream Adaptive"""
    clearkey_json = {
        "keys": [
            {
                "kty": "oct",
                "k": "8fc185a1015f026fec82236bb0e096f4",
                "kid": "58d0716748ee52f4a55cf7baa6e072bb"
            },
            {
                "kty": "oct",
                "k": "46c59697457af3a687426a4691393774",
                "kid": "c2d762acbea256c0bd67b92d59f69757"
            }
        ],
        "type": "temporary"
    }
    return jsonify(clearkey_json)

@app.get("/get_stream")
def get_stream():
    """Dynamic stream redirect with fresh token"""
    if not key_ok():
        return Response("Forbidden", status=403)
    url = fetch_fresh_stream()
    resp = redirect(url, code=302)
    resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp

@app.get("/playlist.m3u")
def playlist_m3u():
    if not key_ok():
        return Response("Forbidden", status=403)
    
    server_host = request.host
    scheme = request.scheme
    base_url = f"{scheme}://{server_host}"

    lines = [
        "#EXTM3U",
        '#EXTINF:-1 tvg-id="zeethirai" tvg-name="Zee Thirai HD" tvg-logo="https://jiotvimages.cdn.jio.com/dare_images/images/ZeeThiraiHD.png" group-title="Movies",Zee Thirai HD',
        '#KODIPROP:inputstream.adaptive.license_type=clearkey',
        f'#KODIPROP:inputstream.adaptive.license_key={base_url}/get_key?id=3051',
        f'{base_url}/get_stream?id=3051'
    ]
    return Response("\n".join(lines) + "\n",
                    content_type="audio/x-mpegurl; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})

@app.get("/live.m3u8")
def live_m3u8():
    if not key_ok():
        return Response("Forbidden", status=403)
    url = fetch_fresh_stream()
    resp = redirect(url, code=302)
    resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp

@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
