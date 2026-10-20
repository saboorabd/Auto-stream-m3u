"""
Warm-browser capture.

The browser stays open and keeps the page playing, so the player keeps requesting
its playlist. The newest .m3u8 request is always the current link, so the page
is NOT reloaded every cycle. It is reloaded only when needed:
  - first run, or the target page changed
  - a viewer reported a dead link (request_reload)
  - it has been loaded for reload_after_s (signed links expire)
  - no stream was seen for a whole capture_timeout_s
  - only EXPIRED tokens were seen (the player stopped asking for new ones)

Sources, priority order (first match wins, newest match is returned):
  1. Video Link Extractor Pro (getLinks)
  2. Network requests from every frame (checked every second)
  3. Store extension popups (every SLOW_SCAN_S seconds)
  4. Page source scan (every SLOW_SCAN_S seconds)
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

STAGE = {"name": "idle", "since": None, "error": None}

EXTENSIONS_ROOT = Path(os.environ.get("EXTENSIONS_ROOT", "/opt/extensions"))
VLE_NAME = "Video Link Extractor Pro"
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
SEEN_CAP = 500
POLL_S = 1.0
SLOW_SCAN_S = 10.0

REFRESH = threading.Event()
M3U8_RE = re.compile(r'https?:[^\s"\'<>]*?\.m3u8[^\s"\'<>]*', re.IGNORECASE)

PLAY_VIDEO_JS = """
() => {
  const v = document.querySelector('video');
  if (v) { v.muted = true; v.play().catch(() => {}); }
}
"""

COLLECT_LINKS_JS = """
async () => {
  const self = await chrome.tabs.getCurrent();
  const tabs = await chrome.tabs.query({});
  const out = [];
  for (const t of tabs) {
    if (!t.id || (self && t.id === self.id)) continue;
    const r = await new Promise((resolve) =>
      chrome.runtime.sendMessage({ action: 'getLinks', tabId: t.id }, (res) => resolve(res))
    );
    if (r && Array.isArray(r.links)) {
      for (const l of r.links) out.push({ url: l.url });
    }
  }
  return out;
}
"""

CLEAR_LINKS_JS = """
async () => {
  const self = await chrome.tabs.getCurrent();
  const tabs = await chrome.tabs.query({});
  for (const t of tabs) {
    if (!t.id || (self && t.id === self.id)) continue;
    await new Promise((resolve) =>
      chrome.runtime.sendMessage({ action: 'clearLinks', tabId: t.id }, () => resolve())
    );
  }
  return true;
}
"""

IS_EXTENSION_PAGE_JS = """
() => typeof chrome !== 'undefined' && !!chrome.runtime && !!chrome.runtime.id
"""

TOKEN_MARGIN_S = 60  # a token this close to expiry is treated as already expired


def token_expired(url, margin=TOKEN_MARGIN_S):
    """True if the URL carries an expiry time (exp=, or __hdnea__=...~exp=...) that has passed.
    Tokenized CDN links (Jio, Akamai style) stop working after that time."""
    if not url:
        return False
    parsed = urlparse(url)
    candidates = []
    for values in parse_qs(parsed.query).values():
        candidates.extend(values)
    for value in candidates:
        match = re.search(r"(?:^|[~;&=])exp=(\d{9,11})", value)
        if match:
            return int(match.group(1)) - margin <= time.time()
    return False


def request_reload():
    """Called when a viewer reports a dead link. The next cycle reloads the page."""
    REFRESH.set()


def set_stage(name):
    STAGE["name"] = name
    STAGE["since"] = datetime.now().isoformat(timespec="seconds")
    print(f"[{datetime.now():%H:%M:%S}] [capture] {name}", flush=True)


def _empty_session():
    return {
        "pw": None, "context": None, "page": None, "vle_popup": None, "scan_tab": None,
        "profile": None, "exts": [], "vle_ready": False, "seen": [],
        "loaded_url": None, "loaded_at": 0.0, "clicked": False,
        "store_hits": [], "source_hits": [], "last_slow": 0.0, "last_found": None,
    }


_session = _empty_session()


def ext_id_for_path(path):
    """Chrome's ID for an unpacked extension: first 32 hex chars of SHA-256(path), a-p alphabet."""
    digest = hashlib.sha256(os.path.abspath(path).encode("utf-8")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(c, 16)) for c in digest)


def discover_extensions():
    found = []
    if not EXTENSIONS_ROOT.is_dir():
        return found
    for d in sorted(EXTENSIONS_ROOT.iterdir()):
        manifest_path = d / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        action = data.get("action") or data.get("browser_action") or {}
        name = data.get("name", d.name)
        found.append({
            "dir": str(d),
            "name": name,
            "id": ext_id_for_path(str(d)),
            "popup": action.get("default_popup") or "popup.html",
            "is_vle": name == VLE_NAME,
        })
    return found


def resolve_ids(context, exts):
    """Replace computed IDs with the real ones, read from each live service worker."""
    for worker in context.service_workers:
        try:
            name = worker.evaluate("() => chrome.runtime.getManifest().name")
            real_id = worker.url.split("/")[2]
        except Exception:
            continue
        for ext in exts:
            if ext["name"] == name:
                ext["id"] = real_id


def verify_vle(popup, ext):
    try:
        popup.goto(f"chrome-extension://{ext['id']}/{ext['popup']}", timeout=15_000)
        return bool(popup.evaluate(IS_EXTENSION_PAGE_JS))
    except Exception as exc:
        print(f"[capture] video link extractor not reachable: {type(exc).__name__}", flush=True)
        return False


def safe_eval(page, script):
    try:
        return page.evaluate(script)
    except Exception:
        return None


def collect_vle(popup):
    result = safe_eval(popup, COLLECT_LINKS_JS) or []
    return [i["url"] for i in result if i.get("url")]


def scan_store_popups(scan_tab, exts):
    hits = []
    for ext in exts:
        if ext["is_vle"]:
            continue
        try:
            scan_tab.goto(f"chrome-extension://{ext['id']}/{ext['popup']}",
                          wait_until="domcontentloaded", timeout=4_000)
            text = scan_tab.evaluate(
                "() => document.documentElement.innerHTML + ' ' + document.body.innerText"
            )
            hits.extend(M3U8_RE.findall(text.replace("\\/", "/")))
        except Exception:
            continue
    return hits


def scan_page_sources(page):
    found = []
    for frame in page.frames:
        try:
            html = frame.content().replace("\\/", "/")
        except Exception:
            continue
        found.extend(M3U8_RE.findall(html))
    return found


def open_session():
    exts = discover_extensions()
    profile = tempfile.mkdtemp(prefix="vle-")
    pw = sync_playwright().start()
    try:
        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--autoplay-policy=no-user-gesture-required",
        ]
        if exts:
            paths = ",".join(e["dir"] for e in exts)
            args += [f"--disable-extensions-except={paths}", f"--load-extension={paths}"]

        kwargs = {
            "headless": False,  # Extensions need headful Chromium (Xvfb on Render).
            "ignore_default_args": ["--disable-extensions"],
            "args": args,
            "viewport": {"width": 1280, "height": 720},
        }
        if PROXY_URL:
            kwargs["proxy"] = {"server": PROXY_URL}

        context = pw.chromium.launch_persistent_context(profile, **kwargs)
        seen = []

        def on_request(request):
            if "m3u8" in request.url.lower():
                seen.append(request.url)
                if len(seen) > SEEN_CAP:
                    del seen[: SEEN_CAP // 2]

        context.on("request", on_request)

        page = context.new_page()
        vle_popup = context.new_page()
        scan_tab = context.new_page()

        vle_ready = False
        if exts:
            time.sleep(3)  # Let extension service workers start.
            resolve_ids(context, exts)
            vle = next((e for e in exts if e["is_vle"]), None)
            if vle:
                vle_ready = verify_vle(vle_popup, vle)
            print(f"[capture] extensions: {[e['name'] for e in exts]} | vle_ready={vle_ready}",
                  flush=True)
        else:
            print("[capture] no extensions found, using network and page-source sources only",
                  flush=True)
    except Exception:
        shutil.rmtree(profile, ignore_errors=True)
        pw.stop()
        raise

    _session.update(pw=pw, context=context, page=page, vle_popup=vle_popup,
                    scan_tab=scan_tab, profile=profile, exts=exts,
                    vle_ready=vle_ready, seen=seen)
    set_stage("chrome_started")


def close_session():
    try:
        if _session["context"]:
            _session["context"].close()
    except Exception:
        pass
    try:
        if _session["pw"]:
            _session["pw"].stop()
    except Exception:
        pass
    if _session["profile"]:
        shutil.rmtree(_session["profile"], ignore_errors=True)
    _session.clear()
    _session.update(_empty_session())


def click_play(page, selectors):
    for frame in page.frames:
        for selector in selectors:
            try:
                frame.click(selector, timeout=800)
                print(f"[capture] clicked '{selector}'", flush=True)
                return True
            except Exception:
                continue
    return False


def start_video(page):
    for frame in page.frames:
        try:
            frame.evaluate(PLAY_VIDEO_JS)
        except Exception:
            pass


def needs_load(cfg):
    if _session["loaded_url"] != cfg["page_url"]:
        return True
    if REFRESH.is_set():
        return True
    if time.monotonic() - _session["loaded_at"] > cfg.get("reload_after_s", 300):
        return True
    return False


def load_page(cfg):
    REFRESH.clear()
    if _session["vle_ready"]:
        safe_eval(_session["vle_popup"], CLEAR_LINKS_JS)
    _session["seen"].clear()
    _session["store_hits"] = []
    _session["source_hits"] = []
    _session["clicked"] = False
    _session["loaded_url"] = None

    set_stage("opening_page")
    try:
        _session["page"].goto(cfg["page_url"], wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:
        STAGE["error"] = f"page load: {type(exc).__name__}: {exc}"
        print(f"[capture] page load warning: {exc}", flush=True)

    _session["loaded_url"] = cfg["page_url"]
    _session["loaded_at"] = time.monotonic()
    set_stage("page_loaded")
    set_stage("waiting_for_stream")


def pick(pattern, *groups):
    """Returns the newest NON-EXPIRED match from the first group that has one.
    Also returns whether an expired match was seen (so the caller can reload)."""
    stale = False
    for group in groups:
        matches = [u for u in group if pattern.search(u)]
        fresh = [u for u in matches if not token_expired(u)]
        if len(fresh) < len(matches):
            stale = True
        if fresh:
            return fresh[-1], stale
    return None, stale


def capture_once(cfg):
    pattern = re.compile(cfg["stream_pattern"], re.IGNORECASE)
    STAGE["error"] = None

    try:
        if not _session["context"]:
            set_stage("launching_chrome")
            open_session()

        if needs_load(cfg):
            load_page(cfg)

        page = _session["page"]
        deadline = time.monotonic() + cfg["capture_timeout_s"]
        reloaded_for_token = False

        while True:
            if not _session["clicked"]:
                _session["clicked"] = click_play(page, cfg.get("play_selectors", []))
            start_video(page)

            vle_hits = collect_vle(_session["vle_popup"]) if _session["vle_ready"] else []
            found, stale = pick(pattern, vle_hits, list(_session["seen"]))

            if not found and time.monotonic() - _session["last_slow"] >= SLOW_SCAN_S:
                _session["last_slow"] = time.monotonic()
                _session["store_hits"] = scan_store_popups(_session["scan_tab"], _session["exts"])
                _session["source_hits"] = scan_page_sources(page)
                found, stale_slow = pick(pattern, _session["store_hits"], _session["source_hits"])
                stale = stale or stale_slow

            # Only expired links were seen: the player has stopped asking for fresh ones.
            # Reload the page right away so it requests a new token.
            if not found and stale and not reloaded_for_token:
                reloaded_for_token = True
                print("[capture] only expired token seen, reloading page", flush=True)
                load_page(cfg)
                deadline = max(deadline, time.monotonic() + cfg["capture_timeout_s"])
                continue

            if found:
                if found != _session["last_found"]:
                    print(f"[capture] Found stream: {found}", flush=True)
                    _session["last_found"] = found
                set_stage("stream_found")
                return found

            if time.monotonic() >= deadline:
                break
            page.wait_for_timeout(int(POLL_S * 1000))

        # Nothing found: reload the page on the next cycle.
        _session["loaded_url"] = None
        try:
            title = page.title()
        except Exception:
            title = ""
        diag = {
            "diagnosis": "No fresh (non-expired) stream from any source. Check play_selectors or page_url.",
            "page_title": title,
            "frames": len(page.frames),
            "clicked_play": _session["clicked"],
            "vle_ready": _session["vle_ready"],
            "network_m3u8_seen": len(_session["seen"]),
            "page_source_m3u8": len(_session["source_hits"]),
            "store_popup_m3u8": len(_session["store_hits"]),
            "extensions_loaded": [e["name"] for e in _session["exts"]],
        }
        STAGE["error"] = json.dumps(diag, ensure_ascii=False)
        print(f"[capture] DIAGNOSIS: {STAGE['error']}", flush=True)
        set_stage("timeout_no_stream")
        return None

    except Exception as exc:
        STAGE["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[capture] failed, resetting browser: {STAGE['error']}", flush=True)
        close_session()
        raise
