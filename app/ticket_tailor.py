from __future__ import annotations

import httpx

from app.config import Settings
from app.tenants import TenantConfig


TICKET_TAILOR_API_BASE_URL = "https://api.tickettailor.com"


class TicketTailorClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def configured(self, tenant: TenantConfig) -> bool:
        return tenant.ticket_tailor_configured

    async def validate_api_key(self, api_key: str) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{TICKET_TAILOR_API_BASE_URL}/v1/issued_tickets",
                headers={"Accept": "application/json"},
                auth=(api_key, ""),
                params={"limit": 1},
            )
            response.raise_for_status()
            payload = response.json() if response.content else {}
        total = None
        if isinstance(payload, dict):
            for key in ("total", "total_count", "count"):
                if key in payload:
                    total = payload.get(key)
                    break
        return {"status": "valid", "total": total}

    async def confirm_offline_payment(
        self, tenant: TenantConfig, order_id: str
    ) -> dict[str, str]:
        if not self.configured(tenant):
            raise ValueError("Ticket Tailor tenant is not configured")

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{TICKET_TAILOR_API_BASE_URL}/v1/orders/{order_id}/confirm-payment-received",
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
        if not self.configured(tenant):
            raise ValueError("Ticket Tailor tenant is not configured")

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{TICKET_TAILOR_API_BASE_URL}/v1/issued_tickets",
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
        if not self.configured(tenant):
            raise ValueError("Ticket Tailor tenant is not configured")

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{TICKET_TAILOR_API_BASE_URL}/v1/issued_tickets/{issued_ticket_id}/void",
                headers={"Accept": "application/json"},
                auth=(tenant.ticket_tailor_api_key, ""),
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return {"status": "voided", "issued_ticket_id": issued_ticket_id}
