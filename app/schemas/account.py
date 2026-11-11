from pydantic import BaseModel, Field

class AccountCreate(BaseModel):
    account_id: str = Field(min_length=1)
    account_name: str = Field(min_length=1)
    industry: str | None = None
    relationship_manager: str = Field(min_length=1)
    annual_revenue: float | None = Field(default=None, ge=0)