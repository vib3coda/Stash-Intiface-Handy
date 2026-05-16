# IntifaceSync

A [Stash](https://github.com/stashapp/stash) plugin that syncs funscript files with your toys via **[Intiface Central](https://intiface.com/central/)** (Buttplug.io) or **[The Handy](https://www.thehandy.com/)** over WiFi (HSSP).

---

## Features

- 🎯 **Auto-detect funscripts** — automatically loads the funscript matching the scene filename
- 📂 **Manual funscript selection** — all `.funscript` files in the scene's folder are listed in a dropdown; the script matching the scene name is selected by default
- ▶️ **Play / Pause / Seek sync** — toy follows the video player in real time
- 🎚️ **Stroke range** — limit minimum and maximum stroke depth (per device)
- ⏱️ **Offset** — fine-tune timing (ms) to compensate for device or network latency
- 🔄 **Invert** — flip the script direction
- 🔌 **Dual mode** — switch between Intiface Central and The Handy (WiFi) on the fly
- 🌐 **Built-in tunnel** — for Handy mode, a `localhost.run` SSH tunnel is started automatically so The Handy can fetch the script from your local machine

---

## Requirements

- **Stash** (recent version with plugin support)
- **Python 3.8+** (for the backend)
- **Intiface Central** running locally or on your network (only for Intiface mode)
- **The Handy** connection key (only for Handy WiFi mode)
- **SSH client** (`ssh`, available by default on Linux/macOS; on Windows via OpenSSH or Git Bash) — required for Handy mode tunneling

### Python dependencies

The backend will install these automatically on first run if missing:

- `websockets`
- `aiohttp`

---

## Installation

### Via Plugin Source (recommended)

1. In Stash go to **Settings → Plugins**.
2. Click **Sources → Add Source**.
3. Enter:
   - **Name:** `IntifaceSync`
   - **URL:** https://raw.githubusercontent.com/vib3coda/Stash-Intiface-Handy/main/index.yml
4. Install **IntifaceSync** from the **Available** tab.
5. Scroll to the plugin and enter your **Intiface WebSocket IP** if using Intiface mode.
6. Open any scene that has a funscript — the toolbar appears and sync starts automatically.

### Manual Installation

1. Copy the `IntifaceSync` folder into your Stash `plugins` directory.
2. In Stash go to **Settings → Plugins** and click **Reload Plugins**.
3. Open **Settings → Plugins → IntifaceSync** and enter your **Intiface WebSocket IP** if using Intiface mode.
4. Open any scene that has a funscript — the toolbar appears and sync starts automatically.

---

## Usage

1. **Start the backend** — go to **Settings → Tasks**, scroll to the bottom, and run the **Start Backend** task. The backend listens on port `7880`. (maybe you need to open port 7880 in your firewall or docker settings)
2. **Open a scene** — the IntifaceSync toolbar appears below the video player.
3. **Choose a mode** — click **Intiface** or **The Handy** in the toolbar.
   - For Handy mode, enter your **connection key** when prompted.
4. **Click Connect** — the plugin connects to your device.
5. **Play the scene** — playback, pause, and seek are mirrored to the device.
6. **Scene change** — when you switch scenes, the matching funscript is loaded automatically.

### Toolbar controls

| Control | Function |
|---|---|
| Mode (Intiface / The Handy) | Switch backend mode |
| Connect / Stop | Connect or disconnect the device |
| Funscript dropdown | Select which `.funscript` to use |
| Offset | Timing offset in milliseconds (negative = earlier) |
| Stroke Range | Min/max stroke depth (0–100 %) |
| Invert | Flip script direction |
| ⚙ Settings | Open settings popup |

Settings are persisted in `localStorage`.

---

## Debug logging

### Backend

Create an empty file named `debug` in the plugin folder:

```bash
touch /path/to/stash/plugins/IntifaceSync/debug
```

Restart the backend (re-run **Start Backend** task). Logs go to:

```
/tmp/intiface_sync.log
```

### Frontend

Open the browser DevTools console and run:

```js
localStorage.setItem("intifaceSyncDebug", "1");
```

Reload the page. To disable:

```js
localStorage.removeItem("intifaceSyncDebug");
```

---

## How it works

- **Intiface mode** — the backend connects to Intiface Central via WebSocket (Buttplug protocol v3), parses the selected funscript, and streams `LinearCmd` messages to all connected linear devices at ~50 Hz with look-ahead scheduling.
- **Handy WiFi mode** — the backend
  1. starts a local HTTP server serving the selected funscript,
  2. opens an SSH tunnel via `localhost.run` to expose it publicly,
  3. uploads the script URL + SHA-256 to The Handy via the HSSP API,
  4. drives play / pause / seek through the Handy v2 REST API with server-time synchronization.

---

## Troubleshooting

If something doesn't work, please collect logs **before** opening an issue.

---

### Common issues

- **Toolbar doesn't appear** — make sure the backend is running, port `7880` is open (check your firewall and/or docker settings) and reload the scene page.
- **No devices found (Intiface)** — verify Intiface Central is running, the server is started, and your toy is connected and listed as a *linear* device.
- **Handy upload fails** — check that `ssh` is installed (should install automatically) and the connection key is correct. Check `/tmp/intiface_sync.log` for tunnel errors.
- **Sync drifts** — adjust the **Offset** value in the toolbar.

---

## Reporting bugs

Please include:

1. Stash version
2. Browser + OS
3. Browser console output (with debug logging enabled)
4. `intiface_sync.log` contents (see /tmp foldere)
5. A short description of what you did and what happened

---

## Privacy & Security

- **Intiface mode** — no credentials involved. The backend connects directly to your local Intiface Central instance via WebSocket. Nothing leaves your machine.
- **Handy WiFi mode** — your connection key is entered in the browser toolbar and sent only to the local Python backend via the WebSocket on port `7880`. From there it is used exclusively to communicate with the official [Handy API](https://www.handyfeeling.com/api/handy/v2). It is **not** stored on disk.
- The funscript is served temporarily over a `localhost.run` SSH tunnel so The Handy can fetch it. The tunnel is only active while the script is being uploaded and is torn down automatically. The tunnel URL is a random subdomain and is never shared or logged.
- The WebSocket backend binds to `0.0.0.0:7880` — if you are on a shared or untrusted network, consider firewalling this port.

---

## License

MIT — see `LICENSE`.

---

## Author

**vib3coda** — https://github.com/vib3coda/Stash-Intiface-Handy

---

## Disclaimer

This is an unofficial, community-made plugin. Not affiliated with or endorsed by TheHandy or Intiface.
This plugin was largely created with the help of AI (vibecoded). I've tested it thoroughly on my end and it works well, but any feedback or code improvements from experienced devs are highly welcome!

This plugin is provided as-is, without any warranty. The author is not responsible for any damage to devices, data loss, or any other issues arising from the use of this software. Use at your own risk.

