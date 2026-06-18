import pytest

from app.config import Settings
from app.job_runner import is_authorized_job_request, run_expire_overdue_orders


def test_job_runner_requires_matching_bearer_token() -> None:
    settings = Settings(job_runner_token="secret-token")

    assert is_authorized_job_request(settings, "Bearer secret-token") is True
    assert is_authorized_job_request(settings, "Bearer wrong-token") is False
    assert is_authorized_job_request(settings, "Basic secret-token") is False
    assert is_authorized_job_request(settings, None) is False


def test_job_runner_is_disabled_when_token_is_not_configured() -> None:
    settings = Settings(job_runner_token="")

    assert is_authorized_job_request(settings, "Bearer secret-token") is False


@pytest.mark.asyncio
async def test_expire_overdue_orders_runner_uses_runtime_settings(tmp_path) -> None:
    settings = Settings(
        database_path=tmp_path / "integration.sqlite3",
        ticket_tailor_pending_ticket_expiration_hours=0,
    )

    result = await run_expire_overdue_orders(settings=settings)

    assert result["status"] == "disabled"
    assert result["expired_count"] == 0
