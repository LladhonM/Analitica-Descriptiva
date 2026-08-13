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

NOTA 2 (2026): Remax migro el backend. Tres cosas cambiaron y rompieron el
scraper original:
  1. El host paso de api.redremax.com a api-ar.redremax.com (el viejo dominio
     ya ni siquiera resuelve por DNS).
  2. El filtro `in:operationId=` quedado sin efecto: la API lo ignora y
     siempre devuelve una mezcla de venta/alquiler/temporal. Ahora se filtra
     por `operacion` del lado del cliente usando el campo `operation.value`
     de cada item ("sale" / "rent" / "temporal").
  3. El filtro geografico `locations=` ya no acepta el id numerico viejo
     (14028 para Capital Federal): ahora usa el codigo de provincia/estado
     ("CF"). Como referencia extra, tambien se filtra por `addressInfo`
     ("Barrio, Capital Federal") por si el filtro de la API vuelve a cambiar.
  4. El listado (`findAll`) dejo de traer dormitorios, cocheras, antiguedad,
     descripcion y features -- esos campos se movieron a la ficha individual
     (`findBySlug/{slug}`). Por eso `--detalle` ahora importa mas que antes:
     sin el, quedan varias columnas vacias.
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


API = "https://api-ar.redremax.com/remaxweb-ar/api/listings/findAll"
API_DETALLE = "https://api-ar.redremax.com/remaxweb-ar/api/listings/findBySlug/{slug}"

# Valor de operation.value en la respuesta de la API, por operacion.
# El filtro `in:operationId=` del lado del servidor ya no funciona (ver
# NOTA 2 en el docstring del modulo), asi que esto se usa para filtrar
# del lado del cliente.
OPERACION_VALUE = {"venta": "sale", "alquiler": "rent"}


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
        # La API pagina desde 0.
        # No se filtra por tipo de inmueble (typeId): los ids cambiaron y hoy
        # llegan hasta el 18+, filtrar con el rango viejo (1-13) excluiria
        # tipos nuevos como terrenos_y_lotes. Tampoco se filtra por operacion
        # aca (ver OPERACION_VALUE): se hace del lado del cliente en
        # _parse_item.
        # Importante: NO ordenar por `-priceUsd`. Como el filtro operationId
        # esta roto, ordenar por precio deja los alquileres (precio mensual,
        # ordenes de magnitud mas bajo que una venta) enterrados a miles de
        # paginas de distancia -- con esa orden, --operacion alquiler no
        # trae una sola fila en las primeras paginas. Sin sort, la mezcla
        # venta/alquiler queda pareja pagina a pagina.
        page = pagina - 1
        return (
            f"{API}"
            f"?page={page}"
            f"&pageSize={self.page_size}"
            f"&filterCount=1"
            f"&locations=in:CF::::"                         # CF = Capital Federal
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

    def _extraer_antiguedad(self, year_built) -> tuple[Optional[int], Optional[int]]:
        """De un anio de construccion (ej. 1998) deriva antiguedad_anios y a_estrenar."""
        av = utils.to_float(year_built)
        if av is None:
            return None, None
        antiguedad_anios = None
        if av > 1800:   # vino como anio de construccion
            from datetime import datetime
            antiguedad_anios = datetime.now().year - int(av)
        elif 0 <= av <= 200:
            antiguedad_anios = int(av)
        if antiguedad_anios is None:
            return None, None
        return antiguedad_anios, (1 if antiguedad_anios <= 0 else 0)

    def _parse_item(self, it: dict) -> Optional[dict]:
        if not isinstance(it, dict):
            return None

        # El filtro `in:operationId=` del lado del servidor esta roto (ver
        # NOTA 2 en el docstring): findAll devuelve venta/alquiler/temporal
        # mezclados sin importar el filtro pedido. Se filtra aca.
        op_value = utils.norm_key(str(dig(it, "operation", "value") or ""))
        if op_value != OPERACION_VALUE.get(self.operacion):
            return None

        id_aviso = str(dig(it, "id") or dig(it, "internalId") or "")
        slug = dig(it, "slug") or ""
        url = f"https://www.remax.com.ar/listings/{slug}" if slug else \
              (f"https://www.remax.com.ar/listings/{id_aviso}" if id_aviso else None)
        if not url:
            return None

        # --- Ubicacion ---
        # El filtro `locations=in:CF::::` (ver NOTA 2) ya restringe a Capital
        # Federal, pero se re-valida por si ese filtro vuelve a cambiar de
        # formato sin que nadie lo note.
        address_info = utils.clean_text(dig(it, "addressInfo") or "")
        if "capital federal" not in utils.norm_key(address_info):
            return None
        # addressInfo viene como "Barrio, Capital Federal" o
        # "Sub-barrio, Barrio, Capital Federal": el primer segmento es el
        # mas especifico.
        barrio_raw = address_info.split(",")[0].strip() if address_info else ""

        # --- Precio ---
        precio = utils.to_float(dig(it, "price"))
        moneda_raw = utils.norm_key(str(dig(it, "currency", "value") or ""))
        if "usd" in moneda_raw or "dolar" in moneda_raw:
            moneda = "USD"
        elif "ars" in moneda_raw or "peso" in moneda_raw:
            moneda = "ARS"
        else:
            moneda = None

        expensas = utils.to_float(dig(it, "expensesPrice"))
        expensas_moneda_raw = utils.norm_key(str(dig(it, "expensesCurrency", "value") or ""))
        if "ars" in expensas_moneda_raw or "peso" in expensas_moneda_raw:
            expensas_moneda = "ARS"
        elif "usd" in expensas_moneda_raw or "dolar" in expensas_moneda_raw:
            expensas_moneda = "USD"
        else:
            expensas_moneda = "ARS" if expensas else None

        direccion = utils.clean_text(dig(it, "displayAddress") or "")
        calle, altura, piso = utils.parse_address(direccion)

        # Coordenadas: la API las trae en GeoJSON [lon, lat]
        coords = dig(it, "location", "coordinates")
        lat = lon = None
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lon, lat = utils.to_float(coords[0]), utils.to_float(coords[1])
        # Sanity check: CABA esta cerca de (-34.6, -58.4)
        if lat is not None and not (-35.1 < lat < -34.4):
            lat = lon = None

        # --- Metricas ---
        sup_total = utils.to_float(dig(it, "dimensionTotalBuilt") or dig(it, "dimensionLand"))
        sup_cub = utils.to_float(dig(it, "dimensionCovered"))

        ambientes = dig(it, "totalRooms")
        banos = dig(it, "bathrooms")
        # dormitorios/cocheras/antiguedad/descripcion/features ya no vienen
        # en el listado (findAll): se movieron a la ficha individual. Se
        # completan en enrich_detail() cuando se corre con --detalle.
        dormitorios = None
        cocheras = None
        antiguedad_anios, a_estrenar = None, None

        titulo = utils.clean_text(dig(it, "title") or "")
        descripcion = ""

        tipo = utils.clean_text(dig(it, "type", "value") or "")

        feats_txt = ""

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
            expensas_moneda=expensas_moneda,
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

    # ---------------------------------------------------------------- detalle

    def enrich_detail(self, rec: dict) -> dict:
        """
        Visita la ficha individual (findBySlug) para completar dormitorios,
        cocheras, antiguedad, descripcion y amenities: campos que el listado
        (findAll) ya no trae (ver NOTA 2 en el docstring del modulo).
        Solo se ejecuta con --detalle, porque duplica el tiempo de corrida.
        """
        url = rec.get("url")
        if not url:
            return rec

        slug = url.rstrip("/").rsplit("/", 1)[-1]
        detalle_url = API_DETALLE.format(slug=slug)

        resp = self.session.get(detalle_url, timeout=25)
        if resp.status_code != 200:
            return rec
        try:
            payload = resp.json()
        except ValueError:
            return rec

        d = dig(payload, "data")
        if not isinstance(d, dict):
            return rec

        rec["dormitorios"] = utils.to_float(dig(d, "bedrooms"))
        if rec["dormitorios"] is not None:
            rec["dormitorios"] = int(rec["dormitorios"])

        cocheras = utils.to_float(dig(d, "parkingSpaces"))
        rec["cocheras"] = int(cocheras) if cocheras is not None else None

        antiguedad_anios, a_estrenar = self._extraer_antiguedad(dig(d, "yearBuilt"))
        rec["antiguedad_anios"] = antiguedad_anios
        rec["es_a_estrenar"] = a_estrenar

        descripcion = utils.clean_text(dig(d, "description") or "")
        if descripcion:
            rec["descripcion"] = descripcion

        # Los features vienen como lista de objetos {value, lang, category, ...}
        feats = dig(d, "features") or []
        if isinstance(feats, list) and feats:
            feats_txt = " ".join(
                str(dig(f, "value") or dig(f, "lang") or "") for f in feats
            )
            rec["detalles"] = utils.clean_text(feats_txt)

        # geo.citie es un barrio limpio (sin el "Capital Federal" pegado al
        # lado): si el barrio no se pudo resolver desde addressInfo, probar
        # con este.
        if not rec.get("barrio"):
            citie = utils.clean_text(dig(d, "geo", "citie") or "")
            if citie:
                rec["barrio"] = utils.normalizar_barrio(citie)

        return rec


def scrape(operacion: str = "venta", max_pages: int = 5, **kwargs):
    """Atajo funcional para usar desde un notebook."""
    return RemaxScraper(operacion=operacion, max_pages=max_pages, **kwargs).run()


if __name__ == "__main__":
    # Corrida de prueba: py src/remax.py
    # Para mas control usá el CLI: py run_scrapers.py --portal remax --paginas 5
    df = scrape(operacion="venta", max_pages=3)
    utils.resumen(df)
