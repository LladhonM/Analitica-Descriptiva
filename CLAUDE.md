# Contexto del proyecto

**Toda la documentación está en [README.md](README.md). Leerlo antes de tocar código.**

Ahí están el contexto de negocio, las decisiones metodológicas, los criterios de
limpieza, el modelo y —sobre todo— las trampas ya resueltas que conviene no
volver a pisar.

## Atajos a lo que más se consulta

| Tema | Sección |
|---|---|
| Por qué un scraper devuelve 0 avisos sin dar error | Anexo A.1 — compresión Brotli |
| Por qué no se puede promediar `precio_m2` | Anexo B.1 |
| Errores de carga ya corregidos (no volver a "arreglar") | Anexo B.2 |
| Cómo se estima el alquiler y cuánto error tiene | Anexo C |
| Qué se versiona y qué no | Estructura del repositorio |

## Reglas para intervenir

- **No revertir el manejo de compresión de `src/utils.py`.** Si se toca
  `HEADERS_BASE`, no hardcodear `br` en `Accept-Encoding`.
- **No sacar el shim de imports relativos** al inicio de los módulos de `src/`
  (`if __package__ in (None, "")`): permite ejecutarlos directamente.
- **Usar las columnas del dataset limpio**, no `precio_m2` del crudo.
- **Correr `py test_parseo.py`** antes de dar por buena cualquier modificación al
  parseo.
- Convenciones de estilo: ver la sección correspondiente del README.
