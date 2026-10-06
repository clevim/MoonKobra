"""
kobrax_moonraker_bridge.py - Moonraker-compatible HTTP/WebSocket bridge for the Anycubic Kobra X

Emulates the Moonraker/Klipper API so OrcaSlicer can control the Kobra X directly.

Usage:
  python kobrax_moonraker_bridge.py --printer-ip 192.168.1.50

OrcaSlicer setup:
  Connection type: Moonraker  |  Host: http://<bridge-ip>:7125

────────────────────────────────────────────────────────────────────────────
Copyright (C) 2026 viewit (KX-Bridge contributors)

This program is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License v3.0 as published
by the Free Software Foundation. See the LICENSE file at the project root
or <https://www.gnu.org/licenses/gpl-3.0.html> for the full text.

This program is distributed WITHOUT ANY WARRANTY; without even the implied warranty
of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

The Anycubic Kobra X MQTT protocol was reverse engineered
for interoperability purposes (§69e UrhG / EU Software Directive Art. 6).
This project is not affiliated with Anycubic. See NOTICE.md for details.
"""

import argparse
import sqlite3
import calendar
import uuid
try:
    import config_loader as env_loader
except ImportError:
    import env_loader
import auth
import asyncio
import hashlib
import copy
import json
import logging
import os
import pathlib
import lzma
import re
import orca_filaments
import pricing
import shutil
import subprocess
import sys
import time
import threading
import html
import ipaddress
import socket
import urllib.parse
from urllib.parse import quote

# In PyInstaller binaries everything sits next to sys.executable, otherwise next to __file__
_BASE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _BASE)
# The read-only web assets (themes) are embedded in the onefile binary via --add-data and
# extracted to sys._MEIPASS; in script/Docker mode they sit next to this file.
_WEB_BASE = getattr(sys, "_MEIPASS", _BASE)
# Windows: console children (ffmpeg) would pop up their own black window without this
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_tray = None  # Windows .exe tray icon (pystray), hidden before the process exits
_running_bridges: dict = {}  # printer_id -> bridge of the running run_bridge(), read by the tray
from kobrax_client import KobraXClient


try:
    import imageio_ffmpeg
    def _find_ffmpeg() -> str:
        return imageio_ffmpeg.get_ffmpeg_exe()
except ImportError:
    def _find_ffmpeg() -> str:
        exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
        local = os.path.join(_BASE, exe_name)
        if os.path.isfile(local):
            return local
        return "ffmpeg"

try:
    from aiohttp import web
    import aiohttp
except ImportError:
    print("Error: aiohttp is not installed. Run: pip install aiohttp")
    sys.exit(1)

try:
    import base64 as _base64
    from Crypto.Cipher import AES as _AES
    from Crypto.Util.Padding import unpad as _unpad
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


def _kx_generate_signature(token: str, ts: int, nonce: str) -> str:
    first = hashlib.md5(token[:16].encode()).hexdigest()
    return hashlib.md5((first + str(ts) + nonce).encode()).hexdigest()


def _kx_decrypt_info(encrypted_b64: str, key: str, iv: str) -> dict:
    cipher = _AES.new(key.encode(), _AES.MODE_CBC, iv.encode())
    raw = _base64.b64decode(encrypted_b64)
    return json.loads(_unpad(cipher.decrypt(raw), _AES.block_size).decode())


def _kx_fetch_bundle(ip: str, port: int = 18910, timeout: float = 10) -> dict:
    """The printer's HTTP /info + /ctrl handshake, read-only (does not rotate the credentials).

    Returns the decrypted bundle: MQTT username/password, deviceId, modeId and the mTLS
    certificate the printer generates itself (devicecrt/devicepk). Algorithm from
    fetch_credentials.py (AES-256-CBC, Key=token[16:32], IV=ctrl-token).
    """
    if not _HAS_CRYPTO:
        raise RuntimeError("pycryptodome is not installed")
    import random, string
    import requests
    nonce = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(6))
    r = requests.get(f"http://{ip}:{port}/info", timeout=timeout)
    r.raise_for_status()
    token = r.json()["token"]
    ts = int(time.time() * 1000)
    params = {"ts": ts, "nonce": nonce, "sign": _kx_generate_signature(token, ts, nonce), "did": "random"}
    r = requests.post(f"http://{ip}:{port}/ctrl", params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    result = _kx_decrypt_info(data["data"]["info"], token[16:32], data["data"]["token"])
    if "error" in result:
        raise RuntimeError(result.get("error", "decryption failed"))
    return result


def _printer_cert_provider(ip: str, device_id: str, cert_dir: str):
    """The printer's own mTLS certificate, saved to config/certs/<device_id>.crt/.key.

    Fetched on the first connection and reused afterwards. provide(refresh=True) fetches
    again (the printer rejected the saved one, e.g. it generated a new one). Deleting
    config/certs/ forces a new fetch. Returns (crt, key), or None if there is no certificate.
    """
    name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (device_id or ip)) or "printer"
    crt, key = os.path.join(cert_dir, name + ".crt"), os.path.join(cert_dir, name + ".key")

    def provide(refresh: bool = False):
        have = os.path.isfile(crt) and os.path.isfile(key)
        if refresh or not have:
            try:
                b = _kx_fetch_bundle(ip, timeout=5)
                pem_crt, pem_key = b.get("devicecrt") or "", b.get("devicepk") or ""
                if "BEGIN CERTIFICATE" not in pem_crt or "PRIVATE KEY" not in pem_key:
                    raise RuntimeError("the printer did not send a certificate")
                os.makedirs(cert_dir, exist_ok=True)
                with open(crt, "w", encoding="ascii") as f:
                    f.write(pem_crt if pem_crt.endswith("\n") else pem_crt + "\n")
                fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as f:
                    f.write(pem_key if pem_key.endswith("\n") else pem_key + "\n")
                log.info(f"Printer certificate saved to {crt}")
                have = True
            except Exception as e:
                log.warning(f"Could not fetch the printer certificate from {ip}: {e}")
                if refresh:
                    return None
        return (crt, key) if have else None
    return provide


async def _kx_fetch_credentials(ip: str, port: int = 18910) -> dict:
    """Fetches + decrypts the printer credentials (HTTP /info + /ctrl)."""
    result = await asyncio.get_running_loop().run_in_executor(None, _kx_fetch_bundle, ip, port)
    return {
        "printer_ip": result.get("ip", ip),
        "username":   result.get("username", ""),
        "password":   result.get("password", ""),
        "device_id":  result.get("deviceId", ""),
        "mode_id":    str(result.get("modeId", "20030")),
        "model":      result.get("modelName", "Anycubic Kobra"),
    }

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)-5s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("bridge")
# aiohttp logs one INFO line per HTTP request (access log) — with the frontend's
# 2 s polling that drowns the bridge's own logs by default. Toggled at
# runtime through the verbose_http_log setting (see handle_api_settings_post).
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def _set_verbose_http_log(enabled: bool):
    logging.getLogger("aiohttp.access").setLevel(logging.INFO if enabled else logging.WARNING)

# Web UI: subdirectory in web/themes/<name>/index.html
_UI_THEME_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
# Static theme files allowed under /kx/ui/<name>
_KX_UI_ASSETS: dict[str, str] = {
    "style.css": "text/css",
    "app.js":    "application/javascript",
}
# lib/ files are served by extension (they need no whitelist entry)
_KX_UI_LIB_TYPES: dict[str, str] = {
    ".js":  "application/javascript",
    ".css": "text/css",
    ".woff2": "font/woff2",
    ".png":  "image/png",   # poses do Moko (lib/moko/)
}
_KX_UI_TRANSLATION_RE = re.compile(r"^translations/([a-z]{2}(?:-[a-z]{2})?)\.json$")

# Ring buffer for the browser log stream (last 200 entries)
import collections as _collections
_log_buffer: "_collections.deque[dict]" = _collections.deque(maxlen=500)
_log_sse_queues: "list[asyncio.Queue]" = []
# Log files of the running prints (JobLogs), one per job.
# ponytail: with 2+ printers printing at once, each log gets the lines of
# all of them (like the main log); filtering per printer would need a tag on the LogRecord.
_job_log_fps: set = set()


def _fmt_log_line(e: dict) -> str:
    return f"[{e['ts']}] {e['lvl']:<7} {e['name']}: {e['msg']}"


class _BrowserLogHandler(logging.Handler):
    """Sends log records to the ring buffer, every open SSE queue
    and, during a print, to that print's log."""
    _fmt = logging.Formatter(datefmt="%H:%M:%S")

    def emit(self, record: logging.LogRecord):
        msg = record.getMessage()
        # Forward exceptions with their traceback to the browser (otherwise the
        # user only sees "Error: X" with no context).
        if record.exc_info:
            try:
                msg += "\n" + self._fmt.formatException(record.exc_info)
            except Exception:
                pass
        entry = {
            "ts":    self._fmt.formatTime(record, "%H:%M:%S"),
            "lvl":   record.levelname,
            "name":  record.name,
            "msg":   msg,
        }
        _log_buffer.append(entry)
        for fp in _job_log_fps:
            try:
                fp.write(_fmt_log_line(entry) + "\n")
            except Exception:
                pass
        for q in list(_log_sse_queues):
            try:
                q.put_nowait(entry)
            except Exception:
                pass

_browser_handler = _BrowserLogHandler()
logging.getLogger().addHandler(_browser_handler)

# info/report carries fileUploadurl?s=<session token> (and URLs with tokens), and its
# RX gets logged - the token used to end up in the log downloadable at /api/log/download.
_SECRET_QS_RE = re.compile(r"([?&](?:s|token|sign|key|password)=)[^&\s\"'\\]+", re.I)


class _RedactFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        red = _SECRET_QS_RE.sub(r"\1***", msg)
        if red != msg:
            record.msg, record.args = red, None
        return True


for _h in logging.getLogger().handlers:
    _h.addFilter(_RedactFilter())


# Limits of the log parameters (Settings → Log): (min, max, default).
LOG_LIMITS = {
    "job_log_keep":          (0, 1000, 100),   # 0 = no per-print log
    "job_log_context_lines": (0, 2000, 200),
    "log_buffer_lines":      (100, 20000, 500),
}


def _log_setting(args, key: str) -> int:
    lo, hi, default = LOG_LIMITS[key]
    try:
        return max(lo, min(hi, int(getattr(args, key, default))))
    except (TypeError, ValueError):
        return default


class JobLogs:
    """Log of each print in <data_dir>/job_logs/<job_id>.log.

    While printing it writes plain text (survives a crash). When done it compresses
    to .log.xz in a thread - same rotator/namer scheme as Python's Logging
    Cookbook, with xz instead of zlib (~20x smaller than the text; ~2x smaller than gzip).
    read() returns the text again, from either format."""
    # ponytail: preset 6e = same size as 9e on real logs, with ~94 MB of RAM
    # instead of ~674 MB (matters on a Raspberry Pi / small container).
    PRESET = 6 | lzma.PRESET_EXTREME

    def __init__(self, data_dir: str):
        self.keep = 100           # set by the bridge (Settings → Log)
        self.context_lines = 200
        self.dir = os.path.join(data_dir, "job_logs")
        os.makedirs(self.dir, exist_ok=True)
        self._open: dict = {}  # job_id -> arquivo aberto
        # Leftover .log = the bridge restarted mid-print: compress it now.
        for n in os.listdir(self.dir):
            if n.endswith(".log"):
                self._compress_bg(os.path.join(self.dir, n))

    def _path(self, job_id: str) -> str:
        return os.path.join(self.dir, os.path.basename(job_id) + ".log")

    def start(self, job_id: str, filename: str) -> None:
        if not job_id or job_id in self._open or self.keep <= 0:
            return
        fp = open(self._path(job_id), "w", encoding="utf-8", buffering=1)
        fp.write(f"# MoonKobra - print {filename}  |  job {job_id}  |  "
                 f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        # Context from before the start: upload, print/start, ACE mapping.
        for e in (list(_log_buffer)[-self.context_lines:] if self.context_lines else []):
            fp.write(_fmt_log_line(e) + "\n")
        with _browser_handler.lock:
            _job_log_fps.add(fp)
        self._open[job_id] = fp
        self._prune()

    def stop(self, job_id: str) -> None:
        fp = self._open.pop(job_id, None)
        if fp is not None:
            with _browser_handler.lock:
                _job_log_fps.discard(fp)
            fp.close()
            self._compress_bg(fp.name)

    def _compress_bg(self, path: str) -> None:
        def run():
            tmp = path + ".xz.tmp"
            try:
                with open(path, "rb") as src, lzma.open(tmp, "wb", preset=self.PRESET) as dst:
                    shutil.copyfileobj(src, dst)
                os.replace(tmp, path + ".xz")
                os.remove(path)
            except Exception as e:
                log.warning(f"Failed to compress the print log {path}: {e}")
        threading.Thread(target=run, daemon=True, name="joblog-xz").start()

    def _prune(self) -> None:
        files = sorted((os.path.join(self.dir, n) for n in os.listdir(self.dir)
                        if n.endswith(".log.xz")), key=os.path.getmtime)
        for p in files[:max(len(files) - self.keep, 0)]:
            try:
                os.remove(p)
            except OSError:
                pass

    def read(self, job_id: str) -> str | None:
        """Log text (decompresses .xz); None if it does not exist."""
        p = self._path(job_id)
        # .log first: it is the running print, or compression has not finished yet.
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            pass
        try:
            with lzma.open(p + ".xz", "rt", encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def exists(self, job_id: str) -> bool:
        p = self._path(job_id)
        return os.path.exists(p) or os.path.exists(p + ".xz")

KOBRA_TO_KLIPPER_STATE = {
    "free":          "standby",
    "busy":          "printing",
    "printing":      "printing",
    "preheating":    "printing",
    "auto_leveling": "printing",
    "checking":      "printing",
    "updated":       "printing",
    "init":          "printing",
    "pausing":       "paused",
    "paused":        "paused",
    "resuming":      "printing",
    "resumed":       "printing",
    "stopping":      "printing",
    # Sub-stages the printer sends mid-print (temperature change from
    # the G-code, colour change on the ACE). Without them the bridge fell back
    # to "standby" and stopped polling the progress.
    "nozzle_heating":             "printing",
    "hotbed_heating":             "printing",
    "frequency_sweeping":         "printing",
    "extfilbox_filament_feeding": "printing",
    "nozzle_purging":             "printing",
    "stoped":        "standby",
    "finished":      "complete",
    "failed":        "error",
    "canceled":      "standby",
}

# Anycubic error codes (the "code" field of print/report) -> English text.
# Used when the printer sends only the code, and in the display_status.message that
# Mainsail/Fluidd/Mobileraker show. The UI translates through the err_<code> keys.
# Table from stribor/anycubic_kobrax (MIT, Copyright (c) 2026 stribor) - see NOTICE.md.
ANYCUBIC_ERROR_MESSAGES = {
    10000: "Unknown error",
    10105: "File download failed",
    10106: "Insufficient memory available",
    10107: "Abnormal filament",
    10108: "Print scratch, check nozzle and hotbed distance",
    10113: "Failed to verify the MD5 checksum of the downloaded file",
    10115: "The device cannot parse the file",
    10116: "Abnormal slice file",
    10118: "X-axis homing failed",
    10119: "Y-axis homing failed",
    10120: "Z-axis homing failed",
    10121: "Hotbed heating abnormal",
    10122: "Extruder heating abnormal",
    10123: "Hotbed NTC abnormal",
    10124: "Extruder NTC abnormal",
    10125: "Nozzle MCU abnormal",
    10126: "X-axis belt detection abnormal",
    10127: "Y-axis vibration compensation abnormal",
    10128: "X-axis vibration compensation abnormal",
    10129: "Y-axis vibration compensation abnormal",
    10130: "Z-axis homing failed, check the Z-axis sensor and drive",
    10131: "Nozzle MCU abnormal",
    10133: "File lacks necessary commands",
    10134: "This feature requires access to the camera",
    10135: "This function requires a USB flash drive",
    10136: "This function requires access to the camera and USB flash drive",
    10402: "Network connection timed out",
    10403: "Filament broken, load a new filament into the ACE",
    10408: "Firmware version is too low to start the system",
    10409: "An issue occurred, restart the device manually",
    10410: "Heater temperature exceeds limit",
    10412: "An issue occurred, restart the device manually",
    10413: "An issue occurred, restart the device manually",
    10414: "A system error occurred, reboot the device manually",
    10536: "Filament Hub ID error",
    10539: "The nozzle must be heated first",
    10801: "Firmware download failed",
    10802: "Firmware update failed",
    10803: "Insufficient memory available",
    10804: "Failed to mount external storage",
    10805: "Firmware verification failed",
    11001: "Failed to delete local file",
    11101: "Wi-Fi connection failed",
    11402: "Network connection timed out",
    11407: "Device startup failed",
    11412: "MCU disconnected from host",
    11503: "Color engine connection problem",
    11504: "Unknown feed location for non-multi-color model",
    11509: "Color engine response timed out",
    11511: "Extrusion abnormal",
    11512: "Retraction abnormal",
    11513: "ACE Pro response timed out",
    11518: "Filament clogging detected",
    11519: "Filament tangle detected",
    11520: "ACE filament abnormal",
    11521: "Color engine motor rotation abnormal",
    11524: "Color engine connection problem",
    11529: "ACE NTC abnormal",
    11530: "ACE PTC abnormal",
    11531: "Model has too many colors to print",
    11532: "Device lacks automatic leveling",
    11533: "Device lacks leveling resonance",
    11801: "Spaghetti detected",
    11816: "Z-axis motor anomaly",
    11819: "Motor cable fault",
    11842: "Filament clogging or entanglement",
    11854: "Retraction failure",
    11900: "Model nozzle diameter is not compatible with the equipment nozzle",
}

# The printer sends print/report ~1x per minute while printing; with none for
# this long, the progress becomes an estimate (_estimate_progress_if_stale).
_STALE_REPORT_SEC = 150

MOONRAKER_VERSION = "v0.9.3-1"
KLIPPER_VERSION   = "v0.12.0-1"


def _parse_gcode_estimated_time(data: bytes) -> int:
    """Reads the estimated print time from the G-code (OrcaSlicer + PrusaSlicer).
    Returns seconds, 0 when not found.
    PrusaSlicer writes the time in the header (first 16KB),
    OrcaSlicer writes it at the end of the file (last 16KB)."""
    import re
    # Look at the start + end of the file (OrcaSlicer writes the time at the end)
    search_text = (data[:16384] + data[-65536:]).decode("utf-8", errors="ignore")
    # OrcaSlicer:  ; total estimated time: 9m 20s
    # PrusaSlicer: ; estimated printing time (normal mode) = 1h 9m 20s
    m = (re.search(r";\s*total estimated time:\s*(.*)", search_text) or
         re.search(r";\s*estimated printing time \(normal mode\)\s*=\s*(.*)", search_text))
    if not m:
        return 0
    parts = re.findall(r"(\d+)\s*([hms])", m.group(1))
    secs = 0
    for val, unit in parts:
        if unit == "h":   secs += int(val) * 3600
        elif unit == "m": secs += int(val) * 60
        elif unit == "s": secs += int(val)
    if secs:
        log.info(f"Estimativa do slicer: {secs}s ({m.group(1).strip()})")
    return secs


def _parse_gcode_layer_heights(data: bytes) -> tuple[float, float]:
    """Reads (layer_height, initial_layer_height) from the OrcaSlicer/PrusaSlicer G-code header.
    Both sit in a config block at the end of the G-code.

    Example lines:
      ; layer_height = 0.2
      ; initial_layer_print_height = 0.2

    Returns (0.0, 0.0) when not found - the caller decides what to do
    (typically: show no Z value)."""
    import re
    head = data[:16384].decode("utf-8", errors="ignore")
    tail = data[-65536:].decode("utf-8", errors="ignore")
    search = head + "\n" + tail
    def _grab(pat):
        m = re.search(pat, search)
        if not m:
            return 0.0
        try:
            return float(m.group(1))
        except Exception:
            return 0.0
    layer_h   = _grab(r";\s*layer_height\s*=\s*([0-9.]+)")
    first_h   = (_grab(r";\s*initial_layer_print_height\s*=\s*([0-9.]+)") or
                 _grab(r";\s*first_layer_height\s*=\s*([0-9.]+)") or
                 layer_h)
    return layer_h, first_h


def _extract_thumbnail(data: bytes) -> str:
    """Extracts the base64 PNG thumbnail from the G-code (OrcaSlicer format)."""
    try:
        marker = b"; thumbnail begin"
        end_marker = b"; thumbnail end"
        start = data.find(marker)
        if start == -1:
            return ""
        start = data.find(b"\n", start) + 1
        end = data.find(end_marker, start)
        if end == -1:
            return ""
        lines = data[start:end].split(b"\n")
        b64 = b"".join(
            line[2:].strip() if line.startswith(b"; ") else line.strip()
            for line in lines
        )
        return b64.decode("ascii")
    except Exception:
        return ""


def _extract_filament_info(data: bytes) -> list[dict]:
  """Reads filament colours/materials, incl. the tool order, from the Orca/Prusa G-code.

  Returns a list of {slot_index, color_hex, material} in tool/paint order
  (T0, T1, ...).
  Looks at both the start and the end of the file, since Orca may insert
  large thumbnail blocks, pushing the metadata to the end.
  """
  try:
    head = data[:131072]
    tail = data[-131072:] if len(data) > 131072 else b""
    header = (head + b"\n" + tail).decode("utf-8", errors="ignore")
    colors, materials = [], []
    paint_count_hint = 0
    tool_filament_order = []
    for line in header.splitlines():
      if re.match(r"^\s*;\s*filament_colour\s*=", line):
        val = line.split("=", 1)[-1].strip()
        colors = [c.strip().lstrip("#") for c in val.split(";") if c.strip()]
      elif re.match(r"^\s*;\s*filament_multi_colour\s*=", line) and not colors:
        val = line.split("=", 1)[-1].strip()
        colors = [c.strip().lstrip("#") for c in val.split(";") if c.strip()]
      elif re.match(r"^\s*;\s*filament_type\s*=", line):
        val = line.split("=", 1)[-1].strip()
        parts = [m.strip() for m in re.split(r"[;,]", val) if m.strip()]
        materials = parts
        paint_count_hint = max(paint_count_hint, len(parts))
      elif re.match(r"^\s*;\s*filament_density\s*:", line):
        val = line.split(":", 1)[-1].strip()
        parts = [x.strip() for x in re.split(r"[;,]", val) if x.strip()]
        paint_count_hint = max(paint_count_hint, len(parts))
      elif re.match(r"^\s*;\s*filament_diameter\s*:", line):
        val = line.split(":", 1)[-1].strip()
        parts = [x.strip() for x in re.split(r"[;,]", val) if x.strip()]
        paint_count_hint = max(paint_count_hint, len(parts))
      elif re.match(r"^\s*;\s*filament\s*:", line):
        raw = line.split(":", 1)[-1]
        parsed = []
        for p in [x.strip() for x in raw.split(",") if x.strip()]:
          try:
            parsed.append(int(p))
          except Exception:
            pass
        if parsed:
          tool_filament_order = parsed
    total_paints = max(len(colors), len(materials), paint_count_hint)
    if tool_filament_order:
      total_paints = max(total_paints, max(tool_filament_order))
    if total_paints <= 0:
      return []

    # Keep the full paint list visible; mark as used the ones referenced by Orca's tool order.
    if len(colors) < total_paints:
      colors.extend(["FFFFFF"] * (total_paints - len(colors)))
    if len(materials) < total_paints:
      materials.extend(["PLA"] * (total_paints - len(materials)))
    # Prefer the real tool change commands in the G-code body.
    # That avoids passing on paints that are in the metadata but never used.
    used_paints_zero_based = set()
    try:
      for m in re.finditer(br"(?m)^[ \t]*T([0-9]+)\b", data):
        used_paints_zero_based.add(int(m.group(1)))
    except Exception:
      used_paints_zero_based = set()

    # Fallback for slicers that only report paint usage in the header metadata.
    used_paints_from_header = set()
    for n in tool_filament_order:
      try:
        # Orca/Prusa filament: the list usually starts at 1.
        used_paints_from_header.add(max(0, int(n) - 1))
      except Exception:
        pass

    result = []
    for i in range(total_paints):
      hex_color = colors[i] if i < len(colors) else "FFFFFF"
      result.append({
        "slot_index": i,
        "color_hex":  "#" + hex_color.upper() if hex_color else "#FFFFFF",
        "material":   materials[i] if i < len(materials) else "PLA",
        "is_used":    (i in used_paints_zero_based) if used_paints_zero_based else ((i in used_paints_from_header) if used_paints_from_header else True),
      })
    return result
  except Exception:
    return []


class GCodeStore:
    """Persistent G-code store per bridge instance (SQLite)."""

    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self._gcode_dir = os.path.join(data_dir, "gcodes")
        os.makedirs(self._gcode_dir, exist_ok=True)
        db_path = os.path.join(data_dir, "moonkobra.db")
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.data_dir = data_dir
        self._init_schema()
        self.job_logs = JobLogs(data_dir)

    def _init_schema(self):
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS gcode_files (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    thumbnail_b64 TEXT,
                    est_print_time_sec INTEGER,
                    filament_used_mm REAL,
                    layer_count INTEGER,
                    gcode_filaments TEXT,
                    objects_skip_parts TEXT,
                    svg_image TEXT,
                    web_unverified INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS quotes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    gcode_file_id TEXT,
                    filename TEXT,
                    client TEXT,
                    total REAL,
                    data TEXT NOT NULL,
                    snapshot TEXT
                );
                CREATE TABLE IF NOT EXISTS print_jobs (
                    id TEXT PRIMARY KEY,
                    gcode_file_id TEXT NOT NULL,
                    printer_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL,
                    duration_sec INTEGER,
                    filament_assignments TEXT,
                    abort_reason TEXT
                );
            """)
            # Migration: add the gcode_filaments column to old databases
            try:
                self._conn.execute("ALTER TABLE gcode_files ADD COLUMN gcode_filaments TEXT")
                self._conn.commit()
            except Exception:
                pass
            # Migration: objects_skip_parts + svg_image columns (skip-part feature, v0.9.10)
            # Plus layer_height / first_layer_height (Obico Z height, v0.9.18)
            for col, typ in (
                ("objects_skip_parts", "TEXT"),
                ("svg_image", "TEXT"),
                ("layer_height", "REAL"),
                ("first_layer_height", "REAL"),
            ):
                try:
                    self._conn.execute(f"ALTER TABLE gcode_files ADD COLUMN {col} {typ}")
                    self._conn.commit()
                except Exception:
                    pass
            # Migration: flag for web uploads (warning before printing)
            try:
                self._conn.execute("ALTER TABLE gcode_files ADD COLUMN web_unverified INTEGER NOT NULL DEFAULT 0")
                self._conn.commit()
            except Exception:
                pass
            # Migration: file name on the job itself - prints started from the printer's
            # screen (outside the store) also go into the history.
            try:
                self._conn.execute("ALTER TABLE print_jobs ADD COLUMN filename TEXT")
                self._conn.commit()
            except Exception:
                pass

    def save_file(self, file_id: str, filename: str, data: bytes,
                  est_time_sec: int = 0, thumbnail_b64: str = "",
                  gcode_filaments: list | None = None,
                  web_unverified: bool = False,
                  layer_height: float = 0.0,
                  first_layer_height: float = 0.0) -> str:
        """Saves a G-code file to disk and to the DB. Returns the path."""
        safe_name = os.path.basename(filename)
        path = os.path.join(self._gcode_dir, safe_name)
        with open(path, "wb") as f:
            f.write(data)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            filaments_json = json.dumps(gcode_filaments) if gcode_filaments else None
            self._conn.execute(
                """INSERT OR REPLACE INTO gcode_files
                   (id, filename, path, size_bytes, uploaded_at, thumbnail_b64, est_print_time_sec, gcode_filaments, web_unverified, layer_height, first_layer_height)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (file_id, filename, path, len(data), now, thumbnail_b64 or None, est_time_sec or None, filaments_json, 1 if web_unverified else 0, layer_height or None, first_layer_height or None)
            )
            self._conn.commit()
        return path

    def list_files(self) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM gcode_files ORDER BY uploaded_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_file(self, file_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM gcode_files WHERE id=?", (file_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_file_by_name(self, filename: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM gcode_files WHERE filename=? ORDER BY uploaded_at DESC LIMIT 1",
                (filename,)
            ).fetchone()
        return dict(row) if row else None

    def running_job_start(self, filename: str) -> float | None:
        """Start epoch of this file's job still 'printing' (survives a bridge restart)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT j.started_at FROM print_jobs j JOIN gcode_files f ON f.id=j.gcode_file_id "
                "WHERE f.filename=? AND j.status='printing' ORDER BY j.started_at DESC LIMIT 1",
                (filename,)
            ).fetchone()
        if not row:
            return None
        return calendar.timegm(time.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ"))

    def update_file_objects(self, filename: str, objects: list, svg: str = "") -> None:
        """Saves a file's object list + optional SVG (matched by name)."""
        if not filename:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE gcode_files SET objects_skip_parts=?, svg_image=? "
                "WHERE filename=?",
                (json.dumps(objects), svg or "", filename),
            )
            self._conn.commit()

    def update_file_filaments(self, file_id: str, gcode_filaments: list | None) -> None:
      """Updates the parsed G-code filaments of an existing DB entry."""
      with self._lock:
        self._conn.execute(
          "UPDATE gcode_files SET gcode_filaments=? WHERE id=?",
          (json.dumps(gcode_filaments) if gcode_filaments else None, file_id),
        )
        self._conn.commit()

    def clear_web_unverified(self, file_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE gcode_files SET web_unverified=0 WHERE id=?",
                (file_id,),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete_file(self, file_id: str) -> bool:
        row = self.get_file(file_id)
        if not row:
            return False
        with self._lock:
            self._conn.execute("DELETE FROM gcode_files WHERE id=?", (file_id,))
            self._conn.commit()
            # Disk keys by name and the DB by md5: a re-upload with the same name
            # adds another row pointing to the SAME file. Only delete it when nobody else uses it.
            shared = self._conn.execute(
                "SELECT 1 FROM gcode_files WHERE path=? LIMIT 1", (row["path"],)).fetchone()
        if not shared:
            try:
                os.remove(row["path"])
            except OSError:
                pass
        return True

    def start_job(self, gcode_file_id: str, printer_id: str,
                  filament_assignments: list | None = None, filename: str = "") -> str:
        job_id = str(uuid.uuid4())
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        assignments_json = json.dumps(filament_assignments) if filament_assignments else None
        with self._lock:
            self._conn.execute(
                """INSERT INTO print_jobs
                   (id, gcode_file_id, printer_id, started_at, status, filament_assignments, filename)
                   VALUES (?,?,?,?,'printing',?,?)""",
                (job_id, gcode_file_id, printer_id, now, assignments_json, filename or None)
            )
            self._conn.commit()
        return job_id

    def finish_job(self, job_id: str, status: str = "completed",
                   abort_reason: str = "") -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            row = self._conn.execute(
                "SELECT started_at FROM print_jobs WHERE id=?", (job_id,)
            ).fetchone()
            duration = None
            if row:
                try:
                    import calendar
                    start = time.strptime(row["started_at"], "%Y-%m-%dT%H:%M:%SZ")
                    duration = int(time.time() - calendar.timegm(start))
                except Exception:
                    pass
            self._conn.execute(
                """UPDATE print_jobs SET ended_at=?, status=?, duration_sec=?, abort_reason=?
                   WHERE id=?""",
                (now, status, duration, abort_reason or None, job_id)
            )
            self._conn.commit()

    # ── Quotes (pricing module) ──
    def save_quote(self, gcode_file_id: str, filename: str, client: str, total: float,
                   data: dict, snapshot: str) -> int:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO quotes (created_at, gcode_file_id, filename, client, total, data, snapshot) "
                "VALUES (?,?,?,?,?,?,?)",
                (now, gcode_file_id, filename, client, total, json.dumps(data, ensure_ascii=False), snapshot))
            self._conn.commit()
        return cur.lastrowid

    def list_quotes(self, limit: int = 200) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, gcode_file_id, filename, client, total FROM quotes "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_quote(self, quote_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
        if not row:
            return None
        q = dict(row)
        q["data"] = json.loads(q["data"] or "{}")
        return q

    def delete_quote(self, quote_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM quotes WHERE id=?", (quote_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def last_job_duration(self, filename: str) -> int:
        """Real duration (s) of this file's last completed print; 0 if never."""
        with self._lock:
            row = self._conn.execute(
                "SELECT j.duration_sec FROM print_jobs j LEFT JOIN gcode_files f ON f.id=j.gcode_file_id "
                "WHERE COALESCE(f.filename, j.filename)=? AND j.status='completed' AND j.duration_sec>0 "
                "ORDER BY j.started_at DESC LIMIT 1", (filename,)).fetchone()
        return int(row[0]) if row else 0

    def list_jobs(self, limit: int = 50, offset: int = 0) -> list:
        with self._lock:
            rows = self._conn.execute(
                """SELECT j.*, COALESCE(f.filename, j.filename) AS filename, f.thumbnail_b64
                   FROM print_jobs j
                   LEFT JOIN gcode_files f ON j.gcode_file_id = f.id
                   ORDER BY j.started_at DESC LIMIT ? OFFSET ?""",
                (limit, offset)
            ).fetchall()
        return [dict(r) for r in rows]


class CameraCache:
    """Central camera demuxer.

    Reads the printer's FLV stream with as few connections as possible:
      - MJPEG @ 15fps/640px -> fanned out to every /api/camera/stream subscriber
        (the live view used by the dashboard AND by every Moonraker-compatible
        client, since server.webcams.list announces this same stream_url);
        the last frame stays in RAM and also serves /api/camera/snapshot
      - MPEG-TS (-c:v copy) -> fanned out to /api/camera/h264 subscribers (Obico).
        Only starts when someone asks for /api/camera/h264.

    The printer's camera server answers 429 when there are too many clients:
    three ffmpeg processes used to start ALWAYS (2fps snapshot, mjpeg and h264), each with its
    own FLV connection, and all three got 429 together. Now normal use
    (dashboard, Orca, snapshots) is ONE connection; with Obico, two.

    As a result:
      * At most ONE FLV connection to the printer per output type - /api/camera/stream used to open a
        new ffmpeg + printer connection, uncached, per HTTP client, which
        competed with the cached jpeg/h264 connections for the printer's very
        limited number of simultaneous camera clients and caused intermittent
        "stream unavailable" failures.
      * Snapshots are instant (read from memory, no ffmpeg spawn per request)
      * Several H.264/MJPEG consumers in parallel are possible (plugin + web UI + ...)

    Lazy start on the first consumer, automatic restart if ffmpeg dies.
    """

    JPEG_SOI = b"\xff\xd8"
    JPEG_EOI = b"\xff\xd9"
    TS_CHUNK = 65536

    def __init__(self):
        self._url: str = ""
        self.latest_jpeg: bytes = b""
        self.latest_jpeg_ts: float = 0.0
        self.h264_subscribers: "set[asyncio.Queue[bytes]]" = set()
        self.mjpeg_subscribers: "set[asyncio.Queue[bytes]]" = set()
        self._proc_h264: "asyncio.subprocess.Process | None" = None
        self._proc_mjpeg: "asyncio.subprocess.Process | None" = None
        self._task_h264: "asyncio.Task | None" = None
        self._task_mjpeg: "asyncio.Task | None" = None
        self._lock = asyncio.Lock()
        self._fail_count_h264: int = 0
        self._fail_count_mjpeg: int = 0

    def set_url(self, url: str):
        # A different URL means the printer rotated the stream token (typically
        # after a reboot). Running ffmpeg processes still hold the old URL
        # and will never pick it up on their own - they only re-read self._url
        # at the top of the outer loop, which they never reach while blocked
        # on a stdout read of the old, now silent connection. Kill them;
        # the next ensure_running() recreates them with the new URL.
        changed = bool(url and self._url and url != self._url)
        self._url = url
        if changed:
            self.reset()

    def reset(self):
        """Resets the backoff counters and force-kills any running ffmpeg
        loop - including cancelling the background tasks.

        Killing the ffmpeg subprocess alone is not enough: the owning task
        may be parked in `await asyncio.sleep(delay)` of an earlier
        exponential backoff (up to 300s) after a failure.
        Resetting the failure counter does not wake it earlier, so a user
        clicking "reset" might see nothing happen for minutes. Cancelling
        the task guarantees an immediate, clean restart on the next
        ensure_running() call.
        """
        self._fail_count_h264 = 0
        self._fail_count_mjpeg = 0
        for task in (self._task_h264, self._task_mjpeg):
            if task is not None and not task.done():
                task.cancel()
        for proc in (self._proc_h264, self._proc_mjpeg):
            if proc is not None:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._task_h264 = self._task_mjpeg = None
        self._proc_h264 = self._proc_mjpeg = None

    async def ensure_running(self, h264: bool = False):
        """Ensures the mjpeg loop (stream + snapshots); h264 only when asked for,
        because each loop is one more connection to the printer (limit / 429)."""
        # NOTE: we check the *task* state, not self._proc_* - the process
        # handle is only assigned later, inside the task body, once ffmpeg
        # has actually started. Checking self._proc_* here left a race
        # window: two callers arriving before the freshly created task had a
        # chance to run would both see "no process yet" and each create a
        # duplicate ffmpeg + duplicate printer connection, silently orphaning
        # the older one (the coroutine that runs last
        # overwrites the shared self._proc_* reference, so nobody keeps
        # a handle to kill the orphaned process). Task creation is
        # synchronous, so checking self._task_* here is race-free.
        if h264 and (self._task_h264 is None or self._task_h264.done()):
            self._task_h264 = asyncio.create_task(self._run_h264_loop())
        if self._task_mjpeg is None or self._task_mjpeg.done():
            self._task_mjpeg = asyncio.create_task(self._run_mjpeg_loop())

    # ffmpeg warnings that do not signal a problem and only hid the real error
    # (only the first 500 bytes used to be logged, and they were exactly these).
    _FFMPEG_NOISE = ("deprecated pixel format used",)

    async def _log_ffmpeg_stderr(self, kind: str, proc) -> None:
        try:
            err = (await proc.stderr.read(8000)).decode(errors="replace")
        except Exception:
            return
        lines = [l.strip() for l in err.splitlines()
                 if l.strip() and not any(n in l for n in self._FFMPEG_NOISE)]
        if lines:
            log.warning(f"CameraCache: ffmpeg-{kind} failed: {' | '.join(lines[-3:])[:600]}")

    def _input_args(self, url: str) -> list[str]:
        args = ["-fflags", "nobuffer", "-flags", "low_delay",
                 # Give up if the source goes silent. A printer reboot or
                 # network loss leaves the TCP connection ESTABLISHED with no
                 # data and no FIN, so a passive stdout read blocks forever
                 # without this (Issue #99). Value in microseconds.
                 "-timeout", "10000000"]
        if url.lower().startswith("rtsp://"):
            args += ["-probesize", "32", "-analyzeduration", "0", "-rtsp_transport", "tcp"]
        else:
            # The printer's FLV source sometimes emits non-monotonic container
            # timestamps (PTS jumps of days) while the video itself stays
            # valid. Without this flag ffmpeg's real-time pacing breaks on such a
            # jump and the stream freezes after ~15-30 min (Issue #90).
            args += ["-use_wallclock_as_timestamps", "1",
                     "-probesize", "500000", "-analyzeduration", "500000"]
        return args

    async def _run_h264_loop(self):
        """Keeps alive an ffmpeg process that fans MPEG-TS out to every subscriber."""
        while True:
            url = self._url
            if not url:
                await asyncio.sleep(2.0)
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    _find_ffmpeg(), "-loglevel", "warning",
                    *self._input_args(url), "-i", url,
                    "-c:v", "copy", "-an",
                    "-f", "mpegts", "pipe:1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=_NO_WINDOW,
                )
                self._proc_h264 = proc
            except Exception as e:
                log.warning(f"CameraCache: failed to start ffmpeg-h264: {e}")
                await asyncio.sleep(3.0)
                continue

            rc = None
            try:
                while True:
                    chunk = await proc.stdout.read(self.TS_CHUNK)
                    if not chunk:
                        break
                    # Fan-out: non-blocking per subscriber; slow clients
                    # lose the oldest chunk (queue full -> drop).
                    for q in list(self.h264_subscribers):
                        if q.full():
                            try:
                                q.get_nowait()
                            except Exception:
                                pass
                        try:
                            q.put_nowait(chunk)
                        except Exception:
                            pass
            except Exception as e:
                log.debug(f"CameraCache: h264 loop interrupted: {e}")
            finally:
                # NOTE: cleanup acts on the local `proc` reference, not on
                # self._proc_h264 - see the identical comment in _run_mjpeg_loop.
                # If this task was cancelled (e.g. by reset()), a new task may
                # already have started and assigned its own process to
                # self._proc_h264 by the time we get here; killing that
                # shared attribute instead of our local proc would kill
                # the WRONG (newer) process and orphan this one.
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
                rc = proc.returncode
                # rc < 0 = we killed the process ourselves (reset / new URL / cancellation):
                # not a failure, so no dumping stderr as a warning.
                if rc and rc > 0:
                    await self._log_ffmpeg_stderr("h264", proc)
                if self._proc_h264 is proc:
                    self._proc_h264 = None
            if rc and rc > 0:
                self._fail_count_h264 += 1
                delay = min(2.0 * (2 ** self._fail_count_h264), 300.0)
                log.warning(f"CameraCache: ffmpeg-h264 exited with {rc}, retrying in {delay:.0f}s (attempt {self._fail_count_h264})")
                await asyncio.sleep(delay)
            else:
                self._fail_count_h264 = 0
                await asyncio.sleep(2.0)

    async def _run_mjpeg_loop(self):
        """Keeps alive an ffmpeg process that fans MJPEG@15fps/640px
        (complete JPEG frames) out to every /api/camera/stream subscriber."""
        while True:
            url = self._url
            if not url:
                await asyncio.sleep(2.0)
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    _find_ffmpeg(), "-loglevel", "warning",
                    *self._input_args(url), "-i", url,
                    "-vf", "fps=15,scale=640:-1",
                    "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3",
                    "-flush_packets", "1", "pipe:1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=_NO_WINDOW,
                )
                self._proc_mjpeg = proc
            except Exception as e:
                log.warning(f"CameraCache: failed to start ffmpeg-mjpeg: {e}")
                await asyncio.sleep(3.0)
                continue

            buf = b""
            rc = None
            try:
                while True:
                    chunk = await proc.stdout.read(self.TS_CHUNK)
                    if not chunk:
                        break
                    buf += chunk
                    # extract complete JPEG frames and fan them out whole
                    # (so every subscriber gets clean multipart boundaries,
                    # not arbitrary byte chunks as in the h264/mpegts fan-out)
                    while True:
                        start = buf.find(self.JPEG_SOI)
                        if start == -1:
                            buf = b""
                            break
                        end = buf.find(self.JPEG_EOI, start + 2)
                        if end == -1:
                            buf = buf[start:]
                            break
                        frame = buf[start:end + 2]
                        buf = buf[end + 2:]
                        self.latest_jpeg = frame
                        self.latest_jpeg_ts = time.time()
                        for q in list(self.mjpeg_subscribers):
                            if q.full():
                                try:
                                    q.get_nowait()
                                except Exception:
                                    pass
                            try:
                                q.put_nowait(frame)
                            except Exception:
                                pass
            except Exception as e:
                log.debug(f"CameraCache: mjpeg loop interrupted: {e}")
            finally:
                # NOTE: cleanup acts on the local `proc` reference, not
                # on self._proc_mjpeg. If this task was cancelled (e.g. by
                # reset()) a new task may already have started and assigned
                # its own process to self._proc_mjpeg by the time we get
                # here - killing that shared attribute instead of our
                # local proc would kill the WRONG (newer) process.
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    pass
                rc = proc.returncode
                # rc < 0 = we killed the process ourselves (reset / new URL / cancellation):
                # not a failure, so no dumping stderr as a warning.
                if rc and rc > 0:
                    await self._log_ffmpeg_stderr("mjpeg", proc)
                if self._proc_mjpeg is proc:
                    self._proc_mjpeg = None
            if rc and rc > 0:
                self._fail_count_mjpeg += 1
                delay = min(2.0 * (2 ** self._fail_count_mjpeg), 300.0)
                log.warning(f"CameraCache: ffmpeg-mjpeg exited with {rc}, retrying in {delay:.0f}s (attempt {self._fail_count_mjpeg})")
                await asyncio.sleep(delay)
            else:
                self._fail_count_mjpeg = 0
                await asyncio.sleep(2.0)


class SpoolmanClient:
    """Small synchronous HTTP client for Spoolman filament tracking.

    Meant to be called from daemon threads (poll loop, _on_print callbacks).
    Uses requests (already in requirements), so it does not depend on an event loop.
    """

    def __init__(self, server_url: str, sync_rate: int = 0):
        self.server_url = server_url.rstrip("/")
        self.sync_rate = sync_rate

    def _req(self, method: str, path: str, **kwargs):
        import requests
        r = requests.request(method, f"{self.server_url}{path}", timeout=5, **kwargs)
        r.raise_for_status()
        return r.json()

    def health_check(self) -> bool:
        try:
            self._req("GET", "/api/v1/health")
            return True
        except Exception:
            return False

    def list_spools(self) -> list:
        return self._req("GET", "/api/v1/spool")

    def use_filament(self, spool_id: int, use_length_mm: float) -> None:
        """Reports the consumed filament length in mm. Spoolman converts it to weight
        using the density of the spool's filament profile."""
        self._req("PUT", f"/api/v1/spool/{spool_id}/use",
                  json={"use_length": round(use_length_mm, 2)})


class KobraXBridge:
    def __init__(self, client: KobraXClient, args=None, store=None, printer_id: str = "1", all_bridges=None):
        self.client = client
        self._args = args
        self._printer_id = printer_id
        self._all_bridges = all_bridges if all_bridges is not None else {}
        self.ws_clients: set[web.WebSocketResponse] = set()
        # Who is talking to this bridge (OrcaSlicer, Obico, browser...):
        # {(ip, app): {...}}. Memory only; fed by the clients middleware
        # and by the WebSocket's server.connection.identify.
        self._clients: dict[tuple[str, str], dict] = {}
        self._client_hosts: dict[str, str] = {}   # cache de DNS reverso por IP
        # In-memory KV store for Moonraker's /server/database/item (moonraker-obico,
        # mainsail presets, etc.). Not persistent - does not survive a restart.
        self._moonraker_kv_store: dict[str, dict] = {}
        # Slot -> Orca filament profile mapping (from config.ini [filament_profiles]).
        # Format: {slot_idx: {"id": "OGFL01", "vendor": "Polymaker"}}.
        # Used in _build_lane_data so OrcaSlicer shows the concrete
        # brand ("PolyTerra PLA - Polymaker") instead of just "Generic PLA".
        try:
            import config_loader as _cl
            self._filament_profiles: dict[int, dict] = _cl.list_filament_profiles(self._printer_id)
        except Exception:
            self._filament_profiles = {}
        # Vendor visibility filter for the slot profile dropdown (Issue #41 option A).
        # Empty list = every vendor visible (compatible with old versions).
        try:
            import config_loader as _cl
            self._visible_vendors: list[str] = _cl.list_visible_vendors(self._printer_id)
        except Exception:
            self._visible_vendors = []
        self._last_state: dict = {}
        self._last_ams_set_request: dict | None = None
        self._state = {
          "nozzle_temp":        0.0,
          "nozzle_target":      0.0,
          "bed_temp":           0.0,
          "bed_target":         0.0,
          "print_state":        "standby",
          "kobra_state":        "free",
          "filename":           "",
          "slicer_time":        0,
          "progress":           0.0,
          "progress_estimated": False,
          "print_duration":     0,
          "remain_time":        0,
          "curr_layer":         0,
          "total_layers":       0,
          # Layer heights of the file being printed (parsed from the
          # G-code header). Set on the upload path + in _fetch_from_store.
          # Obico uses currentZ from gcode_position[2] - the bridge computes
          # currentZ from curr_layer + these values in build_print_payload.
          "layer_height":       0.0,
          "first_layer_height": 0.0,
          "printer_name":       env_loader.get("BRIDGE_PRINTER_NAME", "Anycubic Kobra X"),
          "firmware_version":   "unknown",
          "upload_url":         "",
          "camera_url":         "",
          "fan_speed":          0,
          "light_on":           False,
          "light_brightness":   80,
          "taskid":             "-1",
          "print_speed_mode":   2,
          "connection_error":   "",
          "file_ready":         "",
          "filament_mismatch":  None,
          "print_start_dialog": getattr(args, "print_start_dialog", 1),
          "filament_mode":      "toolhead",
          "supplies_usage":     0,
          "ace_drying": {"status": 0, "target_temp": 0, "duration": 0, "remain_time": 0, "humidity": None, "current_temp": None},
          "error_code":         0,
          "pause_msg":          "",
          "storage_total_mb":   0,
          "storage_used_mb":    0,
        }
        self._ams_slots: list[dict] = []       # flat global list; each entry has global_index + box_id
        self._ams_loaded_slot: int = -1        # global index of the currently loaded slot
        self._pending_load_slot: int = -1      # global index of the slot requested via /api/ams/feed type=1
        self._ace_box_ids: list[int] = []      # IDs of the detected ACE units (0..3)
        self._ace_auto_feed: dict[int, int] = {}   # auto_feed state per box (0/1)
        self._head_tools_model: int = -1
        self._filament_mode: str = "toolhead"
        self._last_uploaded_file: str = ""
        # Pending waiters for a specific file/report `action` (e.g. "listLocal",
        # "deleteBatch"). publish()'s own return for those actions is only
        # a generic immediate-ACK skeleton (code=0, every field empty) - the real
        # answer arrives later through the file/report callback (_on_file), like
        # the existing fire-and-forget fileDetails pattern. Format:
        # {action: {"event": threading.Event(), "result": dict|None}}.
        self._file_action_waiters: dict[str, dict] = {}
        # Thumbnail cache of files on the printer's own storage (name ->
        # base64 PNG string, "" if the file has no embedded thumbnail).
        # Memory only - not persisted, cleared on restart.
        self._printer_thumbnail_cache: dict[str, str] = {}
        # Last buried/report payload (the printer's own analytics event, fired once
        # per print start, regardless of the slicer). Carries
        # gcode_size/estimate_duration/total_layers that
        # are otherwise unavailable for files not uploaded by the bridge itself
        # (Issue #102). Only one entry - the most recent print.
        self._buried_cache: dict | None = None
        self._store = store if store is not None else GCodeStore(args.data_dir)
        self._apply_log_settings()
        self._serve_dir_path: str = self._store._gcode_dir
        self._current_job_id: str = ""
        # File name behind _current_job_id, kept alongside so that
        # the "finished" handler can still delete it from the printer's own
        # storage (delete after printing) after self._state["filename"]
        # was already cleared as part of the terminal state reset below.
        self._current_job_filename: str = ""
        self._camera_autostarted: bool = False
        self._camera_user_stopped: bool = False  # user stopped the camera manually during a print
        self._light_desired = None  # last explicit UI light state (None = user never set it)
        self._camera_start_ts: float = 0.0  # time of our last video/startCapture
        self._update_info = {"current": "", "latest": None, "url": "", "available": False}
        self._update_checked_ts: float = 0.0  # last GitHub release check
        self.camera_cache: CameraCache = CameraCache()

        self._thumbnail_b64: str = ""
        self._ace_dry_presets: dict[str, dict] = self._load_ace_dry_presets_config()

        # Skip part: latest skipped list reported by the printer (v0.9.10)
        self._skip_state: dict = {"objects": [], "skipped": [], "ts": 0}
        # Skip before printing: pending until the printer enters the printing state
        self._pending_preprint_skip: list[str] = []
        self._pending_preprint_skip_deadline: float = 0.0

        # Spoolman filament tracking
        _sm_url = (getattr(args, "spoolman_server", "") or "").strip()
        self._spoolman: SpoolmanClient | None = (
            SpoolmanClient(_sm_url, getattr(args, "spoolman_sync_rate", 0))
            if _sm_url else None
        )
        # Load the persisted spool assignment (AMS slot → Spoolman spool) per printer.
        # Fix: this referenced `config_loader`, but the module alias is
        # `env_loader` -> NameError swallowed by the generic `except`,
        # so persistence never loaded. Now through a local import + per printer.
        try:
            import config_loader as _cl
            self._spoolman_slot_spools: dict[int, int] = _cl.list_spool_map(self._printer_id)
        except Exception as _e:
            log.warning("Spoolman: failed to load the slot map: %s", _e)
            self._spoolman_slot_spools = {}  # {ams_slot_idx: spoolman_spool_id}
        self._spoolman_slot_usage: dict[int, float] = {}   # mm accumulated per slot in this print
        self._spoolman_slot_reported: dict[int, float] = {}  # mm per slot already sent to Spoolman
        self._spoolman_last_usage: float = 0.0   # supplies_usage at the last attribution tick
        self._spoolman_last_sync: float = 0.0

        # Validate the theme name (no special characters or accents)
        raw_theme = (getattr(args, "ui_theme", None) or "default").strip()
        if not _UI_THEME_NAME_RE.match(raw_theme):
            log.warning("Invalid UI theme name %r – using the default", raw_theme)
            raw_theme = "default"
        self._ui_theme = raw_theme
        self._index_tpl_cache: str | None = None
        self._index_tpl_cache_key: tuple[str, float] | None = None
        # Signing key of the login cookies. Sits next to the G-code store so
        # restarting the bridge does not log every browser out.
        self._session_secret: bytes = auth.load_or_create_secret(args.data_dir)

        # Register the MQTT push callbacks
        client.callbacks["tempature/report"]      = self._on_temp
        client.callbacks["print/report"]          = self._on_print
        client.callbacks["info/report"]           = self._on_info
        client.callbacks["file/report"]           = self._on_file
        client.callbacks["buried/report"]         = self._on_buried
        client.callbacks["multiColorBox/report"]  = self._on_multicolor_box
        client.callbacks["light/report"]          = self._on_light
        client.callbacks["skip/report"]           = self._on_skip
        client.callbacks["fan/report"]            = self._on_fan

        # Reachability is rechecked periodically (not only once at boot) so
        # the status indicator in the UI reflects the real current printer/Spoolman state
        # instead of freezing on the boot result.
        self._spoolman_reachable: bool = False
        self._spoolman_last_health_check: float = 0.0
        if self._spoolman:
            def _check():
                ok = self._spoolman.health_check()
                self._spoolman_reachable = ok
                self._spoolman_last_health_check = time.time()
                log.info(f"Spoolman: {'OK' if ok else 'unreachable'} at {self._spoolman.server_url}")
            threading.Thread(target=_check, daemon=True, name="spoolman-health").start()

    # ── Spoolman helpers ──────────────────────────────────────────────────────

    def _spoolman_filament_mm(self) -> float:
        """Total filament_used_mm of the file being printed, from the G-code DB."""
        filename = self._state.get("filename", "")
        if not filename:
            return 0.0
        try:
            gf = self._store.get_file_by_name(filename)
            return float(gf.get("filament_used_mm") or 0.0) if gf else 0.0
        except Exception:
            return 0.0

    def _spoolman_attribute_tick(self, activity_map: dict) -> None:
        """Attributes the supplies_usage delta since the last tick to the active slot.

        Skips attribution during load/unload transitions (tool changes +
        purges) so purge material is not charged to the wrong spool."""
        if not self._spoolman or not self._spoolman_slot_spools:
            return
        if self._state.get("print_state") != "printing":
            return
        current = self._state.get("supplies_usage", 0)
        delta = current - self._spoolman_last_usage
        self._spoolman_last_usage = current
        if delta <= 0:
            return
        loaded = self._ams_loaded_slot
        if loaded < 0:
            return
        if activity_map.get(loaded):
            return
        self._spoolman_slot_usage[loaded] = self._spoolman_slot_usage.get(loaded, 0.0) + delta

    def _spoolman_unreported(self) -> dict[int, float]:
        """Returns {slot_idx: mm} of usage not yet reported to Spoolman.

        Without per-slot attribution data (single extruder without AMS - only one
        possible spool), credits the whole supplies_usage to the single mapped slot.
        With more than one mapped slot, splitting the unattributed usage evenly
        among all of them would silently deduct filament from spools that were not even
        used in this print (Issue: filament removed from spools outside the
        print) - it is safer to report nothing for those slots and wait for
        real attribution data than to guess wrong."""
        total_used = self._state.get("supplies_usage", 0)
        if self._spoolman_slot_usage:
            return {
                slot: self._spoolman_slot_usage.get(slot, 0.0)
                       - self._spoolman_slot_reported.get(slot, 0.0)
                for slot in self._spoolman_slot_spools
            }
        if len(self._spoolman_slot_spools) == 1:
            slot = next(iter(self._spoolman_slot_spools))
            already = self._spoolman_slot_reported.get(slot, 0.0)
            return {slot: total_used - already}
        return {}

    def _spoolman_report(self, unreported: dict[int, float], min_mm: float = 0.1) -> None:
        """Sends the unreported mm for each mapped spool without waiting for a reply."""
        sm = self._spoolman
        for slot_idx, mm in unreported.items():
            if mm < min_mm:
                continue
            spool_id = self._spoolman_slot_spools.get(slot_idx)
            if not spool_id:
                continue
            self._spoolman_slot_reported[slot_idx] = (
                self._spoolman_slot_reported.get(slot_idx, 0.0) + mm
            )
            def _send(sid=spool_id, length=mm):
                try:
                    sm.use_filament(sid, length)
                    log.info(f"Spoolman: {length:.1f} mm → carretel {sid}")
                except Exception as e:
                    log.warning(f"Spoolman: failed to report (spool {sid}): {e}")
            threading.Thread(target=_send, daemon=True, name="spoolman-report").start()

    def _apply_log_settings(self):
        global _log_buffer
        self._store.job_logs.keep = _log_setting(self._args, "job_log_keep")
        self._store.job_logs.context_lines = _log_setting(self._args, "job_log_context_lines")
        n = _log_setting(self._args, "log_buffer_lines")
        if _log_buffer.maxlen != n:
            _log_buffer = _collections.deque(_log_buffer, maxlen=n)

    def _spoolman_notify_end(self):
        """Reports the remaining filament at the end of the print."""
        if not self._spoolman or not self._spoolman_slot_spools:
            return
        self._spoolman_report(self._spoolman_unreported())

    def _spoolman_sync_midprint(self):
        """Reports incremental filament usage during the print (sync_rate interval)."""
        if not self._spoolman or not self._spoolman_slot_spools:
            return
        self._spoolman_report(self._spoolman_unreported(), min_mm=10.0)

    # ── Spoolman API handlers ─────────────────────────────────────────────────

    async def handle_kx_spoolman_status(self, request):
        """GET /kx/spoolman/status"""
        return self._json_cors({
            "configured":   bool(self._spoolman),
            "reachable":    self._spoolman_reachable if self._spoolman else False,
            "server":       self._spoolman.server_url if self._spoolman else "",
            "sync_rate":    self._spoolman.sync_rate if self._spoolman else 0,
            "slot_spools":  {str(k): v for k, v in self._spoolman_slot_spools.items()},
        })

    async def handle_kx_spoolman_spools(self, request):
        """GET /kx/spoolman/spools — proxied from Spoolman."""
        if not self._spoolman:
            return self._json_cors({"error": "Spoolman not configured"}, status=503)
        try:
            spools = await asyncio.get_event_loop().run_in_executor(
                None, self._spoolman.list_spools
            )
            return self._json_cors({"spools": spools})
        except Exception as e:
            log.warning(f"Spoolman: list_spools failed: {e}")
            return self._json_cors({"error": str(e)}, status=502)

    async def handle_kx_spoolman_set_active(self, request):
        """POST /kx/spoolman/active-spool
        Body: {"slot_map": {"0": 42, "2": 17}}  — AMS slot index → Spoolman spool ID."""
        try:
            data = await request.json()
        except Exception:
            return self._json_cors({"error": "invalid JSON"}, status=400)
        slot_map = data.get("slot_map") or data.get("slot_spools") or {}
        self._spoolman_slot_spools = {
            int(k): int(v) for k, v in slot_map.items()
            if str(v).isdigit() and int(v) > 0
        }
        # Persist per printer (its own [spoolman_<id>] section) so the
        # assignment survives bridge restarts and two AMS units do not overwrite each other.
        # (Before: NameError on `config_loader` -> nothing was saved.)
        try:
            import config_loader as _cl
            _cl.save_spool_map(self._spoolman_slot_spools, self._printer_id)
        except Exception as _e:
            log.warning("Spoolman: failed to save the slot map: %s", _e)
        self._spoolman_slot_usage = {}
        self._spoolman_slot_reported = {}
        self._spoolman_last_usage = 0.0
        return self._json_cors({"slot_spools": {str(k): v for k, v in self._spoolman_slot_spools.items()}})

    def _default_ace_dry_presets(self) -> dict[str, dict]:
        return {
            "pla": {"temp": 45, "duration_sec": 4 * 3600},
        "pla_plus": {"temp": 45, "duration_sec": 4 * 3600},
        "petg": {"temp": 50, "duration_sec": 4 * 3600},
        "tpu": {"temp": 55, "duration_sec": 4 * 3600},
        "abs_asa": {"temp": 45, "duration_sec": 8 * 3600},
        "pa_pc": {"temp": 55, "duration_sec": 12 * 3600},
        "custom_1": {"name": "Custom 1", "temp": 45, "duration_sec": 4 * 3600},
        "custom_2": {"name": "Custom 2", "temp": 45, "duration_sec": 4 * 3600},
        "custom_3": {"name": "Custom 3", "temp": 45, "duration_sec": 4 * 3600},
        }

    def _sanitize_ace_dry_presets(self, presets: dict) -> dict[str, dict]:
        out = self._default_ace_dry_presets()
        for key in list(out.keys()):
            src = presets.get(key) if isinstance(presets, dict) else None
            if not isinstance(src, dict):
                continue
            try:
                t = int(src.get("temp", out[key]["temp"]))
            except Exception:
                t = out[key]["temp"]
            try:
                d = int(src.get("duration_sec", out[key]["duration_sec"]))
            except Exception:
                d = out[key]["duration_sec"]
            out[key]["temp"] = max(30, min(80, t))
            out[key]["duration_sec"] = max(10 * 60, min(24 * 3600, d))
            if key.startswith("custom_"):
                name = str(src.get("name", out[key].get("name", key.replace("_", " ").title()))).strip()
                out[key]["name"] = name or out[key].get("name", "Custom")
        return out

    def _load_ace_dry_presets_config(self) -> dict[str, dict]:
        import configparser
        defaults = self._default_ace_dry_presets()
        cfg_path = self._find_config_path()
        if not cfg_path.is_file():
            return defaults
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read(cfg_path, encoding="utf-8")
        sec = "ace_dry_presets"
        if not cfg.has_section(sec):
            return defaults
        out = {}
        for key, d in defaults.items():
            temp_k = f"{key}_temp"
            dur_k = f"{key}_duration_sec"
            try:
                temp = int(cfg.get(sec, temp_k, fallback=str(d["temp"])))
            except Exception:
                temp = d["temp"]
            try:
                dur = int(cfg.get(sec, dur_k, fallback=str(d["duration_sec"])))
            except Exception:
                dur = d["duration_sec"]
            out[key] = {
                "temp": max(30, min(80, temp)),
                "duration_sec": max(10 * 60, min(24 * 3600, dur)),
            }
            if key.startswith("custom_"):
              name_k = f"{key}_name"
              name = cfg.get(sec, name_k, fallback=str(d.get("name", key.replace("_", " ").title()))).strip()
              out[key]["name"] = name or str(d.get("name", "Custom"))
        return out

    # -------------------------------------------------------------------------
    # MQTT callbacks (called by the reader thread)
    # -------------------------------------------------------------------------

    def _on_temp(self, payload: dict):
        d = payload.get("data") or {}
        self._state["nozzle_temp"]   = float(d.get("curr_nozzle_temp", 0))
        self._state["nozzle_target"] = float(d.get("target_nozzle_temp", 0))
        self._state["bed_temp"]      = float(d.get("curr_hotbed_temp", 0))
        self._state["bed_target"]    = float(d.get("target_hotbed_temp", 0))
        self._push_status_update()

    # -------------------------------------------------------------------------
    # Notifications (ntfy / Telegram / Discord / JSON webhook)
    # -------------------------------------------------------------------------

    def _maybe_notify(self, kobra_state: str, filename: str) -> None:
        """Notifies finish, failure and pause with an error. Called from _on_print and _on_info.
        Only notifies finish/failure of a print the bridge saw running - otherwise every
        restart with the printer sitting at "finished" sent a notification.
        ponytail: dedup by (event, file, msg) for 30 min; the same file
        reprinted and finished within 30 min does not notify again."""
        if not (getattr(self._args, "notify_url", "") or "").strip():
            return
        if kobra_state == "printing":
            self._notify_armed = True
            return
        msg = self._state.get("pause_msg", "")
        if kobra_state in ("finished", "failed"):
            if not getattr(self, "_notify_armed", False):
                return
            self._notify_armed = False
            event = "notify_finished" if kobra_state == "finished" else "notify_failed"
        elif kobra_state in ("pause", "paused") and msg:
            event = "notify_paused"
        else:
            return
        key = (event, filename, msg)
        last_key, last_t = getattr(self, "_notify_last", (None, 0.0))
        if key == last_key and time.time() - last_t < 1800:
            return
        self._notify_last = (key, time.time())
        tr = self._notify_translations()
        code = int(self._state.get("error_code") or 0)
        text = tr.get(event, event).format(
            file=filename or "-",
            msg=tr.get(f"err_{code}", msg) if code else msg,
        ).rstrip(" -:")
        title = self._state.get("printer_name") or "MoonKobra"
        threading.Thread(target=self._send_notification, args=(title, text),
                         daemon=True, name="notify").start()

    def _notify_translations(self) -> dict:
        """Texts in the language the UI saved (notify_lang), filled in with English."""
        out = {}
        lang = getattr(self._args, "notify_lang", "") or "en"
        for l in ("en", lang):
            if not re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", l):
                continue
            try:
                with open(os.path.join(_WEB_BASE, "web", "translations", f"{l}.json"), encoding="utf-8") as f:
                    out.update(json.load(f))
            except (OSError, ValueError):
                pass
        return out

    def _send_notification(self, title: str, text: str) -> None:
        """POST to notify_url. The format comes from the host: Discord and Telegram have
        their own body, ntfy gets plain text, everything else gets JSON."""
        import urllib.request
        url = self._args.notify_url.strip()
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        headers = {"Content-Type": "application/json"}
        if host.endswith("discord.com") or host.endswith("discordapp.com"):
            body = {"content": f"**{title}**: {text}"}
        elif host == "api.telegram.org":
            # URL: https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<ID>
            chat_id = urllib.parse.parse_qs(parts.query).get("chat_id", [""])[0]
            body = {"chat_id": chat_id, "text": f"{title}: {text}"}
        elif "ntfy" in host:
            # HTTP headers are latin-1: a title with accents goes in the text itself.
            body = None
            if title.isascii():
                headers = {"Title": title}
            else:
                headers, text = {}, f"{title}: {text}"
        else:
            body = {"printer": title, "message": text, "state": self._state.get("kobra_state", ""),
                    "filename": self._state.get("filename", "")}
        data = text.encode("utf-8") if body is None else json.dumps(body).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                log.info(f"Notification sent ({r.status}): {text}")
        except Exception as e:
            log.warning(f"Failed to send notification: {e}")

    def _on_fan(self, payload: dict):
        fan = (payload.get("data") or {}).get("fan_speed_pct")
        if fan is not None:
            self._state["fan_speed"] = int(fan)
            self._push_status_update()

    def _on_print(self, payload: dict):
        d = payload.get("data") or {}
        kobra_state = payload.get("state", "")
        self._state["print_state"]    = KOBRA_TO_KLIPPER_STATE.get(kobra_state, "printing")
        if kobra_state:
            self._state["kobra_state"] = kobra_state

        # Turn the camera on automatically when a print starts (option in the settings).
        # Centralized here to cover every print start path (OrcaSlicer + UI).
        # _camera_autostarted prevents multiple triggers per print.
        if kobra_state == "printing":
            if (getattr(self._args, "camera_on_print", 0)
                    and not self._camera_autostarted
                    and not self._camera_user_stopped):
                self._camera_autostarted = True
                try:
                    self._camera_start_ts = time.time()
                    self.client.start_camera()
                    log.info("Camera turned on automatically at print start")
                except Exception as e:
                    log.warning(f"Camera auto-start failed: {e}")
        elif kobra_state in ("free", "finished", "stoped", "canceled"):
            self._camera_autostarted = False
            self._camera_user_stopped = False  # free again for the next print
        
        if kobra_state in ("pause", "paused", "failed"):
            try:
                error_code = int(payload.get("code") or 0)
            except (TypeError, ValueError):
                error_code = 0
            pause_msg = payload.get("msg", "") or ANYCUBIC_ERROR_MESSAGES.get(error_code, "")
            if pause_msg:
                self._state["error_code"] = error_code
                self._state["pause_msg"] = pause_msg
                log.warning(f"Printer {'failed' if kobra_state == 'failed' else 'paused'}: [{error_code}] {pause_msg}")
        elif kobra_state in ("resuming", "resumed", "printing", "finished", "stoped", "canceled", "free"):
            self._state["error_code"] = 0
            self._state["pause_msg"] = ""
        self._maybe_notify(kobra_state, d.get("filename") or self._state.get("filename", ""))

        # Job history: detect the print start
        if kobra_state == "printing" and not self._current_job_id:
            filename = d.get("filename", self._state.get("filename", ""))
            if filename:
                gf = self._store.get_file_by_name(filename)
                # Outside the store (started from the printer's screen / factory file)
                # it also goes into the history, with a log - but without _current_job_filename,
                # which drives "delete the file from the printer after printing".
                self._current_job_id = self._store.start_job(
                    gcode_file_id=gf["id"] if gf else "",
                    printer_id=self._printer_id,
                    filename=filename,
                )
                if gf:
                    self._current_job_filename = filename
                self._store.job_logs.start(self._current_job_id, filename)
                log.info(f"Job started: {self._current_job_id} for {filename}")
            self._spoolman_slot_usage = {}
            self._spoolman_slot_reported = {}
            self._spoolman_last_usage = 0.0
            # Must be "now", not 0.0/epoch: _spoolman_sync_midprint() checks
            # time.time() - _spoolman_last_sync >= sync_rate in the poll loop,
            # and runs BEFORE _spoolman_attribute_tick() in the same iteration
            # (see the poll loop). With last_sync=0.0 that condition is
            # already true on the first tick after the print start, before
            # any per-slot usage was attributed - _spoolman_unreported()
            # then fell back to splitting the printer's whole supplies_usage
            # (possibly already non-zero/inherited) evenly among all
            # mapped spools, silently deducting filament from spools
            # that were not even used in this print (seen live, a few grams
            # per spool per print).
            self._spoolman_last_sync = time.time()

        # Job history: detect the print end
        if kobra_state in ("finished",) and self._current_job_id:
            self._store.finish_job(self._current_job_id, status="completed")
            log.info(f"Job finished: {self._current_job_id}")
            self._spoolman_notify_end()
            self._store.job_logs.stop(self._current_job_id)
            self._current_job_id = ""
            # Optional cleanup (Settings -> Print): only for files that
            # are also in the bridge's own G-code store - never for prints
            # started straight from the printer/Anycubic Slicer, which would otherwise
            # be deleted with no copy left anywhere (delete the file from the
            # printer after a successful print). Deliberately only on a clean
            # "finished" - stoped/canceled prints keep the file.
            if getattr(self._args, "delete_printer_file_after_print", 0) and self._current_job_filename:
                self._delete_printer_file_fire_and_forget(self._current_job_filename)
            self._current_job_filename = ""
        elif kobra_state in ("stoped", "canceled") and self._current_job_id:
            self._store.finish_job(self._current_job_id, status="cancelled")
            log.info(f"Job cancelled: {self._current_job_id}")
            self._spoolman_notify_end()
            self._store.job_logs.stop(self._current_job_id)
            self._current_job_id = ""
            self._current_job_filename = ""

        # Terminal states (successful finish AND stop/cancel) must leave the
        # same clean final state - a "finished" print used to only clear
        # file_ready (Issue #29), leaving progress/filename/duration/layer
        # stuck on the last job's values until the *next* print
        # happened to overwrite them (Issue #102).
        if kobra_state in ("finished", "stoped", "canceled"):
            self._state["progress"] = 0.0
            self._state["filename"] = ""
            self._state["file_ready"] = ""
            self._state["print_duration"] = 0
            self._state["remain_time"] = 0
            self._state["slicer_time"] = 0
            self._state["layer_height"] = 0.0
            self._state["first_layer_height"] = 0.0
            self._state["supplies_usage"] = 0
            self._state["curr_layer"] = 0
            self._state["total_layers"] = 0
            self._thumbnail_b64 = ""
        else:
            # Only adopt the payload's filename outside terminal states - the
            # printer often still reports the just-finished job's name
            # in the same "finished"/"stoped"/"canceled" message that triggered
            # the reset above, which would undo it right away.
            self._state["filename"] = d.get("filename", self._state["filename"])
        # Pre-print phases (leveling/preheating/check) report their own
        # "progress" - passing it on would make display_status.progress/
        # virtual_sdcard.progress jump non-monotonically when the real print
        # starts and the value resets (Issue #102).
        if "progress" in d and kobra_state not in ("preheating", "auto_leveling", "checking", "updated", "init"):
            self._state["progress"]   = float(d["progress"]) / 100.0
            self._state["progress_estimated"] = False
            self._prog_anchor = (self._state["progress"], int(d.get("remain_time") or 0) * 60, time.time())
        if kobra_state in ("finished", "stoped", "canceled"):
            self._prog_anchor = None
            self._state["progress_estimated"] = False
        if "print_time" in d:
            self._state["print_duration"] = int(d["print_time"]) * 60
        if "remain_time" in d:
            self._state["remain_time"] = int(d["remain_time"]) * 60
        if "curr_layer" in d:
            self._state["curr_layer"] = d["curr_layer"]
        if "total_layers" in d:
            self._state["total_layers"] = d["total_layers"]
        if "taskid" in d:
            self._state["taskid"] = str(d["taskid"])
        if "supplies_usage" in d:
            self._state["supplies_usage"] = int(d["supplies_usage"])
        settings = d.get("settings") or {}
        if "print_speed_mode" in settings:
            self._state["print_speed_mode"] = int(settings["print_speed_mode"])
        self._push_status_update()

    def _estimate_progress_if_stale(self):
        """The Kobra X sometimes stops sending print/report mid-print
        (seen after a nozzle_heating coming from the G-code) and info/report stays
        frozen. With no real report for _STALE_REPORT_SEC, estimate from time:
        from the last real report (progress + the printer's remaining time)
        or, after a bridge restart, from the job start in the DB + the
        slicer time. Never moves progress backwards; the next real report replaces it.
        ponytail: a long pause mid-print counts as printed time until the next report."""
        s = self._state
        if s["print_state"] != "printing" or not s.get("filename"):
            return
        now = time.time()
        a = getattr(self, "_prog_anchor", None)
        if a and now - a[2] < _STALE_REPORT_SEC:
            return
        gf = self._store.get_file_by_name(s["filename"]) or {}
        est = int(gf.get("est_print_time_sec") or s.get("slicer_time") or 0)
        job_t0 = self._store.running_job_start(s["filename"])
        if job_t0 is not None:
            s["print_duration"] = int(max(now - job_t0, 0))
        if a:
            p0, remain, t0 = a
            if remain <= 0:
                remain = est * (1 - p0)
        else:
            if job_t0 is None:
                return
            p0, remain, t0 = 0.0, est, job_t0
        if remain <= 0:
            return
        el = max(now - t0, 0)
        p = p0 + (1 - p0) * min(el / remain, 0.99)
        if p > s["progress"]:
            s["progress"] = p
        s["remain_time"] = int(max(remain - el, 0))
        s["progress_estimated"] = True

    def _on_info(self, payload: dict):
        d = payload.get("data") or {}
        # Only adopt the MQTT name if there is no custom name (env or per-printer config)
        if not env_loader.get("BRIDGE_PRINTER_NAME") and not getattr(self, "_name_locked", False):
            self._state["printer_name"] = d.get("printerName", self._state["printer_name"])
        self._state["firmware_version"] = d.get("version", self._state["firmware_version"])
        # The real print state lives in info/report, inside the nested
        # project.state ("printing"/"paused"/...). The top-level data.state is only
        # the device state ("busy"/"free") and would swallow "paused".
        project = d.get("project") or {}
        proj_state = project.get("state", "")
        kobra_state = proj_state or d.get("state", "")
        if kobra_state:
            # An unknown sub-state with the printer "busy" is still printing.
            fallback = "printing" if d.get("state") == "busy" else "standby"
            self._state["print_state"] = KOBRA_TO_KLIPPER_STATE.get(kobra_state, fallback)
            self._state["kobra_state"] = kobra_state
            # Hide the upload banner once the print ends (Issue #29) - the state also
            # arrives through info/report (project.state) depending on the printer, not only print/report.
            # The layer fields must reset here too (Issue #102) - info/report is the
            # only source of curr_layer/total_layers on some printers, and otherwise they
            # stay stuck on the last job's values forever.
            if kobra_state in ("finished", "stoped", "canceled"):
                self._state["file_ready"] = ""
                self._state["curr_layer"] = 0
                self._state["total_layers"] = 0
            # Camera auto-start here too (OrcaSlicer often reports the start through info/report).
            # The _camera_autostarted guard prevents a double start with _on_print.
            if kobra_state == "printing":
                if (getattr(self._args, "camera_on_print", 0)
                        and not self._camera_autostarted
                        and not self._camera_user_stopped):
                    self._camera_autostarted = True
                    try:
                        self._camera_start_ts = time.time()
                        self.client.start_camera()
                        log.info("Camera turned on automatically at print start")
                    except Exception as e:
                        log.warning(f"Camera auto-start failed: {e}")
            elif kobra_state in ("free", "finished", "stoped", "canceled"):
                self._camera_autostarted = False
                self._camera_user_stopped = False  # free again for the next print
            self._maybe_notify(kobra_state, project.get("filename") or self._state.get("filename", ""))
        if project:
            # info/report's project can freeze mid-print
            # (seen on the Kobra X: stuck at nozzle_heating/1% while printing at
            # 11%). On the same file, a project behind the known progress is
            # stale and does not overwrite progress/layer/time. Terminal states
            # reset the progress, so the next print starts at 0.
            same_job = project.get("filename", "") == self._state.get("filename", "")
            stale = (same_job and "progress" in project
                     and float(project["progress"]) / 100.0 < self._state.get("progress", 0.0))
            if "filename" in project:
                self._state["filename"] = project["filename"]
            # Same non-monotonic progress guard as _on_print (Issue #102).
            if not stale:
                if "progress" in project and kobra_state not in ("preheating", "auto_leveling", "checking", "updated", "init"):
                    self._state["progress"] = float(project["progress"]) / 100.0
                if "print_time" in project:
                    self._state["print_duration"] = int(project["print_time"]) * 60
                if "remain_time" in project:
                    self._state["remain_time"] = int(project["remain_time"]) * 60
                if "curr_layer" in project:
                    self._state["curr_layer"] = project["curr_layer"]
                if "total_layers" in project:
                    self._state["total_layers"] = project["total_layers"]
        t = d.get("temp") or {}
        if t:
            self._state["nozzle_temp"]   = float(t.get("curr_nozzle_temp", 0))
            self._state["nozzle_target"] = float(t.get("target_nozzle_temp", 0))
            self._state["bed_temp"]      = float(t.get("curr_hotbed_temp", 0))
            self._state["bed_target"]    = float(t.get("target_hotbed_temp", 0))
        urls = d.get("urls") or {}
        if urls.get("fileUploadurl"):
            self._state["upload_url"] = urls["fileUploadurl"]
        if urls.get("rtspUrl"):
            self._state["camera_url"] = urls["rtspUrl"]
            self.camera_cache.set_url(urls["rtspUrl"])
        fan = d.get("fan_speed_pct")
        if fan is not None:
            self._state["fan_speed"] = int(fan)
        speed_mode = d.get("print_speed_mode")
        if speed_mode is not None:
            self._state["print_speed_mode"] = int(speed_mode)
        self._push_status_update()

    def _on_skip(self, payload: dict):
        """skip/report callback (skip-part feature, v0.9.10).

        The printer ALWAYS reports the list of already skipped objects here
        (objects_skip_parts), whether in query_obj or after skip/start.
        The full object list comes from file/report.
        """
        d = payload.get("data") or {}
        skipped = d.get("objects_skip_parts") or d.get("skipped") or d.get("skipped_parts") or []
        # While a pre-print skip is still pending, ignore early empty reports
        # so the UI does not jump back before the printer confirms the skip.
        now = time.time()
        if (not skipped and self._pending_preprint_skip
                and now <= self._pending_preprint_skip_deadline):
            return

        # During an active print, skip states are effectively monotonic.
        # Some firmware reports come back empty/partial mid-print;
        # they must not remove already confirmed skipped objects from the UI.
        existing_skipped = [str(n) for n in (self._skip_state.get("skipped") or []) if n]
        existing_set = set(existing_skipped)
        incoming_skipped = [str(n) for n in (skipped or []) if n]
        incoming_set = set(incoming_skipped)
        active_print = self._state.get("print_state") in ("printing", "paused")
        if active_print and existing_set:
            if not incoming_set:
                skipped = list(existing_skipped)
            elif not incoming_set.issuperset(existing_set):
                merged = list(existing_skipped)
                for n in incoming_skipped:
                    if n not in existing_set:
                        merged.append(n)
                skipped = merged

        # Release the pending lock when the printer confirms the requested objects
        if self._pending_preprint_skip and set(skipped) >= set(self._pending_preprint_skip):
            self._pending_preprint_skip = []
            self._pending_preprint_skip_deadline = 0.0
        self._skip_state = {
            "skipped":  list(skipped),
            "ts":       int(time.time()),
        }
        if payload.get("state") == "done" or payload.get("code") == 200:
            log.info(f"Skip response: state={payload.get('state')} code={payload.get('code')} skipped={skipped}")

    def _delete_printer_file_fire_and_forget(self, filename: str) -> None:
        """Deletes a file from the printer's own storage without waiting for
        the reply - called from _on_print(), which runs on the MQTT reader thread
        itself, so blocking here (as _wait_for_file_action
        does) would deadlock: the file/report reply that would unblock it is
        dispatched by that very thread. Fire-and-forget is safe because what
        matters for correctness here is the bridge's own copy in the G-code store;
        a failed delete only leaves the printer's storage as it is
        (Settings -> Print -> "Delete the file from the printer after a successful print")."""
        try:
            self.client.publish(
                "file", "deleteBatch",
                {"root": "local", "files": [{"path": "/", "filename": filename}]},
                timeout=0,
            )
            log.info(f"Deletion from the printer's storage requested for {filename} after a successful print")
        except Exception as e:
            log.warning(f"Post-print deletion request failed for {filename}: {e}")

    def _wait_for_file_action(self, action: str, send_fn, timeout: float = 8.0) -> dict | None:
        """Sends a file/* MQTT request (through send_fn, which must call
        self.client.publish(..., timeout=0) without waiting for a reply) and blocks the
        calling thread until a matching file/report with this `action` arrives
        through _on_file, or until the timeout.

        Needed because the printer's publish() return for actions such as
        listLocal/deleteBatch is only a generic immediate-ACK skeleton
        (code=0, empty fields) - the real answer is a separate file/report
        message that arrives later, like the existing fileDetails pattern.
        Must be called from a worker thread (e.g. through run_in_executor), not
        from the asyncio event loop, since it blocks on a threading.Event.
        """
        event = threading.Event()
        waiter = {"event": event, "result": None}
        self._file_action_waiters[action] = waiter
        try:
            send_fn()
            event.wait(timeout)
            return waiter["result"]
        finally:
            if self._file_action_waiters.get(action) is waiter:
                del self._file_action_waiters[action]

    def _on_buried(self, payload: dict):
        """buried/report - the printer's own analytics event, fired once per
        print start (verified live on a real Kobra X: it fires the same
        for prints started from Anycubic Slicer Next and from OrcaSlicer/the
        bridge). Carries gcode_size/estimate_duration/total_layers, which
        _build_file_metadata() uses as a fallback for files not in our
        GCodeStore (Issue #102), plus the printer's storage usage."""
        d = payload.get("data") or {}
        task_name = d.get("task_name") or ""
        if not task_name:
            return
        self._buried_cache = {
            "task_name": task_name,
            "gcode_size": int(d.get("gcode_size") or 0),
            "estimate_duration": int(d.get("estimate_duration") or 0),
            "total_layers": int(d.get("total_layers") or 0),
        }
        self._state["storage_total_mb"] = int(d.get("storage_total") or 0)
        self._state["storage_used_mb"] = int(d.get("storage_used") or 0)
        log.info(
            f"buried/report: {task_name}  size={d.get('gcode_size')}  "
            f"est={d.get('estimate_duration')}s  layers={d.get('total_layers')}"
        )

    def _on_file(self, payload: dict):
        # Deliver first to any pending listLocal/deleteBatch waiter (see
        # _wait_for_file_action) - those actions carry no file_details/
        # thumbnail payload of their own, so this does not interfere with the
        # handling below.
        action = payload.get("action") or ""
        waiter = self._file_action_waiters.get(action)
        if waiter is not None:
            waiter["result"] = payload
            waiter["event"].set()

        d = payload.get("data") or {}
        details = d.get("file_details") or {}
        thumb = details.get("thumbnail") or details.get("png_image") or ""
        file_name = d.get("filename") or details.get("filename") or self._last_uploaded_file
        active_print = self._state.get("print_state") in ("printing", "paused")
        current_print_file = self._state.get("filename") or ""
        # Uploads during a running print must not overwrite the
        # active progress preview.
        if thumb and (not active_print or (file_name and file_name == current_print_file)):
            self._thumbnail_b64 = thumb
            log.info(f"Thumbnail received: {len(thumb)} base64 characters")
        # Skip part: object list + optional SVG (v0.9.10)
        objs = details.get("objects_skip_parts") or []
        svg  = details.get("svg_image") or ""
        if objs:
            filename = file_name
            if filename:
                try:
                    self._store.update_file_objects(filename, objs, svg)
                    log.info(f"Objects to skip in {filename}: {len(objs)} ({'with SVG' if svg else 'no SVG'})")
                except Exception as e:
                    log.warning(f"update_file_objects failed: {e}")
        self._push_status_update()

    def _apply_preprint_skip_after_start(self, names: list[str], retries: int = 20, delay_s: float = 0.75):
        """Sends the skip command only after the printer entered the printing state.

        Before that, the command goes nowhere (no active print).
        """
        wanted = [str(n) for n in (names or []) if isinstance(n, str) and n]
        if not wanted:
            return False
        for i in range(max(1, int(retries))):
            try:
                if self._state.get("print_state") not in ("printing", "paused"):
                    time.sleep(max(0.1, float(delay_s)))
                    continue
                resp = self.client.skip_objects(wanted)
                if resp is not None:
                    log.info(f"Pre-print skip applied ({len(wanted)} objects) on attempt {i+1}/{retries}")
                    self._pending_preprint_skip = []
                    self._pending_preprint_skip_deadline = 0.0
                    return True
            except Exception as e:
                log.debug(f"Pre-print skip attempt {i+1}/{retries} failed: {e}")
            time.sleep(max(0.1, float(delay_s)))
        log.warning(f"Pre-print skip could not be confirmed after {retries} attempts")
        self._pending_preprint_skip = []
        self._pending_preprint_skip_deadline = 0.0
        return False

    @staticmethod
    def _detect_filament_mode(boxes: list, head_tools_model: int = -1) -> str:
        """Detects the active filament topology mode.

        Modes:
        - toolhead: toolhead slots only
        - ace_direct: ACE channels mapped directly, no box on the toolhead.
          Covers one unit (Kobra X) and also several daisy-chained units
          (Kobra S1 with 2+ ACE Pro, Issue #95) — each unit contributes a
          block of 4 global slots at box_id * 4.
        - ace_hub: toolhead + ACE through a hub (slot 4 as the hub path)
        """
        toolhead = any(b.get("id") == -1 for b in boxes)
        ace = any(b.get("id", -1) >= 0 for b in boxes)
        if ace and toolhead:
            return "ace_hub"
        if ace:
            return "ace_direct"
        return "toolhead"

    @staticmethod
    def _aggregate_slots(boxes: list, mode: str = "toolhead") -> tuple:
        """Aggregates the multi_color_box list into a flat list of global slots."""
        toolhead = next((b for b in boxes if b.get("id") == -1), None)
        ace_boxes = sorted(
            [b for b in boxes if b.get("id", -1) >= 0],
            key=lambda b: b["id"]
        )

        global_slots: list = []
        global_loaded: int = -1

        if mode == "toolhead":
            if toolhead:
                for local_idx, s in enumerate(toolhead.get("slots") or []):
                    s = dict(s)
                    s["global_index"] = local_idx
                    s["box_id"] = -1
                    global_slots.append(s)
                loaded = toolhead.get("loaded_slot", -1)
                if loaded >= 0:
                    global_loaded = loaded
            return global_slots, global_loaded

        if mode == "ace_direct":
            # One or more ACE units, no toolhead buffer (Kobra X: 1 unit,
            # Kobra S1: up to 2+ units, Issue #95). Global index =
            # box_id * 4 + local slot, so the numbering matches the
            # //4-%4 fallback of _global_to_box_slot and stays stable
            # regardless of report order.
            for ace in ace_boxes:
                ace_id = int(ace["id"])
                base = ace_id * 4
                for local_idx, s in enumerate((ace.get("slots") or [])[:4]):
                    s = dict(s)
                    s["global_index"] = base + local_idx
                    s["box_id"] = ace_id
                    global_slots.append(s)
                ace_loaded = ace.get("loaded_slot", -1)
                if 0 <= ace_loaded < 4:
                    global_loaded = base + ace_loaded
            return global_slots, global_loaded

        # ace_hub
        if toolhead:
            for local_idx, s in enumerate((toolhead.get("slots") or [])[:3]):
                s = dict(s)
                s["global_index"] = local_idx
                s["box_id"] = -1
                global_slots.append(s)
            th_loaded = toolhead.get("loaded_slot", -1)
            if 0 <= th_loaded <= 2:
                global_loaded = th_loaded

        for ace in ace_boxes:
            ace_id = ace["id"]
            base = 3 + ace_id * 4
            for local_idx, s in enumerate(ace.get("slots") or []):
                s = dict(s)
                s["global_index"] = base + local_idx
                s["box_id"] = ace_id
                global_slots.append(s)
            ace_loaded = ace.get("loaded_slot", -1)
            if ace_loaded >= 0:
                global_loaded = base + ace_loaded

        return global_slots, global_loaded

    def _global_to_box_slot(self, global_index: int) -> tuple:
        """Converts a global slot index into (box_id, local_slot_index)."""
        for s in self._ams_slots:
            if s.get("global_index") == global_index:
                return s.get("box_id", -1), s.get("index", global_index)

        ace_present = any(s.get("box_id", -1) >= 0 for s in self._ams_slots)
        if self._filament_mode == "ace_direct" and ace_present:
            return global_index // 4, global_index % 4
        if not ace_present or global_index < 3:
            return -1, global_index
        offset = global_index - 3
        return offset // 4, offset % 4

    def _slot_to_print_ams_index(self, global_index: int) -> int:
      """Converts the UI/global slot index into the printer's print/start ams_index.

      In ace_hub mode, print/start uses global channel numbering where
      the toolhead channels take 1..3 and ACE0 starts at index 4.
      """
      idx = int(global_index)
      if self._filament_mode == "ace_hub":
        box_id, local_slot = self._global_to_box_slot(idx)
        if box_id >= 0:
          return 4 + box_id * 4 + int(local_slot)
        return idx
      return idx

    def _slot_usable_for_print(self, global_index: int) -> bool:
        """Whether a global slot can be used in the current filament mode."""
        slot = next((s for s in self._ams_slots if int(s.get("global_index", -1)) == int(global_index)), None)
        if not slot:
            return False
        if int(slot.get("status", 0)) != 5:
            return False

        box_id = int(slot.get("box_id", -1))
        if self._filament_mode == "ace_hub":
          # In hub mode, both the toolhead channels (0..2) and the ACE ones can print.
          return box_id == -1 or box_id >= 0
        if self._filament_mode == "ace_direct":
            return box_id >= 0
        return box_id == -1

    def _loaded_slots_for_print(self) -> list[tuple[int, dict]]:
        """Loaded slots filtered by the current filament mode."""
        loaded = [
            (int(s.get("global_index", i)), s)
            for i, s in enumerate(self._ams_slots)
        if s.get("status") == 5 and self._slot_usable_for_print(int(s.get("global_index", i)))
        ]
        return loaded

    def _select_loaded_slots_for_print(self, warn_on_empty_default: bool = False) -> list[tuple[int, dict]]:
        """Returns the loaded slots, honouring default_ams_slot when configured."""
        default_slot = getattr(self._args, "default_ams_slot", "auto")
        all_loaded = self._loaded_slots_for_print()
        if default_slot == "auto":
            return all_loaded

        try:
            slot_idx = int(default_slot)
        except ValueError:
            return all_loaded

        selected = [(i, s) for i, s in all_loaded if i == slot_idx]
        if selected:
            return selected

        if warn_on_empty_default:
            log.warning(f"Default slot {slot_idx} is empty - falling back to auto")
        return all_loaded

    @staticmethod
    def _slot_color_rgba(slot: dict) -> list[int]:
        color = slot.get("color", [255, 255, 255])
        if isinstance(color, list) and len(color) >= 3:
            return [int(color[0]), int(color[1]), int(color[2]), 255]
        return [255, 255, 255, 255]

    def _build_auto_ams_box_mapping(
        self,
        warn_on_empty_default: bool = False,
        loaded_slots: list[tuple[int, dict]] | None = None,
    ) -> list[dict]:
        """Builds the print mapping from the loaded slots (no explicit dialog assignments)."""
        loaded = loaded_slots
        if loaded is None:
            loaded = self._select_loaded_slots_for_print(warn_on_empty_default=warn_on_empty_default)
        if not loaded:
            return []
        loaded_map = {gidx: s for gidx, s in loaded}
        max_idx = max(loaded_map.keys())
        # The printer reads ams_box_mapping as an ordered list (entry N = TN).
        # Missing slots must go in as placeholders, otherwise everything shifts.
        # A placeholder must NOT point to a physically empty tray: the printer
        # rejects such an entry even for a tool the G-code never calls (printing
        # with Filament 4 and the slot below empty fails; all full works). Point
        # gap placeholders at a tray that is surely loaded instead of the gap's own
        # (empty) index.
        fallback_gidx = max_idx  # highest loaded slot -> loaded + printable
        fallback_slot = loaded_map[fallback_gidx]
        fallback_ams = self._slot_to_print_ams_index(fallback_gidx)
        result = []
        for i in range(max_idx + 1):
            if i in loaded_map:
                s = loaded_map[i]
                result.append({
                    "paint_index": i,
                    "ams_index": self._slot_to_print_ams_index(i),
                    "paint_color": [255, 255, 255, 255],
                    "ams_color": self._slot_color_rgba(s),
                    "material_type": s.get("type", "PLA"),
                })
            else:
                result.append({
                    "paint_index": i,
                    "ams_index": fallback_ams,
                    "paint_color": [255, 255, 255, 255],
                    "ams_color": self._slot_color_rgba(fallback_slot),
                    "material_type": fallback_slot.get("type", "PLA"),
                })
        return result

    def _build_assigned_ams_box_mapping(self, assignments: list) -> tuple[list[dict], int, int]:
        """Builds the print mapping from the UI's filament assignments.

        Returns (mapping, unused_count, invalid_count).
        """
        slot_by_global_index = {
            int(s.get("global_index", i)): s
            for i, s in enumerate(self._ams_slots)
        }
        ams_box_mapping: list[dict] = []
        unused_count = 0
        invalid_count = 0

        for i, a in enumerate(assignments):
            try:
                if a.get("is_used") is False:
                    unused_count += 1
                    continue
                global_slot = int(a["slot_index"])
            except (ValueError, TypeError, KeyError):
                invalid_count += 1
                continue

            if global_slot < 0:
                unused_count += 1
                continue
            if not self._slot_usable_for_print(global_slot):
                invalid_count += 1
                continue

            slot = slot_by_global_index.get(global_slot, {})
            ams_box_mapping.append({
                # Keep the slicer's paint indices (they may be sparse when paint 0 is unused).
                "paint_index": a.get("paint_index", i),
                "ams_index": self._slot_to_print_ams_index(global_slot),
                "paint_color": a.get("paint_color", [255, 255, 255, 255]),
                "ams_color": self._slot_color_rgba(slot),
                "material_type": slot.get("type", a.get("material", "PLA")),
            })

        return ams_box_mapping, unused_count, invalid_count

    def _box_local_to_global(self, box_id: int, local_slot: int, boxes: list) -> int:
        """Converts (box_id, local slot) into the global slot index for the current topology."""
        if box_id == -1:
            return local_slot
        if self._filament_mode == "ace_direct":
            # Multi-ACE (Issue #95): each unit takes its own block of 4.
            # Identical to the old `return local_slot` for a single unit (id 0).
            return box_id * 4 + local_slot
        return 3 + box_id * 4 + local_slot

    def _slot_activity_map(self, boxes: list, global_loaded: int = -1) -> dict:
        """Builds {global_slot_index: loading|unloading} from the feed_status data."""
        # Note: every box is considered — the old primary_ace_id filter (skipping
        # every ACE box but the first in ace_direct mode) was removed, since
        # slot aggregation now handles several ACE units (Issue #95).
        activity: dict = {}
        for box in boxes:
            fs = box.get("feed_status") or {}
            current_status = int(fs.get("current_status", -1))
            local_slot = int(fs.get("slot_index", -1))
            feed_type = int(fs.get("type", -1))
            if current_status in (-1, 10, 11) or local_slot < 0:
                continue
            box_slots = box.get("slots") or []
            if local_slot >= len(box_slots) or (box_slots[local_slot] or {}).get("status") != 5:
                continue
            if feed_type == 1:
                act = "loading"
            elif feed_type == 2:
                act = "unloading"
            else:
                continue
            global_slot = self._box_local_to_global(int(box.get("id", -1)), local_slot, boxes)
            if feed_type == 1 and self._pending_load_slot >= 0 and global_slot != self._pending_load_slot:
                # Ignore transient loading slots reported by the firmware that differ from the requested target.
                if global_loaded >= 0 and global_loaded != self._pending_load_slot:
                    activity[global_loaded] = "unloading"
                continue
            if feed_type == 1 and global_loaded >= 0 and global_slot != global_loaded:
                # During a slot change the firmware reports the target slot right away,
                # while the previously loaded slot is still being unloaded.
                activity[global_loaded] = "unloading"
            activity[global_slot] = act
        return activity

    def _on_multicolor_box(self, payload: dict):
        if payload.get("state") == "failed":
            req = getattr(self, "_last_ams_set_request", None)
            log.warning(
                f"multiColorBox setInfo rejected by the printer: request={req}  raw_response={payload.get('data')}"
            )
            self._state["last_ams_set_error"] = True
            return
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            log.warning(f"multiColorBox/report: unexpected data format: {data!r}")
            return
        boxes = data.get("multi_color_box") or []
        if not boxes:
            # No multiColorBox data — attribute anyway (no transitions to skip)
            self._spoolman_attribute_tick({})
            return
        self._state["last_ams_set_error"] = False
        self._head_tools_model = int(data.get("head_tools_model", self._head_tools_model))
        self._filament_mode = self._detect_filament_mode(boxes, self._head_tools_model)
        self._state["filament_mode"] = self._filament_mode

        global_slots, global_loaded = self._aggregate_slots(boxes, self._filament_mode)
        self._ams_loaded_slot = global_loaded
        self._update_ace_drying_state(data, boxes)
        for box in boxes:
            bid = int(box.get("id", -1))
            if 0 <= bid <= 3 and "auto_feed" in box:
                self._ace_auto_feed[bid] = int(box["auto_feed"])
        if self._pending_load_slot >= 0 and global_loaded == self._pending_load_slot:
          self._pending_load_slot = -1
        activity_map = self._slot_activity_map(boxes, global_loaded)
        for s in global_slots:
            s["activity"] = activity_map.get(s.get("global_index"), "")
        self._spoolman_attribute_tick(activity_map)

        # Tip forming: after loading (status=10) or unloading (status=11)
        # the original slicer automatically sends type=3 (extruder retraction).
        # Check ALL boxes so events triggered by the ACE are handled correctly.
        for box in boxes:
            fs = box.get("feed_status") or {}
            current_status = fs.get("current_status")
            slot_index = fs.get("slot_index", 0)
            box_id = box.get("id", -1)
            if current_status in (10, 11):
                def _tip_form(bi=box_id, si=slot_index, cs=current_status):
                    import time; time.sleep(2)
                    self.client.publish(
                        "multiColorBox", "feedFilament",
                        {"multi_color_box": [{"id": bi, "feed_status": {"slot_index": si, "type": 3}}]},
                        timeout=0
                    )
                    log.info(f"Tip forming (type=3) after status={cs} box={bi} slot={si}")
                threading.Thread(target=_tip_form, daemon=True).start()

        if global_slots:
            self._ams_slots = global_slots
            log.info(f"AMS slots received: {len(global_slots)}, loaded_slot={self._ams_loaded_slot}")
            self._push_status_update()

    def _update_ace_drying_state(self, data: dict, boxes: list):
        """Extracts the ACE drying state from the multiColorBox report/getInfo payloads."""
        ace_ids = sorted({int(b.get("id", -1)) for b in boxes if int(b.get("id", -1)) >= 0})
        self._ace_box_ids = [i for i in ace_ids if 0 <= i <= 3]

        def _num_from(src: dict, keys: tuple[str, ...], default=None):
            for k in keys:
                v = src.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except Exception:
                        return default
            return default

        def _humidity_from(src: dict, default=None):
            return _num_from(src, ("humidity", "current_humidity", "cur_humidity", "relative_humidity", "humidity_value"), default)

        def _current_temp_from(src: dict, default=None):
            return _num_from(src, ("current_temp", "cur_temp", "temperature", "temp", "drying_temp", "chamber_temp"), default)

        def _minutes_from(src: dict, key: str, default=0):
          raw = src.get(key, default)
          try:
            value = int(float(raw))
          except Exception:
            return int(default)
          # Some firmware payloads report the dryer times in seconds while the UI uses minutes.
          if value > (24 * 60):
            return max(0, int(round(value / 60.0)))
          return max(0, value)

        per_unit: list[dict] = []
        for box in boxes:
            bid = int(box.get("id", -1))
            if bid < 0:
                continue

            bs = box.get("drying_status") or box.get("drying_settings")
            bs = bs if isinstance(bs, dict) else {}
            hu = _humidity_from(bs, _humidity_from(box))
            ct = _current_temp_from(bs, _current_temp_from(box))

            if bs or hu is not None or ct is not None:
                per_unit.append({
                    "id": bid,
                    "status": int(bs.get("status", 0)),
                    "target_temp": int(bs.get("target_temp", 0)),
                "duration": _minutes_from(bs, "duration", 0),
                "remain_time": _minutes_from(bs, "remain_time", 0),
                    "humidity": hu,
                    "current_temp": ct,
                })

        src = data.get("drying_status") or data.get("drying_settings")
        if not isinstance(src, dict):
            for box in boxes:
                if int(box.get("id", -1)) < 0:
                    continue
                cand = box.get("drying_status") or box.get("drying_settings")
                if isinstance(cand, dict):
                    src = cand
                    break

        if isinstance(src, dict):
          cur = self._state.get("ace_drying") or {}
          active = [u for u in per_unit if u.get("status", 0)]
          primary = active[0] if active else (per_unit[0] if per_unit else {})
          self._state["ace_drying"] = {
            "status": int(src.get("status", cur.get("status", 0))),
            "target_temp": int(src.get("target_temp", cur.get("target_temp", 0))),
            "duration": _minutes_from(src, "duration", cur.get("duration", 0)),
            "remain_time": _minutes_from(src, "remain_time", cur.get("remain_time", 0)),
            "humidity": _humidity_from(src, primary.get("humidity", cur.get("humidity"))),
            "current_temp": _current_temp_from(src, primary.get("current_temp", cur.get("current_temp"))),
            "units": per_unit,
          }
        elif per_unit:
            active = [u for u in per_unit if u.get("status", 0)]
            primary = active[0] if active else per_unit[0]
            self._state["ace_drying"] = {
                "status": int(primary.get("status", 0)),
                "target_temp": int(primary.get("target_temp", 0)),
                "duration": int(primary.get("duration", 0)),
                "remain_time": int(primary.get("remain_time", 0)),
                "humidity": primary.get("humidity"),
                "current_temp": primary.get("current_temp"),
                "units": per_unit,
            }

    def _on_light(self, payload: dict):
        d = payload.get("data") or {}
        on = bool(d.get("status", 0))
        self._state["light_on"]         = on
        self._state["light_brightness"] = int(d.get("brightness", 80))
        # The Kobra X firmware turns the chamber light on by itself on video/startCapture.
        # If the user had explicitly turned it off, restore that right after a camera start,
        # so enabling the camera never flips the light back on on its own.
        # ponytail: reacts to the firmware's own light report; assumes a startCapture->light-on
        # coupling (needs a real-printer check). Acts only when the user set the light off.
        if (on and self._light_desired is False
                and time.time() - self._camera_start_ts < 6.0):
            threading.Thread(target=self._restore_light_off, daemon=True,
                             name="light-restore").start()
        self._push_status_update()

    def _restore_light_off(self) -> None:
        """Re-asserts the light off after the camera turned it on against the user's wish."""
        try:
            self.client.publish("light", "control",
                                 {"type": 3, "status": 0,
                                  "brightness": self._state["light_brightness"]},
                                 timeout=0)
            self._state["light_on"] = False
        except Exception as e:
            log.warning(f"Could not restore the light to off after camera start: {e}")

    @staticmethod
    def _cmp_ver(a: str, b: str) -> int:
        """-1/0/1 comparing dotted versions, ignoring a leading 'v' and any -suffix."""
        def parts(x):
            x = x.lstrip("vV").split("-")[0]
            return [int(re.match(r"\d+", seg).group()) if re.match(r"\d+", seg) else 0
                    for seg in x.split(".")]
        pa, pb = parts(a), parts(b)
        n = max(len(pa), len(pb))
        pa += [0] * (n - len(pa)); pb += [0] * (n - len(pb))
        return (pa > pb) - (pa < pb)

    def _refresh_update_info(self) -> None:
        """Fetches the latest GitHub release of clevim/MoonKobra and compares it with VERSION."""
        import urllib.request
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/clevim/MoonKobra/releases/latest",
                headers={"User-Agent": "MoonKobra", "Accept": "application/vnd.github+json"})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
            latest = (data.get("tag_name") or "").strip()
            cur = self._read_version()
            if latest:
                self._update_info = {
                    "current": cur, "latest": latest,
                    "url": data.get("html_url") or "https://github.com/clevim/MoonKobra/releases",
                    "available": self._cmp_ver(latest, cur) > 0,
                }
        except Exception as e:
            log.info(f"Update check failed: {e}")

    async def handle_api_update(self, request):
        """GET /api/update - cached GitHub release check (refreshes in the background at most every 6 h)."""
        if time.time() - self._update_checked_ts > 21600:
            self._update_checked_ts = time.time()
            threading.Thread(target=self._refresh_update_info, daemon=True, name="update-check").start()
        return web.json_response(self._update_info)

    # (nozzle, bed) in °C per material, for OrcaSlicer's lanes and Happy Hare's gates.
    # ponytail: family defaults; the exact values live in the chosen Orca profile.
    _LANE_TEMPS = {"PLA": (210, 60), "PLA+": (215, 60), "PETG": (230, 80), "ABS": (240, 100),
                   "ASA": (250, 100), "TPU": (220, 50), "PA": (260, 90), "PC": (270, 110),
                   "HIPS": (220, 100)}

    # OrcaSlicer filament preset IDs (mapping from MoonrakerPrinterAgent.cpp)
    # Default mapping per material type when the user set no profile override
    # for the slot. For the Kobra X we prefer Anycubic's own filament IDs
    # from the `@Anycubic Kobra X 0.4 nozzle` profiles - those
    # are printer-specific is_compatible and OrcaSlicer matches them
    # directly. Library fallbacks (OGF*) only for material types without
    # a Kobra X-specific Anycubic profile - their @system profiles have
    # `compatible_printers: []` (= compatible with every printer).
    _TRAY_INFO_IDX = {
        # Anycubic's own Kobra X profiles
        "PLA":        "GFPLA",
        "PLA+":       "GFPLA+",
        "PLA SILK":   "GFPLA Silk",
        "PLA-SILK":   "GFPLA Silk",
        "PLASILK":    "GFPLA Silk",
        "SILK PLA":   "GFPLA Silk",
        "PLA MATTE":  "GFPLA",
        "PLA-MATTE":  "GFPLA",
        "PLA MARBLE": "GFPLA",
        "PLA WOOD":   "GFPLA",
        "PETG":       "GFPETG",
        "PETG+":      "GFPETG",
        "ABS":        "GFABS",
        "ASA":        "GFASA",
        "TPU":        "GFTPU 95A",
        "TPE":        "GFTPU 95A",
        "PVA":        "GFPVA",
        # No Anycubic Kobra X profile → library fallback
        "PLA-CF":     "OGFL98",
        "PLA CF":     "OGFL98",
        "PETG-CF":    "OGFG98",
        "PETG CF":    "OGFG98",
        "PA":         "OGFN99",
        "PA-CF":      "OGFN98",
        "PA CF":      "OGFN98",
        "PC":         "OGFC99",
        "HIPS":       "OGFS98",
    }

    # Normalizes material type strings to the canonical key of _TRAY_INFO_IDX
    # and _default_filament_name. PLA variants without an exact match fall back
    # to the base family (PLA+ -> PLA+, PLA Matte -> PLA, etc.).
    @staticmethod
    def _normalize_material(mat: str) -> str:
        m = mat.upper().strip().replace("-", " ").replace("_", " ")
        # Normalize known variants
        _ALIASES = {
            "PLAPLUS": "PLA+", "PLA PLUS": "PLA+",
            "SILK PLA": "PLA SILK", "PLASILK": "PLA SILK",
            "PLA MATTE": "PLA MATTE", "PLA MARBLE": "PLA MARBLE",
            "PLA WOOD": "PLA WOOD",
            "TPE": "TPU",
            "PETG PLUS": "PETG+",
            "PA6": "PA", "PA12": "PA", "PA66": "PA",
        }
        if m in _ALIASES:
            return _ALIASES[m]
        return m

    @staticmethod
    def _material_family(mat: str) -> str:
        """Reduces a material to its base polymer family.

        PLA / PLA+ / PLA SILK / PLA MATTE -> "PLA"; PETG / PETG+ -> "PETG"; etc.
        Used by the stale-profile guard: only a *family* change (e.g. PETG ->
        PLA) invalidates a saved slot profile — a change within the family
        (PLA -> PLA SILK) must not drop an otherwise valid profile.
        """
        if not mat:
            return ""
        m = KobraXBridge._normalize_material(mat)
        # Longest prefixes first so "PETG" is not swallowed by "PET".
        for fam in ("PETG", "PLA", "ABS", "ASA", "TPU", "PVA", "HIPS", "PA", "PC", "PET"):
            if m.startswith(fam):
                return fam
        return m

    def _parse_combined_rfid_type(self, raw_type: str) -> tuple[str, str]:
        """Splits a combined ACE RFID string "VENDOR TYPE SERIAL" (e.g.
        "GEEETECH PLA Bas", written by third-party RFID tools) into
        (vendor, material_family).

        Anycubic's ACE RFID system concatenates vendor + material + a
        truncated serial/variant into a single `type` string for custom tags -
        unlike a normal spool report, where `type` is just "PLA"/"PETG"/etc.
        Returns ("", "") when the first token is not a known vendor (from the
        merged system+user filament library), which leaves plain type strings
        such as "PLA" fully intact (Issue #101).
        """
        tokens = raw_type.split()
        if len(tokens) < 2:
            return "", ""
        first = tokens[0].strip().lower()
        vendors = {p.get("vendor", "").lower(): p.get("vendor", "") for p in self._load_orca_filaments()}
        vendor = vendors.get(first)
        if not vendor:
            return "", ""
        family = self._material_family(" ".join(tokens[1:]))
        if not family:
            return "", ""
        return vendor, family

    @staticmethod
    def _rfid_variant_tokens(raw_type: str) -> list[str]:
        """Tokens after "VENDOR TYPE" in a combined ACE RFID string (e.g.
        ["bas"] for "GEEETECH PLA Bas") - the truncated variant/serial that
        tells apart several profiles of the same (vendor, material family),
        e.g. "Basic" vs. "Matte". Kept separate from _parse_combined_rfid_type()
        so that function's 2-element signature (and its callers/tests)
        stays the same (Issue #101)."""
        tokens = raw_type.split()
        return [t.lower() for t in tokens[2:]]

    def _match_profile_by_vendor_family(self, vendor: str, family: str,
                                         variant_tokens: list[str] | None = None) -> dict:
        """Finds an imported/system filament profile by (vendor, material
        family) - used to automatically resolve a combined ACE RFID type string
        to the OrcaSlicer profile the user already imported (Issue #101), since
        the profile's exact `name` never appears literally in the truncated
        RFID string.

        When several profiles share the same (vendor, family) - e.g. "Geeetech
        PLA Basic" and "Geeetech PLA Matte" both matching (Geeetech, PLA) -
        the variant_tokens (the remaining tokens of the RFID string, e.g. ["bas"] for
        "Basic") are scored against each candidate's name: a word-prefix match
        scores higher than a plain substring match, so "bas" prefers "Basic"
        over "Matte" or over an unrelated profile name that happens to contain "bas".
        Falls back to the first match when nothing breaks the tie."""
        matches = [
            p for p in self._load_orca_filaments()
            if p.get("vendor", "").lower() == vendor.lower()
            and self._material_family(p.get("type", "")) == family
        ]
        if not matches:
            return {}
        if len(matches) == 1 or not variant_tokens:
            return matches[0]

        best = matches[0]
        best_score = -1
        for p in matches:
            name_words = p.get("name", "").lower().split()
            score = 0
            for tok in variant_tokens:
                if any(w.startswith(tok) for w in name_words):
                    score += 2
                elif tok in p.get("name", "").lower():
                    score += 1
            if score > best_score:
                best_score = score
                best = p
        log.debug(
            f"_match_profile_by_vendor_family: {len(matches)} profiles match "
            f"vendor={vendor!r} family={family!r}, variant_tokens={variant_tokens!r} "
            f"-> {best.get('name')!r} (score={best_score})"
        )
        return best

    def _profile_material(self, profile: dict) -> str:
        """Material type (e.g. "PETG") of a saved slot profile, resolved by
        (vendor, name) in Orca's filament library. Returns "" when the
        profile is not in the library — in that case we do NOT guess."""
        name = (profile or {}).get("name", "")
        if not name:
            return ""
        vendor = profile.get("vendor", "")
        for p in self._load_orca_filaments():
            if p.get("vendor") == vendor and p.get("name") == name:
                return p.get("type", "") or ""
        return ""

    def _effective_slot_profile(self, global_idx: int, ams_material: str) -> dict:
        """Saved slot profile override — but only while the material *family*
        still matches the material loaded in the AMS. Falls back to
        automatically resolving a combined ACE RFID type string (Issue #101) when
        there is no (usable) manual override.

        Non-destructive suppression (Option A): when the family no longer matches
        (e.g. a PETG profile but PLA loaded) the override is skipped → falls
        back to the RFID auto-match / generic default. The override stays in
        config.ini and applies again as soon as the right material is loaded
        again. When the profile's family is unknown we do NOT suppress (fail-safe).

        Centralized here (instead of duplicated per caller) so every consumer -
        the dashboard's /kx/filament/slots, the Happy-Hare gate data and the
        OrcaSlicer lane-data sync - benefits from the RFID auto-match the same way,
        instead of only the single call site that happened to also call
        _parse_combined_rfid_type() directly."""
        # A combined ACE RFID string ("GEEETECH PLA Bas") carries a vendor
        # prefix that _material_family() alone cannot see (it only
        # strips known polymer prefixes, so "GEEETECH PLA BAS" resolves to
        # itself, not "PLA") - resolve the plain material family through the
        # RFID parser first so the stale-profile guard below compares against
        # the real polymer family, not the raw combined string.
        vendor, family = self._parse_combined_rfid_type(ams_material)
        plain_material = family or ams_material

        profile = self._filament_profiles.get(global_idx) or {}
        if profile.get("name"):
            prof_fam = self._material_family(self._profile_material(profile))
            ams_fam  = self._material_family(plain_material)
            if not (prof_fam and ams_fam and prof_fam != ams_fam):
                return profile

        if vendor:
            variant_tokens = self._rfid_variant_tokens(ams_material)
            auto = self._match_profile_by_vendor_family(vendor, family, variant_tokens)
            if auto.get("name"):
                return auto

        return {}

    def _build_lane_data(self) -> dict:
      """Builds the BBL AMS JSON for OrcaSlicer's DevFilaSystemParser::ParseV1_0.

      POSITION-FAITHFUL: every physical slot keeps its position (tray id =
      slot position). Empty slots are reported as placeholder trays, NOT
      filtered/compacted - otherwise the colours land on wrong positions
      (e.g. slot 1=yellow, 2=empty, 3=red -> red must not land on position 2).
      """
      slots = self._ams_slots
      total = len(slots)
      if total == 0:
        return {"ams": [], "ams_exist_bits": "0", "tray_exist_bits": "0"}

      ams_count = (total + 3) // 4
      ams_exist_bits = 0
      tray_exist_bits = 0
      ams_array = []
      # Lanes in AFC format ({"lane1": {"lane": "0", "material", "color", "name",
      # "vendor_name", ...}}): THIS is what OrcaSlicer reads in fetch_moonraker_filament_data()
      # - the official build as well as OrcaSlicer-KX and PR #13719. The BBL block ("ams"/
      # "tray_exist_bits") alone was never understood: Orca found no lane
      # and fell into the Happy Hare path, which only carries the name (no vendor and no
      # bed temperature). Both formats live in the same dict because Orca
      # skips values that are not objects ("ams" is a list, the bits are a string).
      lanes = {}

      for ams_id in range(ams_count):
        ams_exist_bits |= (1 << ams_id)
        tray_array = []
        max_slot = min(3, total - ams_id * 4 - 1)
        for slot_id in range(max_slot + 1):
          slot_index = ams_id * 4 + slot_id
          slot = slots[slot_index] if slot_index < total else {}
          occupied = slot.get("status") == 5

          if occupied:
            tray_exist_bits |= (1 << slot_index)
            color_raw = slot.get("color", [255, 255, 255])
            if isinstance(color_raw, list) and len(color_raw) >= 3:
              color_hex = "{:02X}{:02X}{:02X}FF".format(
                int(color_raw[0]), int(color_raw[1]), int(color_raw[2])
              )
            elif isinstance(color_raw, str) and len(color_raw) >= 6:
              color_hex = color_raw[:6].upper() + "FF"
            else:
              color_hex = "FFFFFFFF"
            material = self._normalize_material(slot.get("type", "PLA"))
            # The user's override from config.ini [filament_profiles].slot_N_id
            # takes precedence over the default mapping by material type.
            # The vendor goes along (tray_sub_brands + filament_vendor),
            # so a patched OrcaSlicer can match by brand + type +
            # colour (like SnapmakerPrinterAgent).
            # Three-layer resolution of the filament hint sent to OrcaSlicer,
            # all handled inside _effective_slot_profile() (Issue #101):
            #   1. User choice (config.ini [filament_profiles]) — exact control
            #   2. Combined ACE RFID string "VENDOR TYPE SERIAL" (e.g.
            #      "GEEETECH PLA Bas") automatically matched against the library of
            #      profiles the user already imported. Not persisted in
            #      config.ini - recomputed on every call, so a spool with another
            #      tag loaded later does not get stuck on a stale match.
            #   3. Generic fallback (_TRAY_INFO_IDX) by material type - no
            #      vendor hint; OrcaSlicer then picks its own generic preset
            user_profile = self._effective_slot_profile(slot_index, material)
            if user_profile.get("name"):
                material = self._material_family(user_profile.get("type", material)) or material
                vendor    = user_profile.get("vendor", "")
                fila_name = user_profile.get("name", "")
                tray_info_idx = user_profile.get("id") or self._TRAY_INFO_IDX.get(material, "OGFL99")
            else:
                # Default: generic library profile (see _default_filament_name) —
                # it is compatible with every printer and surely visible.
                # The user deliberately picks a concrete brand per slot if
                # they want; the default stays neutral.
                fila_name = self._default_filament_name(material)
                vendor    = "Generic" if fila_name.startswith("Generic ") else ""
                tray_info_idx = self._lookup_filament_id(vendor, fila_name) or self._TRAY_INFO_IDX.get(material, "OGFL99")
            tray_array.append({
              "id": str(slot_id),
              "tag_uid": "0000000000000000",
              "tray_info_idx": tray_info_idx,
              "tray_type": material,
              "tray_color": color_hex,
              "tray_sub_brands": vendor,
              # OrcaSlicer PR #13719's receiving patch expects `name` +
              # `vendor_name` per lane (staged matching: Vendor+Name → Name →
              # filament_id_by_type). We send both spellings so
              # older patch variants + future upstream PRs stay
              # covered.
              "name":         fila_name,
              "vendor_name":  vendor,
              # Aliases for older patch variants (variant 2,
              # MoonrakerPrinterAgent.cpp): filament_id directly (exact),
              # otherwise the preset name is resolved via find_preset().
              "filament_id":     tray_info_idx,
              "filament_vendor": vendor,
              "filament_name":   fila_name,
              "preset":          fila_name,
            })
            lanes[f"lane{slot_index + 1}"] = {
              "lane":        str(slot_index),
              "material":    material,
              "color":       "#" + color_hex[:6],
              "name":        fila_name,
              "vendor_name": vendor,
              "nozzle_temp": self._LANE_TEMPS.get(material, (210, 60))[0],
              "bed_temp":    self._LANE_TEMPS.get(material, (210, 60))[1],
              "spool_id":    self._spoolman_slot_spools.get(slot_index, -1),
            }
          else:
            # An empty lane stays present (material "" = no filament) so
            # Orca keeps the positions - otherwise the colours slide between slots.
            lanes[f"lane{slot_index + 1}"] = {"lane": str(slot_index), "material": "", "color": ""}
            tray_array.append({
              "id": str(slot_id),
              "tag_uid": "0000000000000000",
              "tray_info_idx": "",
              "tray_type": "",
              "tray_color": "00000000",
              "tray_slot_placeholder": "1",
            })

        ams_array.append({"id": str(ams_id), "info": "0002", "tray": tray_array})

      return {
        "ams": ams_array,
        "ams_exist_bits": format(ams_exist_bits, "X"),
        "tray_exist_bits": format(tray_exist_bits, "X"),
        **lanes,
      }

    @staticmethod
    def _layer_height_from_filename(fname: str) -> float:
        """OrcaSlicer's default file name pattern: `<plate>_<material>_<layer>_<dur>.gcode`
        e.g. `adapter_e27_plate(01)_PLA_0.2_41m1s.gcode` → 0.2.

        Fallback when the G-code header was not parsed (e.g. a file started straight
        from the slicer, or uploaded before v0.9.18). Returns 0.0 when the
        pattern does not match."""
        import re
        if not fname:
            return 0.0
        m = re.search(r"_(0\.\d+)_(\d+[hms])", fname)
        if not m:
            return 0.0
        try:
            return float(m.group(1))
        except Exception:
            return 0.0

    def _estimate_current_z(self) -> float:
        """Estimates the current Z height from curr_layer + layer heights.

        The printer provides no real Z position over MQTT, but Obico
        (moonraker-obico/printer.py:267) reads currentZ from `gcode_position[2]`.
        We compute it back from the G-code header's layer_height:
          z = first_layer_height + (curr_layer - 1) * layer_height

        The values are set on the upload path and only reset on print
        cancel/finish (slot/colour changes do not affect them). If the values
        are missing (e.g. because the print was started straight from the slicer
        without an upload through the bridge), they are reloaded once from the
        G-code store. Returns 0.0 when nothing is known - then Obico shows
        no Z value."""
        s = self._state
        layer_h = float(s.get("layer_height") or 0.0)
        first_h = float(s.get("first_layer_height") or 0.0)
        fname = s.get("filename", "")
        if not layer_h and fname:
            try:
                gf = self._store.get_file_by_name(fname)
                if gf:
                    layer_h = float(gf.get("layer_height") or 0.0)
                    first_h = float(gf.get("first_layer_height") or layer_h)
            except Exception:
                pass
        if not layer_h and fname:
            # Last fallback: OrcaSlicer's default file name contains the layer height
            layer_h = self._layer_height_from_filename(fname)
            if layer_h and not first_h:
                first_h = layer_h
        if layer_h:
            # cache it in the state so not every build queries the store again
            s["layer_height"] = layer_h
            s["first_layer_height"] = first_h
        if not layer_h:
            return 0.0
        curr = int(s.get("curr_layer") or 0)
        if curr <= 0:
            return 0.0
        # Layer 1 = first_layer_height, layer 2 = first + layer_h, …
        return round(first_h + max(0, curr - 1) * layer_h, 3)

    # -------------------------------------------------------------------------
    # Push via WebSocket
    # -------------------------------------------------------------------------

    # Static objects that never change at runtime. They are delivered once
    # through objects.query/subscribe, but NOT included in every
    # notify_status_update - otherwise Mobileraker's ConfigFile.parse
    # (expensive + strict) runs on every status tick and the app
    # freezes/closes on update (Issue #48).
    _STATIC_STATUS_OBJECTS = ("configfile", "webhooks", "heaters", "history")

    _push_lock = threading.Lock()

    def _push_status_update(self):
        if not self.ws_clients:
            self._last_pushed = {}
            return
        # Like real Moonraker: only the fields that changed since the last
        # push. A new client gets the full state on connect/subscribe, so
        # diffing against the last push never leaves it without a value. The lock
        # makes sure two concurrent pushes do not go out of order (the older
        # arriving last and the client keeping the old value).
        with self._push_lock:
            objs = self._build_printer_objects()
            last = getattr(self, "_last_pushed", {})
            diff = {}
            for k, v in objs.items():
                if k in self._STATIC_STATUS_OBJECTS:
                    continue
                prev = last.get(k)
                if not isinstance(v, dict) or not isinstance(prev, dict):
                    if v != prev:
                        diff[k] = v
                    continue
                changed = {f: fv for f, fv in v.items() if f not in prev or prev[f] != fv}
                if changed:
                    diff[k] = changed
            self._last_pushed = objs
            if not diff:
                return
            text = json.dumps({
                "jsonrpc": "2.0",
                "method":  "notify_status_update",
                "params": [diff, time.time()],
            })
            dead = set()
            for ws in self.ws_clients:
                try:
                    asyncio.run_coroutine_threadsafe(ws.send_str(text), ws._loop)
                except Exception:
                    dead.add(ws)
            self.ws_clients -= dead

    def _build_mmu_object(self) -> dict:
      # POSITION-FAITHFUL: one gate per physical slot, in order. Empty slots
      # get gate_status=0 (instead of being omitted) - otherwise the
      # colours in OrcaSlicer land on wrong gates (slot 1=yellow, 2=empty, 3=red →
      # red must not land on gate 1). gate_status 0=empty, 1=available.
      slots = sorted(
        ((int(s.get("global_index", i)), s) for i, s in enumerate(self._ams_slots)),
        key=lambda item: item[0],
      )
      if not slots:
        return {}

      num_gates = len(slots)
      gate_status, gate_material, gate_color, gate_temperature, gate_color_rgb = [], [], [], [], []
      gate_filament_name = []
      gate_spool_id = []
      for _global_index, slot in slots:
        occupied = slot.get("status") == 5
        gate_status.append(1 if occupied else 0)
        material = self._normalize_material(slot.get("type") or "PLA") if occupied else ""
        gate_material.append(material)
        c = slot.get("color", [0, 0, 0]) if occupied else [0, 0, 0]
        # Happy Hare expects gate_color as RRGGBB WITHOUT '#' (a Klipper limitation).
        # Empty gate: empty string + RGB [0,0,0].
        gate_color.append("{:02X}{:02X}{:02X}".format(*c[:3]) if occupied else "")
        gate_color_rgb.append([round(c[0]/255, 3), round(c[1]/255, 3), round(c[2]/255, 3)] if occupied else [0.0, 0.0, 0.0])
        gate_temperature.append(self._LANE_TEMPS.get(material, (210, 60))[0] if occupied else 0)
        # gate_filament_name from the user override or the material default for the
        # HH path in OrcaSlicer (fetch_hh_filament_info). When Orca uses the
        # HH path (MMU detection), PR #13719 evaluates this field as a
        # preset name -> 'Anycubic PLA' matches the printer-specific
        # preset; an empty string used to lead to Generic PLA.
        if occupied:
          # Stale-profile guard (see _effective_slot_profile): only applies the
          # override while the material family still matches the loaded filament.
          user_profile = self._effective_slot_profile(_global_index, material)
          fila_name = user_profile.get("name") or self._default_filament_name(material)
          gate_filament_name.append(fila_name)
        else:
          gate_filament_name.append("")
        # Spoolman spool ID per gate from the (printer-specific) slot map so
        # Happy Hare/OrcaSlicer can show the linked spool (-1 = none).
        gate_spool_id.append(self._spoolman_slot_spools.get(_global_index, -1) if occupied else -1)

      loaded_index_map = {global_index: idx for idx, (global_index, _) in enumerate(slots)}
      active_gate = loaded_index_map.get(int(self._ams_loaded_slot), -1)
      return {
        "num_gates":          num_gates,
        "enabled":            True,
        "gate_status":        gate_status,
        "gate_material":      gate_material,
        "gate_color":         gate_color,
        "gate_temperature":   gate_temperature,
        "gate_color_rgb":     gate_color_rgb,
        "gate_filament_name": gate_filament_name,
        "gate_spool_id":      gate_spool_id,
        "ttg_map":            list(range(num_gates)),
        "tool":               active_gate,
        "gate":               active_gate,
      }

    def _default_filament_name(self, material: str) -> str:
      """Default name for `gate_filament_name`/`name` in lane_data when there is no
      user override. Deliberate design decision: **always
      Generic <type>** as the default - the library profile is `compatible_printers:[]`
      (= compatible with every printer) and therefore surely visible.

      OrcaSlicer then matches the neutral generic preset and the user
      can set a concrete brand per slot if they want."""
      if not material:
        return ""
      mat = self._normalize_material(material)
      profs = self._load_orca_filaments()
      # Variant mapping: the printer reports e.g. "PLA SILK", OrcaSlicer stores
      # every variant under type=PLA with the variant name in the name field.
      _VARIANT_NAME = {
          "PLA SILK":   "Generic PLA Silk",
          "PLA MATTE":  "Generic PLA Matte",
          "PLA+":       "Generic PLA",
          "PLA-CF":     "Generic PLA-CF",
          "PETG-CF":    "Generic PETG-CF",
      }
      if mat in _VARIANT_NAME:
          target = _VARIANT_NAME[mat]
          for p in profs:
              if p.get("vendor") == "Generic" and p.get("name") == target:
                  return p["name"]
      def _match_type(p: dict) -> bool:
        pt = (p.get("type") or "").upper()
        return pt == mat or pt.startswith(mat + "-") or pt.startswith(mat + " ")
      # Generic library profile (always is_visible+is_compatible)
      for p in profs:
        if p.get("vendor") == "Generic" and p.get("name", "").startswith("Generic ") and _match_type(p):
          return p.get("name", "")
      # If the library generic for this exotic material type does not exist,
      # we return nothing - OrcaSlicer falls back to filament_id_by_type.
      return ""

    def _build_printer_objects(self) -> dict:
        s = self._state
        return {
            "extruder": {
                "temperature": s["nozzle_temp"],
                "target":      s["nozzle_target"],
                "power":       0.0,
            },
            "heater_bed": {
                "temperature": s["bed_temp"],
                "target":      s["bed_target"],
                "power":       0.0,
            },
            "print_stats": {
                "state":          s["print_state"],
                "filename":       s["filename"],
                "print_duration": s["print_duration"],
                "total_duration": s["print_duration"],
                "remain_time":    s["remain_time"],
                "message":        s.get("pause_msg", ""),
                "info": {
                    "current_layer": s["curr_layer"],
                    "total_layer":   s["total_layers"],
                },
            },
            "display_status": {
                "progress": s["progress"],
                "message":  s.get("pause_msg", ""),
            },
            "virtual_sdcard": {
                "progress":  s["progress"],
                "is_active": s["print_state"] == "printing",
                "file_path": s["filename"],
                # Approximate file_position: fraction × est_total_size.
                # The printer provides no exact value; Obico only uses it for display.
                "file_position": int(s["progress"] * 1_000_000) if s["progress"] else 0,
            },
            "toolhead": {
                "position":         [0, 0, 0, 0],
                "homed_axes":       "xyz",
                "print_time":       s["print_duration"],
                "estimated_print_time": s["print_duration"],
            },
            "mmu": self._build_mmu_object(),
            # -- Moonraker compatibility for moonraker-obico --
            "heaters": {
                "available_heaters": ["extruder", "heater_bed"],
                "available_sensors": [],
                "available_monitors": [],
            },
            "webhooks": {
                "state":         "ready",
                "state_message": "Printer is ready",
            },
            # speed_factor: 1=silent(0.5) / 2=standard(1.0) / 3=high(1.3) / 4=ultra(1.5)
            # Estimate the current Z height for Obico from curr_layer + layer heights
            # (the printer provides no real Z position over MQTT). gcode_position[2]
            # is the value moonraker-obico reads as currentZ in printer.py.
            "gcode_move": {
                "speed_factor":   {1: 0.5, 2: 1.0, 3: 1.3, 4: 1.5}.get(int(s.get("print_speed_mode") or 2), 1.0),
                "extrude_factor": 1.0,
                "speed":          0,
                "gcode_position": [0, 0, self._estimate_current_z(), 0],
                "absolute_coordinates": True,
                "absolute_extrude":     True,
                "homing_origin":  [0, 0, 0, 0],
                "position":       [0, 0, self._estimate_current_z(), 0],
            },
            # motion_report: Mobileraker reads the live speed here
            # (live_velocity). The Kobra X's MQTT provides NO real mm/s, only
            # a print_speed_mode (1-4). live_velocity therefore stays 0 - but the
            # object must exist, otherwise Mobileraker shows nothing
            # (motion_report used to be null). live_position mirrors the
            # estimated Z height (like gcode_move).
            "motion_report": {
                "live_position":          [0, 0, self._estimate_current_z(), 0],
                "live_velocity":          0.0,
                "live_extruder_velocity": 0.0,
            },
            "fan": {
                "speed": (int(s.get("fan_speed") or 0)) / 100.0,
                "rpm":   None,
            },
            # history (object): Obico subscribes to it as an object; the real
            # /server/history/list endpoint delivers the actual list separately.
            "history": {
                "job_totals": {
                    "total_jobs":  0,
                    "total_time":  0,
                    "total_print_time": 0,
                    "total_filament_used": 0.0,
                    "longest_job": 0,
                    "longest_print": 0,
                },
                "current_job": None,
            },
            # Klipper pseudo-macros for moonraker-obico:
            # - _OBICO_LAYER_CHANGE reports the current layer number. Obico uses it
            #   for "first layer scan" triggers and layer-aligned time-lapse frames.
            #   We feed it from the MQTT stream (s["curr_layer"]).
            # - TIMELAPSE_TAKE_FRAME signals that the current pause comes from the
            #   time-lapse (otherwise Obico would read the pause as a user
            #   pause). We set is_paused=False because our pauses
            #   are never time-lapse pauses.
            "gcode_macro _OBICO_LAYER_CHANGE": {
                "current_layer":         int(s.get("curr_layer") or 0),
                "first_layer_scanning":  False,
                "first_layer_scan_enabled": False,
            },
            "gcode_macro TIMELAPSE_TAKE_FRAME": {
                "is_paused": False,
            },
            # configfile stub - Mobileraker and other clients break without
            # this object (Missing field: configFile). Values from the
            # decrypted avata_main.conf (ACCFG1.0 - Kobra X firmware).
            # Mobileraker (Issue #48) parses BOTH branches config + settings through the
            # same ConfigFile.parse → ConfigExtruder.fromJson; an empty config:{}
            # crashed the non-nullable Dart parser. That is why the
            # config is mirrored identical to settings.
            "configfile": self._klipper_configfile_stub(),
        }

    def _klipper_configfile_stub(self) -> dict:
        """Minimal Klipper configfile stub for Mobileraker/OctoApp (Issue #48).

        Mobileraker parses BOTH the `config` and `settings` branches through the same
        ConfigFile.parse → ConfigExtruder.fromJson. An empty `config: {}`
        crashed the non-nullable Dart parser, which is why `config` is
        mirrored identical to `settings`. Values from the
        decrypted avata_main.conf (ACCFG1.0 — Kobra X firmware).
        """
        settings = {
            "printer": {
                "kinematics":              "cartesian",
                "max_velocity":            450,
                "max_accel":               10000,
                "max_z_velocity":          12,
                "max_z_accel":             100,
                "square_corner_velocity":  20.0,
            },
            "extruder": {
                "nozzle_diameter":    0.4,
                "filament_diameter":  1.75,
                "sensor_type":        "ATC Semitec 104GT-2",
                "min_temp":           0,
                "max_temp":           320,
                "min_extrude_temp":   10,
                # Mobileraker's ConfigExtruder expects these non-nullable fields
                # (max_extrude_only_distance, max_power) or present as a key
                # (max_extrude_only_velocity/accel may be null). Missing =
                # crash in ConfigExtruder.fromJson (Issue #48).
                "max_extrude_only_distance": 100.0,
                "max_power":                 1.0,
                "max_extrude_only_velocity": None,
                "max_extrude_only_accel":    None,
            },
            "heater_bed": {
                # Mobileraker's ConfigHeaterBed: heater_pin, sensor_type, control
                # are non-nullable. The values are placeholders (the bridge does not know
                # the real pins - Anycubic firmware, no Klipper printer.cfg).
                "heater_pin":  "PA0",
                "sensor_type": "ATC Semitec 104GT-2",
                "control":     "pid",
                "min_temp":    0,
                "max_temp":    120,
            },
            # Fill stepper_* with the required non-nullable fields (step_pin, dir_pin,
            # rotation_distance), otherwise ConfigStepper.fromJson breaks.
            "stepper_x": {"step_pin": "PA1", "dir_pin": "PA2", "rotation_distance": 40,
                          "position_min": -18.5, "position_max": 280},
            "stepper_y": {"step_pin": "PA3", "dir_pin": "PA4", "rotation_distance": 40,
                          "position_min": -6.5,  "position_max": 272.5},
            "stepper_z": {"step_pin": "PA5", "dir_pin": "PA6", "rotation_distance": 8,
                          "position_min": -4,    "position_max": 262},
            "virtual_sdcard": {"path": "/data/gcodes"},
            "pause_resume":   {},
            "display_status": {},
        }
        # config + settings must have the same fields - Mobileraker
        # parses both. deepcopy so no client, through a shared
        # reference, accidentally mutates both branches.
        return {
            "config":   copy.deepcopy(settings),
            "settings": settings,
            "warnings": [],
            "save_config_pending": False,
            "save_config_pending_items": {},
        }

    # -------------------------------------------------------------------------
    # /kx/ API handlers (G-code store, history, filament)
    # -------------------------------------------------------------------------

    _CORS = {
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }

    def _json_cors(self, data, status=200):
        return web.json_response(data, status=status, headers=self._CORS)

    async def handle_kx_options(self, request):
        return web.Response(status=204, headers=self._CORS)

    async def handle_kx_files(self, request):
        files = self._store.list_files()
        # Fill legacy entries without saved filament metadata
        # so the left side of the dialog shows the G-code colours instead of the AMS slots.
        for f in files:
            needs_refresh = not f.get("gcode_filaments")
            if not needs_refresh:
                try:
                    cached = f.get("gcode_filaments")
                    parsed_cached = cached if isinstance(cached, list) else json.loads(cached)
                    needs_refresh = any("is_used" not in item for item in (parsed_cached or []))
                except Exception:
                    needs_refresh = True
            if not needs_refresh:
                continue
            path = f.get("path") or ""
            if not path or not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as fh:
                    parsed_filaments = _extract_filament_info(fh.read())
                if parsed_filaments:
                    f["gcode_filaments"] = json.dumps(parsed_filaments)
                    self._store.update_file_filaments(f["id"], parsed_filaments)
            except Exception as e:
                log.debug(f"Filament metadata backfill failed for {f.get('filename')}: {e}")
        # Add the last job's status + duration per file
        jobs = self._store.list_jobs(limit=500)
        last_job: dict = {}
        for j in reversed(jobs):
            last_job[j["gcode_file_id"]] = j
        for f in files:
            f["web_unverified"] = bool(f.get("web_unverified"))
            lj = last_job.get(f["id"])
            f["last_print_status"]   = lj["status"]       if lj else None
            f["last_print_duration"] = lj["duration_sec"] if lj else None
            f["last_print_at"]       = lj["started_at"]   if lj else None
        return self._json_cors({"result": files})

    async def handle_kx_file_delete(self, request):
        file_id = request.match_info["file_id"]
        if self._store.delete_file(file_id):
            return self._json_cors({"result": "ok"})
        return self._json_cors({"error": "not found"}, status=404)

    async def handle_kx_printer_files(self, request):
        """GET /kx/printer-files - lists the files on the printer's OWN internal
        storage (MQTT action file/listLocal), unlike /kx/files, which
        lists what the bridge itself stored. Needed because prints
        started straight from Anycubic Slicer Next (not through the bridge)
        leave files on the printer that used to be visible/
        deletable only on the printer's own display (context of Issue #102)."""
        loop = asyncio.get_event_loop()
        def _fetch():
            return self._wait_for_file_action(
                "listLocal",
                lambda: self.client.publish(
                    "file", "listLocal",
                    {"page_num": 1, "page_size": 200, "path": "/"},
                    timeout=0,
                ),
                timeout=8.0,
            )
        result = await loop.run_in_executor(None, _fetch)
        if not result or result.get("code") != 200:
            return self._json_cors({"error": "printer unreachable or query failed"}, status=502)
        records = (result.get("data") or {}).get("records") or []
        files = [r for r in records if not r.get("is_dir")]
        return self._json_cors({"result": files})

    async def handle_kx_printer_file_delete(self, request):
        """POST /kx/printer-files/delete - body: {"filenames": ["a.gcode", ...]}.
        One endpoint for single and multiple deletion - the printer's MQTT action
        file/deleteBatch accepts a list natively."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        filenames = body.get("filenames") or []
        if not filenames:
            return self._json_cors({"error": "no file name given"}, status=400)
        files = [{"path": "/", "filename": fn} for fn in filenames if fn]
        loop = asyncio.get_event_loop()
        def _delete():
            return self._wait_for_file_action(
                "deleteBatch",
                lambda: self.client.publish(
                    "file", "deleteBatch",
                    {"root": "local", "files": files},
                    timeout=0,
                ),
                timeout=8.0,
            )
        result = await loop.run_in_executor(None, _delete)
        if not result or result.get("state") != "success":
            return self._json_cors({"error": "deletion failed", "detail": result}, status=502)
        return self._json_cors({"result": "ok"})

    async def handle_kx_printer_file_thumbnail(self, request):
        """GET /kx/printer-files/{filename}/thumbnail - fetches the thumbnail embedded
        in the G-code of a file on the printer's own storage, through
        file/fileDetails. The printer extracts and base64-encodes the
        "; thumbnail begin" block of the G-code header on demand and
        returns it inline in data.file_details.thumbnail - no separate
        download/pre-signed URL step (verified live on a real
        Kobra X). Cached in memory per file name, since the thumbnail
        never changes while the file exists on the printer, and querying again
        on every render/scroll would mean one MQTT roundtrip per visible card."""
        filename = request.match_info.get("filename", "")
        if not filename:
            return self._json_cors({"error": "no file name given"}, status=400)
        cached = self._printer_thumbnail_cache.get(filename)
        if cached is not None:
            return self._json_cors({"result": {"thumbnail": cached}})
        loop = asyncio.get_event_loop()
        def _fetch():
            return self._wait_for_file_action(
                "fileDetails",
                lambda: self.client.publish(
                    "file", "fileDetails",
                    {"root": "local", "filename": filename},
                    timeout=0,
                ),
                timeout=8.0,
            )
        result = await loop.run_in_executor(None, _fetch)
        if not result or result.get("code") != 200:
            return self._json_cors({"error": "printer unreachable or query failed"}, status=502)
        thumb = ((result.get("data") or {}).get("file_details") or {}).get("thumbnail") or ""
        self._printer_thumbnail_cache[filename] = thumb
        return self._json_cors({"result": {"thumbnail": thumb}})

    async def handle_kx_file_download(self, request):
        file_id = request.match_info["file_id"]
        f = self._store.get_file(file_id)
        if not f:
            return self._json_cors({"error": "not found"}, status=404)
        path = f.get("path") or ""
        if not path or not os.path.isfile(path):
            return self._json_cors({"error": "not found"}, status=404)
        filename = os.path.basename(f.get("filename") or path)
        # RFC 5987: filename* with URL encoding for special chars/UTF-8,
        # plus an ASCII fallback (strips every " and \ from the name for the
        # quoted-string part).
        ascii_fallback = filename.encode("ascii", "replace").decode("ascii").replace('"', "").replace("\\", "")
        encoded = quote(filename, safe="")
        disposition = f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'
        return web.FileResponse(path, headers={"Content-Disposition": disposition})

    async def handle_kx_file_verify(self, request):
        file_id = request.match_info["file_id"]
        if self._store.clear_web_unverified(file_id):
            return self._json_cors({"result": "ok"})
        return self._json_cors({"error": "not found"}, status=404)

    async def handle_kx_filament_slots(self, request):
        slots = []
        for i, s in enumerate(self._ams_slots):
            gidx = int(s.get("global_index", i))
            # Stale-profile guard: only show the override while the material
            # family matches the material loaded in the AMS (otherwise the slot stays unbranded).
            profile = self._effective_slot_profile(gidx, s.get("type", ""))
            slots.append({
                "slot_index":  gidx,
                "material":    s.get("type", ""),
                "color_hex":   "#{:02X}{:02X}{:02X}".format(*s.get("color", [0,0,0])[:3]),
                "status":      "loaded" if s.get("status") == 5 else "empty",
                "nozzle_temp": 0,
                # The user's current override from config.ini [filament_profiles]
                # - (vendor,name) is unique, the id is only a hint.
                "filament_id":     profile.get("id", ""),
                "filament_vendor": profile.get("vendor", ""),
                "filament_name":   profile.get("name", ""),
            })
        return self._json_cors({"result": slots})

    async def handle_kx_filament_profiles(self, request):
        """Returns the static list of OrcaSlicer filament profiles
        (from data/orca_filaments.json, generated from OrcaSlicer's
        profile tree).

        Optional filter via ?type=PLA / ?vendor=Polymaker.
        The frontend uses it in the slot profile dropdown.
        """
        type_filter = request.rel_url.query.get("type", "").upper().strip()
        vendor_filter = request.rel_url.query.get("vendor", "").strip()
        profiles = self._load_orca_filaments()
        if type_filter:
            profiles = [p for p in profiles if p.get("type", "").upper() == type_filter]
        if vendor_filter:
            profiles = [p for p in profiles if p.get("vendor", "") == vendor_filter]
        return self._json_cors({"result": profiles})

    async def handle_kx_filament_profiles_user_list(self, request):
        """GET /kx/filament/profiles/user - only the user-imported profiles,
        for the settings tab (management with delete buttons)."""
        path = self._orca_filaments_user_path()
        if not os.path.isfile(path):
            return self._json_cors({"result": []})
        try:
            with open(path, encoding="utf-8") as f:
                user_profiles = json.load(f) or []
        except Exception:
            user_profiles = []
        return self._json_cors({"result": user_profiles})

    async def handle_kx_filament_profiles_import(self, request):
        """POST /kx/filament/profiles/user - multipart upload with one
        ZIP file or several `.json` files from
        ~/.config/OrcaSlicer/user/<id>/filament/.

        Existing user profiles with the same (vendor, name) key are
        overwritten. Parsed profiles use the same schema as
        orca_filaments.json (id, name, vendor, type, color)."""
        import io, zipfile
        from orca_filaments import parse_profile_bytes
        added: list[dict] = []
        skipped: int = 0
        # System index to resolve inherits: user profiles reference
        # system parents through "inherits" (e.g. "Generic PLA @System"). That way
        # we can pull filament_id/vendor/type/color from the system parent
        # when the user profile does not define them.
        sys_idx = [p for p in self._load_orca_filaments() if not p.get("is_user")]
        try:
            reader = await request.multipart()
        except Exception:
            return self._json_cors({"error": "multipart expected"}, status=400)
        async for part in reader:
            if part.name not in ("file", "files", "upload"):
                continue
            blob = await part.read()
            fn   = (part.filename or "").lower()
            if fn.endswith(".zip"):
                try:
                    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                        for inner in zf.namelist():
                            if not inner.lower().endswith(".json"):
                                continue
                            try:
                                with zf.open(inner) as zf_in:
                                    p = parse_profile_bytes(zf_in.read(), source_name=inner, system_index=sys_idx)
                            except Exception:
                                skipped += 1
                                continue
                            if p:
                                added.append(p)
                            else:
                                skipped += 1
                except zipfile.BadZipFile:
                    return self._json_cors({"error": "invalid zip"}, status=400)
            elif fn.endswith(".json"):
                p = parse_profile_bytes(blob, source_name=fn, system_index=sys_idx)
                if p:
                    added.append(p)
                else:
                    skipped += 1

        if not added:
            return self._json_cors({"result": "ok", "added": 0, "skipped": skipped})

        # Merge with the existing user JSON (same (vendor,name) -> replace)
        path = self._orca_filaments_user_path()
        existing: list[dict] = []
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    existing = json.load(f) or []
            except Exception:
                existing = []
        by_key = {(p.get("vendor"), p.get("name")): p for p in existing}
        for p in added:
            by_key[(p.get("vendor"), p.get("name"))] = p
        merged = sorted(by_key.values(), key=lambda x: (x.get("vendor",""), x.get("name","")))
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except Exception as e:
            return self._json_cors({"error": f"failed to write: {e}"}, status=500)
        self._invalidate_filaments_cache()
        return self._json_cors({"result": "ok",
                                "added": len(added),
                                "skipped": skipped,
                                "total_user": len(merged)})

    async def handle_kx_filament_profiles_user_delete(self, request):
        """DELETE /kx/filament/profiles/user - deletes a single
        entry (?vendor=...&name=...) or all of them when no query is given."""
        vendor = request.rel_url.query.get("vendor", "").strip()
        name   = request.rel_url.query.get("name", "").strip()
        path = self._orca_filaments_user_path()
        if not os.path.isfile(path):
            return self._json_cors({"result": "ok", "removed": 0})
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f) or []
        except Exception:
            existing = []
        before = len(existing)
        if vendor and name:
            existing = [p for p in existing
                        if not (p.get("vendor") == vendor and p.get("name") == name)]
        else:
            existing = []
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except Exception as e:
            return self._json_cors({"error": str(e)}, status=500)
        self._invalidate_filaments_cache()
        return self._json_cors({"result": "ok",
                                "removed": before - len(existing),
                                "total_user": len(existing)})

    def _find_orca_filaments_json(self) -> str | None:
        """Finds the static JSON file. It sits next to web/ in _WEB_BASE/data/
        — in the 3 deployment modes:
          • Script:  data/orca_filaments.json next to this file
          * Docker:  /app/static/orca_filaments.json (static in the image, NOT the
                     data/ volume that holds the runtime state - see the Dockerfile)
          • Onefile: sys._MEIPASS/static/orca_filaments.json
        When the /app/data/ mounted as a volume hides the static data, a copy
        also sits in _WEB_BASE/data/ (= /app/ in Docker = the same path).
        As a last resort it also looks in ../bridge/data/ (old dev layout)."""
        candidates = [
            # Docker: COPY data/ -> /app/static/ (data/ is a volume -> hidden)
            os.path.join(_WEB_BASE, "static", "orca_filaments.json"),
            os.path.join(_WEB_BASE, "data", "orca_filaments.json"),
        ]
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.join(here, "data", "orca_filaments.json"))
        candidates.append(os.path.join(here, "..", "bridge", "data", "orca_filaments.json"))
        for c in candidates:
            if os.path.isfile(c):
                return c
        return None

    async def handle_kx_filament_slot_profile(self, request):
        """POST /kx/filament/slots/<idx>/profile - saves or deletes
        a user override mapping for a single AMS slot.

        The main key is (vendor, name) - the ID is not unique in Orca's data
        model (136 profiles share e.g. 'OGFL99'). The ID is looked up
        in orca_filaments.json on save and carried along as a hint
        for OrcaSlicer's `tray_info_idx`.

        Body: {"vendor": "Polymaker", "name": "PolyTerra PLA"}
              {"vendor": "", "name": ""} → removes the mapping
              (Backward compatibility: {"id":..., "vendor":...} is accepted,
              but `name` is the main key since v0.9.18.)
        """
        try:
            slot_idx = int(request.match_info.get("idx", "-1"))
        except ValueError:
            return self._json_cors({"error": "invalid slot index"}, status=400)
        if slot_idx < 0:
            return self._json_cors({"error": "invalid slot index"}, status=400)
        try:
            data = await request.json()
        except Exception:
            data = {}
        new_vendor = (data.get("vendor") or "").strip()
        new_name   = (data.get("name")   or "").strip()
        new_id     = (data.get("id")     or "").strip()  # backward-compatibility hint
        if new_vendor and new_name:
            # Look the ID up in the JSON (not in the request body, which may
            # be stale or a generic fallback).
            looked_up_id = self._lookup_filament_id(new_vendor, new_name)
            self._filament_profiles[slot_idx] = {
                "vendor": new_vendor,
                "name":   new_name,
                "id":     looked_up_id or new_id,
            }
        else:
            self._filament_profiles.pop(slot_idx, None)
        # Persiste no config.ini
        try:
            import config_loader as _cl
            _cl.save_filament_profiles(self._filament_profiles, self._printer_id)
        except Exception as e:
            log.warning(f"save_filament_profiles failed: {e}")
            return self._json_cors({"error": str(e)}, status=500)
        entry = self._filament_profiles.get(slot_idx, {})
        return self._json_cors({"result": "ok",
                                "slot_index": slot_idx,
                                "vendor": entry.get("vendor", ""),
                                "name":   entry.get("name", ""),
                                "id":     entry.get("id", "")})

    async def handle_kx_visible_vendors(self, request):
        """GET/POST /kx/filament/visible_vendors — vendor visibility filter
        for the slot profile dropdown (Issue #41 option A).

        GET  → {"result": ["Polymaker", "eSUN", ...]}
        POST {"vendors": [...]} → saves to config.ini [filament_profiles]
             visible_vendors. Empty list = all visible. No bridge restart
             needed (it is only a display filter)."""
        if request.method == "POST":
            try:
                data = await request.json()
            except Exception:
                data = {}
            vendors = data.get("vendors") or []
            if not isinstance(vendors, list):
                return self._json_cors({"error": "vendors must be a list"}, status=400)
            self._visible_vendors = [str(v).strip() for v in vendors if str(v).strip()]
            try:
                import config_loader as _cl
                _cl.save_visible_vendors(self._visible_vendors, self._printer_id)
            except Exception as e:
                log.warning(f"save_visible_vendors failed: {e}")
                return self._json_cors({"error": str(e)}, status=500)
        return self._json_cors({"result": self._visible_vendors})

    def _load_orca_filaments(self) -> list[dict]:
        """Loads system + user profiles from the cache. System ones come
        from data/orca_filaments.json (embedded in the image), user ones
        from <KX_DATA_DIR>/orca_filaments.user.json (persistent on the volume -
        survives image updates). User profiles get the flag
        `is_user: True` so the frontend can mark them."""
        if getattr(self, "_orca_filaments_cache", None) is not None:
            return self._orca_filaments_cache
        merged: list[dict] = []
        # Sistema
        sys_path = self._find_orca_filaments_json()
        if sys_path and os.path.isfile(sys_path):
            try:
                with open(sys_path, encoding="utf-8") as f:
                    merged.extend(json.load(f) or [])
            except Exception as e:
                log.warning(f"error reading orca_filaments.json: {e}")
        # User
        usr_path = self._orca_filaments_user_path()
        if usr_path and os.path.isfile(usr_path):
            try:
                with open(usr_path, encoding="utf-8") as f:
                    for p in (json.load(f) or []):
                        p["is_user"] = True
                        merged.append(p)
            except Exception as e:
                log.warning(f"error reading orca_filaments.user.json: {e}")
        self._orca_filaments_cache = merged
        return self._orca_filaments_cache

    def _orca_filaments_user_path(self) -> str:
        """Path of the user profiles JSON. Lives on the mounted volume (KX_DATA_DIR)
        so image updates do not destroy the data."""
        data_dir = os.environ.get("KX_DATA_DIR") or os.path.join(_WEB_BASE, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "orca_filaments.user.json")

    def _invalidate_filaments_cache(self):
        self._orca_filaments_cache = None

    def _lookup_filament_id(self, vendor: str, name: str) -> str:
        """Looks up the filament_id of a (vendor,name) tuple in
        orca_filaments.json. Returns '' when not found."""
        for p in self._load_orca_filaments():
            if p.get("vendor") == vendor and p.get("name") == name:
                return p.get("id", "")
        return ""

    async def handle_kx_history(self, request):
        limit  = int(request.rel_url.query.get("limit", 50))
        offset = int(request.rel_url.query.get("offset", 0))
        jobs   = self._store.list_jobs(limit=limit, offset=offset)
        for j in jobs:
            j["has_log"] = self._store.job_logs.exists(j["id"])
        return self._json_cors({"result": jobs})

    async def handle_kx_history_log(self, request):
        """GET /kx/history/{id}/log - the print's log, already decompressed, as text.
        ?download=1 returns it as a .txt attachment."""
        job_id = request.match_info.get("id", "")
        text = await asyncio.get_event_loop().run_in_executor(
            None, self._store.job_logs.read, job_id)
        if text is None:
            return self._json_cors({"error": "log not found"}, status=404)
        headers = {}
        if request.rel_url.query.get("download"):
            headers["Content-Disposition"] = f'attachment; filename="moonkobra-job_{os.path.basename(job_id)}.txt"'
        return web.Response(text=text, content_type="text/plain", charset="utf-8", headers=headers)

    # ── Pricing module (Quote) ────────────────────────────────────────────────
    _pricing_parsed: dict = {}   # (path, mtime) -> parse_gcode; shared between printers

    def _pricing_cfg_path(self) -> str:
        return os.path.join(self._store.data_dir, "pricing.json")

    def _pricing_parse(self, path: str) -> dict:
        key = (path, os.path.getmtime(path))
        p = KobraXBridge._pricing_parsed.get(key)
        if p is None:
            p = pricing.parse_gcode(path)
            if len(KobraXBridge._pricing_parsed) > 30:
                KobraXBridge._pricing_parsed.clear()
            KobraXBridge._pricing_parsed[key] = p
        return p

    def _pricing_run(self, file_id: str, opt: dict) -> dict:
        f = self._store.get_file(file_id)
        if not f or not os.path.isfile(f.get("path") or ""):
            raise LookupError("file not found")
        parsed = self._pricing_parse(f["path"])
        # Real price of the imported profile (e.g. 3DFila PLA - Preto = R$ 99.90/kg), matched by name.
        costs = {p.get("name"): p["cost"] for p in self._load_orca_filaments() if p.get("cost")}
        if costs:
            parsed = dict(parsed, profile_cost=[costs.get(orca_filaments.clean_name(n), 0)
                                                for n in parsed.get("settings_ids") or []])
        cfg = pricing.load_config(self._pricing_cfg_path())
        result = pricing.compute(parsed, cfg, opt if isinstance(opt, dict) else {})
        seq = parsed.get("tool_sequence") or []
        return {
            "file": {"id": f["id"], "filename": f["filename"]},
            "parsed": {"time_s": parsed["time_s"], "layers": parsed["layers"], "slicer": parsed["slicer"],
                       "tool_changes": max(len(seq) - 1, 0), "has_grams": bool(parsed["grams"])},
            "result": result,
            "last_job_s": self._store.last_job_duration(f["filename"]),
        }

    # ── Settings backup (whole project) ────────────────────────────────────────
    # Settings only: history, G-codes and saved quotes are data and stay out.
    def _backup_files(self) -> dict:
        """name inside the zip -> path on disk."""
        finder = getattr(env_loader, "find_config_path", None)
        cfg = str(finder() if finder else pathlib.Path(_BASE) / "config" / "config.ini")
        return {
            "config/config.ini": cfg,
            "data/pricing.json": self._pricing_cfg_path(),
            "data/orca_filaments.user.json": self._orca_filaments_user_path(),
        }

    async def handle_backup_export(self, request):
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps({
                "app": "MoonKobra", "version": self._read_version(),
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "files": [n for n, pth in self._backup_files().items() if os.path.isfile(pth)],
            }, indent=2))
            for name, pth in self._backup_files().items():
                if os.path.isfile(pth):
                    z.write(pth, name)
        fname = f"moonkobra-config_{time.strftime('%Y%m%d-%H%M%S')}.zip"
        return web.Response(body=buf.getvalue(), content_type="application/zip",
                            headers={"Content-Disposition": f'attachment; filename="{fname}"'})

    async def handle_backup_import(self, request):
        """Body = the export .zip. Only known names go in, each one validated
        before writing; any error = nothing is written. Then the bridge restarts."""
        import io, zipfile, configparser
        body = await request.read()
        if len(body) > 10 * 1024 * 1024:
            return self._json_cors({"error": "file too large"}, status=413)
        try:
            z = zipfile.ZipFile(io.BytesIO(body))
        except zipfile.BadZipFile:
            return self._json_cors({"error": "not a MoonKobra backup .zip"}, status=400)
        targets, staged = self._backup_files(), {}
        try:
            for name in z.namelist():
                if name not in targets:
                    continue
                raw = z.read(name)
                if name.endswith(".ini"):
                    cp = configparser.ConfigParser(interpolation=None)
                    cp.read_string(raw.decode("utf-8"))
                elif name == "data/pricing.json":
                    raw = json.dumps(pricing.sanitize_config(json.loads(raw)), ensure_ascii=False, indent=2).encode()
                else:
                    lst = json.loads(raw)
                    if not isinstance(lst, list) or not all(isinstance(x, dict) for x in lst):
                        raise ValueError("invalid profiles")
                staged[name] = raw
        except Exception as e:
            return self._json_cors({"error": f"invalid backup: {e}"}, status=400)
        if not staged:
            return self._json_cors({"error": "no configuration file in the .zip"}, status=400)
        for name, raw in staged.items():
            dest = targets[name]
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".tmp", "wb") as f:
                f.write(raw)
            os.replace(dest + ".tmp", dest)
        self._invalidate_filaments_cache()
        log.info(f"Configuration restored from backup: {', '.join(staged)} - restarting")
        asyncio.get_event_loop().call_later(0.5, self._restart_bridge)
        return self._json_cors({"result": sorted(staged)})

    async def handle_pricing_config_get(self, request):
        return self._json_cors({"result": pricing.load_config(self._pricing_cfg_path())})

    async def handle_pricing_config_set(self, request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return self._json_cors({"error": "invalid JSON"}, status=400)
        return self._json_cors({"result": pricing.save_config(self._pricing_cfg_path(), body)})

    async def handle_pricing_calc(self, request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return self._json_cors({"error": "invalid JSON"}, status=400)
        try:
            out = await asyncio.get_event_loop().run_in_executor(
                None, self._pricing_run, str(body.get("file_id", "")), body.get("opt") or {})
        except LookupError as e:
            return self._json_cors({"error": str(e)}, status=404)
        except ValueError as e:
            return self._json_cors({"error": str(e)}, status=400)
        return self._json_cors({"result": out})

    async def handle_pricing_quotes(self, request):
        return self._json_cors({"result": self._store.list_quotes()})

    async def handle_pricing_quote_get(self, request):
        try:
            q = self._store.get_quote(int(request.match_info["id"]))
        except ValueError:
            q = None
        if not q:
            return self._json_cors({"error": "quote not found"}, status=404)
        return self._json_cors({"result": q})

    async def handle_pricing_quote_save(self, request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return self._json_cors({"error": "invalid JSON"}, status=400)
        snap = str(body.get("snapshot") or "")
        if snap and (not snap.startswith("data:image/") or len(snap) > 1_500_000):
            snap = ""
        opt = body.get("opt") or {}
        try:
            out = await asyncio.get_event_loop().run_in_executor(
                None, self._pricing_run, str(body.get("file_id", "")), opt)
        except LookupError as e:
            return self._json_cors({"error": str(e)}, status=404)
        except ValueError as e:
            return self._json_cors({"error": str(e)}, status=400)
        client = str(body.get("client") or "")[:120]
        data = {"opt": opt, "client": client, "notes": str(body.get("notes") or "")[:1000],
                "parsed": out["parsed"], "result": out["result"],
                "config": {k: pricing.load_config(self._pricing_cfg_path())[k]
                           for k in ("currency", "business", "quote_valid_days")}}
        qid = self._store.save_quote(out["file"]["id"], out["file"]["filename"], client,
                                     out["result"]["total"], data, snap)
        return self._json_cors({"result": {"id": qid}})

    async def handle_pricing_quote_delete(self, request):
        try:
            ok = self._store.delete_quote(int(request.match_info["id"]))
        except ValueError:
            ok = False
        return self._json_cors({"result": "ok"} if ok else {"error": "not found"}, status=200 if ok else 404)

    async def handle_kx_file_objects(self, request):
        """Returns a file's object list + optional SVG.

        GET /kx/files/{id}/objects → {"names": [...], "svg_b64": "..."}
        If the file has no objects yet (old entry): asking the printer for file/fileDetails
        and waiting for the answer is the frontend's job
        (reload after the upload). Here it only returns the DB state.
        """
        fid = request.match_info.get("id", "")
        f = self._store.get_file(fid)
        if not f:
            return self._json_cors({"error": "file not found"}, status=404)
        try:
            names = json.loads(f.get("objects_skip_parts") or "[]")
        except Exception:
            names = []
        # No objects in the store yet (new upload from Orca/web): actively ask
        # the printer for file/fileDetails once. _on_file() fills the store,
        # the frontend polls this endpoint and gets the list on the next
        # try (Issue #57 - skip-part parity outside the file browser too).
        if not names:
            fn = f.get("filename") or ""
            if fn:
                try:
                    self.client.publish("file", "fileDetails",
                                        {"root": "local", "filename": fn}, timeout=0)
                except Exception as e:
                    log.debug(f"fileDetails request failed: {e}")
        return self._json_cors({
            "result": {
                "names":   names,
                "svg_b64": f.get("svg_image") or "",
            }
        })

    async def handle_kx_skip(self, request):
        """Triggers a part skip during the print.

        POST /kx/skip  body={"names": ["..", ".."]}
        """
        try:
            body = await request.json()
        except Exception:
            return self._json_cors({"error": "invalid json"}, status=400)
        names = body.get("names") or []
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            return self._json_cors({"error": "names must be list[str]"}, status=400)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: self.client.skip_objects(names))
        except Exception as e:
            return self._json_cors({"error": str(e)}, status=502)
        return self._json_cors({"result": "ok", "names": names})

    def _build_skip_state_result(self) -> dict:
        """Builds the combined skip state for the UI endpoints."""
        filename = self._state.get("filename", "")
        all_objects: list[str] = []
        svg = ""
        if filename:
            try:
                f = self._store.get_file_by_name(filename)
                if f:
                    all_objects = json.loads(f.get("objects_skip_parts") or "[]")
                    svg = f.get("svg_image") or ""
            except Exception as e:
                log.warning(f"skip_state query failed: {e}")
        return {
            "objects":  all_objects,
            "skipped":  list(self._skip_state.get("skipped", [])),
            "svg_b64":  svg,
            "ts":       self._skip_state.get("ts", 0),
            "filename": filename,
        }

    async def handle_kx_skip_query(self, request):
        """Asks the printer for the print's object list again.

        POST /kx/skip/query  → triggers skip/query_obj, waits a little for the
        asynchronous skip/report and returns the merged skip state.
        """
        prev_ts = int(self._skip_state.get("ts", 0) or 0)
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: self.client.query_skip_objects())
        except Exception as e:
            return self._json_cors({"error": str(e)}, status=502)

        deadline = time.time() + 1.5
        while time.time() < deadline:
            if int(self._skip_state.get("ts", 0) or 0) > prev_ts:
                break
            await asyncio.sleep(0.1)

        return self._json_cors({"result": self._build_skip_state_result()})

    async def handle_kx_skip_state(self, request):
        """Current skip state.

        Combines:
        - Full object list: from the G-code store, matched by the name of the file
          being printed (the file/report at print start filled the list).
          skip/query_obj only returns the already skipped ones,
          not the full list.
        - Skipped: from self._skip_state (updated by skip/report).
        """
        return self._json_cors({"result": self._build_skip_state_result()})

    async def handle_kx_printers(self, request):
        # Collect the active printers (with an IP)
        active = [(pid, br) for pid, br in self._all_bridges.items()
                  if (br._args.printer_ip or "").strip()]
        # Host for bridge_url: keep the browser's view, but never export "localhost" -
        # otherwise the browser's fetches fail when the UI is opened through the LAN IP.
        host = request.host.split(":")[0]
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            host = ""
        out = []
        for pid, br in active:
            port = getattr(br._args, "port", 7125)
            # Only set a concrete bridge_url in multi-printer setups (fetch between instances).
            # One printer: empty bridge_url -> the JS uses relative paths (same origin as the UI).
            bridge_url = ""
            if len(active) > 1 and host:
                bridge_url = f"http://{host}:{port}"
            out.append({
                "id":         pid,
                "name":       br._state.get("printer_name") or f"Drucker {pid}",
                "bridge_url": bridge_url,
                "printer_ip": br._args.printer_ip,
                "device_id":  br._args.device_id or "",
                "has_power_control": bool(
                    (getattr(br._args, "power_on_url", "") or "").strip()
                    or (getattr(br._args, "power_off_url", "") or "").strip()
                ),
                "power_status_inverted": bool(getattr(br._args, "power_status_inverted", 0)),
            })
        return self._json_cors({"result": out})

    async def handle_kx_printer_power(self, request):
        """Turns an external smart plug (e.g. Tasmota) on/off for a printer that
        has no power-off/standby command of its own at the MQTT level (Issue #103).

        Only fires a plain HTTP GET at the configured power_on_url/power_off_url -
        works with Tasmota-style cmnd=Power%20on/off URLs and any other
        switch that exposes an on/off endpoint triggered by GET."""
        pid = str(request.match_info.get("pid", "")).strip()
        br = self._all_bridges.get(pid)
        if br is None:
            return self._json_cors({"error": "unknown printer id"}, status=404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str(body.get("action", "")).lower()
        if action not in ("on", "off"):
            return self._json_cors({"error": "action must be 'on' or 'off'"}, status=400)
        url = getattr(br._args, f"power_{action}_url", "") or ""
        if not url:
            return self._json_cors({"error": f"no power_{action}_url configured"}, status=400)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    ok = resp.status == 200
        except Exception as e:
            return self._json_cors({"error": f"smart plug unreachable: {e}"}, status=502)
        return self._json_cors({"result": "ok" if ok else "error", "status": "on" if action == "on" else "off"})

    async def handle_kx_printer_power_status(self, request):
        """Asks the configured smart plug for its current on/off state.

        First tries to read a Tasmota-style JSON body {"POWER":"ON"/"OFF"},
        and falls back to a plain "ON"/"OFF" substring search in the raw
        response, so other switch firmwares with a simpler status endpoint
        work too."""
        pid = str(request.match_info.get("pid", "")).strip()
        br = self._all_bridges.get(pid)
        if br is None:
            return self._json_cors({"error": "unknown printer id"}, status=404)
        url = getattr(br._args, "power_status_url", "") or ""
        if not url:
            return self._json_cors({"error": "no power_status_url configured"}, status=400)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    text = await resp.text()
        except Exception as e:
            return self._json_cors({"error": f"smart plug unreachable: {e}"}, status=502)
        state = "unknown"
        try:
            data = json.loads(text)
            power = str(data.get("POWER", "")).upper()
            if power in ("ON", "OFF"):
                state = power.lower()
        except Exception:
            pass
        if state == "unknown":
            up = text.upper()
            if "ON" in up and "OFF" not in up:
                state = "on"
            elif "OFF" in up:
                state = "off"
        return self._json_cors({"state": state})

    async def handle_kx_print(self, request):
        """Print start from the G-code store with optional filament assignments."""
        try:
            body = await request.json()
        except Exception:
            return self._json_cors({"error": "invalid json"}, status=400)

        file_id = body.get("file_id")
        if not file_id:
            return self._json_cors({"error": "file_id required"}, status=400)

        gcode_file = self._store.get_file(file_id)
        if not gcode_file:
            return self._json_cors({"error": "file not found"}, status=404)

        # filament_assignments: [{slot_index, material, color_hex}, …]
        assignments = body.get("filament_assignments")
        # excluded_objects: ["name1","name2",...] – skip before printing (v0.9.10)
        excluded_objects = body.get("excluded_objects") or []
        if not isinstance(excluded_objects, list):
            excluded_objects = []

        if assignments:
          ams_box_mapping, unused_count, invalid_count = self._build_assigned_ams_box_mapping(assignments)
          if unused_count:
            log.debug(f"Skipped {unused_count} unused filament assignment(s) for mode={self._filament_mode}")
          if invalid_count:
            log.warning(f"Ignored {invalid_count} unusable filament assignment(s) for mode={self._filament_mode}")
            if not ams_box_mapping:
                return self._json_cors({"error": "no usable filament assignment for the current filament mode"}, status=400)
        else:
            # No dialog -> every loaded slot, like a normal upload+print
            ams_box_mapping = self._build_auto_ams_box_mapping()

        auto_leveling = int(body.get("auto_leveling", getattr(self._args, "auto_leveling", 1)))
        filename = gcode_file["filename"]
        file_path = gcode_file["path"]

        # Serve the file through the internal serve endpoint
        url = f"http://localhost:{self._args.port}/serve/{os.path.basename(file_path)}"

        payload = self._build_print_payload(
            filename, url, "", gcode_file.get("size_bytes", 0),
            ams_box_mapping=ams_box_mapping,
            auto_leveling=auto_leveling,
            excluded_objects=excluded_objects,
        )
        self._reset_skip_state(excluded_objects)

        log.info(f"Print start from the KX store: {filename}  ams={len(ams_box_mapping)} slots  assignments={bool(assignments)}  excluded={len(excluded_objects)}")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: self.client.publish("print", "start", payload, timeout=15.0)
        )
        if result is None:
            return self._json_cors({"error": "no response from the printer"}, status=504)

        if excluded_objects:
            loop.run_in_executor(None, lambda: self._apply_preprint_skip_after_start(excluded_objects))

        # Start the job in the history
        self._current_job_id = self._store.start_job(
            gcode_file_id=gcode_file["id"],
            printer_id=getattr(self._args, "device_id", "unknown"),
            filament_assignments=assignments,
            filename=filename,
        )
        self._current_job_filename = filename
        self._store.job_logs.start(self._current_job_id, filename)

        return self._json_cors({"result": "ok", "filename": filename})

    # -------------------------------------------------------------------------
    # Handlers HTTP
    # -------------------------------------------------------------------------

    async def handle_server_info(self, request):
        return web.json_response({
            "result": {
                "klippy_connected": True,
                "klippy_state":     "ready",
                "components":       ["file_manager", "job_state", "virtual_sdcard"],
                "failed_components":[],
                "registered_directories": ["gcodes"],
                "warnings":         [],
                "websocket_count":  len(self.ws_clients),
                "moonraker_version": MOONRAKER_VERSION,
                "api_version":      [1, 3, 0],
                "api_version_string": "1.3.0",
            }
        })

    async def handle_printer_info(self, request):
        s = self._state
        return web.json_response({
            "result": {
                "state":           "ready",
                "state_message":   "Printer is ready",
                "hostname":        "kobrax-bridge",
                "klipper_path":    "/home/pi/klipper",
                "python_path":     "/home/pi/klippy-env/bin/python",
                "log_file":        "/tmp/klippy.log",
                "config_file":     "/home/pi/printer.cfg",
                "software_version": KLIPPER_VERSION,
                "cpu_info":        s["printer_name"],
            }
        })

    async def handle_machine_system_info(self, request):
        return web.json_response({
            "result": {
                "system_info": {
                    "cpu_info": {"cpu_count": 4, "bits": "64bit", "processor": "armv7l",
                                 "cpu_desc": "Anycubic Kobra X Bridge", "serial_number": "",
                                 "hardware_desc": "", "model": "Kobra X Bridge",
                                 "total_memory": 524288, "memory_units": "kB"},
                    "sd_info": {},
                    "distribution": {"name": "Linux", "id": "linux", "version": "1.0",
                                     "version_parts": {}, "like": "", "codename": ""},
                    "available_services": [],
                    "service_state": {},
                    "python": {"version": list(sys.version_info[:3]), "version_string": sys.version},
                    "network": {},
                    "canbus": {},
                }
            }
        })

    async def handle_objects_query(self, request):
        objects = self._build_printer_objects()
        requested = []
        query = request.rel_url.query
        if "objects" in query:
            requested = [x.strip() for x in str(query.get("objects", "")).split(",") if x.strip()]
        elif query:
            requested = [k for k in query.keys() if k]

        filtered = {k: objects[k] for k in requested if k in objects} if requested else objects
        return web.json_response({"result": {"status": filtered, "eventtime": time.time()}})

    async def handle_objects_list(self, request):
        return web.json_response({
            "result": {
                "objects": list(self._build_printer_objects().keys())
            }
        })

    async def handle_objects_subscribe(self, request):
        return web.json_response({
            "result": {
                "status": self._build_printer_objects(),
                "eventtime": time.time(),
            }
        })

    async def handle_files_list(self, request):
        filename = self._state.get("filename", "")
        files = []
        if filename:
            files.append({
                "path":     filename,
                "modified": time.time(),
                "size":     0,
                "permissions": "rw",
            })
        return web.json_response({"result": files})

    def _build_file_metadata(self, filename: str) -> dict:
        """Builds Moonraker file metadata for a file. Shared source
        for HTTP /server/files/metadata AND the WS RPC server.files.metadata
        (the WS path used to have its own broken logic with a nonexistent store
        method → empty answer → Mobileraker asked in an
        endless loop, the app froze on update, Issue #48).

        Delivers the Mobileraker-compatible required fields: `filename`, `size`,
        `modified` are non-nullable in GCodeFile; `print_start_time` and the
        slicer fields are optional."""
        s = self._state
        # The live _state values only matter for the currently/last tracked job's
        # file - using them as a starting point for a DIFFERENT file name
        # leaked the tracked job's layer/time counts into unrelated metadata
        # queries (Issue #102). For any other file, use only
        # that file's own row in the GCodeStore.
        is_tracked_file = bool(filename) and filename == s.get("filename")
        layer_h = float(s.get("layer_height") or 0.0) if is_tracked_file else 0.0
        first_h = float(s.get("first_layer_height") or 0.0) if is_tracked_file else 0.0
        total_layers = int(s.get("total_layers") or 0) if is_tracked_file else 0
        est_time = int(s.get("slicer_time") or 0) if is_tracked_file else 0
        size_bytes = 0
        try:
            gf = self._store.get_file_by_name(filename) or {}
            if not layer_h:
                layer_h = float(gf.get("layer_height") or 0.0)
                first_h = float(gf.get("first_layer_height") or layer_h)
            if not total_layers:
                total_layers = int(gf.get("layer_count") or 0)
            if not est_time:
                est_time = int(gf.get("est_print_time_sec") or 0)
            size_bytes = int(gf.get("size_bytes") or 0)
        except Exception:
            pass
        # Third fallback: the printer's own buried/report analytics event
        # (fires once per print start, regardless of the slicer), for files that
        # are neither the currently tracked job nor in our GCodeStore -
        # e.g. printed straight from Anycubic Slicer Next (Issue #102).
        buried = self._buried_cache
        if buried and buried.get("task_name") == filename:
            if not total_layers:
                total_layers = buried.get("total_layers") or total_layers
            if not est_time:
                est_time = buried.get("estimate_duration") or est_time
            if not size_bytes:
                size_bytes = buried.get("gcode_size") or size_bytes
        if not layer_h:
            layer_h = self._layer_height_from_filename(filename)
            if layer_h and not first_h:
                first_h = layer_h
        object_height = round(first_h + max(0, total_layers - 1) * layer_h, 3) if (layer_h and total_layers) else 0.0
        return {
            "filename":           filename,
            # GCodeFile (Mobileraker) exige size como int non-nullable.
            "size":               size_bytes or 1,
            "modified":           time.time(),
            "estimated_time":     est_time or None,
            "layer_height":       layer_h or None,
            "first_layer_height": first_h or None,
            "layer_count":        total_layers or None,
            "object_height":      object_height or None,
            "thumbnails":         [],
        }

    async def handle_files_metadata(self, request):
        """Moonraker /server/files/metadata — moonraker-obico + Mobileraker
        fetch file metadata (slicer time, layers, object_height).
        Logic in _build_file_metadata (shared with the WS RPC)."""
        filename = request.rel_url.query.get("filename", "") or self._state.get("filename", "")
        if not filename:
            return web.json_response({"result": {}})
        return web.json_response({"result": self._build_file_metadata(filename)})

    # -- Moonraker stubs for moonraker-obico ----------------------------------
    async def handle_access_api_key(self, request):
        """Moonraker /access/api_key - returns the configured API key for
        moonraker-obico to authenticate. With no login configured there is
        nothing to hand over, so the old dummy stays."""
        key = (getattr(self._args, "auth_api_key", "") or "").strip()
        return web.json_response({"result": key or "moonkobra-no-auth-required"})

    async def handle_machine_update_status(self, request):
        """Moonraker /machine/update/status - Obico uses it to show the installed plugins."""
        return web.json_response({
            "result": {
                "busy":         False,
                "github_rate_limit":     60,
                "github_requests_remaining": 60,
                "github_limit_reset_time":   time.time() + 3600,
                "version_info": {},
            }
        })

    async def handle_history_list(self, request):
        """Moonraker /server/history/list - job history from the GCodeStore.

        moonraker-obico only uses the last element (limit=1, order=desc)."""
        try:
            limit = int(request.rel_url.query.get("limit", "50"))
        except ValueError:
            limit = 50
        try:
            jobs = self._store.list_jobs(limit=limit) or []
        except Exception:
            jobs = []
        # Mapping to Moonraker's schema. Moonraker returns start_time as a
        # Unix timestamp (float), not an ISO string - moonraker-obico parses it with
        # int(start_time) and breaks otherwise.
        def _to_unix_ts(iso: str | None) -> float:
            if not iso:
                return 0.0
            try:
                from datetime import datetime
                # Formato do GCodeStore: "2026-05-27T21:22:25Z"
                dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
                return dt.replace(tzinfo=__import__("datetime").timezone.utc).timestamp()
            except Exception:
                return 0.0
        result_jobs = []
        for j in jobs:
            start_ts = _to_unix_ts(j.get("started_at"))
            dur = j.get("duration_sec") or 0
            result_jobs.append({
                "job_id":       j.get("id"),
                "exists":       True,
                "end_time":     (start_ts + dur) if start_ts and dur else None,
                "filament_used": 0.0,
                "filename":     j.get("filename", ""),
                "metadata":     {},
                "print_duration": dur,
                "status":       j.get("status") or "completed",
                "start_time":   start_ts,
                "total_duration": dur,
            })
        return web.json_response({"result": {"count": len(result_jobs), "jobs": result_jobs}})

    # Moonraker temperature history (server.temperature_store): 1 sample
    # per second, 20 min. Feeds the Mainsail/Fluidd/Mobileraker chart
    # when the page opens - without it, it started empty.
    _TEMP_STORE_SIZE = 1200
    _temp_store_lock = threading.Lock()

    def _sample_temperatures(self) -> None:
        """Called on every poll loop cycle. The cycle lasts poll_interval
        seconds, so the sample is repeated for the seconds that passed so the
        chart's time axis (which assumes 1 s per point) stays right.
        ponytail: poll_interval steps instead of a curve; the cycle does not sample faster."""
        import collections
        now = time.monotonic()
        last = getattr(self, "_temp_store_t", None)
        n = 1 if last is None else min(int(now - last), self._TEMP_STORE_SIZE)
        if n <= 0:
            return
        self._temp_store_t = now if last is None else last + n
        s = self._state
        with self._temp_store_lock:
            if not hasattr(self, "_temp_store"):
                self._temp_store = {
                    h: {k: collections.deque(maxlen=self._TEMP_STORE_SIZE)
                        for k in ("temperatures", "targets", "powers")}
                    for h in ("extruder", "heater_bed")
                }
            for h, cur, tgt in (("extruder", s["nozzle_temp"], s["nozzle_target"]),
                                ("heater_bed", s["bed_temp"], s["bed_target"])):
                st = self._temp_store[h]
                st["temperatures"].extend([round(float(cur), 2)] * n)
                st["targets"].extend([float(tgt)] * n)
                st["powers"].extend([0.0] * n)

    def _temperature_store(self) -> dict:
        with self._temp_store_lock:
            return {h: {k: list(v) for k, v in d.items()}
                    for h, d in getattr(self, "_temp_store", {}).items()}

    async def handle_temperature_store(self, request):
        return web.json_response({"result": self._temperature_store()})

    async def handle_webcams_list(self, request):
        """Moonraker /server/webcams/list - Obico fetches the webcam URLs here.

        When the client comes from another host (e.g. moonraker-obico on a
        separate server), it needs absolute URLs to reach the stream.
        A Host header with localhost/127.0.0.1 is replaced by the real LAN IP."""
        host_hdr = request.headers.get("Host", "") if request else ""
        host_name = (host_hdr or "").split(":")[0]
        port_part = f":{host_hdr.split(':')[1]}" if ":" in (host_hdr or "") else f":{self._args.port}"
        local_ip  = getattr(self, "_local_ip", None) or host_name
        if host_name in ("localhost", "127.0.0.1", ""):
            host_name = local_ip
        base         = f"http://{host_name}{port_part}"
        stream_url   = f"{base}/api/camera/stream"
        snapshot_url = f"{base}/api/camera/snapshot"
        return web.json_response({
            "result": {
                "webcams": [
                    {
                        "name":         "MoonKobra",
                        "location":     "printer",
                        "service":      "mjpegstreamer",
                        "enabled":      True,
                        "icon":         "mdiWebcam",
                        "target_fps":   5,
                        "target_fps_idle": 2,
                        "stream_url":   stream_url,
                        "snapshot_url": snapshot_url,
                        "flip_horizontal": False,
                        "flip_vertical":   False,
                        "rotation":     0,
                        "aspect_ratio": "16:9",
                        "extra_data":   {},
                    }
                ]
            }
        })

    def _upload_progress(self, name: str, phase: str, sent: int, total: int, error: str = "") -> None:
        """Stage of the upload in progress, exposed in /api/state -> "upload" so the UI
        shows the bar (receiving -> preparing -> sending to the printer -> done/error).
        Also called from the sending thread; dict assignment is atomic in CPython."""
        self._state["upload"] = {"name": name, "phase": phase, "sent": int(sent), "total": int(total),
                                 "error": error, "ts": time.time()}

    async def handle_file_upload(self, request):
        log.info(f"Upload request: {request.method} {request.path_qs}  CT={request.headers.get('Content-Type','')[:60]}")
        ct = request.headers.get("Content-Type", "")
        if "multipart" not in ct:
            return web.json_response({"error": "multipart expected"}, status=400)
        auto_print = False
        web_upload = False
        reader = await request.multipart()
        file_data = None
        remote_filename = self._last_uploaded_file or "upload.gcode"
        total_in = request.content_length or 0
        self._upload_progress(remote_filename, "receiving", 0, total_in)

        async for part in reader:
            if part.name in ("file", "gcode", "upload_file"):
                remote_filename = part.filename or remote_filename
                # Read in blocks so the "receiving" progress shows in the UI - applies to
                # the browser upload AND the OrcaSlicer one (same endpoint).
                buf = bytearray()
                while True:
                    chunk = await part.read_chunk(1 << 20)
                    if not chunk:
                        break
                    buf += chunk
                    self._upload_progress(remote_filename, "receiving", len(buf), total_in)
                file_data = bytes(buf)
                del buf
                log.info(f"Campo multipart '{part.name}': {remote_filename} ({len(file_data)} bytes)")
            elif part.name == "path":
                val = (await part.read()).decode("utf-8", errors="replace").strip()
                if val:
                    remote_filename = val
            elif part.name == "print":
                val = (await part.read()).decode("utf-8", errors="replace").strip().lower()
                auto_print = val == "true"
            elif part.name == "web_upload":
                val = (await part.read()).decode("utf-8", errors="replace").strip().lower()
                web_upload = val == "true"
            else:
                log.debug(f"Unknown multipart field: {part.name}")

        if not file_data:
            self._upload_progress(remote_filename, "error", 0, 0, "no file received")
            return web.json_response({"error": "no file received"}, status=400)

        # Only printable files allowed (Issue #59) - the Kobra X accepts
        # only .gcode and .bgcode; .3mf uploads are not processed by the
        # printer and are therefore rejected (Issue #59, @gangoke).
        _allowed_ext = (".gcode", ".bgcode")
        _fn_lower = (remote_filename or "").lower()
        if not _fn_lower.endswith(_allowed_ext):
            log.warning(f"Upload rejected (not G-code): {remote_filename}")
            self._upload_progress(remote_filename, "error", 0, 0, "only .gcode/.bgcode files")
            return web.json_response(
                {"error": f"only G-code files are allowed ({', '.join(_allowed_ext)})"},
                status=400,
            )

        file_size  = len(file_data)
        self._upload_progress(remote_filename, "processing", file_size, file_size)

        # md5, thumbnail, filaments, layer heights and the disk write run OUTSIDE
        # the event loop: with a large G-code this used to freeze the whole bridge (status,
        # UI, camera) while it lasted.
        def _prepare():
            md5 = hashlib.md5(file_data).hexdigest()
            est = _parse_gcode_estimated_time(file_data)
            thumb = _extract_thumbnail(file_data)
            fil = _extract_filament_info(file_data)
            lh, fh = _parse_gcode_layer_heights(file_data)
            self._store.save_file(
                file_id=md5, filename=remote_filename, data=file_data, est_time_sec=est,
                thumbnail_b64=thumb, gcode_filaments=fil or None, web_unverified=web_upload,
                layer_height=lh, first_layer_height=fh)
            return md5, est, thumb, fil, lh, fh
        loop = asyncio.get_event_loop()
        file_md5, est_time, thumbnail_b64, gcode_filaments, layer_h, first_h = \
            await loop.run_in_executor(None, _prepare)
        self._state["slicer_time"] = est_time
        self._state["layer_height"] = layer_h
        self._state["first_layer_height"] = first_h
        serve_path = os.path.join(self._serve_dir_path, os.path.basename(remote_filename))
        del file_data  # libera RAM

        self._last_uploaded_file = remote_filename
        log.info(f"Upload: {remote_filename} ({file_size} bytes) md5={file_md5} -> store + printer")

        # Send the file to the printer over HTTP (serve_path is already on disk)
        upload_url = self._state.get("upload_url") or None
        self._upload_progress(remote_filename, "sending", 0, file_size)
        t0 = time.time()
        try:
            result = await loop.run_in_executor(
                None, lambda: self.client.upload_gcode(
                    serve_path, remote_filename, upload_url,
                    progress=lambda sent, total: self._upload_progress(remote_filename, "sending", sent, total)))
        except Exception as e:
            log.error(f"Upload failed: {e}")
            self._upload_progress(remote_filename, "error", 0, file_size, str(e))
            return web.json_response({"error": str(e)}, status=500)

        dt = max(time.time() - t0, 0.001)
        log.info(f"Upload succeeded in {dt:.1f}s ({file_size / dt / 1024:.0f} KB/s): {result}")
        self._upload_progress(remote_filename, "done", file_size, file_size)

        # Start the print with the full payload (incl. serve URL + md5 + size)
        serve_url = f"http://{request.host}/serve/{remote_filename}"

        # print=true in the multipart form (Moonraker) or in the query string -> start the print
        # print=false or missing -> upload only
        if not auto_print:
            auto_print = request.rel_url.query.get("print", "false").lower() == "true"

        # Always ask for the thumbnail (the printer answers asynchronously with file/report)
        self._thumbnail_b64 = ""
        self.client.publish("file", "fileDetails", {"root": "local", "filename": remote_filename}, timeout=0)

        self._state["last_upload_url"]  = serve_url
        self._state["last_upload_md5"]  = file_md5
        self._state["last_upload_size"] = file_size

        if auto_print:
            mismatch = self._check_filament_mismatch(gcode_filaments)
            if mismatch:
                log.info(f"Upload+print blocked - filament does not match: {mismatch}")
                self._state["file_ready"] = remote_filename
                self._state["filament_mismatch"] = mismatch
                return self._octoprint_upload_response(
                    request, remote_filename,
                    extra={"filament_mismatch": True, "mismatch_details": mismatch},
                )
            log.info(f"Upload+imprimir (print=true): {remote_filename}")
            self._state["file_ready"] = ""
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, lambda: self._start_print(remote_filename, serve_url, file_md5, file_size, gcode_filaments=gcode_filaments))
        else:
            log.info(f"Upload only (print=false): {remote_filename}")
            self._state["file_ready"] = remote_filename

        return self._octoprint_upload_response(request, remote_filename)

    @staticmethod
    def _octoprint_upload_response(request, remote_filename: str, extra: dict | None = None):
        """OctoPrint-compatible upload response (OrcaSlicer evaluates refs)."""
        body = {
            "done": True,
            "files": {
                "local": {
                    "name": remote_filename,
                    "origin": "local",
                    "path": remote_filename,
                    "refs": {
                        "download": f"http://{request.host}/api/files/local/{remote_filename}",
                        "resource":  f"http://{request.host}/api/files/local/{remote_filename}",
                    }
                }
            },
            "result": {
                "item": {"path": remote_filename, "root": "gcodes"},
                "action": "create_file",
            }
        }
        if extra:
            body.update(extra)
        return web.json_response(body, status=201)

    def _check_filament_mismatch(self, gcode_filaments: list | None) -> list[dict] | None:
        """Compares the G-code filaments (is_used=True) with the currently loaded AMS slots.

        Returns a list of mismatches when at least one used
        G-code slot has no matching material in the AMS - otherwise None.
        Only fires when there is AMS data (at least 1 loaded slot)."""
        if not gcode_filaments:
            return None
        slots = self._ams_slots or []
        occupied = {s["global_index"]: s for s in slots if s.get("type") and s.get("status") == 5}
        if not occupied:
            return None
        mismatches = []
        for f in gcode_filaments:
            if not f.get("is_used"):
                continue
            idx = int(f.get("slot_index", -1))
            gcode_mat = (f.get("material") or "").upper().strip()
            if not gcode_mat:
                continue
            slot = occupied.get(idx)
            if slot is None:
                mismatches.append({
                    "slot_index": idx,
                    "gcode_material": gcode_mat,
                    "ams_material": None,
                    "reason": "empty",
                })
            else:
                ams_mat = (slot.get("type") or "").upper().strip()
                if ams_mat and ams_mat != gcode_mat:
                    mismatches.append({
                        "slot_index": idx,
                        "gcode_material": gcode_mat,
                        "ams_material": ams_mat,
                        "reason": "mismatch",
                    })
        return mismatches if mismatches else None

    def _build_print_payload(self, filename: str, url: str, md5: str, filesize: int,
                             ams_box_mapping: list, auto_leveling: int,
                             excluded_objects: list | None = None,
                             ai_type: int = 1, timelapse_type: int = 64) -> dict:
        """Builds the full print/start MQTT payload. Single source for the
        three print start paths (upload, KX store, Moonraker API)."""
        return {
            "taskid":       "-1",
            "url":          url,
            "filename":     filename,
            "md5":          md5,
            "filepath":     None,
            "filetype":     1,
            "project_type": 1,
            "filesize":     filesize,
            "ams_settings": {
                "use_ams":         len(ams_box_mapping) > 0,
                "ams_box_mapping": ams_box_mapping,
            },
            "task_settings": {
                "auto_leveling":          auto_leveling,
                "vibration_compensation": getattr(self._args, "vibration_compensation", 0),
                "flow_calibration":       0,
                "dry_mode":               0,
                "ai_settings":   {"status": 0, "count": 0, "type": ai_type},
                "timelapse":     {"status": 0, "count": 0, "type": timelapse_type},
                "drying_settings": {"status": 0, "target_temp": 0, "duration": 0, "remain_time": 0},
                "model_objects_skip_parts": excluded_objects or [],
            },
        }

    def _reset_skip_state(self, excluded_objects: list | None = None):
        """Resets the skip state before starting a print. The UI only marks something as
        "skipped" after the printer's real confirmation."""
        self._skip_state = {"skipped": [], "ts": int(time.time())}
        if excluded_objects:
            self._pending_preprint_skip = [str(n) for n in excluded_objects if isinstance(n, str) and n]
            self._pending_preprint_skip_deadline = time.time() + 12.0
        else:
            self._pending_preprint_skip = []
            self._pending_preprint_skip_deadline = 0.0

    def _start_print(self, filename: str, url: str = "", md5: str = "", filesize: int = 0,
                     gcode_filaments: list | None = None):
        self._state["file_ready"] = ""
        loaded = self._select_loaded_slots_for_print(warn_on_empty_default=True)

        # Only map to slots the paints REALLY used in the G-code. OrcaSlicer
        # writes every configured filament in the header (filament_colour=...;...;...),
        # but often uses only one (e.g. single colour -> only T3). If we mapped every
        # loaded slot, the printer would expect every colour and would hang
        # when another (unused) slot was empty. The used paint indices
        # come from _extract_filament_info through is_used (real T<n> tool changes).
        used_paint_indices = None
        if gcode_filaments:
            used = [int(f["slot_index"]) for f in gcode_filaments
                    if f.get("is_used") and "slot_index" in f]
            if used:
                used_paint_indices = set(used)

        if used_paint_indices is not None:
            # G-code paint index N corresponds to AMS slot N (global_index). Only used
            # loaded slots; used but not loaded -> a warning may come later.
            loaded = [(gidx, s) for (gidx, s) in loaded if gidx in used_paint_indices]

        ams_box_mapping = self._build_auto_ams_box_mapping(loaded_slots=loaded)
        log.debug(f"AMS slots: {len(loaded)} mapped (paints used: {used_paint_indices}) -> {[i for i, _ in loaded]}")
        payload = self._build_print_payload(
            filename, url, md5, filesize,
            ams_box_mapping=ams_box_mapping,
            auto_leveling=getattr(self._args, "auto_leveling", 1),
        )
        log.info(f"print/start → {filename}  url={url}  ams={len(ams_box_mapping)} slots  mode={self._filament_mode}")
        result = self.client.publish("print", "start", payload, timeout=15.0)
        if result:
            log.info(f"Print start confirmed: state={result.get('state')}")
        else:
            log.warning("Print start: no response from the printer")

    def _theme_index_path(self) -> str:
        return os.path.join(_WEB_BASE, "web", "themes", self._ui_theme, "index.html")

    def _load_index_template_cached(self) -> str:
        path = self._theme_index_path()
        mtime = os.path.getmtime(path)
        key = (path, mtime)
        if self._index_tpl_cache is not None and self._index_tpl_cache_key == key:
            return self._index_tpl_cache
        with open(path, "r", encoding="utf-8") as f:
            self._index_tpl_cache = f.read()
        self._index_tpl_cache_key = key
        return self._index_tpl_cache

    def _ui_asset_cache_buster(self) -> str:
        base = os.path.join(_WEB_BASE, "web", "themes", self._ui_theme)
        mt = 0.0
        files = [os.path.join(base, fn) for fn in ("index.html", "style.css", "app.js")]
        # lib/ images are served with immutable caching: without going through here, a new
        # Moko pose would never reach someone who already had the page open.
        for sub in ("lib/moko", "lib/icon"):
            try:
                files += [os.path.join(base, sub, f) for f in os.listdir(os.path.join(base, sub))]
            except OSError:
                pass
        for f in files:
            try:
                mt = max(mt, os.path.getmtime(f))
            except OSError:
                pass
        return str(int(mt)) if mt else "0"

    async def handle_print_start(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        filename = (request.rel_url.query.get("filename")
                    or body.get("filename")
                    or self._last_uploaded_file)
        if not filename:
            return web.json_response({"error": "no file name"}, status=400)

        log.info(f"Starting print: {filename}")

        # Optional slot selection from the filament dialog
        filament_assignments = body.get("filament_assignments")
        # Skip before printing (v0.9.10)
        excluded_objects = body.get("excluded_objects") or []
        if not isinstance(excluded_objects, list):
            excluded_objects = []

        auto_leveling = int(body.get("auto_leveling", getattr(self._args, "auto_leveling", 1)))
        url = self._state.get("last_upload_url", "")
        filesize = self._state.get("last_upload_size", 0)
        md5 = self._state.get("last_upload_md5", "")

        if filament_assignments is not None:
            # Explicit slot assignment from the filament dialog
            ams_box_mapping, unused_count, invalid_count = self._build_assigned_ams_box_mapping(filament_assignments)
            if unused_count:
                log.debug(f"Skipped {unused_count} unused filament assignment(s) for mode={self._filament_mode}")
            if invalid_count:
                log.warning(f"Ignored {invalid_count} unusable filament assignment(s) for mode={self._filament_mode}")
                if not ams_box_mapping:
                    return web.json_response({"error": "no usable filament assignment for the current filament mode"}, status=400)
        else:
            # Reprint from the dashboard: load gcode_filaments from the DB so the
            # used_paint_indices filter applies and empty/shifted slots are not mismapped.
            gcode_filaments = None
            try:
                db_file = self._store.get_file_by_name(filename)
                if db_file and db_file.get("gcode_filaments"):
                    gcode_filaments = json.loads(db_file["gcode_filaments"])
            except Exception as e:
                log.warning(f"Could not load cached gcode_filaments for {filename}: {e} "
                            "- slot mapping falls back to every loaded slot")

            # Set the pre-print skip before calling _start_print
            self._reset_skip_state(excluded_objects)

            log.info(f"print/start api=1 mode={self._filament_mode} assignments=False gcode_filaments={gcode_filaments is not None}")
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, lambda: self._start_print(
                filename, url, md5, filesize,
                gcode_filaments=gcode_filaments,
            ))
            return web.json_response({"result": "ok"})

        payload = self._build_print_payload(
            filename, url, md5, filesize,
            ams_box_mapping=ams_box_mapping,
            auto_leveling=auto_leveling,
            excluded_objects=excluded_objects,
            ai_type=0, timelapse_type=0,
        )
        self._reset_skip_state(excluded_objects)

        log.info(
          f"print/start api=1 mode={self._filament_mode} "
          f"ams={len(ams_box_mapping)} slots assignments=True"
        )

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: self.client.publish("print", "start", payload, timeout=15.0)
        )
        if result is None:
            return web.json_response({"error": "no response from the printer"}, status=504)

        if excluded_objects:
            loop.run_in_executor(None, lambda: self._apply_preprint_skip_after_start(excluded_objects))

        return web.json_response({"result": "ok"})

    async def handle_print_pause(self, request):
        loop = asyncio.get_event_loop()
        taskid = self._state.get("taskid", "-1")
        await loop.run_in_executor(None, lambda: self.client.pause_print(taskid))
        return web.json_response({"result": "ok"})

    async def handle_print_resume(self, request):
        loop = asyncio.get_event_loop()
        taskid = self._state.get("taskid", "-1")
        await loop.run_in_executor(None, lambda: self.client.resume_print(taskid))
        return web.json_response({"result": "ok"})

    async def handle_print_cancel(self, request):
        loop = asyncio.get_event_loop()
        taskid = self._state.get("taskid", "-1")
        await loop.run_in_executor(None, lambda: self.client.stop_print(taskid))
        return web.json_response({"result": "ok"})

    async def handle_api_file_ready_clear(self, request):
        self._state["file_ready"] = ""
        self._state["filament_mismatch"] = None
        self._thumbnail_b64 = ""
        self._push_status_update()
        return web.json_response({"result": "ok"})

    async def handle_octoprint_version(self, request):
        return web.json_response({
            "api":     "0.1",
            "server":  "1.9.0",
            "text":    "OctoPrint (Kobra X Bridge)",
        })

    async def handle_kx_ui_asset(self, request):
        name = request.match_info.get("name", "").lstrip("/")
        ctype = _KX_UI_ASSETS.get(name)
        cache_control = "public, max-age=86400"

        if ctype is not None:
            path = os.path.join(_WEB_BASE, "web", "themes", self._ui_theme, name)
        elif name.startswith("lib/"):
            ext = os.path.splitext(name)[1].lower()
            ctype = _KX_UI_LIB_TYPES.get(ext)
            if not ctype:
                raise web.HTTPNotFound()
            path = os.path.join(_WEB_BASE, "web", "themes", self._ui_theme, name)
        else:
            m = _KX_UI_TRANSLATION_RE.match(name)
            if not m:
                raise web.HTTPNotFound()
            lang = m.group(1)
            ctype = "application/json"
            cache_control = "no-store"
            path = os.path.join(_WEB_BASE, "web", "translations", f"{lang}.json")

        # Serve binary assets (fonts) as bytes - read_text() would die with a 500
        # on the first non-UTF-8 chunk of a .woff2.
        if not ctype.startswith(("text/", "application/javascript", "application/json")):
            try:
                data = pathlib.Path(path).read_bytes()
            except OSError:
                raise web.HTTPNotFound()
            return web.Response(body=data, content_type=ctype,
                                headers={"Cache-Control": "public, max-age=31536000, immutable"})

        try:
            raw = pathlib.Path(path).read_text(encoding="utf-8")
        except OSError:
            raise web.HTTPNotFound()
        if name == "app.js":
            raw = raw.replace("'__VERSION__'", f"'{self._read_version()}'")
        return web.Response(
            text=raw,
            content_type=ctype,
            headers={"Cache-Control": cache_control},
        )

    async def handle_index(self, request):
        try:
            tpl = self._load_index_template_cached()
        except OSError:
            p = self._theme_index_path()
            log.error("Web UI theme file missing or unreadable: %s (theme: %s)", p, self._ui_theme)
            return web.Response(
                text="<pre>MoonKobra: index.html not found.\nExpected:\n"
                + html.escape(p, quote=True)
                + "</pre>",
                status=500,
                content_type="text/html; charset=utf-8",
            )
        page = tpl.replace("__UI_ASSETS_VER__", self._ui_asset_cache_buster())

        # Embed CSS + JS INLINE instead of only linking them. The device tab webview
        # embedded in OrcaSlicer does NOT load external <link>/<script src>
        # (only the bare HTML) -> without inlining not even
        # one button works there (Issue #29). In a normal browser it works the same.
        base = os.path.join(_WEB_BASE, "web", "themes", self._ui_theme)

        # Also embed the CSS of the bundled libs — OrcaSlicer's webview does not load
        # external <link>/<script src>.
        def _inline_css(rel_path: str, link_tag: str):
            nonlocal page
            try:
                data = pathlib.Path(os.path.join(base, rel_path)).read_text(encoding="utf-8")
                page = page.replace(link_tag, "<style>\n" + data + "\n</style>")
            except OSError:
                pass

        def _inline_js(rel_path: str, script_tag: str, version_sub: bool = False):
            nonlocal page
            try:
                data = pathlib.Path(os.path.join(base, rel_path)).read_text(encoding="utf-8")
                if version_sub:
                    data = data.replace("'__VERSION__'", f"'{self._read_version()}'")
                page = page.replace(script_tag, "<script>\n" + data + "\n</script>")
            except OSError:
                pass

        _inline_css("lib/icons.css", '<link rel="stylesheet" href="/kx/ui/lib/icons.css">')
        _inline_css("style.css", '<link rel="stylesheet" href="/kx/ui/style.css">')
        _inline_js("app.js", '<script src="/kx/ui/app.js"></script>', version_sub=True)

        return web.Response(text=page, content_type="text/html",
                            headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

    async def handle_api_light(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        on         = bool(body.get("on", True))
        brightness = int(body.get("brightness", self._state["light_brightness"]))
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self.client.publish(
            "light", "control",
            {"type": 3, "status": 1 if on else 0, "brightness": brightness},
            timeout=0
        ))
        self._state["light_on"]         = on
        self._state["light_brightness"] = brightness
        self._light_desired = on
        return web.json_response({"result": "ok"})

    async def handle_api_fan(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        speed = int(body.get("speed", 0))
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self.client.publish(
            "fan", "setSpeed", {"fan_speed_pct": speed}, timeout=0
        ))
        self._state["fan_speed"] = speed
        return web.json_response({"result": "ok"})

    async def handle_api_connect(self, request):
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self.client.connect)
            self._state["print_state"] = "standby"
            self._state["kobra_state"] = "free"
            log.info("Connected manually")
            return web.json_response({"result": "connected"})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def handle_api_disconnect(self, request):
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self.client.disconnect)
        except Exception:
            pass
        self._state["print_state"] = "error"
        self._state["kobra_state"] = "offline"
        log.info("Disconnected manually")
        return web.json_response({"result": "disconnected"})

    async def handle_api_restart(self, request):
        log.info("Restart requested through the API")
        response = web.json_response({"status": "restarting"})
        asyncio.get_event_loop().call_later(0.3, self._restart_bridge)
        return response

    async def handle_api_speed(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        mode = int(body.get("mode", 2))
        loop = asyncio.get_event_loop()
        taskid = self._state.get("taskid", "-1")
        await loop.run_in_executor(None, lambda: self.client.publish_web(
            "print", "update",
            {"taskid": taskid, "settings": {"print_speed_mode": mode}},
        ))
        self._state["print_speed_mode"] = mode
        return web.json_response({"result": "ok"})

    async def handle_api_ams_set_slot(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        index  = int(body.get("index", 0))   # global slot index
        mat    = str(body.get("type", "PLA")).upper()
        color  = body.get("color", [255, 255, 255])
        if not (isinstance(color, list) and len(color) == 3):
            return web.json_response({"error": "color must be [r,g,b]"}, status=400)
        box_id, local_slot = self._global_to_box_slot(index)
        loop = asyncio.get_event_loop()
        self._state["last_ams_set_error"] = False
        # Kept so a later report with state="failed" (which carries no
        # slot info of its own, see _on_multicolor_box) can be logged together with the
        # request that triggered it - otherwise the failure cannot be attributed.
        self._last_ams_set_request = {"global": index, "box": box_id, "local_slot": local_slot, "type": mat, "color": color}
        # setInfo goes through the web/printer topic (like tempature/set). Verified through
        # Workbench-Vue's mqtt_setInfo — through slicer/printer/ the
        # slot changes are ignored by the printer and overwritten with the old
        # material on the next multiColorBox/report.
        def _send():
            self.client.publish_web(
                "multiColorBox", "setInfo",
                {"multi_color_box": [{"id": box_id, "slots": [{"index": local_slot, "type": mat, "color": color}]}]},
            )
            log.info(f"setInfo (web) global={index} box={box_id} local_slot={local_slot} type={mat} color={color}")
        await loop.run_in_executor(None, _send)
        # Optimistic update: adjust the cached slot right away (the printer echoes
        # soon through multiColorBox/report — if it ignores the command,
        # the report overwrites it again).
        for s in self._ams_slots:
            if s.get("global_index") == index:
                s["type"]  = mat
                s["color"] = color
                break
        return web.json_response({"result": "ok"})

    async def handle_api_ams_feed(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        slot_index = int(body.get("slot_index", 0))
        feed_type  = int(body.get("type", 1))
        if feed_type == 1:
          self._pending_load_slot = slot_index
        # Unload (type=2): if no slot was chosen explicitly, use the last loaded one
        if feed_type == 2 and self._ams_loaded_slot >= 0:
            slot_index = self._ams_loaded_slot
        box_id, local_slot = self._global_to_box_slot(slot_index)
        loop = asyncio.get_event_loop()
        def _send():
            resp = self.client.publish(
                "multiColorBox", "feedFilament",
                {"multi_color_box": [{"id": box_id, "feed_status": {"slot_index": local_slot, "type": feed_type}}]},
                timeout=5
            )
            log.info(f"feedFilament type={feed_type} global_slot={slot_index} box={box_id} local_slot={local_slot} loaded_slot={self._ams_loaded_slot} → {resp}")
        await loop.run_in_executor(None, _send)
        return web.json_response({"result": "ok"})

    async def handle_api_ace_auto_feed(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}

        ace_id_raw = body.get("ace_id", None)
        on_raw = body.get("on", None)
        if ace_id_raw is None or on_raw is None:
            return web.json_response({"error": "ace_id and on are required"}, status=400)
        try:
            ace_id = int(ace_id_raw)
            on = int(bool(on_raw))
        except Exception:
            return web.json_response({"error": "invalid parameters"}, status=400)
        if not (0 <= ace_id <= 3):
            return web.json_response({"error": "ace_id must be 0-3"}, status=400)

        payload = {"multi_color_box": [{"id": ace_id, "auto_feed": on}]}
        loop = asyncio.get_event_loop()
        # Fire-and-forget: the setAutoFeed ACK arrives through the multiColorBox/report callback.
        # Waiting for a reply on that busy push topic causes false "code:0" rejections.
        await loop.run_in_executor(
            None,
            lambda: self.client.publish("multiColorBox", "setAutoFeed", payload, timeout=0)
        )
        self._ace_auto_feed[ace_id] = on
        self._state_dirty = True
        return web.json_response({"result": "ok", "ace_id": ace_id, "auto_feed": on})

    async def handle_api_ace_dry(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}

        action = str(body.get("action", "start")).lower()
        if action not in ("start", "stop"):
            return web.json_response({"error": "action must be 'start' or 'stop'"}, status=400)

        ace_ids = [i for i in self._ace_box_ids if 0 <= i <= 3]
        if not ace_ids:
            ace_ids = sorted({
                int(s.get("box_id", -1))
                for s in self._ams_slots
                if 0 <= int(s.get("box_id", -1)) <= 3
            })
        if not ace_ids and self._state.get("filament_mode") != "toolhead":
            ace_ids = [0]
        if not ace_ids:
            return web.json_response({"error": "ACE not detected"}, status=400)

        ace_id_raw = body.get("ace_id", None)
        if ace_id_raw is not None:
          try:
            ace_id = int(ace_id_raw)
          except Exception:
            return web.json_response({"error": "ace_id must be an integer"}, status=400)
          if ace_id not in ace_ids:
            return web.json_response({"error": f"ACE {ace_id + 1} not detected"}, status=400)
          ace_ids = [ace_id]

        if action == "start":
            target_temp = int(body.get("target_temp", 45))
            duration = int(body.get("duration", 240))
            target_temp = max(30, min(80, target_temp))
            duration = max(10, min(24 * 60, duration))
            humidity = (self._state.get("ace_drying") or {}).get("humidity")
            current_temp = (self._state.get("ace_drying") or {}).get("current_temp")
            drying_status = {
                "status": 1,
                "target_temp": target_temp,
                "duration": duration,
                "remain_time": duration,
            }
            ui_state = {
                "status": 1,
                "target_temp": target_temp,
                "duration": duration,
                "remain_time": duration,
                "humidity": humidity,
                "current_temp": current_temp,
            }
        else:
            drying_status = {"status": 0}
            humidity = (self._state.get("ace_drying") or {}).get("humidity")
            current_temp = (self._state.get("ace_drying") or {}).get("current_temp")
            ui_state = {
                "status": 0,
                "target_temp": 0,
                "duration": 0,
                "remain_time": 0,
                "humidity": humidity,
                "current_temp": current_temp,
            }

        payload = {
            "multi_color_box": [
                {"id": bid, "drying_status": dict(drying_status)}
                for bid in ace_ids
            ]
        }

        loop = asyncio.get_event_loop()

        def _send():
            return self.client.publish("multiColorBox", "setDry", payload, timeout=0)
        # Fire-and-forget: the setDry ACK arrives through the multiColorBox/report callback.
        # Waiting for a reply on that busy push topic causes false "code:0" rejections.
        await loop.run_in_executor(None, _send)

        self._state["ace_drying"] = ui_state
        self._state_dirty = True
        return web.json_response({"result": "ok"})

    async def handle_api_axis(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}

        loop = asyncio.get_event_loop()
        action = str(body.get("action", "")).lower()

        if action == "turnoff":
            await loop.run_in_executor(None, lambda: self.client.publish(
                "axis", "turnOff", None, timeout=0
            ))
        else:
            axis = int(body.get("axis", 4))
            move_type = int(body.get("move_type", 2))
            distance = float(body.get("distance", 0))
            await loop.run_in_executor(None, lambda: self.client.publish(
                "axis", "move",
                {"axis": axis, "move_type": move_type, "distance": distance},
                timeout=0
            ))

        return web.json_response({"result": "ok"})

    async def handle_api_temperature(self, request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        nozzle = body.get("nozzle")
        bed    = body.get("bed")
        loop = asyncio.get_event_loop()
        printing = self._state.get("print_state") == "printing"
        if printing:
            # While printing: runtime update through the web/printer topic, one setting at a time
            taskid = self._state.get("taskid", "-1")
            if nozzle is not None:
                n = int(float(nozzle))
                await loop.run_in_executor(None, lambda: self.client.publish_web(
                    "print", "update",
                    {"taskid": taskid, "settings": {"target_nozzle_temp": n}},
                ))
            if bed is not None:
                b = int(float(bed))
                await loop.run_in_executor(None, lambda: self.client.publish_web(
                    "print", "update",
                    {"taskid": taskid, "settings": {"target_hotbed_temp": b}},
                ))
        else:
            # Idle: tempature/set through the `web/printer` topic with a `type` field.
            # Confirmed by sniffing Anycubic Slicer Next live on 2026-05-29:
            #   topic = web/printer/.../tempature
            #   data  = {"type": 0|1|2, "target_hotbed_temp": B, "target_nozzle_temp": N}
            # type values (from the Workbench Vue): 0=nozzle, 1=bed, 2=both.
            # Without `type` OR on the `slicer/printer` topic → system error on the printer.
            if nozzle is not None and bed is not None:
                t, n, b = 2, int(float(nozzle)), int(float(bed))
            elif nozzle is not None:
                t, n, b = 0, int(float(nozzle)), 0
            elif bed is not None:
                t, n, b = 1, 0, int(float(bed))
            else:
                return web.json_response({"result": "ok"})
            await loop.run_in_executor(None, lambda: self.client.publish_web(
                "tempature", "set",
                {"type": t, "target_nozzle_temp": n, "target_hotbed_temp": b},
            ))
        return web.json_response({"result": "ok"})

    async def handle_api_camera(self, request):
        return web.json_response({"url": self._state["camera_url"]})

    async def handle_api_camera_start(self, request):
        loop = asyncio.get_event_loop()
        # Wait for the pushStarted confirmation before returning
        self._camera_start_ts = time.time()
        result = await loop.run_in_executor(None, lambda: self.client.publish(
            "video", "startCapture", None, timeout=8.0
        ))
        state = (result or {}).get("state", "")
        log.info(f"Camera startCapture: state={state}")
        return web.json_response({"result": "ok", "state": state})

    async def handle_api_camera_stop(self, request):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self.client.publish(
            "video", "stopCapture", None, timeout=0
        ))
        # Keeps the auto-start guard from turning the camera on again during the
        # running print (state flapping issue).
        self._camera_user_stopped = True
        return web.json_response({"result": "ok"})

    async def handle_api_camera_reset(self, request):
        """Resets the backoff counter and restarts ffmpeg right away.
        Useful after a 429 block (Retry-After expired) or after restarting the printer."""
        self.camera_cache.reset()
        url = self._state.get("camera_url", "")
        if not url:
            log.warning("Camera reset requested, but there is no known camera_url yet (waiting for printer status)")
            return web.json_response({
                "result": "no_url",
                "message": "No camera URL known yet - wait for the next printer status update, or start a print/turn the camera on first.",
            })
        self.camera_cache.set_url(url)
        await self.camera_cache.ensure_running()
        return web.json_response({"result": "ok", "url": url})

    async def handle_api_camera_snapshot(self, request):
        """Latest JPEG frame from the CameraCache - instant from RAM,
        no separate ffmpeg instance (avoids the printer's single-client 429
        and is ~1 s faster)."""
        url = self._state.get("camera_url", "")
        if not url:
            return web.Response(status=503, text="No camera URL known")
        self.camera_cache.set_url(url)
        await self._ensure_camera_capture()
        await self.camera_cache.ensure_running()
        # Warm-up: wait up to 12 s for the first frame
        deadline = time.time() + 12.0   # the camera may be starting right now
        while not self.camera_cache.latest_jpeg and time.time() < deadline:
            await asyncio.sleep(0.1)
        jpeg = self.camera_cache.latest_jpeg
        if not jpeg:
            return web.Response(status=503, text="No frame in cache yet")
        # If the last frame is older than 10 s -> the cache's ffmpeg is probably
        # not running steadily anymore; deliver it anyway, but with a stale header.
        age = time.time() - self.camera_cache.latest_jpeg_ts
        headers = {"Cache-Control": "no-cache"}
        if age > 10:
            headers["X-Frame-Age"] = f"{age:.1f}"
        return web.Response(body=jpeg, content_type="image/jpeg", headers=headers)

    async def handle_camera_stream(self, request):
        """Live MJPEG view, served as multipart/x-mixed-replace.

        Fed by the central CameraCache fan-out (same pattern as
        handle_camera_h264) instead of spawning a dedicated ffmpeg process
        per HTTP client. The printer's camera server only tolerates a very
        limited number of simultaneous connections (see the CameraCache docstring)
        - every consumer of this endpoint (dashboard, OrcaSlicer,
        moonraker-obico, a second browser tab, ...) used to open its own
        connection, so two simultaneous viewers could already exhaust the
        printer's connection limit and cause intermittent "stream
        unavailable" failures. Now every consumer shares one connection.
        """
        url = self._state.get("camera_url", "")
        if not url:
            return web.Response(status=503, text="No camera URL known")
        self.camera_cache.set_url(url)
        await self._ensure_camera_capture()
        await self.camera_cache.ensure_running()

        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)
        self.camera_cache.mjpeg_subscribers.add(q)

        # Wait for the first frame BEFORE resp.prepare() - once prepare() sends
        # the response headers the status is fixed at 200, so a stuck
        # source (Issue #99) must be caught here to actually return a 503
        # instead of leaving the client hanging forever with no frame arriving.
        try:
            first_frame = await asyncio.wait_for(q.get(), timeout=5.0)
        except asyncio.TimeoutError:
            self.camera_cache.mjpeg_subscribers.discard(q)
            return web.Response(status=503, text="No frame in cache yet")

        boundary = "kobraxframe"
        resp = web.StreamResponse(headers={
            "Content-Type": f"multipart/x-mixed-replace;boundary={boundary}",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        })
        await resp.prepare(request)
        try:
            frame = first_frame
            while True:
                header = (
                    f"--{boundary}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame)}\r\n\r\n"
                ).encode()
                try:
                    await resp.write(header + frame + b"\r\n")
                except (ConnectionResetError, asyncio.CancelledError):
                    break
                except Exception:
                    break
                frame = await q.get()
        except Exception as e:
            log.warning(f"Camera stream interrupted: {e}")
        finally:
            self.camera_cache.mjpeg_subscribers.discard(q)

        return resp

    async def handle_camera_h264(self, request):
        """H.264 passthrough as MPEG-TS, fed by the central CameraCache
        fan-out. Allows several consumers in parallel without an extra
        FLV connection to the printer (single-client limit)."""
        url = self._state.get("camera_url", "")
        if not url:
            return web.Response(status=503, text="No camera URL known")
        self.camera_cache.set_url(url)
        # Each h264 client holds one extra connection to the printer (limit / 429),
        # so it is worth knowing who it is.
        log.info(f"H.264 client connected: {request.remote} ({request.headers.get('User-Agent', '?')[:60]})")
        await self.camera_cache.ensure_running(h264=True)

        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        self.camera_cache.h264_subscribers.add(q)

        resp = web.StreamResponse(headers={
            "Content-Type": "video/mp2t",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        })
        await resp.prepare(request)
        try:
            while True:
                chunk = await q.get()
                try:
                    await resp.write(chunk)
                except (ConnectionResetError, asyncio.CancelledError):
                    break
        except Exception as e:
            log.warning(f"H.264 stream interrupted: {e}")
        finally:
            self.camera_cache.h264_subscribers.discard(q)
        return resp

    async def handle_serve_file(self, request):
        """Serves the uploaded G-code files from the temporary directory (for the printer to download)."""
        filename = os.path.basename(request.match_info.get("filename", ""))
        serve_path = os.path.join(self._serve_dir_path, filename)
        if not os.path.isfile(serve_path):
            return web.Response(status=404, text="not found")
        size = os.path.getsize(serve_path)
        log.info(f"Printer downloading file: {filename} ({size} bytes)")
        return web.FileResponse(serve_path, headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        })

    async def handle_api_state(self, request):
        s = self._state
        # Slicer time + thumbnail only exist transiently in the state (set on upload).
        # After a browser reload or a print straight from OrcaSlicer (the file did not
        # come through the UI upload) they are missing -> restore them from the G-code store
        # by the name of the file being printed.
        slicer_time = s["slicer_time"]
        thumbnail = self._thumbnail_b64
        fname = s.get("filename", "")
        if fname and (not slicer_time or not thumbnail):
            try:
                gf = self._store.get_file_by_name(fname)
                if gf:
                    if not slicer_time and gf.get("est_print_time_sec"):
                        slicer_time = int(gf["est_print_time_sec"])
                    if not thumbnail and gf.get("thumbnail_b64"):
                        thumbnail = gf["thumbnail_b64"]
            except Exception:
                pass
        return web.json_response({
            "printer_name":     s["printer_name"],
            "firmware_version": s["firmware_version"],
            "print_state":      s["print_state"],
            "kobra_state":      s["kobra_state"],
            "nozzle_temp":      s["nozzle_temp"],
            "nozzle_target":    s["nozzle_target"],
            "bed_temp":         s["bed_temp"],
            "bed_target":       s["bed_target"],
            "progress":         s["progress"],
            "progress_estimated": bool(s.get("progress_estimated")),
            "print_duration":   s["print_duration"],
            "remain_time":      s["remain_time"],
            "curr_layer":       s["curr_layer"],
            "total_layers":     s["total_layers"],
            "z_mm":             self._estimate_current_z(),
            "filename":         s["filename"],
            "slicer_time":      slicer_time,
            "camera_url":       s["camera_url"],
            "fan_speed":        s["fan_speed"],
            "print_speed_mode": s["print_speed_mode"],
            "auto_leveling":          getattr(self._args, "auto_leveling", 1),
            "vibration_compensation": getattr(self._args, "vibration_compensation", 0),
            "camera_on_print":        getattr(self._args, "camera_on_print", 0),
            "web_upload_warning":     getattr(self._args, "web_upload_warning", 1),
            "light_on":               s["light_on"],
            "light_brightness": s["light_brightness"],
            "ams_slots":        self._ams_slots,
            "ams_loaded_slot":  self._ams_loaded_slot,
            "filament_mode":    s.get("filament_mode", self._filament_mode),
            "ace_drying":       s.get("ace_drying", {"status": 0, "target_temp": 0, "duration": 0, "remain_time": 0, "humidity": None, "current_temp": None}),
            "ace_units":        list(self._ace_box_ids),
            "ace_auto_feed":    dict(self._ace_auto_feed),
            "ace_dry_presets":  self._ace_dry_presets,
            "thumbnail":        thumbnail,
            "connection_error": s["connection_error"],
            "file_ready":       s["file_ready"],
            "print_start_dialog": s.get("print_start_dialog", getattr(self._args, "print_start_dialog", 1)),
            "version":          self._read_version(),
            "pause_msg":        s.get("pause_msg", ""),
            "error_code":       s.get("error_code", 0),
            "storage_total_mb": s.get("storage_total_mb", 0),
            "storage_used_mb":  s.get("storage_used_mb", 0),
            # Only whether a login is active - never the user, password or key.
            # The UI uses it to show the sign-out button.
            "auth_enabled":     bool((getattr(self._args, "auth_user", "") or "").strip()
                                     and (getattr(self._args, "auth_password", "") or "").strip()),
            "clients":          self._clients_summary(),
            "upload":           s.get("upload") or None,
        })

    async def handle_moonraker_database(self, request):
        """OrcaSlicer filament sync: /server/database/item?namespace=lane_data&key=lanes (AFC format)"""
        namespace = request.rel_url.query.get("namespace", "")
        key       = request.rel_url.query.get("key", "")

        if namespace == "lane_data":
            await asyncio.get_event_loop().run_in_executor(None, self._get_ams_slots_fresh)
            lanes = self._build_lane_data()
            log.info(f"AMS sync: {len(lanes)} lanes for OrcaSlicer")
            return web.json_response({
                "result": {
                    "namespace": "lane_data",
                    "key":       key or "lanes",
                    "value":     lanes,
                }
            })

        if namespace in ("AFC", "afc-install", "happy_hare"):
            return web.json_response({
                "result": {"namespace": namespace, "key": key, "value": None}
            })

        # mainsail/presets: Obico asks for temperature presets. The schema is read in
        # find_all_thermal_presets as data['value']['presets'].values(),
        # so we need at least {presets: {}} for it not to break.
        if namespace == "mainsail":
            if key == "presets":
                return web.json_response({
                    "result": {"namespace": "mainsail", "key": "presets",
                               "value": {"presets": {}}}
                })
            return web.json_response({
                "result": {"namespace": "mainsail", "key": key, "value": {}}
            })

        # obico namespace: in-memory KV store for the plugin settings (key=printer_id etc.)
        if namespace == "obico":
            store = self._moonraker_kv_store.setdefault("obico", {})
            if key and key in store:
                return web.json_response({
                    "result": {"namespace": "obico", "key": key, "value": store[key]}
                })
            return web.json_response({
                "result": {"namespace": "obico", "key": key, "value": store if not key else None}
            })

        return web.json_response(
            {"error": {"code": 404, "message": f"Namespace '{namespace}' not found"}},
            status=404
        )

    async def handle_moonraker_database_post(self, request):
        """POST /server/database/item — write to the KV store (used by moonraker-obico).
        moonraker-obico sends namespace/key/value as form-urlencoded POST parameters."""
        # Try JSON, fall back to form-data, fall back to query parameters
        namespace = ""
        key       = ""
        value     = None
        try:
            data = await request.json()
            if isinstance(data, dict):
                namespace = data.get("namespace", "")
                key       = data.get("key", "")
                value     = data.get("value")
        except Exception:
            try:
                form = await request.post()
                namespace = form.get("namespace", "") or ""
                key       = form.get("key", "") or ""
                value     = form.get("value")
            except Exception:
                pass
        if not namespace:
            namespace = request.rel_url.query.get("namespace", "")
        if not key:
            key = request.rel_url.query.get("key", "")
        if namespace and key:
            store = self._moonraker_kv_store.setdefault(namespace, {})
            store[key] = value
            return web.json_response({
                "result": {"namespace": namespace, "key": key, "value": value}
            })
        return web.json_response({"error": {"code": 400, "message": "namespace + key required"}}, status=400)

    async def handle_database_list(self, request):
        """OrcaSlicer checks which namespaces exist to detect the MMU type."""
        return web.json_response({"result": {"namespaces": ["lane_data", "mainsail", "obico"]}})

    def _get_ams_slots_fresh(self):
        """Fetches fresh slot data through getInfo, falling back to the cached data."""
        resp = self.client.publish("multiColorBox", "getInfo", None, timeout=5)
        if resp and resp.get("data"):
            data = resp["data"]
            self._head_tools_model = int(data.get("head_tools_model", self._head_tools_model))
            boxes = data.get("multi_color_box") or []
            if boxes:
                self._update_ace_drying_state(data, boxes)
                self._filament_mode = self._detect_filament_mode(boxes, self._head_tools_model)
                self._state["filament_mode"] = self._filament_mode
                global_slots, global_loaded = self._aggregate_slots(boxes, self._filament_mode)
                activity_map = self._slot_activity_map(boxes, global_loaded)
                for s in global_slots:
                    s["activity"] = activity_map.get(s.get("global_index"), "")
                if global_slots:
                    self._ams_slots = global_slots
                self._ams_loaded_slot = global_loaded
        return self._ams_slots

    # ─── Settings ────────────────────────────────────────────────────────────

    def _find_config_path(self) -> pathlib.Path:
        """Returns the config.ini path."""
        if hasattr(env_loader, "find_config_path"):
            return env_loader.find_config_path()
        # Fallback to the old env_loader
        script_dir = pathlib.Path(_BASE)
        for base in (script_dir, script_dir.parent):
            p = base / "config" / "config.ini"
            if p.is_file():
                return p
        return script_dir / "config" / "config.ini"

    async def handle_api_settings_get(self, request):
        return web.json_response({
            "printer_name":     self._state.get("printer_name", ""),
            "printer_ip":       self._args.printer_ip,
            "lan_ip":           getattr(self, "_local_ip", "") or "",
            "mqtt_port":        self._args.mqtt_port,
            "username":         self._args.username,
            "password":         self._args.password,
            "mode_id":          self._args.mode_id,
            "device_id":        self._args.device_id,
            "power_on_url":     getattr(self._args, "power_on_url", "") or "",
            "power_off_url":    getattr(self._args, "power_off_url", "") or "",
            "power_status_url": getattr(self._args, "power_status_url", "") or "",
            "power_status_inverted": getattr(self._args, "power_status_inverted", 0),
            "default_ams_slot": getattr(self._args, "default_ams_slot", "auto"),
            "auto_leveling":          getattr(self._args, "auto_leveling", 1),
            "vibration_compensation": getattr(self._args, "vibration_compensation", 0),
            "camera_on_print":        getattr(self._args, "camera_on_print", 0),
            "web_upload_warning":     getattr(self._args, "web_upload_warning", 1),
            "delete_printer_file_after_print": getattr(self._args, "delete_printer_file_after_print", 0),
            "print_start_dialog":     getattr(self._args, "print_start_dialog", 1),
            "poll_interval":    getattr(self._args, "poll_interval", 3),
            **{k: _log_setting(self._args, k) for k in LOG_LIMITS},
            "verbose_http_log": getattr(self._args, "verbose_http_log", 0),
            "notify_url":       getattr(self._args, "notify_url", "") or "",
            "filament_profiles": {str(k): v for k, v in self._filament_profiles.items()},
            "visible_vendors":  self._visible_vendors,
            "ace_dry_presets":  self._ace_dry_presets,
            "spoolman_server":  getattr(self._args, "spoolman_server", "") or "",
            "spoolman_sync_rate": getattr(self._args, "spoolman_sync_rate", 0),
            **self._auth_settings(),
        })

    def _auth_settings(self) -> dict:
        """Login state for the settings screen. Never returns the password; the
        API key only when login is on - then whoever sees it already went through the login."""
        user = (getattr(self._args, "auth_user", "") or "").strip()
        on = bool(user and (getattr(self._args, "auth_password", "") or "").strip())
        return {"auth_enabled": on, "auth_user": user,
                "auth_must_change": bool(getattr(self._args, "auth_must_change", False)),
                "auth_camera_token": (getattr(self._args, "auth_camera_token", "") or "") if on else "",
                "auth_api_key": (getattr(self._args, "auth_api_key", "") or "") if on else ""}

    async def handle_api_camera_token(self, request):
        """POST /api/auth/camera-token - generates a new camera token (invalidates the old
        OBS URLs) and restarts the bridge."""
        import configparser
        config_path = self._find_config_path()
        cfg = configparser.ConfigParser(interpolation=None)
        if config_path.is_file():
            cfg.read(config_path, encoding="utf-8")
        if not cfg.has_section("auth"):
            cfg.add_section("auth")
        cfg.set("auth", "camera_token", _new_camera_token())
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("# MoonKobra configuration file\n\n")
            cfg.write(f)
        log.warning("Camera token changed through the UI")
        response = web.json_response({"status": "restarting"})
        asyncio.get_event_loop().call_later(0.3, self._restart_bridge)
        return response

    async def handle_webcam_compat(self, request):
        """/webcam/?action=stream|snapshot - same format as OctoPrint/mjpg-streamer,
        which OBS, apps and plugins already recognise."""
        if request.query.get("action") == "snapshot":
            return await self.handle_api_camera_snapshot(request)
        return await self.handle_camera_stream(request)

    async def _ensure_camera_capture(self) -> None:
        """Turns the printer camera on if nobody did (OBS opening the stream with
        the camera off). No recent frame = camera stopped; asks for startCapture
        at most once every 15 s."""
        if time.time() - self.camera_cache.latest_jpeg_ts < 5.0:
            return
        if time.time() - getattr(self, "_camera_autostart_ts", 0.0) < 15.0:
            return
        self._camera_autostart_ts = time.time()
        self._camera_start_ts = time.time()
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, lambda: self.client.publish(
                "video", "startCapture", None, timeout=8.0))
            log.info("Camera turned on automatically for an external client (stream/snapshot)")
        except Exception as e:
            log.warning(f"Could not turn the camera on: {e}")

    async def handle_api_auth_change(self, request):
        """POST /api/auth/change - password change (required on first access
        with the default login). Body: {"password": "new"}. Only reachable with a
        valid session (the login middleware guarantees it). Restarts the bridge; the session
        cookie stays valid, so the user does not need to sign in again."""
        import configparser
        try:
            data = await request.json()
        except Exception:
            return self._json_cors({"error": "invalid json"}, status=400)
        password = str(data.get("password") or "")
        if len(password) < 8:
            return self._json_cors({"error": "the password must have at least 8 characters"}, status=400)
        if password == auth.DEFAULT_PASSWORD:
            return self._json_cors({"error": "choose a password different from the default"}, status=400)
        config_path = self._find_config_path()
        cfg = configparser.ConfigParser(interpolation=None)
        if config_path.is_file():
            cfg.read(config_path, encoding="utf-8")
        if not cfg.has_section("auth"):
            cfg.add_section("auth")
        cfg.set("auth", "password", auth.hash_password(password))
        cfg.set("auth", "must_change", "0")
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("# MoonKobra configuration file\n\n")
            cfg.write(f)
        log.warning("Login password changed through the UI")
        response = web.json_response({"status": "restarting"})
        asyncio.get_event_loop().call_later(0.3, self._restart_bridge)
        return response

    async def handle_api_auth_post(self, request):
        """POST /api/auth - turns login on/off and writes [auth] to config.ini.

        Body: {"enabled": true, "user": "...", "password": "...", "api_key": "..."}
        The password goes to config.ini only as a scrypt hash. An empty password with login
        already on keeps the current one (so only the user or key can be changed). Restarts the
        bridge so the login middleware picks up the new values."""
        import configparser
        try:
            data = await request.json()
        except Exception:
            return self._json_cors({"error": "invalid json"}, status=400)
        config_path = self._find_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        cfg = configparser.ConfigParser(interpolation=None)
        if config_path.is_file():
            cfg.read(config_path, encoding="utf-8")

        if not data.get("enabled"):
            # enabled=0 instead of deleting the section: without [auth] the next start
            # would recreate the default login.
            cfg.remove_section("auth")
            cfg.add_section("auth")
            cfg.set("auth", "enabled", "0")
            log.warning("Login turned off through the UI")
        else:
            user = str(data.get("user") or "").strip()
            password = str(data.get("password") or "")
            api_key = str(data.get("api_key") or "").strip()
            old_pw = cfg.get("auth", "password", fallback="")
            if not user:
                return self._json_cors({"error": "enter the user"}, status=400)
            if not password and not old_pw:
                return self._json_cors({"error": "enter the password"}, status=400)
            if password and len(password) < 8:
                return self._json_cors({"error": "the password must have at least 8 characters"}, status=400)
            if password == auth.DEFAULT_PASSWORD:
                return self._json_cors({"error": "choose a password different from the default"}, status=400)
            if api_key and len(api_key) < 16:
                return self._json_cors({"error": "the API key must have at least 16 characters"}, status=400)
            if not cfg.has_section("auth"):
                cfg.add_section("auth")
            cfg.remove_option("auth", "enabled")
            if password:
                cfg.set("auth", "must_change", "0")
            elif not old_pw:
                cfg.set("auth", "must_change", "1")
            cfg.set("auth", "user", user)
            cfg.set("auth", "password", auth.hash_password(password) if password else old_pw)
            if api_key:
                cfg.set("auth", "api_key", api_key)
            else:
                cfg.remove_option("auth", "api_key")
            log.warning(f"Login turned on through the UI (user {user!r})")

        with open(config_path, "w", encoding="utf-8") as f:
            f.write("# MoonKobra configuration file\n\n")
            cfg.write(f)
        response = web.json_response({"status": "restarting"})
        asyncio.get_event_loop().call_later(0.3, self._restart_bridge)
        return response

    async def handle_api_settings_post(self, request):
        import configparser
        try:
            data = await request.json()
        except Exception:
            return self._json_cors({"error": "invalid json"}, status=400)
        config_path = self._find_config_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Read the existing config.ini (comments are lost, the values stay)
        cfg = configparser.ConfigParser(interpolation=None)
        if config_path.is_file():
            cfg.read(config_path, encoding="utf-8")

        # Make sure the sections exist
        for section in ("connection", "print", "bridge", "ace_dry_presets", "spoolman"):
            if not cfg.has_section(section):
                cfg.add_section(section)

        printer_ip = str(data.get("printer_ip", self._args.printer_ip or "")).split(":")[0]
        cfg.set("connection", "printer_ip", printer_ip)
        cfg.set("connection", "mqtt_port",  str(data.get("mqtt_port",  self._args.mqtt_port or 9883)))
        cfg.set("connection", "username",   str(data.get("username",   self._args.username  or "")))
        cfg.set("connection", "password",   str(data.get("password",   self._args.password  or "")))
        cfg.set("connection", "mode_id",    str(data.get("mode_id",    self._args.mode_id   or "")))
        cfg.set("connection", "device_id",  str(data.get("device_id",  self._args.device_id or "")))
        cfg.set("connection", "power_on_url",     str(data.get("power_on_url",     getattr(self._args, "power_on_url", "")     or "")).strip())
        cfg.set("connection", "power_off_url",    str(data.get("power_off_url",    getattr(self._args, "power_off_url", "")    or "")).strip())
        cfg.set("connection", "power_status_url", str(data.get("power_status_url", getattr(self._args, "power_status_url", "") or "")).strip())
        cfg.set("connection", "power_status_inverted", str(int(bool(data.get("power_status_inverted", getattr(self._args, "power_status_inverted", 0))))))
        cfg.set("print",      "default_ams_slot", str(data.get("default_ams_slot", getattr(self._args, "default_ams_slot", "auto"))))
        cfg.set("print",      "auto_leveling",           str(data.get("auto_leveling",           getattr(self._args, "auto_leveling",           1))))
        cfg.set("print",      "vibration_compensation",  str(int(bool(data.get("vibration_compensation", getattr(self._args, "vibration_compensation", 0))))))
        cfg.set("print",      "camera_on_print",         str(int(bool(data.get("camera_on_print",        getattr(self._args, "camera_on_print",        0))))))
        cfg.set("print",      "web_upload_warning", str(int(bool(data.get("web_upload_warning", getattr(self._args, "web_upload_warning", 1))))))
        cfg.set("print",      "delete_printer_file_after_print", str(int(bool(data.get("delete_printer_file_after_print", getattr(self._args, "delete_printer_file_after_print", 0))))))
        cfg.set("print",      "print_start_dialog", str(int(bool(data.get("print_start_dialog", getattr(self._args, "print_start_dialog", 1))))))
        if "poll_interval" in data:
            try:
                pi = max(1, min(60, int(data["poll_interval"])))
            except (TypeError, ValueError):
                pi = 3
            cfg.set("bridge", "poll_interval", str(pi))
        elif not cfg.has_option("bridge", "poll_interval"):
            cfg.set("bridge", "poll_interval", "3")
        for k in LOG_LIMITS:
            if k in data:
                cfg.set("bridge", k, str(_log_setting(argparse.Namespace(**{k: data[k]}), k)))
        verbose_http_log = int(bool(data.get("verbose_http_log", getattr(self._args, "verbose_http_log", 0))))
        cfg.set("bridge", "verbose_http_log", str(verbose_http_log))
        _set_verbose_http_log(bool(verbose_http_log))
        self._args.verbose_http_log = verbose_http_log
        if "notify_url" in data:
            cfg.set("bridge", "notify_url", str(data["notify_url"]).strip())
        if re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", str(data.get("notify_lang", ""))):
            cfg.set("bridge", "notify_lang", data["notify_lang"])
        printer_name = str(data.get("printer_name", "")).strip()
        if printer_name:
            cfg.set("bridge", "printer_name", printer_name)
        elif cfg.has_option("bridge", "printer_name"):
            cfg.remove_option("bridge", "printer_name")

        # Spoolman
        if "spoolman_server" in data:
            cfg.set("spoolman", "server", str(data["spoolman_server"]).strip())
        if "spoolman_sync_rate" in data:
            try:
                sr = max(0, int(data["spoolman_sync_rate"]))
            except (TypeError, ValueError):
                sr = 30
            cfg.set("spoolman", "sync_rate", str(sr))

        incoming_presets = data.get("ace_dry_presets") if isinstance(data, dict) else None
        presets = self._sanitize_ace_dry_presets(incoming_presets if isinstance(incoming_presets, dict) else self._ace_dry_presets)
        for key, val in presets.items():
          cfg.set("ace_dry_presets", f"{key}_temp", str(val["temp"]))
          cfg.set("ace_dry_presets", f"{key}_duration_sec", str(val["duration_sec"]))
          if key.startswith("custom_"):
            cfg.set("ace_dry_presets", f"{key}_name", str(val.get("name", key.replace("_", " ").title())))
        self._ace_dry_presets = presets

        with open(config_path, "w", encoding="utf-8") as f:
            f.write("# MoonKobra configuration file\n\n")
            cfg.write(f)
        log.info(f"Settings saved to {config_path}")
        # Send the response, then restart
        response = web.json_response({"status": "restarting"})
        asyncio.get_event_loop().call_later(0.3, self._restart_bridge)
        return response

    async def handle_kx_printer_add(self, request):
        """Adds a printer: fetches the credentials by IP, writes [printer_N], restarts."""
        try:
            body = await request.json()
        except Exception:
            return self._json_cors({"error": "invalid json"}, status=400)
        ip   = str(body.get("printer_ip", "")).strip().split(":")[0]
        name = str(body.get("name", "")).strip()
        if not ip:
            return self._json_cors({"error": "printer_ip required"}, status=400)
        try:
            creds = await _kx_fetch_credentials(ip)
        except Exception as e:
            return self._json_cors({"error": f"printer unreachable or error: {e}"}, status=502)

        import configparser
        config_path = self._find_config_path()
        cfg = configparser.ConfigParser(interpolation=None)
        if config_path.is_file():
            cfg.read(config_path, encoding="utf-8")

        # Find the existing [printer_N] sections + taken http_ports
        n = 1
        existing_ports: set[int] = set()
        while cfg.has_section(f"printer_{n}"):
            p = cfg[f"printer_{n}"]
            if p.get("http_port"):
                try:
                    existing_ports.add(int(p["http_port"]))
                except ValueError:
                    pass
            n += 1

        # No [printer_N], but [connection] filled in? -> migrate it as printer_1
        # (empty [connection] = no existing printer -> no migration, the new one becomes printer_1)
        if n == 1 and cfg.has_section("connection") and (cfg["connection"].get("printer_ip") or "").strip():
            c = cfg["connection"]
            cfg.add_section("printer_1")
            cfg.set("printer_1", "name", self._state.get("printer_name") or "Kobra X")
            for k in ("printer_ip", "mqtt_port", "username", "password", "mode_id", "device_id"):
                if c.get(k):
                    cfg.set("printer_1", k, c.get(k))
            cfg.set("printer_1", "http_port", "7125")
            existing_ports.add(7125)
            n = 2

        # Create the new printer as [printer_n], pick a free port
        new_port = 7125 + (n - 1)
        while new_port in existing_ports:
            new_port += 1
        sec = f"printer_{n}"
        cfg.add_section(sec)
        cfg.set(sec, "name",       name or creds["model"])
        cfg.set(sec, "printer_ip", creds["printer_ip"])
        cfg.set(sec, "mqtt_port",  "9883")
        cfg.set(sec, "username",   creds["username"])
        cfg.set(sec, "password",   creds["password"])
        cfg.set(sec, "mode_id",    creds["mode_id"])
        cfg.set(sec, "device_id",  creds["device_id"])
        cfg.set(sec, "http_port",  str(new_port))

        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("# MoonKobra configuration file\n\n")
            cfg.write(f)
        log.info(f"Printer '{name or creds['model']}' added as {sec} (port {new_port})")
        response = self._json_cors({"status": "restarting", "section": sec, "http_port": new_port})
        asyncio.get_event_loop().call_later(0.5, self._restart_bridge)
        return response

    async def handle_kx_printer_remove(self, request):
        """Removes a printer from config.ini, then restarts.

        - Multi mode: [printer_N] is deleted, the rest renumbered (printer_3 -> printer_2),
          printer_1 always gets http_port 7125.
        - Single mode (no [printer_N], only [connection]): pid "1" clears the [connection] block
          → the bridge starts in offline mode on 7125, the UI stays reachable.
        - When the last [printer_N] is removed: everything goes -> also the "empty" state.
        """
        pid = str(request.match_info.get("pid", "")).strip()
        if not pid:
            return self._json_cors({"error": "printer id required"}, status=400)

        import configparser
        config_path = self._find_config_path()
        cfg = configparser.ConfigParser(interpolation=None)
        if config_path.is_file():
            cfg.read(config_path, encoding="utf-8")

        has_printer_sections = cfg.has_section("printer_1")
        target = f"printer_{pid}"

        if has_printer_sections:
            if not cfg.has_section(target):
                return self._json_cors({"error": f"{target} not found"}, status=404)
            # Collect every [printer_N] (except the one being deleted), renumber
            kept = []
            n = 1
            while cfg.has_section(f"printer_{n}"):
                if str(n) != pid:
                    kept.append(dict(cfg[f"printer_{n}"]))
                cfg.remove_section(f"printer_{n}")
                n += 1
            for i, sec_data in enumerate(kept, start=1):
                sec = f"printer_{i}"
                cfg.add_section(sec)
                for k, v in sec_data.items():
                    cfg.set(sec, k, v)
                cfg.set(sec, "http_port", str(7125 + i - 1))
            remaining = len(kept)
            # Was it the last printer? Then also clear [connection] -> really "no printer"
            if remaining == 0 and cfg.has_section("connection"):
                for k in ("printer_ip", "username", "password", "device_id"):
                    cfg.set("connection", k, "")
        else:
            # Single mode: only pid "1" is valid (pseudo entry of handle_kx_printers)
            if pid != "1":
                return self._json_cors({"error": "no printer with this ID"}, status=404)
            # Clear the [connection] values -> the bridge starts with no printer
            if cfg.has_section("connection"):
                for k in ("printer_ip", "username", "password", "device_id"):
                    cfg.set("connection", k, "")
            remaining = 0

        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("# MoonKobra configuration file\n\n")
            cfg.write(f)
        log.info(f"Printer {target} removed ({remaining} left)")
        response = self._json_cors({"status": "restarting", "removed": target, "remaining": remaining})
        asyncio.get_event_loop().call_later(0.5, self._restart_bridge)
        return response

    def _restart_bridge(self):
        log.info("Restarting the bridge...")
        # config_loader puts the config.ini values in os.environ ("only if not set").
        # On restart the environ must be cleared, otherwise the new process reads
        # the old values instead of the modified config.ini. The keys come
        # from config_loader.CONFIG_ENV_MAPPING (single source of truth) so a
        # new setting is never forgotten here again.
        try:
            import config_loader as _cl
            _restart_env_keys = set(_cl.CONFIG_ENV_MAPPING.keys()) | {"FILE_READY_DIALOG"}
        except Exception:
            _restart_env_keys = ()
        for _k in _restart_env_keys:
            os.environ.pop(_k, None)

        in_docker = os.path.exists("/.dockerenv") or os.environ.get("KX_IN_DOCKER")
        if in_docker:
            # Docker/systemd: just exit the process - the supervisor restarts it (fresh environ)
            log.info("Container environment detected – exiting so the supervisor restarts")
            os._exit(0)

        frozen = getattr(sys, "frozen", False)

        # Linux: os.execv replaces the process image directly - clean even with PyInstaller onefile
        # (subprocess+exit would fail there on the deleted _MEIxxxx temp directory).
        if sys.platform != "win32":
            exe = sys.executable
            try:
                if frozen:
                    os.execv(exe, [exe] + sys.argv[1:])
                else:
                    os.execv(exe, [exe] + sys.argv)
            except Exception as e:
                log.error(f"Restart (execv) failed: {e} - restart the bridge manually")
                os._exit(1)

        # Windows: os.execv is broken there (new PID, the old process returns) -> subprocess
        cmd = ([sys.executable] + sys.argv[1:]) if frozen else ([sys.executable] + sys.argv)
        # Script: new visible console, like the first one. The .exe lives in the tray and
        # has no console. The onefile child must unpack its own _MEI dir, the parent's is
        # deleted on exit.
        env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1", MOONKOBRA_NO_BROWSER="1")
        try:
            subprocess.Popen(cmd, cwd=os.getcwd(), env=env,
                             creationflags=((0 if frozen else subprocess.CREATE_NEW_CONSOLE)
                                            | subprocess.CREATE_NEW_PROCESS_GROUP
                                            | subprocess.CREATE_BREAKAWAY_FROM_JOB))
        except Exception as e:
            log.error(f"Restart failed: {e} - restart the bridge manually")
        if _tray:
            _tray.visible = False  # otherwise a dead icon stays in the tray until hovered
        os._exit(0)

    # ─── Version ──────────────────────────────────────────────────────────────

    def _read_version(self) -> str:
        # PyInstaller onefile extracts VERSION (through moonkobra.spec's datas) to
        # sys._MEIPASS - that is why it uses _WEB_BASE instead of _BASE.
        for base in (pathlib.Path(_WEB_BASE), pathlib.Path(_BASE), pathlib.Path(_BASE).parent):
            p = base / "VERSION"
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()
        return "unknown"

    LOG_KEEPALIVE_S = 25

    async def handle_api_log_stream(self, request):
        """SSE endpoint: streams the log entries live to the browser."""
        resp = web.StreamResponse(headers={
            "Content-Type":  "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        q: asyncio.Queue = asyncio.Queue()
        _log_sse_queues.append(q)
        try:
            # Ring buffer first, then live. With no new log, a keepalive
            # keeps the connection open (the stream used to close every 25 s and the
            # browser reconnected, receiving the whole buffer again).
            for entry in list(_log_buffer):
                await resp.write(f"data: {json.dumps(entry, ensure_ascii=False)}\n\n".encode())
            while True:
                try:
                    entry = await asyncio.wait_for(q.get(), timeout=self.LOG_KEEPALIVE_S)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                await resp.write(f"data: {json.dumps(entry, ensure_ascii=False)}\n\n".encode())
        except ConnectionResetError:
            # The browser closed/reloaded the tab: normal end, not an error.
            pass
        finally:
            if q in _log_sse_queues:
                _log_sse_queues.remove(q)
        return resp

    async def handle_api_log_download(self, request):
        """Returns every buffered log entry as plain text for download."""
        header = (f"# MoonKobra Log  |  Version {self._read_version()}  |  "
                  f"{time.strftime('%Y-%m-%d %H:%M:%S')}  |  {len(_log_buffer)} entries\n")
        lines = [_fmt_log_line(e) for e in _log_buffer]
        text = header + "\n".join(lines) + "\n"
        fname = f"moonkobra-log_{time.strftime('%Y%m%d-%H%M%S')}.txt"
        return web.Response(
            body=text.encode("utf-8"),
            content_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    async def handle_catchall(self, request):
        body = await request.read()
        log.warning(f"UNKNOWN {request.method} {request.path_qs}  body={body[:200]}")
        return web.json_response({"result": {}}, status=200)

    async def handle_favicon(self, request):
        # Minimal 1x1 ICO so the browser does not log a 404
        ico = bytes([
            0,0,1,0,1,0,1,1,0,0,1,0,24,0,40,0,0,0,22,0,0,0,40,0,0,0,
            1,0,0,0,2,0,0,0,1,0,24,0,0,0,0,0,4,0,0,0,0,0,0,0,0,0,0,0,
            0,0,0,0,0,0,0,0,255,102,0,0,0,0,0,0
        ])
        return web.Response(body=ico, content_type="image/x-icon")

    # -------------------------------------------------------------------------
    # Klipper G-code script emulation for moonraker-obico
    # -------------------------------------------------------------------------

    async def _exec_gcode_script(self, script: str) -> str:
        """Maps a Klipper or Marlin G-code line to a Kobra X MQTT
        command. Supports:
        - PAUSE / M25, RESUME / M24, CANCEL_PRINT / M0/M1/M524/ABORT
        - M104 S<temp>                         → nozzle temperature
        - M140 S<temp>                         → bed temperature
        - SET_HEATER_TEMPERATURE HEATER=extruder TARGET=200   (Klipper)
        - SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=60  (Klipper)
        Unknown scripts are acknowledged with 'ok' (Obico, for example, sends G28
        for homing, which the bridge silently ignores)."""
        if not script:
            return "ok"
        s = script.strip().upper()
        loop = asyncio.get_event_loop()

        def _parse_marlin_temp(line: str) -> int | None:
            """Extracts the temperature value from 'M104 S200' or 'M140 S60'."""
            try:
                return int(line.split("S", 1)[1].split()[0])
            except Exception:
                return None

        def _parse_klipper_set_heater(line: str) -> tuple[str | None, int | None]:
            """Extracts heater + target from 'SET_HEATER_TEMPERATURE HEATER=extruder TARGET=143'.
            Heater is 'extruder' or
            'heater_bed', the target is an int. Returns (None,None) on error."""
            heater = None
            target = None
            for part in line.split():
                if part.startswith("HEATER="):
                    heater = part.split("=", 1)[1].strip().lower()
                elif part.startswith("TARGET="):
                    try:
                        target = int(float(part.split("=", 1)[1]))
                    except Exception:
                        pass
            return heater, target

        async def _set_temps(nozzle: int | None, bed: int | None):
            """Sets the nozzle/bed temperature through the right MQTT path -
            printing: print/update with taskid, idle: tempature/set with both."""
            is_printing = self._state.get("print_state") in ("printing", "paused")
            if is_printing:
                taskid = self._state.get("taskid", "")
                if nozzle is not None:
                    await loop.run_in_executor(None, lambda: self.client.publish_web(
                        "print", "update",
                        {"taskid": taskid, "settings": {"target_nozzle_temp": int(nozzle)}},
                    ))
                if bed is not None:
                    await loop.run_in_executor(None, lambda: self.client.publish_web(
                        "print", "update",
                        {"taskid": taskid, "settings": {"target_hotbed_temp": int(bed)}},
                    ))
            else:
                # Idle: tempature/set through the web/printer topic with a type field
                # (live sniff 2026-05-29). type: 0=nozzle, 1=bed, 2=both.
                if nozzle is not None and bed is not None:
                    t, n, b = 2, int(nozzle), int(bed)
                elif nozzle is not None:
                    t, n, b = 0, int(nozzle), 0
                elif bed is not None:
                    t, n, b = 1, 0, int(bed)
                else:
                    return
                await loop.run_in_executor(None, lambda: self.client.publish_web(
                    "tempature", "set",
                    {"type": t, "target_nozzle_temp": n, "target_hotbed_temp": b},
                ))

        try:
            if s in ("PAUSE", "M25"):
                await loop.run_in_executor(None, self.client.pause_print)
            elif s in ("RESUME", "M24"):
                await loop.run_in_executor(None, self.client.resume_print)
            elif s in ("CANCEL_PRINT", "M0", "M1", "M524", "ABORT"):
                await loop.run_in_executor(None, self.client.stop_print)
            elif s.startswith("M104 "):
                t = _parse_marlin_temp(s)
                if t is not None:
                    log.info(f"gcode.script: alvo do bico {t}°C (M104)")
                    await _set_temps(t, None)
            elif s.startswith("M140 "):
                t = _parse_marlin_temp(s)
                if t is not None:
                    log.info(f"gcode.script: alvo da mesa {t}°C (M140)")
                    await _set_temps(None, t)
            elif s.startswith("SET_HEATER_TEMPERATURE"):
                heater, target = _parse_klipper_set_heater(s)
                if target is not None and heater:
                    if heater == "extruder":
                        log.info(f"gcode.script: alvo do bico {target}°C (Klipper)")
                        await _set_temps(target, None)
                    elif heater in ("heater_bed", "bed"):
                        log.info(f"gcode.script: alvo da mesa {target}°C (Klipper)")
                        await _set_temps(None, target)
                    else:
                        log.debug(f"gcode.script: unknown heater '{heater}' ignored")
            else:
                # Unknown script: silently acknowledge OK.
                log.debug(f"gcode.script ignored: {s[:60]}")
        except Exception as e:
            log.warning(f"gcode.script {s[:30]}: {e}")
        return "ok"

    async def handle_printer_gcode_script(self, request):
        """HTTP POST /printer/gcode/script — Klipper G-code wrapper (see _exec_gcode_script)."""
        script = ""
        if request.method == "POST":
            try:
                body = await request.json()
                if isinstance(body, dict):
                    script = body.get("script", "") or ""
            except Exception:
                pass
        if not script:
            script = request.rel_url.query.get("script", "")
        result = await self._exec_gcode_script(script)
        return web.json_response({"result": result})

    # -------------------------------------------------------------------------
    # Handler de WebSocket
    # -------------------------------------------------------------------------

    _CLIENT_TTL = 120.0   # s of inactivity before leaving the list (an open WS counts as active)

    def _note_client(self, ip: str, app: str, version: str = "", ws=None) -> None:
        now = time.time()
        c = self._clients.setdefault((ip, app), {"ip": ip, "app": app, "version": "", "first_seen": now, "ws": set()})
        c["last_seen"] = now
        if version:
            c["version"] = version
        if ws is not None:
            c["ws"].add(ws)
        if ip not in self._client_hosts:
            self._client_hosts[ip] = ""
            asyncio.get_event_loop().run_in_executor(None, self._resolve_client_host, ip)

    def _resolve_client_host(self, ip: str) -> None:
        try:
            name = socket.gethostbyaddr(ip)[0]
            self._client_hosts[ip] = "" if name == ip else name.split(".")[0]
        except Exception:
            pass

    def _clients_summary(self) -> list[dict]:
        now = time.time()
        out = []
        for key, c in list(self._clients.items()):
            c["ws"] = {w for w in c["ws"] if not w.closed}
            if not c["ws"] and now - c["last_seen"] > self._CLIENT_TTL:
                del self._clients[key]
                continue
            out.append({"app": c["app"], "version": c["version"], "ip": c["ip"],
                        "host": self._client_hosts.get(c["ip"], ""),
                        "live": bool(c["ws"]), "idle_s": int(now - c["last_seen"])})
        return sorted(out, key=lambda c: (c["app"] == "Navegador", c["idle_s"]))

    async def handle_websocket(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        ws._loop = asyncio.get_event_loop()
        self.ws_clients.add(ws)
        ws._kx_ip = request.remote or "?"
        self._note_client(ws._kx_ip, *_classify_client(request.headers.get("User-Agent", "")), ws=ws)
        log.info(f"WS client connected ({len(self.ws_clients)} in total)")

        # Send the klippy_ready notification
        await ws.send_str(json.dumps({
            "jsonrpc": "2.0",
            "method":  "notify_klippy_ready",
            "params":  [],
        }))
        # Envia o status inicial
        await ws.send_str(json.dumps({
            "jsonrpc": "2.0",
            "method":  "notify_status_update",
            "params":  [self._build_printer_objects(), time.time()],
        }))

        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._handle_ws_rpc(ws, msg.data)
            elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

        self.ws_clients.discard(ws)
        log.info(f"WS client disconnected ({len(self.ws_clients)} left)")
        return ws

    async def _handle_ws_rpc(self, ws: web.WebSocketResponse, raw: str):
        try:
            req = json.loads(raw)
        except Exception:
            return
        rpc_id = req.get("id")
        method  = req.get("method", "")
        log.info(f"WS RPC: {method}  params={str(req.get('params',''))[:120]}")
        params  = req.get("params") or {}
        if isinstance(params, list):
            params = params[0] if params else {}

        result = None
        error  = None

        try:
            if method in ("printer.info", "printer_info"):
                result = {
                    "state":           "ready",
                    "state_message":   "Printer is ready",
                    "hostname":        "kobrax-bridge",
                    "software_version": KLIPPER_VERSION,
                    "cpu_info":        self._state["printer_name"],
                    "klipper_path":    "/home/pi/klipper",
                    "python_path":     "/home/pi/klippy-env/bin/python",
                }
            elif method in ("server.info", "server_info"):
                result = {
                    "klippy_connected": True,
                    "klippy_state":     "ready",
                    "moonraker_version": MOONRAKER_VERSION,
                    "components":       [],
                    "failed_components": [],
                    "registered_directories": ["gcodes"],
                    "warnings":         [],
                }
            elif method in ("printer.objects.list",):
                result = {"objects": list(self._build_printer_objects().keys())}
            elif method in ("printer.objects.query", "printer.objects.get"):
                objects = params.get("objects", {})
                all_objs = self._build_printer_objects()
                if objects:
                    filtered = {k: all_objs.get(k, {}) for k in objects}
                else:
                    filtered = all_objs
                result = {"status": filtered, "eventtime": time.time()}
            elif method == "printer.objects.subscribe":
                objects = params.get("objects", {})
                all_objs = self._build_printer_objects()
                if objects:
                    filtered = {k: all_objs.get(k, {}) for k in objects}
                else:
                    filtered = all_objs
                result = {"status": filtered, "eventtime": time.time()}
            elif method == "printer.print.start":
                filename = params.get("filename", self._last_uploaded_file)
                loop = asyncio.get_event_loop()
                resp = await loop.run_in_executor(
                    None, lambda: self.client.publish("print", "start",
                        {"filename": filename, "use_ams": False}, timeout=15.0)
                )
                result = "ok" if resp else "timeout"
            elif method == "printer.print.pause":
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.client.pause_print)
                result = "ok"
            elif method == "printer.print.resume":
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.client.resume_print)
                result = "ok"
            elif method == "printer.print.cancel":
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.client.stop_print)
                result = "ok"
            elif method == "machine.system_info":
                result = {"system_info": {"cpu_info": {"cpu_desc": "Kobra X Bridge"}}}
            elif method == "server.files.list":
                result = []
            # ── moonraker-obico passthru targets ──
            elif method == "printer.gcode.script":
                script = (params.get("script") or "").strip().upper() if isinstance(params, dict) else ""
                result = await self._exec_gcode_script(script)
            elif method in ("server.connection.identify",):
                # Obico, OrcaSlicer, Mobileraker... identify themselves on connect. The
                # connection ID does not matter, but name/version show who is connected.
                p = req.get("params") or {}
                if p.get("client_name"):
                    self._note_client(getattr(ws, "_kx_ip", "?"), str(p["client_name"])[:40],
                                      str(p.get("version") or "")[:24], ws=ws)
                result = {"connection_id": 1}
            elif method == "connection.register_remote_method":
                # Obico registers the obico_remote_event callback. We accept it empty.
                result = "ok"
            elif method == "server.webcams.list":
                # WS variant: absolute URL with the real LAN IP instead of localhost
                _lip = getattr(self, "_local_ip", None) or "127.0.0.1"
                _base = f"http://{_lip}:{self._args.port}"
                result = {"webcams": [{
                    "name": "MoonKobra", "location": "printer", "service": "mjpegstreamer",
                    "enabled": True,
                    "stream_url":   f"{_base}/api/camera/stream",
                    "snapshot_url": f"{_base}/api/camera/snapshot",
                    "flip_horizontal": False, "flip_vertical": False, "rotation": 0,
                    "target_fps": 5, "aspect_ratio": "16:9",
                }]}
            elif method == "server.history.list":
                # Reuse the HTTP handler's logic (Moonraker schema with a Unix TS).
                try:
                    jobs = self._store.list_jobs(limit=50) or []
                except Exception:
                    jobs = []
                from datetime import datetime, timezone as _tz
                def _ts(iso):
                    try:
                        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_tz.utc).timestamp()
                    except Exception:
                        return 0.0
                result = {"count": len(jobs), "jobs": [
                    {"job_id": j.get("id"), "exists": True, "filename": j.get("filename",""),
                     "status": j.get("status") or "completed",
                     "print_duration": j.get("duration_sec") or 0,
                     "total_duration": j.get("duration_sec") or 0,
                     "start_time": _ts(j.get("started_at")),
                     "end_time":   (_ts(j.get("started_at")) + (j.get("duration_sec") or 0)) if j.get("started_at") and j.get("duration_sec") else None,
                     "filament_used": 0.0, "metadata": {}}
                    for j in jobs
                ]}
            elif method == "machine.update.status":
                result = {"busy": False, "version_info": {}}
            elif method == "server.temperature_store":
                result = self._temperature_store()
            elif method == "server.files.metadata":
                # Obico + Mobileraker ask for a file's metadata. Same
                # logic as the HTTP endpoint (it used to be a separate broken path with
                # a nonexistent store method -> empty answer ->
                # endless loop in Mobileraker, Issue #48).
                fname = (params or {}).get("filename") if isinstance(params, dict) else None
                fname = fname or self._state.get("filename", "")
                result = self._build_file_metadata(fname) if fname else {}
            else:
                log.debug(f"Unknown RPC method: {method}")
                result = {}
        except Exception as e:
            log.error(f"RPC error in {method}: {e}")
            error = {"code": -32603, "message": str(e)}

        if rpc_id is not None:
            response = {"jsonrpc": "2.0", "id": rpc_id}
            if error:
                response["error"] = error
            else:
                response["result"] = result
            await ws.send_str(json.dumps(response))

    # -------------------------------------------------------------------------
    # Poll loop (synchronous, runs in the executor)
    # -------------------------------------------------------------------------

    def _printer_reachable(self) -> bool:
        """TCP probe on the MQTT port - needs neither ICMP nor root."""
        import socket as _socket
        try:
            with _socket.create_connection(
                (self._args.printer_ip, self._args.mqtt_port), timeout=2.0
            ):
                return True
        except OSError:
            return False

    def _poll_loop(self, stop_event: threading.Event):
        _offline = self._state["kobra_state"] == "offline"
        _probe_interval = 10.0   # seconds between TCP probes in offline mode

        while not stop_event.is_set():
            # ── Offline mode: wait for the printer to become reachable again ────
            if _offline:
                if self._printer_reachable():
                    log.info("Printer reachable - establishing the MQTT connection...")
                    try:
                        self.client.connect()
                        _offline = False
                        self._state["print_state"] = "standby"
                        self._state["kobra_state"] = "free"
                        self._state["connection_error"] = ""
                        log.info("MQTT connection re-established")
                    except Exception as e:
                        err = _mqtt_error_msg(e)
                        self._state["connection_error"] = err
                        log.warning(f"Connection attempt failed: {err}")
                        stop_event.wait(_probe_interval)
                        continue
                else:
                    stop_event.wait(_probe_interval)
                    continue

            # ── Online mode: normal polling ──────────────────────────────────
            try:
                # Queries without waiting for a reply (as hass-anycubic does on LAN):
                # the replies arrive through the _dispatch callbacks. Each
                # query used to wait up to 5 s in sequence - with a slow printer a
                # cycle went past 15 s and every report was processed twice
                # (callback + loop).
                self.client.publish("info", "query", timeout=0)
                self.client.publish("multiColorBox", "getInfo", timeout=0)
                self.client.publish("tempature", "query", timeout=0)
                self.client.publish("fan", "query", timeout=0)
                self._sample_temperatures()
                if (not self.client.is_connected()
                      or time.monotonic() - self.client.last_rx > _SILENT_SESSION_TIMEOUT):
                    # publish() swallows send/reconnect failures internally
                    # (Issue #105), so the dead session is detected here: socket
                    # down or "zombie" session (TCP up, but the printer has sent
                    # nothing for _SILENT_SESSION_TIMEOUT) - without this cut the
                    # dashboard froze on the last state until the container restarted.
                    log.warning("MQTT connection lost (query without reply) - switching to offline mode")
                    self._state["print_state"] = "error"
                    self._state["kobra_state"] = "offline"
                    self._state["connection_error"] = f"MQTT connection lost ({self._args.printer_ip})"
                    try:
                        self.client.disconnect()
                    except Exception:
                        pass
                    _offline = True
                    stop_event.wait(getattr(self._args, "poll_interval", 3))
                    continue
                # While printing: query print/report directly
                if self._state["print_state"] in ("printing", "preheating",
                                                   "auto_leveling", "checking", "init"):
                    self.client.publish("print", "query", timeout=0)
                    self._estimate_progress_if_stale()
                    # Spoolman sync during the print
                    if (self._spoolman and self._spoolman.sync_rate > 0
                            and self._spoolman_slot_spools
                            and self._state.get("print_state") == "printing"):
                        now = time.time()
                        if now - self._spoolman_last_sync >= self._spoolman.sync_rate:
                            self._spoolman_sync_midprint()
                            self._spoolman_last_sync = now
                # Recheck Spoolman reachability periodically so the UI's status
                # indicator reflects the current state, not just the boot result.
                if self._spoolman and time.time() - self._spoolman_last_health_check >= 30.0:
                    self._spoolman_reachable = self._spoolman.health_check()
                    self._spoolman_last_health_check = time.time()
            except Exception as e:
                log.warning(f"Polling error: {e}")
                # Check whether the printer really vanished
                if not self._printer_reachable():
                    log.info("Printer unreachable - switching to offline mode")
                    self._state["print_state"] = "error"
                    self._state["kobra_state"] = "offline"
                    self._state["connection_error"] = f"Printer unreachable ({self._args.printer_ip})"
                    try:
                        self.client.disconnect()
                    except Exception:
                        pass
                    _offline = True
            stop_event.wait(getattr(self._args, "poll_interval", 3))


# ---------------------------------------------------------------------------
# Factory da app + main
# ---------------------------------------------------------------------------

# With no reply at all to info/query for this long while the socket is "up", the session
# is treated as dead and reconnected through the offline path.
# ponytail: fixed value; becomes a setting if some printer really takes longer than this.
_SILENT_SESSION_TIMEOUT = 45.0


def _mqtt_error_msg(exc: Exception) -> str:
    msg = str(exc)
    if "20020005" in msg:
        return "Wrong MQTT credentials (username, password or device ID incorrect)"
    return msg


_SLICER_UA = re.compile(r"(OrcaSlicer|PrusaSlicer|SuperSlicer|BambuStudio|Snapmaker_Orca|Cura|Simplify3D)[/ ]v?([\w.+-]+)?", re.I)


def _classify_client(ua: str) -> tuple[str, str]:
    """(app, version) from the User-Agent."""
    ua = ua or ""
    m = _SLICER_UA.search(ua)
    if m:
        return m.group(1), m.group(2) or ""
    low = ua.lower()
    for key, name in (("mobileraker", "Mobileraker"), ("obico", "Obico"), ("octoapp", "OctoApp"),
                      ("printoid", "Printoid"), ("homeassistant", "Home Assistant"), ("home assistant", "Home Assistant")):
        if key in low:
            return name, ""
    if "mozilla" in low:
        return "Browser", next((b for b in ("Firefox", "Edg", "OPR", "Chrome", "Safari") if b in ua), "").replace("Edg", "Edge")
    if low.startswith("dart/"):
        return "App (Dart/Flutter)", ""
    if low.startswith("python-requests") or low.startswith("python/") or "aiohttp" in low:
        return "Python script", ""
    return (ua.split("/")[0][:30] or "Unknown"), ""


_BRIDGE_KEY = web.AppKey("bridge", object)


@web.middleware
async def clients_middleware(request, handler):
    """Records who is using the bridge. Skips preflight and the theme assets."""
    if request.method != "OPTIONS" and not request.path.startswith(("/kx/ui/", "/websocket")):
        bridge = request.app.get(_BRIDGE_KEY)
        if bridge is not None:
            bridge._note_client(request.remote or "?", *_classify_client(request.headers.get("User-Agent", "")))
    return await handler(request)


# Host names accepted in the Host header. Guards against DNS rebinding: any
# website points its own domain at the bridge's IP and the browser starts
# treating the responses as "same origin". IPs, localhost and local network names
# always pass; other names (e.g. behind a reverse proxy) through KX_ALLOWED_HOSTS.
# ponytail: LAN suffix heuristic; becomes a setting if someone needs more.
_LAN_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain")
_EXTRA_HOSTS = {h.strip().lower() for h in os.environ.get("KX_ALLOWED_HOSTS", "").split(",") if h.strip()}


def _host_allowed(hostname: str) -> bool:
    h = (hostname or "").lower().rstrip(".")
    if not h or h == "localhost" or "." not in h or h.endswith(_LAN_SUFFIXES) or h in _EXTRA_HOSTS:
        return True
    try:
        ipaddress.ip_address(h.strip("[]"))
        return True
    except ValueError:
        return False


def _origin_trusted(request) -> bool:
    """No Origin = non-browser client (OrcaSlicer, Obico, HA) or
    same-origin navigation. With an Origin, only the SAME host (any port):
    the multi-printer UI calls the sibling instances on other ports."""
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    return urllib.parse.urlsplit(origin).hostname == (request.url.host or "")


@web.middleware
async def cors_middleware(request, handler):
    if not _host_allowed(request.url.host):
        return web.Response(status=403, text="Host not allowed (set KX_ALLOWED_HOSTS)")
    trusted = _origin_trusted(request)
    cors = {
        "Access-Control-Allow-Origin":  request.headers.get("Origin", ""),
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Api-Key",
        "Vary": "Origin",
    }
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=cors if trusted else {})
    # CSRF: an outside website must not change anything - not even with a multipart form,
    # which the browser sends without a CORS preflight.
    if not trusted and request.method not in ("GET", "HEAD"):
        return web.json_response({"error": "origin not allowed"}, status=403)
    resp = await handler(request)
    resp.headers.pop("Access-Control-Allow-Origin", None)
    if trusted and request.headers.get("Origin"):
        resp.headers.update(cors)
    return resp


def _load_auth_config(args) -> None:
    """Reads [auth] from config.ini into the args. Without the section (first start), creates the
    default login kx / kx123 with a mandatory password change on first access -
    the bridge never stays open on the network just because nobody configured anything.
    `enabled = 0` (turned off through the UI) keeps the login off."""
    import configparser
    finder = getattr(env_loader, "find_config_path", None)
    path = finder() if finder else pathlib.Path(_BASE) / "config" / "config.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    if path.is_file():
        cfg.read(path, encoding="utf-8")
    if not cfg.has_section("auth"):
        cfg.add_section("auth")
        cfg.set("auth", "user", auth.DEFAULT_USER)
        cfg.set("auth", "password", auth.hash_password(auth.DEFAULT_PASSWORD))
        cfg.set("auth", "must_change", "1")
        # OrcaSlicer needs a key to get past the login; the first-run guide shows it
        cfg.set("auth", "api_key", _new_camera_token())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("# MoonKobra configuration file\n\n")
                cfg.write(f)
            log.warning(f"Default login created ({auth.DEFAULT_USER} / {auth.DEFAULT_PASSWORD}) - "
                        "the password must be changed on first access")
        except OSError as e:
            log.error(f"Could not write the default login to {path}: {e}")
    if cfg.get("auth", "enabled", fallback="1").strip() == "0":
        args.auth_user = args.auth_password = args.auth_api_key = ""
        args.auth_must_change = False
        return
    args.auth_user = cfg.get("auth", "user", fallback="")
    args.auth_password = cfg.get("auth", "password", fallback="")
    args.auth_api_key = cfg.get("auth", "api_key", fallback="")
    args.auth_must_change = cfg.get("auth", "must_change", fallback="0").strip() == "1"
    args.auth_camera_token = cfg.get("auth", "camera_token", fallback="")
    if not args.auth_camera_token:
        args.auth_camera_token = _new_camera_token()
        cfg.set("auth", "camera_token", args.auth_camera_token)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("# MoonKobra configuration file\n\n")
                cfg.write(f)
        except OSError as e:
            log.error(f"Could not write the camera token to {path}: {e}")


def _new_camera_token() -> str:
    import secrets
    return secrets.token_urlsafe(18)


def build_app(bridge: KobraXBridge) -> web.Application:
    # Login barrier (optional). Without auth_user/auth_password in config.ini
    # `mw` is None and the bridge behaves exactly as before.
    _a = bridge._args
    _auth_user = (getattr(_a, "auth_user", "") or "").strip()
    _auth_pw   = (getattr(_a, "auth_password", "") or "").strip()
    _api_key   = (getattr(_a, "auth_api_key", "") or "").strip()
    _must_change = bool(getattr(_a, "auth_must_change", False))
    _mw = auth.make_auth_middleware(_auth_user, _auth_pw, _api_key, bridge._session_secret, _must_change,
                                    (getattr(_a, "auth_camera_token", "") or "").strip())

    app = web.Application(
        client_max_size=256 * 1024 * 1024,
        # Auth first: a 401 must not go through the CORS layer first.
        # Clients are recorded only AFTER login/CORS: whoever got a 401/403 does not show up.
        middlewares=[_mw, cors_middleware, clients_middleware] if _mw else [cors_middleware, clients_middleware],
    )
    app[_BRIDGE_KEY] = bridge
    r = app.router

    if _mw:
        _login_page, _login_post, _logout = auth.make_handlers(
            _auth_user, _auth_pw, bridge._session_secret,
            os.path.join(_WEB_BASE, "web", "themes", bridge._ui_theme, "login.html"),
            _must_change,
        )
        r.add_get(auth.LOGIN_PATH,  _login_page)
        r.add_post("/api/login",    _login_post)
        r.add_post("/api/logout",   _logout)
        r.add_post(auth.CHANGE_PATH, bridge.handle_api_auth_change)

    # API Moonraker
    r.add_get("/server/info",                bridge.handle_server_info)
    r.add_get("/printer/info",               bridge.handle_printer_info)
    r.add_get("/machine/system_info",        bridge.handle_machine_system_info)
    r.add_get("/printer/objects/list",       bridge.handle_objects_list)
    r.add_get("/printer/objects/query",      bridge.handle_objects_query)
    r.add_get("/printer/objects/subscribe",  bridge.handle_objects_subscribe)
    r.add_post("/printer/objects/subscribe", bridge.handle_objects_subscribe)
    r.add_get("/server/files/list",          bridge.handle_files_list)
    r.add_get("/server/files/metadata",      bridge.handle_files_metadata)
    r.add_post("/server/files/upload",       bridge.handle_file_upload)
    r.add_post("/printer/print/start",       bridge.handle_print_start)
    r.add_post("/printer/print/pause",       bridge.handle_print_pause)
    r.add_post("/printer/print/resume",      bridge.handle_print_resume)
    r.add_post("/printer/print/cancel",      bridge.handle_print_cancel)

    # Moonraker stubs for moonraker-obico
    r.add_get("/access/api_key",             bridge.handle_access_api_key)
    r.add_get("/machine/update/status",      bridge.handle_machine_update_status)
    r.add_get("/server/history/list",        bridge.handle_history_list)
    r.add_get("/server/webcams/list",        bridge.handle_webcams_list)
    r.add_get("/server/temperature_store",   bridge.handle_temperature_store)
    r.add_post("/printer/gcode/script",      bridge.handle_printer_gcode_script)

    # OctoPrint compatibility (OrcaSlicer probes this + uploads here)
    r.add_get("/api/version",                bridge.handle_octoprint_version)
    r.add_post("/api/files/local",           bridge.handle_file_upload)
    r.add_post("/api/files/{path:.*}",       bridge.handle_file_upload)

    # Moonraker database (AMS sync with OrcaSlicer)
    r.add_get("/server/database/item",       bridge.handle_moonraker_database)
    r.add_post("/server/database/item",      bridge.handle_moonraker_database_post)
    r.add_get("/server/database/list",       bridge.handle_database_list)

    # Endpoints novos da API
    r.add_post("/api/light",               bridge.handle_api_light)
    r.add_post("/api/fan",                 bridge.handle_api_fan)
    r.add_post("/api/connect",             bridge.handle_api_connect)
    r.add_post("/api/disconnect",          bridge.handle_api_disconnect)
    r.add_post("/api/restart",             bridge.handle_api_restart)
    r.add_get("/api/update",               bridge.handle_api_update)
    r.add_post("/api/speed",               bridge.handle_api_speed)
    r.add_post("/api/ams/feed",            bridge.handle_api_ams_feed)
    r.add_post("/api/ams/set_slot",        bridge.handle_api_ams_set_slot)
    r.add_post("/api/ace/auto_feed",        bridge.handle_api_ace_auto_feed)
    r.add_post("/api/ace/dry",             bridge.handle_api_ace_dry)
    r.add_post("/api/axis",                bridge.handle_api_axis)
    r.add_post("/api/temperature",         bridge.handle_api_temperature)
    r.add_get("/api/camera",               bridge.handle_api_camera)
    r.add_get("/api/camera/stream",        bridge.handle_camera_stream)
    r.add_get("/api/camera/h264",          bridge.handle_camera_h264)
    r.add_get("/api/camera/snapshot",      bridge.handle_api_camera_snapshot)
    r.add_get("/webcam/",                  bridge.handle_webcam_compat)
    r.add_get("/webcam",                   bridge.handle_webcam_compat)
    r.add_post("/api/auth/camera-token",   bridge.handle_api_camera_token)
    r.add_post("/api/camera/start",        bridge.handle_api_camera_start)
    r.add_post("/api/camera/stop",         bridge.handle_api_camera_stop)
    r.add_post("/api/camera/reset",        bridge.handle_api_camera_reset)
    r.add_get("/api/state",                bridge.handle_api_state)
    r.add_get("/api/settings",             bridge.handle_api_settings_get)
    r.add_post("/api/settings",            bridge.handle_api_settings_post)
    r.add_post("/api/auth",                bridge.handle_api_auth_post)
    r.add_post("/api/file_ready/clear",    bridge.handle_api_file_ready_clear)
    r.add_get("/api/log/stream",           bridge.handle_api_log_stream)
    r.add_get("/api/log/download",         bridge.handle_api_log_download)
    r.add_get("/serve/{filename}",         bridge.handle_serve_file)
    # /kx/ G-code store + history + filament
    r.add_get("/kx/printers",              bridge.handle_kx_printers)
    r.add_post("/kx/printers/add",         bridge.handle_kx_printer_add)
    r.add_delete("/kx/printers/{pid}",     bridge.handle_kx_printer_remove)
    r.add_post("/kx/printers/{pid}/power",        bridge.handle_kx_printer_power)
    r.add_get("/kx/printers/{pid}/power-status",  bridge.handle_kx_printer_power_status)
    r.add_post("/kx/print",               bridge.handle_kx_print)
    r.add_get("/kx/files",                bridge.handle_kx_files)
    r.add_delete("/kx/files/{file_id}",    bridge.handle_kx_file_delete)
    r.add_get("/kx/files/{file_id}/download", bridge.handle_kx_file_download)
    r.add_post("/kx/files/{file_id}/verify", bridge.handle_kx_file_verify)
    r.add_get("/kx/printer-files",         bridge.handle_kx_printer_files)
    r.add_post("/kx/printer-files/delete", bridge.handle_kx_printer_file_delete)
    r.add_get("/kx/printer-files/{filename}/thumbnail", bridge.handle_kx_printer_file_thumbnail)
    r.add_get("/kx/filament/slots",        bridge.handle_kx_filament_slots)
    r.add_get("/kx/filament/profiles",     bridge.handle_kx_filament_profiles)
    r.add_post("/kx/filament/slots/{idx}/profile", bridge.handle_kx_filament_slot_profile)
    r.add_get("/kx/filament/visible_vendors",  bridge.handle_kx_visible_vendors)
    r.add_post("/kx/filament/visible_vendors", bridge.handle_kx_visible_vendors)
    # Custom profile import (Issue #41) - the user uploads their own Orca filament
    # profiles as ZIP/JSON (e.g. from ~/.config/OrcaSlicer/user/<id>/filament/),
    # because the bridge usually does not run on the same host as OrcaSlicer.
    r.add_get("/kx/filament/profiles/user",    bridge.handle_kx_filament_profiles_user_list)
    r.add_post("/kx/filament/profiles/user",   bridge.handle_kx_filament_profiles_import)
    r.add_delete("/kx/filament/profiles/user", bridge.handle_kx_filament_profiles_user_delete)
    r.add_get("/kx/history",               bridge.handle_kx_history)
    r.add_get("/kx/history/{id}/log",      bridge.handle_kx_history_log)
    r.add_get("/kx/backup/export",         bridge.handle_backup_export)
    r.add_post("/kx/backup/import",        bridge.handle_backup_import)
    r.add_get("/kx/pricing/config",        bridge.handle_pricing_config_get)
    r.add_post("/kx/pricing/config",       bridge.handle_pricing_config_set)
    r.add_post("/kx/pricing/calc",         bridge.handle_pricing_calc)
    r.add_get("/kx/pricing/quotes",        bridge.handle_pricing_quotes)
    r.add_post("/kx/pricing/quotes",       bridge.handle_pricing_quote_save)
    r.add_get("/kx/pricing/quotes/{id}",   bridge.handle_pricing_quote_get)
    r.add_post("/kx/pricing/quotes/{id}/delete", bridge.handle_pricing_quote_delete)
    r.add_get("/kx/ui/{name:.*}",          bridge.handle_kx_ui_asset)
    r.add_get("/kx/files/{id}/objects",    bridge.handle_kx_file_objects)
    r.add_post("/kx/skip",                 bridge.handle_kx_skip)
    r.add_post("/kx/skip/query",           bridge.handle_kx_skip_query)
    r.add_get("/kx/skip/state",            bridge.handle_kx_skip_state)
    r.add_get("/kx/spoolman/status",       bridge.handle_kx_spoolman_status)
    r.add_get("/kx/spoolman/spools",       bridge.handle_kx_spoolman_spools)
    r.add_post("/kx/spoolman/active-spool", bridge.handle_kx_spoolman_set_active)
    r.add_route("OPTIONS", "/kx/{path:.*}", bridge.handle_kx_options)

    # Root + printer routes (single page, the JS reads the pathname)
    r.add_get("/",                           bridge.handle_index)
    r.add_get(r"/printer{num:\d+}",          bridge.handle_index)
    r.add_get("/favicon.ico",               bridge.handle_favicon)

    # WebSocket
    r.add_get("/websocket",                  bridge.handle_websocket)

    # Catch-all: logs every unknown request instead of a 404
    r.add_route("*", "/{path:.*}",           bridge.handle_catchall)

    return app


def _build_per_printer_args(base_args, p: dict):
    """Copies the CLI args, overriding them with the printer's entry in config.ini."""
    import copy
    a = copy.copy(base_args)
    a.printer_ip = p.get("printer_ip") or base_args.printer_ip
    a.mqtt_port  = int(p.get("mqtt_port") or base_args.mqtt_port)
    a.username   = p.get("username")  or base_args.username
    a.password   = p.get("password")  or base_args.password
    a.mode_id    = p.get("mode_id")   or base_args.mode_id
    a.device_id  = p.get("device_id") or base_args.device_id
    a.port       = int(p.get("http_port") or base_args.port)
    a.power_on_url     = p.get("power_on_url")     or getattr(base_args, "power_on_url", "")     or ""
    a.power_off_url    = p.get("power_off_url")    or getattr(base_args, "power_off_url", "")    or ""
    a.power_status_url = p.get("power_status_url") or getattr(base_args, "power_status_url", "") or ""
    a.power_status_inverted = int(p.get("power_status_inverted") or getattr(base_args, "power_status_inverted", 0) or 0)
    return a


async def run_bridge(args):
    _set_verbose_http_log(bool(getattr(args, "verbose_http_log", 0)))
    printers = env_loader.list_printers()
    multi_mode = bool(printers)
    if not printers:
        printers = [{
            "id":         "1",
            "name":       getattr(args, "printer_name", None) or "Anycubic Kobra X",
            "printer_ip": args.printer_ip,
            "mqtt_port":  args.mqtt_port,
            "username":   args.username,
            "password":   args.password,
            "mode_id":    args.mode_id,
            "device_id":  args.device_id,
            "http_port":  args.port,
        }]

    store = GCodeStore(args.data_dir)
    _finder = getattr(env_loader, "find_config_path", None)
    cert_dir = str((_finder() if _finder else pathlib.Path(_BASE) / "config" / "config.ini").parent / "certs")
    global _running_bridges
    all_bridges: dict = {}
    _running_bridges = all_bridges
    runners = []
    stop_event = threading.Event()
    loop = asyncio.get_event_loop()

    for idx, p in enumerate(printers):
        pid = str(p.get("id") or (idx + 1))
        per_args = _build_per_printer_args(args, p)
        # Default port convention: 7125 + (id-1) when no http_port is set
        if not p.get("http_port") and multi_mode:
            try:
                per_args.port = 7125 + (int(pid) - 1)
            except ValueError:
                per_args.port = 7125 + idx

        client = KobraXClient(
            host=per_args.printer_ip,
            port=per_args.mqtt_port,
            username=per_args.username,
            password=per_args.password,
            mode_id=per_args.mode_id,
            device_id=per_args.device_id,
            client_id=f"kobrax_bridge_{pid}",
            cert_provider=_printer_cert_provider(per_args.printer_ip, per_args.device_id, cert_dir),
        )
        bridge = KobraXBridge(
            client, args=per_args, store=store,
            printer_id=pid, all_bridges=all_bridges,
        )
        # Adopt printer_name from config.ini if it is set
        if p.get("name"):
            bridge._state["printer_name"] = p["name"]
            bridge._name_locked = True
        all_bridges[pid] = bridge

        log.info(f"[Printer {pid}] Connecting to {per_args.printer_ip}:{per_args.mqtt_port}...")
        try:
            await loop.run_in_executor(None, client.connect)
            log.info(f"[Printer {pid}] MQTT connected")
        except Exception as e:
            err = _mqtt_error_msg(e)
            log.warning(f"[Printer {pid}] Connection failed: {err} - offline mode")
            bridge._state["print_state"] = "error"
            bridge._state["kobra_state"] = "offline"
            bridge._state["connection_error"] = err

        threading.Thread(
            target=bridge._poll_loop, args=(stop_event,),
            daemon=True, name=f"poll-{pid}",
        ).start()

        app = build_app(bridge)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, args.host, per_args.port)
        await site.start()
        runners.append((runner, client, pid))

    import socket as _socket
    _in_docker = os.path.exists("/.dockerenv")
    _host_ip_override = env_loader.BRIDGE_HOST_IP.strip()
    if _host_ip_override:
        _local_ip = _host_ip_override
    else:
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM) as _s:
                _s.connect(("8.8.8.8", 80))
                _local_ip = _s.getsockname()[0]
        except Exception:
            _local_ip = args.host
    # Propagate to every bridge instance - used in the absolute webcam URLs
    for _b in all_bridges.values():
        _b._local_ip = _local_ip
    ports = ", ".join(str(getattr(b._args, 'port', 0)) for b in all_bridges.values())
    if _in_docker and not _host_ip_override:
        # In a container the UDP trick only gives Docker's internal IP - it does not show
        log.info(f"OrcaSlicer → Moonraker → http://<this Docker host's IP>:{ports}")
        log.info("Running in Docker — set BRIDGE_HOST_IP to show the exact address")
    else:
        log.info(f"OrcaSlicer → Klipper → http://{_local_ip}:{ports}")
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        log.info("MoonKobra runs in the tray (next to the clock): right-click the icon to quit")
    else:
        log.info("Press Ctrl-C to stop")

    # Windows .exe: double-click should land on the dashboard (not again after a restart)
    if sys.platform == "win32" and getattr(sys, "frozen", False) and not os.environ.get("MOONKOBRA_NO_BROWSER"):
        import webbrowser
        webbrowser.open(f"http://localhost:{args.port}")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop_event.set()
        for runner, client, pid in runners:
            try:
                await runner.cleanup()
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception:
                pass
        log.info("Bridge stopped")


def _default_data_dir() -> str:
    """Persistence directory: Docker sets KX_DATA_DIR, the binary uses <exe-dir>/data,
    the dev script uses <repo>/data (or /app/data if it exists)."""
    if os.environ.get("KX_DATA_DIR"):
        return os.environ["KX_DATA_DIR"]
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "data")
    if os.path.isdir("/app"):
        return "/app/data"
    return os.path.normpath(os.path.join(_BASE, "..", "data"))


def main():
    parser = argparse.ArgumentParser(description="Moonraker bridge for the Anycubic Kobra X")
    parser.add_argument("--printer-ip",  default=env_loader.PRINTER_IP,
                        help="IP-Adresse des Druckers")
    parser.add_argument("--mqtt-port",   type=int, default=env_loader.MQTT_PORT)
    parser.add_argument("--username",    default=env_loader.USERNAME)
    parser.add_argument("--password",    default=env_loader.PASSWORD)
    parser.add_argument("--mode-id",     default=env_loader.MODE_ID)
    parser.add_argument("--device-id",       default=env_loader.DEVICE_ID)
    parser.add_argument("--power-on-url",     default=env_loader.POWER_ON_URL,
                        help="HTTP GET URL to power the printer on (e.g. a Tasmota smart plug)")
    parser.add_argument("--power-off-url",    default=env_loader.POWER_OFF_URL,
                        help="HTTP GET URL to power the printer off")
    parser.add_argument("--power-status-url", default=env_loader.POWER_STATUS_URL,
                        help="HTTP GET URL returning the smart plug's current on/off state")
    parser.add_argument("--power-status-inverted", type=int, default=env_loader.POWER_STATUS_INVERTED,
                        help="Shows the power switch's current state instead of the button action")
    parser.add_argument("--default-ams-slot",default=env_loader.DEFAULT_AMS_SLOT)
    parser.add_argument("--auto-leveling",           type=int, default=env_loader.AUTO_LEVELING)
    parser.add_argument("--vibration-compensation",  type=int, default=env_loader.VIBRATION_COMPENSATION)
    parser.add_argument("--camera-on-print",         type=int, default=env_loader.CAMERA_ON_PRINT)
    parser.add_argument("--web-upload-warning", type=int, default=env_loader.WEB_UPLOAD_WARNING)
    parser.add_argument("--delete-printer-file-after-print", type=int,
                        default=env_loader.DELETE_PRINTER_FILE_AFTER_PRINT,
                        help="After a successful print, delete the file from the printer's "
                             "own storage if it's also in the bridge's own GCode store")
    parser.add_argument("--print-start-dialog", dest="print_start_dialog", type=int, default=env_loader.PRINT_START_DIALOG)
    parser.add_argument("--file-ready-dialog",  dest="print_start_dialog", type=int)
    parser.add_argument("--spoolman-server",    default=env_loader.SPOOLMAN_SERVER,
                        help="Spoolman URL (e.g. http://192.168.x.x:7912); leave empty to disable")
    parser.add_argument("--spoolman-sync-rate", type=int, default=env_loader.SPOOLMAN_SYNC_RATE,
                        help="Mid-print filament sync interval in seconds (0 = only on print end)")
    for _k in LOG_LIMITS:
        parser.add_argument("--" + _k.replace("_", "-"), dest=_k, type=int,
                            default=getattr(env_loader, _k.upper(), LOG_LIMITS[_k][2]))
    parser.add_argument("--poll-interval", type=int, default=env_loader.POLL_INTERVAL,
                        help="Printer poll interval in seconds")
    parser.add_argument("--verbose-http-log", type=int, default=env_loader.VERBOSE_HTTP_LOG,
                        help="Log every HTTP request (aiohttp access log)")
    parser.add_argument("--notify-url", default=env_loader.NOTIFY_URL,
                        help="ntfy / Telegram / Discord / webhook URL for print notifications")
    parser.add_argument("--notify-lang", default=env_loader.NOTIFY_LANG,
                        help="Language of the notification texts (UI translation code)")

    parser.add_argument("--auth-user",     default=env_loader.AUTH_USER,
                        help="Username for the web login; empty disables the login entirely")
    parser.add_argument("--auth-password", default=env_loader.AUTH_PASSWORD,
                        help="Password for the web login - a scrypt hash from "
                             "'python3 auth.py <password>', or plain text for a quick start")
    parser.add_argument("--auth-api-key",  default=env_loader.AUTH_API_KEY,
                        help="API key for OrcaSlicer/moonraker-obico (X-Api-Key header), "
                             "so they get through the login")

    parser.add_argument("--host",            default="0.0.0.0",
                        help="Bind address for the bridge server")
    parser.add_argument("--port",        type=int, default=7125,
                        help="HTTP/WS-Port (Moonraker-Standard: 7125)")
    parser.add_argument("--data-dir",    default=_default_data_dir(),
                        help="Persistence directory for the GCode store and DB")
    parser.add_argument(
        "--ui-theme",
        default=os.environ.get("KX_UI_THEME", "default"),
        metavar="NAME",
        help="Web-UI-Theme (Ordner web/themes/NAME/, Standard: default). "
        "Alternativ: Umgebungsvariable KX_UI_THEME.",
    )
    args = parser.parse_args()
    _load_auth_config(args)
    if args.printer_ip and ":" in args.printer_ip:
        args.printer_ip = args.printer_ip.split(":")[0]

    # Windows needs the ProactorEventLoop for asyncio.create_subprocess_exec
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        _kill_children_with_us()

    if not (sys.platform == "win32" and getattr(sys, "frozen", False)):
        asyncio.run(run_bridge(args))
        return

    _run_windows_tray(args)


def _run_windows_tray(args):
    """Windows .exe (built without a console): the bridge runs in a thread and the
    tray icon is the program - a click opens the dashboard, right-click -> Quit stops it.
    A green/red sphere on the icon shows whether the printers are connected."""
    import ctypes
    import webbrowser
    import pystray
    from PIL import Image
    global _tray

    # No console: the log goes to a file next to the .exe (the dashboard also shows it)
    fh = logging.FileHandler(os.path.join(_BASE, "moonkobra.log"), mode="w", encoding="utf-8")
    fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-5s %(name)s: %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(fh)

    url = f"http://localhost:{args.port}"
    # Second double-click while it is already running: just show the dashboard.
    # Not on a self-restart: the old process may still hold the port for a moment.
    if not os.environ.get("MOONKOBRA_NO_BROWSER"):
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", args.port)) == 0:
                webbrowser.open(url)
                return

    pt = (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x16  # LANG_PORTUGUESE
    t = (lambda p, e: p) if pt else (lambda p, e: e)

    base = Image.open(os.path.join(_WEB_BASE, "web", "themes", "default", "lib", "icon", "favicon-64.png")).convert("RGBA")

    def icon_with_dot(color):
        # Moko + a small status sphere in the bottom-right corner
        from PIL import ImageDraw
        img = base.copy()
        ImageDraw.Draw(img).ellipse((38, 38, 62, 62), fill=color, outline=(20, 20, 20, 255), width=3)
        return img

    icons = {True: icon_with_dot((46, 204, 64, 255)), False: icon_with_dot((231, 60, 50, 255))}

    def watch_printers():
        last = None
        while True:
            clients = [b.client for b in list(_running_bridges.values())]
            ok = sum(1 for c in clients if c.is_connected())
            state = (bool(clients) and ok == len(clients), ok, len(clients))
            if state != last:
                last = state
                _tray.icon = icons[state[0]]
                _tray.title = (f"MoonKobra - {url}\n"
                               + (t(f"{ok}/{len(clients)} impressora(s) conectada(s)", f"{ok}/{len(clients)} printer(s) connected")
                                  if clients else t("Nenhuma impressora configurada", "No printer configured")))
            time.sleep(3)

    def bridge():
        try:
            asyncio.run(run_bridge(args))
        except Exception as e:
            log.exception("MoonKobra stopped with an error")
            ctypes.windll.user32.MessageBoxW(
                None,
                t(f"O MoonKobra parou com um erro:\n\n{e}\n\nDetalhes em moonkobra.log, na pasta do programa.",
                  f"MoonKobra stopped with an error:\n\n{e}\n\nDetails in moonkobra.log, in the program folder."),
                "MoonKobra", 0x10)  # MB_ICONERROR
        _tray.stop()

    def quit_(icon):
        icon.visible = False
        os._exit(0)  # same as closing the old console window; the job object takes ffmpeg along

    _tray = pystray.Icon(
        "MoonKobra",
        icons[False],
        f"MoonKobra - {url}",
        menu=pystray.Menu(
            pystray.MenuItem(t("Abrir painel", "Open dashboard"), lambda: webbrowser.open(url), default=True),
            pystray.MenuItem(t("Abrir pasta do MoonKobra", "Open MoonKobra folder"), lambda: os.startfile(_BASE)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(t("Sair", "Quit"), quit_),
        ),
    )
    threading.Thread(target=bridge, daemon=True).start()
    _tray.run(setup=lambda icon: (setattr(icon, "visible", True),
                                  threading.Thread(target=watch_printers, daemon=True).start()))
    os._exit(0)


def _kill_children_with_us():
    """Windows: puts this process in a Job Object that kills every child (ffmpeg)
    as soon as we die - window closed, crash or Task Manager - so nothing keeps
    running in the background. The self-restart breaks away (CREATE_BREAKAWAY_FROM_JOB)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    KILL_ON_JOB_CLOSE, BREAKAWAY_OK, EXTENDED_LIMIT_INFO = 0x2000, 0x800, 9
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = _Extended()
    info.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    ok = job and k32.SetInformationJobObject(wintypes.HANDLE(job), EXTENDED_LIMIT_INFO,
                                             ctypes.byref(info), ctypes.sizeof(info))
    # GetCurrentProcess() is the pseudo-handle -1: as a bare int ctypes overflows on it
    if not ok or not k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(k32.GetCurrentProcess())):
        log.warning("Could not create the Windows job object - ffmpeg may outlive the bridge")
    # ponytail: the handle stays open on purpose; Windows closes it (and kills the job) when we exit


if __name__ == "__main__":
    main()
