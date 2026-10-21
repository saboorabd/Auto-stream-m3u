"""
Stream Capture server.
  - Capture thread finds the current stream link from the Jio page (capture.py).
  - /live.m3u8 and /playlist.m3u give OTT Navigator a stable channel link.
  - /api/status, /api/stream, /api/refresh are used by keeper.sh and the web page.
No fallback link is ever served. If capture has no fresh link, the server says so.
"""

import json
import os
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, request, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix

from capture import STAGE, capture_once, request_reload, token_expired
from cors import install_cors

BASE_DIR = Path(__file__).resolve().parent
CFG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

ACCESS_KEY = os.environ.get("ACCESS_KEY", "").strip()
LIVE_WAIT_S = int(os.environ.get("LIVE_WAIT_S", "25"))  # keep under the player's timeout
CHANNEL_NAME = CFG.get("channel_name", "Live")
CHANNEL_ID = CFG.get("channel_id", "live1")
CHANNEL_LOGO = CFG.get("channel_logo", "")
CHANNEL_GROUP = CFG.get("channel_group", "Live")
USER_AGENT = CFG.get("user_agent", "")
REFERER = CFG.get("referer", "")

LOCK = threading.Lock()
WAKE = threading.Event()

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
install_cors(app)

STATE = {
    "page_url": CFG.get("page_url", ""),
    "manual_url": None,
    "url": None,
    "updated_at": None,
    "last_error": None,
    "cycles": 0,
}


def log(message):
    print(f"[{datetime.now():%H:%M:%S}] [app] {message}", flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key_ok():
    return not ACCESS_KEY or request.args.get("key", "") == ACCESS_KEY


def fresh_url():
    with LOCK:
        url = STATE["url"]
    return url if url and not token_expired(url) else None


def capture_loop():
    log("Capture loop started.")
    while True:
        with LOCK:
            page_url = STATE["page_url"]
            manual = STATE["manual_url"]
            STATE["cycles"] += 1
            cycle = STATE["cycles"]

        if manual or not page_url:
            WAKE.wait(CFG.get("refresh_interval_s", 5))
            WAKE.clear()
            continue

        log(f"Cycle {cycle} on {page_url}")
        try:
            url = capture_once({**CFG, "page_url": page_url})
            with LOCK:
                if STATE["page_url"] == page_url and not STATE["manual_url"]:
                    if url and not token_expired(url):
                        if url != STATE["url"]:
                            STATE["url"] = url
                            STATE["updated_at"] = now_iso()
                        STATE["last_error"] = None
                    else:
                        STATE["last_error"] = STAGE.get("error") or "No fresh stream found"
        except Exception as exc:
            log(f"Cycle {cycle} failed:\n{traceback.format_exc()}")
            with LOCK:
                STATE["last_error"] = f"{type(exc).__name__}: {exc}"

        with LOCK:
            if STATE["url"] and token_expired(STATE["url"]):
                log("Stored link expired, clearing it.")
                STATE["url"] = None
                STATE["last_error"] = "Stream token expired, waiting for a fresh one"
        WAKE.wait(CFG.get("refresh_interval_s", 5))
        WAKE.clear()


@app.get("/api/status")
def api_status():
    with LOCK:
        data = dict(STATE)
    data["stage"] = dict(STAGE)
    data["live_mode"] = "direct"
    url = data.get("url") or ""
    data["stream_kind"] = "dash" if ".mpd" in url.lower() else ("hls" if ".m3u8" in url.lower() else None)
    return jsonify(data)


@app.get("/api/stream")
def api_stream():
    url = fresh_url()
    if not url:
        with LOCK:
            err = STATE["last_error"]
        return jsonify({"error": "No fresh stream captured yet", "last_error": err}), 404
    return jsonify({"url": url, "updated_at": STATE["updated_at"]})


@app.post("/api/refresh")
def api_refresh():
    request_reload()
    WAKE.set()
    log("Refresh requested.")
    return jsonify({"ok": True}), 202


@app.get("/playlist.m3u")
def playlist_m3u():
    if not key_ok():
        return Response("Forbidden", status=403)
    base = request.host_url.rstrip("/")
    key_q = f"?key={ACCESS_KEY}" if ACCESS_KEY else ""
    logo = f' tvg-logo="{CHANNEL_LOGO}"' if CHANNEL_LOGO else ""
    lines = [
        "#EXTM3U",
        f'#EXTINF:-1 tvg-id="{CHANNEL_ID}" tvg-name="{CHANNEL_NAME}"{logo} group-title="{CHANNEL_GROUP}",{CHANNEL_NAME}',
    ]
    if REFERER:
        lines.append(f"#EXTVLCOPT:http-referrer={REFERER}/")
        lines.append(f"#EXTVLCOPT:http-origin={REFERER}")
    if USER_AGENT:
        lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}")
    lines.append(f"{base}/live.m3u8{key_q}")
    return Response("\n".join(lines) + "\n", content_type="audio/x-mpegurl; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


@app.get("/live.m3u8")
def live_m3u8():
    if not key_ok():
        return Response("Forbidden", status=403)
    url = fresh_url()
    if not url:
        request_reload()
        WAKE.set()
        deadline = time.monotonic() + LIVE_WAIT_S
        while time.monotonic() < deadline and not url:
            time.sleep(1)
            url = fresh_url()
    if not url:
        return Response("Stream is starting, retry in about 10 seconds.",
                        status=503, headers={"Retry-After": "10"})
    resp = redirect(url, code=302)
    resp.headers["Cache-Control"] = "no-cache, no-store"
    return resp


@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


threading.Thread(target=capture_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
