import hashlib
import json


def advice_request_key(row: dict) -> str:
    """Identify one advice request without hiding a later request for the same CN."""
    tracking = "".join(ch for ch in str(row.get("tracking_no") or row.get("tracking_number") or "") if ch.isdigit())
    identity = {
        "tracking": tracking,
        "requested_at": next((str(row.get(key) or "").strip() for key in ("status_date", "advice_date", "requested_at", "created_at", "request_date") if row.get(key)), ""),
        "reason": str(row.get("courier_status_reason") or row.get("status_reason") or row.get("reason") or "").strip().casefold(),
        "gateway_id": str(row.get("gateway_id") or "").strip(),
        "order_id": str(row.get("order_id") or row.get("reference_no") or "").strip(),
    }
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def remove_acknowledged_advice(rows, acknowledged_keys):
    acknowledged = set(acknowledged_keys or ())
    return [row for row in (rows or []) if advice_request_key(row) not in acknowledged]
