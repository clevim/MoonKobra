#!/usr/bin/env python3
"""Manual probe for Issue #100's open remainder: the printer rejects
multiColorBox/setInfo for physical ACE-box slots (box id >= 0) while the
same call succeeds for the no-ACE/single-slot case (box id == -1).

Sends a series of setInfo payload variants against ONE real ACE slot and
prints the printer's multiColorBox/report response for each, so the
accepted schema (if any of these match it) becomes visible by diff.

DO NOT run this against a printer with a print in progress - AMS slot
writes are safe at idle but untested behavior mid-print.

Usage:
    python3 tools/probe_ams_setinfo.py <printer_ip> <box_id> <local_slot> <material_type> <r> <g> <b>

Example (box 0, slot 2, keep existing PLA/color to be non-destructive):
    python3 tools/probe_ams_setinfo.py 192.168.2.144 0 2 PLA 238 190 152
"""
import sys
import os
import time
import json
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bridge"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kobrax_client import KobraXClient  # noqa: E402

MODE_ID = "20030"


def make_client(host):
    c = KobraXClient(
        host=host,
        username=os.environ.get("PROBE_USER", ""),
        password=os.environ.get("PROBE_PASS", ""),
        mode_id=os.environ.get("PROBE_MODE_ID", MODE_ID),
        device_id=os.environ.get("PROBE_DEVICE_ID", ""),
        port=9883,
        client_id="ams-probe",
    )
    return c


def variant_original(box_id, local_slot, mat, color):
    return {"multi_color_box": [{"id": box_id, "slots": [{"index": local_slot, "type": mat, "color": color}]}]}


def variant_full_slot_object(box_id, local_slot, mat, color, existing_slot):
    slot = dict(existing_slot)
    slot["index"] = local_slot
    slot["type"] = mat
    slot["color"] = color
    slot["color_group"] = [color + [255]]
    return {"multi_color_box": [{"id": box_id, "slots": [slot]}]}


def variant_filaments_key(box_id, local_slot, mat, color):
    # Mirrors the shape the printer's OWN rejection echo uses:
    # {"filaments": {"id": local_slot}, "id": box_id}
    return {"multi_color_box": [{"id": box_id, "filaments": [{"id": local_slot, "type": mat, "color": color}]}]}


def variant_filaments_singular(box_id, local_slot, mat, color):
    return {"multi_color_box": [{"id": box_id, "filaments": {"id": local_slot, "type": mat, "color": color}}]}


def _extract_slot(payload, box_id, local_slot):
    """multiColorBox/report's data comes as either a dict ({"multi_color_box": [...]})
    or, on rejection, a bare 2-element list (["multi_color_box", [...]]) - see
    Issue #100. Handle both so the probe doesn't silently miss a rejection reply."""
    data = payload.get("data")
    if isinstance(data, dict):
        boxes = data.get("multi_color_box") or []
    elif isinstance(data, list) and len(data) == 2 and data[0] == "multi_color_box":
        boxes = data[1] or []
    else:
        return None
    for box in boxes:
        if int(box.get("id", -999)) == box_id:
            slots = box.get("slots") or []
            if 0 <= local_slot < len(slots):
                return slots[local_slot]
    return None


def get_existing_slot(client, box_id, local_slot, timeout=5.0):
    """Reads the current slot state via the multiColorBox/report CALLBACK,
    not publish()'s return value - the generic empty response ACK
    ({"code":0,"data":None}) can arrive under the same msgid and race the
    real report, silently winning the publish() wait (observed live)."""
    result = {}
    done = threading.Event()

    def on_report(payload):
        slot = _extract_slot(payload, box_id, local_slot)
        if slot is not None:
            result["slot"] = slot
            done.set()

    client.callbacks["multiColorBox/report"] = on_report
    client.publish_web("multiColorBox", "getInfo", None)
    done.wait(timeout)
    client.callbacks.pop("multiColorBox/report", None)
    return result.get("slot")


def try_variant(client, name, payload, settle=3.0):
    print(f"\n=== {name} ===")
    print("TX:", payload)
    reports = []
    client.callbacks["multiColorBox/report"] = lambda p: reports.append(p)
    client.publish_web("multiColorBox", "setInfo", payload)
    time.sleep(settle)
    client.callbacks.pop("multiColorBox/report", None)
    for r in reports:
        print("RX report:", json.dumps(r, ensure_ascii=False)[:600])
    return reports


def main():
    if len(sys.argv) != 8:
        print(__doc__)
        sys.exit(1)
    host = sys.argv[1]
    box_id = int(sys.argv[2])
    local_slot = int(sys.argv[3])
    mat = sys.argv[4]
    color = [int(sys.argv[5]), int(sys.argv[6]), int(sys.argv[7])]

    client = make_client(host)
    client.connect()
    time.sleep(1)

    existing = get_existing_slot(client, box_id, local_slot)
    print("Existing slot state:", existing)
    if existing is None:
        print("Could not read existing slot - aborting (don't probe blind).")
        return

    try_variant(client, "A: original bridge shape (slots/index/type/color)",
                variant_original(box_id, local_slot, mat, color))

    try_variant(client, "B: full slot object (all fields from getInfo, only type/color changed)",
                variant_full_slot_object(box_id, local_slot, mat, color, existing))

    try_variant(client, "C: filaments list (mirrors printer's rejection echo shape)",
                variant_filaments_key(box_id, local_slot, mat, color))

    try_variant(client, "D: filaments singular dict",
                variant_filaments_singular(box_id, local_slot, mat, color))

    print("\nDone. Compare RX multiColorBox/report lines above/in the bridge log "
          "for each variant - state=success with the slot's type actually changed "
          "in the FOLLOWING getInfo/report is the only real confirmation.")
    client.disconnect()


if __name__ == "__main__":
    main()
