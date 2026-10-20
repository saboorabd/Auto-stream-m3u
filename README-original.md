# Stream Capture

A small web app that finds a video stream (`.m3u8`) on a page and plays it in your browser.

Paste a page URL, the server opens it in a real Chromium with the
[video_link_extractor](https://github.com/jrcramos/video_link_extractor) extension loaded,
reads the stream link the extension sniffs, and plays it in the player. You can also paste a
`.m3u8` link directly.

## Features

- **Auto-capture:** give a page URL, the server finds the stream for you.
- **Direct mode:** paste a `.m3u8` link and play it immediately.
- **CORS-safe playback:** streams go through a built-in proxy that adds the needed headers and rewrites HLS playlists.
- **Fast refresh:** one browser stays open; each cycle only reloads the page.
- **Diagnostics:** `/api/status` shows what the server is doing and why a capture failed.
- **Private:** every capture cycle uses a fresh, temporary browser profile.

## Architecture

```
Browser (your phone or PC)
   │  paste page URL or .m3u8
   ▼
Flask server (app.py)  ──►  Chromium + extension (capture.py)
   │                              │  finds .m3u8
   ▼◄─────────────────────────────┘
/proxy  ──►  HLS player (hls.js)
```

| File | Purpose |
|---|---|
| `app.py` | Flask server: API, capture loop, CORS proxy |
| `capture.py` | Runs Chromium with the extension and reads the sniffed links |
| `static/index.html` | Player and input form |
| `config.json` | Default page URL, selectors, timeouts |
| `Dockerfile` | Builds the image, downloads the extension at build time, runs a virtual display |
| `requirements.txt` | Python dependencies |

## Quick start (Render)

1. Fork or upload this repo to your GitHub.
2. In [Render](https://render.com), create a **New → Web Service**, connect the repo, and choose **Docker** as the runtime.
3. Use at least the **Starter** plan. Headful Chromium needs more memory than the free tier.
4. Deploy. Open the service URL when the build finishes.

## Usage

### Option 1: capture from a page

1. Paste a page URL into **Option 1** and click **Capture**.
2. Wait about a minute on the first run. Later refreshes are faster.
3. The player starts when a stream is found.

### Option 2: play a `.m3u8` directly

1. Paste a public `.m3u8` URL into **Option 2** and click **Play**.

Use this to check that playback works before you rely on capture.

### Default page

Set `page_url` in `config.json` to start capturing automatically after deploy.

```json
{
  "page_url": "https://example.com/your-stream-page",
  "play_selectors": ["button.play", "video"],
  "stream_pattern": "\\.m3u8(?:$|[?#])",
  "capture_timeout_s": 60,
  "refresh_interval_s": 30,
  "headless": false
}
```

| Key | Meaning |
|---|---|
| `page_url` | Page to open for capture |
| `play_selectors` | Play buttons to try, in every frame. Add site-specific selectors here |
| `stream_pattern` | Regex matched against request URLs |
| `capture_timeout_s` | Max seconds to wait for a stream per cycle |
| `refresh_interval_s` | Seconds between capture cycles |
| `headless` | Ignored. Extensions need a visible browser, which runs on a virtual display |

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/target` | Body `{"page_url": "..."}` starts capture. Body `{"stream_url": "..."}` plays a direct link |
| `GET` | `/api/stream` | Current stream: `url` and `proxy_url`. `404` until one is found |
| `GET` | `/api/status` | Cycle count, last error, capture stage, and diagnosis |
| `GET` | `/proxy?url=...` | CORS-safe proxy for streams and segments |

## Troubleshooting

Open `/api/status` first. `stage.name` shows the current step, and `stage.error` explains the last failure.

| Symptom | Likely cause | Fix |
|---|---|---|
| `No open ports detected` on deploy | Server did not start | Check the logs for a Python traceback |
| `stage.name` stays on `waiting_for_stream` | Player did not start | Add the site's play button to `play_selectors` |
| `diagnosis` says the site blocked the server (403 / 429 / 503) | Site blocks datacenter IPs | Capture cannot work from this host. Use Direct mode with a link you copy yourself |
| Stream found but does not play | Stream needs headers the proxy does not send | Check the `/proxy` response in the browser's Network tab |
| `404` on `/api/stream` | No stream yet | Wait for a capture cycle, or paste a `.m3u8` in Option 2 |

## Limits

- **DRM streams** (Widevine, PlayReady, FairPlay) cannot be played by this project. It does not decrypt or bypass DRM.
- **Login-protected or paid content** is not supported. Each capture uses a fresh browser with no login.
- **Some sites block cloud servers.** If capture is blocked, Direct mode still works.
- **Signed URLs** expire. The capture loop refreshes them, but Direct mode links may stop working after a while.

## Legal

Use this only with streams you own, have permission to use, or that are public and allowed to be accessed. You are responsible for following each site's terms and the law where you live.

## Credits

- Stream detection: [jrcramos/video_link_extractor](https://github.com/jrcramos/video_link_extractor) (MIT)
- Player: [hls.js](https://github.com/video-dev/hls.js)
- Browser automation: [Playwright](https://playwright.dev/)

## Example stream for testing

Paste this into **Option 2** to check playback. It is a public sample from Apple's HLS examples:

```
https://devstreaming-cdn.apple.com/videos/streaming/examples/bipbop_4x3/bipbop_4x3_variant.m3u8
```

If it stops working, look for another public HLS test stream and use that instead.
