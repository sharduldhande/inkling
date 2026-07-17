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
        return _serve(config, args.role)

    print(f"role {args.role!r} is not implemented yet", file=sys.stderr)
    return 1


def _serve(config: Config, role: str) -> int:
    """Run the API, the workers, or both in one asyncio process.

    The real inference gateway lands with §7; until then every role that turns
    triggers refuses to start rather than quietly answering with a stub.
    """
    print(
        f"role {role!r} needs the §7 inference gateway, which is not built yet.\n"
        "The turn pipeline and API are testable via the test suite "
        "(StubGateway); wiring a real adapter is the next milestone.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
