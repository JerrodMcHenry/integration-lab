from fastapi import FastAPI, HTTPException

app = FastAPI(title="IntegrationLab Mock CRM")

# Simulated Salesforce account database
accounts = {
    "SF-1001": {
        "Id": "SF-1001",
        "Name": "Acme Capital",
        "Industry": "Financial Services",
        "AnnualRevenue": 2500000,
        "OwnerName": "Sarah Johnson"
    },
    "SF-1002": {
        "Id": "SF-1002",
        "Name": "Meridian Holdings",
        "Industry": "Investment Management",
        "AnnualRevenue": 7500000,
        "OwnerName": "Michael Chen"
    }
}


@app.get("/crm/accounts/{account_id}")
def get_crm_account(account_id: str):
    account = accounts.get(account_id)

    if account is None:
        raise HTTPException(
            status_code=404,
            detail="Account not found"
        )

    return account