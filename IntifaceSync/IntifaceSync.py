#!/usr/bin/env python3
"""IntifaceSync Backend – Intiface Central + The Handy WiFi (HSSP) mode."""

import sys
import os
import json
import asyncio
import logging
import tempfile
import time
import signal
import subprocess
import hashlib
import threading
import re
import socket
import select
from logging.handlers import RotatingFileHandler
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG = os.path.exists(os.path.join(PLUGIN_DIR, "debug"))
LOG_FILE = os.path.join(PLUGIN_DIR, "intiface_sync.log")


LEVEL_PREFIX = {
    logging.DEBUG:    "\x02",  # Debug
    logging.INFO:     "\x03",  # Info
    logging.WARNING:  "\x04",  # Warning
    logging.ERROR:    "\x05",  # Error
    logging.CRITICAL: "\x05",  # Critical
}


class StashFormatter(logging.Formatter):
    def format(self, record):
        prefix = LEVEL_PREFIX.get(record.levelno, "\x03")
        msg = super().format(record)
        return "\n".join(prefix + line for line in msg.splitlines())


class StashHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            super().emit(record)
            self.flush()
        except Exception:
            self.handleError(record)


stash_handler = StashHandler(sys.stdout)
stash_handler.setFormatter(StashFormatter("[IntifaceSync] %(message)s"))

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=500_000, backupCount=1, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

root = logging.getLogger()
root.handlers.clear()
root.addHandler(stash_handler)
root.addHandler(file_handler)
root.setLevel(logging.DEBUG if DEBUG else logging.INFO)

log = logging.getLogger("IntifaceSync")


def log_debug(msg):
    log.debug(msg)



try:
    import websockets
    from websockets.asyncio.server import serve as ws_serve
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "--quiet"])
    except subprocess.CalledProcessError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "websockets", "--quiet", "--break-system-packages"])
    import websockets
    try:
        from websockets.asyncio.server import serve as ws_serve
    except ImportError:
        from websockets.server import serve as ws_serve

try:
    import aiohttp
except ImportError:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "aiohttp", "--quiet"])
    except subprocess.CalledProcessError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "aiohttp", "--quiet", "--break-system-packages"])
    import aiohttp

BACKEND_PORT      = 7880
BACKEND_HOST      = "0.0.0.0"
FUNSCRIPT_PORT    = 7881
LOCK_FILE         = os.path.join(tempfile.gettempdir(), "intiface_sync.lock")
BUTTPLUG_CLIENT   = "IntifaceSync/Stash"
BUTTPLUG_MSG_VER  = 3
SEND_INTERVAL_MS  = 20
MIN_DURATION_MS   = 30
DISCONNECT_WAIT_S = 0.5
LOOKAHEAD_MS      = 40
HANDY_API_BASE    = "https://www.handyfeeling.com/api/handy/v2"
SSH_KEY_PATH = os.path.join(PLUGIN_DIR, "tunnel_key")


# ─── SSH / Tunnel ────────────────────────────────────────────────────────────

def ensure_ssh() -> bool:
    """Install SSH if it is not already installed."""
    try:
        subprocess.run(["ssh", "-V"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        log.info("SSH not found, installing...")
        for cmd in [
            ["apk", "add", "openssh-client", "--no-cache"],
            ["apt-get", "install", "-y", "openssh-client"],
        ]:
            try:
                subprocess.run(cmd, capture_output=True, check=True)
                log.info("SSH installed.")
                return True
            except Exception:
                continue
        log.error("Failed to install SSH.")
        return False


def ensure_ssh_key() -> bool:
    """Create an SSH key if one does not already exist."""
    if os.path.exists(SSH_KEY_PATH):
        return True
    try:
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", SSH_KEY_PATH, "-N", ""],
            capture_output=True, check=True
        )
        log.info(f"SSH key created: {SSH_KEY_PATH}")
        return True
    except Exception as e:
        log.error(f"Failed to create SSH key: {e}")
        return False


class TunnelManager:

    def __init__(self):
        self._proc    = None
        self._url     = None
        self._lock    = asyncio.Lock()
        self._handy_connected = False
        self._handy_playing = False

    @property
    def url(self) -> str | None:
        return self._url

    async def start(self, local_port: int) -> str | None:
        async with self._lock:
            if self._proc and self._proc.returncode is None:
                return self._url

            if not ensure_ssh():
                return None
            if not ensure_ssh_key():
                return None

            log.info(f"Starting tunnel for port {local_port}...")
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    "ssh",
                    "-i", SSH_KEY_PATH,
                    "-o", "StrictHostKeyChecking=no",
                    "-o", "ServerAliveInterval=30",
                    "-o", "ServerAliveCountMax=3",
                    "-R", f"80:localhost:{local_port}",
                    "nokey@localhost.run",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )

                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        line = await asyncio.wait_for(
                            self._proc.stdout.readline(), timeout=2.0
                        )
                        line = line.decode("utf-8", errors="replace").strip()
                        log_debug(f"Tunnel raw: {line!r}")
                        if "tunneled" in line and "https://" in line:
                            for part in line.split():
                                part = part.rstrip(".,")
                                if part.startswith("https://"):
                                    self._url = part
                                    log.info(f"Tunnel established: {self._url}")
                                    return self._url
                    except asyncio.TimeoutError:
                        continue

                log.error("Tunnel started but no URL received within timeout.")
                return None

            except Exception as e:
                log.error(f"Failed to start tunnel: {e}")
                return None

    async def stop(self) -> None:
        async with self._lock:
            had_proc = self._proc is not None
            self._url = None
            if self._proc and self._proc.returncode is None:
                try:
                    self._proc.terminate()
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self._proc = None
            if had_proc:
                log.info("Tunnel stopped.")


# ─── Funscript HTTP Server ────────────────────────────────────────────────────

class FunscriptHandler(BaseHTTPRequestHandler):

    funscript_path: str | None = None

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self.__class__.funscript_path
        if not path or not os.path.isfile(path):
            self.send_error(404, "No funscript loaded")
            return
        if not path.lower().endswith(".funscript"):
            self.send_error(403, "Forbidden")
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            log.error(f"Failed to serve funscript {path!r}: {e}")
            self.send_error(500, str(e))

class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        super().server_bind()


class FunscriptServer:

    def __init__(self, port: int = FUNSCRIPT_PORT):
        self._port   = port
        self._server = None
        self._thread = None

    def serve(self, funscript_path: str) -> None:
        FunscriptHandler.funscript_path = funscript_path
        if self._server is None:
            try:
                self._server = ReusableHTTPServer(("0.0.0.0", self._port), FunscriptHandler)
            except OSError as e:
                log.error(f"Failed to bind funscript server port {self._port}: {e}")
                raise
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            log.info(f"Funscript HTTP server listening on port {self._port} ({funscript_path})")
        else:
            log_debug(f"Funscript server updated: {funscript_path}")

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None
            log.info("Funscript HTTP server stopped.")


# ─── The Handy API ───────────────────────────────────────────────────────────

class HandyClient:

    def __init__(self, connection_key: str):
        self.key      = connection_key
        self._session = None

    def _headers(self) -> dict:
        return {"X-Connection-Key": self.key, "Content-Type": "application/json"}

    async def set_slide(self, min_pos: float, max_pos: float) -> dict:
        """min_pos/max_pos: 0.0–1.0"""
        return await self._put("/slide", {
            "min": round(min_pos * 100),
            "max": round(max_pos * 100),
        })

    async def _session_get(self):
        if self._session is None or self._session.closed:
            log_debug(f"Creating new aiohttp session for Handy API")
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get(self, path: str) -> dict:
        s = await self._session_get()
        async with s.get(f"{HANDY_API_BASE}{path}", headers=self._headers()) as r:
            return await r.json()

    async def _put(self, path: str, body: dict) -> dict:
        s = await self._session_get()
        log_debug(f"Handy PUT {path} body={body}")
        try:
            async with s.put(f"{HANDY_API_BASE}{path}", headers=self._headers(), json=body) as r:
                return await r.json()
        except Exception as e:
            log.error(f"Handy PUT {path} failed: {type(e).__name__}: {e}")
            raise

    async def is_connected(self) -> bool:
        try:
            result = await self._get("/info")
            log_debug(f"Handy /info response: {result}")
            return bool(result.get("sessionId"))
        except Exception as e:
            log.warning(f"Handy connection check failed: {e}")
            return False

    async def setup_hssp(self, script_url: str, sha256: str) -> dict:
        return await self._put("/hssp/setup", {
            "url":    script_url,
            "sha256": sha256,
        })

    async def play(self, start_time_ms: float = 0) -> dict:
        # Servertime for Sync
        server_time = await self._get_server_time()
        return await self._put("/hssp/play", {
            "estimatedServerTime": server_time,
            "startTime":           int(start_time_ms),
        })

    async def pause(self) -> dict:
        return await self._put("/hssp/stop", {})

    async def seek(self, time_ms: float, resume: bool = False) -> None:
        await self.pause()
        await asyncio.sleep(0.1)
        if resume:
            await self.play(time_ms)

    async def _get_server_time(self) -> int:
        """Estimate server time (round-trip / 2)."""
        try:
            t0 = int(time.time() * 1000)
            result = await self._get("/servertime")
            t1 = int(time.time() * 1000)
            server_time = result.get("serverTime", t0)
            rtd = (t1 - t0) // 2
            return server_time + rtd
        except Exception as e:
            log.warning(f"Failed to fetch Handy server time, using local: {e}")
            return int(time.time() * 1000)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ─── Buttplug (Intiface) ─────────────────────────────────────────────────────

class ButtplugClient:

    def __init__(self, url: str):
        self.url                  = url
        self.ws                   = None
        self.devices              = {}
        self._msg_id              = 1
        self._connected           = False
        self._listeners           = {}
        self._linear_device_cache = None

    def _next_id(self) -> int:
        mid = self._msg_id
        self._msg_id += 1
        return mid

    def _wrap(self, msg_type: str, payload: dict) -> str:
        payload["Id"] = self._next_id()
        return json.dumps([{msg_type: payload}])

    def _is_ws_open(self) -> bool:
        if self.ws is None:
            return False
        try:
            return self.ws.state.name == "OPEN"
        except AttributeError:
            try:
                return not self.ws.closed
            except AttributeError:
                return False

    async def _send(self, msg_type: str, payload: dict) -> None:
        if not self._is_ws_open():
            return
        try:
            await self.ws.send(self._wrap(msg_type, payload))
        except Exception as e:
            log.warning(f"Buttplug send failed ({msg_type}): {e}")

    async def _recv_loop(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    for msg_obj in json.loads(raw):
                        for msg_type, payload in msg_obj.items():
                            await self._dispatch(msg_type, payload)
                except Exception as e:
                    log.warning(f"Failed to parse Buttplug message: {e}")
        except websockets.exceptions.ConnectionClosed:
            log.info("Disconnected from Intiface.")
            self._connected = False
        except Exception as e:
            log.warning(f"Buttplug receive loop ended: {e}")
            self._connected = False

    async def _dispatch(self, msg_type: str, payload: dict) -> None:
        if msg_type == "DeviceAdded":
            idx = payload["DeviceIndex"]
            self.devices[idx] = payload
            self._linear_device_cache = None
            log.info(f"Device connected: [{idx}] {payload.get('DeviceName', '?')}")
        elif msg_type == "DeviceRemoved":
            idx = payload["DeviceIndex"]
            name = self.devices.pop(idx, {}).get("DeviceName", "?")
            self._linear_device_cache = None
            log.info(f"Device disconnected: [{idx}] {name}")
        elif msg_type == "Error":
            log.warning(f"Buttplug error: {payload}")
        for cb in self._listeners.get(msg_type, []):
            try:
                await cb(payload)
            except Exception as e:
                log.warning(f"Listener error ({msg_type}): {e}")

    def on(self, msg_type: str, callback) -> None:
        self._listeners.setdefault(msg_type, []).append(callback)

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> bool:
        try:
            self.ws = await websockets.connect(self.url)
            await self._send("RequestServerInfo", {
                "ClientName":     BUTTPLUG_CLIENT,
                "MessageVersion": BUTTPLUG_MSG_VER,
            })
            for msg_obj in json.loads(await self.ws.recv()):
                if "ServerInfo" in msg_obj:
                    log.info(f"Connected to Intiface: {msg_obj['ServerInfo'].get('ServerName', '?')}")
                    self._connected = True
                elif "Error" in msg_obj:
                    log.error(f"Intiface handshake error: {msg_obj['Error']}")
                    return False

            if not self._connected:
                return False

            await self._send("RequestDeviceList", {})
            for msg_obj in json.loads(await self.ws.recv()):
                if "DeviceList" in msg_obj:
                    for dev in msg_obj["DeviceList"].get("Devices", []):
                        self.devices[dev["DeviceIndex"]] = dev
                        log.info(f"Device found: [{dev['DeviceIndex']}] {dev.get('DeviceName', '?')}")
            self._linear_device_cache = None

            await self._send("StartScanning", {})
            asyncio.ensure_future(self._recv_loop())
            return True

        except Exception as e:
            log.error(f"Failed to connect to Intiface ({self.url}): {e}")
            return False

    async def disconnect(self) -> None:
        was_connected = self._connected
        self._connected = False
        self._linear_device_cache = None
        if self.ws is not None:
            if self._is_ws_open():
                try:
                    await self.stop_all()
                except Exception:
                    pass
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        await asyncio.sleep(DISCONNECT_WAIT_S)
        if was_connected:
            log.info("Intiface disconnected.")

    async def stop_all(self) -> None:
        await self._send("StopAllDevices", {})

    async def linear(self, device_index: int, duration_ms: int, position: float) -> None:
        position = max(0.0, min(1.0, position))
        await self._send("LinearCmd", {
            "DeviceIndex": device_index,
            "Vectors": [{"Index": 0, "Duration": int(duration_ms), "Position": position}],
        })

    def linear_devices(self) -> list:
        if self._linear_device_cache is not None:
            return self._linear_device_cache
        result = [
            idx for idx, dev in self.devices.items()
            if isinstance(dev.get("DeviceMessages", {}), dict)
            and "LinearCmd" in dev.get("DeviceMessages", {})
        ]
        if not result and self.devices:
            log.debug(f"No linear devices found. All devices: {list(self.devices.keys())}")
        self._linear_device_cache = result
        return result


# ─── Funscript Player (Intiface Modus) ───────────────────────────────────────

class FunscriptPlayer:

    def __init__(self, buttplug: ButtplugClient):
        self.bp                = buttplug
        self.actions           = []
        self.playing           = False
        self.offset_ms         = 0
        self.stroke_min        = 0.0
        self.stroke_max        = 1.0
        self.invert            = False
        self._play_start_wall  = None
        self._play_start_media = None
        self._task             = None
        self._last_sent_idx    = -1
        self._last_log_s       = 0

    def apply_settings(self, offset_ms=None, stroke_min=None, stroke_max=None, invert=None):
        if offset_ms  is not None: self.offset_ms  = int(offset_ms)
        if stroke_min is not None: self.stroke_min = float(stroke_min)
        if stroke_max is not None: self.stroke_max = float(stroke_max)
        if invert     is not None: self.invert     = bool(invert)
        log.info(f"Settings: offset={self.offset_ms}ms "
                 f"range=[{self.stroke_min:.2f},{self.stroke_max:.2f}] invert={self.invert}")

    def _map_position(self, raw: float) -> float:
        pos = self.stroke_min + raw * (self.stroke_max - self.stroke_min)
        if self.invert:
            pos = self.stroke_min + self.stroke_max - pos
        return max(0.0, min(1.0, pos))

    def load(self, actions: list) -> None:
        self.actions = sorted(actions, key=lambda a: a["at"])
        self._last_sent_idx = -1
        if self.actions:
            log.info(f"Funscript loaded: {len(self.actions)} keyframes "
                     f"({self.actions[0]['at']}ms – {self.actions[-1]['at']}ms)")
        else:
            log.warning("Funscript loaded but empty.")

    def play(self, media_time_ms: float) -> None:
        self._play_start_wall  = time.monotonic()
        self._play_start_media = media_time_ms
        self._reset_index_for_time(media_time_ms + self.offset_ms)
        self.playing = True
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._loop())
        log_debug(f"Play @ {media_time_ms:.0f}ms ({len(self.actions)} keyframes)")

    def pause(self) -> None:
        self.playing        = False
        self._last_sent_idx = -1
        asyncio.ensure_future(self.bp.stop_all())
        log_debug(f"Paused")

    def seek(self, media_time_ms: float) -> None:
        self._play_start_wall  = time.monotonic()
        self._play_start_media = media_time_ms
        self._reset_index_for_time(media_time_ms + self.offset_ms)
        log_debug(f"Seek @ {media_time_ms:.0f}ms")

    def stop(self) -> None:
        self.playing        = False
        self._last_sent_idx = -1
        if self._task and not self._task.done():
            self._task.cancel()
        asyncio.ensure_future(self.bp.stop_all())

    def _current_media_ms(self) -> float:
        if self._play_start_wall is None:
            return 0.0
        return self._play_start_media + (time.monotonic() - self._play_start_wall) * 1000.0

    def _find_next_keyframe_idx(self, t_ms: float) -> int | None:
        actions = self.actions
        if not actions:
            return None
        lo, hi = 0, len(actions)
        while lo < hi:
            mid = (lo + hi) // 2
            if actions[mid]["at"] < t_ms:
                lo = mid + 1
            else:
                hi = mid
        return lo if lo < len(actions) else None

    def _reset_index_for_time(self, t_ms: float) -> None:
        nxt = self._find_next_keyframe_idx(t_ms)
        if nxt is None:
            self._last_sent_idx = len(self.actions) - 1
        else:
            self._last_sent_idx = nxt - 1

    async def _loop(self) -> None:
        interval_s = SEND_INTERVAL_MS / 1000.0
        while True:
            await asyncio.sleep(interval_s)
            if not self.playing or not self.actions:
                continue

            now_ms  = self._current_media_ms() + self.offset_ms
            devices = self.bp.linear_devices()
            if not devices:
                continue

            last_kf       = None
            last_duration = 0

            while self._last_sent_idx + 1 < len(self.actions):
                kf_idx = self._last_sent_idx + 1
                kf     = self.actions[kf_idx]
                if kf["at"] > now_ms + LOOKAHEAD_MS:
                    break

                if kf_idx + 1 < len(self.actions):
                    next_kf  = self.actions[kf_idx + 1]
                    duration = max(MIN_DURATION_MS, int(next_kf["at"] - now_ms))
                else:
                    duration = MIN_DURATION_MS

                kf_pos = self._map_position(kf["pos"] / 100.0)

                for idx in devices:
                    try:
                        await self.bp.linear(idx, duration, kf_pos)
                    except Exception as e:
                        log.warning(f"LinearCmd failed (device {idx}): {e}")

                self._last_sent_idx += 1
                last_kf       = kf
                last_duration = duration

            if last_kf is not None:
                now_s = time.monotonic()
                if now_s - self._last_log_s > 10.0:
                    self._last_log_s = now_s
                    log.debug(f"Loop: now={now_ms:.0f}ms kf={last_kf['at']:.0f}ms "
                              f"pos={self._map_position(last_kf['pos']/100.0):.2f} "
                              f"dur={last_duration}ms devices={devices}")


# ─── Utility ─────────────────────────────────────────────────────────────────

def find_funscripts(video_path: str) -> tuple[list, str | None]:
    if not video_path:
        return [], None
    try:
        video_path = unquote(video_path)
    except Exception:
        pass

    video_dir  = os.path.dirname(video_path)
    video_name = os.path.splitext(os.path.basename(video_path))[0]

    if not video_dir or not os.path.isdir(video_dir):
        log.warning(f"Invalid video directory: {video_dir!r}")
        return [], None

    try:
        all_files = os.listdir(video_dir)
    except Exception as e:
        log.error(f"Cannot read directory {video_dir!r}: {e}")
        return [], None

    funscripts = sorted([
        os.path.join(video_dir, f)
        for f in all_files
        if f.lower().endswith(".funscript")
    ])

    if not funscripts:
        log_debug(f"No funscripts found in {video_dir!r}")
        return [], None

    default_script = next(
        (fs for fs in funscripts
         if os.path.splitext(os.path.basename(fs))[0].lower() == video_name.lower()),
        funscripts[0]
    )

    if funscripts[0] != default_script:
        funscripts.remove(default_script)
        funscripts.insert(0, default_script)

    log.info(f"Found {len(funscripts)} funscript(s), default: {os.path.basename(default_script)}")
    return funscripts, default_script


def load_funscript_file(path: str, invert: bool = False) -> list | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        actions = data.get("actions", [])
        if invert:
            actions = [{**a, "pos": 100 - a.get("pos", 0)} for a in actions]
        log.info(f"Funscript read: {path} ({len(actions)} keyframes, invert={invert})")
        return actions
    except Exception as e:
        log.error(f"Failed to read funscript {path!r}: {e}")
        return None




# ─── Backend Server ───────────────────────────────────────────────────────────

class BackendServer:

    def __init__(self):
        self.clients          = set()
        self._mode            = "intiface"   # "intiface" | "handy_wifi"
        self._pending_actions = None
        self._current_script_path: str | None = None

        # Intiface
        self.bp               = None
        self.player           = None
        self._intiface_url    = "ws://localhost:12345"

        # Handy WiFi
        self._handy_key       = ""
        self._handy           = None
        self._handy_playing   = False
        self._handy_connected = False
        self._tunnel          = TunnelManager()
        self._fs_server       = FunscriptServer()
        self._tunnel_url      = None
        self._last_setup_path = None
        self._last_setup_tunnel = None

    # ── Status Broadcast ──────────────────────────────────────────────────────

    async def _broadcast_status(self, error: str = "") -> None:
        if not self.clients:
            return

        if self._mode == "intiface":
            await self._broadcast({
                "type":      "status",
                "mode":      "intiface",
                "connected": self.bp.connected if self.bp else False,
                "playing":   self.player.playing if self.player else False,
                "devices":   [
                    {"index": idx, "name": dev.get("DeviceName", "?")}
                    for idx, dev in (self.bp.devices.items() if self.bp else {})
                ],
                "error": error,
            })
        else:
            await self._broadcast({
                "type":       "status",
                "mode":       "handy_wifi",
                "connected":  self._handy_connected,
                "playing":    self._handy_playing,
                "tunnelUrl":  self._tunnel_url,
                "error":      error,
            })

    async def _broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        try:
            data = json.dumps(msg)
        except TypeError as e:
            log.error(f"Failed to serialize broadcast message: {e} | msg={msg!r}")
            return
        dead = set()
        for ws in self.clients:
            try:
                await ws.send(data)
            except Exception:
                dead.add(ws)
        self.clients -= dead

    async def _broadcast_event(self, level: str, message: str, **extra):
        payload = json.dumps({
            "type": "event",
            "level": level,
            "message": message,
            **extra,
        })
        for ws in list(self.clients):
            try:
                await ws.send(payload)
            except Exception:
                pass

    # ── Message Handler ───────────────────────────────────────────────────────

    async def _handle(self, ws, msg: dict) -> None:
        t = msg.get("type", "")
        log_debug(f"Message: {t} – {str(msg)[:300]}")

        # ── Modus wechseln ────────────────────────────────────────────────────
        if t == "setMode":
            new_mode = msg.get("mode", "intiface")
            if new_mode not in ("intiface", "handy_wifi"):
                await self._broadcast_status("Unknown mode")
                return
            await self._switch_mode(new_mode)
            return

        # ── Intiface: Connect ─────────────────────────────────────────────────
        if t == "connect":
            if self._mode != "intiface":
                log.info("connect received but mode != intiface → switching to intiface")
                await self._switch_mode("intiface")

            url = msg.get("url", self._intiface_url)
            self._intiface_url = url

            if self.player:
                self.player.stop()
            if self.bp is not None:
                try:
                    await self.bp.disconnect()
                except Exception as e:
                    log_debug(f"Disconnect before reconnect failed (ignored): {e}")

            self.bp     = ButtplugClient(url)
            self.player = FunscriptPlayer(self.bp)

            if self._pending_actions is not None:
                log.info(f"Loading buffered funscript ({len(self._pending_actions)} actions)")
                self.player.load(self._pending_actions)
                self._pending_actions = None

            async def on_device_change(_):
                await self._broadcast_status()
            self.bp.on("DeviceAdded",   on_device_change)
            self.bp.on("DeviceRemoved", on_device_change)

            ok = await self.bp.connect()
            await self._broadcast_status("" if ok else f"Failed to connect to {url}")
            return

        # ── Handy WiFi: Connect ───────────────────────────────────────────────
        if t == "connectHandy":
            if self._mode != "handy_wifi":
                log.info("connectHandy received but mode != handy_wifi → switching")
                await self._switch_mode("handy_wifi")

            key = msg.get("connectionKey", "").strip()
            if not key:
                await self._broadcast_status("No connection key provided")
                return

            self._handy_key = key
            if self._handy:
                await self._handy.close()

            self._handy = HandyClient(key)
            self._handy_connected = False

            result = await self._handy._get("/info")
            log_debug(f"Handy /info response: {result}")
            ok = bool(result.get("sessionId"))
            if not ok:
                log.warning("Handy not reachable – no sessionId in /info response")
                await self._handy.close()
                self._handy = None
                self._handy_connected = False
                await self._broadcast_status("Handy not reachable – check connection key and WiFi")
                return

            self._handy_connected = True
            log.info("Handy connected.")

            # start Tunnel
            await self._broadcast({"type": "info", "message": "Starting tunnel..."})
            self._tunnel_url = await self._tunnel.start(FUNSCRIPT_PORT)
            if not self._tunnel_url:
                await self._handy.close()
                self._handy = None
                self._handy_connected = False
                await self._broadcast_status("Failed to start tunnel")
                return

            # If Funscript is already loaded → set it up right away
            if self._current_script_path:
                await self._handy_setup_script(
                    self._current_script_path,
                    invert=getattr(self, "_current_invert", False),
                )

            await self._broadcast_status()
            return

        # ── Find Funscripts ─────────────────────────────────────────────────
        if t == "findFunscripts":
            video_path = msg.get("videoPath", "")
            files, default = find_funscripts(video_path)
            await self._broadcast({"type": "funscripts", "files": files, "default": default})
            return

        # ── Loading Funscript ───────────────────────────────────────────────────
        if t == "loadFile":
            path   = msg.get("path", "")
            force  = bool(msg.get("force", False))
            invert = bool(msg.get("invert", False))
            log.info(f"loadFile: path={path!r} force={force} invert={invert}")
            await self._load_script(path, force=force, invert=invert)
            return

        # ── Play ──────────────────────────────────────────────────────────────
        if t == "play":
            time_ms = float(msg.get("time", 0))
            if self._mode == "intiface":
                if self.player:
                    self.player.play(time_ms)
            else:
                await self._handy_play(time_ms)
            await self._broadcast_status()
            return

        # ── Pause ─────────────────────────────────────────────────────────────
        if t == "pause":
            if self._mode == "intiface":
                if self.player:
                    self.player.pause()
            else:
                await self._handy_pause()
            await self._broadcast_status()
            return

        # ── Seek ──────────────────────────────────────────────────────────────
        if t == "seek":
            time_ms = float(msg.get("time", 0))
            if self._mode == "intiface":
                if self.player:
                    self.player.seek(time_ms)
            else:
                if self._handy_playing:
                    await self._handy_seek(time_ms)
                elif self._handy and self._handy_connected:
                    await self._handy.pause()
                    await asyncio.sleep(0.1)
            return

        # ── Settings ───────────────────────────────────────────
        if t == "settings":
            if self._mode == "intiface":
                if self.player:
                    self.player.apply_settings(
                        offset_ms  = msg.get("offsetMs"),
                        stroke_min = msg.get("strokeMin"),
                        stroke_max = msg.get("strokeMax"),
                        invert     = msg.get("invert"),
                    )
            else:
                # Handy-Mode
                if msg.get("offsetMs") is not None:
                    self._handy_offset_ms = int(msg.get("offsetMs"))
                stroke_min = msg.get("strokeMin")
                stroke_max = msg.get("strokeMax")
                if (stroke_min is not None or stroke_max is not None) and self._handy:
                    mn = float(stroke_min) if stroke_min is not None else 0.0
                    mx = float(stroke_max) if stroke_max is not None else 1.0
                    await self._handy.set_slide(mn, mx)
            await self._broadcast_status()
            return

        # ── Stop ──────────────────────────────────────────────────────────────
        if t == "stop":
            if self._mode == "intiface":
                if self.player:
                    self.player.stop()
                if self.bp:
                    try:
                        await self.bp.disconnect()
                    except Exception:
                        pass
            else:
                await self._handy_pause()
                await self._tunnel.stop()
                self._tunnel_url = None
                if self._handy:
                    await self._handy.close()
                    self._handy = None
                    self._last_setup_path = None
                    self._last_setup_tunnel = None
                self._handy_connected = False
                self._handy_playing = False
            await self._broadcast_status()
            return

        # ── Status ────────────────────────────────────────────────────────────
        if t == "status":
            await self._broadcast_status()
            return
    # ── Switch Mode ────────────────────────────────────────────────────────

    async def _switch_mode(self, new_mode: str) -> None:
        if new_mode == self._mode:
            await self._broadcast_status()
            return

        log.info(f"Switching mode: {self._mode} → {new_mode}")

        if self._mode == "intiface":
            if self.player:
                self.player.stop()
            if self.bp:
                try:
                    await self.bp.disconnect()
                except Exception as e:
                    log_debug(f"Disconnect during mode switch failed (ignored): {e}")
        else:
            await self._handy_pause()
            await self._tunnel.stop()
            self._tunnel_url = None
            if self._handy:
                await self._handy.close()
                self._handy = None
                self._last_setup_path = None
                self._last_setup_tunnel = None
            # ← Reset Handy-State
            self._handy_connected = False
            self._handy_playing   = False

        self._mode = new_mode
        await self._broadcast_status()

    # ── Handy Helpfunctions ─────────────────────────────────────────────────

    async def _handy_setup_script(self, path: str, force: bool = False, invert: bool = False) -> bool:
        log_debug(f"_handy_setup_script: path={path} force={force} invert={invert}")
        if not self._handy or not self._tunnel_url:
            log.warning("Cannot setup HSSP script: Handy or tunnel not ready")
            return False
        if not force and getattr(self, "_last_setup_path", None) == path \
                and getattr(self, "_last_setup_tunnel", None) == self._tunnel_url \
                and getattr(self, "_last_setup_invert", None) == invert:  # invert check
            log_debug(f"HSSP setup skipped (already set): {path}")
            return True

        await self._broadcast_event("info", f"Script upload started: {os.path.basename(path)}")
        try:
            # Create a temporary inverted file if necessary
            if invert:
                actions = load_funscript_file(path, invert=True)
                if actions is None:
                    raise ValueError("Failed to load funscript for invert")
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                data["actions"] = actions
                tmp_path = path + ".inverted.funscript"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                serve_path = tmp_path
            else:
                serve_path = path

            self._fs_server.serve(serve_path)
            script_url = f"{self._tunnel_url}/script.funscript"
            sha        = sha256_file(serve_path)
            result = await self._handy.setup_hssp(script_url, sha)
            log.info(f"HSSP setup OK: {os.path.basename(path)} (invert={invert})")
            log_debug(f"HSSP setup full response: {result}")
            self._last_setup_path   = path
            self._last_setup_tunnel = self._tunnel_url
            self._last_setup_invert = invert
            await self._broadcast_event("success", "Script upload successful", uploaded=True)
            return True
        except Exception as e:
            log.error(f"HSSP setup failed: {e}")
            await self._broadcast_event("error", f"Script upload failed: {e}")
            await self._broadcast_status(f"HSSP setup failed: {e}")
            return False


    async def _handy_play(self, time_ms: float) -> None:
        if not self._handy or not self._handy_connected:
            log_debug(f"Handy play skipped: not connected")
            return
        try:
            adjusted = time_ms + getattr(self, "_handy_offset_ms", 0)
            result = await self._handy.play(adjusted)
            if isinstance(result, dict) and result.get("error"):
                self._handy_playing = False
                err = result["error"]
                log.warning(f"Handy play returned error: {err}")
                if err.get("name") in ("DeviceTimeout", "DeviceNotConnected"):
                    self._handy_connected = False
                    await self._broadcast_status(f"Handy disconnected: {err.get('message','')}")
            else:
                self._handy_playing = True
                log_debug(f"Handy play @ {adjusted:.0f}ms")
        except Exception as e:
            log.error(f"Handy play failed: {e}")
            await self._broadcast_status(f"Handy play failed: {e}")

    async def _handy_pause(self) -> None:
        if not self._handy or not self._handy_connected:
            self._handy_playing = False
            return
        result = await self._handy._put("/hssp/stop", {})
        self._handy_playing = False
        if result.get("error") and result["error"]["name"] in ("DeviceTimeout", "DeviceNotConnected"):
            self._handy_connected = False
            log.warning(f"Handy pause: device disconnected ({result['error']})")
        else:
            log_debug(f"Handy paused")
            log_debug(f"Handy pause response: {result}")

    async def _handy_seek(self, time_ms: float) -> None:
        if not self._handy:
            return
        try:
            adjusted = time_ms + getattr(self, "_handy_offset_ms", 0)
            if self._handy:
                await self._handy.pause()
            await asyncio.sleep(0.1)
            await self._handy.play(adjusted)
            self._handy_playing = True
            log_debug(f"Handy seek @ {adjusted:.0f}ms")
        except Exception as e:
            log.error(f"Handy seek failed: {e}")

    # ── Load script (both modes) ─────────────────────────────────────────────

    async def _load_script(self, path: str, force: bool = False, invert: bool = False) -> None:
        log.info(f"Loading script: {os.path.basename(path) if path else '(empty)'} (force={force}, invert={invert})")
        if not path:
            return

        self._current_script_path = path
        self._current_invert = invert

        if self._mode == "intiface":
            actions = load_funscript_file(path, invert=invert)
            if actions is None:
                await self._broadcast_status(f"Failed to load: {path}")
                return
            if not self.player:
                log.info(f"Player not ready – buffering ({len(actions)} actions)")
                self._pending_actions = actions
                await self._broadcast_status()
                return
            self.player.load(actions)
            self._pending_actions = None

        else:  # handy_wifi
            if self._handy and self._tunnel_url:
                await self._handy_setup_script(path, force=force, invert=invert)
            else:
                log.info("Handy not connected yet – script will be sent on connect")

        await self._broadcast_status()

    # ── WebSocket Handler ─────────────────────────────────────────────────────

    async def _ws_handler(self, ws) -> None:
        self.clients.add(ws)
        log.info(f"Frontend connected: {ws.remote_address} (clients={len(self.clients)})")
        try:
            await self._broadcast_status()
            async for raw in ws:
                try:
                    await self._handle(ws, json.loads(raw))
                except json.JSONDecodeError:
                    log.warning(f"Invalid JSON from frontend: {raw[:200]}")
                except Exception as e:
                    log.error(f"Handler error: {e}", exc_info=True)
                    try:
                        await self._broadcast_status(str(e))
                    except Exception:
                        pass
        except websockets.exceptions.ConnectionClosed:
            log_debug(f"Frontend connection closed: {ws.remote_address}")
        except Exception as e:
            log.warning(f"WS handler ended unexpectedly: {e}")
        finally:
            self.clients.discard(ws)
            log.info(f"Frontend disconnected (remaining clients={len(self.clients)})")
            if not self.clients:
                try:
                    await self._handle(ws, {"type": "pause"})
                    log_debug(f"No clients left – device paused")
                except Exception as e:
                    log.warning(f"Auto-pause on disconnect failed: {e}")

                # Reset Handy-State, damit nach Page-Reload neu verbunden werden muss
                if self._mode == "handy_wifi":
                    try:
                        await self._tunnel.stop()
                    except Exception as e:
                        log_debug(f"Tunnel stop on disconnect failed (ignored): {e}")
                    self._tunnel_url = None
                    if self._handy:
                        try:
                            await self._handy.close()
                        except Exception as e:
                            log_debug(f"Handy close on disconnect failed (ignored): {e}")
                        self._handy = None
                    self._handy_connected   = False
                    self._handy_playing     = False
                    self._last_setup_path   = None
                    self._last_setup_tunnel = None

    async def run(self) -> None:
        log.info(f"Backend starting on ws://{BACKEND_HOST}:{BACKEND_PORT}")
        loop       = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
        loop.add_signal_handler(signal.SIGINT,  stop_event.set)

        async with ws_serve(self._ws_handler, BACKEND_HOST, BACKEND_PORT):
            log.info(f"Backend ready, listening on ws://{BACKEND_HOST}:{BACKEND_PORT}")
            await stop_event.wait()

        log.info("Shutdown signal received, cleaning up...")
        # Cleanup
        self._fs_server.stop()
        await self._tunnel.stop()
        if self._handy:
            await self._handy.close()
        log.info("Server stopped.")


# ─── Daemon / Main ────────────────────────────────────────────────────────────

def release_lock() -> None:
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def daemonize(ready_fd: int = -1) -> None:
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "r") as f:
        os.dup2(f.fileno(), sys.stdin.fileno())

    log_fh = open(LOG_FILE, "a")
    os.dup2(log_fh.fileno(), sys.stdout.fileno())
    os.dup2(log_fh.fileno(), sys.stderr.fileno())

    logging.root.handlers.clear()
    logging.basicConfig(
        level=logging.DEBUG if DEBUG else logging.INFO,
        format="%(asctime)s [IntifaceSync] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )



def _wait_pid_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def _port_open(host: str, port: int, timeout: float = 0.2) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def main() -> None:
    mode = "startBackend"
    try:
        mode = json.loads(sys.stdin.read()).get("args", {}).get("mode", "startBackend")
    except Exception:
        pass

    if mode == "stopBackend":
        if not os.path.exists(LOCK_FILE):
            log.info("No running backend found.")
            return
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
        except Exception as e:
            log.warning(f"Failed to read lock file: {e}")
            return
        try:
            os.kill(pid, signal.SIGTERM)
            log.info(f"Stop signal sent to PID {pid}, waiting...")
        except ProcessLookupError:
            log.info(f"PID {pid} already gone, cleaning lock.")
            try: os.remove(LOCK_FILE)
            except OSError: pass
            return
        except Exception as e:
            log.warning(f"Failed to stop backend: {e}")
            return

        if _wait_pid_gone(pid, timeout=5.0):
            log.info(f"Backend (PID {pid}) stopped cleanly.")
        else:
            log.warning(f"Backend (PID {pid}) did not stop in time, sending SIGKILL.")
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _wait_pid_gone(pid, timeout=2.0)
            log.info(f"Backend (PID {pid}) force-killed.")
        if os.path.exists(LOCK_FILE):
            try: os.remove(LOCK_FILE)
            except OSError: pass
        return

    if mode == "startBackend":
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE) as f:
                    pid = int(f.read().strip())
                os.kill(pid, 0)
                log.info(f"Backend already running (PID {pid}), exiting.")
                return
            except (ProcessLookupError, ValueError, OSError):
                log_debug(f"Stale lock file found, will be replaced.")

        log.info("Starting backend as daemon...")
        r, w = os.pipe()
        os.set_inheritable(w, True)

        pid = os.fork()
        if pid > 0:
            os.close(w)
            deadline = time.time() + 10.0
            ready = False
            while time.time() < deadline:
                rlist, _, _ = select.select([r], [], [], 0.2)
                if r in rlist:
                    data = os.read(r, 1)
                    if data == b"":
                        ready = True
                        break
                    if data == b"K":
                        ready = True
                        break
            os.close(r)
            os.waitpid(pid, 0)
            if ready and _port_open("127.0.0.1", BACKEND_PORT):
                log.info(f"Backend is up and listening on port {BACKEND_PORT}.")
            elif ready:
                log.error(f"Daemon signaled ready but port {BACKEND_PORT} not open.")
            else:
                log.error(f"Timeout waiting for backend startup (port {BACKEND_PORT}).")
            return

        os.close(r)
        daemonize(ready_fd=w)

        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))

        log.info(f"Daemon started (PID {os.getpid()}), log: {LOG_FILE}")

        server = BackendServer()

        async def _run_with_ready():
            async def _signal_when_ready():
                for _ in range(100):
                    if _port_open("127.0.0.1", BACKEND_PORT):
                        break
                    await asyncio.sleep(0.1)
                try:
                    os.write(w, b"K")
                    os.close(w)
                except OSError:
                    pass

            asyncio.create_task(_signal_when_ready())
            await server.run()

        try:
            asyncio.run(_run_with_ready())
        finally:
            try: os.close(w)
            except OSError: pass
        release_lock()




if __name__ == "__main__":
    main()
