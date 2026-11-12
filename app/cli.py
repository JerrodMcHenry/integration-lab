"""Operator CLI.

    python -m app.cli seed-dev [--file seed/dev_tenants.json]

``seed-dev`` inserts development tenants, their integration settings and
their first mapping version **only if absent**. It never updates or deletes
anything, and it refuses to run in production-like environments.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core.config import get_settings
from app.db.engine import build_engine
from app.mapping.mapper import FieldMapper
from app.repositories.tenants import seed_postgres
from app.seed import DEFAULT_SEED_FILE, load_seed


def seed_dev(path: Path) -> int:
    settings = get_settings()
    if settings.is_production:
        print("refusing to seed development tenants in a production environment", file=sys.stderr)
        return 2
    if not settings.database.url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    engine = build_engine(settings.database, application_name="integrationlab-cli")
    try:
        for item in load_seed(path):
            if item.mapping:
                FieldMapper.from_dict(item.mapping)  # validate before storing
            created = seed_postgres(engine, item.tenant, item.integration, item.mapping)
            summary = ", ".join(f"{k}={'created' if v else 'exists'}" for k, v in created.items())
            print(f"tenant {item.tenant.id}: {summary}")
    finally:
        engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed-dev", help="insert development tenants if absent")
    seed.add_argument("--file", type=Path, default=DEFAULT_SEED_FILE)
    args = parser.parse_args(argv)
    if args.command == "seed-dev":
        return seed_dev(args.file)
    return 1  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
