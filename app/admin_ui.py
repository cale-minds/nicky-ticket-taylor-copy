from __future__ import annotations

import datetime
import html
import json
import secrets
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from app import admin_auth
from app.config import Settings, external_api_url
from app.i18n import (
    LOCALE_NAMES,
    SUPPORTED_LOCALES,
    current_locale,
    set_request_locale,
    t,
)
from app.db import Database
from app.nicky import NickyApiError, NickyClient
from app.service import IntegrationService, row_to_dict
from app.tenants import (
    NICKY_WEBHOOK_TYPE,
    TenantConfig,
    normalize_tenant_id,
    tenant_from_settings,
    tenant_to_safe_dict,
)


AdminDependency = Callable[..., Any]
DEFAULT_PAGE_SIZE = 10
DASHBOARD_PAGE_SIZE = 10
NO_TENANT_SCOPE = "__nicky_no_tenant_scope__"


@dataclass(frozen=True)
class TenantScope:
    tenant_filters: dict[str, str]
    order_tenant_id: str | None
    scoped: bool
    # None = privileged user (no restriction); frozenset = allowed tenant IDs for regular user
    allowed_tenant_ids: frozenset[str] | None = None


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

    @router.get("/admin-ui/assets/favicon.png")
    async def favicon():
        return FileResponse(static_dir / "favicon.png", media_type="image/png")

    @router.get("/admin-ui/assets/{name}.svg")
    async def static_svg(name: str):
        path = static_dir / f"{name}.svg"
        if not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="image/svg+xml")

    @router.get("/admin-ui/assets/flags/{name}.svg")
    async def flag_svg(name: str):
        path = static_dir / "flags" / f"{name}.svg"
        if not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="image/svg+xml")

    def require_admin_web(request: Request) -> admin_auth.AdminUser:
        user = admin_auth.authenticate_admin_request(
            settings,
            request,
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

        # Maintain the auth-subject <-> login-email directory so admins can later
        # resolve a user's owner_auth_subject from the email they type.
        if user.subject:
            db.upsert_user(user.subject, user.email, user.name)
            # Claim any active tenant created for this email by an admin (no owner yet).
            orphan = db.find_active_tenant_by_user_email(user.email)
            if orphan and not orphan.owner_auth_subject:
                db.upsert_tenant(replace(orphan, owner_auth_subject=user.subject))

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

    @router.get("/admin-ui/set-language")
    async def set_language(request: Request, lang: str = "en", next: str = "/admin-ui"):
        safe_next = next if next.startswith("/") else "/admin-ui"
        locale = lang if lang in SUPPORTED_LOCALES else "en"
        response = RedirectResponse(safe_next, status_code=303)
        response.set_cookie("lang", locale, max_age=60 * 60 * 24 * 365, httponly=False, samesite="lax")
        return response

    @router.get("/", response_class=HTMLResponse)
    @router.get("/admin-ui", response_class=HTMLResponse)
    @router.get("/admin-ui/", response_class=HTMLResponse)
    async def dashboard(
        request: Request, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        set_request_locale(request)
        tenant_scope = scoped_tenant_scope(user, settings, db)
        tenants = db.list_tenants(**tenant_scope.tenant_filters)
        order_filters = scoped_order_filters(dashboard_order_filters(request), tenant_scope)
        webhook_filters = scoped_webhook_filters(dashboard_webhook_filters(request), tenant_scope)
        tenants_page_number = page_query_value(request.query_params.get("tenants_page"))
        orders_page_number = page_query_value(request.query_params.get("orders_page"))
        webhooks_page_number = page_query_value(request.query_params.get("webhooks_page"))
        tenants_page_size = page_size_query_value(request.query_params.get("tenants_per_page"), DASHBOARD_PAGE_SIZE)
        orders_page_size = page_size_query_value(request.query_params.get("orders_per_page"), DASHBOARD_PAGE_SIZE)
        webhooks_page_size = page_size_query_value(request.query_params.get("webhooks_per_page"), DASHBOARD_PAGE_SIZE)
        tenants_total = db.count_tenants(**tenant_scope.tenant_filters)
        orders_total = db.count_orders(**order_filters)
        webhooks_total = db.count_webhook_events(**webhook_filters)
        dashboard_tenants = db.list_tenants(
            limit=tenants_page_size,
            offset=page_offset(tenants_page_number, tenants_page_size),
            **tenant_scope.tenant_filters,
        )
        orders = [
            row_to_dict(row)
            for row in db.list_orders(
                limit=orders_page_size,
                offset=page_offset(orders_page_number, orders_page_size),
                **order_filters,
            )
        ]
        webhooks = [
            dict(row)
            for row in db.list_webhook_events(
                limit=webhooks_page_size,
                offset=page_offset(webhooks_page_number, webhooks_page_size),
                **webhook_filters,
            )
        ]
        body = f"""
        {setup_modal(user, tenants, settings)}
        <section class="mb-6 flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
          <div>
            <h1 class="text-2xl font-semibold leading-8 text-slate-950">{t("DASHBOARD.TITLE")}</h1>
            <p class="mt-2 text-sm text-slate-500">{t("DASHBOARD.SUBTITLE")}</p>
          </div>
          {new_tenant_link(user, tenants, settings)}
        </section>
        {summary_grid(tenants, orders_total, webhooks_total)}
        {no_tenants_cta(user, tenants, settings)}
        <section class="mb-7 min-w-0">
          <h2 class="mb-3 text-lg font-semibold text-slate-950">{t("DASHBOARD.CUSTOMER_CONNECTIONS")}</h2>
          <div class="overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky">
            {tenant_table(dashboard_tenants, user=user, settings=settings, framed=False)}
            {pagination_controls(tenants_page_number, tenants_page_size, tenants_total, "/overview", request.query_params, "tenants_page", size_param="tenants_per_page", size_options=PAGE_SIZE_OPTIONS)}
          </div>
        </section>
        <div id="dash-filter-sections">
        <section class="mb-7 min-w-0">
          <h2 class="mb-3 text-lg font-semibold text-slate-950">{t("DASHBOARD.RECENT_WEBHOOKS")}</h2>
          <div class="overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky">
            {webhook_filters_form(webhook_filters, order_filters, tenants, action="/overview", show_tenant_filter=scope_shows_tenant_filter(tenant_scope), allow_all_tenants=not tenant_scope.scoped)}
            {webhook_table(webhooks, tenant_names={tn.tenant_id: tn.name for tn in tenants})}
            {pagination_controls(webhooks_page_number, webhooks_page_size, webhooks_total, "/overview", request.query_params, "webhooks_page", size_param="webhooks_per_page", size_options=PAGE_SIZE_OPTIONS)}
          </div>
        </section>
        <section class="mb-7 min-w-0">
          <h2 class="mb-3 text-lg font-semibold text-slate-950">{t("DASHBOARD.RECENT_ORDERS")}</h2>
          <div class="overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky">
            {order_filters_form(order_filters, webhook_filters, tenants, action="/overview", show_tenant_filter=scope_shows_tenant_filter(tenant_scope), allow_all_tenants=not tenant_scope.scoped)}
            {orders_table(orders, user=user, settings=settings, tenants={tn.tenant_id: tn for tn in tenants}, tenant_names={tn.tenant_id: tn.name for tn in tenants})}
            {pagination_controls(orders_page_number, orders_page_size, orders_total, "/overview", request.query_params, "orders_page", size_param="orders_per_page", size_options=PAGE_SIZE_OPTIONS)}
          </div>
        </section>
        </div>
        """
        return html_response(render(request, t("NAV.DASHBOARD"), body, current_path="/admin-ui", settings=settings))

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
        set_request_locale(request)
        tenant_scope = scoped_tenant_scope(user, settings, db)
        filters = tenant_page_filters(request)
        page_number = page_query_value(request.query_params.get("page"))
        page_size = page_size_query_value(request.query_params.get("per_page"))
        total = db.count_tenants(**filters, **tenant_scope.tenant_filters)
        tenants = db.list_tenants(
            limit=page_size,
            offset=page_offset(page_number, page_size),
            **filters,
            **tenant_scope.tenant_filters,
        )
        body = f"""
        <section class="mb-6 flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
          <div>
            <h1 class="text-2xl font-semibold leading-8 text-slate-950">{t("TENANTS.TITLE")}</h1>
            <p class="mt-2 text-sm text-slate-500">{t("TENANTS.SUBTITLE")}</p>
          </div>
          {new_tenant_link(user, tenants, settings)}
        </section>
        <div id="tenants-card" class="overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky">
          {tenant_filters_form(filters)}
          {tenant_table(tenants, user=user, settings=settings, framed=False)}
          {pagination_controls(page_number, page_size, total, "/admin-ui/tenants", request.query_params, "page", size_param="per_page", size_options=PAGE_SIZE_OPTIONS)}
        </div>
        """
        return html_response(render(request, t("NAV.TENANTS"), body, current_path="/admin-ui/tenants", settings=settings))

    @router.get("/admin-ui/tenants/new", response_class=HTMLResponse)
    async def new_tenant(
        request: Request, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        set_request_locale(request)
        if admin_auth.is_support(user) and not admin_auth.is_admin(user, settings):
            raise HTTPException(status_code=403, detail="Support access is read-only")
        tenant_id = "new-tenant" if admin_auth.is_admin(user, settings) else admin_auth.nicky_user_uuid(user)
        tenant = tenant_from_settings(settings, tenant_id)
        existing_tenant: TenantConfig | None = None
        if not admin_auth.is_admin(user, settings):
            existing_tenant = db.find_active_tenant_by_owner(user.subject)
        return html_response(
            render(
                request,
                t("TENANTS.FORM_TITLE_NEW"),
                tenant_form(tenant, is_new=True, settings=settings, user=user, replace_notice=existing_tenant),
                current_path="/admin-ui/tenants",
                settings=settings,
            )
        )

    @router.get("/admin-ui/tenants/{tenant_id}/edit", response_class=HTMLResponse)
    async def edit_tenant(
        tenant_id: str, request: Request, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        set_request_locale(request)
        tenant = db.get_tenant(normalize_tenant_id(tenant_id))
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant not found")
        require_tenant_visible(user, settings, tenant)
        return html_response(
            render(
                request,
                f"{t('TENANTS.FORM_TITLE_EDIT')} {tenant.tenant_id}",
                tenant_form(
                    tenant,
                    is_new=False,
                    settings=settings,
                    user=user,
                    saved=request.query_params.get("saved") == "1",
                    message=request.query_params.get("message"),
                    warn=request.query_params.get("warn"),
                ),
                current_path="/admin-ui/tenants",
                settings=settings,
            )
        )

    @router.post("/admin-ui/tenants/save")
    async def save_tenant(
        request: Request, user: admin_auth.AdminUser = Depends(require_admin_web)
    ):
        set_request_locale(request)
        if admin_auth.is_support(user) and not admin_auth.is_admin(user, settings):
            raise HTTPException(status_code=403, detail="Support access is read-only")
        form = await read_form(request)
        requested_tenant_id = form.get("tenant_id", "")
        normalized_requested_tenant_id = (
            normalize_tenant_id(requested_tenant_id) if requested_tenant_id else None
        )
        existing = (
            db.get_tenant(normalized_requested_tenant_id)
            if normalized_requested_tenant_id
            else None
        )
        base_for_secret = existing or tenant_from_settings(
            settings, normalized_requested_tenant_id or "pending-tenant"
        )
        nicky_key_changed = bool(form.get("nicky_api_key", ""))
        api_key = form_secret(form, "nicky_api_key", base_for_secret.nicky_api_key)
        if not api_key:
            raise HTTPException(status_code=400, detail="Nicky API key is required")

        if nicky_key_changed:
            try:
                validation = await nicky_client.validate_api_key(api_key)
            except NickyApiError as exc:
                raise HTTPException(status_code=400, detail=f"Invalid Nicky API key: {exc}") from exc
            raw_nicky_user_uuid = str(validation.get("nicky_user_uuid") or "")
            raw_nicky_user_short_id = str(validation.get("nicky_user_short_id") or "")
            raw_nicky_user_email = str(validation.get("nicky_user_email") or "")
            asset_id = form.get("nicky_default_blockchain_asset_id", "")
            available_asset_ids = {str(a.get("id") or "") for a in (validation.get("assets") or [])}
            if not asset_id:
                raise HTTPException(status_code=400, detail="Nicky asset is required")
            if available_asset_ids and asset_id not in available_asset_ids:
                raise HTTPException(status_code=400, detail="Selected asset is not available for this Nicky API key")
        else:
            # Key unchanged — skip external validation, reuse stored values
            raw_nicky_user_uuid = existing.nicky_user_uuid if existing else ""
            raw_nicky_user_short_id = existing.nicky_user_short_id if existing else ""
            raw_nicky_user_email = existing.nicky_user_email if existing else ""
            asset_id = (
                form.get("nicky_default_blockchain_asset_id", "")
                or (existing.nicky_default_blockchain_asset_id if existing else "")
            )
            if not asset_id:
                raise HTTPException(status_code=400, detail="Nicky asset is required")

        auth0_identifier = admin_auth.user_identifier(user)
        manual_nicky_user_email = form.get("nicky_user_email", "").strip()
        if admin_auth.is_admin(user, settings):
            nicky_user_email = raw_nicky_user_email or manual_nicky_user_email
        else:
            nicky_user_email = raw_nicky_user_email or auth0_identifier
        if not nicky_user_email:
            raise HTTPException(status_code=400, detail="Nicky email is required")
        tenant_id = (
            normalized_requested_tenant_id
            if normalized_requested_tenant_id and existing
            else normalize_tenant_id(raw_nicky_user_uuid) if raw_nicky_user_uuid else generate_tenant_id(db)
        )
        base = existing or db.get_tenant(tenant_id) or tenant_from_settings(settings, tenant_id)
        if (
            not admin_auth.is_admin(user, settings)
            and base.owner_auth_subject
            and base.owner_auth_subject != user.subject
        ):
            raise HTTPException(status_code=403, detail="Tenant outside user scope")
        ticket_tailor_api_key = form_secret(
            form, "ticket_tailor_api_key", base.ticket_tailor_api_key
        )

        # --- Resolve the owner subject from the users directory (admin flow) ---
        is_admin_user = admin_auth.is_admin(user, settings)
        if is_admin_user:
            resolved_owner_subject = (
                base.owner_auth_subject
                or (db.find_user_subject_by_email(nicky_user_email) or "")
            )
        else:
            resolved_owner_subject = user.subject

        # --- Auto-deactivate existing active tenant for this user/email ---
        is_new_tenant = existing is None
        if is_new_tenant:
            conflict_tenant: TenantConfig | None = None
            if resolved_owner_subject:
                conflict_tenant = db.find_active_tenant_by_owner(resolved_owner_subject)
            if not conflict_tenant and is_admin_user and nicky_user_email:
                conflict_tenant = db.find_active_tenant_by_user_email(nicky_user_email)
            if not conflict_tenant and not is_admin_user:
                conflict_tenant = db.find_active_tenant_by_owner(user.subject)
            if conflict_tenant and conflict_tenant.tenant_id != tenant_id:
                try:
                    old_url_pattern = f"/webhooks/nicky/{conflict_tenant.tenant_id}"
                    webhooks = await nicky_client.list_webhooks(api_key)
                    for wh in webhooks:
                        wh_url = str(wh.get("url") or "")
                        wh_id = str(wh.get("id") or "")
                        if old_url_pattern in wh_url and wh_id:
                            await nicky_client.delete_webhook(api_key, wh_id)
                except NickyApiError:
                    pass
                db.deactivate_tenant(conflict_tenant.tenant_id)

        ensure_unique_active_api_keys(
            db,
            nicky_api_key=api_key,
            ticket_tailor_api_key=ticket_tailor_api_key,
            exclude_tenant_id=tenant_id,
        )

        tenant = replace(
            base,
            tenant_id=tenant_id,
            name=form.get("name") or raw_nicky_user_short_id or nicky_user_email or tenant_id,
            active=True,
            nicky_user_uuid=raw_nicky_user_uuid,
            nicky_user_short_id=raw_nicky_user_short_id,
            nicky_user_email=nicky_user_email,
            ticket_tailor_api_key=ticket_tailor_api_key,
            ticket_tailor_webhook_signing_secret="",
            nicky_api_key=api_key,
            nicky_default_blockchain_asset_id=asset_id,
            nicky_receiver_short_id=raw_nicky_user_short_id,
            nicky_webhook_token=base.nicky_webhook_token or secrets.token_urlsafe(24),
            nicky_webhook_type=NICKY_WEBHOOK_TYPE,
            nicky_send_notification=True,
            owner_auth_subject=resolved_owner_subject,
        )
        db.upsert_tenant(tenant)
        if nicky_key_changed:
            try:
                webhook_result = await nicky_client.create_webhook(tenant, build_nicky_webhook_url(settings, tenant))
                webhook_id = webhook_result.get("webhook_id", "")
                if webhook_id:
                    db.upsert_tenant(replace(tenant, nicky_webhook_id=webhook_id))
            except NickyApiError:
                return RedirectResponse(
                    f"/admin-ui/tenants/{urllib.parse.quote(tenant.tenant_id)}/edit?saved=1&warn=webhook_failed",
                    status_code=303,
                )
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
        if not can_write_tenants(user, settings):
            raise HTTPException(status_code=403, detail="Support access is read-only")
        tenant = get_tenant_or_404(db, tenant_id)
        require_tenant_visible(user, settings, tenant)
        if tenant.nicky_webhook_id:
            try:
                await nicky_client.delete_webhook(tenant.nicky_api_key, tenant.nicky_webhook_id)
            except NickyApiError:
                pass
        db.deactivate_tenant(tenant.tenant_id)
        return RedirectResponse("/admin-ui/tenants", status_code=303)

    @router.get("/admin-ui/orders", response_class=HTMLResponse)
    async def orders_page(
        request: Request,
        user: admin_auth.AdminUser = Depends(require_admin_web),
    ):
        set_request_locale(request)
        tenant_scope = scoped_tenant_scope(user, settings, db)
        tenants = db.list_tenants(**tenant_scope.tenant_filters)
        order_filters = scoped_order_filters(orders_page_filters(request), tenant_scope)
        page_number = page_query_value(request.query_params.get("page"))
        page_size = page_size_query_value(request.query_params.get("per_page"))
        total = db.count_orders(**order_filters)
        orders = [
            row_to_dict(row)
            for row in db.list_orders(
                limit=page_size,
                offset=page_offset(page_number, page_size),
                **order_filters,
            )
        ]
        body = f"""
        <section class="mb-6 flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
          <div>
            <h1 class="text-2xl font-semibold leading-8 text-slate-950">{t("ORDERS.TITLE")}</h1>
            <p class="mt-2 text-sm text-slate-500">{t("ORDERS.SUBTITLE")}</p>
          </div>
        </section>
        <div id="orders-card" class="overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky">
          {orders_page_filters_form(order_filters, tenants, show_tenant_filter=scope_shows_tenant_filter(tenant_scope), allow_all_tenants=not tenant_scope.scoped)}
          {orders_table(orders, user=user, settings=settings, tenants={t.tenant_id: t for t in tenants}, tenant_names={t.tenant_id: t.name for t in tenants})}
          {pagination_controls(page_number, page_size, total, "/admin-ui/orders", request.query_params, "page", size_param="per_page", size_options=PAGE_SIZE_OPTIONS)}
        </div>
        """
        return html_response(render(request, t("NAV.ORDERS"), body, current_path="/admin-ui/orders", settings=settings))

    @router.get("/admin-ui/orders/{ticket_tailor_order_id}", response_class=HTMLResponse)
    async def order_detail(
        ticket_tailor_order_id: str,
        request: Request,
        tenant_id: str,
        user: admin_auth.AdminUser = Depends(require_admin_web),
    ):
        set_request_locale(request)
        tenant = normalize_tenant_id(tenant_id)
        tenant_scope = scoped_tenant_scope(user, settings, db)
        if tenant_scope.scoped:
            allowed = tenant_scope.allowed_tenant_ids or frozenset()
            if tenant not in allowed:
                raise HTTPException(status_code=403, detail="Order outside user scope")
        row = db.get_order(tenant, ticket_tailor_order_id)
        if not row:
            raise HTTPException(status_code=404, detail="Order not found")
        order = row_to_dict(row)
        tenant_config = db.get_tenant(tenant)
        tenant_display = (tenant_config.name or "").strip() if tenant_config else ""
        tenant_label = tenant_display if tenant_display else compact_identifier(tenant)
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
        nicky_dashboard_button_html = nicky_dashboard_button(
            order, settings, user=user, tenant=tenant_config
        )
        body = f"""
        <section class="mb-6 flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
          <div>
            <h1 class="text-2xl font-semibold leading-8 text-slate-950">{t("ORDERS.DETAIL_TITLE", order_id=e(ticket_tailor_order_id))}</h1>
            <p class="mt-2 text-sm text-slate-500">{t("ORDERS.DETAIL_SUBTITLE", tenant=e(tenant_label), buyer=e(order.get("buyer_email") or ""))}</p>
          </div>
          <div class="flex flex-wrap items-center gap-3">
            {nicky_dashboard_button_html}
            <a class="inline-flex h-10 shrink-0 items-center justify-center rounded-lg border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-950 hover:bg-slate-50" href="/admin-ui/orders?tenant_id={u(tenant)}">{t("ORDERS.DETAIL_BUTTON_BACK")}</a>
          </div>
        </section>
        {order_mapping_panel(order)}
        {order_actions(order, user=user, settings=settings)}
        <section class="mb-7 min-w-0">
          <h2 class="mb-3 text-lg font-semibold text-slate-950">{t("ORDERS.DETAIL_SECTION_LOGS")}</h2>
          <div class="overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky">
            {logs_table(logs, framed=False)}
            {pagination_controls(log_page, DEFAULT_PAGE_SIZE, logs_total, f"/admin-ui/orders/{u(ticket_tailor_order_id)}", request.query_params, "logs_page")}
          </div>
        </section>
        <section class="mb-7 rounded-xl border border-slate-100 bg-white p-4 shadow-nicky">
          <h2 class="mb-3 text-lg font-semibold text-slate-950">{t("ORDERS.DETAIL_SECTION_PAYLOAD")}</h2>
          <details><summary class="cursor-pointer text-sm font-semibold text-slate-950">{t("COMMON.PAYLOAD")}</summary><pre class="mt-3 overflow-x-auto rounded-lg bg-zinc-950 p-4 text-xs text-slate-100">{e(json.dumps(order.get("raw_payload"), indent=2, ensure_ascii=False))}</pre></details>
        </section>
        """
        return html_response(render(request, t("ORDERS.TITLE"), body, current_path="/admin-ui/orders", settings=settings))

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


def render(
    request: Request, title: str, body: str, *, current_path: str, settings: Settings
) -> str:
    return page(
        title,
        body,
        current_path=current_path,
        user=admin_auth.get_session_user(request),
        settings=settings,
    )


def page(
    title: str,
    body: str,
    *,
    current_path: str,
    user: admin_auth.AdminUser | None,
    settings: Settings,
) -> str:
    user_block = ""
    hamburger_user_section = ""
    if user:
        easter_egg = user_easter_egg(user, settings)
        initials = e("".join(p[0].upper() for p in (user.name or "?").split()[:2]))
        email = e(user.email or "")
        user_block = f"""
        <div class="hidden items-center gap-3 text-sm md:flex">
          {lang_switcher(current_path)}
          <div class="relative" id="user-menu-wrapper">
            <button type="button" onclick="document.getElementById('user-menu').classList.toggle('hidden')"
              class="inline-flex h-9 items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 text-sm font-semibold text-slate-900 hover:bg-slate-50">
              <span class="flex h-6 w-6 items-center justify-center rounded-full bg-slate-900 text-xs font-bold text-white">{initials}</span>
              {easter_egg}
              <i class="ph ph-caret-down text-xs text-slate-400"></i>
            </button>
            <div id="user-menu" class="absolute right-0 z-50 mt-1 hidden min-w-[220px] rounded-xl border border-slate-100 bg-white py-1 shadow-nicky">
              <div class="flex items-center gap-3 border-b border-slate-100 px-4 py-3">
                <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-slate-900 text-sm font-bold text-white">{initials}</span>
                <div class="min-w-0">
                  <p class="truncate font-semibold text-slate-900">{e(user.name)}</p>
                  <p class="truncate text-xs text-slate-400">{email}</p>
                </div>
              </div>
              <a href="/admin-ui/logout"
                class="flex items-center gap-3 px-4 py-2.5 text-sm text-slate-700 hover:bg-slate-50">
                <i class="ph ph-sign-out text-base text-slate-400"></i>
                {t("NAV.LOG_OUT")}
              </a>
            </div>
          </div>
        </div>
        <script>
          document.addEventListener('click', function(e) {{
            var w = document.getElementById('user-menu-wrapper');
            if (w && !w.contains(e.target)) document.getElementById('user-menu').classList.add('hidden');
          }});
        </script>
        """
        locale = current_locale()
        locale_name = e(LOCALE_NAMES.get(locale, locale))
        locale_items = "".join(
            f'<a class="flex items-center gap-2.5 px-4 py-2 text-sm {"font-semibold text-slate-950" if lc == locale else "text-slate-700"} hover:bg-slate-50" href="/admin-ui/set-language?lang={e(lc)}&next={e(current_path)}"><img src="/admin-ui/assets/flags/{e(lc)}.svg" class="h-4 w-6 rounded-sm object-cover" alt="{e(lc)}"><span>{e(LOCALE_NAMES.get(lc, lc))}</span></a>'
            for lc in SUPPORTED_LOCALES
        )
        hamburger_user_section = f"""
        <div class="flex items-center gap-3 border-b border-slate-100 px-4 py-3">
          <span class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-slate-900 text-sm font-bold text-white">{initials}</span>
          <div class="min-w-0 flex-1">
            <p class="flex items-center gap-1 truncate font-semibold text-slate-900">{e(user.name)}{easter_egg}</p>
            <p class="truncate text-xs text-slate-400">{email}</p>
          </div>
        </div>
        <div class="relative border-b border-slate-100" id="hamburger-lang-wrapper">
          <button type="button" onclick="document.getElementById('hamburger-lang-menu').classList.toggle('hidden')"
            class="flex w-full items-center gap-3 px-4 py-2.5 text-sm text-slate-700 hover:bg-slate-50">
            <img src="/admin-ui/assets/flags/{e(locale)}.svg" class="h-4 w-6 rounded-sm object-cover" alt="{e(locale)}">
            <span class="flex-1 text-left">{locale_name}</span>
            <i class="ph ph-caret-down text-xs text-slate-400"></i>
          </button>
          <div id="hamburger-lang-menu" class="hidden border-t border-slate-100 bg-slate-50 py-1">
            {locale_items}
          </div>
        </div>
        <a href="/admin-ui/logout" class="flex items-center gap-3 px-4 py-2.5 text-sm text-slate-700 hover:bg-slate-50">
          <i class="ph ph-sign-out text-base text-slate-400"></i>
          {t("NAV.LOG_OUT")}
        </a>
        <script>
          document.addEventListener('click', function(ev) {{
            var w2 = document.getElementById('hamburger-lang-wrapper');
            if (w2 && !w2.contains(ev.target)) {{
              var m2 = document.getElementById('hamburger-lang-menu');
              if (m2) m2.classList.add('hidden');
            }}
          }});
        </script>
        """
    nav = "".join(
        nav_link(t(key), href, current_path, icon)
        for key, href, icon in _NAV_ITEMS
    )
    nav_dropdown = "".join(
        nav_dropdown_link(t(key), href, current_path, icon)
        for key, href, icon in _NAV_ITEMS
    )
    locale = current_locale()
    return f"""<!doctype html>
<html lang="{e(locale)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(title)} - Nicky Ticket Tailor</title>
  <link rel="icon" type="image/png" href="/admin-ui/assets/favicon.png">
  <link rel="stylesheet" href="https://unpkg.com/@phosphor-icons/web@2.1.1/src/regular/style.css">
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
<body class="flex min-h-screen flex-col overflow-x-hidden bg-[#f1f1f1] font-sans text-slate-950">

  <!-- Nicky loading overlay -->
  <div id="nicky-overlay" aria-hidden="true" style="display:none;position:fixed;inset:0;z-index:99998;background:rgba(0,0,0,0.78);align-items:center;justify-content:center;">
    <div style="display:flex;flex-direction:column;align-items:center;gap:1rem;">
      <svg id="nicky-overlay-logo" style="width:120px;animation:nicky-pulse 1.8s ease-in-out infinite;" viewBox="0 0 1479 584" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M475.396 526.93C485.802 484.133 496.193 440.133 512.193 376.523C527.802 312.93 538.99 269.336 550.193 226.93H398.193C388.599 269.336 377.396 313.336 361.396 376.93C345.802 440.523 334.99 484.133 323.396 526.93H475.396ZM812.994 394.523C788.994 394.93 768.197 395.727 744.197 395.727C720.994 395.727 703.4 395.336 686.197 394.523C685.4 400.133 683.79 406.133 682.197 411.727C677.79 428.523 672.197 442.523 664.994 442.523C655.79 442.523 656.603 431.336 666.994 388.93C679.4 340.93 684.197 334.133 691.79 334.133C698.197 334.133 698.994 341.336 692.994 366.133C691.79 370.133 689.79 375.336 688.994 380.93C701.4 380.133 724.603 379.727 748.993 379.727C776.197 379.727 797.4 380.133 815.79 380.93C818.994 371.727 822.197 363.727 823.79 356.133C843.79 276.93 812.994 257.727 695.79 257.727C600.994 257.727 556.994 278.133 531.79 378.93C503.4 492.93 525.79 532.133 646.603 532.133C745.79 532.133 784.603 512.93 806.993 423.336C809.4 414.523 811.4 404.93 812.994 394.523ZM1035.39 262.93C1029.79 276.93 1019 303.336 1005.39 328.523C999.794 339.336 991.794 353.727 984.201 368.133H982.591C998.591 304.523 1008.59 260.93 1021 217.336H878.997C869.388 258.133 858.201 302.523 846.201 350.133C830.591 412.93 814.997 477.727 801.794 526.93H943.794C950.201 496.133 957.794 465.727 967.388 428.93H970.591C974.998 444.133 969.794 477.727 967.794 512.133C967.388 523.727 970.591 526.93 983.794 526.93H1107C1109.39 456.523 1138.2 395.336 1069.39 391.727L1069.79 390.133C1143.39 382.93 1149.79 314.133 1173.79 262.93H1035.39ZM1342.19 262.93C1336.6 283.727 1319.79 352.93 1314.19 374.133C1310.99 387.336 1306.19 393.336 1299.4 393.336C1293.4 393.336 1290.6 389.336 1293.79 377.336C1303.79 336.93 1315.4 292.93 1323.79 262.93H1189.79C1185.4 282.523 1180.6 301.727 1174.6 326.133C1168.6 351.336 1162.6 372.133 1156.6 396.523C1145.79 442.93 1169.79 469.336 1230.99 469.336C1260.6 469.336 1278.99 464.133 1292.19 451.727L1294.19 452.523C1291.79 460.133 1289.4 468.133 1286.99 476.93C1280.99 500.133 1276.99 506.93 1267.79 506.93C1260.19 506.93 1259.4 500.523 1262.6 488.133C1262.99 486.133 1263.79 483.727 1264.6 480.93C1250.19 482.133 1233.4 482.93 1202.6 482.93C1173.4 482.93 1153.4 482.133 1137.79 480.93C1134.6 490.133 1132.19 498.133 1130.19 506.133C1117.4 556.523 1146.99 583.727 1255.79 583.727C1386.19 554.133 1406.6 554.133 1430.6 457.336C1436.99 432.133 1448.6 380.523 1454.19 358.523C1464.6 316.93 1471.4 290.133 1478.99 262.93H1342.19Z" fill="#ffffff"/>
        <path class="nicky-star-1" d="M540.953 157.333C540.953 163.224 511.105 168 474.286 168C437.468 168 407.62 163.224 407.62 157.333C407.62 151.443 437.468 146.667 474.286 146.667C511.105 146.667 540.953 151.443 540.953 157.333Z" fill="#6B7280"/>
        <path class="nicky-star-1" d="M486.286 157.333C486.286 190.471 480.914 217.333 474.286 217.333C467.658 217.333 462.286 190.471 462.286 157.333C462.286 124.196 467.658 97.3335 474.286 97.3335C480.914 97.3335 486.286 124.196 486.286 157.333Z" fill="#6B7280"/>
        <path class="nicky-star-2" d="M648.953 48.6667C648.953 53.4453 624.744 57.3186 594.879 57.3186C565.015 57.3186 540.805 53.4453 540.805 48.6667C540.805 43.888 565.015 40.0146 594.879 40.0146C624.744 40.0146 648.953 43.888 648.953 48.6667Z" fill="#D1D5DB"/>
        <path class="nicky-star-2" d="M604.612 48.6667C604.612 75.544 600.254 97.3333 594.878 97.3333C589.504 97.3333 585.145 75.544 585.145 48.6667C585.145 21.7893 589.504 0 594.878 0C600.254 0 604.612 21.7893 604.612 48.6667Z" fill="#D1D5DB"/>
        <path class="nicky-star-3" d="M726.286 157C726.286 160.24 709.87 162.867 689.62 162.867C669.369 162.867 652.953 160.24 652.953 157C652.953 153.76 669.369 151.133 689.62 151.133C709.87 151.133 726.286 153.76 726.286 157Z" fill="#111827"/>
        <path class="nicky-star-3" d="M696.22 157C696.22 175.225 693.265 190 689.62 190C685.974 190 683.02 175.225 683.02 157C683.02 138.775 685.974 124 689.62 124C693.265 124 696.22 138.775 696.22 157Z" fill="#111827"/>
        <path d="M374.851 195.892L258.492 196.693L232.114 314.918L197.392 196.408L62.2746 197.163C62.2746 197.163 72.4649 214.524 70.9553 245.095C69.4456 275.666 0 526.274 0 526.274H114.736L149.082 376.438L193.24 525.897H290.237L374.851 195.892Z" fill="#ffffff"/>
      </svg>
    </div>
  </div>

  <header class="flex w-full items-center justify-between border-b border-slate-200 bg-white px-5 py-3 md:px-10 md:py-4 xl:px-14">
    <div class="flex min-w-0 items-center gap-4 md:gap-6">
      <strong class="nicky-logo">Nicky Ticket Tailor Admin</strong>
      <span class="hidden min-h-7 items-center border-l border-slate-300 pl-6 text-base font-semibold text-slate-700 sm:inline-flex">Ticket Tailor Admin</span>
      <nav id="page-nav" class="hidden min-w-0 items-center gap-2 md:flex">{nav}</nav>
    </div>
    <div class="flex items-center gap-2">
      {user_block}
      <div class="relative md:hidden" id="hamburger-wrapper">
        <button type="button" id="hamburger-btn" aria-label="Menu"
          onclick="document.getElementById('hamburger-menu').classList.toggle('hidden')"
          class="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-slate-200 bg-white text-slate-700 hover:bg-slate-50">
          <i class="ph ph-list text-lg"></i>
        </button>
        <div id="hamburger-menu" class="absolute right-0 z-50 mt-1 hidden min-w-[200px] rounded-xl border border-slate-100 bg-white py-1 shadow-nicky">
          {nav_dropdown}
          <div class="border-t border-slate-100 pt-1">{hamburger_user_section}</div>
        </div>
      </div>
    </div>
  </header>
  <script>
    document.addEventListener('click', function(ev) {{
      var w = document.getElementById('hamburger-wrapper');
      if (w && !w.contains(ev.target)) {{
        var m = document.getElementById('hamburger-menu');
        if (m) m.classList.add('hidden');
      }}
    }});
  </script>
  <main id="page-main" class="w-full min-w-0 flex-1 overflow-x-hidden px-5 py-6 md:px-10 xl:px-14">{body}</main>
  <footer class="border-t border-gray-100 bg-white text-xs">
    <div class="flex min-h-[52px] flex-col-reverse items-center justify-between gap-2 px-5 py-3 sm:flex-row sm:items-center md:px-10 xl:px-14">
      <div class="text-center text-gray-500 sm:text-left">&copy; <span id="nicky-footer-year"></span> Nicky L.L.C.</div>
      <div class="flex flex-col items-center gap-1 sm:items-end">
        <p class="text-center text-gray-500 sm:text-right">Nicky does not hold funds. We are an API enabled service.</p>
        <nav>
          <ul class="flex flex-wrap items-center justify-center gap-3 sm:justify-end sm:gap-6">
            <li><a class="text-gray-500 transition-colors duration-200 hover:text-gray-900" href="https://nicky.me/terms-of-use/" target="_blank" rel="noopener noreferrer">Terms of Service</a></li>
            <li><a class="text-gray-500 transition-colors duration-200 hover:text-gray-900" href="https://nicky.me/privacy-policy/" target="_blank" rel="noopener noreferrer">Privacy Policy</a></li>
            <li><a class="text-gray-500 transition-colors duration-200 hover:text-gray-900" href="https://nicky.me/legal-disclosure/" target="_blank" rel="noopener noreferrer">Legal Disclosure</a></li>
            <li><a class="text-gray-500 transition-colors duration-200 hover:text-gray-900" href="https://nicky.me/privacy-policy/" target="_blank" rel="noopener noreferrer">Cookie Policy</a></li>
          </ul>
        </nav>
      </div>
    </div>
  </footer>
  <script>
    document.getElementById("nicky-footer-year").textContent = new Date().getFullYear();
  </script>
  <script>
    (function () {{
      /* ── Nicky loading overlay ─────────────────────────────────── */
      var _overlay = document.getElementById('nicky-overlay');

      function showNickyLoader() {{
        _overlay.style.display = 'flex';
        _overlay.setAttribute('aria-hidden', 'false');
      }}
      function hideNickyLoader() {{
        _overlay.style.display = 'none';
        _overlay.setAttribute('aria-hidden', 'true');
      }}
      window.showNickyLoader = showNickyLoader;
      window.hideNickyLoader = hideNickyLoader;

      /* star color animation */
      var _starPalette = ['#111827', '#6B7280', '#D1D5DB'];
      var _starIdx = 0;
      var _starEls = [
        document.querySelector('.nicky-star-1'),
        document.querySelector('.nicky-star-2'),
        document.querySelector('.nicky-star-3'),
      ].filter(Boolean);
      if (_starEls.length) {{
        setInterval(function () {{
          _starEls.forEach(function (el, i) {{
            el.style.fill = _starPalette[(_starIdx + i) % _starPalette.length];
          }});
          _starIdx = (_starIdx + 1) % _starPalette.length;
        }}, 200);
      }}

      /* show on form submit */
      document.querySelectorAll('form').forEach(function (form) {{
        form.addEventListener('submit', function () {{ showNickyLoader(); }});
      }});

      /* show on internal link clicks */
      document.addEventListener('click', function (e) {{
        var el = e.target.closest('a[href]');
        if (!el) return;
        var href = el.getAttribute('href');
        if (!href || href.startsWith('#') || href.startsWith('javascript')) return;
        if (el.target === '_blank') return;
        try {{
          var url = new URL(href, location.href);
          if (url.origin !== location.origin) return;
        }} catch (_) {{ return; }}
        showNickyLoader();
      }});

      /* wrap fetch to show overlay during API calls */
      var _origFetch = window.fetch;
      window._nickyRawFetch = _origFetch;
      window.fetch = function () {{
        showNickyLoader();
        return _origFetch.apply(this, arguments).finally(function () {{ hideNickyLoader(); }});
      }};

      /* safety: hide on back-nav */
      window.addEventListener('pageshow', function () {{ hideNickyLoader(); }});
    }})();
  </script>
  <script>
    /* ── AJAX navigation — zero-friction in-place updates ─────── */
    (function () {{
      var _raw = window._nickyRawFetch || window.fetch;

      /* card IDs that support in-place swap */
      var CARD_IDS = ['tenants-card', 'orders-card', 'dash-filter-sections'];

      /* ── helpers ── */
      function runScripts(root) {{
        root.querySelectorAll('script').forEach(function (s) {{
          var el = document.createElement('script');
          if (s.src) {{ el.src = s.src; el.async = false; }}
          else {{ el.textContent = s.textContent; }}
          document.head.appendChild(el);
          el.parentNode.removeChild(el);
        }});
      }}

      function swapById(doc, id, reinit) {{
        var cur = document.getElementById(id);
        var fresh = doc.getElementById(id);
        if (cur && fresh) {{ cur.replaceWith(fresh); runScripts(fresh); if (reinit) reinit(fresh); return true; }}
        return false;
      }}

      function swapMain(doc) {{
        var cur = document.getElementById('page-main');
        var fresh = doc.getElementById('page-main');
        var nav = document.getElementById('page-nav');
        var freshNav = doc.getElementById('page-nav');
        if (cur && fresh) {{ cur.replaceWith(fresh); runScripts(fresh); }}
        if (nav && freshNav) nav.replaceWith(freshNav);
        document.title = doc.title;
        initAll(document);
      }}

      function ajaxGo(url, targetId) {{
        showNickyLoader();
        history.pushState(null, '', url);
        _raw(url)
          .then(function (r) {{
            if (!r.ok) throw new Error(r.status);
            return r.text();
          }})
          .then(function (html) {{
            var doc = new DOMParser().parseFromString(html, 'text/html');
            if (targetId) {{
              if (!swapById(doc, targetId, initAll)) swapMain(doc);
            }} else {{
              swapMain(doc);
            }}
          }})
          .catch(function () {{ window.location.href = url; }})
          .finally(function () {{ hideNickyLoader(); }});
      }}

      /* ── find which card (if any) contains an element ── */
      function cardContaining(el) {{
        for (var i = 0; i < CARD_IDS.length; i++) {{
          var card = document.getElementById(CARD_IDS[i]);
          if (card && card.contains(el)) return CARD_IDS[i];
        }}
        return null;
      }}

      /* ── filter form submit ── */
      function initForms(root) {{
        (root || document).querySelectorAll('form[data-ajax-filter]').forEach(function (form) {{
          if (form._ajaxInit) return;
          form._ajaxInit = true;
          form.addEventListener('submit', function (e) {{
            var targetId = form.getAttribute('data-ajax-filter');
            if (!document.getElementById(targetId)) return;
            e.preventDefault();
            var params = new URLSearchParams(new FormData(form));
            var base = form.getAttribute('action') || window.location.pathname;
            ajaxGo(base + '?' + params.toString(), targetId);
          }});
        }});
      }}

      /* ── global click interception ── */
      document.addEventListener('click', function (e) {{
        var link = e.target.closest('a[href]');
        if (!link || e.defaultPrevented) return;
        var href = link.getAttribute('href');
        if (!href || href.startsWith('#') || href.startsWith('javascript')) return;
        if (link.target === '_blank') return;
        var url;
        try {{ url = new URL(href, location.href); }} catch (_) {{ return; }}
        if (url.origin !== location.origin) return;

        /* 1. link inside a card → swap that card only */
        var cardId = cardContaining(link);
        if (cardId) {{
          e.preventDefault();
          ajaxGo(href, cardId);
          return;
        }}

        /* 2. nav link → swap main + nav */
        if (link.closest('#page-nav')) {{
          e.preventDefault();
          ajaxGo(href, null);
          return;
        }}

        /* 3. everything else → let overlay script handle (normal nav with loader) */
      }});

      /* ── back / forward ── */
      window.addEventListener('popstate', function () {{
        showNickyLoader();
        _raw(window.location.href)
          .then(function (r) {{ return r.text(); }})
          .then(function (html) {{
            var doc = new DOMParser().parseFromString(html, 'text/html');
            swapMain(doc);
          }})
          .finally(function () {{ hideNickyLoader(); }});
      }});

      function initAll(root) {{
        initForms(root || document);
      }}

      initAll(document);
    }})();
  </script>
  <script>
    (function () {{
      const DATETIME_RE = /^\\d{{4}}-\\d{{2}}-\\d{{2}}[ T]\\d{{2}}:\\d{{2}}:\\d{{2}}/;
      function relativeTime(date) {{
        const diff = Math.round((Date.now() - date.getTime()) / 1000);
        if (diff < 60) return diff <= 1 ? "just now" : diff + "s ago";
        if (diff < 3600) return Math.floor(diff / 60) + "m ago";
        if (diff < 86400) return Math.floor(diff / 3600) + "h ago";
        if (diff < 86400 * 7) return Math.floor(diff / 86400) + "d ago";
        return date.toLocaleDateString(undefined, {{ month: "short", day: "numeric", year: date.getFullYear() !== new Date().getFullYear() ? "numeric" : undefined }});
      }}
      document.querySelectorAll("td, dd").forEach(function (el) {{
        const text = el.textContent.trim();
        if (!DATETIME_RE.test(text)) return;
        const date = new Date(text.replace(" ", "T") + (text.includes("+") || text.endsWith("Z") ? "" : "Z"));
        if (isNaN(date.getTime())) return;
        const time = document.createElement("time");
        time.dateTime = date.toISOString();
        time.title = text;
        time.textContent = relativeTime(date);
        time.className = "cursor-default";
        el.replaceChildren(time);
      }});
    }})();
  </script>
</body>
</html>"""


def user_profile_label(user: admin_auth.AdminUser, settings: Settings) -> str:
    if admin_auth.is_admin(user, settings):
        return t("PROFILE.ADMIN")
    if admin_auth.is_support(user):
        return t("PROFILE.SUPPORT")
    return t("PROFILE.USER")


def user_easter_egg(user: admin_auth.AdminUser, settings: Settings) -> str:
    if admin_auth.is_admin(user, settings):
        return (
            '<span title="👑 Admin" class="cursor-default select-none text-base leading-none" '
            'style="filter:grayscale(0.2)">👑</span>'
        )
    if admin_auth.is_support(user):
        return (
            '<span title="🛠️ Support" class="cursor-default select-none text-base leading-none" '
            'style="filter:grayscale(0.1)">🛠️</span>'
        )
    return ""


_NAV_ITEMS: list[tuple[str, str, str]] = [
    ("NAV.DASHBOARD", "/admin-ui",        "ph-squares-four"),
    ("NAV.TENANTS",   "/admin-ui/tenants","ph-users"),
    ("NAV.ORDERS",    "/admin-ui/orders", "ph-ticket"),
]


def nav_link(label: str, href: str, current_path: str, icon: str = "") -> str:
    base = "inline-flex h-10 items-center gap-2 rounded-xl px-4 text-sm font-semibold transition"
    active = "bg-black text-white" if href == current_path else "text-slate-950 hover:bg-slate-100"
    icon_html = f'<i class="ph {e(icon)}"></i>' if icon else ""
    return f'<a class="{base} {active}" href="{href}">{icon_html}{e(label)}</a>'


def nav_dropdown_link(label: str, href: str, current_path: str, icon: str = "") -> str:
    active = "font-semibold text-slate-950 bg-slate-50" if href == current_path else "text-slate-700"
    icon_html = f'<i class="ph {e(icon)} text-base text-slate-400"></i>' if icon else ""
    return f'<a class="flex items-center gap-3 px-4 py-2.5 text-sm {active} hover:bg-slate-50" href="{href}">{icon_html}{e(label)}</a>'


def lang_switcher(current_path: str) -> str:
    locale = current_locale()
    name = LOCALE_NAMES.get(locale, locale)
    items = "".join(
        f'<a class="flex items-center gap-2.5 px-4 py-2 text-sm text-slate-700 hover:bg-slate-50{"font-semibold" if lc == locale else ""}" '
        f'href="/admin-ui/set-language?lang={e(lc)}&next={e(current_path)}">'
        f'<img src="/admin-ui/assets/flags/{e(lc)}.svg" class="h-4 w-6 rounded-sm object-cover" alt="{e(lc)}">'
        f'<span>{e(LOCALE_NAMES.get(lc, lc))}</span></a>'
        for lc in SUPPORTED_LOCALES
    )
    return f"""
    <div class="relative" id="lang-switcher">
      <button type="button" onclick="document.getElementById('lang-menu').classList.toggle('hidden')"
        class="inline-flex h-9 items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 text-sm font-medium text-slate-700 hover:bg-slate-50">
        <img src="/admin-ui/assets/flags/{e(locale)}.svg" class="h-4 w-6 rounded-sm object-cover" alt="{e(locale)}">
        <span>{e(name)}</span><i class="ph ph-caret-down text-xs"></i>
      </button>
      <div id="lang-menu" class="absolute right-0 z-50 mt-1 hidden min-w-[160px] rounded-xl border border-slate-100 bg-white py-1 shadow-nicky">
        {items}
      </div>
    </div>
    <script>
      document.addEventListener('click', function(e) {{
        var sw = document.getElementById('lang-switcher');
        if (sw && !sw.contains(e.target)) document.getElementById('lang-menu').classList.add('hidden');
      }});
    </script>
    """


def scoped_tenant_scope(
    user: admin_auth.AdminUser, settings: Settings, db: Database
) -> TenantScope:
    if admin_auth.is_privileged(user, settings):
        return TenantScope(
            tenant_filters={}, order_tenant_id=None, scoped=False, allowed_tenant_ids=None
        )
    owner_uuid = admin_auth.nicky_user_uuid_claim(user)
    if owner_uuid:
        return TenantScope(
            tenant_filters={"nicky_user_uuid": owner_uuid},
            order_tenant_id=owner_uuid,
            scoped=True,
            allowed_tenant_ids=frozenset([owner_uuid]),
        )
    # Fetch ALL tenants owned by this user (not just the first one) so multi-tenant
    # users can filter by any of their tenants.
    user_tenants = db.list_tenants(owner_auth_subject=user.subject)
    allowed = frozenset(t.tenant_id for t in user_tenants)
    first_tenant_id = user_tenants[0].tenant_id if user_tenants else NO_TENANT_SCOPE
    return TenantScope(
        tenant_filters={"owner_auth_subject": user.subject},
        order_tenant_id=first_tenant_id,
        scoped=True,
        allowed_tenant_ids=allowed,
    )


def scope_shows_tenant_filter(scope: TenantScope) -> bool:
    """Return True when the tenant dropdown should be shown in filter forms."""
    return True


def can_write_tenants(user: admin_auth.AdminUser, settings: Settings) -> bool:
    return not (admin_auth.is_support(user) and not admin_auth.is_admin(user, settings))


def require_tenant_visible(
    user: admin_auth.AdminUser, settings: Settings, tenant: TenantConfig
) -> None:
    if admin_auth.is_privileged(user, settings):
        return
    owner_uuid = admin_auth.nicky_user_uuid_claim(user)
    if owner_uuid and tenant.nicky_user_uuid != owner_uuid:
        raise HTTPException(status_code=403, detail="Tenant outside user scope")
    if not owner_uuid and tenant.owner_auth_subject != user.subject:
        raise HTTPException(status_code=403, detail="Tenant outside user scope")


def generate_tenant_id(db: Database) -> str:
    for _ in range(10):
        tenant_id = normalize_tenant_id(f"tenant-{secrets.token_hex(8)}")
        if not db.get_tenant(tenant_id):
            return tenant_id
    raise HTTPException(status_code=500, detail="Could not generate tenant id")


def ensure_unique_active_api_keys(
    db: Database,
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


def scoped_order_filters(
    filters: dict[str, str | None], scope: TenantScope
) -> dict[str, str | None]:
    if scope.allowed_tenant_ids is None:
        # Privileged user — no tenant restriction.
        return filters
    filters = dict(filters)
    requested = filters.get("tenant_id")
    if requested and requested in scope.allowed_tenant_ids:
        # User explicitly selected one of their own tenants — allow it.
        filters["tenant_id"] = requested
    else:
        # Default to the first/only tenant in their scope.
        filters["tenant_id"] = scope.order_tenant_id
    return filters


def scoped_webhook_filters(
    filters: dict[str, str | None], scope: TenantScope
) -> dict[str, str | None]:
    if scope.allowed_tenant_ids is None:
        return filters
    filters = dict(filters)
    requested = filters.get("tenant_id")
    if requested and requested in scope.allowed_tenant_ids:
        filters["tenant_id"] = requested
    else:
        filters["tenant_id"] = scope.order_tenant_id
    return filters


def new_tenant_link(user: admin_auth.AdminUser, tenants: list[TenantConfig], settings: Settings) -> str:
    if not can_write_tenants(user, settings):
        return ""
    return f'<a class="inline-flex h-10 shrink-0 items-center justify-center gap-2 rounded-lg bg-black px-4 text-sm font-semibold text-white hover:bg-zinc-800" href="/admin-ui/tenants/new"><i class="ph ph-plus text-sm"></i>{t("TENANTS.BUTTON_NEW")}</a>'


def setup_modal(user: admin_auth.AdminUser, tenants: list[TenantConfig], settings: Settings) -> str:
    """Modal shown to regular users who have no active tenant configured."""
    if admin_auth.is_privileged(user, settings):
        return ""
    if any(tenant.active for tenant in tenants):
        return ""
    return f"""
    <div id="setup-modal-backdrop"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onclick="if(event.target===this)document.getElementById('setup-modal-backdrop').classList.add('hidden')">
      <div class="relative w-full max-w-md rounded-2xl bg-white p-8 shadow-2xl">
        <button type="button"
          onclick="document.getElementById('setup-modal-backdrop').classList.add('hidden')"
          class="absolute right-4 top-4 inline-flex h-8 w-8 items-center justify-center rounded-full text-slate-400 hover:bg-slate-100 hover:text-slate-700">
          <i class="ph ph-x text-lg"></i>
        </button>
        <div class="mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-[#deff96]">
          <i class="ph ph-plug text-3xl text-slate-900"></i>
        </div>
        <h2 class="text-xl font-bold text-slate-950">{t("DASHBOARD.SETUP_MODAL_TITLE")}</h2>
        <p class="mt-2 text-sm leading-relaxed text-slate-500">{t("DASHBOARD.SETUP_MODAL_BODY")}</p>
        <div class="mt-6 flex flex-col gap-3 sm:flex-row">
          <a href="/admin-ui/tenants/new"
            class="inline-flex h-10 flex-1 items-center justify-center gap-2 rounded-lg bg-black px-5 text-sm font-semibold text-white hover:bg-zinc-800">
            <i class="ph ph-plug text-sm"></i>
            {t("DASHBOARD.SETUP_MODAL_BUTTON")}
          </a>
          <button type="button"
            onclick="document.getElementById('setup-modal-backdrop').classList.add('hidden')"
            class="inline-flex h-10 items-center justify-center rounded-lg border border-slate-200 px-5 text-sm font-semibold text-slate-700 hover:bg-slate-50">
            {t("DASHBOARD.SETUP_MODAL_DISMISS")}
          </button>
        </div>
      </div>
    </div>
    """


def no_tenants_cta(user: admin_auth.AdminUser, tenants: list[TenantConfig], settings: Settings) -> str:
    if tenants or not can_write_tenants(user, settings):
        return ""
    return f"""
    <div class="mb-7 overflow-hidden rounded-xl border border-dashed border-slate-200 bg-white px-6 py-10 text-center shadow-nicky">
      <div class="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-slate-100">
        <i class="ph ph-plug text-2xl text-slate-400"></i>
      </div>
      <h3 class="text-base font-semibold text-slate-900">{t("DASHBOARD.NO_TENANTS_CTA_TITLE")}</h3>
      <p class="mt-1 text-sm text-slate-500">{t("DASHBOARD.NO_TENANTS_CTA_HINT")}</p>
      <a href="/admin-ui/tenants/new" class="mt-5 inline-flex h-10 items-center justify-center gap-2 rounded-lg bg-black px-5 text-sm font-semibold text-white hover:bg-zinc-800">
        <i class="ph ph-plus text-sm"></i>
        {t("DASHBOARD.NO_TENANTS_CTA_BUTTON")}
      </a>
    </div>
    """


def summary_grid(
    tenants: list[TenantConfig], orders_total: int, webhooks_total: int
) -> str:
    active_tenants = sum(1 for tenant in tenants if tenant.active)
    return f"""
    <section class="mb-7 grid min-w-0 grid-cols-1 gap-3 sm:grid-cols-3">
      {metric(t("DASHBOARD.METRIC_TENANTS"), str(len(tenants)), t("DASHBOARD.METRIC_ACTIVE", count=str(active_tenants)), "ph-users")}
      {metric(t("DASHBOARD.METRIC_TOTAL_ORDERS"), str(orders_total), t("DASHBOARD.METRIC_ALL_TIME"), "ph-ticket")}
      {metric(t("DASHBOARD.METRIC_TOTAL_WEBHOOKS"), str(webhooks_total), t("DASHBOARD.METRIC_ALL_TIME"), "ph-webhooks")}
    </section>
    """


def metric(label: str, value: str, hint: str, icon: str = "ph-chart-bar") -> str:
    return f"""
    <div class="relative min-w-0 overflow-hidden rounded-xl border border-slate-100 bg-white p-4 shadow-nicky before:absolute before:inset-x-0 before:top-0 before:h-1 before:bg-[#deff96]">
      <div class="mb-2 flex items-center gap-2">
        <i class="ph {e(icon)} text-lg text-slate-400"></i>
        <span class="text-xs font-semibold text-slate-500">{e(label)}</span>
      </div>
      <strong class="block text-2xl font-bold leading-8 text-slate-950">{e(value)}</strong>
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
    mobile_cards = "".join(tenant_mobile_card(tenant, user=user, settings=settings) for tenant in tenants)
    empty_state = f'<div class="px-4 py-10 text-center"><img src="/admin-ui/assets/no-contacts.svg" alt="" class="mx-auto mb-3 h-16 w-16 opacity-80"><p class="text-sm font-semibold text-slate-700">{t("TENANTS.NO_TENANTS")}</p><p class="mt-1 text-xs text-slate-400">{t("TENANTS.NO_TENANTS_HINT")}</p></div>'
    if not rows:
        rows = f'<tr><td colspan="9" class="px-4 py-10 text-center"><img src="/admin-ui/assets/no-contacts.svg" alt="" class="mx-auto mb-3 h-16 w-16 opacity-80"><p class="text-sm font-semibold text-slate-700">{t("TENANTS.NO_TENANTS")}</p><p class="mt-1 text-xs text-slate-400">{t("TENANTS.NO_TENANTS_HINT")}</p></td></tr>'
        mobile_cards = empty_state
    wrapper = (
        'class="min-w-0 overflow-x-auto rounded-xl border border-slate-100 bg-white shadow-nicky"'
        if framed
        else 'class="min-w-0"'
    )
    return f"""
    <div {wrapper}>
      <div class="hidden md:block overflow-x-auto">
        <table class="w-full min-w-[900px] border-separate border-spacing-0 text-left">
          <thead>
            <tr class="bg-gray-100 text-xs font-semibold text-gray-600">
              <th class="border-b border-slate-100 px-4 py-3">{t("COMMON.TENANT")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("TENANTS.TABLE_NICKY_EMAIL")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("TENANTS.FILTER_ACTIVE")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("TENANTS.TABLE_TICKET_TAILOR")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("TENANTS.TABLE_NICKY")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("TENANTS.TABLE_ASSET")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("TENANTS.TABLE_CREATED")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("TENANTS.TABLE_UPDATED")}</th>
              <th class="border-b border-slate-100 px-4 py-3"></th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <div class="divide-y divide-slate-100 md:hidden">{mobile_cards}</div>
    </div>
    """


def tenant_mobile_card(tenant: TenantConfig, *, user: admin_auth.AdminUser, settings: Settings) -> str:
    safe = tenant_to_safe_dict(tenant)
    actions = ""
    if can_write_tenants(user, settings):
        actions = f'<a class="shrink-0 inline-flex h-9 items-center gap-2 rounded-lg bg-black px-3 text-sm font-semibold text-white hover:bg-zinc-800" href="/admin-ui/tenants/{u(tenant.tenant_id)}/edit"><i class="ph ph-pencil text-sm"></i>{t("COMMON.EDIT")}</a>'
    tt_badge = badge(t("COMMON.CONFIGURED") if safe["ticket_tailor_configured"] else t("COMMON.MISSING"), safe["ticket_tailor_configured"])
    nicky_badge = badge(t("COMMON.CONFIGURED") if safe["nicky_configured"] else t("COMMON.MISSING"), safe["nicky_configured"])
    active_badge = badge(t("COMMON.ACTIVE") if tenant.active else t("COMMON.INACTIVE"), tenant.active)
    return f"""
    <div class="flex items-start justify-between gap-3 px-4 py-4">
      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-center gap-2">
          <strong class="font-semibold text-slate-950">{e(tenant.name or tenant.tenant_id)}</strong>
          {active_badge}
        </div>
        <p class="mt-0.5 text-xs text-slate-400">{e(compact_identifier(tenant.tenant_id))}</p>
        <p class="mt-1.5 text-sm text-slate-600">{e(tenant.nicky_user_email or "-")}</p>
        <div class="mt-2 flex flex-wrap gap-2">
          <span class="inline-flex items-center gap-1 text-xs text-slate-400">{t("TENANTS.TABLE_TICKET_TAILOR")}: {tt_badge}</span>
          <span class="inline-flex items-center gap-1 text-xs text-slate-400">{t("TENANTS.TABLE_NICKY")}: {nicky_badge}</span>
        </div>
      </div>
      {actions}
    </div>
    """


def tenant_row(tenant: TenantConfig, *, user: admin_auth.AdminUser, settings: Settings) -> str:
    safe = tenant_to_safe_dict(tenant)
    actions = ""
    if can_write_tenants(user, settings):
        actions = f'<a class="inline-flex h-9 items-center gap-2 rounded-lg bg-black px-4 text-sm font-semibold text-white hover:bg-zinc-800" href="/admin-ui/tenants/{u(tenant.tenant_id)}/edit"><i class="ph ph-pencil text-sm"></i>{t("COMMON.EDIT")}</a>'
    return f"""
    <tr class="bg-white transition-colors duration-150 even:bg-[#f8f8f9] hover:bg-gray-50/80">
      <td class="border-b border-slate-100 px-4 py-3 align-top"><strong class="font-semibold text-slate-950">{e(tenant.name or tenant.tenant_id)}</strong><br><small class="text-sm text-slate-400">{e(compact_identifier(tenant.tenant_id))}</small></td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{e(tenant.nicky_user_email or "-")}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{badge(t("COMMON.ACTIVE") if tenant.active else t("COMMON.INACTIVE"), tenant.active)}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{badge(t("COMMON.CONFIGURED") if safe["ticket_tailor_configured"] else t("COMMON.MISSING"), safe["ticket_tailor_configured"])}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{badge(t("COMMON.CONFIGURED") if safe["nicky_configured"] else t("COMMON.MISSING"), safe["nicky_configured"])}</td>
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
    warn: str | None = None,
    replace_notice: TenantConfig | None = None,
) -> str:
    tt_webhook = external_api_url(settings, f"/webhooks/ticket-tailor/{tenant.tenant_id}")
    is_inactive = not is_new and not tenant.active
    notice = ""
    if saved and warn == "webhook_failed":
        notice = f'<p class="notice-warn mb-5 flex items-center gap-2 rounded-lg border px-4 py-3 text-sm font-medium"><i class="ph ph-warning"></i> {t("TENANTS.NOTICE_WEBHOOK_FAILED")}</p>'
    elif saved:
        notice = f'<p class="notice-ok mb-5 flex items-center gap-2 rounded-lg border px-4 py-3 text-sm font-medium"><i class="ph ph-check-circle"></i> {t("TENANTS.NOTICE_SAVED")}</p>'
    elif replace_notice:
        notice = f'<p class="notice-warn mb-5 flex items-center gap-2 rounded-lg border px-4 py-3 text-sm font-medium"><i class="ph ph-info"></i> {t("TENANTS.REPLACE_NOTICE", name=e(replace_notice.name), tenant_id=e(replace_notice.tenant_id))}</p>'
    elif message:
        notice = f'<p class="notice-ok mb-5 flex items-center gap-2 rounded-lg border px-4 py-3 text-sm font-medium"><i class="ph ph-check-circle"></i> {e(message.replace("_", " ").capitalize())}.</p>'
    delete_action = ""
    if not is_new and not is_inactive and can_write_tenants(user, settings):
        delete_action = f"""
        <form method="post" action="/admin-ui/tenants/{u(tenant.tenant_id)}/delete">
          <button type="submit" class="inline-flex h-10 items-center justify-center rounded-lg border border-rose-200 bg-white px-4 text-sm font-semibold text-rose-700 hover:bg-rose-50">{t("TENANTS.FORM_BUTTON_DEACTIVATE")}</button>
        </form>
        """
    hidden_tenant_id = (
        f'<input type="hidden" name="tenant_id" value="{e(tenant.tenant_id)}">'
        if not is_new
        else ""
    )
    asset_option = (
        f'<option value="{e(tenant.nicky_default_blockchain_asset_id)}" selected>{e(tenant.nicky_default_blockchain_asset_id)}</option>'
        if tenant.nicky_default_blockchain_asset_id
        else f'<option value="">{t("TENANTS.FORM_PLACEHOLDER_ASSET")}</option>'
    )
    is_admin_user = admin_auth.is_admin(user, settings)
    auth0_identifier = admin_auth.user_identifier(user)
    nicky_email_value = (
        ""
        if is_new and is_admin_user
        else auth0_identifier
        if is_new
        else tenant.nicky_user_email or ("" if is_admin_user else auth0_identifier)
    )
    nicky_identity_readonly = "" if is_admin_user else " readonly"
    nicky_identity_class = (
        "mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black"
        if is_admin_user
        else "mt-2 h-11 w-full rounded-lg border border-slate-100 bg-slate-50 px-3 text-sm text-slate-500"
    )
    nicky_api_placeholder = t("TENANTS.FORM_PLACEHOLDER_NICKY_KEY_NEW") if is_new else t("TENANTS.FORM_PLACEHOLDER_NICKY_KEY_EDIT")
    ticket_tailor_placeholder = t("TENANTS.FORM_PLACEHOLDER_TT_KEY_NEW") if is_new else t("TENANTS.FORM_PLACEHOLDER_TT_KEY_EDIT")
    webhook_block = ""
    tenant_uuid_block = ""
    if not is_new:
        tenant_uuid_block = f"""
          {text_input(t("TENANTS.FORM_LABEL_UUID"), "", value=tenant.tenant_id, disabled=True)}
        """
        webhook_block = f"""
        <div class="mt-5 border-t border-slate-100 pt-5">
          <h3 class="text-sm font-semibold text-slate-950">{t("TENANTS.FORM_WEBHOOK_TITLE")}</h3>
          <p class="mt-1 text-sm leading-6 text-slate-500">{t("TENANTS.FORM_WEBHOOK_HINT")}</p>
          <div class="mt-3 flex min-w-0 flex-col gap-3 sm:flex-row">
            <input id="ticket-tailor-webhook-url" class="h-11 min-w-0 flex-1 rounded-lg border border-slate-200 bg-slate-50 px-3 text-sm font-semibold text-slate-700 shadow-sm outline-none" type="text" value="{e(tt_webhook)}" readonly>
            <button id="copy-ticket-tailor-webhook-url" class="inline-flex h-11 shrink-0 items-center justify-center rounded-lg border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-950 hover:bg-slate-50" type="button">{t("COMMON.COPY")}</button>
          </div>
          <p id="ticket-tailor-webhook-copy-status" class="mt-2 min-h-5 text-sm text-slate-500"></p>
        </div>
        """
    def _locked_api_key_field(label: str, configured: bool) -> str:
        masked = "•" * 24 if configured else ""
        return f"""
          <div>
            <label class="block text-sm font-semibold text-slate-950">{label}</label>
            <div class="mt-2 flex min-w-0 overflow-hidden rounded-lg border border-slate-100 bg-slate-50 shadow-sm">
              <input class="h-11 min-w-0 flex-1 border-0 bg-transparent px-3 text-sm tracking-widest text-slate-400" type="text" value="{masked}" disabled>
              <span class="inline-flex h-11 w-11 shrink-0 items-center justify-center border-l border-slate-100 text-slate-400"><i class="ph ph-lock"></i></span>
            </div>
            <p class="mt-2 min-h-5 text-sm text-slate-400">{t("TENANTS.FORM_API_KEY_LOCKED")}</p>
          </div>
        """

    nicky_api_field = (
        _locked_api_key_field(t("TENANTS.FORM_LABEL_API_KEY"), bool(tenant.nicky_api_key))
        if not is_new
        else f"""
          <div>
            <label for="nicky-api-key" class="block text-sm font-semibold text-slate-950">
              {t("TENANTS.FORM_LABEL_API_KEY")}
            </label>
            <div class="mt-2 flex min-w-0 flex-col gap-3 sm:flex-row">
              <div class="flex min-w-0 flex-1 overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm focus-within:ring-2 focus-within:ring-black">
                <input id="nicky-api-key" class="h-11 min-w-0 flex-1 border-0 bg-transparent px-3 text-sm text-slate-700 outline-none" name="nicky_api_key" type="password" placeholder="{e(nicky_api_placeholder)}" autocomplete="new-password">
                <button class="inline-flex h-11 w-14 shrink-0 items-center justify-center border-l border-slate-100 text-xs font-semibold text-slate-500 hover:bg-slate-50" type="button" data-toggle-secret="nicky-api-key" aria-label="{t("COMMON.SHOW")}" title="{t("COMMON.SHOW")}">{t("COMMON.SHOW")}</button>
              </div>
              <button id="validate-nicky-key" class="inline-flex h-11 shrink-0 items-center justify-center rounded-lg border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-950 hover:bg-slate-50 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-300" type="button">{t("COMMON.VALIDATE")}</button>
            </div>
            <p id="nicky-validation-status" class="mt-2 min-h-5 text-sm text-slate-500"></p>
          </div>
        """
    )

    ticket_tailor_api_field = (
        _locked_api_key_field(t("TENANTS.FORM_LABEL_API_KEY"), bool(tenant.ticket_tailor_api_key))
        if not is_new
        else f"""
          <div>
            <label for="ticket-tailor-api-key" class="block text-sm font-semibold text-slate-950">
              {t("TENANTS.FORM_LABEL_API_KEY")}
            </label>
            <div class="mt-2 flex min-w-0 flex-col gap-3 sm:flex-row">
              <div class="flex min-w-0 flex-1 overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm focus-within:ring-2 focus-within:ring-black">
                <input id="ticket-tailor-api-key" class="h-11 min-w-0 flex-1 border-0 bg-transparent px-3 text-sm text-slate-700 outline-none" name="ticket_tailor_api_key" type="password" placeholder="{e(ticket_tailor_placeholder)}" autocomplete="new-password">
                <button class="inline-flex h-11 w-14 shrink-0 items-center justify-center border-l border-slate-100 text-xs font-semibold text-slate-500 hover:bg-slate-50" type="button" data-toggle-secret="ticket-tailor-api-key" aria-label="{t("COMMON.SHOW")}" title="{t("COMMON.SHOW")}">{t("COMMON.SHOW")}</button>
              </div>
              <button id="validate-ticket-tailor-key" class="inline-flex h-11 shrink-0 items-center justify-center rounded-lg border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-950 hover:bg-slate-50 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-300" type="button">{t("COMMON.VALIDATE")}</button>
            </div>
            <p id="ticket-tailor-validation-status" class="mt-2 min-h-5 text-sm text-slate-500"></p>
          </div>
        """
    )

    save_label = t("TENANTS.FORM_BUTTON_CREATE") if is_new else t("TENANTS.FORM_BUTTON_SAVE")
    page_title = t("TENANTS.FORM_TITLE_NEW") if is_new else f"{t('TENANTS.FORM_TITLE_EDIT')} {e(tenant.name or tenant.tenant_id)}"
    inactive_banner = f'<p class="notice-warn mb-5 flex items-center gap-2 rounded-lg border px-4 py-3 text-sm font-medium"><i class="ph ph-lock text-base"></i>{t("TENANTS.NOTICE_INACTIVE")}</p>' if is_inactive else ""
    form_fieldset_open = '<fieldset disabled class="contents">' if is_inactive else ""
    form_fieldset_close = "</fieldset>" if is_inactive else ""
    return f"""
    <section class="mb-6">
      <h1 class="text-2xl font-semibold leading-8 text-slate-950">{page_title}</h1>
    </section>
    {inactive_banner}
    {notice}
    <form id="tenant-form" method="post" action="/admin-ui/tenants/save" autocomplete="off" class="mx-auto grid max-w-6xl min-w-0 grid-cols-1 gap-4">
      {hidden_tenant_id}
      {form_fieldset_open}

      <!-- Tenant section -->
      <div class="grid grid-cols-1 gap-6 rounded-xl border border-slate-100 bg-white p-6 shadow-nicky md:grid-cols-3">
        <div>
          <h2 class="text-base font-semibold text-slate-950">{t("TENANTS.FORM_SECTION_TENANT")}</h2>
        </div>
        <div class="md:col-span-2 space-y-5">
          {text_input(t("TENANTS.FORM_LABEL_NAME"), "name", value=tenant.name if not is_new else "", placeholder=t("TENANTS.FORM_PLACEHOLDER_NAME"))}
          {tenant_uuid_block}
        </div>
      </div>

      <!-- Nicky section -->
      <div class="grid grid-cols-1 gap-6 rounded-xl border border-slate-100 bg-white p-6 shadow-nicky md:grid-cols-3">
        <div>
          <h2 class="text-base font-semibold text-slate-950">{t("TENANTS.FORM_SECTION_NICKY")}</h2>
          <div class="mt-3">
            <a href="{e(settings.nicky_pay_base_url)}/settings/api-key-management" target="_blank" rel="noopener" class="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-500 hover:text-slate-900">
              <i class="ph ph-key text-sm"></i>{t("TENANTS.FORM_GET_API_KEY")}<i class="ph ph-arrow-square-out text-xs"></i>
            </a>
          </div>
        </div>
        <div class="md:col-span-2 space-y-5">
          {nicky_api_field}
          <div>
            <label class="block text-sm font-semibold text-slate-950">
              {t("TENANTS.FORM_LABEL_NICKY_EMAIL")}
              <input id="nicky-email" class="{nicky_identity_class}" name="nicky_user_email" type="email" value="{e(nicky_email_value)}" placeholder="{t("TENANTS.FORM_PLACEHOLDER_NICKY_EMAIL")}" autocomplete="off"{nicky_identity_readonly}>
            </label>
          </div>
          <div>
            <label class="block text-sm font-semibold text-slate-950">
              {t("TENANTS.FORM_LABEL_ASSET")}
              <select id="nicky-asset" class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="nicky_default_blockchain_asset_id" required autocomplete="off">{asset_option}</select>
            </label>
          </div>
        </div>
      </div>

      <!-- Ticket Tailor section -->
      <div class="grid grid-cols-1 gap-6 rounded-xl border border-slate-100 bg-white p-6 shadow-nicky md:grid-cols-3">
        <div>
          <h2 class="text-base font-semibold text-slate-950">{t("TENANTS.FORM_SECTION_TICKET_TAILOR")}</h2>
          <div class="mt-3">
            <a href="https://app.tickettailor.com/api" target="_blank" rel="noopener" class="inline-flex items-center gap-1.5 text-xs font-semibold text-slate-500 hover:text-slate-900">
              <i class="ph ph-key text-sm"></i>{t("TENANTS.FORM_GET_API_KEY")}<i class="ph ph-arrow-square-out text-xs"></i>
            </a>
          </div>
        </div>
        <div class="md:col-span-2 space-y-5">
          {ticket_tailor_api_field}
          {webhook_block}
        </div>
      </div>
      {form_fieldset_close}
    </form>

    <!-- Form actions — outside the main form to avoid nested-form HTML violation -->
    <div class="mx-auto w-full max-w-6xl flex flex-wrap items-center justify-between gap-3 pt-2">
      <div>{delete_action}</div>
      <div class="flex gap-3">
        <a class="inline-flex h-10 shrink-0 items-center justify-center rounded-lg border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-950 hover:bg-slate-50" href="/admin-ui/tenants">{t("TENANTS.FORM_BUTTON_BACK")}</a>
        {"" if is_inactive else f'<button id="save-tenant-button" form="tenant-form" class="inline-flex h-10 items-center justify-center rounded-lg bg-black px-5 text-sm font-semibold text-white hover:bg-zinc-800 disabled:cursor-not-allowed disabled:bg-slate-300 disabled:text-white" type="submit" disabled>{save_label}</button>'}
      </div>
    </div>
    <script>
      const tenantForm = document.querySelector('form[action="/admin-ui/tenants/save"]');
      const nameInput = document.querySelector('input[name="name"]');
      const saveButton = document.getElementById("save-tenant-button");
      const nickyValidateButton = document.getElementById("validate-nicky-key");
      const ticketTailorValidateButton = document.getElementById("validate-ticket-tailor-key");
      const nickyApiKeyInput = document.getElementById("nicky-api-key");
      const ticketTailorApiKeyInput = document.getElementById("ticket-tailor-api-key");
      const nickyStatusEl = document.getElementById("nicky-validation-status");
      const ticketTailorStatusEl = document.getElementById("ticket-tailor-validation-status");
      const webhookUrlInput = document.getElementById("ticket-tailor-webhook-url");
      const copyWebhookButton = document.getElementById("copy-ticket-tailor-webhook-url");
      const webhookCopyStatusEl = document.getElementById("ticket-tailor-webhook-copy-status");
      const assetSelect = document.getElementById("nicky-asset");
      const nickyEmailInput = document.getElementById("nicky-email");
      const isAdminUser = {str(is_admin_user).lower()};
      const auth0Identifier = {json.dumps(auth0_identifier)};

      const validationState = {{
        nicky: {{ valid: {str(not is_new and bool(tenant.nicky_api_key)).lower()}, key: "" }},
        ticketTailor: {{ valid: {str(not is_new and bool(tenant.ticket_tailor_api_key)).lower()}, key: "" }}
      }};

      function setStatus(element, message, state) {{
        element.textContent = message;
        element.className = "mt-2 min-h-5 text-sm " + (
          state === "ok" ? "text-emerald-700" : state === "error" ? "text-rose-700" : "text-slate-500"
        );
      }}

      function formReady() {{
        const hasName = Boolean(nameInput?.value.trim());
        const hasNickyKey = Boolean(nickyApiKeyInput?.value.trim()) || validationState.nicky.valid;
        const hasTicketTailorKey = Boolean(ticketTailorApiKeyInput?.value.trim()) || validationState.ticketTailor.valid;
        const hasAsset = Boolean(assetSelect?.value);
        const hasNickyIdentity = Boolean(nickyEmailInput?.value.trim());
        return hasName && hasNickyKey && hasTicketTailorKey && hasAsset && hasNickyIdentity && validationState.nicky.valid && validationState.ticketTailor.valid;
      }}

      function refreshSubmitState() {{
        if (saveButton) saveButton.disabled = !formReady();
      }}

      async function parseResponse(response) {{
        const responseText = await response.text();
        try {{
          return responseText ? JSON.parse(responseText) : {{}};
        }} catch (_) {{
          return {{ detail: responseText || "Unexpected non-JSON response" }};
        }}
      }}

      document.querySelectorAll("[data-toggle-secret]").forEach((button) => {{
        button.addEventListener("click", () => {{
          const input = document.getElementById(button.dataset.toggleSecret);
          if (!input) return;
          const showing = input.type === "text";
          input.type = showing ? "password" : "text";
          button.textContent = showing ? "{t("COMMON.SHOW")}" : "{t("COMMON.HIDE")}";
          button.setAttribute("aria-label", showing ? "{t("COMMON.SHOW")}" : "{t("COMMON.HIDE")}");
        }});
      }});

      copyWebhookButton?.addEventListener("click", async () => {{
        const webhookUrl = webhookUrlInput?.value || "";
        if (!webhookUrl) return;
        try {{
          await navigator.clipboard.writeText(webhookUrl);
          setStatus(webhookCopyStatusEl, "{t("COMMON.COPIED")}", "ok");
        }} catch (_) {{
          webhookUrlInput?.select();
          document.execCommand("copy");
          setStatus(webhookCopyStatusEl, "{t("COMMON.COPIED")}", "ok");
        }}
      }});

      nickyApiKeyInput?.addEventListener("input", () => {{
        validationState.nicky.valid = false;
        validationState.nicky.key = "";
        setStatus(nickyStatusEl, "", "idle");
        nickyEmailInput.value = isAdminUser ? "" : auth0Identifier;
        refreshSubmitState();
      }});

      nickyEmailInput?.addEventListener("input", refreshSubmitState);

      ticketTailorApiKeyInput?.addEventListener("input", () => {{
        validationState.ticketTailor.valid = false;
        validationState.ticketTailor.key = "";
        setStatus(ticketTailorStatusEl, "", "idle");
        refreshSubmitState();
      }});

      nameInput?.addEventListener("input", refreshSubmitState);
      assetSelect?.addEventListener("change", refreshSubmitState);

      nickyValidateButton?.addEventListener("click", async () => {{
        const apiKey = nickyApiKeyInput.value.trim();
        if (!apiKey) {{
          validationState.nicky.valid = false;
          setStatus(nickyStatusEl, "{t("TENANTS.VALIDATION_ERROR_NICKY_EMPTY")}", "error");
          refreshSubmitState();
          return;
        }}
        nickyValidateButton.disabled = true;
        setStatus(nickyStatusEl, "{t("TENANTS.VALIDATION_VALIDATING")}", "idle");
        try {{
          const response = await fetch("{e(settings.admin_api_base_path)}/admin/nicky/validate-api-key", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ nicky_api_key: apiKey }})
          }});
          const payload = await parseResponse(response);
          if (!response.ok) throw new Error(payload.detail || "{t("TENANTS.VALIDATION_FAILED")}");
          assetSelect.innerHTML = "";
          for (const asset of payload.assets || []) {{
            const option = document.createElement("option");
            option.value = asset.id;
            option.textContent = asset.name ? `${{asset.name}} (${{asset.id}})` : asset.id;
            assetSelect.appendChild(option);
          }}
          nickyEmailInput.value = payload.nicky_user_email || (isAdminUser ? "" : auth0Identifier);
          validationState.nicky.valid = true;
          validationState.nicky.key = apiKey;
          setStatus(nickyStatusEl, "{t("TENANTS.VALIDATION_NICKY_OK")}", "ok");
        }} catch (error) {{
          validationState.nicky.valid = false;
          setStatus(nickyStatusEl, error.message, "error");
        }} finally {{
          nickyValidateButton.disabled = false;
          refreshSubmitState();
        }}
      }});

      ticketTailorValidateButton?.addEventListener("click", async () => {{
        const apiKey = ticketTailorApiKeyInput.value.trim();
        if (!apiKey) {{
          validationState.ticketTailor.valid = false;
          setStatus(ticketTailorStatusEl, "{t("TENANTS.VALIDATION_ERROR_TT_EMPTY")}", "error");
          refreshSubmitState();
          return;
        }}
        ticketTailorValidateButton.disabled = true;
        setStatus(ticketTailorStatusEl, "{t("TENANTS.VALIDATION_VALIDATING")}", "idle");
        try {{
          const response = await fetch("{e(settings.admin_api_base_path)}/admin/ticket-tailor/validate-api-key", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ ticket_tailor_api_key: apiKey }})
          }});
          const payload = await parseResponse(response);
          if (!response.ok) throw new Error(payload.detail || "{t("TENANTS.VALIDATION_FAILED")}");
          validationState.ticketTailor.valid = true;
          validationState.ticketTailor.key = apiKey;
          setStatus(ticketTailorStatusEl, "{t("TENANTS.VALIDATION_TT_OK")}", "ok");
        }} catch (error) {{
          validationState.ticketTailor.valid = false;
          setStatus(ticketTailorStatusEl, error.message, "error");
        }} finally {{
          ticketTailorValidateButton.disabled = false;
          refreshSubmitState();
        }}
      }});

      tenantForm?.addEventListener("submit", (event) => {{
        refreshSubmitState();
        if (!formReady()) {{
          event.preventDefault();
        }}
      }});
      refreshSubmitState();
    </script>
    """


def nicky_status_badge(status: Any) -> str:
    if not status:
        return "-"
    s = str(status).lower()
    if s in ("finished", "concluído", "completed", "paid"):
        ok: bool | str = True
    elif s in ("expired", "failed", "cancelled", "canceled", "recusado"):
        ok = False
    else:
        ok = "warn"
    return badge(str(status), ok)


def truncated_id(value: Any) -> str:
    if not value:
        return "-"
    text = str(value)
    if len(text) <= 16:
        return e(text)
    short = text[:8] + "…" + text[-4:]
    return f'<abbr class="cursor-help font-mono text-xs" title="{e(text)}">{e(short)}</abbr>'


def orders_table(
    orders: list[dict[str, Any]],
    *,
    user: admin_auth.AdminUser,
    settings: Settings,
    tenants: dict[str, TenantConfig],
    tenant_names: dict[str, str] | None = None,
) -> str:
    rows = "".join(
        order_row(
            order,
            user=user,
            settings=settings,
            tenant=tenants.get(str(order.get("tenant_id") or "")),
            tenant_names=tenant_names,
        )
        for order in orders
    )
    mobile_cards = "".join(
        order_mobile_card(
            order,
            user=user,
            settings=settings,
            tenant=tenants.get(str(order.get("tenant_id") or "")),
            tenant_names=tenant_names,
        )
        for order in orders
    )
    empty_state = f'<div class="px-4 py-10 text-center"><img src="/admin-ui/assets/no-transactions.svg" alt="" class="mx-auto mb-3 h-16 w-16 opacity-80"><p class="text-sm font-semibold text-slate-700">{t("ORDERS.NO_ORDERS")}</p><p class="mt-1 text-xs text-slate-400">{t("ORDERS.NO_ORDERS_HINT")}</p></div>'
    if not rows:
        rows = f'<tr><td colspan="9" class="px-4 py-10 text-center"><img src="/admin-ui/assets/no-transactions.svg" alt="" class="mx-auto mb-3 h-16 w-16 opacity-80"><p class="text-sm font-semibold text-slate-700">{t("ORDERS.NO_ORDERS")}</p><p class="mt-1 text-xs text-slate-400">{t("ORDERS.NO_ORDERS_HINT")}</p></td></tr>'
        mobile_cards = empty_state
    return f"""
    <div class="min-w-0">
      <div class="hidden md:block overflow-x-auto">
        <table class="w-full min-w-[980px] border-separate border-spacing-0 text-left">
          <thead>
            <tr class="bg-gray-100 text-xs font-semibold text-gray-600">
              <th class="border-b border-slate-100 px-4 py-3">{t("ORDERS.TABLE_ORDER")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("ORDERS.TABLE_TENANT")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("ORDERS.TABLE_BUYER")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("ORDERS.TABLE_AMOUNT")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("ORDERS.TABLE_NICKY_PR")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("ORDERS.TABLE_NICKY_STATUS")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("ORDERS.TABLE_TT_STATE")}</th>
              <th class="border-b border-slate-100 px-4 py-3">{t("ORDERS.TABLE_UPDATED")}</th>
              <th class="border-b border-slate-100 px-4 py-3"></th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <div class="divide-y divide-slate-100 md:hidden">{mobile_cards}</div>
    </div>
    """


def order_mobile_card(
    order: dict[str, Any],
    *,
    user: admin_auth.AdminUser,
    settings: Settings,
    tenant: TenantConfig | None,
    tenant_names: dict[str, str] | None = None,
) -> str:
    tenant_id = str(order.get("tenant_id") or "")
    order_id = str(order.get("ticket_tailor_order_id") or "")
    tenant_name = (tenant_names or {}).get(tenant_id)
    tenant_label = tenant_name or compact_identifier(tenant_id)
    dashboard_button = nicky_dashboard_button(order, settings, user=user, tenant=tenant, compact=True)
    return f"""
    <div class="flex items-start justify-between gap-3 px-4 py-4">
      <div class="min-w-0 flex-1">
        <div class="flex flex-wrap items-center gap-2">
          <strong class="font-semibold text-slate-950">{e(order_id)}</strong>
          {ticket_tailor_state_cell(order)}
        </div>
        <p class="mt-0.5 text-sm text-slate-600">{e(tenant_label)} · {e(order.get("buyer_email") or "-")}</p>
        <p class="mt-1 text-sm font-medium text-slate-950">{format_amount(order)}</p>
        <div class="mt-1.5 flex flex-wrap items-center gap-1.5">
          {nicky_status_badge(order.get("nicky_status"))}
          <span class="text-xs text-slate-400">{e(order.get("updated_at") or "")}</span>
        </div>
      </div>
      <div class="flex shrink-0 flex-col items-end gap-2">
        {dashboard_button}
        <a class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-black text-base text-white hover:bg-zinc-800" href="/admin-ui/orders/{u(order_id)}?tenant_id={u(tenant_id)}" title="{e(t("COMMON.OPEN"))}"><i class="ph ph-sidebar-simple" aria-hidden="true"></i><span class="sr-only">{t("COMMON.OPEN")}</span></a>
      </div>
    </div>
    """


def order_row(
    order: dict[str, Any],
    *,
    user: admin_auth.AdminUser,
    settings: Settings,
    tenant: TenantConfig | None,
    tenant_names: dict[str, str] | None = None,
) -> str:
    tenant_id = str(order.get("tenant_id") or "")
    order_id = str(order.get("ticket_tailor_order_id") or "")
    tenant_name = (tenant_names or {}).get(tenant_id)
    tenant_cell = (
        f'<strong class="font-semibold text-slate-950">{e(tenant_name)}</strong><br><small class="text-slate-400">{e(compact_identifier(tenant_id))}</small>'
        if tenant_name else e(compact_identifier(tenant_id))
    )
    dashboard_button = nicky_dashboard_button(order, settings, user=user, tenant=tenant, compact=True)
    return f"""
    <tr class="bg-white transition-colors duration-150 even:bg-[#f8f8f9] hover:bg-gray-50/80">
      <td class="border-b border-slate-100 px-4 py-3 align-top"><strong class="font-semibold text-slate-950">{e(order_id)}</strong></td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{tenant_cell}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{e(order.get("buyer_email") or "-")}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{format_amount(order)}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{truncated_id(order.get("nicky_payment_request_id"))}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{nicky_status_badge(order.get("nicky_status"))}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{ticket_tailor_state_cell(order)}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top">{e(order.get("updated_at") or "-")}</td>
      <td class="border-b border-slate-100 px-4 py-3 align-top text-right">
        <div class="flex items-center justify-end gap-2 whitespace-nowrap">
          {dashboard_button}
          <a class="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-black text-base text-white hover:bg-zinc-800" href="/admin-ui/orders/{u(order_id)}?tenant_id={u(tenant_id)}" title="{e(t("COMMON.OPEN"))}"><i class="ph ph-sidebar-simple" aria-hidden="true"></i><span class="sr-only">{t("COMMON.OPEN")}</span></a>
        </div>
      </td>
    </tr>
    """


def webhook_table(webhooks: list[dict[str, Any]], tenant_names: dict[str, str] | None = None) -> str:
    def _tenant_label(wh: dict[str, Any]) -> str:
        tid = str(wh.get("tenant_id") or "")
        name = (tenant_names or {}).get(tid)
        return name or compact_identifier(tid)

    def _tenant_cell(wh: dict[str, Any]) -> str:
        tid = str(wh.get("tenant_id") or "")
        name = (tenant_names or {}).get(tid)
        if name:
            return f'<strong class="font-semibold text-slate-950">{e(name)}</strong><br><small class="text-slate-400">{e(compact_identifier(tid))}</small>'
        return e(compact_identifier(tid))

    rows = "".join(
        f"""
        <tr class="bg-white transition-colors duration-150 even:bg-[#f8f8f9] hover:bg-gray-50/80">
          <td class="border-b border-slate-100 px-4 py-3 align-top">{e(webhook.get("received_at") or "")}</td>
          <td class="border-b border-slate-100 px-4 py-3 align-top">{_tenant_cell(webhook)}</td>
          <td class="border-b border-slate-100 px-4 py-3 align-top">{e(webhook.get("source") or "")}</td>
          <td class="border-b border-slate-100 px-4 py-3 align-top">{e(webhook.get("event_type") or "")}</td>
          <td class="border-b border-slate-100 px-4 py-3 align-top">{badge(str(webhook.get("status") or ""), webhook.get("status") != "failed")}</td>
        </tr>
        """
        for webhook in webhooks
    )
    mobile_cards = "".join(
        f"""
        <div class="px-4 py-4">
          <div class="flex flex-wrap items-center gap-2">
            <span class="font-semibold text-slate-950">{e(webhook.get("event_type") or "")}</span>
            {badge(str(webhook.get("status") or ""), webhook.get("status") != "failed")}
          </div>
          <p class="mt-0.5 text-xs text-slate-400">{e(webhook.get("received_at") or "")}</p>
          <p class="mt-1 text-sm text-slate-600">{e(webhook.get("source") or "")} · {e(_tenant_label(webhook))}</p>
        </div>
        """
        for webhook in webhooks
    )
    empty_state = f'<div class="px-4 py-10 text-center"><i class="ph ph-webhooks mb-3 block text-4xl text-slate-300"></i><p class="text-sm font-semibold text-slate-700">{t("WEBHOOKS.NO_WEBHOOKS")}</p><p class="mt-1 text-xs text-slate-400">{t("WEBHOOKS.NO_WEBHOOKS_HINT")}</p></div>'
    if not rows:
        rows = f'<tr><td colspan="5" class="px-4 py-10 text-center"><i class="ph ph-webhooks mb-3 block text-4xl text-slate-300"></i><p class="text-sm font-semibold text-slate-700">{t("WEBHOOKS.NO_WEBHOOKS")}</p><p class="mt-1 text-xs text-slate-400">{t("WEBHOOKS.NO_WEBHOOKS_HINT")}</p></td></tr>'
        mobile_cards = empty_state
    return f"""
    <div class="min-w-0">
      <div class="hidden md:block overflow-x-auto">
        <table class="w-full min-w-[760px] border-separate border-spacing-0 text-left">
          <thead><tr class="bg-gray-100 text-xs font-semibold text-gray-600"><th class="border-b border-slate-100 px-4 py-3">{t("WEBHOOKS.TABLE_RECEIVED")}</th><th class="border-b border-slate-100 px-4 py-3">{t("WEBHOOKS.TABLE_TENANT")}</th><th class="border-b border-slate-100 px-4 py-3">{t("WEBHOOKS.TABLE_SOURCE")}</th><th class="border-b border-slate-100 px-4 py-3">{t("WEBHOOKS.TABLE_TYPE")}</th><th class="border-b border-slate-100 px-4 py-3">{t("WEBHOOKS.TABLE_STATUS")}</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <div class="divide-y divide-slate-100 md:hidden">{mobile_cards}</div>
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
        <tr class="bg-white transition-colors duration-150 even:bg-[#f8f8f9] hover:bg-gray-50/80">
          <td class="border-b border-slate-100 px-4 py-3 align-top">{e(log.get("created_at") or "")}</td>
          <td class="border-b border-slate-100 px-4 py-3 align-top">{e(log.get("event_type") or "")}</td>
          <td class="border-b border-slate-100 px-4 py-3 align-top">{e(log.get("message") or "")}<details class="mt-2"><summary class="cursor-pointer text-sm font-semibold text-slate-950">{t("COMMON.PAYLOAD")}</summary><pre class="mt-2 overflow-x-auto rounded-lg bg-zinc-950 p-4 text-xs text-slate-100">{e(payload)}</pre></details></td>
        </tr>
        """
    if not rows:
        rows = f'<tr><td colspan="3" class="px-4 py-10 text-center"><p class="text-sm font-medium text-slate-500">{t("LOGS.NO_LOGS")}</p></td></tr>'
    wrapper = (
        'class="min-w-0 overflow-hidden rounded-xl border border-slate-100 bg-white shadow-nicky"'
        if framed
        else 'class="min-w-0 overflow-hidden"'
    )
    return f"""
    <div {wrapper}>
      <div class="min-w-0 overflow-x-auto">
      <table class="w-full min-w-[760px] border-separate border-spacing-0 text-left">
        <thead><tr class="bg-gray-100 text-xs font-semibold text-gray-600"><th class="border-b border-slate-100 px-4 py-3">{t("LOGS.TABLE_CREATED")}</th><th class="border-b border-slate-100 px-4 py-3">{t("LOGS.TABLE_EVENT")}</th><th class="border-b border-slate-100 px-4 py-3">{t("LOGS.TABLE_MESSAGE")}</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      </div>
    </div>
    """


def order_mapping_panel(order: dict[str, Any]) -> str:
    rows = [
        (t("ORDERS.DETAIL_MAPPING_TT_ORDER"), order.get("ticket_tailor_order_id")),
        (t("ORDERS.DETAIL_MAPPING_NICKY_PR"), order.get("nicky_payment_request_id")),
        (t("ORDERS.DETAIL_MAPPING_NICKY_BILL"), order.get("nicky_bill_short_id")),
        (t("ORDERS.DETAIL_MAPPING_NICKY_RECEIVER"), order.get("nicky_receiver_short_id")),
        (t("ORDERS.DETAIL_MAPPING_NICKY_URL"), order.get("nicky_payment_url")),
        (t("ORDERS.DETAIL_MAPPING_NICKY_STATUS"), order.get("nicky_status")),
        (t("ORDERS.DETAIL_MAPPING_TT_CONFIRMED"), order.get("ticket_tailor_confirmed_at")),
        (t("ORDERS.DETAIL_MAPPING_TT_VOIDED"), order.get("ticket_tailor_tickets_voided_at")),
        (t("ORDERS.DETAIL_MAPPING_VOID_REASON"), order.get("ticket_tailor_void_reason")),
    ]
    items = "".join(
        f'<dt class="text-slate-500">{e(label)}</dt><dd class="min-w-0 break-words text-slate-950">{link_or_text(value)}</dd>' for label, value in rows
    )
    notice = ticket_tailor_state_notice(order)
    return f'<section class="mb-7 rounded-xl border border-slate-100 bg-white p-4 shadow-nicky"><h2 class="mb-4 text-lg font-semibold text-slate-950">{t("ORDERS.DETAIL_SECTION_MAPPING")}</h2>{notice}<dl class="grid grid-cols-1 gap-2 text-sm md:grid-cols-[220px_minmax(0,1fr)]">{items}</dl></section>'


def order_actions(order: dict[str, Any], *, user: admin_auth.AdminUser, settings: Settings) -> str:
    if not admin_auth.is_admin(user, settings):
        return ""
    tenant_id = str(order.get("tenant_id") or "")
    order_id = str(order.get("ticket_tailor_order_id") or "")
    nicky_status = str(order.get("nicky_status") or "").strip().lower()
    has_nicky_payment = bool(order.get("nicky_payment_request_id") or order.get("nicky_bill_short_id"))
    is_paid_status = nicky_status in {"finished", "concluído"}
    is_ticket_tailor_closed = bool(
        order.get("ticket_tailor_confirmed_at") or order.get("ticket_tailor_tickets_voided_at")
    )
    actions: list[str] = []
    if not has_nicky_payment:
        actions.append(
            f"""
            <form method="post" action="/admin-ui/orders/{u(order_id)}/create-nicky-payment-request">
              <input type="hidden" name="tenant_id" value="{e(tenant_id)}">
              <button type="submit" class="inline-flex h-10 items-center justify-center rounded-lg border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-950 hover:bg-slate-50">{t("ORDERS.DETAIL_ACTION_CREATE_PR")}</button>
            </form>
            """
        )
    if is_paid_status and not is_ticket_tailor_closed:
        actions.append(
            f"""
            <form method="post" action="/admin-ui/orders/{u(order_id)}/confirm-ticket-tailor-payment">
              <input type="hidden" name="tenant_id" value="{e(tenant_id)}">
              <button type="submit" class="inline-flex h-10 items-center justify-center rounded-lg border border-rose-200 bg-white px-4 text-sm font-semibold text-rose-700 hover:bg-rose-50">{t("ORDERS.DETAIL_ACTION_CONFIRM_TT")}</button>
            </form>
            """
        )
    if not actions:
        return ""
    actions_html = "".join(actions)
    return f"""
    <section class="mb-7 rounded-xl border border-slate-100 bg-white p-4 shadow-nicky">
      <h2 class="mb-4 text-lg font-semibold text-slate-950">{t("ORDERS.DETAIL_SECTION_ACTIONS")}</h2>
      <div class="flex flex-wrap items-center gap-3">
        {actions_html}
      </div>
    </section>
    """


def nicky_dashboard_button(
    order: dict[str, Any],
    settings: Settings,
    *,
    user: admin_auth.AdminUser,
    tenant: TenantConfig | None,
    compact: bool = False,
) -> str:
    bill_short_id = order.get("nicky_bill_short_id")
    if not bill_short_id:
        return ""
    label = t("ORDERS.DETAIL_ACTION_OPEN_DASHBOARD")
    icon = '<i class="ph ph-arrow-line-up-right text-base" aria-hidden="true"></i>'
    if compact:
        size_classes = "h-9 w-9 text-base"
        content = f'{icon}<span class="sr-only">{label}</span>'
    else:
        size_classes = "h-10 gap-2 px-4 text-sm"
        content = f'{icon}<span>{label}</span>'
    if not can_open_tenant_nicky_dashboard(user, tenant):
        hint = t("ORDERS.DETAIL_ACTION_OPEN_DASHBOARD_DISABLED_HINT")
        return (
            f'<span class="inline-flex {size_classes} shrink-0 cursor-not-allowed items-center justify-center '
            f'rounded-lg bg-zinc-200 font-semibold text-zinc-500" title="{e(hint)}" '
            f'aria-disabled="true">{content}</span>'
        )
    url = build_nicky_dashboard_payment_orders_url(settings, str(bill_short_id))
    return (
        f'<a class="inline-flex {size_classes} shrink-0 items-center justify-center rounded-lg bg-black '
        f'font-semibold text-white hover:bg-zinc-800" href="{e(url)}" target="_blank" rel="noreferrer" title="{e(label)}">'
        f'{content}</a>'
    )


def can_open_tenant_nicky_dashboard(
    user: admin_auth.AdminUser, tenant: TenantConfig | None
) -> bool:
    if tenant is None:
        return False
    if tenant.owner_auth_subject and tenant.owner_auth_subject == user.subject:
        return True
    owner_uuid = admin_auth.nicky_user_uuid_claim(user)
    return bool(owner_uuid and tenant.nicky_user_uuid == owner_uuid)


def tenant_options(
    tenants: list[TenantConfig], selected: str | None, *, include_all: bool = True
) -> str:
    options = option_tag("", t("COMMON.ALL_TENANTS"), selected or "") if include_all else ""
    for tenant in tenants:
        options += option_tag(tenant.tenant_id, tenant_option_label(tenant), selected or "")
    return options


def tenant_option_label(tenant: TenantConfig) -> str:
    label = tenant.name.strip() if tenant.name else ""
    if label and label != tenant.tenant_id:
        return label
    return compact_identifier(tenant.tenant_id)


def compact_identifier(value: str, *, prefix: int = 8, suffix: int = 6) -> str:
    if len(value) <= prefix + suffix + 3:
        return value
    return f"{value[:prefix]}...{value[-suffix:]}"


def tenant_filter_field(
    *,
    name: str,
    tenants: list[TenantConfig],
    selected: str,
    include_all: bool,
) -> str:
    if not include_all and len(tenants) == 1:
        tenant = tenants[0]
        return f"""
        <label class="min-w-0 text-sm font-semibold text-slate-950">{t("COMMON.TENANT")}
          <input type="hidden" name="{e(name)}" value="{e(tenant.tenant_id)}">
          <div class="mt-2 flex h-11 min-w-0 items-center rounded-lg border border-slate-200 bg-slate-50 px-3 text-sm text-slate-700 shadow-sm" title="{e(tenant.tenant_id)}">
            <span class="min-w-0 truncate font-semibold">{e(tenant_option_label(tenant))}</span>
          </div>
        </label>
        """
    return f'<label class="min-w-0 text-sm font-semibold text-slate-950">{t("COMMON.TENANT")}<select class="mt-2 h-11 w-full min-w-0 rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="{e(name)}">{tenant_options(tenants, selected, include_all=include_all)}</select></label>'


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


PAGE_SIZE_OPTIONS = [10, 25, 50]


def page_size_query_value(value: str | None, default: int = DEFAULT_PAGE_SIZE) -> int:
    try:
        size = int(value or str(default))
    except ValueError:
        return default
    return size if size in PAGE_SIZE_OPTIONS else default


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
    allow_all_tenants: bool = True,
) -> str:
    selected = str(order_filters.get("order_state") or "")
    options = "".join(
        option_tag(value, label, selected)
        for value, label in [
            ("", t("COMMON.ALL_STATES")),
            ("pending", t("ORDERS.FILTER_PENDING")),
            ("confirmed", t("ORDERS.FILTER_CONFIRMED")),
            ("tickets_voided", t("ORDERS.FILTER_VOIDED")),
        ]
    )
    selected_tenant = str(order_filters.get("tenant_id") or "")
    tenant_field = ""
    if show_tenant_filter:
        tenant_field = tenant_filter_field(
            name="orders_tenant_id",
            tenants=tenants,
            selected=selected_tenant,
            include_all=allow_all_tenants,
        )
    return f"""
    <div class="border-b border-slate-100 p-4">
      <form method="get" action="{e(action)}" data-ajax-filter="dash-filter-sections" class="grid min-w-0 grid-cols-1 items-end gap-4 md:grid-cols-2 xl:grid-cols-[minmax(160px,0.9fr)_minmax(160px,1fr)_minmax(160px,1fr)_minmax(200px,1.1fr)_auto]">
        {hidden_filter_inputs(webhook_filters, prefix="webhooks")}
        {tenant_field}
        <label class="min-w-0 text-sm font-semibold text-slate-950">{t("ORDERS.FILTER_UPDATED_FROM")}<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="date" name="orders_from" value="{e(order_filters.get("updated_from") or "")}"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">{t("ORDERS.FILTER_UPDATED_TO")}<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="date" name="orders_to" value="{e(order_filters.get("updated_to") or "")}"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">{t("WEBHOOKS.TABLE_STATUS")}<select class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="order_state">{options}</select></label>
        <div class="flex flex-wrap items-center gap-3 md:col-span-2 xl:col-span-1 xl:justify-end">
          <button class="inline-flex h-11 items-center justify-center rounded-lg bg-black px-5 text-sm font-semibold text-white hover:bg-zinc-800" type="submit">{t("COMMON.APPLY")}</button>
          <a class="inline-flex h-11 items-center justify-center rounded-lg border border-slate-200 bg-white px-5 text-sm font-semibold text-slate-950 hover:bg-slate-50" href="{e(reset_filters_href(order_filters, webhook_filters, reset='orders'))}">{t("COMMON.CLEAR")}</a>
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
    allow_all_tenants: bool = True,
) -> str:
    selected = str(webhook_filters.get("status") or "")
    options = "".join(
        option_tag(value, label, selected)
        for value, label in [
            ("", t("WEBHOOKS.FILTER_ALL")),
            ("processed", t("WEBHOOKS.FILTER_PROCESSED")),
            ("failed", t("WEBHOOKS.FILTER_FAILED")),
            ("ignored", t("WEBHOOKS.FILTER_IGNORED")),
            ("received", t("WEBHOOKS.FILTER_RECEIVED")),
        ]
    )
    selected_tenant = str(webhook_filters.get("tenant_id") or "")
    tenant_field = ""
    if show_tenant_filter:
        tenant_field = tenant_filter_field(
            name="webhooks_tenant_id",
            tenants=tenants,
            selected=selected_tenant,
            include_all=allow_all_tenants,
        )
    return f"""
    <div class="border-b border-slate-100 p-4">
      <form method="get" action="{e(action)}" data-ajax-filter="dash-filter-sections" class="grid min-w-0 grid-cols-1 items-end gap-4 md:grid-cols-2 xl:grid-cols-[minmax(160px,0.9fr)_minmax(160px,1fr)_minmax(160px,1fr)_minmax(200px,1.1fr)_auto]">
        {hidden_filter_inputs(order_filters, prefix="orders")}
        {tenant_field}
        <label class="min-w-0 text-sm font-semibold text-slate-950">{t("WEBHOOKS.FILTER_RECEIVED_FROM")}<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="date" name="webhooks_from" value="{e(webhook_filters.get("received_from") or "")}"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">{t("WEBHOOKS.FILTER_RECEIVED_TO")}<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="date" name="webhooks_to" value="{e(webhook_filters.get("received_to") or "")}"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">{t("WEBHOOKS.TABLE_STATUS")}<select class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="webhook_status">{options}</select></label>
        <div class="flex flex-wrap items-center gap-3 md:col-span-2 xl:col-span-1 xl:justify-end">
          <button class="inline-flex h-11 items-center justify-center rounded-lg bg-black px-5 text-sm font-semibold text-white hover:bg-zinc-800" type="submit">{t("COMMON.APPLY")}</button>
          <a class="inline-flex h-11 items-center justify-center rounded-lg border border-slate-200 bg-white px-5 text-sm font-semibold text-slate-950 hover:bg-slate-50" href="{e(reset_filters_href(order_filters, webhook_filters, reset='webhooks'))}">{t("COMMON.CLEAR")}</a>
        </div>
      </form>
    </div>
    """


def orders_page_filters_form(
    order_filters: dict[str, str | None],
    tenants: list[TenantConfig],
    *,
    show_tenant_filter: bool = True,
    allow_all_tenants: bool = True,
) -> str:
    selected = str(order_filters.get("order_state") or "")
    options = "".join(
        option_tag(value, label, selected)
        for value, label in [
            ("", t("COMMON.ALL_STATES")),
            ("pending", t("ORDERS.FILTER_PENDING")),
            ("confirmed", t("ORDERS.FILTER_CONFIRMED")),
            ("tickets_voided", t("ORDERS.FILTER_VOIDED")),
        ]
    )
    tenant_field = ""
    if show_tenant_filter:
        tenant_field = tenant_filter_field(
            name="tenant_id",
            tenants=tenants,
            selected=str(order_filters.get("tenant_id") or ""),
            include_all=allow_all_tenants,
        )
    return f"""
    <div class="border-b border-slate-100 p-4">
      <form method="get" action="/admin-ui/orders" data-ajax-filter="orders-card" class="grid min-w-0 grid-cols-1 items-end gap-4 md:grid-cols-2 xl:grid-cols-[minmax(160px,0.9fr)_minmax(160px,1fr)_minmax(160px,1fr)_minmax(200px,1.1fr)_auto]">
        {tenant_field}
        <label class="min-w-0 text-sm font-semibold text-slate-950">{t("ORDERS.FILTER_UPDATED_FROM")}<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="date" name="orders_from" value="{e(order_filters.get("updated_from") or "")}"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">{t("ORDERS.FILTER_UPDATED_TO")}<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="date" name="orders_to" value="{e(order_filters.get("updated_to") or "")}"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">{t("WEBHOOKS.TABLE_STATUS")}<select class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="order_state">{options}</select></label>
        <div class="flex flex-wrap items-center gap-3 md:col-span-2 xl:col-span-1 xl:justify-end">
          <button class="inline-flex h-11 items-center justify-center rounded-lg bg-black px-5 text-sm font-semibold text-white hover:bg-zinc-800" type="submit">{t("COMMON.APPLY")}</button>
          <a class="inline-flex h-11 items-center justify-center rounded-lg border border-slate-200 bg-white px-5 text-sm font-semibold text-slate-950 hover:bg-slate-50" href="/admin-ui/orders">{t("COMMON.CLEAR")}</a>
        </div>
      </form>
    </div>
    """


def tenant_filters_form(filters: dict[str, str | None]) -> str:
    active = str(filters.get("active") or "")
    configuration = str(filters.get("configuration") or "")
    active_options = "".join(
        option_tag(value, label, active)
        for value, label in [("", t("COMMON.ALL_STATUSES")), ("active", t("COMMON.ACTIVE")), ("inactive", t("COMMON.INACTIVE"))]
    )
    configuration_options = "".join(
        option_tag(value, label, configuration)
        for value, label in [
            ("", t("COMMON.ALL_CONFIGURATIONS")),
            ("complete", t("TENANTS.FILTER_CONFIGURED")),
            ("missing", t("TENANTS.FILTER_MISSING")),
        ]
    )
    return f"""
    <div class="border-b border-slate-100 p-4">
      <form method="get" action="/admin-ui/tenants" data-ajax-filter="tenants-card" class="grid min-w-0 grid-cols-1 items-end gap-4 md:grid-cols-2 xl:grid-cols-[minmax(220px,1.3fr)_minmax(180px,1fr)_minmax(220px,1.1fr)_auto]">
        <label class="min-w-0 text-sm font-semibold text-slate-950">{t("COMMON.SEARCH")}<input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" type="search" name="q" value="{e(filters.get("query") or "")}" placeholder="{t("TENANTS.FILTER_PLACEHOLDER")}"></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">{t("TENANTS.FILTER_ACTIVE")}<select class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="active">{active_options}</select></label>
        <label class="min-w-0 text-sm font-semibold text-slate-950">{t("TENANTS.FILTER_CONFIG")}<select class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black" name="configuration">{configuration_options}</select></label>
        <div class="flex flex-wrap items-center gap-3 md:col-span-2 xl:col-span-1 xl:justify-end">
          <button class="inline-flex h-11 items-center justify-center rounded-lg bg-black px-5 text-sm font-semibold text-white hover:bg-zinc-800" type="submit">{t("COMMON.APPLY")}</button>
          <a class="inline-flex h-11 items-center justify-center rounded-lg border border-slate-200 bg-white px-5 text-sm font-semibold text-slate-950 hover:bg-slate-50" href="/admin-ui/tenants">{t("COMMON.CLEAR")}</a>
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
    size_param: str | None = None,
    size_options: list[int] | None = None,
) -> str:
    total_pages = max(1, (total + page_size - 1) // page_size)
    current = min(max(1, page), total_pages)
    start = 0 if total == 0 else ((current - 1) * page_size) + 1
    end = min(total, current * page_size)
    prev_href = page_href(path, query_params, page_param, current - 1)
    next_href = page_href(path, query_params, page_param, current + 1)
    can_prev = current > 1
    can_next = current < total_pages
    prev_btn = (
        f'<a href="{e(prev_href)}" class="inline-flex h-7 w-7 items-center justify-center rounded text-slate-500 hover:bg-slate-100"><i class="ph ph-caret-left text-sm"></i></a>'
        if can_prev else
        '<span class="inline-flex h-7 w-7 items-center justify-center rounded text-slate-300 pointer-events-none"><i class="ph ph-caret-left text-sm"></i></span>'
    )
    next_btn = (
        f'<a href="{e(next_href)}" class="inline-flex h-7 w-7 items-center justify-center rounded text-slate-500 hover:bg-slate-100"><i class="ph ph-caret-right text-sm"></i></a>'
        if can_next else
        '<span class="inline-flex h-7 w-7 items-center justify-center rounded text-slate-300 pointer-events-none"><i class="ph ph-caret-right text-sm"></i></span>'
    )
    count_text = f"{start} - {end} of {total}" if total > 0 else t("COMMON.NO_RESULTS")
    rows_per_page_html = ""
    if size_param and size_options:
        opts = "".join(
            f'<option value="{s}"{"selected" if s == page_size else ""}>{s}</option>'
            for s in size_options
        )
        rows_per_page_html = f"""
        <div class="flex items-center gap-2 text-slate-500">
          <span class="whitespace-nowrap">{t("COMMON.ROWS_PER_PAGE")}:</span>
          <select class="h-7 rounded border border-slate-200 bg-white px-1 text-xs font-semibold text-slate-700 focus:outline-none"
            onchange="(function(s){{var u=new URL(window.location.href);u.searchParams.set('{e(size_param)}',s);u.searchParams.delete('{e(page_param)}');window.location.href=u.toString();}})(this.value)">
            {opts}
          </select>
        </div>"""
    return f"""
    <div class="flex flex-col gap-2 border-t border-slate-100 px-4 py-3 text-sm text-slate-500 sm:flex-row sm:items-center sm:justify-between">
      <span>{count_text}</span>
      <div class="flex items-center gap-3">
        {rows_per_page_html}
        <div class="flex items-center gap-1">
          {prev_btn}
          <span class="inline-flex h-7 min-w-[2rem] items-center justify-center rounded border border-slate-200 bg-white px-2 text-xs font-semibold text-slate-700">{current}/{total_pages}</span>
          {next_btn}
        </div>
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
        return badge(t("ORDERS.STATE_CONFIRMED"), True)
    if order.get("ticket_tailor_tickets_voided_at"):
        return (
            f'{badge(t("ORDERS.STATE_VOIDED"), False)}'
            f'<small class="mt-1 block max-w-48 text-xs leading-4 text-slate-400">{t("ORDERS.FILTER_PENDING")}</small>'
        )
    return badge(t("ORDERS.STATE_PENDING"), "warn")


def ticket_tailor_state_notice(order: dict[str, Any]) -> str:
    if not order.get("ticket_tailor_tickets_voided_at"):
        return ""
    return """
    <p class="notice-warn mb-4 flex items-start gap-2 rounded-lg border px-4 py-3 text-sm font-medium">
      <i class="ph ph-warning mt-0.5 shrink-0"></i>
      {t("ORDERS.DETAIL_VOID_NOTICE")}
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
    disabled: bool = False,
    required: bool = False,
) -> str:
    name_attr = f' name="{e(name)}"' if name else ""
    readonly_attr = " readonly" if readonly else ""
    disabled_attr = " disabled" if disabled else ""
    required_attr = " required" if required else ""
    placeholder_attr = f' placeholder="{e(placeholder)}"' if placeholder else ""
    return f"""
    <label class="mb-4 block min-w-0 text-sm font-semibold text-slate-950">
      {e(label)}
      <input class="mt-2 h-11 w-full rounded-lg border border-slate-200 bg-white px-3 text-sm text-slate-700 shadow-sm outline-none focus:ring-2 focus:ring-black disabled:bg-slate-50 disabled:text-slate-400 disabled:cursor-not-allowed" type="{e(input_type)}"{name_attr} value="{e(value)}"{placeholder_attr}{readonly_attr}{disabled_attr}{required_attr}>
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
    url = external_api_url(settings, f"/webhooks/nicky/{tenant.tenant_id}")
    if tenant.nicky_webhook_token:
        url = f"{url}?token={urllib.parse.quote(tenant.nicky_webhook_token)}"
    return url


def build_nicky_dashboard_payment_orders_url(settings: Settings, bill_short_id: str) -> str:
    params = urllib.parse.urlencode({"tab": "paymentReport", "shortId": bill_short_id})
    return f"{settings.nicky_pay_base_url}/overview?{params}"


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


_CSS_LEGACY = """
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

/* ── Nicky design tokens ─────────────────────────────────────── */
:root {
  --nicky-brand:      #deff96;
  --nicky-bg:         #f1f1f1;
  --nicky-panel:      #ffffff;
  --nicky-border:     #eff0f4;
  --nicky-border-md:  #e8e8e8;
  --nicky-text:       #252525;
  --nicky-text-dark:  #202b42;
  --nicky-muted:      #929292;
  --nicky-shadow:     14px 27px 45px 4px rgba(112,144,176,0.16);

  /* toast */
  --toast-ok-bg:      #edf7ed;
  --toast-ok-text:    #1e4620;
  --toast-ok-border:  #c3e6cb;
  --toast-warn-bg:    #fff3e0;
  --toast-warn-text:  #663c00;
  --toast-warn-border:#ffe0ab;
  --toast-err-bg:     #fdecea;
  --toast-err-text:   #611a15;
  --toast-err-border: #f5c2c7;
}

/* ── Nicky logo ──────────────────────────────────────────────── */
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

/* ── Scrollbar (matches Nicky web client) ────────────────────── */
::-webkit-scrollbar        { width: 4px; height: 4px; }
::-webkit-scrollbar-track  { background: #f3f4f6; }
::-webkit-scrollbar-thumb  { background: #d1d5db; border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: #9ca3af; }
* { scrollbar-width: thin; scrollbar-color: #d1d5db #f3f4f6; }

/* ── Nicky loading overlay animation ────────────────────────── */
@keyframes nicky-pulse {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.72; }}
}}

/* ── Phosphor icon sizing inside badges/cells ────────────────── */
.ph { font-size: 1rem; vertical-align: -2px; }

/* ── Notice/toast alignment with Nicky tokens ───────────────── */
.notice-ok   { background: var(--toast-ok-bg);   color: var(--toast-ok-text);   border-color: var(--toast-ok-border); }
.notice-warn { background: var(--toast-warn-bg);  color: var(--toast-warn-text);  border-color: var(--toast-warn-border); }
.notice-err  { background: var(--toast-err-bg);   color: var(--toast-err-text);   border-color: var(--toast-err-border); }
"""
