from app.extractors import extract_order, has_payment_keyword


def test_extract_order_from_ticket_tailor_like_payload() -> None:
    payload = {
        "id": "or_123",
        "status": "pending",
        "currency": "USD",
        "total": 1234,
        "buyer": {"email": "buyer@example.com", "name": "Buyer Example"},
        "payment_method": {"name": "Nicky Payment"},
    }

    order = extract_order(payload)

    assert order["ticket_tailor_order_id"] == "or_123"
    assert order["amount_minor"] == 1234
    assert order["currency"] == "USD"
    assert order["buyer_email"] == "buyer@example.com"
    assert has_payment_keyword(payload, ["nicky"])


def test_decimal_amount_is_converted_to_minor_units() -> None:
    payload = {"id": "or_123", "total": "12.34"}

    order = extract_order(payload)

    assert order["amount_minor"] == 1234


def test_extract_order_from_real_ticket_tailor_payload_shape() -> None:
    payload = {
        "object": "order",
        "id": "or_77878500",
        "buyer_details": {
            "email": "buyer@example.com",
            "first_name": "Nicky",
            "last_name": "Skip Test",
            "name": "Nicky Skip Test",
        },
        "currency": {"base_multiplier": 100, "code": "usd"},
        "event_summary": {"event_id": "ev_8254567"},
        "payment_method": {"name": "Nicky Payment", "type": "offline"},
        "status": "pending",
        "total": 1,
    }

    order = extract_order(payload)

    assert order["ticket_tailor_order_id"] == "or_77878500"
    assert order["event_id"] == "ev_8254567"
    assert order["status"] == "pending"
    assert order["currency"] == "usd"
    assert order["amount_minor"] == 1
    assert order["buyer_email"] == "buyer@example.com"
    assert order["buyer_name"] == "Nicky Skip Test"
    assert has_payment_keyword(payload, ["nicky"])
