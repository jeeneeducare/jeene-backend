from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app import db
from app.routers import health


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await db.connect_pool()
    yield
    await db.disconnect_pool()


app = FastAPI(title="Jeene Backend", lifespan=lifespan)
app.include_router(health.router)
