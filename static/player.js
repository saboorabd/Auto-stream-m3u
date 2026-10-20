/* Stream player logic. Text, timings and labels come from player-config.json. */
(() => {
  'use strict';

  const SELF = document.currentScript ? document.currentScript.src : location.href;
  const CONFIG_URL = new URL('player-config.json', SELF).href;

  // Icon paths (24x24 viewBox).
  const ICON = {
    play: '<path d="M8 5v14l11-7z" fill="currentColor"/>',
    pause: '<path d="M7 5h4v14H7zM13 5h4v14h-4z" fill="currentColor"/>',
    vol: '<path d="M4 9h4l5-4v14l-5-4H4z" fill="currentColor"/>'
       + '<path d="M16 9a4 4 0 0 1 0 6M18.5 6.5a7.5 7.5 0 0 1 0 11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
    mute: '<path d="M4 9h4l5-4v14l-5-4H4z" fill="currentColor"/>'
        + '<path d="M16 9l5 6M21 9l-5 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  };

  // Used only if player-config.json cannot be loaded.
  const FALLBACK = {
    api_base_on_pages: 'https://automatic-stream-capture.onrender.com',
    pages_host_suffix: 'github.io',
    poll_ms: 2000,
    status_every_ticks: 8,
    keepalive_ms: 240000,
    ctrl_hide_ms: 3000,
    last_stream_max_age_hours: 6,
    bad_hosts_max: 20,
    buffer_target_s: 10,
    fit_labels: { contain: 'Fit', cover: 'Crop', fill: 'Stretch' },
    stage_progress: {},
    stage_labels: {},
    messages: {
      waking_server: 'Server is waking up…',
      paste_hint: 'Paste a page or .m3u8 link below.',
      waiting_next: 'Waiting for the next capture…',
      no_stream: 'No stream found.',
      cannot_play_hls: 'This browser cannot play HLS.',
      reconnecting: 'Reconnecting to a fresh stream…',
      stream_set: 'Stream set',
      capture_started: 'Capture started',
      copied: 'Stream link copied',
      copy_failed: 'Copy failed',
      no_stream_yet: 'No stream yet',
      live_toast: 'Live edge',
      rotate_hint: 'Turn your phone sideways for a full view',
      buffer: 'Buffer',
      loading: 'Loading',
      updated: 'updated',
      pill: {
        ready: 'Ready', idle: 'Idle', capturing: 'Capturing', loading: 'Loading',
        live: 'Live', no_stream: 'No stream', waking: 'Waking server', offline: 'Offline',
      },
    },
  };

  async function loadConfig() {
    try {
      const res = await fetch(CONFIG_URL, { cache: 'no-store' });
      if (!res.ok) throw new Error('config ' + res.status);
      return { ...FALLBACK, ...(await res.json()) };
    } catch (_) {
      return FALLBACK;
    }
  }

  function start(C) {
    const M = C.messages;
    const API = location.hostname.endsWith(C.pages_host_suffix) ? C.api_base_on_pages : '';
    const M3U8 = /\.m3u8(\?|#|$)/i;
    const LAST_KEY = 'stream:last';
    const BAD_KEY = 'stream:badhosts';
    const FIT_KEY = 'stream:fit';
    const VOL_KEY = 'stream:volume';
    const LAST_MAX_AGE_MS = C.last_stream_max_age_hours * 3600 * 1000;
    const FIT_MODES = ['contain', 'cover', 'fill'];
    const LIVE_BUFFER_TARGET_S = 6;   // Live streams: show progress against 6 s, not 10 s.

    const $ = (id) => document.getElementById(id);
    const stage = $('stage');
    const video = $('v');
    const overlay = $('overlay');
    const msg = $('msg');
    const pct = $('pct');
    const spin = $('spin');
    const pill = $('pill');
    const bufChip = $('buf');
    const bar = $('progress');
    const form = $('form');
    const src = $('src');
    const go = $('go');
    const copyBtn = $('copy');
    const age = $('age');
    const toast = $('toast');
    const big = $('big');
    const playBtn = $('play');
    const playIcon = $('playIcon');
    const seek = $('seek');
    const liveEl = $('live');
    const goLiveBtn = $('golive');
    const timeEl = $('time');
    const muteBtn = $('mute');
    const volIcon = $('volIcon');
    const vol = $('vol');
    const qualitySel = $('quality');
    const fitBtn = $('fit');
    const fsBtn = $('fs');

    let hls = null;
    let rawUrl = null;
    let proxyUrl = null;
    let usingProxy = false;
    let fragOk = false;
    let netRetries = 0;
    let pollTick = 0;
    let seeking = false;
    let isLive = false;
    let hideTimer = null;
    let fitIdx = 0;

    // ---------- helpers ----------
    function setIcon(svgEl, paths) {
      if (svgEl) svgEl.innerHTML = paths;
    }

    function paintRange(input, ratio) {
      input.style.setProperty('--fill', Math.round(ratio * 100) + '%');
    }

    function toastMsg(text, ms = 1600) {
      toast.textContent = text;
      toast.classList.add('show');
      clearTimeout(toastMsg.t);
      toastMsg.t = setTimeout(() => toast.classList.remove('show'), ms);
    }

    function setPill(kind, text) {
      pill.className = 'chip pill ' + kind;
      pill.textContent = text;
    }

    function showOverlay(text, isError = false, percent = null) {
      overlay.classList.remove('hide');
      msg.textContent = text;
      msg.className = 'msg' + (isError ? ' err' : '');
      spin.style.display = isError ? 'none' : 'block';
      pct.textContent = percent === null || isError ? '' : percent + '%';
      updateUI();
    }

    function hideOverlay() {
      overlay.classList.add('hide');
      updateUI();
    }

    function ago(iso) {
      const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
      if (s < 60) return `${Math.floor(s)}s`;
      if (s < 3600) return `${Math.floor(s / 60)}m`;
      return `${Math.floor(s / 3600)}h`;
    }

    function explain(text) {
      try {
        const d = JSON.parse(text);
        return d.diagnosis || text;
      } catch (_) {
        return text || M.no_stream;
      }
    }

    function fmt(sec) {
      if (!isFinite(sec) || sec < 0) return '0:00';
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = Math.floor(sec % 60);
      const mm = h ? String(m).padStart(2, '0') : String(m);
      return (h ? h + ':' : '') + mm + ':' + String(s).padStart(2, '0');
    }

    function readJSON(key, fallback) {
      try {
        const v = JSON.parse(localStorage.getItem(key));
        return v === null || v === undefined ? fallback : v;
      } catch (_) { return fallback; }
    }

    function writeJSON(key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* storage blocked */ }
    }

    function hostOf(url) {
      try { return new URL(url).host; } catch (_) { return ''; }
    }

    function directBlocked(url) {
      return readJSON(BAD_KEY, []).includes(hostOf(url));
    }

    function markDirectBlocked(url) {
      const host = hostOf(url);
      const list = readJSON(BAD_KEY, []);
      if (host && !list.includes(host)) {
        list.push(host);
        writeJSON(BAD_KEY, list.slice(-C.bad_hosts_max));
      }
    }

    function saveLast(url, proxy) {
      writeJSON(LAST_KEY, { url, proxy, t: Date.now() });
    }

    function loadLast() {
      const v = readJSON(LAST_KEY, null);
      if (v && v.url && Date.now() - v.t < LAST_MAX_AGE_MS) return v;
      return null;
    }

    // ---------- one place that keeps every control in sync ----------
    function updateUI() {
      const playing = !video.paused && !video.ended;
      setIcon(playIcon, playing ? ICON.pause : ICON.play);
      stage.classList.toggle('playing', playing);
      // Big centre button shows only when paused and no loading overlay covers the picture.
      const overlayShown = !overlay.classList.contains('hide');
      stage.classList.toggle('paused', !playing && !overlayShown && !!rawUrl);
      updateVolume();
    }

    function updateVolume() {
      const off = video.muted || video.volume === 0;
      setIcon(volIcon, off ? ICON.mute : ICON.vol);
      const level = video.muted ? 0 : video.volume;
      vol.value = String(Math.round(level * 100));
      paintRange(vol, level);
    }

    // ---------- buffer: seconds loaded ahead of the playhead ----------
    function bufferSeconds() {
      const t = video.currentTime;
      const b = video.buffered;
      for (let i = 0; i < b.length; i++) {
        if (t >= b.start(i) - 0.25 && t <= b.end(i) + 0.25) {
          return Math.max(0, b.end(i) - t);
        }
      }
      return 0;
    }

    function updateBufferChip() {
      if (!rawUrl || video.readyState < 2) {
        bufChip.classList.add('hide');
        return;
      }
      const target = Math.max(1, isLive ? LIVE_BUFFER_TARGET_S : C.buffer_target_s);
      const secs = bufferSeconds();
      const percent = Math.min(100, Math.round((secs / target) * 100));
      bufChip.classList.remove('hide');
      bufChip.textContent = `${M.buffer} ${percent}% · ${secs.toFixed(1)}s`;
    }

    // ---------- play / pause ----------
    function togglePlay() {
      if (video.paused) video.play().catch(() => {});
      else video.pause();
    }

    big.addEventListener('click', (e) => {
      e.stopPropagation();
      togglePlay();
    });

    playBtn.addEventListener('click', togglePlay);

    video.addEventListener('play', () => { updateUI(); showControls(); });
    video.addEventListener('playing', updateUI);
    video.addEventListener('pause', () => { updateUI(); showControls(); });
    video.addEventListener('ended', updateUI);

    // ---------- volume + mute ----------
    muteBtn.addEventListener('click', () => {
      video.muted = !video.muted;
      if (!video.muted && video.volume === 0) video.volume = 0.5;
      writeJSON(VOL_KEY, { volume: video.volume, muted: video.muted });
      updateVolume();
    });

    vol.addEventListener('input', () => {
      video.volume = Number(vol.value) / 100;
      video.muted = video.volume === 0;
      writeJSON(VOL_KEY, { volume: video.volume, muted: video.muted });
      updateVolume();
    });

    video.addEventListener('volumechange', updateVolume);

    function restoreVolume() {
      const saved = readJSON(VOL_KEY, null);
      if (saved && typeof saved.volume === 'number') video.volume = saved.volume;
      // Browsers only allow sound after a tap, so the video starts muted and the user taps to unmute.
      video.muted = true;
      updateVolume();
    }

    // ---------- fit ----------
    function applyFit() {
      const mode = FIT_MODES[fitIdx];
      video.style.objectFit = mode;
      fitBtn.textContent = C.fit_labels[mode] || mode;
    }

    fitBtn.addEventListener('click', () => {
      fitIdx = (fitIdx + 1) % FIT_MODES.length;
      writeJSON(FIT_KEY, FIT_MODES[fitIdx]);
      applyFit();
    });

    // ---------- fullscreen + landscape ----------
    async function enterFullscreen() {
      try {
        if (stage.requestFullscreen) {
          await stage.requestFullscreen();
          if (screen.orientation && screen.orientation.lock) {
            await screen.orientation.lock('landscape');
          }
        } else if (video.webkitEnterFullscreen) {
          video.webkitEnterFullscreen();
        }
      } catch (_) {
        toastMsg(M.rotate_hint, 2600);
      }
    }

    function leaveFullscreen() {
      try {
        if (screen.orientation && screen.orientation.unlock) screen.orientation.unlock();
      } catch (_) { /* ignore */ }
      if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen();
      if (video.webkitDisplayingFullscreen && video.webkitExitFullscreen) video.webkitExitFullscreen();
    }

    fsBtn.addEventListener('click', () => {
      if (document.fullscreenElement || video.webkitDisplayingFullscreen) leaveFullscreen();
      else enterFullscreen();
    });

    document.addEventListener('fullscreenchange', () => {
      if (!document.fullscreenElement) {
        try {
          if (screen.orientation && screen.orientation.unlock) screen.orientation.unlock();
        } catch (_) { /* ignore */ }
      }
    });

    // ---------- controls: show on tap, hide while playing ----------
    function showControls() {
      stage.classList.add('show-ctrl');
      clearTimeout(hideTimer);
      if (!video.paused) {
        hideTimer = setTimeout(() => stage.classList.remove('show-ctrl'), C.ctrl_hide_ms);
      }
    }

    // Tapping the picture only shows or hides the controls. It never pauses playback.
    stage.addEventListener('click', (e) => {
      if (e.target.closest('.ctrl, .big')) return;
      if (stage.classList.contains('show-ctrl')) stage.classList.remove('show-ctrl');
      else showControls();
    });

    function setLive(live) {
      isLive = live;
      liveEl.classList.toggle('hide', !live);
      goLiveBtn.classList.toggle('hide', !live);
      seek.classList.toggle('hide', live);
      timeEl.classList.toggle('hide', live);
    }

    function updateTime() {
      const d = video.duration;
      if (d === Infinity) { setLive(true); return; }
      if (!isFinite(d) || isLive) return;
      timeEl.textContent = `${fmt(video.currentTime)} / ${fmt(d)}`;
      const ratio = video.currentTime / d;
      if (!seeking) {
        seek.value = String(Math.round(ratio * 1000));
        paintRange(seek, ratio);
      }
    }

    function fillQuality(inst) {
      qualitySel.innerHTML = '';
      const add = (value, label) => {
        const o = document.createElement('option');
        o.value = value;
        o.textContent = label;
        qualitySel.appendChild(o);
      };
      add('-1', 'Auto');

      // One entry per resolution. If a resolution has several bitrates, keep the highest.
      const best = new Map();
      inst.levels.forEach((lvl, idx) => {
        if (!lvl.height) return;
        const cur = best.get(lvl.height);
        if (cur === undefined || (lvl.bitrate || 0) > (inst.levels[cur].bitrate || 0)) best.set(lvl.height, idx);
      });
      [...best.keys()].sort((a, b) => b - a).forEach((h) => add(String(best.get(h)), h + 'p'));
      qualitySel.value = '-1';
    }

    qualitySel.addEventListener('change', () => {
      if (hls) hls.currentLevel = Number(qualitySel.value);
    });

    goLiveBtn.addEventListener('click', () => {
      if (hls && hls.liveSyncPosition) video.currentTime = hls.liveSyncPosition;
      else if (isFinite(video.duration)) video.currentTime = video.duration;
      video.play().catch(() => {});
      toastMsg(M.live_toast);
    });

    video.addEventListener('timeupdate', () => { updateTime(); updateBufferChip(); });
    video.addEventListener('durationchange', updateTime);
    video.addEventListener('progress', updateBufferChip);

    seek.addEventListener('input', () => {
      seeking = true;
      paintRange(seek, Number(seek.value) / 1000);
    });
    seek.addEventListener('change', () => {
      const d = video.duration;
      if (isFinite(d)) video.currentTime = (Number(seek.value) / 1000) * d;
      seeking = false;
    });

    // ---------- playback ----------
    function onHlsError(_e, d) {
      if (!d.fatal) return;

      if (d.type === Hls.ErrorTypes.NETWORK_ERROR) {
        if (!usingProxy && proxyUrl) {
          if (!fragOk) markDirectBlocked(rawUrl);
          usingProxy = true;
          fragOk = false;
          netRetries = 0;
          hls.loadSource(proxyUrl);
          return;
        }
        netRetries += 1;
        if (netRetries > 5) {
          rawUrl = null;
          netRetries = 0;
          showOverlay(M.reconnecting);
          fetch(API + '/api/refresh', { method: 'POST' }).catch(() => {});
          return;
        }
        hls.startLoad();
      } else if (d.type === Hls.ErrorTypes.MEDIA_ERROR) {
        hls.recoverMediaError();
      } else {
        showOverlay('Player error: ' + d.details, true);
      }
    }

    function createHls() {
      hls = new Hls({
        enableWorker: true,
        // Fast first frame: start on the lowest quality, then climb as bandwidth allows.
        startLevel: 0,
        capLevelToPlayerSize: true,
        startFragPrefetch: true,
        lowLatencyMode: true,           // Live streams: stay close to the live edge.
        liveSyncDurationCount: 2,
        liveMaxLatencyDurationCount: 6,
        maxBufferLength: 8,             // Less to load before the first frame.
        maxMaxBufferLength: 20,
        backBufferLength: 10,
        manifestLoadingTimeOut: 8000,
        fragLoadingTimeOut: 15000,
      });
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        fillQuality(hls);
        hideOverlay();
        video.play().catch(() => {});
      });
      hls.on(Hls.Events.LEVEL_LOADED, (_e, data) => setLive(!!data.details.live));
      hls.on(Hls.Events.FRAG_LOADED, () => { fragOk = true; netRetries = 0; });
      hls.on(Hls.Events.ERROR, onHlsError);
    }

    function attach(url) {
      const hasHls = window.Hls && Hls.isSupported();
      qualitySel.classList.toggle('hide', !hasHls);

      if (hasHls) {
        if (!hls) createHls();
        usingProxy = !!proxyUrl && directBlocked(url);
        fragOk = false;
        netRetries = 0;
        hls.loadSource(usingProxy ? proxyUrl : url);
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = proxyUrl || url;
        video.play().catch(() => {});
      } else {
        showOverlay(M.cannot_play_hls, true);
      }
    }

    function playStream(url, proxy) {
      rawUrl = url;
      proxyUrl = proxy && !proxy.startsWith('http') ? API + proxy : proxy;
      hideOverlay();
      setPill('ok', M.pill.live);
      attach(url);
      saveLast(url, proxyUrl);
    }

    // ---------- server sync ----------
    async function keepAlive() {
      try {
        await fetch(API + '/api/status?ka=' + Date.now(), { cache: 'no-store' });
      } catch (_) { /* offline or server asleep */ }
    }

    async function checkStream() {
      try {
        const res = await fetch(API + '/api/stream?t=' + Date.now(), { cache: 'no-store' });
        if (res.status === 404) return;
        const data = await res.json();

        if (data.updated_at) {
          age.textContent = `${M.updated} ${ago(data.updated_at)} ago`;
          age.classList.remove('hide');
        }
        if (data.url && data.url !== rawUrl) playStream(data.url, data.proxy_url);
      } catch (_) { /* server waking up; next tick retries */ }
    }

    async function checkStatus() {
      try {
        const res = await fetch(API + '/api/status?t=' + Date.now(), { cache: 'no-store' });
        const s = await res.json();
        const name = (s.stage && s.stage.name) || 'idle';

        if (rawUrl) return;

        const label = C.stage_labels[name];
        const progress = C.stage_progress[name] || 0;
        if (label) {
          setPill('warn', M.pill.capturing);
          bar.style.width = progress + '%';
          showOverlay(label + '…', false, progress);
        } else if (name === 'timeout_no_stream') {
          setPill('err', M.pill.no_stream);
          bar.style.width = '0%';
          showOverlay(explain(s.stage.error), true);
        } else if (s.page_url) {
          setPill('', M.pill.idle);
          showOverlay(s.last_error ? explain(s.last_error) : M.waiting_next);
        } else {
          setPill('', M.pill.ready);
          showOverlay(M.paste_hint);
        }
      } catch (_) {
        if (!rawUrl) {
          setPill('warn', M.pill.waking);
          showOverlay(M.waking_server);
        }
      }
    }

    // ---------- user input ----------
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const value = src.value.trim();
      if (!value) return;

      const isStream = M3U8.test(value);
      const body = isStream ? { stream_url: value } : { page_url: value };

      go.disabled = true;
      try {
        const res = await fetch(API + '/api/target', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);

        rawUrl = null;
        proxyUrl = null;
        src.value = '';
        bar.style.width = '5%';
        setPill('warn', isStream ? M.pill.loading : M.pill.capturing);
        showOverlay(isStream ? M.loading + '…' : M.capture_started + '…', false, 5);
        toastMsg(isStream ? M.stream_set : M.capture_started);
        checkStream();
      } catch (err) {
        toastMsg('Failed: ' + err.message);
      } finally {
        go.disabled = false;
      }
    });

    copyBtn.addEventListener('click', async () => {
      if (!proxyUrl) return toastMsg(M.no_stream_yet);
      const link = proxyUrl.startsWith('http') ? proxyUrl : location.origin + proxyUrl;
      try {
        await navigator.clipboard.writeText(link);
        toastMsg(M.copied);
      } catch (_) {
        toastMsg(M.copy_failed);
      }
    });

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        keepAlive();
        checkStream();
        checkStatus();
      }
    });

    // ---------- start ----------
    fitIdx = Math.max(0, FIT_MODES.indexOf(readJSON(FIT_KEY, 'contain')));
    applyFit();
    restoreVolume();
    updateUI();

    // 1. Last known stream plays from cache immediately, before the server answers.
    const last = loadLast();
    if (last) playStream(last.url, last.proxy);

    // 2. Then sync with the server in the background.
    keepAlive();
    checkStream();
    checkStatus();
    setInterval(keepAlive, C.keepalive_ms);
    setInterval(() => {
      checkStream();
      pollTick += 1;
      if (!rawUrl || pollTick >= C.status_every_ticks) {
        pollTick = 0;
        checkStatus();
      }
    }, C.poll_ms);
    setInterval(updateBufferChip, 1000);
  }

  loadConfig().then(start);
})();
