# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente de WhatsApp.
Funciona con cualquier proveedor (Meta, Twilio) gracias a la capa de providers.
"""

import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, obtener_ultimo_timestamp, listar_conversaciones, obtener_historial_completo, get_modo, get_estado
from agent.providers import obtener_proveedor
from agent.tools import notificar_camila

load_dotenv()

# Configuración de logging según entorno
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

# Proveedor de WhatsApp (se configura en .env con WHATSAPP_PROVIDER)
proveedor = obtener_proveedor()
PORT = int(os.getenv("PORT", 8000))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la base de datos al arrancar el servidor."""
    await inicializar_db()
    logger.info("Base de datos inicializada")
    logger.info(f"Servidor AgentKit corriendo en puerto {PORT}")
    logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
    yield


app = FastAPI(
    title="AgentKit — WhatsApp AI Agent",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
@app.get("/")
async def health_check():
    """Endpoint de salud para Railway/monitoreo."""
    return {"status": "ok", "service": "agentkit"}


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificación GET del webhook (requerido por Meta Cloud API, no-op para otros)."""
    resultado = await proveedor.validar_webhook(request)
    if resultado is not None:
        return PlainTextResponse(str(resultado))
    return {"status": "ok"}


async def _avisar_mensaje_en_modo_humano(telefono: str, texto: str):
    """
    Cuando una conversación está en modo humano, Francisca no responde, pero
    Camila igual debe enterarse del mensaje entrante para contestarlo desde el
    panel. Reenvía el mensaje del cliente (sin respuesta de la IA).
    """
    if telefono == os.getenv("CAMILA_WHATSAPP_NUMBER"):
        return
    await notificar_camila(
        f"🙋 Mensaje de {telefono} (modo humano — Francisca en silencio)\n\n"
        f"Cliente: {texto}\n\n"
        f"Respóndele desde el panel."
    )


@app.post("/webhook")
async def webhook_handler(request: Request):
    """
    Recibe mensajes de WhatsApp via el proveedor configurado.
    Procesa el mensaje, genera respuesta con Claude y la envía de vuelta.
    """
    try:
        # Parsear webhook — el proveedor normaliza el formato
        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            # Ignorar mensajes propios o vacíos
            if msg.es_propio or not msg.texto:
                continue

            logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")

            # ── Check 1 (antes del LLM): si un humano tomó el control, Francisca
            # se queda en silencio. Igual guardamos el mensaje y avisamos a Camila.
            # Falla "abierto" hacia IA: ante cualquier error consultando el modo,
            # respondemos como siempre para nunca dejar al cliente sin respuesta.
            try:
                modo_actual = await get_modo(msg.telefono)
            except Exception as e:
                logger.warning(f"No se pudo leer el modo de {msg.telefono}, asumo IA: {e}")
                modo_actual = "ia"

            if modo_actual == "humano":
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                logger.info(f"Modo humano activo para {msg.telefono}: Francisca en silencio")
                await _avisar_mensaje_en_modo_humano(msg.telefono, msg.texto)
                continue

            # Obtener historial ANTES de guardar el mensaje actual
            # (brain.py agrega el mensaje actual, evitando duplicados)
            historial = await obtener_historial(msg.telefono)

            # Si el cliente ya escribió hace menos de 12 horas, el agente no debe
            # volver a saludar/presentarse como si fuera la primera vez
            ultimo_ts = await obtener_ultimo_timestamp(msg.telefono)
            conversacion_reciente = bool(ultimo_ts and (datetime.utcnow() - ultimo_ts) < timedelta(hours=12))

            # Generar respuesta con Claude
            respuesta = await generar_respuesta(msg.texto, historial, conversacion_reciente, msg.telefono)

            # ── Check 2 (anti-carrera): volver a consultar el modo justo antes de
            # enviar. Si un HUMANO intervino mientras el LLM generaba, descartamos
            # la respuesta para no responder dos veces. OJO: si el cambio a 'humano'
            # lo provocó la propia Francisca al escalar (francisca_escalo), NO se
            # descarta — esa respuesta es su despedida y sí debe enviarse.
            try:
                estado_post = await get_estado(msg.telefono)
            except Exception as e:
                logger.warning(f"No se pudo re-leer el modo de {msg.telefono}, asumo IA: {e}")
                estado_post = {"modo": "ia", "cambiado_por": None}

            if estado_post["modo"] == "humano" and estado_post.get("cambiado_por") != "francisca_escalo":
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                logger.info(f"Respuesta descartada por intervención humana para {msg.telefono}")
                await _avisar_mensaje_en_modo_humano(msg.telefono, msg.texto)
                continue

            # Guardar mensaje del usuario Y respuesta del agente en memoria
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            # Enviar respuesta por WhatsApp via el proveedor
            await proveedor.enviar_mensaje(msg.telefono, respuesta)

            logger.info(f"Respuesta a {msg.telefono}: {respuesta}")

            # Reenviar copia de la conversación para monitoreo (no bloquea la respuesta
            # al cliente). Si Francisca acaba de escalar, Camila ya recibió el aviso de
            # escalamiento con el motivo, así que evitamos el doble ping.
            escalo_este_turno = estado_post["modo"] == "humano" and estado_post.get("cambiado_por") == "francisca_escalo"
            if msg.telefono != os.getenv("CAMILA_WHATSAPP_NUMBER") and not escalo_este_turno:
                await notificar_camila(
                    f"💬 Conversación con {msg.telefono}\n\n"
                    f"Cliente: {msg.texto}\n\n"
                    f"Francisca: {respuesta}"
                )
        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

def _verificar_admin_token(x_admin_token: str | None):
    if not ADMIN_TOKEN or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="No autorizado")
@app.get("/admin/conversaciones")
async def admin_listar_conversaciones(x_admin_token: str | None = Header(default=None)):
    """Lista de conversaciones para el panel de monitoreo."""
    _verificar_admin_token(x_admin_token)
    return await listar_conversaciones()

@app.get("/admin/conversaciones/{telefono}")
async def admin_historial_conversacion(telefono: str, x_admin_token: str | None = Header(default=None)):
    """Historial completo de una conversación para el panel de monitoreo."""
    _verificar_admin_token(x_admin_token)
    return await obtener_historial_completo(telefono)
