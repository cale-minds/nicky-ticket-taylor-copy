import json
from dataclasses import replace

import pytest

from app.config import Settings
from app.db import Database
from app.nicky import NickyClient
from app.service import IntegrationService
from app.tenants import tenant_from_settings
from app.ticket_tailor import TicketTailorClient


def live_tenant(settings: Settings, tenant_id: str = "tenant-live"):
    return replace(
        tenant_from_settings(settings, tenant_id),
        ticket_tailor_api_key="tt_live_key",
        nicky_api_key="nicky_live_key",
        nicky_default_blockchain_asset_id="USD.USD",
        nicky_receiver_short_id="RCV123",
        nicky_user_uuid=tenant_id,
        nicky_user_short_id="RCV123",
    )


class FakeNickyClient(NickyClient):
    async def create_payment_request(self, tenant, order: dict) -> dict:
        return {
            "id": f"pr_{order['ticket_tailor_order_id']}",
            "status": "PaymentPending",
            "bill": {
                "shortId": f"tt-{order['ticket_tailor_order_id']}",
                "receiverUser": {"shortId": tenant.nicky_receiver_short_id},
            },
        }


class FakeTicketTailorClient(TicketTailorClient):
    async def confirm_offline_payment(self, tenant, order_id: str) -> dict[str, str]:
        return {"status": "confirmed", "order_id": order_id}

    async def void_issued_ticket(self, tenant, issued_ticket_id: str) -> dict[str, str]:
        return {"status": "voided", "issued_ticket_id": issued_ticket_id}

    async def list_issued_tickets_for_order(self, tenant, order_id: str) -> list[dict[str, object]]:
        return []


class SelectiveFailureTicketTailorClient(TicketTailorClient):
    def __init__(self, settings: Settings, failed_ticket_ids: set[str]) -> None:
        super().__init__(settings)
        self.failed_ticket_ids = failed_ticket_ids

    async def void_issued_ticket(self, tenant, issued_ticket_id: str) -> dict[str, str]:
        if issued_ticket_id in self.failed_ticket_ids:
            raise RuntimeError(f"void failed for {issued_ticket_id}")
        return {"status": "voided", "issued_ticket_id": issued_ticket_id}

    async def list_issued_tickets_for_order(self, tenant, order_id: str) -> list[dict[str, object]]:
        return []


def seed_expired_order(
    db: Database,
    tenant_id: str,
    order_id: str,
    ticket_id: str,
    *,
    created_hours_ago: int = 3,
) -> None:
    db.upsert_order(
        tenant_id,
        {
            "ticket_tailor_order_id": order_id,
            "event_id": "ev_123",
            "status": "pending",
            "payment_status": "pending",
            "currency": "USD",
            "amount_minor": 100,
            "buyer_email": "buyer@example.com",
            "buyer_name": "Buyer Example",
        },
        {
            "id": order_id,
            "payment_method": {"name": "Nicky Payment"},
            "issued_tickets": [
                {"object": "issued_ticket", "id": ticket_id, "status": "valid"}
            ],
        },
    )
    db.update_nicky_payment_request(
        tenant_id=tenant_id,
        ticket_tailor_order_id=order_id,
        payment_request_id=f"pr_{order_id}",
        bill_short_id="BILL123",
        receiver_short_id="RCV123",
        payment_url="https://pay.nicky.me/payment-report/RCV123?paymentId=BILL123",
        status="PaymentPending",
    )
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE integration_orders
            SET created_at = datetime('now', ?)
            WHERE tenant_id = ? AND ticket_tailor_order_id = ?
            """,
            (f"-{created_hours_ago} hours", tenant_id, order_id),
        )


@pytest.mark.asyncio
async def test_ticket_tailor_webhook_is_idempotent_and_creates_live_payment(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "integration.sqlite3",
        dry_run=True,
        auto_create_nicky_payment_request=True,
        nicky_receiver_short_id="RCV123",
    )
    db = Database(settings.database_path)
    db.init()
    tenant = live_tenant(settings)
    db.upsert_tenant(tenant)
    service = IntegrationService(
        settings=settings,
        db=db,
        nicky=FakeNickyClient(settings),
        ticket_tailor=FakeTicketTailorClient(settings),
    )
    event = {
        "id": "wh_1",
        "event": "ORDER.CREATED",
        "resource_url": "https://api.tickettailor.com/v1/orders/or_123",
        "payload": {
            "id": "or_123",
            "status": "pending",
            "currency": "USD",
            "total": 100,
            "buyer": {"email": "buyer@example.com", "name": "Buyer Example"},
            "payment_method": {"name": "Nicky Payment"},
        },
    }
    raw = json.dumps(event).encode("utf-8")

    first = await service.process_ticket_tailor_webhook(tenant, event, raw)
    duplicate = await service.process_ticket_tailor_webhook(tenant, event, raw)
    row = db.get_order(tenant.tenant_id, "or_123")

    assert first["status"] == "processed"
    assert first["tenant_id"] == tenant.tenant_id
    assert first["nicky_payment_request"]["id"] == "pr_or_123"
    assert (
        first["nicky_payment_request"]["payment_url"]
        == "https://pay.nicky.me/payment-report/RCV123?paymentId=tt-or_123"
    )
    assert duplicate["status"] == "duplicate"
    assert row is not None
    assert row["nicky_payment_request_id"] == "pr_or_123"
    assert row["nicky_payment_url"] == "https://pay.nicky.me/payment-report/RCV123?paymentId=tt-or_123"


@pytest.mark.asyncio
async def test_nicky_finished_status_confirms_ticket_tailor_in_live_flow(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "integration.sqlite3",
        dry_run=True,
        auto_confirm_ticket_tailor_payments=True,
    )
    db = Database(settings.database_path)
    db.init()
    tenant = live_tenant(settings)
    db.upsert_tenant(tenant)
    service = IntegrationService(
        settings=settings,
        db=db,
        nicky=FakeNickyClient(settings),
        ticket_tailor=FakeTicketTailorClient(settings),
    )
    db.upsert_order(
        tenant.tenant_id,
        {
            "ticket_tailor_order_id": "or_123",
            "event_id": "ev_123",
            "status": "pending",
            "payment_status": "pending",
            "currency": "USD",
            "amount_minor": 100,
            "buyer_email": "buyer@example.com",
            "buyer_name": "Buyer Example",
        },
        {"id": "or_123", "payment_method": {"name": "Nicky Payment"}},
    )
    db.update_nicky_payment_request(
        tenant_id=tenant.tenant_id,
        ticket_tailor_order_id="or_123",
        payment_request_id="pr_123",
        bill_short_id="BILL123",
        receiver_short_id="RCV123",
        payment_url="https://pay.nicky.me/payment-report/RCV123?paymentId=BILL123",
        status="PaymentPending",
    )
    event = {
        "webHookId": "wh_nicky",
        "webHookType": "PaymentRequest_StatusChanged",
        "itemId": "pr_123",
        "data": {"previousStatus": "PaymentPending", "newStatus": "Finished"},
    }

    result = await service.process_nicky_webhook(tenant, event, b"{}")
    row = db.get_order(tenant.tenant_id, "or_123")

    assert result["status"] == "processed"
    assert result["ticket_tailor_confirmation"]["status"] == "confirmed"
    assert result["ticket_tailor_void"] is None
    assert row is not None
    assert row["ticket_tailor_confirmed_at"] is not None
    assert row["payment_status"] == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["Paid", "Completed", "Succeeded", "Confirmed", "PaymentReceived"])
async def test_non_finished_nicky_status_voids_ticket_tailor_ticket(tmp_path, status) -> None:
    settings = Settings(
        database_path=tmp_path / "integration.sqlite3",
        dry_run=True,
        auto_confirm_ticket_tailor_payments=True,
    )
    db = Database(settings.database_path)
    db.init()
    tenant = live_tenant(settings)
    db.upsert_tenant(tenant)
    service = IntegrationService(
        settings=settings,
        db=db,
        nicky=FakeNickyClient(settings),
        ticket_tailor=FakeTicketTailorClient(settings),
    )
    db.upsert_order(
        tenant.tenant_id,
        {
            "ticket_tailor_order_id": "or_123",
            "event_id": "ev_123",
            "status": "pending",
            "payment_status": "pending",
            "currency": "USD",
            "amount_minor": 100,
            "buyer_email": "buyer@example.com",
            "buyer_name": "Buyer Example",
        },
        {
            "id": "or_123",
            "payment_method": {"name": "Nicky Payment"},
            "issued_tickets": [
                {"object": "issued_ticket", "id": "it_123", "status": "valid"}
            ],
        },
    )
    db.update_nicky_payment_request(
        tenant_id=tenant.tenant_id,
        ticket_tailor_order_id="or_123",
        payment_request_id="pr_123",
        bill_short_id="BILL123",
        receiver_short_id="RCV123",
        payment_url="https://pay.nicky.me/payment-report/RCV123?paymentId=BILL123",
        status="PaymentPending",
    )
    event = {
        "webHookId": "wh_nicky",
        "webHookType": "PaymentRequest_StatusChanged",
        "itemId": "pr_123",
        "data": {"previousStatus": "PaymentPending", "newStatus": status},
    }

    result = await service.process_nicky_webhook(tenant, event, b"{}")
    row = db.get_order(tenant.tenant_id, "or_123")

    assert result["status"] == "processed"
    assert result["ticket_tailor_confirmation"] is None
    assert result["ticket_tailor_void"]["status"] == "voided"
    assert result["ticket_tailor_void"]["issued_ticket_ids"] == ["it_123"]
    assert row is not None
    assert row["nicky_status"] == status
    assert row["payment_status"] == "voided"
    assert row["ticket_tailor_confirmed_at"] is None
    assert row["ticket_tailor_tickets_voided_at"] is not None


@pytest.mark.asyncio
async def test_expire_overdue_orders_voids_unfinished_ticket_tailor_ticket(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "integration.sqlite3",
        dry_run=True,
        ticket_tailor_pending_ticket_expiration_hours=2,
    )
    db = Database(settings.database_path)
    db.init()
    tenant = live_tenant(settings)
    db.upsert_tenant(tenant)
    service = IntegrationService(
        settings=settings,
        db=db,
        nicky=FakeNickyClient(settings),
        ticket_tailor=FakeTicketTailorClient(settings),
    )
    db.upsert_order(
        tenant.tenant_id,
        {
            "ticket_tailor_order_id": "or_expired",
            "event_id": "ev_123",
            "status": "pending",
            "payment_status": "pending",
            "currency": "USD",
            "amount_minor": 100,
            "buyer_email": "buyer@example.com",
            "buyer_name": "Buyer Example",
        },
        {
            "id": "or_expired",
            "payment_method": {"name": "Nicky Payment"},
            "issued_tickets": [
                {"object": "issued_ticket", "id": "it_expired", "status": "valid"}
            ],
        },
    )
    db.update_nicky_payment_request(
        tenant_id=tenant.tenant_id,
        ticket_tailor_order_id="or_expired",
        payment_request_id="pr_expired",
        bill_short_id="BILL123",
        receiver_short_id="RCV123",
        payment_url="https://pay.nicky.me/payment-report/RCV123?paymentId=BILL123",
        status="PaymentPending",
    )
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE integration_orders
            SET created_at = datetime('now', '-3 hours')
            WHERE tenant_id = ? AND ticket_tailor_order_id = ?
            """,
            (tenant.tenant_id, "or_expired"),
        )

    result = await service.expire_overdue_orders()
    row = db.get_order(tenant.tenant_id, "or_expired")

    assert result["status"] == "processed"
    assert result["expired_count"] == 1
    assert result["results"][0]["issued_ticket_ids"] == ["it_expired"]
    assert row is not None
    assert row["payment_status"] == "voided"
    assert row["ticket_tailor_tickets_voided_at"] is not None
    assert row["ticket_tailor_void_reason"] == "expired_after_2_hours"


@pytest.mark.asyncio
async def test_expire_overdue_orders_uses_batch_size(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "integration.sqlite3",
        dry_run=True,
        ticket_tailor_pending_ticket_expiration_hours=2,
        ticket_tailor_expiration_batch_size=2,
    )
    db = Database(settings.database_path)
    db.init()
    tenant = live_tenant(settings)
    db.upsert_tenant(tenant)
    service = IntegrationService(
        settings=settings,
        db=db,
        nicky=FakeNickyClient(settings),
        ticket_tailor=FakeTicketTailorClient(settings),
    )
    seed_expired_order(db, tenant.tenant_id, "or_1", "it_1")
    seed_expired_order(db, tenant.tenant_id, "or_2", "it_2")
    seed_expired_order(db, tenant.tenant_id, "or_3", "it_3")

    result = await service.expire_overdue_orders()

    assert result["batch_size"] == 2
    assert result["selected_count"] == 2
    assert result["expired_count"] == 2
    assert result["failed_count"] == 0
    assert db.get_order(tenant.tenant_id, "or_1")["payment_status"] == "voided"
    assert db.get_order(tenant.tenant_id, "or_2")["payment_status"] == "voided"
    assert db.get_order(tenant.tenant_id, "or_3")["payment_status"] == "pending"


@pytest.mark.asyncio
async def test_expire_overdue_orders_continues_after_item_failure(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "integration.sqlite3",
        dry_run=True,
        ticket_tailor_pending_ticket_expiration_hours=2,
        ticket_tailor_expiration_batch_size=10,
    )
    db = Database(settings.database_path)
    db.init()
    tenant = live_tenant(settings)
    db.upsert_tenant(tenant)
    service = IntegrationService(
        settings=settings,
        db=db,
        nicky=FakeNickyClient(settings),
        ticket_tailor=SelectiveFailureTicketTailorClient(settings, {"it_fail"}),
    )
    seed_expired_order(db, tenant.tenant_id, "or_fail", "it_fail")
    seed_expired_order(db, tenant.tenant_id, "or_ok", "it_ok")

    result = await service.expire_overdue_orders()

    assert result["selected_count"] == 2
    assert result["expired_count"] == 1
    assert result["failed_count"] == 1
    assert result["results"][0]["status"] == "failed"
    assert result["results"][1]["status"] == "voided"
    assert db.get_order(tenant.tenant_id, "or_fail")["payment_status"] == "pending"
    assert db.get_order(tenant.tenant_id, "or_ok")["payment_status"] == "voided"


@pytest.mark.asyncio
async def test_nicky_concluido_status_confirms_ticket_tailor(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "integration.sqlite3",
        dry_run=True,
        skip_nicky=True,
        auto_confirm_ticket_tailor_payments=True,
        nicky_receiver_short_id="RCV123",
    )
    db = Database(settings.database_path)
    db.init()
    tenant = live_tenant(settings)
    db.upsert_tenant(tenant)
    service = IntegrationService(
        settings=settings,
        db=db,
        nicky=FakeNickyClient(settings),
        ticket_tailor=FakeTicketTailorClient(settings),
    )
    db.upsert_order(
        tenant.tenant_id,
        {
            "ticket_tailor_order_id": "or_done",
            "event_id": "ev_123",
            "status": "pending",
            "payment_status": "pending",
            "currency": "USD",
            "amount_minor": 100,
            "buyer_email": "buyer@example.com",
            "buyer_name": "Buyer Example",
        },
        {"id": "or_done", "payment_method": {"name": "Nicky Payment"}},
    )
    db.update_nicky_payment_request(
        tenant_id=tenant.tenant_id,
        ticket_tailor_order_id="or_done",
        payment_request_id="pr_done",
        bill_short_id="BILL123",
        receiver_short_id="RCV123",
        payment_url="https://pay.nicky.me/payment-report/RCV123?paymentId=BILL123",
        status="PaymentPending",
    )
    event = {
        "webHookId": "wh_nicky_done",
        "webHookType": "PaymentRequest_StatusChanged",
        "itemId": "pr_done",
        "data": {"previousStatus": "PaymentPending", "newStatus": "Concluído"},
    }

    result = await service.process_nicky_webhook(tenant, event, b"{}")
    row = db.get_order(tenant.tenant_id, "or_done")

    assert result["ticket_tailor_confirmation"]["status"] == "confirmed"
    assert row is not None
    assert row["nicky_status"] == "Concluído"
    assert row["ticket_tailor_confirmed_at"] is not None


@pytest.mark.asyncio
async def test_webhook_ids_are_idempotent_per_tenant(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "integration.sqlite3",
        dry_run=True,
        auto_create_nicky_payment_request=True,
        nicky_receiver_short_id="RCV123",
    )
    db = Database(settings.database_path)
    db.init()
    tenant_a = live_tenant(settings, "client-a")
    tenant_b = live_tenant(settings, "client-b")
    db.upsert_tenant(tenant_a)
    db.upsert_tenant(tenant_b)
    service = IntegrationService(
        settings=settings,
        db=db,
        nicky=FakeNickyClient(settings),
        ticket_tailor=FakeTicketTailorClient(settings),
    )
    event = {
        "id": "same_webhook_id",
        "event": "ORDER.CREATED",
        "resource_url": "https://api.tickettailor.com/v1/orders/or_123",
        "payload": {
            "id": "or_123",
            "status": "pending",
            "currency": "USD",
            "total": 100,
            "buyer": {"email": "buyer@example.com", "name": "Buyer Example"},
            "payment_method": {"name": "Nicky Payment"},
        },
    }
    raw = json.dumps(event).encode("utf-8")

    result_a = await service.process_ticket_tailor_webhook(tenant_a, event, raw)
    result_b = await service.process_ticket_tailor_webhook(tenant_b, event, raw)

    assert result_a["status"] == "processed"
    assert result_b["status"] == "processed"
    assert db.get_order("client-a", "or_123") is not None
    assert db.get_order("client-b", "or_123") is not None


def test_failed_webhook_can_be_retried(tmp_path) -> None:
    settings = Settings(database_path=tmp_path / "integration.sqlite3", dry_run=True)
    db = Database(settings.database_path)
    db.init()

    inserted = db.insert_webhook_event(
        tenant_id="client-a",
        source="ticket_tailor",
        event_id="wh_retry",
        event_type="ORDER.CREATED",
        raw_body=b'{"id":"wh_retry"}',
    )
    db.mark_webhook_event(
        "client-a", "ticket_tailor", "wh_retry", "failed", "temporary error"
    )
    retried = db.insert_webhook_event(
        tenant_id="client-a",
        source="ticket_tailor",
        event_id="wh_retry",
        event_type="ORDER.CREATED",
        raw_body=b'{"id":"wh_retry","retry":true}',
    )

    assert inserted is True
    assert retried is True
