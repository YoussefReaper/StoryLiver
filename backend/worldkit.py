"""World abstraction.

The MVP hardcoded Emberfall. The full product treats every world - the starter,
one a player forged by hand, one the AI bootstrapped from a setting they named -
as the same shape of data behind one interface. Emberfall becomes an optional
starter world rather than the product.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

PHASES = ["morning", "midday", "evening", "night"]

REQUIRED_NPC_ANCHORS = ("voice", "constraints", "goals", "taboos")
MIN_RULES = 9
MIN_FATED = 7


class WorldError(ValueError):
    pass


def slug(text: str, fallback: str = "x") -> str:
    out = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return out[:40] or fallback


class World:
    """Read-only view over a world dict, with the lookups the engine needs."""

    __slots__ = ("data", "id", "name", "by_id", "loc_by_id", "rule_by_id", "npcs",
                 "locations", "rules", "fated_events")

    def __init__(self, data: dict):
        self.data = data
        self.id = data["id"]
        self.name = data["name"]
        self.npcs = data["npcs"]
        self.locations = data["locations"]
        self.rules = data["rules"]
        self.fated_events = sorted(data["fated_events"], key=lambda f: f["turn"])
        self.by_id = {n["id"]: n for n in self.npcs}
        self.loc_by_id = {l["id"]: l for l in self.locations}
        self.rule_by_id = {r["id"]: r for r in self.rules}

    # -- convenience --------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default=None) -> Any:
        return self.data.get(key, default)

    def phase_for(self, turn: int) -> str:
        return PHASES[turn % 4]

    def day_for(self, turn: int) -> int:
        return turn // 4 + 1

    def npc_name(self, npc_id: str) -> str:
        npc = self.by_id.get(npc_id)
        return npc["name"] if npc else npc_id

    def loc_name(self, loc_id: str) -> str:
        loc = self.loc_by_id.get(loc_id)
        return loc["name"] if loc else loc_id

    def connects(self, loc_id: str) -> list[str]:
        loc = self.loc_by_id.get(loc_id)
        return list(loc.get("connects", [])) if loc else []

    def fate_kills(self) -> dict[str, str]:
        return {f["id"]: f["kills"] for f in self.fated_events if f.get("kills")}

    def summary(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "tagline": self.get("tagline", ""),
            "premise": self.get("premise", ""),
            "fate_note": self.get("fate_note", ""),
            "npc_count": len(self.npcs), "fated_events": len(self.fated_events),
            "locations": len(self.locations), "rules": len(self.rules),
            "origin": self.get("origin", "forged"),
            "personal_only": bool(self.get("personal_only")),
            "start_location": self.get("start_location"),
        }


# ---------------------------------------------------------------------------
# Validation / normalisation
# ---------------------------------------------------------------------------

def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(v).strip() for v in value if str(v).strip()]



def _factions(raw: dict, npc_ids: set, loc_ids: set) -> list:
    """Declared power blocs, cleaned against the world that actually exists.

    A faction naming a character or a seat that is not in the world would make
    the authority layer point at nothing, so members and seats are filtered to
    real ids and an officer's patrol schedule is filtered to real places. A
    world that declares no factions gets an empty list here; awareness.py
    supplies the diffuse fallback, so this stays a declaration, not a default.
    """
    out = []
    for f in raw.get("factions") or []:
        fid = slug(f.get("id") or f.get("name") or "")
        if not fid:
            continue
        members = [m for m in _as_list(f.get("members")) if m in npc_ids]
        officers = []
        for o in f.get("officers") or []:
            oid = str(o.get("id") or "").strip()
            if oid not in npc_ids:
                continue
            sched = {}
            for phase, where in (o.get("schedule") or {}).items():
                places = [w for w in _as_list(where) if w in loc_ids]
                if places:
                    sched[str(phase)] = places[0] if len(places) == 1 else places
            officers.append({"id": oid, "name": str(o.get("name") or oid),
                             "rank": str(o.get("rank") or "officer"),
                             "schedule": sched})
        seat = f.get("seat") if f.get("seat") in loc_ids else ""
        out.append({
            "id": fid, "name": str(f.get("name") or fid),
            "seat": seat, "law": max(0, min(10, int(f.get("law") or 0))),
            "members": members or [o["id"] for o in officers],
            "officers": officers,
        })
    return out[:8]


def _npc_edges(raw: dict, npc_ids: set) -> list:
    """Directed feelings between characters: (src, dst, (aff, trust, fear, obl)).

    Filtered to characters that exist and clamped to the range the relationship
    engine uses, so a malformed world seeds nothing rather than poisoning the
    table."""
    out = []
    for edge in raw.get("npc_edges") or []:
        try:
            src, dst, vals = edge[0], edge[1], list(edge[2])
        except (TypeError, IndexError, KeyError):
            continue
        if src not in npc_ids or dst not in npc_ids or src == dst:
            continue
        vals = [max(-100.0, min(100.0, float(v))) for v in (vals + [0, 0, 0, 0])[:4]]
        out.append((src, dst, tuple(vals)))
    return out[:60]


def normalise(raw: dict, *, strict: bool = True) -> dict:
    """Coerce a world dict (hand-authored, imported, or model-generated) into the
    engine's shape. Raises WorldError on anything the engine cannot run."""
    if not isinstance(raw, dict):
        raise WorldError("world must be an object")

    name = str(raw.get("name") or "").strip()
    if not name:
        raise WorldError("world needs a name")

    world_id = slug(raw.get("id") or name, "world")

    locations = []
    for i, loc in enumerate(raw.get("locations") or []):
        lid = slug(loc.get("id") or loc.get("name") or f"place_{i}", f"place_{i}")
        locations.append({
            "id": lid,
            "name": str(loc.get("name") or lid.replace("_", " ").title()),
            "kind": str(loc.get("kind") or "place"),
            "desc": str(loc.get("desc") or ""),
            "connects": [slug(c) for c in _as_list(loc.get("connects"))],
        })
    if len(locations) < 2:
        raise WorldError("world needs at least 2 locations")

    valid_locs = {l["id"] for l in locations}
    for loc in locations:
        loc["connects"] = [c for c in loc["connects"] if c in valid_locs and c != loc["id"]]
    # Guarantee the map is traversable: anything stranded links to the first place.
    hub = locations[0]["id"]
    for loc in locations[1:]:
        if not loc["connects"]:
            loc["connects"] = [hub]
            if loc["id"] not in locations[0]["connects"]:
                locations[0]["connects"].append(loc["id"])
    for loc in locations:                       # make adjacency symmetric
        for other_id in loc["connects"]:
            other = next(l for l in locations if l["id"] == other_id)
            if loc["id"] not in other["connects"]:
                other["connects"].append(loc["id"])
    if not locations[0]["connects"]:
        locations[0]["connects"] = [locations[1]["id"]]

    npcs = []
    seen_npc: set[str] = set()
    for i, npc in enumerate(raw.get("npcs") or []):
        nid = slug(npc.get("id") or npc.get("name") or f"npc_{i}", f"npc_{i}")
        while nid in seen_npc:
            nid = f"{nid}_{i}"
        seen_npc.add(nid)
        anchors = npc.get("anchors") or {}
        start = slug(npc.get("start_location") or "")
        if start not in valid_locs:
            start = locations[i % len(locations)]["id"]
        schedule_in = npc.get("schedule") or {}
        schedule = {}
        for phase in PHASES:
            where = slug(schedule_in.get(phase) or "")
            schedule[phase] = where if where in valid_locs else start
        npcs.append({
            "id": nid,
            "name": str(npc.get("name") or nid.replace("_", " ").title()),
            "role": str(npc.get("role") or anchors.get("role") or "villager"),
            "start_location": start,
            "anchors": {
                "name": str(npc.get("name") or nid),
                "role": str(npc.get("role") or anchors.get("role") or "villager"),
                "voice": str(anchors.get("voice") or "Plain, direct, unhurried."),
                "constraints": _as_list(anchors.get("constraints")) or ["Is an ordinary mortal person."],
                "goals": _as_list(anchors.get("goals")) or ["Survive what is coming."],
                "taboos": _as_list(anchors.get("taboos")) or ["Never breaks their own word."],
            },
            "schedule": schedule,
            "seed_memories": _as_list(npc.get("seed_memories"))[:6],
            "initial_relationship": {
                k: max(-100, min(100, float((npc.get("initial_relationship") or {}).get(k, 0) or 0)))
                for k in ("affinity", "trust", "fear", "obligation")
            },
        })
    if len(npcs) < 2:
        raise WorldError("world needs at least 2 characters")

    rules = []
    for i, rule in enumerate(raw.get("rules") or []):
        text = str(rule.get("text") if isinstance(rule, dict) else rule or "").strip()
        if not text:
            continue
        rid = slug((rule.get("id") if isinstance(rule, dict) else None) or f"R{i+1}_{text[:24]}", f"R{i+1}")
        rules.append({"id": rid, "text": text,
                      "check": (rule.get("check") if isinstance(rule, dict) else None) or None})
    if strict and len(rules) < MIN_RULES:
        raise WorldError(f"world needs at least {MIN_RULES} rules (got {len(rules)})")

    fated = []
    for i, ev in enumerate(sorted(raw.get("fated_events") or [], key=lambda e: e.get("turn", 0))):
        turn = int(ev.get("turn") or (i + 1) * 6)
        loc = slug(ev.get("location") or "")
        fated.append({
            "id": slug(ev.get("id") or f"F{i+1}_{ev.get('title', '')}", f"F{i+1}"),
            "turn": max(1, turn),
            "immutable": True,
            "title": str(ev.get("title") or f"Fated event {i+1}"),
            "desc": str(ev.get("desc") or ev.get("title") or ""),
            "location": loc if loc in valid_locs else locations[0]["id"],
            "kills": slug(ev["kills"]) if ev.get("kills") and slug(ev["kills"]) in seen_npc else None,
        })
    # Fated turns must be strictly increasing or two fire at once.
    for i in range(1, len(fated)):
        if fated[i]["turn"] <= fated[i - 1]["turn"]:
            fated[i]["turn"] = fated[i - 1]["turn"] + 1
    if strict and len(fated) < MIN_FATED:
        raise WorldError(f"world needs at least {MIN_FATED} fated events (got {len(fated)})")

    start_location = slug(raw.get("start_location") or "")
    if start_location not in valid_locs:
        start_location = locations[0]["id"]

    return {
        "id": world_id,
        "name": name,
        "tagline": str(raw.get("tagline") or ""),
        "premise": str(raw.get("premise") or ""),
        "fate_note": str(raw.get("fate_note") or "Fate is fixed. Your path through it is not."),
        "start_location": start_location,
        "default_protagonist": str(raw.get("default_protagonist") or "a traveller nobody here has heard of"),
        "opening": str(raw.get("opening") or raw.get("premise") or ""),
        "arrival": str(raw.get("arrival") or f"You arrive in {name}."),
        "locations": locations,
        "rules": rules,
        "fated_events": fated,
        "npcs": npcs,
        "origin": str(raw.get("origin") or "forged"),
        "source_prompt": str(raw.get("source_prompt") or ""),
        "personal_only": bool(raw.get("personal_only")),
        "inspired_by": str(raw.get("inspired_by") or ""),
        "factions": _factions(raw, {n["id"] for n in npcs},
                              {l["id"] for l in locations}),
        # How the CHARACTERS feel about each other, not just about the player.
        # Dropped here until now, so a world could declare that Nessa distrusts
        # Corvin and the engine seeded nothing - two people who cannot stand
        # each other stood in a room being uniformly pleasant.
        "npc_edges": _npc_edges(raw, {n["id"] for n in npcs}),
        # Where a bootstrapped world was researched from. Kept on the world so
        # attribution survives export, and so a player can see their sources.
        "sources": [
            {"title": str(x.get("title", ""))[:120],
             "url": str(x.get("url", ""))[:300],
             "source": str(x.get("source", ""))[:40],
             "license": str(x.get("license", ""))[:40]}
            for x in (raw.get("sources") or [])[:8]
            if str(x.get("url", "")).startswith("https://")
        ],
        "researched": bool(raw.get("researched")),
        # "canon" (continued from a real setting) or "original".
        "mode": "canon" if raw.get("mode") == "canon" else "original",
    }


def load(raw: dict, *, strict: bool = True) -> World:
    return World(normalise(raw, strict=strict))
