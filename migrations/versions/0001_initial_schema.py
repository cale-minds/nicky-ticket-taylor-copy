"""initial schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-06-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return inspector.has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def upgrade() -> None:
    if not _has_table("tenants"):
        op.create_table(
            "tenants",
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
            sa.Column("nicky_webhook_type", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("nicky_send_notification", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("owner_auth_subject", sa.String(255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    else:
        _add_column_if_missing("tenants", sa.Column("nicky_user_uuid", sa.String(255), nullable=False, server_default=""))
        _add_column_if_missing("tenants", sa.Column("nicky_user_short_id", sa.String(255), nullable=False, server_default=""))
        _add_column_if_missing("tenants", sa.Column("nicky_user_email", sa.String(255), nullable=False, server_default=""))
        _add_column_if_missing("tenants", sa.Column("owner_auth_subject", sa.String(255), nullable=False, server_default=""))

    if not _has_table("webhook_events"):
        op.create_table(
            "webhook_events",
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

    if not _has_table("integration_orders"):
        op.create_table(
            "integration_orders",
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
    else:
        _add_column_if_missing("integration_orders", sa.Column("ticket_tailor_tickets_voided_at", sa.DateTime(), nullable=True))
        _add_column_if_missing("integration_orders", sa.Column("ticket_tailor_void_reason", sa.Text(), nullable=True))

    if not _has_table("order_logs"):
        op.create_table(
            "order_logs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("ticket_tailor_order_id", sa.String(255), nullable=False),
            sa.Column("event_type", sa.String(255), nullable=False),
            sa.Column("message", sa.Text(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
    else:
        _add_column_if_missing("order_logs", sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"))

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("integration_orders")}
    if "idx_integration_orders_nicky_payment" not in indexes:
        op.create_index(
            "idx_integration_orders_nicky_payment",
            "integration_orders",
            ["tenant_id", "nicky_payment_request_id"],
        )

    tenants_table = sa.table(
        "tenants",
        sa.column("nicky_user_uuid"),
        sa.column("nicky_user_short_id"),
        sa.column("nicky_user_email"),
    )
    bind.execute(
        tenants_table.update()
        .where(tenants_table.c.nicky_user_email == "")
        .where(tenants_table.c.nicky_user_uuid.like("%@%"))
        .values(
            nicky_user_email=tenants_table.c.nicky_user_uuid,
            nicky_user_uuid="",
            nicky_user_short_id="",
        )
    )


def downgrade() -> None:
    op.drop_index("idx_integration_orders_nicky_payment", table_name="integration_orders")
    op.drop_table("order_logs")
    op.drop_table("integration_orders")
    op.drop_table("webhook_events")
    op.drop_table("tenants")
