"""Cost architecture (§8) - the valve the whole design depends on.

Two rules, enforced here rather than trusted:

  1. Deterministic systems cost nothing. Stats, reputation, stealth, witness
     propagation, combat math, pre-commit, relationship deltas and world-state
     all run every turn for every entity at $0.
  2. Only six things may call a model, and a turn may make at most N of them.
     The ledger is per-turn and asserted in tests, so a new feature cannot
     quietly add a seventh caller or a runaway loop.

Mana prices LLM ACTIONS, not turns: exploring is cheap, a cinematic combat
resolve or a plot twist is not.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

# The only roles permitted to reach a model. Anything else raises.
ALLOWED = {
    "narrator",           # prose
    "narrator_premium",
    "world_master",       # ambiguous adjudication / contested arbitration
    "npc",                # ambiguous NPC decision, whisper reply, reflection
    "director",           # plot twist / beat
    "rumor",              # narrating a rumour's arrival
    "ooc",                # token-thrifty World Master reply in the OOC channel
    "card",               # character-card autofill, cheapest model
}

# Hard ceiling on model calls inside one resolved turn.
MAX_LLM_CALLS_PER_TURN = 6

# Mana per LLM action. Deterministic actions are absent from this table because
# they are free; asking for one costs nothing.
ACTION_PRICE = {
    "action": 1,             # an ordinary turn: WM (maybe) + narrator
    "action_premium": 3,     # the stronger narrator
    "whisper": 1,
    "twist": 2,              # Director-authored plot twist
    "rumor": 1,
    "ooc": 0,                # capped at ~40 tokens; effectively free, kept honest
    "card_autofill": 1,
    "combat_resolve": 1,     # the round's narration
    "combat_cinematic": 3,   # the long version, only on request
    "contest": 2,            # PVP arbitration
    "bootstrap": 6,          # building a whole world
    "voice_studio": 2,
}

FREE_ACTIONS = (
    "move", "look", "atlas", "graph", "stats", "reputation", "stealth_check",
    "witness", "precommit", "reveal", "combat_declare", "combat_math",
    "relationship_delta", "world_tick", "hunt_tick", "knowledge_query",
)


class BudgetExceeded(RuntimeError):
    pass


class _Ledger(threading.local):
    def __init__(self) -> None:
        self.active = False
        self.calls: list[str] = []
        self.limit = MAX_LLM_CALLS_PER_TURN
        self.label = ""


_ledger = _Ledger()


@contextmanager
def turn(label: str = "", limit: int | None = None):
    """Wrap one resolved turn. Nested turns share the outermost ledger so a
    sub-system cannot escape the cap by opening its own."""
    if _ledger.active:
        yield _ledger
        return
    _ledger.active = True
    _ledger.calls = []
    _ledger.limit = MAX_LLM_CALLS_PER_TURN if limit is None else limit
    _ledger.label = label
    try:
        yield _ledger
    finally:
        _ledger.active = False


def record(role: str) -> None:
    """Called by llm.complete on every model call."""
    if role not in ALLOWED:
        raise BudgetExceeded(
            f"role {role!r} may not call a model; add it to budget.ALLOWED "
            f"deliberately, or make the system deterministic")
    if not _ledger.active:
        return
    _ledger.calls.append(role)
    if len(_ledger.calls) > _ledger.limit:
        raise BudgetExceeded(
            f"turn {_ledger.label!r} tried {len(_ledger.calls)} model calls "
            f"(cap {_ledger.limit}): {_ledger.calls}")


def used() -> list[str]:
    return list(_ledger.calls)


def remaining() -> int:
    return max(0, _ledger.limit - len(_ledger.calls)) if _ledger.active else MAX_LLM_CALLS_PER_TURN


def affordable(role: str) -> bool:
    """Lets an optional caller (Director, NPC sim) stand down instead of raising."""
    return not _ledger.active or len(_ledger.calls) < _ledger.limit


def price(action: str, premium: bool = False) -> int:
    if action == "action" and premium:
        return ACTION_PRICE["action_premium"]
    return ACTION_PRICE.get(action, 1)


def table() -> dict:
    return {
        "llm_actions": dict(sorted(ACTION_PRICE.items())),
        "free_actions": sorted(FREE_ACTIONS),
        "max_llm_calls_per_turn": MAX_LLM_CALLS_PER_TURN,
        "allowed_roles": sorted(ALLOWED),
        "note": "Mana prices model calls, never turns. Everything deterministic is free.",
    }
