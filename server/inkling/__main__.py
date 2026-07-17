"""Entrypoint: `python -m inkling <role>`.

Roles api/worker/scheduler/delivery/calendar/jobs must each be independently
runnable (§1); `all` runs everything in one asyncio process. api/worker/all are
wired; scheduler/delivery/calendar/jobs land with their milestones.
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

    if args.role in ("api", "worker", "all"):
        import asyncio

        return asyncio.run(_serve(config, args.role))

    print(f"role {args.role!r} is not implemented yet", file=sys.stderr)
    return 1


async def _serve(config: Config, role: str) -> int:
    """Run the API, the workers, or both in one asyncio process (§1).

    - api: HTTP only; triggers are recorded, another process turns them.
    - worker: turns only; no HTTP socket.
    - all: both in one process — the initial systemd shape.
    """
    import asyncio

    from inkling.api.auth import load_roster
    from inkling.api.broker import Broker
    from inkling.db.client import Database
    from inkling.inference.profile import ProfileRoutingGateway
    from inkling.turns.supervisor import Supervisor

    roster = load_roster(config.chat_tokens_file)
    db = Database(config.database_url)
    await db.open()
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
        await db.close()


if __name__ == "__main__":
    sys.exit(main())
