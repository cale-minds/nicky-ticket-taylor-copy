from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any


def deep_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from deep_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from deep_values(item)
    else:
        yield value


def deep_find_by_key(value: Any, candidate_keys: set[str]) -> Any | None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in candidate_keys and item not in (None, ""):
                return item
        for item in value.values():
            found = deep_find_by_key(item, candidate_keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = deep_find_by_key(item, candidate_keys)
            if found not in (None, ""):
                return found
    return None


def deep_payment_text(value: Any) -> str:
    chunks: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = key.lower()
            if "payment" in normalized_key or "method" in normalized_key:
                chunks.extend(str(v) for v in deep_values(item) if v not in (None, ""))
            else:
                chunks.append(deep_payment_text(item))
    elif isinstance(value, list):
        for item in value:
            chunks.append(deep_payment_text(item))
    return " ".join(chunk for chunk in chunks if chunk)


def has_payment_keyword(payload: dict[str, Any], keywords: list[str]) -> bool:
    text = deep_payment_text(payload).lower()
    return any(keyword in text for keyword in keywords)


def as_minor_units(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        decimal_value = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        return None
    if decimal_value == decimal_value.to_integral_value():
        return int(decimal_value)
    return int((decimal_value * 100).to_integral_value())


def as_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        for key in ("code", "id", "name", "value", "status"):
            nested = value.get(key)
            if nested not in (None, "") and not isinstance(nested, (dict, list)):
                return str(nested)
        return None
    if isinstance(value, list):
        return None
    return str(value)


def extract_order(payload: dict[str, Any]) -> dict[str, Any]:
    order_id = deep_find_by_key(payload, {"id", "order_id", "orderid"})
    if isinstance(order_id, dict):
        order_id = deep_find_by_key(order_id, {"id"})

    amount = None
    for key in (
        "total",
        "total_cents",
        "total_minor",
        "total_amount",
        "total_gross",
        "amount",
        "amount_total",
        "value",
    ):
        amount = as_minor_units(deep_find_by_key(payload, {key}))
        if amount is not None:
            break

    first_name = deep_find_by_key(payload, {"first_name", "firstname"})
    last_name = deep_find_by_key(payload, {"last_name", "lastname"})
    full_name = deep_find_by_key(payload, {"name", "full_name", "fullname"})
    if not full_name and (first_name or last_name):
        full_name = " ".join(str(part) for part in (first_name, last_name) if part)

    event_id = deep_find_by_key(payload, {"event_id", "eventid", "event_series_id"})
    status = deep_find_by_key(payload, {"status", "order_status"})
    payment_status = deep_find_by_key(payload, {"payment_status", "paymentstatus"})
    currency = deep_find_by_key(payload, {"currency", "currency_code", "currencycode"})
    buyer_email = deep_find_by_key(payload, {"email", "buyer_email", "customer_email"})

    return {
        "ticket_tailor_order_id": str(order_id) if order_id else "",
        "event_id": as_text(event_id),
        "status": as_text(status),
        "payment_status": as_text(payment_status),
        "currency": as_text(currency),
        "amount_minor": amount,
        "buyer_email": as_text(buyer_email),
        "buyer_name": as_text(full_name),
    }


def extract_issued_ticket_ids(payload: dict[str, Any]) -> list[str]:
    ticket_ids: list[str] = []

    def append_ticket_id(candidate: Any) -> None:
        if not isinstance(candidate, dict):
            return
        ticket_id = candidate.get("id") or candidate.get("issued_ticket_id")
        if not ticket_id:
            return
        status = as_text(candidate.get("status")) or ""
        if candidate.get("voided_at") or status.lower() == "voided":
            return
        normalized_id = str(ticket_id)
        if normalized_id not in ticket_ids:
            ticket_ids.append(normalized_id)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("object") == "issued_ticket":
                append_ticket_id(value)
            for key, item in value.items():
                if key.lower().replace("-", "_") in {"issued_tickets", "tickets"}:
                    if isinstance(item, list):
                        for ticket in item:
                            append_ticket_id(ticket)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return ticket_ids
