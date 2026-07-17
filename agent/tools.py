# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas específicas del negocio de República Ciclismo.
Estas funciones extienden las capacidades del agente más allá de responder texto.
"""

import os
import yaml
import logging
from datetime import datetime

logger = logging.getLogger("agentkit")

HORARIO_SHOWROOM = {
    # 0 = lunes ... 6 = domingo
    0: ("09:00", "19:00"),
    1: ("09:00", "19:00"),
    2: ("09:00", "19:00"),
    3: ("09:00", "19:00"),
    4: ("09:00", "19:00"),
    5: ("10:00", "13:00"),
}


def cargar_info_negocio() -> dict:
    """Carga la información del negocio desde business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atención del negocio."""
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "No disponible"),
        "showroom": info.get("negocio", {}).get("showroom", "No disponible"),
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca información relevante en los archivos de /knowledge.
    Retorna el contenido más relevante encontrado.
    """
    resultados = []
    knowledge_dir = "knowledge"

    if not os.path.exists(knowledge_dir):
        return "No hay archivos de conocimiento disponibles."

    for archivo in os.listdir(knowledge_dir):
        ruta = os.path.join(knowledge_dir, archivo)
        if archivo.startswith(".") or not os.path.isfile(ruta):
            continue
        try:
            with open(ruta, "r", encoding="utf-8") as f:
                contenido = f.read()
                if consulta.lower() in contenido.lower():
                    resultados.append(f"[{archivo}]: {contenido[:500]}")
        except (UnicodeDecodeError, IOError):
            continue

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontré información específica sobre eso en mis archivos."


# ════════════════════════════════════════════════════════════
# Agendar citas — visitas al showroom
# ════════════════════════════════════════════════════════════

def validar_horario_showroom(fecha: str, hora: str) -> dict:
    """
    Valida si una fecha/hora propuesta cae dentro del horario del showroom.

    Args:
        fecha: Fecha en formato YYYY-MM-DD
        hora: Hora en formato HH:MM

    Returns:
        dict con "valido" (bool) y "mensaje" (str) explicando el resultado
    """
    try:
        dia = datetime.strptime(fecha, "%Y-%m-%d")
    except ValueError:
        return {"valido": False, "mensaje": "Formato de fecha inválido, usa YYYY-MM-DD."}

    dia_semana = dia.weekday()
    if dia_semana not in HORARIO_SHOWROOM:
        return {"valido": False, "mensaje": "El showroom no atiende los domingos."}

    apertura, cierre = HORARIO_SHOWROOM[dia_semana]
    if not (apertura <= hora <= cierre):
        return {
            "valido": False,
            "mensaje": f"Ese horario no está disponible. Ese día atendemos de {apertura} a {cierre}.",
        }

    return {"valido": True, "mensaje": "Horario disponible."}


async def notificar_camila(mensaje: str) -> bool:
    """
    Envía un WhatsApp a Camila (gerente) avisando de una cita o lead nuevo.
    Si CAMILA_WHATSAPP_NUMBER no está configurado, solo lo deja en el log.
    """
    numero = os.getenv("CAMILA_WHATSAPP_NUMBER")
    if not numero:
        logger.warning("CAMILA_WHATSAPP_NUMBER no configurado, no se pudo notificar a Camila")
        return False

    from agent.providers import obtener_proveedor
    proveedor = obtener_proveedor()
    enviado = await proveedor.enviar_mensaje(numero, mensaje)
    if not enviado:
        logger.error("No se pudo enviar la notificación de WhatsApp a Camila")
    return enviado


async def registrar_cita(telefono: str, nombre: str, fecha: str, hora: str, motivo: str) -> dict:
    """
    Registra una cita para visitar el showroom, la guarda en config/citas.yaml
    y avisa a Camila por WhatsApp para que la confirme con el cliente.

    Args:
        telefono: Número de teléfono del cliente
        nombre: Nombre del cliente
        fecha: Fecha propuesta (YYYY-MM-DD)
        hora: Hora propuesta (HH:MM)
        motivo: Qué quiere ver o probar el cliente

    Returns:
        dict con el resultado del registro
    """
    validacion = validar_horario_showroom(fecha, hora)
    if not validacion["valido"]:
        return {"registrada": False, "mensaje": validacion["mensaje"]}

    ruta_citas = "config/citas.yaml"
    citas = []
    if os.path.exists(ruta_citas):
        with open(ruta_citas, "r", encoding="utf-8") as f:
            citas = yaml.safe_load(f) or []

    citas.append({
        "telefono": telefono,
        "nombre": nombre,
        "fecha": fecha,
        "hora": hora,
        "motivo": motivo,
        "creada": datetime.utcnow().isoformat(),
    })

    with open(ruta_citas, "w", encoding="utf-8") as f:
        yaml.safe_dump(citas, f, allow_unicode=True)

    logger.info(f"Cita registrada: {nombre} ({telefono}) — {fecha} {hora}")

    await notificar_camila(
        f"📅 Nueva solicitud de cita en el showroom\n"
        f"Cliente: {nombre} ({telefono})\n"
        f"Día/hora solicitada: {fecha} {hora}\n"
        f"Quiere ver: {motivo}\n\n"
        f"Confírmale directo al cliente por WhatsApp: {telefono}"
    )

    return {"registrada": True, "mensaje": "Cita registrada correctamente, Camila fue notificada."}


# ════════════════════════════════════════════════════════════
# Calificación y atención de leads de venta
# ════════════════════════════════════════════════════════════

async def registrar_lead(telefono: str, nombre: str, interes: str, calificacion: str) -> dict:
    """
    Registra un lead de venta en config/leads.yaml y avisa a Camila por WhatsApp
    para que le haga seguimiento.

    Args:
        telefono: Número de teléfono del cliente
        nombre: Nombre del cliente (si lo dio)
        interes: Producto o categoría de interés
        calificacion: "alto", "medio" o "bajo" según urgencia/intención de compra

    Returns:
        dict con el resultado del registro
    """
    ruta_leads = "config/leads.yaml"
    leads = []
    if os.path.exists(ruta_leads):
        with open(ruta_leads, "r", encoding="utf-8") as f:
            leads = yaml.safe_load(f) or []

    leads.append({
        "telefono": telefono,
        "nombre": nombre,
        "interes": interes,
        "calificacion": calificacion,
        "creado": datetime.utcnow().isoformat(),
    })

    with open(ruta_leads, "w", encoding="utf-8") as f:
        yaml.safe_dump(leads, f, allow_unicode=True)

    logger.info(f"Lead registrado: {nombre or telefono} — interés: {interes} — calificación: {calificacion}")

    emoji_calificacion = {"alto": "🔥", "medio": "🙂", "bajo": "💤"}.get(calificacion, "🙂")
    await notificar_camila(
        f"{emoji_calificacion} Nuevo lead de venta ({calificacion})\n"
        f"Cliente: {nombre or 'sin nombre'} ({telefono})\n"
        f"Interés: {interes}\n\n"
        f"Puedes escribirle directo por WhatsApp: {telefono}"
    )

    return {"registrado": True, "mensaje": "Lead registrado correctamente, Camila fue notificada."}
