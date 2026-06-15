from __future__ import annotations

import httpx

from app.config import Settings
from app.tenants import TenantConfig


class TicketTailorClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def configured(self, tenant: TenantConfig) -> bool:
        return tenant.ticket_tailor_configured

    async def confirm_offline_payment(
        self, tenant: TenantConfig, order_id: str
    ) -> dict[str, str]:
        if tenant.dry_run or not self.configured(tenant):
            return {"status": "dry_run", "order_id": order_id}

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.settings.ticket_tailor_api_base_url}/v1/orders/{order_id}/confirm-payment-received",
                headers={"Accept": "application/json"},
                auth=(tenant.ticket_tailor_api_key, ""),
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return {"status": "confirmed", "order_id": order_id}

    async def list_issued_tickets_for_order(
        self, tenant: TenantConfig, order_id: str
    ) -> list[dict[str, object]]:
        if tenant.dry_run or not self.configured(tenant):
            return []

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.settings.ticket_tailor_api_base_url}/v1/issued_tickets",
                headers={"Accept": "application/json"},
                auth=(tenant.ticket_tailor_api_key, ""),
                params={"order_id": order_id},
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, dict):
                for key in ("data", "items", "issued_tickets"):
                    value = payload.get(key)
                    if isinstance(value, list):
                        return [item for item in value if isinstance(item, dict)]
            return []

    async def void_issued_ticket(
        self, tenant: TenantConfig, issued_ticket_id: str
    ) -> dict[str, str]:
        if tenant.dry_run or not self.configured(tenant):
            return {"status": "dry_run", "issued_ticket_id": issued_ticket_id}

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.settings.ticket_tailor_api_base_url}/v1/issued_tickets/{issued_ticket_id}/void",
                headers={"Accept": "application/json"},
                auth=(tenant.ticket_tailor_api_key, ""),
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return {"status": "voided", "issued_ticket_id": issued_ticket_id}
