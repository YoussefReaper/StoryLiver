"""Realtime layer: cache, pub/sub, presence, turn queue, locks.

Backed by Redis (Upstash-compatible) when ``REDIS_URL`` is set, and by an
in-process implementation with the identical API when it is not. The fallback
is correct for a single worker, which is what local development and a single
free-tier dyno actually are; set ``REDIS_URL`` the moment you run more than one.

SQLite remains the system of record. Redis holds hot session state so a turn
does not re-read cold storage, and carries the fan-out that WebSockets need.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, AsyncIterator, Optional

REDIS_URL = os.getenv("REDIS_URL", "").strip()
PRESENCE_TTL = int(os.getenv("STORYLIVER_PRESENCE_TTL", "45"))
SESSION_TTL = int(os.getenv("STORYLIVER_SESSION_TTL", "86400"))

_redis_sync: Any = None
_redis_async: Any = None


def enabled() -> bool:
    return bool(REDIS_URL)


def backend_name() -> str:
    return "redis" if REDIS_URL else "in-process"


def _sync():
    global _redis_sync
    if _redis_sync is None:
        import redis
        _redis_sync = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_sync


def _aio():
    global _redis_async
    if _redis_async is None:
        import redis.asyncio as aioredis
        _redis_async = aioredis.Redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_async


# ---------------------------------------------------------------------------
# In-process fallback
# ---------------------------------------------------------------------------

class _Local:
    """Single-process stand-in. Same surface as the Redis paths below."""

    def __init__(self) -> None:
        self.kv: dict[str, tuple[float | None, str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.channels: dict[str, list[asyncio.Queue]] = {}
        self.locks: dict[str, float] = {}

    def _live(self, key: str) -> bool:
        item = self.kv.get(key)
        if item is None:
            return False
        expiry, _ = item
        if expiry is not None and expiry < time.time():
            self.kv.pop(key, None)
            return False
        return True

    def get(self, key: str) -> Optional[str]:
        return self.kv[key][1] if self._live(key) else None

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        self.kv[key] = (time.time() + ttl if ttl else None, value)

    def delete(self, *keys: str) -> None:
        for k in keys:
            self.kv.pop(k, None)
            self.hashes.pop(k, None)

    def hgetall(self, key: str) -> dict[str, str]:
        out = {}
        for field, raw in list(self.hashes.get(key, {}).items()):
            expiry, value = json.loads(raw)
            if expiry is not None and expiry < time.time():
                self.hashes[key].pop(field, None)
                continue
            out[field] = value
        return out

    def hset(self, key: str, field: str, value: str, ttl: int | None = None) -> None:
        self.hashes.setdefault(key, {})[field] = json.dumps(
            [time.time() + ttl if ttl else None, value])

    def hdel(self, key: str, field: str) -> None:
        self.hashes.get(key, {}).pop(field, None)

    def scan(self, prefix: str) -> list[str]:
        return [k for k in list(self.kv) if k.startswith(prefix) and self._live(k)]


_local = _Local()


# ---------------------------------------------------------------------------
# Cache (sync — called from the request threadpool alongside SQLite)
# ---------------------------------------------------------------------------

def cache_get(key: str) -> Any:
    raw = _sync().get(key) if REDIS_URL else _local.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def cache_set(key: str, value: Any, ttl: int = SESSION_TTL) -> None:
    raw = json.dumps(value, default=str)
    if REDIS_URL:
        _sync().set(key, raw, ex=ttl)
    else:
        _local.set(key, raw, ttl)


def cache_drop(*keys: str) -> None:
    if not keys:
        return
    if REDIS_URL:
        _sync().delete(*keys)
    else:
        _local.delete(*keys)


def invalidate_playthrough(pt_id: str) -> None:
    """Every cached projection of one playthrough's hot state."""
    cache_drop(
        f"sl:pt:{pt_id}:rels",
        f"sl:pt:{pt_id}:npcstate",
        f"sl:pt:{pt_id}:events",
        f"sl:pt:{pt_id}:snapshot",
    )


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------

def presence_touch(session_id: str, player_id: str, meta: dict) -> None:
    key = f"sl:sess:{session_id}:presence"
    raw = json.dumps({**meta, "seen": time.time()}, default=str)
    if REDIS_URL:
        r = _sync()
        r.hset(key, player_id, raw)
        r.expire(key, SESSION_TTL)
    else:
        _local.hset(key, player_id, raw, PRESENCE_TTL * 4)


def presence_drop(session_id: str, player_id: str) -> None:
    key = f"sl:sess:{session_id}:presence"
    if REDIS_URL:
        _sync().hdel(key, player_id)
    else:
        _local.hdel(key, player_id)


def presence_list(session_id: str) -> list[dict]:
    key = f"sl:sess:{session_id}:presence"
    raw = _sync().hgetall(key) if REDIS_URL else _local.hgetall(key)
    now = time.time()
    out = []
    for player_id, blob in raw.items():
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            continue
        if now - float(data.get("seen", 0)) > PRESENCE_TTL:
            continue
        out.append({"player_id": player_id, **data})
    out.sort(key=lambda p: p.get("joined", 0))
    return out


# ---------------------------------------------------------------------------
# Pub/sub (async — the WebSocket fan-out)
# ---------------------------------------------------------------------------

async def publish(session_id: str, message: dict) -> None:
    channel = f"sl:chan:{session_id}"
    payload = json.dumps(message, default=str)
    if REDIS_URL:
        await _aio().publish(channel, payload)
        return
    for queue in list(_local.channels.get(channel, [])):
        queue.put_nowait(payload)


async def subscribe(session_id: str) -> AsyncIterator[dict]:
    channel = f"sl:chan:{session_id}"
    if REDIS_URL:
        pubsub = _aio().pubsub()
        await pubsub.subscribe(channel)
        try:
            async for raw in pubsub.listen():
                if raw.get("type") != "message":
                    continue
                try:
                    yield json.loads(raw["data"])
                except (json.JSONDecodeError, TypeError):
                    continue
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        return

    queue: asyncio.Queue = asyncio.Queue()
    _local.channels.setdefault(channel, []).append(queue)
    try:
        while True:
            raw = await queue.get()
            try:
                yield json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
    finally:
        subs = _local.channels.get(channel, [])
        if queue in subs:
            subs.remove(queue)


# ---------------------------------------------------------------------------
# Turn lock — actions in one session serialise
# ---------------------------------------------------------------------------

class TurnBusy(RuntimeError):
    pass


def acquire_turn(session_id: str, player_id: str, ttl: int = 90) -> bool:
    key = f"sl:sess:{session_id}:turnlock"
    if REDIS_URL:
        return bool(_sync().set(key, player_id, nx=True, ex=ttl))
    held = _local.locks.get(key)
    if held is not None and held > time.time():
        return False
    _local.locks[key] = time.time() + ttl
    _local.set(key, player_id, ttl)
    return True


def release_turn(session_id: str) -> None:
    key = f"sl:sess:{session_id}:turnlock"
    if REDIS_URL:
        _sync().delete(key)
    else:
        _local.locks.pop(key, None)
        _local.delete(key)


def turn_holder(session_id: str) -> Optional[str]:
    key = f"sl:sess:{session_id}:turnlock"
    return _sync().get(key) if REDIS_URL else _local.get(key)


def health() -> dict:
    info = {"backend": backend_name(), "url_set": bool(REDIS_URL), "ok": True}
    if REDIS_URL:
        try:
            _sync().ping()
        except Exception as exc:                     # surfaced in /api/health
            info.update(ok=False, error=str(exc)[:200])
    return info
