# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente de WhatsApp.
Funciona con cualquier proveedor (Meta, Twilio) gracias a la capa de providers.
"""

import os
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, obtener_ultimo_timestamp, listar_conversaciones, obtener_historial_completo, get_modo, get_estado, set_modo, MARCADOR_HUMANO, conversaciones_para_auto_retorno
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

# Auto-retorno: minutos de inactividad tras los cuales una conversación en modo
# humano vuelve sola a la IA. 0 = desactivado.
AUTO_RETORNO_MINUTOS = int(os.getenv("AUTO_RETORNO_MINUTOS", 60))
# Cada cuánto revisa el loop en background (10 minutos)
INTERVALO_AUTO_RETORNO_SEG = 600


async def _loop_auto_retorno():
    """
    Tarea liviana en background: cada 10 min busca conversaciones en modo humano
    inactivas por más de AUTO_RETORNO_MINUTOS y las devuelve a la IA.
    Tolerante a fallos: nunca debe tumbar la app.
    """
    while True:
        try:
            await asyncio.sleep(INTERVALO_AUTO_RETORNO_SEG)
            try:
                telefonos = await conversaciones_para_auto_retorno(AUTO_RETORNO_MINUTOS)
            except Exception as e:
                logger.warning(f"Auto-retorno: no se pudieron consultar conversaciones: {e}")
                continue
            for tel in telefonos:
                try:
                    await set_modo(tel, "ia", "auto_retorno")
                    logger.info(f"Auto-retorno: {tel} devuelto a IA por inactividad")
                except Exception as e:
                    logger.warning(f"Auto-retorno: no se pudo devolver {tel} a IA: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Auto-retorno: error inesperado en el loop: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa la base de datos al arrancar el servidor."""
    await inicializar_db()
    logger.info("Base de datos inicializada")
    logger.info(f"Servidor AgentKit corriendo en puerto {PORT}")
    logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")

    tarea_auto_retorno = None
    if AUTO_RETORNO_MINUTOS > 0:
        tarea_auto_retorno = asyncio.create_task(_loop_auto_retorno())
        logger.info(f"Auto-retorno activo: {AUTO_RETORNO_MINUTOS} min de inactividad")
    else:
        logger.info("Auto-retorno desactivado (AUTO_RETORNO_MINUTOS=0)")

    yield

    if tarea_auto_retorno is not None:
        tarea_auto_retorno.cancel()


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


class ResponderBody(BaseModel):
    mensaje: str


class ModoBody(BaseModel):
    modo: str
    nota: str | None = None


@app.post("/admin/conversaciones/{telefono}/responder")
async def admin_responder(
    telefono: str,
    body: ResponderBody,
    x_admin_token: str | None = Header(default=None),
):
    """
    Envía un mensaje al cliente como humano (Camila) y toma el control de la
    conversación automáticamente (modo → humano). Reutiliza la misma función de
    envío del proveedor que usa el resto del sistema.
    """
    _verificar_admin_token(x_admin_token)
    mensaje = (body.mensaje or "").strip()
    if not mensaje:
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")

    # 1) Enviar al cliente. Si falla (ej. ventana de 24h cerrada o error de Meta),
    #    NO cambiamos el modo ni guardamos nada: el cliente no recibió el mensaje,
    #    así que devolvemos el error real al panel (nada de 200 silencioso).
    enviado = await proveedor.enviar_mensaje(telefono, mensaje)
    if not enviado:
        raise HTTPException(
            status_code=502,
            detail=(
                "No se pudo entregar el mensaje al cliente (posible ventana de 24h "
                "cerrada o error de Meta). Revisa los logs. El modo no cambió."
            ),
        )

    # 2) El humano toma el control con solo escribir (sin paso extra).
    await set_modo(telefono, "humano", "humano_respondio")

    # 3) Guardar en el historial con marcador interno. El marcador queda SOLO en la
    #    base de datos (para distinguirlo en el panel); al cliente ya se le envió el
    #    texto limpio en el paso 1.
    await guardar_mensaje(telefono, "assistant", MARCADOR_HUMANO + mensaje)

    logger.info(f"Humano respondió a {telefono}; conversación en modo humano")
    return {"ok": True, "modo": "humano"}


@app.post("/admin/conversaciones/{telefono}/modo")
async def admin_cambiar_modo(
    telefono: str,
    body: ModoBody,
    x_admin_token: str | None = Header(default=None),
):
    """Cambia manualmente el modo de una conversación (ia | humano)."""
    _verificar_admin_token(x_admin_token)
    modo = (body.modo or "").strip().lower()
    if modo not in ("ia", "humano"):
        raise HTTPException(status_code=400, detail="Modo inválido, usa 'ia' o 'humano'")

    await set_modo(telefono, modo, "admin_manual", body.nota)

    # Al devolver la conversación a la IA, avisamos brevemente al cliente que
    # seguimos por acá. Es cosmético: si el envío falla (ventana de 24h cerrada),
    # lo ignoramos silenciosamente.
    if modo == "ia":
        try:
            enviado = await proveedor.enviar_mensaje(
                telefono,
                "¡Gracias por tu paciencia! Sigo por acá para lo que necesites. 🚴",
            )
            if not enviado:
                logger.info(f"Mensaje de retome a {telefono} no entregado (ignorado)")
        except Exception as e:
            logger.info(f"No se pudo enviar mensaje de retome a {telefono} (ignorado): {e}")

    return {"ok": True, "modo": modo}
