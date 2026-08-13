#!/usr/bin/env python
"""
test_parseo.py — Tests de las funciones de parseo, sin tocar la red.

Correr con:  py test_parseo.py

Sirve para dos cosas:
  1. Verificar que el parseo funciona antes de gastar tiempo scrapeando.
  2. Es evidencia de calidad de codigo para el informe del TP.
"""

import sys
from src import utils
from src.base import first_text, first_elements
from bs4 import BeautifulSoup

fallos = []
oks = 0


def check(nombre, obtenido, esperado):
    global oks
    if obtenido == esperado:
        oks += 1
    else:
        fallos.append(f"{nombre}: esperaba {esperado!r}, obtuve {obtenido!r}")


print("=" * 62)
print("TESTS DE PARSEO")
print("=" * 62)

# ------------------------------------------------------------ numeros
print("\n[1] Numeros en formato argentino")
check("mil separado por puntos", utils.parse_number_ar("1.250.000"), 1250000.0)
check("decimal con coma", utils.parse_number_ar("1.250,50"), 1250.5)
check("decimal simple", utils.parse_number_ar("85,5"), 85.5)
check("entero simple", utils.parse_number_ar("120"), 120.0)
check("vacio", utils.parse_number_ar(""), None)
check("solo texto", utils.parse_number_ar("Consultar"), None)

# ------------------------------------------------------------ precios
print("[2] Precios y monedas")
check("USD con puntos", utils.parse_price("USD 145.000"), (145000.0, "USD"))
check("U$S", utils.parse_price("U$S 89.500"), (89500.0, "USD"))
check("pesos", utils.parse_price("$ 850.000"), (850000.0, "ARS"))
check("consultar", utils.parse_price("Consultar precio"), (None, None))
check("a convenir", utils.parse_price("Precio a convenir"), (None, None))
check("None", utils.parse_price(None), (None, None))
check("precio + expensas", utils.parse_price("USD 145.000 + $ 95.000 expensas"),
      (145000.0, "USD"))

print("[3] Expensas")
check("expensas con +", utils.parse_expensas("USD 145.000 + $ 95.000"), 95000.0)
check("expensas nombradas", utils.parse_expensas("Expensas $ 120.500"), 120500.0)
check("sin expensas", utils.parse_expensas("USD 145.000"), None)

# ------------------------------------------------------------ superficie
print("[4] Superficie")
check("m2 con simbolo", utils.parse_superficie("68 m² tot."), 68.0)
check("m2 sin simbolo", utils.parse_superficie("Sup. cubierta 55 m2"), 55.0)
check("con decimales", utils.parse_superficie("85,5 m²"), 85.5)
check("valor absurdo alto", utils.parse_superficie("99999 m²"), None)
check("valor absurdo bajo", utils.parse_superficie("3 m²"), None)
check("sin m2", utils.parse_superficie("3 ambientes"), None)

# ------------------------------------------------------------ atributos
print("[5] Ambientes / dormitorios / banos / cocheras")
check("3 amb", utils.parse_ambientes("3 ambientes"), 3)
check("abreviado", utils.parse_ambientes("2 amb."), 2)
check("monoambiente", utils.parse_ambientes("Monoambiente en Palermo"), 1)
check("sin ambientes", utils.parse_ambientes("68 m2"), None)

check("2 dorm", utils.parse_dormitorios("2 dormitorios"), 2)
check("habitaciones", utils.parse_dormitorios("3 habitaciones"), 3)
check("sin dormitorios", utils.parse_dormitorios("sin dormitorios"), 0)

check("1 bano", utils.parse_banos("1 baño"), 1)
check("2 banos", utils.parse_banos("2 baños"), 2)

check("1 cochera", utils.parse_cocheras("1 cochera"), 1)
check("cochera sin numero", utils.parse_cocheras("Con cochera fija"), 1)
check("sin cochera", utils.parse_cocheras("sin cochera"), 0)

# ------------------------------------------------------------ antiguedad
print("[6] Antiguedad")
check("a estrenar", utils.parse_antiguedad("A estrenar"), (0, 1))
check("en pozo", utils.parse_antiguedad("Venta en pozo"), (0, 1))
check("15 anios", utils.parse_antiguedad("15 años"), (15, 0))
check("antiguedad label", utils.parse_antiguedad("Antigüedad: 30"), (30, 0))
check("sin dato", utils.parse_antiguedad("Departamento luminoso"), (None, None))

# ------------------------------------------------------------ direcciones
print("[7] Direcciones")
check("calle + altura", utils.parse_address("Av. Cabildo 2450"), ("Av. Cabildo", "2450", ""))
check("con piso", utils.parse_address("Thames 1580, Piso 7"), ("Thames", "1580", "7"))
check("con al", utils.parse_address("Gurruchaga al 1200"), ("Gurruchaga", "1200", ""))
check("solo calle", utils.parse_address("Av. Santa Fe"), ("Av. Santa Fe", "", ""))
check("planta baja", utils.parse_address("Malabia 750, PB"), ("Malabia", "750", "0"))

check("piso con simbolo", utils.parse_piso("3º piso"), "3")
check("PB", utils.parse_piso("PB"), "0")

# ------------------------------------------------------------ barrios
print("[8] Normalizacion de barrios")
check("directo", utils.normalizar_barrio("Palermo"), "palermo")
check("con contexto", utils.normalizar_barrio("Depto en Villa Urquiza, CABA"), "villa urquiza")
check("alias hollywood", utils.normalizar_barrio("Palermo Hollywood"), "palermo")
check("alias barrio norte", utils.normalizar_barrio("Barrio Norte"), "recoleta")
check("alias canitas", utils.normalizar_barrio("Las Cañitas"), "palermo")
check("alias once", utils.normalizar_barrio("Once"), "balvanera")
check("alias microcentro", utils.normalizar_barrio("Microcentro"), "san nicolas")
check("con tilde", utils.normalizar_barrio("Núñez"), "nunez")
check("belgrano r", utils.normalizar_barrio("Belgrano R"), "belgrano")
check("desambiguacion", utils.normalizar_barrio("Villa del Parque"), "villa del parque")
check("inexistente", utils.normalizar_barrio("Vicente Lopez"), None)
check("vacio", utils.normalizar_barrio(""), None)

print("[9] Comunas")
check("palermo -> 14", utils.comuna_de_barrio("palermo"), 14)
check("recoleta -> 2", utils.comuna_de_barrio("recoleta"), 2)
check("puerto madero -> 1", utils.comuna_de_barrio("puerto madero"), 1)
check("inexistente", utils.comuna_de_barrio("xxxx"), None)

# ------------------------------------------------------------ dummies
print("[10] Variables dicotomicas")
d = utils.extract_dummies("Depto con pileta, parrilla y SUM. Apto credito. Cochera fija.")
check("amenities", d["amenities"], 1)
check("pileta", d["pileta"], 1)
check("apto_credito", d["apto_credito"], 1)
check("cochera_txt", d["cochera_txt"], 1)
check("losa_radiante ausente", d["losa_radiante"], 0)

d2 = utils.extract_dummies("Departamento a reciclar, ideal inversor")
check("a_reciclar", d2["a_reciclar"], 1)
check("amenities ausente", d2["amenities"], 0)

# ------------------------------------------------------------ esquema
print("[11] Esquema unificado")
rec = utils.build_record(portal="test", url="http://x", precio_valor=150000,
                         precio_moneda="USD", sup_total_m2=60,
                         titulo="Depto en Palermo con pileta")
check("todas las columnas", len(rec), len(utils.SCHEMA))
check("campo no pasado es None", rec["latitud"], None)

rec = utils.enriquecer_registro(rec)
check("precio_m2 derivado", rec["precio_m2"], 2500.0)
check("barrio inferido del titulo", rec["barrio"], "palermo")
check("comuna inferida", rec["comuna"], 14)
check("dummy pileta", rec["pileta"], 1)

df = utils.to_dataframe([rec])
check("df columnas", list(df.columns), utils.SCHEMA)
check("df filas", len(df), 1)

df_vacio = utils.to_dataframe([])
check("df vacio con esquema", list(df_vacio.columns), utils.SCHEMA)

# ------------------------------------------------------------ selectores
print("[12] Selectores con fallback")
html = """
<div class="listing__item">
  <a class="card" href="/depto--123">
    <p class="card__price">USD 145.000 + $ 95.000</p>
    <p class="card__address">Av. Cabildo 2450, Piso 7</p>
    <p class="card__title--primary">Belgrano, Capital Federal</p>
    <ul class="card__main-features">
      <li><span class="icono-superficie"></span> 68 m²</li>
      <li><span class="icono-ambiente"></span> 3 amb.</li>
      <li><span class="icono-dormitorio"></span> 2 dorm.</li>
      <li><span class="icono-bano"></span> 1 baño</li>
    </ul>
  </a>
</div>
"""
soup = BeautifulSoup(html, "html.parser")
check("primer selector", first_text(soup, ["p.card__price"]), "USD 145.000 + $ 95.000")
check("fallback al segundo", first_text(soup, ["p.no-existe", "p.card__address"]),
      "Av. Cabildo 2450, Piso 7")
check("ninguno matchea", first_text(soup, ["p.no", "div.tampoco"]), "")
check("elementos", len(first_elements(soup, ["li"])), 4)

# ------------------------------------------------------------ resultado
print("\n" + "=" * 62)
if fallos:
    print(f"RESULTADO: {oks} OK, {len(fallos)} FALLOS")
    print("=" * 62)
    for f in fallos:
        print(f"  X {f}")
    sys.exit(1)
else:
    print(f"RESULTADO: {oks}/{oks} tests OK")
    print("=" * 62)
    sys.exit(0)
