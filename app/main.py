from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from app import admin_auth
from app.admin_ui import create_admin_ui_router
from app.config import Settings, get_settings
from app.db import Database
from app.nicky import NickyApiError, NickyClient
from app.security import SignatureError, verify_ticket_tailor_signature
from app.service import IntegrationService, row_to_dict
from app.tenants import (
    NICKY_WEBHOOK_TYPE,
    TenantConfig,
    normalize_tenant_id,
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
    nicky_api_key: str | None = None
    nicky_user_email: str | None = None
    nicky_default_blockchain_asset_id: str | None = None


class NickyApiKeyValidationRequest(BaseModel):
    nicky_api_key: str


class TicketTailorApiKeyValidationRequest(BaseModel):
    ticket_tailor_api_key: str


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
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
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.admin_session_secret,
    same_site="lax",
    https_only=settings.app_base_url.startswith("https://"),
    max_age=settings.admin_session_max_age_seconds,
)


def model_data(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=True)
    return model.dict(exclude_unset=True)


def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> admin_auth.AdminUser | None:
    user = admin_auth.authenticate_admin_request(
        settings,
        request,
        authorization=authorization,
    )
    if user:
        return user

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin authentication required")


def require_admin_role(user: admin_auth.AdminUser) -> None:
    if not admin_auth.is_admin(user, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")


def require_writer(user: admin_auth.AdminUser) -> None:
    if admin_auth.is_support(user) and not admin_auth.is_admin(user, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Support access is read-only")


def scoped_nicky_user_uuid(user: admin_auth.AdminUser) -> str | None:
    if admin_auth.is_privileged(user, settings):
        return None
    return admin_auth.nicky_user_uuid_claim(user)


def scoped_owner_auth_subject(user: admin_auth.AdminUser) -> str | None:
    if admin_auth.is_privileged(user, settings):
        return None
    if admin_auth.nicky_user_uuid_claim(user):
        return None
    return user.subject


def scoped_tenant_id(user: admin_auth.AdminUser, requested: str | None = None) -> str | None:
    owner_uuid = scoped_nicky_user_uuid(user)
    if admin_auth.is_privileged(user, settings):
        return requested
    if owner_uuid:
        if requested and requested != owner_uuid:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant outside user scope")
        return owner_uuid
    if requested:
        tenant = db.get_tenant(requested)
        if tenant and tenant.owner_auth_subject == user.subject:
            return requested
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant outside user scope")
    tenants = db.list_tenants(owner_auth_subject=user.subject, limit=1)
    if tenants:
        return tenants[0].tenant_id
    return "__nicky_no_tenant_scope__"


app.include_router(
    create_admin_ui_router(
        settings=settings,
        db=db,
        nicky_client=nicky_client,
        service=service,
        require_admin=require_admin,
    )
)


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


def generate_tenant_id() -> str:
    for _ in range(10):
        tenant_id = normalize_tenant_id(f"tenant-{secrets.token_hex(8)}")
        if not db.get_tenant(tenant_id):
            return tenant_id
    raise HTTPException(status_code=500, detail="Could not generate tenant id")


def ensure_unique_active_api_keys(
    *,
    nicky_api_key: str,
    ticket_tailor_api_key: str,
    exclude_tenant_id: str | None = None,
) -> None:
    nicky_conflict = db.find_active_tenant_by_api_key(
        "nicky_api_key",
        nicky_api_key,
        exclude_tenant_id=exclude_tenant_id,
    )
    if nicky_conflict:
        raise HTTPException(status_code=409, detail="Nicky API key is already used by an active tenant")
    ticket_tailor_conflict = db.find_active_tenant_by_api_key(
        "ticket_tailor_api_key",
        ticket_tailor_api_key,
        exclude_tenant_id=exclude_tenant_id,
    )
    if ticket_tailor_conflict:
        raise HTTPException(
            status_code=409,
            detail="Ticket Tailor API key is already used by an active tenant",
        )


async def build_tenant_config(
    tenant_id: str | None,
    payload: TenantUpsertRequest,
    user: admin_auth.AdminUser,
) -> TenantConfig:
    require_writer(user)
    requested_tenant_id = tenant_id or payload.tenant_id
    existing = db.get_tenant(normalize_tenant_id(requested_tenant_id)) if requested_tenant_id else None
    api_key = payload.nicky_api_key or (existing.nicky_api_key if existing else "")
    if not api_key:
        raise HTTPException(status_code=400, detail="Nicky API key is required")

    try:
        nicky_validation = await nicky_client.validate_api_key(api_key)
    except NickyApiError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid Nicky API key: {exc}") from exc

    raw_nicky_user_uuid = str(nicky_validation.get("nicky_user_uuid") or "").strip()
    raw_nicky_user_short_id = str(nicky_validation.get("nicky_user_short_id") or "").strip()
    raw_nicky_user_email = str(nicky_validation.get("nicky_user_email") or "").strip()
    auth0_identifier = admin_auth.user_identifier(user)
    updates = model_data(payload)
    updates.pop("tenant_id", None)
    updates = {key: value for key, value in updates.items() if value is not None}
    nicky_user_email = (
        raw_nicky_user_email
        or str(updates.get("nicky_user_email") or "").strip()
        or ("" if admin_auth.is_admin(user, settings) else auth0_identifier)
    )
    if not nicky_user_email:
        raise HTTPException(status_code=400, detail="Nicky email is required")
    resolved_tenant_id = (
        requested_tenant_id
        if requested_tenant_id
        else raw_nicky_user_uuid
        if raw_nicky_user_uuid
        else generate_tenant_id()
    )
    try:
        normalized_tenant_id = normalize_tenant_id(resolved_tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    base = existing or db.get_tenant(normalized_tenant_id) or tenant_from_settings(settings, normalized_tenant_id)
    if (
        not admin_auth.is_admin(user, settings)
        and base.owner_auth_subject
        and base.owner_auth_subject != user.subject
    ):
        raise HTTPException(status_code=403, detail="Tenant outside user scope")
    asset_id = str(updates.get("nicky_default_blockchain_asset_id") or base.nicky_default_blockchain_asset_id or "")
    available_asset_ids = {str(asset.get("id") or "") for asset in nicky_validation.get("assets") or []}
    if not asset_id:
        raise HTTPException(status_code=400, detail="Nicky asset is required")
    if available_asset_ids and asset_id not in available_asset_ids:
        raise HTTPException(status_code=400, detail="Selected asset is not available for this Nicky API key")
    ticket_tailor_api_key = str(updates.get("ticket_tailor_api_key") or base.ticket_tailor_api_key)
    ensure_unique_active_api_keys(
        nicky_api_key=api_key,
        ticket_tailor_api_key=ticket_tailor_api_key,
        exclude_tenant_id=normalized_tenant_id,
    )

    webhook_token = base.nicky_webhook_token or secrets.token_urlsafe(24)
    return replace(
        base,
        tenant_id=normalized_tenant_id,
        name=str(updates.get("name") or raw_nicky_user_short_id or nicky_user_email or normalized_tenant_id),
        active=bool(updates.get("active", True)),
        nicky_user_uuid=raw_nicky_user_uuid,
        nicky_user_short_id=raw_nicky_user_short_id,
        nicky_user_email=nicky_user_email,
        ticket_tailor_api_key=ticket_tailor_api_key,
        ticket_tailor_webhook_signing_secret="",
        nicky_api_key=api_key,
        nicky_default_blockchain_asset_id=asset_id,
        nicky_receiver_short_id=raw_nicky_user_short_id,
        nicky_webhook_token=webhook_token,
        nicky_webhook_type=NICKY_WEBHOOK_TYPE,
        nicky_send_notification=True,
        owner_auth_subject=base.owner_auth_subject if admin_auth.is_admin(user, settings) else user.subject,
    )


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
        "tenant_count": len(tenants),
        "active_tenant_count": len(active_tenants),
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
    raise HTTPException(status_code=404, detail="Tenant id is required")


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
    raise HTTPException(status_code=404, detail="Tenant id is required")


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
    user: admin_auth.AdminUser = Depends(require_admin),
    limit: int = 50,
    tenant_id: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    try:
        normalized_tenant_id = normalize_tenant_id(tenant_id) if tenant_id else None
        normalized_tenant_id = scoped_tenant_id(user, normalized_tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [
        row_to_dict(row)
        for row in db.list_orders(limit=limit, tenant_id=normalized_tenant_id)
    ]


@app.get("/orders/{ticket_tailor_order_id}")
async def get_order(
    ticket_tailor_order_id: str,
    user: admin_auth.AdminUser = Depends(require_admin),
    tenant_id: str = Query(...),
) -> dict[str, Any]:
    try:
        normalized_tenant_id = normalize_tenant_id(tenant_id)
        normalized_tenant_id = scoped_tenant_id(user, normalized_tenant_id) or normalized_tenant_id
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = db.get_order(normalized_tenant_id, ticket_tailor_order_id)
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    return row_to_dict(row)


@app.get("/admin/tenants")
async def admin_list_tenants(user: admin_auth.AdminUser = Depends(require_admin)) -> list[dict[str, Any]]:
    return [
        tenant_to_safe_dict(tenant)
        for tenant in db.list_tenants(
            nicky_user_uuid=scoped_nicky_user_uuid(user),
            owner_auth_subject=scoped_owner_auth_subject(user),
        )
    ]


@app.post("/admin/tenants")
async def admin_upsert_tenant(
    payload: TenantUpsertRequest, user: admin_auth.AdminUser = Depends(require_admin)
) -> dict[str, Any]:
    tenant = await build_tenant_config(payload.tenant_id, payload, user)
    db.upsert_tenant(tenant)
    await nicky_client.create_webhook(tenant, nicky_webhook_url(tenant))
    return tenant_to_safe_dict(tenant)


@app.get("/admin/tenants/{tenant_id}")
async def admin_get_tenant(
    tenant_id: str, user: admin_auth.AdminUser = Depends(require_admin)
) -> dict[str, Any]:
    scoped = scoped_tenant_id(user, normalize_tenant_id(tenant_id))
    return tenant_to_safe_dict(get_tenant_or_404(scoped or tenant_id))


@app.put("/admin/tenants/{tenant_id}")
async def admin_update_tenant(
    tenant_id: str,
    payload: TenantUpsertRequest,
    user: admin_auth.AdminUser = Depends(require_admin),
) -> dict[str, Any]:
    scoped_tenant_id(user, normalize_tenant_id(tenant_id))
    tenant = await build_tenant_config(tenant_id, payload, user)
    db.upsert_tenant(tenant)
    await nicky_client.create_webhook(tenant, nicky_webhook_url(tenant))
    return tenant_to_safe_dict(tenant)


@app.delete("/admin/tenants/{tenant_id}")
async def admin_delete_tenant(
    tenant_id: str,
    user: admin_auth.AdminUser = Depends(require_admin),
) -> dict[str, Any]:
    require_writer(user)
    scoped = scoped_tenant_id(user, normalize_tenant_id(tenant_id))
    tenant = get_tenant_or_404(scoped or tenant_id)
    db.deactivate_tenant(tenant.tenant_id)
    return {"status": "deactivated", "tenant_id": tenant.tenant_id}


@app.post("/admin/nicky/validate-api-key")
async def admin_validate_nicky_api_key(
    payload: NickyApiKeyValidationRequest,
    user: admin_auth.AdminUser = Depends(require_admin),
) -> dict[str, Any]:
    try:
        validation = await nicky_client.validate_api_key(payload.nicky_api_key)
    except NickyApiError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid Nicky API key: {exc}") from exc
    auth0_identifier = "" if admin_auth.is_admin(user, settings) else admin_auth.user_identifier(user)
    owner_uuid = str(validation.get("nicky_user_uuid") or "")
    short_id = str(validation.get("nicky_user_short_id") or "")
    email = str(validation.get("nicky_user_email") or "") or auth0_identifier
    nicky_conflict = db.find_active_tenant_by_api_key("nicky_api_key", payload.nicky_api_key)
    if nicky_conflict:
        raise HTTPException(status_code=409, detail="Nicky API key is already used by an active tenant")
    return {
        "nicky_user_uuid": owner_uuid,
        "nicky_user_short_id": short_id,
        "nicky_user_email": email,
        "assets": validation.get("assets") or [],
    }


@app.post("/admin/ticket-tailor/validate-api-key")
async def admin_validate_ticket_tailor_api_key(
    payload: TicketTailorApiKeyValidationRequest,
    user: admin_auth.AdminUser = Depends(require_admin),
) -> dict[str, Any]:
    conflict = db.find_active_tenant_by_api_key(
        "ticket_tailor_api_key",
        payload.ticket_tailor_api_key,
    )
    if conflict:
        raise HTTPException(
            status_code=409,
            detail="Ticket Tailor API key is already used by an active tenant",
        )
    try:
        return await ticket_tailor_client.validate_api_key(payload.ticket_tailor_api_key)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid Ticket Tailor API key: {exc}") from exc


@app.post("/admin/orders/{ticket_tailor_order_id}/create-nicky-payment-request")
async def admin_create_default_nicky_payment_request(
    ticket_tailor_order_id: str, _: None = Depends(require_admin)
) -> dict[str, Any]:
    raise HTTPException(status_code=404, detail="Tenant id is required")


@app.post(
    "/admin/tenants/{tenant_id}/orders/{ticket_tailor_order_id}/create-nicky-payment-request"
)
async def admin_create_tenant_nicky_payment_request(
    tenant_id: str,
    ticket_tailor_order_id: str,
    user: admin_auth.AdminUser = Depends(require_admin),
) -> dict[str, Any]:
    require_admin_role(user)
    tenant = get_tenant_or_404(tenant_id)
    try:
        return await service.create_nicky_payment_request(tenant, ticket_tailor_order_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/admin/orders/{ticket_tailor_order_id}/confirm-ticket-tailor-payment")
async def admin_confirm_default_ticket_tailor_payment(
    ticket_tailor_order_id: str, _: None = Depends(require_admin)
) -> dict[str, Any]:
    raise HTTPException(status_code=404, detail="Tenant id is required")


@app.post(
    "/admin/tenants/{tenant_id}/orders/{ticket_tailor_order_id}/confirm-ticket-tailor-payment"
)
async def admin_confirm_tenant_ticket_tailor_payment(
    tenant_id: str,
    ticket_tailor_order_id: str,
    user: admin_auth.AdminUser = Depends(require_admin),
) -> dict[str, Any]:
    require_admin_role(user)
    tenant = get_tenant_or_404(tenant_id)
    try:
        return await service.confirm_ticket_tailor_payment(tenant, ticket_tailor_order_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/admin/expire-overdue-orders")
async def admin_expire_overdue_orders(
    user: admin_auth.AdminUser = Depends(require_admin),
    tenant_id: str | None = Query(default=None),
    expiration_hours: float | None = Query(default=None),
    batch_size: int | None = Query(default=None),
) -> dict[str, Any]:
    try:
        normalized_tenant_id = normalize_tenant_id(tenant_id) if tenant_id else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not admin_auth.is_privileged(user, settings):
        normalized_tenant_id = scoped_tenant_id(user, normalized_tenant_id)
    return await service.expire_overdue_orders(
        tenant_id=normalized_tenant_id,
        expiration_hours=expiration_hours,
        batch_size=batch_size,
    )


@app.post("/admin/nicky/webhooks")
async def admin_create_default_nicky_webhook(
    url: str | None = None, _: None = Depends(require_admin)
) -> dict[str, Any]:
    raise HTTPException(status_code=404, detail="Tenant id is required")


@app.post("/admin/tenants/{tenant_id}/nicky/webhooks")
async def admin_create_tenant_nicky_webhook(
    tenant_id: str,
    url: str | None = None,
    user: admin_auth.AdminUser = Depends(require_admin),
) -> dict[str, Any]:
    require_admin_role(user)
    tenant = get_tenant_or_404(tenant_id)
    return await nicky_client.create_webhook(tenant, nicky_webhook_url(tenant, url))


@app.post("/admin/nicky/webhooks/test-status-change")
async def admin_test_default_nicky_status_change(
    _: None = Depends(require_admin),
) -> dict[str, Any]:
    raise HTTPException(status_code=404, detail="Tenant id is required")


@app.post("/admin/tenants/{tenant_id}/nicky/webhooks/test-status-change")
async def admin_test_tenant_nicky_status_change(
    tenant_id: str,
    user: admin_auth.AdminUser = Depends(require_admin),
) -> dict[str, Any]:
    require_admin_role(user)
    tenant = get_tenant_or_404(tenant_id)
    return await nicky_client.test_status_change_webhook(tenant)
