#!/usr/bin/env python
"""
limpieza.py — Convierte el dataset crudo del scraping en un dataset analizable.

FILOSOFIA
---------
Nada se borra en silencio. Cada fila que sale del dataset limpio se guarda en
`dataset_excluidos.csv` con el motivo exacto de exclusion. Eso permite:
  - Auditar las decisiones de limpieza (y defenderlas en el informe).
  - Revertir un criterio sin volver a scrapear.
  - Reportar cuantos casos afecto cada regla.

Uso:
    py limpieza.py
    py limpieza.py --tc 1500 --outliers moderado
    py limpieza.py --input data/raw/dataset_maestro.csv --outdir data/processed

Salidas (en data/processed/):
    dataset_limpio.csv      listo para analisis
    dataset_excluidos.csv   lo descartado, con columna motivo_exclusion
    reporte_limpieza.txt    log completo de la corrida
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd


# ==========================================================================
# CONFIGURACION
# ==========================================================================

# Tipos de inmueble que cuentan como vivienda individual.
# Se excluyen cochera, oficina, local, terreno, deposito, galpon, edificio,
# consultorio, fondo_de_comercio, hotel y otros: no son objeto de la tesis de
# inversion (comprar para alquilar como vivienda) y distorsionan el precio/m2.
TIPOS_RESIDENCIALES = [
    "departamento_estandar", "departamento_monoambiente", "departamento_semipiso",
    "departamento_duplex", "departamento_piso", "departamento_loft",
    "departamento_triplex", "departamento_penthouse",
    "ph",
    "casa", "casa_duplex", "casa_triplex",
]

# Columnas que no aportan al analisis (constantes, redundantes o casi vacias).
COLUMNAS_A_ELIMINAR = [
    "portal",         # constante: todo el dataset es Remax
    "localidad",      # constante: todo Capital Federal
    "precio_texto",   # redundante: es precio_valor + precio_moneda concatenados
    "piso",           # 0.3% de completitud
    "credito_uva",    # 0.4% de prevalencia: sin varianza util
    "altura",         # Remax la redondea a la cuadra; usar latitud/longitud
]

# --- Correcciones puntuales (errores de carga detectados en el analisis) ---

# Publicado como venta en ARS, pero el titulo dice ALQUILER y el monto
# (600.000 ARS) es tipico de alquiler mensual, no de venta.
CORREGIR_A_ALQUILER = [
    "768037e0-4549-443b-8719-687b7487f2b9",
]

# Publicados como alquiler en USD, pero el monto solo tiene sentido en pesos.
# Verificado por dos vias: (1) caen en el rango normal de alquileres ARS,
# (2) leidos como ARS/m2/mes dan valores coherentes con la mediana del mercado.
CORREGIR_A_ARS = [
    "c2c07532-3a36-47b1-aa44-e687bd8c1a9e",  # 650.000 - monoambiente 30 m2 Palermo
    "58242e4e-8095-420c-b694-771c60beab76",  # 900.000 - 2 amb 51 m2 Cañitas
    "f6fd715a-7b59-459c-8b17-c6e3fef920a2",  # 1.000.000 - oficinas 179 m2
    "4592b8b4-6985-4388-abee-102d93ffc962",  # 2.400.000 - local 220 m2 Flores
    "78f7fd56-e24e-4f56-a7c9-56ca55523212",  # 650.000 - ademas tiene la sup mal
]

# Este ademas tiene la superficie mal cargada (694 m2 para un monoambiente).
SUPERFICIE_SOSPECHOSA = ["78f7fd56-e24e-4f56-a7c9-56ca55523212"]

# --- Limites de sanidad (imposibles fisicos, no criterios estadisticos) ---
LIMITES = {
    "sup_min": 10,        # menos de 10 m2 no es una vivienda
    "sup_max": 2000,      # mas de 2000 m2 no es vivienda individual en CABA
    "antig_min": 0,       # negativa = error (o pozo mal cargado)
    "antig_max": 150,     # mas de 150 años es improbable y suele ser error
    "amb_max": 15,
    "dorm_max": 12,
}

# Recorte de colas del precio/m2 segun agresividad elegida
PERCENTILES = {
    "conservador": (0.000, 1.000),   # no recorta: solo imposibles fisicos
    "moderado":    (0.010, 0.990),   # 1% de cada cola
    "agresivo":    (0.050, 0.950),   # 5% de cada cola
}


# ==========================================================================
# UTILIDADES DE REPORTE
# ==========================================================================

class Reporte:
    """Acumula el log de la corrida para imprimirlo y guardarlo."""

    def __init__(self):
        self.lineas: list[str] = []

    def __call__(self, texto: str = "") -> None:
        print(texto)
        self.lineas.append(texto)

    def titulo(self, texto: str) -> None:
        self("")
        self("=" * 74)
        self(texto)
        self("=" * 74)

    def paso(self, n: int, texto: str) -> None:
        self("")
        self(f"--- PASO {n}: {texto} " + "-" * max(0, 66 - len(texto)))

    def guardar(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lineas))


def marcar_exclusion(df: pd.DataFrame, mask: pd.Series, motivo: str,
                     excluidos: list, rep: Reporte) -> pd.DataFrame:
    """
    Saca del dataframe las filas que cumplen `mask`, guardandolas con su motivo.

    Devuelve el dataframe sin esas filas. El detalle va a la lista `excluidos`,
    que al final se concatena en dataset_excluidos.csv.
    """
    n = int(mask.sum())
    if n:
        fuera = df[mask].copy()
        fuera["motivo_exclusion"] = motivo
        excluidos.append(fuera)
    rep(f"    {motivo:<46} {n:>6} filas")
    return df[~mask].copy()


# ==========================================================================
# PASOS DE LIMPIEZA
# ==========================================================================

def paso_1_correcciones(df: pd.DataFrame, rep: Reporte) -> pd.DataFrame:
    """
    Corrige errores de carga identificados manualmente.

    Se hace ANTES de todo lo demas: si no, estas filas caerian como outliers
    y perderiamos observaciones validas por un error de tipeo del publicador.
    """
    rep.paso(1, "Correcciones puntuales de errores de carga")

    m = df["id_aviso"].isin(CORREGIR_A_ALQUILER)
    df.loc[m, "operacion"] = "alquiler"
    rep(f"    venta -> alquiler (monto y titulo de alquiler)      {int(m.sum()):>6} filas")

    m = df["id_aviso"].isin(CORREGIR_A_ARS)
    df.loc[m, "precio_moneda"] = "ARS"
    rep(f"    moneda USD -> ARS (monto solo coherente en pesos)   {int(m.sum()):>6} filas")

    # La superficie mal cargada se anula en vez de inventar un valor:
    # es preferible un NaN honesto a un dato fabricado.
    m = df["id_aviso"].isin(SUPERFICIE_SOSPECHOSA)
    df.loc[m, ["sup_total_m2", "sup_cubierta_m2"]] = np.nan
    rep(f"    superficie anulada por inconsistente                {int(m.sum()):>6} filas")

    # El precio_m2 de las filas corregidas quedo mal: se recalcula despues.
    tocadas = df["id_aviso"].isin(CORREGIR_A_ALQUILER + CORREGIR_A_ARS + SUPERFICIE_SOSPECHOSA)
    df.loc[tocadas, "precio_m2"] = np.nan
    return df


def paso_2_residencial(df: pd.DataFrame, excluidos: list, rep: Reporte) -> pd.DataFrame:
    """Deja solo vivienda individual: departamentos, PH y casas."""
    rep.paso(2, "Filtrado a inmuebles de vivienda individual")

    fuera = df[~df["tipo_propiedad"].isin(TIPOS_RESIDENCIALES)]
    if len(fuera):
        rep("    tipos excluidos:")
        for t, n in fuera["tipo_propiedad"].value_counts().items():
            rep(f"        {t:<28} {n:>5}")

    mask = ~df["tipo_propiedad"].isin(TIPOS_RESIDENCIALES)
    return marcar_exclusion(df, mask, "no_residencial", excluidos, rep)


def paso_3_duplicados(df: pd.DataFrame, excluidos: list, rep: Reporte) -> pd.DataFrame:
    """
    Elimina republicaciones del mismo inmueble.

    Dos niveles:
      a) url / id_aviso identicos -> duplicado tecnico del scraping.
      b) misma direccion + superficie + precio + operacion -> el mismo inmueble
         publicado dos veces (pasa cuando lo cargan dos agentes de la red).
    Se conserva la captura mas reciente.
    """
    rep.paso(3, "Deduplicacion")

    if "fecha_scraping" in df.columns:
        df = df.sort_values("fecha_scraping", ascending=False)

    df = marcar_exclusion(df, df.duplicated(subset=["url"], keep="first"),
                          "duplicado_url", excluidos, rep)
    df = marcar_exclusion(df, df.duplicated(subset=["id_aviso"], keep="first"),
                          "duplicado_id", excluidos, rep)

    clave = ["direccion", "sup_total_m2", "precio_valor", "operacion"]
    dup = df.duplicated(subset=clave, keep="first") & df["direccion"].notna()
    df = marcar_exclusion(df, dup, "duplicado_mismo_inmueble", excluidos, rep)

    return df.reset_index(drop=True)


def paso_4_imposibles(df: pd.DataFrame, excluidos: list, rep: Reporte) -> pd.DataFrame:
    """
    Saca valores fisicamente imposibles.

    Esto NO es un criterio estadistico: son datos que no pueden existir
    (superficie cero, antiguedad negativa). Se aplica siempre, en cualquier
    nivel de agresividad.
    """
    rep.paso(4, "Valores imposibles")

    L = LIMITES

    df = marcar_exclusion(df, df["precio_valor"].isna() | (df["precio_valor"] <= 0),
                          "precio_ausente_o_cero", excluidos, rep)
    df = marcar_exclusion(df, df["sup_total_m2"].isna() | (df["sup_total_m2"] < L["sup_min"]),
                          f"superficie_menor_a_{L['sup_min']}m2", excluidos, rep)
    df = marcar_exclusion(df, df["sup_total_m2"] > L["sup_max"],
                          f"superficie_mayor_a_{L['sup_max']}m2", excluidos, rep)

    # La antiguedad fuera de rango se anula, pero la fila se conserva:
    # el resto de sus variables sigue siendo valido.
    m = df["antiguedad_anios"].notna() & (
        (df["antiguedad_anios"] < L["antig_min"]) | (df["antiguedad_anios"] > L["antig_max"]))
    df.loc[m, ["antiguedad_anios", "es_a_estrenar"]] = np.nan
    rep(f"    antiguedad fuera de rango -> NaN (fila conservada)  {int(m.sum()):>6} filas")

    for col, lim in [("ambientes", L["amb_max"]), ("dormitorios", L["dorm_max"])]:
        m = df[col].notna() & (df[col] > lim)
        df.loc[m, col] = np.nan
        rep(f"    {col} > {lim} -> NaN (fila conservada){' '*(19-len(col))}{int(m.sum()):>6} filas")

    return df


def paso_5_monedas(df: pd.DataFrame, tc: float, rep: Reporte) -> pd.DataFrame:
    """
    Normaliza todo a dolares y crea columnas con unidad explicita.

    POR QUE ES EL PASO MAS IMPORTANTE
    ---------------------------------
    En el dataset crudo, `precio_m2` mezcla cuatro unidades distintas en una
    sola columna:
        venta USD       -> USD por m2          (mediana ~1.900)
        alquiler ARS    -> ARS por m2 por MES  (mediana ~18.000)
        alquiler USD    -> USD por m2 por MES  (mediana ~15)
    Promediar esa columna sin filtrar da un numero sin significado.

    Ademas de la moneda hay una diferencia dimensional: venta es un precio
    (stock), alquiler es un flujo mensual. No son comparables ni en la misma
    moneda.

    La solucion es crear columnas cuyo nombre diga la unidad, de modo que
    mezclarlas por accidente sea imposible.
    """
    rep.paso(5, f"Normalizacion de monedas (TC = {tc:,.0f} ARS/USD)")

    # Tipo de cambio implicito del propio mercado.
    #
    # Se compara el precio POR M2, no el precio total: los alquileres publicados
    # en dolares son sistematicamente propiedades mas grandes y premium, asi que
    # comparar medianas de precio crudo mezcla el efecto moneda con el efecto
    # tamaño y da un TC subestimado. Dividir por la superficie neutraliza eso.
    a = df[(df["operacion"] == "alquiler") & (df["sup_total_m2"] > 0)].copy()
    a["p_m2"] = a["precio_valor"] / a["sup_total_m2"]
    m_ars = a[a["precio_moneda"] == "ARS"]["p_m2"].median()
    m_usd = a[a["precio_moneda"] == "USD"]["p_m2"].median()
    tc_implicito = m_ars / m_usd if m_usd else np.nan

    rep(f"    TC usado                    : {tc:>10,.0f}")
    rep(f"    TC implicito en los datos   : {tc_implicito:>10,.0f}   "
        f"(mediana ARS/m2 / mediana USD/m2)")
    desvio = (tc / tc_implicito - 1) * 100 if tc_implicito else np.nan
    rep(f"    desvio                      : {desvio:>9.1f}%")
    if abs(desvio) > 30:
        rep("    AVISO: el TC elegido se aleja >30% del implicito.")
        rep("           El implicito refleja como el mercado convierte al publicar.")
        rep("           Si usás una cotizacion oficial, es esperable cierta brecha;")
        rep("           si es muy grande, revisá que el TC corresponda a la fecha del scraping.")

    # Precio unificado en USD
    df["precio_usd"] = np.where(df["precio_moneda"] == "ARS",
                                df["precio_valor"] / tc,
                                df["precio_valor"])

    # Expensas: en Remax vienen casi siempre en ARS
    df["expensas_usd"] = np.where(df["expensas_moneda"] == "ARS",
                                  df["expensas_valor"] / tc,
                                  df["expensas_valor"])

    # Columnas con unidad explicita en el nombre.
    # Solo se llenan para la operacion que corresponde: para una fila de venta,
    # alquiler_usd_mes es NaN, y viceversa. Asi es imposible mezclarlas.
    es_venta = df["operacion"] == "venta"
    df["venta_usd"] = df["precio_usd"].where(es_venta)
    df["alquiler_usd_mes"] = df["precio_usd"].where(~es_venta)

    df["venta_m2_usd"] = (df["venta_usd"] / df["sup_total_m2"]).round(2)
    df["alquiler_m2_usd_mes"] = (df["alquiler_usd_mes"] / df["sup_total_m2"]).round(2)

    # Se recalcula precio_m2 para las filas corregidas en el paso 1
    df["precio_m2"] = (df["precio_valor"] / df["sup_total_m2"]).round(2)

    rep(f"    columnas creadas: precio_usd, expensas_usd, venta_usd,")
    rep(f"                      alquiler_usd_mes, venta_m2_usd, alquiler_m2_usd_mes")
    return df


def paso_6_outliers(df: pd.DataFrame, modo: str, excluidos: list,
                    rep: Reporte) -> pd.DataFrame:
    """
    Recorta las colas del precio por m2.

    Se calcula por separado para venta y alquiler, porque son distribuciones
    distintas. Se usan percentiles y no desvios estandar: la distribucion de
    precios inmobiliarios tiene cola derecha larga y la media/sigma no la
    describen bien.
    """
    p_lo, p_hi = PERCENTILES[modo]
    rep.paso(6, f"Outliers de precio/m2 — modo {modo} "
                f"(recorte {p_lo*100:.0f}% / {(1-p_hi)*100:.0f}% por cola)")

    if modo == "conservador":
        rep("    modo conservador: no se recorta nada")
        return df

    for op, col in [("venta", "venta_m2_usd"), ("alquiler", "alquiler_m2_usd_mes")]:
        sub = df.loc[df["operacion"] == op, col].dropna()
        if len(sub) < 100:
            rep(f"    {op}: muy pocos datos ({len(sub)}), no se recorta")
            continue

        lo, hi = sub.quantile(p_lo), sub.quantile(p_hi)
        mask = (df["operacion"] == op) & df[col].notna() & ((df[col] < lo) | (df[col] > hi))
        rep(f"    {op:<9} rango valido: {lo:>10,.2f}  a {hi:>10,.2f} USD/m2")
        df = marcar_exclusion(df, mask, f"outlier_precio_m2_{op}", excluidos, rep)

    return df


def paso_7_derivadas(df: pd.DataFrame, rep: Reporte) -> pd.DataFrame:
    """
    Crea variables nuevas utiles para el analisis exploratorio.

    Las categoricas ordenadas (rangos de antiguedad, de superficie) facilitan
    los cruces y los graficos sin perder la variable continua original.
    """
    rep.paso(7, "Variables derivadas")

    # Antiguedad en tramos con sentido de mercado
    df["antiguedad_rango"] = pd.cut(
        df["antiguedad_anios"],
        bins=[-0.1, 0, 5, 15, 30, 50, 150],
        labels=["a estrenar", "1-5", "6-15", "16-30", "31-50", "50+"],
    )

    # Tamaño
    df["superficie_rango"] = pd.cut(
        df["sup_total_m2"],
        bins=[0, 35, 55, 80, 120, 2000],
        labels=["hasta 35", "36-55", "56-80", "81-120", "120+"],
    )

    # Proporcion de superficie descubierta (balcon, patio, terraza)
    df["ratio_cubierta"] = (df["sup_cubierta_m2"] / df["sup_total_m2"]).round(3)
    df.loc[(df["ratio_cubierta"] > 1.05) | (df["ratio_cubierta"] <= 0), "ratio_cubierta"] = np.nan

    # Peso de las expensas sobre el alquiler: clave para el cap rate neto
    df["expensas_sobre_alquiler"] = (df["expensas_usd"] / df["alquiler_usd_mes"]).round(3)
    df.loc[df["expensas_sobre_alquiler"] > 2, "expensas_sobre_alquiler"] = np.nan

    # Familia de tipo de propiedad, para agrupar sin perder el detalle
    df["tipo_familia"] = np.select(
        [df["tipo_propiedad"].str.startswith("departamento"),
         df["tipo_propiedad"].str.startswith("casa"),
         df["tipo_propiedad"] == "ph"],
        ["departamento", "casa", "ph"], default="otro")

    rep("    antiguedad_rango, superficie_rango, ratio_cubierta,")
    rep("    expensas_sobre_alquiler, tipo_familia")
    return df


def paso_8_columnas(df: pd.DataFrame, rep: Reporte) -> pd.DataFrame:
    """Elimina columnas sin valor analitico."""
    rep.paso(8, "Eliminacion de columnas inutiles")

    presentes = [c for c in COLUMNAS_A_ELIMINAR if c in df.columns]
    for c in presentes:
        motivo = {
            "portal": "constante (todo Remax)",
            "localidad": "constante (todo CABA)",
            "precio_texto": "redundante con precio_valor + precio_moneda",
            "piso": "0.3% de completitud",
            "credito_uva": "0.4% de prevalencia, sin varianza",
            "altura": "redondeada a la cuadra por el portal",
        }.get(c, "")
        rep(f"    {c:<22} {motivo}")

    return df.drop(columns=presentes)


# ==========================================================================
# ORQUESTACION
# ==========================================================================

def limpiar(path_in: str, outdir: str, tc: float, modo: str) -> pd.DataFrame:
    rep = Reporte()
    excluidos: list[pd.DataFrame] = []

    rep.titulo("LIMPIEZA DEL DATASET INMOBILIARIO")
    rep(f"Fecha        : {datetime.now():%Y-%m-%d %H:%M}")
    rep(f"Entrada      : {path_in}")
    rep(f"Tipo cambio  : {tc:,.0f} ARS/USD")
    rep(f"Outliers     : {modo}")

    df = pd.read_csv(path_in, encoding="utf-8-sig", low_memory=False)
    n0 = len(df)
    rep(f"Filas leidas : {n0:,}")

    df = paso_1_correcciones(df, rep)
    df = paso_2_residencial(df, excluidos, rep)
    df = paso_3_duplicados(df, excluidos, rep)
    df = paso_4_imposibles(df, excluidos, rep)
    df = paso_5_monedas(df, tc, rep)
    df = paso_6_outliers(df, modo, excluidos, rep)
    df = paso_7_derivadas(df, rep)
    df = paso_8_columnas(df, rep)

    # ---------------------------------------------------------------- salida
    os.makedirs(outdir, exist_ok=True)
    p_limpio = os.path.join(outdir, "dataset_limpio.csv")
    df.to_csv(p_limpio, index=False, encoding="utf-8-sig")

    if excluidos:
        exc = pd.concat(excluidos, ignore_index=True)
        p_exc = os.path.join(outdir, "dataset_excluidos.csv")
        exc.to_csv(p_exc, index=False, encoding="utf-8-sig")
    else:
        exc = pd.DataFrame()

    # ---------------------------------------------------------------- resumen
    rep.titulo("RESUMEN")
    rep(f"Filas iniciales : {n0:>7,}")
    rep(f"Filas excluidas : {len(exc):>7,}  ({len(exc)/n0*100:.1f}%)")
    rep(f"Filas limpias   : {len(df):>7,}  ({len(df)/n0*100:.1f}%)")
    rep(f"Columnas        : {df.shape[1]:>7}")

    if len(exc):
        rep("")
        rep("Motivos de exclusion:")
        for m, n in exc["motivo_exclusion"].value_counts().items():
            rep(f"    {m:<34} {n:>6}")

    rep("")
    rep("Composicion final:")
    for (op,), g in df.groupby(["operacion"]):
        rep(f"    {op:<10} {len(g):>6}")
    rep("")
    for t, n in df["tipo_familia"].value_counts().items():
        rep(f"    {t:<10} {n:>6}")

    rep("")
    rep("Completitud de variables clave:")
    for c in ["venta_usd", "alquiler_usd_mes", "sup_total_m2", "ambientes",
              "dormitorios", "banos", "antiguedad_anios", "barrio", "latitud",
              "expensas_usd"]:
        if c in df.columns:
            rep(f"    {c:<22} {df[c].notna().mean()*100:>5.1f}%")

    # Vista previa del KPI, para verificar que el resultado tiene sentido
    rep.titulo("CONTROL: rentabilidad bruta anual por barrio")
    rep("Formula: (alquiler mediano USD x 12) / venta mediana USD")
    rep("Solo barrios con >= 25 alquileres y >= 25 ventas")
    rep("")
    v = df[df["operacion"] == "venta"].groupby("barrio")["venta_usd"].agg(["median", "size"])
    a = df[df["operacion"] == "alquiler"].groupby("barrio")["alquiler_usd_mes"].agg(["median", "size"])
    k = a.join(v, lsuffix="_alq", rsuffix="_vta", how="inner")
    k = k[(k["size_alq"] >= 25) & (k["size_vta"] >= 25)]
    if len(k):
        k["rent_bruta_%"] = (k["median_alq"] * 12 / k["median_vta"] * 100).round(2)
        k = k.sort_values("rent_bruta_%", ascending=False)
        rep(f"{'barrio':<20}{'n_alq':>7}{'n_vta':>7}{'alq_usd':>10}{'venta_usd':>12}{'rent %':>9}")
        rep("-" * 66)
        for b, r in k.iterrows():
            rep(f"{b:<20}{int(r['size_alq']):>7}{int(r['size_vta']):>7}"
                f"{r['median_alq']:>10,.0f}{r['median_vta']:>12,.0f}{r['rent_bruta_%']:>9.2f}")

    rep("")
    rep("Archivos generados:")
    rep(f"    {p_limpio}")
    if len(exc):
        rep(f"    {os.path.join(outdir, 'dataset_excluidos.csv')}")
    p_rep = os.path.join(outdir, "reporte_limpieza.txt")
    rep(f"    {p_rep}")
    rep.guardar(p_rep)

    return df


def main() -> int:
    p = argparse.ArgumentParser(
        description="Limpieza del dataset inmobiliario",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--input", default="data/raw/dataset_maestro.csv",
                   help="CSV crudo de entrada")
    p.add_argument("--outdir", default="data/processed",
                   help="Carpeta de salida (default: data/processed)")
    p.add_argument("--tc", type=float, default=1500,
                   help="Tipo de cambio ARS/USD (default: 1500)")
    p.add_argument("--outliers", default="moderado",
                   choices=list(PERCENTILES.keys()),
                   help="Agresividad del filtro de outliers (default: moderado)")
    args = p.parse_args()

    if not os.path.exists(args.input):
        print(f"No existe el archivo: {args.input}")
        print("Pasá la ruta con --input")
        return 1

    limpiar(args.input, args.outdir, args.tc, args.outliers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
