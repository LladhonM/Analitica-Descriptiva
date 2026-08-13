"""
mercadolibre.py — Scraper de MercadoLibre Inmuebles (CABA).

MELI renderiza los resultados en el servidor, asi que requests+bs4 alcanza.
Ademas embebe un JSON con el estado inicial de la pagina; cuando esta
disponible lo usamos porque es mucho mas confiable que los selectores CSS.

Estrategia en dos capas:
  1. Intentar extraer el JSON embebido (__PRELOADED_STATE__ / melidata).
  2. Si no aparece, caer al parseo del DOM con selectores `data-testid` y
     `ui-search-*`, que son los mas estables del sitio.

Paginacion: MELI usa offset en la URL (_Desde_49, _Desde_97...), de a 48 items.

Nota sobre la API oficial: MercadoLibre cerro el acceso anonimo a
api.mercadolibre.com/sites/MLA/search — hoy exige un access_token de aplicacion
registrada. Si el grupo tramita las credenciales en developers.mercadolibre.com.ar,
conviene migrar a la API. Mientras tanto, scraping de HTML.
"""

from __future__ import annotations

# Permite ejecutar este archivo directamente (py src/mercadolibre.py) ademas de
# importarlo como parte del paquete (from src import mercadolibre).
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


BASE = "https://inmuebles.mercadolibre.com.ar"
ITEMS_POR_PAGINA = 48

# MELI cambia la estructura de URL cada tanto y no todas las variantes devuelven
# el listado renderizado. Probamos en orden hasta que una traiga avisos.
# {base} {tipo} {op} {zona} {desde}
PATRONES_URL = [
    "https://inmuebles.mercadolibre.com.ar/{tipo}/{op}/{zona}/{desde}",
    "https://inmuebles.mercadolibre.com.ar/{tipo}/{op}/{zona}/{desde}_NoIndex_True",
    "https://listado.mercadolibre.com.ar/inmuebles/{tipo}/{op}/{zona}/{desde}",
    "https://inmuebles.mercadolibre.com.ar/{tipo}/{op}/{desde}",
]

# Marcadores que indican que el HTML si trae el listado
MARCADORES_LISTADO = [
    "ui-search-layout__item",
    "poly-card",
    "ui-search-result",
    '"permalink"',
]


class MercadoLibreScraper(BaseScraper):

    portal = "mercadolibre"
    delay_base = 2.0          # MELI es sensible al ritmo de requests

    def __init__(self, tipo: str = "departamentos", zona: str = "capital-federal", **kwargs):
        self.tipo = tipo
        self.zona = zona
        # Indice del patron de URL que funciono. Se fija en la primera pagina
        # para no reintentar todas las variantes en cada iteracion.
        self._patron_ok: int | None = None
        super().__init__(**kwargs)

    def extra_headers(self) -> dict:
        return {"Referer": BASE + "/"}

    # ------------------------------------------------------------------ URLs

    def _urls_candidatas(self, pagina: int) -> list[str]:
        """Todas las variantes de URL para una pagina dada."""
        desde = "" if pagina == 1 else f"_Desde_{(pagina - 1) * ITEMS_POR_PAGINA + 1}"
        urls = [
            p.format(tipo=self.tipo, op=self.operacion, zona=self.zona, desde=desde)
            for p in PATRONES_URL
        ]
        # Si ya sabemos cual anda, esa va primero
        if self._patron_ok is not None:
            urls = [urls[self._patron_ok]] + [u for i, u in enumerate(urls) if i != self._patron_ok]
        return urls

    def build_url(self, pagina: int) -> str:
        """URL principal (la que se muestra en el log)."""
        return self._urls_candidatas(pagina)[0]

    def fetch_page(self, url: str):
        """
        Prueba las variantes de URL hasta que una devuelva el listado.
        MELI a veces redirige o sirve una pagina sin resultados segun la forma
        exacta de la URL, asi que no alcanza con una sola.
        """
        # Reconstruimos la pagina a partir de la URL que armo el ciclo base
        m = re.search(r"_Desde_(\d+)", url)
        pagina = (int(m.group(1)) - 1) // ITEMS_POR_PAGINA + 1 if m else 1

        ultimo_html = None
        for i, candidata in enumerate(self._urls_candidatas(pagina)):
            try:
                resp = self.session.get(candidata, timeout=30, allow_redirects=True)
            except Exception as e:
                if self.debug:
                    print(f"    [url {i}] error de red: {e}")
                continue

            if resp.status_code in (403, 429):
                raise PermissionError(
                    f"MercadoLibre devolvio {resp.status_code}. Subí el delay (--delay 4) "
                    "o esperá unos minutos."
                )
            if resp.status_code != 200:
                if self.debug:
                    print(f"    [url {i}] status {resp.status_code}: {candidata}")
                continue

            html = resp.text
            ultimo_html = html

            if any(mk in html for mk in MARCADORES_LISTADO):
                if self._patron_ok is None:
                    self._patron_ok = i
                    if i > 0:
                        print(f"    [url] usando variante {i}: {candidata}")
                if self.debug:
                    p = utils.save_debug_html(html, self.portal, f"p{pagina}")
                    print(f"    [debug] HTML guardado en {p}")
                return html

            if self.debug:
                print(f"    [url {i}] sin marcadores de listado ({len(html):,} bytes)")

        # Ninguna variante trajo el listado
        if ultimo_html is not None:
            p = utils.save_debug_html(ultimo_html, self.portal, "sin_listado")
            print(f"    [warn] Ninguna URL trajo avisos. HTML guardado en:\n           {p}")
            print("    [warn] Corré:  py diagnostico.py mercadolibre")
        return ultimo_html

    # --------------------------------------------------------------- listado

    def parse_listado(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")

        # Capa 1: JSON embebido
        registros = self._parse_desde_json(html)
        if registros:
            return registros

        # Capa 2: DOM
        return self._parse_desde_dom(soup)

    # ------------------------------------------------------------ capa JSON

    def _parse_desde_json(self, html: str) -> list[dict]:
        """Busca el estado inicial que MELI embebe en un <script>."""
        patrones = [
            r'window\.__PRELOADED_STATE__\s*=\s*(\{.*?\});\s*</script>',
            r'"results"\s*:\s*(\[\{.*?\}\])\s*,\s*"',
        ]
        for pat in patrones:
            m = re.search(pat, html, flags=re.DOTALL)
            if not m:
                continue
            try:
                data = json.loads(m.group(1))
            except (ValueError, json.JSONDecodeError):
                continue

            resultados = self._buscar_results(data)
            if not resultados:
                continue

            registros = []
            for it in resultados:
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
    def _buscar_results(data, profundidad: int = 0) -> list:
        """Busca recursivamente una lista de avisos dentro del JSON."""
        if profundidad > 6:
            return []
        if isinstance(data, list):
            if data and isinstance(data[0], dict) and any(
                k in data[0] for k in ("permalink", "title", "price")
            ):
                return data
            for x in data[:20]:
                r = MercadoLibreScraper._buscar_results(x, profundidad + 1)
                if r:
                    return r
        elif isinstance(data, dict):
            for k in ("results", "items", "polycards", "listings"):
                if k in data:
                    r = MercadoLibreScraper._buscar_results(data[k], profundidad + 1)
                    if r:
                        return r
            for v in list(data.values())[:30]:
                r = MercadoLibreScraper._buscar_results(v, profundidad + 1)
                if r:
                    return r
        return []

    def _parse_item_json(self, it: dict) -> Optional[dict]:
        url = it.get("permalink") or it.get("url")
        if not url:
            return None
        url = url.split("#")[0].split("?")[0]

        id_aviso = it.get("id") or self._id_desde_url(url)

        precio = utils.to_float(it.get("price"))
        moneda_raw = utils.norm_key(str(it.get("currency_id") or it.get("currency") or ""))
        moneda = "USD" if "usd" in moneda_raw else ("ARS" if "ars" in moneda_raw else None)

        titulo = utils.clean_text(it.get("title") or "")

        # Ubicacion
        loc = it.get("location") or it.get("address") or {}
        barrio_raw = utils.clean_text(
            (loc.get("neighborhood") or {}).get("name") if isinstance(loc.get("neighborhood"), dict)
            else loc.get("neighborhood")
            or loc.get("address_line")
            or loc.get("city_name")
            or ""
        )
        direccion = utils.clean_text(loc.get("address_line") or "")

        # Atributos estructurados
        attrs = {}
        for a in (it.get("attributes") or []):
            if isinstance(a, dict):
                nombre = utils.norm_key(a.get("name") or a.get("id") or "")
                valor = a.get("value_name") or a.get("value_struct") or a.get("values")
                if nombre:
                    attrs[nombre] = valor

        def attr(*claves):
            for c in claves:
                for k, v in attrs.items():
                    if c in k:
                        return v
            return None

        sup_total = utils.parse_superficie(str(attr("superficie total", "total area") or ""))
        sup_cub = utils.parse_superficie(str(attr("superficie cubierta", "covered area") or ""))
        ambientes = utils.parse_ambientes(str(attr("ambiente", "rooms") or "") + " amb")
        dormitorios = utils.to_float(str(attr("dormitorio", "bedrooms") or "").split()[0]
                                     if attr("dormitorio", "bedrooms") else None)
        banos = utils.to_float(str(attr("bano", "baño", "bathrooms") or "").split()[0]
                               if attr("bano", "baño", "bathrooms") else None)
        antig_txt = str(attr("antigued", "age") or "")
        antiguedad, a_estrenar = utils.parse_antiguedad(antig_txt)

        texto_fallback = f"{titulo} {' '.join(str(v) for v in attrs.values())}"
        if sup_total is None:
            sup_total = utils.parse_superficie(texto_fallback)
        if ambientes is None:
            ambientes = utils.parse_ambientes(texto_fallback)

        calle, altura, piso = utils.parse_address(direccion or titulo)

        return utils.build_record(
            portal=self.portal,
            id_aviso=str(id_aviso) if id_aviso else None,
            url=url,
            operacion=self.operacion,
            tipo_propiedad=self.tipo.rstrip("s"),
            precio_valor=precio,
            precio_moneda=moneda,
            precio_texto=f"{moneda or ''} {precio or ''}".strip(),
            direccion=direccion,
            calle=calle,
            altura=altura,
            piso=piso,
            barrio=utils.normalizar_barrio(barrio_raw) or utils.normalizar_barrio(titulo),
            barrio_raw=barrio_raw,
            localidad="Capital Federal",
            latitud=utils.to_float(loc.get("latitude")),
            longitud=utils.to_float(loc.get("longitude")),
            sup_total_m2=sup_total,
            sup_cubierta_m2=sup_cub,
            ambientes=int(ambientes) if ambientes else None,
            dormitorios=int(dormitorios) if dormitorios is not None else None,
            banos=int(banos) if banos is not None else None,
            antiguedad_anios=antiguedad,
            es_a_estrenar=a_estrenar,
            titulo=titulo,
            detalles=utils.clean_text(" | ".join(f"{k}: {v}" for k, v in list(attrs.items())[:25])),
        )

    # ------------------------------------------------------------- capa DOM

    def _parse_desde_dom(self, soup) -> list[dict]:
        items = first_elements(soup, [
            "li.ui-search-layout__item",
            "div.ui-search-result__wrapper",
            "[class*='ui-search-layout__item']",
            "div.poly-card",
            "[class*='poly-card']",
            "ol.ui-search-layout > li",
            "section.ui-search-results li",
            "div.andes-card[class*='search']",
            "[class*='search-result']",
        ])

        if not items:
            print("    [warn] No se encontraron cards en el DOM.")
            print("    [warn] Corré:  py diagnostico.py mercadolibre")
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

        # Diagnostico util: encontramos tarjetas pero ninguna parseo
        if items and not registros:
            print(f"    [warn] Se encontraron {len(items)} tarjetas pero ninguna "
                  "pudo parsearse (cambiaron los selectores internos).")
            print("    [warn] Corré:  py diagnostico.py mercadolibre")

        return registros

    def _parse_card_dom(self, item) -> Optional[dict]:
        link = item.find("a", href=True)
        if not link:
            return None
        url = link["href"].split("#")[0].split("?")[0]
        if not url.startswith("http"):
            return None

        # --- Precio ---
        # MELI separa simbolo y parte entera en spans distintos
        simbolo = first_text(item, [
            "span.andes-money-amount__currency-symbol",
            "[class*='currency-symbol']",
        ])
        fraccion = first_text(item, [
            "span.andes-money-amount__fraction",
            "[class*='money-amount__fraction']",
        ])
        precio_texto = utils.clean_text(f"{simbolo} {fraccion}")
        if not fraccion:
            precio_texto = first_text(item, [
                "[class*='andes-money-amount']", "[class*='price']", ".poly-price__current",
            ])

        precio_valor, precio_moneda = utils.parse_price(precio_texto)
        if precio_moneda is None and simbolo:
            sl = utils.norm_key(simbolo)
            precio_moneda = "USD" if "u$s" in sl or "usd" in sl else ("ARS" if "$" in sl else None)

        titulo = first_text(item, [
            "h2.ui-search-item__title", "a.poly-component__title",
            "[class*='item__title']", "[class*='component__title']", "h2", "h3",
        ])

        # --- Ubicacion ---
        barrio_raw = first_text(item, [
            "span.ui-search-item__location", "span.poly-component__location",
            "[class*='item__location']", "[class*='component__location']",
        ])
        direccion = first_text(item, [
            "[class*='item__subtitle']", "[class*='component__headline']",
        ])

        # --- Atributos (m2, ambientes, dormitorios, banos) ---
        attrs_txt = first_text(item, [
            "ul.ui-search-card-attributes", "ul.poly-attributes-list",
            "[class*='card-attributes']", "[class*='attributes-list']",
        ])
        # Leer cada <li> por separado da mejor precision
        lis = first_elements(item, [
            "ul.ui-search-card-attributes li", "ul.poly-attributes-list li",
            "[class*='attributes'] li",
        ])
        partes = [utils.clean_text(li.get_text(" ", strip=True)) for li in lis]
        attrs_full = " | ".join(p for p in partes if p) or attrs_txt

        sup_total = ambientes = dormitorios = banos = None
        for p in partes or [attrs_txt]:
            pl = utils.norm_key(p)
            if ("m²" in p or "m2" in pl) and sup_total is None:
                sup_total = utils.parse_superficie(p)
            elif "amb" in pl and ambientes is None:
                ambientes = utils.parse_ambientes(p)
            elif ("dormitorio" in pl or "habitacion" in pl) and dormitorios is None:
                dormitorios = utils.parse_dormitorios(p)
            elif "ban" in pl and banos is None:
                banos = utils.parse_banos(p)

        texto_todo = f"{titulo} {attrs_full} {barrio_raw} {direccion}"
        sup_total = sup_total or utils.parse_superficie(texto_todo)
        ambientes = ambientes or utils.parse_ambientes(texto_todo)
        dormitorios = dormitorios if dormitorios is not None else utils.parse_dormitorios(texto_todo)
        banos = banos if banos is not None else utils.parse_banos(texto_todo)
        antiguedad, a_estrenar = utils.parse_antiguedad(texto_todo)

        calle, altura, piso = utils.parse_address(direccion or titulo)

        return utils.build_record(
            portal=self.portal,
            id_aviso=self._id_desde_url(url),
            url=url,
            operacion=self.operacion,
            tipo_propiedad=self.tipo.rstrip("s"),
            precio_valor=precio_valor,
            precio_moneda=precio_moneda,
            precio_texto=precio_texto,
            direccion=direccion,
            calle=calle,
            altura=altura,
            piso=piso,
            barrio=utils.normalizar_barrio(barrio_raw) or utils.normalizar_barrio(texto_todo),
            barrio_raw=barrio_raw,
            localidad="Capital Federal",
            sup_total_m2=sup_total,
            ambientes=ambientes,
            dormitorios=dormitorios,
            banos=banos,
            antiguedad_anios=antiguedad,
            es_a_estrenar=a_estrenar,
            titulo=titulo,
            detalles=attrs_full,
        )

    @staticmethod
    def _id_desde_url(url: str) -> Optional[str]:
        """Extrae el MLA-XXXXXXXX de la URL del aviso."""
        m = re.search(r"(MLA-?\d+)", url, flags=re.IGNORECASE)
        return m.group(1).upper().replace("-", "") if m else None

    # ---------------------------------------------------------------- detalle

    def enrich_detail(self, rec: dict) -> dict:
        """Visita la ficha para traer descripcion y la tabla de especificaciones."""
        url = rec.get("url")
        if not url:
            return rec

        resp = self.session.get(url, timeout=25)
        if resp.status_code != 200:
            return rec

        soup = BeautifulSoup(resp.content, "html.parser")

        desc = first_text(soup, [
            "p.ui-pdp-description__content", "[class*='description__content']",
            "[class*='ui-pdp-description']",
        ])
        if desc:
            rec["descripcion"] = desc

        # Tabla de especificaciones tecnicas (filas clave/valor)
        specs = {}
        for tr in soup.select("tr.andes-table__row, [class*='specs'] tr"):
            th = tr.find("th")
            td = tr.find("td")
            if th and td:
                k = utils.norm_key(th.get_text(" ", strip=True))
                v = utils.clean_text(td.get_text(" ", strip=True))
                if k and v:
                    specs[k] = v

        for k, v in specs.items():
            if "superficie total" in k and not rec.get("sup_total_m2"):
                rec["sup_total_m2"] = utils.parse_superficie(v)
            elif "superficie cubierta" in k and not rec.get("sup_cubierta_m2"):
                rec["sup_cubierta_m2"] = utils.parse_superficie(v)
            elif "ambiente" in k and not rec.get("ambientes"):
                rec["ambientes"] = utils.parse_ambientes(v + " amb")
            elif "dormitorio" in k and rec.get("dormitorios") is None:
                rec["dormitorios"] = utils.parse_dormitorios(v + " dorm")
            elif "bano" in k and rec.get("banos") is None:
                rec["banos"] = utils.parse_banos(v + " banos")
            elif "cochera" in k and rec.get("cocheras") is None:
                rec["cocheras"] = utils.parse_cocheras(v + " cochera")
            elif "antigued" in k and rec.get("antiguedad_anios") is None:
                a, e = utils.parse_antiguedad(v)
                rec["antiguedad_anios"], rec["es_a_estrenar"] = a, e
            elif "expensas" in k and not rec.get("expensas_valor"):
                rec["expensas_valor"] = utils.parse_number_ar(v)
                rec["expensas_moneda"] = "ARS"

        if specs:
            rec["detalles"] = utils.clean_text(
                (rec.get("detalles") or "") + " | " +
                " | ".join(f"{k}: {v}" for k, v in specs.items())
            )

        return rec


def scrape(operacion: str = "venta", max_pages: int = 5, **kwargs):
    """Atajo funcional para usar desde un notebook."""
    return MercadoLibreScraper(operacion=operacion, max_pages=max_pages, **kwargs).run()


if __name__ == "__main__":
    # Corrida de prueba: py src/mercadolibre.py
    # Para mas control usá el CLI: py run_scrapers.py --portal mercadolibre --paginas 5
    df = scrape(operacion="venta", max_pages=3)
    utils.resumen(df)
