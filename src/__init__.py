"""
Paquete de scrapers inmobiliarios — TP Integrador.

Fuentes disponibles:
    argenprop     HTML server-rendered. Base provista por la catedra, robustecida.
    remax         API JSON publica. La mas confiable; trae lat/long.
    mercadolibre  HTML + JSON embebido.
    zonaprop      HTML + JSON embebido. Protegido con DataDome (ver README).

Uso rapido desde un notebook:

    from src import argenprop, remax, mercadolibre, zonaprop
    from src import utils

    df = argenprop.scrape(operacion="venta", max_pages=5)
    utils.resumen(df)
"""

from . import utils          # noqa: F401
from . import base           # noqa: F401
from . import argenprop      # noqa: F401
from . import remax          # noqa: F401
from . import mercadolibre   # noqa: F401
from . import zonaprop       # noqa: F401

SCRAPERS = {
    "argenprop": argenprop.ArgenpropScraper,
    "remax": remax.RemaxScraper,
    "mercadolibre": mercadolibre.MercadoLibreScraper,
    "zonaprop": zonaprop.ZonapropScraper,
}

__all__ = ["utils", "base", "argenprop", "remax", "mercadolibre", "zonaprop", "SCRAPERS"]
