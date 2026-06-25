"""add users table mapping auth subject to login email

Revision ID: 0003_users_table
Revises: 0002_tenant_nicky_webhook_id
Create Date: 2026-06-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0003_users_table"
down_revision = "0002_tenant_nicky_webhook_id"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return inspector.has_table(table_name)


def upgrade() -> None:
    if not _has_table("users"):
        op.create_table(
            "users",
            sa.Column("auth_subject", sa.String(255), primary_key=True),
            sa.Column("email", sa.String(255), nullable=False, server_default=""),
            sa.Column("name", sa.String(255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("idx_users_email", "users", ["email"])


def downgrade() -> None:
    op.drop_index("idx_users_email", table_name="users")
    op.drop_table("users")
