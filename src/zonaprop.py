"""
zonaprop.py — Scraper de Zonaprop (CABA).

ADVERTENCIA IMPORTANTE PARA EL INFORME DEL TP
----------------------------------------------
Zonaprop es el portal con MAYOR volumen del mercado argentino, pero tambien el
mas protegido. Usa DataDome, un servicio anti-bot comercial que aplica:
  - Fingerprinting de TLS y de headers HTTP.
  - Challenge de JavaScript (la respuesta viene vacia sin un browser real).
  - Rate limiting agresivo por IP.
  - Captcha tras N requests sospechosos.

Resultado esperado: este scraper con `requests` funciona a veces (sobre todo en
las primeras paginas y desde IP residencial), y otras veces devuelve 403 o un
HTML de challenge. ESO ES EN SI MISMO UN HALLAZGO DOCUMENTABLE: la consigna pide
explicitamente reportar "los bloqueos o desafios tecnicos encontrados y como
fueron sorteados".

Estrategia implementada:
  1. Headers de navegador completos + sesion con cookies + Referer.
  2. Extraccion del JSON embebido (`window.__NEXT_DATA__` o el bloque de avisos),
     que es mucho mas rico que el DOM.
  3. Fallback al parseo de DOM con selectores `data-qa`.
  4. Deteccion explicita del challenge de DataDome, con mensaje claro.

Plan B si el bloqueo persiste (ver README): usar Playwright, que corre un Chromium
real y resuelve el challenge de JS automaticamente.
    pip install playwright && playwright install chromium
"""

from __future__ import annotations

# Permite ejecutar este archivo directamente (py src/zonaprop.py) ademas de
# importarlo como parte del paquete (from src import zonaprop).
if __package__ in (None, ""):
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    __package__ = "src"

import json
import re
from typing import Optional

from bs4 import BeautifulSoup

from .base import BaseScraper, first_text, first_elements
from . import utils


BASE = "https://www.zonaprop.com.ar"


class ZonapropScraper(BaseScraper):

    portal = "zonaprop"
    delay_base = 4.0          # deliberadamente lento: ir rapido garantiza el bloqueo
    delay_jitter = 3.0

    def __init__(self, tipo: str = "departamentos", zona: str = "capital-federal", **kwargs):
        self.tipo = tipo
        self.zona = zona
        kwargs.setdefault("delay", 4.0)
        super().__init__(**kwargs)

    def extra_headers(self) -> dict:
        return {
            "Referer": BASE + "/",
            "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        }

    # ------------------------------------------------------------------ URLs

    def build_url(self, pagina: int) -> str:
        # departamentos-venta-capital-federal.html
        # departamentos-venta-capital-federal-pagina-2.html
        slug = f"{self.tipo}-{self.operacion}-{self.zona}"
        if pagina > 1:
            slug += f"-pagina-{pagina}"
        return f"{BASE}/{slug}.html"

    # -------------------------------------------------------------- descarga

    def fetch_page(self, url: str):
        """Fetch con deteccion explicita del anti-bot."""
        # Primera visita: pasar por la home para levantar cookies de sesion
        if not self.session.cookies:
            try:
                self.session.get(BASE + "/", timeout=20)
                utils.polite_sleep(2.0, 1.0)
            except Exception:
                pass

        resp = self.session.get(url, timeout=30)

        if resp.status_code in (403, 405, 429):
            raise PermissionError(
                f"Zonaprop devolvio {resp.status_code} (DataDome). "
                "Documentá esto en el informe como bloqueo encontrado. "
                "Opciones: subir --delay a 8, esperar unos minutos, o usar Playwright "
                "(ver la funcion scrape_con_playwright en este archivo)."
            )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()

        html = resp.text

        # El challenge de DataDome devuelve 200 con una pagina casi vacia
        marcadores = ["datadome", "captcha-delivery", "geo.captcha", "interstitial"]
        if any(m in html.lower() for m in marcadores) and len(html) < 60000:
            if self.debug:
                utils.save_debug_html(html, self.portal, "challenge")
            raise PermissionError(
                "Zonaprop devolvio un challenge de DataDome (200 pero sin avisos). "
                "Documentá el bloqueo y usá Playwright como plan B."
            )

        if self.debug:
            p = utils.save_debug_html(html, self.portal, "page")
            print(f"    [debug] HTML guardado en {p}")

        return html

    # --------------------------------------------------------------- listado

    def parse_listado(self, html: str) -> list[dict]:
        registros = self._parse_desde_json(html)
        if registros:
            return registros
        return self._parse_desde_dom(BeautifulSoup(html, "html.parser"))

    # ------------------------------------------------------------ capa JSON

    def _parse_desde_json(self, html: str) -> list[dict]:
        """
        Zonaprop es una app Next.js: embebe todo el estado en __NEXT_DATA__.
        Si esta disponible, es la mejor fuente (trae lat/long y atributos tipados).
        """
        patrones = [
            r'<script[^>]*id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>',
            r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});',
            r'"listPostings"\s*:\s*(\[.*?\])\s*,\s*"',
        ]
        for pat in patrones:
            m = re.search(pat, html, flags=re.DOTALL)
            if not m:
                continue
            try:
                data = json.loads(m.group(1))
            except (ValueError, json.JSONDecodeError):
                continue

            postings = self._buscar_postings(data)
            if not postings:
                continue

            registros = []
            for it in postings:
                try:
                    rec = self._parse_item_json(it)
                    if rec:
                        registros.append(rec)
                except Exception:
                    self.errores += 1
            if registros:
                print(f"    [json] {len(registros)} avisos leidos del JSON embebido")
                return registros
        return []

    @staticmethod
    def _buscar_postings(data, profundidad: int = 0) -> list:
        """Busca recursivamente la lista de avisos dentro del JSON."""
        if profundidad > 7:
            return []
        if isinstance(data, list):
            if data and isinstance(data[0], dict) and any(
                k in data[0] for k in ("postingId", "postingLocation", "priceOperationTypes", "url")
            ):
                return data
            for x in data[:20]:
                r = ZonapropScraper._buscar_postings(x, profundidad + 1)
                if r:
                    return r
        elif isinstance(data, dict):
            for k in ("listPostings", "postings", "results", "listings"):
                if k in data:
                    r = ZonapropScraper._buscar_postings(data[k], profundidad + 1)
                    if r:
                        return r
            for v in list(data.values())[:40]:
                r = ZonapropScraper._buscar_postings(v, profundidad + 1)
                if r:
                    return r
        return []

    def _parse_item_json(self, it: dict) -> Optional[dict]:
        from .remax import dig  # reutilizamos el navegador de dicts anidados

        id_aviso = str(dig(it, "postingId") or dig(it, "id") or "")
        url_rel = dig(it, "url") or ""
        if not url_rel and not id_aviso:
            return None
        url = url_rel if str(url_rel).startswith("http") else f"{BASE}{url_rel}"

        # --- Precio: Zonaprop lo anida en priceOperationTypes ---
        precio_valor = precio_moneda = None
        precio_texto = ""
        ops = dig(it, "priceOperationTypes") or []
        if isinstance(ops, list):
            for op in ops:
                prices = dig(op, "prices") or []
                if isinstance(prices, list) and prices:
                    p0 = prices[0]
                    precio_valor = utils.to_float(dig(p0, "amount"))
                    cur = utils.norm_key(str(dig(p0, "currency") or ""))
                    precio_moneda = "USD" if "usd" in cur or "dolar" in cur else (
                        "ARS" if cur else None)
                    precio_texto = utils.clean_text(str(dig(p0, "formattedAmount") or ""))
                    break

        if precio_valor is None:
            precio_valor, precio_moneda = utils.parse_price(
                str(dig(it, "priceText") or dig(it, "price") or "")
            )

        expensas_valor = utils.to_float(dig(it, "expenses", "amount"))
        if expensas_valor is None:
            expensas_valor = utils.parse_expensas(str(dig(it, "expensesText") or ""))

        # --- Ubicacion ---
        loc = dig(it, "postingLocation") or {}
        direccion = utils.clean_text(
            dig(loc, "address", "name") or dig(it, "address") or ""
        )
        barrio_raw = utils.clean_text(
            dig(loc, "location", "name")
            or dig(loc, "locationLabel")
            or dig(loc, "name")
            or ""
        )
        lat = utils.to_float(dig(loc, "postingGeolocation", "geolocation", "latitude"))
        lon = utils.to_float(dig(loc, "postingGeolocation", "geolocation", "longitude"))
        if lat is not None and not (-35.1 < lat < -34.4):
            lat = lon = None

        calle, altura, piso = utils.parse_address(direccion)

        # --- Atributos principales ---
        features = dig(it, "mainFeatures") or {}
        def feat(*claves):
            if not isinstance(features, dict):
                return None
            for k, v in features.items():
                kl = utils.norm_key(str(k) + " " + str(dig(v, "label") or ""))
                if any(c in kl for c in claves):
                    return dig(v, "value") or v
            return None

        sup_total = utils.to_float(feat("superficie total", "cft100"))
        sup_cub = utils.to_float(feat("superficie cubierta", "cft101"))
        ambientes = utils.to_float(feat("ambiente", "cfT2"))
        dormitorios = utils.to_float(feat("dormitorio", "cfT1"))
        banos = utils.to_float(feat("bano", "baño", "cfT3"))
        cocheras = utils.to_float(feat("cochera", "garage", "cfT7"))
        antig_val = feat("antigued")

        titulo = utils.clean_text(dig(it, "postingTitle") or dig(it, "title") or "")
        descripcion = utils.clean_text(
            re.sub(r"<[^>]+>", " ", str(dig(it, "description") or ""))
        )

        texto_todo = f"{titulo} {descripcion[:400]} {direccion}"
        if sup_total is None:
            sup_total = utils.parse_superficie(texto_todo)
        if ambientes is None:
            ambientes = utils.parse_ambientes(texto_todo)

        antiguedad, a_estrenar = utils.parse_antiguedad(str(antig_val or "") or texto_todo)

        return utils.build_record(
            portal=self.portal,
            id_aviso=id_aviso or None,
            url=url,
            operacion=self.operacion,
            tipo_propiedad=utils.clean_text(str(dig(it, "realEstateType", "name") or self.tipo.rstrip("s"))),
            precio_valor=precio_valor,
            precio_moneda=precio_moneda,
            precio_texto=precio_texto,
            expensas_valor=expensas_valor,
            expensas_moneda="ARS" if expensas_valor else None,
            direccion=direccion,
            calle=calle,
            altura=altura,
            piso=piso,
            barrio=utils.normalizar_barrio(barrio_raw) or utils.normalizar_barrio(texto_todo),
            barrio_raw=barrio_raw,
            localidad="Capital Federal",
            latitud=lat,
            longitud=lon,
            sup_total_m2=sup_total,
            sup_cubierta_m2=sup_cub,
            ambientes=int(ambientes) if ambientes else None,
            dormitorios=int(dormitorios) if dormitorios is not None else None,
            banos=int(banos) if banos is not None else None,
            cocheras=int(cocheras) if cocheras is not None else None,
            antiguedad_anios=antiguedad,
            es_a_estrenar=a_estrenar,
            titulo=titulo,
            descripcion=descripcion,
        )

    # ------------------------------------------------------------- capa DOM

    def _parse_desde_dom(self, soup) -> list[dict]:
        items = first_elements(soup, [
            "div[data-qa='posting PROPERTY']",
            "[data-qa*='posting']",
            "div.postingCard",
            "[class*='postingCard']",
            "[class*='PostingCard']",
        ])

        if not items:
            print("    [warn] Sin cards en el DOM. Probablemente sea el challenge "
                  "de DataDome. Corré con --debug y revisá data/debug/.")
            return []

        registros = []
        for item in items:
            try:
                rec = self._parse_card_dom(item)
                if rec:
                    registros.append(rec)
            except Exception as e:
                self.errores += 1
                if self.debug:
                    print(f"    [card] error: {type(e).__name__}: {e}")
        return registros

    def _parse_card_dom(self, item) -> Optional[dict]:
        href = item.get("data-to-posting") or ""
        if not href:
            a = item.find("a", href=True)
            href = a["href"] if a else ""
        if not href:
            return None
        url = href if href.startswith("http") else BASE + href

        id_aviso = item.get("data-id") or item.get("data-posting-id")
        if not id_aviso:
            m = re.search(r"-(\d+)\.html", url)
            id_aviso = m.group(1) if m else None

        precio_texto = first_text(item, [
            "div[data-qa='POSTING_CARD_PRICE']",
            "[data-qa*='PRICE']", "[class*='price']", "[class*='Price']",
        ])
        precio_valor, precio_moneda = utils.parse_price(precio_texto)

        expensas_txt = first_text(item, [
            "div[data-qa='expensas']", "[data-qa*='xpensas']", "[class*='expensas']",
        ])
        expensas_valor = utils.parse_expensas(expensas_txt or precio_texto)

        direccion = first_text(item, [
            "div.postingAddress", "[class*='postingAddress']", "[class*='Address']",
        ])
        barrio_raw = first_text(item, [
            "h2[data-qa='POSTING_CARD_LOCATION']",
            "[data-qa*='LOCATION']", "[class*='postingLocation']",
        ])

        features_txt = first_text(item, [
            "h3[data-qa='POSTING_CARD_FEATURES']",
            "[data-qa*='FEATURES']", "[class*='postingMainFeatures']",
        ])
        descripcion = first_text(item, [
            "div[data-qa='POSTING_CARD_DESCRIPTION']", "[data-qa*='DESCRIPTION']",
        ])
        titulo = first_text(item, ["h2", "h3", "[class*='title']"])

        texto_todo = f"{titulo} {features_txt} {descripcion} {direccion} {barrio_raw}"
        antiguedad, a_estrenar = utils.parse_antiguedad(texto_todo)
        calle, altura, piso = utils.parse_address(direccion)

        return utils.build_record(
            portal=self.portal,
            id_aviso=str(id_aviso) if id_aviso else None,
            url=url,
            operacion=self.operacion,
            tipo_propiedad=self.tipo.rstrip("s"),
            precio_valor=precio_valor,
            precio_moneda=precio_moneda,
            precio_texto=precio_texto,
            expensas_valor=expensas_valor,
            expensas_moneda="ARS" if expensas_valor else None,
            direccion=direccion,
            calle=calle,
            altura=altura,
            piso=piso,
            barrio=utils.normalizar_barrio(barrio_raw) or utils.normalizar_barrio(texto_todo),
            barrio_raw=barrio_raw,
            localidad="Capital Federal",
            sup_total_m2=utils.parse_superficie(features_txt),
            ambientes=utils.parse_ambientes(features_txt or texto_todo),
            dormitorios=utils.parse_dormitorios(features_txt or texto_todo),
            banos=utils.parse_banos(features_txt or texto_todo),
            cocheras=utils.parse_cocheras(features_txt or texto_todo),
            antiguedad_anios=antiguedad,
            es_a_estrenar=a_estrenar,
            titulo=titulo,
            descripcion=descripcion,
            detalles=features_txt,
        )


# --------------------------------------------------------------------------
# Plan B: Playwright
# --------------------------------------------------------------------------

def scrape_con_playwright(operacion: str = "venta", max_pages: int = 3,
                          tipo: str = "departamentos", zona: str = "capital-federal",
                          headless: bool = False):
    """
    Plan B para cuando DataDome bloquea a requests.

    Levanta un Chromium real que ejecuta el JavaScript del challenge, y despues
    le pasamos el HTML ya renderizado al mismo parser de la clase.

    Requisitos:
        pip install playwright
        playwright install chromium

    Usar headless=False la primera vez: si aparece un captcha lo resolves a mano
    y la sesion queda validada por un rato.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "Playwright no esta instalado. Corré:\n"
            "    py -m pip install playwright\n"
            "    py -m playwright install chromium"
        )

    scraper = ZonapropScraper(operacion=operacion, max_pages=max_pages,
                              tipo=tipo, zona=zona)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(
            user_agent=utils.USER_AGENTS[0],
            locale="es-AR",
            viewport={"width": 1920, "height": 1080},
        )
        page = ctx.new_page()

        for pagina in range(1, max_pages + 1):
            url = scraper.build_url(pagina)
            print(f"[zonaprop/playwright] Pagina {pagina}: {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                # Esperar a que aparezcan las cards (o seguir igual si no llegan)
                try:
                    page.wait_for_selector("[data-qa*='posting']", timeout=20000)
                except Exception:
                    print("    [warn] No aparecieron cards. Puede haber captcha en pantalla.")
                page.wait_for_timeout(2500)

                html = page.content()
                nuevos = scraper.parse_listado(html)
                agregados = scraper._agregar(nuevos)
                print(f"    -> {agregados} avisos nuevos (total: {len(scraper.records)})")
            except Exception as e:
                print(f"    !! Error: {type(e).__name__}: {e}")

            page.wait_for_timeout(3000)

        browser.close()

    df = utils.to_dataframe(scraper.records)
    if not df.empty:
        path = utils.guardar(df, "zonaprop", operacion)
        print(f"[zonaprop/playwright] Guardado en: {path}")
    return df


def scrape(operacion: str = "venta", max_pages: int = 5, **kwargs):
    """Atajo funcional para usar desde un notebook."""
    return ZonapropScraper(operacion=operacion, max_pages=max_pages, **kwargs).run()


if __name__ == "__main__":
    # Corrida de prueba: py src/zonaprop.py
    # Para mas control usá el CLI: py run_scrapers.py --portal zonaprop --paginas 5
    df = scrape(operacion="venta", max_pages=3)
    utils.resumen(df)
