from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings, external_api_url
from app.tenants import NICKY_WEBHOOK_TYPE, TenantConfig


class NickyApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
        return await self._request_json(
            "POST",
            "/api/public/PaymentRequestPublicApi/create",
            api_key=tenant.nicky_api_key,
            operation="create Nicky payment request",
            json=body,
        )

    async def get_payment_request(
        self, tenant: TenantConfig, payment_request_id: str
    ) -> dict[str, Any]:
        return await self._request_json(
            "GET",
            "/api/public/PaymentRequestPublicApi/get-by-id",
            api_key=tenant.nicky_api_key,
            operation="get Nicky payment request",
            params={"id": payment_request_id},
        )

    async def create_webhook(self, tenant: TenantConfig, url: str) -> dict[str, Any]:
        if not tenant.nicky_api_key:
            raise ValueError("Nicky API key is required to create webhook")

        response = await self._request_json(
            "POST",
            "/api/public/WebHookApi/create",
            api_key=tenant.nicky_api_key,
            operation="create Nicky webhook",
            json={"webHookType": NICKY_WEBHOOK_TYPE, "url": url},
        )
        webhook_id = self._extract_webhook_id(response)
        return {"webhook_id": webhook_id, "raw": response}

    async def delete_webhook(self, api_key: str, webhook_id: str) -> None:
        if not api_key or not webhook_id:
            return
        await self._request_json(
            "POST",
            "/api/public/WebHookApi/delete",
            api_key=api_key,
            operation="delete Nicky webhook",
            params={"id": webhook_id},
        )

    async def list_webhooks(self, api_key: str) -> list[dict[str, Any]]:
        if not api_key:
            return []
        response = await self._request_json(
            "GET",
            "/api/public/WebHookApi/list",
            api_key=api_key,
            operation="list Nicky webhooks",
        )
        if isinstance(response, list):
            return response
        if isinstance(response, dict):
            for key in ("data", "items", "webhooks", "webHooks"):
                value = response.get(key)
                if isinstance(value, list):
                    return value
        return []

    async def test_status_change_webhook(self, tenant: TenantConfig) -> dict[str, Any]:
        if not tenant.nicky_api_key:
            raise ValueError("Nicky API key is required to test webhook")

        return await self._request_json(
            "POST",
            "/api/public/WebHookApi/test-status-change",
            api_key=tenant.nicky_api_key,
            operation="test Nicky webhook",
        )

    async def validate_api_key(self, api_key: str) -> dict[str, Any]:
        assets_payload = await self._request_json(
            "GET",
            "/AcceptedAsset/get-for-user",
            api_key=api_key,
            operation="validate Nicky API key",
        )
        user_payload: Any = assets_payload
        if not self._extract_user_uuid(user_payload):
            user_payload = await self._request_json(
                "POST",
                "/api/public/PaymentRequestPublicApi/all",
                api_key=api_key,
                operation="load Nicky user profile",
                json={"pageIndex": 0, "pageSize": 1},
            )
        return {
            "nicky_user_uuid": self._extract_user_uuid(user_payload),
            "nicky_user_short_id": self._extract_user_short_id(user_payload),
            "nicky_user_email": self._extract_user_email(user_payload),
            "assets": self._extract_assets(assets_payload),
            "raw": assets_payload,
        }

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        api_key: str,
        operation: str,
        **request_kwargs: Any,
    ) -> Any:
        url = f"{self.settings.nicky_api_base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(
                    method,
                    url,
                    headers={"X-API-KEY": api_key},
                    **request_kwargs,
                )
        except httpx.RequestError as exc:
            raise NickyApiError(
                f"Could not {operation}: {exc.__class__.__name__}",
            ) from exc

        if response.status_code >= 400:
            raise NickyApiError(
                self._error_message(operation, response),
                status_code=response.status_code,
            )

        # Some endpoints (e.g. webhook delete) return 200 with an empty body.
        if not response.content or not response.content.strip():
            return None

        try:
            return response.json()
        except ValueError as exc:
            raise NickyApiError(
                f"Could not {operation}: Nicky returned a non-JSON response",
                status_code=response.status_code,
            ) from exc

    @staticmethod
    def _error_message(operation: str, response: httpx.Response) -> str:
        reason = response.reason_phrase or "HTTP error"
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail_value = (
                    payload.get("detail")
                    or payload.get("message")
                    or payload.get("error")
                    or payload.get("title")
                )
                if detail_value:
                    detail = f": {detail_value}"
        except ValueError:
            body = response.text.strip()
            if body:
                detail = f": {body[:180]}"
        return f"Could not {operation}: Nicky returned {response.status_code} {reason}{detail}"

    def _success_url(self, tenant: TenantConfig) -> str | None:
        if self.settings.nicky_success_url:
            return self.settings.nicky_success_url
        return external_api_url(self.settings, f"/nicky/success?tenant_id={tenant.tenant_id}")

    def _cancel_url(self, tenant: TenantConfig) -> str | None:
        if self.settings.nicky_cancel_url:
            return self.settings.nicky_cancel_url
        return external_api_url(self.settings, f"/nicky/cancel?tenant_id={tenant.tenant_id}")

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
            name = (
                item.get("name")
                or item.get("assetName")
                or item.get("symbol")
                or item.get("ticker")
                or item.get("assetTicker")
                or asset_id
            )
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
    def _extract_user_email(cls, payload: Any) -> str:
        user = cls._find_user_object(payload)
        if not user:
            return ""
        for key in ("email", "userEmail", "user_email"):
            value = user.get(key)
            if value:
                return str(value)
        return ""

    @staticmethod
    def _extract_webhook_id(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        for key in ("id", "Id", "webhookId", "webhook_id", "hookId", "webHookId"):
            value = payload.get(key)
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
