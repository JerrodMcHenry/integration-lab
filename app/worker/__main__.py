"""Run the webhook event worker.

    python -m app.worker                 # run until SIGTERM/SIGINT
    python -m app.worker --once          # claim and process a single batch, then exit
    python -m app.worker --max-events 50 --max-seconds 120
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from app.api.deps import get_engine, get_sync_service, get_tenant_repository
from app.core.config import get_settings
from app.repositories.sync_history import RepositoryError
from app.repositories.webhook_events import PostgresWebhookEventRepository
from app.worker.runner import EventWorker, WorkerConfigurationError, resolve_lease_seconds


def build_worker(worker_id: str | None = None) -> EventWorker:
    settings = get_settings()
    engine = get_engine()
    if engine is None:
        raise WorkerConfigurationError("DATABASE_URL is required: the worker processes durable events")
    return EventWorker(events=PostgresWebhookEventRepository(engine), service=get_sync_service(),
                       tenants=get_tenant_repository(), lease_seconds=resolve_lease_seconds(settings),
                       batch_size=settings.worker.batch_size, max_attempts=settings.worker.max_attempts,
                       poll_interval=settings.worker.poll_interval_seconds, worker_id=worker_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.worker")
    parser.add_argument("--once", action="store_true", help="process one batch and exit")
    parser.add_argument("--max-events", type=int, default=None)
    parser.add_argument("--max-seconds", type=float, default=None)
    parser.add_argument("--worker-id", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        worker = build_worker(args.worker_id)
    except WorkerConfigurationError as exc:
        print(f"worker configuration error: {exc}", file=sys.stderr)
        return 2

    def _graceful(signum, _frame):
        logging.getLogger(__name__).info("worker.signal signal=%s: finishing current event", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    if args.once:
        try:
            worker.run_once()
        except RepositoryError:
            return 1
        return 0
    worker.run(max_events=args.max_events, max_seconds=args.max_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
