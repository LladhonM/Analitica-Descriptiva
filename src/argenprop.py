"""
argenprop.py — Scraper de Argenprop (version robustecida del script base).

Mejoras respecto del script provisto por la catedra:
  1. Precio parseado a valor numerico + moneda separada (antes: string "USD 145.000").
  2. Extrae m2, ambientes, dormitorios, banos, cocheras y antiguedad como numeros.
  3. Detecta el barrio y lo normaliza contra los 48 barrios oficiales de CABA.
  4. Soporta venta y alquiler (antes: solo venta hardcodeada).
  5. Selectores con fallback: si Argenprop cambia una clase, prueba alternativas
     en vez de devolver un dataset vacio.
  6. Reintentos con backoff, checkpoints y deduplicacion.
  7. Los `except: pass` silenciosos del original se reemplazan por manejo de
     errores que cuenta y reporta las fallas.

Argenprop es server-rendered (no necesita JavaScript), por eso requests+bs4 alcanza.
"""

from __future__ import annotations

# Permite ejecutar este archivo directamente (py src/argenprop.py) ademas de
# importarlo como parte del paquete (from src import argenprop).
if __package__ in (None, ""):
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    __package__ = "src"

import re
from bs4 import BeautifulSoup

from .base import BaseScraper, first_text, first_elements
from . import utils


BASE = "https://www.argenprop.com"


class ArgenpropScraper(BaseScraper):

    portal = "argenprop"
    delay_base = 1.5

    # 'departamentos' es el tipo mas relevante para inversion en renta.
    # Alternativas: 'casas', 'ph', 'inmuebles' (todos los tipos).
    def __init__(self, tipo: str = "departamentos", zona: str = "capital-federal", **kwargs):
        self.tipo = tipo
        self.zona = zona
        super().__init__(**kwargs)

    def extra_headers(self) -> dict:
        return {"Referer": BASE + "/"}

    # ------------------------------------------------------------------ URLs

    def build_url(self, pagina: int) -> str:
        # Estructura: /departamentos/venta/capital-federal?pagina-2
        base_url = f"{BASE}/{self.tipo}/{self.operacion}/{self.zona}"
        return base_url if pagina == 1 else f"{base_url}?pagina-{pagina}"

    # --------------------------------------------------------------- listado

    def parse_listado(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")

        items = first_elements(soup, [
            ("div", {"class": "listing__item"}),
            "div.listing__item",
            "div[class*='listing__item']",
            "a.card",
        ])

        if not items:
            print("    [warn] No se encontraron cards. Corré con --debug "
                  "y revisá el HTML en data/debug/ para actualizar selectores.")
            return []

        registros = []
        for item in items:
            try:
                rec = self._parse_card(item)
                if rec:
                    registros.append(rec)
            except Exception as e:
                self.errores += 1
                if self.debug:
                    print(f"    [card] error: {type(e).__name__}: {e}")
        return registros

    def _parse_card(self, item) -> dict | None:
        # --- URL del aviso ---
        link_tag = item.find("a", class_="card") or item.find("a", href=True)
        if not link_tag or not link_tag.get("href"):
            return None
        href = link_tag["href"]
        url = href if href.startswith("http") else BASE + href

        # El id del aviso viene al final del slug: ...--12345678
        m_id = re.search(r"--(\d+)$", url)
        id_aviso = m_id.group(1) if m_id else url.rstrip("/").split("/")[-1]

        # --- Precio y expensas ---
        precio_texto = first_text(item, [
            ("p", {"class": "card__price"}),
            "p.card__price",
            "[class*='card__price']",
            "[class*='price']",
        ])
        precio_valor, precio_moneda = utils.parse_price(precio_texto)
        expensas_valor = utils.parse_expensas(precio_texto)

        # Si no hay expensas en el bloque de precio, buscarlas aparte
        if expensas_valor is None:
            exp_txt = first_text(item, [
                "[class*='expenses']", "[class*='expensas']", "span.card__expenses",
            ])
            expensas_valor = utils.parse_expensas(exp_txt)

        # --- Direccion y ubicacion ---
        direccion = first_text(item, [
            ("p", {"class": "card__address"}),
            "p.card__address",
            "[class*='card__address']",
            "h2.card__address",
        ])
        # Argenprop suele poner el barrio en una linea aparte
        barrio_raw = first_text(item, [
            ("p", {"class": "card__title--primary"}),
            "p.card__title--primary",
            "[class*='card__title']",
            "[class*='location']",
        ])

        calle, altura, piso = utils.parse_address(direccion)

        # --- Features de la card (m2, ambientes, banos, cocheras) ---
        features_txt = first_text(item, [
            ("ul", {"class": "card__main-features"}),
            "ul.card__main-features",
            "[class*='main-features']",
            "[class*='card__features']",
        ])

        # Argenprop marca cada feature con un <span> con clase de icono.
        # Leerlos por separado es mas fiable que parsear el texto concatenado.
        feats = self._parse_feature_items(item)

        titulo = first_text(item, [
            ("h2", {"class": "card__title"}),
            "h2.card__title", "[class*='card__title']", "h2", "h3",
        ])

        texto_todo = f"{titulo} {features_txt} {direccion} {barrio_raw}"

        sup_total = feats.get("sup_total") or utils.parse_superficie(features_txt)
        ambientes = feats.get("ambientes") or utils.parse_ambientes(texto_todo)
        dormitorios = feats.get("dormitorios") or utils.parse_dormitorios(texto_todo)
        banos = feats.get("banos") or utils.parse_banos(texto_todo)
        cocheras = feats.get("cocheras") or utils.parse_cocheras(texto_todo)
        antiguedad, a_estrenar = utils.parse_antiguedad(texto_todo)

        barrio = utils.normalizar_barrio(barrio_raw) or utils.normalizar_barrio(direccion)

        return utils.build_record(
            portal=self.portal,
            id_aviso=id_aviso,
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
            piso=piso or feats.get("piso"),
            barrio=barrio,
            barrio_raw=barrio_raw,
            localidad="Capital Federal" if self.zona == "capital-federal" else self.zona,
            sup_total_m2=sup_total,
            sup_cubierta_m2=feats.get("sup_cubierta"),
            ambientes=ambientes,
            dormitorios=dormitorios,
            banos=banos,
            cocheras=cocheras,
            antiguedad_anios=antiguedad,
            es_a_estrenar=a_estrenar,
            titulo=titulo,
            detalles=features_txt,
        )

    @staticmethod
    def _parse_feature_items(item) -> dict:
        """
        Lee los <li> de features leyendo la clase del icono.
        Argenprop usa clases tipo 'icono-superficie_cubierta', 'icono-ambiente',
        'icono-dormitorio', 'icono-bano', 'icono-cochera', 'icono-antiguedad'.
        """
        out: dict = {}
        lis = first_elements(item, [
            "ul.card__main-features li",
            "[class*='main-features'] li",
            "ul[class*='features'] li",
            "li",
        ])

        for li in lis:
            texto = utils.clean_text(li.get_text(" ", strip=True))
            if not texto:
                continue

            # Clases de todos los descendientes: ahi vive el nombre del icono
            clases = " ".join(
                " ".join(el.get("class", [])) for el in li.find_all(True)
            ) + " " + " ".join(li.get("class", []))
            clases = utils.norm_key(clases)

            num_match = re.search(r"(\d[\d.,]*)", texto)
            num = utils.parse_number_ar(num_match.group(1)) if num_match else None

            if "superficie_cubierta" in clases or "cubierta" in clases:
                v = utils.parse_superficie(texto) or num
                if v:
                    out["sup_cubierta"] = v
            elif "superficie" in clases or "m2" in clases or "metros" in clases:
                v = utils.parse_superficie(texto) or num
                if v:
                    out["sup_total"] = v
            elif "ambiente" in clases:
                if num:
                    out["ambientes"] = int(num)
            elif "dormitorio" in clases or "cuarto" in clases:
                if num is not None:
                    out["dormitorios"] = int(num)
            elif "bano" in clases or "baño" in clases:
                if num is not None:
                    out["banos"] = int(num)
            elif "cochera" in clases or "garage" in clases:
                out["cocheras"] = int(num) if num is not None else 1
            elif "antiguedad" in clases:
                a, _ = utils.parse_antiguedad(texto)
                if a is not None:
                    out["antiguedad"] = a
            elif "piso" in clases:
                p = utils.parse_piso(texto)
                if p:
                    out["piso"] = p
            else:
                # Sin clase reconocible: inferir por el texto del propio <li>
                tl = utils.norm_key(texto)
                if "m²" in texto or "m2" in tl:
                    v = utils.parse_superficie(texto)
                    if v and "sup_total" not in out:
                        out["sup_total"] = v
                elif "amb" in tl and num:
                    out.setdefault("ambientes", int(num))
                elif "dorm" in tl and num is not None:
                    out.setdefault("dormitorios", int(num))
                elif "ban" in tl and num is not None:
                    out.setdefault("banos", int(num))

        # Si solo tenemos cubierta, usarla tambien como total
        if "sup_total" not in out and "sup_cubierta" in out:
            out["sup_total"] = out["sup_cubierta"]

        return out

    # ---------------------------------------------------------------- detalle

    def enrich_detail(self, rec: dict) -> dict:
        """
        Visita la ficha individual para traer la descripcion completa y los
        atributos que no aparecen en la card (antiguedad, orientacion, etc.).
        Duplica el tiempo de corrida, por eso es opcional (--detalle).
        """
        url = rec.get("url")
        if not url:
            return rec

        resp = self.session.get(url, timeout=25)
        if resp.status_code != 200:
            return rec

        soup = BeautifulSoup(resp.content, "html.parser")

        # --- Descripcion ---
        desc = first_text(soup, [
            ("section", {"class": "section-description"}),
            "section.section-description",
            "[class*='section-description']",
            "#descripcion", "[class*='description']",
        ])
        desc = desc.replace("Leer más Leer menos", "").replace("Leer mas Leer menos", "").strip()
        if desc:
            rec["descripcion"] = desc

        # --- Ficha tecnica: lista de "Etiqueta: Valor" ---
        ficha = {}
        for li in first_elements(soup, [
            "ul.property-features li",
            "[class*='property-features'] li",
            "#caracteristicas li",
            "[class*='features'] li",
            "section li",
        ]):
            txt = utils.clean_text(li.get_text(" ", strip=True))
            if not txt or len(txt) > 80:
                continue
            tl = utils.norm_key(txt)
            num_m = re.search(r"(\d[\d.,]*)", txt)
            num = utils.parse_number_ar(num_m.group(1)) if num_m else None

            if "antigued" in tl:
                a, e = utils.parse_antiguedad(txt)
                if a is not None:
                    ficha["antiguedad_anios"], ficha["es_a_estrenar"] = a, e
            elif "superficie total" in tl or "sup. total" in tl:
                v = utils.parse_superficie(txt)
                if v:
                    ficha["sup_total_m2"] = v
            elif "superficie cubierta" in tl or "sup. cubierta" in tl:
                v = utils.parse_superficie(txt)
                if v:
                    ficha["sup_cubierta_m2"] = v
            elif "ambiente" in tl and num:
                ficha["ambientes"] = int(num)
            elif "dormitorio" in tl and num is not None:
                ficha["dormitorios"] = int(num)
            elif "bano" in tl and num is not None:
                ficha["banos"] = int(num)
            elif "cochera" in tl:
                ficha["cocheras"] = int(num) if num is not None else 1
            elif "expensas" in tl:
                v = utils.parse_expensas(txt) or num
                if v:
                    ficha["expensas_valor"] = v
                    ficha["expensas_moneda"] = "ARS"
            elif tl.startswith("piso"):
                p = utils.parse_piso(txt)
                if p:
                    ficha["piso"] = p

        # Solo completa lo que falta; no pisa lo que ya vino de la card
        for k, v in ficha.items():
            if rec.get(k) in (None, "", 0) or k in ("antiguedad_anios", "sup_total_m2"):
                if v is not None:
                    rec[k] = v

        # --- Barrio desde el breadcrumb (mas confiable que el titulo) ---
        if not rec.get("barrio"):
            crumb = first_text(soup, [
                "[class*='breadcrumb']", "nav[aria-label*='readcrumb']", ".titlebar__address",
            ])
            rec["barrio"] = utils.normalizar_barrio(crumb)

        return rec


def scrape(operacion: str = "venta", max_pages: int = 5, **kwargs):
    """Atajo funcional para usar desde un notebook."""
    return ArgenpropScraper(operacion=operacion, max_pages=max_pages, **kwargs).run()


if __name__ == "__main__":
    # Corrida de prueba: py src/argenprop.py
    # Para mas control usá el CLI: py run_scrapers.py --portal argenprop --paginas 5
    df = scrape(operacion="venta", max_pages=3, con_detalle=False)
    utils.resumen(df)
