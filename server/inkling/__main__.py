"""Entrypoint: `python -m inkling <role>`.

Roles api/worker/scheduler/delivery/calendar/jobs must each be independently
runnable (§1); `all` runs everything in one asyncio process. All roles are
wired; calendar and the backup/drill jobs no-op cleanly until their §19
credentials (Google OAuth, S3 bucket) are configured.
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
    from inkling.lib.logsetup import setup_logging

    setup_logging(config.log_level, config.log_format)

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

    if args.role in (
        "api", "worker", "scheduler", "delivery", "calendar", "jobs", "all"
    ):
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
    - calendar: sync engine only (§12); requires Google OAuth config, else
      exits with an explanation. In `all` it simply isn't started when
      unconfigured — the feature is absent, not broken.
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
    calendar_sync = None
    calendar_task = None
    google = None
    jobs_runner = None
    jobs_task = None
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
        if role in ("calendar", "all") and config.google_oauth_client_id:
            from inkling.calendar_.google import GoogleCalendar
            from inkling.calendar_.sync import CalendarSync
            from inkling.db.queries import calendar as calendar_q

            # Per-user token files live in calendar_sync_state (§16); a user
            # with no rows simply contributes nothing to the map.
            token_file_for: dict[str, str] = {}
            for user_id in roster.user_ids:
                async with db.with_tenant(user_id) as conn:
                    for state in await calendar_q.states(conn):
                        token_file_for[user_id] = state["oauth_token_file"]
            google = GoogleCalendar(
                client_id=config.google_oauth_client_id,
                client_secret=config.google_oauth_client_secret,
                token_file_for=token_file_for,
            )
            calendar_sync = CalendarSync(
                db,
                roster.user_ids,
                google,
                webhook_url=config.calendar_webhook_url,
                nudge=supervisor.nudge if supervisor else None,
            )
            calendar_task = asyncio.create_task(
                calendar_sync.run_forever(), name="calendar"
            )
        elif role == "calendar":
            print(
                "calendar role needs GOOGLE_OAUTH_CLIENT_ID/SECRET configured",
                file=sys.stderr,
            )
            return 1

        if role in ("delivery", "all"):
            ntfy = NtfyClient(config.ntfy_base)
            extra = {}
            if google is not None:
                from inkling.calendar_.channel import CalendarChannel

                extra["calendar"] = CalendarChannel(db, google)
            drainer = Drainer(
                db,
                roster.user_ids,
                channel=ntfy,
                operator_topic=config.operator_ntfy,
                extra_channels=extra,
            )
            drainer_task = asyncio.create_task(
                drainer.run_forever(), name="delivery"
            )
        if role in ("jobs", "all"):
            from pathlib import Path

            from inkling.jobs.backup import run_backup
            from inkling.jobs.restore_drill import run_drill
            from inkling.jobs.runner import JobsRunner
            from inkling.lib.s3 import stub_or_real

            s3 = stub_or_real(config)
            if ntfy is None:
                ntfy = NtfyClient(config.ntfy_base)

            async def _alert(message: str) -> None:
                if config.operator_ntfy:
                    await ntfy.send(topic=config.operator_ntfy, body=message)

            async def _backup(workdir: Path) -> None:
                await run_backup(
                    db,
                    database_url=config.database_url_migrate or config.database_url,
                    gpg_recipient=config.backup_gpg_recipient,
                    s3=s3,
                    user_ids=roster.user_ids,
                    export_dir=config.export_git_dir,
                    workdir=workdir,
                    alert=_alert,
                )

            async def _drill(workdir: Path) -> None:
                await run_drill(
                    db,
                    s3=s3,
                    live_url=config.database_url_migrate or config.database_url,
                    scratch_url=config.restore_drill_url,
                    workdir=workdir,
                    alert=_alert,
                )

            jobs_runner = JobsRunner(
                backup=_backup,
                drill=_drill if config.restore_drill_url else None,
            )
            jobs_task = asyncio.create_task(
                jobs_runner.run_forever(), name="jobs"
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

        if role == "calendar":
            try:
                await calendar_task
            finally:
                calendar_sync.stop()
            return 0

        if role == "jobs":
            try:
                await jobs_task
            finally:
                jobs_runner.stop()
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

        app = create_app(
            db, roster, broker=broker, supervisor=supervisor,
            calendar_sync=calendar_sync,
        )
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
        for service, task in (
            (scheduler, scheduler_task),
            (drainer, drainer_task),
            (calendar_sync, calendar_task),
            (jobs_runner, jobs_task),
        ):
            if service is not None:
                service.stop()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if ntfy is not None:
            await ntfy.close()
        if google is not None:
            await google.close()
        await db.close()


if __name__ == "__main__":
    sys.exit(main())
