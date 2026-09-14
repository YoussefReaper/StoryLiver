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

# How many characters the opening scene must contain. Three is the smallest
# number that can hold a conversation the player is not the centre of - two
# people disagreeing while a third watches - which is what makes a room feel
# inhabited rather than staffed.
OPENING_CAST = 3


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


# A portrait is a path this server issued, never a URL.
#
# A world dict is untrusted input - hand-edited, imported from a file, or
# produced by a model - so an arbitrary `portrait` would let one embed an
# off-site image: a tracking pixel that fires for every player who meets that
# character, or a `javascript:` string dropped into an href somewhere
# downstream. Accepting ONLY what backend/uploads.store() produced makes the
# field structurally incapable of pointing anywhere but this origin.
_MEDIA_PATH = re.compile(r"^/media/[0-9a-f]{32}\.(?:png|jpg|gif|webp)$")


def _portrait(value: Any) -> str:
    text = str(value or "").strip()
    return text if _MEDIA_PATH.match(text) else ""



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


# The wider identity card. Empty keys are OMITTED rather than defaulted:
# anchor_block tests for presence to decide which block to build, so writing
# empty lists here would flip every world onto the persona path and change the
# prompt bytes of worlds that never asked for it.
_CARD_LISTS = ("mannerisms", "values", "flaws", "secrets", "catchphrases",
               "relationships_to_canon")
_CARD_TEXT = ("power_profile", "backstory", "concept", "speech_constraints")


def _card(anchors: dict) -> dict:
    out = {}
    for field in _CARD_LISTS:
        items = _as_list(anchors.get(field))[:6]
        if items:
            out[field] = items
    for field in _CARD_TEXT:
        value = str(anchors.get(field) or "").strip()
        if value:
            out[field] = value[:400]
    lines = []
    for entry in anchors.get("famous_lines") or []:
        if isinstance(entry, dict):
            line = str(entry.get("line") or "").strip()
            if line:
                lines.append({"beat": str(entry.get("beat") or "").strip()[:24],
                              "line": line[:200]})
        elif str(entry).strip():
            lines.append({"beat": "", "line": str(entry).strip()[:200]})
    if lines:
        out["famous_lines"] = lines[:8]
    return out


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


def _revival(raw: dict) -> dict:
    r = raw.get("revival_rule") or {}
    if not isinstance(r, dict):
        return {}
    name = str(r.get("name") or "").strip()
    cost = str(r.get("cost") or "").strip()
    # A rule with no cost is not a rule. Worlds that say "nothing brings anyone
    # back" land here and correctly declare nothing.
    if not (name and cost):
        return {}
    return {"name": name[:80], "cost": cost[:200]}


def _orgs(raw: dict, npc_ids: set, loc_ids: set) -> list:
    out = []
    for o in raw.get("orgs") or []:
        oid = slug(o.get("id") or o.get("name") or "")
        name = str(o.get("name") or "").strip()
        if not (oid and name):
            continue
        out.append({
            "id": oid, "name": name[:60],
            "kind": (str(o.get("kind") or "cell").strip().lower()
                     if str(o.get("kind") or "").strip().lower() in
                     ("cell", "house", "company", "order", "crew") else "cell"),
            "seat": o.get("seat") if o.get("seat") in loc_ids else "",
            "charter": str(o.get("charter") or "")[:200],
            "members": [m for m in _as_list(o.get("members")) if m in npc_ids][:8],
        })
    return out[:5]


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
            # A real place of the setting that this world knows the name of and
            # has not built yet. You can walk to it; arriving is what makes it
            # real. See worldforge.expand_frontier - a town is a town, not the
            # edge of the universe, and the canon geography the build did not
            # use is the obvious place for it to grow into.
            "frontier": bool(loc.get("frontier")),
            # F3: "canon" (a real, researched/seeded name) or "original"
            # (invented to fill a scale the source material didn't cover) -
            # worldforge stamps this when it knows the difference; a
            # hand-forged or imported world has no such distinction to make,
            # so it defaults to "canon" rather than mislabelling authored
            # content as filler.
            #
            # IN A CANON WORLD the default is the other way round, and it
            # matters: worldforge stamps every record explicitly before this
            # runs, so a MISSING stamp here means something got past that pass
            # - and calling an untraced name "canon" is the one error that
            # cannot be caught by reading. An untraced name in a world built
            # from a real source is the builder's own work until proven
            # otherwise. `raw.get("mode")` is read below from the same dict.
            "origin": str(loc.get("origin")
                          or ("original" if raw.get("mode") == "canon" else "canon")),
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
    # ONE PERSON IS ONE RECORD - but only when they really are one person.
    # Every other guard in this file is about ids, and ids are unique by
    # construction, so two entries both called "Canute" - one written by the
    # builder and one seated by the canon pass - both survived and the narrator
    # was handed the same character twice, breaking the engine's own R5 law.
    #
    # The fix has to key on IDENTITY, not on the name string alone. A plain
    # name guard also deletes honest namesakes: every district of an 8-district
    # world that names a local "Person 1" (offline stub) or two unrelated
    # villages that each have a "Yuki" collapsed into one, and a world-scale
    # build came back with 6 characters where 48 were generated. Two records
    # are the SAME person only when they share a name AND plausibly share a
    # life - same canon origin, or the same home location. Two strangers who
    # happen to be called the same thing are two characters, and the id
    # uniqueness above already keeps them apart on the wire.
    seen_person: dict[str, list[dict]] = {}

    def _same_person(a: dict, b: dict) -> bool:
        # A CANON WORLD HAS ONE OF EACH. The origin/home test below exists to
        # protect honest namesakes - two villages that each have a Yuki, eight
        # districts that each name a local "Person 1" - and those are invented
        # people. A canon character is not like that: there is exactly one
        # Satoru Gojo in Jujutsu Kaisen, and if he turns up twice it is because
        # two passes both seated him, not because the world has two. Applying
        # the namesake rule to canon is what let a live build seat "Satoru
        # Gojo" (the persona pass, source spelling) beside "gojo satoru" (the
        # player's typing, carried in as an import) as two separate people.
        # Requiring BOTH to be stamped canon was too narrow. The player's
        # carried-in "gojo satoru" and the persona pass's "Satoru Gojo" are one
        # man, and the two records reach here with different origin stamps
        # because different passes made them - so the guard read them as a
        # namesake pair and a live world shipped both. In a canon world, two
        # records sharing a name are the same person unless BOTH are invented
        # locals, which is the only place honest namesakes actually come from.
        if raw.get("mode") == "canon" and not (
                (a.get("origin") or "") == "original"
                and (b.get("origin") or "") == "original"):
            return True
        if (a.get("origin") or "") != (b.get("origin") or ""):
            return False
        home_a, home_b = a.get("start_location"), b.get("start_location")
        if home_a and home_b:
            return home_a == home_b
        return True

    # A SHORTER NAME FOR THE SAME PERSON. "geto" and "Suguru Geto" do not share
    # a word SET, so the key below cannot match them, and the same live build
    # seated both. A premise names people the way a fan talks about them and the
    # source spells them out in full; both are correct and they are one person.
    #
    # Folded only when the short name fits exactly ONE longer one. "Zenin" in a
    # world holding both Naobito Zenin and Maki Zenin is ambiguous, and a wrong
    # merge deletes a character - so ambiguity is left alone and two records
    # survive, which is the recoverable direction.
    # Only in a canon world, and over every record in it regardless of origin
    # stamp - the short spelling usually arrives as the player's import while
    # the long one comes from the source, so restricting this to records
    # already stamped canon missed exactly the pair it exists to catch. In an
    # ORIGINAL world "Bob" and "Bob Smith" may well be two invented people, so
    # this never runs there.
    _canon_names = [
        (i, set(re.sub(r"[^a-z0-9]+", " ", str(n.get("name") or "").lower()).split()))
        for i, n in enumerate(raw.get("npcs") or [])
        if raw.get("mode") == "canon" and str(n.get("name") or "").strip()]
    _absorbed: set[int] = set()
    for idx, words in _canon_names:
        if not words:
            continue
        bigger = [j for j, other in _canon_names
                  if j != idx and words < other and other]
        if len(bigger) == 1:
            _absorbed.add(idx)

    for i, npc in enumerate(raw.get("npcs") or []):
        if i in _absorbed:
            continue
        # THE SAME WORDS IN A DIFFERENT ORDER ARE THE SAME PERSON. Published
        # material is inconsistent about this and so is a premise: a live build
        # seated "Satoru Gojo" (the persona pass, from the source's own
        # spelling) AND "gojo satoru" (the player's typing, carried in as an
        # import) as two separate characters in one world. A plain fold keyed
        # them differently and the one-person-one-record law never fired. The
        # key is the SET of words, sorted - which `canon_lore.card` already had
        # to learn for exactly the same reason.
        name_key = " ".join(sorted(re.sub(
            r"[^a-z0-9]+", " ", str(npc.get("name") or "").lower()).split()))
        if name_key:
            prior = seen_person.get(name_key) or []
            if any(_same_person(npc, seen) for seen in prior):
                continue
            prior.append(npc)
            seen_person[name_key] = prior
        nid = slug(npc.get("id") or npc.get("name") or f"npc_{i}", f"npc_{i}")
        while nid in seen_npc:
            nid = f"{nid}_{i}"
        seen_npc.add(nid)
        anchors = npc.get("anchors") or {}
        start = slug(npc.get("start_location") or "")
        if start not in valid_locs:
            # The hub, not `locations[i % len]`. Round-robin took exactly the
            # characters the builder gave no home and spread them as thinly as
            # the map allowed, which is the opposite of what an unplaced person
            # should do: they belong where everyone passes through.
            start = locations[0]["id"]
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
            # A face, and only ever one the PLAYER brought. Nothing in this
            # product generates a likeness - see backend/uploads.py - so this
            # holds a /media/<hash> path from their own upload, or "".
            # Whitelisted here or it is dropped on the first save, which is
            # what happens to every field this function does not name: the
            # editor would appear to accept a portrait and the world would
            # come back without one.
            "portrait": _portrait(npc.get("portrait")),
            # F3: see the matching note on locations above - including why the
            # default flips in a canon world.
            "origin": str(npc.get("origin")
                          or ("original" if raw.get("mode") == "canon" else "canon")),
            # WHICH STORY THIS PERSON IS FROM, when it is not this world's.
            # A carried-in character - "Gojo from Jujutsu Kaisen" - is filed
            # under the HOST setting everywhere downstream unless this is
            # kept, and the cost is not cosmetic. The persona call asks for a
            # character's card under the source they are listed under, so
            # Gojo was asked "Demon Slayer: who is Satoru Gojo?" and answered
            # plausibly as a Demon Slayer sorcerer. He then stood in a world
            # whose entire history he has never heard of and agreed with a
            # local about it. Whitelisted here or it is dropped on the first
            # save - which is exactly what was happening.
            "from_source": str(npc.get("from_source") or ""),
            "persona_evidence": [
                {k: str(p.get(k) or "")[:400] for k in
                 ("text", "source_id", "quote", "url") if p.get(k)}
                for p in (npc.get("persona_evidence") or [])[:20]
                if isinstance(p, dict) and p.get("source_id") and p.get("quote")
            ],
            # Somebody a stranger could not simply walk up to at the start:
            # the hidden antagonist, the sealed thing, the one who rules from
            # a distance. Whitelisted here or it would be dropped, and the
            # opening-cast fill would put them back in the square.
            "hidden_start": bool(npc.get("hidden_start")),
            # Somebody the player arrived WITH - "me and my girlfriend Charlie".
            # Whitelisted or it is dropped here, which is what happened: the
            # premise parser worked out she was a companion, worldforge set the
            # flag, normalise threw it away, and nothing downstream could ever
            # act on it. She was left behind in chapter one.
            "companion": bool(npc.get("companion")),
            # The five original fields, plus the persona card when the world
            # carries one. memory.anchor_block switches to the full identity
            # block the moment any card field is present, so a world authored
            # before the card existed produces byte-identical prompts and a
            # world that has one gets the anti-drift treatment for free.
            "anchors": {
                "name": str(npc.get("name") or nid),
                "role": str(npc.get("role") or anchors.get("role") or "villager"),
                "voice": str(anchors.get("voice") or "Plain, direct, unhurried."),
                "constraints": _as_list(anchors.get("constraints")) or ["Is an ordinary mortal person."],
                "goals": _as_list(anchors.get("goals")) or ["Survive what is coming."],
                "taboos": _as_list(anchors.get("taboos")) or ["Never breaks their own word."],
                **_card(anchors),
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
            # Source-backed canon events keep the exact evidence that admitted
            # them. Original/hand-authored events simply carry empty fields.
            "source_id": str(ev.get("source_id") or "")[:40],
            "source_url": str(ev.get("source_url") or "")[:300],
            "source_quote": str(ev.get("source_quote") or "")[:400],
        })
    # Fated turns must be strictly increasing or two fire at once.
    for i in range(1, len(fated)):
        if fated[i]["turn"] <= fated[i - 1]["turn"]:
            fated[i]["turn"] = fated[i - 1]["turn"] + 1
    # A SHORT SPINE IS ALLOWED WHEN IT IS REAL. The minimum exists to stop a
    # thin build reaching the player, and a canon world that could only cite
    # three events honestly is not a thin build - it is an accurate one. But
    # "the entry contract exists" was standing in for "the spine is sourced",
    # so a world with an empty future and ZERO fated events sailed through
    # this check: no chapters, nothing that ever happens on its own. The test
    # is whether the events themselves carry a source.
    entry = raw.get("canon_entry") or {}
    sourced = any(f.get("source_id") or f.get("source_url") for f in fated)
    source_spine = raw.get("mode") == "canon" and fated and (
        sourced or entry.get("asked") == "after")
    if strict and len(fated) < MIN_FATED and not source_spine:
        raise WorldError(f"world needs at least {MIN_FATED} fated events (got {len(fated)})")

    start_location = slug(raw.get("start_location") or "")
    if start_location not in valid_locs:
        start_location = locations[0]["id"]

    # The opening scene must have people in it.
    #
    # A live Demon Slayer build put its seven characters on seven different
    # famous landmarks - one each - so the player opened the game alone and
    # every question about anybody was refused for two turns running: "he is
    # nowhere in sight". A world where nobody is ever in the room is not a
    # world, it is an empty museum, and it is the single biggest difference
    # between this and a chat model running the same setting, where the whole
    # cast is in the lobby talking over each other.
    #
    # Scattering is also partly this function's own doing: an NPC whose
    # start_location the builder got wrong used to be round-robined across the
    # map by `locations[i % len]`, which spread exactly the characters that had
    # no home of their own as widely as possible.
    # Once, at creation. Re-running it on every normalise made the world grow
    # wrong: a character built FOR the Butterfly Mansion, on the turn the
    # player walked into it, was immediately dragged back to the town square
    # to pad an opening scene that had been populated hours ago.
    # Somebody who cannot be walked up to does not START where the player is
    # standing. Skipping them in the fill below was not enough: a live build
    # put Muzan Kibutsuji in the Market Square because the BUILDER placed him
    # there, and nothing moved him. Concealment is the character; a world that
    # opens with him at arm's length has given away its own ending in the first
    # sentence.
    elsewhere = [l["id"] for l in locations if l["id"] != start_location]
    if elsewhere:
        for i, npc in enumerate(npcs):
            if npc.get("hidden_start") and npc["start_location"] == start_location:
                moved = elsewhere[i % len(elsewhere)]
                npc["start_location"] = moved
                for phase in PHASES:
                    if npc["schedule"].get(phase) == start_location:
                        npc["schedule"][phase] = moved

    here = [n for n in npcs if n["start_location"] == start_location]
    if len(here) < OPENING_CAST and not raw.get("opening_cast_set"):
        # Pull from whoever is furthest down the list - the builder front-loads
        # the people who matter, so the tail is the safest thing to move, and
        # anyone with a schedule that already names the opening place stays.
        for npc in reversed(npcs):
            if len(here) >= OPENING_CAST:
                break
            if npc["start_location"] == start_location:
                continue
            # Never drag somebody into the opening square who would not be
            # standing in one. A live build opened with Muzan Kibutsuji in
            # public view on turn one - a character whose whole existence is
            # concealment - purely because this loop fills from the end of the
            # list and he was last. Populating a first scene must not cost the
            # setting its most important secret.
            if npc.get("hidden_start"):
                continue
            npc["start_location"] = start_location
            for phase in PHASES:
                if npc["schedule"].get(phase) not in valid_locs:
                    npc["schedule"][phase] = start_location
            npc["schedule"][PHASES[0]] = start_location
            here.append(npc)

    # Evidence is untrusted imported data too. Re-compact it at every save/load
    # boundary so article bodies or arbitrary nested fields cannot hitch a ride.
    from . import canon_evidence
    canon_entry = canon_evidence.persist(raw.get("canon_entry") or {})

    return {
        "id": world_id,
        "name": name,
        "tagline": str(raw.get("tagline") or ""),
        "premise": str(raw.get("premise") or ""),
        "fate_note": str(raw.get("fate_note") or "Fate is fixed. Your path through it is not."),
        # What a carried-in character costs themselves by being here: what this
        # world reads them as, what gives them away, and what it does about it.
        # Empty for every world that is not a crossover.
        "friction": str(raw.get("friction") or ""),
        # Set once the opening scene has been given its cast, so a world that
        # grows later is never re-arranged around its own first turn.
        "opening_cast_set": True,
        # The source's own chapters, in its own order. Empty for an original
        # world: there is no canon running order to follow.
        "chapters": [c for c in (raw.get("chapters") or []) if isinstance(c, dict)][:12],
        "start_location": start_location,
        "default_protagonist": str(raw.get("default_protagonist") or "a traveller nobody here has heard of"),
        # The player's own picture, answered in Session Zero and carried on the
        # world so it survives a save, a reload and a second playthrough of the
        # same world. engine.create_playthrough turns it into their card.
        "default_portrait": _portrait(raw.get("default_portrait")),
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
        # How this world answers death, if it answers at all. death.py only
        # offers the `revive` resolution when a world declares one, so without
        # this the option could never appear on any generated world.
        # The seed this world was built from, carried through normalisation so
        # a Daily can be verified after the fact rather than taken on trust.
        "seed": int(raw.get("seed") or 0),
        "revival_rule": _revival(raw),
        # Groups already operating when the player arrives, so the Power panel
        # and the infiltration verb have something to point at on turn one.
        "orgs": _orgs(raw, {n["id"] for n in npcs}, {l["id"] for l in locations}),
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
        # WHERE IN THE SOURCE THIS STARTS - "start", an arc id, or "after".
        # Whitelisted here or it is dropped on the first save and the narrator
        # goes back to being told only who exists, never who has met whom:
        # a build that opens at the very beginning greets the player by name
        # in a scene where nobody has met anybody yet. See narrator.py.
        "entry_point": str(raw.get("entry_point") or ""),
        # Revision-linked, quote-backed state for the exact requested moment.
        # The narrator receives only present/past facts; future stays engine-only.
        "canon_entry": canon_entry,
    }


def load(raw: dict, *, strict: bool = True) -> World:
    return World(normalise(raw, strict=strict))
