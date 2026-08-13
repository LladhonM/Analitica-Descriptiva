#!/usr/bin/env python
"""
diagnostico.py — Averigua POR QUE un scraper devuelve 0 avisos.

Descarga una pagina del portal y reporta:
  - Status HTTP, tamano de la respuesta y redirecciones.
  - Si hay senales de bloqueo (captcha, challenge, "acceso denegado").
  - Cuantos elementos matchea cada selector candidato.
  - Si hay JSON embebido con los avisos.
  - Una muestra del HTML alrededor de la primera aparicion de un precio.

Uso:
    py diagnostico.py mercadolibre
    py diagnostico.py argenprop --operacion alquiler
    py diagnostico.py zonaprop
    py diagnostico.py remax
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from bs4 import BeautifulSoup

from src import utils


# URLs candidatas por portal. Si la primera falla, probamos las siguientes:
# los portales cambian de estructura de URL cada tanto.
URLS = {
    "mercadolibre": [
        "https://inmuebles.mercadolibre.com.ar/departamentos/{op}/capital-federal/",
        "https://inmuebles.mercadolibre.com.ar/departamentos/{op}/capital-federal/_NoIndex_True",
        "https://listado.mercadolibre.com.ar/inmuebles/departamentos/{op}/capital-federal/",
        "https://inmuebles.mercadolibre.com.ar/departamentos/{op}/",
    ],
    "argenprop": [
        "https://www.argenprop.com/departamentos/{op}/capital-federal",
    ],
    "zonaprop": [
        "https://www.zonaprop.com.ar/departamentos-{op}-capital-federal.html",
    ],
    "remax": [
        "https://api.redremax.com/remaxweb-ar/api/listings/findAll"
        "?page=0&pageSize=20&sort=-priceUsd&in:operationId={opid}"
        "&in:typeId=1,2,3,4,5,6,7,8,9,10,11,12,13&locations=in::::14028:::"
        "&filterCount=0&viewMode=list",
    ],
}

# Selectores candidatos para las tarjetas de aviso, por portal
SELECTORES = {
    "mercadolibre": [
        "li.ui-search-layout__item",
        "div.ui-search-result__wrapper",
        "[class*='ui-search-layout__item']",
        "div.poly-card",
        "[class*='poly-card']",
        "div.andes-card",
        "[class*='search-result']",
        "ol.ui-search-layout li",
        "section.ui-search-results li",
    ],
    "argenprop": [
        "div.listing__item",
        "[class*='listing__item']",
        "a.card",
        "[class*='card__price']",
    ],
    "zonaprop": [
        "div[data-qa='posting PROPERTY']",
        "[data-qa*='posting']",
        "div.postingCard",
        "[class*='postingCard']",
        "[class*='PostingCard']",
    ],
}

BLOQUEO = [
    "captcha", "datadome", "acceso denegado", "access denied",
    "unusual traffic", "are you a robot", "cf-browser-verification",
    "challenge-platform", "blocked", "forbidden",
]


def diagnosticar(portal: str, operacion: str) -> int:
    print(utils.info_backend())
    session = utils.build_session()
    print(f"Backend en uso: {session.backend}")
    opid = 1 if operacion == "venta" else 2

    plantillas = URLS.get(portal, [])
    if not plantillas:
        print(f"Portal desconocido: {portal}")
        return 1

    encontrado = False

    for plantilla in plantillas:
        url = plantilla.format(op=operacion, opid=opid)
        print("\n" + "=" * 70)
        print(f"URL: {url}")
        print("=" * 70)

        try:
            resp = session.get(url, timeout=30, allow_redirects=True)
        except Exception as e:
            print(f"  ERROR de red: {type(e).__name__}: {e}")
            continue

        print(f"  Status HTTP    : {resp.status_code}")
        print(f"  Tamano         : {len(resp.content):,} bytes")
        print(f"  Content-Type   : {resp.headers.get('Content-Type', '?')}")

        if resp.history:
            print(f"  Redirecciones  : {len(resp.history)}")
            print(f"  URL final      : {resp.url}")

        if resp.status_code != 200:
            print(f"  >> El portal respondio {resp.status_code}, no 200.")
            if resp.status_code in (403, 429):
                print("  >> Es un BLOQUEO. Probá subir el delay o usar Playwright.")
            continue

        # --- API JSON (Remax) ---
        if "json" in resp.headers.get("Content-Type", "").lower():
            try:
                data = resp.json()
            except ValueError:
                print("  >> Dice ser JSON pero no parsea.")
                continue
            print("  Respuesta JSON OK")
            claves = list(data.keys()) if isinstance(data, dict) else f"lista de {len(data)}"
            print(f"  Claves de nivel 1: {claves}")
            items = (data.get("data", {}).get("data") if isinstance(data.get("data"), dict)
                     else data.get("data")) or []
            print(f"  Avisos en la respuesta: {len(items) if isinstance(items, list) else '?'}")
            if isinstance(items, list) and items:
                print(f"  Campos del primer aviso ({len(items[0])}):")
                for k in list(items[0].keys())[:35]:
                    v = str(items[0][k])[:60]
                    print(f"      {k:<28} = {v}")
                encontrado = True
            continue

        html = resp.text
        low = html.lower()

        # --- Senales de bloqueo ---
        hits = [m for m in BLOQUEO if m in low]
        if hits:
            print(f"  >> POSIBLE BLOQUEO. Palabras detectadas: {hits}")
        if len(html) < 40000:
            print("  >> La pagina es muy chica para un listado real. "
                  "Suele indicar challenge o redireccion a login.")

        soup = BeautifulSoup(html, "html.parser")
        titulo = soup.find("title")
        print(f"  <title>        : {utils.clean_text(titulo.text) if titulo else '(sin title)'}")

        # --- Selectores ---
        print("\n  Selectores candidatos:")
        algun_match = False
        for sel in SELECTORES.get(portal, []):
            try:
                n = len(soup.select(sel))
            except Exception:
                n = -1
            marca = "OK " if n > 0 else "   "
            print(f"    {marca}{n:>4} x  {sel}")
            if n > 0:
                algun_match = True
                encontrado = True

        # --- Busqueda a ciegas de clases que parezcan de tarjeta ---
        if not algun_match:
            print("\n  Ningun selector conocido matcheo.")
            print("  Clases mas frecuentes que parecen tarjetas de aviso:")
            from collections import Counter
            c = Counter()
            for el in soup.find_all(attrs={"class": True}):
                for cl in el.get("class", []):
                    if any(k in cl.lower() for k in
                           ("card", "item", "result", "posting", "listing", "poly", "ad-")):
                        c[cl] += 1
            for cl, n in c.most_common(20):
                print(f"    {n:>4} x  .{cl}")
            if not c:
                print("    (ninguna. La pagina probablemente no trae el listado en el HTML)")

        # --- JSON embebido ---
        print("\n  JSON embebido:")
        patrones = {
            "__NEXT_DATA__": r'<script[^>]*id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>',
            "__PRELOADED_STATE__": r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});',
            '"results":': r'"results"\s*:\s*(\[\{.*?\}\])\s*,\s*"',
            '"listPostings":': r'"listPostings"\s*:\s*(\[.*?\])\s*,\s*"',
        }
        hallado_json = False
        for nombre, pat in patrones.items():
            m = re.search(pat, html, flags=re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    tam = len(data) if isinstance(data, list) else len(json.dumps(data))
                    print(f"    OK  {nombre}  ({tam} elementos/chars)")
                    hallado_json = True
                    encontrado = True
                except Exception:
                    print(f"    ??  {nombre} encontrado pero no parsea")
            else:
                print(f"        {nombre}: no esta")
        if not hallado_json:
            # Buscar cualquier script grande con pinta de estado
            for s in soup.find_all("script"):
                txt = s.string or ""
                if len(txt) > 20000 and ('"price"' in txt or '"permalink"' in txt):
                    print(f"    Hay un <script> de {len(txt):,} chars con datos de avisos.")
                    break

        # --- Muestra alrededor de un precio ---
        m = re.search(r"(U\$S|USD|\$)\s?\d{2,3}[.,]\d{3}", html)
        if m:
            ini = max(0, m.start() - 600)
            print(f"\n  Contexto HTML alrededor del primer precio encontrado:")
            print("  " + "-" * 66)
            for linea in html[ini:m.end() + 300].split("\n")[:14]:
                print("   " + linea.strip()[:110])
            print("  " + "-" * 66)
        else:
            print("\n  >> No hay ningun precio en el HTML. "
                  "El listado NO viene renderizado en el servidor.")

        # Guardar para inspeccion manual
        path = utils.save_debug_html(html, portal, "diagnostico")
        print(f"\n  HTML completo guardado en:\n    {path}")

        if algun_match or hallado_json:
            print("\n  >> Esta URL SI trae avisos. Usá esta en el scraper.")
            break

    print("\n" + "=" * 70)
    if encontrado:
        print("RESULTADO: se encontraron avisos en al menos una URL/selector.")
    else:
        print("RESULTADO: ninguna URL devolvio avisos parseables.")
        print("Pasame el archivo HTML de data/debug/ y ajusto los selectores.")
    print("=" * 70)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Diagnostico de scrapers")
    p.add_argument("portal", choices=list(URLS.keys()))
    p.add_argument("--operacion", default="venta", choices=["venta", "alquiler"])
    args = p.parse_args()
    return diagnosticar(args.portal, args.operacion)


if __name__ == "__main__":
    sys.exit(main())
