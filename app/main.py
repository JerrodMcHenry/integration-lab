from fastapi import FastAPI
from app.schemas.account import AccountCreate

app = FastAPI()

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