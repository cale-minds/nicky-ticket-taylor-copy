"""add nicky_webhook_id to tenants

Revision ID: 0002_tenant_nicky_webhook_id
Revises: 0001_initial_schema
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002_tenant_nicky_webhook_id"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return False
    return column_name in {col["name"] for col in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("tenants", "nicky_webhook_id"):
        op.add_column(
            "tenants",
            sa.Column("nicky_webhook_id", sa.String(255), nullable=False, server_default=""),
        )


def downgrade() -> None:
    op.drop_column("tenants", "nicky_webhook_id")
