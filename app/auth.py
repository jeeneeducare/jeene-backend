"""Firebase Auth: Admin SDK initialisation and the request dependencies.

Identity comes from Firebase (the app sends a Firebase ID token as a Bearer
header). This module verifies that token and resolves the caller's tenant. User
profile data lives in Postgres (the `users` table), keyed by the Firebase UID.
"""

import asyncio
import json
import logging
from typing import Optional

import asyncpg
import firebase_admin
from fastapi import Depends, Header, HTTPException, status
from firebase_admin import auth as fb_auth, credentials

from app.config import settings
from app.db import get_connection

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "JEENE_MASTER"


def init_firebase() -> None:
    """Initialise the Admin SDK once, at startup. No-op if already initialised
    or if no credential is configured (verification then fails closed)."""
    if firebase_admin._apps:
        return
    cred = None
    if settings.firebase_service_account_json:
        try:
            cred = credentials.Certificate(json.loads(settings.firebase_service_account_json))
        except Exception:
            logger.exception("Failed to parse FIREBASE_SERVICE_ACCOUNT_JSON")
    elif settings.google_application_credentials:
        try:
            cred = credentials.Certificate(settings.google_application_credentials)
        except Exception:
            logger.exception("Failed to load GOOGLE_APPLICATION_CREDENTIALS file")
    if cred is None:
        logger.error("No Firebase credential configured; token verification will fail")
        return
    firebase_admin.initialize_app(cred)


async def _decode_bearer(authorization: Optional[str]) -> Optional[dict]:
    """Return the decoded Firebase token, or None if absent/invalid. Never raises."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        # verify_id_token is blocking (and may fetch/cache Google's public keys),
        # so keep it off the event loop.
        return await asyncio.to_thread(fb_auth.verify_id_token, token)
    except Exception:
        logger.info("Rejected an invalid Firebase ID token", exc_info=False)
        return None


async def require_user(authorization: Optional[str] = Header(default=None)) -> dict:
    """Dependency for endpoints that require a signed-in user. 401 if missing/invalid."""
    decoded = await _decode_bearer(authorization)
    if decoded is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return decoded


async def optional_user(authorization: Optional[str] = Header(default=None)) -> Optional[dict]:
    """Dependency for auth-optional endpoints. Returns the decoded token or None."""
    return await _decode_bearer(authorization)


async def current_tenant(
    user: Optional[dict] = Depends(optional_user),
    connection: asyncpg.Connection = Depends(get_connection),
) -> str:
    """Resolve the tenant to scope content to: the signed-in user's tenant, or
    JEENE_MASTER for anonymous callers (and users not yet in the DB)."""
    if user is None:
        return DEFAULT_TENANT
    tenant = await connection.fetchval(
        "SELECT tenant_id FROM users WHERE firebase_uid = $1", user["uid"]
    )
    return tenant or DEFAULT_TENANT
