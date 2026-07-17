# agent/memory.py — Memoria de conversaciones con SQLite
# Generado por AgentKit

"""
Sistema de memoria del agente. Guarda el historial de conversaciones
por número de teléfono usando SQLite (local) o PostgreSQL (producción).
"""

import os
from datetime import datetime
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Text, DateTime, select,Integer, func
from dotenv import load_dotenv

load_dotenv()

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

        return [
            {"role": msg.role, "content": msg.content}
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
        return [
            {
                "telefono": msg.telefono,
                "ultimo_mensaje": msg.content,
                "timestamp": msg.timestamp.isoformat() if msg.timestamp else None,
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
