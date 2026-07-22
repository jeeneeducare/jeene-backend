import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.auth import require_user
from app.db import get_connection
from app.schemas import ProfileUpdate, SessionRequest, UserProfile

router = APIRouter()

_PROFILE_COLUMNS = (
    "firebase_uid, tenant_id, display_name, email, phone, "
    "class_level, target_exam, auth_provider"
)


def _to_profile(row: asyncpg.Record, is_new: bool = False) -> UserProfile:
    return UserProfile(
        firebase_uid=row["firebase_uid"],
        tenant_id=row["tenant_id"],
        display_name=row["display_name"],
        email=row["email"],
        phone=row["phone"],
        class_level=row["class_level"],
        target_exam=row["target_exam"],
        auth_provider=row["auth_provider"],
        is_new=is_new,
    )


@router.post("/auth/session", response_model=UserProfile)
async def create_session(
    body: SessionRequest,
    user: dict = Depends(require_user),
    connection: asyncpg.Connection = Depends(get_connection),
) -> UserProfile:
    """Establish/refresh the user record after a Firebase sign-in. Idempotent:
    creates the row on first login, otherwise updates identity + last_login.
    Tenant is never set from the client; it defaults to JEENE_MASTER and is
    assigned server-side (coaching provisioning does this later)."""
    provider = (user.get("firebase") or {}).get("sign_in_provider")
    row = await connection.fetchrow(
        f"""
        INSERT INTO users (firebase_uid, email, phone, auth_provider,
                           display_name, class_level, target_exam, last_login_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, now())
        ON CONFLICT (firebase_uid) DO UPDATE SET
            email         = COALESCE(EXCLUDED.email, users.email),
            phone         = COALESCE(EXCLUDED.phone, users.phone),
            auth_provider = COALESCE(EXCLUDED.auth_provider, users.auth_provider),
            display_name  = COALESCE(EXCLUDED.display_name, users.display_name),
            class_level   = COALESCE(EXCLUDED.class_level, users.class_level),
            target_exam   = COALESCE(EXCLUDED.target_exam, users.target_exam),
            last_login_at = now(),
            updated_at    = now()
        RETURNING {_PROFILE_COLUMNS}, (xmax = 0) AS is_new
        """,
        user["uid"],
        user.get("email"),
        user.get("phone_number"),
        provider,
        body.display_name,
        body.class_level,
        body.target_exam,
    )
    return _to_profile(row, is_new=row["is_new"])


@router.get("/auth/me", response_model=UserProfile)
async def get_me(
    user: dict = Depends(require_user),
    connection: asyncpg.Connection = Depends(get_connection),
) -> UserProfile:
    row = await connection.fetchrow(
        f"SELECT {_PROFILE_COLUMNS} FROM users WHERE firebase_uid = $1", user["uid"]
    )
    if row is None:
        raise HTTPException(status_code=404, detail="User has no session yet; call /auth/session first")
    return _to_profile(row)


@router.patch("/auth/me", response_model=UserProfile)
async def update_me(
    body: ProfileUpdate,
    user: dict = Depends(require_user),
    connection: asyncpg.Connection = Depends(get_connection),
) -> UserProfile:
    row = await connection.fetchrow(
        f"""
        UPDATE users SET
            display_name = COALESCE($2, display_name),
            class_level  = COALESCE($3, class_level),
            target_exam  = COALESCE($4, target_exam),
            updated_at   = now()
        WHERE firebase_uid = $1
        RETURNING {_PROFILE_COLUMNS}
        """,
        user["uid"],
        body.display_name,
        body.class_level,
        body.target_exam,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="User has no session yet; call /auth/session first")
    return _to_profile(row)
