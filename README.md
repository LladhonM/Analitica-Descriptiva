# Análisis de rentabilidad inmobiliaria en CABA

**TP Integrador — 82.04 Analítica Descriptiva (ITBA)**

Construcción de un dataset del mercado inmobiliario porteño mediante web scraping,
su limpieza y su análisis, orientado a una pregunta: **¿qué conviene comprar para
poner en alquiler?**

La materia prima no venía dada. Este repositorio documenta cómo se construyó,
qué decisiones se tomaron y qué se encontró.

---

## Reproducir el análisis

```bash
py -m pip install -r requirements.txt

py test_parseo.py                    # verifica el parseo, no toca la red
py run_scrapers.py --portal remax --operacion ambas --paginas 200 --detalle
py limpieza.py --tc 1500             # crudo -> dataset_limpio
py modelo_alquiler.py                # estima alquiler + rentabilidad
py simular.py                        # simulador de una propiedad
```

Los tres últimos comandos reconstruyen `data/processed/` completo en un minuto a
partir de `data/raw/dataset_maestro.csv`, que es lo único que se versiona.

> `Brotli` no es opcional. Sin ese paquete el scraper devuelve 0 avisos sin dar
> ningún error. Ver [Desafíos técnicos](#a-desafíos-técnicos-encontrados).

---

## 1. Descripción del dataset y fuentes

### Fuente utilizada

**RE/MAX Argentina**, a través de su API JSON pública (`api.redremax.com`), la
misma que consume su sitio web.

Se evaluaron y descartaron otras tres fuentes:

| Portal | Método evaluado | Resultado |
|---|---|---|
| Argenprop | HTML server-rendered | Funcional. Descartado por criterio de unicidad. |
| MercadoLibre | HTML + JSON embebido | Funcional. Descartado por criterio de unicidad. |
| Zonaprop | HTML + JSON embebido | Bloqueado por DataDome (ver anexo A). |
| **RE/MAX** | **API JSON** | **Fuente elegida.** |

**Por qué una sola fuente.** Consolidar cuatro portales exige un dedup cruzado por
dirección, superficie y precio, porque un mismo inmueble suele publicarse en
varios. Ese cruce es impreciso y arrastra un error difícil de cuantificar. Además,
cada portal normaliza distinto los barrios, las superficies y las tipologías.
Se priorizó la **consistencia interna** sobre el volumen.

**Límite que esto impone.** RE/MAX es una red inmobiliaria con cartera propia y
exclusividades, no un clasificado abierto. El dataset describe *su* oferta, no el
mercado completo de CABA. Las conclusiones comparativas entre barrios son válidas;
los niveles absolutos deben leerse como representativos de ese segmento.

### Volumen y composición

| | Crudo | Limpio |
|---|---:|---:|
| Total de avisos | 15.261 | 12.097 |
| Venta | 13.424 | 10.749 |
| Alquiler | 1.837 | 1.348 |

Alcance geográfico: **48 barrios de CABA**. Captura realizada con `--detalle`, que
entra a cada ficha individual — sin ese flag, la antigüedad y las descripciones
vienen vacías.

### Tipos de dato capturados

El esquema unificado tiene **55 columnas** que cubren los cuatro tipos que pide la
consigna:

- **Numéricas** — `precio_valor`, `sup_total_m2`, `sup_cubierta_m2`, `ambientes`,
  `dormitorios`, `banos`, `cocheras`, `antiguedad_anios`, `expensas_valor`,
  `latitud`, `longitud`
- **Categóricas / textuales** — `operacion`, `tipo_propiedad`, `barrio`, `calle`,
  `titulo`, `descripcion`
- **Ordinales** — `comuna`, `antiguedad_rango`, `superficie_rango`
- **Dicotómicas (0/1)** — `amenities`, `pileta`, `parrilla`, `gimnasio`,
  `losa_radiante`, `aire_acondicionado`, `apto_credito`, `baulera`, `seguridad`,
  `luminoso`, `balcon`, `a_reciclar`, `reciclado`, `amoblado`, `patio_jardin`,
  `ascensor`, `es_a_estrenar`

RE/MAX es la única de las cuatro fuentes con **latitud y longitud confiables**
(99,9% de completitud), lo que habilita los joins espaciales de la Fase 3.

### Fuentes externas previstas para la Fase 3

| Fuente | Uso | Hipótesis que alimenta |
|---|---|---|
| Polígonos de barrios y comunas (BA Data) | Join espacial con lat/long | Validación de la asignación de barrio |
| Estaciones de subte y Metrobús (BA Data) | Distancia a transporte | H1: el precio/m² decrece con la distancia al subte |
| Delitos por comuna (BA Data) | Índice de seguridad | Efecto de la seguridad sobre la rentabilidad |
| Espacios verdes (BA Data) | Distancia a plazas y parques | Prima por cercanía a espacio verde |
| Tipo de cambio (API BCRA) | Conversión ARS/USD | Sensibilidad del KPI al TC (ver anexo B) |
| Censo 2022 por radio censal (INDEC) | Nivel socioeconómico | Segmentación de demanda |

---

## 2. Contexto y situación de negocio

> **Sección a completar por el equipo.** Requiere definir el perfil del cliente
> antes de escribirla. La elección cambia los KPIs: un fondo que compra en volumen
> optimiza rentabilidad media y liquidez; un particular con ahorros optimiza riesgo
> y previsibilidad; un desarrollador busca inmuebles a reciclar con descuento.
>
> El dataset soporta cualquiera de los tres. La decisión es del equipo.

---

## 3. Preguntas clave según los 4 niveles de análisis

### Descriptivo — ¿qué pasó?

- ¿Cuál es el precio por m² mediano de cada barrio de CABA?
- ¿Cómo se distribuye la oferta entre tipologías y rangos de superficie?
- ¿Qué proporción de la oferta tiene cochera, balcón o amenities?
- ¿Cuál es la antigüedad mediana del stock por barrio?

### Diagnóstico — ¿por qué pasó?

- ¿Qué características explican la variación del precio de alquiler?
  *(respondido: ver [anexo C](#c-modelo-de-estimación-de-alquiler))*
- ¿Por qué los barrios premium rinden menos como renta?
- ¿Cuánto del precio se explica por ubicación y cuánto por atributos del inmueble?

### Predictivo — ¿qué va a pasar?

- Dado un inmueble en venta, ¿cuánto se alquilaría?
  *(respondido: modelo con R² = 0,851 y error mediano 11,4%)*
- ¿Qué rentabilidad esperada tiene cada propiedad publicada?

### Prescriptivo — ¿qué conviene hacer?

- ¿En qué barrios conviene comprar para maximizar renta?
  *(respondido: ver [ranking](#resultado-principal))*
- ¿Qué combinación de barrio, tipología y superficie ofrece la mejor relación
  rentabilidad/riesgo?
- ¿Conviene pagar la prima por cochera o amenities en términos de renta?

---

## 4. Hipótesis

**H1 — La rentabilidad bruta es inversamente proporcional al precio del m² del
barrio.** Los barrios premium rinden menos como renta.

> **Validada.** Correlación de Spearman entre precio/m² y rentabilidad bruta:
> **−0,923** sobre 29 barrios con 30+ ventas. Constitución rinde 10,19% contra
> 6,12% de Palermo.

**H2 — El precio por m² decrece con la distancia a la estación de subte más
cercana.** Requiere el dataset de transporte de BA Data (Fase 3).

**H3 — Los inmuebles "a reciclar" cotizan con descuento respecto de su valor
esperado según características, y ese descuento es la oportunidad de arbitraje.**
La dummy `a_reciclar` está en el dataset (4,3% de prevalencia). Pendiente.

**H4 — La antigüedad impacta más en el precio de venta que en el de alquiler.**
Parcialmente medible: en el modelo de alquiler, la antigüedad resta 5,3% por
desvío estándar. Falta el modelo equivalente para venta.

---

## 5. Objetivo principal del análisis

> **Sección a completar por el equipo**, una vez definido el cliente del punto 2.
>
> Formulación tentativa: *identificar, dentro de la oferta de CABA, los segmentos
> de inmuebles que maximizan la rentabilidad esperada de una inversión para renta,
> cuantificando la incertidumbre de esa estimación.*

---

## 6. Alcance

**Incluye.** Inmuebles de vivienda individual (departamentos, PH y casas)
publicados en CABA por RE/MAX, en operaciones de venta y alquiler, capturados en
agosto de 2026.

**Excluye.** Cocheras, oficinas, locales, terrenos, depósitos, galpones,
consultorios, hoteles y edificios completos — 2.270 avisos que no responden a la
tesis de inversión y distorsionan el precio/m².

**Limitaciones declaradas.**

1. Fuente única: la cartera de RE/MAX, no el mercado completo.
2. Corte temporal único: no permite análisis de series ni estacionalidad.
3. Precios de **publicación**, no de cierre. En un mercado con negociación, el
   precio final suele ser menor.
4. Ratio de 8 ventas por cada alquiler: la estimación de renta se apoya en un
   modelo, no en observación directa.

---

## 7. Definición del usuario final

> **Sección a completar por el equipo**, alineada con el punto 2.
>
> El deliverable técnico ya soporta dos perfiles de uso: consulta agregada
> (`ranking_barrios.csv`, para decidir dónde buscar) y consulta puntual
> (`simular.py`, para evaluar una propiedad concreta).

---

## Resultado principal

Rentabilidad bruta anual estimada por barrio, con TC 1500 ARS/USD:

| Barrio | Venta mediana USD | USD/m² | Alquiler est. | Bruta | Neta |
|---|---:|---:|---:|---:|---:|
| Constitución | 65.000 | 1.129 | 570 | 10,19% | 8,25% |
| San Cristóbal | 88.000 | 1.548 | 603 | 8,63% | 6,99% |
| Balvanera | 84.950 | 1.493 | 580 | 8,30% | 6,72% |
| Barracas | 110.000 | 1.533 | 738 | 8,21% | 6,65% |
| San Nicolás | 85.000 | 1.616 | 605 | 8,20% | 6,64% |
| Flores | 119.950 | 1.516 | 768 | 8,09% | 6,55% |
| Almagro | 119.000 | 1.931 | 689 | 7,25% | 5,87% |
| Villa Crespo | 119.250 | 2.126 | 693 | 7,16% | 5,79% |
| Caballito | 145.000 | 2.078 | 812 | 6,80% | 5,51% |
| Recoleta | 169.000 | 2.333 | 850 | 6,38% | 5,17% |
| Belgrano | 175.000 | 2.577 | 900 | 6,21% | 5,03% |
| Palermo | 163.250 | 2.720 | 804 | 6,12% | 4,95% |

*Tabla parcial. El ranking completo (29 barrios) está en
`data/processed/ranking_barrios.csv`.*

**Advertencia de lectura.** Mayor rentabilidad no equivale a mejor inversión. Los
barrios del tope del ranking concentran también mayor riesgo de vacancia, mayor
morosidad y menor revalorización del capital. El KPI mide renta, no retorno total.

### Definición de los KPIs

```
Rentabilidad Bruta Anual = (Alquiler mensual estimado × 12) / Precio de venta

Rentabilidad Neta Anual  = Bruta × (1 − vacancia) × (1 − gastos)

Meses de repago          = Precio de venta / Alquiler mensual estimado
```

Supuestos por defecto: vacancia 8% (un mes cada doce entre inquilinos) y gastos
del propietario 12% (administración, ABL, expensas extraordinarias,
mantenimiento). Ambos parametrizables con `--vacancia` y `--gastos`.

Las expensas ordinarias **no se descuentan**: en CABA las paga el inquilino.

---

# Anexos técnicos

## A. Desafíos técnicos encontrados

### A.1 Compresión Brotli — el bug silencioso

**Síntoma.** Tres de los cuatro portales devolvían 0 avisos. Status HTTP 200,
respuestas de ~290 KB, ninguna excepción. Todo indicaba un cambio de selectores CSS.

**Diagnóstico.** El HTML guardado en `data/debug/` era **78% de bytes no
imprimibles**: no era HTML, era un blob binario.

**Causa.** El scraper declaraba `Accept-Encoding: gzip, deflate, br` pero el
paquete `Brotli` no estaba instalado. El servidor toma la palabra y responde
comprimido, urllib3 no sabe descomprimirlo y entrega los bytes crudos. `resp.text`
devuelve basura binaria y BeautifulSoup no encuentra nada.

Argenprop nunca falló porque su CDN devuelve **gzip**, que la librería estándar sí
descomprime. Esa asimetría hacía parecer que el problema era de los otros portales.

**Lo insidioso** es que no falla ruidosamente: no hay excepción, el status es 200 y
el tamaño del body es el esperable. Es indistinguible de un problema de selectores.

**Solución, en dos capas** (`src/utils.py`):

1. `_codecs_disponibles()` verifica en tiempo de import qué librerías de
   descompresión están instaladas y declara **solo esos codecs**.
2. `_arreglar_compresion()` detecta un cuerpo binario y lo descomprime a mano, por
   si el servidor comprime aunque no se lo hayamos pedido.

Probado contra gzip, brotli, zstd y deflate.

### A.2 Huella TLS

`requests` tiene un fingerprint TLS (JA3) que no coincide con el de ningún
navegador: orden de cipher suites, extensiones, curvas elípticas. Los anti-bot
comerciales lo detectan **en el handshake, antes de leer un solo header** — por eso
declarar un User-Agent de Chrome no ayuda.

`utils.py` usa [`curl_cffi`](https://github.com/lexiforest/curl_cffi), que replica
el handshake exacto de Chrome, con fallback a `requests` si no está instalado.

### A.3 Zonaprop y DataDome

Zonaprop es el portal de mayor volumen del mercado y el más protegido:
fingerprinting de TLS, challenge de JavaScript, rate limiting por IP y captcha.
Ninguna librería HTTP resuelve el challenge de JS.

Se implementó un plan B con Playwright (`scrape_con_playwright`), que corre un
Chromium real. Funciona, pero el rendimiento y la fragilidad del enfoque —sumados
a la decisión de fuente única— llevaron a descartar el portal.

### A.4 API oficial de MercadoLibre

MercadoLibre cerró el acceso anónimo a `api.mercadolibre.com`; hoy exige un token
de aplicación registrada. Por eso se scrapeó HTML. La salida limpia sería tramitar
credenciales en developers.mercadolibre.com.ar.

---

## B. Criterios de limpieza

`limpieza.py` no borra nada en silencio: cada fila excluida queda en
`data/processed/dataset_excluidos.csv` con su `motivo_exclusion`.

**Resultado: 12.097 filas limpias (81,4%), 2.770 excluidas.**

| Motivo | Filas |
|---|---:|
| No residencial | 2.270 |
| Duplicado del mismo inmueble | 277 |
| Outlier de precio/m² en venta | 226 |
| Precio ausente o cero | 49 |
| Outlier de precio/m² en alquiler | 30 |
| Superficie fuera de rango | 7 |

### B.1 La trampa de `precio_m2`

En el dataset **crudo**, `precio_m2` es `precio_valor / sup_total_m2` sin ninguna
conversión. Eso mezcla **cuatro unidades distintas** en una sola columna:

| Operación | Moneda | Mediana | Unidad real |
|---|---|---:|---|
| Venta | USD | 1.903 | USD/m² |
| Alquiler | ARS | 17.981 | ARS/m²/**mes** |
| Alquiler | USD | 15,6 | USD/m²/**mes** |

Además de la moneda hay un problema **dimensional**: venta es un precio (stock),
alquiler es un flujo mensual. No son comparables ni en la misma moneda.

Un `groupby('barrio')['precio_m2'].mean()` sobre el crudo devuelve un número sin
significado. El dataset limpio resuelve esto con columnas cuyo nombre **declara la
unidad**: `venta_usd`, `alquiler_usd_mes`, `venta_m2_usd`, `alquiler_m2_usd_mes`.
Cada una se llena solo para su operación, así mezclarlas por accidente es imposible.

### B.2 Errores de carga corregidos

Seis registros con la moneda u operación mal cargada por el publicador:

- **1 publicado como venta en ARS** que era alquiler (600.000 ARS, título "ALQUILER").
- **5 publicados como alquiler en USD** cuyo monto solo tiene sentido en pesos
  (650.000 / 900.000 / 1.000.000 / 2.400.000). Verificado por dos vías: caen en el
  rango normal de alquileres en pesos (percentiles 31 a 95), y leídos como ARS/m²/mes
  dan valores coherentes con la mediana del mercado.

Uno de ellos tiene además la superficie mal cargada (694 m² para un monoambiente):
se le anula `sup_total_m2` en vez de inventar un valor.

**Impacto:** corregir 5 filas sobre 434 baja la media de alquileres en USD un **82%**.

### B.3 Tipo de cambio

Parámetro `--tc`, default 1500. El script calcula además el **TC implícito** del
propio mercado: mediana de ARS/m² dividida por mediana de USD/m² en alquileres
(≈1.122). Compara precios **por m²** y no totales, porque los alquileres publicados
en dólares son sistemáticamente propiedades más grandes.

La rentabilidad es sensible al TC: pasar de 1.200 a 1.500 mueve el KPI de
7,35%–4,32% a 6,54%–3,86%. **El orden entre barrios se mantiene** — las
conclusiones comparativas son robustas, el nivel absoluto no.

---

## C. Modelo de estimación de alquiler

### El problema

Hay 8 ventas por cada alquiler. Para calcular la rentabilidad de una propiedad en
venta hace falta saber cuánto se alquilaría, pero esa propiedad no está publicada
en alquiler.

### Por qué no alcanza con promediar por celda

| Estrato | Cobertura de las ventas con n≥20 |
|---|---:|
| Barrio × ambientes | 28,2% |
| Comuna × ambientes | 48,7% |
| Barrio | 67,1% |

Con la granularidad intuitiva (barrio × ambientes) tres cuartos del dataset quedan
sin estimación confiable.

### Solución: regresión sobre log(alquiler)

Se modela el **logaritmo** porque el precio inmobiliario es multiplicativo, no
aditivo: una cochera no suma "USD 80", suma un porcentaje. Eso además estabiliza
la varianza y hace que los coeficientes se lean como efectos porcentuales.

Al volver a la escala original se aplica la **corrección de Duan (smearing)**,
porque E[exp(X)] ≠ exp(E[X]).

**Validación 5-fold, fuera de muestra:**

| Métrica | Valor |
|---|---:|
| R² (en log) | 0,851 |
| Error relativo mediano | 11,4% |
| Predicciones con error < 20% | 73,3% |
| Control: mediana barrio × m² | 14,2% |

### De dónde viene el poder predictivo

| Modelo | Columnas | R² |
|---|---:|---:|
| Solo superficie | 1 | 0,739 |
| + ambientes, baños, dormitorios, antigüedad | 5 | 0,778 |
| + barrio | 46 | 0,822 |
| + tipo de propiedad | 49 | 0,822 |
| + las 14 dummies | 63 | 0,851 |

La superficie sola explica el 87% del poder predictivo. El tipo de propiedad no
aporta nada una vez conocidos superficie, ambientes y barrio.

**El R² no está inflado por cantidad de variables.** Se verificó agregando
predictoras de ruido puro: con 100 variables aleatorias el R² fuera de muestra
**baja** de 0,851 a 0,834. La brecha entre R² in-sample (0,862) y out-of-sample
(0,851) es de 0,011 — un modelo sobreajustado mostraría 0,10 o más.

### El error no es uniforme

| Segmento | Error mediano |
|---|---:|
| < 35 m² | 10,5% |
| 35–50 m² | 10,0% |
| 50–70 m² | 12,8% |
| 70–100 m² | 15,2% |
| 100+ m² | 18,0% |

Como las propiedades en venta son más grandes que las alquiladas (mediana 66
contra 45 m²), el error ponderado sobre las ventas es **13,8%**, no 11,4%. Cada
propiedad recibe la banda que le corresponde según su tamaño.

**Propagación al KPI:** una rentabilidad estimada del 6% corresponde a un valor
real entre 5,2% y 6,8%. Alcanza para **rankear barrios**, no para dictaminar sobre
un inmueble puntual.

### Cuántos alquileres hacen falta

La curva de aprendizaje se aplana alrededor de **n = 674**: con la mitad de los
alquileres se obtiene prácticamente el mismo resultado.

| n | R² | Error |
|---:|---:|---:|
| 202 | 0,789 | 13,1% |
| 674 | 0,838 | 11,7% |
| 1.348 | 0,843 | 11,7% |

Scrapear más alquileres no mejoraría la estimación: el techo lo pone la
variabilidad del mercado, no el tamaño de la muestra.

### Simulador

```bash
py simular.py                                    # interactivo
py simular.py --barrio palermo --m2 65 --ambientes 3 --antiguedad 20 --balcon
py simular.py --barrio belgrano --m2 80 --venta 250000
```

Devuelve el alquiler estimado, su banda de error, la comparación contra la mediana
del barrio y —si se pasa `--venta`— la rentabilidad de la operación.

---

## D. Mejoras sobre el script base de la cátedra

1. **Precio numérico con moneda separada.** Antes quedaba como string
   `"USD 145.000"`, imposible de promediar.
2. **Superficie, ambientes, dormitorios, baños, cocheras y antigüedad** parseados
   a valores numéricos.
3. **Barrio normalizado** contra los 48 barrios oficiales de CABA, resolviendo
   alias (`Palermo Hollywood` → `palermo`, `Barrio Norte` → `recoleta`,
   `Once` → `balvanera`), con derivación de comuna.
4. **Venta y alquiler** parametrizados (el original tenía venta hardcodeada).
5. **Selectores con fallback**: si el portal cambia una clase, prueba alternativas
   en vez de devolver un dataset vacío.
6. **Reintentos con backoff exponencial, checkpoints y deduplicación.**
7. **Manejo de errores explícito.** Los `except: pass` del script original ocultaban
   fallas; ahora se cuentan y se reportan.
8. **97 tests de parseo sin red** (`test_parseo.py`), reducidos a 82 al descartar
   los portales no utilizados.

---

## Estructura del repositorio

```
├── src/                          paquete de extracción
│   ├── utils.py                  parseo, barrios, esquema, HTTP, compresión
│   ├── base.py                   BaseScraper: paginación, dedup, checkpoints
│   └── remax.py                  cliente de la API de RE/MAX
├── notebooks/
│   ├── 01_extraccion.ipynb       evidencia del scraping
│   └── 02_analisis_exploratorio.ipynb
├── data/
│   ├── raw/dataset_maestro.csv   FUENTE — se versiona
│   ├── processed/                derivados — no se versionan
│   └── debug/                    HTML crudo — no se versiona
├── run_scrapers.py               CLI de extracción
├── limpieza.py                   crudo -> dataset limpio
├── modelo_alquiler.py            estimación de alquiler + rentabilidad
├── simular.py                    simulador de una propiedad
├── test_parseo.py                tests sin red
├── requirements.txt
└── README.md                     este documento
```

**Qué se versiona y qué no.** `data/raw/dataset_maestro.csv` sí: es la fuente y
regenerarlo exige volver a scrapear. `data/processed/` no: son derivados que salen
de correr dos scripts, y versionarlos generaría diffs de decenas de MB y conflictos
irresolubles (un CSV no se mergea).

---

## Convenciones de código

- Código y comentarios en español, sin tildes en los comentarios (evita problemas
  de encoding en Windows). En Markdown sí van tildes.
- Los comentarios explican **por qué**, no qué. Se documentan las decisiones y las
  trampas encontradas.
- No usar `except: pass`. Los errores se cuentan y se reportan.
- Todo parseo nuevo va con su test en `test_parseo.py`.
- Los CSV se guardan con `utf-8-sig` para que Excel abra bien las tildes.
- Respetar los delays entre requests. No bajarlos para ir más rápido.

## Si un scraper deja de funcionar

Los portales cambian el HTML seguido. Cuando devuelva 0 avisos:

```bash
py run_scrapers.py --portal remax --paginas 1 --debug
```

Revisar el HTML en `data/debug/`. Si tiene un alto porcentaje de bytes no
imprimibles, es el problema de compresión del anexo A.1, no los selectores. Si es
HTML legible, buscar el nombre de clase nuevo y agregarlo **al principio** de la
lista en `first_elements([...])`, sin borrar los viejos.
