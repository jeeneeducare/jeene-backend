import asyncio
import json
import logging
from typing import AsyncIterator

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def _init_connection(connection: asyncpg.Connection) -> None:
    await connection.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )


def _ssl_mode(dsn: str):
    """TLS is required unless the connection string explicitly says otherwise.

    Supabase requires it and the default must stay `require`, so that forgetting to
    configure anything cannot quietly downgrade a production connection.

    But it was hardcoded, which meant the app could not connect to a database without
    TLS — including every local Postgres — and so nothing in this repo could be run
    against a real database at all. That is not a small thing: it is why the integration
    tests only ever ran against Supabase, and why four tickets of Jeene Mode were written
    without a single query being executed.

    So a DSN that carries its own `sslmode` is believed. Saying `?sslmode=disable` is a
    deliberate act, it is visible in the connection string, and nobody types it into a
    Render environment variable by accident.
    """
    if dsn and "sslmode=" in dsn:
        return None  # asyncpg reads sslmode from the DSN when ssl is not given
    return "require"


async def connect_pool() -> None:
    global _pool
    if not settings.database_url:
        logger.error("DATABASE_URL is not set; the connection pool will not be created")
        _pool = None
        return
    # Keep min_size small so a fresh instance only needs one connection to come up
    # (the Supabase pooler has a limited client budget); retry so a transient
    # saturation at startup self-heals instead of leaving the instance DB-down.
    last_exc: Exception | None = None
    for attempt in range(1, 6):
        try:
            _pool = await asyncpg.create_pool(
                dsn=settings.database_url,
                ssl=_ssl_mode(settings.database_url),
                statement_cache_size=0,
                min_size=1,
                max_size=5,
                command_timeout=30,
                init=_init_connection,
            )
            logger.info("Database pool connected (attempt %d)", attempt)
            return
        except Exception as exc:
            last_exc = exc
            logger.warning("DB pool connect attempt %d failed: %s", attempt, exc)
            await asyncio.sleep(min(2 * attempt, 8))
    logger.error("Failed to connect the database pool after retries", exc_info=last_exc)
    _pool = None


async def disconnect_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Connection pool is not initialized")
    return _pool


def get_pool_or_none() -> asyncpg.Pool | None:
    return _pool


async def get_connection() -> AsyncIterator[asyncpg.Connection]:
    async with get_pool().acquire() as connection:
        yield connection
