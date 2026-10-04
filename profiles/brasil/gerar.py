"""Generates filament profiles of Brazilian brands for OrcaSlicer and MoonKobra.

Usage:  python3 profiles/brasil/gerar.py

Output (next to this script):
  orcaslicer/<Brand> <Line>.json            line profile (neutral colour)
  orcaslicer/<Brand> <Line> - <Colour>.json one profile per colour (3DFila only)
  filamentos-brasil.zip                      the same files, to import into the bridge

Each profile inherits from the equivalent Anycubic Kobra X profile (compatible with the
printer and with the rest of Anycubic's settings) and only overrides what the
manufacturer publishes: nozzle temperature and range, bed, density and fan.
The filament_id is fixed and unique per profile ("P" + 7 hex, the
OrcaSlicer-KX convention), so AMS sync matching does not fall back to Generic.

Temperatures: manufacturers' datasheets (Sep/2026). Every batch/colour varies -
calibrate on your printer and adjust the tables below.
3DFila colours: 3dfila_cores.json (official HEX from each product's "Pantone"
image on the website; "oficial": false = colour taken from the spool photo).
3DFila price: "preco_kg" from 3dfila_cores.json (full price of a 1 kg spool,
updated by precos_3dfila.py) becomes the colour profile's filament_cost (R$/kg);
the line profile takes the most common price among its colours.
"""
import collections
import hashlib
import json
import pathlib
import zipfile

AQUI = pathlib.Path(__file__).resolve().parent
KX = "@Anycubic Kobra X 0.4 nozzle"

ORCA_VERSAO = "2.4.2.0"

# base material -> (parent profile in OrcaSlicer, filament_type, parent setting_id in Orca v2.4.2)
# The setting_id goes in the .info next to the .json (base_id), just like Orca writes it.
PAIS = {
    "PLA":       (f"Anycubic PLA {KX}", "PLA", "1hyHPSJZgUpLcW7u"),
    "PLA Matte": (f"Anycubic PLA Matte {KX}", "PLA", "JxYmWTeCtw0VvUcC"),
    "PLA Silk":  (f"Anycubic PLA Silk {KX}", "PLA", "Yc1P4D9Z42Abjfut"),
    "PLA HS":    (f"Anycubic PLA High Speed {KX}", "PLA", "hQ90rvCLS2rVEPVJ"),
    "PETG":      (f"Anycubic PETG {KX}", "PETG", "HyTi93UGno6uAJEO"),
    "ABS":       (f"Anycubic ABS {KX}", "ABS", "KeEk3yQZrKmJY9Q0"),
    "ASA":       (f"Anycubic ASA {KX}", "ASA", "PEg6aLXcyUc70p9F"),
    "TPU":       (f"Anycubic TPU 95A {KX}", "TPU", "9zwUSJcKXHTuDM1i"),
}

# (brand, line, base, nozzle, nozzle min, nozzle max, bed, density, fan (min, max) or None, source)
# ponytail: hand-made table; if the list grows a lot, it becomes a CSV.
LINHAS = [
    ("3DFila", "PLA",             "PLA",       210, 190, 230, 60, 1.25, (60, 100), "3dfila.com.br/categoria filamento-pla: bico 190-230, mesa 50-65, 1,24-1,27 g/cm3, ventoinha 60-100%"),
    ("3DFila", "PLA Matte",       "PLA Matte", 210, 190, 220, 55, 1.30, (60, 100), "3dfila.com.br/categoria filamento-pla-matte: bico 190-220, mesa 50-60, 1,25-1,35 g/cm3"),
    ("3DFila", "PLA Silk",        "PLA Silk",  220, 200, 230, 60, 1.22, (30, 70),  "3dfila.com.br/categoria filamento-pla-silk (Silk, Duo, Tricolor, Rainbow): bico 200-230, mesa 50-60, ventoinha 30-70%"),
    ("3DFila", "PLA Magic",       "PLA",       210, 195, 225, 55, 1.24, (60, 100), "3dfila.com.br/categoria filamento-pla-magic: bico 195-225, mesa 50-60"),
    ("3DFila", "PLA EasyFill",    "PLA",       215, 200, 230, 60, 1.24, (50, 100), "3dfila.com.br/categoria filamento-pla-easyfill: bico 200-230, mesa 50-65, 60-300 mm/s"),
    ("3DFila", "PLA Wood",        "PLA",       215, 200, 230, 55, 1.22, None,      "3dfila.com.br - PLA Madeira: bico 200-230, mesa 45-60, 1,20-1,25 g/cm3"),
    ("3DFila", "PLA GF",          "PLA",       215, 200, 230, 55, 1.22, None,      "3dfila.com.br - PLA Fibra de Vidro: bico 200-230, mesa 45-60, 1,20-1,25 g/cm3"),
    ("3DFila", "PETG XT",         "PETG",      240, 220, 260, 80, 1.27, (20, 60),  "3dfila.com.br/categoria filamento-petg: bico 220-260, mesa 70-90, 1,25-1,29 g/cm3, ventoinha 20-60%"),
    ("3DFila", "PETG High Speed", "PETG",      250, 230, 270, 75, 1.27, (20, 60),  "3dfila.com.br - PETG HS: bico 230-270, mesa 70-80 (produto) / 70-90 (categoria), 50-500 mm/s"),
    ("3DFila", "TPU Flex",        "TPU",       225, 210, 240, 50, 1.22, (0, 40),   "3dfila.com.br/categoria filamento-tpu-flex: bico 210-240, mesa 40-60, 20-100 mm/s, ventoinha 0-40%"),
    ("3DFila", "ABS Premium",     "ABS",       240, 220, 255, 100, 1.05, (0, 10),  "3dfila.com.br/categoria filamento-abs-premium: bico 220-255, mesa 90-115"),
    ("3DFila", "PLA Mármore",      "PLA",       210, 190, 230, 60, 1.25, (60, 100), "3dfila.com.br - PLA Mármore: fabricante não publica ficha técnica; defaults do PLA base — calibre bico/mesa na impressora"),
    ("3DFila", "PLA Fosforescente",  "PLA",       210, 195, 225, 55, 1.24, (60, 100), "3dfila.com.br - PLA Fosforescente (Glow): sem ficha técnica publicada; defaults do PLA — calibrar"),
    ("3DFila", "PLA HT",             "PLA",       215, 200, 230, 60, 1.24, (50, 100), "3dfila.com.br - PLA HT (recozível): sem temperatura de impressão publicada; defaults do PLA — calibrar (a peça pode ser recozida à parte)"),
    ("3DFila", "PLA ABS-Like",       "PLA",       215, 200, 230, 60, 1.24, (40, 80),  "3dfila.com.br - PLA ABS-Like: sem ficha técnica publicada; defaults do PLA — calibrar"),
    ("3DFila", "PLA CF",             "PLA",       220, 210, 235, 60, 1.22, None,      "3dfila.com.br - PLA CF (fibra de carbono, abrasivo): sem ficha técnica; defaults do PLA — use bico endurecido e calibre"),
    ("3DFila", "ABS MG94",           "ABS",       240, 220, 255, 100, 1.05, (0, 10),  "3dfila.com.br - ABS MG94 (ABS industrial): sem ficha técnica publicada; defaults do ABS base — calibrar"),
    ("GTMax3D", "PLA",            "PLA",       205, 190, 220, 60, 1.24, None, "gtmax3d.com.br - PLA: bico 190-220, mesa 0-60"),
    ("GTMax3D", "PLA Speed+",     "PLA HS",    215, 190, 220, 60, 1.24, None, "gtmax3d.com.br - PLA Speed+: bico 190-220, 80-500 mm/s"),
    ("GTMax3D", "PETG",           "PETG",      240, 225, 270, 85, 1.27, None, "gtmax3d.com.br - PETG: bico 225-270, mesa 80-95"),
    ("GTMax3D", "ABS Premium",    "ABS",       240, 220, 250, 105, 1.05, None, "gtmax3d.com.br - ABS Premium: bico 220-250, mesa 100-110"),
    ("3D Lab", "PLA",             "PLA",       215, 190, 220, 60, 1.24, None, "3dlab.com.br - PLA: bico ideal 215 (190-220)"),
    ("3D Lab", "PETG",            "PETG",      245, 235, 255, 80, 1.27, None, "3dlab.com.br - PETG: bico 245 (235-255), mesa 70-85"),
    ("F3D", "PLA Premium",        "PLA",       215, 205, 230, 60, 1.24, None, "filamentos3dbrasil.com.br - PLA Premium: bico 205-230, mesa 25-60"),
]


def _id(nome: str) -> str:
    return "P" + hashlib.md5(nome.encode()).hexdigest()[:7]


def perfil(marca, linha, base, bico, lo, hi, mesa, dens, fan, fonte, cor=None, preco=None) -> dict:
    """Line profile, or - with `cor` (an entry of 3dfila_cores.json) - that colour's profile."""
    pai, ftype, _ = PAIS[base]
    nome = f"{marca} {linha}" + (f" - {cor['cor']}" if cor else "")
    s = lambda v: [str(v)]
    notas = f"Gerado por MoonKobra profiles/brasil. Fonte: {fonte}"
    p = {
        "type": "filament",
        "name": nome,
        "inherits": pai,
        "from": "User",
        "instantiation": "true",
        "is_custom_defined": "0",
        "version": ORCA_VERSAO,
        "filament_settings_id": [nome],
        "filament_id": _id(nome),
        "filament_vendor": [marca],
        "filament_type": [ftype],
        "filament_density": s(dens),
        "nozzle_temperature": s(bico),
        "nozzle_temperature_initial_layer": s(min(bico + 5, hi)),
        "nozzle_temperature_range_low": s(lo),
        "nozzle_temperature_range_high": s(hi),
        "hot_plate_temp": s(mesa),
        "hot_plate_temp_initial_layer": s(mesa),
        "textured_plate_temp": s(mesa),
        "textured_plate_temp_initial_layer": s(mesa),
    }
    if fan:
        p["fan_min_speed"], p["fan_max_speed"] = s(fan[0]), s(fan[1])
    if preco:
        p["filament_cost"] = s(preco)
    if cor:
        p["default_filament_colour"] = [cor["hex"]]
        origem = ("HEX oficial 3DFila (Pantone " + ", ".join(cor["pantone"]) + ")") if cor["oficial"] \
            else "cor aproximada da foto do produto (a 3DFila não publica o HEX desta cor)"
        if len(cor["hex_todos"]) > 1:
            origem += "; cores do filamento: " + " ".join(cor["hex_todos"])
        notas += f" | Cor: {origem}"
        if cor.get("preco_kg"):
            notas += f" | Preço 1 kg: R$ {cor['preco_kg']:.2f} em {cor['preco_data']}".replace(".", ",", 1)
        notas += f" | {cor['url']}"
    p["filament_notes"] = [notas]
    p["_base_id"] = PAIS[base][2]
    return p


def todos() -> list[dict]:
    cores = json.loads((AQUI / "3dfila_cores.json").read_text(encoding="utf-8"))
    out = []
    for linha in LINHAS:
        da_linha = [c for c in cores if linha[0] == "3DFila" and c["linha"] == linha[1]]
        precos = collections.Counter(c["preco_kg"] for c in da_linha if c.get("preco_kg"))
        out.append(perfil(*linha, preco=precos.most_common(1)[0][0] if precos else None))
        out += [perfil(*linha, cor=c, preco=c.get("preco_kg")) for c in da_linha]
    return out


def main():
    saida = AQUI / "orcaslicer"
    saida.mkdir(exist_ok=True)
    for velho in [*saida.glob("*.json"), *saida.glob("*.info")]:
        velho.unlink()
    perfis = todos()
    ids = set()
    with zipfile.ZipFile(AQUI / "filamentos-brasil.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for p in perfis:
            assert p["filament_id"] not in ids, p["name"]
            ids.add(p["filament_id"])
            base_id = p.pop("_base_id")
            texto = json.dumps(p, indent=4, ensure_ascii=False) + "\n"
            (saida / f"{p['name']}.json").write_text(texto, encoding="utf-8")
            (saida / f"{p['name']}.info").write_text(
                f"sync_info = update\nuser_id = \nsetting_id = \nbase_id = {base_id}\nupdated_time = 0\n", encoding="utf-8")
            z.writestr(f"{p['name']}.json", texto)
    print(f"{len(perfis)} profiles in {saida} + filamentos-brasil.zip")


if __name__ == "__main__":
    main()
