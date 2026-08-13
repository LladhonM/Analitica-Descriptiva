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

**Hecho — extracción técnica:**

- 4 scrapers funcionando, unificados a un esquema común de 55 columnas.
- CLI (`run_scrapers.py`) para correr cualquier portal/operación.
- Herramienta de diagnóstico (`diagnostico.py`) para cuando un portal deja de andar.
- 97 tests de parseo sin red (`test_parseo.py`), todos pasando.
- Datos capturados de Argenprop venta en `data/raw/`.

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
