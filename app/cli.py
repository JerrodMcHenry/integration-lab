"""Operator CLI.

    python -m app.cli seed-dev [--file seed/dev_tenants.json]
    python -m app.cli retention [--execute]      # default: dry run (counts only)

``seed-dev`` inserts development tenants, their integration settings and
their first mapping version **only if absent**. It never updates or deletes
anything, and it refuses to run in production-like environments.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.core.config import ConfigurationError, get_settings
from app.db.engine import build_engine
from app.mapping.mapper import FieldMapper
from app.repositories.tenants import seed_postgres
from app.seed import DEFAULT_SEED_FILE, load_seed


def seed_dev(path: Path) -> int:
    settings = get_settings()
    if settings.is_production:
        print("refusing to seed development tenants in a production environment", file=sys.stderr)
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


def retention(execute_flag: bool) -> int:
    import getpass

    from app import retention as ret

    settings = get_settings()
    engine = build_engine(settings.database, application_name="integrationlab-retention")
    try:
        policy = settings.retention
        print(f"policy: succeeded events > {policy.succeeded_event_days}d, ignored events > "
              f"{policy.ignored_event_days}d, unreferenced sync records > {policy.sync_record_days}d "
              f"(failed/dead_letter/pending/processing events and the audit log are never deleted)")
        if not execute_flag:
            report = ret.plan(engine, policy)
            print(f"DRY RUN: would delete {report.total()} rows")
        else:
            try:
                report = ret.execute(engine, policy, actor=f"cli:{getpass.getuser()}")
            except ret.RetentionDisabled as exc:
                print(f"refusing: {exc}", file=sys.stderr)
                return 3
            print(f"DELETED {report.total()} rows (audited)")
        for name, per in report.counts.items():
            print(f"  {name}: " + (", ".join(f"{t}={n}" for t, n in sorted(per.items())) or "0"))
    finally:
        engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed-dev", help="insert development tenants if absent")
    seed.add_argument("--file", type=Path, default=DEFAULT_SEED_FILE)
    rp = sub.add_parser("retention", help="apply the retention policy (dry run unless --execute)")
    rp.add_argument("--execute", action="store_true",
                    help="actually delete (also requires RETENTION_ENABLED=true)")
    args = parser.parse_args(argv)
    try:
        get_settings()
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "seed-dev":
        return seed_dev(args.file)
    if args.command == "retention":
        return retention(args.execute)
    return 1  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
