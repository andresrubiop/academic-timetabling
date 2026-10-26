"""Password hashing.

**Argon2id**, at OWASP's current parameters. Not because bcrypt is broken —
it is not — but because bcrypt silently truncates at 72 bytes and is cheap to
attack on the GPUs this institution's own students have. Argon2id is
memory-hard, which is the property that makes an offline crack of a stolen
`app_user` table expensive rather than merely slow.

Three things this module refuses to do
======================================

**It never logs a password, and never returns one.** The only value that
leaves here is a hash.

**It never compares hashes with ``==``.** `argon2` does the constant-time
comparison internally; a manual equality check on a secret leaks its prefix
through timing.

**It does not accept an unbounded password.** Argon2 has no 72-byte
truncation bug, but hashing a 10 MB "password" is a free denial of service
against a memory-hard function — the whole point of which is that each hash
is expensive. The cap is on *bytes*, not characters, because a 200-character
Spanish password with accents is longer than it looks.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

__all__ = [
    "MAX_PASSWORD_BYTES",
    "MIN_PASSWORD_LENGTH",
    "PasswordPolicyError",
    "hash_password",
    "needs_rehash",
    "verify_password",
]

#: OWASP's 2024 argon2id baseline: 19 MiB, 2 iterations, 1 degree of
#: parallelism. Tuned for *defence*, not for benchmark scores — raising
#: `time_cost` on this 56-core host would be cheap for us and cheap for an
#: attacker with a bigger one; raising `memory_cost` is what actually hurts
#: parallel cracking hardware.
_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)

#: A floor, not a policy. The institution's real policy belongs in
#: configuration a human can change; this is the number below which the
#: platform will not store a credential at all.
MIN_PASSWORD_LENGTH = 12

#: 1 KiB of UTF-8. Long enough for any passphrase a human types, short enough
#: that an attacker cannot turn the login endpoint into a memory bomb.
MAX_PASSWORD_BYTES = 1024


class PasswordPolicyError(ValueError):
    """The password was rejected before hashing. Never contains the password."""


def _check(password: str) -> bytes:
    encoded = password.encode("utf-8")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes when encoded "
            "as UTF-8"
        )
    return encoded


def hash_password(password: str) -> str:
    """Hash a new or changed password. Raises rather than storing a weak one."""
    _check(password)
    return _HASHER.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    """Whether this password produced that hash.

    Returns ``False`` for a wrong password *and* for a malformed stored hash,
    rather than raising. A corrupt hash in the database must fail the login,
    not return a 500 that tells an attacker the row exists.

    Deliberately does **not** apply `MIN_PASSWORD_LENGTH`: the policy applies
    to setting a password, not to checking one. Rejecting a short candidate
    early would turn the policy into an oracle for how long the real password
    is not.
    """
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return False
    try:
        return _HASHER.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """Whether this hash predates the current parameters.

    Called on a *successful* login, which is the only moment the plaintext is
    available to re-hash with. Without this, raising the cost parameters
    protects nobody who already has an account — which is everybody.
    """
    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True
