# Scrapers inmobiliarios CABA — TP Integrador

Extracción de avisos de venta y alquiler de 4 portales, normalizados a un **esquema único de 55 columnas** para poder concatenarlos y calcular rentabilidad.

## Instalación

```bash
py -m pip install -r requirements.txt
```

> **`Brotli` no es opcional.** Sin él, MercadoLibre, Remax y Zonaprop devuelven
> 0 avisos sin dar ningún error.
> Ver [Por qué tres portales devolvían 0](#por-qué-tres-portales-devolvían-0--compresión-brotli).

## Uso

> **Importante:** todos los comandos se corren **parado en la carpeta del proyecto**
> (la que contiene `run_scrapers.py`). Si la terminal de VSCode arranca en otro lado,
> hacé primero `cd` a esa carpeta, o abrí la carpeta del proyecto con
> *File → Open Folder* para que la terminal arranque ahí sola.

```bash
# Verificar que el parseo funciona (no toca la red)
py test_parseo.py

# Empezá por Remax: es la fuente más estable
py run_scrapers.py --portal remax --operacion venta --paginas 3

# Argenprop (el script de la cátedra, robustecido)
py run_scrapers.py --portal argenprop --operacion venta --paginas 5

# Lo que necesitás para rentabilidad: venta + alquiler de todos los portales
py run_scrapers.py --portal todos --operacion ambas --paginas 10

# Consolidar todo lo capturado en un dataset maestro
py run_scrapers.py --consolidar
```

Desde un notebook:

```python
from src import argenprop, remax, utils

df = remax.scrape(operacion="venta", max_pages=5)
utils.resumen(df)
```

Los CSV salen a `data/raw/` con nombre `portal_operacion_YYYYMMDD_HHMM.csv`.

## Flags útiles

| Flag | Para qué |
|---|---|
| `--detalle` | Entra a cada ficha individual. Más lento (~2x) pero trae descripción completa y más atributos. |
| `--delay 4` | Sube la pausa entre requests. Usalo si te empiezan a devolver 403. |
| `--debug` | Guarda el HTML crudo en `data/debug/`. Imprescindible cuando un selector deja de funcionar. |
| `--tipo casas` | Cambia el tipo de inmueble (`departamentos`, `casas`, `ph`). |

## Las 4 fuentes

| Portal | Método | Confiabilidad | Notas |
|---|---|---|---|
| **Remax** | API JSON pública | Alta | La más limpia. Trae **lat/long**, clave para el join espacial de la Fase 3. Catálogo más chico. |
| **Argenprop** | HTML server-rendered | Alta | Base de la cátedra, robustecida. Buen volumen. |
| **MercadoLibre** | HTML + JSON embebido | Media-alta | Buen volumen. Sensible al ritmo: usa delay 2s. |
| **Zonaprop** | HTML + JSON embebido | **Baja** | Mayor volumen del mercado pero protegido con **DataDome**. Ver abajo. |

## Por qué tres portales devolvían 0 — compresión Brotli

**Este es el hallazgo más interesante del proceso de extracción, y sirve tal cual
para el apartado de desafíos técnicos del informe.**

Síntoma: Argenprop devolvía datos normalmente, pero MercadoLibre, Remax y Zonaprop
devolvían **0 avisos**, sin ningún error. Status HTTP 200, respuestas de ~290 KB.
Todo parecía indicar que habían cambiado los selectores CSS.

No era eso. Al inspeccionar el HTML guardado en `data/debug/`, el contenido era
**78% de bytes no imprimibles**: no era HTML, era un blob binario.

La causa: el scraper declaraba

```
Accept-Encoding: gzip, deflate, br
```

pero el paquete `Brotli` no estaba instalado. Entonces:

1. El servidor nos toma la palabra y responde comprimido con Brotli.
2. `urllib3` no sabe descomprimirlo y entrega los bytes crudos.
3. `resp.text` es basura binaria → BeautifulSoup no encuentra nada → 0 avisos.

Argenprop funcionaba porque su CDN devuelve **gzip**, que sí se descomprime con la
librería estándar.

Lo insidioso del bug es que **no falla ruidosamente**: no hay excepción, el status
es 200 y el body pesa lo esperable. Se parece exactamente a un problema de
selectores, y es fácil perder horas ahí.

**La solución tiene dos capas:**

1. `src/utils.py` ahora declara **solo los codecs que realmente puede descomprimir**
   (`_codecs_disponibles()`), verificando en tiempo de import qué librerías están.
2. Una red de seguridad (`_arreglar_compresion`) que detecta un cuerpo binario y lo
   descomprime a mano, por si el servidor comprime aunque no se lo pidamos.

```bash
py -m pip install Brotli zstandard
```

Al arrancar cualquier corrida se imprime qué codecs están activos.

## Huella TLS (`curl_cffi`)

Aparte del tema de compresión, `requests` tiene una huella TLS (*JA3 fingerprint*)
que no coincide con la de ningún navegador: orden de cipher suites, extensiones,
curvas. Los anti-bot comerciales la detectan **en el handshake, antes de leer un
solo header** — por eso no alcanza con declarar un User-Agent de Chrome.

[`curl_cffi`](https://github.com/lexiforest/curl_cffi) replica el handshake exacto
de Chrome. `utils.py` lo usa si está instalado y cae a `requests` si no:

```bash
py -m pip install curl_cffi
```

Esto importa sobre todo para Zonaprop (DataDome). Para MercadoLibre y Remax, el
problema real era la compresión.

## Zonaprop y el bloqueo anti-bot

Zonaprop usa DataDome: fingerprinting de TLS, challenge de JavaScript y rate limiting por IP. Con `requests` va a funcionar a veces y a veces devolver 403 o una página de challenge.

**Eso es material para el informe** — la consigna pide documentar los bloqueos encontrados y cómo se sortearon.

Plan B con un navegador real:

```bash
py -m pip install playwright
py -m playwright install chromium
```

```python
from src.zonaprop import scrape_con_playwright
df = scrape_con_playwright(operacion="venta", max_pages=3, headless=False)
```

Con `headless=False` ves la ventana: si aparece un captcha lo resolvés a mano y la sesión queda validada un rato.

## Qué mejora respecto del script original

1. **Precio numérico + moneda separada** — antes quedaba como string `"USD 145.000"`, imposible de promediar.
2. **m2, ambientes, dormitorios, baños, cocheras y antigüedad** parseados a números.
3. **Barrio normalizado** contra los 48 barrios oficiales de CABA, resolviendo alias (`Palermo Hollywood` → `palermo`, `Barrio Norte` → `recoleta`) + número de comuna.
4. **Venta y alquiler** (el original tenía venta hardcodeada).
5. **Selectores con fallback** — si el portal cambia una clase, prueba alternativas en vez de devolver un dataset vacío.
6. **Reintentos con backoff, checkpoints cada N páginas y deduplicación** por URL.
7. Los `except: pass` silenciosos del original se reemplazan por manejo de errores que cuenta y reporta las fallas.

## Esquema de salida

55 columnas cubriendo los 4 tipos de dato que pide la consigna:

- **Numéricas**: `precio_valor`, `sup_total_m2`, `sup_cubierta_m2`, `ambientes`, `dormitorios`, `banos`, `cocheras`, `antiguedad_anios`, `precio_m2`, `expensas_valor`, `latitud`, `longitud`
- **Categóricas/textuales**: `portal`, `operacion`, `tipo_propiedad`, `barrio`, `calle`, `titulo`, `descripcion`
- **Ordinales**: `comuna`, `piso`, `ambientes`
- **Dicotómicas (0/1)**: `amenities`, `pileta`, `parrilla`, `gimnasio`, `sum`, `losa_radiante`, `aire_acondicionado`, `apto_credito`, `baulera`, `seguridad`, `luminoso`, `balcon`, `a_reciclar`, `reciclado`, `credito_uva`, `amoblado`, `patio_jardin`, `ascensor`, `es_a_estrenar`, y más

## Estructura

```
├── src/
│   ├── utils.py          # parseo, barrios, esquema, guardado
│   ├── base.py           # BaseScraper: paginación, dedup, checkpoints
│   ├── argenprop.py
│   ├── remax.py
│   ├── mercadolibre.py
│   └── zonaprop.py
├── data/raw/             # CSVs de salida
├── data/debug/           # HTML crudo (con --debug)
├── run_scrapers.py       # CLI
├── test_parseo.py        # 97 tests, sin red
└── requirements.txt
```

## Si algo deja de funcionar

Los portales cambian el HTML seguido. Cuando un scraper devuelva 0 avisos:

```bash
py run_scrapers.py --portal argenprop --paginas 1 --debug
```

Abrí el HTML en `data/debug/`, buscá el nombre de clase nuevo de las cards y agregalo **al principio** de la lista de selectores en el módulo del portal. La estructura de `first_elements([...])` está pensada para eso: agregás un selector nuevo sin romper los viejos.
