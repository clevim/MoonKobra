"""
config_loader.py - loads the connection parameters from config/config.ini (primary)
or from .env (fallback / migration).
Environment variables always take precedence.
"""
import os
import sys
import pathlib
import configparser
import logging
from typing import Optional

log = logging.getLogger("kobrax.config")

_BASE = pathlib.Path(sys.executable).parent if getattr(sys, "frozen", False) else pathlib.Path(__file__).parent

CONFIG_SECTION_CONNECTION = "connection"
CONFIG_SECTION_PRINT      = "print"
CONFIG_SECTION_BRIDGE     = "bridge"
CONFIG_SECTION_SPOOLMAN   = "spoolman"
CONFIG_SECTION_AUTH       = "auth"


def _find_config_file() -> pathlib.Path | None:
    for base in (_BASE, _BASE.parent):
        p = base / "config" / "config.ini"
        if p.is_file():
            return p
    return None


def _find_env_file() -> pathlib.Path | None:
    for base in (_BASE, _BASE.parent):
        p = base / ".env"
        if p.is_file():
            return p
    return None


def _load_env_file(path: pathlib.Path):
    """Loads the .env file as a fallback - only sets keys not yet in os.environ."""
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


# Single source of truth for the env-var <-> config.ini mapping. _restart_bridge()
# in kobrax_moonraker_bridge.py removes exactly these keys from os.environ
# before restarting, so a value removed here or when saving the settings
# in the UI never survives as a stale env var read by the new process. Add
# new settings ONLY here - there is no second list to keep in sync.
CONFIG_ENV_MAPPING = {
    "PRINTER_IP":       (CONFIG_SECTION_CONNECTION, "printer_ip"),
    "MQTT_PORT":        (CONFIG_SECTION_CONNECTION, "mqtt_port"),
    "MQTT_USERNAME":    (CONFIG_SECTION_CONNECTION, "username"),
    "MQTT_PASSWORD":    (CONFIG_SECTION_CONNECTION, "password"),
    "MODE_ID":          (CONFIG_SECTION_CONNECTION, "mode_id"),
    "DEVICE_ID":        (CONFIG_SECTION_CONNECTION, "device_id"),
    "POWER_ON_URL":     (CONFIG_SECTION_CONNECTION, "power_on_url"),
    "POWER_OFF_URL":    (CONFIG_SECTION_CONNECTION, "power_off_url"),
    "POWER_STATUS_URL": (CONFIG_SECTION_CONNECTION, "power_status_url"),
    "POWER_STATUS_INVERTED": (CONFIG_SECTION_CONNECTION, "power_status_inverted"),
    "DEFAULT_AMS_SLOT":     (CONFIG_SECTION_PRINT,   "default_ams_slot"),
    "AUTO_LEVELING":            (CONFIG_SECTION_PRINT,   "auto_leveling"),
    "VIBRATION_COMPENSATION":   (CONFIG_SECTION_PRINT,   "vibration_compensation"),
    "CAMERA_ON_PRINT":          (CONFIG_SECTION_PRINT,   "camera_on_print"),
    "WEB_UPLOAD_WARNING":   (CONFIG_SECTION_PRINT,   "web_upload_warning"),
    "DELETE_PRINTER_FILE_AFTER_PRINT": (CONFIG_SECTION_PRINT, "delete_printer_file_after_print"),
    "PRINT_START_DIALOG":   (CONFIG_SECTION_PRINT,   "print_start_dialog"),
    "BRIDGE_PRINTER_NAME":  (CONFIG_SECTION_BRIDGE,  "printer_name"),
    "BRIDGE_HOST_IP":       (CONFIG_SECTION_BRIDGE,  "host_ip"),
    "POLL_INTERVAL":        (CONFIG_SECTION_BRIDGE,  "poll_interval"),
    "JOB_LOG_KEEP":          (CONFIG_SECTION_BRIDGE,  "job_log_keep"),
    "JOB_LOG_CONTEXT_LINES": (CONFIG_SECTION_BRIDGE,  "job_log_context_lines"),
    "LOG_BUFFER_LINES":      (CONFIG_SECTION_BRIDGE,  "log_buffer_lines"),
    "VERBOSE_HTTP_LOG":     (CONFIG_SECTION_BRIDGE,  "verbose_http_log"),
    "NOTIFY_URL":           (CONFIG_SECTION_BRIDGE,  "notify_url"),
    "NOTIFY_LANG":          (CONFIG_SECTION_BRIDGE,  "notify_lang"),
    "SPOOLMAN_SERVER":      (CONFIG_SECTION_SPOOLMAN, "server"),
    "SPOOLMAN_SYNC_RATE":   (CONFIG_SECTION_SPOOLMAN, "sync_rate"),
    "AUTH_USER":            (CONFIG_SECTION_AUTH,     "user"),
    "AUTH_PASSWORD":        (CONFIG_SECTION_AUTH,     "password"),
    "AUTH_API_KEY":         (CONFIG_SECTION_AUTH,     "api_key"),
}


def _load_config_file(path: pathlib.Path):
    """Loads config.ini and sets the keys in os.environ (only if not set yet)."""
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path, encoding="utf-8")

    for env_key, (section, option) in CONFIG_ENV_MAPPING.items():
        if env_key not in os.environ:
            try:
                val = cfg.get(section, option)
                if val:
                    os.environ[env_key] = val
            except (configparser.NoSectionError, configparser.NoOptionError):
                pass


    # Compatibilidade retroativa: chave antiga FILE_READY_DIALOG → PRINT_START_DIALOG
    if "PRINT_START_DIALOG" not in os.environ:
        try:
            legacy = cfg.get(CONFIG_SECTION_PRINT, "file_ready_dialog")
            if legacy:
                os.environ["PRINT_START_DIALOG"] = legacy
        except (configparser.NoSectionError, configparser.NoOptionError):
            pass
    if "PRINT_START_DIALOG" not in os.environ and "FILE_READY_DIALOG" in os.environ:
        os.environ["PRINT_START_DIALOG"] = os.environ["FILE_READY_DIALOG"]


def migrate_env_to_config(env_path: pathlib.Path, config_path: pathlib.Path):
    """One-time migration: creates config.ini from .env."""
    env_vals: dict[str, str] = {}
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env_vals[k.strip()] = v.strip()

    cfg = configparser.ConfigParser(interpolation=None)
    cfg[CONFIG_SECTION_CONNECTION] = {
        "printer_ip": env_vals.get("PRINTER_IP", ""),
        "mqtt_port":  env_vals.get("MQTT_PORT",  "9883"),
        "username":   env_vals.get("MQTT_USERNAME", ""),
        "password":   env_vals.get("MQTT_PASSWORD", ""),
        "mode_id":    env_vals.get("MODE_ID",    ""),
        "device_id":  env_vals.get("DEVICE_ID",  ""),
    }
    cfg[CONFIG_SECTION_PRINT] = {
        "default_ams_slot": env_vals.get("DEFAULT_AMS_SLOT", "auto"),
        "auto_leveling":          env_vals.get("AUTO_LEVELING",           "1"),
        "vibration_compensation": env_vals.get("VIBRATION_COMPENSATION",  "0"),
        "camera_on_print":        env_vals.get("CAMERA_ON_PRINT",         "0"),
        "web_upload_warning": env_vals.get("WEB_UPLOAD_WARNING", "1"),
    }
    cfg[CONFIG_SECTION_BRIDGE] = {
        "poll_interval": "3",
    }
    # This runs at module import (see the "Load" section below) - an
    # unhandled mkdir/write failure (e.g. a read-only filesystem) would take
    # the whole bridge down at startup with a raw traceback. Log a clear
    # diagnostic before re-raising, so the real cause (permissions, full
    # disk) is visible instead of a stack trace pointing at configparser.
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write("# MoonKobra configuration file\n")
            f.write("# Automatically migrated from .env\n\n")
            cfg.write(f)
    except OSError as e:
        log.error("Failed to write the migrated config.ini to %s: %s", config_path, e)
        raise


def find_config_path() -> pathlib.Path:
    """Returns the config.ini path (even if it does not exist yet)."""
    for base in (_BASE, _BASE.parent):
        config_dir = base / "config"
        if config_dir.is_dir():
            return config_dir / "config.ini"
    return _BASE / "config" / "config.ini"


# ─── Load ────────────────────────────────────────────────────────────────────

_config_path = _find_config_file()
_env_path    = _find_env_file()

if _config_path:
    _load_config_file(_config_path)
elif _env_path:
    # No config.ini -> migrate from .env
    _target = find_config_path()
    migrate_env_to_config(_env_path, _target)
    _load_config_file(_target)
    _config_path = _target


def list_printers() -> list[dict]:
    """Reads every [printer_N] section of config.ini.

    Each section may contain the following keys:
      name, printer_ip, mqtt_port, username, password, mode_id, device_id,
      bridge_url, default_ams_slot, auto_leveling

    Returns an empty list when there are no [printer_N] sections
    (single-printer operation through [connection]).
    """
    path = _find_config_file()
    if not path:
        return []
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path, encoding="utf-8")
    printers: list[dict] = []
    idx = 1
    while True:
        section = f"printer_{idx}"
        if not cfg.has_section(section):
            break
        p = dict(cfg[section])
        p.setdefault("id", str(idx))
        if "mqtt_port" in p:
            try:
                p["mqtt_port"] = int(p["mqtt_port"])
            except ValueError:
                p["mqtt_port"] = 9883
        printers.append(p)
        idx += 1
    return printers


def _filament_section(printer_id: Optional[str] = None) -> str:
    """Name of the section holding one printer's filament profile mapping.

    Multi-printer (one bridge, N printers): each printer has its own
    ``[filament_profiles_<id>]`` section so mappings do not overwrite each other.
    ``printer_id is None`` (single printer / legacy callers) maps to the
    original global ``[filament_profiles]`` section — fully backward compatible.
    """
    pid = str(printer_id).strip() if printer_id is not None else ""
    if pid and pid != "0":
        return f"filament_profiles_{pid}"
    return "filament_profiles"


def list_filament_profiles(printer_id: Optional[str] = None) -> dict[int, dict]:
    """Reads the [filament_profiles] section of config.ini.

    With ``printer_id`` set, reads the per-printer section ``[filament_profiles_<id>]``
    and falls back to the legacy global ``[filament_profiles]`` section while
    that printer has no section of its own.

    Format per AMS slot - the main key is (vendor, name); the `id` is
    looked up in orca_filaments.json on save and carried along
    (as a hint for OrcaSlicer; in Orca's data model ~136 profiles share
    the same filament_id, such as 'OGFL99', so the ID is not unique):

      [filament_profiles]
      slot_0_vendor = Polymaker
      slot_0_name   = PolyTerra PLA
      slot_0_id     = OGFL01

    Returns a dict {slot_index: {"id": ..., "vendor": ..., "name": ...}}.
    Empty/missing slots are NOT included - the default mapping
    (by filament_type) in the bridge stays active.

    Backward compatibility: old configs with only (vendor, id) stay readable; `name`
    is missing and the caller may optionally rebuild it from
    orca_filaments.json.
    """
    path = _find_config_file()
    if not path:
        return {}
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path, encoding="utf-8")
    section = _filament_section(printer_id)
    if not cfg.has_section(section):
        section = "filament_profiles"          # fallback: legacy global section
    if not cfg.has_section(section):
        return {}
    result: dict[int, dict] = {}
    for key, value in cfg.items(section):
        # Expects: slot_<idx>_id or slot_<idx>_vendor or slot_<idx>_name
        if not key.startswith("slot_"):
            continue
        parts = key.split("_", 2)
        if len(parts) < 3:
            continue
        try:
            slot_idx = int(parts[1])
        except ValueError:
            continue
        field = parts[2]
        if field not in ("id", "vendor", "name"):
            continue
        if not value.strip():
            continue
        result.setdefault(slot_idx, {})[field] = value.strip()
    return result


def save_filament_profiles(profiles: dict[int, dict], printer_id: Optional[str] = None) -> bool:
    """Writes the given slot profiles to the [filament_profiles] section
    of config.ini. Existing entries are fully replaced.

    profiles: {slot_index: {"id": "OGFL01", "vendor": "Polymaker", "name": "PolyTerra PLA"}}
    At least vendor+name must be set; id is optional (a hint).

    With ``printer_id`` set, writes only the per-printer section ``[filament_profiles_<id>]``
    — other printers and the legacy global section are left alone.
    """
    path = _find_config_file()
    if not path:
        return False
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path, encoding="utf-8")
    section = _filament_section(printer_id)
    # visible_vendors (Issue #41) is not a slot mapping - keep it when
    # replacing the section, otherwise the vendor filter is lost when saving the slots.
    # The first save of a per-printer section inherits the legacy global filter.
    preserved_vendors = None
    if cfg.has_option(section, "visible_vendors"):
        preserved_vendors = cfg.get(section, "visible_vendors")
    elif cfg.has_option("filament_profiles", "visible_vendors"):
        preserved_vendors = cfg.get("filament_profiles", "visible_vendors")
    if cfg.has_section(section):
        cfg.remove_section(section)
    if profiles or preserved_vendors:
        cfg[section] = {}
        if preserved_vendors:
            cfg[section]["visible_vendors"] = preserved_vendors
        for slot_idx in sorted(profiles.keys()):
            entry = profiles[slot_idx] or {}
            if entry.get("vendor"):
                cfg[section][f"slot_{slot_idx}_vendor"] = entry["vendor"]
            if entry.get("name"):
                cfg[section][f"slot_{slot_idx}_name"] = entry["name"]
            if entry.get("id"):
                cfg[section][f"slot_{slot_idx}_id"] = entry["id"]
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)
    return True


def list_visible_vendors(printer_id: Optional[str] = None) -> list[str]:
    """Reads [filament_profiles] visible_vendors (comma separated) from config.ini.

    Vendor visibility filter for the slot profile dropdown (Issue #41 option A).
    Empty list = no restriction (compatible with old versions: every vendor).

    With ``printer_id`` set, reads the per-printer section and falls back to the
    legacy global ``[filament_profiles]`` filter.
    """
    path = _find_config_file()
    if not path:
        return []
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path, encoding="utf-8")
    section = _filament_section(printer_id)
    if not cfg.has_option(section, "visible_vendors"):
        section = "filament_profiles"          # fallback: legacy global section
    if not cfg.has_option(section, "visible_vendors"):
        return []
    raw = cfg.get(section, "visible_vendors")
    return [v.strip() for v in raw.split(",") if v.strip()]


def save_visible_vendors(vendors: list[str], printer_id: Optional[str] = None) -> bool:
    """Writes visible_vendors to [filament_profiles] without touching or losing the
    slot mappings (slot_N_*). An empty list removes the key again.

    With ``printer_id`` set, writes the per-printer section. When that section is
    created here for the first time, the slot mappings are copied from the legacy
    global section so they are not orphaned by the read fallback in
    ``list_filament_profiles``."""
    path = _find_config_file()
    if not path:
        return False
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path, encoding="utf-8")
    section = _filament_section(printer_id)
    if not cfg.has_section(section):
        cfg.add_section(section)
        if section != "filament_profiles" and cfg.has_section("filament_profiles"):
            for key, value in cfg.items("filament_profiles"):
                if key.startswith("slot_"):
                    cfg[section][key] = value
    clean = [v.strip() for v in (vendors or []) if v and v.strip()]
    if clean:
        cfg[section]["visible_vendors"] = ", ".join(clean)
    elif cfg.has_option(section, "visible_vendors"):
        cfg.remove_option(section, "visible_vendors")
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)
    return True


def _spoolman_map_section(printer_id: Optional[str] = None) -> str:
    """Name of the section holding one printer's AMS slot → Spoolman spool map.

    Multi-printer (one bridge, N printers): each printer keeps its map in its
    own ``[spoolman_<id>]`` section so two AMS units do not overwrite each other's
    mapping. ``printer_id is None`` (single printer / legacy callers)
    uses the original ``[spoolman] slot_spools`` key — fully backward
    compatible. The global ``[spoolman]`` section keeps ``server`` /
    ``sync_rate`` either way.
    """
    pid = str(printer_id).strip() if printer_id is not None else ""
    if pid and pid != "0":
        return f"{CONFIG_SECTION_SPOOLMAN}_{pid}"
    return CONFIG_SECTION_SPOOLMAN


def _parse_slot_spools(raw: str) -> dict[int, int]:
    """Converts ``"0:42,1:17"`` → ``{0: 42, 1: 17}`` (positive spool ids only)."""
    result: dict[int, int] = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if ":" not in pair:
            continue
        k, _, v = pair.partition(":")
        k, v = k.strip(), v.strip()
        if k.isdigit() and v.lstrip("-").isdigit() and int(v) > 0:
            result[int(k)] = int(v)
    return result


def list_spool_map(printer_id: Optional[str] = None) -> dict[int, int]:
    """Reads the AMS slot → Spoolman spool id map from config.ini.

    With ``printer_id`` set, reads the per-printer key ``[spoolman_<id>]
    slot_spools`` and falls back to the legacy global ``[spoolman]
    slot_spools`` while that printer has no section of its own. Returns
    ``{slot_index: spool_id}`` (positive ids only).
    """
    path = _find_config_file()
    if not path:
        return {}
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path, encoding="utf-8")
    section = _spoolman_map_section(printer_id)
    if cfg.has_option(section, "slot_spools"):
        return _parse_slot_spools(cfg.get(section, "slot_spools", fallback=""))
    if cfg.has_option(CONFIG_SECTION_SPOOLMAN, "slot_spools"):   # legacy global fallback
        return _parse_slot_spools(cfg.get(CONFIG_SECTION_SPOOLMAN, "slot_spools", fallback=""))
    return {}


def save_spool_map(slot_spools: dict[int, int], printer_id: Optional[str] = None) -> bool:
    """Persists the AMS slot → Spoolman spool id map to config.ini.

    With ``printer_id`` set, writes only the per-printer section ``[spoolman_<id>]``
    so other printers and the global ``[spoolman]`` server config are not
    touched. An empty map removes the key.
    """
    path = _find_config_file()
    if not path:
        return False
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path, encoding="utf-8")
    section = _spoolman_map_section(printer_id)
    clean = {int(k): int(v) for k, v in (slot_spools or {}).items() if int(v) > 0}
    if clean:
        if not cfg.has_section(section):
            cfg.add_section(section)
        cfg[section]["slot_spools"] = ",".join(f"{k}:{v}" for k, v in sorted(clean.items()))
    elif cfg.has_option(section, "slot_spools"):
        cfg.remove_option(section, "slot_spools")
    with open(path, "w", encoding="utf-8") as f:
        cfg.write(f)
    return True


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _safe_int(value: str, default: int) -> int:
    """Falls back to `default` instead of raising on a non-numeric value.

    All of this runs at module import - an unhandled ValueError here
    (e.g. a hand-edited config.ini with a typo such as `mqtt_port = 98833x`)
    would take the whole bridge down before it even starts, with a raw traceback
    instead of a clear diagnostic. list_printers() already guards this same
    class of input the same way; this applies it to the module-level
    shortcuts."""
    try:
        return int(value)
    except (TypeError, ValueError):
        log.warning("config: expected a number, got %r - using the default %r", value, default)
        return default


# Frequently used shortcuts
PRINTER_IP       = get("PRINTER_IP",       "")
MQTT_PORT        = _safe_int(get("MQTT_PORT", "9883"), 9883)
USERNAME         = get("MQTT_USERNAME",    "")
PASSWORD         = get("MQTT_PASSWORD",    "")
MODE_ID          = get("MODE_ID",          "")
DEVICE_ID        = get("DEVICE_ID",        "")
POWER_ON_URL     = get("POWER_ON_URL",     "")
POWER_OFF_URL    = get("POWER_OFF_URL",    "")
POWER_STATUS_URL = get("POWER_STATUS_URL", "")
POWER_STATUS_INVERTED = _safe_int(get("POWER_STATUS_INVERTED", "0"), 0)
DEFAULT_AMS_SLOT = get("DEFAULT_AMS_SLOT", "auto")
AUTO_LEVELING           = _safe_int(get("AUTO_LEVELING",          "1"), 1)
VIBRATION_COMPENSATION  = _safe_int(get("VIBRATION_COMPENSATION", "0"), 0)
CAMERA_ON_PRINT         = _safe_int(get("CAMERA_ON_PRINT",        "0"), 0)
WEB_UPLOAD_WARNING = _safe_int(get("WEB_UPLOAD_WARNING", "1"), 1)
DELETE_PRINTER_FILE_AFTER_PRINT = _safe_int(get("DELETE_PRINTER_FILE_AFTER_PRINT", "0"), 0)
PRINT_START_DIALOG = _safe_int(get("PRINT_START_DIALOG", get("FILE_READY_DIALOG", "1")), 1)
SPOOLMAN_SERVER    = get("SPOOLMAN_SERVER",    "")
SPOOLMAN_SYNC_RATE = _safe_int(get("SPOOLMAN_SYNC_RATE", "0"), 0)
BRIDGE_HOST_IP     = get("BRIDGE_HOST_IP",     "")
POLL_INTERVAL      = _safe_int(get("POLL_INTERVAL",  "3"), 3)
JOB_LOG_KEEP          = _safe_int(get("JOB_LOG_KEEP", "100"), 100)
JOB_LOG_CONTEXT_LINES = _safe_int(get("JOB_LOG_CONTEXT_LINES", "200"), 200)
LOG_BUFFER_LINES      = _safe_int(get("LOG_BUFFER_LINES", "500"), 500)
VERBOSE_HTTP_LOG   = _safe_int(get("VERBOSE_HTTP_LOG", "0"), 0)
NOTIFY_URL         = get("NOTIFY_URL",  "")
NOTIFY_LANG        = get("NOTIFY_LANG", "en")
AUTH_USER          = get("AUTH_USER",     "")
AUTH_PASSWORD      = get("AUTH_PASSWORD", "")
AUTH_API_KEY       = get("AUTH_API_KEY",  "")
