from fastapi import APIRouter, Response

from app import db
from app.schemas import HealthResponse

router = APIRouter()


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
