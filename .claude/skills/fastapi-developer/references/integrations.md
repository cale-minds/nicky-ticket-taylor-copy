# Nicky and Ticket Tailor integration

## NickyClient (`app/nicky.py`)

- Instantiated with `settings: Settings` — the tenant `api_key` is passed **per call** (not in the constructor).
- Public async methods: `create_payment_request()`, `get_payment_request()`, `create_webhook()`, `test_status_change_webhook()`, `validate_api_key()`.
- Raises `NickyApiError` on failure — catch and handle in routes.
- Base URL: `settings.nicky_api_base_url` (dev: `https://api-public.dev.pay.nicky.me`).
- Authentication: `X-API-KEY` header with the tenant's api_key.

## TicketTailorClient (`app/ticket_tailor.py`)

- Instantiated with `settings: Settings` — the tenant `api_key` is passed **per call**.
- Public async methods: `validate_api_key()`, `confirm_offline_payment()`, `list_issued_tickets_for_order()`, `void_issued_ticket()`.
- Authentication: HTTP Basic Auth with `(api_key, "")` (empty password).

## Webhook verification

- Ticket Tailor webhooks arrive at `POST /webhook/ticket-tailor/{tenant_id}`.
- HMAC signature verified via `app/security.py:verify_ticket_tailor_signature()`.
- Nicky webhooks arrive at `POST /webhook/nicky/{tenant_id}` — authenticated by a token in the request header.

## Integration flow

1. TT sends an offline order webhook → service creates `integration_order` + `webhook_event`
2. `IntegrationService.create_nicky_payment_request()` → creates a PR in Nicky, stores `nicky_payment_request_id`
3. Nicky sends a payment-confirmed webhook → `IntegrationService.handle_nicky_payment()` → confirms in TT
4. Periodic job expires pending orders without payment after N hours
