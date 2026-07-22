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
                ssl="require",
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
