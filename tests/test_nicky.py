import pytest

from app.config import Settings
from app.nicky import NickyClient


class ProfileFallbackNickyClient(NickyClient):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.calls: list[tuple[str, str]] = []

    async def _request_json(self, method: str, path: str, **kwargs):
        self.calls.append((method, path))
        if path == "/AcceptedAsset/get-for-user":
            return [
                {
                    "id": "USD.USD",
                    "assetName": "US Dollar",
                    "assetTicker": "USD",
                }
            ]
        if path == "/api/public/PaymentRequestPublicApi/all":
            return {
                "total": 1,
                "data": [
                    {
                        "bill": {
                            "receiverUser": {
                                "id": "nicky-user-uuid",
                                "email": "owner@example.com",
                                "publicName": "Nicky Public Name",
                            }
                        }
                    }
                ],
            }
        raise AssertionError(f"Unexpected path {path}")


@pytest.mark.asyncio
async def test_validate_api_key_uses_payment_request_profile_fallback() -> None:
    client = ProfileFallbackNickyClient(Settings())

    result = await client.validate_api_key("api-key")

    assert result["nicky_user_uuid"] == "nicky-user-uuid"
    assert result["nicky_user_short_id"] == "Nicky Public Name"
    assert result["nicky_user_email"] == "owner@example.com"
    assert result["assets"] == [{"id": "USD.USD", "name": "US Dollar"}]
    assert client.calls == [
        ("GET", "/AcceptedAsset/get-for-user"),
        ("POST", "/api/public/PaymentRequestPublicApi/all"),
    ]
