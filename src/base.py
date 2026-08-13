"""
base.py — Clase base compartida por todos los scrapers.

Resuelve lo que es comun a las 4 fuentes:
  - Ciclo de paginacion con corte automatico.
  - Deduplicacion por URL/id.
  - Checkpoints: guarda parcial cada N paginas para no perder trabajo
    si el proceso se corta o el portal empieza a bloquear.
  - Manejo de errores por aviso (un aviso roto no tumba la corrida).
  - Logging del progreso.
"""

from __future__ import annotations

import os
import traceback
from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd

from . import utils


class BaseScraper(ABC):
    """
    Scraper base. Cada portal implementa:
      - build_url(operacion, pagina) -> str
      - parse_listado(html_o_json, operacion) -> list[dict]

    Y opcionalmente:
      - fetch_page(url) -> str | dict   (si necesita POST o headers especiales)
      - enrich_detail(rec) -> dict      (si visita la ficha individual)
    """

    portal: str = "base"
    # Delay entre requests. Subirlo si el portal empieza a devolver 403/429.
    delay_base: float = 1.5
    delay_jitter: float = 1.2

    def __init__(
        self,
        operacion: str = "venta",
        max_pages: int = 5,
        con_detalle: bool = False,
        checkpoint_cada: int = 3,
        debug: bool = False,
        delay: Optional[float] = None,
    ):
        """
        operacion       : 'venta' o 'alquiler'
        max_pages       : cuantas paginas de resultados recorrer
        con_detalle     : si True, entra a cada ficha individual (mas lento,
                          pero trae descripcion completa y mas atributos)
        checkpoint_cada : guarda un parcial cada N paginas
        debug           : guarda el HTML crudo en data/debug/ para inspeccionar
        delay           : override del delay entre requests
        """
        if operacion not in ("venta", "alquiler"):
            raise ValueError("operacion debe ser 'venta' o 'alquiler'")

        self.operacion = operacion
        self.max_pages = max_pages
        self.con_detalle = con_detalle
        self.checkpoint_cada = checkpoint_cada
        self.debug = debug
        if delay is not None:
            self.delay_base = delay

        self.session = utils.build_session(self.extra_headers())
        self.records: list[dict] = []
        self.seen: set[str] = set()
        self.errores: int = 0

    # ---------------------------------------------------------------- hooks

    def extra_headers(self) -> dict:
        """Headers adicionales especificos del portal."""
        return {}

    @abstractmethod
    def build_url(self, pagina: int) -> str:
        """URL de la pagina N de resultados para la operacion configurada."""

    @abstractmethod
    def parse_listado(self, contenido) -> list[dict]:
        """Parsea una pagina de resultados y devuelve una lista de registros."""

    def fetch_page(self, url: str):
        """
        Descarga una pagina. Por defecto GET devolviendo texto HTML.
        Los portales con API JSON sobreescriben este metodo.
        """
        resp = self.session.get(url, timeout=25)

        if resp.status_code == 403:
            raise PermissionError(
                f"403 Forbidden en {url}. El portal esta bloqueando el scraper. "
                "Probá bajar la velocidad (--delay 4) o usar Playwright."
            )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()

        html = resp.text
        if self.debug:
            path = utils.save_debug_html(html, self.portal, f"p{url[-12:]}")
            print(f"    [debug] HTML guardado en {path}")
        return html

    def enrich_detail(self, rec: dict) -> dict:
        """Enriquecimiento opcional visitando la ficha individual."""
        return rec

    # ------------------------------------------------------------- ejecucion

    def run(self, guardar: bool = True, fmt: str = "csv") -> pd.DataFrame:
        """Ejecuta la corrida completa y devuelve el DataFrame resultante."""
        print(f"\n{'#'*62}")
        print(f"# {self.portal.upper()}  |  operacion: {self.operacion}  |  "
              f"paginas: {self.max_pages}  |  detalle: {self.con_detalle}")
        print(f"# {utils.info_backend()}")
        print(f"{'#'*62}")

        paginas_vacias = 0

        for pagina in range(1, self.max_pages + 1):
            url = self.build_url(pagina)
            print(f"\n[{self.portal}] Pagina {pagina}/{self.max_pages}")
            print(f"  URL: {url}")

            try:
                contenido = self.fetch_page(url)
            except PermissionError as e:
                print(f"  !! {e}")
                print("  Corto la paginacion y guardo lo obtenido hasta ahora.")
                break
            except Exception as e:
                print(f"  !! Error descargando la pagina: {type(e).__name__}: {e}")
                self.errores += 1
                paginas_vacias += 1
                if paginas_vacias >= 2:
                    print("  Dos fallos seguidos. Corto.")
                    break
                utils.polite_sleep(self.delay_base * 2, self.delay_jitter)
                continue

            if contenido is None:
                print("  Pagina inexistente (404). Fin de la paginacion.")
                break

            try:
                nuevos = self.parse_listado(contenido)
            except Exception as e:
                print(f"  !! Error parseando: {type(e).__name__}: {e}")
                if self.debug:
                    traceback.print_exc()
                self.errores += 1
                nuevos = []

            if not nuevos:
                paginas_vacias += 1
                print("  Sin avisos en esta pagina.")
                if paginas_vacias >= 2:
                    print("  Dos paginas vacias seguidas. Fin de la paginacion.")
                    break
                utils.polite_sleep(self.delay_base, self.delay_jitter)
                continue

            paginas_vacias = 0
            agregados = self._agregar(nuevos)
            print(f"  -> {agregados} avisos nuevos (total acumulado: {len(self.records)})")

            if guardar and self.checkpoint_cada and pagina % self.checkpoint_cada == 0:
                self._checkpoint()

            utils.polite_sleep(self.delay_base, self.delay_jitter)

        df = utils.to_dataframe(self.records)

        print(f"\n[{self.portal}] Corrida finalizada. "
              f"{len(df)} avisos unicos, {self.errores} errores.")

        if guardar and not df.empty:
            path = utils.guardar(df, self.portal, self.operacion, fmt=fmt)
            print(f"[{self.portal}] Guardado en: {path}")
            self._limpiar_checkpoint()

        return df

    # ---------------------------------------------------------------- helpers

    def _agregar(self, nuevos: list[dict]) -> int:
        """Agrega registros nuevos, deduplicando y enriqueciendo."""
        agregados = 0
        for rec in nuevos:
            clave = rec.get("url") or rec.get("id_aviso")
            if not clave or clave in self.seen:
                continue
            self.seen.add(clave)

            if self.con_detalle:
                try:
                    rec = self.enrich_detail(rec)
                    utils.polite_sleep(self.delay_base * 0.7, self.delay_jitter * 0.5)
                except Exception as e:
                    self.errores += 1
                    if self.debug:
                        print(f"    [detalle] fallo en {clave}: {e}")

            rec.setdefault("portal", self.portal)
            rec.setdefault("operacion", self.operacion)
            rec["fecha_scraping"] = utils.now_iso()

            self.records.append(utils.enriquecer_registro(rec))
            agregados += 1
        return agregados

    def _checkpoint_path(self) -> str:
        utils.ensure_dirs()
        return os.path.join(utils.RAW_DIR, f"_checkpoint_{self.portal}_{self.operacion}.csv")

    def _checkpoint(self) -> None:
        """Guardado parcial: si la corrida se corta, no se pierde nada."""
        try:
            df = utils.to_dataframe(self.records)
            if not df.empty:
                df.to_csv(self._checkpoint_path(), index=False, encoding="utf-8-sig")
                print(f"  [checkpoint] {len(df)} registros guardados")
        except Exception as e:
            print(f"  [checkpoint] fallo: {e}")

    def _limpiar_checkpoint(self) -> None:
        try:
            p = self._checkpoint_path()
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def first_text(soup, selectores: list, attr: Optional[str] = None) -> str:
    """
    Devuelve el texto (o atributo) del primer selector que matchee.

    Clave para la robustez: los portales cambian los nombres de clase seguido.
    En vez de un selector unico que rompe todo, probamos varios en orden.
    """
    for sel in selectores:
        try:
            if isinstance(sel, tuple):          # ('div', {'class': 'x'})
                el = soup.find(sel[0], sel[1])
            else:                                # selector CSS
                el = soup.select_one(sel)
            if el:
                if attr:
                    val = el.get(attr)
                    if val:
                        return utils.clean_text(val)
                else:
                    txt = utils.clean_text(el.get_text(" ", strip=True))
                    if txt:
                        return txt
        except Exception:
            continue
    return ""


def first_elements(soup, selectores: list) -> list:
    """Devuelve la lista de elementos del primer selector que produzca resultados."""
    for sel in selectores:
        try:
            if isinstance(sel, tuple):
                els = soup.find_all(sel[0], sel[1])
            else:
                els = soup.select(sel)
            if els:
                return els
        except Exception:
            continue
    return []
