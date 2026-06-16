import time

from app import admin_auth
from app.config import Settings


def test_extract_roles_from_auth0_array_and_namespaced_claims() -> None:
    claims = {
        "sub": "auth0|123",
        "https://nicky.example/roles": ["Admin", "Support"],
        "permissions": '["read:orders","write:tenants"]',
    }

    roles = admin_auth.extract_roles(claims)

    assert roles == ["Admin", "Support", "read:orders", "write:tenants"]


def test_allowed_roles_are_case_insensitive() -> None:
    assert admin_auth.has_allowed_role(["admin"], ["Admin"]) is True
    assert admin_auth.has_allowed_role(["Viewer"], ["Admin", "Support"]) is False


def test_allowed_roles_wildcard_allows_authenticated_user() -> None:
    assert admin_auth.has_allowed_role([], ["*"]) is True
    assert admin_auth.has_allowed_role(["Viewer"], ["*"]) is True


def test_auth0_domain_accepts_host_or_url() -> None:
    assert (
        admin_auth.auth0_domain(Settings(auth0_domain="tenant.us.auth0.com"))
        == "https://tenant.us.auth0.com"
    )
    assert (
        admin_auth.auth0_domain(Settings(auth0_domain="https://tenant.us.auth0.com/"))
        == "https://tenant.us.auth0.com"
    )


def test_auth0_enabled_does_not_require_client_secret() -> None:
    settings = Settings(
        auth0_domain="tenant.us.auth0.com",
        auth0_client_id="public-client-id",
        auth0_client_secret="",
    )

    assert admin_auth.auth0_enabled(settings) is True


def test_expired_auth0_session_is_ignored() -> None:
    user = admin_auth.AdminUser(
        subject="auth0|123",
        name="Admin",
        email="admin@example.com",
        roles=["Admin"],
        claims={"sub": "auth0|123", "roles": ["Admin"], "exp": int(time.time()) - 1},
        auth_method="auth0",
    )

    assert admin_auth.user_from_session(admin_auth.user_to_session(user)) is None


def test_safe_return_to_rejects_external_urls() -> None:
    settings = Settings(app_base_url="https://admin.example.com")

    assert admin_auth.safe_return_to(settings, "/admin-ui/tenants") == "/admin-ui/tenants"
    assert (
        admin_auth.safe_return_to(settings, "https://admin.example.com/admin-ui/orders")
        == "/admin-ui/orders"
    )
    assert admin_auth.safe_return_to(settings, "https://evil.example.com") == "/admin-ui"
    assert admin_auth.safe_return_to(settings, "//evil.example.com") == "/admin-ui"
