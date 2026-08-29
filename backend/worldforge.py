"""World Forge - players build worlds; nobody is an author.

Two ways in:
  * hand-forged - the UI posts a world JSON, this validates and stores it
  * AI bootstrap - a player names any setting and the system builds a playable
    world from it, which they then own and can edit freely

COPYRIGHT BOUNDARY (load-bearing, not decoration): a world bootstrapped from an
existing fictional IP is an inspired-by personal world. It is marked
personal_only, forced to private visibility, can never be published or shared
to a public listing, and is never presented as official or licensed. Worlds
that can be shared publicly must be original.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from . import arcs, db, llm, research, worldkit
from . import worlds as world_registry

# Settings that read as an existing IP get the personal-only treatment. This is
# a deliberately broad net: the cost of a false positive is a private world.
IP_MARKERS = (
    "demon slayer", "naruto", "one piece", "bleach", "jujutsu", "attack on titan",
    "middle-earth", "middle earth", "lord of the rings", "hobbit", "tolkien",
    "harry potter", "hogwarts", "star wars", "star trek", "marvel", "dc comics",
    "witcher", "warhammer", "game of thrones", "westeros", "dune", "arrakis",
    "pokemon", "pokémon", "zelda", "elden ring", "dark souls", "final fantasy",
    "avatar", "cyberpunk 2077", "night city", "fallout", "mass effect", "halo",
    "stranger things", "the expanse", "discworld", "narnia", "percy jackson",
    "my hero academia", "death note", "evangelion", "ghibli", "sailor moon",
    "dragon ball", "hunter x hunter", "chainsaw man", "spy x family", "berserk",
    "resident evil", "silent hill", "bioshock", "skyrim", "elder scrolls",
    "minecraft", "roblox", "fortnite", "genshin", "honkai", "arcane", "league of legends",
)

STRUCTURE_SYSTEM = """You are a world architect for a turn-based text RPG engine. You output data, never prose commentary.

You are given a setting. Produce the PLACES and the CHARACTERS of a small, dense, playable corner of it - one town, one ship, one district, one siege. Not the whole franchise: one place where a story can happen in fifty turns.

Characters must be people, not archetypes. Each one needs a VOICE another writer could imitate, hard CONSTRAINTS that limit what they can do, WANTS that conflict with someone else's, and TABOOS they will not cross. At least two pairs of characters must want incompatible things.

Return ONLY JSON:
{
  "name": "the place, not the franchise",
  "tagline": "one line, under 60 characters",
  "premise": "120-180 words, second person, addressed to the player arriving",
  "arrival": "one sentence: how the player got here",
  "default_protagonist": "who the player is by default",
  "locations": [{"id":"snake_case","name":"The Name","kind":"tavern|civic|work|sacred|open|threshold","desc":"25-40 words, sensory","connects":["other_id"]}],
  "npcs": [{"id":"snake_case","name":"Full Name","role":"what they do here","start_location":"place_id",
            "anchors":{"voice":"how they speak, 15-30 words, specific and imitable",
                       "constraints":["hard limit","hard limit"],
                       "goals":["what they want","what they want"],
                       "taboos":["what they never do","what they never do"]},
            "schedule":{"morning":"place_id","midday":"place_id","evening":"place_id","night":"place_id"},
            "seed_memories":["something they already know, first person","another"],
            "initial_relationship":{"affinity":-30..30,"trust":-30..30,"fear":0..30,"obligation":0..30}}]
}

Exactly 9-11 locations and 10-12 characters. Every connects[] id must exist. The map must be connected."""

LAW_SYSTEM = """You are the World Master's rule-writer for a turn-based text RPG.

Given a world's places and people, write the LAWS and the FATE.

LAWS are hard constraints the engine enforces before any prose is written. Good laws are checkable and specific ("the gate is barred between dusk and dawn"), never vague ("be respectful"). Where a law can be caught by matching words in the player's typed action, give a `check`.

FATE is the spine: seven events that WILL happen on their turn no matter what any character does. They escalate. The player can never prevent one - only be somewhere, with someone, when it lands. At least one fated event should kill a named character.

Return ONLY JSON:
{
  "rules": [{"id":"R1_short_slug","text":"the law, one sentence, absolute",
             "check":{"pattern":"regex of words in a player action that violate it",
                      "when":{"turn_lt":15},
                      "reason":"one sentence told to the player when it stops them"}}],
  "fated_events": [{"id":"F1_short_slug","turn":4,"title":"Short Title",
                    "desc":"one or two sentences, present tense","location":"place_id",
                    "kills":"npc_id or null"}],
  "fate_note": "one line about what fate means here"
}

Exactly 9-11 rules and exactly 7 fated events, on turns roughly 4, 9, 15, 22, 30, 38, 45. `check` is optional per rule; include it on at least 5. `when` is optional and may use turn_lt, turn_gte, phase_in, location."""


def looks_like_ip(setting: str) -> bool:
    text = (setting or "").lower()
    return any(marker in text for marker in IP_MARKERS)


# ---------------------------------------------------------------------------
# Offline stub - a real, playable world with no key and no spend
# ---------------------------------------------------------------------------

NEUTRAL_NAMES = [
    "The Hollow", "Greyfall", "Ninefold", "The Long Watch", "Marrowmere",
    "Coldharbour", "The Undertow", "Ashmouth", "Wrenlow", "The Quiet Mile",
]


def neutral_name(seed: str) -> str:
    """An original place name for an inspired-by world, so the trademark never
    ends up on the artefact. Stable across processes, unlike hash()."""
    digest = hashlib.sha1(seed.lower().encode("utf-8")).digest()
    return NEUTRAL_NAMES[digest[0] % len(NEUTRAL_NAMES)]


def _strip_ip_name(name: str, setting: str) -> str:
    """A generated name that is just the franchise gets replaced. Applies to the
    model path as well as the stub - instructions get ignored sometimes."""
    a = worldkit.slug(name)
    b = worldkit.slug(setting)
    if not a or a == b or a in b or b in a or looks_like_ip(name):
        return neutral_name(setting)
    return name


def _stub_structure(setting: str, personal: bool = False) -> dict:
    name = (neutral_name(setting) if personal
            else (setting or "The Hollow").strip().title()[:40])
    places = [
        ("landing", "The Landing", "threshold", "Where you came in, and the only way anyone leaves."),
        ("commons", "The Commons", "open", "A trampled square where the whole place can see the whole place."),
        ("house_of_lamps", "The House of Lamps", "tavern", "Warm, loud, and the only room where people say true things."),
        ("the_works", "The Works", "work", "Machinery that has not stopped in living memory, and is stopping now."),
        ("high_office", "The High Office", "civic", "One desk, one ledger, one person who decides."),
        ("quiet_house", "The Quiet House", "sacred", "Cold stone. People come here to be told it means something."),
        ("the_deep", "The Deep", "work", "Down. Warm where it should be cold."),
        ("physic_room", "The Physic Room", "work", "Jars, labels, and not enough of anything."),
        ("the_road", "The Road", "threshold", "Out. Whoever is standing here at the end is who lived."),
    ]
    locations = [{"id": pid, "name": nm, "kind": kind, "desc": desc,
                  "connects": ["commons"] if pid != "commons" else [p for p, *_ in places if p != "commons"]}
                 for pid, nm, kind, desc in places]
    roles = [
        ("warden", "The Warden", "keeper of the order here", "Short declarative sentences. Never raises her voice."),
        ("smith", "The Smith", "keeps the machines running", "Blunt, profane, talks while working."),
        ("priest", "The Keeper", "keeper of the Quiet House", "Warm, unhurried, speaks in the plural."),
        ("broker", "The Broker", "buys what people must sell", "Over-polite. Itemises everything."),
        ("host", "The Host", "keeps the House of Lamps", "Fast, wry, trades information like coin."),
        ("digger", "The Digger", "works the Deep", "Rambles, then lands one devastating sentence."),
        ("physician", "The Physician", "the only doctor", "Clipped, exhausted, precise about doses."),
        ("holdout", "The Holdout", "will not leave", "Slow, stubborn, repeats their own last words."),
        ("sword", "The Sword", "sells protection", "Easy, transactional, quotes prices for everything."),
        ("child", "The Runner", "a child who goes everywhere", "Breathless run-ons, asks three questions at once."),
    ]
    npcs = []
    for i, (nid, nm, role, voice) in enumerate(roles):
        home = places[i % len(places)][0]
        npcs.append({
            "id": nid, "name": nm, "role": role, "start_location": home,
            "anchors": {"voice": voice,
                        "constraints": [f"Will not leave {name} while anyone is still in it.",
                                        "Is an ordinary mortal person."],
                        "goals": [f"Get through what is coming to {name}.",
                                  "Be believed once, before the end."],
                        "taboos": ["Never admits fear aloud.", "Never begs."]},
            "schedule": {"morning": home, "midday": "commons", "evening": "house_of_lamps", "night": home},
            "seed_memories": [f"I have been in {name} longer than anyone asks about.",
                              "Something under this place is waking up."],
            "initial_relationship": {"affinity": (i * 5) % 20 - 5, "trust": (i * 3) % 15 - 5,
                                     "fear": 0, "obligation": 0},
        })
    return {
        "name": name,
        "tagline": "Something here is about to end.",
        "premise": (f"You came into {name} yesterday with a road behind you and no claim here. "
                    "Something under this place is waking up, the people who know are not saying, "
                    "and the ones who do not know are making plans. What happens to "
                    f"{name} is already decided. Who is standing beside you when it does is not."),
        "arrival": f"You arrive in {name} off the road, with nothing anyone here wants.",
        "default_protagonist": "a traveller nobody here has heard of",
        "locations": locations, "npcs": npcs,
    }


def _stub_laws(structure: dict) -> dict:
    npc_ids = [n["id"] for n in structure["npcs"]]
    loc_ids = [l["id"] for l in structure["locations"]]
    return {
        "fate_note": "Fate is fixed. Your path through it is not.",
        "rules": [
            {"id": "R1_no_magic", "text": "There is no magic here. No spell, vision, or supernatural power is available to anyone.",
             "check": {"pattern": r"\b(cast|spell|magic|summon|teleport|enchant|conjure)\w*",
                       "reason": "There is no magic here. Whatever you reach for, your hands come back empty."}},
            {"id": "R2_road_closed", "text": "The road out is impassable until the ground opens it on turn 15.",
             "check": {"pattern": r"\b(leave|escape|flee)\b.*\b(town|place|valley|city)\b", "when": {"turn_lt": 15},
                       "reason": "The road is closed. Nobody is walking out of here yet."}},
            {"id": "R3_office_authority", "text": "Only the Warden may give an order that the whole place must obey.",
             "check": {"pattern": r"\b(order|command)\b.*\b(everyone|the town|them all)\b",
                       "reason": "Nobody here takes an order from you. Not yet."}},
            {"id": "R4_deep_after_dark", "text": "Nobody goes down into the Deep after dark.",
             "check": {"pattern": r"\b(go|climb|descend|enter)\b.*\bdeep\b", "when": {"phase_in": ["night"]},
                       "reason": "Not after dark. Not down there."}},
            {"id": "R5_one_place", "text": "No character is in two places at once; you must share someone's location to act on them."},
            {"id": "R6_fate_immutable", "text": "Fated events cannot be prevented, delayed, or undone. They can only be witnessed and answered."},
            {"id": "R7_mortal_stakes", "text": "People are ordinary and mortal, and nobody who has died acts again."},
            {"id": "R8_no_invented_property", "text": "You own only what you arrived with or what has been given, taken, or made on-screen.",
             "check": {"pattern": r"\bI (pull out|produce|reveal) (a|my) (gun|sword|fortune|army|badge)\b",
                       "reason": "You do not have that. You arrived with what you arrived with."}},
            {"id": "R9_no_crowd_control", "text": "No one character can move a crowd by speech alone; people here decide one at a time."},
        ],
        "fated_events": [
            {"id": "F1_water_turns", "turn": 4, "title": "The water turns",
             "desc": "Every well and tap runs black and tastes of iron.", "location": loc_ids[1]},
            {"id": "F2_the_sound", "turn": 9, "title": "The sound underneath",
             "desc": "A low note comes up through the floor and does not stop.", "location": loc_ids[1]},
            {"id": "F3_ground_opens", "turn": 15, "title": "The ground opens",
             "desc": "The road out cracks open, and for the first time leaving is possible.", "location": loc_ids[-1]},
            {"id": "F4_the_works_stop", "turn": 22, "title": "The Works stop",
             "desc": "The machinery halts mid-stroke. The silence is worse than the noise was.", "location": loc_ids[3]},
            {"id": "F5_the_warden_falls", "turn": 30, "title": "The Warden falls",
             "desc": "The Warden dies at their post, having done the whole job alone.",
             "location": loc_ids[4], "kills": npc_ids[0]},
            {"id": "F6_the_end", "turn": 38, "title": "It comes up",
             "desc": "What was under the ground is no longer under the ground.", "location": loc_ids[1]},
            {"id": "F7_after", "turn": 45, "title": "Dawn on the road",
             "desc": "Whoever walked out has walked out. Behind them, nothing.", "location": loc_ids[-1]},
        ],
    }


# ---------------------------------------------------------------------------

def _top_up(raw: dict) -> dict:
    """Guarantee the engine's minimums even if a model came back thin, so a
    bootstrap never dead-ends the player."""
    fallback = _stub_laws(raw)
    have_rules = {r.get("id") for r in raw.get("rules") or []}
    rules = list(raw.get("rules") or [])
    for r in fallback["rules"]:
        if len(rules) >= worldkit.MIN_RULES:
            break
        if r["id"] not in have_rules:
            rules.append(r)
    raw["rules"] = rules

    fated = sorted(raw.get("fated_events") or [], key=lambda f: f.get("turn", 0))
    if len(fated) < worldkit.MIN_FATED:
        used = {f.get("id") for f in fated}
        for f in fallback["fated_events"]:
            if len(fated) >= worldkit.MIN_FATED:
                break
            if f["id"] not in used:
                fated.append(f)
        fated.sort(key=lambda f: f.get("turn", 0))
    raw["fated_events"] = fated
    return raw


def _empty_dossier(setting):
    """Original mode looks nothing up, so it hands back the same empty shape
    the research layer would - one code path, not two."""
    return {"setting": setting, "canonical_name": "", "found": False,
            "summary": "", "wiki": "", "characters": [], "places": [],
            "factions": [], "sources": [], "note": "original world",
            "cached": False, "depth": "none", "fetched_at": ""}


def bootstrap(setting: str, *, user_id: str, tone: str = "",
              mode: str = "auto") -> dict:
    """Build a playable world from a named setting. Returns a world dict; the
    caller decides whether to save it."""
    setting = (setting or "").strip()
    if not setting:
        raise ValueError("name a setting")

    # TWO MODES, and the player picks - the system does not guess.
    #
    #   original : build it from imagination. Nothing is looked up.
    #   canon    : go and read about it, then continue it with the real names,
    #              places and factions.
    #   auto     : look first. If it is a real setting, continue it; if nothing
    #              is found, it is original by definition.
    #
    # This replaces inferring intent from a keyword list, which got it wrong in
    # both directions: it flagged original worlds that happened to share a word
    # with something famous, and missed anything not on the list. The player
    # knows which they meant.
    mode = mode if mode in ("original", "canon", "auto") else "auto"

    # Go and LOOK before building. For a real setting this is the difference
    # between a world that uses the ACTUAL names and one that invents
    # plausible-sounding ones - the model cannot tell you which it is doing,
    # so we hand it the answer instead of hoping. Zero model calls, and it
    # degrades to the old ungrounded behaviour when there is no network.
    found = (_empty_dossier(setting) if mode == "original"
             else research.dossier(setting))
    grounding = research.grounding_brief(found)
    # The world is CANON if we actually found something to continue. An
    # original world is not a failed canon world, it is the other mode.
    canon = bool(found.get("found"))
    # PRIVATE BY DEFAULT is not a judgement about the player - they own this
    # world, play it, and export it. It only means a world that continues
    # someone else's setting is not PUBLICLY LISTED on a shared service, which
    # protects whoever runs the deployment as much as the player.
    #
    # The keyword list survives for exactly one job: research is what normally
    # tells us a world is canon, and research cannot run offline. Without the
    # fallback, the same world would be private when built online and public
    # when built offline - and the offline answer is the unsafe one.
    personal = canon or looks_like_ip(setting)

    brief = f"SETTING: {setting}"
    if found.get("canonical_name") and found["canonical_name"].lower() != setting.lower():
        brief += f"\nCANONICAL TITLE: {found['canonical_name']}"
    if tone:
        brief += f"\nTONE THE PLAYER ASKED FOR: {tone}"
    if personal:
        brief += (
            "\n\nThis continues an existing setting. This world is the PLAYER'S: it is "
            "private to them, never listed publicly, and not affiliated with or endorsed "
            "by any rights holder.\n"
            "Use the setting's real characters, places and factions - that is the point. "
            "Build them an ORIGINAL situation inside that world: a corner the source never "
            "covers, with its own laws and its own fate.\n"
            "Do not copy sentences from the source material, and do not simply retell the "
            "story that already exists - the player wants to LIVE somewhere, not re-read it."
        )

    if grounding:
        brief += "\n\n" + grounding

    structure = llm.complete(
        "narrator", STRUCTURE_SYSTEM, brief + "\n\nBuild the places and the people. JSON only.",
        user_id=user_id, json_mode=True, max_tokens=3600, temperature=0.9,
        stub=lambda: _stub_structure(setting, personal))

    laws_brief = (
        f"WORLD: {structure.get('name')}\n"
        f"PLACES: {', '.join(l.get('id', '') for l in structure.get('locations', []))}\n"
        f"PEOPLE: {', '.join(n.get('id', '') for n in structure.get('npcs', []))}\n\n"
        "Write the laws and the fate. JSON only."
    )
    laws = llm.complete(
        "narrator", LAW_SYSTEM, laws_brief, user_id=user_id, json_mode=True,
        max_tokens=2400, temperature=0.7, stub=lambda: _stub_laws(structure))

    raw = {**structure, **{k: v for k, v in laws.items() if v}}
    raw["origin"] = "bootstrap"
    raw["source_prompt"] = setting
    # Attribution travels with the world: CC BY-SA asks for it, and a
    # player deserves to know which wiki their world was grounded on.
    raw["sources"] = research.attribution(found)
    raw["researched"] = bool(found.get("found"))
    raw["personal_only"] = personal
    raw["inspired_by"] = found.get("canonical_name") or (setting if personal else "")
    raw["mode"] = "canon" if canon else "original"
    if personal:
        raw["name"] = _strip_ip_name(str(raw.get("name") or ""), setting)
    raw.setdefault("opening", raw.get("premise", ""))
    raw = _top_up(raw)
    return worldkit.normalise(raw, strict=True)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def derive_arcs(data: dict) -> list:
    """A timeline, free, from the fate the world already has.

    Every world gets an ordered arc list without a second model call: the
    fated events ARE the spine of the story, so the stretch leading up to each
    one is an arc. Deriving them rather than generating them means entry-point
    selection works on EVERY world, including hand-forged ones, and the arcs
    can never contradict the fate they came from.

    Each arc seeds the state as it stands when it begins - who has already
    died, where the last event landed - so starting at arc 4 is a real
    starting position rather than a label."""
    fate = data.get("fated_events") or []
    arcs, dead = [], []
    for i, f in enumerate(fate):
        arcs.append({
            "id": f"{data.get('id', 'w')}:arc{i}",
            "name": f.get("title") or f"Arc {i + 1}",
            "summary": f.get("desc", "")[:280],
            "seeds": {
                # Everything fated BEFORE this arc has already happened.
                "dead": list(dead),
                "location": f.get("location", "") or data.get("start_location", ""),
                "flags": {f"fate:{p['id']}": True for p in fate[:i] if p.get("id")},
            },
        })
        if f.get("kills"):
            dead.append(f["kills"])
    return arcs


def save(user_id, raw: dict, *, world_id=None, visibility="private") -> dict:
    data = worldkit.normalise(raw, strict=True)
    personal = bool(data.get("personal_only"))
    if personal:
        visibility = "private"          # the boundary, enforced in code
    if visibility not in ("private", "unlisted"):
        visibility = "private"

    if world_id:
        row = db.row("SELECT * FROM worlds WHERE id=? AND owner_user_id=?", (world_id, user_id))
        if not row:
            raise KeyError("no such world")
        data["id"] = world_id
        db.run(
            "UPDATE worlds SET name=?,tagline=?,origin=?,source_prompt=?,personal_only=?,"
            "visibility=?,json=?,updated_at=? WHERE id=?",
            (data["name"], data["tagline"], data["origin"], data["source_prompt"],
             int(personal), visibility, json.dumps(data), db.now(), world_id))
        world_registry.forget(world_id)
        arcs.set_timeline(world_id, derive_arcs(data))
        return get(world_id, user_id)

    world_id = f"w{uuid.uuid4().hex[:10]}"
    data["id"] = world_id
    db.run(
        "INSERT INTO worlds (id,owner_user_id,name,tagline,origin,source_prompt,personal_only,"
        "visibility,json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (world_id, user_id, data["name"], data["tagline"], data["origin"], data["source_prompt"],
         int(personal), visibility, json.dumps(data), db.now(), db.now()))
    arcs.set_timeline(world_id, derive_arcs(data))
    return get(world_id, user_id)


def get(world_id, user_id=None) -> dict:
    row = db.row("SELECT * FROM worlds WHERE id=?", (world_id,))
    if not row:
        raise KeyError("no such world")
    if user_id and row["owner_user_id"] != user_id and row["visibility"] == "private":
        raise PermissionError("this world is private")
    data = json.loads(row["json"])
    return {**{k: row[k] for k in ("id", "name", "tagline", "origin", "source_prompt",
                                   "visibility", "created_at", "updated_at")},
            "personal_only": bool(row["personal_only"]),
            "owned": user_id == row["owner_user_id"],
            "world": data,
            "summary": worldkit.World(data).summary()}


def listing(user_id) -> list[dict]:
    rows = db.rows(
        "SELECT id,name,tagline,origin,personal_only,visibility,updated_at FROM worlds"
        " WHERE owner_user_id=? ORDER BY updated_at DESC", (user_id,))
    return [{**r, "personal_only": bool(r["personal_only"])} for r in rows]


def delete(world_id, user_id) -> bool:
    row = db.row("SELECT * FROM worlds WHERE id=? AND owner_user_id=?", (world_id, user_id))
    if not row:
        raise KeyError("no such world")
    db.run("DELETE FROM worlds WHERE id=?", (world_id,))
    world_registry.forget(world_id)
    return True


def blank(name="A new world") -> dict:
    """The Forge's starting point: a valid skeleton the UI fills in."""
    return worldkit.normalise({**_stub_structure(name), **_stub_laws(_stub_structure(name)),
                               "origin": "forged"}, strict=True)
