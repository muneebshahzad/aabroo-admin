import os

os.environ.setdefault("INITIALIZE_APP", "false")

import main


def _checkout_node():
    return {
        "id": "gid://shopify/AbandonedCheckout/1",
        "name": "#123456",
        "abandonedCheckoutUrl": "https://aabroo.store/checkouts/recover/123",
        "completedAt": None,
        "createdAt": "2026-11-03T10:00:00+05:00",
        "updatedAt": "2026-11-03T10:05:00+05:00",
        "customer": {
            "firstName": "Aabroo",
            "lastName": "Customer",
            "numberOfOrders": "2",
            "defaultEmailAddress": {"emailAddress": "customer@example.com"},
            "defaultPhoneNumber": {"phoneNumber": "03001234567"},
        },
        "shippingAddress": {"name": "Aabroo Customer", "city": "Lahore", "country": "Pakistan", "phone": "03001234567"},
        "subtotalPriceSet": {"shopMoney": {"amount": "2500", "currencyCode": "PKR"}},
        "totalPriceSet": {"shopMoney": {"amount": "2700", "currencyCode": "PKR"}},
        "lineItems": {"nodes": [{
            "title": "Stole",
            "variantTitle": "Blue",
            "quantity": 1,
            "image": {"url": "https://cdn.example/stole.jpg"},
            "discountedUnitPriceSet": {"shopMoney": {"amount": "2500", "currencyCode": "PKR"}},
        }]},
    }


def test_builds_aabroo_abandoned_checkout_cards(monkeypatch):
    monkeypatch.setattr(main, "fetch_shopify_abandoned_checkouts", lambda days=7, force=False: [_checkout_node()])
    monkeypatch.setattr(main, "load_abandoned_viewed_tokens", lambda: set())

    rows, summary = main.build_abandoned_checkouts_data()

    assert rows[0]["customer_name"] == "Aabroo Customer"
    assert rows[0]["customer_phone"] == "923001234567"
    assert rows[0]["items"][0]["title"] == "Stole - Blue"
    assert rows[0]["display_shipping"] == "PKR 200"
    assert summary["open"] == 1
    assert summary["not_viewed"] == 1


def test_abandoned_page_and_mark_viewed(monkeypatch):
    monkeypatch.setattr(main, "fetch_shopify_abandoned_checkouts", lambda days=7, force=False: [_checkout_node()])
    monkeypatch.setattr(main, "load_abandoned_viewed_tokens", lambda: set())
    saved = []
    monkeypatch.setattr(main, "save_abandoned_viewed_tokens", lambda tokens: saved.append(tokens) or True)
    client = main.app.test_client()

    page = client.get("/abandoned")
    marked = client.post("/abandoned/mark-viewed", json={"token": "#123456"})

    assert page.status_code == 200
    assert b"Abandoned Checkouts" in page.data
    assert b"Stole - Blue" in page.data
    assert marked.status_code == 200
    assert saved == [{"#123456"}]
