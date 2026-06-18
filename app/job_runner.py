from __future__ import annotations

import secrets
from typing import Any

from app.config import Settings, get_settings
from app.db import Database
from app.nicky import NickyClient
from app.service import IntegrationService
from app.ticket_tailor import TicketTailorClient


def is_authorized_job_request(settings: Settings, authorization: str | None) -> bool:
    if not settings.job_runner_token:
        return False
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    return secrets.compare_digest(token, settings.job_runner_token)


async def run_expire_overdue_orders(
    *,
    settings: Settings | None = None,
    db: Database | None = None,
    service: IntegrationService | None = None,
    tenant_id: str | None = None,
    expiration_hours: float | None = None,
    batch_size: int | None = None,
) -> dict[str, Any]:
    resolved_settings = settings or get_settings()
    resolved_db = db or Database(resolved_settings.resolved_database_url)
    if service is None:
        resolved_db.init()
    resolved_service = service or IntegrationService(
        settings=resolved_settings,
        db=resolved_db,
        nicky=NickyClient(resolved_settings),
        ticket_tailor=TicketTailorClient(resolved_settings),
    )
    return await resolved_service.expire_overdue_orders(
        tenant_id=tenant_id,
        expiration_hours=expiration_hours,
        batch_size=batch_size,
    )
