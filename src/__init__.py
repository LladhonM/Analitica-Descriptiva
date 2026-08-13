"""
Paquete de scrapers inmobiliarios — TP Integrador.

Fuente disponible:
    remax   API JSON publica. La mas confiable; trae lat/long.

El TP originalmente contemplaba 4 portales (argenprop, mercadolibre,
zonaprop ademas de remax), pero el equipo decidio analizar solo Remax:
las otras 3 fuentes se sacaron del paquete (ver el historial de git si
hace falta recuperarlas).

Uso rapido desde un notebook:

    from src import remax
    from src import utils

    df = remax.scrape(operacion="venta", max_pages=5)
    utils.resumen(df)
"""

from . import utils   # noqa: F401
from . import base    # noqa: F401
from . import remax   # noqa: F401

SCRAPERS = {
    "remax": remax.RemaxScraper,
}

__all__ = ["utils", "base", "remax", "SCRAPERS"]
