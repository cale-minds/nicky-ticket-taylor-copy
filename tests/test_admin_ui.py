from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app import admin_auth
from app.admin_ui import create_admin_ui_router
from app.config import Settings
from app.db import Database
from app.nicky import NickyClient
from app.service import IntegrationService
from app.tenants import tenant_from_settings
from app.ticket_tailor import TicketTailorClient


def build_test_client(tmp_path, **settings_overrides) -> TestClient:
    settings_values = {
        "database_path": tmp_path / "integration.sqlite3",
        "admin_token": "admin-secret",
        "admin_session_secret": "test-session-secret",
        "dry_run": True,
        "nicky_default_blockchain_asset_id": "USD.USD",
    }
    settings_values.update(settings_overrides)
    settings = Settings(**settings_values)
    db = Database(settings.database_path)
    db.init()
    db.upsert_tenant(tenant_from_settings(settings, "demo-tenant"))
    nicky_client = NickyClient(settings)
    ticket_tailor_client = TicketTailorClient(settings)
    service = IntegrationService(
        settings=settings,
        db=db,
        nicky=nicky_client,
        ticket_tailor=ticket_tailor_client,
    )

    def require_admin(
        request: Request, x_admin_token: str | None = Header(default=None)
    ):
        if x_admin_token == settings.admin_token:
            return admin_auth.make_admin_token_user()
        user = admin_auth.get_session_user(request)
        if user and admin_auth.has_allowed_role(user.roles, settings.admin_allowed_roles):
            return user
        raise HTTPException(status_code=401, detail="Admin authentication required")

    app = FastAPI()
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


def test_admin_ui_requires_auth0_configuration(tmp_path) -> None:
    client = build_test_client(tmp_path)

    login_page = client.get("/admin-ui/login?return_to=%2Fadmin-ui%2Ftenants")
    assert login_page.status_code == 503
    assert login_page.json()["detail"] == "Auth0 is required but is not configured"

    response = client.post(
        "/admin-ui/login/admin-token",
        data={"admin_token": "admin-secret", "return_to": "/admin-ui/tenants"},
        follow_redirects=False,
    )

    assert response.status_code == 404


def test_admin_ui_redirects_to_login_when_session_is_missing(tmp_path) -> None:
    client = build_test_client(tmp_path)

    response = client.get("/admin-ui/tenants?tenant_id=demo-tenant", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/admin-ui/login?return_to=%2Fadmin-ui%2Ftenants%3Ftenant_id%3Ddemo-tenant"
    )


def test_admin_ui_auth0_login_redirects_to_universal_login(tmp_path) -> None:
    client = build_test_client(
        tmp_path,
        app_base_url="http://testserver",
        admin_token="",
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
        admin_token="",
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
