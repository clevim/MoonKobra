"""Updates the full (non-sale) price of the 1 kg spool of every 3DFila colour
in 3dfila_cores.json ("preco_kg" in R$ and "preco_data"). Then run gerar.py.

Usage:  python3 profiles/brasil/precos_3dfila.py

Reads the WooCommerce variations JSON (data-product_variations) of each product
page: the variation with "1kg" in the attribute and its display_regular_price.
Product without variations: the page's full price (<del>, or its only price).
"""
import datetime
import html
import json
import pathlib
import re
import sys
import time
import urllib.request

AQUI = pathlib.Path(__file__).resolve().parent
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


def _brl(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def preco_1kg(page: str) -> float | None:
    m = re.search(r'data-product_variations="([^"]*)"', page)
    if m:
        try:
            variacoes = json.loads(html.unescape(m.group(1)))
        except ValueError:
            variacoes = []
        for v in variacoes if isinstance(variacoes, list) else []:
            attrs = " ".join(str(a) for a in (v.get("attributes") or {}).values()).lower()
            if re.search(r"\b1\s*-?\s*kg\b", attrs) and v.get("display_regular_price"):
                return float(v["display_regular_price"])
    # Simple product (e.g. TPU Flex): on sale, the full price is the <del>;
    # otherwise the JSON-LD "price" counts (the visible block only has Pix/instalments,
    # with the R$ in HTML entities).
    bloco = re.search(r'<p class="price[^"]*">(.*?)</p>', page, re.S)
    if bloco:
        cheio = re.search(r"<del[^>]*>.*?R\$\s*(?:</span>)?\s*([\d.,]+)", html.unescape(bloco.group(1)), re.S)
        if cheio:
            return _brl(cheio.group(1))
    ld = re.search(r'"price"\s*:\s*"?(\d+(?:\.\d+)?)', page)
    return float(ld.group(1)) if ld else None


def main():
    arq = AQUI / "3dfila_cores.json"
    cores = json.loads(arq.read_text(encoding="utf-8"))
    hoje = datetime.date.today().isoformat()
    falhas = []
    for i, c in enumerate(cores, 1):
        try:
            req = urllib.request.Request(c["url"], headers={"User-Agent": UA})
            page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
            p = preco_1kg(page)
        except Exception as e:  # network/404: keeps the previous price, if any
            p, err = None, str(e)
        else:
            err = "no 1 kg price on the page"
        if p:
            c["preco_kg"], c["preco_data"] = p, hoje
        else:
            falhas.append(f"{c['linha']} - {c['cor']}: {err}")
        print(f"[{i}/{len(cores)}] {c['linha']} - {c['cor']}: {p}", file=sys.stderr)
        time.sleep(0.5)
    arq.write_text(json.dumps(cores, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(cores) - len(falhas)} prices updated; {len(falhas)} failures")
    for f in falhas:
        print("  " + f)


if __name__ == "__main__":
    main()
