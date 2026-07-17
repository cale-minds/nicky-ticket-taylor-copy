import base64
import base64
import json
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from starlette.middleware.sessions import SessionMiddleware

from app import admin_auth
from app.admin_ui import create_admin_ui_router, ticket_tailor_state_notice
from app.config import Settings
from app.db import Database
from app.nicky import NickyApiError, NickyClient
from app.service import IntegrationService
from app.tenants import tenant_from_settings
from app.ticket_tailor import TicketTailorClient


class FakeAdminNickyClient(NickyClient):
    async def validate_api_key(self, nicky_api_key: str) -> dict:
        return {
            "nicky_user_uuid": "uuid-from-nicky",
            "nicky_user_short_id": "NICKY01",
            "assets": [{"id": "USD.USD", "name": "USD"}],
        }

    async def create_webhook(self, tenant, url: str) -> dict:
        return {"status": "created", "tenant_id": tenant.tenant_id, "url": url}


class FakeCommonUserNickyClient(FakeAdminNickyClient):
    async def validate_api_key(self, nicky_api_key: str) -> dict:
        return {
            "nicky_user_uuid": "uuid-from-nicky",
            "nicky_user_short_id": "NICKY01",
            "nicky_user_email": "common@example.com",
            "assets": [{"id": "USD.USD", "name": "USD"}],
        }


class FakeOtherUserNickyClient(FakeAdminNickyClient):
    async def validate_api_key(self, nicky_api_key: str) -> dict:
        return {
            "nicky_user_uuid": "uuid-from-nicky",
            "nicky_user_short_id": "NICKY01",
            "nicky_user_email": "other@example.com",
            "assets": [{"id": "USD.USD", "name": "USD"}],
        }


class FakeNoIdentityNickyClient(FakeAdminNickyClient):
    async def validate_api_key(self, nicky_api_key: str) -> dict:
        return {
            "nicky_user_uuid": "",
            "nicky_user_short_id": "",
            "assets": [{"id": "USD.USD", "name": "USD"}],
        }


class FailingNickyValidationClient(NickyClient):
    async def validate_api_key(self, nicky_api_key: str) -> dict:
        raise NickyApiError(
            "Could not validate Nicky API key: Nicky returned 401 Unauthorized",
            status_code=401,
        )


def test_ticket_tailor_state_notice_renders_translation() -> None:
    notice = ticket_tailor_state_notice(
        {"ticket_tailor_tickets_voided_at": "2026-07-09T03:12:35.693000"}
    )

    assert "Issued tickets were voided through the Ticket Tailor API." in notice
    assert '{t("ORDERS.DETAIL_VOID_NOTICE")}' not in notice


def build_test_client(tmp_path, nicky_client_class=NickyClient, **settings_overrides) -> TestClient:
    settings_values = {
        "database_path": tmp_path / "integration.sqlite3",
        "admin_session_secret": "test-session-secret",
        "nicky_default_blockchain_asset_id": "USD.USD",
    }
    settings_values.update(settings_overrides)
    settings = Settings(**settings_values)
    db = Database(settings.database_path)
    db.init()
    db.upsert_tenant(tenant_from_settings(settings, "demo-tenant"))
    nicky_client = nicky_client_class(settings)
    ticket_tailor_client = TicketTailorClient(settings)
    service = IntegrationService(
        settings=settings,
        db=db,
        nicky=nicky_client,
        ticket_tailor=ticket_tailor_client,
    )

    def require_admin(request: Request):
        user = admin_auth.get_session_user(request)
        if user and admin_auth.has_allowed_role(user.roles, settings.admin_allowed_roles):
            return user
        raise HTTPException(status_code=401, detail="Admin authentication required")

    app = FastAPI()
    app.state.db = db
    app.state.settings = settings
    app.add_middleware(SessionMiddleware, secret_key=settings.admin_session_secret)
    app.include_router(
        create_admin_ui_router(
            settings=settings,
            db=db,
            nicky_client=nicky_client,
            service=service,
            require_admin=require_admin,
        )
    )
    return TestClient(app)


def sign_admin_session(
    secret: str = "test-session-secret",
    *,
    subject: str = "auth0|admin",
    name: str = "Admin User",
    email: str = "admin@example.com",
    roles: list[str] | None = None,
    claims: dict | None = None,
) -> str:
    user_roles = ["Admin"] if roles is None else roles
    user_claims = {"sub": subject, "roles": user_roles} if claims is None else claims
    user = admin_auth.AdminUser(
        subject=subject,
        name=name,
        email=email,
        roles=user_roles,
        claims=user_claims,
        auth_method="test",
    )
    data = {admin_auth.SESSION_KEY: admin_auth.user_to_session(user)}
    payload = base64.b64encode(json.dumps(data).encode("utf-8"))
    return TimestampSigner(secret).sign(payload).decode("utf-8")


def authenticate_admin(client: TestClient) -> None:
    client.cookies.set("session", sign_admin_session())


def authenticate_common_user(client: TestClient, *, nicky_user_uuid: str) -> None:
    client.cookies.set(
        "session",
        sign_admin_session(
            subject="auth0|common",
            name="Common User",
            email="common@example.com",
            roles=[],
            claims={
                "sub": "auth0|common",
                "nicky_user_uuid": nicky_user_uuid,
            },
        ),
    )


def authenticate_common_user_without_nicky_claim(client: TestClient) -> None:
    client.cookies.set(
        "session",
        sign_admin_session(
            subject="auth0|common",
            name="Common User",
            email="common@example.com",
            roles=[],
            claims={"sub": "auth0|common"},
        ),
    )


def authenticate_support(client: TestClient) -> None:
    client.cookies.set(
        "session",
        sign_admin_session(
            subject="auth0|support",
            name="Support User",
            email="support@example.com",
            roles=["Support"],
            claims={"sub": "auth0|support", "roles": ["Support"]},
        ),
    )


def seed_tenant(
    client: TestClient,
    tenant_id: str,
    *,
    active: bool = True,
    nicky_api_key: str = "existing_nicky_key",
    ticket_tailor_api_key: str = "existing_tt_key",
    owner_auth_subject: str = "",
) -> None:
    tenant = replace(
        tenant_from_settings(client.app.state.settings, tenant_id),
        active=active,
        nicky_api_key=nicky_api_key,
        ticket_tailor_api_key=ticket_tailor_api_key,
        nicky_default_blockchain_asset_id="USD.USD",
        owner_auth_subject=owner_auth_subject,
    )
    client.app.state.db.upsert_tenant(tenant)


def seed_dashboard_order(client: TestClient, tenant_id: str, order_id: str) -> None:
    client.app.state.db.upsert_order(
        tenant_id,
        {
            "ticket_tailor_order_id": order_id,
            "event_id": f"event-{order_id}",
            "status": "pending",
            "payment_status": "pending",
            "currency": "USD",
            "amount_minor": 100,
            "buyer_email": f"{order_id}@example.com",
            "buyer_name": f"Buyer {order_id}",
        },
        {
            "id": order_id,
            "issued_tickets": [],
        },
    )


def seed_dashboard_webhook(client: TestClient, tenant_id: str, event_id: str) -> None:
    client.app.state.db.insert_webhook_event(
        tenant_id=tenant_id,
        source="ticket_tailor",
        event_id=event_id,
        event_type=f"{event_id.upper()}.CREATED",
        raw_body=json.dumps({"id": event_id}).encode("utf-8"),
    )
    client.app.state.db.mark_webhook_event(
        tenant_id,
        "ticket_tailor",
        event_id,
        "processed",
    )


def test_admin_ui_requires_auth0_configuration(tmp_path) -> None:
    client = build_test_client(tmp_path)

    login_page = client.get("/admin-ui/login?return_to=%2Fadmin-ui%2Ftenants")
    assert login_page.status_code == 503
    assert login_page.json()["detail"] == "Auth0 is required but is not configured"


def test_admin_ui_redirects_to_login_when_session_is_missing(tmp_path) -> None:
    client = build_test_client(tmp_path)

    response = client.get("/admin-ui/tenants?tenant_id=demo-tenant", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/admin-ui/login?return_to=%2Fadmin-ui%2Ftenants%3Ftenant_id%3Ddemo-tenant"
    )


def test_root_redirects_to_login_when_session_is_missing(tmp_path) -> None:
    client = build_test_client(tmp_path)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin-ui/login?return_to=%2F"


def test_admin_ui_auth0_login_redirects_to_universal_login(tmp_path) -> None:
    client = build_test_client(
        tmp_path,
        app_base_url="http://testserver",
        auth0_domain="nicky-prod.us.auth0.com",
        auth0_client_id="auth0-client-id",
        auth0_audience="https://nicky-prod.azurewebsites.net",
    )

    response = client.get("/admin-ui/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin-ui/login?return_to=%2Fadmin-ui%2F"

    login_response = client.get(response.headers["location"], follow_redirects=False)

    assert login_response.status_code == 303
    auth0_url = urlparse(login_response.headers["location"])
    auth0_params = parse_qs(auth0_url.query)
    assert auth0_url.scheme == "https"
    assert auth0_url.netloc == "nicky-prod.us.auth0.com"
    assert auth0_url.path == "/authorize"
    assert auth0_params["response_type"] == ["code"]
    assert auth0_params["client_id"] == ["auth0-client-id"]
    assert auth0_params["redirect_uri"] == ["http://testserver/admin-ui/callback"]
    assert auth0_params["audience"] == ["https://nicky-prod.azurewebsites.net"]
    assert auth0_params["code_challenge_method"] == ["S256"]
    assert auth0_params.get("code_challenge")
    assert "offline_access" in auth0_params["scope"][0]
    assert auth0_params.get("state")
    assert auth0_params.get("nonce")


def test_admin_ui_auth0_local_angular_compatibility_routes(tmp_path) -> None:
    client = build_test_client(
        tmp_path,
        app_base_url="http://localhost:4200",
        auth0_domain="dev-eq0ptfwdhb1s1h12.us.auth0.com",
        auth0_client_id="auth0-client-id",
        auth0_audience="https://nicky-tech.azurewebsites.net",
        auth0_callback_path="/overview",
    )

    response = client.get("/overview", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin-ui/login?return_to=%2Foverview"

    login_response = client.get(response.headers["location"], follow_redirects=False)

    assert login_response.status_code == 303
    auth0_url = urlparse(login_response.headers["location"])
    auth0_params = parse_qs(auth0_url.query)
    assert auth0_url.netloc == "dev-eq0ptfwdhb1s1h12.us.auth0.com"
    assert auth0_params["redirect_uri"] == ["http://localhost:4200/overview"]


def test_dashboard_does_not_show_mode_card(tmp_path) -> None:
    client = build_test_client(tmp_path)
    authenticate_admin(client)

    response = client.get("/overview")

    assert response.status_code == 200
    assert ">Mode<" not in response.text
    assert "fixed behavior" not in response.text


def test_wildcard_allowed_roles_keeps_common_user_in_scoped_view(tmp_path) -> None:
    client = build_test_client(tmp_path, admin_allowed_roles=["*"])
    authenticate_common_user(client, nicky_user_uuid="demo-tenant")

    response = client.get("/overview")

    assert response.status_code == 200
    assert ">User<" in response.text
    assert ">Admin<" not in response.text
    assert 'href="/admin-ui/tenants/new"' in response.text
    assert 'type="hidden" name="orders_tenant_id" value="demo-tenant"' in response.text
    assert 'type="hidden" name="webhooks_tenant_id" value="demo-tenant"' in response.text
    assert ">demo-tenant<" in response.text
    assert "All tenants" not in response.text


def test_admin_can_filter_dashboard_recent_orders_and_webhooks_by_tenant(tmp_path) -> None:
    client = build_test_client(tmp_path)
    seed_tenant(client, "tenant-alpha")
    seed_tenant(client, "tenant-beta")
    seed_dashboard_order(client, "tenant-alpha", "order-alpha")
    seed_dashboard_order(client, "tenant-beta", "order-beta")
    seed_dashboard_webhook(client, "tenant-alpha", "webhook-alpha")
    seed_dashboard_webhook(client, "tenant-beta", "webhook-beta")
    authenticate_admin(client)

    response = client.get(
        "/overview?orders_tenant_id=tenant-alpha&webhooks_tenant_id=tenant-beta"
    )
    html = response.text

    assert response.status_code == 200
    assert 'name="orders_tenant_id"' in html
    assert 'name="webhooks_tenant_id"' in html
    assert '<option value="tenant-alpha" selected>tenant-alpha</option>' in html
    assert '<option value="tenant-beta" selected>tenant-beta</option>' in html
    assert "order-alpha" in html
    assert "order-beta" not in html
    assert "WEBHOOK-BETA.CREATED" in html
    assert "WEBHOOK-ALPHA.CREATED" not in html


def test_support_can_filter_dashboard_recent_orders_and_webhooks_by_tenant(tmp_path) -> None:
    client = build_test_client(tmp_path)
    seed_tenant(client, "tenant-alpha")
    seed_tenant(client, "tenant-beta")
    seed_dashboard_order(client, "tenant-alpha", "support-order-alpha")
    seed_dashboard_order(client, "tenant-beta", "support-order-beta")
    seed_dashboard_webhook(client, "tenant-alpha", "support-webhook-alpha")
    seed_dashboard_webhook(client, "tenant-beta", "support-webhook-beta")
    authenticate_support(client)

    response = client.get(
        "/overview?orders_tenant_id=tenant-beta&webhooks_tenant_id=tenant-alpha"
    )
    html = response.text

    assert response.status_code == 200
    assert 'name="orders_tenant_id"' in html
    assert 'name="webhooks_tenant_id"' in html
    assert "support-order-beta" in html
    assert "support-order-alpha" not in html
    assert "SUPPORT-WEBHOOK-ALPHA.CREATED" in html
    assert "SUPPORT-WEBHOOK-BETA.CREATED" not in html


def test_common_user_can_filter_dashboard_only_by_created_tenants(tmp_path) -> None:
    client = build_test_client(tmp_path, admin_allowed_roles=["Admin"])
    seed_tenant(client, "owned-alpha", owner_auth_subject="auth0|common")
    seed_tenant(client, "owned-beta", owner_auth_subject="auth0|common")
    seed_tenant(client, "other-tenant", owner_auth_subject="auth0|other")
    seed_dashboard_order(client, "owned-alpha", "owned-order-alpha")
    seed_dashboard_order(client, "owned-beta", "owned-order-beta")
    seed_dashboard_order(client, "other-tenant", "other-order")
    seed_dashboard_webhook(client, "owned-alpha", "owned-webhook-alpha")
    seed_dashboard_webhook(client, "owned-beta", "owned-webhook-beta")
    seed_dashboard_webhook(client, "other-tenant", "other-webhook")
    authenticate_common_user_without_nicky_claim(client)

    allowed_response = client.get(
        "/overview?orders_tenant_id=owned-beta&webhooks_tenant_id=owned-alpha"
    )
    allowed_html = allowed_response.text

    assert allowed_response.status_code == 200
    assert 'name="orders_tenant_id"' in allowed_html
    assert 'name="webhooks_tenant_id"' in allowed_html
    assert '<option value="owned-alpha" selected>owned-alpha</option>' in allowed_html
    assert '<option value="owned-beta" selected>owned-beta</option>' in allowed_html
    assert 'value="other-tenant"' not in allowed_html
    assert "owned-order-beta" in allowed_html
    assert "owned-order-alpha" not in allowed_html
    assert "other-order" not in allowed_html
    assert "OWNED-WEBHOOK-ALPHA.CREATED" in allowed_html
    assert "OWNED-WEBHOOK-BETA.CREATED" not in allowed_html
    assert "OTHER-WEBHOOK.CREATED" not in allowed_html

    blocked_response = client.get(
        "/overview?orders_tenant_id=other-tenant&webhooks_tenant_id=other-tenant"
    )
    blocked_html = blocked_response.text

    assert blocked_response.status_code == 200
    assert "other-order" not in blocked_html
    assert "OTHER-WEBHOOK.CREATED" not in blocked_html
    assert "owned-order-alpha" in blocked_html or "owned-order-beta" in blocked_html
    assert (
        "OWNED-WEBHOOK-ALPHA.CREATED" in blocked_html
        or "OWNED-WEBHOOK-BETA.CREATED" in blocked_html
    )


def test_new_tenant_form_keeps_derived_fields_out_of_identity(tmp_path) -> None:
    client = build_test_client(tmp_path)
    authenticate_admin(client)

    response = client.get("/admin-ui/tenants/new")
    html = response.text
    identity_block = html[html.index(">Tenant<") : html.index(">Nicky<")]
    nicky_block = html[html.index(">Nicky<") : html.index(">Ticket Tailor<")]

    assert response.status_code == 200
    assert "Name" in identity_block
    assert "Tenant UUID" not in identity_block
    assert "Nicky user UUID" not in identity_block
    assert "Nicky short ID" not in identity_block
    assert "Nicky email" in nicky_block
    assert "Nicky user UUID" not in nicky_block
    assert "Nicky short ID" not in nicky_block
    assert 'name="tenant_id"' not in html
    assert "Webhook URL" not in html
    assert "/webhooks/ticket-tailor/" not in html
    assert 'data-toggle-secret="nicky-api-key"' in html
    assert 'data-toggle-secret="ticket-tailor-api-key"' in html
    assert 'id="validate-ticket-tailor-key"' in html
    assert 'id="save-tenant-button"' in html
    assert "disabled>Create tenant</button>" in html
    assert 'name="nicky_user_email" type="email" value=""' in html
    assert 'name="nicky_user_uuid"' not in html
    assert 'name="nicky_user_short_id"' not in html


def test_common_new_tenant_form_uses_auth0_email_for_nicky_identity(tmp_path) -> None:
    client = build_test_client(tmp_path, admin_allowed_roles=["Admin"])
    authenticate_common_user_without_nicky_claim(client)

    response = client.get("/admin-ui/tenants/new")
    html = response.text

    assert response.status_code == 200
    assert 'name="nicky_user_email" type="email" value="common@example.com" readonly' in html
    assert 'name="nicky_user_uuid"' not in html
    assert 'name="nicky_user_short_id"' not in html


def test_existing_tenant_form_shows_final_ticket_tailor_webhook_url(tmp_path) -> None:
    client = build_test_client(tmp_path, app_base_url="http://localhost:4200")
    authenticate_admin(client)

    response = client.get("/admin-ui/tenants/demo-tenant/edit")
    html = response.text
    identity_block = html[html.index(">Tenant<") : html.index(">Nicky<")]
    ticket_tailor_block = html[html.index(">Ticket Tailor<") :]

    assert response.status_code == 200
    assert "Tenant UUID" in identity_block
    assert "Ticket Tailor webhook" in ticket_tailor_block
    assert "Settings &gt; API &gt; WebHook" in ticket_tailor_block
    assert 'id="copy-ticket-tailor-webhook-url"' in ticket_tailor_block
    assert "http://localhost:4200/api/webhooks/ticket-tailor/demo-tenant" in ticket_tailor_block


def test_admin_order_detail_disables_filtered_nicky_dashboard_for_other_user_tenant(
    tmp_path,
) -> None:
    client = build_test_client(tmp_path, nicky_pay_base_url="https://pay.nicky.me")
    authenticate_admin(client)
    seed_dashboard_order(client, "demo-tenant", "or_123")
    client.app.state.db.update_nicky_payment_request(
        tenant_id="demo-tenant",
        ticket_tailor_order_id="or_123",
        payment_request_id="935c2ac4-97eb-4f25-b174-08deda5b7ec4",
        bill_short_id="QYGAV1",
        receiver_short_id="NICKY01",
        payment_url="https://pay.nicky.me/payment-report/NICKY01?paymentId=QYGAV1",
        status="PaymentPending",
    )

    response = client.get("/admin-ui/orders/or_123?tenant_id=demo-tenant")
    html = response.text

    assert response.status_code == 200
    assert "Open in Nicky dashboard" in html
    assert "This opens the dashboard of the tenant owner" in html
    assert 'aria-disabled="true"' in html
    assert "https://pay.nicky.me/overview?tab=paymentReport" not in html
    assert "Create Nicky Payment Request" not in html
    assert "Confirm Ticket Tailor payment" not in html
    assert ">Actions<" not in html


def test_tenant_owner_order_detail_links_to_filtered_nicky_dashboard(tmp_path) -> None:
    client = build_test_client(
        tmp_path,
        admin_allowed_roles=["*"],
        nicky_pay_base_url="https://pay.nicky.me",
    )
    seed_tenant(client, "owned-tenant", owner_auth_subject="auth0|common")
    seed_dashboard_order(client, "owned-tenant", "or_123")
    client.app.state.db.update_nicky_payment_request(
        tenant_id="owned-tenant",
        ticket_tailor_order_id="or_123",
        payment_request_id="935c2ac4-97eb-4f25-b174-08deda5b7ec4",
        bill_short_id="QYGAV1",
        receiver_short_id="NICKY01",
        payment_url="https://pay.nicky.me/payment-report/NICKY01?paymentId=QYGAV1",
        status="PaymentPending",
    )
    authenticate_common_user_without_nicky_claim(client)

    response = client.get("/admin-ui/orders/or_123?tenant_id=owned-tenant")
    html = response.text

    assert response.status_code == 200
    assert 'href="https://pay.nicky.me/overview?tab=paymentReport&amp;shortId=QYGAV1"' in html
    assert 'aria-disabled="true"' not in html


def test_order_list_shows_disabled_nicky_dashboard_button_for_admin_on_other_user_tenant(
    tmp_path,
) -> None:
    client = build_test_client(tmp_path, nicky_pay_base_url="https://pay.nicky.me")
    authenticate_admin(client)
    seed_dashboard_order(client, "demo-tenant", "or_123")
    client.app.state.db.update_nicky_payment_request(
        tenant_id="demo-tenant",
        ticket_tailor_order_id="or_123",
        payment_request_id="935c2ac4-97eb-4f25-b174-08deda5b7ec4",
        bill_short_id="QYGAV1",
        receiver_short_id="NICKY01",
        payment_url="https://pay.nicky.me/payment-report/NICKY01?paymentId=QYGAV1",
        status="PaymentPending",
    )

    response = client.get("/overview")
    html = response.text

    assert response.status_code == 200
    assert "Open in Nicky dashboard" in html
    assert "This opens the dashboard of the tenant owner" in html
    assert 'aria-disabled="true"' in html
    assert "https://pay.nicky.me/overview?tab=paymentReport" not in html


def test_order_list_links_to_filtered_nicky_dashboard_for_tenant_owner(tmp_path) -> None:
    client = build_test_client(
        tmp_path,
        admin_allowed_roles=["*"],
        nicky_pay_base_url="https://pay.nicky.me",
    )
    seed_tenant(client, "owned-tenant", owner_auth_subject="auth0|common")
    seed_dashboard_order(client, "owned-tenant", "or_123")
    client.app.state.db.update_nicky_payment_request(
        tenant_id="owned-tenant",
        ticket_tailor_order_id="or_123",
        payment_request_id="935c2ac4-97eb-4f25-b174-08deda5b7ec4",
        bill_short_id="QYGAV1",
        receiver_short_id="NICKY01",
        payment_url="https://pay.nicky.me/payment-report/NICKY01?paymentId=QYGAV1",
        status="PaymentPending",
    )
    authenticate_common_user_without_nicky_claim(client)

    response = client.get("/admin-ui/orders?tenant_id=owned-tenant")
    html = response.text

    assert response.status_code == 200
    assert 'href="https://pay.nicky.me/overview?tab=paymentReport&amp;shortId=QYGAV1"' in html
    assert 'aria-disabled="true"' not in html


def test_common_user_can_deactivate_owned_tenant(tmp_path) -> None:
    client = build_test_client(tmp_path, admin_allowed_roles=["Admin"])
    seed_tenant(client, "owned-tenant", owner_auth_subject="auth0|common")
    authenticate_common_user_without_nicky_claim(client)

    edit_response = client.get("/admin-ui/tenants/owned-tenant/edit")
    response = client.post(
        "/admin-ui/tenants/owned-tenant/delete",
        follow_redirects=False,
    )
    tenant = client.app.state.db.get_tenant("owned-tenant")

    assert edit_response.status_code == 200
    assert "Deactivate tenant" in edit_response.text
    assert response.status_code == 303
    assert response.headers["location"] == "/admin-ui/tenants"
    assert tenant is not None
    assert tenant.active is False


def test_common_user_cannot_deactivate_other_users_tenant(tmp_path) -> None:
    client = build_test_client(tmp_path, admin_allowed_roles=["Admin"])
    seed_tenant(client, "other-tenant", owner_auth_subject="auth0|other")
    authenticate_common_user_without_nicky_claim(client)

    response = client.post("/admin-ui/tenants/other-tenant/delete")

    assert response.status_code == 403
    assert client.app.state.db.get_tenant("other-tenant").active is True


def test_new_tenant_save_derives_tenant_uuid_from_nicky_validation(tmp_path) -> None:
    client = build_test_client(tmp_path, nicky_client_class=FakeAdminNickyClient)
    authenticate_admin(client)

    response = client.post(
        "/admin-ui/tenants/save",
        data={
            "name": "Tenant from Nicky",
            "ticket_tailor_api_key": "tt_live_key",
            "nicky_api_key": "nicky_live_key",
            "nicky_user_email": "admin@example.com",
            "nicky_default_blockchain_asset_id": "USD.USD",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin-ui/tenants/uuid-from-nicky/edit?saved=1"


def test_common_user_without_nicky_claim_can_save_matching_nicky_key(tmp_path) -> None:
    client = build_test_client(
        tmp_path,
        nicky_client_class=FakeCommonUserNickyClient,
        admin_allowed_roles=["Admin"],
    )
    authenticate_common_user_without_nicky_claim(client)

    response = client.post(
        "/admin-ui/tenants/save",
        data={
            "name": "Tenant from Nicky",
            "ticket_tailor_api_key": "tt_live_key",
            "nicky_api_key": "nicky_live_key",
            "nicky_user_email": "admin@example.com",
            "nicky_default_blockchain_asset_id": "USD.USD",
        },
        follow_redirects=False,
    )
    tenant = client.app.state.db.get_tenant("uuid-from-nicky")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin-ui/tenants/uuid-from-nicky/edit?saved=1"
    assert tenant is not None
    assert tenant.owner_auth_subject == "auth0|common"


def test_common_user_without_nicky_claim_can_save_valid_key_without_email_match(tmp_path) -> None:
    client = build_test_client(
        tmp_path,
        nicky_client_class=FakeOtherUserNickyClient,
        admin_allowed_roles=["Admin"],
    )
    authenticate_common_user_without_nicky_claim(client)

    response = client.post(
        "/admin-ui/tenants/save",
        data={
            "name": "Tenant from Nicky",
            "ticket_tailor_api_key": "tt_live_key",
            "nicky_api_key": "nicky_live_key",
            "nicky_user_email": "admin@example.com",
            "nicky_default_blockchain_asset_id": "USD.USD",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin-ui/tenants/uuid-from-nicky/edit?saved=1"


def test_new_tenant_save_generates_tenant_id_when_nicky_returns_no_identity(tmp_path) -> None:
    client = build_test_client(tmp_path, nicky_client_class=FakeNoIdentityNickyClient)
    authenticate_admin(client)

    response = client.post(
        "/admin-ui/tenants/save",
        data={
            "name": "Tenant without Nicky identity",
            "ticket_tailor_api_key": "tt_live_key",
            "nicky_api_key": "nicky_live_key",
            "nicky_user_email": "manual-user@example.com",
            "nicky_default_blockchain_asset_id": "USD.USD",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin-ui/tenants/tenant-")
    assert response.headers["location"].endswith("/edit?saved=1")
    tenant_id = response.headers["location"].split("/admin-ui/tenants/", 1)[1].split("/edit", 1)[0]
    tenant = client.app.state.db.get_tenant(tenant_id)
    assert tenant is not None
    assert tenant.nicky_user_email == "manual-user@example.com"
    assert tenant.nicky_user_uuid == ""
    assert tenant.nicky_user_short_id == ""
    assert tenant.nicky_receiver_short_id == ""


def test_new_tenant_save_blocks_nicky_api_key_used_by_active_tenant(tmp_path) -> None:
    client = build_test_client(tmp_path, nicky_client_class=FakeAdminNickyClient)
    authenticate_admin(client)
    seed_tenant(
        client,
        "active-conflict",
        active=True,
        nicky_api_key="nicky_live_key",
        ticket_tailor_api_key="other_tt_key",
    )

    response = client.post(
        "/admin-ui/tenants/save",
        data={
            "name": "Tenant from Nicky",
            "ticket_tailor_api_key": "tt_live_key",
            "nicky_api_key": "nicky_live_key",
            "nicky_user_email": "admin@example.com",
            "nicky_default_blockchain_asset_id": "USD.USD",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Nicky API key is already used by an active tenant"


def test_new_tenant_save_allows_nicky_api_key_used_by_inactive_tenant(tmp_path) -> None:
    client = build_test_client(tmp_path, nicky_client_class=FakeAdminNickyClient)
    authenticate_admin(client)
    seed_tenant(
        client,
        "inactive-conflict",
        active=False,
        nicky_api_key="nicky_live_key",
        ticket_tailor_api_key="other_tt_key",
    )

    response = client.post(
        "/admin-ui/tenants/save",
        data={
            "name": "Tenant from Nicky",
            "ticket_tailor_api_key": "tt_live_key",
            "nicky_api_key": "nicky_live_key",
            "nicky_user_email": "admin@example.com",
            "nicky_default_blockchain_asset_id": "USD.USD",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin-ui/tenants/uuid-from-nicky/edit?saved=1"


def test_new_tenant_save_blocks_ticket_tailor_api_key_used_by_active_tenant(tmp_path) -> None:
    client = build_test_client(tmp_path, nicky_client_class=FakeAdminNickyClient)
    authenticate_admin(client)
    seed_tenant(
        client,
        "active-tt-conflict",
        active=True,
        nicky_api_key="other_nicky_key",
        ticket_tailor_api_key="tt_live_key",
    )

    response = client.post(
        "/admin-ui/tenants/save",
        data={
            "name": "Tenant from Nicky",
            "ticket_tailor_api_key": "tt_live_key",
            "nicky_api_key": "nicky_live_key",
            "nicky_user_email": "admin@example.com",
            "nicky_default_blockchain_asset_id": "USD.USD",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Ticket Tailor API key is already used by an active tenant"


def test_new_tenant_save_allows_ticket_tailor_api_key_used_by_inactive_tenant(tmp_path) -> None:
    client = build_test_client(tmp_path, nicky_client_class=FakeAdminNickyClient)
    authenticate_admin(client)
    seed_tenant(
        client,
        "inactive-tt-conflict",
        active=False,
        nicky_api_key="other_nicky_key",
        ticket_tailor_api_key="tt_live_key",
    )

    response = client.post(
        "/admin-ui/tenants/save",
        data={
            "name": "Tenant from Nicky",
            "ticket_tailor_api_key": "tt_live_key",
            "nicky_api_key": "nicky_live_key",
            "nicky_user_email": "admin@example.com",
            "nicky_default_blockchain_asset_id": "USD.USD",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin-ui/tenants/uuid-from-nicky/edit?saved=1"


def test_new_tenant_save_returns_json_error_when_nicky_rejects_key(tmp_path) -> None:
    client = build_test_client(tmp_path, nicky_client_class=FailingNickyValidationClient)
    authenticate_admin(client)

    response = client.post(
        "/admin-ui/tenants/save",
        data={
            "name": "Tenant from Nicky",
            "ticket_tailor_api_key": "tt_live_key",
            "nicky_api_key": "nicky_live_key",
            "nicky_default_blockchain_asset_id": "USD.USD",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Invalid Nicky API key: Could not validate Nicky API key: "
        "Nicky returned 401 Unauthorized"
    )
