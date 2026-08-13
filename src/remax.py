"""
remax.py — Scraper de RE/MAX Argentina via su API JSON publica.

Es la fuente MAS CONFIABLE de las cuatro: el sitio de Remax es una SPA en Angular
que consume su propio backend REST. En vez de scrapear HTML renderizado, le
pegamos directo al endpoint que usa el frontend.

Ventajas frente al scraping de HTML:
  - Datos ya estructurados y tipados (no hay que parsear texto).
  - Trae latitud/longitud -> permite el join espacial con comunas, subte,
    comisarias, etc. que pide la Fase 3 del TP.
  - Sin captchas ni renderizado JavaScript.
  - Paginacion limpia por pageSize/page.

Contra: el catalogo de Remax es mas chico que Zonaprop/Argenprop y esta sesgado
a inmuebles de gama media-alta con exclusividad de la red.

NOTA: al ser una API no documentada, los nombres de campo pueden cambiar sin aviso.
El parser lee cada campo de forma defensiva con `dig()`.
"""

from __future__ import annotations

# Permite ejecutar este archivo directamente (py src/remax.py) ademas de
# importarlo como parte del paquete (from src import remax).
if __package__ in (None, ""):
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    __package__ = "src"

from typing import Any, Optional

from .base import BaseScraper
from . import utils


API = "https://api.redremax.com/remaxweb-ar/api/listings/findAll"

# IDs internos de operacion en la API de Remax
OPERACION_ID = {"venta": 1, "alquiler": 2}


def dig(d: Any, *keys, default=None):
    """
    Navega un dict anidado sin romper si falta un nivel.
      dig(obj, "geo", "location", "coordinates", 0)
    Acepta claves de dict e indices de lista.
    """
    cur = d
    for k in keys:
        if cur is None:
            return default
        try:
            if isinstance(k, int):
                cur = cur[k] if isinstance(cur, (list, tuple)) and len(cur) > k else None
            elif isinstance(cur, dict):
                cur = cur.get(k)
            else:
                cur = getattr(cur, k, None)
        except (KeyError, IndexError, TypeError):
            return default
    return cur if cur is not None else default


class RemaxScraper(BaseScraper):

    portal = "remax"
    delay_base = 1.0          # es una API, tolera un ritmo mas alto
    page_size = 60            # maximo estable observado

    def __init__(self, zona: str = "capital-federal", **kwargs):
        self.zona = zona
        super().__init__(**kwargs)

    def extra_headers(self) -> dict:
        return {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.remax.com.ar",
            "Referer": "https://www.remax.com.ar/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        }

    # ------------------------------------------------------------------ URLs

    def build_url(self, pagina: int) -> str:
        # La API pagina desde 0
        page = pagina - 1
        op_id = OPERACION_ID.get(self.operacion, 1)
        return (
            f"{API}"
            f"?page={page}"
            f"&pageSize={self.page_size}"
            f"&sort=-priceUsd"
            f"&in:operationId={op_id}"
            f"&in:typeId=1,2,3,4,5,6,7,8,9,10,11,12,13"   # todos los tipos de inmueble
            f"&locations=in::::14028:::"                    # 14028 = Capital Federal
            f"&filterCount=0"
            f"&viewMode=list"
        )

    def fetch_page(self, url: str):
        """Sobrescribe el fetch para devolver JSON en vez de HTML."""
        resp = self.session.get(url, timeout=30)

        if resp.status_code == 403:
            raise PermissionError(
                "403 en la API de Remax. Puede que hayan cerrado el endpoint "
                "o cambiado los headers requeridos."
            )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()

        try:
            return resp.json()
        except ValueError:
            print("    [warn] La API no devolvio JSON valido. "
                  "Revisá si cambio la URL del endpoint.")
            if self.debug:
                utils.save_debug_html(resp.text[:50000], self.portal, "resp")
            return None

    # --------------------------------------------------------------- listado

    def parse_listado(self, payload) -> list[dict]:
        if not payload:
            return []

        # La API devolvio la lista bajo distintas claves segun la version
        items = (
            dig(payload, "data", "data")
            or dig(payload, "data")
            or dig(payload, "content")
            or (payload if isinstance(payload, list) else [])
        )
        if not isinstance(items, list):
            print(f"    [warn] Estructura inesperada: {type(items)}")
            return []

        registros = []
        for it in items:
            try:
                rec = self._parse_item(it)
                if rec:
                    registros.append(rec)
            except Exception as e:
                self.errores += 1
                if self.debug:
                    print(f"    [item] error: {type(e).__name__}: {e}")
        return registros

    def _parse_item(self, it: dict) -> Optional[dict]:
        if not isinstance(it, dict):
            return None

        id_aviso = str(dig(it, "id") or dig(it, "listingId") or "")
        slug = dig(it, "slug") or ""
        url = f"https://www.remax.com.ar/listings/{slug}" if slug else \
              (f"https://www.remax.com.ar/listings/{id_aviso}" if id_aviso else None)
        if not url:
            return None

        # --- Precio ---
        # La API expone price + currency, y ademas priceUsd ya convertido
        precio = utils.to_float(dig(it, "price"))
        moneda_raw = (dig(it, "currency", "value")
                      or dig(it, "currency", "code")
                      or dig(it, "currency")
                      or "")
        moneda_raw = utils.norm_key(str(moneda_raw))
        if "usd" in moneda_raw or "dolar" in moneda_raw:
            moneda = "USD"
        elif "ars" in moneda_raw or "peso" in moneda_raw or "$" in moneda_raw:
            moneda = "ARS"
        else:
            moneda = "USD" if utils.to_float(dig(it, "priceUsd")) == precio else None

        if precio is None:
            precio = utils.to_float(dig(it, "priceUsd"))
            moneda = moneda or "USD"

        expensas = utils.to_float(dig(it, "expenses") or dig(it, "expensas"))

        # --- Ubicacion ---
        direccion = utils.clean_text(dig(it, "displayAddress") or dig(it, "address") or "")
        barrio_raw = utils.clean_text(
            dig(it, "geo", "name")
            or dig(it, "geoLabel")
            or dig(it, "neighbourhood")
            or dig(it, "location", "name")
            or ""
        )
        # Remax devuelve el label completo tipo "Belgrano, Capital Federal, ..."
        if not barrio_raw:
            barrio_raw = utils.clean_text(dig(it, "geo", "label") or "")

        calle, altura, piso = utils.parse_address(direccion)

        # Coordenadas: la API las trae en GeoJSON [lon, lat]
        coords = dig(it, "geo", "location", "coordinates") or dig(it, "coordinates")
        lat = lon = None
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lon, lat = utils.to_float(coords[0]), utils.to_float(coords[1])
        else:
            lat = utils.to_float(dig(it, "latitude") or dig(it, "lat"))
            lon = utils.to_float(dig(it, "longitude") or dig(it, "lng"))
        # Sanity check: CABA esta cerca de (-34.6, -58.4)
        if lat is not None and not (-35.1 < lat < -34.4):
            lat = lon = None

        # --- Metricas ---
        sup_total = utils.to_float(dig(it, "dimensionTotalBuilt") or dig(it, "totalSurface"))
        sup_cub = utils.to_float(dig(it, "dimensionCovered") or dig(it, "coveredSurface"))
        terreno = utils.to_float(dig(it, "dimensionLand"))
        if not sup_total:
            sup_total = sup_cub or terreno

        ambientes = dig(it, "totalRooms") or dig(it, "rooms") or dig(it, "ambientes")
        dormitorios = dig(it, "bedrooms") or dig(it, "dormitorios")
        banos = dig(it, "bathrooms") or dig(it, "banos")
        cocheras = dig(it, "parkingSpaces") or dig(it, "garages")

        antiguedad = dig(it, "yearBuilt") or dig(it, "antiquity")
        antiguedad_anios, a_estrenar = None, None
        if antiguedad is not None:
            av = utils.to_float(antiguedad)
            if av is not None:
                if av > 1800:   # vino como anio de construccion
                    from datetime import datetime
                    antiguedad_anios = datetime.now().year - int(av)
                elif 0 <= av <= 200:
                    antiguedad_anios = int(av)
                if antiguedad_anios is not None:
                    a_estrenar = 1 if antiguedad_anios <= 0 else 0

        titulo = utils.clean_text(dig(it, "title") or "")
        descripcion = utils.clean_text(dig(it, "description") or "")

        tipo = utils.clean_text(
            dig(it, "type", "value") or dig(it, "propertyType") or dig(it, "type") or ""
        )

        # Features vienen como lista de objetos {name: ...}
        feats = dig(it, "features") or []
        feats_txt = " ".join(
            str(dig(f, "name") or dig(f, "value") or f) for f in feats
        ) if isinstance(feats, list) else ""

        barrio = utils.normalizar_barrio(barrio_raw) or utils.normalizar_barrio(direccion)

        return utils.build_record(
            portal=self.portal,
            id_aviso=id_aviso,
            url=url,
            operacion=self.operacion,
            tipo_propiedad=tipo,
            precio_valor=precio,
            precio_moneda=moneda,
            precio_texto=f"{moneda or ''} {precio or ''}".strip(),
            expensas_valor=expensas,
            expensas_moneda="ARS" if expensas else None,
            direccion=direccion,
            calle=calle,
            altura=altura,
            piso=piso,
            barrio=barrio,
            barrio_raw=barrio_raw,
            localidad="Capital Federal",
            latitud=lat,
            longitud=lon,
            sup_total_m2=sup_total,
            sup_cubierta_m2=sup_cub,
            ambientes=int(ambientes) if utils.to_float(ambientes) else None,
            dormitorios=int(dormitorios) if dormitorios is not None and utils.to_float(dormitorios) is not None else None,
            banos=int(banos) if banos is not None and utils.to_float(banos) is not None else None,
            cocheras=int(cocheras) if cocheras is not None and utils.to_float(cocheras) is not None else None,
            antiguedad_anios=antiguedad_anios,
            es_a_estrenar=a_estrenar,
            titulo=titulo,
            descripcion=descripcion,
            detalles=utils.clean_text(feats_txt),
        )


def scrape(operacion: str = "venta", max_pages: int = 5, **kwargs):
    """Atajo funcional para usar desde un notebook."""
    return RemaxScraper(operacion=operacion, max_pages=max_pages, **kwargs).run()


if __name__ == "__main__":
    # Corrida de prueba: py src/remax.py
    # Para mas control usá el CLI: py run_scrapers.py --portal remax --paginas 5
    df = scrape(operacion="venta", max_pages=3)
    utils.resumen(df)
