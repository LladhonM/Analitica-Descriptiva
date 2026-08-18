# CLAUDE.md — Contexto del proyecto

Guía para retomar este trabajo. Leer entero antes de tocar código.

---

## 1. Qué es esto

TP Integrador de **82.04 Analítica Descriptiva (ITBA)**. El equipo actúa como
analistas de datos de una consultora de tecnología inmobiliaria. La materia prima
**no viene dada**: hay que construirla con web scraping, limpiarla, enriquecerla
con fuentes externas y analizarla.

**Enfoque de análisis elegido: compra de inmuebles para ponerlos en alquiler.**
Todo lo que se construya debe servir a esa pregunta (rentabilidad de la inversión).

**Alcance geográfico: CABA.**

Variables de interés declaradas: cantidad de ambientes, barrio, m², edad del
edificio, precio de venta, precio de alquiler.

La cátedra proveyó un `scrapper.py` base para Argenprop
(https://github.com/nachols1986/argenprop_scrapper). No es limitante: se espera
robustecerlo y sumar otras fuentes.

---

## 2. Estado actual

> **Esta (`/TP`) es la carpeta de trabajo del proyecto.** Hubo un clon paralelo
> en `../claude` que quedó obsoleto — no trabajar ahí.

**Hecho — extracción técnica:**

- 4 scrapers funcionando, unificados a un esquema común de 55 columnas.
- CLI (`run_scrapers.py`) para correr cualquier portal/operación.
- Herramienta de diagnóstico (`diagnostico.py`) para cuando un portal deja de andar.
- 97 tests de parseo sin red (`test_parseo.py`), todos pasando.

**Hecho — datos:**

- `data/raw/dataset_maestro.csv`: **15.261 avisos de Remax CABA**
  (13.424 venta + 1.837 alquiler), corrido **con `--detalle`**.
- Decisión metodológica: **solo Remax**, por unicidad de criterio y mejor
  normalización. Documentar en el informe como límite — Remax es una red con
  cartera propia, no una muestra del mercado completo.

**Hecho — limpieza:**

- `limpieza.py` deja el dataset analizable en `data/processed/`.
  Corre con: `py limpieza.py --input data/raw/dataset_maestro.csv --tc 1500`
- Resultado: **12.402 filas limpias (81,3%)**, 2.859 excluidas con motivo
  trazable en `dataset_excluidos.csv`.
- Ver sección 10 para los criterios y las trampas de este dataset.

**Pendiente — todo lo demás de la Actividad #1:**

- [ ] Notebook de extracción documentado (evidencia de la corrida para el entregable).
- [ ] Corridas de volumen: los 4 portales, venta **y** alquiler.
- [ ] Definición de negocio: contexto, perfil de cliente/interlocutor.
- [ ] Preguntas clave en los 4 niveles (descriptivo, diagnóstico, predictivo, prescriptivo).
- [ ] KPIs con definición **matemática y comercial** (ej. Rentabilidad Bruta =
      (Precio Alquiler Promedio Zona × 12) / Precio Venta Inmueble).
- [ ] Al menos 3 hipótesis a validar.
- [ ] Listado justificado de fuentes externas para la Fase 3.
- [ ] Repo en GitHub con estructura `/data/raw` + notebooks documentados.
- [ ] PDF (o README.md) con los 7 apartados que pide la consigna.

---

## 3. Estructura

```
├── src/
│   ├── utils.py          # parseo, barrios CABA, esquema, HTTP, guardado
│   ├── base.py           # BaseScraper: paginación, dedup, checkpoints
│   ├── argenprop.py      # HTML server-rendered
│   ├── remax.py          # API JSON pública (la más confiable)
│   ├── mercadolibre.py   # HTML + JSON embebido
│   └── zonaprop.py       # HTML + JSON embebido (+ plan B Playwright)
├── data/raw/             # CSVs de salida
├── data/debug/           # HTML crudo (con --debug), gitignoreado
├── run_scrapers.py       # CLI
├── diagnostico.py        # por qué un scraper devuelve 0
├── test_parseo.py        # 97 tests, sin red
└── requirements.txt
```

---

## 4. Cómo correr

```bash
py -m pip install -r requirements.txt
py test_parseo.py                                          # verificar sin red
py run_scrapers.py --portal remax --operacion venta --paginas 3
py run_scrapers.py --portal todos --operacion ambas --paginas 10
py run_scrapers.py --consolidar                            # dataset maestro
py diagnostico.py mercadolibre                             # cuando algo da 0
```

### Pipeline de análisis

```bash
py limpieza.py --tc 1500        # crudo -> data/processed/dataset_limpio.csv
py modelo_alquiler.py           # estima alquiler + rentabilidad de cada venta
py simular.py                   # simulador interactivo de una propiedad
```

`simular.py` también acepta todo por argumentos:

```bash
py simular.py --barrio palermo --m2 65 --ambientes 3 --antiguedad 20 --balcon
py simular.py --barrio "puerto madero" --m2 80 --ambientes 3 --venta 350000
```

Levanta el modelo de `data/processed/modelo_alquiler.joblib`, que genera
`modelo_alquiler.py`. Si no existe, hay que correr los dos pasos previos.

Desde notebook:

```python
from src import argenprop, remax, mercadolibre, zonaprop, utils
df = remax.scrape(operacion="venta", max_pages=5)
utils.resumen(df)
```

Flags: `--detalle` (entra a cada ficha, ~2x más lento, más variables),
`--delay 4` (si bloquean), `--debug` (guarda HTML crudo), `--tipo casas`.

---

## 5. Decisiones y trampas — LEER ANTES DE DEBUGGEAR

### 5.1 El bug de compresión Brotli (el más importante)

**Síntoma:** un portal devuelve 0 avisos. Status 200, respuesta de ~290 KB, ninguna
excepción. Parece un cambio de selectores. **No lo es.**

**Causa:** si se declara `Accept-Encoding: ... br` y el paquete `Brotli` no está
instalado, el servidor responde comprimido, urllib3 no sabe descomprimirlo y
entrega bytes crudos. `resp.text` es basura binaria.

**Cómo verificarlo:** mirar el HTML de `data/debug/`. Si tiene un alto porcentaje
de bytes no imprimibles, es esto y no los selectores.

**Ya está resuelto en `utils.py`** por dos vías:
1. `_codecs_disponibles()` declara solo los codecs realmente descomprimibles.
2. `_arreglar_compresion()` detecta cuerpos binarios y descomprime a mano.

**No revertir esto.** Si se toca `HEADERS_BASE`, no hardcodear `br`.

Argenprop nunca falló porque su CDN devuelve gzip (stdlib).

### 5.2 Huella TLS

`requests` tiene un JA3 fingerprint que no es el de ningún navegador. Los anti-bot
lo detectan en el handshake, antes de leer headers — el User-Agent no ayuda.
`utils.py` usa `curl_cffi` (imita a Chrome) si está instalado, con fallback a
`requests`. Importa sobre todo para Zonaprop.

### 5.3 Zonaprop y DataDome

Es el portal de mayor volumen del mercado y el más protegido: fingerprinting,
challenge de JavaScript, rate limiting, captcha. Con `requests` funciona a veces.
Hay plan B con Playwright en `zonaprop.scrape_con_playwright(headless=False)`.

**Este bloqueo es contenido para el informe**, no un fracaso: la consigna pide
documentar los desafíos técnicos encontrados y cómo se sortearon.

### 5.4 Imports relativos

Los módulos de `src/` usan imports relativos. Cada uno tiene un shim al inicio
(`if __package__ in (None, "")`) que permite ejecutarlos directamente además de
importarlos. **No sacar ese shim.**

### 5.5 MercadoLibre

Cerró el acceso anónimo a su API oficial (`api.mercadolibre.com`); hoy exige un
token de app registrada. Por eso se scrapea HTML. Si el bloqueo se vuelve
persistente, tramitar credenciales en developers.mercadolibre.com.ar es la salida
limpia — y es otro punto documentable.

`mercadolibre.py` prueba 4 variantes de URL automáticamente y recuerda cuál funcionó.

### 5.6 Selectores con fallback

`first_text()` y `first_elements()` (en `base.py`) prueban una lista de selectores
en orden. Cuando un portal cambie el markup: correr con `--debug`, mirar el HTML,
y agregar el selector nuevo **al principio** de la lista. No reemplazar los viejos.

---

## 6. Esquema de datos

55 columnas, definidas en `utils.SCHEMA`. Cubre los 4 tipos de dato que pide la
consigna:

- **Numéricas:** `precio_valor`, `sup_total_m2`, `sup_cubierta_m2`, `ambientes`,
  `dormitorios`, `banos`, `cocheras`, `antiguedad_anios`, `precio_m2`,
  `expensas_valor`, `latitud`, `longitud`
- **Textuales/categóricas:** `portal`, `operacion`, `tipo_propiedad`, `barrio`,
  `calle`, `titulo`, `descripcion`
- **Ordinales:** `comuna`, `piso`, `ambientes`
- **Dicotómicas (0/1):** `amenities`, `pileta`, `parrilla`, `gimnasio`, `sum`,
  `losa_radiante`, `aire_acondicionado`, `apto_credito`, `baulera`, `seguridad`,
  `luminoso`, `balcon`, `a_reciclar`, `reciclado`, `credito_uva`, `amoblado`,
  `patio_jardin`, `ascensor`, `es_a_estrenar`

**Precio:** siempre separado en `precio_valor` (float) + `precio_moneda`
(`USD`/`ARS`). Nunca como string. Cuidado al agregar: **no promediar USD con ARS**.

**Barrio:** normalizado contra los 48 barrios oficiales de CABA, resolviendo alias
(`Palermo Hollywood` → `palermo`, `Barrio Norte` → `recoleta`, `Once` → `balvanera`).
`comuna` se deriva del barrio. Ver `BARRIOS_CABA` y `ALIAS_BARRIOS` en `utils.py`.

**Remax es la única fuente con lat/long confiable** — clave para joins espaciales.

---

## 7. Próximos pasos sugeridos

### Inmediato
1. Correr volumen: los 4 portales × venta y alquiler. Verificar completitud con
   `utils.resumen(df)`.
2. Consolidar y revisar duplicados entre portales (un mismo inmueble suele estar
   publicado en varios; hoy solo se deduplica por `url` e `id_aviso` dentro de
   cada portal — un dedup cruzado por dirección + m² + precio sería una mejora).

### Notebook de extracción
Debe mostrar evidencia: shape de los dataframes, `df.dtypes`, `df.head()`,
completitud por variable, y los tipos de dato capturados. Documentar bloqueos
encontrados (Brotli, DataDome) y cómo se sortearon.

### KPIs a definir
El principal, dado el enfoque de inversión para renta:

```
Rentabilidad Bruta Anual = (Precio Alquiler Promedio de la Zona × 12) / Precio de Venta
```

Ojo con la **coherencia de monedas**: los alquileres en CABA suelen publicarse en
ARS y las ventas en USD. Hace falta el tipo de cambio (ver fuentes externas) y
decidir explícitamente en qué moneda se expresa el KPI.

Otros candidatos: precio/m² por barrio, cap rate neto (descontando expensas e
impuestos), meses de repago, dispersión de precios intra-barrio.

### Fuentes externas para la Fase 3
Buscar en **BA Data** (data.buenosaires.gob.ar) y datos.gob.ar:
- Polígonos de barrios y comunas de CABA (GeoJSON) → join espacial con lat/long
- Estaciones de subte, Metrobús y paradas de colectivo → accesibilidad
- Ubicación de comisarías / datos de delitos → seguridad
- Espacios verdes, establecimientos educativos y de salud
- Tipo de cambio histórico (BCRA API) → conversión USD/ARS
- Índice de inflación / ICC (INDEC) → deflactar series
- Censo 2022 por radio censal → nivel socioeconómico

Justificar cada una en función de las hipótesis.

### Hipótesis (ejemplos para desarrollar)
- El precio por m² decrece con la distancia a la estación de subte más cercana.
- La rentabilidad bruta es **inversamente** proporcional al precio del m² del barrio
  (los barrios premium rinden menos como renta).
- Los inmuebles "a reciclar" tienen un descuento medible sobre el precio esperado
  según sus características, y ese descuento es la oportunidad de arbitraje.
- La antigüedad impacta más en el precio de venta que en el de alquiler.

---

## 8. Convenciones

- Código y comentarios **en español**, sin tildes en los comentarios del código
  (para evitar problemas de encoding en Windows). En Markdown sí van tildes.
- Los comentarios explican **por qué**, no qué. Documentar decisiones y trampas.
- No usar `except: pass`. Contar los errores y reportarlos (el script original de
  la cátedra tenía este problema y ocultaba fallas).
- Todo parseo nuevo va con test en `test_parseo.py`. Correrlo antes de commitear.
- Los CSV se guardan con `utf-8-sig` para que Excel abra bien las tildes.
- Ser respetuoso con los portales: mantener los delays. No bajar `delay_base`
  para ir más rápido.

---

## 9. Git

El repo todavía no está inicializado. La consigna pide repositorio colaborativo
en GitHub con estructura `/data/raw` y notebooks documentados.

```bash
git init
git add .
git commit -m "Scrapers de 4 portales inmobiliarios con esquema unificado"
```

`.gitignore` ya excluye `__pycache__`, `data/debug/` y los checkpoints parciales.
**Decidir en equipo si los CSV de `data/raw/` se versionan** — la consigna sugiere
que sí (pide la carpeta en la estructura), pero ojo con el tamaño si crecen mucho.

---

## 10. Limpieza del dataset — criterios y trampas

`limpieza.py` no borra nada en silencio: todo lo excluido queda en
`data/processed/dataset_excluidos.csv` con la columna `motivo_exclusion`.

### 10.1 La trampa de `precio_m2`

En el dataset **crudo**, `precio_m2` es `precio_valor / sup_total_m2` sin ninguna
conversión, así que mezcla **cuatro unidades distintas** en una sola columna:

| operación | moneda | mediana | unidad real |
|---|---|---:|---|
| venta | USD | 1.903 | USD/m² |
| alquiler | ARS | 17.981 | ARS/m²/**mes** |
| alquiler | USD | 15,6 | USD/m²/**mes** |

Además de la moneda hay un problema **dimensional**: venta es un precio (stock),
alquiler es un flujo mensual. No son comparables ni en la misma moneda.

`df.groupby('barrio')['precio_m2'].mean()` sobre el crudo devuelve un número sin
significado. **Usar siempre las columnas del dataset limpio**, cuyo nombre declara
la unidad: `venta_usd`, `alquiler_usd_mes`, `venta_m2_usd`, `alquiler_m2_usd_mes`.
Cada una se llena solo para su operación; en una fila de venta,
`alquiler_usd_mes` es `NaN`.

### 10.2 Errores de carga corregidos

Seis registros con la moneda u operación mal cargada, corregidos por `id_aviso`
en el paso 1 de `limpieza.py`:

- 1 publicado como **venta en ARS** que era alquiler (600.000 ARS, título "ALQUILER").
- 5 publicados como **alquiler en USD** cuyo monto solo tiene sentido en pesos
  (650.000 / 900.000 / 1.000.000 / 2.400.000). Verificado por dos vías: caen en el
  rango normal de alquileres ARS, y leídos como ARS/m²/mes dan valores coherentes.
- Uno de ellos (`78f7fd56…`) además tiene la superficie mal (694 m² para un
  monoambiente): se le anula `sup_total_m2` en vez de inventar un valor.

Corregir estas 5 filas sobre 434 baja la media de alquileres USD **un 82%**.

### 10.3 Tipo de cambio

Parámetro `--tc` (default 1500). El script además calcula el **TC implícito**
del propio mercado: mediana de ARS/m² dividido mediana de USD/m² en alquileres
(da ~1.122). Compara precios **por m²**, no totales, porque los alquileres en
dólares son sistemáticamente propiedades más grandes.

La rentabilidad es muy sensible al TC: pasar de 1.200 a 1.500 mueve el KPI de
7,35%–4,32% a 6,54%–3,86%. **El orden entre barrios se mantiene** — las
conclusiones comparativas son robustas, el nivel absoluto no.

### 10.4 Cuántos alquileres hacen falta

Hay ~8 ventas por cada alquiler. Eso condiciona cómo estimar el alquiler
esperado de una propiedad en venta:

- **Por barrio × ambientes:** solo el 28% de las ventas cae en una celda con 20+
  alquileres. **No alcanza.**
- **Por barrio:** 85,6% de cobertura con 10+ alquileres. Error relativo mediano
  del 11,3% (bootstrap sobre la mediana). Sirve para el KPI agregado.
- **Modelo de regresión** sobre `log(alquiler)` con superficie, ambientes, baños,
  antigüedad, barrio y dummies: **R² = 0,84 y error mediano 11,5%** en validación
  cruzada, cubriendo el 100% de las ventas. Es la mejor opción.

La curva de aprendizaje **se aplana alrededor de n=674**: scrapear más alquileres
no mejoraría la estimación. El límite es la variabilidad del mercado, no el tamaño
de la muestra.

Ese 11,5% de error se propaga al KPI: una rentabilidad real del 5% se estima
entre 4,4% y 5,6%. Alcanza para **rankear barrios**, no para decidir sobre un
inmueble puntual.
