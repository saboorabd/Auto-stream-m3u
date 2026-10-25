"""
Warm-browser capture for the ZeeThirai stream.

The browser stays open and keeps the page playing. The newest FRESH ZeeThirai
manifest request (.mpd or .m3u8) is returned. Expired tokens are never returned.
The page is reloaded only when:
  - it is first opened, or the target page changed
  - a viewer asked for a refresh (request_reload)
  - it has been loaded for reload_after_s
  - only expired tokens were seen for STALE_GRACE_S seconds (reloaded once)
  - nothing fresh was found within capture_timeout_s
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
STALE_GRACE_S = 45.0
TOKEN_MARGIN_S = 60

REFRESH = threading.Event()
M3U8_RE = re.compile(r'https?:[^\s"\'<>]*?\.(?:m3u8|mpd)[^\s"\'<>]*', re.IGNORECASE)
STREAM_HINTS = (".m3u8", ".mpd")

CLICK_PLAY_JS = """
(selectors) => {
  for (const el of document.querySelectorAll('div, iframe')) {
    const s = getComputedStyle(el);
    if (s.position === 'fixed' && parseFloat(s.opacity) < 0.1
        && el.offsetWidth > 200 && el.offsetHeight > 200) {
      el.remove();
    }
  }
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) { el.click(); return sel; }
  }
  return null;
}
"""

PLAYER_STATE_JS = """
() => {
  const btn = document.querySelector('.shaka-play-button');
  const v = document.querySelector('video');
  return {
    play_button_label: btn ? btn.getAttribute('aria-label') : null,
    video_paused: v ? v.paused : null,
    video_ready_state: v ? v.readyState : null,
    video_src_kind: v ? (v.currentSrc || v.src || '').slice(0, 5) : null
  };
}
"""

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


def token_expired(url, margin=TOKEN_MARGIN_S):
    """True if the URL's exp= time (e.g. __hdnea__=...~exp=...) has passed."""
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
    except Exception:
        return False


def safe_eval(page, script, arg=None):
    try:
        if arg is None:
            return page.evaluate(script)
        return page.evaluate(script, arg)
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


def player_state(page):
    """Reads the player state from every frame, for diagnosis when nothing plays."""
    states = []
    for frame in page.frames:
        state = safe_eval(frame, PLAYER_STATE_JS)
        if state:
            states.append(state)
    return states


def open_session(cfg):
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
            "headless": False,
            "ignore_default_args": ["--disable-extensions"],
            "args": args,
            "viewport": {"width": 1280, "height": 720},
        }
        if cfg.get("user_agent"):
            kwargs["user_agent"] = cfg["user_agent"]
        if PROXY_URL:
            kwargs["proxy"] = {"server": PROXY_URL}

        context = pw.chromium.launch_persistent_context(profile, **kwargs)

        seen = []

        def on_request(request):
            low = request.url.lower()
            if any(h in low for h in STREAM_HINTS) and "zeethirai" in low:
                seen.append(request.url)
                if len(seen) > SEEN_CAP:
                    del seen[: SEEN_CAP // 2]

        context.on("request", on_request)

        page = context.new_page()
        vle_popup = context.new_page()
        scan_tab = context.new_page()

        vle_ready = False
        if exts:
            time.sleep(3)
            resolve_ids(context, exts)
            vle = next((e for e in exts if e["is_vle"]), None)
            if vle:
                vle_ready = verify_vle(vle_popup, vle)
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
    if not selectors:
        return False
    for frame in page.frames:
        result = safe_eval(frame, CLICK_PLAY_JS, selectors)
        if result:
            print(f"[capture] JS-clicked '{result}'", flush=True)
            return True
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
        safe_eval(frame, PLAY_VIDEO_JS)


def needs_load(cfg):
    if _session["loaded_url"] != cfg["page_url"]:
        return True
    if REFRESH.is_set():
        return True
    if time.monotonic() - _session["loaded_at"] > cfg.get("reload_after_s", 1800):
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

    _session["loaded_url"] = cfg["page_url"]
    _session["loaded_at"] = time.monotonic()
    set_stage("page_loaded")
    set_stage("waiting_for_stream")


def pick(pattern, *groups):
    """Newest FRESH matching link from the first group that has one.
    Also reports whether any expired match was seen."""
    stale = False
    for group in groups:
        matches = [u for u in group if pattern.search(u) and "zeethirai" in u.lower()]
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
            open_session(cfg)

        if needs_load(cfg):
            load_page(cfg)

        page = _session["page"]
        deadline = time.monotonic() + cfg["capture_timeout_s"]
        stale_since = None
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
                found, _ = pick(pattern, _session["store_hits"], _session["source_hits"])

            if found:
                if found != _session["last_found"]:
                    print(f"[capture] Found ZeeThirai Stream: {found}", flush=True)
                    _session["last_found"] = found
                set_stage("stream_found")
                return found

            # Only expired links seen: wait the grace period, then reload once.
            if stale:
                if stale_since is None:
                    stale_since = time.monotonic()
                    print("[capture] expired ZeeThirai token seen, waiting for a fresh one", flush=True)
                elif not reloaded_for_token and time.monotonic() - stale_since > STALE_GRACE_S:
                    reloaded_for_token = True
                    stale_since = None
                    print("[capture] still only expired token, reloading page once", flush=True)
                    load_page(cfg)
                    deadline = max(deadline, time.monotonic() + cfg["capture_timeout_s"])
                    continue

            if time.monotonic() >= deadline:
                break
            page.wait_for_timeout(int(POLL_S * 1000))

        # Nothing fresh within the timeout. Record what the player looked like.
        _session["loaded_url"] = None
        diag = {
            "diagnosis": "No fresh stream found. See 'player' and 'network_stream_seen'.",
            "clicked_play": _session["clicked"],
            "vle_ready": _session["vle_ready"],
            "frames": len(page.frames),
            "network_stream_seen": len(_session["seen"]),
            "page_source_hits": len(_session["source_hits"]),
            "store_popup_hits": len(_session["store_hits"]),
            "player": player_state(page),
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
