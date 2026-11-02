import asyncio
import os
import re
import ssl
import time
from datetime import datetime
from html.parser import HTMLParser

import certifi
from aiohttp import ClientTimeout


_token = ""
_token_created_at = 0.0
_status_cache = {}
_inflight = {}
_TOKEN_TTL_SECONDS = 6 * 60 * 60
_ACTIVE_CACHE_SECONDS = 5 * 60
_TERMINAL_CACHE_SECONDS = 24 * 60 * 60
_TERMINAL_STATUSES = {"delivered", "returned", "return delivered", "cancelled"}
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


class _PublicTrackingParser(HTMLParser):
    def __init__(self, tracking_number):
        super().__init__()
        self.tracking_number = tracking_number
        self.in_tracking_table = False
        self.table_depth = 0
        self.in_row = False
        self.in_cell = False
        self.cell_text = []
        self.row = []
        self.events = []
        self.all_rows = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table":
            if self.in_tracking_table:
                self.table_depth += 1
            elif "tracking-table" in str(attributes.get("class") or "").split():
                self.in_tracking_table = True
                self.table_depth = 1
        if tag == "tr":
            self.in_row = True
            self.row = []
        elif tag in {"td", "th"} and self.in_row:
            self.in_cell = True
            self.cell_text = []

    def handle_data(self, data):
        if self.in_cell:
            self.cell_text.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.in_cell:
            self.row.append(" ".join("".join(self.cell_text).split()))
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if len(self.row) >= 2:
                self.all_rows.append(self.row[:2])
                if self.in_tracking_table and self.row[0].casefold() != "date/time" and self.row[1].casefold() != "status":
                    self.events.append({
                        "ConsignmentNo": self.tracking_number,
                        "TransactionDate": self.row[0],
                        "ProcessDescForPortal": _clean_status(self.row[1]),
                        "ReasonDesc": "",
                    })
            self.in_row = False
            self.row = []
        elif tag == "table" and self.in_tracking_table:
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_tracking_table = False

    def history(self):
        values = {}
        for label, value in self.all_rows:
            key = label.replace(":", "").strip().casefold()
            if key in {"destination", "consignee"}:
                values[key] = value.strip()
        history = list(reversed(self.events))
        for event in history:
            event["ConsigneeName"] = values.get("consignee", "")
            event["ConsigneeAddress"] = ""
            event["ContactNo"] = ""
            event["ConsigneeCity"] = values.get("destination", "")
        return history


def configuration():
    values = {
        "base_url": (os.getenv("DIGIDOKAAN_API_URL") or "https://digidokaan.pk").rstrip("/"),
        "phone": (os.getenv("DIGIDOKAAN_PHONE") or "").strip(),
        "password": os.getenv("DIGIDOKAAN_PASSWORD") or "",
        "gateway_id": str(os.getenv("DIGIDOKAAN_GATEWAY_ID") or "5").strip(),
    }
    if not values["phone"] or not values["password"]:
        return None
    return values


def _clean_status(value):
    return re.sub(r"^shipment\s*[-–—:]\s*", "", str(value or "").strip(), flags=re.IGNORECASE).strip()


def _event_timestamp(event):
    raw = str((event or {}).get("date_time") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    for pattern in ("%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, pattern).timestamp()
        except ValueError:
            continue
    return None


def _latest_tracking_event(events):
    valid = [event for event in (events or []) if isinstance(event, dict) and str(event.get("status") or "").strip()]
    if not valid:
        return None
    dated = [(timestamp, event) for event in valid if (timestamp := _event_timestamp(event)) is not None]
    return max(dated, key=lambda item: item[0])[1] if dated else valid[0]


def _display_status(detail_body, fallback=None):
    data = detail_body.get("data") if isinstance(detail_body, dict) else None
    tracking = data.get("tracking_response") if isinstance(data, dict) else None
    events = tracking.get("data") if isinstance(tracking, dict) else None
    latest = _latest_tracking_event(events)
    if latest:
        status = _clean_status(latest.get("status"))
        reason = str(latest.get("status_reason") or "").strip()
        if reason and any(marker in status.casefold() for marker in ("delivery unsuccessful", "reason validation", "shipper advise", "undelivered")):
            return f"Undelivered - {reason}"
        if status:
            return status
    current = _clean_status(tracking.get("courier_status")) if isinstance(tracking, dict) else ""
    return current or fallback


async def _access_token(session, config):
    global _token, _token_created_at
    if _token and time.monotonic() - _token_created_at < _TOKEN_TTL_SECONDS:
        return _token
    loop = asyncio.get_running_loop()
    lock = getattr(loop, "digidokaan_auth_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        loop.digidokaan_auth_lock = lock
    async with lock:
        if _token and time.monotonic() - _token_created_at < _TOKEN_TTL_SECONDS:
            return _token
        async with session.post(
            config["base_url"] + "/api/auth/login",
            json={"phone": config["phone"], "password": config["password"]},
            headers={"Accept": "application/json"},
            timeout=ClientTimeout(total=20),
            ssl=_SSL_CONTEXT,
        ) as response:
            payload = await response.json(content_type=None)
        token = str(payload.get("token") or "").strip() if isinstance(payload, dict) else ""
        if response.status != 200 or not token:
            raise RuntimeError("DigiDokaan authentication failed")
        _token = token
        _token_created_at = time.monotonic()
        return token


async def _authorized_post(session, config, path, payload):
    global _token, _token_created_at
    token = await _access_token(session, config)
    headers = {"Accept": "application/json", "Authorization": "Bearer " + token}
    async with session.post(
        config["base_url"] + path, json=payload, headers=headers,
        timeout=ClientTimeout(total=20), ssl=_SSL_CONTEXT,
    ) as response:
        body = await response.json(content_type=None)
        status = response.status
    if status == 401 or (isinstance(body, dict) and body.get("code") == 401):
        _token = ""
        _token_created_at = 0.0
        headers["Authorization"] = "Bearer " + await _access_token(session, config)
        async with session.post(
            config["base_url"] + path, json=payload, headers=headers,
            timeout=ClientTimeout(total=20), ssl=_SSL_CONTEXT,
        ) as response:
            body = await response.json(content_type=None)
            status = response.status
    return status, body


async def _order_detail(session, tracking_number, config):
    status, body = await _authorized_post(
        session, config, "/api/orders/search-courier-order",
        {"phone": config["phone"], "search_value": tracking_number, "gateway_id": config["gateway_id"]},
    )
    rows = body.get("data") if isinstance(body, dict) else None
    if status == 200 and (not isinstance(rows, list) or not rows):
        status, body = await _authorized_post(
            session, config, "/api/orders/search-courier-order",
            {"phone": config["phone"], "search_value": tracking_number},
        )
        rows = body.get("data") if isinstance(body, dict) else None
    if status != 200 or not isinstance(rows, list) or not rows:
        return None, None
    order = next(
        (row for row in rows if re.sub(r"\D", "", str(row.get("tracking_no") or "")) == tracking_number),
        rows[0],
    )
    order_id = str(order.get("order_id") or "").strip()
    if not order_id:
        return order, None
    detail_status, detail = await _authorized_post(
        session, config, "/api/seller/order/get_single_order_detail",
        {"phone": config["phone"], "order_no": order_id},
    )
    return order, detail if detail_status == 200 else None


async def _public_tracking_history(session, tracking_number):
    """Read DigiDokaan's customer-facing tracker when merchant search has no row."""
    try:
        async with session.get(
            "https://digidokaan.pk/real-time-tracking",
            params={"t_id": tracking_number},
            headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"},
            timeout=ClientTimeout(total=20),
            ssl=_SSL_CONTEXT,
        ) as response:
            page = await response.text()
        if response.status != 200:
            return []
        parser = _PublicTrackingParser(tracking_number)
        parser.feed(page)
        return parser.history()
    except Exception as error:
        print(f"DigiDokaan public tracking unavailable for {tracking_number}: {error}")
        return []


def _cached_status(tracking_number):
    cached = _status_cache.get(tracking_number)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    if cached:
        _status_cache.pop(tracking_number, None)
    return None


async def _fetch_status(session, tracking_number, config):
    order, detail = await _order_detail(session, tracking_number, config)
    if not order:
        history = await _public_tracking_history(session, tracking_number)
        return history[-1].get("ProcessDescForPortal") if history else None
    fallback = _clean_status(order.get("courier_status") or order.get("status")) or None
    return _display_status(detail, fallback) if detail else fallback


async def fetch_tracking_status(session, tracking_number):
    """Return the latest DigiDokaan status, deduplicating concurrent lookups."""
    normalized = re.sub(r"\D", "", str(tracking_number or ""))
    if not normalized:
        return None
    cached = _cached_status(normalized)
    if cached:
        return cached
    config = configuration()
    if not config:
        history = await _public_tracking_history(session, normalized)
        return history[-1].get("ProcessDescForPortal") if history else None
    task = _inflight.get(normalized)
    if task is None:
        task = asyncio.create_task(_fetch_status(session, normalized, config))
        _inflight[normalized] = task
    try:
        status = await task
        if status:
            ttl = _TERMINAL_CACHE_SECONDS if status.casefold() in _TERMINAL_STATUSES else _ACTIVE_CACHE_SECONDS
            _status_cache[normalized] = (status, time.monotonic() + ttl)
        return status
    except Exception as error:
        print(f"DigiDokaan tracking unavailable for {normalized}: {error}")
        return None
    finally:
        if _inflight.get(normalized) is task:
            _inflight.pop(normalized, None)


async def fetch_tracking_history(session, tracking_number):
    """Return DigiDokaan events in the portal's existing history shape."""
    normalized = re.sub(r"\D", "", str(tracking_number or ""))
    config = configuration()
    if not normalized:
        return []
    if not config:
        return await _public_tracking_history(session, normalized)
    try:
        order, detail_body = await _order_detail(session, normalized, config)
        data = detail_body.get("data") if isinstance(detail_body, dict) else None
        detail = data.get("order_detail") if isinstance(data, dict) else None
        tracking = data.get("tracking_response") if isinstance(data, dict) else None
        events = tracking.get("data") if isinstance(tracking, dict) else None
        if not isinstance(detail, dict) or not isinstance(events, list):
            return await _public_tracking_history(session, normalized)
        history = []
        for event in reversed(events):
            if not isinstance(event, dict) or not str(event.get("status") or "").strip():
                continue
            history.append({
                "ConsignmentNo": normalized,
                "TransactionDate": event.get("date_time") or "",
                "ProcessDescForPortal": _clean_status(event.get("status")),
                "ReasonDesc": event.get("status_reason") or "",
                "ConsigneeName": detail.get("customer_name") or "",
                "ConsigneeAddress": detail.get("customer_address") or detail.get("address") or "",
                "ContactNo": detail.get("customer_phone") or detail.get("phone") or "",
                "ConsigneeCity": detail.get("customer_city") or "",
            })
        return history
    except Exception as error:
        print(f"DigiDokaan tracking history unavailable for {normalized}: {error}")
        return await _public_tracking_history(session, normalized)
