# agent/memory.py — Memoria de conversaciones con SQLite
# Generado por AgentKit

"""
Sistema de memoria del agente. Guarda el historial de conversaciones
por número de teléfono usando SQLite (local) o PostgreSQL (producción).
"""

import os
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, select,Integer, func, Boolean
from dotenv import load_dotenv

load_dotenv()

# Marcador interno para mensajes escritos por un humano (Camila) desde el panel.
# Se guarda SOLO en la base de datos, como prefijo del contenido, para poder
# distinguir en el panel los mensajes de Francisca de los del humano. NUNCA se
# envía al cliente ni se le muestra a Claude (se remueve en obtener_historial).
MARCADOR_HUMANO = "[HUMANO] "

# Modos válidos de una conversación
MODOS_VALIDOS = {"ia", "humano"}

# Configuración de base de datos
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

# Si es PostgreSQL en producción, ajustar el esquema de URL
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Modelo de mensaje en la base de datos."""
    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" o "assistant"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PerfilCliente(Base):
    """
    Perfil durable de un cliente (memoria larga). El agente lo va completando
    a medida que conoce a la persona: nombre, disciplina, tallas, intereses, notas.
    Sobrevive más allá de la ventana de historial de mensajes.
    """
    __tablename__ = "perfiles_cliente"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    nombre: Mapped[str | None] = mapped_column(String(120), nullable=True)
    disciplina: Mapped[str | None] = mapped_column(String(120), nullable=True)  # ruta, gravel, MTB, triatlón
    tallas: Mapped[str | None] = mapped_column(String(120), nullable=True)      # ej. "polera M, calza L"
    intereses: Mapped[str | None] = mapped_column(Text, nullable=True)          # productos/categorías de interés
    notas: Mapped[str | None] = mapped_column(Text, nullable=True)              # cualquier dato útil adicional
    actualizado: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class EstadoConversacion(Base):
    """
    Estado de control de una conversación: define si responde la IA (Francisca)
    o si un humano (Camila) tomó el control.

    Si NO existe fila para un teléfono, el modo por defecto es 'ia', de modo que
    el comportamiento histórico queda intacto (incluso en un deploy a medias donde
    la tabla recién se está creando: los checks fallan "abierto" hacia 'ia').

    Nota: modo_desde se guarda en UTC naïve, igual que el resto del esquema
    (timestamp de Mensaje, actualizado de PerfilCliente), para que las
    comparaciones de tiempo del auto-retorno sean consistentes.
    """
    __tablename__ = "conversation_state"

    telefono: Mapped[str] = mapped_column(String(50), primary_key=True)
    modo: Mapped[str] = mapped_column(String(20), default="ia")  # 'ia' | 'humano'
    modo_desde: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 'francisca_escalo' | 'humano_respondio' | 'admin_manual' | 'auto_retorno'
    cambiado_por: Mapped[str | None] = mapped_column(String(40), nullable=True)
    nota: Mapped[str | None] = mapped_column(Text, nullable=True)


class MetricaIA(Base):
    """
    Métrica mínima por turno de la IA: modelo usado, latencia, si escaló a humano
    y tokens (cuando el SDK los expone). Tabla nueva y aislada, para no tocar el
    esquema del historial.
    """
    __tablename__ = "ia_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    modelo: Mapped[str] = mapped_column(String(60))
    latencia_ms: Mapped[int] = mapped_column(Integer)
    escalado: Mapped[bool] = mapped_column(Boolean, default=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de conversación."""
    async with async_session() as session:
        mensaje = Mensaje(
            telefono=telefono,
            role=role,
            content=content,
            timestamp=datetime.utcnow()
        )
        session.add(mensaje)
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Recupera los últimos N mensajes de una conversación.

    Args:
        telefono: Número de teléfono del cliente
        limite: Máximo de mensajes a recuperar (default: 20)

    Returns:
        Lista de diccionarios con role y content
    """
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.desc())
            .limit(limite)
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()

        # Invertir para orden cronológico (los más recientes están primero)
        mensajes.reverse()

        # Los mensajes escritos por un humano se guardan como 'assistant' con el
        # prefijo MARCADOR_HUMANO. Ese prefijo es solo para el panel: al construir
        # el historial que ve Claude lo removemos, para que vea texto natural.
        return [
            {
                "role": msg.role,
                "content": (
                    msg.content[len(MARCADOR_HUMANO):]
                    if msg.content.startswith(MARCADOR_HUMANO)
                    else msg.content
                ),
            }
            for msg in mensajes
        ]


async def obtener_ultimo_timestamp(telefono: str) -> datetime | None:
    """Retorna el timestamp del último mensaje de esa conversación, o None si no hay historial."""
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.desc())
            .limit(1)
        )
        result = await session.execute(query)
        ultimo = result.scalars().first()
        return ultimo.timestamp if ultimo else None


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversación."""
    async with async_session() as session:
        query = select(Mensaje).where(Mensaje.telefono == telefono)
        result = await session.execute(query)
        mensajes = result.scalars().all()
        for msg in mensajes:
            await session.delete(msg)
        await session.commit()


async def listar_conversaciones() -> list[dict]:
    """Retorna la lista de conversaciones con teléfono, último mensaje y timestamp."""
    async with async_session() as session:
        subq = (
            select(Mensaje.telefono, func.max(Mensaje.timestamp).label("ultimo_ts"))
            .group_by(Mensaje.telefono)
            .subquery()
        )
        query = (
            select(Mensaje)
            .join(
                subq,
                (Mensaje.telefono == subq.c.telefono) & (Mensaje.timestamp == subq.c.ultimo_ts),
            )
            .order_by(subq.c.ultimo_ts.desc())
        )
        result = await session.execute(query)
        ultimos = result.scalars().all()

        # Estado de modo (ia/humano) por conversación, para el panel.
        estados_result = await session.execute(select(EstadoConversacion))
        estados = {
            e.telefono: {
                "modo": e.modo,
                "modo_desde": e.modo_desde.isoformat() if e.modo_desde else None,
            }
            for e in estados_result.scalars().all()
        }

        return [
            {
                "telefono": msg.telefono,
                "ultimo_mensaje": msg.content,
                "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
                "modo": estados.get(msg.telefono, {}).get("modo", "ia"),
                "modo_desde": estados.get(msg.telefono, {}).get("modo_desde"),
            }
            for msg in ultimos
        ]

async def obtener_perfil(telefono: str) -> dict | None:
    """Retorna el perfil durable del cliente, o None si aún no existe."""
    async with async_session() as session:
        perfil = await session.get(PerfilCliente, telefono)
        if perfil is None:
            return None
        return {
            "nombre": perfil.nombre,
            "disciplina": perfil.disciplina,
            "tallas": perfil.tallas,
            "intereses": perfil.intereses,
            "notas": perfil.notas,
        }


async def actualizar_perfil(telefono: str, **campos) -> dict:
    """
    Crea o actualiza el perfil de un cliente. Solo modifica los campos entregados
    con un valor no vacío; deja los demás intactos.
    """
    permitidos = {"nombre", "disciplina", "tallas", "intereses", "notas"}
    async with async_session() as session:
        perfil = await session.get(PerfilCliente, telefono)
        if perfil is None:
            perfil = PerfilCliente(telefono=telefono)
            session.add(perfil)
        for clave, valor in campos.items():
            if clave in permitidos and valor:
                setattr(perfil, clave, str(valor))
        perfil.actualizado = datetime.utcnow()
        await session.commit()
    return {"guardado": True}


async def obtener_historial_completo(telefono: str) -> list[dict]:
    """Retorna todo el historial de mensajes de un número, sin límite, para el panel de monitoreo."""
    async with async_session() as session:
        query = (
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.timestamp.asc())
        )
        result = await session.execute(query)
        mensajes = result.scalars().all()
        return [
            {
                "role": msg.role,
                "content": msg.content,
                "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
            }
            for msg in mensajes
        ]


# ════════════════════════════════════════════════════════════
# Estado de conversación (modo IA / humano)
# ════════════════════════════════════════════════════════════

async def get_modo(telefono: str) -> str:
    """
    Retorna el modo de la conversación: 'ia' o 'humano'.
    Si no hay fila para ese teléfono, devuelve 'ia' (default seguro).
    """
    async with async_session() as session:
        estado = await session.get(EstadoConversacion, telefono)
        return estado.modo if estado else "ia"


async def get_estado(telefono: str) -> dict:
    """
    Retorna el estado completo de la conversación (modo, cuándo cambió, quién lo
    cambió y la nota). Si no hay fila, devuelve el default seguro con modo 'ia'.
    """
    async with async_session() as session:
        estado = await session.get(EstadoConversacion, telefono)
        if estado is None:
            return {"modo": "ia", "modo_desde": None, "cambiado_por": None, "nota": None}
        return {
            "modo": estado.modo,
            "modo_desde": estado.modo_desde.isoformat() if estado.modo_desde else None,
            "cambiado_por": estado.cambiado_por,
            "nota": estado.nota,
        }


async def set_modo(telefono: str, modo: str, cambiado_por: str, nota: str | None = None) -> dict:
    """
    Crea o actualiza (upsert) el estado de una conversación y refresca modo_desde.

    Args:
        telefono: Número del cliente
        modo: 'ia' o 'humano'
        cambiado_por: quién/qué provocó el cambio
            ('francisca_escalo', 'humano_respondio', 'admin_manual', 'auto_retorno')
        nota: motivo u observación opcional
    """
    if modo not in MODOS_VALIDOS:
        raise ValueError(f"Modo inválido: {modo}. Usa 'ia' o 'humano'.")
    async with async_session() as session:
        estado = await session.get(EstadoConversacion, telefono)
        if estado is None:
            estado = EstadoConversacion(telefono=telefono)
            session.add(estado)
        estado.modo = modo
        estado.modo_desde = datetime.utcnow()
        estado.cambiado_por = cambiado_por
        estado.nota = nota
        await session.commit()
    return {"modo": modo, "cambiado_por": cambiado_por}


async def conversaciones_para_auto_retorno(minutos: int) -> list[str]:
    """
    Retorna los teléfonos en modo 'humano' cuya última actividad fue hace más de
    `minutos` minutos, para devolverlos automáticamente a la IA.

    "Última actividad" es lo más reciente entre el cambio de modo (modo_desde) y
    el último mensaje de la conversación, para no interrumpir un chat humano activo.

    Si `minutos` <= 0, el auto-retorno está desactivado y retorna lista vacía.
    """
    if minutos <= 0:
        return []
    limite = datetime.utcnow() - timedelta(minutes=minutos)
    expiradas: list[str] = []
    async with async_session() as session:
        result = await session.execute(
            select(EstadoConversacion).where(EstadoConversacion.modo == "humano")
        )
        for estado in result.scalars().all():
            ultima_actividad = estado.modo_desde or datetime.min
            ult_msg = await session.execute(
                select(func.max(Mensaje.timestamp)).where(Mensaje.telefono == estado.telefono)
            )
            ts = ult_msg.scalar()
            if ts and ts > ultima_actividad:
                ultima_actividad = ts
            if ultima_actividad < limite:
                expiradas.append(estado.telefono)
    return expiradas


# ════════════════════════════════════════════════════════════
# Métricas por turno de IA
# ════════════════════════════════════════════════════════════

async def registrar_metrica(
    telefono: str,
    modelo: str,
    latencia_ms: int,
    escalado: bool = False,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
):
    """Guarda una métrica del turno de IA. Nunca debe romper la respuesta al cliente."""
    async with async_session() as session:
        session.add(MetricaIA(
            telefono=telefono,
            modelo=modelo,
            latencia_ms=latencia_ms,
            escalado=escalado,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ))
        await session.commit()
