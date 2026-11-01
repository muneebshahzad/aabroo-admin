import json
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import psycopg2
import requests
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", "change-me-in-production")
app.config.update(SESSION_COOKIE_SECURE=os.environ.get("FLASK_ENV") == "production", SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


@app.template_filter("format_number")
def format_number(value):
    try:
        return f"{float(value or 0):,.0f}"
    except (TypeError, ValueError):
        return "0"


def money(value):
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def db_connection():
    url = os.environ.get("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
    if not url:
        return None
    return psycopg2.connect(url)


def init_db():
    conn = db_connection()
    if not conn:
        return
    with conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS order_overrides (
            order_id TEXT PRIMARY KEY, amount_received NUMERIC(12,2) NOT NULL DEFAULT 0,
            product_cost NUMERIC(12,2) NOT NULL DEFAULT 0, packaging_cost NUMERIC(12,2) NOT NULL DEFAULT 0,
            delivery_cost NUMERIC(12,2) NOT NULL DEFAULT 0, payment_status TEXT NOT NULL DEFAULT 'Pending',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("""CREATE TABLE IF NOT EXISTS cash_settlements (
            id BIGSERIAL PRIMARY KEY, amount NUMERIC(12,2) NOT NULL, note TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
    conn.close()


def load_overrides():
    conn = db_connection()
    if not conn:
        return {}
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM order_overrides")
        rows = cur.fetchall()
    conn.close()
    return {row["order_id"]: dict(row) for row in rows}


def load_settlements():
    conn = db_connection()
    if not conn:
        return []
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT amount, note, created_at FROM cash_settlements ORDER BY created_at DESC")
        rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def shopify_base():
    domain = os.environ.get("AABROO_SHOP_URL", "").strip()
    if not domain:
        raise RuntimeError("AABROO_SHOP_URL is not configured")
    parsed = urlparse(domain if "://" in domain else f"https://{domain}")
    return f"https://{parsed.netloc or parsed.path}/admin/api/{os.environ.get('AABROO_ADMIN_API_VERSION', '2026-04')}"


def shopify_headers():
    token = os.environ.get("AABROO_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("AABROO_ACCESS_TOKEN is not configured")
    return {"X-Shopify-Access-Token": token, "Accept": "application/json"}


def next_link(header):
    for part in str(header or "").split(","):
        if 'rel="next"' in part:
            return part.split(";", 1)[0].strip().strip("<>")
    return None


def fetch_orders():
    url = f"{shopify_base()}/orders.json"
    params = {"status": "any", "limit": 250, "created_at_min": os.environ.get("AABROO_CREATED_AT_MIN", "2026-01-01T00:00:00+05:00")}
    result = []
    while url:
        response = requests.get(url, headers=shopify_headers(), params=params, timeout=25)
        response.raise_for_status()
        result.extend(response.json().get("orders") or [])
        url, params = next_link(response.headers.get("Link")), None
    return result


def courier_data(order):
    notes = {str(x.get("name") or ""): x.get("value") for x in order.get("note_attributes") or []}
    raw = str(notes.get("hxs_courier_status") or "").strip(); upper = raw.upper()
    if order.get("cancelled_at"):
        status = "Returned" if notes.get("hxs_courier_tracking") else "Cancelled"
    elif "DELIVER" in upper and "UNDELIVER" not in upper:
        status = "Delivered"
    elif any(word in upper for word in ("RETURN", "UNDELIVER", "REFUS")):
        status = "Returned"
    else:
        status = "Other status" if raw else "Inprocess"
    return status, raw or "Courier status not available yet", notes


def portal_data():
    try:
        source_orders, error = fetch_orders(), ""
    except Exception as exc:
        source_orders, error = [], str(exc)
    overrides = load_overrides(); orders = []
    domain = os.environ.get("AABROO_SHOP_URL", ""); shop = (urlparse(domain if "://" in domain else f"https://{domain}").hostname or "").split(".")[0] if domain else ""
    for source in source_orders:
        order_id = str(source.get("name") or f"#{source.get('order_number', '')}"); saved = overrides.get(order_id) or {}
        delivery, detail, notes = courier_data(source); fulfillment = str(source.get("fulfillment_status") or "unfulfilled").lower(); fulfilled = fulfillment in {"fulfilled", "partial"}
        items = [{"title": x.get("title") or "Product", "qty": int(x.get("current_quantity", x.get("quantity", 0)) or 0), "line_cost": 0.0} for x in source.get("line_items") or []]
        product_cost = money(saved.get("product_cost")); packaging = money(saved.get("packaging_cost")); delivery_cost = money(saved.get("delivery_cost")); received = money(saved.get("amount_received")); payment = saved.get("payment_status") or ("Not Payable" if delivery == "Cancelled" else "Pending")
        address = source.get("shipping_address") or source.get("billing_address") or {}; customer = source.get("customer") or {}
        orders.append({"order_id": order_id, "shopify_link": f"https://admin.shopify.com/store/{shop}/orders/{source.get('id')}" if shop else "#", "created_at": source.get("created_at", ""),
          "customer_name": address.get("name") or f"{customer.get('first_name','')} {customer.get('last_name','')}".strip(), "customer_phone": address.get("phone") or customer.get("phone") or "", "customer_city": address.get("city") or "",
          "fulfillment_status": "Partially fulfilled" if fulfillment == "partial" else ("Fulfilled" if fulfilled else "Unfulfilled"), "fulfilled": fulfilled, "delivery_status": delivery, "delivery_status_detail": detail, "payment_status": payment,
          "courier_name": notes.get("hxs_courier_name") or "", "tracking_number": notes.get("hxs_courier_tracking") or "", "items": items, "item_qty": sum(x["qty"] for x in items), "item_cost": product_cost,
          "packaging_cost": packaging, "delivery_cost": delivery_cost, "amount_received": received, "payable": round(received-product_cost-packaging-delivery_cost,2), "order_total": money(source.get("current_total_price", source.get("total_price")))})
    orders.sort(key=lambda row: row["created_at"] or "", reverse=True); visible = [x for x in orders if x["delivery_status"] != "Cancelled"]
    closed = [x for x in visible if x["payment_status"] == "Paid"]; fulfilled_rows = [x for x in visible if x["fulfilled"] and x["payment_status"] != "Paid"]; unfulfilled = [x for x in visible if not x["fulfilled"] and x["payment_status"] != "Paid"]
    settlements = load_settlements(); total_cost = sum(x["item_cost"]+x["packaging_cost"]+x["delivery_cost"] for x in visible if x["fulfilled"]); cod = sum(x["amount_received"] for x in visible); cash = sum(money(x["amount"]) for x in settlements)
    summary = {"fulfilled_orders":len(fulfilled_rows),"unfulfilled_orders":len(unfulfilled),"closed_orders":len(closed),"delivered_orders":sum(x["delivery_status"]=="Delivered" for x in visible),"returned_orders":sum(x["delivery_status"]=="Returned" for x in visible),"total_cost":total_cost,"total_cod":cod,"total_cash_paid":cash,"balance":cod+cash-total_cost,"net_payment_received_entries":settlements}
    return dict(orders=visible, fulfilled_orders=fulfilled_rows, unfulfilled_orders=unfulfilled, closed_orders=closed, summary=summary, error_message=error)


@app.route("/", methods=["GET", "POST"])
@app.route("/aabroo-portal", methods=["GET", "POST"])
def portal():
    if request.method == "POST":
        if (request.form.get("password") or "") == os.environ.get("AABROO_PORTAL_PASSWORD", "") and os.environ.get("AABROO_PORTAL_PASSWORD"):
            session["aabroo_authenticated"] = True
            return redirect(url_for("portal"))
        return render_template("portal.html", view="login", login_error="Wrong password. Try again."), 401
    if not session.get("aabroo_authenticated"):
        return render_template("portal.html", view="login", login_error="")
    return render_template("portal.html", view="portal", **portal_data())


@app.post("/logout")
def logout():
    session.clear(); return redirect(url_for("portal"))


@app.get("/health")
def health():
    return jsonify(status="ok", time=datetime.now(timezone.utc).isoformat())


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
