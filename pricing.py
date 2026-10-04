"""pricing.py - print quote from the G-code.

parse_gcode(path)          -> grams per tool, time, slicer price/kg and density,
                              purge matrix and filament changes.
compute(parsed, cfg, opt)  -> cost breakdown, selling price and real profit.

Fees are computed "inside out": commission, tax and card fees apply to the
selling price itself, so price = (base + fixed) / (1 - Σ%). Coupons and
discounts come off the list price and fees apply to what the customer actually
pays - the profit shown is already what is left after the coupon.
Every fee/tax lives in cfg (data/pricing.json), never in the code:
Shopee and Mercado Livre changed their rules in Mar/2026 and will change again.
"""
import copy
import datetime
import json
import math
import os
import re

HEAD_BYTES = 64 * 1024
TAIL_BYTES = 512 * 1024   # Orca's config block sits at the end and exceeds 100 KB

DEFAULT_CONFIG = {
    "currency": "R$",
    # Values from Sep/2026 (Muriaé-MG, Brazil); sources in the CHANGELOG. Review now and then.
    "updated_at": "2026-09-25",
    "business": {"name": "", "contact": "", "footer": "Obrigado pela preferência!"},
    "quote_valid_days": 7,
    # Kobra X: R$ 3,899 full price at the official BR store; 150-250 W printing (measured, ~300 W peak).
    "printer": {"name": "Anycubic Kobra X", "watts": 200, "price": 3899, "life_hours": 4000,
                "maint_per_hour": 0.30},
    # Energisa Minas Rio B1: R$ 0.919 before taxes (since 2026-06-22) + yellow tariff flag
    # (R$ 1.885/100 kWh) + ICMS MG 18% and PIS/COFINS included ≈ R$ 1.25/kWh.
    "kwh_price": 1.25,
    "labor_rate": 30,          # R$/h
    "prep_minutes": 10,        # per order (slicing, preparing the bed)
    "post_minutes": 5,         # per part (removing supports, cleaning)
    "failure_pct": 8,
    "margin_pct": 100,
    "packaging": 2,            # per part
    "min_price": 10,           # per part
    "rounding": "90",          # none | 90 | 1 | 5
    "urgency_pct": 30,
    "include_flush": True,     # ACE purge (the Kobra X purges in firmware, outside "filament used")
    # Full price of a 1 kg 3DFila spool (Sep/2026). 3DFila profiles imported into
    # MoonKobra already carry each colour's exact price and win over this table.
    "materials": [
        {"name": "PLA (3DFila)",       "type": "PLA",  "price_kg": 99.90,  "density": 1.25},
        {"name": "PLA Matte (3DFila)", "type": "PLA",  "price_kg": 100.90, "density": 1.30},
        {"name": "PLA Silk (3DFila)",  "type": "PLA",  "price_kg": 119.90, "density": 1.22},
        {"name": "PETG XT (3DFila)",   "type": "PETG", "price_kg": 96.90,  "density": 1.27},
        {"name": "PETG HS (3DFila)",   "type": "PETG", "price_kg": 109.90, "density": 1.27},
        {"name": "ABS Premium (3DFila)", "type": "ABS", "price_kg": 85.90, "density": 1.05},
        {"name": "TPU Flex (3DFila)",  "type": "TPU",  "price_kg": 147.90, "density": 1.22},
        {"name": "ASA (genérico)",     "type": "ASA",  "price_kg": 150,    "density": 1.07},
    ],
    "taxes": [
        {"name": "MEI", "pct": 0},
        {"name": "Simples - Anexo I (comércio)", "pct": 4},
        {"name": "Simples - Anexo II (fabricação)", "pct": 4.5},
        {"name": "ISS (serviço)", "pct": 5},
    ],
    # Two tiers per channel: below `threshold` (price per part) pct_low/fixed_low apply.
    "channels": [
        {"name": "Venda direta / Pix", "threshold": 0, "pct_low": 0, "fixed_low": 0, "pct": 0, "fixed": 0},
        {"name": "Cartão de crédito", "threshold": 0, "pct_low": 0, "fixed_low": 0, "pct": 4.99, "fixed": 0},
        {"name": "Shopee (CNPJ)", "threshold": 80, "pct_low": 20, "fixed_low": 4, "pct": 14, "fixed": 20},
        {"name": "Shopee (CPF, +450 pedidos/90 dias)", "threshold": 80, "pct_low": 20, "fixed_low": 7, "pct": 14, "fixed": 23},
        {"name": "Mercado Livre Clássico", "threshold": 79, "pct_low": 12, "fixed_low": 6.75, "pct": 12, "fixed": 0},
        {"name": "Mercado Livre Premium", "threshold": 79, "pct_low": 17, "fixed_low": 6.75, "pct": 17, "fixed": 0},
    ],
    "qty_discounts": [
        {"min_qty": 10, "pct": 5},
        {"min_qty": 50, "pct": 10},
    ],
    "coupons": [
        {"code": "BEMVINDO10", "kind": "pct", "value": 10, "min_total": 0,
         "valid_until": "", "active": True, "note": "Primeira compra"},
    ],
}


# ─── G-code reading ────────────────────────────────────────────────────────────

def _floats(s: str) -> list:
    out = []
    for p in re.split(r"[,;]", s):
        p = p.strip().strip('"')
        try:
            out.append(float(p))
        except ValueError:
            out.append(0.0)
    return out


def _strs(s: str) -> list:
    return [p.strip().strip('"') for p in re.split(r";", s)] if ";" in s else \
           [p.strip().strip('"') for p in s.split(",")]


def _time_s(txt: str) -> int:
    secs = 0
    for val, unit in re.findall(r"(\d+)\s*([dhms])", txt):
        secs += int(val) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return secs


def parse_gcode(path: str) -> dict:
    """Reads the header/footer (Orca, Prusa, Bambu/Anycubic Slicer Next keys)
    and scans the `T<n>` lines for the filament change sequence."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(HEAD_BYTES)
        if size > HEAD_BYTES + TAIL_BYTES:
            f.seek(-TAIL_BYTES, os.SEEK_END)
        tail = f.read()
    text = (head + b"\n" + tail).decode("utf-8", errors="ignore")

    kv = {}
    for m in re.finditer(r"^;\s*([^=:\n]+?)\s*[=:]\s*(.*)$", text, re.M):
        kv.setdefault(m.group(1).strip().lower(), m.group(2).strip())

    grams = _floats(kv.get("filament used [g]", "")) if "filament used [g]" in kv else []
    mm = _floats(kv.get("filament used [mm]", "")) if "filament used [mm]" in kv else []
    density = _floats(kv.get("filament_density", ""))
    diameter = _floats(kv.get("filament_diameter", ""))
    if not grams and mm:  # length only: g = mm × area × ρ
        grams = [l * math.pi * ((diameter[i] if i < len(diameter) and diameter[i] else 1.75) / 2) ** 2
                 * (density[i] if i < len(density) and density[i] else 1.24) / 1000
                 for i, l in enumerate(mm)]
    if not grams:
        tot = kv.get("total filament used [g]") or kv.get("total filament weight [g]")
        if tot:
            grams = [_floats(tot)[0]]

    time_txt = (kv.get("estimated printing time (normal mode)") or kv.get("total estimated time")
                or kv.get("model printing time") or "")
    matrix = _floats(kv.get("flush_volumes_matrix", "")) if kv.get("flush_volumes_matrix") else []

    # Tool sequence (T0..T15) over the whole body - streamed read.
    seq = []
    tool_re = re.compile(rb"^T(\d{1,2})\s*(?:;|$)")
    with open(path, "rb") as f:
        for line in f:
            if line[:1] == b"T":
                m = tool_re.match(line)
                if m:
                    t = int(m.group(1))
                    if not seq or seq[-1] != t:
                        seq.append(t)

    n = len(grams)
    colours = _strs(kv.get("filament_colour", "")) if kv.get("filament_colour") else []
    types = _strs(kv.get("filament_type", "")) if kv.get("filament_type") else []
    return {
        "grams": [round(g, 3) for g in grams],
        "time_s": _time_s(time_txt),
        "layers": int(_floats(kv.get("total layers count") or kv.get("total layer number") or "0")[0]),
        "cost_per_kg": _floats(kv.get("filament_cost", ""))[:max(n, 1)] if kv.get("filament_cost") else [],
        "density": density[:max(n, 1)],
        "types": types[:max(n, 1)],
        "colours": colours[:max(n, 1)],
        # Filament profile name of each tool (matched against the profiles imported into MoonKobra).
        "settings_ids": (_strs(kv["filament_settings_id"]) if kv.get("filament_settings_id") else [])[:max(n, 1)],
        "flush_matrix": matrix,
        "flush_multiplier": _floats(kv.get("flush_multiplier", "1"))[0] or 1.0,
        "tool_sequence": seq,
        "slicer": (re.search(r"^;\s*generated by\s+(.+)$", text, re.I | re.M) or [None, ""])[1].strip()
                  if re.search(r"^;\s*generated by", text, re.I | re.M) else "",
    }


def flush_grams(parsed: dict, densities: list) -> list:
    """Purged grams per destination tool: Σ matrix[from][to] × multiplier
    (mm³) × density. The first load does not count (there is no 'from')."""
    mtx, seq = parsed.get("flush_matrix") or [], parsed.get("tool_sequence") or []
    n = int(round(math.sqrt(len(mtx)))) if mtx else 0
    out = [0.0] * max(len(parsed.get("grams") or []), n, 1)
    if n * n != len(mtx):
        return out
    mult = parsed.get("flush_multiplier") or 1.0
    for a, b in zip(seq, seq[1:]):
        if a < n and b < n:
            d = densities[b] if b < len(densities) and densities[b] else 1.24
            out[b] += mtx[a * n + b] * mult * d / 1000.0
    return out


# ─── Configuration ──────────────────────────────────────────────────────────────

def _num(v, default=0.0, lo=None, hi=None) -> float:
    try:
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            raise ValueError
    except (TypeError, ValueError):
        x = float(default)
    if lo is not None:
        x = max(lo, x)
    if hi is not None:
        x = min(hi, x)
    return x


_ROW_BLANK = {"kind": "pct"}


def sanitize_config(cfg) -> dict:
    """Merges with the defaults and coerces types - the JSON comes from the browser."""
    base = copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(cfg, dict):
        return base
    for k, dv in DEFAULT_CONFIG.items():
        if k not in cfg:
            continue
        v = cfg[k]
        if isinstance(dv, dict):
            if isinstance(v, dict):
                for kk, ddv in dv.items():
                    if kk in v:
                        base[k][kk] = str(v[kk])[:200] if isinstance(ddv, str) else _num(v[kk], ddv, 0)
        elif isinstance(dv, list):
            if isinstance(v, list):
                tmpl = dv[0]
                rows = []
                for r in v[:200]:
                    if not isinstance(r, dict):
                        continue
                    row = {}
                    for kk, ddv in tmpl.items():
                        # Missing field = empty, never the value of the example row.
                        rv = r.get(kk, _ROW_BLANK.get(kk, True if isinstance(ddv, bool) else
                                                      "" if isinstance(ddv, str) else 0))
                        if isinstance(ddv, bool):
                            row[kk] = bool(rv)
                        elif isinstance(ddv, str):
                            row[kk] = str(rv if rv is not None else "")[:120]
                        else:
                            row[kk] = _num(rv, 0, 0)
                    rows.append(row)
                base[k] = rows
        elif isinstance(dv, bool):
            base[k] = bool(v)
        elif isinstance(dv, str):
            base[k] = str(v)[:40]
        else:
            base[k] = _num(v, dv, 0)
    if base["rounding"] not in ("none", "90", "1", "5"):
        base["rounding"] = "90"
    base["failure_pct"] = min(base["failure_pct"], 90)
    for c in base["coupons"]:
        if c["kind"] not in ("pct", "value"):
            c["kind"] = "pct"
        c["code"] = c["code"].strip().upper()
    return base


def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return sanitize_config(json.load(f))
    except (OSError, ValueError):
        return copy.deepcopy(DEFAULT_CONFIG)


def save_config(path: str, cfg) -> dict:
    clean = sanitize_config(cfg)
    clean["updated_at"] = datetime.date.today().isoformat()  # saving = the fees were reviewed
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return clean


# ─── Calculation ───────────────────────────────────────────────────────────────────

def _round_price(p: float, mode: str) -> float:
    if mode == "90":
        return math.floor(p) + 0.90 if p - math.floor(p) <= 0.90 else math.floor(p) + 1.90
    if mode == "1":
        return float(math.ceil(p - 1e-9))
    if mode == "5":
        return float(math.ceil(p / 5 - 1e-9) * 5)
    return math.ceil(p * 100 - 1e-6) / 100


def _tier(ch: dict, unit_price: float) -> tuple:
    if ch.get("threshold", 0) > 0 and unit_price < ch["threshold"]:
        return ch.get("pct_low", 0) / 100, ch.get("fixed_low", 0)
    return ch.get("pct", 0) / 100, ch.get("fixed", 0)


def _gross_up(net: float, ch: dict, tax: float) -> float:
    """Price per part that, after tax + commission + the channel's fixed fee, leaves `net`.
    Tries both tiers of the channel and keeps the one consistent with its own price."""
    thr = ch.get("threshold", 0)
    candidates = []
    for low in ((True, False) if thr > 0 else (False,)):
        pct = (ch.get("pct_low", 0) if low else ch.get("pct", 0)) / 100
        fixed = ch.get("fixed_low", 0) if low else ch.get("fixed", 0)
        den = 1 - pct - tax
        if den <= 0.05:
            raise ValueError("fees add up to 95% or more of the price")
        p = (net + fixed) / den
        if thr <= 0 or (low and p < thr) or (not low and p >= thr):
            candidates.append(p)
    # None consistent = the price lands exactly on the step: stay on the step.
    return min(candidates) if candidates else float(thr)


def _pick(lst, idx):
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return None
    return lst[i] if 0 <= i < len(lst) else None


def find_coupon(cfg: dict, code: str, total: float, today: datetime.date | None = None):
    """(coupon, error). error is a short key that the UI translates."""
    code = (code or "").strip().upper()
    if not code:
        return None, ""
    today = today or datetime.date.today()
    for c in cfg.get("coupons", []):
        if c.get("code", "").strip().upper() != code:
            continue
        if not c.get("active", True):
            return None, "inactive"
        vu = c.get("valid_until") or ""
        if vu:
            try:
                if datetime.date.fromisoformat(vu) < today:
                    return None, "expired"
            except ValueError:
                pass
        if total < c.get("min_total", 0):
            return None, "min_total"
        return c, ""
    return None, "not_found"


_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _list_for_total(total: float, qd: dict | None, coupon: dict | None, dp: float, dv: float) -> float:
    """Inverse of the discount chain (quantity → coupon → manual): which list price,
    after every discount, gives exactly `total`."""
    rem = (total + dv) / (1 - dp / 100) if dp < 100 else total + dv
    if coupon:
        rem = rem / (1 - coupon["value"] / 100) if coupon["kind"] == "pct" and coupon["value"] < 100 \
            else rem + (coupon["value"] if coupon["kind"] == "value" else 0)
    if qd and 0 < qd.get("pct", 0) < 100:
        rem = rem / (1 - qd["pct"] / 100)
    return rem


# Costs that can be dropped from one specific order (e.g. no packaging, customer picks up).
EXCLUDABLE = {"flush", "energy", "machine", "labor", "failure", "packaging"}


def compute(parsed: dict, cfg: dict, opt: dict) -> dict:
    """opt: qty, per_plate (parts the G-code prints at once), materials{tool: idx},
    price_kg{tool: R$}, hours, channel, tax, urgent, coupon, discount_pct, discount_value, extras, shipping, design_fee,
    exclude[] (components zeroed for this order only: EXCLUDABLE),
    colours{tool: "#RRGGBB"} and glow{tool: bool} (colour/effect of the real spool, visual only),
    total_override (R$ > 0: the total the customer pays is this one - costs stay, fees and
    profit are recalculated on it and the discounts are still shown).
    parsed["profile_cost"][t] (optional, filled in by the bridge): R$/kg of the imported
    profile with the same name as that tool's filament_settings_id."""
    qty = int(_num(opt.get("qty"), 1, 1, 100000))
    # Plate with several parts: the G-code's time, filament and purge are split among them.
    # ponytail: proportional split - does not charge the incomplete plate when qty is not
    # a multiple of per_plate; if that matters, charge ceil(qty / per_plate) plates.
    per_plate = int(_num(opt.get("per_plate"), 1, 1, 1000))
    grams = [g / per_plate for g in parsed.get("grams") or []]
    mats = cfg.get("materials", [])
    hours = (_num(opt.get("hours"), 0, 0) or (parsed.get("time_s", 0) / 3600)) / per_plate

    # Material per tool: chosen > imported profile (real brand price)
    # > matched by the G-code type > the G-code's filament_cost (the Anycubic default is generic).
    tools, densities = [], []
    for t, g in enumerate(grams):
        gtype = (parsed.get("types") or [""] * (t + 1))[t] if t < len(parsed.get("types") or []) else ""
        chosen = (opt.get("materials") or {}).get(str(t))
        m = _pick(mats, chosen) if chosen not in (None, "") else None
        if m is None:
            m = next((x for x in mats if x.get("type", "").upper() == gtype.upper() and gtype), None)
        slicer_kg = (parsed.get("cost_per_kg") or [0] * (t + 1))[t] if t < len(parsed.get("cost_per_kg") or []) else 0
        prof = parsed.get("profile_cost") or []
        prof_kg = prof[t] if t < len(prof) and chosen in (None, "") else 0
        sid = (parsed.get("settings_ids") or [])
        price_kg = prof_kg or (m or {}).get("price_kg") or slicer_kg or 100
        override = (opt.get("price_kg") or {}).get(str(t))
        if override not in (None, ""):
            price_kg = _num(override, price_kg, 0)
        dens = (m or {}).get("density") or ((parsed.get("density") or [0] * (t + 1))[t]
                                            if t < len(parsed.get("density") or []) else 0) or 1.24
        densities.append(dens)
        colour = (parsed.get("colours") or [""] * (t + 1))[t] if t < len(parsed.get("colours") or []) else ""
        # Colour changed for this quote only (the real spool is not the slicer's) and special effect.
        cov = str((opt.get("colours") or {}).get(str(t)) or "")
        if _HEX.match(cov):
            colour = cov.upper()
        tools.append({"tool": t, "grams": g, "type": gtype,
                      "material": (sid[t] if prof_kg and t < len(sid) else (m or {}).get("name", gtype)),
                      "from_profile": bool(prof_kg),
                      "material_idx": mats.index(m) if m in mats else -1, "price_kg": price_kg,
                      "colour": colour, "colour_custom": bool(_HEX.match(cov)),
                      "glow": bool((opt.get("glow") or {}).get(str(t)))})

    off = set(opt.get("exclude") or []) & EXCLUDABLE
    fl = flush_grams(parsed, densities) if cfg.get("include_flush", True) and "flush" not in off else []
    for t in tools:
        t["flush_g"] = round((fl[t["tool"]] if t["tool"] < len(fl) else 0.0) / per_plate, 3)

    material = sum(t["grams"] / 1000 * t["price_kg"] for t in tools)
    flush = sum(t["flush_g"] / 1000 * t["price_kg"] for t in tools)
    pr = cfg["printer"]
    energy = 0.0 if "energy" in off else pr["watts"] / 1000 * hours * cfg["kwh_price"]
    machine = 0.0 if "machine" in off else \
        ((pr["price"] / pr["life_hours"] if pr["life_hours"] else 0) + pr["maint_per_hour"]) * hours
    extras = _num(opt.get("extras"), 0, 0)
    direct = material + flush + energy + machine + extras
    failure = 0.0 if "failure" in off else direct * cfg["failure_pct"] / 100
    post = 0.0 if "labor" in off else cfg["post_minutes"] / 60 * cfg["labor_rate"]
    packaging = 0.0 if "packaging" in off else cfg["packaging"]
    unit_cost = direct + failure + post + packaging
    prep = 0.0 if "labor" in off else cfg["prep_minutes"] / 60 * cfg["labor_rate"]
    design = _num(opt.get("design_fee"), 0, 0)
    job_cost = unit_cost * qty + prep + design

    urgent = bool(opt.get("urgent"))
    margin = job_cost * cfg["margin_pct"] / 100
    urgency = (job_cost + margin) * cfg["urgency_pct"] / 100 if urgent else 0.0
    shipping = _num(opt.get("shipping"), 0, 0)
    net_target = job_cost + margin + urgency + shipping

    ch = _pick(cfg["channels"], opt.get("channel")) or (cfg["channels"][0] if cfg["channels"] else
                                                          {"name": "", "threshold": 0, "pct": 0, "fixed": 0})
    tx = _pick(cfg["taxes"], opt.get("tax")) or {"name": "", "pct": 0}
    tax = tx["pct"] / 100
    unit_list = _gross_up(net_target / qty, ch, tax)
    unit_list = _round_price(max(unit_list, cfg["min_price"]), cfg["rounding"])
    list_total = round(unit_list * qty, 2)

    # Discounts, in order: quantity → coupon → manual (each on what is left).
    discounts, remaining = [], list_total
    qd = max((d for d in cfg["qty_discounts"] if qty >= d["min_qty"] > 0), key=lambda d: d["min_qty"], default=None)
    if qd and qd["pct"]:
        v = round(remaining * qd["pct"] / 100, 2)
        discounts.append({"kind": "qty", "label": f"{int(qd['min_qty'])}+ un.", "pct": qd["pct"], "value": v})
        remaining -= v
    coupon, coupon_err = find_coupon(cfg, opt.get("coupon", ""), remaining)
    if coupon:
        v = round(remaining * coupon["value"] / 100 if coupon["kind"] == "pct" else coupon["value"], 2)
        v = min(v, remaining)
        discounts.append({"kind": "coupon", "label": coupon["code"].upper(),
                          "pct": coupon["value"] if coupon["kind"] == "pct" else 0, "value": v})
        remaining -= v
    dp, dv = _num(opt.get("discount_pct"), 0, 0, 100), _num(opt.get("discount_value"), 0, 0)
    if dp or dv:
        v = min(round(remaining * dp / 100 + dv, 2), remaining)
        discounts.append({"kind": "manual", "label": "", "pct": dp, "value": v})
        remaining -= v
    total = round(max(remaining, 0), 2)
    auto_total = total
    override = _num(opt.get("total_override"), 0, 0)
    if override > 0:
        total = round(override, 2)
        lst = _list_for_total(total, qd, coupon, dp, dv)
        # Redo the discounts on the new list, in the same order and with the same rules.
        rem, new_disc = lst, []
        for d in discounts:
            if d["kind"] == "qty":
                v = round(rem * d["pct"] / 100, 2)
            elif d["kind"] == "coupon":
                v = round(rem * coupon["value"] / 100 if coupon["kind"] == "pct" else coupon["value"], 2)
            else:
                v = round(rem * dp / 100 + dv, 2)
            v = min(v, rem)
            new_disc.append(dict(d, value=v))
            rem -= v
        discounts = new_disc
        # Rounding cents: the list closes exactly on the typed total.
        list_total = round(total + sum(d["value"] for d in discounts), 2)
        unit_list = list_total / qty

    # Real fees on what the customer pays (the channel tier follows the final price per part).
    pct, fixed = _tier(ch, total / qty)
    fees_channel = total * pct + fixed * qty
    fees_tax = total * tax
    profit = total - fees_channel - fees_tax - job_cost - shipping

    r2 = lambda x: round(x, 2)  # noqa: E731
    return {
        "qty": qty, "per_plate": per_plate, "hours": round(hours, 3), "tools": tools,
        "channel": ch.get("name", ""), "tax_name": tx.get("name", ""),
        "costs": {
            "material": r2(material * qty), "flush": r2(flush * qty), "energy": r2(energy * qty),
            "machine": r2(machine * qty), "extras": r2(extras * qty), "failure": r2(failure * qty),
            "labor": r2(post * qty + prep), "packaging": r2(packaging * qty), "design": r2(design),
        },
        "excluded": sorted(off),
        "job_cost": r2(job_cost), "unit_cost": r2(unit_cost),
        "urgency": r2(urgency), "shipping": r2(shipping),
        "unit_list": r2(unit_list), "list_total": r2(list_total),
        "discounts": discounts, "coupon_error": coupon_err,
        "total": total, "unit_total": r2(total / qty),
        "total_override": override > 0, "auto_total": r2(auto_total),
        "fees_channel": r2(fees_channel), "fees_tax": r2(fees_tax),
        "profit": r2(profit), "margin_real_pct": r2(profit / total * 100) if total else 0.0,
        "grams_total": r2(sum(t["grams"] + t["flush_g"] for t in tools) * qty),
    }


if __name__ == "__main__":
    import sys
    p = parse_gcode(sys.argv[1])
    print(json.dumps(p, indent=1)[:2000])
    print(json.dumps(compute(p, DEFAULT_CONFIG, {"qty": 1, "channel": 0, "tax": 0}), indent=1, ensure_ascii=False))
