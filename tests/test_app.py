import os

os.environ.setdefault("APP_SECRET_KEY", "test-secret")
os.environ.setdefault("AABROO_PORTAL_PASSWORD", "test-password")

import app


def test_health_check():
    response = app.app.test_client().get("/health")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_portal_requires_password():
    client = app.app.test_client()
    assert b"Portal password" in client.get("/").data
    assert client.post("/", data={"password": "wrong"}).status_code == 401


def test_login_opens_portal(monkeypatch):
    monkeypatch.setattr(app, "portal_data", lambda: {
        "orders": [], "fulfilled_orders": [], "unfulfilled_orders": [], "closed_orders": [],
        "summary": {"fulfilled_orders": 0, "unfulfilled_orders": 0, "closed_orders": 0,
                    "delivered_orders": 0, "returned_orders": 0, "total_cost": 0,
                    "total_cod": 0, "total_cash_paid": 0, "balance": 0,
                    "net_payment_received_entries": []}, "error_message": "",
    })
    client = app.app.test_client()
    assert client.post("/", data={"password": "test-password"}).status_code == 302
    assert b"Aabroo Orders" in client.get("/").data
