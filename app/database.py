"""Async SQLAlchemy engine/session + schema bootstrap (PostgreSQL + pgvector)."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

log = logging.getLogger("kisansetu.db")

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DB_ECHO,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_POOL_SIZE,
    pool_pre_ping=True,
    pool_recycle=1800,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            # Detach (don't expire) what was loaded so the error page can still read e.g. the signed-in user's name —
            # a plain rollback expires every instance and turned 403/404 pages for logged-in users into HTTP 500.
            session.expunge_all()
            await session.rollback()
            raise


@asynccontextmanager
async def advisory_lock(key: int) -> AsyncIterator[None]:
    """Postgres session-level advisory lock: worker processes starting together create tables / seed data one at a time."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
        try:
            yield
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


async def init_db() -> None:
    """Create the pgvector extension and all tables (idempotent)."""
    import app.models  # noqa: F401  (register models)

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("database schema ready")
