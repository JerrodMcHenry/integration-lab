from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.admin import router as admin_router
from app.api.deps import get_engine
from app.api.ops import router as ops_router
from app.api.sync import router as sync_router
from app.api.webhooks import router as webhooks_router
from app.auth.dependencies import require
from app.auth.permissions import Permission
from app.auth.tokens import Principal
from app.core.config import get_settings
from app.repositories.sync_history import RepositoryError, RepositoryUnavailableError
from app.schemas.account import AccountCreate

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
    unavailable = isinstance(exc, RepositoryUnavailableError)
    return JSONResponse(
        status_code=503 if unavailable else 500,
        content={"detail": "Sync history storage unavailable" if unavailable
                 else "Sync history storage error"},
    )


@app.get("/health/ready")
def readiness():
    """Readiness: the database (if configured) answers a trivial query."""
    engine = get_engine()
    if engine is None:
        return {"status": "ready", "database": "not_configured"}
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
    
    
@app.post("/accounts", status_code=201)
def create_account(account: AccountCreate,
                   _principal: Annotated[Principal, Depends(require(Permission.SYNC_RUN))]):
    """Legacy M1 intake endpoint (validates and echoes). Now requires sync:run."""
    return {
        "message": "Account recieved successfully",
        "account": account
    }
    
