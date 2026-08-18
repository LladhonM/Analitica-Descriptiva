#!/usr/bin/env python
"""
simular.py — Simula una propiedad y estima cuanto se alquilaria.

Usa el modelo entrenado por modelo_alquiler.py. Requiere haber corrido antes:
    py limpieza.py --tc 1500
    py modelo_alquiler.py

MODO INTERACTIVO (te va preguntando):
    py simular.py

MODO DIRECTO (todo por argumentos):
    py simular.py --barrio palermo --m2 65 --ambientes 3 --antiguedad 20
    py simular.py --barrio caballito --m2 45 --ambientes 2 --cochera --balcon
    py simular.py --barrio belgrano --m2 80 --ambientes 3 --venta 250000

Si pasas --venta, ademas calcula la rentabilidad de esa operacion.
"""

from __future__ import annotations

import argparse
import os
import sys

import joblib
import numpy as np
import pandas as pd


RUTA_MODELO = "data/processed/modelo_alquiler.joblib"

# Caracteristicas que se pueden activar. Cada una es una dummy del modelo.
CARACTERISTICAS = {
    "balcon": "balcón",
    "cochera_txt": "cochera",
    "amenities": "amenities",
    "ascensor": "ascensor",
    "aire_acondicionado": "aire acondicionado",
    "pileta": "pileta",
    "amoblado": "amoblado",
    "parrilla": "parrilla",
    "seguridad": "seguridad 24h",
    "baulera": "baulera",
    "luminoso": "luminoso",
    "patio_jardin": "patio o jardín",
    "a_reciclar": "a reciclar",
    "reciclado": "reciclado",
}


# ==========================================================================
# CARGA DEL MODELO
# ==========================================================================

def cargar(ruta: str) -> dict:
    if not os.path.exists(ruta):
        print(f"No encuentro el modelo en: {ruta}")
        print()
        print("Corré primero:")
        print("    py limpieza.py --tc 1500")
        print("    py modelo_alquiler.py")
        sys.exit(1)

    m = joblib.load(ruta)
    print(f"Modelo cargado — entrenado el {m['entrenado']} "
          f"con {m['n_entrenamiento']:,} alquileres")
    return m


# ==========================================================================
# PREDICCION
# ==========================================================================

def estimar(m: dict, prop: dict) -> dict:
    """
    Estima el alquiler de una propiedad.

    Devuelve el valor puntual, la banda de error segun el tamaño, y una
    comparacion contra la mediana del barrio como control de sanidad.
    """
    fila = {
        "log_sup": np.log(prop["m2"]) if prop["m2"] > 0 else np.nan,
        "ambientes": prop.get("ambientes"),
        "banos": prop.get("banos"),
        "dormitorios": prop.get("dormitorios"),
        "antiguedad_anios": prop.get("antiguedad"),
        "barrio": prop["barrio"],
        "tipo_familia": prop.get("tipo", "departamento"),
    }
    for c in m["dummies"]:
        fila[c] = int(prop.get(c, 0))

    X = pd.DataFrame([fila])[m["numericas"] + m["categoricas"] + m["dummies"]]
    alquiler = float(np.exp(m["pipeline"].predict(X)[0]) * m["smearing"])

    # Banda de error segun el segmento de superficie al que pertenece
    seg = pd.cut([prop["m2"]], m["bins_sup"], labels=m["lab_sup"])[0]
    err = float(m["error_por_segmento"].get(seg, np.median(list(m["error_por_segmento"]))))

    # Control: cuanto daria usando solo la mediana del barrio por m2
    med_m2 = m["alquiler_m2_por_barrio"].get(prop["barrio"])
    ref = med_m2 * prop["m2"] if med_m2 else None

    n_barrio = m["cobertura_barrio"].get(prop["barrio"], 0)

    return {
        "alquiler": alquiler,
        "alquiler_m2": alquiler / prop["m2"],
        "error": err,
        "min": alquiler * (1 - err),
        "max": alquiler * (1 + err),
        "referencia_barrio": ref,
        "n_alq_barrio": n_barrio,
        "confiable": n_barrio >= m["min_alq_barrio"],
        "segmento": seg,
    }


def mostrar(prop: dict, r: dict, venta: float | None,
            vacancia: float, gastos: float) -> None:
    print()
    print("=" * 62)
    print("PROPIEDAD SIMULADA")
    print("=" * 62)
    print(f"  Barrio        : {prop['barrio']}")
    print(f"  Superficie    : {prop['m2']:.0f} m²")
    if prop.get("ambientes"):
        print(f"  Ambientes     : {prop['ambientes']:.0f}")
    if prop.get("dormitorios") is not None:
        print(f"  Dormitorios   : {prop['dormitorios']:.0f}")
    if prop.get("banos"):
        print(f"  Baños         : {prop['banos']:.0f}")
    if prop.get("antiguedad") is not None:
        a = prop["antiguedad"]
        print(f"  Antigüedad    : {'a estrenar' if a == 0 else f'{a:.0f} años'}")
    print(f"  Tipo          : {prop.get('tipo', 'departamento')}")

    activas = [n for c, n in CARACTERISTICAS.items() if prop.get(c)]
    print(f"  Características: {', '.join(activas) if activas else '(ninguna)'}")

    print()
    print("=" * 62)
    print("ALQUILER ESTIMADO")
    print("=" * 62)
    print(f"  USD {r['alquiler']:,.0f} por mes")
    print(f"  Rango probable : USD {r['min']:,.0f} a {r['max']:,.0f}  "
          f"(±{r['error']*100:.0f}%)")
    print(f"  Por m²         : USD {r['alquiler_m2']:.1f} /m²/mes")

    if r["referencia_barrio"]:
        dif = (r["alquiler"] / r["referencia_barrio"] - 1) * 100
        print()
        print(f"  Referencia: la mediana de {prop['barrio']} para esa superficie")
        print(f"  daria USD {r['referencia_barrio']:,.0f}. El modelo estima "
              f"{dif:+.0f}% respecto de eso,")
        print("  por las caracteristicas particulares de esta propiedad.")

    print()
    if not r["confiable"]:
        print(f"  AVISO: {prop['barrio']} tiene solo {r['n_alq_barrio']} alquileres")
        print("  en la muestra. La estimacion extrapola desde otros barrios;")
        print("  tomala como orientativa.")
    else:
        print(f"  Basado en {r['n_alq_barrio']} alquileres de {prop['barrio']} "
              f"(segmento {r['segmento']} m²).")

    if venta:
        bruta = r["alquiler"] * 12 / venta * 100
        neta = bruta * (1 - vacancia / 100) * (1 - gastos / 100)
        print()
        print("=" * 62)
        print("RENTABILIDAD DE LA OPERACION")
        print("=" * 62)
        print(f"  Precio de venta      : USD {venta:,.0f}")
        print(f"  Alquiler anual       : USD {r['alquiler']*12:,.0f}")
        print()
        print(f"  Rentabilidad bruta   : {bruta:.2f}%")
        print(f"     rango             : {bruta*(1-r['error']):.2f}% a "
              f"{bruta*(1+r['error']):.2f}%")
        print(f"  Rentabilidad neta    : {neta:.2f}%   "
              f"(vacancia {vacancia:.0f}%, gastos {gastos:.0f}%)")
        print(f"  Repago               : {venta/r['alquiler']:,.0f} meses "
              f"({venta/r['alquiler']/12:.1f} años)")
        print()
        ref = 6.99  # mediana del dataset
        if bruta > ref * 1.15:
            print(f"  Rinde por encima de la mediana del mercado ({ref:.1f}%).")
        elif bruta < ref * 0.85:
            print(f"  Rinde por debajo de la mediana del mercado ({ref:.1f}%).")
        else:
            print(f"  Rinde en linea con la mediana del mercado ({ref:.1f}%).")
    print()


# ==========================================================================
# MODO INTERACTIVO
# ==========================================================================

def leer(prompt: str, default: str = "") -> str:
    """input() que no revienta si se corta la entrada (Ctrl+C o pipe vacio)."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def preguntar(texto: str, default=None, tipo=float, opciones=None):
    """Pregunta con valor por defecto y validacion."""
    for _ in range(10):          # tope de reintentos, evita bucle infinito
        sufijo = f" [{default}]" if default is not None else ""
        r = leer(f"  {texto}{sufijo}: ")
        if not r and default is not None:
            return default
        if not r:
            print("    Requerido.")
            continue
        if opciones and r.lower() not in opciones:
            print(f"    No reconozco '{r}'. Ejemplos: {', '.join(opciones[:5])}")
            continue
        try:
            return r.lower() if tipo is str else tipo(r)
        except ValueError:
            print("    Valor invalido, probá con un numero.")
    return default


def interactivo(m: dict) -> dict:
    print()
    print("=" * 62)
    print("SIMULADOR DE ALQUILER — enter para aceptar el valor por defecto")
    print("=" * 62)
    print()

    barrios = m["barrios_validos"]
    print(f"  Barrios disponibles ({len(barrios)}):")
    for i in range(0, len(barrios), 4):
        print("    " + "  ".join(f"{b:<18}" for b in barrios[i:i+4]))
    print()

    prop = {}
    prop["barrio"] = preguntar("Barrio", "palermo", str, barrios)
    prop["m2"] = preguntar("Superficie total (m²)", 60.0)
    prop["ambientes"] = preguntar("Ambientes", 2.0)
    prop["dormitorios"] = preguntar("Dormitorios", 1.0)
    prop["banos"] = preguntar("Baños", 1.0)
    prop["antiguedad"] = preguntar("Antigüedad en años (0 = a estrenar)", 20.0)
    prop["tipo"] = preguntar("Tipo (departamento/casa/ph)", "departamento", str,
                             ["departamento", "casa", "ph"])

    print()
    print("  Características (s/n):")
    for c, nombre in CARACTERISTICAS.items():
        r = leer(f"    ¿{nombre}? [n]: ", "n").lower()
        prop[c] = 1 if r in ("s", "si", "sí", "y", "yes") else 0

    print()
    v = leer("  Precio de venta en USD (enter para omitir): ")
    try:
        prop["_venta"] = float(v) if v else None
    except ValueError:
        prop["_venta"] = None
    return prop


# ==========================================================================
# MAIN
# ==========================================================================

def main() -> int:
    p = argparse.ArgumentParser(
        description="Simulador de alquiler",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--modelo", default=RUTA_MODELO)
    p.add_argument("--barrio")
    p.add_argument("--m2", type=float)
    p.add_argument("--ambientes", type=float)
    p.add_argument("--dormitorios", type=float)
    p.add_argument("--banos", type=float, default=1)
    p.add_argument("--antiguedad", type=float)
    p.add_argument("--tipo", default="departamento",
                   choices=["departamento", "casa", "ph"])
    p.add_argument("--venta", type=float, help="Precio de venta USD (calcula rentabilidad)")
    p.add_argument("--vacancia", type=float, default=8.0)
    p.add_argument("--gastos", type=float, default=12.0)
    for c in CARACTERISTICAS:
        p.add_argument(f"--{c.replace('_txt','').replace('_','-')}",
                       dest=c, action="store_true")
    args = p.parse_args()

    m = cargar(args.modelo)

    if args.barrio and args.m2:
        prop = {
            "barrio": args.barrio.lower(), "m2": args.m2,
            "ambientes": args.ambientes, "dormitorios": args.dormitorios,
            "banos": args.banos, "antiguedad": args.antiguedad,
            "tipo": args.tipo, "_venta": args.venta,
        }
        for c in CARACTERISTICAS:
            prop[c] = int(getattr(args, c, False))

        if prop["barrio"] not in m["barrios_validos"]:
            print(f"\nBarrio '{prop['barrio']}' no esta en el modelo.")
            print("Disponibles:", ", ".join(m["barrios_validos"]))
            return 1
    else:
        prop = interactivo(m)

    r = estimar(m, prop)
    mostrar(prop, r, prop.get("_venta"), args.vacancia, args.gastos)
    return 0


if __name__ == "__main__":
    sys.exit(main())
