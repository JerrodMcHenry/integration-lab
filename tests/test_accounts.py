import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(auth):
    # Since M5 the legacy intake endpoint requires an authenticated caller with sync:run.
    return TestClient(app, headers=auth.headers("operator"))


def test_create_account_success(client):
    payload = {
        "account_id": "SF-1001",
        "account_name": "Acme Capital",
        "industry": "Financial Services",
        "relationship_manager": "Sarah Johnson",
        "annual_revenue": 2500000
    }

    response = client.post("/accounts", json=payload)

    assert response.status_code == 201
    assert response.json()["account"]["account_id"] == "SF-1001"
    assert response.json()["account"]["annual_revenue"] == 2500000


def test_create_account_negative_revenue(client):
    payload = {
        "account_id": "SF-1002",
        "account_name": "Bad Revenue Account",
        "relationship_manager": "Sarah Johnson",
        "annual_revenue": -100
    }

    response = client.post("/accounts", json=payload)

    assert response.status_code == 422