"""
Advanced Stream server combining PHP speed with Capture.py intelligence.
Extracts fresh tokens on-the-fly using curl_cffi without headless browser delay.
"""

import os
import re
import json
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from pathlib import Path
from flask import Flask, Response, jsonify, request, send_from_directory
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

USER_AGENT = CFG.get("user_agent") or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
install_cors(app)

def log(message):
    print(f"[{datetime.now():%H:%M:%S}] [app] {message}", flush=True)

def origin_of(url):
    parsed = urlparse(url or "")
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""

# Target web page or fallback stream
PAGE_URL = CFG.get("page_url") or "https://jjtvxweb.pages.dev/pind?id=3051"
ACCESS_KEY = os.environ.get("ACCESS_KEY", "").strip()
CHANNEL_NAME = CFG.get("channel_name", "Zee Thirai HD")
CHANNEL_ID = CFG.get("channel_id", "zeethirai")
CHANNEL_REFERER = CFG.get("referer") or "https://jjtvxweb.pages.dev/"
CHANNEL_LOGO = "https://jiotvimages.cdn.jio.com/dare_images/images/ZeeThiraiHD.png"

def key_ok():
    return not ACCESS_KEY or request.args.get("key", "") == ACCESS_KEY

def fetch_fresh_stream():
    """Smart extraction like capture.py but instant via HTTP request"""
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": CHANNEL_REFERER,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    kwargs = {"headers": headers, "timeout": 10, "allow_redirects": True}
    if HTTP_IMPERSONATE:
        kwargs["impersonate"] = HTTP_IMPERSONATE

    try:
        resp = http_client.get(PAGE_URL, **kwargs)
        if resp.status_code == 200:
            # Search for mpd/m3u8 link with tokens in page source/scripts
            match = re.search(r'https?://[^\s<>"]+?\.(?:mpd|m3u8)[^\s<>"]*', resp.text)
            if match:
                stream_url = match.group(0)
                log(f"Successfully fetched fresh stream: {stream_url}")
                return stream_url
    except Exception as exc:
        log(f"Failed to fetch fresh stream: {exc}")
    
    # Fallback to default pattern if live fetch fails
    return "https://jiotvpllive.cdn.jio.com/bpk-tv/ZeeThiraiHD_BTS/WDVLive/index.mpd"

@app.post("/api/target")
def api_target():
    body = request.get_json(silent=True) or {}
    value = str(body.get("page_url") or body.get("stream_url") or "").strip()
    if not value:
        return jsonify({"error": "Paste a URL"}), 400
    global PAGE_URL
    PAGE_URL = value
    return jsonify({"ok": True, "url": value}), 200

@app.get("/api/stream")
def api_stream():
    url = fetch_fresh_stream()
    return jsonify({
        "url": url,
        "proxy_url": "/proxy?url=" + quote(url, safe=""),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })

@app.get("/api/status")
def api_status():
    url = fetch_fresh_stream()
    return jsonify({
        "url": url,
        "live_mode": "direct",
        "http_client": "curl_cffi" if HTTP_IMPERSONATE else "requests",
        "stream_kind": "dash" if ".mpd" in url.lower() else "hls"
    })

@app.get("/proxy")
def proxy():
    target = request.args.get("url", "").strip()
    if not target:
        return jsonify({"error": "Missing URL"}), 400
    
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": CHANNEL_REFERER,
        "Origin": origin_of(CHANNEL_REFERER)
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
    
    url = fetch_fresh_stream()
    parsed_url = urlparse(url)
    query_string = parsed_url.query

    lines = [
        "#EXTM3U",
        f'#EXTINF:-1 tvg-id="{CHANNEL_ID}" tvg-name="{CHANNEL_NAME}" tvg-logo="{CHANNEL_LOGO}" group-title="Tamil",{CHANNEL_NAME}',
        '#KODIPROP:inputstream.adaptive.license_type=clearkey',
        '#KODIPROP:inputstream.adaptive.license_key={"58d0716748ee52f4a55cf7baa6e072bb":"8fc185a1015f026fec82236bb0e096f4","c2d762acbea256c0bd67b92d59f69757":"46c59697457af3a687426a4691393774"}',
    ]
    if CHANNEL_REFERER:
        lines.append(f"#EXTVLCOPT:http-referrer={CHANNEL_REFERER}")
        lines.append(f"#EXTVLCOPT:http-origin={origin_of(CHANNEL_REFERER)}")
    lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
    
    if query_string:
        lines.append(f'#EXTHTTP:{{"cookie":"{query_string}"}}')

    lines.append(url)
    return Response("\n".join(lines) + "\n",
                    content_type="audio/x-mpegurl; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})

@app.get("/api/keys")
def api_keys():
    return jsonify({
        "keys": {
            "58d0716748ee52f4a55cf7baa6e072bb": "8fc185a1015f026fec82236bb0e096f4",
            "c2d762acbea256c0bd67b92d59f69757": "46c59697457af3a687426a4691393774"
        }
    })

@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
