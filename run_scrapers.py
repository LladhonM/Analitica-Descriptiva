#!/usr/bin/env python
"""
run_scrapers.py — CLI para correr los scrapers desde la terminal.

Ejemplos
--------
    # Venta, 5 paginas
    py run_scrapers.py --operacion venta --paginas 5

    # Venta y alquiler (lo que necesitas para rentabilidad)
    py run_scrapers.py --operacion ambas --paginas 10

    # Con detalle (entra a cada ficha: mas lento, mas variables)
    py run_scrapers.py --operacion venta --paginas 5 --detalle

    # Diagnostico cuando algo no anda
    py diagnostico.py

    # Consolidar todo lo que haya en data/raw en un unico dataset maestro
    py run_scrapers.py --consolidar
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import traceback

import pandas as pd

from src import SCRAPERS, utils


PORTALES = list(SCRAPERS.keys())


def correr(portal: str, operacion: str, args) -> pd.DataFrame:
    """Instancia y ejecuta un scraper, capturando errores para no cortar el resto."""
    cls = SCRAPERS[portal]
    kwargs = dict(
        operacion=operacion,
        max_pages=args.paginas,
        con_detalle=args.detalle,
        debug=args.debug,
        checkpoint_cada=args.checkpoint,
    )
    if args.delay:
        kwargs["delay"] = args.delay

    try:
        return cls(**kwargs).run(fmt=args.formato)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(f"\n!! {portal}/{operacion} fallo: {type(e).__name__}: {e}")
        if args.debug:
            traceback.print_exc()
        return pd.DataFrame(columns=utils.SCHEMA)


def consolidar() -> pd.DataFrame:
    """
    Une todos los CSV de data/raw en un dataset maestro.
    Deduplica por (portal, id_aviso) quedandose con la captura mas reciente.
    """
    utils.ensure_dirs()
    archivos = [f for f in glob.glob(os.path.join(utils.RAW_DIR, "*.csv"))
                if not os.path.basename(f).startswith("_checkpoint")
                and not os.path.basename(f).startswith("dataset_maestro")]

    if not archivos:
        print("No hay archivos en data/raw/ para consolidar.")
        return pd.DataFrame(columns=utils.SCHEMA)

    print(f"Consolidando {len(archivos)} archivos...")
    dfs = []
    for f in archivos:
        try:
            dfs.append(pd.read_csv(f, encoding="utf-8-sig", low_memory=False))
            print(f"  + {os.path.basename(f)}")
        except Exception as e:
            print(f"  ! {os.path.basename(f)}: {e}")

    if not dfs:
        return pd.DataFrame(columns=utils.SCHEMA)

    df = pd.concat(dfs, ignore_index=True)
    antes = len(df)

    if "fecha_scraping" in df.columns:
        df = df.sort_values("fecha_scraping", ascending=False)
    df = df.drop_duplicates(subset=["portal", "id_aviso"], keep="first")
    df = df.drop_duplicates(subset=["url"], keep="first").reset_index(drop=True)

    print(f"\n{antes} filas -> {len(df)} unicas ({antes - len(df)} duplicados eliminados)")

    out = os.path.join(utils.RAW_DIR, "dataset_maestro.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"Dataset maestro: {out}")

    utils.resumen(df)

    print("\nAvisos por portal y operacion:")
    if {"portal", "operacion"}.issubset(df.columns):
        print(df.groupby(["portal", "operacion"]).size().to_string())

    return df


def main() -> int:
    p = argparse.ArgumentParser(
        description="Scrapers de portales inmobiliarios (CABA) — TP Integrador",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--portal", default="remax",
                   choices=PORTALES + ["todos"],
                   help="Portal a scrapear (default: remax)")
    p.add_argument("--operacion", default="venta",
                   choices=["venta", "alquiler", "ambas"],
                   help="Tipo de operacion (default: venta)")
    p.add_argument("--paginas", type=int, default=5,
                   help="Paginas de resultados a recorrer (default: 5)")
    p.add_argument("--detalle", action="store_true",
                   help="Entrar a cada ficha individual (mas lento, mas variables)")
    p.add_argument("--delay", type=float, default=None,
                   help="Segundos entre requests. Subilo si te bloquean (ej. 4)")
    p.add_argument("--checkpoint", type=int, default=3,
                   help="Guardar parcial cada N paginas (default: 3, 0 desactiva)")
    p.add_argument("--formato", default="csv", choices=["csv", "parquet"],
                   help="Formato de salida (default: csv)")
    p.add_argument("--debug", action="store_true",
                   help="Guarda el HTML crudo en data/debug/ y muestra tracebacks")
    p.add_argument("--consolidar", action="store_true",
                   help="Solo unir los CSV existentes de data/raw en un dataset maestro")

    args = p.parse_args()

    if args.consolidar:
        consolidar()
        return 0

    portales = PORTALES if args.portal == "todos" else [args.portal]
    operaciones = ["venta", "alquiler"] if args.operacion == "ambas" else [args.operacion]

    resultados: dict[str, pd.DataFrame] = {}

    try:
        for portal in portales:
            for op in operaciones:
                df = correr(portal, op, args)
                resultados[f"{portal}/{op}"] = df
                if not df.empty:
                    utils.resumen(df)
    except KeyboardInterrupt:
        print("\n\nInterrumpido por el usuario. Los checkpoints quedaron en data/raw/.")
        return 130

    # Resumen final de la corrida
    print(f"\n{'='*62}")
    print("RESUMEN DE LA CORRIDA")
    print(f"{'='*62}")
    total = 0
    for k, df in resultados.items():
        estado = "OK" if not df.empty else "SIN DATOS"
        print(f"  {k:<28} {len(df):>6} avisos   [{estado}]")
        total += len(df)
    print(f"  {'TOTAL':<28} {total:>6} avisos")

    if len(resultados) > 1 and total > 0:
        print("\nConsolidando en dataset maestro...")
        consolidar()

    if total == 0:
        print("\nNingun scraper devolvio datos. Probá:")
        print("  1. py run_scrapers.py --portal remax --paginas 2   (la fuente mas estable)")
        print("  2. Agregá --debug y revisá el HTML en data/debug/")
        print("  3. Subí el delay: --delay 4")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
