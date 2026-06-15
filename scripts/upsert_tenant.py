from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.tenants import (  # noqa: E402
    normalize_tenant_id,
    parse_keywords,
    tenant_from_settings,
    tenant_to_safe_dict,
)


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("Use true or false")


def add_optional_bool(parser: argparse.ArgumentParser, name: str, dest: str) -> None:
    parser.add_argument(name, dest=dest, type=parse_bool, choices=[True, False])


def compact(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update a tenant mapping.")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--name")
    parser.add_argument("--ticket-tailor-api-key")
    parser.add_argument("--ticket-tailor-webhook-signing-secret")
    parser.add_argument("--ticket-tailor-offline-payment-keywords")
    parser.add_argument("--nicky-api-key")
    parser.add_argument("--nicky-default-blockchain-asset-id")
    parser.add_argument("--nicky-receiver-short-id")
    parser.add_argument("--nicky-webhook-token")
    parser.add_argument("--nicky-webhook-type", type=int)
    add_optional_bool(parser, "--active", "active")
    add_optional_bool(parser, "--dry-run", "dry_run")
    add_optional_bool(parser, "--auto-create-nicky-payment-request", "auto_create")
    add_optional_bool(parser, "--auto-confirm-ticket-tailor-payments", "auto_confirm")
    add_optional_bool(parser, "--nicky-send-notification", "send_notification")
    add_optional_bool(parser, "--skip-nicky", "skip_nicky")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings.database_path)
    db.init()

    tenant_id = normalize_tenant_id(args.tenant_id)
    base = db.get_tenant(tenant_id) or tenant_from_settings(settings, tenant_id)
    updates = compact(
        {
            "name": args.name,
            "active": args.active,
            "ticket_tailor_api_key": args.ticket_tailor_api_key,
            "ticket_tailor_webhook_signing_secret": args.ticket_tailor_webhook_signing_secret,
            "ticket_tailor_offline_payment_keywords": parse_keywords(
                args.ticket_tailor_offline_payment_keywords
            )
            if args.ticket_tailor_offline_payment_keywords is not None
            else None,
            "nicky_api_key": args.nicky_api_key,
            "nicky_default_blockchain_asset_id": args.nicky_default_blockchain_asset_id,
            "nicky_receiver_short_id": args.nicky_receiver_short_id,
            "nicky_webhook_token": args.nicky_webhook_token,
            "nicky_webhook_type": args.nicky_webhook_type,
            "auto_create_nicky_payment_request": args.auto_create,
            "auto_confirm_ticket_tailor_payments": args.auto_confirm,
            "nicky_send_notification": args.send_notification,
            "skip_nicky": args.skip_nicky,
            "dry_run": args.dry_run,
        }
    )
    tenant = replace(base, tenant_id=tenant_id, **updates)
    db.upsert_tenant(tenant)
    print(json.dumps(tenant_to_safe_dict(tenant), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
