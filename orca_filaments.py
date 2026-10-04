"""OrcaSlicer filament profile parser.

Used by the custom profile import endpoint (kobrax_moonraker_bridge.py) and by
whatever generates data/orca_filaments.json from OrcaSlicer's profile tree.

Reads Orca filament JSON files (system or user profiles) and returns
them as a normalized list of (id, name, vendor, type, color).
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("kobrax.filaments")


def first_str(value, default: str = "") -> str:
    """Orca profiles store some fields as ['value']. Returns the first
    element as a string."""
    if isinstance(value, list):
        return str(value[0]) if value else default
    if isinstance(value, str):
        return value
    return default


def clean_name(raw: str) -> str:
    """Strips printer/variant-specific suffixes:
      'PolyTerra PLA @base'                       → 'PolyTerra PLA'
      'Anycubic PLA @Anycubic Kobra X 0.4 nozzle' → 'Anycubic PLA'
      'Anker Generic PLA 0.4 nozzle'              → 'Anker Generic PLA'
    """
    name = re.sub(r"\s*@.*$", "", raw).strip()
    name = re.sub(r"\s+\d+(\.\d+)?\s*nozzle\s*$", "", name, flags=re.IGNORECASE).strip()
    return name or raw


def parse_profile(data: dict, by_name: dict | None = None,
                  path_vendor: str | None = None,
                  source_path: str = "",
                  system_index: list | None = None) -> dict | None:
    """Parses a single Orca filament profile into the bridge's schema.

    `by_name` is optionally a {name: [profile, ...]} index to resolve inherits
    from the raw source tree (generator). For single-file imports
    (a user file from the OrcaSlicer folder) we pass `system_index` -
    the ready-made list of system profiles from orca_filaments.json. That way we can
    derive filament_id/vendor/type/color through the `inherits` chain from the
    system parent, even when the user profile does not define those fields
    (typically: user override profiles only contain tweaks).

    Returns {id, name, vendor, type, color}, or None when the profile
    has no filament_id (e.g. abstract @base templates).
    """
    if not isinstance(data, dict):
        return None
    # User profiles from the OrcaSlicer folder often do NOT define "type" -
    # it comes from the system parent. We accept it when "type" is explicitly
    # "filament" OR when an "inherits" points to another profile.
    if data.get("type") not in (None, "filament") and not data.get("inherits"):
        return None
    if data.get("type") == "filament" and data.get("inherits") is None and not data.get("filament_id"):
        # type=filament but no parent + no ID -> worthless stub
        return None
    inst = data.get("instantiation", "true")
    if isinstance(inst, str) and inst.lower() == "false":
        return None

    # Builds the system name index for the fallback when system_index is set.
    sys_by_name: dict[str, dict] = {}
    if system_index:
        for p in system_index:
            if isinstance(p, dict) and p.get("name"):
                pname = p["name"]
                if pname in sys_by_name and sys_by_name[pname] is not p:
                    # clean_name() deliberately collapses names with a variant suffix
                    # (e.g. "...@base" vs "...@Anycubic Kobra X 0.4
                    # nozzle") into the same clean name - expected, but the
                    # "last write wins" overwrite here used to be silent, which
                    # made an unexpected parent resolution through inherits hard
                    # to debug.
                    log.debug("orca_filaments: duplicate system profile name %r - "
                              "overwriting %r with %r", pname, sys_by_name[pname].get("id"), p.get("id"))
                sys_by_name[pname] = p

    def _resolve(key: str, depth: int = 5):
        cur_list = [data]
        for _ in range(depth):
            for cur in cur_list:
                v = cur.get(key)
                if v not in ("", [], None, [""]) and v is not None:
                    return v
            # First raw inherits through by_name (generator path)
            if by_name:
                next_list: list[dict] = []
                for cur in cur_list:
                    parent_name = cur.get("inherits")
                    if parent_name and parent_name in by_name:
                        next_list.extend(by_name[parent_name])
                if next_list:
                    cur_list = next_list
                    continue
            break
        return None

    def _resolve_via_system_index(key: str):
        """Inherits chain through system_index (matched by clean_name)."""
        parent_raw = data.get("inherits")
        if not parent_raw or not sys_by_name:
            return None
        parent_clean = clean_name(parent_raw)
        sys_p = sys_by_name.get(parent_clean)
        if not sys_p:
            return None
        # The system JSON already uses the normalized schema
        mapping = {
            "filament_id":              "id",
            "filament_vendor":          "vendor",
            "filament_type":            "type",
            "default_filament_colour":  "color",
        }
        return sys_p.get(mapping.get(key, key))

    def _resolve_full(key: str):
        v = _resolve(key)
        if v not in ("", [], None, [""]) and v is not None:
            return v
        return _resolve_via_system_index(key)

    fid = _resolve_full("filament_id")
    if not fid or not isinstance(fid, str):
        return None

    name_raw = first_str(data.get("name"), fid)
    name = clean_name(name_raw)
    vendor = first_str(_resolve_full("filament_vendor")) or (path_vendor or "Generic")
    ftype  = first_str(_resolve_full("filament_type"), "")
    color  = first_str(_resolve_full("default_filament_colour"), "")

    out = {
        "id":     fid,
        "name":   name,
        "vendor": vendor,
        "type":   ftype,
        "color":  color,
    }
    # Price per kg (Quote): only the profile's own - the system parent's is
    # Anycubic's generic value, not a real price.
    try:
        cost = float(first_str(data.get("filament_cost"), "0") or 0)
    except ValueError:
        cost = 0.0
    if cost > 0:
        out["cost"] = cost
    return out


def parse_profile_bytes(blob: bytes, source_name: str = "",
                        system_index: list | None = None) -> dict | None:
    """Reads a single profile from JSON bytes. For the file upload path.
    `system_index` is optionally the ready-made list from orca_filaments.json -
    used to resolve inherits for user profiles that do not carry the full
    schema of the system parent."""
    try:
        data = json.loads(blob.decode("utf-8", errors="replace"))
    except Exception:
        return None
    return parse_profile(data, source_path=source_name, system_index=system_index)
