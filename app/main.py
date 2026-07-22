from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from app import db
from app.auth import init_firebase
from app.routers import auth, content, health


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_firebase()
    await db.connect_pool()
    yield
    await db.disconnect_pool()


app = FastAPI(title="Jeene Backend", lifespan=lifespan)
app.include_router(health.router)
app.include_router(content.router)
app.include_router(auth.router)
