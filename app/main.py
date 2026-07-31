import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import db
from app.auth import init_firebase
from app.routers import attempts, auth, content, health, tests


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_firebase()
    await db.connect_pool()
    yield
    await db.disconnect_pool()


app = FastAPI(title="Jeene Backend", lifespan=lifespan)

# The web test client is served from Firebase Hosting, a different origin, and static
# hosting cannot proxy to this API the way the dev server does — so the browser needs
# CORS. Kept to an explicit list rather than "*": these endpoints accept credentials,
# and a wildcard would let any site drive a sitting on a student's behalf.
_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "JEENE_WEB_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Test-Token"],
)
app.include_router(health.router)
app.include_router(content.router)
app.include_router(auth.router)
app.include_router(attempts.router)
app.include_router(tests.router)
