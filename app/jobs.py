from __future__ import annotations

import argparse
import asyncio
import json
from typing import Sequence

from app.job_runner import run_expire_overdue_orders
from app.tenants import normalize_tenant_id


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Nicky Ticket Tailor maintenance jobs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    expire = subparsers.add_parser(
        "expire-overdue-orders",
        help="Void Ticket Tailor tickets for overdue pending Nicky payment requests.",
    )
    expire.add_argument("--tenant-id", default=None)
    expire.add_argument("--expiration-hours", type=float, default=None)
    expire.add_argument("--batch-size", type=int, default=None)
    return parser


async def run_command(args: argparse.Namespace) -> dict:
    if args.command == "expire-overdue-orders":
        tenant_id = normalize_tenant_id(args.tenant_id) if args.tenant_id else None
        return await run_expire_overdue_orders(
            tenant_id=tenant_id,
            expiration_hours=args.expiration_hours,
            batch_size=args.batch_size,
        )
    raise ValueError(f"Unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = asyncio.run(run_command(args))
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
