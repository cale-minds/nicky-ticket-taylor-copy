from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from fastapi import HTTPException, Request, status
from jwt import PyJWKClient

from app.config import Settings


SESSION_KEY = "admin_user"
STATE_KEY = "auth0_state"
NONCE_KEY = "auth0_nonce"
RETURN_TO_KEY = "auth0_return_to"
CODE_VERIFIER_KEY = "auth0_code_verifier"


@dataclass(frozen=True)
class AdminUser:
    subject: str
    name: str
    email: str
    roles: list[str]
    claims: dict[str, Any]
    auth_method: str


def auth0_enabled(settings: Settings) -> bool:
    return bool(settings.auth0_domain and settings.auth0_client_id)


def auth0_domain(settings: Settings) -> str:
    domain = settings.auth0_domain.strip().rstrip("/")
    if not domain:
        return ""
    if not domain.startswith(("http://", "https://")):
        domain = f"https://{domain}"
    return domain


def auth0_issuer(settings: Settings) -> str:
    domain = auth0_domain(settings)
    return f"{domain}/" if domain and not domain.endswith("/") else domain


def auth0_callback_url(settings: Settings) -> str:
    path = settings.auth0_callback_path
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{settings.app_base_url}{path}"


def safe_return_to(settings: Settings, return_to: str | None) -> str:
    if not return_to:
        return "/admin-ui"
    value = return_to.strip()
    if not value:
        return "/admin-ui"
    if value.startswith("/") and not value.startswith("//"):
        return value

    base = urllib.parse.urlparse(settings.app_base_url)
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme and parsed.netloc and parsed.scheme == base.scheme and parsed.netloc == base.netloc:
        path = parsed.path or "/admin-ui"
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{path}{query}"

    return "/admin-ui"


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_auth0_authorize_url(
    settings: Settings, request: Request, *, return_to: str | None = None
) -> str:
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    code_verifier = secrets.token_urlsafe(64)
    request.session[STATE_KEY] = state
    request.session[NONCE_KEY] = nonce
    request.session[RETURN_TO_KEY] = safe_return_to(settings, return_to)
    request.session[CODE_VERIFIER_KEY] = code_verifier

    params = {
        "response_type": "code",
        "client_id": settings.auth0_client_id,
        "redirect_uri": auth0_callback_url(settings),
        "scope": "openid profile email offline_access",
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    if settings.auth0_audience:
        params["audience"] = settings.auth0_audience
    return f"{auth0_domain(settings)}/authorize?{urllib.parse.urlencode(params)}"


def build_auth0_logout_url(settings: Settings) -> str:
    params = {
        "client_id": settings.auth0_client_id,
        "returnTo": f"{settings.app_base_url}/admin-ui/login",
    }
    return f"{auth0_domain(settings)}/v2/logout?{urllib.parse.urlencode(params)}"


def consume_return_to(request: Request, settings: Settings) -> str:
    return safe_return_to(settings, request.session.pop(RETURN_TO_KEY, None))


def get_code_verifier(request: Request) -> str | None:
    value = request.session.get(CODE_VERIFIER_KEY)
    return str(value) if value else None


async def exchange_auth0_code(
    settings: Settings, code: str, *, code_verifier: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "grant_type": "authorization_code",
        "client_id": settings.auth0_client_id,
        "code": code,
        "redirect_uri": auth0_callback_url(settings),
    }
    if settings.auth0_client_secret:
        payload["client_secret"] = settings.auth0_client_secret
    if code_verifier:
        payload["code_verifier"] = code_verifier

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{auth0_domain(settings)}/oauth/token",
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def jwks_url(settings: Settings) -> str:
    return f"{auth0_domain(settings)}/.well-known/jwks.json"


def decode_and_verify_jwt(settings: Settings, token: str, *, audience: str) -> dict[str, Any]:
    signing_key = PyJWKClient(jwks_url(settings)).get_signing_key_from_jwt(token)
    claims = jwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience=audience,
        issuer=auth0_issuer(settings),
        options={"verify_exp": True},
    )
    if not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="Invalid Auth0 claims")
    return claims


def decode_unverified_claims(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return {}


def _claim_values(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("[") and stripped.endswith("]"):
            try:
                parsed = json.loads(stripped)
                return _claim_values(parsed)
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in stripped.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            values.extend(_claim_values(item))
        return values
    return [str(value)]


def extract_roles(claims: dict[str, Any]) -> list[str]:
    roles: list[str] = []
    for key, value in claims.items():
        normalized_key = key.lower()
        if (
            normalized_key in {"role", "roles", "permissions", "permission"}
            or "role" in normalized_key
            or "permission" in normalized_key
        ):
            for role in _claim_values(value):
                if role and role not in roles:
                    roles.append(role)
    return roles


def has_allowed_role(roles: list[str], allowed_roles: list[str]) -> bool:
    if not allowed_roles:
        return True
    if any(role.strip() == "*" for role in allowed_roles):
        return True
    normalized_roles = {role.lower() for role in roles}
    return any(role.lower() in normalized_roles for role in allowed_roles)


def has_role(user: AdminUser | None, role: str) -> bool:
    if not user:
        return False
    return role.lower() in {item.lower() for item in user.roles}


def is_admin(user: AdminUser | None, settings: Settings | None = None) -> bool:
    if not user:
        return False
    if user.auth_method in {"admin_token", "development"}:
        return True
    if settings is not None:
        return has_allowed_role(user.roles, settings.admin_allowed_roles)
    return has_role(user, "Admin")


def is_support(user: AdminUser | None) -> bool:
    return bool(user and has_role(user, "Support"))


def is_privileged(user: AdminUser | None, settings: Settings) -> bool:
    return bool(user and (is_admin(user, settings) or is_support(user)))


def nicky_user_uuid(user: AdminUser) -> str:
    for key in (
        "nicky_user_uuid",
        "nickyUserUuid",
        "nicky_user_id",
        "nickyUserId",
        "user_uuid",
        "userUuid",
    ):
        value = user.claims.get(key)
        if value:
            return str(value)
    for key, value in user.claims.items():
        normalized = key.lower().replace("-", "_")
        if "nicky" in normalized and ("uuid" in normalized or "user_id" in normalized):
            if value:
                return str(value)
    return user.subject.replace("|", "_").replace(":", "_")


def nicky_user_short_id(user: AdminUser) -> str:
    for key in (
        "nicky_user_short_id",
        "nickyUserShortId",
        "nicky_short_id",
        "nickyShortId",
        "short_id",
        "shortId",
    ):
        value = user.claims.get(key)
        if value:
            return str(value)
    for key, value in user.claims.items():
        normalized = key.lower().replace("-", "_")
        if "nicky" in normalized and "short" in normalized and value:
            return str(value)
    return ""


def user_from_claims(claims: dict[str, Any], *, auth_method: str) -> AdminUser:
    return AdminUser(
        subject=str(claims.get("sub") or ""),
        name=str(claims.get("name") or claims.get("nickname") or claims.get("email") or "Admin"),
        email=str(claims.get("email") or ""),
        roles=extract_roles(claims),
        claims=claims,
        auth_method=auth_method,
    )


def user_to_session(user: AdminUser) -> dict[str, Any]:
    return {
        "subject": user.subject,
        "name": user.name,
        "email": user.email,
        "roles": user.roles,
        "claims": user.claims,
        "auth_method": user.auth_method,
        "stored_at": int(time.time()),
    }


def user_from_session(data: Any) -> AdminUser | None:
    if not isinstance(data, dict):
        return None
    claims = data.get("claims") if isinstance(data.get("claims"), dict) else {}
    expires_at = claims.get("exp")
    if expires_at:
        try:
            if int(expires_at) <= int(time.time()):
                return None
        except (TypeError, ValueError):
            return None
    return AdminUser(
        subject=str(data.get("subject") or ""),
        name=str(data.get("name") or "Admin"),
        email=str(data.get("email") or ""),
        roles=[str(role) for role in data.get("roles") or []],
        claims=claims,
        auth_method=str(data.get("auth_method") or "session"),
    )


def get_session_user(request: Request) -> AdminUser | None:
    session_data = request.session.get(SESSION_KEY)
    user = user_from_session(session_data)
    if session_data is not None and user is None:
        request.session.pop(SESSION_KEY, None)
    return user


def set_session_user(request: Request, user: AdminUser) -> None:
    request.session[SESSION_KEY] = user_to_session(user)


def clear_session(request: Request) -> None:
    for key in (SESSION_KEY, STATE_KEY, NONCE_KEY, RETURN_TO_KEY, CODE_VERIFIER_KEY):
        request.session.pop(key, None)


def validate_callback_state(request: Request, supplied_state: str | None) -> str:
    expected_state = request.session.get(STATE_KEY)
    if not expected_state or supplied_state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid Auth0 state")
    nonce = request.session.get(NONCE_KEY)
    if not nonce:
        raise HTTPException(status_code=400, detail="Missing Auth0 nonce")
    return str(nonce)


def assert_nonce(claims: dict[str, Any], expected_nonce: str) -> None:
    if claims.get("nonce") != expected_nonce:
        raise HTTPException(status_code=400, detail="Invalid Auth0 nonce")


def make_admin_token_user() -> AdminUser:
    return AdminUser(
        subject="admin-token",
        name="Admin token",
        email="",
        roles=["Admin"],
        claims={"sub": "admin-token", "roles": ["Admin"]},
        auth_method="admin_token",
    )


def make_development_admin_user() -> AdminUser:
    return AdminUser(
        subject="development",
        name="Development admin",
        email="",
        roles=["Admin"],
        claims={"sub": "development", "roles": ["Admin"]},
        auth_method="development",
    )


def authenticate_admin_request(
    settings: Settings,
    request: Request,
    *,
    x_admin_token: str | None = None,
    authorization: str | None = None,
) -> AdminUser | None:
    if settings.admin_token and x_admin_token == settings.admin_token:
        return make_admin_token_user()

    session_user = get_session_user(request)
    if session_user:
        return session_user

    bearer_token = bearer_token_from_header(authorization)
    if bearer_token and auth0_enabled(settings):
        audience = settings.auth0_audience or settings.auth0_client_id
        claims = decode_and_verify_jwt(settings, bearer_token, audience=audience)
        return user_from_claims(claims, auth_method="bearer")

    return None


def bearer_token_from_header(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()
