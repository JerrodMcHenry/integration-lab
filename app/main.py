from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import get_engine
from app.api.sync import router as sync_router
from app.core.config import get_settings
from app.repositories.sync_history import RepositoryError, RepositoryUnavailableError
from app.schemas.account import AccountCreate

# Fail fast on unsafe configuration (e.g. dev secrets in production).
get_settings()

app = FastAPI(title="IntegrationLab", version="0.3.0")
app.include_router(sync_router)


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
def create_account(account: AccountCreate):
    return {
        "message": "Account recieved successfully",
        "account": account
    }
    
