from typing import AsyncIterator

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def connect_pool() -> None:
    global _pool
    if not settings.database_url:
        _pool = None
        return
    try:
        _pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            ssl="require",
            statement_cache_size=0,
        )
    except Exception:
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
