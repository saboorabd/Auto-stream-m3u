# Stream M3U (based on Automatic-stream-capture)

Give it a web page that plays a stream. It captures the `.m3u8` link (and keeps refreshing
the token), then serves a **fixed M3U playlist URL** that always points to the current stream.
Add that URL once in OTT Navigator. It works in a browser too.

## Endpoints

| URL | What it does |
|---|---|
| `/` | Web player (same as the original app) |
| `/playlist.m3u` | M3U file with one channel. Add this URL to OTT Navigator |
| `/live.m3u8` | Current captured stream (302 to `/proxy`). Players open this |
| `/api/status` | Capture status and errors |

## How the token refresh works

1. The capture loop opens `page_url` in a headless-style Chromium with the extension.
2. It finds the newest `.m3u8` request and stores it.
3. The loop keeps running every `refresh_interval_s` seconds, and reloads the page every `reload_after_s` seconds, so tokens are refreshed.
4. `/live.m3u8` always returns the newest stored link. OTT Navigator re-opens the channel, so it gets the fresh token.

## Setup (Render, same as the original)

1. Put this folder in a GitHub repo and deploy it with **Docker** on Render (Starter plan or higher).
2. In Render > Environment, add:
   - `ACCESS_KEY` = any long random string (strongly recommended, see Security).
3. Edit `config.json`:
   - `page_url`: the page that plays the stream
   - `channel_name`: name shown in OTT Navigator
   - `play_selectors`: add the site's play button selector if capture is not starting
4. Open `https://YOUR-APP.onrender.com/api/status` until `stage.name` shows `stream_found`.

## Add to OTT Navigator

1. OTT Navigator > Playlists (or Settings > Playlists) > **Add playlist** > **URL**.
2. Paste:
   `https://YOUR-APP.onrender.com/playlist.m3u?key=YOUR_ACCESS_KEY`
3. Refresh the playlist, then open the channel.

## Web player

Open `https://YOUR-APP.onrender.com/?key=YOUR_ACCESS_KEY` or play `/live.m3u8?key=...` in any HLS player (VLC: Media > Open Network, paste the `/live.m3u8` link).

## Security

- Set `ACCESS_KEY`. Without it, anyone who finds your URL can watch the stream.
- The `/proxy` route is open, but it only serves links the capture found, and it blocks private/internal addresses.

## Limits

- Video goes through your server, so bandwidth is used on the server host.
- If the page needs a login, or the site blocks cloud servers (403/429), capture will fail. See `/api/status`.
- DRM streams (Widevine etc.) cannot be played. This project does not decrypt DRM.
- Use this only for streams you have the right to use.
