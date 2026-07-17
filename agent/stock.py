# agent/stock.py — Consulta de stock en vivo desde Google Sheet
# Generado por AgentKit

"""
Lee el stock actualizado desde un Google Sheet publicado como CSV.

El Sheet se actualiza a diario con columnas: Nombre | SKU | Stock | Precio Venta.
Los SKU coinciden con los de Shopify, así que se pueden cruzar con el catálogo.

La URL del CSV se configura en la variable de entorno STOCK_SHEET_CSV_URL
(publicar el Sheet en: Archivo → Compartir → Publicar en la web → CSV).
El resultado se cachea en memoria unos minutos para no golpear el Sheet en cada mensaje.
"""

import os
import csv
import time
import io
import logging
import unicodedata
import httpx

logger = logging.getLogger("agentkit")

# Cache en memoria: (timestamp_fetch, filas, ultima_actualizacion)
_CACHE_TTL = 600  # 10 minutos
_cache: dict = {"ts": 0.0, "filas": [], "actualizado": None}


def _normalizar(texto: str) -> str:
    """Minúsculas y sin acentos, para comparar de forma robusta."""
    texto = texto.lower().strip()
    texto = unicodedata.normalize("NFD", texto)
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def _parsear_csv(contenido: str) -> tuple[list[dict], str | None]:
    """
    Parsea el CSV del Sheet. Tolera filas de preámbulo antes del encabezado real.
    Retorna (filas, ultima_actualizacion).
    """
    filas = []
    actualizado = None
    lector = csv.reader(io.StringIO(contenido))
    encabezado_visto = False
    idx = {}

    for celdas in lector:
        if not any(c.strip() for c in celdas):
            continue

        texto_fila = " ".join(celdas)
        if "ltima actualiza" in _normalizar(texto_fila) and not encabezado_visto:
            actualizado = texto_fila.strip(" |")
            continue

        # Buscar la fila de encabezado (contiene "SKU")
        if not encabezado_visto:
            norm = [_normalizar(c) for c in celdas]
            if "sku" in norm:
                idx = {nombre: i for i, nombre in enumerate(norm)}
                encabezado_visto = True
            continue

        # Filas de datos
        def val(col):
            i = idx.get(col)
            return celdas[i].strip() if i is not None and i < len(celdas) else ""

        sku = val("sku")
        nombre = val("nombre")
        if not sku and not nombre:
            continue

        stock_txt = val("stock")
        try:
            stock = int(float(stock_txt)) if stock_txt else 0
        except ValueError:
            stock = 0

        precio_txt = val("precio venta") or val("precio")
        try:
            precio = int(float(precio_txt)) if precio_txt else None
        except ValueError:
            precio = None

        filas.append({"nombre": nombre, "sku": sku, "stock": stock, "precio": precio})

    return filas, actualizado


async def _cargar_stock() -> tuple[list[dict], str | None]:
    """Carga el stock desde el Sheet, usando cache con TTL."""
    ahora = time.time()
    if _cache["filas"] and (ahora - _cache["ts"]) < _CACHE_TTL:
        return _cache["filas"], _cache["actualizado"]

    url = os.getenv("STOCK_SHEET_CSV_URL")
    if not url:
        logger.warning("STOCK_SHEET_CSV_URL no configurado, no se puede consultar stock")
        return [], None

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            r = await client.get(url)
            if r.status_code != 200:
                logger.error(f"Error al leer stock Sheet: {r.status_code}")
                return _cache["filas"], _cache["actualizado"]
            filas, actualizado = _parsear_csv(r.text)
    except Exception as e:
        logger.error(f"Error al descargar stock Sheet: {e}")
        return _cache["filas"], _cache["actualizado"]

    _cache.update({"ts": ahora, "filas": filas, "actualizado": actualizado})
    logger.info(f"Stock cargado: {len(filas)} filas (actualizado: {actualizado})")
    return filas, actualizado


async def consultar_stock(consulta: str) -> dict:
    """
    Busca disponibilidad de un producto por nombre o SKU en el stock en vivo.

    Args:
        consulta: nombre del producto (o parte), o un SKU exacto.

    Returns:
        dict con las coincidencias, unidades totales y la fecha de actualización.
    """
    filas, actualizado = await _cargar_stock()
    if not filas:
        return {
            "disponible": None,
            "mensaje": "No pude consultar el stock en este momento. Sugiere confirmar disponibilidad en el sitio o con el equipo.",
        }

    consulta = (consulta or "").strip()
    if not consulta:
        return {"coincidencias": [], "mensaje": "Consulta vacía."}

    consulta_norm = _normalizar(consulta)

    # 1) Coincidencia exacta por SKU
    por_sku = [f for f in filas if _normalizar(f["sku"]) == consulta_norm]
    if por_sku:
        coincidencias = por_sku
    else:
        # 2) Coincidencia por tokens del nombre (todos los tokens presentes)
        tokens = [t for t in consulta_norm.split() if len(t) > 1]
        def score(fila):
            nombre_norm = _normalizar(fila["nombre"])
            return sum(1 for t in tokens if t in nombre_norm)
        con_score = [(score(f), f) for f in filas]
        max_score = max((s for s, _ in con_score), default=0)
        if max_score == 0:
            return {
                "coincidencias": [],
                "actualizado": actualizado,
                "mensaje": f"No encontré '{consulta}' en el stock. Prueba con otro término o confirma disponibilidad con el equipo.",
            }
        # Preferir filas que matchean TODOS los tokens; si no, las de mayor score
        umbral = len(tokens) if max_score >= len(tokens) else max_score
        coincidencias = [f for s, f in con_score if s >= umbral]

    # Agrupar total y armar detalle (máximo 20 variantes para no saturar)
    total = sum(f["stock"] for f in coincidencias)
    detalle = [
        {"nombre": f["nombre"], "sku": f["sku"], "stock": f["stock"], "precio": f["precio"]}
        for f in sorted(coincidencias, key=lambda x: -x["stock"])[:20]
    ]

    return {
        "coincidencias": detalle,
        "unidades_totales": total,
        "hay_stock": total > 0,
        "actualizado": actualizado,
        "nota": (
            "Stock referencial actualizado a diario; puede variar. "
            "Si el cliente pide confirmación exacta de talla, ofrécele confirmar con el equipo."
        ),
    }
