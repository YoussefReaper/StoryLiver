"""Workstream D - a real account, replacing a spoofable string.

What was here before: `ensure_user()` inserted whatever `user_id` the client
sent. Anyone could play as anyone by typing their id, which meant Mana, run
history, purchases and - once Workstream C lands - a town's memory of your
crimes were all attached to a claim rather than to a person. C4 is impossible
on top of that, because a reputation you can escape by editing localStorage is
not a reputation.

The choices here follow current OWASP guidance rather than habit:

  ARGON2ID for password hashing. It is OWASP's first recommendation because it
  is memory-hard, which is what actually resists GPU and ASIC cracking; bcrypt
  is a fine fallback but is neither memory-hard nor able to take a password
  over 72 bytes. Parameters are OWASP's interactive-login baseline.

  SERVER-SIDE SESSIONS, not stateless JWTs. A JWT cannot be revoked before it
  expires, and "log out everywhere" is a thing people legitimately need after
  losing a device. A session row can be deleted. Only a HASH of the token is
  stored, so a database leak does not hand over live sessions.

  HTTPONLY + SECURE + SAMESITE cookies, never localStorage. Anything in
  localStorage is readable by any script on the page, so one XSS is total
  account compromise. HttpOnly takes the token out of JavaScript's reach
  entirely.

  GUEST MODE is preserved deliberately. The blueprint's no-account, room-code
  entry is a real advantage over competitors and forcing a login at the door
  would throw it away. A guest can play everything; what a guest cannot do is
  BUY Mana or accumulate anything that has to survive a device change, because
  those are exactly the things that need a verifiable owner.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from . import db

SESSION_DAYS = 30
COOKIE = "storyliver_session"

# OWASP's interactive-login baseline: t=2, m=19456 KiB, p=1 (~100ms/core).
ARGON_TIME, ARGON_MEMORY, ARGON_PARALLELISM = 2, 19456, 1

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
MIN_PASSWORD = 10          # length beats composition rules; NIST agrees


class AuthError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Hashing
# --------------------------------------------------------------------------

_hasher = None


def _argon():
    global _hasher
    if _hasher is None:
        from argon2 import PasswordHasher
        _hasher = PasswordHasher(time_cost=ARGON_TIME, memory_cost=ARGON_MEMORY,
                                 parallelism=ARGON_PARALLELISM)
    return _hasher


def hash_password(password: str) -> str:
    return _argon().hash(password)


def verify_password(stored: str, password: str) -> bool:
    from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
    try:
        return bool(_argon().verify(stored, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    """Parameters get stronger over time; a login is the one moment we hold the
    plaintext and can transparently upgrade an old hash."""
    try:
        return _argon().check_needs_rehash(stored)
    except Exception:
        return False


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc)


def register(email: str, password: str, display_name: str = "") -> dict:
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise AuthError("that does not look like an email address")
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"a password needs at least {MIN_PASSWORD} characters")
    if db.row("SELECT id FROM accounts WHERE email=?", (email,)):
        raise AuthError("an account with that email already exists")

    account_id = "u" + uuid.uuid4().hex[:14]
    db.run(
        "INSERT INTO accounts (id,email,display_name,password_hash,provider,created_at,last_seen)"
        " VALUES (?,?,?,?,?,?,?)",
        (account_id, email, (display_name or email.split("@")[0])[:40],
         hash_password(password), "password", db.now(), db.now()))
    # Keep the legacy users row in step so every existing foreign key still
    # resolves - accounts are additive, not a migration of the whole engine.
    db.run("INSERT OR IGNORE INTO users (id,created_at) VALUES (?,?)",
           (account_id, db.now()))
    return public(account_id)


def login(email: str, password: str, *, user_agent: str = "") -> dict:
    email = (email or "").strip().lower()
    row = db.row("SELECT * FROM accounts WHERE email=?", (email,))

    # Verify against a dummy hash when the account does not exist, so the
    # response time does not reveal which emails are registered.
    if not row:
        verify_password(_ensure_dummy(), password or "x")
        raise AuthError("wrong email or password")
    if not verify_password(row["password_hash"], password or ""):
        raise AuthError("wrong email or password")

    if needs_rehash(row["password_hash"]):
        db.run("UPDATE accounts SET password_hash=? WHERE id=?",
               (hash_password(password), row["id"]))

    db.run("UPDATE accounts SET last_seen=? WHERE id=?", (db.now(), row["id"]))
    token = start_session(row["id"], user_agent=user_agent)
    return {"token": token, "account": public(row["id"])}


# A real Argon2id hash of a random string, used only to keep the failure path
# as slow as the success path.
_DUMMY_HASH = None


def _ensure_dummy():
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(24))
    return _DUMMY_HASH


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(account_id: str, *, user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=SESSION_DAYS)
    db.run(
        "INSERT INTO auth_sessions (token_hash,account_id,created_at,expires_at,last_used,user_agent)"
        " VALUES (?,?,?,?,?,?)",
        (_token_hash(token), account_id, db.now(), expires.isoformat(timespec="seconds"),
         db.now(), (user_agent or "")[:200]))
    return token


def session_account(token: str) -> str | None:
    """The account behind a session token, or None. Expired rows are deleted
    on sight rather than merely ignored."""
    if not token:
        return None
    row = db.row("SELECT * FROM auth_sessions WHERE token_hash=?", (_token_hash(token),))
    if not row:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        expires = _now()
    if expires <= _now():
        db.run("DELETE FROM auth_sessions WHERE token_hash=?", (_token_hash(token),))
        return None
    db.run("UPDATE auth_sessions SET last_used=? WHERE token_hash=?",
           (db.now(), _token_hash(token)))
    return row["account_id"]


def logout(token: str) -> bool:
    if not token:
        return False
    db.run("DELETE FROM auth_sessions WHERE token_hash=?", (_token_hash(token),))
    return True


def logout_everywhere(account_id: str) -> int:
    """The reason sessions are rows and not JWTs."""
    with db.conn() as c:
        return c.execute("DELETE FROM auth_sessions WHERE account_id=?", (account_id,)).rowcount


def sessions_for(account_id: str) -> list:
    return db.rows("SELECT created_at,last_used,expires_at,user_agent FROM auth_sessions"
                   " WHERE account_id=? ORDER BY last_used DESC", (account_id,))


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------

def public(account_id: str) -> dict | None:
    """Everything safe to show. There is no branch of this function that can
    return the password hash."""
    row = db.row("SELECT id,email,display_name,avatar_url,bio,provider,created_at,last_seen"
                 " FROM accounts WHERE id=?", (account_id,))
    return dict(row) if row else None


def update_profile(account_id: str, *, display_name=None, bio=None, avatar_url=None) -> dict:
    row = db.row("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not row:
        raise AuthError("no such account")
    db.run("UPDATE accounts SET display_name=?, bio=?, avatar_url=? WHERE id=?",
           ((display_name if display_name is not None else row["display_name"])[:40],
            (bio if bio is not None else row["bio"])[:400],
            (avatar_url if avatar_url is not None else row["avatar_url"])[:300],
            account_id))
    return public(account_id)


def change_password(account_id: str, current: str, new: str) -> dict:
    row = db.row("SELECT * FROM accounts WHERE id=?", (account_id,))
    if not row:
        raise AuthError("no such account")
    if not verify_password(row["password_hash"], current or ""):
        raise AuthError("your current password is wrong")
    if len(new or "") < MIN_PASSWORD:
        raise AuthError(f"a password needs at least {MIN_PASSWORD} characters")
    db.run("UPDATE accounts SET password_hash=? WHERE id=?", (hash_password(new), account_id))
    # Changing a password ends every other session - that is the whole point
    # of doing it after a device is lost.
    logout_everywhere(account_id)
    return {"changed": True, "sessions_ended": True}


def is_guest(user_id: str) -> bool:
    """A guest is any identity with no account row behind it. They can play
    everything; they cannot buy Mana or carry anything between devices."""
    if not user_id:
        return True
    return not db.row("SELECT 1 FROM accounts WHERE id=?", (user_id,))


def cookie_kwargs(request_secure: bool = False) -> dict:
    """HttpOnly always. Secure whenever the request arrived over HTTPS - forcing
    it on plain HTTP would silently break local development, and a cookie the
    browser refuses to send is worse than one it sends over the same channel
    the rest of the page already used."""
    return {
        "httponly": True,
        "secure": bool(request_secure) or os.getenv("STORYLIVER_FORCE_SECURE_COOKIE") == "1",
        "samesite": "lax",
        "max_age": SESSION_DAYS * 24 * 3600,
        "path": "/",
    }


# --------------------------------------------------------------------------
# Claiming guest work
# --------------------------------------------------------------------------

# Tables where a row belongs to whoever the user_id column names. When a guest
# signs in, every one of these has to follow them or the account is a downgrade:
# they would sign up to "keep" their Mana and immediately lose the story it was
# in. That is the single worst moment a signup flow can produce.
OWNED_BY_USER = (
    ("playthroughs", "user_id"),
    ("worlds", "owner_user_id"),
    ("streaks", "user_id"),
    ("runs", "user_id"),
    ("meta_progress", "user_id"),
    ("daily_ledger", "user_id"),
    ("usage_log", "user_id"),
    ("sessions", "host_user_id"),
    ("session_players", "user_id"),
    ("payments", "user_id"),
)


def claim(guest_id: str, account_id: str) -> dict:
    """Move everything a guest made onto their new account.

    Guarded on both sides: the source must genuinely be a guest (no account
    row) and the target must genuinely be an account. Without the first check
    this would be an account-takeover primitive - anyone could 'claim' another
    real user's id."""
    if not guest_id or not account_id or guest_id == account_id:
        return {"claimed": False, "reason": "nothing to claim"}
    if not is_guest(guest_id):
        raise AuthError("that identity already belongs to an account")
    if is_guest(account_id):
        raise AuthError("cannot claim onto something that is not an account")

    moved = {}
    for table, column in OWNED_BY_USER:
        try:
            with db.conn() as c:
                n = c.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?",
                              (account_id, guest_id)).rowcount
            if n:
                moved[table] = n
        except Exception:
            # A table that does not exist in an older database must not stop
            # the rest of the move; a partial claim beats a failed one.
            continue

    # The guest's placeholder users row has done its job.
    db.run("INSERT OR IGNORE INTO users (id,created_at) VALUES (?,?)",
           (account_id, db.now()))
    return {"claimed": bool(moved), "moved": moved,
            "note": "Your stories, Mana and streak now belong to your account."}


def claimable(guest_id: str) -> dict:
    """What a guest would bring with them, so the offer can be specific
    ('3 stories and 120 Mana') rather than a vague 'keep your progress'."""
    if not guest_id or not is_guest(guest_id):
        return {"any": False}
    row = db.row("SELECT COUNT(*) AS n, COALESCE(SUM(mana_balance),0) AS mana"
                 " FROM playthroughs WHERE user_id=?", (guest_id,))
    worlds = db.row("SELECT COUNT(*) AS n FROM worlds WHERE owner_user_id=?", (guest_id,))
    return {"any": bool(row and row["n"]),
            "stories": row["n"] if row else 0,
            "mana": row["mana"] if row else 0,
            "worlds": worlds["n"] if worlds else 0}
