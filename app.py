"""
Stream Capture server (M3U edition with DASH & DRM support).
"""

import ipaddress
import json
import os
import re
import socket
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse, urlunparse

import requests as _plain_requests
from flask import Flask, Response, jsonify, redirect, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from capture import STAGE, capture_once, request_reload, token_expired
from cors import install_cors

try:
    from curl_cffi import requests as http_client
    HTTP_IMPERSONATE = "chrome"
except ImportError:
    http_client = _plain_requests
    HTTP_IMPERSONATE = None

BASE_DIR = Path(__file__).resolve().parent
CFG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

STREAM_RE = re.compile(r"\.(?:m3u8|mpd)(?:$|[?#])", re.IGNORECASE)
PLAYER_FILES = {"player.js", "player-config.json", "player.css"}

LIVE_MODE = os.environ.get("LIVE_MODE", "direct").strip().lower()
if LIVE_MODE not in ("direct", "proxy"):
    LIVE_MODE = "direct"

USER_AGENT = CFG.get("user_agent") or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CHUNK_BYTES = 64 * 1024
MAX_PLAYLIST_BYTES = 4 * 1024 * 1024

LOCK = threading.Lock()
WAKE = threading.Event()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
install_cors(app)

def log(message):
    print(f"[{datetime.now():%H:%M:%S}] [app] {message}", flush=True)

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def is_public_host(hostname):
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return False
    return True

def is_allowed_target(value):
    parsed = urlparse(value)
    return (
        parsed.scheme in ("http", "https")
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and is_public_host(parsed.hostname)
    )

def is_valid_page_url(value):
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)

def is_stream_url(value):
    return bool(STREAM_RE.search(value or ""))

def origin_of(url):
    parsed = urlparse(url or "")
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""

_initial_page = CFG.get("page_url", "")
if is_stream_url(_initial_page):
    STATE = {"page_url": "", "manual_url": _initial_page, "url": _initial_page,
             "updated_at": now_iso(), "last_error": None, "cycles": 0}
else:
    STATE = {"page_url": _initial_page, "manual_url": None, "url": None,
             "updated_at": None, "last_error": None, "cycles": 0}

def origin_for(target):
    with LOCK:
        page_url = STATE["page_url"] or ""
    return origin_of(page_url) or origin_of(target)

def upstream_get(target, headers, range_header=None):
    req_headers = dict(headers)
    req_headers["User-Agent"] = USER_AGENT
    if range_header:
        req_headers["Range"] = range_header
    kwargs = {"headers": req_headers, "stream": True,
              "timeout": (10, 30), "allow_redirects": True}
    if HTTP_IMPERSONATE:
        kwargs["impersonate"] = HTTP_IMPERSONATE
    return http_client.get(target, **kwargs)

def read_body(resp):
    data = bytearray()
    for chunk in resp.iter_content(CHUNK_BYTES):
        data.extend(chunk)
        if len(data) > MAX_PLAYLIST_BYTES:
            raise ValueError("Manifest exceeds size limit")
    return data.decode("utf-8", errors="replace")

def rewrite_mpd(text, base_url):
    """DASH: rewrites manifest, keeps tokens on segments, and exposes ClearKey/Widevine mappings if needed."""
    base_query = urlparse(base_url).query
    base = base_url
    m = re.search(r"<BaseURL[^>]*>\s*([^<\s]+)\s*</BaseURL>", text)
    if m:
        base = urljoin(base_url, m.group(1).strip())
    text = re.sub(r"<BaseURL[^>]*>[^<]*</BaseURL>", "", text)

    def wrap(value):
        resolved = urljoin(base, value)
        parsed = urlparse(resolved)
        if base_query and not parsed.query:
            resolved = urlunparse(parsed._replace(query=base_query))
        return "/proxy?url=" + quote(resolved, safe="$/:@!*~,;+")

    def repl(match):
        return f'{match.group(1)}="{wrap(match.group(2))}"'

    return re.sub(r'\b(media|initialization|sourceURL)="([^"]*)"', repl, text)

def cors_headers():
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Length, Content-Range, Accept-Ranges",
        "Cache-Control": "no-cache",
    }

def capture_loop():
    log("Capture loop started.")
    while True:
        with LOCK:
            manual = STATE["manual_url"]
            page_url = STATE["page_url"]
            STATE["cycles"] += 1
            cycle = STATE["cycles"]

        if manual or not page_url:
            WAKE.wait(CFG["refresh_interval_s"])
            WAKE.clear()
            continue

        log(f"Cycle {cycle} on {page_url}")
        try:
            url = capture_once({**CFG, "page_url": page_url})
            with LOCK:
                if STATE["page_url"] != page_url or STATE["manual_url"]:
                    pass
                elif url and not token_expired(url):
                    if url != STATE["url"]:
                        STATE["url"] = url
                        STATE["updated_at"] = now_iso()
                    STATE["last_error"] = None
                else:
                    STATE["last_error"] = STAGE.get("error") or "No fresh stream found within timeout"
        except Exception as exc:
            log(f"Cycle {cycle} failed:\n{traceback.format_exc()}")
            with LOCK:
                STATE["last_error"] = f"{type(exc).__name__}: {exc}"

        with LOCK:
            if STATE["url"] and token_expired(STATE["url"]) and not STATE["manual_url"]:
                log("Stored stream token expired, clearing it.")
                STATE["url"] = None
                STATE["last_error"] = "Stream token expired, waiting for a fresh one"
        WAKE.wait(CFG["refresh_interval_s"])
        WAKE.clear()

@app.post("/api/target")
def api_target():
    body = request.get_json(silent=True) or {}
    value = str(body.get("page_url") or body.get("stream_url") or "").strip()
    if not value:
        return jsonify({"error": "Paste a page URL or a stream link"}), 400

    if is_stream_url(value):
        if not is_allowed_target(value):
            return jsonify({"error": "Enter a valid public http(s) stream URL"}), 400
        with LOCK:
            STATE.update(page_url="", manual_url=value, url=value,
                         updated_at=now_iso(), last_error=None)
        WAKE.set()
        return jsonify({"ok": True, "mode": "manual"}), 202

    if not is_valid_page_url(value):
        return jsonify({"error": "Enter a full http(s) page URL or stream link"}), 400

    with LOCK:
        STATE.update(page_url=value, manual_url=None, url=None,
                     updated_at=None, last_error=None)
    WAKE.set()
    return jsonify({"ok": True, "mode": "capture"}), 202

@app.post("/api/refresh")
def api_refresh():
    request_reload()
    WAKE.set()
    return jsonify({"ok": True}), 202

@app.get("/api/stream")
def api_stream():
    with LOCK:
        url = STATE["url"]
        updated_at = STATE["updated_at"]
        last_error = STATE["last_error"]
    if url and token_expired(url):
        url = None

    if not url:
        return jsonify({"error": "No fresh stream captured yet", "last_error": last_error}), 404
    return jsonify({
        "url": url,
        "proxy_url": "/proxy?url=" + quote(url, safe=""),
        "updated_at": updated_at,
    })

@app.get("/api/status")
def api_status():
    with LOCK:
        data = dict(STATE)
    data["stage"] = dict(STAGE)
    data["http_client"] = "curl_cffi" if HTTP_IMPERSONATE else "requests"
    data["live_mode"] = LIVE_MODE
    url = data.get("url") or ""
    data["stream_kind"] = "dash" if ".mpd" in url.lower() else ("hls" if ".m3u8" in url.lower() else None)
    return jsonify(data)

@app.get("/proxy")
def proxy():
    target = request.args.get("url", "").strip()
    if not is_allowed_target(target):
        return jsonify({"error": "Invalid or disallowed URL"}), 400

    origin = origin_for(target)
    headers = {
        "Accept": "*/*",
        "Referer": origin + "/",
        "Origin": origin,
    }

    try:
        upstream = upstream_get(target, headers, request.headers.get("Range"))
    except Exception as exc:
        return jsonify({"error": f"Upstream failed: {type(exc).__name__}: {exc}"}), 502

    status = upstream.status_code
    ctype = upstream.headers.get("Content-Type", "")

    if status >= 400:
        upstream.close()
        return jsonify({"error": f"Upstream returned HTTP {status}"}), 502

    path_low = urlparse(upstream.url).path.lower()
    is_dash = ("dash+xml" in ctype.lower() or path_low.endswith(".mpd"))

    if is_dash:
        try:
            body = read_body(upstream)
        except ValueError as exc:
            upstream.close()
            return jsonify({"error": str(exc)}), 502
        upstream.close()
        return Response(rewrite_mpd(body, upstream.url), status=200,
                        content_type="application/dash+xml", headers=cors_headers())

    def generate():
        try:
            for chunk in upstream.iter_content(CHUNK_BYTES):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    out_headers = cors_headers()
    for name in ("Content-Range", "Accept-Ranges", "Content-Length", "Last-Modified", "ETag"):
        if name in upstream.headers:
            out_headers[name] = upstream.headers[name]

    return Response(
        generate(),
        status=status,
        content_type=ctype or "application/octet-stream",
        headers=out_headers,
    )

ACCESS_KEY = os.environ.get("ACCESS_KEY", "").strip()
CHANNEL_NAME = CFG.get("channel_name", "Live")
CHANNEL_ID = CFG.get("channel_id", "live1")
CHANNEL_REFERER = CFG.get("referer") or origin_of(CFG.get("page_url", ""))

def key_ok():
    return not ACCESS_KEY or request.args.get("key", "") == ACCESS_KEY

@app.get("/playlist.m3u")
def playlist_m3u():
    if not key_ok():
        return Response("Forbidden", status=403)
    base = request.host_url.rstrip("/")
    key_q = f"?key={ACCESS_KEY}" if ACCESS_KEY else ""
    lines = [
        "#EXTM3U",
        f'#EXTINF:-1 tvg-id="{CHANNEL_ID}" tvg-name="{CHANNEL_NAME}",{CHANNEL_NAME}',
    ]
    if CHANNEL_REFERER:
        lines.append(f"#EXTVLCOPT:http-referrer={CHANNEL_REFERER}/")
        lines.append(f"#EXTVLCOPT:http-origin={CHANNEL_REFERER}")
    lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
    lines.append(f"{base}/live.m3u8{key_q}")
    return Response("\n".join(lines) + "\n",
                    content_type="audio/x-mpegurl; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})

@app.get("/live.m3u8")
def live_m3u8():
    if not key_ok():
        return Response("Forbidden", status=403)
    with LOCK:
        url = STATE["url"]
    if url and token_expired(url):
        url = None
    if not url:
        request_reload()
        WAKE.set()
        return Response("Stream token expired, fetching a fresh one. Retry in ~1 minute.",
                        status=503, headers={"Retry-After": "30"})
    if LIVE_MODE == "direct":
        target = url
    else:
        target = "/proxy?url=" + quote(url, safe="")
    resp = redirect(target, code=302)
    resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp

@app.get("/api/keys")
def api_keys():
    """Returns decrypted keys configuration for custom players supporting Clearkey/Widevine override."""
    return jsonify({
        "keys": {
            "58d0716748ee52f4a55cf7baa6e072bb": "8fc185a1015f026fec82236bb0e096f4",
            "c2d762acbea256c0bd67b92d59f69757": "46c59697457af3a687426a4691393774"
        }
    })

@app.get("/api/probe")
def api_probe():
    if not key_ok():
        return Response("Forbidden", status=403)
    with LOCK:
        url = STATE["url"]
    if not url or token_expired(url):
        return jsonify({"ok": False, "reason": "no fresh stream yet"}), 404
    origin = origin_for(url)
    try:
        r = upstream_get(url, {"Accept": "*/*", "Referer": origin + "/", "Origin": origin})
        ok = r.status_code < 400
        text = r.text[:200000] if ok else ""
        info = {
            "ok": ok,
            "status": r.status_code,
            "content_type": r.headers.get("Content-Type", ""),
            "has_content_protection": ("ContentProtection" in text) if ok else None,
            "first_bytes": text[:120],
        }
        r.close()
        return jsonify(info), (200 if ok else 502)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502

@app.get("/<name>")
def player_asset(name):
    if name not in PLAYER_FILES:
        return jsonify({"error": "not found"}), 404
    return send_from_directory(BASE_DIR / "static", name)

@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")

threading.Thread(target=capture_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
