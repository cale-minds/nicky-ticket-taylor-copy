from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from app.config import Settings
from app.tenants import TenantConfig, keywords_to_csv, tenant_from_row, tenant_from_settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  tenant_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  ticket_tailor_api_key TEXT NOT NULL DEFAULT '',
  ticket_tailor_webhook_signing_secret TEXT NOT NULL DEFAULT '',
  ticket_tailor_offline_payment_keywords TEXT NOT NULL DEFAULT 'nicky,nicky payment',
  nicky_api_key TEXT NOT NULL DEFAULT '',
  nicky_default_blockchain_asset_id TEXT NOT NULL DEFAULT '',
  nicky_receiver_short_id TEXT NOT NULL DEFAULT '',
  nicky_webhook_token TEXT NOT NULL DEFAULT '',
  nicky_webhook_type INTEGER NOT NULL DEFAULT 2,
  auto_create_nicky_payment_request INTEGER NOT NULL DEFAULT 1,
  auto_confirm_ticket_tailor_payments INTEGER NOT NULL DEFAULT 0,
  nicky_send_notification INTEGER NOT NULL DEFAULT 1,
  skip_nicky INTEGER NOT NULL DEFAULT 0,
  dry_run INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS webhook_events (
  tenant_id TEXT NOT NULL,
  source TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  processed_at TEXT,
  status TEXT NOT NULL,
  raw_body TEXT NOT NULL,
  error TEXT,
  PRIMARY KEY (tenant_id, source, event_id)
);

CREATE TABLE IF NOT EXISTS integration_orders (
  tenant_id TEXT NOT NULL,
  ticket_tailor_order_id TEXT NOT NULL,
  event_id TEXT,
  status TEXT,
  payment_status TEXT,
  currency TEXT,
  amount_minor INTEGER,
  buyer_email TEXT,
  buyer_name TEXT,
  raw_payload_json TEXT NOT NULL,
  nicky_payment_request_id TEXT,
  nicky_bill_short_id TEXT,
  nicky_receiver_short_id TEXT,
  nicky_payment_url TEXT,
  nicky_status TEXT,
  ticket_tailor_confirmed_at TEXT,
  ticket_tailor_tickets_voided_at TEXT,
  ticket_tailor_void_reason TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (tenant_id, ticket_tailor_order_id)
);

CREATE INDEX IF NOT EXISTS idx_integration_orders_nicky_payment
ON integration_orders(tenant_id, nicky_payment_request_id);

CREATE TABLE IF NOT EXISTS order_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant_id TEXT NOT NULL,
  ticket_tailor_order_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  payload_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            legacy_tables = self._prepare_legacy_tables(conn)
            conn.executescript(SCHEMA)
            self._copy_legacy_tables(conn, legacy_tables)
            self._ensure_column(conn, "tenants", "skip_nicky", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "order_logs", "tenant_id", "TEXT NOT NULL DEFAULT 'default'")
            self._ensure_column(
                conn, "integration_orders", "ticket_tailor_tickets_voided_at", "TEXT"
            )
            self._ensure_column(
                conn, "integration_orders", "ticket_tailor_void_reason", "TEXT"
            )

    def bootstrap_default_tenant(self, settings: Settings) -> TenantConfig:
        tenant = self.get_tenant(settings.default_tenant_id)
        if tenant:
            return tenant
        tenant = tenant_from_settings(settings)
        self.upsert_tenant(tenant)
        return tenant

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return bool(row)

    @staticmethod
    def _primary_key_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
        columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return [
            row["name"]
            for row in sorted((row for row in columns if row["pk"]), key=lambda item: item["pk"])
        ]

    def _prepare_legacy_tables(self, conn: sqlite3.Connection) -> dict[str, str]:
        desired_primary_keys = {
            "webhook_events": ["tenant_id", "source", "event_id"],
            "integration_orders": ["tenant_id", "ticket_tailor_order_id"],
        }
        legacy_tables: dict[str, str] = {}
        for table_name, desired_pk in desired_primary_keys.items():
            if not self._table_exists(conn, table_name):
                continue
            if self._primary_key_columns(conn, table_name) == desired_pk:
                continue
            legacy_name = f"{table_name}_legacy_{int(time.time())}"
            conn.execute(f"ALTER TABLE {table_name} RENAME TO {legacy_name}")
            legacy_tables[table_name] = legacy_name
        return legacy_tables

    def _copy_legacy_tables(self, conn: sqlite3.Connection, legacy_tables: dict[str, str]) -> None:
        for target_name, legacy_name in legacy_tables.items():
            if target_name == "webhook_events":
                self._copy_legacy_webhook_events(conn, legacy_name)
            elif target_name == "integration_orders":
                self._copy_legacy_orders(conn, legacy_name)

    @staticmethod
    def _copy_legacy_webhook_events(conn: sqlite3.Connection, legacy_name: str) -> None:
        rows = conn.execute(f"SELECT * FROM {legacy_name}").fetchall()
        for row in rows:
            data = dict(row)
            conn.execute(
                """
                INSERT OR IGNORE INTO webhook_events(
                  tenant_id, source, event_id, event_type, received_at, processed_at,
                  status, raw_body, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("tenant_id") or "default",
                    data["source"],
                    data["event_id"],
                    data["event_type"],
                    data.get("received_at"),
                    data.get("processed_at"),
                    data["status"],
                    data["raw_body"],
                    data.get("error"),
                ),
            )

    @staticmethod
    def _copy_legacy_orders(conn: sqlite3.Connection, legacy_name: str) -> None:
        rows = conn.execute(f"SELECT * FROM {legacy_name}").fetchall()
        for row in rows:
            data = dict(row)
            conn.execute(
                """
                INSERT OR IGNORE INTO integration_orders(
                  tenant_id, ticket_tailor_order_id, event_id, status, payment_status,
                  currency, amount_minor, buyer_email, buyer_name, raw_payload_json,
                  nicky_payment_request_id, nicky_bill_short_id, nicky_receiver_short_id,
                  nicky_payment_url, nicky_status, ticket_tailor_confirmed_at,
                  ticket_tailor_tickets_voided_at, ticket_tailor_void_reason,
                  created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    data.get("tenant_id") or "default",
                    data["ticket_tailor_order_id"],
                    data.get("event_id"),
                    data.get("status"),
                    data.get("payment_status"),
                    data.get("currency"),
                    data.get("amount_minor"),
                    data.get("buyer_email"),
                    data.get("buyer_name"),
                    data["raw_payload_json"],
                    data.get("nicky_payment_request_id"),
                    data.get("nicky_bill_short_id"),
                    data.get("nicky_receiver_short_id"),
                    data.get("nicky_payment_url"),
                    data.get("nicky_status"),
                    data.get("ticket_tailor_confirmed_at"),
                    data.get("ticket_tailor_tickets_voided_at"),
                    data.get("ticket_tailor_void_reason"),
                    data.get("created_at"),
                    data.get("updated_at"),
                ),
            )

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection, table_name: str, column_name: str, column_type: str
    ) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        }
        if column_name not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_tenant(self, tenant: TenantConfig) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tenants(
                  tenant_id, name, active, ticket_tailor_api_key,
                  ticket_tailor_webhook_signing_secret,
                  ticket_tailor_offline_payment_keywords, nicky_api_key,
                  nicky_default_blockchain_asset_id, nicky_receiver_short_id,
                  nicky_webhook_token, nicky_webhook_type,
                  auto_create_nicky_payment_request,
                  auto_confirm_ticket_tailor_payments, nicky_send_notification,
                  skip_nicky, dry_run
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET
                  name = excluded.name,
                  active = excluded.active,
                  ticket_tailor_api_key = excluded.ticket_tailor_api_key,
                  ticket_tailor_webhook_signing_secret = excluded.ticket_tailor_webhook_signing_secret,
                  ticket_tailor_offline_payment_keywords = excluded.ticket_tailor_offline_payment_keywords,
                  nicky_api_key = excluded.nicky_api_key,
                  nicky_default_blockchain_asset_id = excluded.nicky_default_blockchain_asset_id,
                  nicky_receiver_short_id = excluded.nicky_receiver_short_id,
                  nicky_webhook_token = excluded.nicky_webhook_token,
                  nicky_webhook_type = excluded.nicky_webhook_type,
                  auto_create_nicky_payment_request = excluded.auto_create_nicky_payment_request,
                  auto_confirm_ticket_tailor_payments = excluded.auto_confirm_ticket_tailor_payments,
                  nicky_send_notification = excluded.nicky_send_notification,
                  skip_nicky = excluded.skip_nicky,
                  dry_run = excluded.dry_run,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    tenant.tenant_id,
                    tenant.name,
                    int(tenant.active),
                    tenant.ticket_tailor_api_key,
                    tenant.ticket_tailor_webhook_signing_secret,
                    keywords_to_csv(tenant.ticket_tailor_offline_payment_keywords),
                    tenant.nicky_api_key,
                    tenant.nicky_default_blockchain_asset_id,
                    tenant.nicky_receiver_short_id,
                    tenant.nicky_webhook_token,
                    tenant.nicky_webhook_type,
                    int(tenant.auto_create_nicky_payment_request),
                    int(tenant.auto_confirm_ticket_tailor_payments),
                    int(tenant.nicky_send_notification),
                    int(tenant.skip_nicky),
                    int(tenant.dry_run),
                ),
            )

    def get_tenant(self, tenant_id: str) -> TenantConfig | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tenants WHERE tenant_id = ?",
                (tenant_id,),
            ).fetchone()
        return tenant_from_row(row) if row else None

    def list_tenants(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        query: str | None = None,
        active: str | None = None,
        configuration: str | None = None,
    ) -> list[TenantConfig]:
        with self.connect() as conn:
            where_clause, params = self._tenant_filters(query, active, configuration)
            limit_clause = ""
            if limit is not None:
                limit_clause = "LIMIT ? OFFSET ?"
                params.extend([max(1, limit), max(0, offset)])
            rows = conn.execute(
                f"""
                SELECT * FROM tenants
                {where_clause}
                ORDER BY created_at DESC, tenant_id ASC
                {limit_clause}
                """,
                params,
            ).fetchall()
        return [tenant_from_row(row) for row in rows]

    def count_tenants(
        self,
        *,
        query: str | None = None,
        active: str | None = None,
        configuration: str | None = None,
    ) -> int:
        with self.connect() as conn:
            where_clause, params = self._tenant_filters(query, active, configuration)
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM tenants {where_clause}",
                params,
            ).fetchone()
            return int(row["count"] if row else 0)

    @staticmethod
    def _tenant_filters(
        query: str | None, active: str | None, configuration: str | None
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if query:
            conditions.append("(tenant_id LIKE ? OR name LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like])
        if active == "active":
            conditions.append("active = 1")
        elif active == "inactive":
            conditions.append("active = 0")
        if configuration == "complete":
            conditions.append("ticket_tailor_api_key != ''")
            conditions.append("nicky_api_key != ''")
            conditions.append("nicky_default_blockchain_asset_id != ''")
        elif configuration == "missing":
            conditions.append(
                "(ticket_tailor_api_key = '' OR nicky_api_key = '' OR nicky_default_blockchain_asset_id = '')"
            )
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return where_clause, params

    def insert_webhook_event(
        self,
        *,
        tenant_id: str,
        source: str,
        event_id: str,
        event_type: str,
        raw_body: bytes,
    ) -> bool:
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO webhook_events(
                      tenant_id, source, event_id, event_type, status, raw_body
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tenant_id,
                        source,
                        event_id,
                        event_type,
                        "received",
                        raw_body.decode("utf-8"),
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                row = conn.execute(
                    """
                    SELECT status FROM webhook_events
                    WHERE tenant_id = ? AND source = ? AND event_id = ?
                    """,
                    (tenant_id, source, event_id),
                ).fetchone()
                if row and row["status"] == "failed":
                    conn.execute(
                        """
                        UPDATE webhook_events
                        SET event_type = ?,
                            status = ?,
                            raw_body = ?,
                            error = NULL,
                            processed_at = NULL,
                            received_at = CURRENT_TIMESTAMP
                        WHERE tenant_id = ? AND source = ? AND event_id = ?
                        """,
                        (
                            event_type,
                            "received",
                            raw_body.decode("utf-8"),
                            tenant_id,
                            source,
                            event_id,
                        ),
                    )
                    return True
                return False

    def mark_webhook_event(
        self, tenant_id: str, source: str, event_id: str, status: str, error: str = ""
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE webhook_events
                SET status = ?, error = ?, processed_at = CURRENT_TIMESTAMP
                WHERE tenant_id = ? AND source = ? AND event_id = ?
                """,
                (status, error or None, tenant_id, source, event_id),
            )

    def upsert_order(
        self, tenant_id: str, order: dict[str, Any], raw_payload: dict[str, Any]
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO integration_orders(
                  tenant_id, ticket_tailor_order_id, event_id, status, payment_status,
                  currency, amount_minor, buyer_email, buyer_name, raw_payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, ticket_tailor_order_id) DO UPDATE SET
                  event_id = excluded.event_id,
                  status = excluded.status,
                  payment_status = excluded.payment_status,
                  currency = excluded.currency,
                  amount_minor = excluded.amount_minor,
                  buyer_email = excluded.buyer_email,
                  buyer_name = excluded.buyer_name,
                  raw_payload_json = excluded.raw_payload_json,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    tenant_id,
                    order["ticket_tailor_order_id"],
                    order.get("event_id"),
                    order.get("status"),
                    order.get("payment_status"),
                    order.get("currency"),
                    order.get("amount_minor"),
                    order.get("buyer_email"),
                    order.get("buyer_name"),
                    json.dumps(raw_payload, separators=(",", ":"), ensure_ascii=False),
                ),
            )

    def update_nicky_payment_request(
        self,
        *,
        tenant_id: str,
        ticket_tailor_order_id: str,
        payment_request_id: str | None,
        bill_short_id: str | None,
        receiver_short_id: str | None,
        payment_url: str | None,
        status: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE integration_orders
                SET nicky_payment_request_id = ?,
                    nicky_bill_short_id = ?,
                    nicky_receiver_short_id = ?,
                    nicky_payment_url = ?,
                    nicky_status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = ? AND ticket_tailor_order_id = ?
                """,
                (
                    payment_request_id,
                    bill_short_id,
                    receiver_short_id,
                    payment_url,
                    status,
                    tenant_id,
                    ticket_tailor_order_id,
                ),
            )

    def update_nicky_status(
        self, tenant_id: str, payment_request_id: str, status: str
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM integration_orders
                WHERE tenant_id = ? AND nicky_payment_request_id = ?
                """,
                (tenant_id, payment_request_id),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """
                UPDATE integration_orders
                SET nicky_status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = ? AND nicky_payment_request_id = ?
                """,
                (status, tenant_id, payment_request_id),
            )
            return row

    def mark_ticket_tailor_confirmed(self, tenant_id: str, ticket_tailor_order_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE integration_orders
                SET ticket_tailor_confirmed_at = CURRENT_TIMESTAMP,
                    payment_status = 'confirmed',
                    updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = ? AND ticket_tailor_order_id = ?
                """,
                (tenant_id, ticket_tailor_order_id),
            )

    def mark_ticket_tailor_tickets_voided(
        self, tenant_id: str, ticket_tailor_order_id: str, reason: str
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE integration_orders
                SET ticket_tailor_tickets_voided_at = CURRENT_TIMESTAMP,
                    ticket_tailor_void_reason = ?,
                    payment_status = 'voided',
                    updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = ? AND ticket_tailor_order_id = ?
                """,
                (reason, tenant_id, ticket_tailor_order_id),
            )

    def list_expirable_orders(
        self, *, expiration_hours: float, limit: int, tenant_id: str | None = None
    ) -> list[sqlite3.Row]:
        if expiration_hours <= 0:
            return []
        resolved_limit = max(1, limit)
        cutoff = f"-{expiration_hours} hours"
        with self.connect() as conn:
            params: list[Any] = [cutoff]
            tenant_filter = ""
            if tenant_id:
                tenant_filter = "AND tenant_id = ?"
                params.append(tenant_id)
            params.append(resolved_limit)
            return list(
                conn.execute(
                    f"""
                    SELECT * FROM integration_orders
                    WHERE ticket_tailor_confirmed_at IS NULL
                      AND ticket_tailor_tickets_voided_at IS NULL
                      AND LOWER(COALESCE(nicky_status, '')) != 'finished'
                      AND datetime(created_at) <= datetime('now', ?)
                      {tenant_filter}
                    ORDER BY created_at ASC, tenant_id ASC, ticket_tailor_order_id ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            )

    def get_order(self, tenant_id: str, ticket_tailor_order_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM integration_orders
                WHERE tenant_id = ? AND ticket_tailor_order_id = ?
                """,
                (tenant_id, ticket_tailor_order_id),
            ).fetchone()

    def list_orders(
        self,
        limit: int = 50,
        offset: int = 0,
        tenant_id: str | None = None,
        updated_from: str | None = None,
        updated_to: str | None = None,
        order_state: str | None = None,
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            where_clause, params = self._order_filters(
                tenant_id, updated_from, updated_to, order_state
            )
            params.extend([max(1, limit), max(0, offset)])
            return list(
                conn.execute(
                    f"""
                    SELECT * FROM integration_orders
                    {where_clause}
                    ORDER BY updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    params,
                ).fetchall()
            )

    def count_orders(
        self,
        *,
        tenant_id: str | None = None,
        updated_from: str | None = None,
        updated_to: str | None = None,
        order_state: str | None = None,
    ) -> int:
        with self.connect() as conn:
            where_clause, params = self._order_filters(
                tenant_id, updated_from, updated_to, order_state
            )
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM integration_orders {where_clause}",
                params,
            ).fetchone()
            return int(row["count"] if row else 0)

    @staticmethod
    def _order_filters(
        tenant_id: str | None,
        updated_from: str | None,
        updated_to: str | None,
        order_state: str | None,
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if tenant_id:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)
        if updated_from:
            conditions.append("date(updated_at) >= date(?)")
            params.append(updated_from)
        if updated_to:
            conditions.append("date(updated_at) <= date(?)")
            params.append(updated_to)
        if order_state == "confirmed":
            conditions.append("ticket_tailor_confirmed_at IS NOT NULL")
        elif order_state == "tickets_voided":
            conditions.append("ticket_tailor_tickets_voided_at IS NOT NULL")
        elif order_state == "pending":
            conditions.append("ticket_tailor_confirmed_at IS NULL")
            conditions.append("ticket_tailor_tickets_voided_at IS NULL")
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return where_clause, params

    def list_webhook_events(
        self,
        limit: int = 50,
        offset: int = 0,
        tenant_id: str | None = None,
        received_from: str | None = None,
        received_to: str | None = None,
        status: str | None = None,
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            where_clause, params = self._webhook_filters(
                tenant_id, received_from, received_to, status
            )
            params.extend([max(1, limit), max(0, offset)])
            return list(
                conn.execute(
                    f"""
                    SELECT tenant_id, source, event_id, event_type, received_at,
                           processed_at, status, error
                    FROM webhook_events
                    {where_clause}
                    ORDER BY received_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    params,
                ).fetchall()
            )

    def count_webhook_events(
        self,
        *,
        tenant_id: str | None = None,
        received_from: str | None = None,
        received_to: str | None = None,
        status: str | None = None,
    ) -> int:
        with self.connect() as conn:
            where_clause, params = self._webhook_filters(
                tenant_id, received_from, received_to, status
            )
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM webhook_events {where_clause}",
                params,
            ).fetchone()
            return int(row["count"] if row else 0)

    @staticmethod
    def _webhook_filters(
        tenant_id: str | None,
        received_from: str | None,
        received_to: str | None,
        status: str | None,
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if tenant_id:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)
        if received_from:
            conditions.append("date(received_at) >= date(?)")
            params.append(received_from)
        if received_to:
            conditions.append("date(received_at) <= date(?)")
            params.append(received_to)
        if status:
            conditions.append("status = ?")
            params.append(status)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return where_clause, params

    def list_order_logs(
        self,
        tenant_id: str,
        ticket_tailor_order_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT id, tenant_id, ticket_tailor_order_id, event_type, message,
                           payload_json, created_at
                    FROM order_logs
                    WHERE tenant_id = ? AND ticket_tailor_order_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (tenant_id, ticket_tailor_order_id, max(1, limit), max(0, offset)),
                ).fetchall()
            )

    def count_order_logs(self, tenant_id: str, ticket_tailor_order_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM order_logs
                WHERE tenant_id = ? AND ticket_tailor_order_id = ?
                """,
                (tenant_id, ticket_tailor_order_id),
            ).fetchone()
            return int(row["count"] if row else 0)

    def log(
        self,
        tenant_id: str,
        ticket_tailor_order_id: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO order_logs(
                  tenant_id, ticket_tailor_order_id, event_type, message, payload_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    tenant_id,
                    ticket_tailor_order_id,
                    event_type,
                    message,
                    json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                    if payload is not None
                    else None,
                ),
            )
