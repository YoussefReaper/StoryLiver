"""World registry.

Starter worlds ship with the app; player-forged and AI-bootstrapped worlds live
in SQLite as JSON. Both resolve to the same ``World`` object, so nothing
downstream knows or cares which it is getting.
"""
from __future__ import annotations

import json

from .. import db, worldkit
from .emberfall import WORLD as EMBERFALL_RAW

STARTERS_RAW = {EMBERFALL_RAW["id"]: EMBERFALL_RAW}
_starter_cache: dict[str, worldkit.World] = {}
_player_cache: dict[str, tuple[str, worldkit.World]] = {}


def starter(world_id: str) -> worldkit.World:
    if world_id not in _starter_cache:
        _starter_cache[world_id] = worldkit.load(STARTERS_RAW[world_id])
    return _starter_cache[world_id]


def starters() -> list[worldkit.World]:
    return [starter(wid) for wid in STARTERS_RAW]


def from_json(raw: dict | str, *, strict: bool = False) -> worldkit.World:
    if isinstance(raw, str):
        raw = json.loads(raw)
    return worldkit.load(raw, strict=strict)


def get(world_id: str) -> worldkit.World:
    """Starter by id, else a stored player world by id."""
    if world_id in STARTERS_RAW:
        return starter(world_id)
    row = db.row("SELECT id, json, updated_at FROM worlds WHERE id=?", (world_id,))
    if not row:
        raise KeyError(f"unknown world: {world_id}")
    cached = _player_cache.get(world_id)
    if cached and cached[0] == row["updated_at"]:
        return cached[1]
    world = from_json(row["json"])
    _player_cache[world_id] = (row["updated_at"], world)
    return world


def resolve(world_id: str, world_json: str | None = None) -> worldkit.World:
    """A playthrough pins its world JSON at creation, so editing a world later
    never rewrites a story already in progress."""
    if world_json:
        return from_json(world_json)
    return get(world_id)


def forget(world_id: str) -> None:
    _player_cache.pop(world_id, None)


def exists(world_id: str) -> bool:
    if world_id in STARTERS_RAW:
        return True
    return bool(db.row("SELECT id FROM worlds WHERE id=?", (world_id,)))
