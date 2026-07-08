# Ticket Tailor Integration — Azure Production Architecture

Deployed 2026-07-08 into resource group `nicky-prod` (subscription `6c7f81bf-a5a0-4520-8c15-0bfd922ca50b`).
All resources tagged `tickettailor-related=true`.

```mermaid
flowchart TB
    subgraph external["External services"]
        TT["Ticket Tailor<br/>(buyer checkout, offline payment)"]
        NICKY["Nicky Public API<br/>api-public.pay.nicky.me"]
        AUTH0["Auth0<br/>nicky-prod.us.auth0.com<br/>(Admin Tools client)"]
    end

    subgraph azure["Azure — nicky-prod (West Europe)"]
        subgraph plan["nicky-prod-linux-plan · B1 Linux · ~$13/mo"]
            APP["nicky-prod-tickettailor<br/>FastAPI · Python 3.11 · Always On<br/>─────────────<br/>/api/webhooks/* · /api/health<br/>/admin-ui (Auth0 login)<br/>in-process expiration loop (5 min)"]
        end
        subgraph sqlsrv["nicky-prod-sql (existing server)"]
            DB[("nicky-tickettailor-db<br/>SQL Basic · ~$5/mo<br/>user: tickettailor_app<br/>tenants · orders · webhook_events · logs")]
        end
        KV["nicky-prod-kv (existing vault)<br/>─────────────<br/>prod-tickettailor-database-url<br/>prod-tickettailor-session-secret<br/>prod-tickettailor-job-token<br/>prod-auth0-client-id/secret-admin"]
    end

    TT -- "order.created webhook<br/>/api/webhooks/ticket-tailor/{tenant_id}" --> APP
    APP -- "create Payment Request<br/>(X-API-KEY per tenant)" --> NICKY
    NICKY -- "PaymentRequest_StatusChanged webhook<br/>/api/webhooks/nicky/{tenant_id} (+token)" --> APP
    APP -- "confirm-payment-received /<br/>void issued tickets (per-tenant API key)" --> TT
    APP -- "mssql+pyodbc<br/>(ODBC Driver 18)" --> DB
    APP -. "Key Vault references<br/>via system managed identity<br/>(Key Vault Secrets User)" .-> KV
    AUTH0 -. "Universal Login redirect<br/>callback: /admin-ui/callback" .-> APP
```

## Payment lifecycle

```mermaid
sequenceDiagram
    participant Buyer
    participant TT as Ticket Tailor
    participant SVC as nicky-prod-tickettailor
    participant N as Nicky

    Buyer->>TT: Checkout with "Nicky Payment" offline method
    TT->>SVC: order.created webhook (tenant-specific URL)
    SVC->>N: Create Payment Request (tenant API key)
    N-->>Buyer: Payment request (hosted pay.nicky.me URL)
    Buyer->>N: Pays
    N->>SVC: Status webhook: Finished
    SVC->>TT: confirm-payment-received → tickets released
    Note over SVC,TT: If not paid within 4h, in-process job<br/>(every 5 min) voids the issued tickets
```

## Pending wiring

- Auth0 callback `https://nicky-prod-tickettailor.azurewebsites.net/admin-ui/callback` not yet registered — admin login blocked until then.
- Custom `nicky.me` subdomain (Cloudflare CNAME + hostname binding + managed cert) not yet set up.
- Tenant data still on the old Vercel/MySQL deployment; Ticket Tailor webhooks per tenant still point at `*.vercel.app`.
