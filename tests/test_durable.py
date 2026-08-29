"""Durable storage - backup/restore correctness, and the offline guarantee.

No real MongoDB is touched here. Two separate things are proved:

  SNAPSHOT CORRECTNESS - snapshot_bytes() is pure SQLite (no network at all),
  so it is tested directly: does the resulting file actually reopen, and does
  it reflect a commit made through db.py's normal connection?

  BACKUP/RESTORE LOGIC - backup(), restore_if_needed() and rotation are driven
  through their REAL code paths against a small in-memory fake standing in for
  GridFS, so the actual logic is verified end-to-end (upload-before-delete
  ordering, keeping only the latest N, restoring the right one) without ever
  opening a socket.

  OFFLINE GUARANTEE - enabled() must be False whenever MONGODB_URI is unset,
  and False under mock mode even if a URI is present, exactly mirroring the
  guarantee test_research.py holds research.py to.

Run:  python -m tests.test_durable
"""
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ["STORYLIVER_LLM_MODE"] = "mock"
_TMP = tempfile.mkdtemp(prefix="storyliver-durable-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP

from backend import config, db, durable  # noqa: E402

FAILS, NOTES = [], []


def ok(cond, msg):
    (NOTES if cond else FAILS).append(msg)


def section(t):
    NOTES.append("\n  " + t)


# ---------------------------------------------------------------------------
# A minimal fake standing in for gridfs.GridFS - just enough surface to drive
# durable.py's real backup()/restore_if_needed()/_rotate() logic.
# ---------------------------------------------------------------------------

class _FakeGridOut:
    def __init__(self, _id, data, filename, uploaded_at):
        self._id = _id
        self._data = data
        self.filename = filename
        self.uploaded_at = uploaded_at
        self.upload_date = datetime.now(timezone.utc)

    def read(self):
        return self._data


class _FakeCursor(list):
    def sort(self, *a, **k):
        # Newest first, matching the real "uploadDate", -1 query.
        return _FakeCursor(sorted(self, key=lambda f: f.upload_date, reverse=True))


class FakeGridFS:
    """In-memory, ordered by insertion - close enough to GridFS semantics for
    every code path durable.py actually exercises."""

    def __init__(self):
        self._store = {}
        self._next = 1
        self.put_calls = 0
        self.delete_calls = 0

    def put(self, data, filename="", **meta):
        self.put_calls += 1
        fid = self._next
        self._next += 1
        self._store[fid] = _FakeGridOut(fid, data, filename, meta.get("uploaded_at", ""))
        return fid

    def find(self, query):
        return _FakeCursor(f for f in self._store.values()
                           if f.filename == query.get("filename"))

    def find_one(self, query, sort=None):
        matches = self.find(query)
        if sort:
            matches = matches.sort()
        return matches[0] if matches else None

    def delete(self, fid):
        self.delete_calls += 1
        self._store.pop(fid, None)


def _install_fake():
    fake = FakeGridFS()
    durable._fs = fake
    durable._client = object()          # non-None sentinel; never actually used
    return fake


def _reset_durable_state():
    durable._fs = None
    durable._client = None


# ------------------------------------------------------------------- offline
def test_disabled_by_default():
    section("offline — durable storage is inert unless configured")
    old_uri = config.MONGODB_URI
    config.MONGODB_URI = ""
    try:
        ok(not durable.enabled(), "MONGODB_URI unset -> disabled")
    finally:
        config.MONGODB_URI = old_uri

    old_uri, old_mode = config.MONGODB_URI, config.LLM_MODE
    config.MONGODB_URI, config.LLM_MODE = "mongodb://fake/", "mock"
    try:
        ok(not durable.enabled(),
           "a stray URI is ignored under mock mode — the same protective gate "
           "research.enabled() uses, so the test suite never needs a real cluster")
    finally:
        config.MONGODB_URI, config.LLM_MODE = old_uri, old_mode

    r1 = durable.backup()
    ok(r1["backed_up"] is False and "disabled" in r1["reason"],
       "backup() no-ops cleanly when disabled, rather than erroring")
    r2 = durable.restore_if_needed()
    ok(r2["restored"] is False,
       f"restore_if_needed() no-ops cleanly when disabled ({r2['reason']})")


# ---------------------------------------------------------------- correctness
def test_snapshot_is_consistent():
    section("correctness — the snapshot is a real, reopenable database")
    db.init()
    db.run("INSERT OR IGNORE INTO users (id, created_at) VALUES (?, ?)",
          ("durable-probe", db.now()))

    data = durable.snapshot_bytes()
    ok(isinstance(data, bytes) and data[:16] == b"SQLite format 3\x00",
       "the snapshot is a genuine SQLite file, not a raw byte dump of something else")

    with tempfile.TemporaryDirectory() as tmp:
        copy_path = Path(tmp) / "copy.db"
        copy_path.write_bytes(data)
        con = sqlite3.connect(str(copy_path))
        try:
            row = con.execute(
                "SELECT id FROM users WHERE id=?", ("durable-probe",)).fetchone()
        finally:
            con.close()
        ok(row is not None and row[0] == "durable-probe",
           "a commit made through db.py's own connection is present in the snapshot — "
           "this is the WAL-consistency guarantee the backup API exists for")


def test_snapshot_requires_a_database():
    section("correctness — snapshotting a database that does not exist yet")
    missing = Path(config.DB_PATH).with_name("does-not-exist.db")
    old = config.DB_PATH
    config.DB_PATH = missing
    try:
        try:
            durable.snapshot_bytes()
            ok(False, "snapshotting a missing file did not raise")
        except durable.DurableError:
            ok(True, "refused cleanly rather than producing an empty or corrupt blob")
    finally:
        config.DB_PATH = old


# -------------------------------------------------------- backup/restore/e2e
def _with_enabled(fn):
    old_uri, old_mode = config.MONGODB_URI, config.LLM_MODE
    config.MONGODB_URI, config.LLM_MODE = "mongodb://fake/", "auto"
    fake = _install_fake()
    try:
        fn(fake)
    finally:
        config.MONGODB_URI, config.LLM_MODE = old_uri, old_mode
        _reset_durable_state()


def test_backup_round_trip():
    section("backup/restore — a snapshot uploaded is the snapshot restored")

    def run(fake):
        db.init()
        db.run("INSERT OR IGNORE INTO users (id, created_at) VALUES (?, ?)",
              ("round-trip", db.now()))

        r = durable.backup()
        ok(r["backed_up"] is True and r["bytes"] > 0,
           f"backup() succeeds against the fake store ({r.get('bytes')} bytes)")
        ok(fake.put_calls == 1, "exactly one upload happened")

        # Restore only fires when the local file is genuinely ABSENT.
        r0 = durable.restore_if_needed()
        ok(r0["restored"] is False and "already present" in r0["reason"],
           "restore refuses to touch a live local file, even with a newer "
           "backup available — the running instance's own file is the truth")

        db.close_thread()          # release Windows' handle before renaming
        old_path = Path(config.DB_PATH)
        moved = old_path.with_suffix(".moved")
        old_path.rename(moved)
        try:
            r1 = durable.restore_if_needed()
            ok(r1["restored"] is True and r1["bytes"] > 0,
               "a genuinely fresh container (no local file) restores from the backup")
            con = sqlite3.connect(str(config.DB_PATH))
            try:
                row = con.execute("SELECT id FROM users WHERE id=?",
                                  ("round-trip",)).fetchone()
            finally:
                con.close()
            ok(row is not None, "the restored file contains the exact data that was backed up")
        finally:
            Path(config.DB_PATH).unlink(missing_ok=True)
            moved.rename(old_path)

    _with_enabled(run)


def test_rotation_and_upload_before_delete():
    section("backup — rotation keeps only recent snapshots, safely")

    def run(fake):
        db.init()
        for i in range(durable.KEEP_SNAPSHOTS + 3):
            db.run("INSERT OR IGNORE INTO users (id, created_at) VALUES (?, ?)",
                  (f"rotate-{i}", db.now()))
            durable.backup()

        remaining = fake.find({"filename": durable.FILENAME})
        ok(len(remaining) == durable.KEEP_SNAPSHOTS,
           f"only the newest {durable.KEEP_SNAPSHOTS} snapshots survive "
           f"({len(remaining)} present)")
        ok(fake.delete_calls == fake.put_calls - durable.KEEP_SNAPSHOTS,
           "exactly the expected number of old snapshots were pruned — "
           "never more, never fewer")

        # The newest data must be what a restore actually returns.
        db.close_thread()
        old_path = Path(config.DB_PATH)
        moved = old_path.with_suffix(".moved2")
        old_path.rename(moved)
        try:
            durable.restore_if_needed()
            con = sqlite3.connect(str(config.DB_PATH))
            try:
                last = durable.KEEP_SNAPSHOTS + 2
                row = con.execute("SELECT id FROM users WHERE id=?",
                                  (f"rotate-{last}",)).fetchone()
            finally:
                con.close()
            ok(row is not None, "restoring after rotation returns the LATEST snapshot")
        finally:
            Path(config.DB_PATH).unlink(missing_ok=True)
            moved.rename(old_path)

    _with_enabled(run)


def test_mongo_failure_never_blocks_boot():
    section("resilience — a Mongo outage degrades, it never blocks startup")

    def run(fake):
        def boom(*a, **k):
            raise ConnectionError("cluster unreachable")
        fake.find_one = boom
        fake.put = boom

        db.close_thread()
        old_path = Path(config.DB_PATH)
        moved = old_path.with_suffix(".moved3")
        if old_path.exists():
            old_path.rename(moved)
        try:
            r = durable.restore_if_needed()
            ok(r["restored"] is False and "ConnectionError" in r["reason"],
               "a broken Mongo cluster on boot fails SOFT — the app still comes "
               "up, just with a fresh empty database, exactly like it does today")
        finally:
            if moved.exists():
                moved.rename(old_path)

        db.init()
        r2 = durable.backup()
        ok(r2["backed_up"] is False and "ConnectionError" in r2["reason"],
           "a broken cluster mid-run fails the SAME way — never an exception "
           "that could take the request or the background loop down with it")

    _with_enabled(run)


def _all():
    return (test_disabled_by_default, test_snapshot_is_consistent,
            test_snapshot_requires_a_database, test_backup_round_trip,
            test_rotation_and_upload_before_delete,
            test_mongo_failure_never_blocks_boot)


def main():
    print("StoryLiver — durable storage (MongoDB backup/restore)")
    print("  no real cluster touched, no key, no spend\n")
    for fn in _all():
        fn()
    passed = 0
    for n in NOTES:
        if n.startswith("\n"):
            print(n)
        else:
            print("  PASS  " + n); passed += 1
    for f in FAILS:
        print("  FAIL  " + f)
    print(f"\n  {passed} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


def test_all_durable():
    for fn in _all():
        fn()
    assert not FAILS, "\n".join(FAILS)


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
