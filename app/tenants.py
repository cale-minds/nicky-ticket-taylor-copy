from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.config import Settings


TENANT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$")
NICKY_PAYMENT_KEYWORDS = ["nicky payment"]
NICKY_WEBHOOK_TYPE = 2


@dataclass(frozen=True)
class TenantConfig:
    tenant_id: str
    name: str
    active: bool
    nicky_user_uuid: str
    nicky_user_short_id: str
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
    created_at: str = ""
    updated_at: str = ""

    @property
    def ticket_tailor_configured(self) -> bool:
        return bool(self.ticket_tailor_api_key)

    @property
    def nicky_configured(self) -> bool:
        return bool(
            self.nicky_api_key
            and self.nicky_default_blockchain_asset_id
            and self.nicky_user_uuid
        )


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
    if not tenant_id:
        raise ValueError("tenant_id is required")
    resolved_tenant_id = normalize_tenant_id(tenant_id)
    return TenantConfig(
        tenant_id=resolved_tenant_id,
        name=resolved_tenant_id,
        active=True,
        nicky_user_uuid=resolved_tenant_id,
        nicky_user_short_id=settings.nicky_receiver_short_id,
        ticket_tailor_api_key=settings.ticket_tailor_api_key,
        ticket_tailor_webhook_signing_secret=settings.ticket_tailor_webhook_signing_secret,
        ticket_tailor_offline_payment_keywords=NICKY_PAYMENT_KEYWORDS,
        nicky_api_key=settings.nicky_api_key,
        nicky_default_blockchain_asset_id=settings.nicky_default_blockchain_asset_id,
        nicky_receiver_short_id=settings.nicky_receiver_short_id,
        nicky_webhook_token=settings.nicky_webhook_token,
        nicky_webhook_type=NICKY_WEBHOOK_TYPE,
        auto_create_nicky_payment_request=True,
        auto_confirm_ticket_tailor_payments=True,
        nicky_send_notification=True,
        skip_nicky=False,
        dry_run=False,
    )


def tenant_from_row(row: Any) -> TenantConfig:
    return TenantConfig(
        tenant_id=str(row["tenant_id"]),
        name=str(row["name"] or row["tenant_id"]),
        active=bool_from_db(row["active"]),
        nicky_user_uuid=str(row["nicky_user_uuid"] or row["tenant_id"]),
        nicky_user_short_id=str(row["nicky_user_short_id"] or row["nicky_receiver_short_id"] or ""),
        ticket_tailor_api_key=str(row["ticket_tailor_api_key"] or ""),
        ticket_tailor_webhook_signing_secret=str(
            row["ticket_tailor_webhook_signing_secret"] or ""
        ),
        ticket_tailor_offline_payment_keywords=NICKY_PAYMENT_KEYWORDS,
        nicky_api_key=str(row["nicky_api_key"] or ""),
        nicky_default_blockchain_asset_id=str(row["nicky_default_blockchain_asset_id"] or ""),
        nicky_receiver_short_id=str(row["nicky_receiver_short_id"] or ""),
        nicky_webhook_token=str(row["nicky_webhook_token"] or ""),
        nicky_webhook_type=NICKY_WEBHOOK_TYPE,
        auto_create_nicky_payment_request=True,
        auto_confirm_ticket_tailor_payments=True,
        nicky_send_notification=True,
        skip_nicky=False,
        dry_run=False,
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
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
        "nicky_user_uuid": tenant.nicky_user_uuid,
        "nicky_user_short_id": tenant.nicky_user_short_id,
        "ticket_tailor_configured": tenant.ticket_tailor_configured,
        "nicky_configured": tenant.nicky_configured,
        "nicky_default_blockchain_asset_id": tenant.nicky_default_blockchain_asset_id,
        "created_at": tenant.created_at,
        "updated_at": tenant.updated_at,
    }
