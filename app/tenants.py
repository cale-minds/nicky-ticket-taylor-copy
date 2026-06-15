from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.config import Settings


TENANT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$")


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    name: str
    active: bool
    ticket_tailor_api_key: str
    ticket_tailor_webhook_signing_secret: str
    ticket_tailor_offline_payment_keywords: list[str]
    nicky_api_key: str
    nicky_default_blockchain_asset_id: str
    nicky_receiver_short_id: str
    nicky_webhook_token: str
    nicky_webhook_type: int
    auto_create_nicky_payment_request: bool
    auto_confirm_ticket_tailor_payments: bool
    nicky_send_notification: bool
    skip_nicky: bool
    dry_run: bool

    @property
    def ticket_tailor_configured(self) -> bool:
        return bool(self.ticket_tailor_api_key)

    @property
    def nicky_configured(self) -> bool:
        return bool(self.skip_nicky or (self.nicky_api_key and self.nicky_default_blockchain_asset_id))


def normalize_tenant_id(value: str) -> str:
    tenant_id = value.strip()
    if not TENANT_ID_PATTERN.fullmatch(tenant_id):
        raise ValueError(
            "tenant_id must be 2-64 chars and contain only letters, numbers, '_' or '-'"
        )
    return tenant_id


def parse_keywords(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = value
    return [item.strip().lower() for item in items if item and item.strip()]


def keywords_to_csv(keywords: list[str]) -> str:
    return ",".join(parse_keywords(keywords))


def bool_from_db(value: Any) -> bool:
    return bool(int(value or 0))


def tenant_from_settings(settings: Settings, tenant_id: str | None = None) -> TenantConfig:
    resolved_tenant_id = normalize_tenant_id(tenant_id or settings.default_tenant_id)
    return TenantConfig(
        tenant_id=resolved_tenant_id,
        name=resolved_tenant_id,
        active=True,
        ticket_tailor_api_key=settings.ticket_tailor_api_key,
        ticket_tailor_webhook_signing_secret=settings.ticket_tailor_webhook_signing_secret,
        ticket_tailor_offline_payment_keywords=settings.ticket_tailor_offline_payment_keywords,
        nicky_api_key=settings.nicky_api_key,
        nicky_default_blockchain_asset_id=settings.nicky_default_blockchain_asset_id,
        nicky_receiver_short_id=settings.nicky_receiver_short_id,
        nicky_webhook_token=settings.nicky_webhook_token,
        nicky_webhook_type=settings.nicky_webhook_type,
        auto_create_nicky_payment_request=settings.auto_create_nicky_payment_request,
        auto_confirm_ticket_tailor_payments=settings.auto_confirm_ticket_tailor_payments,
        nicky_send_notification=settings.nicky_send_notification,
        skip_nicky=settings.skip_nicky,
        dry_run=settings.dry_run,
    )


def tenant_from_row(row: Any) -> TenantConfig:
    return TenantConfig(
        tenant_id=str(row["tenant_id"]),
        name=str(row["name"] or row["tenant_id"]),
        active=bool_from_db(row["active"]),
        ticket_tailor_api_key=str(row["ticket_tailor_api_key"] or ""),
        ticket_tailor_webhook_signing_secret=str(
            row["ticket_tailor_webhook_signing_secret"] or ""
        ),
        ticket_tailor_offline_payment_keywords=parse_keywords(
            row["ticket_tailor_offline_payment_keywords"]
        ),
        nicky_api_key=str(row["nicky_api_key"] or ""),
        nicky_default_blockchain_asset_id=str(row["nicky_default_blockchain_asset_id"] or ""),
        nicky_receiver_short_id=str(row["nicky_receiver_short_id"] or ""),
        nicky_webhook_token=str(row["nicky_webhook_token"] or ""),
        nicky_webhook_type=int(row["nicky_webhook_type"] or 2),
        auto_create_nicky_payment_request=bool_from_db(
            row["auto_create_nicky_payment_request"]
        ),
        auto_confirm_ticket_tailor_payments=bool_from_db(
            row["auto_confirm_ticket_tailor_payments"]
        ),
        nicky_send_notification=bool_from_db(row["nicky_send_notification"]),
        skip_nicky=bool_from_db(row["skip_nicky"]),
        dry_run=bool_from_db(row["dry_run"]),
    )


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def tenant_to_safe_dict(tenant: TenantConfig) -> dict[str, Any]:
    return {
        "tenant_id": tenant.tenant_id,
        "name": tenant.name,
        "active": tenant.active,
        "ticket_tailor_configured": tenant.ticket_tailor_configured,
        "ticket_tailor_api_key": mask_secret(tenant.ticket_tailor_api_key),
        "ticket_tailor_webhook_signing_secret": mask_secret(
            tenant.ticket_tailor_webhook_signing_secret
        ),
        "ticket_tailor_offline_payment_keywords": tenant.ticket_tailor_offline_payment_keywords,
        "nicky_configured": tenant.nicky_configured,
        "nicky_api_key": mask_secret(tenant.nicky_api_key),
        "nicky_default_blockchain_asset_id": tenant.nicky_default_blockchain_asset_id,
        "nicky_receiver_short_id": tenant.nicky_receiver_short_id,
        "nicky_webhook_token": mask_secret(tenant.nicky_webhook_token),
        "nicky_webhook_type": tenant.nicky_webhook_type,
        "auto_create_nicky_payment_request": tenant.auto_create_nicky_payment_request,
        "auto_confirm_ticket_tailor_payments": tenant.auto_confirm_ticket_tailor_payments,
        "nicky_send_notification": tenant.nicky_send_notification,
        "skip_nicky": tenant.skip_nicky,
        "dry_run": tenant.dry_run,
    }
