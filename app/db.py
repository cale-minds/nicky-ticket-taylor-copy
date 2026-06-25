from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import Connection, CursorResult, Engine, RowMapping

from app.config import sqlite_url_from_path
from app.db_models import integration_orders, order_logs, tenants, users, webhook_events
from app.tenants import TenantConfig, tenant_from_row


class Record(dict):
    """Small mapping wrapper that preserves sqlite3.Row-style access."""


class DatabaseResult:
    def __init__(self, result: CursorResult[Any]) -> None:
        self._result = result
        self.rowcount = result.rowcount

    def fetchone(self) -> Record | None:
        if not self._result.returns_rows:
            return None
        row = self._result.mappings().fetchone()
        return record(row)

    def fetchall(self) -> list[Record]:
        if not self._result.returns_rows:
            return []
        return records(self._result.mappings().fetchall())


class DatabaseConnection:
    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def execute(
        self,
        statement: str | sa.Executable,
        parameters: Sequence[Any] | Mapping[str, Any] | None = None,
    ) -> DatabaseResult:
        if isinstance(statement, str):
            sql, params = positional_sql(statement, parameters)
            return DatabaseResult(self._connection.execute(sa.text(sql), params))
        return DatabaseResult(self._connection.execute(statement, parameters or {}))


def record(row: RowMapping | Mapping[str, Any] | None) -> Record | None:
    if row is None:
        return None
    return Record(dict(row))


def records(rows: Sequence[RowMapping | Mapping[str, Any]]) -> list[Record]:
    return [Record(dict(row)) for row in rows]


def positional_sql(
    statement: str,
    parameters: Sequence[Any] | Mapping[str, Any] | None,
) -> tuple[str, Mapping[str, Any]]:
    if parameters is None:
        return statement, {}
    if isinstance(parameters, Mapping):
        return statement, parameters

    values = list(parameters)
    parts = statement.split("?")
    if len(parts) - 1 != len(values):
        raise ValueError("SQL positional parameter count does not match supplied values")

    rewritten: list[str] = []
    params: dict[str, Any] = {}
    for index, part in enumerate(parts[:-1]):
        name = f"p{index}"
        rewritten.append(part)
        rewritten.append(f":{name}")
        params[name] = values[index]
    rewritten.append(parts[-1])
    return "".join(rewritten), params


def database_url_from_value(value: str | Path) -> str:
    if isinstance(value, Path):
        return sqlite_url_from_path(value)
    text = str(value)
    if "://" in text:
        return text
    return sqlite_url_from_path(Path(text))


def prepare_sqlite_parent(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    raw_path = database_url.removeprefix("sqlite:///")
    if raw_path in {"", ":memory:"}:
        return
    Path(raw_path).parent.mkdir(parents=True, exist_ok=True)


def create_engine(database_url: str) -> Engine:
    prepare_sqlite_parent(database_url)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return sa.create_engine(
        database_url,
        future=True,
        pool_pre_ping=not database_url.startswith("sqlite"),
        connect_args=connect_args,
    )


def run_migrations(database_url: str) -> None:
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def date_start(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.combine(date.fromisoformat(value), time.min)


def date_end_exclusive(value: str | None) -> datetime | None:
    start = date_start(value)
    return start + timedelta(days=1) if start else None


class Database:
    def __init__(self, database_url_or_path: str | Path) -> None:
        self.database_url = database_url_from_value(database_url_or_path)
        self.engine = create_engine(self.database_url)

    def init(self) -> None:
        run_migrations(self.database_url)

    @contextmanager
    def connect(self) -> Iterator[DatabaseConnection]:
        connection = self.engine.connect()
        transaction = connection.begin()
        try:
            yield DatabaseConnection(connection)
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _begin(self) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            yield connection

    def upsert_user(self, auth_subject: str, email: str, name: str = "") -> None:
        if not auth_subject:
            return
        with self._begin() as conn:
            exists = conn.execute(
                select(users.c.auth_subject).where(users.c.auth_subject == auth_subject)
            ).first()
            if exists:
                conn.execute(
                    users.update()
                    .where(users.c.auth_subject == auth_subject)
                    .values(email=email or "", name=name or "", updated_at=func.now())
                )
            else:
                conn.execute(
                    users.insert().values(
                        auth_subject=auth_subject, email=email or "", name=name or ""
                    )
                )

    def find_user_subject_by_email(self, email: str) -> str | None:
        if not email:
            return None
        with self._begin() as conn:
            row = conn.execute(
                select(users.c.auth_subject)
                .where(func.lower(users.c.email) == email.strip().lower())
                .order_by(users.c.updated_at.desc())
                .limit(1)
            ).first()
        return str(row[0]) if row else None

    def upsert_tenant(self, tenant: TenantConfig) -> None:
        values = {
            "tenant_id": tenant.tenant_id,
            "name": tenant.name,
            "active": bool(tenant.active),
            "nicky_user_uuid": tenant.nicky_user_uuid,
            "nicky_user_short_id": tenant.nicky_user_short_id,
            "nicky_user_email": tenant.nicky_user_email,
            "ticket_tailor_api_key": tenant.ticket_tailor_api_key,
            "ticket_tailor_webhook_signing_secret": tenant.ticket_tailor_webhook_signing_secret,
            "nicky_api_key": tenant.nicky_api_key,
            "nicky_default_blockchain_asset_id": tenant.nicky_default_blockchain_asset_id,
            "nicky_receiver_short_id": tenant.nicky_receiver_short_id,
            "nicky_webhook_token": tenant.nicky_webhook_token,
            "nicky_webhook_id": tenant.nicky_webhook_id,
            "nicky_webhook_type": tenant.nicky_webhook_type,
            "nicky_send_notification": bool(tenant.nicky_send_notification),
            "owner_auth_subject": tenant.owner_auth_subject,
        }
        with self._begin() as conn:
            exists = conn.execute(
                select(tenants.c.tenant_id).where(tenants.c.tenant_id == tenant.tenant_id)
            ).first()
            if exists:
                update_values = dict(values)
                update_values.pop("tenant_id")
                update_values["updated_at"] = func.now()
                conn.execute(
                    tenants.update()
                    .where(tenants.c.tenant_id == tenant.tenant_id)
                    .values(**update_values)
                )
            else:
                conn.execute(tenants.insert().values(**values))

    def deactivate_tenant(self, tenant_id: str) -> None:
        with self._begin() as conn:
            conn.execute(
                tenants.update()
                .where(tenants.c.tenant_id == tenant_id)
                .values(active=False, updated_at=func.now())
            )

    def find_active_tenant_by_owner(self, owner_auth_subject: str) -> TenantConfig | None:
        if not owner_auth_subject:
            return None
        with self._begin() as conn:
            row = conn.execute(
                select(tenants)
                .where(tenants.c.owner_auth_subject == owner_auth_subject, tenants.c.active == sa.true())
                .limit(1)
            ).mappings().first()
        return tenant_from_row(record(row)) if row else None

    def find_active_tenant_by_user_email(self, email: str) -> TenantConfig | None:
        if not email:
            return None
        with self._begin() as conn:
            row = conn.execute(
                select(tenants)
                .where(tenants.c.nicky_user_email == email, tenants.c.active == sa.true())
                .limit(1)
            ).mappings().first()
        return tenant_from_row(record(row)) if row else None

    def find_active_tenant_by_api_key(
        self,
        column: str,
        api_key: str,
        *,
        exclude_tenant_id: str | None = None,
    ) -> TenantConfig | None:
        if column not in {"nicky_api_key", "ticket_tailor_api_key"}:
            raise ValueError("Unsupported tenant API key column")
        if not api_key:
            return None
        conditions = [tenants.c[column] == api_key, tenants.c.active == sa.true()]
        if exclude_tenant_id:
            conditions.append(tenants.c.tenant_id != exclude_tenant_id)
        with self._begin() as conn:
            row = conn.execute(
                select(tenants).where(and_(*conditions)).limit(1)
            ).mappings().first()
        return tenant_from_row(record(row)) if row else None

    def get_tenant(self, tenant_id: str) -> TenantConfig | None:
        with self._begin() as conn:
            row = conn.execute(
                select(tenants).where(tenants.c.tenant_id == tenant_id)
            ).mappings().first()
        return tenant_from_row(record(row)) if row else None

    def list_tenants(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        query: str | None = None,
        active: str | None = None,
        configuration: str | None = None,
        nicky_user_uuid: str | None = None,
        owner_auth_subject: str | None = None,
    ) -> list[TenantConfig]:
        stmt = (
            select(tenants)
            .where(
                *self._tenant_conditions(
                    query, active, configuration, nicky_user_uuid, owner_auth_subject
                )
            )
            .order_by(tenants.c.created_at.desc(), tenants.c.tenant_id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(max(1, limit)).offset(max(0, offset))
        with self._begin() as conn:
            rows = conn.execute(stmt).mappings().fetchall()
        return [tenant_from_row(row) for row in records(rows)]

    def count_tenants(
        self,
        *,
        query: str | None = None,
        active: str | None = None,
        configuration: str | None = None,
        nicky_user_uuid: str | None = None,
        owner_auth_subject: str | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(tenants).where(
            *self._tenant_conditions(
                query, active, configuration, nicky_user_uuid, owner_auth_subject
            )
        )
        with self._begin() as conn:
            return int(conn.execute(stmt).scalar_one())

    @staticmethod
    def _tenant_conditions(
        query: str | None,
        active: str | None,
        configuration: str | None,
        nicky_user_uuid: str | None = None,
        owner_auth_subject: str | None = None,
    ) -> list[Any]:
        conditions: list[Any] = []
        if query:
            like = f"%{query}%"
            conditions.append(
                or_(
                    tenants.c.tenant_id.like(like),
                    tenants.c.name.like(like),
                    tenants.c.nicky_user_uuid.like(like),
                    tenants.c.nicky_user_short_id.like(like),
                    tenants.c.nicky_user_email.like(like),
                )
            )
        if nicky_user_uuid:
            conditions.append(tenants.c.nicky_user_uuid == nicky_user_uuid)
        if owner_auth_subject:
            conditions.append(tenants.c.owner_auth_subject == owner_auth_subject)
        if active == "active":
            conditions.append(tenants.c.active == sa.true())
        elif active == "inactive":
            conditions.append(tenants.c.active == sa.false())
        if configuration == "complete":
            conditions.extend(
                [
                    tenants.c.ticket_tailor_api_key != "",
                    tenants.c.nicky_api_key != "",
                    tenants.c.nicky_default_blockchain_asset_id != "",
                ]
            )
        elif configuration == "missing":
            conditions.append(
                or_(
                    tenants.c.ticket_tailor_api_key == "",
                    tenants.c.nicky_api_key == "",
                    tenants.c.nicky_default_blockchain_asset_id == "",
                )
            )
        return conditions

    def insert_webhook_event(
        self,
        *,
        tenant_id: str,
        source: str,
        event_id: str,
        event_type: str,
        raw_body: bytes,
    ) -> bool:
        with self._begin() as conn:
            existing = conn.execute(
                select(webhook_events.c.status).where(
                    webhook_events.c.tenant_id == tenant_id,
                    webhook_events.c.source == source,
                    webhook_events.c.event_id == event_id,
                )
            ).mappings().first()
            if existing:
                if existing["status"] == "failed":
                    conn.execute(
                        webhook_events.update()
                        .where(
                            webhook_events.c.tenant_id == tenant_id,
                            webhook_events.c.source == source,
                            webhook_events.c.event_id == event_id,
                        )
                        .values(
                            event_type=event_type,
                            status="received",
                            raw_body=raw_body.decode("utf-8"),
                            error=None,
                            processed_at=None,
                            received_at=func.now(),
                        )
                    )
                    return True
                return False
            conn.execute(
                webhook_events.insert().values(
                    tenant_id=tenant_id,
                    source=source,
                    event_id=event_id,
                    event_type=event_type,
                    status="received",
                    raw_body=raw_body.decode("utf-8"),
                )
            )
            return True

    def mark_webhook_event(
        self, tenant_id: str, source: str, event_id: str, status: str, error: str = ""
    ) -> None:
        with self._begin() as conn:
            conn.execute(
                webhook_events.update()
                .where(
                    webhook_events.c.tenant_id == tenant_id,
                    webhook_events.c.source == source,
                    webhook_events.c.event_id == event_id,
                )
                .values(status=status, error=error or None, processed_at=func.now())
            )

    def upsert_order(
        self, tenant_id: str, order: dict[str, Any], raw_payload: dict[str, Any]
    ) -> None:
        key = {
            "tenant_id": tenant_id,
            "ticket_tailor_order_id": order["ticket_tailor_order_id"],
        }
        values = {
            **key,
            "event_id": order.get("event_id"),
            "status": order.get("status"),
            "payment_status": order.get("payment_status"),
            "currency": order.get("currency"),
            "amount_minor": order.get("amount_minor"),
            "buyer_email": order.get("buyer_email"),
            "buyer_name": order.get("buyer_name"),
            "raw_payload_json": json.dumps(raw_payload, separators=(",", ":"), ensure_ascii=False),
        }
        with self._begin() as conn:
            exists = conn.execute(
                select(integration_orders.c.ticket_tailor_order_id).where(
                    integration_orders.c.tenant_id == tenant_id,
                    integration_orders.c.ticket_tailor_order_id == order["ticket_tailor_order_id"],
                )
            ).first()
            if exists:
                update_values = dict(values)
                update_values.pop("tenant_id")
                update_values.pop("ticket_tailor_order_id")
                update_values["updated_at"] = func.now()
                conn.execute(
                    integration_orders.update()
                    .where(
                        integration_orders.c.tenant_id == tenant_id,
                        integration_orders.c.ticket_tailor_order_id
                        == order["ticket_tailor_order_id"],
                    )
                    .values(**update_values)
                )
            else:
                conn.execute(integration_orders.insert().values(**values))

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
        with self._begin() as conn:
            conn.execute(
                integration_orders.update()
                .where(
                    integration_orders.c.tenant_id == tenant_id,
                    integration_orders.c.ticket_tailor_order_id == ticket_tailor_order_id,
                )
                .values(
                    nicky_payment_request_id=payment_request_id,
                    nicky_bill_short_id=bill_short_id,
                    nicky_receiver_short_id=receiver_short_id,
                    nicky_payment_url=payment_url,
                    nicky_status=status,
                    updated_at=func.now(),
                )
            )

    def update_nicky_status(
        self, tenant_id: str, payment_request_id: str, status: str
    ) -> Record | None:
        with self._begin() as conn:
            row = conn.execute(
                select(integration_orders).where(
                    integration_orders.c.tenant_id == tenant_id,
                    integration_orders.c.nicky_payment_request_id == payment_request_id,
                )
            ).mappings().first()
            if not row:
                return None
            conn.execute(
                integration_orders.update()
                .where(
                    integration_orders.c.tenant_id == tenant_id,
                    integration_orders.c.nicky_payment_request_id == payment_request_id,
                )
                .values(nicky_status=status, updated_at=func.now())
            )
            return record(row)

    def mark_ticket_tailor_confirmed(self, tenant_id: str, ticket_tailor_order_id: str) -> None:
        with self._begin() as conn:
            conn.execute(
                integration_orders.update()
                .where(
                    integration_orders.c.tenant_id == tenant_id,
                    integration_orders.c.ticket_tailor_order_id == ticket_tailor_order_id,
                )
                .values(
                    ticket_tailor_confirmed_at=func.now(),
                    payment_status="confirmed",
                    updated_at=func.now(),
                )
            )

    def mark_ticket_tailor_tickets_voided(
        self, tenant_id: str, ticket_tailor_order_id: str, reason: str
    ) -> None:
        with self._begin() as conn:
            conn.execute(
                integration_orders.update()
                .where(
                    integration_orders.c.tenant_id == tenant_id,
                    integration_orders.c.ticket_tailor_order_id == ticket_tailor_order_id,
                )
                .values(
                    ticket_tailor_tickets_voided_at=func.now(),
                    ticket_tailor_void_reason=reason,
                    payment_status="voided",
                    updated_at=func.now(),
                )
            )

    def list_expirable_orders(
        self, *, expiration_hours: float, limit: int, tenant_id: str | None = None
    ) -> list[Record]:
        if expiration_hours <= 0:
            return []
        cutoff = utc_now() - timedelta(hours=expiration_hours)
        conditions = [
            integration_orders.c.ticket_tailor_confirmed_at.is_(None),
            integration_orders.c.ticket_tailor_tickets_voided_at.is_(None),
            func.lower(func.coalesce(integration_orders.c.nicky_status, "")) != "finished",
            integration_orders.c.created_at <= cutoff,
        ]
        if tenant_id:
            conditions.append(integration_orders.c.tenant_id == tenant_id)
        stmt = (
            select(integration_orders)
            .where(*conditions)
            .order_by(
                integration_orders.c.created_at.asc(),
                integration_orders.c.tenant_id.asc(),
                integration_orders.c.ticket_tailor_order_id.asc(),
            )
            .limit(max(1, limit))
        )
        with self._begin() as conn:
            return records(conn.execute(stmt).mappings().fetchall())

    def get_order(self, tenant_id: str, ticket_tailor_order_id: str) -> Record | None:
        with self._begin() as conn:
            row = conn.execute(
                select(integration_orders).where(
                    integration_orders.c.tenant_id == tenant_id,
                    integration_orders.c.ticket_tailor_order_id == ticket_tailor_order_id,
                )
            ).mappings().first()
        return record(row)

    def list_orders(
        self,
        limit: int = 50,
        offset: int = 0,
        tenant_id: str | None = None,
        updated_from: str | None = None,
        updated_to: str | None = None,
        order_state: str | None = None,
    ) -> list[Record]:
        stmt = (
            select(integration_orders)
            .where(*self._order_conditions(tenant_id, updated_from, updated_to, order_state))
            .order_by(integration_orders.c.updated_at.desc())
            .limit(max(1, limit))
            .offset(max(0, offset))
        )
        with self._begin() as conn:
            return records(conn.execute(stmt).mappings().fetchall())

    def count_orders(
        self,
        *,
        tenant_id: str | None = None,
        updated_from: str | None = None,
        updated_to: str | None = None,
        order_state: str | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(integration_orders).where(
            *self._order_conditions(tenant_id, updated_from, updated_to, order_state)
        )
        with self._begin() as conn:
            return int(conn.execute(stmt).scalar_one())

    @staticmethod
    def _order_conditions(
        tenant_id: str | None,
        updated_from: str | None,
        updated_to: str | None,
        order_state: str | None,
    ) -> list[Any]:
        conditions: list[Any] = []
        if tenant_id:
            conditions.append(integration_orders.c.tenant_id == tenant_id)
        from_date = date_start(updated_from)
        if from_date:
            conditions.append(integration_orders.c.updated_at >= from_date)
        to_date = date_end_exclusive(updated_to)
        if to_date:
            conditions.append(integration_orders.c.updated_at < to_date)
        if order_state == "confirmed":
            conditions.append(integration_orders.c.ticket_tailor_confirmed_at.is_not(None))
        elif order_state == "tickets_voided":
            conditions.append(integration_orders.c.ticket_tailor_tickets_voided_at.is_not(None))
        elif order_state == "pending":
            conditions.append(integration_orders.c.ticket_tailor_confirmed_at.is_(None))
            conditions.append(integration_orders.c.ticket_tailor_tickets_voided_at.is_(None))
        return conditions

    def list_webhook_events(
        self,
        limit: int = 50,
        offset: int = 0,
        tenant_id: str | None = None,
        received_from: str | None = None,
        received_to: str | None = None,
        status: str | None = None,
    ) -> list[Record]:
        stmt = (
            select(
                webhook_events.c.tenant_id,
                webhook_events.c.source,
                webhook_events.c.event_id,
                webhook_events.c.event_type,
                webhook_events.c.received_at,
                webhook_events.c.processed_at,
                webhook_events.c.status,
                webhook_events.c.error,
            )
            .where(*self._webhook_conditions(tenant_id, received_from, received_to, status))
            .order_by(webhook_events.c.received_at.desc())
            .limit(max(1, limit))
            .offset(max(0, offset))
        )
        with self._begin() as conn:
            return records(conn.execute(stmt).mappings().fetchall())

    def count_webhook_events(
        self,
        *,
        tenant_id: str | None = None,
        received_from: str | None = None,
        received_to: str | None = None,
        status: str | None = None,
    ) -> int:
        stmt = select(func.count()).select_from(webhook_events).where(
            *self._webhook_conditions(tenant_id, received_from, received_to, status)
        )
        with self._begin() as conn:
            return int(conn.execute(stmt).scalar_one())

    @staticmethod
    def _webhook_conditions(
        tenant_id: str | None,
        received_from: str | None,
        received_to: str | None,
        status: str | None,
    ) -> list[Any]:
        conditions: list[Any] = []
        if tenant_id:
            conditions.append(webhook_events.c.tenant_id == tenant_id)
        from_date = date_start(received_from)
        if from_date:
            conditions.append(webhook_events.c.received_at >= from_date)
        to_date = date_end_exclusive(received_to)
        if to_date:
            conditions.append(webhook_events.c.received_at < to_date)
        if status:
            conditions.append(webhook_events.c.status == status)
        return conditions

    def list_order_logs(
        self,
        tenant_id: str,
        ticket_tailor_order_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Record]:
        stmt = (
            select(
                order_logs.c.id,
                order_logs.c.tenant_id,
                order_logs.c.ticket_tailor_order_id,
                order_logs.c.event_type,
                order_logs.c.message,
                order_logs.c.payload_json,
                order_logs.c.created_at,
            )
            .where(
                order_logs.c.tenant_id == tenant_id,
                order_logs.c.ticket_tailor_order_id == ticket_tailor_order_id,
            )
            .order_by(order_logs.c.created_at.desc(), order_logs.c.id.desc())
            .limit(max(1, limit))
            .offset(max(0, offset))
        )
        with self._begin() as conn:
            return records(conn.execute(stmt).mappings().fetchall())

    def count_order_logs(self, tenant_id: str, ticket_tailor_order_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(order_logs)
            .where(
                order_logs.c.tenant_id == tenant_id,
                order_logs.c.ticket_tailor_order_id == ticket_tailor_order_id,
            )
        )
        with self._begin() as conn:
            return int(conn.execute(stmt).scalar_one())

    def log(
        self,
        tenant_id: str,
        ticket_tailor_order_id: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._begin() as conn:
            conn.execute(
                order_logs.insert().values(
                    tenant_id=tenant_id,
                    ticket_tailor_order_id=ticket_tailor_order_id,
                    event_type=event_type,
                    message=message,
                    payload_json=(
                        json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                        if payload is not None
                        else None
                    ),
                )
            )


def normalize_sqlserver_url(url: str) -> str:
    # Reserved for future URL normalization without touching callers.
    return url


_QUESTION_MARK_RE = re.compile(r"\?")
