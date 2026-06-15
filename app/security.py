from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass


class SignatureError(ValueError):
    pass


@dataclass(frozen=True)
class TicketTailorSignature:
    timestamp: int
    signature: str


def parse_ticket_tailor_signature(header: str) -> TicketTailorSignature:
    parts: dict[str, str] = {}
    for item in header.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts[key.strip().lower()] = value.strip()

    timestamp = parts.get("t") or parts.get("timestamp")
    signature = parts.get("v1") or parts.get("signature") or parts.get("s")
    if not timestamp or not signature:
        raise SignatureError("Missing timestamp or signature")

    try:
        timestamp_int = int(timestamp)
    except ValueError as exc:
        raise SignatureError("Invalid timestamp") from exc

    return TicketTailorSignature(timestamp=timestamp_int, signature=signature)


def verify_ticket_tailor_signature(
    *,
    raw_body: bytes,
    header: str | None,
    shared_secret: str,
    tolerance_seconds: int = 300,
    now: int | None = None,
) -> None:
    if not shared_secret:
        return
    if not header:
        raise SignatureError("Missing Ticket Tailor signature header")

    parsed = parse_ticket_tailor_signature(header)
    current_time = int(time.time()) if now is None else now
    if abs(current_time - parsed.timestamp) > tolerance_seconds:
        raise SignatureError("Ticket Tailor signature timestamp is outside tolerance")

    expected = hmac.new(
        shared_secret.encode("utf-8"),
        str(parsed.timestamp).encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, parsed.signature):
        raise SignatureError("Invalid Ticket Tailor webhook signature")


def build_ticket_tailor_signature(raw_body: bytes, shared_secret: str, timestamp: int) -> str:
    digest = hmac.new(
        shared_secret.encode("utf-8"),
        str(timestamp).encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={digest}"

