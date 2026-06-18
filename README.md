# Nicky + Ticket Tailor Integration Service

FastAPI microservice for the Ticket Tailor `offline/alternative payment` strategy, now prepared for multiple Nicky customers in one deployment.

Flow:

1. Ticket Tailor creates an order with the Nicky offline payment method.
2. Ticket Tailor sends `order.created` or `order.updated` to this service.
3. This service identifies the tenant from the webhook URL.
4. The service creates a Nicky Payment Request with that tenant's Nicky API key and default asset.
5. Nicky sends a payment status webhook back to this service.
6. When Nicky reports `Finished` or `Concluído`, this service confirms the Ticket Tailor offline payment through that tenant's Ticket Tailor API key.

The service runs in live mode only. It does not bootstrap a default tenant, does not simulate Nicky, and does not expose dry-run or automation toggles.

## Multi-Tenant Mapping

Each Nicky customer should have one row in `tenants`.

Core tenant mapping:

```text
tenant_id | nicky_user_uuid | nicky_user_short_id | ticket_tailor_api_key | nicky_api_key | nicky_default_blockchain_asset_id
```

The webhook token used to protect Nicky callbacks is generated internally when the tenant is saved.

Common users start without a tenant. Their first configuration validates a Nicky API key, uses the Nicky user UUID as `tenant_id`, stores the Nicky Short ID for display, and registers the Nicky webhook automatically.

For production, keep the same table shape but move raw API keys to a secret manager/KMS or encrypt them at rest. The current SQLite storage is appropriate for local validation and controlled prototype usage.

## Setup

From the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
Copy-Item .env.example .env
uvicorn app.main:app --reload --port 8017
```

Health check:

```powershell
Invoke-RestMethod http://localhost:8017/api/health
```

API docs:

```text
http://localhost:8017/api/docs
```

## Environment

Important variables in `.env`:

```dotenv
APP_BASE_URL=http://localhost:8017
API_BASE_PATH=/api
ADMIN_API_BASE_PATH=/api
DATABASE_URL=
DATABASE_PATH=./data/integration.sqlite3
ADMIN_SESSION_SECRET=change-this-in-production
ADMIN_SESSION_MAX_AGE_SECONDS=28800
AUTH0_DOMAIN=
AUTH0_CLIENT_ID=
AUTH0_CLIENT_SECRET=
AUTH0_AUDIENCE=
AUTH0_CALLBACK_PATH=/admin-ui/callback
ADMIN_ALLOWED_ROLES=Admin

NICKY_API_BASE_URL=https://api-public.pay.nicky.me
NICKY_PAY_BASE_URL=https://pay.nicky.me
```

Use `DATABASE_URL` for deployed environments. If it is empty, the service falls back to
SQLite using `DATABASE_PATH`.

Examples:

```dotenv
# Local fallback
DATABASE_PATH=./data/integration.sqlite3

# MySQL
DATABASE_URL=mysql+pymysql://user:password@host:3306/database_name

# SQL Server
DATABASE_URL=mssql+pyodbc://user:password@host:1433/database_name?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
```

The application runs Alembic migrations automatically during startup.
MySQL uses the bundled `PyMySQL` driver. SQL Server requires the `mssql` extra plus an
ODBC driver available in the runtime image.

The Nicky Short ID is saved from the validated Nicky API key and is used to build the hosted payment URL:

```text
https://pay.nicky.me/payment-report/{receiverShortId}?paymentId={bill.shortId}
```

The current Nicky public API authenticates public-account operations with `X-API-KEY`. The webhook type for payment-request status changes is `PaymentRequest_StatusChanged`, enum value `2`.

## Vercel Deploy

The Vercel deployment is configured by `vercel.json` to expose:

```text
/                -> Admin UI
/admin-ui...     -> Admin UI routes and Auth0 callback
/api/...         -> FastAPI endpoints, webhooks, health check, and API docs
```

Do not rewrite every path to FastAPI. Routes such as `/docs`, `/openapi.json`, `/health`, `/admin/...`, and `/webhooks/...` should not be public at the domain root.

Set these Vercel environment variables:

```dotenv
APP_ENV=production
APP_BASE_URL=https://nicky-ticket-taylor.vercel.app
API_BASE_PATH=/api
ADMIN_API_BASE_PATH=/api
AUTH0_CALLBACK_PATH=/admin-ui/callback
AUTH0_DOMAIN=your-tenant.region.auth0.com
AUTH0_CLIENT_ID=...
AUTH0_CLIENT_SECRET=
AUTH0_AUDIENCE=
ADMIN_ALLOWED_ROLES=Admin
ADMIN_SESSION_SECRET=choose-a-long-random-session-secret
DATABASE_URL=mysql+pymysql://user:password@host:3306/database_name
```

Keep real Ticket Tailor and Nicky credentials in tenant records created through the Admin UI. The deploy does not use fake or mocked integrations.

Add this callback URL to the Auth0 application:

```text
https://nicky-ticket-taylor.vercel.app/admin-ui/callback
```

FastAPI endpoints remain available under `/api`, for example:

```text
https://nicky-ticket-taylor.vercel.app/api/health
https://nicky-ticket-taylor.vercel.app/api/webhooks/ticket-tailor/{tenant_id}
https://nicky-ticket-taylor.vercel.app/api/webhooks/nicky/{tenant_id}
https://nicky-ticket-taylor.vercel.app/api/docs
```

## Admin Web Console

The service includes a server-rendered admin console at:

```text
http://localhost:8017/admin-ui
```

Use it to manage the core tenant mapping, inspect order mappings, review recent webhook deliveries, recreate Nicky Payment Requests, manually confirm Ticket Tailor payments, and trigger overdue-order expiration.

The dashboard is built for the same multi-tenant model used by the API:

```text
tenant_id | ticket_tailor_api_key | nicky_api_key | nicky_default_blockchain_asset_id
```

### Auth0 Login

The web console redirects unauthenticated admin users to Auth0 Universal Login, matching the Nicky Admin Tools behavior. If the Auth0 tenant has social connections enabled, the hosted Auth0 page is where users see options such as Google, Microsoft, GitHub, Discord, Apple, and passwordless/email login.

Configure Auth0 and set:

```dotenv
AUTH0_DOMAIN=your-tenant.region.auth0.com
AUTH0_CLIENT_ID=...
AUTH0_CLIENT_SECRET=
AUTH0_AUDIENCE=
AUTH0_CALLBACK_PATH=/admin-ui/callback
ADMIN_ALLOWED_ROLES=Admin
ADMIN_SESSION_SECRET=choose-a-long-random-session-secret
ADMIN_SESSION_MAX_AGE_SECONDS=28800
```

Add this callback URL to the Auth0 application:

```text
https://YOUR_PUBLIC_URL/admin-ui/callback
```

For local-only testing, also allow:

```text
http://localhost:8017/admin-ui/callback
```

`AUTH0_CLIENT_SECRET` is optional. Leave it empty when using a public Auth0 client like the Nicky Admin Tools SPA client; the service uses Authorization Code with PKCE in that mode. Set it only when using a confidential Regular Web Application client.

You can reuse the same Auth0 tenant and public client used by Nicky Admin Tools, but the callback URL must point back to this FastAPI service. Do not reuse the existing Admin Tools callback such as `/authentication/login-callback` unless that exact URL is routed to this service. If Auth0 redirects to the existing frontend callback, the frontend receives the authorization code and this service cannot create its admin session.

For a first local test with the existing Nicky Angular development Auth0 client, run the compatibility helper:

```bat
start-local-auth0-compat.bat
```

or on Linux/macOS:

```bash
./start-local-auth0-compat.sh
```

This starts the FastAPI app at:

```text
http://localhost:4200/overview
```

and uses the Angular development callback style:

```text
AUTH0_CALLBACK_PATH=/overview
```

so the Auth0 `redirect_uri` becomes:

```text
http://localhost:4200/overview
```

The helper defaults to the development Angular Auth0 configuration found in the Nicky frontend:

```dotenv
AUTH0_DOMAIN=dev-eq0ptfwdhb1s1h12.us.auth0.com
AUTH0_CLIENT_ID=SqrJq2fxJ6adrOFaR24oh9COF4vZwqba
AUTH0_AUDIENCE=https://nicky-tech.azurewebsites.net
```

It defaults to `ADMIN_ALLOWED_ROLES=Admin`. Successfully authenticated users without an admin or support role can still enter the console as common users, but they remain scoped to their own Nicky UUID and do not get the admin-wide view. `ADMIN_ALLOWED_ROLES=*` may be used only as an authentication allow-list shortcut; it does not grant admin privileges.

The implementation accepts signed Auth0 sessions in the browser and bearer tokens for admin API calls. Browser sessions use `ADMIN_SESSION_MAX_AGE_SECONDS`; Auth0 sessions are also rejected when the token `exp` claim is expired. It extracts roles from common Auth0 role/permission claims, including namespaced claims. Roles matching `ADMIN_ALLOWED_ROLES` are admins, `Support` is read-only, and users without those roles are common users scoped to their own Nicky UUID.

Auth0 references:

- Authorization Code Flow: https://auth0.com/docs/get-started/authentication-and-authorization-flow/authorization-code-flow
- Role-Based Access Control: https://auth0.com/docs/get-started/apis/enable-role-based-access-control-for-apis

### Admin Screens

- `Dashboard`: active tenants, orders, pending orders, Nicky Payment Requests, and recent webhook deliveries.
- `Tenants`: create and edit tenant mappings, validate the Nicky API key, select an available asset, and store the Ticket Tailor API key.
- `Tenant detail`: copy the generated Ticket Tailor webhook URL; the Nicky webhook is registered automatically when the tenant is saved.
- `Orders`: inspect one Ticket Tailor order to one Nicky Payment Request mapping, current status, buyer/order metadata, and action logs.

## Create A Tenant

Preferred flow: sign in with Auth0, open `Tenants`, inform the Nicky API key, click `Validate`, select one returned asset, inform the Ticket Tailor API key, and save. The service creates or updates the tenant, stores the Nicky user UUID and Short ID, and registers the Nicky webhook automatically with type `2`.

Admin-only PowerShell seed example:

```powershell
python scripts\upsert_tenant.py `
  --tenant-id "{nicky_user_uuid}" `
  --nicky-user-uuid "{nicky_user_uuid}" `
  --nicky-user-short-id "{nicky_short_id}" `
  --name "{nicky_short_id}" `
  --ticket-tailor-api-key "tt_api_key_here" `
  --nicky-api-key "nicky_public_api_key_here" `
  --nicky-default-blockchain-asset-id "asset_id_here"
```

The script prints the saved tenant with API keys masked.

Equivalent HTTP admin endpoint:

```powershell
Invoke-RestMethod -Method Post http://localhost:8017/admin/tenants `
  -Headers @{ Authorization = "Bearer ..." } `
  -ContentType "application/json" `
  -Body '{
    "tenant_id": "{nicky_user_uuid}",
    "ticket_tailor_api_key": "tt_api_key_here",
    "nicky_api_key": "nicky_public_api_key_here",
    "nicky_default_blockchain_asset_id": "asset_id_here"
  }'
```

This endpoint validates the Nicky API key before saving. Common users cannot use an API key that belongs to another Nicky UUID; admins can.

## Ticket Tailor Setup Guide

This integration uses Ticket Tailor's offline payment flow. In Ticket Tailor, an offline payment lets the buyer finish checkout before the external payment is received. Ticket Tailor creates one order, issues the tickets, and marks the order as pending until payment is confirmed. This service listens for that order, creates one Nicky Payment Request for the order total, and later confirms or voids the Ticket Tailor order based on the Nicky status. Ticket Tailor's offline payment confirmation API is order-level, so the mapping is intentionally `one Ticket Tailor order -> one Nicky Payment Request`.

Official Ticket Tailor references:

- Offline payment setup: https://help.tickettailor.com/en/articles/3011516-how-to-set-up-offline-payments
- Alternative payment method with redirect: https://help.tickettailor.com/en/articles/7008722-how-to-use-an-alternative-payment-method-with-ticket-tailor
- API authentication: https://developers.tickettailor.com/docs/api/ticket-tailor-api/
- Webhook configuration: https://developers.tickettailor.com/docs/webhook/configuration/
- Webhook structure: https://developers.tickettailor.com/docs/webhook/structure/
- Webhook security: https://developers.tickettailor.com/docs/webhook/security/
- Webhook retry/testing: https://developers.tickettailor.com/docs/webhook/retry/ and https://developers.tickettailor.com/docs/webhook/testing/
- Confirm offline payment API: https://developers.tickettailor.com/docs/api/confirm-payment-recieved/
- Void issued ticket API: https://developers.tickettailor.com/docs/api/void-issued-ticket-by-id/

### 1. Create The Offline Payment Method

In Ticket Tailor:

1. Open `Box office settings`.
2. Go to `Payment systems`.
3. In `Offline payments`, click `Create offline payment profile`.
4. Use the fixed payment method name `Nicky Payment`.
5. Add buyer-facing payment instructions. A practical message is:

```text
Complete this order and wait for the Nicky payment request by email.
Your tickets remain pending until the Nicky payment is finished.
```

The service no longer exposes offline payment keywords as configuration. Any `order.created` webhook delivered to the tenant-specific Ticket Tailor webhook URL creates a Nicky Payment Request.

### 2. Attach The Payment Method To The Event

In the event settings:

1. Open the event you want to sell.
2. Go to the payment or advanced checkout settings.
3. Enable the `Nicky Payment` offline payment profile for that event.
4. Make at least one ticket type public and purchasable.
5. Publish the event before testing as a normal buyer.

Optional redirect:

If you want the buyer to see a custom pending-payment page after Ticket Tailor checkout, enable `Redirect order confirmation page` in the event's advanced settings and point it to:

```text
https://YOUR_PUBLIC_URL/payment-info
```

The redirect is not the Nicky payment URL itself. The Nicky Payment Request is created after the Ticket Tailor `order.created` webhook arrives, so the safest buyer instruction is still to expect the Nicky email/payment request.

### 3. Create The Ticket Tailor API Key

The API key lets this service call Ticket Tailor after the Nicky status changes.

In Ticket Tailor:

1. Open `Box office settings`.
2. Go to `API`.
3. Click `Generate a new key`.
4. Give it a recognizable name, such as `Nicky integration`.
5. Select access covering `Orders` and `Issued tickets`.
6. Copy the API key and store it in the tenant as `ticket_tailor_api_key`.

This service uses the key with Ticket Tailor HTTP Basic Auth. The key is the username and the password is empty. It is used for:

- `POST /v1/orders/:order_id/confirm-payment-received`
- `GET /v1/issued_tickets?order_id=...`
- `POST /v1/issued_tickets/:issued_ticket_id/void`

### 4. Create The Ticket Tailor Webhook

First start FastAPI and the Cloudflare helper so you have a public URL. The helper prints the exact Ticket Tailor webhook URL:

```text
https://YOUR_PUBLIC_URL/webhooks/ticket-tailor/{tenant_id}
```

In Ticket Tailor:

1. Open `Box office settings`.
2. Go to `API`.
3. Open the `Webhooks` tab.
4. Click `Create new webhook`.
5. Create a webhook for `order.created`.
6. Paste:

```text
https://YOUR_PUBLIC_URL/webhooks/ticket-tailor/{tenant_id}
```

7. Optionally create another webhook for `order.updated` using the same URL.
8. Save the webhook. No extra Ticket Tailor webhook configuration is exposed in the tenant UI.

Ticket Tailor recommends webhook receivers to be idempotent; this service stores the Ticket Tailor webhook `id` so duplicate deliveries are not processed twice.

Ticket Tailor does not send the account API key inside the webhook payload. The tenant-specific URL is therefore what selects the correct tenant mapping row:

```text
/webhooks/ticket-tailor/{tenant_id}
```

## Nicky Setup Guide

The Nicky side has two responsibilities:

1. Create a Payment Request when Ticket Tailor creates a Nicky offline order.
2. Send status-change webhooks back to this service.

Set `NICKY_API_BASE_URL` and `NICKY_PAY_BASE_URL` for the Nicky environment you are testing. The production defaults are in `.env.example`; use the matching dev/staging URLs when validating outside production.

The Nicky public API used by this service exposes:

- `POST /api/public/PaymentRequestPublicApi/create`
- `POST /api/public/WebHookApi/create`
- `POST /api/public/WebHookApi/test-status-change`
- `GET /AcceptedAsset/get-for-user`

### 1. Prepare The Nicky Tenant Values

Each tenant needs:

- `nicky_api_key`: API key for the Nicky user that will receive the payment.
- `nicky_default_blockchain_asset_id`: default asset for Payment Requests, for example `USD.USD`.
- `nicky_user_uuid`: Nicky user UUID returned by API key validation.
- `nicky_user_short_id`: Nicky Short ID returned by API key validation and shown in list views.

Store these values through the admin UI or admin API. The Nicky API key is sent to Nicky as `X-API-KEY`.

### 2. Configure The Nicky Webhook

The admin UI validates the Nicky API key, loads the user's accepted assets, and automatically registers the Nicky webhook with type `2` when the tenant is saved. Users do not need to enter the Nicky webhook URL or webhook type.

The Nicky OpenAPI does not advertise a webhook signature scheme, so this service protects Nicky webhooks with the per-tenant shared token. The token can be supplied either in the query string as `token=...` or in the `X-Nicky-Webhook-Token` header.

### 3. Understand Nicky Status Handling

Nicky payment-request statuses observed in the current codebase:

- `PaymentPending`
- `PaymentValidationRequired`
- `Canceled`
- `Finished`

Only `Finished` or `Concluído` is treated as paid. When Nicky sends a paid status, this service calls:

```text
POST /v1/orders/:order_id/confirm-payment-received
```

Any status other than `Finished` does not confirm the Ticket Tailor order. If the order has not already been confirmed, this service attempts to void the issued Ticket Tailor tickets with:

```text
POST /v1/issued_tickets/:issued_ticket_id/void
```

Pending orders can also expire automatically based on `TICKET_TAILOR_PENDING_TICKET_EXPIRATION_HOURS`.

## End-To-End Setup Checklist

Use this order when setting up a new customer/tenant:

1. Sign in through Auth0.
2. Start FastAPI locally and confirm `/health` returns `ok`.
3. Start the Cloudflare helper and copy the generated Ticket Tailor webhook URL template from `./tunnel-urls.txt`.
4. In Ticket Tailor, create the `Nicky Payment` offline payment method.
5. Attach `Nicky Payment` to the event and publish the event.
6. In Ticket Tailor, generate an API key with access to Orders and Issued tickets.
7. In Ticket Tailor, create the `order.created` webhook pointing to `/webhooks/ticket-tailor/{tenant_id}`.
8. In this service, enter the Nicky API key, validate it, select the asset, and enter the Ticket Tailor API key.
9. Save the tenant. The service uses the Nicky user UUID as `tenant_id`, stores the Nicky Short ID, and registers the Nicky webhook automatically.
10. Copy the generated Ticket Tailor webhook URL from the tenant page into Ticket Tailor.
11. Run a buyer checkout using the Ticket Tailor offline payment method.
12. Confirm that this service stores one `ticket_tailor_order_id -> nicky_payment_request_id` mapping.
13. Complete the Nicky payment.
14. Confirm that Ticket Tailor changes from pending to paid only after Nicky sends `Finished` or `Concluído`.

Pending orders can also be expired automatically. Set:

```env
TICKET_TAILOR_PENDING_TICKET_EXPIRATION_HOURS=4
TICKET_TAILOR_EXPIRATION_CHECK_INTERVAL_SECONDS=300
TICKET_TAILOR_EXPIRATION_BATCH_SIZE=100
```

`TICKET_TAILOR_PENDING_TICKET_EXPIRATION_HOURS=0` disables automatic expiration. Each expiration pass selects at most `TICKET_TAILOR_EXPIRATION_BATCH_SIZE` orders, ordered by creation time. Failures are isolated per order: one failed void operation is returned/logged as a failed item and does not stop the rest of the batch.

You can trigger the same sweep manually with:

```powershell
Invoke-RestMethod -Method Post "http://localhost:8017/admin/expire-overdue-orders?tenant_id={tenant_uuid}&expiration_hours=4&batch_size=25"
```

## Cloudflare Tunnel Helpers

For quick Windows tests, use the bundled BAT:

```bat
start-cloudflare-tunnel.bat
```

For Linux/macOS tests, use the equivalent shell script:

```bash
chmod +x start-cloudflare-tunnel.sh tail-microservice-logs.sh
./start-cloudflare-tunnel.sh
```

Both helpers expect FastAPI to be running locally at:

```text
http://127.0.0.1:8017
```

With `START_FASTAPI=true`, the helpers restart the local FastAPI process on the configured port by default and start it with:

```text
APP_BASE_URL=https://YOUR-TRYCLOUDFLARE-URL
NICKY_SUCCESS_URL=https://YOUR-TRYCLOUDFLARE-URL/nicky/success
NICKY_CANCEL_URL=https://YOUR-TRYCLOUDFLARE-URL/nicky/cancel
```

Set `START_FASTAPI=false` if you want the helper to create only the tunnel. Set `RESTART_FASTAPI=false` only when you intentionally want to reuse an already-running FastAPI process.

Override defaults with environment variables when needed:

```bash
LOCAL_URL=http://127.0.0.1:8017 \
./start-cloudflare-tunnel.sh
```

On Windows CMD:

```bat
set LOCAL_URL=http://127.0.0.1:8017
start-cloudflare-tunnel.bat
```

If `tools\cloudflared.exe` is missing on Windows, the BAT creates the `tools` folder and downloads the Windows AMD64 binary from Cloudflare's GitHub releases:

```text
https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
```

If `tools/cloudflared` is missing on Linux/macOS, the shell script downloads the matching `linux-amd64`, `linux-arm64`, `darwin-amd64`, or `darwin-arm64` release asset.

The local `tools/cloudflared*` binaries are intentionally ignored by Git because they are large machine-specific files. The helpers write the generated public URLs to:

```text
./tunnel-urls.txt
```

Typical output includes:

```text
Admin UI:
https://YOUR-TRYCLOUDFLARE-URL/admin-ui

Auth0 callback URL:
https://YOUR-TRYCLOUDFLARE-URL/admin-ui/callback

Ticket Tailor webhook:
https://YOUR-TRYCLOUDFLARE-URL/webhooks/ticket-tailor/{tenant_uuid}

Nicky webhook:
Cadastrado automaticamente ao salvar o tenant na UI.
```

On Windows, keep the minimized Cloudflare Tunnel window open while testing webhooks. On Linux/macOS, the shell helper starts the tunnel in the background and prints the PID. Stop it with `kill PID`.

To follow logs:

```bat
tail-microservice-logs.bat
```

or:

```bash
./tail-microservice-logs.sh
```

For Auth0, add the printed `Auth0 callback URL` to the Auth0 application's allowed callback URLs before testing login through the public tunnel.

## Manual Operations

Manual API operations require an Auth0 bearer token.

```powershell
# List tenants
Invoke-RestMethod http://localhost:8017/admin/tenants -Headers @{ Authorization = "Bearer ..." }

# List captured orders for one tenant
Invoke-RestMethod "http://localhost:8017/orders?tenant_id={tenant_uuid}" -Headers @{ Authorization = "Bearer ..." }

# Create/recreate a Nicky payment request for a tenant order
Invoke-RestMethod -Method Post http://localhost:8017/admin/tenants/{tenant_uuid}/orders/or_123/create-nicky-payment-request -Headers @{ Authorization = "Bearer ..." }

# Confirm Ticket Tailor offline payment manually through the service
Invoke-RestMethod -Method Post http://localhost:8017/admin/tenants/{tenant_uuid}/orders/or_123/confirm-ticket-tailor-payment -Headers @{ Authorization = "Bearer ..." }
```

## References

- Ticket Tailor webhooks: https://developers.tickettailor.com/docs/webhook/configuration/
- Ticket Tailor webhook structure: https://developers.tickettailor.com/docs/webhook/structure/
- Ticket Tailor webhook security: https://developers.tickettailor.com/docs/webhook/security/
- Ticket Tailor offline payments: https://help.tickettailor.com/en/articles/3011516-how-to-set-up-offline-payments
- Ticket Tailor alternative payment redirect: https://help.tickettailor.com/en/articles/7008722-how-to-use-an-alternative-payment-method-with-ticket-tailor
- Ticket Tailor confirm offline payment API: https://developers.tickettailor.com/docs/api/confirm-payment-recieved/
- Nicky developer page: https://nicky.me/developers/
- Nicky OpenAPI: https://api-public.pay.nicky.me/swagger/index.html
