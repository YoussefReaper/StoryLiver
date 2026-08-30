"""World generation - the depth the rest of the engine is built to consume.

The engine implements about eighteen systems. A forged world used to feed
twelve of them, and the six it starved were not small: the entire authority
workstream (patrols, warrants, bounties, cross-run town memory), NPC-to-NPC
feeling, the anti-drift persona card, and the revival rule. They were dark on
every world a player could actually make, because the bootstrap SCHEMA had no
slot for them - research even collected factions and powers and then dropped
them into a prose paragraph, because there was nowhere structured to put them.

So the bar here is not "does it produce a world". It is:

  DENSITY       A forged world must match the hand-authored starter. Emberfall
                is the standard because it is what the engine was designed
                against; anything the forge cannot reach is a system that only
                works in a world nobody can build.

  PREMISE       A player writes a sentence, not a title. "I and Charlie from
                Hazbin Hotel are inside the world of The Last of Us" names two
                real properties and a character, and the old identifier needed
                60% of that sentence's tokens to appear in an article title -
                so a wholly canon request came back "Nothing found for that
                name". Research must ENRICH and never GATE.

  DETERMINISM   The Daily Challenge's whole premise is that everyone plays the
                same world today. The seed was computed, stored, and never
                read by anything.

Run:  python -m tests.test_worldgen
"""
import json
import os
import shutil
import tempfile

os.environ["STORYLIVER_LLM_MODE"] = "mock"
_TMP = tempfile.mkdtemp(prefix="storyliver-worldgen-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP

from backend import (authority, db, death, engine, memory, modetree,  # noqa: E402
                     research, worldforge, worldkit)
from backend import worlds as registry  # noqa: E402

FAILS, NOTES = [], []


def ok(cond, msg):
    (NOTES if cond else FAILS).append(msg)


def section(t):
    NOTES.append("\n  " + t)


db.init()


def _forge(setting="A drowned cathedral city", **kw):
    return worldkit.load(worldforge.bootstrap(setting, user_id="wg", **kw))


# ------------------------------------------------------------------ premise
def test_a_premise_is_parsed_not_searched():
    section("premise - a player writes a sentence, not a title")
    p = research.parse_premise(
        "I and Charlie from Hazbin Hotel are inside the world of The Last of Us")
    ok(p["host"] == "The Last of Us",
       f"the host world is picked out of the sentence ({p['host']!r})")
    ok(p["imports"] == [{"character": "Charlie", "from": "Hazbin Hotel"}],
       f"and so is who is being carried in, and where from ({p['imports']})")
    ok("I" not in p["entities"] and "Charlie" in p["entities"],
       "a pronoun that opened the sentence is not mistaken for a name")

    for text, host, imports in (
            ("Me and Gojo from Jujutsu Kaisen in the world of Attack on Titan",
             "Attack on Titan", 1),
            ("Frodo from The Lord of the Rings inside Cyberpunk 2077",
             "Cyberpunk 2077", 1),
            ("Tanjiro from Demon Slayer set in Middle-earth", "Middle-earth", 1)):
        q = research.parse_premise(text)
        if q["host"] != host or len(q["imports"]) != imports:
            ok(False, f"{text!r} parsed as host={q['host']!r} imports={q['imports']}")
            return
    ok(True, "titles keep their article, numbers stay inside a name, and a "
             "connective does not swallow the rest of the sentence")

    plain = research.parse_premise("The Last of Us")
    ok(not plain["is_premise"] and plain["host"] == "The Last of Us",
       "a bare title needs no parsing and is left alone")


def test_research_enriches_and_never_gates():
    section("premise - a failed lookup must not delete the request")
    d = research.premise_dossier(
        "I and Charlie from Hazbin Hotel are inside the world of The Last of Us")
    brief = research.premise_brief(d)
    ok("The Last of Us" in brief and "Charlie" in brief and "Hazbin Hotel" in brief,
       "everything the player named reaches the world builder")
    ok("HOST WORLD" in brief and "CARRIED IN" in brief,
       "labelled as a host world and a carried-in character, which is the "
       "shape a crossover actually has")
    ok("Use everything you already know about them" in brief,
       "and when no wiki confirms them the builder is told to use what it "
       "knows - the model knows most fiction, and the old path threw that "
       "away by calling an unconfirmed request 'original'")


def test_an_original_setting_is_still_original():
    section("premise - and an invented setting is not quietly made private")
    p = research.parse_premise("a frozen post-collapse Earth")
    named = bool(p["imports"] or p["host_is_proper"])
    ok(not named,
       "nothing was NAMED here - the raw string is echoed as a host, and "
       "counting that as somebody's property would force every original world "
       "private and unshareable")
    world = worldforge.bootstrap("a frozen post-collapse Earth", user_id="wg")
    ok(not world.get("personal_only"),
       "so an original world stays shareable")
    ok(world.get("mode") == "original", "and is labelled original")


def test_a_crossover_is_private_even_when_research_misses():
    section("premise - somebody else's IP is private whether or not we find it")
    world = worldforge.bootstrap(
        "Charlie from Hazbin Hotel inside The Last of Us", user_id="wg")
    ok(world.get("personal_only"),
       "a named crossover is personal-only even with every lookup missing - "
       "the keyword list contains neither of those titles, so before this a "
       "real crossover of two real properties could be marked publicly listable")
    ok(world.get("mode") == "canon",
       "and it is labelled canon rather than original, because it continues "
       "somebody's setting whatever a wiki said")


# ------------------------------------------------------------------ density
def _density(w):
    d = w.data
    a = d["npcs"][0]["anchors"]
    return {
        "factions": len(d.get("factions") or []),
        "law_holding": len(authority.authorities(w)),
        "npc_edges": len(d.get("npc_edges") or []),
        "orgs": len(d.get("orgs") or []),
        "revival": bool(d.get("revival_rule")),
        "card": [k for k in ("mannerisms", "values", "flaws", "secrets",
                             "catchphrases", "famous_lines", "power_profile")
                 if a.get(k)],
    }


def test_a_forged_world_matches_the_starter():
    section("density - the forge must reach the bar the engine was built against")
    db.init()
    forged = _density(_forge())
    starter = _density(registry.resolve("emberfall", None))

    ok(forged["factions"] >= starter["factions"],
       f"factions {forged['factions']} vs the starter's {starter['factions']} - "
       f"a world with none falls back to a synthetic 'locals' group that can "
       f"never detain, fine or remember anyone")
    ok(forged["law_holding"] >= 1,
       f"at least one faction can actually enforce something "
       f"({forged['law_holding']}) - authority.authorities() needs law>=3, and "
       f"with none the entire patrol/warrant/bounty workstream is dark")
    ok(forged["npc_edges"] >= starter["npc_edges"],
       f"characters have opinions about EACH OTHER "
       f"({forged['npc_edges']} vs {starter['npc_edges']}) - without these "
       f"memory.between_block prints nothing and everyone only ever faces the "
       f"player")
    ok(forged["orgs"] >= 1,
       f"something is already organised when the player arrives "
       f"({forged['orgs']}) - so the Power panel and the infiltration verb "
       f"have a target on turn one instead of an empty board")
    ok(forged["revival"], "the world states how it answers death")
    ok(len(forged["card"]) >= 6,
       f"and every character carries the full persona card "
       f"({', '.join(forged['card'])}) - the anti-drift identity block existed "
       f"and nothing ever filled it, on forged worlds OR the starter")


def test_the_dark_systems_actually_light_up():
    section("density - and the systems downstream of it come on")
    db.init()
    raw = worldforge.bootstrap("A drowned cathedral city", user_id="wg")
    pt = engine.create_playthrough("wg", world_id="emberfall",
                                   world_json=json.dumps(raw))
    world = engine.world_for(engine._pt(pt))

    block = memory.anchor_block(world, world.npcs[0]["id"], beat="threat")
    ok("MANNERISMS" in block,
       "the narrator is handed the full identity card, not the five-field "
       "fallback - anchor_block already switched on the card's presence and "
       "the presence never happened")
    ok("CANON LINES FOR THIS MOMENT" in block,
       "with the line keyed to THIS moment surfaced and the rest withheld")

    auths = authority.authorities(world)
    ok(auths, f"there is an institution with legal power ({len(auths)})")
    ok(authority.officers(world, auths[0]),
       f"with named officers on real patrols "
       f"({[o.get('id') for o in authority.officers(world, auths[0])]})")

    between = memory.between_block(pt, world, [n["id"] for n in world.npcs[:6]])
    ok("BETWEEN THEM" in between,
       f"and the narrator is told who resents whom: "
       f"{between.strip().splitlines()[1].strip()[:56]!r}")

    ok("revive" in [r["id"] for r in death.offered(pt, world=world)],
       "death can offer revival - World keeps its fields in .data behind "
       "__slots__, so the getattr() that guarded this always returned None and "
       "the option could never appear on ANY world")


# ------------------------------------------------------------ determinism
def test_the_daily_is_genuinely_the_same_world():
    section("determinism - the Daily's whole premise, actually enforced")
    db.init()
    seed = modetree.daily_seed("2026-08-30")
    a = worldforge.bootstrap("The Sunken Court", user_id="p1", seed=seed)
    b = worldforge.bootstrap("The Sunken Court", user_id="p2", seed=seed)
    c = worldforge.bootstrap("The Sunken Court", user_id="p3",
                             seed=modetree.daily_seed("2026-08-31"))

    where = lambda w: [(n["id"], n["start_location"]) for n in w["npcs"]]
    ok(where(a) == where(b),
       "two different players on the same day build the identical world - the "
       "seed was computed, stored on the session, and read by NOTHING, so this "
       "held only because every Daily happened to use the same starter")
    ok(where(a) != where(c), "and a different day is a different world")
    ok(a.get("seed") == seed,
       f"the seed is recorded on the world ({a.get('seed')}), so a Daily can "
       f"be verified after the fact rather than taken on trust")


def _all():
    return (test_a_premise_is_parsed_not_searched,
            test_research_enriches_and_never_gates,
            test_an_original_setting_is_still_original,
            test_a_crossover_is_private_even_when_research_misses,
            test_a_forged_world_matches_the_starter,
            test_the_dark_systems_actually_light_up,
            test_the_daily_is_genuinely_the_same_world)


def main():
    print("StoryLiver - world generation")
    print("  the depth the rest of the engine is built to consume\n")
    for fn in _all():
        fn()
    passed = 0
    for n in NOTES:
        if n.startswith("\n"):
            print(n)
        else:
            print("  PASS  " + n)
            passed += 1
    for f in FAILS:
        print("  FAIL  " + f)
    print(f"\n  {passed} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


def test_all_worldgen():
    for fn in _all():
        fn()
    assert not FAILS, "\n".join(FAILS)


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
