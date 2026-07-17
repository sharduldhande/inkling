"""Entrypoint: `python -m inkling <role>`.

Roles api/worker/scheduler/delivery/calendar/jobs must each be independently
runnable (§1); `all` runs everything in one asyncio process. api/worker/
scheduler/delivery/all are wired; calendar/jobs land with their milestones.
"""

import argparse
import json
import logging
import sys

from inkling.config import Config

SERVICE_ROLES = ["all", "api", "worker", "scheduler", "delivery", "calendar", "jobs"]


def main() -> int:
    parser = argparse.ArgumentParser(prog="inkling")
    sub = parser.add_subparsers(dest="role", required=True)

    for role in SERVICE_ROLES:
        sub.add_parser(role)

    sub.add_parser("migrate")

    seed = sub.add_parser("seed")
    seed.add_argument("--user", required=True)
    seed.add_argument("--name", required=True)
    seed.add_argument("--tz", required=True, help="IANA timezone, e.g. America/New_York")
    seed.add_argument("--ntfy-topic", required=True)
    seed.add_argument(
        "--inference-profile",
        required=True,
        help='JSON, e.g. {"type":"subscription","config_dir":"/home/user/.inkling/claude-shardul"}',
    )

    args = parser.parse_args()
    config = Config()
    logging.basicConfig(level=config.log_level.upper())

    if args.role == "migrate":
        from inkling.db.migrate import apply_all

        applied = apply_all(config.database_url_migrate)
        print(f"applied {len(applied)} migration(s): {applied or 'up to date'}")
        return 0

    if args.role == "seed":
        from inkling.db.seed import seed_user

        seed_user(
            config.database_url_migrate,
            user_id=args.user,
            display_name=args.name,
            timezone=args.tz,
            ntfy_topic=args.ntfy_topic,
            inference_profile=json.loads(args.inference_profile),
        )
        print(f"seeded user {args.user!r}")
        return 0

    if args.role in ("api", "worker", "scheduler", "delivery", "all"):
        import asyncio

        return asyncio.run(_serve(config, args.role))

    print(f"role {args.role!r} is not implemented yet", file=sys.stderr)
    return 1


async def _serve(config: Config, role: str) -> int:
    """Run the API, the workers, the scheduler, or all in one process (§1).

    - api: HTTP only; triggers are recorded, another process turns them.
    - worker: turns only; no HTTP socket.
    - scheduler: timers only; refreshes every 60 s (the §10 reconcile scan —
      no in-process invalidation event reaches a separate process).
    - delivery: outbox drainer only (§11); its 60 s reconcile plays the same
      role as the scheduler's.
    - all: everything in one asyncio process — the initial systemd shape;
      scheduler/drainer nudge workers and workers poke them back in-process.
    """
    import asyncio

    from inkling.api.auth import load_roster
    from inkling.api.broker import Broker
    from inkling.db.client import Database
    from inkling.delivery.ntfy import NtfyClient
    from inkling.delivery.outbox import Drainer
    from inkling.inference.profile import ProfileRoutingGateway
    from inkling.scheduler.scheduler import (
        RECONCILE_SECONDS,
        REFRESH_SECONDS,
        Scheduler,
    )
    from inkling.turns.supervisor import Supervisor

    roster = load_roster(config.chat_tokens_file)
    db = Database(config.database_url)
    await db.open()
    scheduler_task = None
    scheduler = None
    drainer_task = None
    drainer = None
    ntfy = None
    try:
        broker = Broker()
        supervisor = None
        if role in ("worker", "all"):
            supervisor = Supervisor(
                db,
                roster.user_ids,
                ProfileRoutingGateway(db),
                subscriber=broker.publish,
            )
        if role in ("scheduler", "all"):
            scheduler = Scheduler(
                db,
                roster.user_ids,
                refresh_seconds=(
                    RECONCILE_SECONDS if role == "scheduler" else REFRESH_SECONDS
                ),
                nudge=supervisor.nudge if supervisor else None,
            )
            scheduler_task = asyncio.create_task(
                scheduler.run_forever(), name="scheduler"
            )
        if role in ("delivery", "all"):
            ntfy = NtfyClient(config.ntfy_base)
            drainer = Drainer(
                db,
                roster.user_ids,
                channel=ntfy,
                operator_topic=config.operator_ntfy,
            )
            drainer_task = asyncio.create_task(
                drainer.run_forever(), name="delivery"
            )
        if supervisor is not None:
            # Wire after everything exists: worker commits poke the timers.
            for worker in supervisor.workers.values():
                if scheduler is not None:
                    worker.on_schedule_change = scheduler.wake
                if drainer is not None:
                    worker.on_delivery_queued = drainer.wake

        if role == "scheduler":
            try:
                await scheduler_task
            finally:
                scheduler.stop()
            return 0

        if role == "delivery":
            try:
                await drainer_task
            finally:
                drainer.stop()
            return 0

        if role == "worker":
            await supervisor.start()
            try:
                await asyncio.Event().wait()  # until SIGINT/SIGTERM
            finally:
                await supervisor.stop()
            return 0

        import uvicorn

        from inkling.api.server import create_app

        app = create_app(db, roster, broker=broker, supervisor=supervisor)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.bind,
                port=config.port,
                log_level=config.log_level,
            )
        )
        await server.serve()  # lifespan starts/stops the supervisor
        return 0
    finally:
        for service, task in ((scheduler, scheduler_task), (drainer, drainer_task)):
            if service is not None:
                service.stop()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if ntfy is not None:
            await ntfy.close()
        await db.close()


if __name__ == "__main__":
    sys.exit(main())
