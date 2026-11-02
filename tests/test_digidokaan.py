import asyncio
import os
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import digidokaan


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, content_type=None):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse(*next(self.responses))


class DigiDokaanTrackingTests(IsolatedAsyncioTestCase):
    def setUp(self):
        digidokaan._status_cache.clear()
        digidokaan._inflight.clear()
        digidokaan._shipper_advice_cache = None
        digidokaan._shipper_advice_cache_expires_at = 0.0

    def test_requires_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(digidokaan.configuration())

    def test_normalizes_local_pakistani_phone_for_api_login(self):
        with patch.dict(os.environ, {"DIGIDOKAAN_PHONE": "03211180807", "DIGIDOKAAN_PASSWORD": "0320"}, clear=True):
            self.assertEqual(digidokaan.configuration()["phone"], "923211180807")

    async def test_concurrent_requests_are_deduplicated_and_cached(self):
        async def delayed_status(*_args):
            await asyncio.sleep(0)
            return "Delivered"

        environment = {"DIGIDOKAAN_PHONE": "923000000000", "DIGIDOKAAN_PASSWORD": "secret"}
        with patch.dict(os.environ, environment, clear=True), patch.object(
            digidokaan, "_fetch_status", AsyncMock(side_effect=delayed_status)
        ) as fetch:
            statuses = await asyncio.gather(
                digidokaan.fetch_tracking_status(object(), "223 22367960798"),
                digidokaan.fetch_tracking_status(object(), "22322367960798"),
            )
            cached = await digidokaan.fetch_tracking_status(object(), "22322367960798")

        self.assertEqual(statuses, ["Delivered", "Delivered"])
        self.assertEqual(cached, "Delivered")
        fetch.assert_awaited_once()

    async def test_detail_status_wins_over_stale_search_status(self):
        config = {
            "base_url": "https://digidokaan.example",
            "phone": "923000000000",
            "password": "secret",
            "gateway_id": "5",
        }
        session = FakeSession([
            ({"data": [{"tracking_no": "22315868148789", "order_id": "PK2924A01", "courier_status": "Delivery In Transit"}]},),
            ({"data": {"tracking_response": {"data": [
                {"status": "Shipment - Delivery Unsuccessful", "status_reason": "Customer unavailable", "date_time": "26/09/2026 02:13 PM"},
                {"status": "Shipment - Out for Delivery", "date_time": "26/09/2026 11:28 AM"},
            ]}}},),
        ])
        with patch.object(digidokaan, "_access_token", AsyncMock(return_value="token")):
            status = await digidokaan._fetch_status(session, "22315868148789", config)

        self.assertEqual(status, "Undelivered - Customer unavailable")

    def test_public_tracker_parser_returns_oldest_to_newest_history(self):
        parser = digidokaan._PublicTrackingParser("22317469504278")
        parser.feed("""
            <table><tr><td><b>Destination :</b></td><td>Islamabad</td></tr>
            <tr><td><b>Consignee :</b></td><td>Umair Nasir</td></tr></table>
            <table class="ChildGrid tracking-table"><tr><th>Date/Time</th><th>Status</th></tr>
            <tr><td>26-10-2026 12:13:00</td><td>Shipment - In Transit</td></tr>
            <tr><td>24-10-2026 07:15:00</td><td>Shipment - Booked</td></tr></table>
        """)
        history = parser.history()

        self.assertEqual([event["ProcessDescForPortal"] for event in history], ["Booked", "In Transit"])
        self.assertEqual(history[-1]["ConsigneeName"], "Umair Nasir")
        self.assertEqual(history[-1]["ConsigneeCity"], "Islamabad")

    def test_sonic_public_payload_is_mapped_to_portal_history(self):
        history = digidokaan._sonic_payload_history({"shipments": {"11411576": {
            "tracking_number": "22320269889521",
            "consignee": {"name": "Customer", "destination": "Karachi"},
            "tracking_history": [{"date_time": "2026-11-02 18:11:24", "status": "Shipment - Booked"}],
        }}}, "22320269889521")

        self.assertEqual(history[-1]["ProcessDescForPortal"], "Booked")
        self.assertEqual(history[-1]["TransactionDate"], "2026-11-02 18:11:24")
        self.assertEqual(history[-1]["ConsigneeCity"], "Karachi")

    def test_public_tracking_wins_only_when_it_has_a_newer_courier_scan(self):
        account_history = [
            {"TransactionDate": "25/10/2026 10:00:00", "ProcessDescForPortal": "In Transit"},
        ]
        newer_public_history = [
            {"TransactionDate": "26-10-2026 12:13:00", "ProcessDescForPortal": "Delivered"},
        ]
        older_public_history = [
            {"TransactionDate": "24-10-2026 07:15:00", "ProcessDescForPortal": "Booked"},
        ]

        self.assertIs(
            digidokaan._prefer_fresher_history(account_history, newer_public_history),
            newer_public_history,
        )
        self.assertIs(
            digidokaan._prefer_fresher_history(account_history, older_public_history),
            account_history,
        )

    async def test_status_uses_newer_public_scan_from_another_merchant_account(self):
        config = {"base_url": "https://digidokaan.example", "phone": "923000000000", "password": "secret", "gateway_id": "5"}
        account = ({"courier_status": "In Transit"}, {"data": {"tracking_response": {"data": [
            {"status": "Shipment - In Transit", "date_time": "25/10/2026 10:00:00"},
        ]}}})
        public = [{"TransactionDate": "26-10-2026 12:13:00", "ProcessDescForPortal": "Delivered"}]
        with patch.object(digidokaan, "_order_detail", AsyncMock(return_value=account)), patch.object(
            digidokaan, "_combined_public_tracking_history", AsyncMock(return_value=public)
        ):
            status = await digidokaan._fetch_status(object(), "22317469504278", config)

        self.assertEqual(status, "Delivered")

    async def test_fetches_and_caches_pending_shipper_advice(self):
        config = {"base_url": "https://digidokaan.example", "phone": "923000000000", "password": "secret", "gateway_id": "5"}
        rows = [{"tracking_no": "22320269889521", "gateway_id": 5}]
        session = FakeSession([({"data": rows},)])
        with patch.object(digidokaan, "configuration", return_value=config), patch.object(
            digidokaan, "_access_token", AsyncMock(return_value="token")
        ):
            first = await digidokaan.fetch_pending_shipper_advice(session)
            second = await digidokaan.fetch_pending_shipper_advice(session)

        self.assertEqual(first, rows)
        self.assertIs(second, first)
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.calls[0][0][0].endswith("/api/courier/get_shipper_advice_order"))

    async def test_no_pending_shipper_advice_is_a_valid_empty_queue(self):
        config = {"base_url": "https://digidokaan.example", "phone": "923000000000", "password": "secret", "gateway_id": "5"}
        session = FakeSession([({"code": 404, "error": "No Order Found"},)])
        with patch.object(digidokaan, "configuration", return_value=config), patch.object(
            digidokaan, "_access_token", AsyncMock(return_value="token")
        ):
            rows = await digidokaan.fetch_pending_shipper_advice(session)

        self.assertEqual(rows, [])

    async def test_submits_shipper_advice_and_invalidates_queue_cache(self):
        config = {"base_url": "https://digidokaan.example", "phone": "923000000000", "password": "secret", "gateway_id": "5"}
        session = FakeSession([({"code": 200, "message": "Advice posted"},)])
        digidokaan._shipper_advice_cache = [{"tracking_no": "22320269889521"}]
        with patch.object(digidokaan, "configuration", return_value=config), patch.object(
            digidokaan, "_access_token", AsyncMock(return_value="token")
        ):
            result = await digidokaan.submit_shipper_advice(
                session, "223 20269889521", "8", "reattempt", "Please try tomorrow"
            )

        payload = session.calls[0][1]["json"]
        self.assertEqual(result["code"], 200)
        self.assertEqual(payload["tracking_no"], "22320269889521")
        self.assertEqual(payload["gateway_id"], "8")
        self.assertEqual(payload["shipper_advice_status"], "reattempt")
        self.assertIsNone(digidokaan._shipper_advice_cache)


def test_aabroo_routes_digidokaan_tracking(monkeypatch):
    monkeypatch.setenv("INITIALIZE_APP", "false")
    import main

    assert main.is_digidokaan_tracking("22322367960798")
    assert main.is_digidokaan_tracking("22320269504284")
    assert main.courier_label_for_tracking("", "22322367960798") == "DigiDokaan"
    assert not main.is_digidokaan_tracking("LE123456")
    assert main.summarize_tracking_result("22317469504278", [])["status"] == "Tracking unavailable"


def test_shipper_advice_is_enriched_with_matching_shopify_order(monkeypatch):
    monkeypatch.setenv("INITIALIZE_APP", "false")
    import main

    result = main.enrich_shipper_advice_orders(
        [{"tracking_no": "22320269889521", "external_reference_no": "1020"}],
        [{
            "order_num": "1020",
            "total_price": "4646.00",
            "line_items": [{
                "tracking_number": "22320269889521",
                "product_title": "Cozy Ultra Soft Stole",
                "quantity": 3,
                "image_src": "https://example.test/stole.jpg",
            }],
        }],
    )

    assert result[0]["shopify_order"]["display_total"] == "PKR 4,646"
    assert result[0]["shopify_order"]["items"][0]["title"] == "Cozy Ultra Soft Stole"
