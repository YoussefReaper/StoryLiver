"""Durable storage - so a free-tier host wiping local disk costs nothing.

The actual problem this solves: Render's free web service has no persistent
disk. Every spin-down (15 minutes idle) or redeploy hands the app a brand new,
empty local filesystem. Without this module, that means every story, account,
and Mana balance is gone the moment nobody's been playing for a quarter hour.

WHAT THIS IS NOT: a second database, or a rewrite of the persistence layer.
SQLite via db.py stays the one and only source of truth for every read and
write while the app is running - every one of the 40+ modules that call
`db.run`/`db.row`/`db.rows` is untouched, and so are the 374 tests that assume
SQLite's semantics. This module only does two things around the edges:

  BACKUP   periodically, and once more on graceful shutdown, take a CONSISTENT
           snapshot of the live database and store it in MongoDB (GridFS, so a
           snapshot larger than BSON's 16MB document cap still works).
  RESTORE  on boot, if there is no local database file yet - the signature of
           a fresh container - pull the most recent snapshot down before
           anything else touches the file.

"Consistent" is not decoration. The database runs in WAL mode: a raw copy of
the .db file can miss commits still sitting in the -wal sidecar file, which
would silently hand back a stale snapshot. SQLite's own **online backup API**
(`Connection.backup()`) exists specifically to avoid that - it is what
`sqlite3`'s own docs recommend for backing up a database that is in use, and
what this uses.

OFF BY DEFAULT. `enabled()` requires MONGODB_URI to be set AND the app not to
be running in the test suite's mock mode - the same two-part gate research.py
uses, for the same reason: an unconfigured deployment or a test run must never
depend on a Mongo cluster existing. Every function here degrades to a no-op
when disabled, and nothing here is on the hot path of a single request -
backups happen on a timer, in a background task, off the event loop.
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

FILENAME = "storyliver.db"
KEEP_SNAPSHOTS = 3          # a little history, still trivial on a 512MB free tier

_client = None
_fs = None
_task = None


class DurableError(RuntimeError):
    pass


def enabled() -> bool:
    """Off unless explicitly configured, and off in the test suite's mock mode
    even if a stray MONGODB_URI is sitting in the environment - the same
    protective gate research.py uses, for the same reason."""
    if not config.MONGODB_URI:
        return False
    return config.LLM_MODE != "mock"


def _get_fs():
    """The GridFS handle, built once and reused. A plain module-level lazy
    singleton rather than a class: everything here is process-wide state
    already (one SQLite file, one Mongo cluster), so a class would only add
    ceremony without adding a second instance anyone would ever want."""
    global _client, _fs
    if _fs is None:
        import gridfs
        from pymongo import MongoClient
        # A misconfigured URI must fail fast: this stacks on top of a
        # cold host's own boot time (Render's free tier alone takes
        # 30-50s to wake), and restore_if_needed() runs synchronously in
        # the startup path - a slow timeout here is a slow app boot.
        _client = MongoClient(config.MONGODB_URI, serverSelectionTimeoutMS=4000,
                              connectTimeoutMS=4000)
        _fs = gridfs.GridFS(_client[config.MONGODB_DB])
    return _fs


# ---------------------------------------------------------------------------
# The snapshot itself - pure SQLite, no Mongo. Kept separate so it is testable
# (and trustable) on its own, independent of anything network-shaped.
# ---------------------------------------------------------------------------

def snapshot_bytes() -> bytes:
    """A consistent, point-in-time copy of the live database, as raw bytes.

    Uses sqlite3's backup API rather than reading the file directly: under WAL
    mode a raw copy can miss committed pages still sitting in the -wal file,
    which is a real correctness bug, not a theoretical one. The backup API
    is what the sqlite3 docs recommend for exactly this - copying a database
    that other connections may be actively writing to.

    The destination is a fresh temp file, not `:memory:`: backing up into an
    in-memory database would not give back a byte string we can later write
    straight to disk and reopen as-is."""
    if not Path(config.DB_PATH).exists():
        raise DurableError("no local database to snapshot yet")

    with tempfile.TemporaryDirectory() as tmp:
        dest_path = Path(tmp) / "snapshot.db"
        src = sqlite3.connect(str(config.DB_PATH))
        dest = sqlite3.connect(str(dest_path))
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        # The destination was never put into WAL mode, so by the time backup()
        # returns and both handles are closed, it is one fully checkpointed,
        # self-contained file - safe to store and later restore verbatim.
        return dest_path.read_bytes()


# ---------------------------------------------------------------------------
# Backup - push the current state up
# ---------------------------------------------------------------------------

def backup() -> dict:
    """Take a snapshot and store it. Uploads the new copy BEFORE deleting any
    old one, so a failure partway through a backup can never leave zero
    restorable copies behind."""
    if not enabled():
        return {"backed_up": False, "reason": "durable storage disabled"}
    try:
        data = snapshot_bytes()
    except DurableError as e:
        return {"backed_up": False, "reason": str(e)}

    try:
        fs = _get_fs()
        new_id = fs.put(data, filename=FILENAME,
                        uploaded_at=datetime.now(timezone.utc).isoformat(),
                        bytes=len(data))
        _rotate(fs, keep=KEEP_SNAPSHOTS)
    except Exception as e:                     # a Mongo hiccup must never crash the app
        return {"backed_up": False, "reason": f"{type(e).__name__}: {e}"}

    return {"backed_up": True, "id": str(new_id), "bytes": len(data)}


def _rotate(fs, *, keep: int):
    """Keep only the most recent `keep` snapshots. Old ones are deleted only
    after the new one is safely stored, and one at a time, so a delete
    failure midway still leaves the newest copies intact."""
    files = list(fs.find({"filename": FILENAME}).sort("uploadDate", -1))
    for old in files[keep:]:
        fs.delete(old._id)


# ---------------------------------------------------------------------------
# Restore - pull state down, but ONLY into a genuinely fresh container
# ---------------------------------------------------------------------------

def restore_if_needed() -> dict:
    """Called once, at boot, before db.init() ever touches the database path.

    Ordering matters: sqlite3.connect() creates an empty file the instant it
    is called, so this MUST run before anything opens DB_PATH - otherwise the
    "does a local file already exist" check below would always be true, and a
    fresh container would never restore anything.

    Deliberately conservative: if a local file already exists, this does
    NOTHING, even if Mongo holds a newer backup. On a normal running instance
    the local file over the current process's lifetime IS the source of
    truth; the only case that needs restoring from is the one where local
    storage was wiped out from under the app, which shows up as the file
    being simply absent."""
    if Path(config.DB_PATH).exists():
        return {"restored": False, "reason": "local database already present"}
    if not enabled():
        return {"restored": False, "reason": "durable storage disabled"}

    try:
        fs = _get_fs()
        latest = fs.find_one({"filename": FILENAME}, sort=[("uploadDate", -1)])
    except Exception as e:
        # A Mongo outage on boot must still let the app come up - with a
        # fresh empty database, exactly like today, rather than refusing to
        # start. Durable storage is a safety net, not a hard dependency.
        return {"restored": False, "reason": f"{type(e).__name__}: {e}"}

    if not latest:
        return {"restored": False, "reason": "no backup exists yet"}

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = latest.read()
    Path(config.DB_PATH).write_bytes(data)
    return {"restored": True, "bytes": len(data),
            "uploaded_at": getattr(latest, "uploaded_at", "")}


# ---------------------------------------------------------------------------
# The background loop
# ---------------------------------------------------------------------------

async def _loop(interval: float):
    import asyncio
    while True:
        await asyncio.sleep(interval)
        try:
            # Blocking sqlite3/pymongo calls belong off the event loop, or
            # every request in flight during a backup would stall with it.
            await asyncio.to_thread(backup)
        except Exception:
            # One bad backup must never end the loop - there is always
            # another one `interval` seconds later.
            pass


def start_background_backup(interval: float | None = None) -> bool:
    """Called once from an ASYNC startup handler (never the sync one that
    calls db.init()) so asyncio.create_task has a running loop to attach to."""
    global _task
    if not enabled() or _task is not None:
        return False
    import asyncio
    _task = asyncio.create_task(_loop(interval or config.MONGODB_BACKUP_INTERVAL))
    return True


async def stop_background_backup():
    """Cancel the timer, then take one LAST backup synchronously before the
    process exits. Render sends SIGTERM before killing a spun-down instance;
    this is the one chance to flush whatever changed since the last tick."""
    import asyncio
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            # Expected: cancelling a task and then awaiting it always raises
            # this. It is a BaseException, not an Exception, in modern Python
            # - catching only Exception here let it escape and crash the ASGI
            # shutdown, which is the one bug this whole module cannot afford.
            pass
        except Exception:
            pass
        _task = None
    if enabled():
        try:
            backup()
        except Exception:
            pass
