import json

import firebase_admin
from fastapi import APIRouter, Response

from app import db
from app.config import settings
from app.schemas import HealthResponse

router = APIRouter()


@router.get("/health/firebase")
async def firebase_health() -> dict:
    """TEMPORARY diagnostic: reports whether the Firebase Admin credential loaded.
    Exposes only booleans, a length, and the (public) project_id, never the key."""
    raw = settings.firebase_service_account_json
    parses = False
    project = None
    if raw:
        try:
            project = json.loads(raw).get("project_id")
            parses = True
        except Exception:
            parses = False
    return {
        "sa_json_present": bool(raw),
        "sa_json_len": len(raw) if raw else 0,
        "sa_json_parses": parses,
        "sa_project_id": project,
        "gac_present": bool(settings.google_application_credentials),
        "firebase_initialized": bool(firebase_admin._apps),
    }


@router.get("/health", response_model=HealthResponse)
async def health(response: Response) -> HealthResponse:
    pool = db.get_pool_or_none()
    if pool is None:
        response.status_code = 503
        return HealthResponse(status="error", db="error")

    try:
        async with pool.acquire() as connection:
            await connection.fetchval("SELECT 1")
    except Exception:
        response.status_code = 503
        return HealthResponse(status="error", db="error")

    return HealthResponse(status="ok", db="ok")
