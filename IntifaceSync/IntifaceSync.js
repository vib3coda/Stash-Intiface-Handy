/**
 * IntifaceSync – Stash UI Plugin
 * Supports: Intiface Central + The Handy WiFi (HSSP)
 */

(function () {
  "use strict";
  if (window.__IntifaceSyncLoaded) return;
  window.__IntifaceSyncLoaded = true;

  const BACKEND_HOST    = window.location.hostname;
  const BACKEND_URL     = `ws://${BACKEND_HOST}:7880`;
  const PLUGIN_ID       = "IntifaceSync";
  const MIN_STROKE_GAP  = 5;
  const LS_KEY          = "IntifaceSync.settings";

  // ── State ──────────────────────────────────────────────────────────────────
  let ws                    = null;
  let wsReady               = false;
  let currentScenePath      = null;
  let funscripts            = [];
  let selectedFunscript     = null;
  let funscriptLoaded       = false;
  let pendingPlay           = null;
  let statusData            = { connected: false, playing: false, devices: [], mode: "intiface" };
  let offsetMs              = 0;
  let strokeMin             = 0;
  let strokeMax             = 100;
  let mode                  = "intiface";   // "intiface" | "handy_wifi"
  let handyKey              = "";
  let videoEl               = null;
  let toolbarInjected       = false;
  let reconnectTimer        = null;
  let reconnectDelay        = 3000;
  let intifaceReady         = false;
  let connectingToIntiface  = false;
  let pendingFindFunscripts = null;
  let statusPollInterval    = null;
  let eventInfoTimer = null;
  let eventInfoActive = false;
  let invert = localStorage.getItem("intifaceSync.invert") === "true";

  function log(msg, level = "info") {
    const prefix = "[IntifaceSync]";
    const debugOn = localStorage.getItem("intifaceSyncDebug") === "1";

    if (level === "error")      console.error(prefix, msg);
    else if (level === "debug") { if (debugOn) console.log(prefix, msg); }
    else                        console.log(prefix, msg);
  }

  // ── Settings ───────────────────────────────────────────────────────────────
  function loadSettingsFromStorage() {
    try {
      const raw = localStorage.getItem(LS_KEY);
      if (!raw) return;
      const s = JSON.parse(raw);
      if (typeof s.offsetMs   === "number")  offsetMs  = s.offsetMs;
      if (typeof s.strokeMin  === "number")  strokeMin = s.strokeMin;
      if (typeof s.strokeMax  === "number")  strokeMax = s.strokeMax;
      if (typeof s.invert     === "boolean") invert    = s.invert;
      if (typeof s.mode       === "string")  mode      = s.mode;
      if (typeof s.handyKey   === "string")  handyKey  = s.handyKey;
    } catch (e) {
      log(`Failed to load settings: ${e}`, "error");
    }
  }

  function saveSettingsToStorage() {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify({
        offsetMs, strokeMin, strokeMax, invert, mode, handyKey,
      }));
    } catch (e) {
      log(`Failed to save settings: ${e}`, "error");
    }
  }

  function sendSettings() {
    saveSettingsToStorage();
    sendMsg({
      type:      "settings",
      offsetMs:  offsetMs,
      strokeMin: strokeMin / 100,
      strokeMax: strokeMax / 100,
      invert:    invert,
    });
  }

  // ── GraphQL ────────────────────────────────────────────────────────────────
  async function gqlQuery(query, variables = {}) {
    const resp = await fetch("/graphql", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ query, variables }),
    });
    const data = await resp.json();
    return data?.data ?? null;
  }

  async function getSceneDetails(sceneId) {
    const q = `
      query ($id: ID!) {
        findScene(id: $id) {
          id
          files { path }
        }
      }`;
    const data = await gqlQuery(q, { id: sceneId });
    return data?.findScene ?? null;
  }

  async function loadPluginConfig() {
    const q = `query { configuration { plugins } }`;
    const data = await gqlQuery(q);
    const plugins = data?.configuration?.plugins ?? {};
    return plugins[PLUGIN_ID] ?? {};
  }

  // ── WebSocket ──────────────────────────────────────────────────────────────
  function sendMsg(obj) {
      if (ws && wsReady) ws.send(JSON.stringify(obj));
    }

  // Synchronous stop attempt on tab close
  window.addEventListener("pagehide", () => {
    try { sendMsg({ type: "pause" }); } catch(e) {}
  });
  window.addEventListener("beforeunload", () => {
    try { sendMsg({ type: "pause" }); } catch(e) {}
  });

  function connectBackend() {
    if (ws) { try { ws.close(); } catch (_) {} }
    ws = new WebSocket(BACKEND_URL);

    ws.addEventListener("open", () => {
      wsReady        = true;
      reconnectDelay = 3000;
      clearTimeout(reconnectTimer);
      log(`Connected to backend (${BACKEND_URL})`);

      sendMsg({ type: "setMode", mode });
      sendSettings();

      statusPollInterval = setInterval(() => {
        if (wsReady && !intifaceReady) sendMsg({ type: "status" });
      }, 3000);
    });

    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      handleBackendMessage(msg);
    });

    ws.addEventListener("close", () => {
      wsReady       = false;
      intifaceReady = false;
      clearInterval(statusPollInterval);
      updateToolbarStatus();
      reconnectTimer = setTimeout(() => {
        reconnectDelay = Math.min(reconnectDelay * 2, 30000);
        connectBackend();
      }, reconnectDelay);
    });

    ws.addEventListener("error", () => { wsReady = false; });
  }

  // ── Backend messages ───────────────────────────────────────────────────────
  function handleBackendMessage(msg) {


    if (msg.type === "info") {
      log(`Backend: ${msg.message}`, "debug");
      updateToolbarInfo(msg.message);
      return;
    }

    if (msg.type === "event") {
      log(msg.message, msg.level);
      updateToolbarInfo(msg.message);
      eventInfoActive = true;
      clearTimeout(eventInfoTimer);
      eventInfoTimer = setTimeout(() => {
        eventInfoActive = false;
        updateToolbarStatus();
      }, 4000);

      if (msg.uploaded && videoEl && !videoEl.paused && !videoEl.ended) {
        const t = videoEl.currentTime * 1000;
        sendMsg({ type: "play", time: t });
        log(`Resume device at ${t.toFixed(0)}ms after upload`, "debug");
      }
      return;
    }


    if (msg.type === "status") {
      statusData = msg;
      updateToolbarStatus();
      if (msg.error) log(`Backend error: ${msg.error}`, "error");

      if (mode === "intiface") {
        if (msg.connected && !intifaceReady) {
          intifaceReady        = true;
          connectingToIntiface = false;
          if (pendingFindFunscripts !== null) {
            sendMsg({ type: "findFunscripts", videoPath: pendingFindFunscripts.videoPath });
            pendingFindFunscripts = null;
          }
        }
      } else {
        if (msg.connected && !intifaceReady) {
          intifaceReady = true;
          if (pendingFindFunscripts !== null) {
            sendMsg({ type: "findFunscripts", videoPath: pendingFindFunscripts.videoPath });
            pendingFindFunscripts = null;
          }
        }
        if (msg.tunnelUrl) updateTunnelUrlDisplay(msg.tunnelUrl);
      }

      if (!msg.error && funscriptLoaded === "pending") {
        funscriptLoaded = true;
        if (pendingPlay !== null) {
          sendMsg({ type: "play", time: pendingPlay.time });
          pendingPlay = null;
        }
      }
      return;
    }

    if (msg.type === "funscripts") {
      funscripts = msg.files ?? [];
      const defaultScript = msg.default ?? null;
      log(`Funscripts received: ${funscripts.length} file(s)`);

      if (funscripts.length === 0) {
        pendingPlay = null;
        updateFunscriptSelector();
      } else {
        selectedFunscript = defaultScript || funscripts[0];
        updateFunscriptSelector();
        loadFunscript(selectedFunscript);
      }
      return;
    }
  }


  // ── Funscript ──────────────────────────────────────────────────────────────
  function loadFunscript(path) {
    log(`Loading funscript: ${path} (invert=${invert})`);
    funscriptLoaded   = "pending";
    selectedFunscript = path;
    sendMsg({ type: "loadFile", path, invert });
    updateFunscriptSelector();
  }

  // ── Stop-Helper ────────────────────────────────────────────────────────────
  function stopPlayback(reason) {
    pendingPlay = null;
    sendMsg({ type: "pause" });
  }

  // ── Video events ───────────────────────────────────────────────────────────
  function attachVideoEvents(video) {
    if (videoEl === video) return;
    videoEl = video;

    video.addEventListener("play", () => {
      const t = video.currentTime * 1000;
      log(`Video play @ ${t.toFixed(0)}ms`, "debug");
      if (!funscriptLoaded) pendingPlay = { time: t };
      else                  sendMsg({ type: "play", time: t });
    });

    video.addEventListener("pause", () => {
      log("Video pause", "debug");
      pendingPlay = null;
      sendMsg({ type: "pause" });
    });

    video.addEventListener("seeked", () => {
      const t = video.currentTime * 1000;
      log(`Video seek @ ${t.toFixed(0)}ms`, "debug");
      sendMsg({ type: "seek", time: t });
      if (!video.paused) {
        if (!funscriptLoaded) pendingPlay = { time: t };
        else                  sendMsg({ type: "play", time: t });
      }
    });

    video.addEventListener("ended", () => {
      log("Video ended", "debug");
      pendingPlay = null;
      sendMsg({ type: "pause" });
    });

    video.addEventListener("emptied", () => {
      log("Video emptied", "debug");
      pendingPlay = null;
      sendMsg({ type: "pause" });
    });
  }


  // ── Page-Lifecycle ─────────────────────────────────────────────────────────
  window.addEventListener("pagehide", () => stopPlayback("pagehide"));
  window.addEventListener("beforeunload", () => stopPlayback("beforeunload"));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden" && videoEl && !videoEl.paused) {
      stopPlayback("hidden");
    }
  });

  // ── Identify a change in the SPA route ─────────────────────────────────────────────
  let lastPath = location.pathname;
  setInterval(() => {
    if (location.pathname !== lastPath) {
      lastPath = location.pathname;
      if (!/^\/scenes\/\d+/.test(lastPath)) {
        stopPlayback("route-change");
        videoEl = null;
        funscriptLoaded = false;
      }
    }
  }, 500);


  // ── Styles ──────────────────────────────────────────────────────────────────
function injectStyles() {
  if (document.getElementById(`${PLUGIN_ID}-style`)) return;
  const st = document.createElement("style");
  st.id = `${PLUGIN_ID}-style`;
  st.textContent = `
    /* ── Toolbar Container ──────────────────────────────────── */
    #${PLUGIN_ID}-toolbar {
      background: linear-gradient(180deg, rgba(20,22,28,0.92), rgba(14,16,20,0.95)) !important;
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      border-top: 1px solid rgba(255,255,255,0.08) !important;
      box-shadow: 0 -4px 20px rgba(0,0,0,0.4);
      color: #e8eaed !important;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif !important;
      font-size: 12px !important;
      padding: 8px 14px !important;
      gap: 12px !important;
    }
    #${PLUGIN_ID}-toolbar span,
    #${PLUGIN_ID}-toolbar label {
      color: #c4c8cf;
      font-weight: 500;
      letter-spacing: 0.2px;
    }

    /* ── Buttons ────────────────────────────────────────────── */
    #${PLUGIN_ID}-toolbar button {
      background: rgba(255,255,255,0.06);
      color: #e8eaed;
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 6px;
      padding: 5px 12px;
      font-size: 11px;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    #${PLUGIN_ID}-toolbar button:hover {
      background: rgba(90,169,255,0.15);
      border-color: rgba(90,169,255,0.4);
      color: #fff;
    }
    #${PLUGIN_ID}-toolbar button:active {
      transform: translateY(1px);
    }

    /* ── Number / Text Inputs ──────────────────────────────── */
    #${PLUGIN_ID}-toolbar input[type=number],
    #${PLUGIN_ID}-toolbar input[type=text] {
      background: rgba(0,0,0,0.35);
      color: #fff;
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 6px;
      padding: 5px 8px;
      font-size: 11px;
      font-family: inherit;
      text-align: center;
      transition: border-color 0.15s, box-shadow 0.15s;
      outline: none;
    }
    #${PLUGIN_ID}-toolbar input[type=number]:focus,
    #${PLUGIN_ID}-toolbar input[type=text]:focus {
      border-color: #5aa9ff;
      box-shadow: 0 0 0 2px rgba(90,169,255,0.2);
    }
    #${PLUGIN_ID}-toolbar input[type=number]::-webkit-inner-spin-button,
    #${PLUGIN_ID}-toolbar input[type=number]::-webkit-outer-spin-button {
      -webkit-appearance: none; margin: 0;
    }
    #${PLUGIN_ID}-toolbar input[type=number] { -moz-appearance: textfield; }

    /* ── Range Sliders ─────────────────────────────────────── */
    #${PLUGIN_ID}-toolbar input[type=range] {
      -webkit-appearance: none;
      appearance: none;
      background: transparent;
      pointer-events: none;
      height: 18px;
      padding: 0;
    }
    #${PLUGIN_ID}-toolbar input[type=range]::-webkit-slider-runnable-track {
      height: 4px; background: transparent; border-radius: 2px;
    }
    #${PLUGIN_ID}-toolbar input[type=range]::-moz-range-track {
      height: 4px; background: transparent; border-radius: 2px;
    }
    #${PLUGIN_ID}-toolbar input[type=range]::-webkit-slider-thumb {
      -webkit-appearance: none;
      appearance: none;
      width: 14px; height: 14px; border-radius: 50%;
      background: #fff;
      border: 2px solid #5aa9ff;
      box-shadow: 0 2px 6px rgba(0,0,0,0.4);
      cursor: pointer;
      pointer-events: all;
      margin-top: -5px;
      transition: transform 0.12s ease, box-shadow 0.12s ease;
    }
    #${PLUGIN_ID}-toolbar input[type=range]::-webkit-slider-thumb:hover {
      transform: scale(1.15);
      box-shadow: 0 2px 10px rgba(90,169,255,0.5);
    }
    #${PLUGIN_ID}-toolbar input[type=range]::-moz-range-thumb {
      width: 14px; height: 14px; border-radius: 50%;
      background: #fff;
      border: 2px solid #5aa9ff;
      box-shadow: 0 2px 6px rgba(0,0,0,0.4);
      cursor: pointer;
      pointer-events: all;
    }

    /* ── Handy Panel ───────────────────────────────────────── */
    #${PLUGIN_ID}-handy-panel {
      display: flex; align-items: center; gap: 8px;
      flex-wrap: wrap; width: 100%;
      padding: 8px 0 4px 0;
      border-top: 1px solid rgba(255,255,255,0.08);
      margin-top: 4px;
    }
    #${PLUGIN_ID}-handy-key {
      background: rgba(0,0,0,0.35);
      color: #fff;
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 6px;
      padding: 5px 10px;
      font-size: 11px;
      font-family: "SF Mono", Menlo, Consolas, monospace;
      letter-spacing: 1.5px;
      width: 160px;
      outline: none;
      transition: border-color 0.15s, box-shadow 0.15s;
    }
    #${PLUGIN_ID}-handy-key:focus {
      border-color: #5aa9ff;
      box-shadow: 0 0 0 2px rgba(90,169,255,0.2);
    }
    #${PLUGIN_ID}-tunnel-url {
      font-size: 10px;
      color: #5aa9ff;
      opacity: 0.85;
      font-family: "SF Mono", Menlo, Consolas, monospace;
      max-width: 260px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    /* ── Mode Buttons (Tab-Style) ──────────────────────────── */
    .${PLUGIN_ID}-mode-btn {
      background: rgba(255,255,255,0.05);
      color: #9aa0a6;
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 6px;
      padding: 5px 12px;
      font-size: 11px;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .${PLUGIN_ID}-mode-btn:hover {
      background: rgba(255,255,255,0.08);
      color: #e8eaed;
    }
    #${PLUGIN_ID}-toolbar .${PLUGIN_ID}-mode-btn.active {
      background: rgba(90,169,255,0.18) !important;
      color: #fff !important;
      border: 1px solid rgba(90,169,255,0.6) !important;
      box-shadow: 0 0 12px rgba(90,169,255,0.25) !important;
    }

    #${PLUGIN_ID}-toolbar #${PLUGIN_ID}-stop {
      background: rgba(255,90,90,0.12) !important;
      border: 1px solid rgba(255,90,90,0.35) !important;
      color: #ff8a8a !important;
    }
    #${PLUGIN_ID}-toolbar #${PLUGIN_ID}-stop:hover {
      background: rgba(255,90,90,0.22) !important;
      border-color: rgba(255,90,90,0.6) !important;
      color: #fff !important;
    }

    #${PLUGIN_ID}-toolbar #${PLUGIN_ID}-connect-btn,
    #${PLUGIN_ID}-handy-panel #${PLUGIN_ID}-connect-handy-btn {
      background: rgba(90,200,120,0.12) !important;
      border: 1px solid rgba(90,200,120,0.35) !important;
      color: #8ee0a3 !important;
    }
    #${PLUGIN_ID}-toolbar #${PLUGIN_ID}-connect-btn:hover,
    #${PLUGIN_ID}-handy-panel #${PLUGIN_ID}-connect-handy-btn:hover {
      background: rgba(90,200,120,0.22) !important;
      border-color: rgba(90,200,120,0.6) !important;
      color: #fff !important;
    }

    #${PLUGIN_ID}-handy-panel #${PLUGIN_ID}-reupload-btn {
      background: rgba(230,190,80,0.12) !important;
      border: 1px solid rgba(230,190,80,0.35) !important;
      color: #e6c374 !important;
    }
    #${PLUGIN_ID}-handy-panel #${PLUGIN_ID}-reupload-btn:hover {
      background: rgba(230,190,80,0.22) !important;
      border-color: rgba(230,190,80,0.6) !important;
      color: #fff !important;
    }

    #${PLUGIN_ID}-toolbar #${PLUGIN_ID}-invert.active {
      background: rgba(90,169,255,0.18) !important;
      color: #fff !important;
      box-shadow: 0 0 12px rgba(90,169,255,0.25) !important;
    }


    /* ── Mobile ────────────────────────────────────────────── */
    @media (max-width: 768px) {
      #${PLUGIN_ID}-toolbar {
        font-size: 14px !important;
        padding: 10px !important;
        gap: 10px !important;
      }
      #${PLUGIN_ID}-toolbar input[type=range] { height: 26px !important; }
      #${PLUGIN_ID}-toolbar input[type=range]::-webkit-slider-thumb {
        width: 20px !important; height: 20px !important; margin-top: -12px !important;
      }
      #${PLUGIN_ID}-toolbar input[type=range]::-moz-range-thumb {
        width: 20px !important; height: 20px !important;
      }
      #${PLUGIN_ID}-toolbar input[type=number] {
        width: 80px !important; font-size: 13px !important; padding: 7px 8px !important;
      }
      #${PLUGIN_ID}-toolbar button,
      .${PLUGIN_ID}-mode-btn {
        padding: 7px 14px !important; font-size: 12px !important;
      }
        #${PLUGIN_ID}-toolbar select {
        font-size: 14px !important;
        padding: 6px 10px !important;
        height: 32px !important;
      }
    }
  `;
  document.head.appendChild(st);
}

  function buttonStyle(bg) {
    return "";
  }

  // ── Toolbar components ─────────────────────────────────────────────────────
  function buildOffsetInput() {
    const wrap = document.createElement("span");
    wrap.style.cssText = "display:inline-flex;align-items:center;gap:3px;margin-left:6px;";

    const label = document.createElement("span");
    label.textContent = "Offset:";
    wrap.appendChild(label);

    const minus = document.createElement("button");
    minus.textContent   = "−";
    minus.style.cssText = "min-width:26px;padding:4px 8px;font-weight:bold;";
    wrap.appendChild(minus);

    const input = document.createElement("input");
    input.id    = `${PLUGIN_ID}-offset`;
    input.type  = "number";
    input.value = String(offsetMs);
    input.min = -2000;
    input.max = 2000;
    input.step = 10;
    input.title = "If device reacts too late → increase.\nIf device reacts too early → decrease.\nRange: -2000 to 2000";
    input.style.cssText = "width:64px;";
    wrap.appendChild(input);

    const plus = document.createElement("button");
    plus.textContent   = "+";
    plus.style.cssText = "min-width:26px;padding:4px 8px;font-weight:bold;";
    wrap.appendChild(plus);

    const unit = document.createElement("span");
    unit.textContent  = "ms";
    unit.style.opacity = "0.7";
    wrap.appendChild(unit);

    function setOffset(v) {
      let val = parseInt(v, 10) || 0;
      val = Math.max(-2000, Math.min(2000, val));
      offsetMs    = val;
      input.value = String(val);
      sendSettings();
    }
    minus.addEventListener("click", () => setOffset(offsetMs - 10));
    plus .addEventListener("click", () => setOffset(offsetMs + 10));
    input.addEventListener("change", () => setOffset(input.value));
    return wrap;
  }

  function buildStrokeRange() {
    const wrap = document.createElement("span");
    wrap.style.cssText = "display:inline-flex;align-items:center;gap:6px;margin-left:6px;";

    const label = document.createElement("span");
    label.textContent = "Stroke Range:";
    wrap.appendChild(label);

    const track = document.createElement("span");
    track.style.cssText = "position:relative;width:140px;height:18px;display:inline-block;z-index:0;";

    const trackBg = document.createElement("span");
    trackBg.style.cssText = "position:absolute;top:7px;left:0;right:0;height:4px;" +
                            "background:rgba(255,255,255,0.1);border-radius:2px;z-index:1;pointer-events:none;";

    const trackFill = document.createElement("span");
    trackFill.style.cssText = "position:absolute;top:7px;height:4px;z-index:2;pointer-events:none;" +
                              "background:linear-gradient(90deg,#5aa9ff,#7dbcff);" +
                              "border-radius:2px;box-shadow:0 0 8px rgba(90,169,255,0.4);";

    function mkSlider() {
      const s = document.createElement("input");
      s.type = "range"; s.min = "0"; s.max = "100";
      s.style.cssText = "position:absolute;top:0;left:0;width:100%;height:18px;" +
                        "background:transparent;pointer-events:none;-webkit-appearance:none;" +
                        "appearance:none;margin:0;z-index:3;";
      return s;
    }

    track.appendChild(trackBg);
    track.appendChild(trackFill);
    wrap.appendChild(track);

    const sMin = mkSlider(); sMin.value = String(strokeMin); track.appendChild(sMin);
    const sMax = mkSlider(); sMax.value = String(strokeMax); track.appendChild(sMax);

    const valLabel = document.createElement("span");
    valLabel.style.cssText = "min-width:62px;text-align:center;opacity:0.85;";
    wrap.appendChild(valLabel);

    function updateUI() {
      trackFill.style.left  = strokeMin + "%";
      trackFill.style.width = (strokeMax - strokeMin) + "%";
      valLabel.textContent  = `${strokeMin}–${strokeMax}%`;
    }

    function onChange(ev) {
      let mn = parseInt(sMin.value, 10);
      let mx = parseInt(sMax.value, 10);
      if (mx - mn < MIN_STROKE_GAP) {
        if (ev.target === sMin) { mn = mx - MIN_STROKE_GAP; sMin.value = String(mn); }
        else                    { mx = mn + MIN_STROKE_GAP; sMax.value = String(mx); }
      }
      strokeMin = mn; strokeMax = mx;
      updateUI(); sendSettings();
    }
    sMin.addEventListener("input", onChange);
    sMax.addEventListener("input", onChange);
    updateUI();
    return wrap;
  }

  // ── Handy WiFi Panel ───────────────────────────────────────────────────────
  function buildHandyPanel() {
    const panel = document.createElement("div");
    panel.id = `${PLUGIN_ID}-handy-panel`;

    const keyLabel = document.createElement("span");
    keyLabel.textContent  = "Connection Key:";
    keyLabel.style.cssText = "font-size:11px;";
    panel.appendChild(keyLabel);

    const keyInput = document.createElement("input");
    keyInput.id          = `${PLUGIN_ID}-handy-key`;
    keyInput.type        = "text";
    keyInput.placeholder = "XXXX-XXXX";
    keyInput.value       = handyKey;
    keyInput.maxLength   = 20;
    panel.appendChild(keyInput);

    const connectBtn = document.createElement("button");
    connectBtn.id = `${PLUGIN_ID}-connect-handy-btn`;
    connectBtn.textContent   = "Connect Handy";
    connectBtn.addEventListener("click", () => {
      const key = keyInput.value.trim();
      if (!key) return;
      handyKey = key;
      saveSettingsToStorage();
      log(`Sending connectHandy, wsReady=${wsReady}, ws=${ws?.readyState}`, "debug");
      sendMsg({ type: "connectHandy", connectionKey: key });
    });
    panel.appendChild(connectBtn);

    const tunnelLabel = document.createElement("span");
    tunnelLabel.textContent   = "Tunnel:";
    tunnelLabel.style.cssText = "font-size:11px;opacity:0.7;";
    panel.appendChild(tunnelLabel);

    const tunnelUrl = document.createElement("span");
    tunnelUrl.id          = `${PLUGIN_ID}-tunnel-url`;
    tunnelUrl.textContent = "–";
    panel.appendChild(tunnelUrl);

        panel.appendChild(tunnelUrl);

    // Reupload-Button
    const reuploadBtn = document.createElement("button");
    reuploadBtn.id          = `${PLUGIN_ID}-reupload-btn`;
    reuploadBtn.textContent = "⟳ Reupload Script";
    reuploadBtn.addEventListener("click", () => {
      if (!selectedFunscript) { log("No funscript selected for reupload", "error"); return; }
      if (ws && ws.readyState === WebSocket.OPEN) {
        log(`Reuploading funscript: ${selectedFunscript}`);
        ws.send(JSON.stringify({ type: "loadFile", path: selectedFunscript, force: true, invert: invert}));
      } else {
        log("WS not connected", "error");
      }
    });
    panel.appendChild(reuploadBtn);

    return panel;
  }

  function updateTunnelUrlDisplay(url) {
    const el = document.getElementById(`${PLUGIN_ID}-tunnel-url`);
    if (el) el.textContent = url || "–";
  }

  function updateToolbarInfo(message) {
    const el = document.getElementById(`${PLUGIN_ID}-status`);
    if (el) { el.textContent = `ℹ ${message}`; el.style.color = "#fa0"; }
  }

  // ── Toolbar ────────────────────────────────────────────────────────────────
  function buildToolbar() {
    injectStyles();

    const bar = document.createElement("div");
    bar.id    = `${PLUGIN_ID}-toolbar`;
    bar.style.cssText = `
      display:flex; align-items:center; gap:8px;
      padding:4px 10px; background:rgba(0,0,0,0.75);
      color:#fff; font-size:12px; font-family:sans-serif;
      border-top:1px solid #444; flex-wrap:wrap; z-index:9999;
    `;

    // ── Row 1 ──────────────────────────────────────────────────────────────
    const row1 = document.createElement("div");
    row1.style.cssText = "display:flex;align-items:center;gap:8px;width:100%;flex-wrap:wrap;";

    // Mode toggle
    const modeWrap = document.createElement("span");
    modeWrap.style.cssText = "display:inline-flex;gap:4px;";

    const btnIntiface = document.createElement("button");
    btnIntiface.textContent = "Intiface";
    btnIntiface.className   = `${PLUGIN_ID}-mode-btn`;

    const btnHandy = document.createElement("button");
    btnHandy.textContent = "The Handy";
    btnHandy.className   = `${PLUGIN_ID}-mode-btn`;


    btnIntiface.addEventListener("click", () => {
      if (mode === "intiface") return;
      mode          = "intiface";
      intifaceReady = false;
      statusData    = { connected: false, playing: false };
      saveSettingsToStorage();
      log("Mode switched to intiface");
      sendMsg({ type: "setMode", mode: "intiface" });
      updateModeButtons();
      updateToolbarStatus();
    });

    btnHandy.addEventListener("click", () => {
      if (mode === "handy_wifi") return;
      mode          = "handy_wifi";
      intifaceReady = false;
      statusData    = { connected: false, playing: false };
      saveSettingsToStorage();
      log("Mode switched to handy_wifi");
      sendMsg({ type: "setMode", mode: "handy_wifi" });
      updateModeButtons();
      updateToolbarStatus();
    });

    modeWrap.appendChild(btnIntiface);
    modeWrap.appendChild(btnHandy);
    row1.appendChild(modeWrap);

    // Funscript selector
    const selectEl = document.createElement("select");
    selectEl.id    = `${PLUGIN_ID}-select`;
    selectEl.style.cssText = "background:#222;color:#fff;border:1px solid #555;" +
                             "border-radius:3px;padding:2px 4px;font-size:11px;max-width:300px;";
    selectEl.addEventListener("change", () => {
      if (selectEl.value) loadFunscript(selectEl.value);
    });
    row1.appendChild(selectEl);

    row1.appendChild(buildOffsetInput());
    row1.appendChild(buildStrokeRange());

    // Invert
    const invertBtn = document.createElement("button");
    invertBtn.id = `${PLUGIN_ID}-invert`;
    function updateInvertBtn() {
      invertBtn.textContent = invert ? "⇅ Invert: ON" : "⇅ Invert: OFF";
      invertBtn.style.cssText = buttonStyle("#333");
      invertBtn.classList.toggle("active", invert);
    }
    updateInvertBtn();
    invertBtn.addEventListener("click", () => {
      invert = !invert;
      updateInvertBtn();
      sendSettings();

      localStorage.setItem("intifaceSync.invert", invert);
      log(`Invert toggled: ${invert}`, "debug");

      if (mode === "handy_wifi" && selectedFunscript) {
        statusEl.textContent = "inverting script...";
        statusEl.style.color = "#fc0";
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "loadFile", path: selectedFunscript, force: true, invert }));
        } else {
          log("WS not connected", "error");
        }
      }
    });
    row1.appendChild(invertBtn);

    // Connect (Intiface only)
    const connectBtn = document.createElement("button");
    connectBtn.id         = `${PLUGIN_ID}-connect-btn`;
    connectBtn.textContent  = "Connect";
    connectBtn.addEventListener("click", () => {
      intifaceReady        = false;
      connectingToIntiface = false;
      loadPluginConfig().then((cfg) => {
        const url = cfg?.intifaceUrl || "ws://localhost:12345";
        log(`Connecting to Intiface: ${url}`);
        sendMsg({ type: "connect", url });
        if (currentScenePath) pendingFindFunscripts = { videoPath: currentScenePath };
      });
    });
    row1.appendChild(connectBtn);

    // Stop
    const stopBtn = document.createElement("button");
    stopBtn.textContent   = "Stop";
    stopBtn.id = `${PLUGIN_ID}-stop`;
    stopBtn.addEventListener("click", () => {
      log("Stop button clicked", "debug");
      sendMsg({ type: "stop" });
    });
    row1.appendChild(stopBtn);

    // Spacer
    const spacer = document.createElement("span");
    spacer.style.cssText = "flex:1 1 auto;";
    row1.appendChild(spacer);

    // Status (with Ellipsis + Tooltip)
    const statusEl = document.createElement("span");
    statusEl.id    = `${PLUGIN_ID}-status`;
    statusEl.style.cssText =
      "opacity:0.8; flex:0 1 auto; min-width:0; " +
      "overflow:hidden; text-overflow:ellipsis; " +
      "white-space:nowrap; text-align:right;";
    row1.appendChild(statusEl);


    bar.appendChild(row1);

    // ── Row 2: Handy Panel (visible only in mobile mode) ───────────────────
    const handyPanel = buildHandyPanel();

    function updateModeButtons() {
      btnIntiface.classList.toggle("active", mode === "intiface");
      btnHandy   .classList.toggle("active", mode === "handy_wifi");

      handyPanel.style.display = mode === "handy_wifi" ? "flex" : "none";
      connectBtn.style.display = mode === "intiface"   ? ""     : "none";
    }


    bar.appendChild(handyPanel);

    // Set mode buttons to their default values
    updateModeButtons();

    return bar;
  }

  // ── Status Display ─────────────────────────────────────────────────────────
  function updateToolbarStatus() {
    if (eventInfoActive) return;
    const statusEl = document.getElementById(`${PLUGIN_ID}-status`);
    if (!statusEl) return;

    if (!wsReady) {
      statusEl.textContent = `⚠ Backend unreachable (${BACKEND_URL})`;
      statusEl.style.color = "#f90";
      statusEl.title = statusEl.textContent;
      return;
    }

    if (mode === "intiface") {
      const { connected, playing, devices, error } = statusData;
      const devNames = (devices || []).map(d => d.name).join(", ") || "–";
      if (error)           { statusEl.textContent = `⚠ ${error}`;              statusEl.style.color = "#f90"; }
      else if (!connected) { statusEl.textContent = "● Intiface: disconnected"; statusEl.style.color = "#f44"; }
      else if (playing)    { statusEl.textContent = `▶ ${devNames}`;            statusEl.style.color = "#4f4"; }
      else                 { statusEl.textContent = `■ ${devNames}`;            statusEl.style.color = "#aaa"; }
    } else {
      const { connected, playing, error } = statusData;
      if (error)           { statusEl.textContent = `⚠ ${error}`;              statusEl.style.color = "#f90"; }
      else if (!connected) { statusEl.textContent = "● Handy: disconnected";   statusEl.style.color = "#f44"; }
      else if (playing)    { statusEl.textContent = "▶ Handy: playing";         statusEl.style.color = "#4f4"; }
      else                 { statusEl.textContent = "■ Handy: connected";       statusEl.style.color = "#aaa"; }
    }
    statusEl.title = statusEl.textContent;
  }

  function updateFunscriptSelector() {
    const sel = document.getElementById(`${PLUGIN_ID}-select`);
    if (!sel) return;
    sel.innerHTML = "";

    if (funscripts.length === 0) {
      const opt = document.createElement("option");
      opt.value = ""; opt.textContent = "No funscript found";
      sel.appendChild(opt);
      return;
    }

    funscripts.forEach((path) => {
      const opt = document.createElement("option");
      opt.value       = path;
      opt.textContent = path.split(/[\\/]/).pop();
      if (path === selectedFunscript) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  function injectToolbar() {
    const existing = document.getElementById(`${PLUGIN_ID}-toolbar`);
    if (existing) {
      existing.remove();
      toolbarInjected = false;
    }
    if (toolbarInjected) return;
    const player = document.querySelector(".VideoPlayer, .video-player, #player");
    if (!player) return;
    player.appendChild(buildToolbar());
    toolbarInjected = true;
    updateToolbarStatus();
    updateFunscriptSelector();
    log("Toolbar injected", "debug");
  }


  function retryInjectToolbar(attempts = 0) {
    injectToolbar();
    const video = document.querySelector("video");
    if (video) attachVideoEvents(video);
    if ((!toolbarInjected || !video) && attempts < 20) {
      setTimeout(() => retryInjectToolbar(attempts + 1), 500);
    }
  }

  // ── Scene detection ────────────────────────────────────────────────────────
  function getSceneIdFromUrl() {
    const m = window.location.pathname.match(/\/scenes\/(\d+)/);
    return m ? m[1] : null;
  }

  async function onSceneLoad(sceneId) {
    log(`Scene loaded: ${sceneId}`);
    funscriptLoaded       = false;
    pendingPlay           = null;
    pendingFindFunscripts = null;
    funscripts            = [];
    selectedFunscript     = null;

    const scene = await getSceneDetails(sceneId);
    if (!scene) return;

    const videoPath  = scene.files?.[0]?.path ?? null;
    currentScenePath = videoPath;

    if (videoPath) {
      if (intifaceReady) sendMsg({ type: "findFunscripts", videoPath });
      else               pendingFindFunscripts = { videoPath };
    }

    toolbarInjected = false;
    retryInjectToolbar();
  }

  // ── Navigation ─────────────────────────────────────────────────────────────
  let lastUrl = location.href;
  let lastSceneId = null;

  function watchNavigation() {
    const observer = new MutationObserver(() => {
      if (location.href !== lastUrl) { lastUrl = location.href; onUrlChange(); }
    });
    observer.observe(document.body, { childList: true, subtree: true });

    const origPush    = history.pushState.bind(history);
    const origReplace = history.replaceState.bind(history);
    history.pushState    = (...a) => { origPush(...a);    onUrlChange(); };
    history.replaceState = (...a) => { origReplace(...a); onUrlChange(); };
    window.addEventListener("popstate", onUrlChange);
  }

  function onUrlChange() {
    const sceneId = getSceneIdFromUrl();
    if (!sceneId) {
      lastSceneId = null;
      return;
    }
    if (sceneId === lastSceneId) return;
    lastSceneId = sceneId;

    const old = document.getElementById(`${PLUGIN_ID}-toolbar`);
    if (old) old.remove();
    toolbarInjected       = false;
    videoEl               = null;
    funscripts            = [];
    selectedFunscript     = null;
    funscriptLoaded       = false;
    pendingPlay           = null;
    pendingFindFunscripts = null;
    onSceneLoad(sceneId);
  }

  // ── Init ───────────────────────────────────────────────────────────────────
  function init() {
    log(`Plugin initialized (backend: ${BACKEND_URL})`);
    loadSettingsFromStorage();
    connectBackend();
    watchNavigation();
    const sceneId = getSceneIdFromUrl();
    if (sceneId) {
      lastSceneId = sceneId;
      onSceneLoad(sceneId);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Close tab → Send stop
  window.addEventListener("beforeunload", () => {
    if (ws && wsReady) {
      log("Tab closing, sending stop", "debug");
      ws.send(JSON.stringify({ type: "stop" }));
      ws.close();
    }
  });

})();
