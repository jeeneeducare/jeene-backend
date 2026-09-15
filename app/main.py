import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import db, refusals
from app.auth import init_firebase
from app.routers import (
    admin,
    billing,
    attempts,
    auth,
    content,
    doubts,
    health,
    internal,
    mistakes,
    notes,
    plans,
    reports,
    tests,
)


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
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Test-Token"],
    # Without this a browser hands script only the CORS-safelisted response headers, so
    # a web client could not read a refusal's audience and would fall back to its own
    # line for the status — the exact bug these headers exist to fix, reappearing on one
    # platform only. The native apps are not subject to CORS and would have hidden it.
    expose_headers=[refusals.AUDIENCE_HEADER, refusals.ACTION_HEADER],
)
app.include_router(health.router)
app.include_router(content.router)
app.include_router(notes.router)
app.include_router(auth.router)
app.include_router(attempts.router)
app.include_router(tests.router)
app.include_router(reports.router)
app.include_router(mistakes.router)
app.include_router(plans.router)
app.include_router(doubts.router)
app.include_router(admin.router)
app.include_router(billing.router)
app.include_router(internal.router)
