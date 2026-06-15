from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.db import Database
from app.nicky import NickyClient
from app.security import SignatureError, verify_ticket_tailor_signature
from app.service import IntegrationService, row_to_dict
from app.tenants import (
    TenantConfig,
    normalize_tenant_id,
    parse_keywords,
    tenant_from_settings,
    tenant_to_safe_dict,
)
from app.ticket_tailor import TicketTailorClient


settings = get_settings()
db = Database(settings.database_path)
nicky_client = NickyClient(settings)
ticket_tailor_client = TicketTailorClient(settings)
service = IntegrationService(
    settings=settings,
    db=db,
    nicky=nicky_client,
    ticket_tailor=ticket_tailor_client,
)


async def expire_overdue_orders_loop() -> None:
    interval = max(settings.ticket_tailor_expiration_check_interval_seconds, 1)
    while True:
        await asyncio.sleep(interval)
        try:
            await service.expire_overdue_orders()
        except Exception:
            # Keep the webhook service alive even if one expiration pass fails.
            pass


class TenantUpsertRequest(BaseModel):
    tenant_id: str | None = None
    name: str | None = None
    active: bool | None = None
    ticket_tailor_api_key: str | None = None
    ticket_tailor_webhook_signing_secret: str | None = None
    ticket_tailor_offline_payment_keywords: str | list[str] | None = None
    nicky_api_key: str | None = None
    nicky_default_blockchain_asset_id: str | None = None
    nicky_receiver_short_id: str | None = None
    nicky_webhook_token: str | None = None
    nicky_webhook_type: int | None = None
    auto_create_nicky_payment_request: bool | None = None
    auto_confirm_ticket_tailor_payments: bool | None = None
    nicky_send_notification: bool | None = None
    skip_nicky: bool | None = None
    dry_run: bool | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    db.bootstrap_default_tenant(settings)
    expiration_task: asyncio.Task[None] | None = None
    if settings.ticket_tailor_pending_ticket_expiration_hours > 0:
        expiration_task = asyncio.create_task(expire_overdue_orders_loop())
    try:
        yield
    finally:
        if expiration_task:
            expiration_task.cancel()
            try:
                await expiration_task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title="Nicky Ticket Tailor Integration",
    version="0.2.0",
    lifespan=lifespan,
)


def model_data(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    if settings.admin_token and x_admin_token != settings.admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token")


def get_tenant_or_404(tenant_id: str, *, require_active: bool = False) -> TenantConfig:
    try:
        normalized_tenant_id = normalize_tenant_id(tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    tenant = db.get_tenant(normalized_tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if require_active and not tenant.active:
        raise HTTPException(status_code=403, detail="Tenant is inactive")
    return tenant


def build_tenant_config(tenant_id: str, payload: TenantUpsertRequest) -> TenantConfig:
    try:
        normalized_tenant_id = normalize_tenant_id(tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    base = db.get_tenant(normalized_tenant_id) or tenant_from_settings(
        settings, normalized_tenant_id
    )
    updates = model_data(payload)
    updates.pop("tenant_id", None)
    if "ticket_tailor_offline_payment_keywords" in updates:
        updates["ticket_tailor_offline_payment_keywords"] = parse_keywords(
            updates["ticket_tailor_offline_payment_keywords"]
        )
    updates = {key: value for key, value in updates.items() if value is not None}
    if not updates.get("name") and base.name == settings.default_tenant_id:
        updates.setdefault("name", normalized_tenant_id)
    return replace(base, tenant_id=normalized_tenant_id, **updates)


def require_nicky_token(
    tenant: TenantConfig,
    *,
    token: str | None,
    x_nicky_webhook_token: str | None,
) -> None:
    if tenant.nicky_webhook_token:
        supplied = x_nicky_webhook_token or token
        if supplied != tenant.nicky_webhook_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Nicky webhook token",
            )


def nicky_webhook_url(tenant: TenantConfig, url: str | None = None) -> str:
    webhook_url = url or f"{settings.app_base_url}/webhooks/nicky/{tenant.tenant_id}"
    if tenant.nicky_webhook_token and "token=" not in webhook_url:
        separator = "&" if "?" in webhook_url else "?"
        webhook_url = f"{webhook_url}{separator}token={tenant.nicky_webhook_token}"
    return webhook_url


async def parse_json_body(request: Request) -> tuple[bytes, dict[str, Any]]:
    raw_body = await request.body()
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return raw_body, body


@app.get("/health")
async def health() -> dict[str, Any]:
    tenants = db.list_tenants()
    active_tenants = [tenant for tenant in tenants if tenant.active]
    return {
        "status": "ok",
        "env": settings.app_env,
        "default_tenant_id": settings.default_tenant_id,
        "tenant_count": len(tenants),
        "active_tenant_count": len(active_tenants),
        "dry_run": settings.dry_run,
        "ticket_tailor_pending_ticket_expiration_hours": (
            settings.ticket_tailor_pending_ticket_expiration_hours
        ),
        "ticket_tailor_expiration_check_interval_seconds": (
            settings.ticket_tailor_expiration_check_interval_seconds
        ),
        "ticket_tailor_expiration_batch_size": (
            settings.ticket_tailor_expiration_batch_size
        ),
        "nicky_configured_tenant_count": sum(tenant.nicky_configured for tenant in tenants),
        "ticket_tailor_configured_tenant_count": sum(
            tenant.ticket_tailor_configured for tenant in tenants
        ),
    }


@app.get("/payment-info", response_class=HTMLResponse)
async def payment_info() -> str:
    return """
    <!doctype html>
    <html lang="en">
      <head><meta charset="utf-8"><title>Nicky payment pending</title></head>
      <body style="font-family: system-ui, sans-serif; max-width: 680px; margin: 48px auto;">
        <h1>Payment pending</h1>
        <p>Your Ticket Tailor order has been created with Nicky as an offline payment method.</p>
        <p>If this integration is configured to send Nicky notifications, check your email for the payment request.</p>
      </body>
    </html>
    """


@app.get("/nicky/success", response_class=HTMLResponse)
async def nicky_success() -> str:
    return """
    <!doctype html>
    <html lang="en">
      <head><meta charset="utf-8"><title>Nicky payment received</title></head>
      <body style="font-family: system-ui, sans-serif; max-width: 680px; margin: 48px auto;">
        <h1>Payment received</h1>
        <p>Nicky has received the payment. Ticket Tailor will be updated by the webhook flow.</p>
      </body>
    </html>
    """


@app.get("/nicky/cancel", response_class=HTMLResponse)
async def nicky_cancel() -> str:
    return """
    <!doctype html>
    <html lang="en">
      <head><meta charset="utf-8"><title>Nicky payment canceled</title></head>
      <body style="font-family: system-ui, sans-serif; max-width: 680px; margin: 48px auto;">
        <h1>Payment canceled</h1>
        <p>The payment was canceled or abandoned. Your Ticket Tailor order may remain pending.</p>
      </body>
    </html>
    """


async def handle_ticket_tailor_webhook(
    tenant_id: str,
    request: Request,
    tickettailor_webhook_signature: str | None,
) -> dict[str, Any]:
    tenant = get_tenant_or_404(tenant_id, require_active=True)
    raw_body, body = await parse_json_body(request)
    try:
        verify_ticket_tailor_signature(
            raw_body=raw_body,
            header=tickettailor_webhook_signature,
            shared_secret=tenant.ticket_tailor_webhook_signing_secret,
            tolerance_seconds=settings.ticket_tailor_webhook_tolerance_seconds,
        )
    except SignatureError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    try:
        return await service.process_ticket_tailor_webhook(tenant, body, raw_body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/webhooks/ticket-tailor")
async def ticket_tailor_webhook_default(
    request: Request,
    tickettailor_webhook_signature: str | None = Header(
        default=None, alias="Tickettailor-Webhook-Signature"
    ),
) -> dict[str, Any]:
    return await handle_ticket_tailor_webhook(
        settings.default_tenant_id, request, tickettailor_webhook_signature
    )


@app.post("/webhooks/ticket-tailor/{tenant_id}")
async def ticket_tailor_webhook_for_tenant(
    tenant_id: str,
    request: Request,
    tickettailor_webhook_signature: str | None = Header(
        default=None, alias="Tickettailor-Webhook-Signature"
    ),
) -> dict[str, Any]:
    return await handle_ticket_tailor_webhook(
        tenant_id, request, tickettailor_webhook_signature
    )


async def handle_nicky_webhook(
    tenant_id: str,
    request: Request,
    token: str | None,
    x_nicky_webhook_token: str | None,
) -> dict[str, Any]:
    tenant = get_tenant_or_404(tenant_id, require_active=True)
    require_nicky_token(
        tenant,
        token=token,
        x_nicky_webhook_token=x_nicky_webhook_token,
    )
    raw_body, body = await parse_json_body(request)
    try:
        return await service.process_nicky_webhook(tenant, body, raw_body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/webhooks/nicky")
async def nicky_webhook_default(
    request: Request,
    token: str | None = Query(default=None),
    x_nicky_webhook_token: str | None = Header(default=None),
) -> dict[str, Any]:
    return await handle_nicky_webhook(
        settings.default_tenant_id, request, token, x_nicky_webhook_token
    )


@app.post("/webhooks/nicky/{tenant_id}")
async def nicky_webhook_for_tenant(
    tenant_id: str,
    request: Request,
    token: str | None = Query(default=None),
    x_nicky_webhook_token: str | None = Header(default=None),
) -> dict[str, Any]:
    return await handle_nicky_webhook(tenant_id, request, token, x_nicky_webhook_token)


@app.get("/orders")
async def list_orders(
    _: None = Depends(require_admin),
    limit: int = 50,
    tenant_id: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    try:
        normalized_tenant_id = normalize_tenant_id(tenant_id) if tenant_id else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [
        row_to_dict(row)
        for row in db.list_orders(limit=limit, tenant_id=normalized_tenant_id)
    ]


@app.get("/orders/{ticket_tailor_order_id}")
async def get_order(
    ticket_tailor_order_id: str,
    _: None = Depends(require_admin),
    tenant_id: str = Query(default=settings.default_tenant_id),
) -> dict[str, Any]:
    try:
        normalized_tenant_id = normalize_tenant_id(tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = db.get_order(normalized_tenant_id, ticket_tailor_order_id)
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    return row_to_dict(row)


@app.get("/admin/tenants")
async def admin_list_tenants(_: None = Depends(require_admin)) -> list[dict[str, Any]]:
    return [tenant_to_safe_dict(tenant) for tenant in db.list_tenants()]


@app.post("/admin/tenants")
async def admin_upsert_tenant(
    payload: TenantUpsertRequest, _: None = Depends(require_admin)
) -> dict[str, Any]:
    if not payload.tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")
    tenant = build_tenant_config(payload.tenant_id, payload)
    db.upsert_tenant(tenant)
    return tenant_to_safe_dict(tenant)


@app.get("/admin/tenants/{tenant_id}")
async def admin_get_tenant(
    tenant_id: str, _: None = Depends(require_admin)
) -> dict[str, Any]:
    return tenant_to_safe_dict(get_tenant_or_404(tenant_id))


@app.put("/admin/tenants/{tenant_id}")
async def admin_update_tenant(
    tenant_id: str,
    payload: TenantUpsertRequest,
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    tenant = build_tenant_config(tenant_id, payload)
    db.upsert_tenant(tenant)
    return tenant_to_safe_dict(tenant)


@app.post("/admin/orders/{ticket_tailor_order_id}/create-nicky-payment-request")
async def admin_create_default_nicky_payment_request(
    ticket_tailor_order_id: str, _: None = Depends(require_admin)
) -> dict[str, Any]:
    tenant = get_tenant_or_404(settings.default_tenant_id)
    try:
        return await service.create_nicky_payment_request(tenant, ticket_tailor_order_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/admin/tenants/{tenant_id}/orders/{ticket_tailor_order_id}/create-nicky-payment-request"
)
async def admin_create_tenant_nicky_payment_request(
    tenant_id: str,
    ticket_tailor_order_id: str,
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    tenant = get_tenant_or_404(tenant_id)
    try:
        return await service.create_nicky_payment_request(tenant, ticket_tailor_order_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/admin/orders/{ticket_tailor_order_id}/confirm-ticket-tailor-payment")
async def admin_confirm_default_ticket_tailor_payment(
    ticket_tailor_order_id: str, _: None = Depends(require_admin)
) -> dict[str, Any]:
    tenant = get_tenant_or_404(settings.default_tenant_id)
    try:
        return await service.confirm_ticket_tailor_payment(tenant, ticket_tailor_order_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/admin/tenants/{tenant_id}/orders/{ticket_tailor_order_id}/confirm-ticket-tailor-payment"
)
async def admin_confirm_tenant_ticket_tailor_payment(
    tenant_id: str,
    ticket_tailor_order_id: str,
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    tenant = get_tenant_or_404(tenant_id)
    try:
        return await service.confirm_ticket_tailor_payment(tenant, ticket_tailor_order_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/admin/expire-overdue-orders")
async def admin_expire_overdue_orders(
    _: None = Depends(require_admin),
    tenant_id: str | None = Query(default=None),
    expiration_hours: float | None = Query(default=None),
    batch_size: int | None = Query(default=None),
) -> dict[str, Any]:
    try:
        normalized_tenant_id = normalize_tenant_id(tenant_id) if tenant_id else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await service.expire_overdue_orders(
        tenant_id=normalized_tenant_id,
        expiration_hours=expiration_hours,
        batch_size=batch_size,
    )


@app.post("/admin/nicky/webhooks")
async def admin_create_default_nicky_webhook(
    url: str | None = None, _: None = Depends(require_admin)
) -> dict[str, Any]:
    tenant = get_tenant_or_404(settings.default_tenant_id)
    return await nicky_client.create_webhook(tenant, nicky_webhook_url(tenant, url))


@app.post("/admin/tenants/{tenant_id}/nicky/webhooks")
async def admin_create_tenant_nicky_webhook(
    tenant_id: str,
    url: str | None = None,
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    tenant = get_tenant_or_404(tenant_id)
    return await nicky_client.create_webhook(tenant, nicky_webhook_url(tenant, url))


@app.post("/admin/nicky/webhooks/test-status-change")
async def admin_test_default_nicky_status_change(
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    tenant = get_tenant_or_404(settings.default_tenant_id)
    return await nicky_client.test_status_change_webhook(tenant)


@app.post("/admin/tenants/{tenant_id}/nicky/webhooks/test-status-change")
async def admin_test_tenant_nicky_status_change(
    tenant_id: str,
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    tenant = get_tenant_or_404(tenant_id)
    return await nicky_client.test_status_change_webhook(tenant)
