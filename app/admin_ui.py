from __future__ import annotations

import datetime
import html
import json
import secrets
import urllib.parse
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app import admin_auth
from app.config import Settings
from app.db import Database
from app.nicky import NickyClient
from app.service import IntegrationService, row_to_dict
from app.tenants import (
    NICKY_PAYMENT_KEYWORDS,
    NICKY_WEBHOOK_TYPE,
    TenantConfig,
    normalize_tenant_id,
    tenant_from_settings,
    tenant_to_safe_dict,
)


AdminDependency = Callable[..., Any]
DEFAULT_PAGE_SIZE = 10
DASHBOARD_PAGE_SIZE = 5


def create_admin_ui_router(
    *,
    settings: Settings,
    db: Database,
    nicky_client: NickyClient,
    service: IntegrationService,
    require_admin: AdminDependency,
) -> APIRouter:
    router = APIRouter(tags=["admin-ui"])
    static_dir = Path(__file__).parent / "static"

    @router.get("/admin-ui/assets/nicky-logo.svg")
    async def nicky_logo():
        return FileResponse(static_dir / "nicky-logo.svg", media_type="image/svg+xml")

    def require_admin_web(request: Request) -> admin_auth.AdminUser:
        user = admin_auth.authenticate_admin_request(
            settings,
            request,
            x_admin_token=request.headers.get("x-admin-token"),
            authorization=request.headers.get("authorization"),
        )
        if user:
            return user
        return_to = request_path_with_query(request) if request.method == "GET" else "/admin-ui"
        login_location = f"/admin-ui/login?return_to={urllib.parse.quote(return_to, safe='')}"
        raise HTTPException(
            status_code=303,
            detail="Admin login required",
            headers={"Location": login_location},
        )

    @router.get("/admin-ui/login", response_class=HTMLResponse)
    async def login(request: Request):
        return_to = admin_auth.safe_return_to(settings, request.query_params.get("return_to"))
        if admin_auth.auth0_enabled(settings):
            return RedirectResponse(
                admin_auth.build_auth0_authorize_url(settings, request, return_to=return_to),
                status_code=303,
            )
        raise HTTPException(status_code=503, detail="Auth0 is required but is not configured")

    @router.post("/admin-ui/login/admin-token")
    async def login_admin_token(request: Request):
        raise HTTPException(status_code=404, detail="Local admin-token UI login is disabled")

    async def complete_auth0_login(
        request: Request, code: str | None = None, state: str | None = None
    ):
        if not admin_auth.auth0_enabled(settings):
            raise HTTPException(status_code=404, detail="Auth0 is not configured")
        if not code:
            raise HTTPException(status_code=400, detail="Missing Auth0 code")

        expected_nonce = admin_auth.validate_callback_state(request, state)
        token_response = await admin_auth.exchange_auth0_code(
            settings,
            code,
            code_verifier=admin_auth.get_code_verifier(request),
        )
        id_token = token_response.get("id_token")
        if not id_token:
            raise HTTPException(status_code=401, detail="Auth0 did not return an id_token")

        claims = admin_auth.decode_and_verify_jwt(
            settings, str(id_token), audience=settings.auth0_client_id
        )
        admin_auth.assert_nonce(claims, expected_nonce)
        user = admin_auth.user_from_claims(claims, auth_method="auth0")

        return_to = admin_auth.consume_return_to(request, settings)
        admin_auth.clear_session(request)
        admin_auth.set_session_user(request, user)
        return RedirectResponse(return_to, status_code=303)

    @router.get("/admin-ui/callback")
    @router.get("/authentication/login-callback")
    async def auth0_callback(
        request: Request, code: str | None = None, state: str | None = None
    ):
        return await complete_auth0_login(request, code, state)

    @router.get("/admin-ui/logout")
    async def logout(request: Request):
        admin_auth.clear_session(request)
        if admin_auth.auth0_enabled(settings):
            return RedirectResponse(admin_auth.build_auth0_logout_url(settings), status_code=303)
        return RedirectResponse("/admin-ui/login", status_code=303)

    @router.get("/admin-ui", response_class=HTMLResponse)
    @router.get("/admin-ui/", response_class=HTMLResponse)
    async def dashboard(
        request: Request, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        owner_uuid = scoped_owner_uuid(user, settings)
        tenants = db.list_tenants(nicky_user_uuid=owner_uuid)
        order_filters = scoped_order_filters(dashboard_order_filters(request), owner_uuid)
        webhook_filters = scoped_webhook_filters(dashboard_webhook_filters(request), owner_uuid)
        tenants_page_number = page_query_value(request.query_params.get("tenants_page"))
        orders_page_number = page_query_value(request.query_params.get("orders_page"))
        webhooks_page_number = page_query_value(request.query_params.get("webhooks_page"))
        tenants_total = db.count_tenants(nicky_user_uuid=owner_uuid)
        orders_total = db.count_orders(**order_filters)
        webhooks_total = db.count_webhook_events(**webhook_filters)
        dashboard_tenants = db.list_tenants(
            limit=DASHBOARD_PAGE_SIZE,
            offset=page_offset(tenants_page_number, DASHBOARD_PAGE_SIZE),
            nicky_user_uuid=owner_uuid,
        )
        orders = [
            row_to_dict(row)
            for row in db.list_orders(
                limit=DASHBOARD_PAGE_SIZE,
                offset=page_offset(orders_page_number, DASHBOARD_PAGE_SIZE),
                **order_filters,
            )
        ]
        webhooks = [
            dict(row)
            for row in db.list_webhook_events(
                limit=DASHBOARD_PAGE_SIZE,
                offset=page_offset(webhooks_page_number, DASHBOARD_PAGE_SIZE),
                **webhook_filters,
            )
        ]
        body = f"""
        <section class="mb-6 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <h1 class="text-2xl font-semibold leading-8 text-slate-950">Integration dashboard</h1>
            <p class="mt-2 text-sm text-slate-500">Operational view for Ticket Tailor offline payments and Nicky Payment Requests.</p>
          </div>
          {new_tenant_link(user, tenants, settings)}
        </section>
        {summary_grid(tenants, orders, webhooks)}
        <section class="mb-7 min-w-0">
          <h2 class="mb-3 text-lg font-semibold text-slate-950">Core tenant mapping</h2>
          <div class="overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky">
            {tenant_table(dashboard_tenants, user=user, settings=settings, framed=False)}
            {pagination_controls(tenants_page_number, DASHBOARD_PAGE_SIZE, tenants_total, "/overview", request.query_params, "tenants_page")}
          </div>
        </section>
        <section class="mb-7 min-w-0">
          <h2 class="mb-3 text-lg font-semibold text-slate-950">Recent webhooks</h2>
          <div class="overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky">
            {webhook_filters_form(webhook_filters, order_filters, tenants, action="/overview", show_tenant_filter=owner_uuid is None)}
            {webhook_table(webhooks)}
            {pagination_controls(webhooks_page_number, DASHBOARD_PAGE_SIZE, webhooks_total, "/overview", request.query_params, "webhooks_page")}
          </div>
        </section>
        <section class="mb-7 min-w-0">
          <h2 class="mb-3 text-lg font-semibold text-slate-950">Recent orders</h2>
          <div class="overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky">
            {order_filters_form(order_filters, webhook_filters, tenants, action="/overview", show_tenant_filter=owner_uuid is None)}
            {orders_table(orders)}
            {pagination_controls(orders_page_number, DASHBOARD_PAGE_SIZE, orders_total, "/overview", request.query_params, "orders_page")}
          </div>
        </section>
        """
        return html_response(render(request, "Dashboard", body, current_path="/admin-ui"))

    @router.get("/overview", response_class=HTMLResponse)
    @router.get("/overview/", response_class=HTMLResponse)
    async def overview(
        request: Request, code: str | None = None, state: str | None = None
    ):
        if code or state:
            return await complete_auth0_login(request, code, state)
        require_admin_web(request)
        return await dashboard(request, require_admin_web(request))

    @router.get("/admin-ui/tenants", response_class=HTMLResponse)
    async def tenants_page(
        request: Request, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        owner_uuid = scoped_owner_uuid(user, settings)
        filters = tenant_page_filters(request)
        page_number = page_query_value(request.query_params.get("page"))
        total = db.count_tenants(**filters, nicky_user_uuid=owner_uuid)
        tenants = db.list_tenants(
            limit=DEFAULT_PAGE_SIZE,
            offset=page_offset(page_number, DEFAULT_PAGE_SIZE),
            **filters,
            nicky_user_uuid=owner_uuid,
        )
        body = f"""
        <section class="mb-6 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <h1 class="text-2xl font-semibold leading-8 text-slate-950">Core tenant mapping</h1>
            <p class="mt-2 text-sm text-slate-500">Map each Ticket Tailor account to the Nicky account that receives payments.</p>
          </div>
          {new_tenant_link(user, tenants, settings)}
        </section>
        <div class="overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky">
          {tenant_filters_form(filters)}
          {tenant_table(tenants, user=user, settings=settings, framed=False)}
          {pagination_controls(page_number, DEFAULT_PAGE_SIZE, total, "/admin-ui/tenants", request.query_params, "page")}
        </div>
        """
        return html_response(render(request, "Tenants", body, current_path="/admin-ui/tenants"))

    @router.get("/admin-ui/tenants/new", response_class=HTMLResponse)
    async def new_tenant(
        request: Request, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        if admin_auth.is_support(user) and not admin_auth.is_admin(user, settings):
            raise HTTPException(status_code=403, detail="Support access is read-only")
        tenant_id = "new-tenant" if admin_auth.is_admin(user, settings) else admin_auth.nicky_user_uuid(user)
        tenant = tenant_from_settings(settings, tenant_id)
        return html_response(
            render(
                request,
                "New tenant",
                tenant_form(tenant, is_new=True, settings=settings, user=user),
                current_path="/admin-ui/tenants",
            )
        )

    @router.get("/admin-ui/tenants/{tenant_id}/edit", response_class=HTMLResponse)
    async def edit_tenant(
        tenant_id: str, request: Request, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        tenant = db.get_tenant(normalize_tenant_id(tenant_id))
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")
        require_tenant_visible(user, settings, tenant)
        return html_response(
            render(
                request,
                f"Tenant {tenant.tenant_id}",
                tenant_form(
                    tenant,
                    is_new=False,
                    settings=settings,
                    user=user,
                    saved=request.query_params.get("saved") == "1",
                    message=request.query_params.get("message"),
                ),
                current_path="/admin-ui/tenants",
            )
        )

    @router.post("/admin-ui/tenants/save")
    async def save_tenant(
        request: Request, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        if admin_auth.is_support(user) and not admin_auth.is_admin(user, settings):
            raise HTTPException(status_code=403, detail="Support access is read-only")
        form = await read_form(request)
        requested_tenant_id = form.get("tenant_id", "")
        tenant_id = normalize_tenant_id(requested_tenant_id) if admin_auth.is_admin(user, settings) and requested_tenant_id else normalize_tenant_id(admin_auth.nicky_user_uuid(user))
        existing = db.get_tenant(tenant_id)
        base = existing or tenant_from_settings(settings, tenant_id)
        api_key = form_secret(form, "nicky_api_key", base.nicky_api_key)
        validation = await nicky_client.validate_api_key(api_key)
        nicky_user_uuid = str(validation.get("nicky_user_uuid") or "")
        nicky_user_short_id = str(validation.get("nicky_user_short_id") or "")
        if not nicky_user_uuid:
            raise HTTPException(status_code=400, detail="Nicky API key validation did not return a user UUID")
        if not admin_auth.is_admin(user, settings) and nicky_user_uuid != admin_auth.nicky_user_uuid(user):
            raise HTTPException(status_code=403, detail="Nicky API key belongs to another user")
        if not admin_auth.is_admin(user, settings):
            tenant_id = normalize_tenant_id(nicky_user_uuid)

        tenant = replace(
            base,
            tenant_id=tenant_id,
            name=form.get("name") or nicky_user_short_id or tenant_id,
            active=True,
            nicky_user_uuid=nicky_user_uuid,
            nicky_user_short_id=nicky_user_short_id,
            ticket_tailor_api_key=form_secret(
                form, "ticket_tailor_api_key", base.ticket_tailor_api_key
            ),
            ticket_tailor_webhook_signing_secret="",
            ticket_tailor_offline_payment_keywords=NICKY_PAYMENT_KEYWORDS,
            nicky_api_key=api_key,
            nicky_default_blockchain_asset_id=form.get(
                "nicky_default_blockchain_asset_id", ""
            ),
            nicky_receiver_short_id=nicky_user_short_id,
            nicky_webhook_token=base.nicky_webhook_token or secrets.token_urlsafe(24),
            nicky_webhook_type=NICKY_WEBHOOK_TYPE,
            auto_create_nicky_payment_request=True,
            auto_confirm_ticket_tailor_payments=True,
            nicky_send_notification=True,
            skip_nicky=False,
            dry_run=False,
        )
        db.upsert_tenant(tenant)
        await nicky_client.create_webhook(tenant, build_nicky_webhook_url(settings, tenant))
        return RedirectResponse(
            f"/admin-ui/tenants/{urllib.parse.quote(tenant.tenant_id)}/edit?saved=1",
            status_code=303,
        )

    @router.post("/admin-ui/tenants/{tenant_id}/register-nicky-webhook")
    async def register_nicky_webhook(
        tenant_id: str, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        if not admin_auth.is_admin(user, settings):
            raise HTTPException(status_code=403, detail="Admin role required")
        tenant = get_tenant_or_404(db, tenant_id)
        url = build_nicky_webhook_url(settings, tenant)
        await nicky_client.create_webhook(tenant, url)
        return RedirectResponse(
            f"/admin-ui/tenants/{urllib.parse.quote(tenant.tenant_id)}/edit?message=nicky_webhook_registered",
            status_code=303,
        )

    @router.post("/admin-ui/tenants/{tenant_id}/test-nicky-webhook")
    async def test_nicky_webhook(
        tenant_id: str, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        if not admin_auth.is_admin(user, settings):
            raise HTTPException(status_code=403, detail="Admin role required")
        tenant = get_tenant_or_404(db, tenant_id)
        await nicky_client.test_status_change_webhook(tenant)
        return RedirectResponse(
            f"/admin-ui/tenants/{urllib.parse.quote(tenant.tenant_id)}/edit?message=nicky_webhook_test_requested",
            status_code=303,
        )

    @router.post("/admin-ui/tenants/{tenant_id}/delete")
    async def delete_tenant(
        tenant_id: str, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        if not admin_auth.is_admin(user, settings):
            raise HTTPException(status_code=403, detail="Admin role required")
        tenant = get_tenant_or_404(db, tenant_id)
        db.deactivate_tenant(tenant.tenant_id)
        return RedirectResponse("/admin-ui/tenants", status_code=303)

    @router.get("/admin-ui/orders", response_class=HTMLResponse)
    async def orders_page(
        request: Request,
        user: admin_auth.AdminUser = Depends(require_admin_web),
    ):
        owner_uuid = scoped_owner_uuid(user, settings)
        tenants = db.list_tenants(nicky_user_uuid=owner_uuid)
        order_filters = scoped_order_filters(orders_page_filters(request), owner_uuid)
        page_number = page_query_value(request.query_params.get("page"))
        total = db.count_orders(**order_filters)
        orders = [
            row_to_dict(row)
            for row in db.list_orders(
                limit=DEFAULT_PAGE_SIZE,
                offset=page_offset(page_number, DEFAULT_PAGE_SIZE),
                **order_filters,
            )
        ]
        body = f"""
        <section class="mb-6 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <h1 class="text-2xl font-semibold leading-8 text-slate-950">Orders</h1>
            <p class="mt-2 text-sm text-slate-500">Ticket Tailor orders mapped to Nicky Payment Requests.</p>
          </div>
        </section>
        <div class="overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky">
          {orders_page_filters_form(order_filters, tenants, show_tenant_filter=owner_uuid is None)}
          {orders_table(orders)}
          {pagination_controls(page_number, DEFAULT_PAGE_SIZE, total, "/admin-ui/orders", request.query_params, "page")}
        </div>
        """
        return html_response(render(request, "Orders", body, current_path="/admin-ui/orders"))

    @router.get("/admin-ui/orders/{ticket_tailor_order_id}", response_class=HTMLResponse)
    async def order_detail(
        ticket_tailor_order_id: str,
        request: Request,
        tenant_id: str,
        user: admin_auth.AdminUser = Depends(require_admin_web),
    ):
        tenant = normalize_tenant_id(tenant_id)
        owner_uuid = scoped_owner_uuid(user, settings)
        if owner_uuid and tenant != owner_uuid:
            raise HTTPException(status_code=403, detail="Order outside user scope")
        row = db.get_order(tenant, ticket_tailor_order_id)
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
        order = row_to_dict(row)
        log_page = page_query_value(request.query_params.get("logs_page"))
        logs_total = db.count_order_logs(tenant, ticket_tailor_order_id)
        logs = [
            dict(log)
            for log in db.list_order_logs(
                tenant,
                ticket_tailor_order_id,
                limit=DEFAULT_PAGE_SIZE,
                offset=page_offset(log_page, DEFAULT_PAGE_SIZE),
            )
        ]
        body = f"""
        <section class="mb-6 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <h1 class="text-2xl font-semibold leading-8 text-slate-950">Order {e(ticket_tailor_order_id)}</h1>
            <p class="mt-2 text-sm text-slate-500">Tenant {e(tenant)} / buyer {e(order.get("buyer_email") or "")}</p>
          </div>
          <a class="inline-flex h-10 shrink-0 items-center justify-center rounded-lg border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-950 hover:bg-slate-50" href="/admin-ui/orders?tenant_id={u(tenant)}">Back to orders</a>
        </section>
        {order_mapping_panel(order)}
        {order_actions(order, user=user, settings=settings)}
        <section class="mb-7 min-w-0">
          <h2 class="mb-3 text-lg font-semibold text-slate-950">Order logs</h2>
          <div class="overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky">
            {logs_table(logs, framed=False)}
            {pagination_controls(log_page, DEFAULT_PAGE_SIZE, logs_total, f"/admin-ui/orders/{u(ticket_tailor_order_id)}", request.query_params, "logs_page")}
          </div>
        </section>
        <section class="mb-7 rounded-xl border border-slate-100 bg-white p-4 shadow-nicky">
          <h2 class="mb-3 text-lg font-semibold text-slate-950">Raw Ticket Tailor payload</h2>
          <details><summary class="cursor-pointer text-sm font-semibold text-slate-950">Show payload</summary><pre class="mt-3 overflow-x-auto rounded-lg bg-zinc-950 p-4 text-xs text-slate-100">{e(json.dumps(order.get("raw_payload"), indent=2, ensure_ascii=False))}</pre></details>
        </section>
        """
        return html_response(render(request, "Order detail", body, current_path="/admin-ui/orders"))

    @router.post("/admin-ui/orders/{ticket_tailor_order_id}/create-nicky-payment-request")
    async def create_payment_request(
        ticket_tailor_order_id: str, request: Request, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        if not admin_auth.is_admin(user, settings):
            raise HTTPException(status_code=403, detail="Admin role required")
        form = await read_form(request)
        tenant = get_tenant_or_404(db, form.get("tenant_id", ""))
        await service.create_nicky_payment_request(tenant, ticket_tailor_order_id)
        return RedirectResponse(
            f"/admin-ui/orders/{u(ticket_tailor_order_id)}?tenant_id={u(tenant.tenant_id)}&message=payment_request_created",
            status_code=303,
        )

    @router.post("/admin-ui/orders/{ticket_tailor_order_id}/confirm-ticket-tailor-payment")
    async def confirm_ticket_tailor_payment(
        ticket_tailor_order_id: str, request: Request, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        if not admin_auth.is_admin(user, settings):
            raise HTTPException(status_code=403, detail="Admin role required")
        form = await read_form(request)
        tenant = get_tenant_or_404(db, form.get("tenant_id", ""))
        await service.confirm_ticket_tailor_payment(tenant, ticket_tailor_order_id)
        return RedirectResponse(
            f"/admin-ui/orders/{u(ticket_tailor_order_id)}?tenant_id={u(tenant.tenant_id)}&message=ticket_tailor_confirmed",
            status_code=303,
        )

    @router.post("/admin-ui/expire-overdue-orders")
    async def expire_overdue_orders(
        request: Request, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        if not admin_auth.is_admin(user, settings):
            raise HTTPException(status_code=403, detail="Admin role required")
        form = await read_form(request)
        tenant_id = form.get("tenant_id") or None
        expiration_hours = (
            float(form["expiration_hours"]) if form.get("expiration_hours") else None
        )
        batch_size = int(form["batch_size"]) if form.get("batch_size") else None
        await service.expire_overdue_orders(
            tenant_id=normalize_tenant_id(tenant_id) if tenant_id else None,
            expiration_hours=expiration_hours,
            batch_size=batch_size,
        )
        target = "/admin-ui/orders"
        if tenant_id:
            target = f"{target}?tenant_id={u(tenant_id)}"
        return RedirectResponse(target, status_code=303)

    return router


def html_response(content: str) -> HTMLResponse:
    return HTMLResponse(content)


def render(request: Request, title: str, body: str, *, current_path: str) -> str:
    return page(title, body, current_path=current_path, user=admin_auth.get_session_user(request))


def page(
    title: str,
    body: str,
    *,
    current_path: str,
    user: admin_auth.AdminUser | None,
) -> str:
    user_block = ""
    if user:
        user_block = f"""
        <div class="flex flex-wrap items-center gap-3 text-sm">
          <span class="font-semibold text-slate-950">{e(user.name)}</span>
          <small class="text-xs text-slate-400">{e(", ".join(user.roles) or user.auth_method)}</small>
          <a class="inline-flex h-9 items-center rounded-lg bg-slate-100 px-3 font-medium text-slate-950 hover:bg-slate-200" href="/admin-ui/logout">Log out</a>
        </div>
        """
    nav = "".join(
        nav_link(label, href, current_path)
        for label, href in [
            ("Dashboard", "/admin-ui"),
            ("Tenants", "/admin-ui/tenants"),
            ("Orders", "/admin-ui/orders"),
            ("API docs", "/docs"),
        ]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)} - Nicky Ticket Tailor</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {{
      theme: {{
        extend: {{
          fontFamily: {{ sans: ["Inter", "ui-sans-serif", "system-ui"] }},
          boxShadow: {{ nicky: "14px 27px 45px 4px rgba(112, 144, 176, 0.16)" }}
        }}
      }}
    }}
  </script>
  <style>{CSS}</style>
</head>
<body class="min-h-screen overflow-x-hidden bg-[#f1f1f1] font-sans text-slate-950">
  <header class="flex min-h-20 w-full flex-wrap items-center justify-between gap-4 border-b border-slate-200 bg-white px-5 py-4 md:px-10 xl:px-14">
    <div class="flex min-w-0 flex-wrap items-center gap-4 md:gap-6">
      <strong class="nicky-logo">Nicky Ticket Tailor Admin</strong>
      <span class="hidden min-h-7 items-center border-l border-slate-300 pl-6 text-base font-semibold text-slate-700 sm:inline-flex">Ticket Tailor Admin</span>
      <nav class="flex min-w-0 flex-wrap items-center gap-1 md:gap-2">{nav}</nav>
    </div>
    {user_block}
  </header>
  <main class="w-full min-w-0 overflow-x-hidden px-5 py-6 md:px-10 xl:px-14">{body}</main>
</body>
</html>"""


def nav_link(label: str, href: str, current_path: str) -> str:
    base = "inline-flex h-10 items-center rounded-xl px-4 text-sm font-semibold transition"
    active = "bg-black text-white" if href == current_path else "text-slate-950 hover:bg-slate-100"
    return f'<a class="{base} {active}" href="{href}">{e(label)}</a>'


def scoped_owner_uuid(user: admin_auth.AdminUser, settings: Settings) -> str | None:
    if admin_auth.is_privileged(user, settings):
        return None
    return admin_auth.nicky_user_uuid(user)


def can_write_tenants(user: admin_auth.AdminUser, settings: Settings) -> bool:
    return not (admin_auth.is_support(user) and not admin_auth.is_admin(user, settings))


def require_tenant_visible(
    user: admin_auth.AdminUser, settings: Settings, tenant: TenantConfig
) -> None:
    owner_uuid = scoped_owner_uuid(user, settings)
    if owner_uuid and tenant.nicky_user_uuid != owner_uuid:
        raise HTTPException(status_code=403, detail="Tenant outside user scope")


def scoped_order_filters(
    filters: dict[str, str | None], owner_uuid: str | None
) -> dict[str, str | None]:
    if owner_uuid:
        filters = dict(filters)
        filters["tenant_id"] = owner_uuid
    return filters


def scoped_webhook_filters(
    filters: dict[str, str | None], owner_uuid: str | None
) -> dict[str, str | None]:
    if owner_uuid:
        filters = dict(filters)
        filters["tenant_id"] = owner_uuid
    return filters


def new_tenant_link(user: admin_auth.AdminUser, tenants: list[TenantConfig], settings: Settings) -> str:
    if not can_write_tenants(user, settings):
        return ""
    if not admin_auth.is_admin(user, settings) and tenants:
        return ""
    return '<a class="inline-flex h-10 shrink-0 items-center justify-center rounded-lg bg-black px-4 text-sm font-semibold text-white hover:bg-zinc-800" href="/admin-ui/tenants/new">New tenant</a>'


def summary_grid(
    tenants: list[TenantConfig], orders: list[dict[str, Any]], webhooks: list[dict[str, Any]]
) -> str:
    active_tenants = sum(1 for tenant in tenants if tenant.active)
    pending_orders = sum(
        1
        for order in orders
        if not order.get("ticket_tailor_confirmed_at")
        and not order.get("ticket_tailor_tickets_voided_at")
    )
    failed_webhooks = sum(1 for webhook in webhooks if webhook.get("status") == "failed")
    return f"""
    <section class="mb-7 grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {metric("Tenants", str(len(tenants)), f"{active_tenants} active")}
      {metric("Recent orders", str(len(orders)), f"{pending_orders} pending")}
      {metric("Recent webhooks", str(len(webhooks)), f"{failed_webhooks} failed")}
      {metric("Mode", "Live", "fixed behavior")}
    </section>
    """


def metric(label: str, value: str, hint: str) -> str:
    return f"""
    <div class="relative min-w-0 overflow-hidden rounded-xl border border-slate-100 bg-white p-4 shadow-nicky before:absolute before:inset-x-0 before:top-0 before:h-1 before:bg-[#deff96]">
      <span class="block text-xs font-semibold text-slate-500">{e(label)}</span>
      <strong class="mt-1 block text-2xl font-bold leading-8 text-slate-950">{e(value)}</strong>
      <small class="block text-sm text-slate-400">{e(hint)}</small>
    </div>
    """


def tenant_table(
    tenants: list[TenantConfig],
    *,
    user: admin_auth.AdminUser,
    settings: Settings,
    framed: bool = True,
) -> str:
    rows = "".join(tenant_row(tenant, user=user, settings=settings) for tenant in tenants)
    if not rows:
        rows = '<tr><td colspan="8" class="px-4 py-4 text-sm text-slate-400">No tenants configured yet.</td></tr>'
    wrapper = (
        'class="min-w-0 overflow-x-auto rounded-xl border border-slate-100 bg-white shadow-nicky"'
        if framed
        else 'class="min-w-0 overflow-x-auto"'
    )
    return f"""
    <div {wrapper}>
      <table class="w-full min-w-[900px] border-separate border-spacing-0 text-left">
        <thead>
          <tr class="bg-slate-50 text-xs font-semibold text-slate-500">
            <th class="border-b border-slate-100 px-4 py-3">Tenant</th>
            <th class="border-b border-slate-100 px-4 py-3">Nicky UUID</th>
            <th class="border-b border-slate-100 px-4 py-3">Short ID</th>
            <th class="border-b border-slate-100 px-4 py-3">Active</th>
            <th class="border-b border-slate-100 px-4 py-3">Ticket Tailor</th>
            <th class="border-b border-slate-100 px-4 py-3">Nicky</th>
            <th class="border-b border-slate-100 px-4 py-3">Asset</th>
            <th class="border-b border-slate-100 px-4 py-3">Created</th>
            <th class="border-b border-slate-100 px-4 py-3">Updated</th>
            <th class="border-b border-slate-100 px-4 py-3"></th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """


def tenant_row(tenant: TenantConfig, *, user: admin_auth.AdminUser, settings: Settings) -> str:
    safe = tenant_to_safe_dict(tenant)
    actions = ""
    if can_write_tenants(user, settings):
        actions = f'<a class="inline-flex h-9 items-center rounded-lg bg-black px-4 text-sm font-semibold text-white hover:bg-zinc-800" href="/admin-ui/tenants/{u(tenant.tenant_id)}/edit">Edit</a>'
    return f"""
    <tr class="even:bg-[#f8f8f9] hover:bg-slate-50">
      <td class="border-b border-slate-100 px-4 py-3 align-top"><strong class="font-semibold text-slate-950">{e(tenant.tenant_id)}</strong><br><small class="text-sm text-slate-400">{e(tenant.name)}</small></td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{e(tenant.nicky_user_uuid or "-")}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{e(tenant.nicky_user_short_id or "-")}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{badge("Active" if tenant.active else "Inactive", tenant.active)}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{badge("Configured" if safe["ticket_tailor_configured"] else "Missing", safe["ticket_tailor_configured"])}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{badge("Configured" if safe["nicky_configured"] else "Missing", safe["nicky_configured"])}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{e(tenant.nicky_default_blockchain_asset_id or "-")}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{e(tenant.created_at or "-")}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{e(tenant.updated_at or "-")}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top text-right">{actions}</td>
    </tr>
    """


def tenant_form(
    tenant: TenantConfig,
    *,
    is_new: bool,
    settings: Settings,
    user: admin_auth.AdminUser,
    saved: bool = False,
    message: str | None = None,
) -> str:
    tt_webhook = f"{settings.app_base_url}/webhooks/ticket-tailor/{tenant.tenant_id}"
    notice = ""
    if saved:
        notice = '<p class="mb-5 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm font-medium text-emerald-900">Tenant saved.</p>'
    elif message:
        notice = f'<p class="mb-5 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm font-medium text-emerald-900">{e(message.replace("_", " ").capitalize())}.</p>'
    delete_action = ""
    if not is_new and admin_auth.is_admin(user, settings):
        delete_action = f"""
        <form method="post" action="/admin-ui/tenants/{u(tenant.tenant_id)}/delete">
          <button type="submit" class="inline-flex h-10 items-center justify-center rounded-lg border border-rose-200 bg-white px-4 text-sm font-semibold text-rose-700 hover:bg-rose-50">Deactivate tenant</button>
        </form>
        """
    asset_option = (
        f'<option value="{e(tenant.nicky_default_blockchain_asset_id)}" selected>{e(tenant.nicky_default_blockchain_asset_id)}</option>'
        if tenant.nicky_default_blockchain_asset_id
        else '<option value="">Validate Nicky API key first</option>'
    )
    tenant_id_field = (
        text_input("Tenant UUID", "tenant_id", value=tenant.tenant_id, readonly=not is_new or not admin_auth.is_admin(user, settings), required=admin_auth.is_admin(user, settings))
        if admin_auth.is_admin(user, settings)
        else ""
    )
    return f"""
    <section class="mb-6 flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
      <div>
        <h1 class="text-2xl font-semibold leading-8 text-slate-950">{"New tenant" if is_new else f"Tenant {e(tenant.tenant_id)}"}</h1>
        <p class="mt-2 text-sm text-slate-500">Live integration configuration for one Nicky user.</p>
      </div>
      <div class="flex flex-wrap gap-3">
        {delete_action}
        <a class="inline-flex h-10 shrink-0 items-center justify-center rounded-lg border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-950 hover:bg-slate-50" href="/admin-ui/tenants">Back to tenants</a>
      </div>
    </section>
    {notice}
    <form method="post" action="/admin-ui/tenants/save" class="grid min-w-0 grid-cols-1 gap-5 xl:grid-cols-2">
      <section class="min-w-0 rounded-xl border border-slate-100 bg-white p-4 shadow-nicky">
        <h2 class="mb-4 text-lg font-semibold text-slate-950">Identity</h2>
        {tenant_id_field}
        {text_input("Name", "name", value=tenant.name)}
        {text_input("Nicky user UUID", "", value=tenant.nicky_user_uuid or "-", readonly=True)}
        {text_input("Nicky short ID", "", value=tenant.nicky_user_short_id or "-", readonly=True)}
      </section>
      <section class="min-w-0 rounded-xl border border-slate-100 bg-white p-4 shadow-nicky">
        <h2 class="mb-4 text-lg font-semibold text-slate-950">Nicky</h2>
        <label class="mb-4 block min-w-0 text-sm font-semibold text-slate-950">
          API key
          <input id="nicky-api-key" class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black disabled:bg-slate-50" name="nicky_api_key" type="password" placeholder="Leave blank to keep existing">
        </label>
        <div class="mb-4 flex flex-wrap items-center gap-3">
          <button id="validate-nicky-key" class="inline-flex h-10 items-center justify-center rounded-lg border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-950 hover:bg-slate-50" type="button">Validate</button>
          <span id="nicky-validation-status" class="text-sm text-slate-500"></span>
        </div>
        <label class="mb-4 block min-w-0 text-sm font-semibold text-slate-950">
          Asset
          <select id="nicky-asset" class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="nicky_default_blockchain_asset_id" required>{asset_option}</select>
        </label>
      </section>
      <section class="min-w-0 rounded-xl border border-slate-100 bg-white p-4 shadow-nicky">
        <h2 class="mb-4 text-lg font-semibold text-slate-950">Ticket Tailor</h2>
        {text_input("API key", "ticket_tailor_api_key", input_type="password", placeholder="Leave blank to keep existing")}
        {text_input("Offline payment name", "", value="Nicky Payment", readonly=True)}
        {text_input("Ticket Tailor webhook", "", value=tt_webhook, readonly=True)}
      </section>
      <div class="xl:col-span-2 flex justify-end">
        <button class="inline-flex h-11 items-center justify-center rounded-lg bg-black px-5 text-sm font-semibold text-white hover:bg-zinc-800" type="submit">Save tenant</button>
      </div>
    </form>
    <script>
      const validateButton = document.getElementById("validate-nicky-key");
      const apiKeyInput = document.getElementById("nicky-api-key");
      const statusEl = document.getElementById("nicky-validation-status");
      const assetSelect = document.getElementById("nicky-asset");
      validateButton?.addEventListener("click", async () => {{
        const apiKey = apiKeyInput.value;
        statusEl.textContent = "Validating...";
        try {{
          const response = await fetch("/admin/nicky/validate-api-key", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ nicky_api_key: apiKey }})
          }});
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || "Validation failed");
          assetSelect.innerHTML = "";
          for (const asset of payload.assets || []) {{
            const option = document.createElement("option");
            option.value = asset.id;
            option.textContent = asset.name ? `${{asset.name}} (${{asset.id}})` : asset.id;
            assetSelect.appendChild(option);
          }}
          statusEl.textContent = payload.nicky_user_short_id || payload.nicky_user_uuid || "Validated";
        }} catch (error) {{
          statusEl.textContent = error.message;
        }}
      }});
    </script>
    """


def orders_table(orders: list[dict[str, Any]]) -> str:
    rows = "".join(order_row(order) for order in orders)
    if not rows:
        rows = '<tr><td colspan="9" class="px-4 py-4 text-sm text-slate-400">No orders captured yet.</td></tr>'
    return f"""
    <div class="min-w-0 overflow-x-auto">
      <table class="w-full min-w-[980px] border-separate border-spacing-0 text-left">
        <thead>
          <tr class="bg-slate-50 text-xs font-semibold text-slate-500">
            <th class="border-b border-slate-100 px-4 py-3">Order</th>
            <th class="border-b border-slate-100 px-4 py-3">Tenant</th>
            <th class="border-b border-slate-100 px-4 py-3">Buyer</th>
            <th class="border-b border-slate-100 px-4 py-3">Amount</th>
            <th class="border-b border-slate-100 px-4 py-3">Nicky PR</th>
            <th class="border-b border-slate-100 px-4 py-3">Nicky status</th>
            <th class="border-b border-slate-100 px-4 py-3">Ticket Tailor state</th>
            <th class="border-b border-slate-100 px-4 py-3">Updated</th>
            <th class="border-b border-slate-100 px-4 py-3"></th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """


def order_row(order: dict[str, Any]) -> str:
    tenant_id = str(order.get("tenant_id") or "")
    order_id = str(order.get("ticket_tailor_order_id") or "")
    return f"""
    <tr class="even:bg-[#f8f8f9] hover:bg-slate-50">
      <td class="border-b border-slate-100 px-4 py-3 align-top"><strong class="font-semibold text-slate-950">{e(order_id)}</strong></td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{e(tenant_id)}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{e(order.get("buyer_email") or "-")}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{format_amount(order)}</td>
      <td class="max-w-[260px] break-words border-b border-slate-100 px-4 py-3 align-top">{e(order.get("nicky_payment_request_id") or "-")}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{e(order.get("nicky_status") or "-")}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{ticket_tailor_state_cell(order)}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{e(order.get("updated_at") or "-")}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top text-right"><a class="inline-flex h-9 items-center rounded-lg bg-black px-4 text-sm font-semibold text-white hover:bg-zinc-800" href="/admin-ui/orders/{u(order_id)}?tenant_id={u(tenant_id)}">Open</a></td>
    </tr>
    """


def webhook_table(webhooks: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"""
        <tr class="even:bg-[#f8f8f9] hover:bg-slate-50">
          <td class="border-b border-slate-100 px-4 py-3 align-top">{e(webhook.get("received_at") or "")}</td>
          <td class="border-b border-slate-100 px-4 py-3 align-top">{e(webhook.get("tenant_id") or "")}</td>
          <td class="border-b border-slate-100 px-4 py-3 align-top">{e(webhook.get("source") or "")}</td>
          <td class="border-b border-slate-100 px-4 py-3 align-top">{e(webhook.get("event_type") or "")}</td>
          <td class="border-b border-slate-100 px-4 py-3 align-top">{badge(str(webhook.get("status") or ""), webhook.get("status") != "failed")}</td>
        </tr>
        """
        for webhook in webhooks
    )
    if not rows:
        rows = '<tr><td colspan="5" class="px-4 py-4 text-sm text-slate-400">No webhooks received yet.</td></tr>'
    return f"""
    <div class="min-w-0 overflow-x-auto">
      <table class="w-full min-w-[760px] border-separate border-spacing-0 text-left">
        <thead><tr class="bg-slate-50 text-xs font-semibold text-slate-500"><th class="border-b border-slate-100 px-4 py-3">Received</th><th class="border-b border-slate-100 px-4 py-3">Tenant</th><th class="border-b border-slate-100 px-4 py-3">Source</th><th class="border-b border-slate-100 px-4 py-3">Type</th><th class="border-b border-slate-100 px-4 py-3">Status</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>
    """


def logs_table(logs: list[dict[str, Any]], *, framed: bool = True) -> str:
    rows = ""
    for log in logs:
        payload = log.get("payload_json")
        try:
            payload = json.dumps(json.loads(payload), indent=2) if payload else ""
        except json.JSONDecodeError:
            payload = str(payload or "")
        rows += f"""
        <tr class="even:bg-[#f8f8f9] hover:bg-slate-50">
          <td class="border-b border-slate-100 px-4 py-3 align-top">{e(log.get("created_at") or "")}</td>
          <td class="border-b border-slate-100 px-4 py-3 align-top">{e(log.get("event_type") or "")}</td>
          <td class="border-b border-slate-100 px-4 py-3 align-top">{e(log.get("message") or "")}<details class="mt-2"><summary class="cursor-pointer text-sm font-semibold text-slate-950">Payload</summary><pre class="mt-2 overflow-x-auto rounded-lg bg-zinc-950 p-4 text-xs text-slate-100">{e(payload)}</pre></details></td>
        </tr>
        """
    if not rows:
        rows = '<tr><td colspan="3" class="px-4 py-4 text-sm text-slate-400">No logs for this order.</td></tr>'
    wrapper = (
        'class="min-w-0 overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky"'
        if framed
        else 'class="min-w-0 overflow-hidden"'
    )
    return f"""
    <div {wrapper}>
      <div class="min-w-0 overflow-x-auto">
      <table class="w-full min-w-[760px] border-separate border-spacing-0 text-left">
        <thead><tr class="bg-slate-50 text-xs font-semibold text-slate-500"><th class="border-b border-slate-100 px-4 py-3">Created</th><th class="border-b border-slate-100 px-4 py-3">Event</th><th class="border-b border-slate-100 px-4 py-3">Message</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      </div>
    </div>
    """


def order_mapping_panel(order: dict[str, Any]) -> str:
    rows = [
        ("Ticket Tailor order", order.get("ticket_tailor_order_id")),
        ("Nicky Payment Request", order.get("nicky_payment_request_id")),
        ("Nicky bill short id", order.get("nicky_bill_short_id")),
        ("Nicky receiver", order.get("nicky_receiver_short_id")),
        ("Nicky payment URL", order.get("nicky_payment_url")),
        ("Nicky status", order.get("nicky_status")),
        ("Ticket Tailor payment confirmed at", order.get("ticket_tailor_confirmed_at")),
        ("Issued tickets voided at", order.get("ticket_tailor_tickets_voided_at")),
        ("Ticket void reason", order.get("ticket_tailor_void_reason")),
    ]
    items = "".join(
        f'<dt class="text-slate-500">{e(label)}</dt><dd class="min-w-0 break-words text-slate-950">{link_or_text(value)}</dd>' for label, value in rows
    )
    notice = ticket_tailor_state_notice(order)
    return f'<section class="mb-7 rounded-xl border border-slate-100 bg-white p-4 shadow-nicky"><h2 class="mb-4 text-lg font-semibold text-slate-950">Order mapping</h2>{notice}<dl class="grid grid-cols-1 gap-2 text-sm md:grid-cols-[220px_minmax(0,1fr)]">{items}</dl></section>'


def order_actions(order: dict[str, Any], *, user: admin_auth.AdminUser, settings: Settings) -> str:
    if not admin_auth.is_admin(user, settings):
        return ""
    tenant_id = str(order.get("tenant_id") or "")
    order_id = str(order.get("ticket_tailor_order_id") or "")
    return f"""
    <section class="mb-7 rounded-xl border border-slate-100 bg-white p-4 shadow-nicky">
      <h2 class="mb-4 text-lg font-semibold text-slate-950">Actions</h2>
      <div class="flex flex-wrap items-center gap-3">
        <form method="post" action="/admin-ui/orders/{u(order_id)}/create-nicky-payment-request">
          <input type="hidden" name="tenant_id" value="{e(tenant_id)}">
          <button type="submit" class="inline-flex h-10 items-center justify-center rounded-lg border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-950 hover:bg-slate-50">Create Nicky Payment Request</button>
        </form>
        <form method="post" action="/admin-ui/orders/{u(order_id)}/confirm-ticket-tailor-payment">
          <input type="hidden" name="tenant_id" value="{e(tenant_id)}">
          <button type="submit" class="inline-flex h-10 items-center justify-center rounded-lg border border-rose-200 bg-white px-4 text-sm font-semibold text-rose-700 hover:bg-rose-50">Confirm Ticket Tailor payment</button>
        </form>
      </div>
    </section>
    """


def tenant_options(tenants: list[TenantConfig], selected: str | None) -> str:
    options = option_tag("", "All tenants", selected or "")
    for tenant in tenants:
        options += option_tag(tenant.tenant_id, tenant.tenant_id, selected or "")
    return options


def tenant_page_filters(request: Request) -> dict[str, str | None]:
    return {
        "query": text_query_value(request.query_params.get("q")),
        "active": choice_query_value(request.query_params.get("active"), {"active", "inactive"}),
        "configuration": choice_query_value(
            request.query_params.get("configuration"), {"complete", "missing"}
        ),
    }


def orders_page_filters(request: Request) -> dict[str, str | None]:
    return {
        "tenant_id": tenant_query_value(request.query_params.get("tenant_id")),
        "updated_from": date_query_value(request.query_params.get("orders_from")),
        "updated_to": date_query_value(request.query_params.get("orders_to")),
        "order_state": order_state_value(request.query_params.get("order_state")),
    }


def dashboard_order_filters(request: Request) -> dict[str, str | None]:
    return {
        "tenant_id": tenant_query_value(request.query_params.get("orders_tenant_id")),
        "updated_from": date_query_value(request.query_params.get("orders_from")),
        "updated_to": date_query_value(request.query_params.get("orders_to")),
        "order_state": order_state_value(request.query_params.get("order_state")),
    }


def dashboard_webhook_filters(request: Request) -> dict[str, str | None]:
    return {
        "tenant_id": tenant_query_value(request.query_params.get("webhooks_tenant_id")),
        "received_from": date_query_value(request.query_params.get("webhooks_from")),
        "received_to": date_query_value(request.query_params.get("webhooks_to")),
        "status": webhook_status_value(request.query_params.get("webhook_status")),
    }


def text_query_value(value: str | None) -> str | None:
    trimmed = (value or "").strip()
    return trimmed[:80] if trimmed else None


def tenant_query_value(value: str | None) -> str | None:
    trimmed = (value or "").strip()
    if not trimmed:
        return None
    try:
        return normalize_tenant_id(trimmed)
    except ValueError:
        return None


def choice_query_value(value: str | None, allowed: set[str]) -> str | None:
    return value if value in allowed else None


def page_query_value(value: str | None) -> int:
    try:
        page = int(value or "1")
    except ValueError:
        return 1
    return max(1, page)


def page_offset(page: int, page_size: int) -> int:
    return (max(1, page) - 1) * page_size


def date_query_value(value: str | None) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) != 10:
        return None
    try:
        datetime.date.fromisoformat(trimmed)
    except ValueError:
        return None
    return trimmed


def order_state_value(value: str | None) -> str | None:
    allowed = {"pending", "confirmed", "tickets_voided"}
    if value in allowed:
        return value
    return None


def webhook_status_value(value: str | None) -> str | None:
    allowed = {"processed", "failed", "ignored", "received"}
    if value in allowed:
        return value
    return None


def order_filters_form(
    order_filters: dict[str, str | None],
    webhook_filters: dict[str, str | None],
    tenants: list[TenantConfig],
    *,
    action: str,
    show_tenant_filter: bool = True,
) -> str:
    selected = str(order_filters.get("order_state") or "")
    options = "".join(
        option_tag(value, label, selected)
        for value, label in [
            ("", "All states"),
            ("pending", "Order pending"),
            ("confirmed", "Payment confirmed"),
            ("tickets_voided", "Tickets voided"),
        ]
    )
    selected_tenant = str(order_filters.get("tenant_id") or "")
    tenant_field = ""
    if show_tenant_filter:
        tenant_field = f'<label class="min-w-0 text-sm font-semibold text-slate-950">Tenant<select class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="orders_tenant_id">{tenant_options(tenants, selected_tenant)}</select></label>'
    return f"""
    <div class="border-b border-slate-100 p-4">
      <form method="get" action="{e(action)}" class="grid min-w-0 grid-cols-1 items-end gap-4 md:grid-cols-2 xl:grid-cols-[minmax(160px,0.9fr)_minmax(160px,1fr)_minmax(160px,1fr)_minmax(200px,1.1fr)_auto]">
        {hidden_filter_inputs(webhook_filters, prefix="webhooks")}
        {tenant_field}
        <label class="min-w-0 text-sm font-semibold text-slate-950">Updated from<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="date" name="orders_from" value="{e(order_filters.get("updated_from") or "")}"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">Updated to<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="date" name="orders_to" value="{e(order_filters.get("updated_to") or "")}"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">Status<select class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="order_state">{options}</select></label>
        <div class="flex flex-wrap items-center gap-3 md:col-span-2 xl:col-span-1 xl:justify-end">
          <button class="inline-flex h-11 items-center justify-center rounded-lg bg-black px-5 text-sm font-semibold text-white hover:bg-zinc-800" type="submit">Apply</button>
          <a class="inline-flex h-11 items-center justify-center rounded-lg border border-slate-200 bg-white px-5 text-sm font-semibold text-slate-950 hover:bg-slate-50" href="{e(reset_filters_href(order_filters, webhook_filters, reset='orders'))}">Clear</a>
        </div>
      </form>
    </div>
    """


def webhook_filters_form(
    webhook_filters: dict[str, str | None],
    order_filters: dict[str, str | None],
    tenants: list[TenantConfig],
    *,
    action: str,
    show_tenant_filter: bool = True,
) -> str:
    selected = str(webhook_filters.get("status") or "")
    options = "".join(
        option_tag(value, label, selected)
        for value, label in [
            ("", "All statuses"),
            ("processed", "Processed"),
            ("failed", "Failed"),
            ("ignored", "Ignored"),
            ("received", "Received"),
        ]
    )
    selected_tenant = str(webhook_filters.get("tenant_id") or "")
    tenant_field = ""
    if show_tenant_filter:
        tenant_field = f'<label class="min-w-0 text-sm font-semibold text-slate-950">Tenant<select class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="webhooks_tenant_id">{tenant_options(tenants, selected_tenant)}</select></label>'
    return f"""
    <div class="border-b border-slate-100 p-4">
      <form method="get" action="{e(action)}" class="grid min-w-0 grid-cols-1 items-end gap-4 md:grid-cols-2 xl:grid-cols-[minmax(160px,0.9fr)_minmax(160px,1fr)_minmax(160px,1fr)_minmax(200px,1.1fr)_auto]">
        {hidden_filter_inputs(order_filters, prefix="orders")}
        {tenant_field}
        <label class="min-w-0 text-sm font-semibold text-slate-950">Received from<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="date" name="webhooks_from" value="{e(webhook_filters.get("received_from") or "")}"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">Received to<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="date" name="webhooks_to" value="{e(webhook_filters.get("received_to") or "")}"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">Status<select class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="webhook_status">{options}</select></label>
        <div class="flex flex-wrap items-center gap-3 md:col-span-2 xl:col-span-1 xl:justify-end">
          <button class="inline-flex h-11 items-center justify-center rounded-lg bg-black px-5 text-sm font-semibold text-white hover:bg-zinc-800" type="submit">Apply</button>
          <a class="inline-flex h-11 items-center justify-center rounded-lg border border-slate-200 bg-white px-5 text-sm font-semibold text-slate-950 hover:bg-slate-50" href="{e(reset_filters_href(order_filters, webhook_filters, reset='webhooks'))}">Clear</a>
        </div>
      </form>
    </div>
    """


def orders_page_filters_form(
    order_filters: dict[str, str | None],
    tenants: list[TenantConfig],
    *,
    show_tenant_filter: bool = True,
) -> str:
    selected = str(order_filters.get("order_state") or "")
    options = "".join(
        option_tag(value, label, selected)
        for value, label in [
            ("", "All states"),
            ("pending", "Order pending"),
            ("confirmed", "Payment confirmed"),
            ("tickets_voided", "Tickets voided"),
        ]
    )
    tenant_field = ""
    if show_tenant_filter:
        tenant_field = f'<label class="min-w-0 text-sm font-semibold text-slate-950">Tenant<select class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="tenant_id">{tenant_options(tenants, str(order_filters.get("tenant_id") or ""))}</select></label>'
    return f"""
    <div class="border-b border-slate-100 p-4">
      <form method="get" action="/admin-ui/orders" class="grid min-w-0 grid-cols-1 items-end gap-4 md:grid-cols-2 xl:grid-cols-[minmax(160px,0.9fr)_minmax(160px,1fr)_minmax(160px,1fr)_minmax(200px,1.1fr)_auto]">
        {tenant_field}
        <label class="min-w-0 text-sm font-semibold text-slate-950">Updated from<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="date" name="orders_from" value="{e(order_filters.get("updated_from") or "")}"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">Updated to<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="date" name="orders_to" value="{e(order_filters.get("updated_to") or "")}"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">Status<select class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="order_state">{options}</select></label>
        <div class="flex flex-wrap items-center gap-3 md:col-span-2 xl:col-span-1 xl:justify-end">
          <button class="inline-flex h-11 items-center justify-center rounded-lg bg-black px-5 text-sm font-semibold text-white hover:bg-zinc-800" type="submit">Apply</button>
          <a class="inline-flex h-11 items-center justify-center rounded-lg border border-slate-200 bg-white px-5 text-sm font-semibold text-slate-950 hover:bg-slate-50" href="/admin-ui/orders">Clear</a>
        </div>
      </form>
    </div>
    """


def tenant_filters_form(filters: dict[str, str | None]) -> str:
    active = str(filters.get("active") or "")
    configuration = str(filters.get("configuration") or "")
    active_options = "".join(
        option_tag(value, label, active)
        for value, label in [("", "All statuses"), ("active", "Active"), ("inactive", "Inactive")]
    )
    configuration_options = "".join(
        option_tag(value, label, configuration)
        for value, label in [
            ("", "All configurations"),
            ("complete", "Configured"),
            ("missing", "Missing configuration"),
        ]
    )
    return f"""
    <div class="border-b border-slate-100 p-4">
      <form method="get" action="/admin-ui/tenants" class="grid min-w-0 grid-cols-1 items-end gap-4 md:grid-cols-2 xl:grid-cols-[minmax(220px,1.3fr)_minmax(180px,1fr)_minmax(220px,1.1fr)_auto]">
        <label class="min-w-0 text-sm font-semibold text-slate-950">Search<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="search" name="q" value="{e(filters.get("query") or "")}" placeholder="Tenant or name"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">Active<select class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="active">{active_options}</select></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">Configuration<select class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="configuration">{configuration_options}</select></label>
        <div class="flex flex-wrap items-center gap-3 md:col-span-2 xl:col-span-1 xl:justify-end">
          <button class="inline-flex h-11 items-center justify-center rounded-lg bg-black px-5 text-sm font-semibold text-white hover:bg-zinc-800" type="submit">Apply</button>
          <a class="inline-flex h-11 items-center justify-center rounded-lg border border-slate-200 bg-white px-5 text-sm font-semibold text-slate-950 hover:bg-slate-50" href="/admin-ui/tenants">Clear</a>
        </div>
      </form>
    </div>
    """


def pagination_controls(
    page: int,
    page_size: int,
    total: int,
    path: str,
    query_params: Any,
    page_param: str,
) -> str:
    total_pages = max(1, (total + page_size - 1) // page_size)
    current = min(max(1, page), total_pages)
    start = 0 if total == 0 else ((current - 1) * page_size) + 1
    end = min(total, current * page_size)
    prev_href = page_href(path, query_params, page_param, current - 1)
    next_href = page_href(path, query_params, page_param, current + 1)
    prev_class = pagination_link_class(current > 1)
    next_class = pagination_link_class(current < total_pages)
    return f"""
    <div class="flex flex-col gap-3 border-t border-slate-100 px-4 py-3 text-sm text-slate-500 sm:flex-row sm:items-center sm:justify-between">
      <span>Showing {start}-{end} of {total}</span>
      <div class="flex items-center gap-2">
        <a class="{prev_class}" href="{e(prev_href)}" aria-disabled="{'true' if current <= 1 else 'false'}">Previous</a>
        <span class="rounded-lg border border-slate-200 bg-white px-3 py-2 font-medium text-slate-700">Page {current} of {total_pages}</span>
        <a class="{next_class}" href="{e(next_href)}" aria-disabled="{'true' if current >= total_pages else 'false'}">Next</a>
      </div>
    </div>
    """


def pagination_link_class(enabled: bool) -> str:
    base = "inline-flex h-10 items-center justify-center rounded-lg px-4 text-sm font-semibold"
    if enabled:
        return f"{base} border border-slate-200 bg-white text-slate-950 hover:bg-slate-50"
    return f"{base} pointer-events-none border border-slate-100 bg-slate-50 text-slate-300"


def page_href(path: str, query_params: Any, page_param: str, page: int) -> str:
    params: list[tuple[str, str]] = []
    for key, value in query_params.multi_items():
        if key == page_param:
            continue
        if key.endswith("_page") and path == "/overview":
            params.append((key, value))
        elif not key.endswith("_page"):
            params.append((key, value))
    if page > 1:
        params.append((page_param, str(page)))
    if not params:
        return path
    return f"{path}?{urllib.parse.urlencode(params)}"


def hidden_filter_inputs(filters: dict[str, str | None], *, prefix: str) -> str:
    fields = {
        "orders": ["tenant_id", "updated_from", "updated_to", "order_state"],
        "webhooks": ["tenant_id", "received_from", "received_to", "status"],
    }
    names = {
        "tenant_id": f"{prefix}_tenant_id",
        "updated_from": "orders_from",
        "updated_to": "orders_to",
        "order_state": "order_state",
        "received_from": "webhooks_from",
        "received_to": "webhooks_to",
        "status": "webhook_status",
    }
    inputs = ""
    for field in fields[prefix]:
        value = filters.get(field)
        if value:
            inputs += f'<input type="hidden" name="{names[field]}" value="{e(value)}">'
    return inputs


def reset_filters_href(
    order_filters: dict[str, str | None],
    webhook_filters: dict[str, str | None],
    *,
    reset: str,
) -> str:
    params: list[tuple[str, str]] = []
    if reset != "orders":
        for field, name in [
            ("tenant_id", "orders_tenant_id"),
            ("updated_from", "orders_from"),
            ("updated_to", "orders_to"),
            ("order_state", "order_state"),
        ]:
            value = order_filters.get(field)
            if value:
                params.append((name, value))
    if reset != "webhooks":
        for field, name in [
            ("tenant_id", "webhooks_tenant_id"),
            ("received_from", "webhooks_from"),
            ("received_to", "webhooks_to"),
            ("status", "webhook_status"),
        ]:
            value = webhook_filters.get(field)
            if value:
                params.append((name, value))
    if not params:
        return "/overview"
    return f"/overview?{urllib.parse.urlencode(params)}"


def option_tag(value: str, label: str, selected: str) -> str:
    selected_attr = " selected" if value == selected else ""
    return f'<option value="{e(value)}"{selected_attr}>{e(label)}</option>'


def ticket_tailor_state_cell(order: dict[str, Any]) -> str:
    if order.get("ticket_tailor_confirmed_at"):
        return badge("Payment confirmed", True)
    if order.get("ticket_tailor_tickets_voided_at"):
        return (
            f'{badge("Tickets voided", False)}'
            '<small class="mt-1 block max-w-48 text-xs leading-4 text-slate-400">Order still pending in Ticket Tailor</small>'
        )
    return badge("Order pending", "warn")


def ticket_tailor_state_notice(order: dict[str, Any]) -> str:
    if not order.get("ticket_tailor_tickets_voided_at"):
        return ""
    return """
    <p class="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm font-medium text-amber-900">
      Issued tickets were voided through the Ticket Tailor API. Ticket Tailor can still show the order itself as Pending because offline payment status is order-level.
    </p>
    """


def format_amount(order: dict[str, Any]) -> str:
    amount = order.get("amount_minor")
    if amount is None:
        return "-"
    currency = str(order.get("currency") or "").upper()
    return f"{int(amount) / 100:.2f} {e(currency)}"


def text_input(
    label: str,
    name: str,
    *,
    value: str = "",
    input_type: str = "text",
    placeholder: str = "",
    readonly: bool = False,
    required: bool = False,
) -> str:
    name_attr = f' name="{e(name)}"' if name else ""
    readonly_attr = " readonly" if readonly else ""
    required_attr = " required" if required else ""
    placeholder_attr = f' placeholder="{e(placeholder)}"' if placeholder else ""
    return f"""
    <label class="mb-4 block min-w-0 text-sm font-semibold text-slate-950">
      {e(label)}
      <input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black disabled:bg-slate-50" type="{e(input_type)}"{name_attr} value="{e(value)}"{placeholder_attr}{readonly_attr}{required_attr}>
    </label>
    """


def checkbox(name: str, label: str, checked: bool) -> str:
    checked_attr = " checked" if checked else ""
    return f'<label class="mb-3 flex items-center gap-3 rounded-lg border border-slate-100 bg-white px-3 py-2 text-sm font-medium text-slate-700"><input class="h-4 w-4 accent-black" type="checkbox" name="{e(name)}"{checked_attr}> {e(label)}</label>'


def badge(label: str, ok: bool | str) -> str:
    if ok == "warn":
        cls = "bg-amber-50 text-amber-800"
    else:
        cls = "bg-emerald-50 text-emerald-900" if ok else "bg-rose-50 text-rose-900"
    return f'<span class="inline-flex min-h-6 items-center rounded-lg px-2 py-1 text-xs font-semibold {cls}">{e(label)}</span>'


def bool_text(value: bool) -> str:
    return "Yes" if value else "No"


def link_or_text(value: Any) -> str:
    if not value:
        return "-"
    text = str(value)
    if text.startswith(("http://", "https://")):
        return f'<a class="text-blue-600 hover:underline" href="{e(text)}" target="_blank" rel="noreferrer">{e(text)}</a>'
    return e(text)


def build_nicky_webhook_url(settings: Settings, tenant: TenantConfig) -> str:
    url = f"{settings.app_base_url}/webhooks/nicky/{tenant.tenant_id}"
    if tenant.nicky_webhook_token:
        url = f"{url}?token={urllib.parse.quote(tenant.nicky_webhook_token)}"
    return url


def get_tenant_or_404(db: Database, tenant_id: str) -> TenantConfig:
    tenant = db.get_tenant(normalize_tenant_id(tenant_id))
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


def request_path_with_query(request: Request) -> str:
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{request.url.path}{query}"


async def read_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


def form_bool(form: dict[str, str], name: str) -> bool:
    return form.get(name, "").lower() in {"1", "true", "yes", "y", "on"}


def form_secret(form: dict[str, str], name: str, existing: str) -> str:
    value = form.get(name, "")
    return value if value else existing


def e(value: Any) -> str:
    return html.escape(str(value), quote=True)


def u(value: Any) -> str:
    return urllib.parse.quote(str(value))


CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
  color-scheme: light;
  --bg: #f6f7f9;
  --panel: #ffffff;
  --line: #d8dde6;
  --text: #17202a;
  --muted: #667085;
  --primary: #155eef;
  --primary-dark: #0f48b8;
  --danger: #b42318;
  --ok-bg: #ecfdf3;
  --ok-text: #067647;
  --bad-bg: #fff1f3;
  --bad-text: #b42318;
}
* { box-sizing: border-box; }
html {
  width: 100%;
  min-width: 100%;
  min-height: 100%;
  overflow-x: hidden;
  background: var(--bg);
}
body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 14px;
  width: 100%;
  min-width: 100%;
  min-height: 100vh;
  overflow-x: hidden;
}
header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 14px 24px;
  background: #111827;
  color: #fff;
}
header strong { display: block; margin-bottom: 8px; font-size: 15px; }
nav { display: flex; gap: 4px; }
nav a, header a { color: #d1d5db; text-decoration: none; }
nav a {
  padding: 6px 10px;
  border-radius: 6px;
}
nav a.active, nav a:hover { background: #374151; color: #fff; }
main { padding: 24px; width: 100%; max-width: none; margin: 0; }
h1, h2 { margin: 0; letter-spacing: 0; }
h1 { font-size: 24px; line-height: 1.25; }
h2 { font-size: 16px; margin-bottom: 12px; }
.muted, small { color: var(--muted); }
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}
.toolbar p { margin: 6px 0 0; }
section { margin-bottom: 22px; }
.panel, .table-wrap, .metric {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
}
.panel { padding: 18px; }
.narrow { max-width: 460px; margin: 64px auto; }
.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}
.metric { padding: 14px; }
.metric span, .metric small { display: block; }
.metric strong { display: block; font-size: 24px; margin: 4px 0; }
.split {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(360px, .65fr);
  gap: 18px;
}
.table-wrap { overflow-x: auto; }
table {
  border-collapse: collapse;
  width: 100%;
  min-width: 860px;
}
.compact table { min-width: 620px; }
th, td {
  border-bottom: 1px solid var(--line);
  padding: 10px 12px;
  text-align: left;
  vertical-align: top;
}
th {
  color: #475467;
  background: #f9fafb;
  font-size: 12px;
  text-transform: uppercase;
}
tr:last-child td { border-bottom: none; }
a { color: var(--primary); }
button, .button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 36px;
  padding: 8px 12px;
  border: 1px solid var(--primary);
  border-radius: 6px;
  background: var(--primary);
  color: #fff;
  text-decoration: none;
  cursor: pointer;
  font: inherit;
}
button:hover, .button:hover { background: var(--primary-dark); }
.button.small { min-height: 30px; padding: 5px 9px; font-size: 12px; }
.secondary {
  background: #fff;
  color: var(--primary);
}
.secondary:hover { background: #eff4ff; color: var(--primary-dark); }
.danger {
  background: #fff;
  border-color: var(--danger);
  color: var(--danger);
}
.danger:hover { background: #fff1f3; }
.badge {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 3px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}
.badge.ok { background: var(--ok-bg); color: var(--ok-text); }
.badge.bad { background: var(--bad-bg); color: var(--bad-text); }
.badge.warn { background: #fff7df; color: #7a4f01; }
.cell-note {
  display: block;
  margin-top: 6px;
  color: var(--muted);
  font-size: 12px;
  line-height: 16px;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 18px;
}
.form-grid section {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 18px;
  margin: 0;
}
label { display: block; color: #344054; font-weight: 600; margin-bottom: 12px; }
input, select {
  width: 100%;
  min-height: 38px;
  border: 1px solid #cbd5e1;
  border-radius: 6px;
  padding: 8px 10px;
  margin-top: 5px;
  background: #fff;
  color: var(--text);
  font: inherit;
}
.check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 500;
}
.check input { width: 16px; min-height: 16px; margin: 0; }
.form-actions {
  grid-column: 1 / -1;
  display: flex;
  justify-content: flex-end;
}
.url-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}
.filters-shell {
  width: 100%;
  max-width: 100%;
  margin: 0 0 14px;
  padding: 14px 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--panel);
}
.filters-form {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) minmax(220px, 0.9fr) auto;
  align-items: end;
  gap: 12px;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  margin: 0;
}
.filters-form label {
  margin: 0;
  min-width: 0;
}
.filter-actions {
  display: flex;
  align-items: flex-end;
  justify-content: flex-end;
  gap: 10px;
  min-width: 0;
  white-space: nowrap;
}
.actions, .inline-form {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.inline-form select { min-width: 220px; }
.user {
  display: flex;
  align-items: center;
  gap: 10px;
  white-space: nowrap;
}
.user span, .user small { display: block; }
dl {
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr);
  gap: 8px 12px;
  margin: 0;
}
dt { color: var(--muted); }
dd { margin: 0; overflow-wrap: anywhere; }
pre {
  white-space: pre-wrap;
  overflow-x: auto;
  background: #0f172a;
  color: #e5e7eb;
  padding: 12px;
  border-radius: 6px;
  font-size: 12px;
}
.notice {
  border: 1px solid #a6f4c5;
  background: #ecfdf3;
  color: #067647;
  padding: 10px 12px;
  border-radius: 6px;
}
.notice.warning {
  border-color: #f6d58b;
  background: #fff8e6;
  color: #7a4f01;
}
.stack > * + * { margin-top: 12px; }
@media (max-width: 960px) {
  header, .toolbar { align-items: flex-start; flex-direction: column; }
  .metrics, .split, .form-grid, .url-grid, .filters-form { grid-template-columns: 1fr; }
  main { padding: 16px; }
  table { min-width: 760px; }
}

/* Nicky Angular visual alignment */
:root {
  --bg: #f1f1f1;
  --panel: #ffffff;
  --line: #eff0f4;
  --line-strong: #e8e8e8;
  --text: #252525;
  --text-strong: #202b42;
  --muted: #929292;
  --primary: #000000;
  --primary-dark: #333333;
  --danger: #b91c1c;
  --brand: #deff96;
  --row: #f8f8f9;
  --soft: #f8f8f8;
  --ok-bg: #edf7ed;
  --ok-text: #1e4620;
  --bad-bg: #fdecea;
  --bad-text: #611a15;
  --shadow: 14px 27px 45px 4px rgba(112, 144, 176, 0.2);
}

body {
  background: var(--bg);
  color: var(--text);
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  width: 100%;
  min-width: 100%;
  min-height: 100vh;
  overflow-x: hidden;
}

header {
  min-height: 80px;
  padding: 0 56px;
  background: #ffffff;
  color: var(--text);
  border-bottom: 1px solid #e5e7eb;
  box-shadow: none;
}

header > div:first-child {
  display: flex;
  align-items: center;
  gap: 24px;
  min-width: 0;
}

header strong {
  display: block;
  width: 102px;
  height: 42px;
  margin: 0;
  overflow: hidden;
  white-space: nowrap;
  text-indent: -9999px;
  background: url("/admin-ui/assets/nicky-logo.svg") left center / contain no-repeat;
  flex: 0 0 auto;
}

header > div:first-child::after {
  content: "Ticket Tailor Admin";
  order: 1;
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  padding-left: 24px;
  border-left: 1px solid #d1d5db;
  color: #374151;
  font-size: 16px;
  font-weight: 600;
  line-height: 24px;
  white-space: nowrap;
}

nav {
  order: 2;
  display: flex;
  gap: 6px;
  margin-left: 14px;
}

nav a,
header a {
  color: #000000;
  text-decoration: none;
}

nav a {
  display: inline-flex;
  align-items: center;
  min-height: 38px;
  padding: 8px 12px;
  border-radius: 12px;
  font-size: 14px;
  font-weight: 500;
  line-height: 20px;
}

nav a.active,
nav a:hover {
  background: #000000;
  color: #ffffff;
}

main {
  width: 100%;
  max-width: none;
  margin: 0;
  padding: 24px clamp(20px, 2.8vw, 56px);
  min-width: 0;
  overflow-x: hidden;
}

h1 {
  color: var(--text);
  font-size: 24px;
  font-weight: 600;
  line-height: 32px;
}

h2 {
  color: #111827;
  font-size: 18px;
  font-weight: 600;
  line-height: 24px;
}

.muted,
small {
  color: var(--muted);
}

.toolbar {
  margin-bottom: 16px;
}

.metrics {
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
}

.panel,
.table-wrap,
.metric,
.form-grid section {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: var(--shadow);
}

.table-wrap {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  overflow-x: auto;
  overscroll-behavior-x: contain;
}

.data-panel {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  box-shadow: var(--shadow);
  overflow: hidden;
}

.filters-shell {
  margin: 0;
  padding: 16px;
  border: 0;
  border-bottom: 1px solid var(--line);
  border-radius: 0;
  box-shadow: none;
  background: transparent;
}

.filters-form {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: 14px;
  width: 100%;
}

.filters-form label {
  display: flex;
  flex: 1 1 240px;
  flex-direction: column;
  gap: 8px;
  min-width: 200px;
  color: var(--text-strong);
  font-size: 14px;
  font-weight: 600;
}

.filters-form label input,
.filters-form label select {
  margin-top: 0;
}

.filter-actions {
  display: flex;
  align-self: end;
  flex: 0 0 auto;
  justify-content: flex-end;
  margin-left: auto;
}

.filter-actions .button,
.filter-actions button {
  min-width: 72px;
}

.data-panel .table-wrap {
  border: 0;
  border-radius: 0;
  box-shadow: none;
}

.data-panel table {
  min-width: max-content;
}

.data-panel th,
.data-panel td {
  white-space: nowrap;
}

.data-panel td:nth-child(5) {
  max-width: 280px;
  white-space: normal;
  overflow-wrap: anywhere;
}

section,
.split > div,
.form-grid section {
  min-width: 0;
}

.panel {
  padding: 16px;
}

.narrow {
  max-width: 460px;
  margin: 64px auto;
  position: relative;
}

.narrow::before {
  content: "";
  display: block;
  width: 118px;
  height: 46px;
  margin: 0 0 20px;
  background: url("/admin-ui/assets/nicky-logo.svg") left center / contain no-repeat;
}

.metric {
  position: relative;
  padding: 16px;
  overflow: hidden;
}

.metric::before {
  content: "";
  position: absolute;
  inset: 0 0 auto;
  height: 4px;
  background: var(--brand);
}

.metric span {
  color: #6b7280;
  font-size: 12px;
  font-weight: 600;
}

.metric strong {
  color: var(--text);
  font-size: 24px;
  font-weight: 700;
  line-height: 30px;
}

table {
  border-collapse: separate;
  border-spacing: 0;
}

th,
td {
  border-bottom: 1px solid var(--line);
  padding: 12px;
}

th {
  background: #f8f8f9;
  color: #6b7280;
  font-size: 12px;
  font-weight: 600;
  text-transform: none;
}

td {
  color: #111827;
  font-size: 14px;
  font-weight: 500;
}

tbody tr:nth-child(even) {
  background: var(--row);
}

tbody tr:hover {
  background: #f5f5f5;
}

.compact table {
  min-width: 100%;
}

a {
  color: #2563eb;
}

button,
.button {
  min-height: 36px;
  padding: 8px 16px;
  border: 1px solid #000000;
  border-radius: 8px;
  background: #000000;
  color: #ffffff;
  font-size: 14px;
  font-weight: 500;
  line-height: 18px;
  transition: background-color 120ms ease, border-color 120ms ease, color 120ms ease;
}

button:hover,
.button:hover {
  background: #333333;
  border-color: #333333;
}

.button.small {
  min-height: 32px;
  padding: 6px 12px;
  font-size: 13px;
}

.secondary {
  background: #ffffff;
  border-color: #e0e0e0;
  color: #000000;
}

.secondary:hover {
  background: #f5f5f5;
  border-color: #bdbdbd;
  color: #000000;
}

.danger {
  background: #ffffff;
  border-color: #f5c2c7;
  color: var(--danger);
}

.danger:hover {
  background: #fdecea;
  border-color: var(--danger);
  color: var(--danger);
}

.badge {
  min-height: 24px;
  padding: 3px 8px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 600;
}

.badge.ok {
  background: var(--ok-bg);
  color: var(--ok-text);
}

.badge.bad {
  background: var(--bad-bg);
  color: var(--bad-text);
}

.badge.warn {
  background: #fff7df;
  color: #7a4f01;
}

.cell-note {
  display: block;
  margin-top: 6px;
  color: var(--muted);
  font-size: 12px;
  line-height: 16px;
}

label {
  color: var(--text-strong);
  font-size: 14px;
  font-weight: 600;
}

input,
select,
textarea {
  min-height: 42px;
  border: 1px solid var(--line-strong);
  border-radius: 8px;
  padding: 8px 12px;
  color: #4b5563;
  background: #ffffff;
  box-shadow: 0 0 4px rgba(0, 0, 0, 0.05);
}

input:focus,
select:focus,
textarea:focus {
  outline: none;
  border-color: transparent;
  box-shadow: 0 0 0 2px #000000;
}

.check {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: #ffffff;
  padding: 10px 12px;
  color: #374151;
}

.check input {
  accent-color: #000000;
}

.notice {
  border: 1px solid #c3e6cb;
  background: #edf7ed;
  color: #1e4620;
  border-radius: 8px;
}

.notice.warning {
  border-color: #f6d58b;
  background: #fff8e6;
  color: #7a4f01;
}

.user,
.user-menu {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #111827;
  white-space: nowrap;
}

.user span,
.user-menu span {
  color: #111827;
  font-weight: 600;
}

.user small,
.user-menu small {
  color: var(--muted);
}

.user a,
.user-menu a {
  display: inline-flex;
  align-items: center;
  min-height: 32px;
  padding: 6px 10px;
  border-radius: 8px;
  background: #f5f5f5;
  color: #000000;
  font-size: 13px;
  font-weight: 500;
}

pre {
  background: #111214;
  border-radius: 8px;
}

@media (max-width: 1100px) {
  header {
    min-height: auto;
    padding: 14px 20px;
  }

  header > div:first-child {
    flex-wrap: wrap;
    gap: 12px;
  }

  header > div:first-child::after {
    padding-left: 12px;
    font-size: 14px;
  }

  nav {
    width: 100%;
    margin-left: 0;
    overflow-x: auto;
  }

  .filter-actions {
    justify-content: flex-start;
    margin-left: 0;
  }
}

@media (max-width: 760px) {
  .filters-shell {
    padding: 12px;
  }

  .filters-form {
    gap: 12px;
  }

  .filters-form label {
    flex-basis: 100%;
    min-width: 0;
  }

  .filter-actions {
    width: 100%;
    flex-wrap: wrap;
    margin-left: 0;
  }
}

/* Tailwind migration compatibility overrides. */
.nicky-logo {
  display: block !important;
  width: 102px !important;
  height: 42px !important;
  margin: 0 !important;
  overflow: hidden !important;
  white-space: nowrap !important;
  text-indent: -9999px !important;
  background: url("/admin-ui/assets/nicky-logo.svg") left center / contain no-repeat !important;
  flex: 0 0 auto !important;
}

header > div:first-child::after {
  content: none !important;
  display: none !important;
}
"""

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

.nicky-logo {
  display: block;
  width: 102px;
  height: 42px;
  margin: 0;
  overflow: hidden;
  white-space: nowrap;
  text-indent: -9999px;
  background: url("/admin-ui/assets/nicky-logo.svg") left center / contain no-repeat;
  flex: 0 0 auto;
}
"""
