from __future__ import annotations

import sqlalchemy as sa


metadata = sa.MetaData()


tenants = sa.Table(
    "tenants",
    metadata,
    sa.Column("tenant_id", sa.String(64), primary_key=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column("nicky_user_uuid", sa.String(255), nullable=False, server_default=""),
    sa.Column("nicky_user_short_id", sa.String(255), nullable=False, server_default=""),
    sa.Column("nicky_user_email", sa.String(255), nullable=False, server_default=""),
    sa.Column("ticket_tailor_api_key", sa.String(2048), nullable=False, server_default=""),
    sa.Column("ticket_tailor_webhook_signing_secret", sa.String(2048), nullable=False, server_default=""),
    sa.Column("nicky_api_key", sa.String(2048), nullable=False, server_default=""),
    sa.Column("nicky_default_blockchain_asset_id", sa.String(255), nullable=False, server_default=""),
    sa.Column("nicky_receiver_short_id", sa.String(255), nullable=False, server_default=""),
    sa.Column("nicky_webhook_token", sa.String(255), nullable=False, server_default=""),
    sa.Column("nicky_webhook_id", sa.String(255), nullable=False, server_default=""),
    sa.Column("nicky_webhook_type", sa.Integer(), nullable=False, server_default="2"),
    sa.Column("nicky_send_notification", sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column("owner_auth_subject", sa.String(255), nullable=False, server_default=""),
    sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
)


webhook_events = sa.Table(
    "webhook_events",
    metadata,
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("source", sa.String(64), nullable=False),
    sa.Column("event_id", sa.String(255), nullable=False),
    sa.Column("event_type", sa.String(255), nullable=False),
    sa.Column("received_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    sa.Column("processed_at", sa.DateTime(), nullable=True),
    sa.Column("status", sa.String(64), nullable=False),
    sa.Column("raw_body", sa.Text(), nullable=False),
    sa.Column("error", sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint("tenant_id", "source", "event_id"),
)


integration_orders = sa.Table(
    "integration_orders",
    metadata,
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("ticket_tailor_order_id", sa.String(255), nullable=False),
    sa.Column("event_id", sa.String(255), nullable=True),
    sa.Column("status", sa.String(64), nullable=True),
    sa.Column("payment_status", sa.String(64), nullable=True),
    sa.Column("currency", sa.String(16), nullable=True),
    sa.Column("amount_minor", sa.Integer(), nullable=True),
    sa.Column("buyer_email", sa.String(255), nullable=True),
    sa.Column("buyer_name", sa.String(255), nullable=True),
    sa.Column("raw_payload_json", sa.Text(), nullable=False),
    sa.Column("nicky_payment_request_id", sa.String(255), nullable=True),
    sa.Column("nicky_bill_short_id", sa.String(255), nullable=True),
    sa.Column("nicky_receiver_short_id", sa.String(255), nullable=True),
    sa.Column("nicky_payment_url", sa.Text(), nullable=True),
    sa.Column("nicky_status", sa.String(64), nullable=True),
    sa.Column("ticket_tailor_confirmed_at", sa.DateTime(), nullable=True),
    sa.Column("ticket_tailor_tickets_voided_at", sa.DateTime(), nullable=True),
    sa.Column("ticket_tailor_void_reason", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    sa.PrimaryKeyConstraint("tenant_id", "ticket_tailor_order_id"),
)

sa.Index(
    "idx_integration_orders_nicky_payment",
    integration_orders.c.tenant_id,
    integration_orders.c.nicky_payment_request_id,
)


users = sa.Table(
    "users",
    metadata,
    sa.Column("auth_subject", sa.String(255), primary_key=True),
    sa.Column("email", sa.String(255), nullable=False, server_default=""),
    sa.Column("name", sa.String(255), nullable=False, server_default=""),
    sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
)

sa.Index("idx_users_email", users.c.email)


order_logs = sa.Table(
    "order_logs",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("ticket_tailor_order_id", sa.String(255), nullable=False),
    sa.Column("event_type", sa.String(255), nullable=False),
    sa.Column("message", sa.Text(), nullable=False),
    sa.Column("payload_json", sa.Text(), nullable=True),
    sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
)
