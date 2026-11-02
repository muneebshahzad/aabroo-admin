import asyncio
import base64
import hashlib
import hmac
import json
import os
import smtplib
import sys
import threading
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from urllib.parse import urlparse

import aiohttp
import requests
import shopify
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from markupsafe import Markup

from db import (
    create_account_transaction,
    delete_order_status,
    get_accounts_page_data,
    get_app_setting,
    init_db,
    load_order_statuses,
    set_app_setting,
    upsert_order_status,
)
from shopify_protected_data import (
    create_oauth_state,
    exchange_oauth_code_for_token,
    fetch_protected_order_details,
    get_graphql_api_version,
    get_graphql_endpoint,
    get_graphql_token,
    get_install_url,
    get_protected_data_config_status,
    get_shop_domain,
    save_offline_token,
    verify_oauth_hmac,
)
from digidokaan import (
    fetch_tracking_history as fetch_digidokaan_tracking_history,
    fetch_tracking_status as fetch_digidokaan_tracking_status,
)


app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET_KEY", "default_secret_key")


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "aabroo-admin"})

EMPLOYEE_PORTAL_SESSION_KEY = "employee_portal_authenticated"
ADMIN_PORTAL_SESSION_KEY = "admin_portal_authenticated"
SHOPIFY_OAUTH_STATE_SESSION_KEY = "shopify_oauth_state"
EMPLOYEE_PORTAL_PASSWORD = os.getenv("EMPLOYEE_PORTAL_PASSWORD", "@@@t")
ADMIN_PORTAL_PASSWORD = os.getenv("ADMIN_PORTAL_PASSWORD", "security")

order_details = []
product_image_cache = {}
tracking_summary_cache = {}
tracking_refresh_lock = threading.Lock()
postex_payment_status_cache = {}
postex_payment_status_cache_loaded = False
RATE_LIMIT = 2
LAST_REQUEST_TIME = 0.0
PRODUCT_COSTS_SETTING_KEY = "product_cost_overrides_v1"
POSTEX_PAYMENT_STATUS_CACHE_SETTING_KEY = "postex_payment_status_cache_v1"
inventory_item_cost_cache = {}
TRACKING_REFRESH_SYNC_LIMIT = int(os.getenv("TRACKING_REFRESH_SYNC_LIMIT", "24"))
TRACKING_REFRESH_BACKGROUND_LIMIT = int(os.getenv("TRACKING_REFRESH_BACKGROUND_LIMIT", "250"))
TRACKING_REFRESH_FRESH_SECONDS = int(os.getenv("TRACKING_REFRESH_FRESH_SECONDS", "1800"))
TRACKING_REFRESH_SYNC_DEADLINE_SECONDS = float(os.getenv("TRACKING_REFRESH_SYNC_DEADLINE_SECONDS", "16"))
TRACKING_REFRESH_BACKGROUND_DEADLINE_SECONDS = float(os.getenv("TRACKING_REFRESH_BACKGROUND_DEADLINE_SECONDS", "90"))
TRACKING_REFRESH_PER_SHIPMENT_TIMEOUT_SECONDS = float(os.getenv("TRACKING_REFRESH_PER_SHIPMENT_TIMEOUT_SECONDS", "8"))

_TAG_STYLES = {
    "Call Courier": "background:#ede7f6;color:#4527a0",
    "Leopards": "background:#e6f6f8;color:#0a5c6e",
    "Order Confirmed": "background:#e8f5e9;color:#1b5e20",
    "Fulfilment Not Set": "background:#fff8e1;color:#e65100",
    "No Throw": "background:#fce4ec;color:#880e4f",
    "Lahore": "background:#fff3cd;color:#8b5a00",
}


def is_leopards_tracking(tracking_number):
    return str(tracking_number or "").strip().upper().startswith("LE")


def is_digidokaan_tracking(tracking_number, courier_name=""):
    normalized = "".join(character for character in str(tracking_number or "") if character.isdigit())
    courier = str(courier_name or "").casefold().replace(" ", "").replace("-", "")
    return (
        (len(normalized) in {14, 15} and normalized.startswith("223"))
        or (bool(normalized) and "digidokaan" in courier)
    )


def is_postex_courier(courier_name=""):
    normalized = str(courier_name or "").strip().lower().replace(" ", "").replace("-", "")
    return "postex" in normalized


def is_postex_tracking(tracking_number="", courier_name=""):
    tracking = str(tracking_number or "").strip().upper()
    if not tracking or tracking == "N/A":
        return False
    if is_postex_courier(courier_name):
        return True
    return tracking.startswith("CX") or (tracking.isdigit() and tracking.startswith("2") and len(tracking) >= 10)


def courier_label_for_tracking(courier_name="", tracking_number=""):
    if is_digidokaan_tracking(tracking_number, courier_name):
        return "DigiDokaan"
    if is_leopards_tracking(tracking_number):
        return "Leopards"
    if is_postex_courier(courier_name) or is_postex_tracking(tracking_number):
        return "PostEx"
    return str(courier_name or "").strip()


def normalize_scan_term(term):
    return (term or "").strip().lower().replace("#", "")


def clean_customer_value(value):
    text = str(value or "").strip()
    if not text or text.upper() in {"-", "N/A", "NULL", "NONE"}:
        return ""
    return text


def is_lahore_city(city):
    normalized = (city or "").strip().lower()
    return "lahore" in normalized or "lhr" in normalized


def is_undelivered_status(status):
    normalized = (status or "").strip().upper()
    if not normalized:
        return False
    delivered_like = {"DELIVERED", "BOOKED", "UN-BOOKED", "UN-FULFILLED", "UNFULFILLED", "CANCELLED"}
    if normalized in delivered_like:
        return False
    return any(
        keyword in normalized
        for keyword in ("OUT FOR", "RETURN", "REFUS", "CALL NOT", "HOLD", "UNTRACEABLE", "ATTEMPT", "DELAY")
    ) or "UNDELIVERED" in normalized


def is_delivered_status(status):
    normalized = (status or "").strip().upper()
    return normalized == "DELIVERED" or normalized.startswith("DELIVERED ")


def normalize_status_bucket(status):
    raw = (status or "Un-Booked").strip()
    upper = raw.upper()
    if "PARTIALLY DELIVERED" in upper:
        return "Partially Delivered"
    if "RETURNED TO SHIPPER" in upper:
        return "RETURNED TO SHIPPER"
    if "BEING RETURN" in upper or "OUT FOR RETURN" in upper or "RETURN SUBMISSION" in upper:
        return "Being Return"
    if "UNDELIVERED" in upper:
        return "Undelivered"
    if "OUT FOR DELIVERY" in upper:
        return "Out For Delivery"
    if is_delivered_status(raw):
        return "Delivered"
    if "PICKED FROM SHIPPER" in upper:
        return "Picked From Shipper"
    if upper == "BOOKED" or "CONSIGNMENT BOOKED" in upper:
        return "Booked"
    if upper in {"UN-BOOKED", "UNBOOKED"}:
        return "Un-Booked"
    return raw


def should_keep_order_in_active_list(order_info, order=None):
    if not order_info:
        return False
    if order is not None and (getattr(order, "cancelled_at", None) or getattr(order, "closed_at", None)):
        return False
    if order_info.get("cancelled_at") or order_info.get("closed_at"):
        return False
    return True


def split_customer_name(name):
    parts = [part for part in str(name or "").strip().split() if part]
    if not parts:
        return "", "Customer"
    if len(parts) == 1:
        return parts[0], "Customer"
    return parts[0], " ".join(parts[1:])


def parse_money(value, default=0.0):
    try:
        return round(float(value or default), 2)
    except (TypeError, ValueError):
        return round(float(default), 2)


def parse_date_for_sort(value):
    if not value:
        return datetime.min
    raw = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return datetime.min


def load_product_cost_overrides():
    raw = get_app_setting(PRODUCT_COSTS_SETTING_KEY, "{}")
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_product_cost_overrides(overrides):
    return set_app_setting(PRODUCT_COSTS_SETTING_KEY, json.dumps(overrides or {}))


def product_cost_key(product_id=None, variant_id=None, title=""):
    if variant_id:
        return f"variant:{variant_id}"
    if product_id:
        return f"product:{product_id}"
    return f"title:{str(title or '').strip().lower()}"


def get_cost_override_for_item(overrides, product_id=None, variant_id=None, title=""):
    for key in (
        product_cost_key(product_id=product_id, variant_id=variant_id, title=title),
        product_cost_key(product_id=product_id, title=title),
        product_cost_key(title=title),
    ):
        entry = overrides.get(key)
        if isinstance(entry, dict):
            return parse_money(entry.get("cost", 0))
    return 0.0


def set_cost_override(overrides, *, product_id=None, variant_id=None, title="", price=None, cost=None):
    key = product_cost_key(product_id=product_id, variant_id=variant_id, title=title)
    payload = overrides.get(key, {}) if isinstance(overrides.get(key), dict) else {}
    payload.update(
        {
            "product_id": product_id,
            "variant_id": variant_id,
            "title": title,
            "price": parse_money(price, 0) if price is not None else parse_money(payload.get("price", 0)),
            "cost": parse_money(cost, 0) if cost is not None else parse_money(payload.get("cost", 0)),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    overrides[key] = payload
    return overrides


@app.template_filter("parse_date")
def parse_date_filter(value):
    return parse_date_for_sort(value)


@app.template_filter("format_number")
def format_number(value):
    try:
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return str(value)


@app.template_global()
def tag_style(label):
    return _TAG_STYLES.get(label, "background:#e8eaf6;color:#283593")


@app.template_global()
def status_badge(status):
    status = status or ""
    upper = status.upper()
    if "PARTIALLY DELIVERED" in upper:
        bg, color, dot = "#fff0d9", "#8b5a00", "#f6c23e"
    elif "UNDELIVERED" in upper or "RETURN" in upper or "CANCEL" in upper:
        bg, color, dot = "#fce8e6", "#8b1a10", "#e74a3b"
    elif is_delivered_status(status):
        bg, color, dot = "#d4f5e9", "#0f6848", "#1cc88a"
    elif status == "Booked":
        bg, color, dot = "#dde4fb", "#2346a8", "#4e73df"
    elif status == "Un-Booked":
        bg, color, dot = "#ebebed", "#4a4b55", "#858796"
    elif "OUT FOR" in upper or "DISPATCH" in upper or "TRANSIT" in upper:
        bg, color, dot = "#fef8e4", "#7a5c00", "#f6c23e"
    elif "CONFIRM" in upper:
        bg, color, dot = "#d4f5e9", "#0f6848", "#1cc88a"
    elif "CALL NOT" in upper:
        bg, color, dot = "#e8f8fb", "#0a5c6e", "#36b9cc"
    else:
        bg, color, dot = "#e8f8fb", "#0a5c6e", "#36b9cc"

    return Markup(
        f'<span style="display:inline-flex;align-items:center;gap:5px;padding:3px 10px;'
        f'border-radius:99px;font-size:11px;font-weight:600;background:{bg};color:{color};white-space:nowrap;">'
        f'<span style="width:6px;height:6px;border-radius:50%;background:{dot};flex-shrink:0;"></span>'
        f'{status or "—"}</span>'
    )


PENDING_LINE_ITEM_STATUSES = {
    "Booked",
    "Un-Booked",
    "Out For Delivery",
    "Drop Off at Express Center",
    "CONSIGNMENT BOOKED",
    "Picked From Shipper",
    "Call Not Attended",
    "Delay",
    "Hold",
    "Untraceable",
    "Attempt to deliver",
    "Ready To Ship",
    "Packed by seller / warehouse",
}

PAID_FINANCIAL_STATUSES = {"paid", "partially paid", "partially_paid", "partially-paid"}


def is_pending_line_item_status(status):
    normalized = normalize_status_bucket(status)
    return normalized in {"Booked", "Un-Booked"}


def is_need_attention_status(status):
    upper = str(status or "").strip().upper()
    normalized = normalize_status_bucket(status)
    attention_keywords = (
        "UNDELIVERED",
        "CONTACTING CONSIGNEE",
        "MOVED TO ORIGIN BRANCH",
        "RETURN SUBMITTED",
        "RETURN SUBMISSION",
        "BEING RETURN",
        "OUT FOR RETURN",
    )
    return normalized in {"Undelivered", "Being Return"} or any(keyword in upper for keyword in attention_keywords)


def aggregate_order_status(line_items):
    if not line_items:
        return "Un-Booked"

    normalized_statuses = [normalize_status_bucket(item.get("status")) for item in line_items]
    tracking_numbers = {
        str(item.get("tracking_number") or "").strip()
        for item in line_items
        if str(item.get("tracking_number") or "").strip() and str(item.get("tracking_number") or "").strip() != "N/A"
    }
    has_different_tracked_statuses = len(tracking_numbers) > 1 and len(set(normalized_statuses)) > 1
    delivered_count = sum(1 for status in normalized_statuses if status == "Delivered")
    pending_statuses = [status for status in normalized_statuses if is_pending_line_item_status(status)]

    if any(status == "RETURNED TO SHIPPER" for status in normalized_statuses):
        return "RETURNED TO SHIPPER"
    if any(status == "Being Return" for status in normalized_statuses):
        return "Being Return"
    if any(status == "Undelivered" for status in normalized_statuses):
        return "Undelivered"
    if any(is_need_attention_status(item.get("status")) for item in line_items):
        return "Need Attention"
    if has_different_tracked_statuses:
        return "Mixed Status"
    if delivered_count and pending_statuses:
        return "Partially Delivered"
    if delivered_count and delivered_count == len(normalized_statuses):
        return "Delivered"
    if any(status == "Out For Delivery" for status in normalized_statuses):
        return "Out For Delivery"
    if any(status == "Booked" for status in normalized_statuses):
        return "Booked"
    if any(status == "Un-Booked" for status in normalized_statuses):
        return "Un-Booked"
    return normalized_statuses[-1] if normalized_statuses else "Un-Booked"


@app.context_processor
def inject_now():
    return {
        "now": datetime.now(),
        "skip_base_password_prompt": bool(session.get(ADMIN_PORTAL_SESSION_KEY)),
        "embedded_mode": request.args.get("embedded") == "1",
        "customer_value": clean_customer_value,
    }


def employee_portal_is_authenticated():
    return bool(session.get(EMPLOYEE_PORTAL_SESSION_KEY) or session.get(ADMIN_PORTAL_SESSION_KEY))


def admin_portal_is_authenticated():
    return bool(session.get(ADMIN_PORTAL_SESSION_KEY))


def employee_portal_safe_next_url(candidate):
    if candidate and str(candidate).startswith("/employee_portal"):
        return candidate
    return url_for("employee_portal")


def setup_shopify():
    shop_url = get_shop_domain() or (os.getenv("SHOP_URL") or "").strip()
    legacy_token = (os.getenv("PASSWORD") or "").strip()
    oauth_token = get_graphql_token()
    token = legacy_token or oauth_token
    api_key = (os.getenv("API_KEY") or "").strip()
    if not shop_url or not token:
        print("SHOP_URL or PASSWORD missing; Shopify client not configured.")
        return

    try:
        shopify.ShopifyResource.clear_session()
    except Exception:
        pass
    if legacy_token:
        base_url = shopify_rest_base_url()
        shopify.ShopifyResource.set_site(base_url)
        if api_key:
            shopify.ShopifyResource.set_user(api_key)
        shopify.ShopifyResource.set_password(legacy_token)
        return

    try:
        session_obj = shopify.Session(shop_url, get_graphql_api_version(), oauth_token)
        shopify.ShopifyResource.activate_session(session_obj)
        return
    except Exception as error:
        print(f"Could not activate Shopify session: {error}")


def shopify_rest_base_url():
    raw_shop_url = (os.getenv("SHOP_URL") or "").strip()
    parsed = urlparse(raw_shop_url if "://" in raw_shop_url else f"https://{raw_shop_url}")
    host = parsed.netloc or parsed.path
    if not host:
        raise RuntimeError("SHOP_URL is not configured.")
    api_version = os.getenv("SHOPIFY_ADMIN_API_VERSION", "2026-04")
    return f"https://{host}/admin/api/{api_version}"


def shopify_rest_headers():
    token = (os.getenv("PASSWORD") or "").strip() or get_graphql_token()
    if not token:
        raise RuntimeError("Shopify admin token is missing.")
    return {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def fetch_shopify_inventory_item_costs(inventory_item_ids):
    ids = [str(item_id).strip() for item_id in (inventory_item_ids or []) if str(item_id).strip()]
    if not ids:
        return {}
    uncached_ids = [item_id for item_id in ids if item_id not in inventory_item_cost_cache]
    if uncached_ids:
        base_url = shopify_rest_base_url()
        headers = shopify_rest_headers()
        for offset in range(0, len(uncached_ids), 50):
            batch = uncached_ids[offset : offset + 50]
            response = requests.get(
                f"{base_url}/inventory_items.json",
                headers=headers,
                params={"ids": ",".join(batch)},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json().get("inventory_items", []) or []
            for item in payload:
                item_id = str(item.get("id") or "").strip()
                if item_id:
                    inventory_item_cost_cache[item_id] = parse_money(item.get("cost", 0))
            for missing_id in batch:
                inventory_item_cost_cache.setdefault(missing_id, 0.0)
    return {item_id: inventory_item_cost_cache.get(item_id, 0.0) for item_id in ids}


def update_shopify_inventory_item_cost(inventory_item_id, cost):
    if not inventory_item_id:
        return
    base_url = shopify_rest_base_url()
    headers = shopify_rest_headers()
    response = requests.put(
        f"{base_url}/inventory_items/{inventory_item_id}.json",
        headers=headers,
        json={"inventory_item": {"id": int(inventory_item_id), "cost": str(parse_money(cost, 0))}},
        timeout=20,
    )
    response.raise_for_status()
    inventory_item_cost_cache[str(inventory_item_id)] = parse_money(cost, 0)


def parse_shopify_next_link(link_header):
    if not link_header:
        return None
    for part in str(link_header).split(","):
        chunk = part.strip()
        if 'rel="next"' not in chunk:
            continue
        url_part = chunk.split(";", 1)[0].strip()
        if url_part.startswith("<") and url_part.endswith(">"):
            return url_part[1:-1]
    return None


def extract_note_attributes(order):
    notes = {}
    for item in (order or {}).get("note_attributes") or []:
        name = str((item or {}).get("name") or "").strip()
        if name:
            notes[name] = (item or {}).get("value")
    return notes


def fetch_tracking_data_sync(tracking_number):
    async def run_lookup():
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session_obj:
            return await fetch_tracking_data(session_obj, tracking_number)

    return asyncio.run(run_lookup())


def build_tracking_summary_payload(tracking_number):
    data = fetch_tracking_data_sync(tracking_number)
    summary = summarize_tracking_result(tracking_number, data)
    remember_tracking_summary(tracking_number, summary)
    events = []
    if is_leopards_tracking(tracking_number):
        packet_list = (data or {}).get("packet_list") or []
        packet = packet_list[0] if packet_list else {}
        for detail in list((packet or {}).get("Tracking Detail") or [])[::-1]:
            title = detail.get("Status") or "Tracking update"
            meta = " ".join(str(part or "").strip() for part in [detail.get("Activity_Date"), detail.get("Activity_Time")] if str(part or "").strip())
            reason = detail.get("Reason") or ""
            events.append({"title": title, "meta": meta, "reason": "" if reason == "N/A" else reason})
    elif isinstance(data, list):
        for detail in list(data)[::-1]:
            title = detail.get("ProcessDescForPortal") or detail.get("ProcessDesc") or "Tracking update"
            meta = detail.get("TransactionDate") or ""
            reason = detail.get("ReasonDesc") or ""
            events.append({"title": title, "meta": meta, "reason": "" if reason == "OK" else reason})
    return {
        "tracking_number": tracking_number,
        "courier": courier_label_for_tracking("", tracking_number) or "Call Courier",
        "status": summary.get("status") or "No tracking status",
        "customer": summary.get("name") or "",
        "city": summary.get("city") or "",
        "phone": summary.get("phone") or "",
        "address": summary.get("address") or "",
        "events": events,
    }


def get_tracking_summary_cached(tracking_number, ttl_seconds=300):
    tracking_number = str(tracking_number or "").strip()
    if not tracking_number:
        return None
    cache_key = tracking_number.upper()
    cached = tracking_summary_cache.get(cache_key)
    now = time.time()
    if cached and now - cached.get("fetched_at", 0) < ttl_seconds:
        return cached.get("summary")
    try:
        data = fetch_tracking_data_sync(tracking_number)
        summary = summarize_tracking_result(tracking_number, data)
    except Exception as error:
        print(f"Could not fetch tracking summary for {tracking_number}: {error}")
        return cached.get("summary") if cached else None
    tracking_summary_cache[cache_key] = {"fetched_at": now, "summary": summary}
    return summary


def get_tracking_summary_cache_only(tracking_number):
    tracking_number = str(tracking_number or "").strip()
    if not tracking_number:
        return None
    cached = tracking_summary_cache.get(tracking_number.upper())
    return cached.get("summary") if cached else None


def get_postex_token():
    return (
        os.getenv("POSTEX_TOKEN")
        or os.getenv("POSTEX_API_TOKEN")
        or os.getenv("POSTEX_AUTH_TOKEN")
        or ""
    ).strip()


def normalize_postex_payment_cache_entry(entry):
    if not isinstance(entry, dict):
        return None
    dist = entry.get("dist") if isinstance(entry.get("dist"), dict) else {}
    status = str(entry.get("status") or entry.get("statusMessage") or "").strip()
    tracking_number = str(entry.get("tracking_number") or dist.get("trackingNumber") or "").strip()
    if not tracking_number:
        return None
    try:
        fetched_at = float(entry.get("fetched_at") or time.time())
    except (TypeError, ValueError):
        fetched_at = time.time()
    return {
        "fetched_at": fetched_at,
        "tracking_number": tracking_number,
        "settled": bool(entry.get("settled")),
        "status": status,
        "settlement_date": str(entry.get("settlement_date") or dist.get("settlementDate") or "").strip(),
        "upfront_payment_date": str(entry.get("upfront_payment_date") or dist.get("upfrontPaymentDate") or "").strip(),
        "reserve_payment_date": str(entry.get("reserve_payment_date") or dist.get("reservePaymentDate") or "").strip(),
        "order_ref_number": str(entry.get("order_ref_number") or dist.get("orderRefNumber") or "").strip(),
        "error": str(entry.get("error") or "").strip(),
    }


def load_persisted_postex_payment_status_cache():
    raw = get_app_setting(POSTEX_PAYMENT_STATUS_CACHE_SETTING_KEY, "{}")
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    loaded = {}
    for tracking_number, entry in payload.items():
        key = str(tracking_number or "").strip().upper()
        normalized = normalize_postex_payment_cache_entry(entry)
        if key and normalized:
            loaded[key] = normalized
    return loaded


def ensure_postex_payment_status_cache_loaded():
    global postex_payment_status_cache_loaded
    if postex_payment_status_cache_loaded:
        return
    persisted = load_persisted_postex_payment_status_cache()
    if persisted:
        postex_payment_status_cache.update(persisted)
    postex_payment_status_cache_loaded = True


def persist_postex_payment_status_cache():
    payload = {
        key: entry
        for key, entry in postex_payment_status_cache.items()
        if normalize_postex_payment_cache_entry(entry)
    }
    return set_app_setting(POSTEX_PAYMENT_STATUS_CACHE_SETTING_KEY, json.dumps(payload))


async def fetch_postex_payment_status(session_obj, tracking_number):
    token = get_postex_token()
    if not token:
        raise RuntimeError("POSTEX_TOKEN is not configured.")
    tracking_number = str(tracking_number or "").strip()
    url = f"https://api.postex.pk/services/integration/api/order/v1/payment-status/{tracking_number}"
    async with session_obj.get(url, headers={"token": token}) as response:
        payload = await response.json(content_type=None)
        if response.status == 404:
            return {
                "tracking_number": tracking_number,
                "settled": False,
                "status": "Order Not Found",
                "error": "Order Not Found",
            }
        response.raise_for_status()
        dist = payload.get("dist") if isinstance(payload, dict) else {}
        dist = dist if isinstance(dist, dict) else {}
        return {
            "tracking_number": str(dist.get("trackingNumber") or tracking_number).strip(),
            "settled": bool(dist.get("settle")),
            "status": str((payload or {}).get("statusMessage") or "").strip(),
            "settlement_date": str(dist.get("settlementDate") or "").strip(),
            "upfront_payment_date": str(dist.get("upfrontPaymentDate") or "").strip(),
            "reserve_payment_date": str(dist.get("reservePaymentDate") or "").strip(),
            "order_ref_number": str(dist.get("orderRefNumber") or "").strip(),
            "error": "",
        }


def refresh_postex_payment_statuses_sync(tracking_numbers, ttl_seconds=3600):
    ensure_postex_payment_status_cache_loaded()
    unique_numbers = []
    seen = set()
    now = time.time()
    for tracking_number in tracking_numbers:
        tracking_number = str(tracking_number or "").strip()
        if not is_postex_tracking(tracking_number):
            continue
        key = tracking_number.upper()
        if key in seen:
            continue
        seen.add(key)
        cached = postex_payment_status_cache.get(key)
        if cached and now - cached.get("fetched_at", 0) < ttl_seconds:
            continue
        unique_numbers.append(tracking_number)

    async def refresh_all():
        timeout = aiohttp.ClientTimeout(total=12)
        semaphore = asyncio.Semaphore(6)
        updates = {}
        async with aiohttp.ClientSession(timeout=timeout) as session_obj:
            async def refresh_one(tracking_number):
                async with semaphore:
                    try:
                        status = await fetch_postex_payment_status(session_obj, tracking_number)
                    except Exception as error:
                        print(f"Could not refresh PostEx payment status for {tracking_number}: {error}")
                        status = {
                            "tracking_number": tracking_number,
                            "settled": False,
                            "status": "",
                            "error": str(error),
                        }
                    status["fetched_at"] = time.time()
                    updates[tracking_number.upper()] = status

            await asyncio.gather(*(refresh_one(number) for number in unique_numbers))
        return updates

    if unique_numbers and get_postex_token():
        updates = asyncio.run(refresh_all())
        if updates:
            postex_payment_status_cache.update(updates)
            persist_postex_payment_status_cache()
    return len(unique_numbers)


def get_postex_payment_status_cache_only(tracking_number):
    tracking_number = str(tracking_number or "").strip()
    if not tracking_number:
        return None
    ensure_postex_payment_status_cache_loaded()
    return postex_payment_status_cache.get(tracking_number.upper())


POSTEX_ORDER_STATUSES = {
    0: "All Orders",
    1: "Unbooked",
    2: "Booked",
    3: "PostEx WareHouse",
    4: "Out For Delivery",
    5: "Delivered",
    6: "Returned",
    7: "Un-Assigned By Me",
    8: "Expired",
    9: "Delivery Under Review",
    15: "Picked By PostEx",
    16: "Out For Return",
    17: "Attempted",
    18: "En-Route to PostEx warehouse",
}


def parse_iso_date(value, default_date):
    raw = str(value or "").strip()
    if not raw:
        return default_date
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except ValueError:
        return default_date


def fetch_postex_orders(order_status_id, start_date, end_date):
    token = get_postex_token()
    if not token:
        raise RuntimeError("POSTEX_TOKEN is not configured.")
    url = "https://api.postex.pk/services/integration/api/order/v1/get-all-order"
    headers = {"token": token}
    primary_params = {
        "orderStatusID": int(order_status_id),
        "fromDate": start_date.isoformat(),
        "toDate": end_date.isoformat(),
    }
    response = requests.get(url, headers=headers, params=primary_params, timeout=30)
    if response.status_code >= 400:
        fallback_params = {
            "orderStatusId": int(order_status_id),
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
        }
        response = requests.get(url, headers=headers, params=fallback_params, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return []
    dist = payload.get("dist") or []
    return dist if isinstance(dist, list) else []


def extract_postex_tracking_response(entry):
    if not isinstance(entry, dict):
        return {}
    response = entry.get("trackingResponse")
    if isinstance(response, dict):
        return response
    return entry


def build_postex_payments_page_data(order_status_id, start_date, end_date):
    orders = fetch_postex_orders(order_status_id, start_date, end_date)
    tracking_numbers = []
    rows = []
    for entry in orders:
        data = extract_postex_tracking_response(entry)
        tracking_number = str(data.get("trackingNumber") or entry.get("trackingNumber") or "").strip()
        if tracking_number:
            tracking_numbers.append(tracking_number)

    refresh_postex_payment_statuses_sync(tracking_numbers, ttl_seconds=900)

    summary = {
        "orders": 0,
        "invoice_payment": 0.0,
        "shipping_fee": 0.0,
        "delivered_tax": 0.0,
        "net_receivable": 0.0,
        "pending_receivable": 0.0,
        "paid_receivable": 0.0,
        "pending_payable": 0.0,
        "paid_payable": 0.0,
        "undelivered_amount": 0.0,
    }

    for entry in orders:
        data = extract_postex_tracking_response(entry)
        tracking_number = str(data.get("trackingNumber") or entry.get("trackingNumber") or "").strip()
        if not tracking_number:
            continue
        invoice_payment = parse_money(data.get("invoicePayment", 0))
        transaction_fee = parse_money(data.get("transactionFee", 0))
        transaction_tax = parse_money(data.get("transactionTax", 0))
        reversal_fee = parse_money(data.get("reversalFee", 0))
        reversal_tax = parse_money(data.get("reversalTax", 0))
        transaction_status = str(data.get("transactionStatus") or "").strip()
        normalized_transaction_status = transaction_status.lower()
        is_unbooked = int(order_status_id) == 1 or "unbook" in normalized_transaction_status
        is_delivered = int(order_status_id) == 5 or "delivered" in normalized_transaction_status
        is_returned = int(order_status_id) in {6, 16} or "return" in normalized_transaction_status
        is_undelivered = not is_delivered and not is_returned and not is_unbooked
        if is_returned:
            shipping_fee = round(reversal_fee + reversal_tax, 2)
        elif is_delivered:
            shipping_fee = round(transaction_fee + transaction_tax, 2)
        else:
            shipping_fee = 0.0
        delivered_tax = round(invoice_payment * 0.04, 2) if is_delivered else 0.0
        if is_returned:
            net_receivable = round(-shipping_fee, 2)
        elif is_delivered:
            net_receivable = round(invoice_payment - shipping_fee - delivered_tax, 2)
        else:
            net_receivable = 0.0
        payment_status = get_postex_payment_status_cache_only(tracking_number) or {}
        is_paid = bool(payment_status.get("settled"))
        status_label = "Paid" if is_paid else "Pending"
        if payment_status.get("error"):
            status_label = "Unknown"
        row = {
            "order_id": str(data.get("orderRefNumber") or "").strip(),
            "tracking_number": tracking_number,
            "product": str(data.get("orderDetail") or "").strip(),
            "invoice_payment": invoice_payment,
            "shipping_fee": shipping_fee,
            "delivered_tax": delivered_tax,
            "net_receivable": net_receivable,
            "payment_status": status_label,
            "settlement_date": payment_status.get("settlement_date") or "",
            "transaction_status": transaction_status,
            "is_returned": is_returned,
            "is_delivered": is_delivered,
            "is_undelivered": is_undelivered,
        }
        rows.append(row)

        summary["orders"] += 1
        summary["invoice_payment"] += invoice_payment
        summary["shipping_fee"] += shipping_fee
        summary["delivered_tax"] += delivered_tax
        if is_undelivered:
            summary["undelivered_amount"] += invoice_payment
        if is_returned:
            summary["net_receivable"] += net_receivable
            if is_paid:
                summary["paid_payable"] += shipping_fee
            elif status_label == "Pending":
                summary["pending_payable"] += shipping_fee
        elif is_delivered and is_paid:
            summary["paid_receivable"] += net_receivable
        elif is_delivered and status_label == "Pending":
            summary["net_receivable"] += net_receivable
            summary["pending_receivable"] += net_receivable

    for key in (
        "invoice_payment",
        "shipping_fee",
        "delivered_tax",
        "net_receivable",
        "pending_receivable",
        "paid_receivable",
        "pending_payable",
        "paid_payable",
        "undelivered_amount",
    ):
        summary[key] = round(summary[key], 2)

    rows.sort(key=lambda item: (item["payment_status"] != "Pending", -parse_money(item["net_receivable"])))
    return {
        "rows": rows,
        "summary": summary,
        "statuses": POSTEX_ORDER_STATUSES,
        "selected_status": int(order_status_id),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "token_configured": bool(get_postex_token()),
    }


def normalize_tracking_numbers(tracking_numbers):
    unique_numbers = []
    seen = set()
    for tracking_number in tracking_numbers:
        tracking_number = str(tracking_number or "").strip()
        if not tracking_number or tracking_number == "N/A":
            continue
        key = tracking_number.upper()
        if key in seen:
            continue
        seen.add(key)
        unique_numbers.append(tracking_number)
    return unique_numbers


def is_final_tracking_status(status):
    normalized = normalize_status_bucket(status)
    upper = str(status or "").strip().upper()
    return normalized == "Delivered" or normalized == "RETURNED TO SHIPPER" or upper in {"RETURNED", "RETURN"}


def active_tracking_numbers_from_orders(orders):
    return normalize_tracking_numbers(
        item.get("tracking_number")
        for order in orders or []
        for item in order.get("line_items", []) or []
        if not is_final_tracking_status(item.get("status"))
    )


def apply_tracking_summary_to_order_cache(tracking_number, summary):
    tracking_number = str(tracking_number or "").strip()
    if not tracking_number or not summary:
        return 0
    normalized = normalize_scan_term(tracking_number)
    updated = 0
    status = summary.get("status") or "Booked"

    def maybe_fill_customer_field(item, field):
        current = clean_customer_value(item.get(field))
        candidate = clean_customer_value(summary.get(field))
        if current or not candidate:
            return
        item[field] = candidate

    for order in order_details:
        changed_order = False
        for item in order.get("line_items", []) or []:
            if normalize_scan_term(item.get("tracking_number")) != normalized:
                continue
            item["status"] = status
            for field in ("name", "address", "city", "phone"):
                maybe_fill_customer_field(item, field)
            changed_order = True
            updated += 1
        if changed_order:
            order["status"] = aggregate_order_status(order.get("line_items", []))
    return updated


def remember_tracking_summary(tracking_number, summary, fetched_at=None):
    tracking_number = str(tracking_number or "").strip()
    if not tracking_number or not summary:
        return
    tracking_summary_cache[tracking_number.upper()] = {
        "fetched_at": fetched_at or time.time(),
        "summary": summary,
    }
    apply_tracking_summary_to_order_cache(tracking_number, summary)


def refresh_tracking_summaries_sync(
    tracking_numbers,
    *,
    limit=None,
    fresh_seconds=0,
    deadline_seconds=TRACKING_REFRESH_SYNC_DEADLINE_SECONDS,
):
    unique_numbers = []
    now = time.time()
    for tracking_number in normalize_tracking_numbers(tracking_numbers):
        cached = tracking_summary_cache.get(tracking_number.upper())
        if cached and fresh_seconds and now - cached.get("fetched_at", 0) < fresh_seconds:
            apply_tracking_summary_to_order_cache(tracking_number, cached.get("summary") or {})
            continue
        unique_numbers.append(tracking_number)
        if limit and len(unique_numbers) >= limit:
            break

    async def refresh_all():
        timeout = aiohttp.ClientTimeout(total=TRACKING_REFRESH_PER_SHIPMENT_TIMEOUT_SECONDS)
        semaphore = asyncio.Semaphore(6)
        refreshed_count = 0
        refreshed_at = time.time()
        async with aiohttp.ClientSession(timeout=timeout) as session_obj:
            async def refresh_one(tracking_number):
                nonlocal refreshed_count
                async with semaphore:
                    try:
                        data = await asyncio.wait_for(
                            fetch_tracking_data(session_obj, tracking_number),
                            timeout=TRACKING_REFRESH_PER_SHIPMENT_TIMEOUT_SECONDS,
                        )
                        summary = summarize_tracking_result(tracking_number, data)
                    except Exception as error:
                        print(f"Could not refresh tracking summary for {tracking_number}: {error}")
                        return
                    remember_tracking_summary(tracking_number, summary, refreshed_at)
                    refreshed_count += 1

            tasks = [asyncio.create_task(refresh_one(number)) for number in unique_numbers]
            done, pending = await asyncio.wait(tasks, timeout=deadline_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                try:
                    task.result()
                except Exception as error:
                    print(f"Could not complete tracking refresh task: {error}")
        return refreshed_count

    if not unique_numbers:
        return 0
    return asyncio.run(refresh_all())


def start_tracking_summaries_background_refresh(tracking_numbers):
    unique_numbers = normalize_tracking_numbers(tracking_numbers)
    if not unique_numbers:
        return False

    def run_background_refresh():
        if not tracking_refresh_lock.acquire(blocking=False):
            return
        try:
            refresh_tracking_summaries_sync(
                unique_numbers,
                limit=TRACKING_REFRESH_BACKGROUND_LIMIT,
                fresh_seconds=TRACKING_REFRESH_FRESH_SECONDS,
                deadline_seconds=TRACKING_REFRESH_BACKGROUND_DEADLINE_SECONDS,
            )
        finally:
            tracking_refresh_lock.release()

    threading.Thread(target=run_background_refresh, daemon=True).start()
    return True


def refresh_active_tracking_for_dashboard():
    tracking_numbers = active_tracking_numbers_from_orders(order_details)
    refreshed_count = refresh_tracking_summaries_sync(
        tracking_numbers,
        limit=TRACKING_REFRESH_SYNC_LIMIT,
        fresh_seconds=TRACKING_REFRESH_FRESH_SECONDS,
        deadline_seconds=TRACKING_REFRESH_SYNC_DEADLINE_SECONDS,
    )
    start_tracking_summaries_background_refresh(tracking_numbers)
    return refreshed_count


def repair_order_customer_cache(orders):
    for order in orders or []:
        customer = order.setdefault("customer_details", {})
        for item in order.get("line_items", []) or []:
            for field in ("name", "address", "city", "phone"):
                item_value = clean_customer_value(item.get(field))
                customer_value = clean_customer_value(customer.get(field))
                if not customer_value and item_value:
                    customer[field] = item_value
                    customer_value = item_value
                if not item_value and customer_value:
                    item[field] = customer_value


def ensure_required_shopify_webhooks():
    app_base_url = (os.getenv("SHOPIFY_APP_BASE_URL") or "").rstrip("/")
    if not app_base_url:
        raise RuntimeError("SHOPIFY_APP_BASE_URL is not configured.")

    desired = {
        "ORDERS_CREATE": f"{app_base_url}/shopify/webhook/order_created",
        "ORDERS_UPDATED": f"{app_base_url}/shopify/webhook/order_updated",
        "ORDERS_CANCELLED": f"{app_base_url}/shopify/webhook/order_cancelled",
    }

    endpoint = get_graphql_endpoint()
    token = get_graphql_token()
    if not endpoint or not token:
        raise RuntimeError("Shopify Admin GraphQL credentials are not configured.")
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    list_query = """
      query AabrooWebhookSubscriptions {
        webhookSubscriptions(first: 100) {
          nodes { id topic uri }
        }
      }
    """
    response = requests.post(endpoint, headers=headers, json={"query": list_query}, timeout=20)
    response.raise_for_status()
    response_payload = response.json()
    if response_payload.get("errors"):
        raise RuntimeError(f"Could not list Shopify webhooks: {response_payload['errors']}")
    existing_hooks = (((response_payload.get("data") or {}).get("webhookSubscriptions") or {}).get("nodes") or [])
    existing_pairs = {
        (
            str((hook or {}).get("topic") or "").strip().lower(),
            str((hook or {}).get("uri") or "").strip().rstrip("/"),
        )
        for hook in existing_hooks
    }

    for topic, address in desired.items():
        if (topic.lower(), address.rstrip("/")) in existing_pairs:
            continue
        create_mutation = """
          mutation AabrooWebhookSubscriptionCreate(
            $topic: WebhookSubscriptionTopic!,
            $webhookSubscription: WebhookSubscriptionInput!
          ) {
            webhookSubscriptionCreate(topic: $topic, webhookSubscription: $webhookSubscription) {
              webhookSubscription { id topic uri }
              userErrors { field message }
            }
          }
        """
        create_response = requests.post(
            endpoint,
            headers=headers,
            json={"query": create_mutation, "variables": {"topic": topic, "webhookSubscription": {"uri": address}}},
            timeout=20,
        )
        create_response.raise_for_status()
        create_payload = create_response.json()
        if create_payload.get("errors"):
            raise RuntimeError(f"Could not create {topic} webhook: {create_payload['errors']}")
        result = ((create_payload.get("data") or {}).get("webhookSubscriptionCreate") or {})
        if result.get("userErrors"):
            raise RuntimeError(f"Could not create {topic} webhook: {result['userErrors']}")

    return {"success": True, "topics": sorted(desired), "destinations": desired}


async def fetch_tracking_data(session_obj, tracking_number):
    if not tracking_number or tracking_number == "N/A":
        return {}
    if is_digidokaan_tracking(tracking_number):
        history = await fetch_digidokaan_tracking_history(session_obj, tracking_number)
        if history:
            return history
        status = await fetch_digidokaan_tracking_status(session_obj, tracking_number)
        return [{"ProcessDescForPortal": status}] if status else []
    if is_leopards_tracking(tracking_number):
        api_key = os.getenv("LEOPARD_API_KEY")
        api_password = os.getenv("LEOPARD_PASSWORD") or os.getenv("LEOPARD_API_PASSWORD")
        url = (
            "https://merchantapi.leopardscourier.com/api/trackBookedPacket/"
            f"?api_key={api_key}&api_password={api_password}&track_numbers={tracking_number}"
        )
    else:
        url = f"https://cod.callcourier.com.pk/api/CallCourier/GetTackingHistory?cn={tracking_number}"

    async with session_obj.get(url) as response:
        return await response.json()


def get_variant_image_and_title(product, line_item):
    cache_key = (getattr(product, "id", None), getattr(line_item, "variant_id", None))
    if cache_key in product_image_cache:
        return product_image_cache[cache_key]

    line_item_variant_id = str(getattr(line_item, "variant_id", "") or "").strip()
    line_item_image_id = str(getattr(line_item, "image_id", "") or "").strip()
    base_image = ""
    if getattr(product, "image", None):
        base_image = getattr(product.image, "src", "") or ""

    variant_name = ""
    image_src = base_image or "/static/aabroo-wordmark.svg"
    inventory_item_id = None
    images = list(getattr(product, "images", []) or [])
    fetched_images = False

    if line_item_image_id and images:
        for image in images:
            if str(getattr(image, "id", "") or "").strip() == line_item_image_id:
                image_src = getattr(image, "src", "") or image_src
                break

    for variant in getattr(product, "variants", []) or []:
        if str(getattr(variant, "id", "") or "").strip() != line_item_variant_id:
            continue
        variant_name = "" if getattr(variant, "title", "") in {"", "Default Title"} else getattr(variant, "title", "")
        inventory_item_id = getattr(variant, "inventory_item_id", None)
        variant_image_id = getattr(variant, "image_id", None)
        if variant_image_id and not images and not fetched_images:
            try:
                images = list(shopify.Image.find(product_id=getattr(product, "id", None)) or [])
            except Exception:
                images = []
            fetched_images = True
        if variant_image_id and images:
            for image in images:
                if str(getattr(image, "id", "") or "").strip() == str(variant_image_id).strip():
                    image_src = getattr(image, "src", "") or image_src
                    break
                attached_variant_ids = getattr(image, "variant_ids", None) or []
                if str(getattr(variant, "id", "") or "").strip() in {str(v).strip() for v in attached_variant_ids}:
                    image_src = getattr(image, "src", "") or image_src
                    break
        break

    product_image_cache[cache_key] = (image_src, variant_name, inventory_item_id)
    return image_src, variant_name, inventory_item_id


def summarize_tracking_result(tracking_number, data):
    if not tracking_number or tracking_number == "N/A":
        return {
            "status": "Un-Booked",
            "name": "",
            "address": "",
            "phone": "",
            "city": "",
        }

    if is_leopards_tracking(tracking_number):
        packet_list = (data or {}).get("packet_list") or []
        if not packet_list:
            return {"status": "Booked", "name": "", "address": "", "phone": "", "city": ""}
        packet = packet_list[0]
        tracking_details = packet.get("Tracking Detail") or []
        final_status = packet.get("booked_packet_status") or "Booked"
        if tracking_details:
            last_tracking = tracking_details[-1]
            final_status = last_tracking.get("Status") or final_status
            reason = last_tracking.get("Reason") or ""
            if reason and reason != "N/A":
                final_status = f"{final_status} - {reason}"
        if final_status in {"Pickup Request not Send", "Pickup Request Sent"}:
            final_status = "Booked"
        return {
            "status": final_status,
            "name": packet.get("consignment_name_eng") or "",
            "address": packet.get("consignment_address") or "",
            "phone": packet.get("consignment_phone") or "",
            "city": packet.get("destination_city_name") or "",
        }

    if isinstance(data, list) and data:
        first = data[0]
        last = data[-1]
        status = last.get("ProcessDescForPortal") or "Booked"
        return {
            "status": status,
            "name": first.get("ConsigneeName") or "",
            "address": first.get("ConsigneeAddress") or "",
            "phone": first.get("ContactNo") or "",
            "city": first.get("ConsigneeCity") or "",
        }
    if is_digidokaan_tracking(tracking_number):
        return {"status": "Tracking unavailable", "name": "", "address": "", "phone": "", "city": ""}
    return {"status": "Booked", "name": "", "address": "", "phone": "", "city": ""}


async def process_line_item(session_obj, line_item, fulfillments):
    if line_item.fulfillment_status is None and getattr(line_item, "fulfillable_quantity", 0) == 0:
        return []

    tracking_info = []

    if line_item.fulfillment_status == "fulfilled":
        for fulfillment in fulfillments:
            if getattr(fulfillment, "status", "") == "cancelled":
                continue
            for item in getattr(fulfillment, "line_items", []) or []:
                if getattr(item, "id", None) != getattr(line_item, "id", None):
                    continue
                tracking_number = getattr(fulfillment, "tracking_number", "") or "N/A"
                courier_name = courier_label_for_tracking(getattr(fulfillment, "tracking_company", ""), tracking_number) or "Call Courier"
                try:
                    data = await fetch_tracking_data(session_obj, tracking_number)
                except Exception as error:
                    print(f"Error fetching tracking for {tracking_number}: {error}")
                    data = {}
                summary = summarize_tracking_result(tracking_number, data)
                tracking_info.append(
                    {
                        "tracking_number": tracking_number,
                        "courier_name": courier_name,
                        "status": summary["status"],
                        "quantity": getattr(item, "quantity", getattr(line_item, "quantity", 1)),
                        "name": summary["name"],
                        "address": summary["address"],
                        "city": summary["city"],
                        "phone": summary["phone"],
                    }
                )

    if tracking_info:
        return tracking_info

    return [
        {
            "tracking_number": "N/A",
            "courier_name": "",
            "status": "Un-Booked",
            "name": "",
            "address": "",
            "phone": "",
            "city": "",
            "quantity": getattr(line_item, "quantity", 1),
        }
    ]


async def process_order(session_obj, order):
    global LAST_REQUEST_TIME

    elapsed_time = time.time() - LAST_REQUEST_TIME
    if elapsed_time < 1 / RATE_LIMIT:
        await asyncio.sleep((1 / RATE_LIMIT) - elapsed_time)
    LAST_REQUEST_TIME = time.time()

    created_at_raw = getattr(order, "created_at", "") or ""
    created_at_display = created_at_raw
    try:
        created_at_display = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass

    fulfillment_status = getattr(order, "fulfillment_status", None)
    order_status = fulfillment_status.title() if fulfillment_status else "Un-fulfilled"

    billing = getattr(order, "billing_address", None)
    shipping = getattr(order, "shipping_address", None)
    customer_details = {
        "name": getattr(billing, "name", None) or getattr(shipping, "name", None) or "",
        "address": getattr(billing, "address1", None) or getattr(shipping, "address1", None) or "",
        "city": getattr(billing, "city", None) or getattr(shipping, "city", None) or "",
        "phone": getattr(billing, "phone", None) or getattr(shipping, "phone", None) or getattr(order, "phone", "") or "",
    }

    order_info = {
        "order_link": f"https://admin.shopify.com/store/{get_shop_domain().split('.')[0]}/orders/{order.id}",
        "order_id": getattr(order, "name", ""),
        "tracking_id": "N/A",
        "created_at": created_at_display,
        "total_price": parse_money(getattr(order, "total_price", 0)),
        "subtotal_price": parse_money(getattr(order, "subtotal_price", 0)),
        "shipping_charges": parse_money(getattr(order, "total_shipping_price_set", {}).get("shop_money", {}).get("amount", 0) if isinstance(getattr(order, "total_shipping_price_set", None), dict) else 0),
        "total_discounts": parse_money(getattr(order, "total_discounts", 0)),
        "line_items": [],
        "financial_status": (getattr(order, "financial_status", "") or "").title(),
        "fulfillment_status": order_status,
        "customer_details": customer_details,
        "tags": [tag for tag in (getattr(order, "tags", "") or "").split(", ") if tag],
        "id": getattr(order, "id", None),
        "cancelled_at": getattr(order, "cancelled_at", None),
        "closed_at": getattr(order, "closed_at", None),
    }

    tasks = [process_line_item(session_obj, line_item, getattr(order, "fulfillments", [])) for line_item in getattr(order, "line_items", [])]
    results = await asyncio.gather(*tasks)

    for tracking_info_list, line_item in zip(results, getattr(order, "line_items", [])):
        if tracking_info_list is None:
            continue
        variant_name = ""
        image_src = "https://cdn.shopify.com/s/files/1/0936/0949/2789/files/7.png?v=1741033934"
        if getattr(line_item, "product_id", None):
            try:
                product = shopify.Product.find(getattr(line_item, "product_id", None))
                if product:
                    image_src, variant_name, inventory_item_id = get_variant_image_and_title(product, line_item)
                else:
                    inventory_item_id = None
            except Exception as error:
                print(f"Could not fetch product image for {getattr(line_item, 'product_id', None)}: {error}")
                inventory_item_id = None
        else:
            inventory_item_id = None

        base_title = getattr(line_item, "title", "") or "Product"
        product_title = base_title if not variant_name else f"{base_title} - {variant_name}"
        unit_price = parse_money(getattr(line_item, "price", 0))
        unit_cost = 0.0

        for info in tracking_info_list:
            quantity = int(info["quantity"] or 0)
            line_total = round(unit_price * quantity, 2)
            line_cost_total = round(unit_cost * quantity, 2)
            order_info["line_items"].append(
                {
                    "fulfillment_status": getattr(line_item, "fulfillment_status", None),
                    "image_src": image_src,
                    "product_title": product_title,
                    "product_id": getattr(line_item, "product_id", None),
                    "variant_id": getattr(line_item, "variant_id", None),
                    "inventory_item_id": inventory_item_id,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "unit_cost": unit_cost,
                    "line_total": line_total,
                    "line_cost_total": line_cost_total,
                    "tracking_number": info["tracking_number"],
                    "courier_name": info.get("courier_name", ""),
                    "status": info["status"],
                    "name": info.get("name", ""),
                    "address": info.get("address", ""),
                    "city": info.get("city", ""),
                    "phone": info.get("phone", ""),
                }
            )
    order_info["status"] = aggregate_order_status(order_info["line_items"])

    return order_info


async def limited_request(coroutine, semaphore_obj):
    async with semaphore_obj:
        await asyncio.sleep(0.5)
        return await coroutine


def enrich_orders_with_protected_customer_data(orders):
    if not orders:
        return orders
    protected_details, errors = fetch_protected_order_details(
        [order.get("id") for order in orders if order.get("id")]
    )
    for error in errors:
        print(f"Shopify protected data warning: {error}")
    for order in orders:
        order_id = str(order.get("id") or "")
        if not order_id or order_id not in protected_details:
            continue
        details = protected_details[order_id]
        customer = order.setdefault("customer_details", {})
        customer["name"] = details.get("name") or customer.get("name", "")
        customer["address"] = details.get("address") or customer.get("address", "")
        customer["city"] = details.get("city") or customer.get("city", "")
        customer["phone"] = details.get("phone") or customer.get("phone", "")
        for item in order.get("line_items", []):
            item["name"] = details.get("name") or item.get("name", "")
            item["address"] = details.get("address") or item.get("address", "")
            item["city"] = details.get("city") or item.get("city", "")
            item["phone"] = details.get("phone") or item.get("phone", "")
    repair_order_customer_cache(orders)
    return orders


def order_has_customer_details(order):
    customer = order.get("customer_details") or {}
    if any(clean_customer_value(customer.get(field)) for field in ("name", "address", "city", "phone")):
        return True
    for item in order.get("line_items", []) or []:
        if any(clean_customer_value(item.get(field)) for field in ("name", "address", "city", "phone")):
            return True
    return False


def refill_missing_customer_details(orders):
    missing_orders = [
        order
        for order in orders or []
        if order.get("id") and not order_has_customer_details(order)
    ]
    if missing_orders:
        enrich_orders_with_protected_customer_data(missing_orders)
    repair_order_customer_cache(orders)


def enrich_orders_with_shopify_costs(orders):
    if not orders:
        return orders
    overrides = load_product_cost_overrides()
    inventory_ids = []
    for order in orders:
        for item in order.get("line_items", []):
            inventory_id = item.get("inventory_item_id")
            if inventory_id:
                inventory_ids.append(str(inventory_id))
    cost_map = {}
    if inventory_ids:
        try:
            cost_map = fetch_shopify_inventory_item_costs(inventory_ids)
        except Exception as error:
            print(f"Could not fetch Shopify inventory costs: {error}")

    for order in orders:
        for item in order.get("line_items", []):
            inventory_id = str(item.get("inventory_item_id") or "").strip()
            fallback_cost = get_cost_override_for_item(
                overrides,
                product_id=item.get("product_id"),
                variant_id=item.get("variant_id"),
                title=item.get("product_title", ""),
            )
            unit_cost = cost_map.get(inventory_id, fallback_cost) if inventory_id else fallback_cost
            quantity = int(item.get("quantity") or 0)
            item["unit_cost"] = parse_money(unit_cost, 0)
            item["line_cost_total"] = round(parse_money(unit_cost, 0) * quantity, 2)
    return orders


def sort_orders_newest_first(orders):
    return sorted(
        orders or [],
        key=lambda order: parse_date_for_sort(order.get("created_at")),
        reverse=True,
    )


def get_shopify_created_at_min():
    raw_value = (os.getenv("SHOPIFY_CREATED_AT_MIN") or "2024-09-01T00:00:00+00:00").strip()
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        print(f"Invalid SHOPIFY_CREATED_AT_MIN '{raw_value}', using 2024-09-01T00:00:00+00:00.")
        return "2024-09-01T00:00:00+00:00"


def get_shopify_fetch_status():
    return (os.getenv("SHOPIFY_ORDER_FETCH_STATUS") or "open").strip().lower() or "open"


def fetch_all_shopify_orders(created_at_min, fetch_status):
    collected = []

    try:
        orders = shopify.Order.find(
            limit=250,
            order="created_at DESC",
            created_at_min=created_at_min,
            status=fetch_status,
        )
    except Exception as error:
        print(f"Error fetching Shopify orders: {error}")
        return None

    page_count = 0
    while True:
        page_orders = list(orders)
        page_count += 1
        collected.extend(page_orders)
        try:
            if not orders.has_next_page():
                break
            orders = orders.next_page()
        except Exception as error:
            print(f"Error loading next page: {error}")
            break

    print(
        f"Fetched {len(collected)} Shopify orders across {page_count} page(s) "
        f"with status={fetch_status}, created_at_min={created_at_min}."
    )
    return collected


async def get_shopify_orders(force_status=None):
    created_at_min = get_shopify_created_at_min()
    fetch_status = (force_status or get_shopify_fetch_status() or "open").strip().lower() or "open"
    fetched_orders = fetch_all_shopify_orders(created_at_min, fetch_status)
    if fetched_orders is None:
        return None

    collected = []
    request_semaphore = asyncio.Semaphore(2)
    async with aiohttp.ClientSession() as session_obj:
        tasks = [limited_request(process_order(session_obj, order), request_semaphore) for order in fetched_orders]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                print(f"Error processing Shopify order: {result}")
                continue
            if not should_keep_order_in_active_list(result):
                continue
            collected.append(result)

    collected = enrich_orders_with_protected_customer_data(collected)
    collected = enrich_orders_with_shopify_costs(collected)
    return sort_orders_newest_first(collected)


def merge_order_refresh(existing_orders, refreshed_orders):
    merged = {}
    for order in existing_orders or []:
        key = str(order.get("id") or order.get("order_id") or "").strip()
        if key:
            merged[key] = order
    for order in refreshed_orders or []:
        key = str(order.get("id") or order.get("order_id") or "").strip()
        if key:
            merged[key] = order
    return sort_orders_newest_first(list(merged.values()))


def reload_orders(preserve_existing_on_partial=False):
    global order_details
    try:
        setup_shopify()
        fetched_orders = asyncio.run(get_shopify_orders())
        if fetched_orders is None:
            print("Keeping existing order cache because Shopify fetch failed.")
            return False
        if preserve_existing_on_partial and order_details and len(fetched_orders) < len(order_details):
            print(
                f"Partial Shopify refresh returned {len(fetched_orders)} orders; "
                f"keeping {len(order_details)} cached orders and merging refreshed tracking data."
            )
            order_details = merge_order_refresh(order_details, fetched_orders)
            return False
        order_details = sort_orders_newest_first(fetched_orders)
        return True
    except Exception as error:
        print(f"Could not reload orders: {error}")
        return False


def find_shopify_order_by_order_name(order_id):
    normalized = normalize_scan_term(order_id)
    for order in order_details:
        if normalize_scan_term(order.get("order_id")) == normalized:
            return order
    return None


def find_shopify_order_by_tracking_number(tracking_number):
    normalized = normalize_scan_term(tracking_number)
    if not normalized:
        return None
    for order in order_details:
        for item in order.get("line_items", []):
            if normalize_scan_term(item.get("tracking_number")) == normalized:
                return order
    return None


def serialize_shopify_order_for_employee(order):
    customer = order.get("customer_details") or {}
    return {
        "source": "shopify",
        "shopify_id": order.get("id"),
        "order_id": str(order.get("order_id", "")),
        "status": order.get("status", ""),
        "customer_name": customer.get("name", ""),
        "customer_phone": customer.get("phone", ""),
        "customer_city": customer.get("city", ""),
        "total_price": order.get("total_price", 0),
        "created_at": order.get("created_at", ""),
        "items": [
            {
                "title": item.get("product_title", ""),
                "quantity": item.get("quantity", 0),
                "image": item.get("image_src", ""),
                "tracking_number": item.get("tracking_number", "N/A"),
                "status": item.get("status", ""),
            }
            for item in order.get("line_items", [])
        ],
    }


def build_employee_portal_orders():
    return [serialize_shopify_order_for_employee(order) for order in order_details]


def build_pending_orders_mobile_data():
    all_orders = []
    statuses = load_order_statuses()

    for shopify_order in order_details:
        if any(tag.startswith("Dispatched") for tag in shopify_order.get("tags", [])):
            continue
        order_status = shopify_order.get("status", "")
        filtered_tags = [tag.strip() for tag in shopify_order.get("tags", []) if tag and tag.strip() != "Leopards Courier"]
        customer_city = ((shopify_order.get("customer_details") or {}).get("city") or "").strip()
        items = []
        for item in shopify_order.get("line_items", []):
            item_status = normalize_status_bucket(item.get("status", ""))
            if not is_pending_line_item_status(item_status):
                continue
            track_num = item.get("tracking_number", "N/A")
            key = f"{shopify_order['order_id']}:{track_num}"
            items.append(
                {
                    "item_image": item.get("image_src", ""),
                    "item_title": item.get("product_title", ""),
                    "product_id": item.get("product_id"),
                    "variant_id": item.get("variant_id"),
                    "quantity": item.get("quantity", 0),
                    "unit_price": item.get("unit_price", 0),
                    "unit_cost": item.get("unit_cost", 0),
                    "line_total": item.get("line_total", 0),
                    "line_cost_total": item.get("line_cost_total", 0),
                    "tracking_number": track_num,
                    "status": item_status,
                    "applied_status": statuses.get(key, ""),
                }
            )
        if not items:
            continue
        pending_total_price = round(sum(parse_money(item.get("line_total", 0)) for item in items), 2)
        pending_total_cost = round(sum(parse_money(item.get("line_cost_total", 0)) for item in items), 2)
        financial_status = str(shopify_order.get("financial_status", "") or "").strip().lower()
        payment_label = "Pending"
        payment_class = "pending"
        if financial_status in PAID_FINANCIAL_STATUSES:
            payment_label = "Partially Paid" if "partially" in financial_status else "Paid"
            payment_class = "partial" if "partially" in financial_status else "paid"
        all_orders.append(
            {
                "order_via": "Shopify",
                "shopify_id": shopify_order.get("id"),
                "order_link": shopify_order.get("order_link"),
                "order_id": shopify_order.get("order_id"),
                "status": normalize_status_bucket(order_status),
                "tags": filtered_tags,
                "customer_name": (shopify_order.get("customer_details") or {}).get("name", ""),
                "customer_phone": (shopify_order.get("customer_details") or {}).get("phone", ""),
                "customer_address": (shopify_order.get("customer_details") or {}).get("address", ""),
                "customer_city": customer_city,
                "is_lahore": is_lahore_city(customer_city),
                "date": shopify_order.get("created_at", ""),
                "items_list": items,
                "financial_status": shopify_order.get("financial_status", ""),
                "payment_status_label": payment_label,
                "payment_status_class": payment_class,
                "subtotal_price": parse_money(shopify_order.get("subtotal_price", 0)),
                "current_subtotal_price": parse_money(shopify_order.get("current_subtotal_price", shopify_order.get("subtotal_price", 0))),
                "shipping_charges": parse_money(shopify_order.get("shipping_charges", 0)),
                "total_discounts": parse_money(shopify_order.get("total_discounts", 0)),
                "total_price": parse_money(shopify_order.get("total_price", 0)),
                "current_total_price": parse_money(shopify_order.get("current_total_price", shopify_order.get("current_subtotal_price", shopify_order.get("total_price", 0)))),
                "display_total_price": parse_money(shopify_order.get("current_subtotal_price", shopify_order.get("subtotal_price", shopify_order.get("total_price", 0)))),
                "pending_total_price": pending_total_price,
                "pending_total_cost": pending_total_cost,
            }
        )

    all_orders.sort(key=lambda order: parse_date_for_sort(order.get("date")), reverse=True)
    return all_orders


def build_pending_items_table_data():
    pending_items = {}
    all_orders = []
    paid_pending_value = 0.0
    unpaid_pending_value = 0.0
    total_items_cost = 0.0
    for order in build_pending_orders_mobile_data():
        all_orders.append(order)
        financial_status = str(order.get("financial_status", "") or "").strip().lower()
        pending_value = parse_money(order.get("pending_total_price", 0))
        pending_cost = parse_money(order.get("pending_total_cost", 0))
        total_items_cost += pending_cost
        if financial_status in PAID_FINANCIAL_STATUSES:
            paid_pending_value += pending_value
        else:
            unpaid_pending_value += pending_value
        for item in order.get("items_list", []):
            product_title = item["item_title"]
            quantity = int(item.get("quantity") or 0)
            item_image = item.get("item_image", "")
            key = product_title
            if key not in pending_items:
                pending_items[key] = {
                    "item_image": item_image,
                    "item_title": product_title,
                    "product_id": item.get("product_id"),
                    "variant_id": item.get("variant_id"),
                    "unit_price": parse_money(item.get("unit_price", 0)),
                    "unit_cost": parse_money(item.get("unit_cost", 0)),
                    "quantity": 0,
                    "total_price": 0.0,
                    "total_cost": 0.0,
                    "statuses": {},
                }
            pending_items[key]["quantity"] += quantity
            pending_items[key]["total_price"] += parse_money(item.get("line_total", 0))
            pending_items[key]["total_cost"] += parse_money(item.get("line_cost_total", 0))
            status = item.get("status", "")
            pending_items[key]["statuses"][status] = pending_items[key]["statuses"].get(status, 0) + quantity
    pending_items_sorted = sorted(pending_items.values(), key=lambda item: item["quantity"], reverse=True)
    for item in pending_items_sorted:
        item["total_price"] = round(parse_money(item.get("total_price", 0)), 2)
        item["total_cost"] = round(parse_money(item.get("total_cost", 0)), 2)
    summary = {
        "paid_pending_value": round(paid_pending_value, 2),
        "unpaid_pending_value": round(unpaid_pending_value, 2),
        "total_items_cost": round(total_items_cost, 2),
    }
    return all_orders, pending_items_sorted, summary


def build_employee_approval_items():
    approvals = []
    statuses = load_order_statuses()
    approval_statuses = {"Delivered in Lahore", "Cancelled by Employee"}
    for shopify_order in order_details:
        customer = shopify_order.get("customer_details") or {}
        for item in shopify_order.get("line_items", []):
            tracking_number = item.get("tracking_number", "N/A")
            key = f"{shopify_order['order_id']}:{tracking_number}"
            applied_status = statuses.get(key, "")
            if applied_status not in approval_statuses:
                continue
            approvals.append(
                {
                    "shopify_id": shopify_order.get("id"),
                    "order_id": shopify_order.get("order_id"),
                    "tracking_number": tracking_number,
                    "requested_status": applied_status,
                    "item_title": item.get("product_title", ""),
                    "item_image": item.get("image_src", ""),
                    "quantity": item.get("quantity", 0),
                    "customer_name": customer.get("name") or item.get("name") or "",
                    "customer_city": customer.get("city") or item.get("city") or "",
                    "customer_phone": customer.get("phone") or item.get("phone") or "",
                    "total_price": shopify_order.get("total_price", 0),
                    "date": shopify_order.get("created_at", ""),
                    "tags": shopify_order.get("tags", []),
                }
            )
    approvals.sort(key=lambda item: parse_date_for_sort(item.get("date")), reverse=True)
    return approvals


def find_employee_portal_order(term):
    normalized = normalize_scan_term(term)
    if not normalized:
        return None
    for order in build_employee_portal_orders():
        if normalize_scan_term(order.get("order_id")) == normalized:
            return order
        for item in order.get("items", []):
            if normalize_scan_term(item.get("tracking_number")) == normalized:
                return order
    return None


def apply_shopify_order_tag(order_id, tag, include_date=False):
    order = shopify.Order.find(order_id)
    tags = [t.strip() for t in order.tags.split(",")] if getattr(order, "tags", "") else []
    clean_tag = tag.strip()
    if include_date:
        clean_tag = f"{clean_tag} ({datetime.now().strftime('%Y-%m-%d')})"
    if clean_tag not in tags:
        tags.append(clean_tag)
    order.tags = ", ".join(tags)
    return order.save()


def mark_shopify_order_as_paid(order_id):
    token = get_graphql_token()
    endpoint = get_graphql_endpoint()
    if not token or not endpoint:
        raise RuntimeError("Shopify GraphQL payment auth is not configured.")

    mutation = """
    mutation MarkOrderAsPaid($input: OrderMarkAsPaidInput!) {
      orderMarkAsPaid(input: $input) {
        order {
          id
          displayFinancialStatus
        }
        userErrors {
          field
          message
        }
      }
    }
    """
    variables = {"input": {"id": f"gid://shopify/Order/{int(order_id)}"}}
    response = requests.post(
        endpoint,
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token},
        json={"query": mutation, "variables": variables},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    errors = payload.get("errors") or []
    if errors:
        raise RuntimeError("; ".join(error.get("message", "Unknown Shopify GraphQL error") for error in errors))
    result = (payload.get("data") or {}).get("orderMarkAsPaid") or {}
    user_errors = result.get("userErrors") or []
    if user_errors:
        raise RuntimeError("; ".join(error.get("message", "Unknown Shopify user error") for error in user_errors))
    return result.get("order") or {}


def archive_shopify_order(order_id):
    order = shopify.Order.find(order_id)
    result = order.close()
    if result is False:
        raise RuntimeError("Shopify order close returned false.")
    return True


def normalize_order_reference(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def get_alkaramat_paid_aabroo_order_ids():
    url = (
        os.getenv("ALKARAMAT_AABROO_PAID_ORDERS_URL")
        or "https://dashboard.alkaramat.com/aabroo-orders/paid-order-ids"
    ).strip()
    if not url:
        return set()
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if payload.get("success") is False:
        raise RuntimeError(payload.get("error") or "Al Karamat paid-order lookup failed.")
    return {
        normalize_order_reference(order_id)
        for order_id in payload.get("paid_order_ids", [])
        if normalize_order_reference(order_id)
    }


def find_cached_order_by_shopify_id(order_id):
    normalized = str(order_id or "").strip()
    for order in order_details:
        if str(order.get("id") or "").strip() == normalized:
            return order
    return None


def mark_paid_and_archive_order(order_id, paid_ledger_order_ids=None, require_ledger_paid=True):
    cached_order = find_cached_order_by_shopify_id(order_id)
    if not cached_order:
        return {"success": False, "order_id": str(order_id), "error": "Order is not loaded in the active dashboard cache."}

    order_name = str(cached_order.get("order_id") or "").strip()
    if require_ledger_paid:
        paid_ledger_order_ids = paid_ledger_order_ids if paid_ledger_order_ids is not None else get_alkaramat_paid_aabroo_order_ids()
        if normalize_order_reference(order_name) not in paid_ledger_order_ids:
            return {
                "success": False,
                "order_id": str(order_id),
                "order_name": order_name,
                "skipped": True,
                "error": "Not marked Paid in Al Karamat ledger.",
            }

    warnings = []
    financial_status = str(cached_order.get("financial_status") or "").strip().lower()
    if financial_status not in PAID_FINANCIAL_STATUSES:
        try:
            mark_shopify_order_as_paid(order_id)
        except Exception as error:
            warnings.append(f"Could not mark paid: {error}")

    try:
        archive_shopify_order(order_id)
    except Exception as error:
        warnings.append(f"Could not archive: {error}")

    if warnings:
        return {
            "success": False,
            "order_id": str(order_id),
            "order_name": order_name,
            "error": " ".join(warnings),
        }

    order_details[:] = [order for order in order_details if str(order.get("id") or "").strip() != str(order_id).strip()]
    return {"success": True, "order_id": str(order_id), "order_name": order_name}


def capture_shopify_payment(order):
    financial_status = ((getattr(order, "financial_status", "") or "")).lower()
    if financial_status in {"paid", "partially_paid"}:
        return []
    response = requests.get(
        f"{shopify_rest_base_url()}/orders/{order.id}/transactions.json",
        headers=shopify_rest_headers(),
        timeout=30,
    )
    response.raise_for_status()
    transactions = response.json().get("transactions", [])
    authorization = next(
        (
            transaction
            for transaction in transactions
            if transaction.get("kind") == "authorization" and transaction.get("status") == "success"
        ),
        None,
    )
    if not authorization:
        return ["No capturable authorization transaction found."]
    payload = {
        "transaction": {
            "kind": "capture",
            "parent_id": authorization["id"],
            "amount": str(getattr(order, "total_price", "")),
            "currency": authorization.get("currency") or getattr(order, "currency", "PKR") or "PKR",
        }
    }
    capture_response = requests.post(
        f"{shopify_rest_base_url()}/orders/{order.id}/transactions.json",
        headers=shopify_rest_headers(),
        json=payload,
        timeout=30,
    )
    capture_response.raise_for_status()
    return []


def fulfill_shopify_order(order):
    fulfillment_status = ((getattr(order, "fulfillment_status", "") or "")).lower()
    if fulfillment_status == "fulfilled":
        return []
    response = requests.get(
        f"{shopify_rest_base_url()}/orders/{order.id}/fulfillment_orders.json",
        headers=shopify_rest_headers(),
        timeout=30,
    )
    response.raise_for_status()
    fulfillment_orders = response.json().get("fulfillment_orders", [])
    open_fulfillment_orders = [
        {"fulfillment_order_id": fulfillment_order["id"]}
        for fulfillment_order in fulfillment_orders
        if fulfillment_order.get("status") not in {"closed", "cancelled", "incomplete"}
    ]
    if not open_fulfillment_orders:
        return ["No open fulfillment orders found."]
    payload = {
        "fulfillment": {
            "notify_customer": False,
            "line_items_by_fulfillment_order": open_fulfillment_orders,
        }
    }
    fulfillment_response = requests.post(
        f"{shopify_rest_base_url()}/fulfillments.json",
        headers=shopify_rest_headers(),
        json=payload,
        timeout=30,
    )
    fulfillment_response.raise_for_status()
    return []


def approve_shopify_delivery(order):
    warnings = []
    try:
        mark_shopify_order_as_paid(order.id)
    except Exception as error:
        try:
            warnings.extend(capture_shopify_payment(order))
        except Exception as capture_error:
            warnings.append(f"Could not mark order as paid: {error}")
            warnings.append(f"Could not capture payment: {capture_error}")
    try:
        warnings.extend(fulfill_shopify_order(order))
    except Exception as error:
        warnings.append(f"Could not create fulfillment: {error}")
    try:
        result = order.close()
        if result is False:
            warnings.append("Shopify order close returned false.")
    except Exception as error:
        warnings.append(f"Could not close Shopify order: {error}")
    try:
        if apply_shopify_order_tag(order.id, "Delivered in Lahore Approved", include_date=True) is False:
            warnings.append("Could not save approval tag.")
    except Exception as error:
        warnings.append(f"Could not save approval tag: {error}")
    return warnings


def approve_shopify_cancellation(order):
    warnings = []
    try:
        result = order.cancel()
        if result is False:
            warnings.append("Shopify order cancel returned false.")
    except Exception as error:
        warnings.append(f"Could not cancel Shopify order: {error}")
    try:
        if apply_shopify_order_tag(order.id, "Cancelled by Employee", include_date=True) is False:
            warnings.append("Could not save cancellation tag.")
    except Exception as error:
        warnings.append(f"Could not save cancellation tag: {error}")
    return warnings


def format_employee_order_note(payment_method, delivery_method, customer_phone, discount_amount, delivery_charges, advance_amount, custom_items, extra_notes=""):
    lines = [
        "Created from Aabroo employee portal.",
        f"Payment method: {payment_method or 'Not specified'}",
        f"Delivery method: {delivery_method or 'Not specified'}",
        f"Phone: {customer_phone or 'Not provided'}",
    ]
    if discount_amount:
        lines.append(f"Discount amount: PKR {discount_amount}")
    if delivery_charges:
        lines.append(f"Delivery charges: PKR {delivery_charges}")
    if advance_amount:
        lines.append(f"Advance paid by customer: PKR {advance_amount}")
    if custom_items:
        lines.append("Custom items:")
        for item in custom_items:
            lines.append(
                f"- {item.get('title', 'Custom item')} | Qty {item.get('quantity', 1)} | "
                f"PKR {item.get('price', 0)} | Image {'Uploaded in portal' if item.get('image') else 'N/A'}"
            )
    if extra_notes:
        lines.append(f"Notes: {extra_notes}")
    return "\n".join(lines)


def get_active_shopify_products(limit=250):
    cost_overrides = load_product_cost_overrides()
    try:
        products = shopify.Product.find(limit=limit, published_status="published")
    except Exception as error:
        print(f"Could not fetch Shopify products: {error}")
        return []

    results = []
    inventory_ids = []
    while True:
        for product in products:
            if getattr(product, "status", "active") != "active":
                continue
            base_image = product.image.src if getattr(product, "image", None) else ""
            product_images = list(getattr(product, "images", []) or [])
            fetched_images = False
            for variant in getattr(product, "variants", []) or []:
                variant_title = getattr(variant, "title", "") or ""
                display_title = product.title if variant_title in {"Default Title", ""} else f"{product.title} - {variant_title}"
                variant_image = base_image
                variant_image_id = getattr(variant, "image_id", None)
                if variant_image_id and not product_images and not fetched_images:
                    try:
                        product_images = list(shopify.Image.find(product_id=getattr(product, "id", None)) or [])
                    except Exception:
                        product_images = []
                    fetched_images = True
                if variant_image_id and product_images:
                    for image in product_images:
                        if getattr(image, "id", None) == variant_image_id:
                            variant_image = getattr(image, "src", "") or base_image
                            break
                        attached_variant_ids = getattr(image, "variant_ids", None) or []
                        if getattr(variant, "id", None) in attached_variant_ids:
                            variant_image = getattr(image, "src", "") or base_image
                            break
                results.append(
                    {
                        "product_id": getattr(product, "id", None),
                        "variant_id": getattr(variant, "id", None),
                        "inventory_item_id": getattr(variant, "inventory_item_id", None),
                        "title": display_title,
                        "product_title": getattr(product, "title", ""),
                        "variant_title": variant_title,
                        "price": float(getattr(variant, "price", 0) or 0),
                        "cost": 0,
                        "image": variant_image,
                        "sku": getattr(variant, "sku", "") or "",
                    }
                )
                if getattr(variant, "inventory_item_id", None):
                    inventory_ids.append(str(getattr(variant, "inventory_item_id", None)))
        try:
            if not products.has_next_page():
                break
            products = products.next_page()
        except Exception as error:
            print(f"Could not load next Shopify product page: {error}")
            break

    cost_map = {}
    if inventory_ids:
        try:
            cost_map = fetch_shopify_inventory_item_costs(inventory_ids)
        except Exception as error:
            print(f"Could not fetch Shopify product costs: {error}")

    for product in results:
        inventory_id = str(product.get("inventory_item_id") or "").strip()
        fallback_cost = get_cost_override_for_item(
            cost_overrides,
            product_id=product.get("product_id"),
            variant_id=product.get("variant_id"),
            title=product.get("title", ""),
        )
        product["cost"] = cost_map.get(inventory_id, fallback_cost) if inventory_id else fallback_cost
    return results


def build_product_cost_rows(limit=250):
    rows = []
    for product in get_active_shopify_products(limit=limit):
        rows.append(
            {
                "product_id": product.get("product_id"),
                "variant_id": product.get("variant_id"),
                "inventory_item_id": product.get("inventory_item_id"),
                "title": product.get("title", ""),
                "product_title": product.get("product_title", ""),
                "variant_title": product.get("variant_title", ""),
                "sku": product.get("sku", ""),
                "image": product.get("image", ""),
                "price": parse_money(product.get("price", 0)),
                "cost": parse_money(product.get("cost", 0)),
            }
        )
    return sorted(rows, key=lambda row: str(row.get("title", "")).lower())


def build_employee_invoice_payload(order_name, customer_name, phone, city, address, payment_method, delivery_method, catalog_items, custom_items, discount_amount, delivery_charges, advance_amount):
    items = []
    subtotal = 0.0

    for item in catalog_items:
        quantity = int(item.get("quantity") or 1)
        unit_price = parse_money(item.get("price"))
        line_total = round(unit_price * quantity, 2)
        subtotal += line_total
        items.append(
            {
                "title": item.get("title") or "Product",
                "quantity": quantity,
                "image": item.get("image") or "",
                "unit_price": unit_price,
                "line_total": line_total,
            }
        )

    for item in custom_items:
        quantity = int(item.get("quantity") or 1)
        unit_price = parse_money(item.get("price"))
        line_total = round(unit_price * quantity, 2)
        subtotal += line_total
        items.append(
            {
                "title": item.get("title") or "Custom product",
                "quantity": quantity,
                "image": item.get("image") or "",
                "unit_price": unit_price,
                "line_total": line_total,
            }
        )

    total = round(subtotal - discount_amount + delivery_charges, 2)
    balance_due = round(max(total - advance_amount, 0), 2)
    return {
        "order_id": order_name,
        "customer_name": customer_name,
        "customer_phone": phone,
        "customer_city": city,
        "customer_address": address,
        "status": "Created",
        "summary_lines": [
            "Shopify order",
            f"Payment: {payment_method or 'Not specified'}",
            f"Delivery: {delivery_method or 'Not specified'}",
        ],
        "items": items,
        "totals": {
            "subtotal": round(subtotal, 2),
            "discount": round(discount_amount, 2),
            "delivery_charges": round(delivery_charges, 2),
            "total": round(total, 2),
            "advance_paid": round(advance_amount, 2),
            "balance_due": round(balance_due, 2),
        },
    }


def create_shopify_employee_order(payload):
    customer_name = (payload.get("customer_name") or "").strip()
    phone = (payload.get("phone") or "").strip()
    city = (payload.get("city") or "").strip()
    address = (payload.get("address") or "").strip()
    discount_amount = parse_money(payload.get("discount_amount"))
    delivery_charges = parse_money(payload.get("delivery_charges"))
    payment_method = (payload.get("payment_method") or "").strip()
    delivery_method = (payload.get("delivery_method") or "").strip()
    advance_amount = parse_money(payload.get("advance_amount"))
    catalog_items = payload.get("catalog_items") or []
    custom_items = payload.get("custom_items") or []
    extra_notes = (payload.get("notes") or "").strip()

    if not customer_name:
        raise ValueError("Customer name is required.")
    if not phone:
        raise ValueError("Phone number is required.")
    if payment_method.lower() == "partial" and advance_amount <= 0:
        raise ValueError("Enter the advance paid amount for partial payment.")

    first_name, last_name = split_customer_name(customer_name)
    line_items = []

    for item in catalog_items:
        variant_id = item.get("variant_id")
        quantity = int(item.get("quantity") or 1)
        if not variant_id or quantity < 1:
            continue
        line_item = {"variant_id": int(variant_id), "quantity": quantity}
        override_price = parse_money(item.get("price"))
        if override_price > 0:
            line_item["original_unit_price"] = override_price
        line_items.append(line_item)

    normalized_custom_items = []
    for item in custom_items:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        price = parse_money(item.get("price"))
        quantity = int(item.get("quantity") or 1)
        normalized_custom_items.append(
            {
                "title": title,
                "price": price,
                "quantity": quantity,
                "image": (item.get("image") or "").strip(),
            }
        )
        line_items.append(
            {"title": title, "original_unit_price": price, "quantity": quantity}
        )

    if not line_items:
        raise ValueError("At least one product is required.")

    estimated_total = 0.0
    for item in catalog_items:
        estimated_total += parse_money(item.get("price")) * int(item.get("quantity") or 1)
    for item in normalized_custom_items:
        estimated_total += parse_money(item.get("price")) * int(item.get("quantity") or 1)
    estimated_total = round(estimated_total - discount_amount + delivery_charges, 2)
    if advance_amount > estimated_total:
        raise ValueError("Advance paid cannot be greater than the order total.")

    note = format_employee_order_note(
        payment_method=payment_method,
        delivery_method=delivery_method,
        customer_phone=phone,
        discount_amount=discount_amount,
        delivery_charges=delivery_charges,
        advance_amount=advance_amount,
        custom_items=normalized_custom_items,
        extra_notes=extra_notes,
    )

    draft_order = shopify.DraftOrder()
    draft_order.line_items = line_items
    draft_order.note = note
    draft_order.tags = "Employee Portal"
    draft_order.use_customer_default_address = False
    draft_order.shipping_address = {
        "first_name": first_name,
        "last_name": last_name or "Customer",
        "phone": phone,
        "address1": address,
        "city": city,
        "country": "Pakistan",
    }
    draft_order.billing_address = draft_order.shipping_address
    draft_order.customer = {
        "first_name": first_name,
        "last_name": last_name or "Customer",
        "phone": phone,
    }

    if discount_amount > 0:
        draft_order.applied_discount = {
            "description": "Employee portal discount",
            "value_type": "fixed_amount",
            "value": discount_amount,
            "amount": discount_amount,
            "title": "Employee portal discount",
        }

    if delivery_charges > 0:
        draft_order.shipping_line = {
            "title": "Delivery Charges",
            "price": delivery_charges,
            "custom": True,
        }

    if not draft_order.save():
        raise RuntimeError(json.dumps(getattr(draft_order, "errors", {}) or {"error": "Could not save draft order"}))

    complete_params = {}
    if payment_method.lower() == "partial":
        complete_params["payment_pending"] = True
    try:
        draft_order.complete(complete_params)
    except Exception as error:
        raise RuntimeError(f"Shopify could not complete the employee order: {error or getattr(draft_order, 'errors', None) or 'Unknown completion error'}")

    try:
        refreshed_draft_order = shopify.DraftOrder.find(draft_order.id)
    except Exception:
        refreshed_draft_order = draft_order

    order_id = getattr(refreshed_draft_order, "order_id", None) or getattr(draft_order, "order_id", None)
    order_name = getattr(refreshed_draft_order, "name", "") or getattr(draft_order, "name", "") or ""
    if not order_id:
        raise RuntimeError("Shopify created the draft, but the completed order ID did not come back. Please check Draft Orders in Shopify.")

    if payment_method.lower() == "full":
        try:
            mark_shopify_order_as_paid(order_id)
        except Exception as error:
            print(f"Could not immediately mark employee order {order_id} as paid: {error}")

    invoice_payload = build_employee_invoice_payload(
        order_name=order_name,
        customer_name=customer_name,
        phone=phone,
        city=city,
        address=address,
        payment_method=payment_method,
        delivery_method=delivery_method,
        catalog_items=catalog_items,
        custom_items=normalized_custom_items,
        discount_amount=discount_amount,
        delivery_charges=delivery_charges,
        advance_amount=advance_amount if payment_method.lower() == "partial" else 0,
    )
    return {
        "draft_order_id": getattr(draft_order, "id", None),
        "order_id": order_id,
        "order_name": order_name,
        "invoice": invoice_payload,
    }


def verify_shopify_webhook(req):
    shopify_hmac = req.headers.get("X-Shopify-Hmac-Sha256")
    data = req.get_data()
    secret = os.getenv("SHOPIFY_WEBHOOK_SECRET") or os.getenv("SHOPIFY_GRAPHQL_CLIENT_SECRET")
    if not secret:
        raise ValueError("SHOPIFY_WEBHOOK_SECRET is not set.")
    digest = hmac.new(secret.encode("utf-8"), data, hashlib.sha256).digest()
    computed_hmac = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed_hmac, shopify_hmac or "")


def build_admin_mobile_sections():
    return [
        {"id": "dashboard", "label": "Dashboard", "icon": "🏠", "src": "/?embedded=1"},
        {
            "id": "alkaramat-aabroo",
            "label": "Aabroo Ledger",
            "icon": "🏪",
            "src": "https://dashboard.alkaramat.com/aabroo-portal",
        },
        {"id": "scanner", "label": "Scanner", "icon": "🔍", "src": "/employee_portal"},
        {"id": "employee-orders", "label": "Orders", "icon": "🧾", "src": "/employee_portal/orders"},
        {"id": "postex-payments", "label": "PostEx Payments", "icon": "💰", "src": "/postex-payments?embedded=1"},
        {"id": "pending", "label": "Pending", "icon": "📋", "src": "/pending?embedded=1"},
        {"id": "accounts", "label": "Accounts", "icon": "💼", "src": "/accounts?embedded=1"},
        {"id": "undelivered", "label": "Undelivered", "icon": "🚚", "src": "/undelivered?embedded=1"},
    ]


@app.route("/send-email", methods=["POST"])
def send_email():
    data = request.get_json() or {}
    to_emails = data.get("to", [])
    cc_emails = data.get("cc", [])
    subject = data.get("subject", "")
    body = data.get("body", "")
    try:
        smtp_server = "smtp.gmail.com"
        smtp_port = 587
        smtp_user = os.getenv("SMTP_USER")
        smtp_password = os.getenv("SMTP_PASSWORD")
        msg = MIMEText(body)
        msg["From"] = smtp_user
        msg["To"] = ", ".join(to_emails)
        msg["Cc"] = ", ".join(cc_emails)
        msg["Subject"] = subject

        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(smtp_user, to_emails + cc_emails, msg.as_string())
        server.quit()
        return jsonify({"message": "Email sent successfully"})
    except Exception as error:
        return jsonify({"error": str(error)}), 500


@app.route("/generate_loadsheet", methods=["POST"])
def generate_loadsheet():
    data = request.get_json() or {}
    cn_numbers = data.get("cn_numbers", [])
    if not cn_numbers:
        return jsonify({"error": "No CN numbers provided"}), 400

    payload = {
        "api_key": os.getenv("LEOPARD_API_KEY"),
        "api_password": os.getenv("LEOPARD_PASSWORD"),
        "cn_numbers": cn_numbers,
        "courier_name": "1",
        "courier_code": "1",
    }
    try:
        response = requests.post(
            "https://merchantapi.leopardscourier.com/api/generateLoadSheet/",
            json=payload,
            timeout=30,
        )
        return jsonify(response.json())
    except requests.RequestException as error:
        return jsonify({"error": f"Failed to connect to the API: {error}"}), 500


@app.route("/apply_tag", methods=["POST"])
def apply_tag():
    data = request.get_json() or {}
    order_id = data.get("order_id")
    tag = (data.get("tag") or "").strip()
    if not order_id or not tag:
        return jsonify({"success": False, "error": "order_id and tag are required."}), 400
    try:
        order = shopify.Order.find(order_id)
        if tag.lower() == "returned":
            try:
                order.cancel()
            except Exception:
                pass
        if tag.lower() == "delivered":
            try:
                order.close()
            except Exception:
                pass
        if apply_shopify_order_tag(order_id, tag, include_date=True):
            return jsonify({"success": True, "message": "Tag applied successfully."})
        return jsonify({"success": False, "error": "Failed to save order changes."}), 500
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/mark_paid_archive", methods=["POST"])
def mark_paid_archive():
    data = request.get_json() or {}
    order_ids = data.get("order_ids")
    if order_ids is None:
        order_ids = [data.get("order_id")]
    order_ids = [str(order_id).strip() for order_id in order_ids if str(order_id or "").strip()]
    if not order_ids:
        return jsonify({"success": False, "error": "No order IDs provided."}), 400

    try:
        paid_ledger_order_ids = get_alkaramat_paid_aabroo_order_ids()
    except Exception as error:
        return jsonify({"success": False, "error": f"Could not read Al Karamat paid ledger: {error}"}), 502

    results = [
        mark_paid_and_archive_order(order_id, paid_ledger_order_ids=paid_ledger_order_ids, require_ledger_paid=True)
        for order_id in order_ids
    ]
    marked = [result for result in results if result.get("success")]
    skipped = [result for result in results if result.get("skipped")]
    failed = [result for result in results if not result.get("success") and not result.get("skipped")]
    return jsonify(
        {
            "success": not failed and bool(marked),
            "marked_order_ids": [result.get("order_id") for result in marked],
            "marked_order_names": [result.get("order_name") for result in marked],
            "skipped": skipped,
            "failed": failed,
            "message": f"Marked paid and archived {len(marked)} order(s). Skipped {len(skipped)} not paid in Al Karamat.",
        }
    ), 207 if failed else 200


@app.route("/")
def tracking():
    refill_missing_customer_details(order_details)
    repair_order_customer_cache(order_details)
    refresh_active_tracking_for_dashboard()
    repair_order_customer_cache(order_details)
    return render_template(
        "track.html",
        order_details=order_details,
        darazOrders=[],
        employee_approvals=build_employee_approval_items(),
    )


@app.route("/refresh", methods=["POST"])
def refresh_data():
    try:
        refreshed_all = reload_orders(preserve_existing_on_partial=True)
        if request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json:
            message = "Data refreshed successfully" if refreshed_all else "Tracking refreshed; existing order cache preserved"
            return jsonify({"message": message})
        return render_template(
            "track.html",
            order_details=order_details,
            darazOrders=[],
            employee_approvals=build_employee_approval_items(),
        )
    except Exception as error:
        return jsonify({"message": f"Failed to refresh data: {error}"}), 500


@app.route("/track/<tracking_num>")
def display_tracking(tracking_num):
    async def run_lookup():
        async with aiohttp.ClientSession() as session_obj:
            if is_digidokaan_tracking(tracking_num):
                return await fetch_digidokaan_tracking_history(session_obj, tracking_num)
            return await fetch_tracking_data(session_obj, tracking_num)

    data = asyncio.run(run_lookup())
    summary = summarize_tracking_result(tracking_num, data)
    remember_tracking_summary(tracking_num, summary)
    matched_order = find_shopify_order_by_tracking_number(tracking_num)
    return render_template("trackingdata.html", data=data, tracking_number=tracking_num, matched_order=matched_order)


@app.route("/tracking-summary/<tracking_num>")
def tracking_summary(tracking_num):
    try:
        return jsonify({"success": True, "tracking": build_tracking_summary_payload(tracking_num)})
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/pending")
def pending_orders():
    all_orders, pending_items, summary = build_pending_items_table_data()
    return render_template("pending.html", all_orders=all_orders, pending_items=pending_items, summary=summary)


@app.route("/postex-payments")
def postex_payments():
    if not admin_portal_is_authenticated():
        return redirect(url_for("admin_portal", section="postex-payments"))

    today = datetime.now().date()
    default_start = today - timedelta(days=60)
    start_date = parse_iso_date(request.args.get("startDate") or request.args.get("start_date"), default_start)
    end_date = parse_iso_date(request.args.get("endDate") or request.args.get("end_date"), today)
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    try:
        order_status_id = int(request.args.get("orderStatusId") or request.args.get("order_status_id") or 0)
    except (TypeError, ValueError):
        order_status_id = 0
    if order_status_id not in POSTEX_ORDER_STATUSES:
        order_status_id = 0

    error_message = ""
    data = {
        "rows": [],
        "summary": {
            "orders": 0,
            "invoice_payment": 0.0,
            "shipping_fee": 0.0,
            "delivered_tax": 0.0,
            "net_receivable": 0.0,
            "pending_receivable": 0.0,
            "paid_receivable": 0.0,
            "undelivered_amount": 0.0,
            "pending_payable": 0.0,
            "paid_payable": 0.0,
        },
        "statuses": POSTEX_ORDER_STATUSES,
        "selected_status": order_status_id,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "token_configured": bool(get_postex_token()),
    }
    try:
        if data["token_configured"]:
            data = build_postex_payments_page_data(order_status_id, start_date, end_date)
        else:
            error_message = "POSTEX_TOKEN is not configured."
    except Exception as error:
        error_message = str(error)

    return render_template("postex_payments.html", data=data, error_message=error_message)


@app.route("/product-costs")
def product_costs():
    return render_template("product_costs.html", products=build_product_cost_rows())


@app.route("/product-costs/update", methods=["POST"])
def update_product_costs():
    data = request.get_json() or {}
    product_id = data.get("product_id")
    variant_id = data.get("variant_id")
    inventory_item_id = data.get("inventory_item_id")
    title = (data.get("title") or "").strip()
    submitted_price = parse_money(data.get("price", 0))
    original_price = parse_money(data.get("original_price", submitted_price))
    submitted_cost = parse_money(data.get("cost", 0))

    if not variant_id and not product_id and not title:
        return jsonify({"success": False, "error": "Product identity is required."}), 400

    try:
        if variant_id and round(original_price, 2) != round(submitted_price, 2):
            current_product = None
            current_product = shopify.Variant.find(int(variant_id))
            current_product.price = submitted_price
            if not current_product.save():
                raise RuntimeError("Shopify price update failed.")

        if inventory_item_id:
            update_shopify_inventory_item_cost(inventory_item_id, submitted_cost)

        overrides = load_product_cost_overrides()
        set_cost_override(
            overrides,
            product_id=product_id,
            variant_id=variant_id,
            title=title,
            price=submitted_price,
            cost=submitted_cost,
        )
        if not save_product_cost_overrides(overrides):
            raise RuntimeError("Could not save cost override.")

        reload_orders()
        return jsonify({"success": True, "price": submitted_price, "cost": submitted_cost})
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/accounts", methods=["GET", "POST"])
def accounts():
    if not admin_portal_is_authenticated():
        return redirect(url_for("admin_portal", section="accounts"))

    notice = ""
    error_message = ""
    if request.method == "POST":
        try:
            create_account_transaction(request.form)
            return redirect(url_for("accounts", saved="1"))
        except Exception as error:
            error_message = str(error)
    elif request.args.get("saved") == "1":
        notice = "Transaction saved."

    data = get_accounts_page_data()
    if data is None:
        error_message = error_message or "Accounts database is not connected yet. Add Railway Postgres DATABASE_URL to enable this page."
        data = {
            "accounts": [],
            "categories": {"income": [], "expense": []},
            "transactions": [],
            "balances": [],
            "summary": {},
            "monthly": [],
            "category_breakdown": [],
            "category_breakdown_by_kind": {"income": [], "expense": []},
            "agha_ledger": {"balance": 0, "investment_total": 0, "withdrawal_total": 0, "rows": []},
            "ledgers": [],
        }
    return render_template("accounts.html", data=data, notice=notice, error_message=error_message)


@app.route("/orders")
def pending_orders_mobile():
    return render_template("orders.html", all_orders=build_pending_orders_mobile_data(), employee_portal_mode=False)


@app.route("/undelivered")
def undelivered():
    undelivered_orders = [order for order in order_details if is_undelivered_status(order.get("status"))]
    return render_template("undelivered.html", order_details=undelivered_orders, darazOrders=[])


@app.route("/shopify/protected-data/status")
def protected_data_status():
    return jsonify(get_protected_data_config_status())


@app.route("/shopify/protected-data")
def protected_data_status_alias():
    return redirect(url_for("protected_data_status"))


@app.route("/shopify/install")
def shopify_install():
    state = create_oauth_state()
    session[SHOPIFY_OAUTH_STATE_SESSION_KEY] = state
    return redirect(get_install_url(state))


@app.route("/shopify/callback")
def shopify_callback():
    shop = (request.args.get("shop") or "").strip()
    code = (request.args.get("code") or "").strip()
    state = (request.args.get("state") or "").strip()
    saved_state = session.get(SHOPIFY_OAUTH_STATE_SESSION_KEY, "")
    hmac_valid = verify_oauth_hmac(request.query_string)

    if state != saved_state:
        return jsonify({"success": False, "error": "Invalid Shopify callback state"}), 400
    if not hmac_valid and shop != get_shop_domain():
        return jsonify({"success": False, "error": "Invalid Shopify callback signature"}), 400
    if not shop or not code:
        return jsonify({"success": False, "error": "Missing Shopify callback parameters"}), 400

    try:
        payload = exchange_oauth_code_for_token(shop, code)
        save_offline_token(shop, payload)
        setup_shopify()
        try:
            ensure_required_shopify_webhooks()
        except Exception as webhook_error:
            print(f"Warning: could not ensure Shopify webhooks: {webhook_error}")
        session.pop(SHOPIFY_OAUTH_STATE_SESSION_KEY, None)
        reload_orders()
        return redirect("/shopify/protected-data/status?connected=1")
    except Exception as error:
        return jsonify({"success": False, "error": f"Shopify token exchange failed: {error}"}), 500


def _handle_shopify_order_webhook():
    global order_details
    try:
        if not verify_shopify_webhook(request):
            return jsonify({"error": "Invalid webhook signature"}), 401

        order_data = request.get_json(silent=True)
        if not order_data:
            return jsonify({"message": "Empty payload received. Ignored."}), 200

        order_id = order_data.get("id")
        if not order_id:
            return jsonify({"error": "No order id found in payload"}), 400

        if order_data.get("cancelled_at") or order_data.get("closed_at"):
            order_details = [order for order in order_details if order.get("id") != order_id]
            return jsonify({"success": True, "message": f"Order {order_id} closed and removed"}), 200

        setup_shopify()
        order = shopify.Order.find(order_id)

        async def update_order():
            async with aiohttp.ClientSession() as session_obj:
                return await process_order(session_obj, order)

        updated_order_info = asyncio.run(update_order())
        enrich_orders_with_protected_customer_data([updated_order_info])

        if not should_keep_order_in_active_list(updated_order_info, order):
            order_details = [existing_order for existing_order in order_details if existing_order.get("id") != updated_order_info.get("id")]
            return jsonify({"success": True, "message": f"Order {order_id} no longer belongs in active orders and was removed."})

        for index, existing_order in enumerate(order_details):
            if existing_order.get("id") == updated_order_info.get("id"):
                order_details[index] = updated_order_info
                order_details[:] = sort_orders_newest_first(order_details)
                break
        else:
            order_details.append(updated_order_info)
            order_details[:] = sort_orders_newest_first(order_details)
        return jsonify({"success": True, "message": f"Order {order_id} processed successfully"})
    except Exception as error:
        print(f"Webhook processing error: {error}")
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/shopify/webhook/order_created", methods=["POST"])
def shopify_order_created():
    return _handle_shopify_order_webhook()


@app.route("/shopify/webhook/order_updated", methods=["POST"])
def shopify_order_updated():
    return _handle_shopify_order_webhook()


@app.route("/shopify/webhook/order_cancelled", methods=["POST"])
def shopify_order_cancelled():
    return _handle_shopify_order_webhook()


@app.post("/shopify/webhooks/ensure")
def shopify_webhooks_ensure():
    if not admin_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        return jsonify(ensure_required_shopify_webhooks())
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 502


@app.route("/scan", methods=["GET", "POST"])
def search():
    search_term = (request.args.get("term") or request.form.get("search_term") or "").split(",")[0].strip()
    if not search_term:
        return render_template("scan.html")

    order_found = find_employee_portal_order(search_term)
    if request.method == "POST":
        if order_found:
            order_found = {
                "line_items": [
                    {"product_title": item.get("title"), "quantity": item.get("quantity"), "image_src": item.get("image")}
                    for item in order_found.get("items", [])
                ]
            }
        return render_template("scan.html", search_term=search_term, order_found=order_found)

    return jsonify(order_found if order_found else {"error": "Order not found"}), 200 if order_found else 404


@app.route("/dispatch", methods=["GET"])
def dispatch():
    return jsonify(build_employee_portal_orders())


@app.route("/return", methods=["GET"])
def return_orders():
    return jsonify(build_employee_portal_orders())


@app.route("/update_status", methods=["POST"])
def update_status():
    data = request.get_json() or {}
    order_id = str(data.get("order_id"))
    tracking_number = str(data.get("tracking_number", "N/A"))
    status = str(data.get("status") or "")
    key = f"{order_id}:{tracking_number}"
    upsert_order_status(key, status)
    response_message = f"Status updated to {status} for {order_id} ({tracking_number})"

    if status == "Delivered in Lahore":
        matching_order = find_shopify_order_by_order_name(order_id)
        if matching_order and matching_order.get("id"):
            try:
                if apply_shopify_order_tag(matching_order["id"], "Delivered in Lahore"):
                    local_tags = list(matching_order.get("tags") or [])
                    if "Delivered in Lahore" not in local_tags:
                        local_tags.append("Delivered in Lahore")
                    matching_order["tags"] = local_tags
                    response_message = (
                        f"Status updated to {status} for {order_id} ({tracking_number}). "
                        "Shopify tag applied: Delivered in Lahore."
                    )
            except Exception as error:
                print(f"Could not apply Lahore tag: {error}")

    return jsonify({"message": response_message})


@app.route("/employee_status/approve", methods=["POST"])
def approve_employee_status():
    data = request.get_json() or {}
    order_id = str(data.get("order_id") or "")
    tracking_number = str(data.get("tracking_number") or "N/A")
    requested_status = str(data.get("requested_status") or "").strip()
    key = f"{order_id}:{tracking_number}"

    if requested_status not in {"Delivered in Lahore", "Cancelled by Employee"}:
        return jsonify({"success": False, "error": "Unsupported employee approval status."}), 400

    matching_order = find_shopify_order_by_order_name(order_id)
    if not matching_order or not matching_order.get("id"):
        return jsonify({"success": False, "error": "Shopify order not found."}), 404

    try:
        order = shopify.Order.find(matching_order["id"])
        warnings = []
        if requested_status == "Delivered in Lahore":
            warnings = approve_shopify_delivery(order)
            order_details[:] = [existing_order for existing_order in order_details if existing_order.get("id") != matching_order.get("id")]
        else:
            warnings = approve_shopify_cancellation(order)
            order_details[:] = [existing_order for existing_order in order_details if existing_order.get("id") != matching_order.get("id")]
        delete_order_status(key)
        message = f"Approved {requested_status} for {order_id}."
        if warnings:
            message = f"{message} Warnings: {' '.join(warnings)}"
        return jsonify({"success": True, "message": message, "warnings": warnings})
    except Exception as error:
        return jsonify({"success": False, "error": str(error)}), 500


@app.route("/employee_portal", methods=["GET", "POST"])
def employee_portal():
    next_url = employee_portal_safe_next_url(request.values.get("next"))
    if request.method == "POST":
        submitted_password = (request.form.get("password") or "").strip()
        if submitted_password == EMPLOYEE_PORTAL_PASSWORD:
            session[EMPLOYEE_PORTAL_SESSION_KEY] = True
            return redirect(next_url)
        return render_template("employee_portal.html", view="login", login_error="Wrong password. Try again.", next_url=next_url), 401
    if not employee_portal_is_authenticated():
        return render_template("employee_portal.html", view="login", login_error="", next_url=next_url)
    return render_template("employee_portal.html", view="portal", employee_orders=build_employee_portal_orders())


@app.route("/employee_portal/orders")
def employee_portal_orders():
    if not employee_portal_is_authenticated():
        return redirect(url_for("employee_portal", next="/employee_portal/orders"))
    return render_template("orders.html", all_orders=build_pending_orders_mobile_data(), employee_portal_mode=True)


@app.route("/employee_portal/products")
def employee_portal_products():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    return jsonify({"success": True, "products": get_active_shopify_products()})


@app.route("/employee_portal/create-order", methods=["POST"])
def employee_portal_create_order():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json() or {}
    try:
        result = create_shopify_employee_order(data)
        reload_orders()
        return jsonify(
            {
                "success": True,
                "draft_order_id": result.get("draft_order_id"),
                "order_id": result.get("order_id"),
                "order_name": result.get("order_name"),
                "invoice": result.get("invoice"),
            }
        )
    except Exception as error:
        print(f"Employee order create failed: {error}")
        return jsonify({"success": False, "error": str(error)}), 400


@app.route("/employee_portal/logout", methods=["POST"])
def employee_portal_logout():
    session.pop(EMPLOYEE_PORTAL_SESSION_KEY, None)
    return redirect(url_for("employee_portal"))


@app.route("/employee_portal/updates")
def employee_portal_updates():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    orders = build_employee_portal_orders()
    summaries = [
        {
            "id": f"{order.get('source')}:{order.get('order_id')}",
            "order_id": order.get("order_id"),
            "source": order.get("source"),
            "created_at": order.get("created_at"),
        }
        for order in orders
    ]
    summaries.sort(key=lambda item: parse_date_for_sort(item.get("created_at")), reverse=True)
    return jsonify(
        {
            "success": True,
            "count": len(summaries),
            "order_ids": [item["id"] for item in summaries],
            "latest": summaries[:6],
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )


@app.route("/employee_portal-manifest.webmanifest")
def employee_portal_manifest():
    return send_from_directory("static", "employee-portal.webmanifest", mimetype="application/manifest+json")


@app.route("/employee_portal-sw.js")
def employee_portal_service_worker():
    return send_from_directory("static", "employee-portal-sw.js", mimetype="application/javascript")


@app.route("/employee_portal/report", methods=["POST"])
def employee_portal_report():
    if not employee_portal_is_authenticated():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    data = request.get_json() or {}
    mode = (data.get("mode") or "").strip().lower()
    scanned_orders = data.get("orders") or []
    if mode not in {"dispatch", "return"}:
        return jsonify({"success": False, "error": "Invalid report mode."}), 400
    if not scanned_orders:
        return jsonify({"success": False, "error": "No scanned orders provided."}), 400
    tag_name = "Dispatched" if mode == "dispatch" else "Return Received"
    tagged_count = 0
    seen_shopify_ids = set()
    for entry in scanned_orders:
        if entry.get("source") != "shopify":
            continue
        shopify_id = entry.get("shopify_id")
        if not shopify_id or shopify_id in seen_shopify_ids:
            continue
        seen_shopify_ids.add(shopify_id)
        if apply_shopify_order_tag(shopify_id, tag_name, include_date=True):
            tagged_count += 1
    return jsonify({"success": True, "tagged_count": tagged_count, "skipped_count": len(scanned_orders) - tagged_count, "tag_name": tag_name})


@app.route("/admin_portal", methods=["GET", "POST"])
def admin_portal():
    selected = (request.values.get("section") or "dashboard").strip().lower()
    sections = build_admin_mobile_sections()
    section_ids = {section["id"] for section in sections}
    if selected not in section_ids:
        selected = "dashboard"

    if request.method == "POST":
        submitted_password = (request.form.get("password") or "").strip()
        if submitted_password == ADMIN_PORTAL_PASSWORD:
            session[ADMIN_PORTAL_SESSION_KEY] = True
            return redirect(url_for("admin_portal", section=selected))
        return render_template("admin_portal.html", view="login", login_error="Wrong password. Try again.", sections=sections, selected_section=selected), 401

    if not admin_portal_is_authenticated():
        return render_template("admin_portal.html", view="login", login_error="", sections=sections, selected_section=selected)

    return render_template("admin_portal.html", view="portal", sections=sections, selected_section=selected, employee_approvals=build_employee_approval_items())


@app.route("/admin_portal/logout", methods=["POST"])
def admin_portal_logout():
    session.pop(ADMIN_PORTAL_SESSION_KEY, None)
    return redirect(url_for("admin_portal"))


@app.route("/admin_portal-manifest.webmanifest")
def admin_portal_manifest():
    return send_from_directory("static", "admin-portal.webmanifest", mimetype="application/manifest+json")


@app.route("/admin_portal-sw.js")
def admin_portal_service_worker():
    return send_from_directory("static", "admin-portal-sw.js", mimetype="application/javascript")


def restart_program():
    print("Restarting the program...")
    os.execv(sys.executable, ["python"] + sys.argv)


def check_restart_times():
    target_times = ["10:00", "20:00", "02:00"]
    while True:
        if datetime.now().strftime("%H:%M") in target_times:
            restart_program()
        time.sleep(30)


init_db()
setup_shopify()
try:
    ensure_required_shopify_webhooks()
except Exception as webhook_error:
    print(f"Warning: could not ensure Shopify webhooks at startup: {webhook_error}")
reload_orders()


if __name__ == "__main__":
    restart_thread = threading.Thread(target=check_restart_times, daemon=True)
    restart_thread.start()
    app.run(host="0.0.0.0", port=5001, debug=True)
