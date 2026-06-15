import pytest

from app.security import (
    SignatureError,
    build_ticket_tailor_signature,
    verify_ticket_tailor_signature,
)


def test_ticket_tailor_signature_valid() -> None:
    raw = b'{"id":"evt_1"}'
    secret = "secret"
    header = build_ticket_tailor_signature(raw, secret, timestamp=1_700_000_000)

    verify_ticket_tailor_signature(
        raw_body=raw,
        header=header,
        shared_secret=secret,
        now=1_700_000_010,
    )


def test_ticket_tailor_signature_rejects_wrong_digest() -> None:
    with pytest.raises(SignatureError):
        verify_ticket_tailor_signature(
            raw_body=b'{"id":"evt_1"}',
            header="t=1700000000,v1=bad",
            shared_secret="secret",
            now=1_700_000_010,
        )


def test_ticket_tailor_signature_rejects_old_timestamp() -> None:
    raw = b'{"id":"evt_1"}'
    secret = "secret"
    header = build_ticket_tailor_signature(raw, secret, timestamp=1_700_000_000)

    with pytest.raises(SignatureError):
        verify_ticket_tailor_signature(
            raw_body=raw,
            header=header,
            shared_secret=secret,
            tolerance_seconds=300,
            now=1_700_001_000,
        )

