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
import pathlib
import re
import shutil
import tempfile

os.environ["STORYLIVER_LLM_MODE"] = "mock"
_TMP = tempfile.mkdtemp(prefix="storyliver-worldgen-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP

from backend import (authority, db, death, engine, memory, modetree,  # noqa: E402
                     research, worldforge, worldkit)
from backend import worlds as registry  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
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

    # A word-boundary regression found by typing into the real UI: "Hazbin
    # Hotel inside The Last of Us" picked HOTEL, because the `in` alternative
    # matched the last two letters of "Hazbin" and took the next capitalised
    # word. A name that contains a preposition is not rare.
    boundary = research.parse_premise("Charlie from Hazbin Hotel inside The Last of Us")
    ok(boundary["host"] == "The Last of Us",
       f"a preposition INSIDE a name does not become the match "
       f"({boundary['host']!r})")

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


def test_all_four_scales_build_without_truncating():
    section("D3 - region and world builds, not just town and city")
    # Bumped once already (6000/7000 token caps) and never actually verified
    # past city scale. A multi-district build is 6-9 separate model calls,
    # and the caps were right - but nothing was checked past what they were
    # tuned against.
    db.init()
    for scale, districts in (("town", 1), ("city", 3), ("region", 5), ("world", 8)):
        w = worldkit.load(worldforge.bootstrap(
            "a drowned lighthouse colony", user_id="d3", scale=scale))
        ok(len(w.locations) > 0 and len(w.npcs) > 0,
           f"{scale} ({districts} district(s)) built real places and people "
           f"({len(w.locations)} places, {len(w.npcs)} people)")
        ok(len(w.rules) >= worldkit.MIN_RULES and len(w.fated_events) == worldkit.MIN_FATED,
           f"{scale}: {len(w.rules)} rules, {len(w.fated_events)} fated events - "
           f"the laws pass output is a FIXED size regardless of world scale, "
           f"so it must not shrink just because more districts fed it")
        ids = [l["id"] for l in w.locations] + [n["id"] for n in w.npcs]
        ok(len(ids) == len(set(ids)),
           f"{scale}: every id across every district is unique, welded correctly")


def test_a_truncated_pass_falls_back_instead_of_failing_the_whole_build():
    section("D3 - one oversized pass degrades gracefully instead of costing the whole build")
    # This was the actual bug behind the reported 502s, not the token caps:
    # `except llm.LLMError: raise HTTPException(502, ...)` turned ANY single
    # truncated response - out of 6 to 9 calls a region/world build makes -
    # into total failure, discarding every call that had already succeeded
    # and been paid for. A retry, then a procedural fallback for just the
    # one pass that failed, is what actually fixes availability; the token
    # bump only lowered how often this path gets exercised.
    from backend import config, llm
    db.init()
    real_complete, real_key_for = llm.complete, config.key_for

    def always_truncates(role, system, user, *, user_id, json_mode=False,
                         max_tokens=700, temperature=0.8, stub=None,
                         playthrough_id=None, model=None):
        raise llm.LLMError("no JSON object in model output")

    llm.complete = always_truncates
    config.key_for = lambda model: "present"   # a real attempt, not a config gap
    try:
        stub_calls = {"n": 0}

        def stub():
            stub_calls["n"] += 1
            return {"districts": [{"id": "d1", "name": "One", "premise": "p", "connects": []}]}

        out = worldforge._resilient("narrator", "sys", "user", user_id="d3fail",
                                    max_tokens=100, temperature=1.0, stub=stub)
        ok(out.get("districts"), "the pass completes via the procedural fallback")
        ok(stub_calls["n"] == 1,
           "exactly one retry happened before falling back — not a silent "
           "first-try surrender, not an infinite retry loop either")
    finally:
        llm.complete, config.key_for = real_complete, real_key_for

    # A missing key is NOT a size problem - retrying cannot fix it, and
    # swallowing it would hide a config error behind bad procedural content.
    llm.complete = always_truncates
    config.key_for = lambda model: ""
    try:
        try:
            worldforge._resilient("narrator", "sys", "user", user_id="d3fail2",
                                  max_tokens=100, temperature=1.0, stub=lambda: {})
            ok(False, "a missing key must still raise, not silently fall back")
        except llm.LLMError:
            ok(True, "a missing key raises cleanly with no retry wasted on it")
    finally:
        llm.complete, config.key_for = real_complete, real_key_for


def test_locations_are_scenes_not_a_floor_plan():
    section("D4 - a sub-room is detail, not its own place")
    # Reported: "Training Ground" inside Butterfly Mansion built as its own
    # PLACE, and a 24-place city read as granular rather than dense. Neither
    # prompt ever told the architect a location was a SCENE, not a room -
    # the schema had a `desc` field and nothing said what belonged in it
    # versus what belonged as its own id. Both passes that emit locations
    # (the single-district STRUCTURE_SYSTEM and the multi-district
    # DISTRICT_FILL_SYSTEM) carry the same instruction now, so a town build
    # and a world build get the same discipline.
    for name, prompt in (("STRUCTURE_SYSTEM", worldforge.STRUCTURE_SYSTEM),
                         ("DISTRICT_FILL_SYSTEM", worldforge.DISTRICT_FILL_SYSTEM)):
        ok("SCENE" in prompt and "floor plan" in prompt,
           f"{name} states the actual test: a location is a scene, not a room")
        ok("sub-room" in prompt.lower(),
           f"{name} names the exact failure mode with a concrete example "
           "(a training ground inside a manor), not an abstract rule a "
           "model can satisfy while still doing the wrong thing")
        ok("folded into the parent" in prompt,
           f"{name} says where the detail actually GOES (the parent's desc) "
           "rather than only saying what not to do")

    # The scale-appropriate per-district cap this sits on top of already
    # existed (SCALES' locs range) - confirm it still builds at every size
    # with the new instruction in place, not just that the text is present.
    db.init()
    for scale in ("town", "city", "region", "world"):
        w = worldkit.load(worldforge.bootstrap(
            "a drowned lighthouse colony", user_id="d4", scale=scale))
        ok(len(w.locations) > 0, f"{scale} still builds real places with the "
                                 f"instruction in place ({len(w.locations)})")


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


# ------------------------------------------------------------------- fate
def test_the_fate_thread_does_not_spoil_itself():
    section("fate - the panel was handing over the ending on turn one")
    db.init()
    pt = engine.create_playthrough("fate-spoil")
    snap = engine.snapshot(pt)
    ahead = [f for f in snap["fate"] if f["status"] != "passed"]
    ok(len(ahead) == len(snap["fate"]),
       "on turn 0 every fated event is still ahead")
    ok(all(not f["title"] and not f["desc"] for f in ahead),
       "and not one of them carries a title or a description - the panel sent "
       "{**event} for all seven, so 'The Warden falls' was readable twenty-six "
       "turns before it happened")
    ok(all(not f.get("kills") for f in ahead),
       "and `kills` never crosses the line at all - it names the character who "
       "dies, and it was going out on turn one")
    ok(all(f["id"].startswith("sealed_") for f in ahead),
       "including the id, which is a slug like 'F5_the_warden_falls' and "
       "carried the title it was supposed to hide")

    blob = json.dumps(snap["fate"]).lower()
    for word in ("warden", "falls", "kindling", "collapse"):
        if word in blob:
            ok(False, f"the word {word!r} reached the client")
            return
    ok(True, "no word from any unreached event is anywhere in the payload")

    # The SAME leak lived on a second screen. narrgraph._futures printed the
    # title and description of the next three fated events onto the Threads
    # panel, so fixing the Fate card alone would have moved the spoiler
    # rather than removed it.
    from backend import narrgraph
    world = engine.world_for(engine._pt(pt))
    futures = narrgraph.view(pt, world, 0)["futures"]
    ok(futures and all(f["label"] == "Something lands here" for f in futures),
       f"the Threads panel marks the future without naming it "
       f"({len(futures)} marks)")
    ok(all(not f["detail"] and not f["place_id"] for f in futures),
       "with no description and no place - a location is a spoiler of its own "
       "when only one thing ever happens there")
    graph_blob = json.dumps(narrgraph.view(pt, world, 0)).lower()
    for word in ("warden", "falls", "kindling", "chapel"):
        if word in graph_blob:
            ok(False, f"the Threads panel leaked {word!r}")
            return
    ok(True, "and no word from an unreached event reaches that panel either")

    db.run("UPDATE playthroughs SET current_turn=10 WHERE id=?", (pt,))
    later = engine.snapshot(pt)["fate"]
    passed = [f for f in later if f["status"] == "passed"]
    ok(passed and all(f["title"] for f in passed),
       f"what has ALREADY happened is named ({len(passed)} of them) - fate "
       f"being fixed is the promise, and history is not a spoiler")
    ok(any(f["status"] == "next" and not f["title"] for f in later),
       "while the one about to land is a mark and a turn: you know something "
       "is coming and when, which is the dread the panel was for")


def test_the_price_of_a_world_is_quoted_correctly():
    section("the number a player decides on")
    # This one number existed three times: hand-written into the backend's
    # scale blurbs, hand-written AGAIN into the client's own copy of the size
    # list, and computed in scale_plan(). All three disagreed, and both
    # hand-written copies quoted every size exactly one model call cheaper
    # than it is - on the screen where the player decides what to spend.
    for key, spec in worldforge.SCALES.items():
        plan = worldforge.scale_plan(key)
        expected = (1 if spec["districts"] > 1 else 0) + spec["districts"] + 1
        ok(plan["model_calls"] == expected,
           f"{key}: the quoted cost is the real one ({plan['model_calls']} calls "
           f"for {spec['districts']} district(s) plus laws)")
        ok(not re.search(r"~?\d+\s*(?:model )?calls?", spec["blurb"]),
           f"{key}: the blurb describes the shape and leaves the count to "
           "arithmetic, so it cannot go stale next time a pass is added")
        lo, hi = plan["locations"]
        ok(lo >= spec["districts"] and hi >= lo,
           f"{key}: the places promised are a real range, not a guess ({lo}-{hi})")

    forge_js = (ROOT / "frontend" / "app" / "forge.js").read_text(encoding="utf-8")
    ok("/forge/scales" in forge_js,
       "and the client asks the server for the numbers rather than keeping a "
       "fourth copy of them")
    ok(worldforge.scale_plan("nonsense")["scale"] == "town",
       "an unknown size falls back to a town rather than raising at the one "
       "moment the player is committing")


def test_a_crossover_is_asked_crossover_questions():
    section("session zero - the two questions a crossover actually raises")
    # Session Zero asked about entry point, role and power level: setting
    # questions. A premise that carries somebody IN from elsewhere raises two
    # different ones - how they got here, and whether what they could do still
    # works - and neither is answerable from the host world's research, so
    # neither was being asked. The endpoint also called research.dossier(),
    # which drops the premise's imports entirely.
    from backend import sessionzero
    d = research.premise_dossier(
        "I and Charlie from Hazbin Hotel are inside the world of The Last of Us")
    q = sessionzero.questions(d)
    ids = [x["id"] for x in q["questions"]]
    ok(q.get("crossover") is True, "the premise is recognised as a crossover")
    ok("arrival" in ids and "keeps" in ids,
       f"and both crossover questions are asked ({', '.join(ids)})")
    arrival = next(x for x in q["questions"] if x["id"] == "arrival")
    ok("Charlie" in arrival["q"],
       "the question names the character being carried in, not 'your character'")
    ok({o["id"] for o in arrival["options"]} == {"always", "torn", "hidden"},
       "with the three answers that actually change the world: always here, "
       "torn through recently, or arrived and hiding it")

    b = sessionzero.brief({"arrival": "hidden", "keeps": "weakened"}, d)
    ok("HIDING" in b and "suspicious" in b,
       "and the answer reaches the builder as an instruction it can act on")
    ok("WHAT CARRIED OVER" in b and "fails when it matters" in b,
       "including what still works, so the builder neither nerfs them quietly "
       "nor leaves the world with no answer")

    plain = sessionzero.questions(research.premise_dossier("a drowned lighthouse colony"))
    ok(not plain.get("crossover")
       and "arrival" not in [x["id"] for x in plain["questions"]],
       "an original setting is not asked how it got here")


def test_the_forge_sends_what_the_endpoint_requires():
    section("the forge - a POST body that satisfies its own model")
    # Every build failed with a 422. api() appends `user_id` to the QUERY
    # STRING, and FastAPI reads a Pydantic model from the BODY alone - so
    # `Bootstrap.user_id`, which is required, arrived empty on every request
    # the rebuilt forge made. The console said "[object Object]", because a
    # 422's `detail` is a LIST of field errors and the error handler stringified
    # it whole, so the one message that would have named the field was the one
    # message the app could not print.
    from backend import main as api_main
    forge_js = (ROOT / "frontend" / "app" / "forge.js").read_text(encoding="utf-8")
    app_js = (ROOT / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")

    required = [n for n, fl in api_main.Bootstrap.model_fields.items() if fl.is_required()]
    ok("user_id" in required, "Bootstrap declares user_id as required")
    body = forge_js[forge_js.index("/forge/bootstrap"):][:600]
    missing = [n for n in required if f"{n}:" not in body]
    ok(not missing,
       "and the forge's build body carries every required field"
       + (" - missing " + ", ".join(missing) if missing else ""))

    ok("Array.isArray(d)" in app_js,
       "a 422 reports which field failed instead of '[object Object]' - the "
       "detail is a list, and stringifying it whole hid every validation error "
       "this app has ever raised")


def _all():
    return (test_a_premise_is_parsed_not_searched,
            test_research_enriches_and_never_gates,
            test_an_original_setting_is_still_original,
            test_a_crossover_is_private_even_when_research_misses,
            test_a_forged_world_matches_the_starter,
            test_all_four_scales_build_without_truncating,
            test_a_truncated_pass_falls_back_instead_of_failing_the_whole_build,
            test_locations_are_scenes_not_a_floor_plan,
            test_the_dark_systems_actually_light_up,
            test_the_daily_is_genuinely_the_same_world,
            test_the_fate_thread_does_not_spoil_itself,
            test_the_price_of_a_world_is_quoted_correctly,
            test_a_crossover_is_asked_crossover_questions,
            test_the_forge_sends_what_the_endpoint_requires)


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
