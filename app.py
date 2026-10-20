"""
Stream Capture server (M3U edition).
  - GET  /playlist.m3u: IPTV playlist with one stable channel link + headers (#EXTVLCOPT)
  - GET  /live.m3u8   : current captured stream
                        LIVE_MODE=direct (default): 302 to the CDN link
                        LIVE_MODE=proxy           : 302 to /proxy (server fetches it)
  - POST /api/target  : a page URL (auto-capture) or a direct .m3u8 URL (manual play).
  - POST /api/refresh : a viewer's link died, reload the capture page.
  - GET  /api/stream  : current stream (raw url + proxy_url).
  - GET  /api/status  : state, capture stage, last error.
  - GET  /proxy       : same-origin proxy with Referer/Origin/UA headers.
  - GET  /            : player page.
  - GET  /player.js, /player.css, /player-config.json : player files.
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

# Prefer curl_cffi: it copies Chrome's TLS fingerprint, which many CDNs require.
try:
    from curl_cffi import requests as http_client
    HTTP_IMPERSONATE = "chrome"
except ImportError:
    http_client = _plain_requests
    HTTP_IMPERSONATE = None

BASE_DIR = Path(__file__).resolve().parent
CFG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

M3U8_RE = re.compile(r"\.m3u8(?:$|[?#])", re.IGNORECASE)

# Only these files are served from the root path. Everything else is 404.
PLAYER_FILES = {"player.js", "player-config.json", "player.css"}

# "direct": /live.m3u8 redirects to the CDN, so the viewer's own device fetches it.
# "proxy":  /live.m3u8 redirects to /proxy, so this server fetches it.
LIVE_MODE = os.environ.get("LIVE_MODE", "direct").strip().lower()
if LIVE_MODE not in ("direct", "proxy"):
    LIVE_MODE = "direct"

USER_AGENT = CFG.get("user_agent") or (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CHUNK_BYTES = 64 * 1024
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024

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
    """Blocks private, loopback and link-local addresses (SSRF protection)."""
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


def is_m3u8(value):
    return bool(M3U8_RE.search(value or ""))


def origin_of(url):
    parsed = urlparse(url or "")
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


# Initial state. If config.json holds a direct .m3u8 link, play it straight away.
_initial_page = CFG.get("page_url", "")
if is_m3u8(_initial_page):
    STATE = {"page_url": "", "manual_url": _initial_page, "url": _initial_page,
             "updated_at": now_iso(), "last_error": None, "cycles": 0}
else:
    STATE = {"page_url": _initial_page, "manual_url": None, "url": None,
             "updated_at": None, "last_error": None, "cycles": 0}


def origin_for(target):
    """Referer origin: the page being captured, or the stream's own host in manual mode."""
    with LOCK:
        page_url = STATE["page_url"] or ""
    return origin_of(page_url) or origin_of(target)


def upstream_get(target, headers, range_header=None):
    """Fetches from the CDN, Chrome-like when curl_cffi is available."""
    req_headers = dict(headers)
    if range_header:
        req_headers["Range"] = range_header
    kwargs = {"headers": req_headers, "stream": True,
              "timeout": (10, 30), "allow_redirects": True}
    if HTTP_IMPERSONATE:
        kwargs["impersonate"] = HTTP_IMPERSONATE
    else:
        kwargs["headers"]["User-Agent"] = USER_AGENT
    return http_client.get(target, **kwargs)


def read_playlist(resp):
    data = bytearray()
    for chunk in resp.iter_content(CHUNK_BYTES):
        data.extend(chunk)
        if len(data) > MAX_PLAYLIST_BYTES:
            raise ValueError("Playlist exceeds size limit")
    return data.decode("utf-8", errors="replace")


def rewrite_playlist(text, base_url):
    """Routes every URI in the playlist (segments, sub-playlists, keys) back through /proxy."""
    base_query = urlparse(base_url).query

    def wrap(uri):
        resolved = urljoin(base_url, uri)
        # Tokenized CDNs (e.g. ?__hdnea__=...) put the token in the playlist URL.
        # Relative segment URIs lose it when joined, so carry it over.
        parsed = urlparse(resolved)
        if base_query and not parsed.query:
            resolved = urlunparse(parsed._replace(query=base_query))
        return "/proxy?url=" + quote(resolved, safe="")

    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
        elif stripped.startswith("#"):
            lines.append(re.sub(r'URI="([^"]+)"', lambda m: f'URI="{wrap(m.group(1))}"', line))
        else:
            lines.append(wrap(stripped))
    return "\n".join(lines) + "\n"


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
                # Discard the result if the user changed the target during this cycle.
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
        return jsonify({"error": "Paste a page URL or a .m3u8 link"}), 400

    if is_m3u8(value):
        if not is_allowed_target(value):
            return jsonify({"error": "Enter a valid public http(s) .m3u8 URL"}), 400
        with LOCK:
            STATE.update(page_url="", manual_url=value, url=value,
                         updated_at=now_iso(), last_error=None)
        WAKE.set()
        log(f"Manual stream set: {value}")
        return jsonify({"ok": True, "mode": "manual"}), 202

    if not is_valid_page_url(value):
        return jsonify({"error": "Enter a full http(s) page URL or .m3u8 link"}), 400

    with LOCK:
        STATE.update(page_url=value, manual_url=None, url=None,
                     updated_at=None, last_error=None)
    WAKE.set()
    log(f"New page target: {value}")
    return jsonify({"ok": True, "mode": "capture"}), 202


@app.post("/api/refresh")
def api_refresh():
    """A viewer's link died. Reload the capture page so a fresh link is found."""
    request_reload()
    WAKE.set()
    log("Refresh requested by viewer.")
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
        log(f"Upstream request failed: {type(exc).__name__}: {exc}")
        return jsonify({"error": f"Upstream failed: {type(exc).__name__}: {exc}"}), 502

    status = upstream.status_code
    ctype = upstream.headers.get("Content-Type", "")
    log(f"Upstream {status} {ctype} <- {upstream.url[:120]}")

    if status >= 400:
        upstream.close()
        hint = "The CDN refused the request."
        if status == 451:
            hint = ("HTTP 451 = geo-blocked. This server's location is outside the "
                    "content's allowed region. Use LIVE_MODE=direct so the device fetches it.")
        return jsonify({"error": f"Upstream returned HTTP {status}", "hint": hint}), 502

    is_playlist = ("mpegurl" in ctype.lower()
                   or urlparse(upstream.url).path.lower().endswith(".m3u8"))

    if is_playlist:
        try:
            body = read_playlist(upstream)
        except ValueError as exc:
            upstream.close()
            return jsonify({"error": str(exc)}), 502
        upstream.close()

        if not body.lstrip().startswith("#EXTM3U"):
            return jsonify({
                "error": "Upstream did not return an HLS playlist",
                "first_bytes": body.lstrip()[:80],
            }), 502

        return Response(
            rewrite_playlist(body, upstream.url),
            status=200,
            content_type="application/vnd.apple.mpegurl",
            headers=cors_headers(),
        )

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


# ---------------------------------------------------------------------------
# M3U endpoints for IPTV apps (OTT Navigator etc.)
#   /playlist.m3u  -> one stable channel, with the headers the CDN expects
#   /live.m3u8     -> always the CURRENT captured stream (302), never an expired one
# ---------------------------------------------------------------------------
ACCESS_KEY = os.environ.get("ACCESS_KEY", "").strip()
CHANNEL_NAME = CFG.get("channel_name", "Live")
CHANNEL_ID = CFG.get("channel_id", "live1")
# Referer/Origin to send to the CDN. Defaults to the page the stream came from.
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


@app.get("/<name>")
def player_asset(name):
    """Serves only the player files listed in PLAYER_FILES. Anything else is 404."""
    if name not in PLAYER_FILES:
        return jsonify({"error": "not found"}), 404
    return send_from_directory(BASE_DIR / "static", name)


@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


threading.Thread(target=capture_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
