from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.tenants import TenantConfig


class NickyClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def configured(self, tenant: TenantConfig) -> bool:
        return tenant.nicky_configured

    async def create_payment_request(
        self, tenant: TenantConfig, order: dict[str, Any]
    ) -> dict[str, Any]:
        if tenant.skip_nicky:
            return {
                "id": f"skip-nicky-{order['ticket_tailor_order_id']}",
                "status": "PaymentPending",
                "blockchainAssetId": tenant.nicky_default_blockchain_asset_id
                or "skip-nicky-asset",
                "amountExpectedNative": self._native_amount(order),
                "bill": {
                    "shortId": f"tt-{order['ticket_tailor_order_id']}",
                    "receiverUser": {
                        "shortId": tenant.nicky_receiver_short_id or "skip-nicky"
                    },
                },
                "billDetails": {
                    "invoiceReference": order["ticket_tailor_order_id"],
                    "description": f"Ticket Tailor order {order['ticket_tailor_order_id']}",
                },
                "requester": {
                    "email": order.get("buyer_email") or "unknown@example.invalid",
                    "name": order.get("buyer_name"),
                },
                "sendNotification": False,
                "successUrl": self._success_url(tenant),
                "cancelUrl": self._cancel_url(tenant),
                "simulated": True,
            }

        if tenant.dry_run or not self.configured(tenant):
            return {
                "id": f"dry-run-{order['ticket_tailor_order_id']}",
                "status": "DRY_RUN",
                "bill": {"shortId": f"tt-{order['ticket_tailor_order_id']}"},
            }

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
            "sendNotification": tenant.nicky_send_notification,
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
        if tenant.dry_run or not tenant.nicky_api_key:
            return {
                "id": "dry-run-webhook",
                "webHookType": tenant.nicky_webhook_type,
                "url": url,
            }

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.settings.nicky_api_base_url}/api/public/WebHookApi/create",
                headers={"X-API-KEY": tenant.nicky_api_key},
                json={"webHookType": tenant.nicky_webhook_type, "url": url},
            )
            response.raise_for_status()
            return response.json()

    async def test_status_change_webhook(self, tenant: TenantConfig) -> dict[str, Any]:
        if tenant.dry_run or not tenant.nicky_api_key:
            return {"status": "dry_run"}

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.settings.nicky_api_base_url}/api/public/WebHookApi/test-status-change",
                headers={"X-API-KEY": tenant.nicky_api_key},
            )
            response.raise_for_status()
            return response.json()

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
