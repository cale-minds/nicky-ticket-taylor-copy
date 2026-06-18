from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _csv_env(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _path_env(name: str, default: str) -> str:
    raw = os.getenv(name, default).strip()
    if not raw or raw == "/":
        return ""
    return raw if raw.startswith("/") else f"/{raw}"


def sqlite_url_from_path(path: Path) -> str:
    resolved = path.expanduser().resolve()
    return f"sqlite:///{resolved.as_posix()}"


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    app_base_url: str = os.getenv("APP_BASE_URL", "http://localhost:8017").rstrip("/")
    api_base_path: str = _path_env("API_BASE_PATH", "/api")
    admin_api_base_path: str = _path_env(
        "ADMIN_API_BASE_PATH", os.getenv("API_BASE_PATH", "/api")
    )
    database_url: str = os.getenv("DATABASE_URL", "").strip()
    database_path: Path = Path(os.getenv("DATABASE_PATH", "./data/integration.sqlite3"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    run_background_jobs: bool = _bool_env("RUN_BACKGROUND_JOBS", False)
    job_runner_token: str = os.getenv("JOB_RUNNER_TOKEN", "")

    ticket_tailor_api_key: str = os.getenv("TICKET_TAILOR_API_KEY", "")
    ticket_tailor_webhook_signing_secret: str = ""
    ticket_tailor_webhook_tolerance_seconds: int = _int_env(
        "TICKET_TAILOR_WEBHOOK_TOLERANCE_SECONDS", 300
    )
    ticket_tailor_pending_ticket_expiration_hours: float = _float_env(
        "TICKET_TAILOR_PENDING_TICKET_EXPIRATION_HOURS", 0
    )
    ticket_tailor_expiration_check_interval_seconds: int = _int_env(
        "TICKET_TAILOR_EXPIRATION_CHECK_INTERVAL_SECONDS", 300
    )
    ticket_tailor_expiration_batch_size: int = _int_env(
        "TICKET_TAILOR_EXPIRATION_BATCH_SIZE", 100
    )

    nicky_api_base_url: str = os.getenv(
        "NICKY_API_BASE_URL", "https://api-public.pay.nicky.me"
    ).rstrip("/")
    nicky_pay_base_url: str = os.getenv("NICKY_PAY_BASE_URL", "https://pay.nicky.me").rstrip("/")
    nicky_api_key: str = os.getenv("NICKY_API_KEY", "")
    nicky_default_blockchain_asset_id: str = os.getenv(
        "NICKY_DEFAULT_BLOCKCHAIN_ASSET_ID", ""
    )
    nicky_receiver_short_id: str = os.getenv("NICKY_RECEIVER_SHORT_ID", "")
    nicky_success_url: str = os.getenv("NICKY_SUCCESS_URL", "")
    nicky_cancel_url: str = os.getenv("NICKY_CANCEL_URL", "")
    nicky_webhook_token: str = ""

    admin_session_secret: str = os.getenv(
        "ADMIN_SESSION_SECRET", "development-admin-session-secret"
    )
    admin_session_max_age_seconds: int = _int_env("ADMIN_SESSION_MAX_AGE_SECONDS", 28800)
    auth0_domain: str = os.getenv("AUTH0_DOMAIN", "").strip().rstrip("/")
    auth0_client_id: str = os.getenv("AUTH0_CLIENT_ID", "")
    auth0_client_secret: str = os.getenv("AUTH0_CLIENT_SECRET", "")
    auth0_audience: str = os.getenv("AUTH0_AUDIENCE", "")
    auth0_callback_path: str = os.getenv("AUTH0_CALLBACK_PATH", "/admin-ui/callback")
    admin_allowed_roles: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "admin_allowed_roles",
            list(self.admin_allowed_roles)
            if self.admin_allowed_roles is not None
            else _csv_env("ADMIN_ALLOWED_ROLES", ["Admin"]),
        )

    @property
    def resolved_database_url(self) -> str:
        return self.database_url or sqlite_url_from_path(self.database_path)


def get_settings() -> Settings:
    return Settings()


def external_api_url(settings: Settings, path: str) -> str:
    normalized_path = path if path.startswith("/") else f"/{path}"
    return f"{settings.app_base_url}{settings.api_base_path}{normalized_path}"
