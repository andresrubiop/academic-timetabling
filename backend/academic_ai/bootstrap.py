"""Create the first administrator. ``python -m academic_ai.bootstrap``.

Every endpoint needs a token, a token needs a login, a login needs a user with
a credential — and until this module existed, nothing could create one. The
platform was deployed, healthy, and impossible to sign into. SESSION 21
verified that the stack starts and answers; it did not verify that anybody
could get in.

**Why an unauthenticated command is the right answer here.** This grants
administrator access with no token, which would be indefensible over HTTP. It
is not over HTTP: it runs inside the container, so using it already requires
`docker compose exec`, and anybody who can exec into the container can read
the database credentials from its environment anyway. The security boundary is
shell access to the host, which this does not widen.

The password is read from the environment or prompted for — **never taken as a
command-line argument**, because arguments are visible in `ps` to every user
on a shared host, and this one is a shared host.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import uuid

from sqlalchemy import func, select


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m academic_ai.bootstrap",
        description="Create the first administrator account.",
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="Administrador")
    parser.add_argument(
        "--role",
        default="SUPER_ADMIN",
        choices=["SUPER_ADMIN", "ADMIN"],
        help="SUPER_ADMIN by default: the first account has to be able to "
        "create the rest.",
    )
    parser.add_argument(
        "--allow-additional",
        action="store_true",
        help="Create the account even though an administrator already exists. "
        "Without this the command refuses, so a second run cannot quietly "
        "mint a second set of keys.",
    )
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def _read_password() -> str:
    """From `BOOTSTRAP_PASSWORD`, or prompted. Never from argv."""
    from academic_ai.security.passwords import MIN_PASSWORD_LENGTH

    password = os.environ.get("BOOTSTRAP_PASSWORD", "")
    if not password:
        if not sys.stdin.isatty():
            raise SystemExit(
                "bootstrap: set BOOTSTRAP_PASSWORD, or run interactively so it "
                "can be prompted for. It is deliberately not a command-line "
                "argument: arguments are visible in `ps` to every user on this "
                "shared host."
            )
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat: "):
            raise SystemExit("bootstrap: the two passwords differ")

    if len(password) < MIN_PASSWORD_LENGTH:
        raise SystemExit(
            f"bootstrap: the password must be at least {MIN_PASSWORD_LENGTH} "
            "characters"
        )
    return password


def create_administrator(
    session,
    *,
    email: str,
    full_name: str,
    password: str,
    role: str = "SUPER_ADMIN",
    allow_additional: bool = False,
) -> uuid.UUID:
    """Create one administrator with a usable password. Returns its id."""
    from academic_ai.domain.enums import UserRole
    from academic_ai.infrastructure.db.models import User, UserCredential
    from academic_ai.security.passwords import hash_password

    email = email.strip().lower()
    if not email or "@" not in email:
        raise SystemExit(f"bootstrap: {email!r} is not an email address")

    existing = session.scalars(select(User).where(User.email == email)).first()
    if existing is not None:
        # Not "update the password": a command that silently resets an
        # administrator's credentials is a password-reset tool wearing a
        # bootstrap's name, and it would be reachable by anybody who can run
        # this one.
        raise SystemExit(
            f"bootstrap: {email} already exists. This command creates the "
            "first account; it does not reset passwords."
        )

    admins = session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.primary_role.in_((UserRole.SUPER_ADMIN, UserRole.ADMIN)))
    )
    if admins and not allow_additional:
        raise SystemExit(
            f"bootstrap: {admins} administrator account(s) already exist. "
            "Pass --allow-additional if you really mean to create another; "
            "the usual reason to see this message is that the platform is "
            "already set up."
        )

    user = User(
        email=email,
        full_name=full_name.strip() or email,
        primary_role=UserRole[role],
        is_active=True,
    )
    session.add(user)
    session.flush()
    session.add(
        UserCredential(app_user_id=user.id, password_hash=hash_password(password))
    )
    session.flush()
    return user.id


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    password = _read_password()

    from academic_ai.infrastructure.db.session import session_factory

    make_session = session_factory()
    with make_session() as session:
        user_id = create_administrator(
            session,
            email=args.email,
            full_name=args.name,
            password=password,
            role=args.role,
            allow_additional=args.allow_additional,
        )
        session.commit()

    # The id, never the password.
    print(f"created {args.role} {args.email} ({user_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
