from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.tenants import NICKY_WEBHOOK_TYPE, TenantConfig


class NickyClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def configured(self, tenant: TenantConfig) -> bool:
        return tenant.nicky_configured

    async def create_payment_request(
        self, tenant: TenantConfig, order: dict[str, Any]
    ) -> dict[str, Any]:
        if not self.configured(tenant):
            raise ValueError("Nicky tenant is not configured")

        body = {
            "blockchainAssetId": tenant.nicky_default_blockchain_asset_id,
            "amountExpectedNative": self._native_amount(order),
            "billDetails": {
                "invoiceReference": order["ticket_tailor_order_id"],
                "description": f"Ticket Tailor order {order['ticket_tailor_order_id']}",
            },
            "requester": {
                "email": order.get("buyer_email") or "unknown@example.invalid",
                "name": order.get("buyer_name"),
            },
            "sendNotification": True,
            "successUrl": self._success_url(tenant),
            "cancelUrl": self._cancel_url(tenant),
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.settings.nicky_api_base_url}/api/public/PaymentRequestPublicApi/create",
                headers={"X-API-KEY": tenant.nicky_api_key},
                json=body,
            )
            response.raise_for_status()
            return response.json()

    async def get_payment_request(
        self, tenant: TenantConfig, payment_request_id: str
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.settings.nicky_api_base_url}/api/public/PaymentRequestPublicApi/get-by-id",
                headers={"X-API-KEY": tenant.nicky_api_key},
                params={"id": payment_request_id},
            )
            response.raise_for_status()
            return response.json()

    async def create_webhook(self, tenant: TenantConfig, url: str) -> dict[str, Any]:
        if not tenant.nicky_api_key:
            raise ValueError("Nicky API key is required to create webhook")

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.settings.nicky_api_base_url}/api/public/WebHookApi/create",
                headers={"X-API-KEY": tenant.nicky_api_key},
                json={"webHookType": NICKY_WEBHOOK_TYPE, "url": url},
            )
            response.raise_for_status()
            return response.json()

    async def test_status_change_webhook(self, tenant: TenantConfig) -> dict[str, Any]:
        if not tenant.nicky_api_key:
            raise ValueError("Nicky API key is required to test webhook")

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.settings.nicky_api_base_url}/api/public/WebHookApi/test-status-change",
                headers={"X-API-KEY": tenant.nicky_api_key},
            )
            response.raise_for_status()
            return response.json()

    async def validate_api_key(self, api_key: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.settings.nicky_api_base_url}/AcceptedAsset/get-for-user",
                headers={"X-API-KEY": api_key},
            )
            response.raise_for_status()
            payload = response.json()
        return {
            "nicky_user_uuid": self._extract_user_uuid(payload),
            "nicky_user_short_id": self._extract_user_short_id(payload),
            "assets": self._extract_assets(payload),
            "raw": payload,
        }

    def _success_url(self, tenant: TenantConfig) -> str | None:
        if self.settings.nicky_success_url:
            return self.settings.nicky_success_url
        return f"{self.settings.app_base_url}/nicky/success?tenant_id={tenant.tenant_id}"

    def _cancel_url(self, tenant: TenantConfig) -> str | None:
        if self.settings.nicky_cancel_url:
            return self.settings.nicky_cancel_url
        return f"{self.settings.app_base_url}/nicky/cancel?tenant_id={tenant.tenant_id}"

    @staticmethod
    def _native_amount(order: dict[str, Any]) -> str:
        amount_minor = order.get("amount_minor")
        if amount_minor is None:
            raise ValueError("Order does not include an amount")
        return f"{int(amount_minor) / 100:.2f}"

    @staticmethod
    def _extract_assets(payload: Any) -> list[dict[str, str]]:
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict):
            candidates = []
            for key in ("data", "items", "assets", "acceptedAssets", "accepted_assets"):
                value = payload.get(key)
                if isinstance(value, list):
                    candidates = value
                    break
            if not candidates:
                candidates = [payload]
        else:
            candidates = []

        assets: list[dict[str, str]] = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            asset_id = item.get("blockchainAssetId") or item.get("assetId") or item.get("id")
            if not asset_id:
                continue
            name = item.get("name") or item.get("symbol") or item.get("ticker") or asset_id
            assets.append({"id": str(asset_id), "name": str(name)})
        return assets

    @classmethod
    def _extract_user_uuid(cls, payload: Any) -> str:
        user = cls._find_user_object(payload)
        if not user:
            return ""
        for key in ("uuid", "id", "userId", "user_id", "userUuid", "user_uuid"):
            value = user.get(key)
            if value:
                return str(value)
        return ""

    @classmethod
    def _extract_user_short_id(cls, payload: Any) -> str:
        user = cls._find_user_object(payload)
        if not user:
            return ""
        for key in ("shortId", "short_id", "publicName", "public_name"):
            value = user.get(key)
            if value:
                return str(value)
        return ""

    @classmethod
    def _find_user_object(cls, value: Any, parent_key: str = "") -> dict[str, Any] | None:
        if isinstance(value, dict):
            normalized_parent = parent_key.lower()
            if any(token in normalized_parent for token in ("user", "owner", "receiver", "account")):
                if any(key in value for key in ("id", "uuid", "userId", "userUuid", "shortId", "short_id")):
                    return value
            for key in ("user", "owner", "receiverUser", "receiver_user", "account"):
                nested = value.get(key)
                if isinstance(nested, dict):
                    return nested
            for key, item in value.items():
                found = cls._find_user_object(item, key)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_user_object(item, parent_key)
                if found:
                    return found
        return None
