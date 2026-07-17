# agent/brain.py — Cerebro del agente: conexión con Claude API
# Generado por AgentKit

"""
Lógica de IA del agente. Lee el system prompt de prompts.yaml
y genera respuestas usando la API de Anthropic Claude.
"""

import os
import json
import yaml
import logging
from datetime import datetime
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent import tools as tools_module
from agent import stock as stock_module
from agent.memory import obtener_perfil, actualizar_perfil

load_dotenv()
logger = logging.getLogger("agentkit")

# Cliente de Anthropic
client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Máximo de idas y vueltas de tool-use por mensaje, para evitar loops infinitos
MAX_ITERACIONES_TOOLS = 5

DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]

# Herramientas que el agente puede invocar durante la conversación
TOOLS = [
    {
        "name": "registrar_cita",
        "description": (
            "Registra una solicitud de cita para visitar el showroom y notifica a Camila "
            "(la gerente) por WhatsApp para que la confirme. Úsala apenas el cliente "
            "confirme día, hora y qué quiere ver, dentro del horario general del showroom "
            "(Lunes a Viernes 9:00-19:00, Sábados 10:00-13:00)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre del cliente"},
                "fecha": {"type": "string", "description": "Fecha de la cita en formato YYYY-MM-DD"},
                "hora": {"type": "string", "description": "Hora de la cita en formato HH:MM (24 horas)"},
                "motivo": {"type": "string", "description": "Qué quiere ver o probar el cliente"},
            },
            "required": ["nombre", "fecha", "hora", "motivo"],
        },
    },
    {
        "name": "registrar_lead",
        "description": (
            "Registra un lead de venta y notifica a Camila (la gerente) por WhatsApp para "
            "que le haga seguimiento. Úsala cuando el cliente muestre intención real de "
            "comprar (no solo curiosidad por precios)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre del cliente, o 'sin nombre' si no lo dio"},
                "interes": {"type": "string", "description": "Producto o categoría de interés"},
                "calificacion": {
                    "type": "string",
                    "enum": ["alto", "medio", "bajo"],
                    "description": "Qué tan urgente/probable es la compra",
                },
            },
            "required": ["nombre", "interes", "calificacion"],
        },
    },
    {
        "name": "consultar_stock",
        "description": (
            "Consulta la disponibilidad y unidades en stock de un producto (stock real, "
            "actualizado a diario). Úsala SIEMPRE que el cliente pregunte si hay algo "
            "disponible, por tallas, o antes de asegurar que un producto está en stock. "
            "Busca por nombre del producto (o parte) o por SKU. Devuelve las variantes "
            "que coinciden con sus unidades disponibles."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "consulta": {
                    "type": "string",
                    "description": "Nombre del producto o parte de él (ej. 'guantes merino', 'casco egos') o un SKU exacto",
                },
            },
            "required": ["consulta"],
        },
    },
    {
        "name": "escalar_a_humano",
        "description": (
            "Escala la conversación a un humano (Camila) y deja de responder tú. "
            "Úsala DE VERDAD (no solo digas que conectarás con alguien) cuando: "
            "(a) el cliente pide explícitamente hablar con una persona, (b) pregunta "
            "algo que no puedes responder con tu información o es demasiado técnico, o "
            "(c) detectas un lead de alta intención que requiere atención personalizada. "
            "Tras llamarla, envía UNA sola despedida breve; luego Camila toma el control."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "motivo": {
                    "type": "string",
                    "description": "Motivo del escalamiento: qué necesita el cliente y por qué lo pasas a un humano",
                },
            },
            "required": ["motivo"],
        },
    },
    {
        "name": "actualizar_perfil_cliente",
        "description": (
            "Guarda datos DURABLES que aprendiste del cliente para recordarlos en futuras "
            "conversaciones (memoria larga). Úsala cuando el cliente comparta algo estable: "
            "su nombre, disciplina (ruta/gravel/MTB/triatlón), tallas, o intereses claros. "
            "No la uses para cosas pasajeras. Solo envía los campos que aprendiste."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre del cliente"},
                "disciplina": {"type": "string", "description": "Disciplina: ruta, gravel, MTB, triatlón, etc."},
                "tallas": {"type": "string", "description": "Tallas conocidas (ej. 'polera M, calza L')"},
                "intereses": {"type": "string", "description": "Productos o categorías que le interesan"},
                "notas": {"type": "string", "description": "Cualquier otro dato útil y estable del cliente"},
            },
        },
    },
]


def cargar_config_prompts() -> dict:
    """Lee toda la configuración desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_catalogo_productos() -> str:
    """Lee el catálogo de productos con enlaces exactos (generado desde el export de Shopify)."""
    try:
        with open("config/catalogo_productos.md", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def cargar_system_prompt() -> str:
    """Lee el system prompt desde config/prompts.yaml y le anexa el catálogo de productos."""
    config = cargar_config_prompts()
    system_prompt = config.get("system_prompt", "Eres un asistente útil. Responde en español.")

    catalogo = cargar_catalogo_productos()
    if catalogo:
        system_prompt += "\n\n" + catalogo

    return system_prompt


def obtener_mensaje_error() -> str:
    """Retorna el mensaje de error configurado en prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("error_message", "Lo siento, estoy teniendo problemas técnicos. Por favor intenta de nuevo en unos minutos.")


def obtener_mensaje_fallback() -> str:
    """Retorna el mensaje de fallback configurado en prompts.yaml."""
    config = cargar_config_prompts()
    return config.get("fallback_message", "Disculpa, no entendí tu mensaje. ¿Podrías reformularlo?")


def obtener_fecha_actual_texto() -> str:
    """Texto en español con la fecha/hora actual, para que el modelo calcule fechas relativas."""
    ahora = datetime.now()
    dia_semana = DIAS_ES[ahora.weekday()]
    mes = MESES_ES[ahora.month - 1]
    return (
        f"Hoy es {dia_semana} {ahora.day} de {mes} de {ahora.year}, {ahora.strftime('%H:%M')} hrs "
        f"(hora de Chile). Usa esta fecha como referencia para calcular fechas relativas "
        f"(\"mañana\", \"el viernes\", etc.) al registrar una cita, siempre en formato YYYY-MM-DD."
    )


async def ejecutar_tool(nombre: str, input_data: dict, telefono: str) -> dict:
    """Ejecuta la herramienta pedida por Claude y retorna su resultado."""
    try:
        if nombre == "registrar_cita":
            return await tools_module.registrar_cita(
                telefono=telefono,
                nombre=input_data.get("nombre", ""),
                fecha=input_data.get("fecha", ""),
                hora=input_data.get("hora", ""),
                motivo=input_data.get("motivo", ""),
            )
        elif nombre == "registrar_lead":
            return await tools_module.registrar_lead(
                telefono=telefono,
                nombre=input_data.get("nombre", ""),
                interes=input_data.get("interes", ""),
                calificacion=input_data.get("calificacion", "medio"),
            )
        elif nombre == "consultar_stock":
            return await stock_module.consultar_stock(input_data.get("consulta", ""))
        elif nombre == "escalar_a_humano":
            return await tools_module.escalar_a_humano(
                telefono=telefono,
                motivo=input_data.get("motivo", ""),
            )
        elif nombre == "actualizar_perfil_cliente":
            return await actualizar_perfil(
                telefono,
                nombre=input_data.get("nombre"),
                disciplina=input_data.get("disciplina"),
                tallas=input_data.get("tallas"),
                intereses=input_data.get("intereses"),
                notas=input_data.get("notas"),
            )
        else:
            return {"error": f"Herramienta desconocida: {nombre}"}
    except Exception as e:
        logger.error(f"Error ejecutando tool '{nombre}': {e}")
        return {"error": str(e)}


async def generar_respuesta(
    mensaje: str,
    historial: list[dict],
    conversacion_reciente: bool = False,
    telefono: str = "",
) -> str:
    """
    Genera una respuesta usando Claude API, con soporte de tool-use para
    registrar citas y leads de venta.

    Args:
        mensaje: El mensaje nuevo del usuario
        historial: Lista de mensajes anteriores [{"role": "user/assistant", "content": "..."}]
        conversacion_reciente: True si el cliente ya escribió hace menos de 12 horas.
            En ese caso el agente no debe volver a saludar/presentarse como si fuera la
            primera vez.
        telefono: Número de teléfono del cliente, usado al registrar citas/leads.

    Returns:
        La respuesta generada por Claude
    """
    # Si el mensaje es muy corto o vacío, usar fallback
    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback()

    # ── Parte ESTABLE del system prompt (base + catálogo): idéntica en cada mensaje
    # y entre todos los clientes, así que se cachea (prompt caching). Las lecturas de
    # caché cuestan ~10% del precio normal, lo que reduce muchísimo el costo por mensaje.
    system_estable = cargar_system_prompt()

    # ── Parte VOLÁTIL: cambia por mensaje/cliente (fecha, perfil, contexto). Va DESPUÉS
    # del punto de caché, sin cachear, para no invalidar la caché del bloque estable.
    partes_volatiles = ["## Fecha y hora actual\n" + obtener_fecha_actual_texto()]

    # Memoria larga: cargar lo que ya sabemos de este cliente
    if telefono:
        try:
            perfil = await obtener_perfil(telefono)
        except Exception as e:
            logger.error(f"Error al cargar perfil de {telefono}: {e}")
            perfil = None
        if perfil:
            datos = [f"{k}: {v}" for k, v in perfil.items() if v]
            if datos:
                partes_volatiles.append(
                    "## Lo que ya sabes de este cliente\n"
                    "Usa estos datos para personalizar la conversación (salúdalo por su nombre "
                    "si lo tienes, no vuelvas a preguntar lo que ya sabes). Si algo cambió, "
                    "actualízalo con la herramienta actualizar_perfil_cliente:\n- "
                    + "\n- ".join(datos)
                )

    if conversacion_reciente:
        partes_volatiles.append(
            "## Contexto de esta conversación\n"
            "El cliente ya te escribió hace menos de 12 horas: esta es una conversación "
            "en curso. NO vuelvas a saludar formalmente ni a presentarte de nuevo como si "
            "fuera la primera vez. Responde directo al mensaje, y si quieres reconocer que "
            "sigue la conversación usa algo breve y natural (ej. \"¡Hola de nuevo!\", "
            "\"Dime\", \"Cuéntame\"), nunca la presentación completa de saludo inicial."
        )

    # system como lista de bloques: el estable se cachea (cache_control), el volátil no.
    system_blocks = [
        {
            "type": "text",
            "text": system_estable,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "\n\n".join(partes_volatiles)},
    ]

    # Construir mensajes para la API
    mensajes = []
    for msg in historial:
        mensajes.append({
            "role": msg["role"],
            "content": msg["content"]
        })

    # Agregar el mensaje actual
    mensajes.append({
        "role": "user",
        "content": mensaje
    })

    try:
        for _ in range(MAX_ITERACIONES_TOOLS):
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_blocks,
                messages=mensajes,
                tools=TOOLS,
            )

            u = response.usage
            logger.info(
                f"Respuesta generada ({u.input_tokens} in / {u.output_tokens} out / "
                f"cache_write {getattr(u, 'cache_creation_input_tokens', 0)} / "
                f"cache_read {getattr(u, 'cache_read_input_tokens', 0)})"
            )

            if response.stop_reason != "tool_use":
                texto = "".join(bloque.text for bloque in response.content if bloque.type == "text")
                return texto or obtener_mensaje_fallback()

            # Claude pidió usar una o más herramientas: ejecutarlas y devolverle el resultado
            mensajes.append({"role": "assistant", "content": response.content})
            resultados_tools = []
            for bloque in response.content:
                if bloque.type == "tool_use":
                    resultado = await ejecutar_tool(bloque.name, bloque.input, telefono)
                    resultados_tools.append({
                        "type": "tool_result",
                        "tool_use_id": bloque.id,
                        "content": json.dumps(resultado, ensure_ascii=False),
                    })
            mensajes.append({"role": "user", "content": resultados_tools})

        logger.warning("Se alcanzó el máximo de iteraciones de tool-use sin respuesta final")
        return obtener_mensaje_error()

    except Exception as e:
        logger.error(f"Error Claude API: {e}")
        return obtener_mensaje_error()
