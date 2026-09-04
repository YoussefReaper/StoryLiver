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

from . import arcs, canon_seed, config, db, llm, research, sessionzero, worldkit
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

# ---------------------------------------------------------------------------
# How big a world is
# ---------------------------------------------------------------------------
# One model call can only carry so much before it starts truncating mid-JSON,
# which is why the original prompt asked for "exactly 9-11 locations and 10-12
# characters" and why every world came out the same small size no matter how
# large the setting actually was.
#
# Anything past a town is therefore built in SEVERAL passes and stitched:
# a first pass lays out the districts, then one pass per district fills in its
# places and people. Each pass is small enough to come back whole, and the
# world as a whole can be as large as the player asked for.
#
# Cost scales honestly with size, and the player is told the number of passes
# before they commit - a "world" is roughly seven model calls, not one.
# The blurb describes the SHAPE. It deliberately carries no call count: the
# count is arithmetic over `districts` and belongs in one place. It was written
# into these strings by hand, into the client by hand again, and computed a
# third time in scale_plan - and all four sizes understated the real cost by
# exactly one call, which is the sort of number a player is entitled to have
# right before they spend on it.
SCALES = {
    "town":   {"districts": 1, "locs": (9, 11),  "npcs": (10, 12),
               "label": "A town", "blurb": "One dense, playable place."},
    "city":   {"districts": 3, "locs": (7, 9),   "npcs": (7, 9),
               "label": "A city", "blurb": "Three districts, each with its own people."},
    "region": {"districts": 5, "locs": (6, 8),   "npcs": (6, 8),
               "label": "A region", "blurb": "Five settlements, connected by road."},
    "world":  {"districts": 8, "locs": (6, 8),   "npcs": (6, 8),
               "label": "A whole world", "blurb": "Eight regions, a continent's worth."},
}


def scale_plan(scale: str) -> dict:
    """What a given scale will actually produce, before anything is built.

    Surfaced to the player so "a whole world" is a known quantity - roughly
    how many places, how many characters, and how many model calls it costs -
    rather than a promise the system might quietly not keep."""
    spec = SCALES.get(scale) or SCALES["town"]
    d = spec["districts"]
    return {
        "scale": scale if scale in SCALES else "town",
        "label": spec["label"], "blurb": spec["blurb"],
        "districts": d,
        "locations": [d * spec["locs"][0], d * spec["locs"][1]],
        "characters": [d * spec["npcs"][0], d * spec["npcs"][1]],
        # one districts pass (when d > 1) + one per district + one laws pass
        "model_calls": (1 if d > 1 else 0) + d + 1,
    }


DISTRICT_SYSTEM = """You are a world architect for a turn-based text RPG engine. You output data, never prose commentary.

You are given a setting and a target size. Lay out the world's DISTRICTS - the distinct places a story could happen in. A district is a town, a quarter, a stronghold, a station: somewhere with its own character, its own problems, and its own people.

Districts must differ from each other in kind, not just in name. If two could swap names without anyone noticing, merge them and invent a different one.

Return ONLY JSON:
{
  "name": "the world or the region, not the franchise",
  "tagline": "one line, under 60 characters",
  "premise": "120-180 words, second person, addressed to the player arriving",
  "arrival": "one sentence: how the player got here",
  "default_protagonist": "who the player is by default",
  "districts": [{"id":"snake_case","name":"The Name","kind":"town|quarter|stronghold|wilds|sacred|industry",
                 "premise":"25-40 words: what this place is and what is wrong here",
                 "connects":["other_district_id"]}]
}

Every connects[] id must exist. The map must be connected - no district cut off from the rest."""


DISTRICT_FILL_SYSTEM = """You are a world architect for a turn-based text RPG engine. You output data, never prose commentary.

You are given ONE district of a larger world, and the other districts around it. Populate THIS district only.

Characters must be people, not archetypes. Each one needs a VOICE another writer could imitate, hard CONSTRAINTS that limit what they can do, WANTS that conflict with someone else's, and TABOOS they will not cross.

Give every character the FULL card. MANNERISMS are physical and repeatable. SECRETS are things they act on and never say aloud. FAMOUS_LINES are keyed to a MOMENT and withheld until it arrives, so key them to moments this world will actually reach. POWER_PROFILE states the ceiling as well as the ability: "can read a room, cannot read a written word" is useful; "very skilled" is not.

FACTIONS are the institutions with a grip on this place. `law` is 0 for a social group and 3-5 for anyone who can detain, fine or execute - at least one faction must have law 3 or more, with named OFFICERS whose schedules put them somewhere specific, because that is who a player meets after breaking a rule. Every member and officer id must be a character you defined.

NPC_EDGES are how the characters feel about EACH OTHER, not about the player. Give at least six. Values are -100..100 for affinity and trust, 0..100 for fear and obligation. Two people who cannot stand each other, in the same room, is where a scene comes from. At least one pair here must want incompatible things.

LOCATIONS are places a SCENE happens, not a floor plan. A location earns its own id when something could happen there that could not happen in the room next to it - a different set of people, a different rule, a different reason to be there. A sub-room of a larger place ("the training ground" inside "the manor", "the back office" behind "the tavern") is DETAIL folded into the parent's `desc`, not its own id, unless it is genuinely a different scene (its own people, its own danger, its own reason to go there alone). A district this size is 6-8 real places, not a dozen rooms of the same building.

Return ONLY JSON:
{
  "locations": [{"id":"snake_case","name":"The Name","kind":"tavern|civic|work|sacred|open|threshold","desc":"25-40 words, sensory, naming what a passerby would only notice by stepping further in - the closest thing this place has to a sub-room, folded in as texture rather than spun into its own id","connects":["other_id"]}],
  "npcs": [{"id":"snake_case","name":"Full Name","role":"what they do here","start_location":"place_id",
            "anchors":{"voice":"how they speak, 15-30 words, specific and imitable",
                       "constraints":["hard limit","hard limit"],
                       "goals":["what they want","what they want"],
                       "taboos":["what they never do","what they never do"],
                       "mannerisms":["a physical tell","another"],
                       "values":["what they will not trade away"],
                       "flaws":["what costs them"],
                       "secrets":["something they know and will not say"],
                       "catchphrases":["a line they actually use"],
                       "famous_lines":[{"beat":"threat|grief|resolve|farewell|greeting|mercy|betrayal|victory|defeat","line":"what they say when that moment comes"}],
                       "power_profile":"what they can actually do, and its ceiling"},
            "schedule":{"morning":"place_id","midday":"place_id","evening":"place_id","night":"place_id"},
            "seed_memories":["something they already know, first person","another"],
            "initial_relationship":{"affinity":-30..30,"trust":-30..30,"fear":0..30,"obligation":0..30}}],

  "factions": [{"id":"snake_case","name":"The Name","seat":"place_id",
                "law":0,
                "members":["npc_id","npc_id"],
                "officers":[{"id":"npc_id","rank":"what they are called",
                             "schedule":{"morning":"place_id","evening":"place_id"}}]}],

  "npc_edges": [["npc_id_who_feels","npc_id_they_feel_about",[affinity,trust,fear,obligation]]]
}

Every id you invent must be unique across the WHOLE world, so prefix them with the district id. Every connects[] and start_location id must be one you defined here, except a single threshold location that may connect to a neighbouring district."""


STRUCTURE_SYSTEM = """You are a world architect for a turn-based text RPG engine. You output data, never prose commentary.

You are given a setting. Produce the PLACES and the CHARACTERS of a small, dense, playable corner of it - one town, one ship, one district, one siege. Not the whole franchise: one place where a story can happen in fifty turns.

Characters must be people, not archetypes. Each one needs a VOICE another writer could imitate, hard CONSTRAINTS that limit what they can do, WANTS that conflict with someone else's, and TABOOS they will not cross. At least two pairs of characters must want incompatible things.

LOCATIONS are places a SCENE happens, not a floor plan. A location earns its own id when something could happen there that could not happen in the room next to it - a different set of people, a different rule, a different reason to be there. A sub-room of a larger place ("the training ground" inside "the manor", "the back office" behind "the tavern") is DETAIL folded into the parent's `desc`, not its own id, unless it is genuinely a different scene (its own people, its own danger, its own reason to go there alone).

Return ONLY JSON:
{
  "name": "the place, not the franchise",
  "tagline": "one line, under 60 characters",
  "premise": "120-180 words, second person, addressed to the player arriving",
  "arrival": "one sentence: how the player got here",
  "default_protagonist": "who the player is by default",
  "locations": [{"id":"snake_case","name":"The Name","kind":"tavern|civic|work|sacred|open|threshold","desc":"25-40 words, sensory, naming what a passerby would only notice by stepping further in - the closest thing this place has to a sub-room, folded in as texture rather than spun into its own id","connects":["other_id"]}],
  "npcs": [{"id":"snake_case","name":"Full Name","role":"what they do here","start_location":"place_id",
            "anchors":{"voice":"how they speak, 15-30 words, specific and imitable",
                       "constraints":["hard limit","hard limit"],
                       "goals":["what they want","what they want"],
                       "taboos":["what they never do","what they never do"],
                       "mannerisms":["a physical tell","another"],
                       "values":["what they will not trade away"],
                       "flaws":["what costs them"],
                       "secrets":["something they know and will not say"],
                       "catchphrases":["a line they actually use"],
                       "famous_lines":[{"beat":"threat|grief|resolve|farewell|greeting|mercy|betrayal|victory|defeat","line":"what they say when that moment comes"}],
                       "power_profile":"what they can actually do, and its ceiling"},
            "schedule":{"morning":"place_id","midday":"place_id","evening":"place_id","night":"place_id"},
            "seed_memories":["something they already know, first person","another"],
            "initial_relationship":{"affinity":-30..30,"trust":-30..30,"fear":0..30,"obligation":0..30}}],

  "factions": [{"id":"snake_case","name":"The Name","seat":"place_id",
                "law":0,
                "members":["npc_id","npc_id"],
                "officers":[{"id":"npc_id","rank":"what they are called",
                             "schedule":{"morning":"place_id","evening":"place_id"}}]}],

  "npc_edges": [["npc_id_who_feels","npc_id_they_feel_about",[affinity,trust,fear,obligation]]]
}

Exactly 9-11 locations and 10-12 characters. Every connects[] id must exist. The map must be connected."""

LAW_SYSTEM = """You are the World Master's rule-writer for a turn-based text RPG.

Given a world's places and people, write the LAWS and the FATE.

LAWS are hard constraints the engine enforces before any prose is written. Good laws are checkable and specific ("the gate is barred between dusk and dawn"), never vague ("be respectful"). Where a law can be caught by matching words in the player's typed action, give a `check`.

FATE is the spine: seven events that WILL happen on their turn no matter what any character does. They escalate. The player can never prevent one - only be somewhere, with someone, when it lands. At least one fated event should kill a named character.

REVIVAL_RULE is how this world answers death, if it answers at all. If nothing here brings anyone back, say so plainly in `cost`.

ORGS are the groups already operating when the player arrives - a crew, a house, a company. Two or three. They give the player something to join, rival or infiltrate on turn one instead of an empty board.

Return ONLY JSON:
{
  "rules": [{"id":"R1_short_slug","text":"the law, one sentence, absolute",
             "check":{"pattern":"regex of words in a player action that violate it",
                      "when":{"turn_lt":15},
                      "reason":"one sentence told to the player when it stops them"}}],
  "fated_events": [{"id":"F1_short_slug","turn":4,"title":"Short Title",
                    "desc":"one or two sentences, present tense","location":"place_id",
                    "kills":"npc_id or null"}],
  "fate_note": "one line about what fate means here",

  "revival_rule": {"name":"what this world calls coming back","cost":"what it takes from you"},

  "orgs": [{"id":"snake_case","name":"The Name","kind":"cell|house|company|order|crew",
            "seat":"place_id","charter":"what it exists to do, one sentence",
            "members":["npc_id"]}]
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


def _stub_structure(setting: str, personal: bool = False, seed: int = 0) -> dict:
    name = (neutral_name(setting) if personal
            else (setting or "The Hollow").strip().title()[:40])
    # Seeded worlds shuffle who stands where. Same seed, same arrangement -
    # which is what makes a Daily comparable between two players and what
    # makes a PvP rematch a genuinely different map.
    _rng = llm.rng("worldgen", setting, seed) if seed else None
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
    # One card per role, so the offline world exercises the same persona path
    # a live one does.
    cards = {
        "warden": (["Rests one hand flat on the ledger"], ["Order before mercy"],
                   ["Cannot admit an error in front of anyone"],
                   ["Knows the ledger has been altered"], ["That is not how this works."],
                   [("threat", "You will not like what I am obliged to do next.")],
                   "Can compel obedience here; cannot enforce anything past the road."),
        "smith": (["Wipes hands that are already clean"], ["Work that holds"],
                  ["Says the cruel thing first"], ["Knows the Works cannot be fixed"],
                  ["It holds or it does not."],
                  [("resolve", "Then we do it the hard way, and we do it now.")],
                  "Can make or mend almost anything; cannot work fast."),
        "priest": (["Speaks to the room, never to one person"], ["Nobody faces it alone"],
                   ["Will not choose between two people"], ["Stopped believing some time ago"],
                   ["We are all still here."],
                   [("grief", "We will say their name until saying it stops hurting.")],
                   "Can gather people; can promise nothing."),
        "broker": (["Counts on fingers while talking"], ["A debt is a relationship"],
                   ["Cannot let a debt go"], ["Holds paper on half the town"],
                   ["Let us call it a favour."],
                   [("betrayal", "You signed. I merely waited.")],
                   "Owns obligations, not force."),
        "host": (["Refills a glass to end a sentence"], ["Everyone gets one night"],
                 ["Trades secrets too cheaply"], ["Hears everything and sells most of it"],
                 ["Sit down. You look like news."],
                 [("greeting", "Sit down. You look like news.")],
                 "Knows who was where; can prove none of it."),
        "digger": (["Long pause before the true sentence"], ["The Deep is owed respect"],
                   ["Drinks before going down"], ["Has seen what is under the Deep"],
                   ["It is warm down there. It should not be."],
                   [("resolve", "I will go down. Somebody has to and it is not going to be you.")],
                   "Knows the Deep; useless above ground."),
        "physician": (["Speaks doses, not comfort"], ["Triage over feeling"],
                      ["Too tired to be kind"], ["Is out of the medicine that matters"],
                      ["Sit. Do not talk."],
                      [("mercy", "I can make it not hurt. That is all I have left.")],
                      "Can keep someone alive a while; cannot cure."),
        "holdout": (["Repeats their own last three words"], ["This is my place"],
                    ["Will not be moved by reason"], ["Knows the road is already cut"],
                    ["I was here first. Here first."],
                    [("farewell", "Go on then. Go on.")],
                    "Immovable; that is the whole of it."),
        "sword": (["Names a price before answering"], ["A contract is a contract"],
                  ["Has no side"], ["Has already been paid by someone else"],
                  ["That will cost you."],
                  [("threat", "I am paid until dawn. After that, we will see.")],
                  "Genuinely dangerous; entirely purchasable."),
        "child": (["Asks three questions in a row"], ["Wants to be told the truth"],
                  ["Repeats what should not be repeated"], ["Saw who went into the Deep"],
                  ["But why though? But why?"],
                  [("greeting", "You are new. What are you? What are you for?")],
                  "Goes everywhere unnoticed; understands half of it."),
    }
    if _rng is not None:
        order = list(range(len(places)))
        _rng.shuffle(order)
    else:
        order = list(range(len(places)))

    npcs = []
    for i, (nid, nm, role, voice) in enumerate(roles):
        home = places[order[i % len(order)]][0]
        npcs.append({
            "id": nid, "name": nm, "role": role, "start_location": home,
            "anchors": {"voice": voice,
                        "constraints": [f"Will not leave {name} while anyone is still in it.",
                                        "Is an ordinary mortal person."],
                        "goals": [f"Get through what is coming to {name}.",
                                  "Be believed once, before the end."],
                        "taboos": ["Never admits fear aloud.", "Never begs."],
                        **_stub_card(cards.get(nid))},
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
        # The institution with a grip on the place, with a named officer on a
        # real patrol - which is what turns authority.py on.
        "factions": [
            {"id": "the_office", "name": "The High Office", "seat": "high_office",
             "law": 4,
             "members": ["warden", "broker", "sword"],
             "officers": [{"id": "warden", "rank": "Warden",
                           "schedule": {"morning": "high_office", "midday": "commons",
                                        "evening": "commons", "night": "high_office"}},
                          {"id": "sword", "rank": "hired blade",
                           "schedule": {"evening": "house_of_lamps", "night": "the_road"}}]},
            {"id": "the_quiet", "name": "The Quiet House", "seat": "quiet_house",
             "law": 0, "members": ["priest", "physician", "holdout"], "officers": []},
        ],
        # People with opinions about each other. Without these the room is a
        # set of characters who only ever face the player.
        "npc_edges": [
            ["smith", "warden", [-35, -30, 10, 0]],
            ["warden", "smith", [-20, -25, 0, 5]],
            ["broker", "host", [-25, -40, 0, 0]],
            ["host", "broker", [-30, -45, 15, 20]],
            ["priest", "warden", [15, -20, 0, 10]],
            ["digger", "holdout", [30, 25, 0, 0]],
            ["child", "host", [40, 35, 0, 0]],
            ["physician", "sword", [-40, -35, 25, 0]],
        ],
    }


def _stub_card(entry):
    """The offline persona card, in the same shape the schema asks for."""
    if not entry:
        return {}
    mannerisms, values, flaws, secrets, catchphrases, lines, power = entry
    return {
        "mannerisms": list(mannerisms), "values": list(values), "flaws": list(flaws),
        "secrets": list(secrets), "catchphrases": list(catchphrases),
        "famous_lines": [{"beat": b, "line": l} for b, l in lines],
        "power_profile": power,
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
        # This world does answer death, at a price - so death.py can actually
        # offer the revive resolution instead of silently never having it.
        "revival_rule": {"name": "The Deep gives back",
                         "cost": "It keeps something of yours in exchange, and does not say what."},
        # Somebody is already organised when the player walks in.
        "orgs": [
            {"id": "the_ledger", "name": "The Ledger", "kind": "company",
             "seat": loc_ids[4], "charter": "Debts, held and called in.",
             "members": [npc_ids[3]] if len(npc_ids) > 3 else []},
            {"id": "the_dig_crew", "name": "The Dig Crew", "kind": "crew",
             "seat": loc_ids[6] if len(loc_ids) > 6 else loc_ids[-1],
             "charter": "Goes down so nobody else has to.",
             "members": [npc_ids[5]] if len(npc_ids) > 5 else []},
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


def _resilient(role, system, user, *, user_id, max_tokens, temperature, stub,
               json_mode=True):
    """D3: a region/world build is 6-9 separate model calls, and ONE truncated
    response used to fail the WHOLE build with a 502 - wasting every other
    call that had already succeeded, at real cost, with nothing to show for
    it. A truncated or malformed JSON response raises llm.LLMError the same
    way a missing API key does, so the two have to be told apart: a missing
    key is a config problem the retry cannot fix and must still surface
    honestly; a truncation is a size problem worth ONE retry at 50% more
    budget before falling back to the same procedural stub offline mode
    already uses, so the build completes instead of dying on the last
    district out of eight."""
    try:
        return llm.complete(role, system, user, user_id=user_id, json_mode=json_mode,
                            max_tokens=max_tokens, temperature=temperature, stub=stub)
    except llm.LLMError as e:
        if not config.key_for(config.MODELS.get(role, config.MODELS["narrator"])):
            raise  # a config problem - no retry can fix a missing key
        try:
            return llm.complete(role, system, user, user_id=user_id, json_mode=json_mode,
                                max_tokens=int(max_tokens * 1.5), temperature=temperature,
                                stub=stub)
        except llm.LLMError:
            return stub()


def _build_districts(brief, *, user_id, spec, setting, personal):
    """Pass 1: lay out the districts. Only runs for multi-district scales."""
    plan = _resilient(
        "narrator", DISTRICT_SYSTEM,
        brief + f"\n\nLay out EXACTLY {spec['districts']} districts. JSON only.",
        user_id=user_id, max_tokens=6000, temperature=1.0,
        stub=lambda: _stub_districts(setting, spec["districts"]))
    districts = [d for d in (plan.get("districts") or []) if d.get("id")]
    return plan, districts[:spec["districts"]]


def _fill_district(district, others, brief, *, user_id, spec, setting):
    """One pass per district. Small enough to always come back whole."""
    lo, hi = spec["locs"]
    nlo, nhi = spec["npcs"]
    ask = (
        f"{brief}\n\n"
        f"THIS DISTRICT: {district['id']} - {district.get('name', '')}\n"
        f"{district.get('premise', '')}\n"
        f"NEIGHBOURING DISTRICTS: {', '.join(o['id'] for o in others) or 'none'}\n\n"
        f"Give it {lo}-{hi} locations and {nlo}-{nhi} characters. "
        f"Prefix every id with '{district['id']}_'. JSON only."
    )
    out = _resilient(
        "narrator", DISTRICT_FILL_SYSTEM, ask, user_id=user_id,
        max_tokens=7000, temperature=1.0,
        stub=lambda: _stub_fill(district, spec))
    return out.get("locations") or [], out.get("npcs") or []


def _stitch(plan, districts, filled) -> dict:
    """Weld the passes into one world.

    Two things have to be true afterwards or the world is not playable: every
    id is unique, and the map is connected. Passes are generated independently
    and cannot guarantee either on their own, so both are enforced here rather
    than hoped for in a prompt."""
    locations, npcs = [], []
    seen_loc, seen_npc = set(), set()

    for d, (locs, people) in zip(districts, filled):
        first_here = None
        for l in locs:
            lid = str(l.get("id") or "").strip()
            if not lid or lid in seen_loc:
                continue
            seen_loc.add(lid)
            l["district"] = d["id"]
            locations.append(l)
            first_here = first_here or lid
        d["_entry"] = first_here
        for n in people:
            nid = str(n.get("id") or "").strip()
            if not nid or nid in seen_npc:
                continue
            seen_npc.add(nid)
            n["district"] = d["id"]
            npcs.append(n)

    # Drop connects that point at nothing, then wire the districts together
    # through their entry locations so the whole map is reachable.
    for l in locations:
        l["connects"] = [c for c in (l.get("connects") or []) if c in seen_loc and c != l["id"]]

    by_id = {l["id"]: l for l in locations}
    for d in districts:
        entry = d.get("_entry")
        if not entry:
            continue
        for other_id in (d.get("connects") or []):
            other = next((x for x in districts if x["id"] == other_id), None)
            if not other or not other.get("_entry"):
                continue
            a, b = by_id[entry], by_id[other["_entry"]]
            if b["id"] not in a["connects"]:
                a["connects"].append(b["id"])
            if a["id"] not in b["connects"]:
                b["connects"].append(a["id"])

    # A district the model forgot to connect still has to be reachable, or a
    # player can be permanently stranded away from most of the world.
    entries = [d["_entry"] for d in districts if d.get("_entry")]
    for i in range(1, len(entries)):
        a, b = by_id[entries[i - 1]], by_id[entries[i]]
        if b["id"] not in a["connects"]:
            a["connects"].append(b["id"])
        if a["id"] not in b["connects"]:
            b["connects"].append(a["id"])

    return {
        "name": plan.get("name") or "A world",
        "tagline": plan.get("tagline") or "",
        "premise": plan.get("premise") or "",
        "arrival": plan.get("arrival") or "",
        "default_protagonist": plan.get("default_protagonist") or "",
        "districts": [{k: v for k, v in d.items() if not k.startswith("_")}
                      for d in districts],
        "locations": locations,
        "npcs": npcs,
    }


def _stub_districts(setting: str, n: int) -> dict:
    """Offline stub, so the multi-pass path is exercised by the test suite at
    every scale without a key or a cent."""
    base = worldkit.slug(setting, "world")
    kinds = ["town", "quarter", "stronghold", "wilds", "sacred", "industry",
             "town", "quarter"]
    return {
        "name": setting.title()[:40] or "A World",
        "tagline": "Built offline, deterministically.",
        "premise": f"You arrive in {setting}. Nobody here knows your name yet.",
        "arrival": "You came in on the road, with the dust still on you.",
        "default_protagonist": "a traveller nobody here has heard of",
        "districts": [
            {"id": f"{base}_d{i}", "name": f"District {i + 1}",
             "kind": kinds[i % len(kinds)],
             "premise": "A place with its own trouble.",
             "connects": [f"{base}_d{j}" for j in range(n) if j != i][:2]}
            for i in range(n)
        ],
    }


def _stub_fill(district: dict, spec: dict) -> dict:
    did = district["id"]
    nlocs, nnpcs = spec["locs"][0], spec["npcs"][0]
    locs = [{"id": f"{did}_p{i}", "name": f"Place {i + 1}", "kind": "open",
             "desc": "A place that exists, plainly, and waits.",
             "connects": [f"{did}_p{(i + 1) % nlocs}"]} for i in range(nlocs)]
    npcs = [{"id": f"{did}_n{i}", "name": f"Person {i + 1}", "role": "someone here",
             "start_location": f"{did}_p{i % nlocs}",
             "anchors": {"voice": "Plain, short sentences. Says the thing.",
                         "constraints": ["cannot leave the district"],
                         "goals": ["get through the week"],
                         "taboos": ["will not lie outright"]},
             "schedule": {p: f"{did}_p{i % nlocs}"
                          for p in ("morning", "midday", "evening", "night")},
             "seed_memories": ["I have been here a long time."],
             "initial_relationship": {"affinity": 0, "trust": 0, "fear": 0, "obligation": 0}}
            for i in range(nnpcs)]
    return {"locations": locs, "npcs": npcs}


def bootstrap(setting: str, *, user_id: str, tone: str = "",
              mode: str = "auto", scale: str = "town",
              answers: dict | None = None, seed: int = 0) -> dict:
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
    # A premise is PARSED, not searched. "I and Charlie from Hazbin Hotel are
    # inside the world of The Last of Us" names two real properties and a
    # character; searching that whole string found nothing, and the player was
    # told "Nothing found for that name" about a wholly canon request.
    found = (_empty_dossier(setting) if mode == "original"
             else research.premise_dossier(setting))

    # F1 - era selection. Chosen in Session Zero (after this dossier already
    # exists), so it has to override the cast/places HERE rather than at
    # lookup time. Asked for "the Sengoku era" of Demon Slayer, a build got
    # Zenitsu and Inosuke - who would not be born for centuries - because
    # nothing in the pipeline knew an era had even been asked for. Only
    # fires when canon_seed actually has that era on record; an unmatched
    # setting or era falls through to whatever research already found,
    # exactly as before.
    era = (answers or {}).get("era") or ""
    if era:
        canonical = found.get("canonical_name", "")
        era_cast = canon_seed.fallback_cast(setting, canonical, era=era)
        era_places = canon_seed.fallback_places(setting, canonical, era=era)
        if era_cast:
            found = {**found, "characters": era_cast}
        if era_places:
            found = {**found, "places": era_places}

    grounding = research.grounding_brief(found)
    premise = research.premise_brief(found)
    parsed = found.get("premise") or {}

    # CANON now means "the player named a property", not "a lookup succeeded".
    # The old rule handed a failed lookup to the builder as an instruction to
    # invent something original - which is how a crossover of two real IPs
    # became a generic fantasy town. The model already knows most fiction; a
    # miss should cost grounding, never the player's actual request.
    # A property the player actually NAMED, as opposed to the raw string
    # echoed back as a host. An original setting names nothing, so research
    # and the keyword list remain the only signals for it - which is the
    # behaviour that was there before and is still the right one.
    named_properties = bool(parsed.get("imports") or parsed.get("host_is_proper"))
    canon = bool(found.get("found")) or (mode != "original" and named_properties)
    # PRIVATE BY DEFAULT is not a judgement about the player - they own this
    # world, play it, and export it. It only means a world that continues
    # someone else's setting is not PUBLICLY LISTED on a shared service, which
    # protects whoever runs the deployment as much as the player.
    #
    # The keyword list survives for exactly one job: research is what normally
    # tells us a world is canon, and research cannot run offline. Without the
    # fallback, the same world would be private when built online and public
    # when built offline - and the offline answer is the unsafe one.
    # A named property is treated as somebody else's IP whether or not the
    # lookup worked. Before this, a real crossover that research missed could
    # be marked PUBLICLY LISTABLE, because the only other signal was a keyword
    # list that contains neither "the last of us" nor "hazbin hotel".
    personal = canon or named_properties or looks_like_ip(setting)

    brief = f"SETTING: {setting}"
    if found.get("canonical_name") and found["canonical_name"].lower() != setting.lower():
        brief += f"\nCANONICAL TITLE: {found['canonical_name']}"
    # What they asked for, in their words, ahead of anything we looked up - so
    # a crossover survives even when every lookup misses.
    if premise:
        brief += "\n\n" + premise
    if tone:
        brief += f"\nTONE THE PLAYER ASKED FOR: {tone}"
    if personal:
        brief += (
            "\n\nThis continues an existing setting. This world is the PLAYER'S: it is "
            "private to them and never listed on the public feed - they play it or invite "
            "friends by room code.\n"
            "Use the setting's real characters, places and factions - that is the point. "
            "Build them an ORIGINAL situation inside that world: a corner the source never "
            "covers, with its own laws and its own fate.\n"
            "Do not copy sentences from the source material, and do not simply retell the "
            "story that already exists - the player wants to LIVE somewhere, not re-read it."
        )
    if parsed.get("imports"):
        brief += (
            "\n\nTHIS IS A CROSSOVER, AND THAT IS DELIBERATE. Build the HOST world "
            "faithfully - its places, its dangers, its rules. Then place the carried-in "
            "characters into it AS THEMSELVES. They keep their own voice, values and "
            "limits; they do not become locals and they are not re-explained. How they "
            "got here is not your problem and must not be narrated - they are simply "
            "here, and the world reacts to them being here."
        )

    if grounding:
        brief += "\n\n" + grounding

    # Session Zero: where the player enters the timeline, where they sit in
    # the world's own power system, and what limits them. Without this the
    # builder produces a good setting in which the protagonist has no defined
    # place - the specific failure that makes canon worlds feel generic.
    sz = sessionzero.brief(answers or {}, found)
    if sz:
        brief += "\n\n" + sz

    # A town is one call. Anything larger is built district by district, so
    # the world can actually be as big as the player asked for instead of
    # being silently capped at whatever fits in a single response.
    spec = SCALES.get(scale) or SCALES["town"]
    if spec["districts"] <= 1:
        structure = _resilient(
            "narrator", STRUCTURE_SYSTEM,
            brief + "\n\nBuild the places and the people. JSON only.",
            user_id=user_id, max_tokens=8000, temperature=1.0,
            stub=lambda: _stub_structure(setting, personal, seed=seed))
    else:
        plan, districts = _build_districts(brief, user_id=user_id, spec=spec,
                                           setting=setting, personal=personal)
        # D3: each district is now its OWN failure domain - a truncation on
        # district 6 of 8 no longer discards the 5 that already succeeded
        # (and were already paid for). _resilient retries once, then falls
        # back to the same procedural stub offline mode uses for just that
        # one district, so a region/world build completes even when a single
        # pass comes back oversized.
        filled = [
            _fill_district(d, [o for o in districts if o["id"] != d["id"]],
                           brief, user_id=user_id, spec=spec, setting=setting)
            for d in districts
        ]
        structure = _stitch(plan, districts, filled)

    laws_brief = (
        f"WORLD: {structure.get('name')}\n"
        f"PLACES: {', '.join(l.get('id', '') for l in structure.get('locations', []))}\n"
        f"PEOPLE: {', '.join(n.get('id', '') for n in structure.get('npcs', []))}\n\n"
        "Write the laws and the fate. JSON only."
    )
    laws = _resilient(
        "narrator", LAW_SYSTEM, laws_brief, user_id=user_id,
        max_tokens=3200, temperature=0.7, stub=lambda: _stub_laws(structure))

    raw = {**structure, **{k: v for k, v in laws.items() if v}}
    raw["origin"] = "bootstrap"
    raw["source_prompt"] = setting
    # Recorded on the world so two worlds built from the same seed are
    # provably the same build, and so a Daily can be verified after the fact.
    if seed:
        raw["seed"] = int(seed)
    # Attribution travels with the world: CC BY-SA asks for it, and a
    # player deserves to know which wiki their world was grounded on.
    raw["sources"] = research.attribution(found)
    raw["researched"] = bool(found.get("found"))
    raw["personal_only"] = personal
    raw["inspired_by"] = found.get("canonical_name") or (setting if personal else "")
    raw["mode"] = "canon" if canon else "original"
    raw["scale"] = scale if scale in SCALES else "town"
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
