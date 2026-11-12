from fastapi import FastAPI

from app.api.sync import router as sync_router
from app.core.config import get_settings
from app.schemas.account import AccountCreate

# Fail fast on unsafe configuration (e.g. dev secrets in production).
get_settings()

app = FastAPI(title="IntegrationLab", version="0.3.0")
app.include_router(sync_router)

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
    
