"""SESSION 8: authentication, authorisation, secrets, and log redaction.

Almost every test here is an attempt to get past something rather than a
demonstration that it works. A password hasher that hashes passwords is not
interesting; one that refuses a 10 MB password, that fails closed on a corrupt
stored hash, and that never compares with ``==`` is.

The load-bearing test in this file is
`TestHumanAuthority::test_no_role_lets_a_non_human_approve` — CLAUDE.md
reserves five decisions to human beings, and that test is the one that fails
if somebody later "fixes" authorisation by granting the service account
SUPER_ADMIN.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from academic_ai.domain.enums import UserRole
from academic_ai.infrastructure.db.models import (
    User,
    UserCredential,
    UserSession,
)
from academic_ai.security import (
    ActorKind,
    AuthorizationError,
    ConfigurationError,
    MIN_PASSWORD_LENGTH,
    Permission,
    PasswordPolicyError,
    Principal,
    RESERVED_TO_HUMANS,
    ROLE_PERMISSIONS,
    TokenError,
    TokenType,
    authorise,
    configure_logging,
    decode_token,
    generate_secret,
    hash_password,
    issue_access_token,
    issue_refresh_token,
    load_security_settings,
    needs_rehash,
    permissions_for,
    redact,
    verify_password,
)

SECRET = "a" * 64
GOOD_PASSWORD = "correct horse battery staple"


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    def test_a_hash_verifies_its_own_password(self) -> None:
        assert verify_password(GOOD_PASSWORD, hash_password(GOOD_PASSWORD))

    def test_a_hash_rejects_a_different_password(self) -> None:
        assert not verify_password("something else entirely", hash_password(GOOD_PASSWORD))

    def test_two_hashes_of_one_password_differ(self) -> None:
        """Salted. Equal hashes would mean two users with the same password
        are visibly the same in a stolen dump."""
        assert hash_password(GOOD_PASSWORD) != hash_password(GOOD_PASSWORD)

    def test_the_hash_is_argon2id(self) -> None:
        """The schema CHECK depends on this prefix, so the two must agree."""
        assert hash_password(GOOD_PASSWORD).startswith("$argon2id$")

    def test_the_plaintext_never_appears_in_the_hash(self) -> None:
        assert GOOD_PASSWORD not in hash_password(GOOD_PASSWORD)

    def test_a_short_password_is_refused_at_hashing(self) -> None:
        with pytest.raises(PasswordPolicyError):
            hash_password("x" * (MIN_PASSWORD_LENGTH - 1))

    def test_an_enormous_password_is_refused_rather_than_hashed(self) -> None:
        """Argon2 is memory-hard on purpose, which makes an unbounded input a
        free denial of service against the login endpoint."""
        with pytest.raises(PasswordPolicyError):
            hash_password("x" * 100_000)

    def test_verifying_an_enormous_password_is_false_not_expensive(self) -> None:
        stored = hash_password(GOOD_PASSWORD)
        assert not verify_password("x" * 100_000, stored)

    def test_a_corrupt_stored_hash_fails_the_login_rather_than_raising(self) -> None:
        """A 500 here would tell an attacker the account exists."""
        assert not verify_password(GOOD_PASSWORD, "not-a-hash-at-all")
        assert not verify_password(GOOD_PASSWORD, "")

    def test_verification_does_not_apply_the_length_policy(self) -> None:
        """Rejecting a short candidate early turns the policy into an oracle
        for how long the real password is not."""
        assert not verify_password("short", hash_password(GOOD_PASSWORD))

    def test_a_current_hash_does_not_need_rehashing(self) -> None:
        assert not needs_rehash(hash_password(GOOD_PASSWORD))

    def test_a_hash_from_weaker_parameters_needs_rehashing(self) -> None:
        """Without this, raising the cost parameters protects nobody who
        already has an account -- which is everybody."""
        from argon2 import PasswordHasher

        weak = PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1)
        assert needs_rehash(weak.hash(GOOD_PASSWORD))

    def test_a_garbage_hash_is_treated_as_needing_rehash(self) -> None:
        assert needs_rehash("garbage")


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class TestTokens:
    def test_an_access_token_round_trips(self) -> None:
        token, jti = issue_access_token(subject="u1", secret=SECRET, role="ADMIN")
        claims = decode_token(token, secret=SECRET, expected_type=TokenType.ACCESS)
        assert claims.subject == "u1"
        assert claims.role == "ADMIN"
        assert claims.jti == jti

    def test_a_refresh_token_is_not_accepted_as_an_access_token(self) -> None:
        """Without the type claim, the week-long credential is silently
        accepted everywhere the thirty-minute one is."""
        token, _ = issue_refresh_token(subject="u1", secret=SECRET)
        with pytest.raises(TokenError, match="expected a access token"):
            decode_token(token, secret=SECRET, expected_type=TokenType.ACCESS)

    def test_an_access_token_is_not_accepted_as_a_refresh_token(self) -> None:
        token, _ = issue_access_token(subject="u1", secret=SECRET)
        with pytest.raises(TokenError):
            decode_token(token, secret=SECRET, expected_type=TokenType.REFRESH)

    def test_a_refresh_token_carries_no_role(self) -> None:
        """A role baked into a week-long token keeps issuing yesterday's
        privileges after a demotion."""
        token, _ = issue_refresh_token(subject="u1", secret=SECRET)
        claims = decode_token(token, secret=SECRET, expected_type=TokenType.REFRESH)
        assert claims.role is None

    def test_a_token_signed_with_another_secret_is_rejected(self) -> None:
        token, _ = issue_access_token(subject="u1", secret=SECRET)
        with pytest.raises(TokenError):
            decode_token(token, secret="b" * 64, expected_type=TokenType.ACCESS)

    def test_an_expired_token_is_rejected(self) -> None:
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        token, _ = issue_access_token(
            subject="u1", secret=SECRET, lifetime_minutes=1, now=past
        )
        with pytest.raises(TokenError, match="expired"):
            decode_token(token, secret=SECRET, expected_type=TokenType.ACCESS)

    def test_the_alg_none_forgery_is_rejected(self) -> None:
        """The classic JWT break: re-sign with `alg: none` and no secret."""
        import jwt as pyjwt

        now = dt.datetime.now(dt.timezone.utc)
        forged = pyjwt.encode(
            {
                "sub": "attacker",
                "typ": "access",
                "jti": "x",
                "iat": int(now.timestamp()),
                "exp": int((now + dt.timedelta(hours=1)).timestamp()),
                "iss": "academic-ai",
                "aud": "academic-ai",
                "role": "SUPER_ADMIN",
            },
            key="",
            algorithm="none",
        )
        with pytest.raises(TokenError):
            decode_token(forged, secret=SECRET, expected_type=TokenType.ACCESS)

    def test_a_token_from_another_issuer_is_rejected(self) -> None:
        token, _ = issue_access_token(
            subject="u1", secret=SECRET, issuer="somebody-else"
        )
        with pytest.raises(TokenError):
            decode_token(token, secret=SECRET, expected_type=TokenType.ACCESS)

    def test_a_token_for_another_audience_is_rejected(self) -> None:
        token, _ = issue_access_token(
            subject="u1", secret=SECRET, audience="another-app"
        )
        with pytest.raises(TokenError):
            decode_token(token, secret=SECRET, expected_type=TokenType.ACCESS)

    def test_extra_claims_cannot_overwrite_a_reserved_one(self) -> None:
        """Otherwise a convenience kwarg mints an access token that never
        expires, or one whose type says whatever the caller likes."""
        for reserved in ("typ", "exp", "sub", "jti"):
            with pytest.raises(TokenError, match="reserved"):
                issue_access_token(
                    subject="u1", secret=SECRET, extra={reserved: "anything"}
                )

    def test_an_empty_secret_refuses_to_sign_or_verify(self) -> None:
        with pytest.raises(TokenError, match="empty secret"):
            issue_access_token(subject="u1", secret="")
        token, _ = issue_access_token(subject="u1", secret=SECRET)
        with pytest.raises(TokenError, match="empty secret"):
            decode_token(token, secret="", expected_type=TokenType.ACCESS)

    def test_every_token_gets_a_distinct_jti(self) -> None:
        seen = {issue_access_token(subject="u1", secret=SECRET)[1] for _ in range(20)}
        assert len(seen) == 20


# ---------------------------------------------------------------------------
# RBAC, and the five decisions reserved to humans
# ---------------------------------------------------------------------------


class TestHumanAuthority:
    """CLAUDE.md: AI recommendations never automatically approve academic
    equivalence, cross-program changes, cross-faculty changes, institutional
    policies, or AI model activation."""

    @pytest.mark.parametrize("permission", sorted(RESERVED_TO_HUMANS, key=str))
    @pytest.mark.parametrize("kind", [ActorKind.SERVICE, ActorKind.AI_MODEL])
    def test_no_role_lets_a_non_human_approve(
        self, permission: Permission, kind: ActorKind
    ) -> None:
        """The load-bearing test. Runs at SUPER_ADMIN, the most privileged
        role there is, so it fails if anybody later "fixes" an authorisation
        problem by granting the service account more."""
        principal = Principal("svc", UserRole.SUPER_ADMIN, kind)
        with pytest.raises(AuthorizationError, match="reserved to a human"):
            authorise(principal, permission)

    @pytest.mark.parametrize("permission", sorted(RESERVED_TO_HUMANS, key=str))
    def test_a_human_super_admin_may_approve(self, permission: Permission) -> None:
        authorise(Principal("u1", UserRole.SUPER_ADMIN), permission)

    def test_the_reserved_set_matches_the_five_claude_md_names(self) -> None:
        assert {p.value for p in RESERVED_TO_HUMANS} == {
            "APPROVE_ACADEMIC_EQUIVALENCE",
            "APPROVE_CROSS_PROGRAM_CHANGE",
            "APPROVE_CROSS_FACULTY_CHANGE",
            "APPROVE_INSTITUTIONAL_POLICY",
            "ACTIVATE_AI_MODEL",
        }

    def test_a_non_human_may_still_do_ordinary_work(self) -> None:
        """The rule is about approvals, not about locking service accounts
        out. The optimizer worker has to be able to run an optimization."""
        worker = Principal("worker", UserRole.COORDINATOR, ActorKind.SERVICE)
        authorise(worker, Permission.RUN_OPTIMIZATION)


class TestRolePermissions:
    def test_a_deactivated_user_may_do_nothing(self) -> None:
        principal = Principal("u1", UserRole.SUPER_ADMIN, is_active=False)
        with pytest.raises(AuthorizationError, match="deactivated"):
            authorise(principal, Permission.VIEW_OWN_SCHEDULE)

    def test_a_student_may_only_see_their_own_schedule(self) -> None:
        assert permissions_for(UserRole.STUDENT) == {Permission.VIEW_OWN_SCHEDULE}

    def test_a_student_may_not_run_the_optimizer(self) -> None:
        with pytest.raises(AuthorizationError):
            authorise(Principal("s1", UserRole.STUDENT), Permission.RUN_OPTIMIZATION)

    def test_a_coordinator_may_not_publish(self) -> None:
        """Drafting a timetable and committing the institution to it are
        different decisions."""
        with pytest.raises(AuthorizationError):
            authorise(
                Principal("c1", UserRole.COORDINATOR), Permission.PUBLISH_SCHEDULE
            )

    def test_a_director_may_not_approve_a_cross_faculty_change(self) -> None:
        """A director owns one program. Crossing a faculty boundary is above
        them by definition, which is the entire reason the permission exists
        separately."""
        with pytest.raises(AuthorizationError):
            authorise(
                Principal("d1", UserRole.DIRECTOR),
                Permission.APPROVE_CROSS_FACULTY_CHANGE,
            )

    def test_an_admin_may_not_publish_a_schedule(self) -> None:
        """Administering the platform and owning an academic decision are
        different jobs; an operator restoring a backup must not be able to
        publish a timetable on the way past."""
        with pytest.raises(AuthorizationError):
            authorise(Principal("a1", UserRole.ADMIN), Permission.PUBLISH_SCHEDULE)

    def test_every_role_has_an_entry(self) -> None:
        """A role missing from the table silently grants nothing, which reads
        as a broken account rather than as a configuration bug."""
        assert set(ROLE_PERMISSIONS) == set(UserRole)

    def test_authorise_raises_rather_than_returning_false(self) -> None:
        """A caller who forgets to check a returned bool has written a bypass
        that looks exactly like working code."""
        assert (
            authorise(Principal("u1", UserRole.SUPER_ADMIN), Permission.MANAGE_USERS)
            is None
        )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettingsFailClosed:
    def test_a_missing_secret_refuses_to_start(self) -> None:
        with pytest.raises(ConfigurationError, match="BACKEND_SECRET_KEY"):
            load_security_settings({})

    def test_the_env_example_placeholder_refuses_to_start(self) -> None:
        """That exact string is committed to this repository, so it is public
        to anyone who can read it."""
        with pytest.raises(ConfigurationError, match="placeholder"):
            load_security_settings(
                {
                    "BACKEND_SECRET_KEY": (
                        "replace-with-a-unique-value-generated-by-openssl-rand-hex-32"
                    )
                }
            )

    def test_a_short_secret_refuses_to_start(self) -> None:
        with pytest.raises(ConfigurationError, match="shorter than"):
            load_security_settings({"BACKEND_SECRET_KEY": "abc123"})

    def test_the_committed_env_example_would_not_boot(self) -> None:
        """Read the real file, so this fails if somebody puts a usable secret
        into the template."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2]
        env = {}
        for line in (root / ".env.example").read_text().splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
        env["ENVIRONMENT"] = "production"
        with pytest.raises(ConfigurationError):
            load_security_settings(env)

    def test_a_generated_secret_is_accepted(self) -> None:
        settings = load_security_settings({"BACKEND_SECRET_KEY": generate_secret()})
        assert settings.environment == "development"

    def test_wildcard_cors_is_refused_in_production(self) -> None:
        with pytest.raises(ConfigurationError, match="CORS"):
            load_security_settings(
                {
                    "BACKEND_SECRET_KEY": generate_secret(),
                    "ENVIRONMENT": "production",
                    "BACKEND_ALLOWED_HOSTS": "63.141.255.7",
                    "BACKEND_CORS_ORIGINS": "*",
                    "DATABASE_URL": "postgresql+psycopg://u:p@db:5432/x",
                }
            )

    def test_an_empty_host_allowlist_is_refused_in_production(self) -> None:
        with pytest.raises(ConfigurationError, match="ALLOWED_HOSTS"):
            load_security_settings(
                {
                    "BACKEND_SECRET_KEY": generate_secret(),
                    "ENVIRONMENT": "production",
                    "DATABASE_URL": "postgresql+psycopg://u:p@db:5432/x",
                }
            )

    def test_a_long_lived_access_token_is_refused_in_production(self) -> None:
        with pytest.raises(ConfigurationError, match="ACCESS_TOKEN"):
            load_security_settings(
                {
                    "BACKEND_SECRET_KEY": generate_secret(),
                    "ENVIRONMENT": "production",
                    "BACKEND_ALLOWED_HOSTS": "63.141.255.7",
                    "ACCESS_TOKEN_EXPIRE_MINUTES": "1440",
                    "DATABASE_URL": "postgresql+psycopg://u:p@db:5432/x",
                }
            )

    def test_the_redacted_view_never_contains_the_secret(self) -> None:
        secret = generate_secret()
        settings = load_security_settings({"BACKEND_SECRET_KEY": secret})
        assert secret not in json.dumps(settings.redacted())
        assert settings.redacted()["secret_key_length"] == len(secret)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class TestLogRedaction:
    @staticmethod
    def _emit(message: str, **extra: object) -> dict:
        buffer = io.StringIO()
        configure_logging(stream=buffer)
        logging.getLogger("test").info(message, extra=extra)
        return json.loads(buffer.getvalue().strip())

    def test_a_password_field_is_redacted(self) -> None:
        record = self._emit("login", password="hunter2")
        assert record["password"] == "[redacted]"
        assert "hunter2" not in json.dumps(record)

    def test_a_nested_secret_is_redacted(self) -> None:
        record = self._emit("config", payload={"db": {"POSTGRES_PASSWORD": "s3cret"}})
        assert "s3cret" not in json.dumps(record)

    def test_a_jwt_inside_a_message_string_is_redacted(self) -> None:
        """The `f"failed for {authorization}"` case, which no key-based
        redaction would catch."""
        token, _ = issue_access_token(subject="u1", secret=SECRET)
        record = self._emit(f"rejected token {token}")
        assert token not in json.dumps(record)
        assert "[redacted]" in record["message"]

    def test_a_bearer_header_is_redacted(self) -> None:
        record = self._emit("inbound", headers={"Authorization": "Bearer abc.def.ghi"})
        assert "abc.def.ghi" not in json.dumps(record)

    def test_output_is_one_json_object_per_line(self) -> None:
        buffer = io.StringIO()
        configure_logging(stream=buffer)
        logging.getLogger("test").info("first")
        logging.getLogger("test").warning("second")
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        assert len(lines) == 2
        assert [json.loads(line)["level"] for line in lines] == ["INFO", "WARNING"]

    def test_an_exception_is_captured_and_scrubbed(self) -> None:
        buffer = io.StringIO()
        configure_logging(stream=buffer)
        try:
            raise ValueError("token eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM failed")
        except ValueError:
            logging.getLogger("test").exception("boom")
        record = json.loads(buffer.getvalue().strip())
        assert "SflKxwRJSM" not in json.dumps(record)

    def test_redaction_survives_a_cycle(self) -> None:
        """A cyclic object must not turn a log call into a stack overflow
        inside the logging path, where the error has nowhere to go."""
        cyclic: dict = {}
        cyclic["self"] = cyclic
        assert redact(cyclic) is not None

    def test_a_derived_length_is_not_hidden_by_the_redactor(self) -> None:
        """`SecuritySettings.redacted()` reports the signing key's *length* on
        purpose -- enough to diagnose "the key got truncated", useless to an
        attacker. The redactor was hiding it, because the key name contains
        "secret". Two safety mechanisms silently cancelling each other out is
        worse than either alone: the log still looks like it is working."""
        settings = load_security_settings({"BACKEND_SECRET_KEY": generate_secret()})
        record = self._emit("api starting", settings=settings.redacted())
        assert record["settings"]["secret_key_length"] == 64
        assert record["settings"]["access_token_minutes"] == 30

    def test_the_allowlist_is_exact_not_a_substring_rule(self) -> None:
        """A substring rule there would be a hole:
        `secret_key_length_and_also_the_key` would match it."""
        record = self._emit("x", secret_key_length_and_also_the_key="sensitive")
        assert record["secret_key_length_and_also_the_key"] == "[redacted]"

    def test_no_module_passes_a_reserved_logrecord_key_through_extra(self) -> None:
        """This class of bug has bitten twice -- `message` in SESSION 9 and
        `created` in SESSION 15 -- and both times it failed the *operation*,
        not the log line: `Logger.makeRecord` raises KeyError before the
        formatter is ever reached, so an import died for a logging reason.

        A scan is cheap and catches it permanently, which a third code review
        would not.
        """
        import ast
        import logging
        import pathlib as _pathlib

        reserved = set(
            logging.LogRecord("", 0, "", 0, "", (), None).__dict__
        ) | {"message", "asctime"}

        root = _pathlib.Path(__file__).resolve().parents[1] / "academic_ai"
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg != "extra" or not isinstance(
                        keyword.value, ast.Dict
                    ):
                        continue
                    for key in keyword.value.keys:
                        if (
                            isinstance(key, ast.Constant)
                            and key.value in reserved
                        ):
                            offenders.append(
                                f"{path.relative_to(root.parent)}:{key.lineno} "
                                f"passes reserved key {key.value!r}"
                            )

        assert not offenders, "\n".join(offenders)

    def test_configure_logging_replaces_rather_than_adds_handlers(self) -> None:
        """uvicorn and celery install their own; leaving them emits every
        line twice, with the unstructured copy unredacted."""
        configure_logging(stream=io.StringIO())
        configure_logging(stream=io.StringIO())
        assert len(logging.getLogger().handlers) == 1


# ---------------------------------------------------------------------------
# The schema states the same rules again
# ---------------------------------------------------------------------------


class TestCredentialSchema:
    @staticmethod
    def _user(session: Session, email: str = "a@example.test") -> User:
        user = User(email=email, full_name="A", primary_role=UserRole.ADMIN)
        session.add(user)
        session.flush()
        return user

    def test_a_plaintext_password_cannot_be_stored(self, session: Session) -> None:
        """The single worst thing that could happen in this schema, and the
        one code review reliably fails to catch."""
        user = self._user(session)
        session.add(
            UserCredential(app_user_id=user.id, password_hash="hunter2")
        )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_an_argon2_hash_is_accepted(self, session: Session) -> None:
        user = self._user(session)
        session.add(
            UserCredential(
                app_user_id=user.id, password_hash=hash_password(GOOD_PASSWORD)
            )
        )
        session.flush()

    def test_a_user_has_at_most_one_credential(self, session: Session) -> None:
        user = self._user(session)
        for _ in range(2):
            session.add(
                UserCredential(
                    app_user_id=user.id, password_hash=hash_password(GOOD_PASSWORD)
                )
            )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_a_session_stores_the_jti_not_the_token(self, session: Session) -> None:
        user = self._user(session)
        token, jti = issue_refresh_token(subject=str(user.id), secret=SECRET)
        row = UserSession(
            app_user_id=user.id,
            refresh_jti=jti,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7),
        )
        session.add(row)
        session.flush()
        assert row.refresh_jti == jti
        assert token not in json.dumps(
            {c.name: str(getattr(row, c.name)) for c in row.__table__.columns}
        )

    def test_two_sessions_cannot_share_a_jti(self, session: Session) -> None:
        user = self._user(session)
        expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=7)
        for _ in range(2):
            session.add(
                UserSession(
                    app_user_id=user.id, refresh_jti="same-jti", expires_at=expires
                )
            )
        with pytest.raises(IntegrityError):
            session.flush()

    def test_a_session_cannot_expire_before_it_starts(self, session: Session) -> None:
        user = self._user(session)
        session.add(
            UserSession(
                app_user_id=user.id,
                refresh_jti="j1",
                expires_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc),
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()
