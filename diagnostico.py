#!/usr/bin/env python
"""
diagnostico.py — Averigua POR QUE Remax devuelve 0 avisos.

Descarga una pagina de la API y reporta:
  - Status HTTP, tamano de la respuesta y redirecciones.
  - Si la respuesta es JSON valido.
  - Cuantos avisos trae y los campos del primero.

Uso:
    py diagnostico.py
    py diagnostico.py --operacion alquiler
"""

from __future__ import annotations

import argparse
import sys

from src import utils


# CF = Capital Federal (ver NOTA 2 en src/remax.py sobre el cambio de formato)
URL = (
    "https://api-ar.redremax.com/remaxweb-ar/api/listings/findAll"
    "?page=0&pageSize=20&filterCount=1&locations=in:CF::::&viewMode=list"
)


def diagnosticar(operacion: str) -> int:
    print(utils.info_backend())
    session = utils.build_session()
    print(f"Backend en uso: {session.backend}")

    print("\n" + "=" * 70)
    print(f"URL: {URL}")
    print("=" * 70)

    try:
        resp = session.get(URL, timeout=30, allow_redirects=True)
    except Exception as e:
        print(f"  ERROR de red: {type(e).__name__}: {e}")
        return 1

    print(f"  Status HTTP    : {resp.status_code}")
    print(f"  Tamano         : {len(resp.content):,} bytes")
    print(f"  Content-Type   : {resp.headers.get('Content-Type', '?')}")
    if resp.history:
        print(f"  Redirecciones  : {len(resp.history)}")
        print(f"  URL final      : {resp.url}")

    if resp.status_code != 200:
        print(f"\n  >> La API respondio {resp.status_code}, no 200.")
        if resp.status_code in (403, 429):
            print("  >> Es un BLOQUEO. Proba subir el delay o esperar unos minutos.")
        return 1

    try:
        data = resp.json()
    except ValueError:
        print("\n  >> La respuesta dice ser JSON pero no parsea. "
              "Puede que el endpoint haya cambiado de nuevo.")
        return 1

    bloque = data.get("data") if isinstance(data, dict) else None
    items = bloque.get("data") if isinstance(bloque, dict) else None
    total = bloque.get("totalItems") if isinstance(bloque, dict) else None

    print(f"\n  Respuesta JSON OK")
    print(f"  totalItems (todas las operaciones mezcladas): {total}")
    print(f"  Avisos en esta pagina: {len(items) if isinstance(items, list) else '?'}")

    if not isinstance(items, list) or not items:
        print("\n  >> No vinieron avisos. Revisa si `locations=in:CF::::` sigue "
              "siendo el filtro valido (la API no esta documentada y puede "
              "volver a cambiar de formato).")
        return 1

    # El filtro operationId esta roto server-side (ver NOTA 2 en src/remax.py):
    # la API devuelve venta/alquiler/temporal mezclados sin importar lo que se
    # pida, por eso el filtro real de operacion ocurre del lado del cliente.
    valor_op = "sale" if operacion == "venta" else "rent"
    matching = [it for it in items if (it.get("operation") or {}).get("value") == valor_op]
    print(f"  De esos, con operation.value == {valor_op!r}: {len(matching)}")
    if not matching:
        print(f"\n  >> Ningun aviso de esta pagina es de '{operacion}'. Es normal en "
              "paginas puntuales (el filtro operationId no funciona server-side, "
              "se filtra del lado del cliente); si pasa en muchas paginas seguidas, "
              "proba con mas --paginas.")

    print(f"\n  Campos del primer aviso ({len(items[0])}):")
    for k in sorted(items[0].keys()):
        v = str(items[0][k])[:60]
        print(f"      {k:<20} = {v}")

    print("\n" + "=" * 70)
    print("RESULTADO: la API respondio con avisos.")
    print("=" * 70)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Diagnostico del scraper de Remax")
    p.add_argument("--operacion", default="venta", choices=["venta", "alquiler"])
    args = p.parse_args()
    return diagnosticar(args.operacion)


if __name__ == "__main__":
    sys.exit(main())
