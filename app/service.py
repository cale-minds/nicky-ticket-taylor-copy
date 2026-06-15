from __future__ import annotations

import json
from typing import Any

from app.config import Settings
from app.db import Database
from app.extractors import extract_issued_ticket_ids, extract_order, has_payment_keyword
from app.nicky import NickyClient
from app.tenants import TenantConfig
from app.ticket_tailor import TicketTailorClient


FINISHED_NICKY_STATUS = "finished"


class IntegrationService:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        nicky: NickyClient,
        ticket_tailor: TicketTailorClient,
    ) -> None:
        self.settings = settings
        self.db = db
        self.nicky = nicky
        self.ticket_tailor = ticket_tailor

    async def process_ticket_tailor_webhook(
        self, tenant: TenantConfig, event: dict[str, Any], raw_body: bytes
    ) -> dict[str, Any]:
        webhook_id = str(event.get("id") or "")
        event_type = str(event.get("event") or "UNKNOWN")
        if not webhook_id:
            raise ValueError("Ticket Tailor webhook is missing id")

        inserted = self.db.insert_webhook_event(
            tenant_id=tenant.tenant_id,
            source="ticket_tailor",
            event_id=webhook_id,
            event_type=event_type,
            raw_body=raw_body,
        )
        if not inserted:
            return {"status": "duplicate", "event_id": webhook_id}

        try:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("Ticket Tailor webhook payload must be an object")

            order = extract_order(payload)
            if not order["ticket_tailor_order_id"]:
                order["ticket_tailor_order_id"] = self._order_id_from_resource_url(
                    event.get("resource_url")
                )
            if not order["ticket_tailor_order_id"]:
                raise ValueError("Could not identify Ticket Tailor order id")

            is_nicky_order = has_payment_keyword(
                payload, tenant.ticket_tailor_offline_payment_keywords
            )
            if not is_nicky_order:
                self.db.mark_webhook_event(
                    tenant.tenant_id, "ticket_tailor", webhook_id, "ignored"
                )
                return {
                    "status": "ignored",
                    "reason": "payment method does not match configured Nicky keywords",
                    "tenant_id": tenant.tenant_id,
                    "order_id": order["ticket_tailor_order_id"],
                }

            self.db.upsert_order(tenant.tenant_id, order, payload)
            self.db.log(
                tenant.tenant_id,
                order["ticket_tailor_order_id"],
                event_type,
                "Ticket Tailor order received",
                {"webhook_id": webhook_id},
            )

            created_payment_request = None
            row = self.db.get_order(tenant.tenant_id, order["ticket_tailor_order_id"])
            if (
                tenant.auto_create_nicky_payment_request
                and row
                and not row["nicky_payment_request_id"]
            ):
                created_payment_request = await self.create_nicky_payment_request(
                    tenant, order["ticket_tailor_order_id"]
                )

            self.db.mark_webhook_event(
                tenant.tenant_id, "ticket_tailor", webhook_id, "processed"
            )
            return {
                "status": "processed",
                "tenant_id": tenant.tenant_id,
                "order_id": order["ticket_tailor_order_id"],
                "nicky_payment_request": created_payment_request,
            }
        except Exception as exc:
            self.db.mark_webhook_event(
                tenant.tenant_id, "ticket_tailor", webhook_id, "failed", str(exc)
            )
            raise

    async def process_nicky_webhook(
        self, tenant: TenantConfig, event: dict[str, Any], raw_body: bytes
    ) -> dict[str, Any]:
        event_id = self._nicky_event_id(event)
        event_type = str(event.get("webHookType") or event.get("type") or "UNKNOWN")
        inserted = self.db.insert_webhook_event(
            tenant_id=tenant.tenant_id,
            source="nicky",
            event_id=event_id,
            event_type=event_type,
            raw_body=raw_body,
        )
        if not inserted:
            return {"status": "duplicate", "event_id": event_id}

        try:
            payment_request_id = self._nicky_payment_request_id(event)
            if not payment_request_id:
                raise ValueError("Could not identify Nicky payment request id")

            new_status = self._nicky_status(event)
            row = self.db.update_nicky_status(tenant.tenant_id, payment_request_id, new_status)
            if not row:
                self.db.mark_webhook_event(tenant.tenant_id, "nicky", event_id, "orphaned")
                return {
                    "status": "orphaned",
                    "tenant_id": tenant.tenant_id,
                    "payment_request_id": payment_request_id,
                    "nicky_status": new_status,
                }

            order_id = row["ticket_tailor_order_id"]
            self.db.log(
                tenant.tenant_id,
                order_id,
                event_type,
                f"Nicky status changed to {new_status}",
                event,
            )

            confirmed = None
            voided = None
            if self._is_paid(new_status):
                if (
                    tenant.auto_confirm_ticket_tailor_payments
                    and not row["ticket_tailor_tickets_voided_at"]
                ):
                    confirmed = await self.confirm_ticket_tailor_payment(tenant, order_id)
            elif not row["ticket_tailor_confirmed_at"]:
                voided = await self.void_ticket_tailor_tickets(
                    tenant,
                    order_id,
                    reason=f"nicky_status:{new_status}",
                )

            self.db.mark_webhook_event(tenant.tenant_id, "nicky", event_id, "processed")
            return {
                "status": "processed",
                "tenant_id": tenant.tenant_id,
                "order_id": order_id,
                "nicky_status": new_status,
                "ticket_tailor_confirmation": confirmed,
                "ticket_tailor_void": voided,
            }
        except Exception as exc:
            self.db.mark_webhook_event(
                tenant.tenant_id, "nicky", event_id, "failed", str(exc)
            )
            raise

    async def create_nicky_payment_request(
        self, tenant: TenantConfig, ticket_tailor_order_id: str
    ) -> dict[str, Any]:
        row = self.db.get_order(tenant.tenant_id, ticket_tailor_order_id)
        if not row:
            raise ValueError("Order not found")
        order = dict(row)
        payment_request = await self.nicky.create_payment_request(tenant, order)
        payment_request_id = str(payment_request.get("id") or "")
        bill_short_id = self._bill_short_id(payment_request)
        receiver_short_id = self._receiver_short_id(tenant, payment_request)
        payment_url = self._payment_url(receiver_short_id, bill_short_id)
        status = str(payment_request.get("status") or "")
        self.db.update_nicky_payment_request(
            tenant_id=tenant.tenant_id,
            ticket_tailor_order_id=ticket_tailor_order_id,
            payment_request_id=payment_request_id,
            bill_short_id=bill_short_id,
            receiver_short_id=receiver_short_id,
            payment_url=payment_url,
            status=status,
        )
        self.db.log(
            tenant.tenant_id,
            ticket_tailor_order_id,
            "NICKY.PAYMENT_REQUEST.CREATED",
            "Nicky payment request created",
            payment_request,
        )
        simulated_webhook_result = None
        if tenant.skip_nicky:
            simulated_webhook_result = await self._simulate_nicky_finished_webhook(
                tenant, payment_request_id
            )
        return {
            "tenant_id": tenant.tenant_id,
            "id": payment_request_id,
            "bill_short_id": bill_short_id,
            "receiver_short_id": receiver_short_id,
            "payment_url": payment_url,
            "status": status,
            "simulated_nicky_webhook": simulated_webhook_result,
        }

    async def confirm_ticket_tailor_payment(
        self, tenant: TenantConfig, ticket_tailor_order_id: str
    ) -> dict[str, Any]:
        result = await self.ticket_tailor.confirm_offline_payment(
            tenant, ticket_tailor_order_id
        )
        self.db.mark_ticket_tailor_confirmed(tenant.tenant_id, ticket_tailor_order_id)
        self.db.log(
            tenant.tenant_id,
            ticket_tailor_order_id,
            "TICKET_TAILOR.PAYMENT.CONFIRMED",
            "Ticket Tailor offline payment confirmed",
            result,
        )
        return result

    async def void_ticket_tailor_tickets(
        self, tenant: TenantConfig, ticket_tailor_order_id: str, *, reason: str
    ) -> dict[str, Any]:
        row = self.db.get_order(tenant.tenant_id, ticket_tailor_order_id)
        if not row:
            raise ValueError("Order not found")
        if row["ticket_tailor_confirmed_at"]:
            return {
                "status": "skipped",
                "reason": "ticket_tailor_order_already_confirmed",
                "order_id": ticket_tailor_order_id,
            }
        if row["ticket_tailor_tickets_voided_at"]:
            return {
                "status": "already_voided",
                "reason": row["ticket_tailor_void_reason"],
                "order_id": ticket_tailor_order_id,
            }

        order = row_to_dict(row)
        raw_payload = order.get("raw_payload")
        ticket_ids = (
            extract_issued_ticket_ids(raw_payload)
            if isinstance(raw_payload, dict)
            else []
        )
        if not ticket_ids:
            issued_tickets = await self.ticket_tailor.list_issued_tickets_for_order(
                tenant, ticket_tailor_order_id
            )
            ticket_ids = extract_issued_ticket_ids({"issued_tickets": issued_tickets})

        results: list[dict[str, Any]] = []
        for ticket_id in ticket_ids:
            try:
                results.append(await self.ticket_tailor.void_issued_ticket(tenant, ticket_id))
            except Exception as exc:
                results.append(
                    {
                        "status": "failed",
                        "issued_ticket_id": ticket_id,
                        "error": str(exc),
                    }
                )

        failed_results = [result for result in results if result.get("status") == "failed"]
        if failed_results:
            status = "failed" if len(failed_results) == len(results) else "partially_voided"
            self.db.log(
                tenant.tenant_id,
                ticket_tailor_order_id,
                "TICKET_TAILOR.TICKETS.VOID_FAILED",
                f"Ticket Tailor ticket void failed: {reason}",
                {
                    "reason": reason,
                    "issued_ticket_ids": ticket_ids,
                    "results": results,
                },
            )
            return {
                "status": status,
                "order_id": ticket_tailor_order_id,
                "issued_ticket_ids": ticket_ids,
                "results": results,
                "reason": reason,
            }

        status = "voided" if ticket_ids else "no_tickets_found"
        self.db.mark_ticket_tailor_tickets_voided(
            tenant.tenant_id, ticket_tailor_order_id, reason
        )
        self.db.log(
            tenant.tenant_id,
            ticket_tailor_order_id,
            "TICKET_TAILOR.TICKETS.VOIDED",
            f"Ticket Tailor tickets voided: {reason}",
            {
                "reason": reason,
                "issued_ticket_ids": ticket_ids,
                "results": results,
            },
        )
        return {
            "status": status,
            "order_id": ticket_tailor_order_id,
            "issued_ticket_ids": ticket_ids,
            "results": results,
            "reason": reason,
        }

    async def expire_overdue_orders(
        self,
        *,
        tenant_id: str | None = None,
        expiration_hours: float | None = None,
        batch_size: int | None = None,
    ) -> dict[str, Any]:
        resolved_expiration_hours = (
            self.settings.ticket_tailor_pending_ticket_expiration_hours
            if expiration_hours is None
            else expiration_hours
        )
        if resolved_expiration_hours <= 0:
            return {
                "status": "disabled",
                "expiration_hours": resolved_expiration_hours,
                "batch_size": 0,
                "expired_count": 0,
                "failed_count": 0,
                "results": [],
            }

        resolved_batch_size = max(
            1,
            batch_size
            if batch_size is not None
            else self.settings.ticket_tailor_expiration_batch_size,
        )
        tenants = {tenant.tenant_id: tenant for tenant in self.db.list_tenants()}
        rows = self.db.list_expirable_orders(
            expiration_hours=resolved_expiration_hours,
            limit=resolved_batch_size,
            tenant_id=tenant_id,
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            tenant = tenants.get(row["tenant_id"])
            if not tenant or not tenant.active:
                continue
            try:
                result = await self.void_ticket_tailor_tickets(
                    tenant,
                    row["ticket_tailor_order_id"],
                    reason=f"expired_after_{resolved_expiration_hours:g}_hours",
                )
            except Exception as exc:
                result = {
                    "status": "failed",
                    "tenant_id": row["tenant_id"],
                    "order_id": row["ticket_tailor_order_id"],
                    "reason": f"expired_after_{resolved_expiration_hours:g}_hours",
                    "error": str(exc),
                }
                self.db.log(
                    row["tenant_id"],
                    row["ticket_tailor_order_id"],
                    "TICKET_TAILOR.TICKETS.VOID_FAILED",
                    "Expiration failed for Ticket Tailor order",
                    result,
                )
            results.append(result)
        failed_count = sum(
            1
            for result in results
            if result.get("status") in {"failed", "partially_voided"}
        )
        return {
            "status": "processed",
            "expiration_hours": resolved_expiration_hours,
            "batch_size": resolved_batch_size,
            "selected_count": len(rows),
            "expired_count": len(results) - failed_count,
            "failed_count": failed_count,
            "results": results,
        }

    @staticmethod
    def _order_id_from_resource_url(resource_url: Any) -> str:
        if not isinstance(resource_url, str) or "/" not in resource_url:
            return ""
        return resource_url.rstrip("/").split("/")[-1]

    @staticmethod
    def _bill_short_id(payment_request: dict[str, Any]) -> str | None:
        bill = payment_request.get("bill")
        if isinstance(bill, dict):
            short_id = bill.get("shortId") or bill.get("short_id")
            return str(short_id) if short_id else None
        return None

    def _receiver_short_id(
        self, tenant: TenantConfig, payment_request: dict[str, Any]
    ) -> str | None:
        if tenant.nicky_receiver_short_id:
            return tenant.nicky_receiver_short_id

        candidates: list[Any] = []
        bill = payment_request.get("bill")
        if isinstance(bill, dict):
            candidates.append(bill.get("receiverUser"))
        candidates.append(payment_request.get("creator"))

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            for key in ("shortId", "short_id", "publicName", "public_name"):
                value = candidate.get(key)
                if value:
                    return str(value)
        return None

    def _payment_url(self, receiver_short_id: str | None, bill_short_id: str | None) -> str | None:
        if not receiver_short_id or not bill_short_id:
            return None
        return (
            f"{self.settings.nicky_pay_base_url}/payment-report/"
            f"{receiver_short_id}?paymentId={bill_short_id}"
        )

    async def _simulate_nicky_finished_webhook(
        self, tenant: TenantConfig, payment_request_id: str
    ) -> dict[str, Any] | None:
        if not payment_request_id:
            return None
        event = {
            "webHookId": f"skip-nicky-{tenant.tenant_id}",
            "webHookType": "PaymentRequest_StatusChanged",
            "itemId": payment_request_id,
            "data": {
                "previousStatus": "PaymentPending",
                "newStatus": "Finished",
            },
            "simulated": True,
        }
        raw_body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        return await self.process_nicky_webhook(tenant, event, raw_body)

    @staticmethod
    def _nicky_event_id(event: dict[str, Any]) -> str:
        pieces = [
            str(event.get("webHookId") or event.get("id") or "nicky"),
            str(event.get("itemId") or event.get("paymentRequestId") or ""),
            str(event.get("webHookType") or event.get("type") or ""),
            IntegrationService._nicky_status(event),
        ]
        return ":".join(piece for piece in pieces if piece)

    @staticmethod
    def _nicky_payment_request_id(event: dict[str, Any]) -> str:
        for key in ("itemId", "paymentRequestId", "payment_request_id"):
            value = event.get(key)
            if value:
                return str(value)
        data = event.get("data")
        if isinstance(data, dict):
            for key in ("paymentRequestId", "payment_request_id", "id"):
                value = data.get(key)
                if value:
                    return str(value)
        payment_request = event.get("paymentRequest")
        if isinstance(payment_request, dict) and payment_request.get("id"):
            return str(payment_request["id"])
        return ""

    @staticmethod
    def _nicky_status(event: dict[str, Any]) -> str:
        data = event.get("data")
        if isinstance(data, dict):
            for key in ("newStatus", "new_status", "status"):
                value = data.get(key)
                if value is not None:
                    return str(value)
        for key in ("status", "newStatus", "new_status"):
            value = event.get(key)
            if value is not None:
                return str(value)
        return "unknown"

    @staticmethod
    def _is_paid(status: str) -> bool:
        return status.strip().lower() == FINISHED_NICKY_STATUS


def row_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    raw = data.get("raw_payload_json")
    if raw:
        try:
            data["raw_payload"] = json.loads(raw)
        except json.JSONDecodeError:
            data["raw_payload"] = raw
    data.pop("raw_payload_json", None)
    return data
