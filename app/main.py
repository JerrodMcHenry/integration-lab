
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.admin import router as admin_router
from app.api.ops import router as ops_router
from app.api.sync import router as sync_router
from app.api.webhooks import router as webhooks_router
from app.api.dependencies import get_engine
from app.core.config import get_settings
from app.db.errors import RepositoryError, RepositoryUnavailableError

# Fail fast on unsafe configuration (e.g. dev secrets in production).
_settings = get_settings()

app = FastAPI(
    title="IntegrationLab",
    version="0.7.0",
    # Interactive docs are a development convenience; not served in production.
    docs_url=None if _settings.is_production else "/docs",
    redoc_url=None if _settings.is_production else "/redoc",
    openapi_url=None if _settings.is_production else "/openapi.json",
)
app.include_router(sync_router)
app.include_router(admin_router)
app.include_router(webhooks_router)
app.include_router(ops_router)


@app.exception_handler(RepositoryError)
def repository_error_handler(_request: Request, exc: RepositoryError) -> JSONResponse:
    """Fallback for database errors a route didn't handle itself (any repository, not only history)."""
    if isinstance(exc, RepositoryUnavailableError):
        return JSONResponse(status_code=503, content={"detail": "database unavailable; retry later"})
    return JSONResponse(status_code=500, content={"detail": "database error"})


@app.get("/health/ready")
def readiness():
    """Readiness: the database answers a trivial query."""
    engine = get_engine()
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"status": "not_ready", "database": "unavailable"})
    return {"status": "ready", "database": "ok"}

@app.get("/health")
def health_check():
    return {
  "status": "healthy",
  "service": "integrationlab"
}
