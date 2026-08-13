"""
utils.py — Funciones comunes a todos los scrapers.

Contiene:
  - Sesion HTTP con reintentos, backoff y rotacion de User-Agent.
  - Parseo de precios, monedas, superficies, ambientes, banos, antiguedad.
  - Normalizacion de barrios de CABA.
  - Extraccion de variables dicotomicas (dummies) desde texto libre.
  - Guardado de datasets al esquema unificado.
"""

from __future__ import annotations

import os
import re
import time
import random
import unicodedata
from datetime import datetime, timezone
from typing import Optional, Iterable

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 v1 y v2 exponen Retry en lugares distintos
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

import pandas as pd


# --------------------------------------------------------------------------
# Configuracion general
# --------------------------------------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]

# Directorios del proyecto (relativos a la raiz del repo)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT_DIR, "data", "raw")
DEBUG_DIR = os.path.join(ROOT_DIR, "data", "debug")


def ensure_dirs() -> None:
    """Crea las carpetas de salida si no existen."""
    for d in (RAW_DIR, DEBUG_DIR):
        os.makedirs(d, exist_ok=True)


# --------------------------------------------------------------------------
# Sesion HTTP
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Compresion HTTP
# --------------------------------------------------------------------------
#
# ATENCION - ESTE FUE EL BUG QUE DEJABA A MELI/REMAX/ZONAPROP EN 0 AVISOS.
#
# Si declaramos "Accept-Encoding: gzip, deflate, br" pero el paquete `brotli`
# NO esta instalado, pasa lo siguiente:
#   1. El servidor nos toma la palabra y responde comprimido con Brotli.
#   2. urllib3 no sabe descomprimirlo y devuelve los bytes crudos.
#   3. resp.text es basura binaria -> BeautifulSoup no encuentra nada -> 0 avisos.
#
# Lo insidioso es que el status es 200 y el body pesa cientos de KB, asi que
# parece un problema de selectores cuando en realidad nunca vimos el HTML.
#
# Argenprop funcionaba porque su CDN devuelve gzip (que si sabemos descomprimir).
#
# Solucion: declarar SOLO las codificaciones que realmente podemos decodificar.

def _codecs_disponibles() -> list[str]:
    """Codificaciones que este entorno puede descomprimir de verdad."""
    codecs = ["gzip", "deflate"]          # zlib esta en la stdlib, siempre estan
    try:
        import brotli  # noqa: F401
        codecs.append("br")
    except ImportError:
        try:
            import brotlicffi  # noqa: F401
            codecs.append("br")
        except ImportError:
            pass
    try:
        import zstandard  # noqa: F401
        codecs.append("zstd")
    except ImportError:
        pass
    return codecs


ACCEPT_ENCODING = ", ".join(_codecs_disponibles())

HEADERS_BASE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
    "Accept-Encoding": ACCEPT_ENCODING,
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def parece_binario(data: bytes, muestra: int = 2000) -> bool:
    """
    Heuristica: detecta un cuerpo que quedo sin descomprimir.
    El HTML real es casi todo ASCII imprimible; un blob comprimido no.
    """
    if not data:
        return False
    d = data[:muestra]
    no_imp = sum(1 for b in d if b < 9 or (13 < b < 32) or b > 126)
    return (no_imp / len(d)) > 0.30


def descomprimir(data: bytes, encoding: str) -> Optional[bytes]:
    """
    Red de seguridad: descomprime a mano si el servidor mando algo que la
    libreria HTTP no decodifico (pasa con proxies mal configurados o cuando
    el servidor ignora nuestro Accept-Encoding).
    """
    enc = (encoding or "").lower().strip()
    try:
        if enc == "gzip":
            import gzip
            return gzip.decompress(data)
        if enc == "deflate":
            import zlib
            try:
                return zlib.decompress(data)
            except zlib.error:
                return zlib.decompress(data, -zlib.MAX_WBITS)
        if enc == "br":
            try:
                import brotli
                return brotli.decompress(data)
            except ImportError:
                import brotlicffi
                return brotlicffi.decompress(data)
        if enc == "zstd":
            import zstandard
            return zstandard.ZstdDecompressor().decompress(data)
    except Exception:
        return None
    return None


def _hay_curl_cffi() -> bool:
    """curl_cffi permite imitar la huella TLS de Chrome. Es opcional."""
    try:
        import curl_cffi  # noqa: F401
        return True
    except ImportError:
        return False


class HttpSession:
    """
    Cliente HTTP con reintentos y backoff.

    POR QUE EXISTE ESTA CLASE
    -------------------------
    La libreria `requests` tiene una huella TLS (el "JA3 fingerprint" del
    handshake) que NO se parece a la de ningun navegador real. Los servicios
    anti-bot (DataDome, Cloudflare, Akamai) la detectan al instante, sin
    importar que User-Agent declaremos: el bloqueo ocurre en el handshake,
    antes de que se envie un solo header.

    Por eso Argenprop (sin proteccion) funciona con requests, pero Zonaprop,
    MercadoLibre y Remax devuelven 0 resultados o 403.

    La solucion es `curl_cffi`, que usa libcurl-impersonate para replicar el
    handshake TLS exacto de Chrome. Si esta instalado lo usamos; si no, caemos
    a requests y avisamos.

        py -m pip install curl_cffi

    Esta capa es transparente para los scrapers: todos usan `.get()` igual.
    """

    # Version de Chrome a imitar. Actualizar si curl_cffi deja de soportarla.
    IMPERSONATE = "chrome"

    def __init__(self, extra_headers: Optional[dict] = None, usar_impersonate: bool = True):
        self.headers = dict(HEADERS_BASE)
        self.headers["User-Agent"] = random.choice(USER_AGENTS)
        if extra_headers:
            self.headers.update(extra_headers)

        self.backend = "requests"
        self._impersonate = None

        if usar_impersonate and _hay_curl_cffi():
            from curl_cffi import requests as cffi_requests
            self._s = cffi_requests.Session()
            self.backend = "curl_cffi"
            self._impersonate = self.IMPERSONATE
        else:
            self._s = requests.Session()
            retry = Retry(
                total=3,
                backoff_factor=1.5,
                status_forcelist=[500, 502, 503, 504],
                allowed_methods=["GET", "POST"],
                raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
            self._s.mount("https://", adapter)
            self._s.mount("http://", adapter)

        try:
            self._s.headers.update(self.headers)
        except Exception:
            pass

    @property
    def cookies(self):
        return self._s.cookies

    def get(self, url: str, timeout: int = 30, allow_redirects: bool = True,
            reintentos: int = 3, **kwargs):
        """GET con reintentos y backoff exponencial ante errores de red y 429."""
        ultimo_error = None

        for intento in range(reintentos):
            kw = dict(timeout=timeout, allow_redirects=allow_redirects, **kwargs)
            if self._impersonate:
                kw["impersonate"] = self._impersonate
            try:
                resp = self._s.get(url, **kw)
            except Exception as e:
                ultimo_error = e
                if intento < reintentos - 1:
                    time.sleep(1.5 * (2 ** intento) + random.random())
                    continue
                raise

            # 429 = rate limit: conviene esperar bastante mas
            if resp.status_code == 429 and intento < reintentos - 1:
                time.sleep(5 * (2 ** intento) + random.random() * 3)
                continue

            return self._arreglar_compresion(resp)

    @staticmethod
    def _arreglar_compresion(resp):
        """
        Si el cuerpo quedo comprimido sin decodificar, lo descomprime a mano.

        No deberia hacer falta ahora que solo declaramos codecs soportados,
        pero algunos servidores comprimen igual aunque no se lo pidamos.
        Sin esta red, el sintoma es "0 avisos" sin ningun error visible.
        """
        try:
            data = resp.content
        except Exception:
            return resp

        enc = (resp.headers.get("Content-Encoding") or "").lower().strip()
        binario = parece_binario(data)

        # Nada que hacer: sin header de compresion y el cuerpo se ve como texto.
        if not enc and not binario:
            return resp

        # Orden de intentos: primero lo que declara el header, despues a ciegas.
        # Si el cuerpo YA venia descomprimido, descomprimir() falla y devuelve
        # None, asi que probar de mas no rompe nada.
        candidatos = ([enc] if enc else []) + ["br", "gzip", "zstd", "deflate"]
        vistos = set()

        for c in candidatos:
            if not c or c in vistos or c == "identity":
                continue
            vistos.add(c)

            plano = descomprimir(data, c)
            if not plano or parece_binario(plano):
                continue
            # Un resultado mas chico que el original no es una descompresion real
            if len(plano) < len(data) // 2 and not binario:
                continue

            try:
                resp._content = plano
                if hasattr(resp, "encoding"):
                    resp.encoding = "utf-8"
            except Exception:
                return resp

            print(f"    [http] cuerpo venia en '{c}' sin decodificar; "
                  f"descomprimido a mano ({len(data):,} -> {len(plano):,} bytes)")
            return resp

        if binario:
            print(f"    [http] AVISO: el cuerpo parece comprimido"
                  f"{f' en {enc}' if enc else ''} y no se pudo descomprimir.")
            if enc == "br" or not enc:
                print("    [http] Probá:  py -m pip install Brotli zstandard")

        return resp

        if ultimo_error:
            raise ultimo_error
        raise RuntimeError(f"No se pudo obtener {url}")


def build_session(extra_headers: Optional[dict] = None) -> HttpSession:
    """Devuelve el cliente HTTP configurado (curl_cffi si esta disponible)."""
    return HttpSession(extra_headers)


def info_backend() -> str:
    """Estado del motor HTTP y de los codecs. Se imprime al arrancar la corrida."""
    motor = "curl_cffi (imita TLS de Chrome)" if _hay_curl_cffi() else "requests"
    codecs = ACCEPT_ENCODING
    linea = f"HTTP: {motor}  |  Accept-Encoding: {codecs}"

    faltantes = []
    if "br" not in codecs:
        faltantes.append("Brotli")
    if "zstd" not in codecs:
        faltantes.append("zstandard")

    if faltantes:
        linea += (
            f"\n      (sin {', '.join(faltantes)}: no los pedimos, asi que no rompe. "
            f"Instalalos para respuestas mas chicas)"
        )
    return linea


def polite_sleep(base: float = 1.5, jitter: float = 1.0) -> None:
    """
    Pausa aleatoria entre requests. El jitter evita un patron de trafico
    perfectamente regular, que es una de las senales que usan los anti-bot.
    """
    time.sleep(base + random.random() * jitter)


def save_debug_html(html: str, portal: str, tag: str = "page") -> str:
    """
    Guarda el HTML crudo en data/debug/. Sirve para inspeccionar la estructura
    cuando un selector deja de funcionar (los portales cambian el markup seguido).
    """
    ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(DEBUG_DIR, f"{portal}_{tag}_{ts}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


# --------------------------------------------------------------------------
# Limpieza y parseo de texto
# --------------------------------------------------------------------------

def clean_text(text: Optional[str]) -> str:
    """Normaliza espacios, quita non-breaking spaces y recorta."""
    if not text:
        return ""
    text = str(text).replace("\xa0", " ").replace("​", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_accents(text: str) -> str:
    """Quita tildes. Util para comparar barrios de forma robusta."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def norm_key(text: str) -> str:
    """Clave normalizada: minusculas, sin tildes, sin espacios extra."""
    return re.sub(r"\s+", " ", strip_accents(str(text)).lower()).strip()


def to_float(value) -> Optional[float]:
    """Convierte a float devolviendo None si no es posible (en vez de romper)."""
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_number_ar(text: Optional[str]) -> Optional[float]:
    """
    Convierte un numero en formato argentino a float.
      "1.250.000"  -> 1250000.0
      "1.250,50"   -> 1250.5
      "85,5"       -> 85.5
      "120"        -> 120.0
    Distingue el separador decimal del de miles segun la posicion.
    """
    if text is None:
        return None
    s = re.sub(r"[^\d.,]", "", str(text))
    if not s:
        return None

    has_dot, has_comma = "." in s, "," in s

    if has_dot and has_comma:
        # El separador decimal es el que aparece mas a la derecha
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_comma:
        # Si hay 3 digitos despues de la coma es separador de miles
        s = s.replace(",", "") if re.search(r",\d{3}$", s) else s.replace(",", ".")
    elif has_dot:
        # Idem para el punto
        if re.search(r"\.\d{3}(\.\d{3})*$", s) or re.search(r"^\d{1,3}(\.\d{3})+$", s):
            s = s.replace(".", "")

    try:
        return float(s)
    except ValueError:
        return None


def parse_price(text: Optional[str]) -> tuple[Optional[float], Optional[str]]:
    """
    Separa monto y moneda de un texto de precio.
    Devuelve (valor, moneda) donde moneda es 'USD', 'ARS' o None.

      "USD 145.000"        -> (145000.0, 'USD')
      "$ 850.000"          -> (850000.0, 'ARS')
      "Consultar precio"   -> (None, None)
    """
    if not text:
        return None, None

    t = clean_text(text)
    tl = norm_key(t)

    if any(x in tl for x in ["consultar", "a convenir", "sin precio"]):
        return None, None

    if re.search(r"\b(usd|u\$s|us\$|dolar|dólar)\b", tl):
        moneda = "USD"
    elif re.search(r"(\$|ars|peso)", tl):
        moneda = "ARS"
    else:
        moneda = None

    # Toma el primer bloque numerico "grande" del texto
    m = re.search(r"(\d[\d.,]*)", t)
    valor = parse_number_ar(m.group(1)) if m else None

    # Descarta valores absurdos (ruido de parseo, ej. un "2" suelto de ambientes)
    if valor is not None and valor < 100:
        valor = None

    return valor, moneda


def parse_expensas(text: Optional[str]) -> Optional[float]:
    """Extrae el monto de expensas de un texto tipo '+ $ 95.000 expensas'."""
    if not text:
        return None
    t = clean_text(text)
    m = re.search(r"\+\s*\$?\s*(\d[\d.,]*)", t)
    if not m:
        m = re.search(r"expensas?\D{0,10}(\d[\d.,]*)", norm_key(t))
    return parse_number_ar(m.group(1)) if m else None


# --------------------------------------------------------------------------
# Parseo de atributos de la propiedad
# --------------------------------------------------------------------------

def parse_superficie(text: Optional[str]) -> Optional[float]:
    """Extrae los m2 de textos como '68 m² tot.', 'Sup. cubierta 55 m2'."""
    if not text:
        return None
    t = clean_text(text)
    m = re.search(r"(\d[\d.,]*)\s*(?:m²|m2|mts2|mts²|m\s*2)\b", t, flags=re.IGNORECASE)
    if not m:
        return None
    v = parse_number_ar(m.group(1))
    # Filtro de sanidad: un depto entre 8 y 10.000 m2
    return v if v and 8 <= v <= 10000 else None


def parse_ambientes(text: Optional[str]) -> Optional[int]:
    """Extrae la cantidad de ambientes. 'Monoambiente' cuenta como 1."""
    if not text:
        return None
    t = norm_key(text)
    if "monoambiente" in t:
        return 1
    m = re.search(r"(\d+)\s*amb", t)
    if not m:
        m = re.search(r"amb\w*\D{0,5}(\d+)", t)
    if m:
        v = int(m.group(1))
        return v if 1 <= v <= 20 else None
    return None


def parse_dormitorios(text: Optional[str]) -> Optional[int]:
    """Extrae dormitorios/habitaciones. Distingue 'sin dormitorios' = 0."""
    if not text:
        return None
    t = norm_key(text)
    if re.search(r"sin\s+dormitorio", t):
        return 0
    m = re.search(r"(\d+)\s*(?:dorm|habitacion|hab\b|cuarto)", t)
    if not m:
        m = re.search(r"(?:dorm\w*|habitacion\w*)\D{0,5}(\d+)", t)
    if m:
        v = int(m.group(1))
        return v if 0 <= v <= 15 else None
    return None


def parse_banos(text: Optional[str]) -> Optional[int]:
    """Extrae la cantidad de banos."""
    if not text:
        return None
    t = norm_key(text)
    m = re.search(r"(\d+)\s*ban", t)
    if not m:
        m = re.search(r"ban\w*\D{0,5}(\d+)", t)
    if m:
        v = int(m.group(1))
        return v if 0 <= v <= 15 else None
    return None


def parse_cocheras(text: Optional[str]) -> Optional[int]:
    """Extrae la cantidad de cocheras."""
    if not text:
        return None
    t = norm_key(text)
    if re.search(r"sin\s+cochera", t):
        return 0
    m = re.search(r"(\d+)\s*(?:cochera|garage|estacionamiento)", t)
    if m:
        v = int(m.group(1))
        return v if 0 <= v <= 20 else None
    if re.search(r"\bcochera\b", t):
        return 1
    return None


def parse_antiguedad(text: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """
    Extrae la antiguedad en anios y una dummy 'a estrenar'.
    Devuelve (antiguedad_anios, es_a_estrenar).

      "A estrenar"        -> (0, 1)
      "15 anios"          -> (15, 0)
      "En pozo"           -> (0, 1)
    """
    if not text:
        return None, None
    t = norm_key(text)

    if any(x in t for x in ["a estrenar", "estrenar", "en pozo", "en construccion", "obra nueva"]):
        return 0, 1

    m = re.search(r"(\d+)\s*(?:anos?|anios?|years?)\b", t)
    if not m:
        m = re.search(r"antiguedad\D{0,8}(\d+)", t)
    if m:
        v = int(m.group(1))
        if 0 <= v <= 200:
            return v, (1 if v == 0 else 0)

    # Algunas fichas traen el anio de construccion en vez de la antiguedad
    m = re.search(r"\b(19\d{2}|20[0-2]\d)\b", t)
    if m and "antigued" in t:
        anio = int(m.group(1))
        edad = datetime.now().year - anio
        if 0 <= edad <= 200:
            return edad, (1 if edad == 0 else 0)

    return None, None


def parse_piso(text: Optional[str]) -> Optional[str]:
    """Extrae el piso de una direccion o titulo. 'PB' se normaliza a '0'."""
    if not text:
        return None
    t = clean_text(text)
    if re.search(r"\b(pb|planta baja)\b", t, flags=re.IGNORECASE):
        return "0"
    m = re.search(r"piso\s*:?\s*(\d+)", t, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(\d+)\s*[º°]", t)
    if m:
        return m.group(1)
    return None


def parse_address(address_raw: Optional[str]) -> tuple[str, str, str]:
    """
    Separa una direccion en (calle, altura, piso).
      "Av. Cabildo 2450, Piso 7" -> ("Av. Cabildo", "2450", "7")
      "Thames al 1500"           -> ("Thames", "1500", "")
    """
    calle, altura, piso = "", "", ""
    if not address_raw:
        return calle, altura, piso

    raw = clean_text(address_raw)
    piso_detectado = parse_piso(raw) or ""

    # Saca la parte de piso para no confundirla con la altura
    limpio = re.sub(r",?\s*(piso|pb|planta baja)\s*:?\s*\d*\s*[º°]?", "", raw, flags=re.IGNORECASE)
    limpio = limpio.split(",")[0].strip()

    m = re.search(r"^(.*?)\s+(?:al\s+)?(\d{1,6})\s*$", limpio)
    if m:
        calle = m.group(1).strip()
        altura = m.group(2).strip()
    else:
        calle = limpio

    return calle, altura, piso_detectado


# --------------------------------------------------------------------------
# Barrios de CABA
# --------------------------------------------------------------------------

# Los 48 barrios oficiales de CABA con su comuna.
BARRIOS_CABA = {
    "agronomia": 15, "almagro": 5, "balvanera": 3, "barracas": 4, "belgrano": 13,
    "boedo": 5, "caballito": 6, "chacarita": 15, "coghlan": 12, "colegiales": 13,
    "constitucion": 1, "flores": 7, "floresta": 10, "la boca": 4, "la paternal": 15,
    "liniers": 9, "mataderos": 9, "monte castro": 10, "monserrat": 1, "nueva pompeya": 4,
    "nunez": 13, "palermo": 14, "parque avellaneda": 9, "parque chacabuco": 7,
    "parque chas": 15, "parque patricios": 4, "puerto madero": 1, "recoleta": 2,
    "retiro": 1, "saavedra": 12, "san cristobal": 3, "san nicolas": 1, "san telmo": 1,
    "santa rita": 11, "velez sarsfield": 10, "versalles": 10, "villa crespo": 15,
    "villa del parque": 11, "villa devoto": 11, "villa gral. mitre": 11,
    "villa lugano": 8, "villa luro": 10, "villa ortuzar": 15, "villa pueyrredon": 12,
    "villa real": 10, "villa riachuelo": 8, "villa santa rita": 11, "villa soldati": 8,
    "villa urquiza": 12,
}

# Alias frecuentes en los portales -> barrio oficial
ALIAS_BARRIOS = {
    "barrio norte": "recoleta",
    "palermo hollywood": "palermo", "palermo soho": "palermo",
    "palermo chico": "palermo", "palermo nuevo": "palermo",
    "palermo viejo": "palermo", "palermo botanico": "palermo",
    "las canitas": "palermo", "canitas": "palermo",
    "belgrano c": "belgrano", "belgrano r": "belgrano", "belgrano chico": "belgrano",
    "bajo belgrano": "belgrano", "belgrano barrancas": "belgrano",
    "centro": "san nicolas", "microcentro": "san nicolas", "tribunales": "san nicolas",
    "congreso": "balvanera", "once": "balvanera", "abasto": "balvanera",
    "villa general mitre": "villa gral. mitre",
    "villa ortuzar": "villa ortuzar",
    "catalinas": "retiro",
    "distrito quartier": "retiro",
    "boca": "la boca",
    "pompeya": "nueva pompeya",
    "parque centenario": "caballito",
    "primera junta": "caballito",
    "villa mitre": "villa gral. mitre",
    "paternal": "la paternal",
    "villa santa rita": "villa santa rita",
    "coghlan": "coghlan",
    "monserrat": "monserrat", "montserrat": "monserrat",
    "puerto retiro": "retiro",
}

_BARRIOS_ORDENADOS = sorted(BARRIOS_CABA.keys(), key=len, reverse=True)
_ALIAS_ORDENADOS = sorted(ALIAS_BARRIOS.keys(), key=len, reverse=True)


def normalizar_barrio(text: Optional[str]) -> Optional[str]:
    """
    Mapea texto libre al nombre de barrio oficial de CABA.
    Busca primero los alias y despues los nombres oficiales, ordenados de
    mas largo a mas corto para que 'villa del parque' no matchee 'parque chas'.
    """
    if not text:
        return None
    t = norm_key(text)
    t = t.replace("capital federal", " ").replace("ciudad de buenos aires", " ")

    for alias in _ALIAS_ORDENADOS:
        if re.search(rf"\b{re.escape(alias)}\b", t):
            return ALIAS_BARRIOS[alias]

    for barrio in _BARRIOS_ORDENADOS:
        if re.search(rf"\b{re.escape(barrio)}\b", t):
            return barrio

    return None


def comuna_de_barrio(barrio: Optional[str]) -> Optional[int]:
    """Devuelve el numero de comuna de un barrio ya normalizado."""
    if not barrio:
        return None
    return BARRIOS_CABA.get(norm_key(barrio))


# --------------------------------------------------------------------------
# Variables dicotomicas desde texto libre
# --------------------------------------------------------------------------

DUMMY_KEYWORDS = {
    "amenities":        ["amenities", "piscina", "pileta", "sum", "solarium", "gimnasio", "gym", "sauna", "laundry", "coworking"],
    "pileta":           ["pileta", "piscina"],
    "parrilla":         ["parrilla", "quincho"],
    "gimnasio":         ["gimnasio", "gym"],
    "sum":              [" sum ", "salon de usos multiples"],
    "losa_radiante":    ["losa radiante", "piso radiante", "calefaccion central", "caldera central"],
    "aire_acondicionado": ["aire acondicionado", "split", " a/c", "frio-calor", "frio calor"],
    "apto_credito":     ["apto credito", "apto hipotecario"],
    "apto_profesional": ["apto profesional", "uso profesional"],
    "cochera_txt":      ["cochera", "guarda coche", "estacionamiento", "garage", "baulera con cochera"],
    "baulera":          ["baulera"],
    "seguridad":        ["vigilancia", "seguridad 24", "totem", "encargado permanente", "camaras de seguridad"],
    "luminoso":         ["luminoso", "muy luminoso", "vista abierta", "vista panoramica", "todo luz", "contrafrente luminoso"],
    "balcon":           ["balcon", "aterrazado", "terraza"],
    "a_reciclar":       ["a reciclar", "a refaccionar", "para refaccionar", "para reciclar", "necesita refaccion", "a poner en valor"],
    "reciclado":        ["reciclado", "reciclada", "refaccionado", "totalmente reciclado"],
    "credito_uva":      ["uva", "credito uva"],
    "frente":           ["al frente", "contrafrente"],
    "amoblado":         ["amoblado", "amueblado", "equipado"],
    "patio_jardin":     ["patio", "jardin", "fondo libre"],
    "ascensor":         ["ascensor"],
    "profesional_renta": ["renta", "inversion", "alquilado", "inquilino"],
}


def extract_dummies(texto: str) -> dict:
    """
    Devuelve un dict de variables dicotomicas (0/1) buscando palabras clave
    en el texto concatenado de titulo + descripcion + detalles.
    """
    t = f" {norm_key(texto)} "
    return {k: int(any(kw in t for kw in kws)) for k, kws in DUMMY_KEYWORDS.items()}


# --------------------------------------------------------------------------
# Esquema unificado y guardado
# --------------------------------------------------------------------------

# Todas las fuentes se normalizan a este esquema para poder concatenarlas.
SCHEMA = [
    # Identificacion
    "portal", "id_aviso", "url", "fecha_scraping",
    # Operacion y tipo
    "operacion", "tipo_propiedad",
    # Precio (numerico + moneda separada)
    "precio_valor", "precio_moneda", "precio_texto",
    "expensas_valor", "expensas_moneda",
    # Ubicacion
    "direccion", "calle", "altura", "piso",
    "barrio", "barrio_raw", "comuna", "localidad", "latitud", "longitud",
    # Metricas numericas
    "sup_total_m2", "sup_cubierta_m2", "ambientes", "dormitorios", "banos", "cocheras",
    "antiguedad_anios", "es_a_estrenar",
    # Derivadas
    "precio_m2",
    # Texto
    "titulo", "descripcion", "detalles",
]

SCHEMA += list(DUMMY_KEYWORDS.keys())


def now_iso() -> str:
    """Timestamp UTC en ISO 8601."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_record(**kwargs) -> dict:
    """
    Construye un registro con todas las claves del esquema.
    Las que no se pasen quedan en None, garantizando columnas consistentes
    entre portales aunque cada uno exponga distinta informacion.
    """
    rec = {k: None for k in SCHEMA}
    rec.update({k: v for k, v in kwargs.items() if k in rec})
    return rec


def enriquecer_registro(rec: dict) -> dict:
    """
    Post-procesa un registro: calcula dummies desde el texto libre,
    completa el barrio/comuna y deriva precio por m2.
    """
    texto = " ".join(str(rec.get(c) or "") for c in ["titulo", "descripcion", "detalles", "direccion"])
    rec.update(extract_dummies(texto))

    # Barrio: si el scraper no lo resolvio, intentar desde el texto
    if not rec.get("barrio"):
        rec["barrio"] = normalizar_barrio(rec.get("barrio_raw") or rec.get("direccion") or texto)
    if rec.get("barrio") and not rec.get("comuna"):
        rec["comuna"] = comuna_de_barrio(rec["barrio"])

    # Cocheras: si no vino estructurado, usar la dummy de texto
    if rec.get("cocheras") is None and rec.get("cochera_txt"):
        rec["cocheras"] = 1

    # Precio por m2 (solo si ambos valores son confiables)
    p, s = to_float(rec.get("precio_valor")), to_float(rec.get("sup_total_m2"))
    if p and s and s > 0:
        rec["precio_m2"] = round(p / s, 2)

    return rec


def to_dataframe(records: Iterable[dict]) -> pd.DataFrame:
    """Convierte registros a DataFrame respetando el orden del esquema."""
    df = pd.DataFrame(list(records))
    if df.empty:
        return pd.DataFrame(columns=SCHEMA)
    for col in SCHEMA:
        if col not in df.columns:
            df[col] = None
    return df[SCHEMA]


def guardar(df: pd.DataFrame, portal: str, operacion: str, fmt: str = "csv") -> str:
    """
    Guarda el DataFrame en data/raw/ con nombre versionado por fecha.
    Usa utf-8-sig para que Excel abra bien las tildes.
    """
    ensure_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    ext = "parquet" if fmt == "parquet" else "csv"
    path = os.path.join(RAW_DIR, f"{portal}_{operacion}_{ts}.{ext}")

    if fmt == "parquet":
        df.to_parquet(path, index=False)
    else:
        df.to_csv(path, index=False, encoding="utf-8-sig")

    return path


def resumen(df: pd.DataFrame) -> None:
    """Imprime un resumen rapido de calidad del dataset extraido."""
    if df.empty:
        print("Dataset vacio.")
        return

    print(f"\n{'='*60}")
    print(f"Registros: {len(df)}   |   Columnas: {len(df.columns)}")
    print(f"{'='*60}")

    criticas = ["precio_valor", "sup_total_m2", "ambientes", "dormitorios",
                "banos", "barrio", "antiguedad_anios"]
    print("\nCompletitud de variables criticas:")
    for c in criticas:
        if c in df.columns:
            pct = df[c].notna().mean() * 100
            print(f"  {c:<20} {pct:>5.1f}%  ({int(df[c].notna().sum())}/{len(df)})")

    if "precio_valor" in df.columns and df["precio_valor"].notna().any():
        for moneda in df["precio_moneda"].dropna().unique():
            sub = df[df["precio_moneda"] == moneda]["precio_valor"].dropna()
            if len(sub):
                print(f"\nPrecio {moneda}: n={len(sub)}  mediana={sub.median():,.0f}  "
                      f"min={sub.min():,.0f}  max={sub.max():,.0f}")

    if "barrio" in df.columns and df["barrio"].notna().any():
        print(f"\nTop 10 barrios:")
        for b, n in df["barrio"].value_counts().head(10).items():
            print(f"  {b:<25} {n}")
    print()
