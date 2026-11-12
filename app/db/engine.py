"""SQLAlchemy engine construction."""

from __future__ import annotations

from sqlalchemy import Engine, create_engine

from app.core.config import DatabaseSettings


def build_engine(settings: DatabaseSettings, *, application_name: str = "integrationlab") -> Engine:
    if not settings.url:
        raise ValueError("DATABASE_URL is not configured")
    return create_engine(
        settings.url,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_timeout=settings.pool_timeout_seconds,
        # Detect connections killed by a DB restart/failover before using them.
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            "connect_timeout": settings.connect_timeout_seconds,
            "application_name": application_name,
        },
        # Never log SQL parameters (they may contain identifiers/error text).
        echo=False,
        hide_parameters=True,
    )
