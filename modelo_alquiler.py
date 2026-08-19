#!/usr/bin/env python
"""
modelo_alquiler.py — Estima el alquiler esperado de cada propiedad en venta
y calcula su rentabilidad.

EL PROBLEMA QUE RESUELVE
------------------------
El dataset tiene ~8 ventas por cada alquiler. Para calcular la rentabilidad de
una propiedad en venta hace falta saber cuanto se alquilaria, pero esa propiedad
no esta publicada en alquiler.

Tres formas de estimarlo, de peor a mejor:

  1. Mediana de barrio x ambientes -> solo el 28% de las ventas cae en una celda
     con 20+ alquileres. No alcanza.
  2. Mediana del barrio x m2 -> cubre 85%, error ~14%. Ignora antiguedad,
     amenities y todo lo demas.
  3. Modelo de regresion -> usa las ~1.350 observaciones JUNTAS en vez de
     partirlas en celdas chicas. Cubre el 100% y da ~11.5% de error.

Este script implementa la opcion 3, con la 2 como referencia de control.

POR QUE log(alquiler) Y NO alquiler
-----------------------------------
El precio inmobiliario es multiplicativo, no aditivo: un balcon no suma "USD 80",
suma "un 8% mas". Modelar el logaritmo captura eso, estabiliza la varianza
(la dispersion crece con el precio) y hace que los coeficientes se lean
directamente como variaciones porcentuales.

Al predecir hay que volver a la escala original. Como E[exp(X)] != exp(E[X]),
se aplica la correccion de Duan (smearing), que evita subestimar sistematicamente.

Uso:
    py modelo_alquiler.py
    py modelo_alquiler.py --vacancia 8 --gastos 12
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

import joblib
import numpy as np
import pandas as pd

from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import RidgeCV
from sklearn.model_selection import KFold, cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler


# ==========================================================================
# CONFIGURACION
# ==========================================================================

# Variables predictoras. Se eligieron por disponibilidad (>95% de completitud)
# y por tener sentido economico, no por busqueda automatica: un modelo que hay
# que defender en un informe se explica mejor si las variables son justificables.
NUMERICAS = ["log_sup", "ambientes", "banos", "dormitorios", "antiguedad_anios"]
CATEGORICAS = ["barrio", "tipo_familia"]
DUMMIES = [
    "balcon", "cochera_txt", "amenities", "ascensor", "aire_acondicionado",
    "pileta", "amoblado", "parrilla", "seguridad", "baulera", "luminoso",
    "patio_jardin", "a_reciclar", "reciclado",
]

# Minimo de alquileres en un barrio para confiar en su estimacion.
# Por debajo, el modelo extrapola desde otros barrios y hay que marcarlo.
MIN_ALQ_BARRIO = 10

# Segmentos de superficie usados para medir el error de forma diferenciada.
# El error del modelo NO es uniforme: crece con el tamaño de la propiedad.
BINS_SUP = [0, 35, 50, 70, 100, 10000]
LAB_SUP = ["<35", "35-50", "50-70", "70-100", "100+"]

# Supuestos por defecto para la rentabilidad neta (todos parametrizables).
# En CABA las expensas ordinarias las paga el inquilino, asi que NO se descuentan;
# lo que si pesa sobre el propietario es la vacancia, la administracion, el ABL
# y las expensas extraordinarias.
VACANCIA_PCT = 8.0      # ~1 mes cada 12 entre inquilinos
GASTOS_PCT = 12.0       # administracion + ABL + extraordinarias + mantenimiento


# ==========================================================================
# UTILIDADES
# ==========================================================================

class Reporte:
    def __init__(self):
        self.lineas: list[str] = []

    def __call__(self, t: str = "") -> None:
        print(t)
        self.lineas.append(t)

    def titulo(self, t: str) -> None:
        self("")
        self("=" * 76)
        self(t)
        self("=" * 76)

    def guardar(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lineas))


def preparar(df: pd.DataFrame) -> pd.DataFrame:
    """Crea las columnas derivadas que necesita el modelo."""
    df = df.copy()
    df["log_sup"] = np.log(df["sup_total_m2"].where(df["sup_total_m2"] > 0))
    for c in DUMMIES:
        if c not in df.columns:
            df[c] = 0
    return df


def construir_pipeline() -> Pipeline:
    """
    Preprocesamiento + modelo.

    RidgeCV elige solo el nivel de regularizacion por validacion cruzada.
    La regularizacion importa porque `barrio` genera ~48 dummies y varios
    barrios tienen pocas observaciones: sin penalizacion, el modelo les
    ajustaria ruido.
    """
    pre = ColumnTransformer([
        ("num", Pipeline([
            ("imp", SimpleImputer(strategy="median")),
            ("esc", StandardScaler()),
        ]), NUMERICAS),
        ("cat", OneHotEncoder(handle_unknown="ignore", min_frequency=5), CATEGORICAS),
        ("dum", "passthrough", DUMMIES),
    ])
    return Pipeline([
        ("pre", pre),
        ("mod", RidgeCV(alphas=np.logspace(-2, 3, 30))),
    ])


def factor_smearing(residuos: np.ndarray) -> float:
    """
    Correccion de Duan para volver de log a la escala original.

    exp(prediccion_en_log) subestima la media porque la transformacion es
    convexa. El factor es el promedio de exp(residuos) y corrige ese sesgo
    sin asumir normalidad.
    """
    return float(np.mean(np.exp(residuos)))


# ==========================================================================
# ENTRENAMIENTO Y VALIDACION
# ==========================================================================

def entrenar_y_validar(alq: pd.DataFrame, rep: Reporte):
    """Entrena el modelo y reporta su desempeño fuera de muestra."""
    X = alq[NUMERICAS + CATEGORICAS + DUMMIES]
    y = np.log(alq["alquiler_usd_mes"])

    pipe = construir_pipeline()
    kf = KFold(5, shuffle=True, random_state=42)

    rep.titulo("VALIDACION DEL MODELO (5-fold, fuera de muestra)")
    rep(f"Observaciones de entrenamiento : {len(alq):,}")
    rep(f"Variables                      : {len(NUMERICAS)} numericas, "
        f"{len(CATEGORICAS)} categoricas, {len(DUMMIES)} dicotomicas")
    rep("")

    # cross_val_predict garantiza que cada prediccion venga de un modelo
    # que NO vio esa fila: es la unica forma honesta de medir el error.
    pred_log = cross_val_predict(pipe, X, y, cv=kf)
    real = alq["alquiler_usd_mes"].values
    est = np.exp(pred_log)

    ss_res = float(((y - pred_log) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot

    err_rel = np.abs(est - real) / real
    rep(f"R2 (en log)                    : {r2:.3f}")
    rep(f"Error relativo mediano         : {np.median(err_rel)*100:.1f}%")
    rep(f"Error relativo medio           : {err_rel.mean()*100:.1f}%")
    rep(f"Predicciones con error < 10%   : {(err_rel < .10).mean()*100:.1f}%")
    rep(f"Predicciones con error < 20%   : {(err_rel < .20).mean()*100:.1f}%")
    rep(f"Predicciones con error > 50%   : {(err_rel > .50).mean()*100:.1f}%")

    # --- Referencia de control: mediana del barrio por m2 ---
    med = alq.groupby("barrio")["alquiler_m2_usd_mes"].transform("median")
    est_simple = (med * alq["sup_total_m2"]).values
    ok = ~np.isnan(est_simple)
    err_simple = np.abs(est_simple[ok] - real[ok]) / real[ok]
    rep("")
    rep(f"Control — mediana barrio x m2  : {np.median(err_simple)*100:.1f}% "
        f"(y es optimista: se calcula dentro de la misma muestra)")
    mejora = (np.median(err_simple) - np.median(err_rel)) / np.median(err_simple) * 100
    rep(f"Mejora del modelo              : {mejora:.0f}%")

    # --- Error POR SEGMENTO DE SUPERFICIE ---
    #
    # Importante: el error global esta calculado sobre la mezcla de ALQUILERES,
    # que son sistematicamente mas chicos que las ventas (mediana 45 vs 66 m2).
    # Como el error crece con el tamaño, aplicar el error global a las ventas
    # lo subestima. Guardamos el error por segmento para asignarle a cada
    # propiedad la incertidumbre que le corresponde.
    tmp = alq.copy()
    tmp["_err"] = err_rel
    tmp["_seg"] = pd.cut(tmp["sup_total_m2"], BINS_SUP, labels=LAB_SUP)
    err_seg = tmp.groupby("_seg", observed=True)["_err"].median()

    rep("")
    rep("Error por segmento de superficie:")
    rep(f"    {'segmento':<12}{'n':>6}{'error':>9}")
    for s in LAB_SUP:
        if s in err_seg.index:
            n = int((tmp["_seg"] == s).sum())
            rep(f"    {s:<12}{n:>6}{err_seg[s]*100:>8.1f}%")
    rep("")
    rep("    El error crece con la superficie. Como las ventas son mas grandes")
    rep("    que los alquileres, el error real sobre las ventas es mayor que el")
    rep("    global. Por eso se asigna la banda por segmento y no una unica.")

    # --- Factor de smearing ---
    #
    # Se calcula con los residuos OUT-OF-FOLD (los de cross_val_predict), no con
    # los del modelo ya ajustado. Duan lo define con residuos in-sample, pero
    # esos son artificialmente chicos: el modelo vio esas filas al entrenar.
    # Como el factor es mean(exp(residuo)), subestimarlos subestima la correccion.
    # La diferencia acá es minima (~0.02%), pero usar los honestos no cuesta nada
    # y evita una objecion metodologica.
    smear = factor_smearing((y - pred_log).values)

    # Modelo FINAL entrenado con todos los datos.
    # Esto es deliberado: las metricas de arriba ya se midieron fuera de muestra,
    # asi que para el modelo que se guarda y se usa en produccion conviene
    # aprovechar las 1.348 observaciones y no solo el 80% de un fold.
    pipe.fit(X, y)

    rep("")
    rep(f"Factor de smearing (Duan)      : {smear:.4f}   (residuos out-of-fold)")

    return pipe, smear, err_seg


def reportar_coeficientes(pipe: Pipeline, rep: Reporte) -> None:
    """
    Traduce los coeficientes a efectos porcentuales legibles.

    Como el target es log(alquiler), un coeficiente b se lee como
    (exp(b) - 1) * 100 por ciento de variacion.
    """
    rep.titulo("EFECTO DE CADA VARIABLE SOBRE EL ALQUILER")
    rep("Lectura: cuanto varia el alquiler esperado, en %, ante la presencia")
    rep("de la caracteristica (dummies) o un desvio estandar mas (numericas).")
    rep("")

    pre = pipe.named_steps["pre"]
    coefs = pipe.named_steps["mod"].coef_
    nombres = list(pre.get_feature_names_out())

    efectos = [(n.split("__")[-1], (np.exp(c) - 1) * 100) for n, c in zip(nombres, coefs)]

    # Las de barrio se listan aparte: son ~48 y tienen otra interpretacion
    barrios = [(n.replace("barrio_", ""), e) for n, e in efectos if n.startswith("barrio_")]
    resto = [(n, e) for n, e in efectos if not n.startswith("barrio_")]

    resto.sort(key=lambda x: -abs(x[1]))
    rep("Caracteristicas del inmueble:")
    for n, e in resto[:18]:
        signo = "+" if e >= 0 else ""
        rep(f"    {n:<26} {signo}{e:>6.1f}%")

    barrios.sort(key=lambda x: -x[1])
    rep("")
    rep("Barrios que mas suman (vs. el promedio):")
    for n, e in barrios[:8]:
        rep(f"    {n:<26} +{e:>6.1f}%")
    rep("")
    rep("Barrios que mas restan:")
    for n, e in barrios[-8:]:
        rep(f"    {n:<26} {e:>7.1f}%")


# ==========================================================================
# IMPUTACION Y RENTABILIDAD
# ==========================================================================

def imputar(vta: pd.DataFrame, pipe: Pipeline, smear: float,
            cobertura: pd.Series, rep: Reporte) -> pd.DataFrame:
    """Estima el alquiler esperado de cada propiedad en venta."""
    rep.titulo("IMPUTACION DEL ALQUILER ESPERADO A LAS VENTAS")

    X = vta[NUMERICAS + CATEGORICAS + DUMMIES]
    vta = vta.copy()
    vta["alquiler_est_usd_mes"] = (np.exp(pipe.predict(X)) * smear).round(2)
    vta["alquiler_est_m2"] = (vta["alquiler_est_usd_mes"] / vta["sup_total_m2"]).round(2)

    # Marca de confianza: si el barrio tiene pocos alquileres, el modelo
    # extrapola desde otros barrios y la estimacion es mas fragil.
    vta["n_alq_barrio"] = vta["barrio"].map(cobertura).fillna(0).astype(int)
    vta["estimacion_confiable"] = (vta["n_alq_barrio"] >= MIN_ALQ_BARRIO).astype(int)

    rep(f"Propiedades estimadas          : {len(vta):,}")
    rep(f"Con estimacion confiable       : {int(vta['estimacion_confiable'].sum()):,} "
        f"({vta['estimacion_confiable'].mean()*100:.1f}%)")
    rep(f"En barrios con < {MIN_ALQ_BARRIO} alquileres  : "
        f"{int((1-vta['estimacion_confiable']).sum()):,}")
    rep("")
    rep(f"Alquiler estimado — mediana    : USD {vta['alquiler_est_usd_mes'].median():,.0f}/mes")
    rep(f"Alquiler estimado — rango p5-p95: USD "
        f"{vta['alquiler_est_usd_mes'].quantile(.05):,.0f} a "
        f"{vta['alquiler_est_usd_mes'].quantile(.95):,.0f}")
    return vta


def calcular_rentabilidad(vta: pd.DataFrame, vacancia: float, gastos: float,
                          err_seg: pd.Series, rep: Reporte) -> pd.DataFrame:
    """
    Calcula los KPIs de rentabilidad.

    Rentabilidad Bruta Anual = (Alquiler mensual x 12) / Precio de venta

    Rentabilidad Neta Anual  = Bruta x (1 - vacancia) x (1 - gastos)

    Nota sobre expensas: en CABA las ordinarias las paga el inquilino, por eso
    NO se descuentan del ingreso del propietario. Lo que si pesa es la vacancia
    (meses sin inquilino), la administracion, el ABL y las extraordinarias:
    todo eso esta agrupado en el parametro `gastos`.
    """
    rep.titulo("KPIs DE RENTABILIDAD")
    rep(f"Vacancia supuesta              : {vacancia:.1f}%")
    rep(f"Gastos del propietario         : {gastos:.1f}%")
    rep("")

    v = vta.copy()
    v["rent_bruta_pct"] = (v["alquiler_est_usd_mes"] * 12 / v["venta_usd"] * 100).round(2)
    v["rent_neta_pct"] = (v["rent_bruta_pct"] * (1 - vacancia / 100)
                          * (1 - gastos / 100)).round(2)
    v["meses_repago"] = (v["venta_usd"] / v["alquiler_est_usd_mes"]).round(0)

    # Banda de incertidumbre POR SEGMENTO: una propiedad de 120 m2 tiene mas
    # error de estimacion que un monoambiente, y el intervalo debe reflejarlo.
    seg = pd.cut(v["sup_total_m2"], BINS_SUP, labels=LAB_SUP)
    v["error_estimacion"] = seg.map(err_seg).astype(float).fillna(err_seg.median())
    v["rent_bruta_min"] = (v["rent_bruta_pct"] * (1 - v["error_estimacion"])).round(2)
    v["rent_bruta_max"] = (v["rent_bruta_pct"] * (1 + v["error_estimacion"])).round(2)

    err_pond = float(v["error_estimacion"].mean())
    rep(f"Error ponderado sobre las ventas: {err_pond*100:.1f}%")
    rep("    (mayor que el error global de validacion, porque las ventas son")
    rep("     mas grandes que los alquileres y ahi el modelo pierde precision)")
    rep("")

    ok = v[v["estimacion_confiable"] == 1]
    rep(f"Rentabilidad bruta — mediana   : {ok['rent_bruta_pct'].median():.2f}%")
    rep(f"Rentabilidad bruta — p25 a p75 : {ok['rent_bruta_pct'].quantile(.25):.2f}% a "
        f"{ok['rent_bruta_pct'].quantile(.75):.2f}%")
    rep(f"Rentabilidad neta — mediana    : {ok['rent_neta_pct'].median():.2f}%")
    rep(f"Meses de repago — mediana      : {ok['meses_repago'].median():,.0f} "
        f"({ok['meses_repago'].median()/12:.1f} años)")
    rep("")
    rep("Lectura de la incertidumbre:")
    for r in [5, 6, 8]:
        rep(f"    rentabilidad estimada {r}%  ->  real entre "
            f"{r*(1-err_pond):.1f}% y {r*(1+err_pond):.1f}%")
    rep("")
    rep("Sirve para rankear barrios, no para decidir sobre un inmueble puntual.")
    return v


def ranking_barrios(v: pd.DataFrame, rep: Reporte) -> pd.DataFrame:
    """Ranking de barrios por rentabilidad bruta mediana."""
    rep.titulo("RANKING DE BARRIOS POR RENTABILIDAD")

    ok = v[v["estimacion_confiable"] == 1]
    g = ok.groupby("barrio").agg(
        n_ventas=("venta_usd", "size"),
        venta_med=("venta_usd", "median"),
        venta_m2=("venta_m2_usd", "median"),
        alq_est=("alquiler_est_usd_mes", "median"),
        rent_bruta=("rent_bruta_pct", "median"),
        rent_neta=("rent_neta_pct", "median"),
    )
    g = g[g["n_ventas"] >= 30].sort_values("rent_bruta", ascending=False)

    rep(f"{'barrio':<20}{'n':>6}{'venta USD':>12}{'USD/m2':>9}"
        f"{'alq est':>9}{'bruta':>8}{'neta':>7}")
    rep("-" * 71)
    for b, r in g.iterrows():
        rep(f"{b:<20}{int(r['n_ventas']):>6}{r['venta_med']:>12,.0f}"
            f"{r['venta_m2']:>9,.0f}{r['alq_est']:>9,.0f}"
            f"{r['rent_bruta']:>7.2f}%{r['rent_neta']:>6.2f}%")

    # Correlacion: valida (o refuta) la hipotesis de relacion inversa
    c = g["venta_m2"].corr(g["rent_bruta"], method="spearman")
    rep("")
    rep(f"Correlacion de Spearman entre precio/m2 y rentabilidad: {c:.3f}")
    if c < -0.5:
        rep("-> Relacion inversa fuerte: los barrios caros rinden menos como renta.")
        rep("   Esto CONFIRMA la hipotesis planteada en el trabajo.")
    elif c < -0.2:
        rep("-> Relacion inversa moderada.")
    else:
        rep("-> No hay relacion inversa clara. Revisar la hipotesis.")
    return g


# ==========================================================================
# MAIN
# ==========================================================================

def main() -> int:
    p = argparse.ArgumentParser(description="Modelo de estimacion de alquiler y rentabilidad")
    p.add_argument("--input", default="data/processed/dataset_limpio.csv")
    p.add_argument("--outdir", default="data/processed")
    p.add_argument("--vacancia", type=float, default=VACANCIA_PCT,
                   help=f"%% de vacancia anual (default: {VACANCIA_PCT})")
    p.add_argument("--gastos", type=float, default=GASTOS_PCT,
                   help=f"%% de gastos del propietario (default: {GASTOS_PCT})")
    args = p.parse_args()

    if not os.path.exists(args.input):
        print(f"No existe {args.input}")
        print("Corré primero:  py limpieza.py")
        return 1

    rep = Reporte()
    rep.titulo("MODELO DE ESTIMACION DE ALQUILER")
    rep(f"Fecha   : {datetime.now():%Y-%m-%d %H:%M}")
    rep(f"Entrada : {args.input}")

    df = preparar(pd.read_csv(args.input, encoding="utf-8-sig", low_memory=False))

    alq = df[(df["operacion"] == "alquiler")].dropna(
        subset=["alquiler_usd_mes", "sup_total_m2", "barrio"])
    vta = df[(df["operacion"] == "venta")].dropna(
        subset=["venta_usd", "sup_total_m2", "barrio"])

    rep(f"Alquileres para entrenar : {len(alq):,}")
    rep(f"Ventas a estimar         : {len(vta):,}")

    pipe, smear, err_seg = entrenar_y_validar(alq, rep)
    reportar_coeficientes(pipe, rep)

    cobertura = alq.groupby("barrio").size()
    vta = imputar(vta, pipe, smear, cobertura, rep)
    vta = calcular_rentabilidad(vta, args.vacancia, args.gastos, err_seg, rep)
    rank = ranking_barrios(vta, rep)

    os.makedirs(args.outdir, exist_ok=True)
    p1 = os.path.join(args.outdir, "ventas_con_rentabilidad.csv")
    p2 = os.path.join(args.outdir, "ranking_barrios.csv")
    p3 = os.path.join(args.outdir, "reporte_modelo.txt")
    vta.to_csv(p1, index=False, encoding="utf-8-sig")
    rank.to_csv(p2, encoding="utf-8-sig")

    # --- Persistencia del modelo ---
    # Se guarda el pipeline entrenado junto con todo lo necesario para predecir
    # de forma consistente mas adelante (simular.py lo levanta de aca).
    # Sin el factor de smearing y los errores por segmento, una prediccion
    # suelta no seria comparable con las del reporte.
    p4 = os.path.join(args.outdir, "modelo_alquiler.joblib")
    joblib.dump({
        "pipeline": pipe,
        "smearing": smear,
        "error_por_segmento": err_seg,
        "bins_sup": BINS_SUP,
        "lab_sup": LAB_SUP,
        "numericas": NUMERICAS,
        "categoricas": CATEGORICAS,
        "dummies": DUMMIES,
        "barrios_validos": sorted(alq["barrio"].dropna().unique().tolist()),
        "cobertura_barrio": cobertura.to_dict(),
        "min_alq_barrio": MIN_ALQ_BARRIO,
        "alquiler_m2_por_barrio": alq.groupby("barrio")["alquiler_m2_usd_mes"]
                                     .median().to_dict(),
        "entrenado": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "n_entrenamiento": len(alq),
    }, p4)

    rep("")
    rep("Archivos generados:")
    rep(f"    {p1}")
    rep(f"    {p2}")
    rep(f"    {p4}   <- lo usa simular.py")
    rep(f"    {p3}")
    rep.guardar(p3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
