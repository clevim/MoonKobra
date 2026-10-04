"""
env_loader.py - loads the connection parameters from .env (repo root or working directory).
Environment variables take precedence over the values in .env.
"""
import os
import sys
import pathlib

_BASE = pathlib.Path(sys.executable).parent if getattr(sys, "frozen", False) else pathlib.Path(__file__).parent


def _find_env_file() -> pathlib.Path | None:
    for base in (_BASE, _BASE.parent):
        p = base / ".env"
        if p.is_file():
            return p
    return None


def _load_env_file(path: pathlib.Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if key and key not in os.environ:
                os.environ[key] = val


_env_path = _find_env_file()
if _env_path:
    _load_env_file(_env_path)


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# Frequently used shortcuts
PRINTER_IP       = get("PRINTER_IP",       "")
MQTT_PORT        = int(get("MQTT_PORT", "9883"))
USERNAME         = get("MQTT_USERNAME",    "")
PASSWORD         = get("MQTT_PASSWORD",    "")
MODE_ID          = get("MODE_ID",          "")
DEVICE_ID        = get("DEVICE_ID",        "")
POWER_ON_URL     = get("POWER_ON_URL",     "")
POWER_OFF_URL    = get("POWER_OFF_URL",    "")
POWER_STATUS_URL = get("POWER_STATUS_URL", "")
DEFAULT_AMS_SLOT = get("DEFAULT_AMS_SLOT", "auto")
AUTO_LEVELING            = int(get("AUTO_LEVELING", "1"))
VIBRATION_COMPENSATION   = int(get("VIBRATION_COMPENSATION", "0"))
CAMERA_ON_PRINT          = int(get("CAMERA_ON_PRINT", "0"))
WEB_UPLOAD_WARNING = int(get("WEB_UPLOAD_WARNING", "1"))
DELETE_PRINTER_FILE_AFTER_PRINT = int(get("DELETE_PRINTER_FILE_AFTER_PRINT", "0"))
PRINT_START_DIALOG = int(get("PRINT_START_DIALOG", get("FILE_READY_DIALOG", "1")))
BRIDGE_HOST_IP   = get("BRIDGE_HOST_IP", "")
AUTH_USER        = get("AUTH_USER",     "")
AUTH_PASSWORD    = get("AUTH_PASSWORD", "")
AUTH_API_KEY     = get("AUTH_API_KEY",  "")
