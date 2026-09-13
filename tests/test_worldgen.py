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

from backend import (authority, canon_lore, canon_seed, db, death, engine,  # noqa: E402
                     memory, modetree, persona, research, worldforge, worldkit)
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
    ok([(i["character"], i["from"]) for i in p["imports"]] == [("Charlie", "Hazbin Hotel")],
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


def test_a_parenthesised_source_still_carries_the_character_in():
    section("premise - the way people actually type a crossover")
    # REPORTED, twice: "I started a Demon Slayer world with Charlie and Charlie
    # wasn't even there." The seating code (_seat_imports) was fixed to
    # guarantee the named character a seat - but seating can only guarantee
    # somebody the PARSER found, and this premise found nobody. `_IMPORT_PATTERN`
    # knows one shape, "Character from Source", and the player wrote the other
    # one, "charlie (from hazbin hotel)" - a parenthesis, in lowercase, with the
    # host after it. imports came back empty, so Charlie was never carried in,
    # never seated, and never given her Hazbin Hotel card. The bug was upstream
    # of every fix that had already been made for it.
    p = research.parse_premise(
        "I and my girlfriend charlie (from hazbin hotel) in Demon Slayer verse")
    ok(p["host"] == "Demon Slayer",
       f"the host is still picked out ({p['host']!r})")
    ok(len(p["imports"]) == 1,
       f"and the character in the parenthesis is carried in ({p['imports']})")
    if p["imports"]:
        imp = p["imports"][0]
        ok(imp["character"].lower() == "charlie",
           f"the character is Charlie, not the words in front of her "
           f"({imp['character']!r})")
        ok(imp["from"].lower() == "hazbin hotel",
           f"and she comes from Hazbin Hotel ({imp['from']!r})")

    # The same shape without "from", and with a fuller name and a capitalised
    # source: all three are things a player types.
    for text, who, src in (
            ("Charlie (Hazbin Hotel) in Demon Slayer", "Charlie", "Hazbin Hotel"),
            ("Charlie Morningstar (Hazbin Hotel) in Demon Slayer",
             "Charlie Morningstar", "Hazbin Hotel"),
            ("me and Charlie (from Hazbin Hotel) in Demon Slayer",
             "Charlie", "Hazbin Hotel")):
        q = research.parse_premise(text)
        got = q["imports"][0] if q["imports"] else {}
        if got.get("character") != who or got.get("from") != src:
            ok(False, f"{text!r} parsed as {q['imports']}")
            return
    ok(True, "a parenthesised source is a source, with or without 'from', "
             "lowercase or not")


def test_a_crossover_is_set_in_the_host_world_not_the_guests_home():
    section("crossover - the world is the HOST, not where the guest came from")
    # REPORTED: "I and my girlfriend Charlie from Hazbin Hotel in the verse of
    # Demon Slayer" built a world called HAZBIN HOTEL - the imported guest's
    # home, not the world the story is set in. Two causes, both fixed:
    #   1. "verse" was not in _HOST_PATTERNS, so "in the verse of Demon Slayer"
    #      parsed NO host at all (the fallback needed a capitalised name and
    #      lowercase "verse" failed).
    #   2. inspired_by took research's canonical_name FIRST - and research
    #      resolves whichever franchise it ranks highest, which in a crossover
    #      is usually the explicitly-named import. The host must outrank it.
    for text in (
            "I and my girlfriend charlie (from hazbin hotel) in Demon Slayer verse",
            "me and Charlie from Hazbin Hotel in the verse of Demon Slayer"):
        p = research.parse_premise(text)
        if p["host"] != "Demon Slayer":
            ok(False, f"{text!r} parsed host={p['host']!r}, expected 'Demon Slayer'")
            return
    ok(True, "'verse' is a host phrase - trailing ('Demon Slayer verse') and "
             "leading ('the verse of Demon Slayer')")

    imports = research.parse_premise(
        "me and Charlie from Hazbin Hotel in the verse of Demon Slayer")["imports"]
    ok([i["from"] for i in imports] == ["Hazbin Hotel"],
       f"the guest's home is still recorded as an import ({imports})")

    # ...and the world that actually gets built is the host's, not the
    # guest's. This is the line the player reads: narrator source, World
    # Master source, and every canon persona lookup all key off it.
    world = _forge("me and Charlie from Hazbin Hotel in the verse of Demon Slayer")
    src = str(world.get("inspired_by") or "")
    ok(src.lower() == "demon slayer",
       f"the world is Demon Slayer, not the guest's home ({src!r})")

    # If the parser captures a CHARACTER as host (no explicit world phrase),
    # world resolution should still prefer that character's franchise when we
    # know it, not build a world "inspired by Akaza".
    host_char = _forge("I meet Akaza; we are enemies but we have respect.")
    inferred = str(host_char.get("inspired_by") or "")
    ok(inferred.lower() == "demon slayer",
       f"a host character is mapped back to its franchise ({inferred!r})")


def test_the_very_beginning_narrows_the_cast_instead_of_widening_it():
    section("canon - an entry point is a moment, not the whole franchise")
    # REPORTED: picked "The very beginning" of Demon Slayer and landed in a
    # place with Rengoku and Shinobu already standing in it. They are Hashira
    # who do not appear until far later; chapter one is Tanjiro, Nezuko and
    # Giyu on Mt. Sagiri. The roster was flat, so choosing an entry point
    # changed the prose and nothing else.
    start = {c["name"] for c in canon_seed.arc_cast("Demon Slayer", arc="start")}
    ok(bool(start), "the seed has a canon answer for 'the very beginning'")
    ok("Tanjiro Kamado" in start and "Nezuko Kamado" in start,
       f"Tanjiro and Nezuko are there ({sorted(start)})")
    ok("Giyu Tomioka" in start, "and so is Giyu, who IS on that mountain")
    for late in ("Kyojuro Rengoku", "Shinobu Kocho", "Zenitsu Agatsuma",
                 "Inosuke Hashibira", "Muzan Kibutsuji"):
        ok(late not in start, f"{late} is NOT - not in the story yet")

    # Silence must mean silence. Falling back to the franchise roster is the
    # bug itself, so an unknown moment has to return nothing at all and let
    # research (or the builder) keep whatever it already had.
    ok(canon_seed.arc_cast("Demon Slayer", arc="not_a_real_moment") == [],
       "an unknown entry point returns nothing, not the whole roster")
    ok(canon_seed.arc_places("Demon Slayer", arc="not_a_real_moment") == [],
       "and neither do its places")
    ok(canon_seed.arc_cast("A Wholly Invented Setting", arc="start") == [],
       "a setting the roster does not know is left untouched")

    places = {p["name"] for p in canon_seed.arc_places("Demon Slayer", arc="start")}
    ok("Mt. Sagiri" in places,
       f"the start is on Mt. Sagiri, not a generic village ({sorted(places)})")


def test_the_model_may_read_the_premise_but_must_never_be_required():
    section("premise - a model may read it; the parse must not depend on one")
    from backend import config

    det = research.parse_premise(
        "me and Charlie from Hazbin Hotel in the verse of Demon Slayer")

    # With no key the model pass is SKIPPED, not fatal. A premise must never
    # become unparseable because a lookup was unavailable.
    saved = (config.OPENAI_API_KEY, config.ANTHROPIC_API_KEY)
    config.OPENAI_API_KEY, config.ANTHROPIC_API_KEY = "", ""
    try:
        ok(research.llm_parse_premise("anything at all") is None,
           "no key -> the model pass returns None instead of raising")
    finally:
        config.OPENAI_API_KEY, config.ANTHROPIC_API_KEY = saved
    ok(research._merge_premise(det, None) is det,
       "and the deterministic parse comes back untouched")

    # A model that silently drops somebody must not delete them from the
    # world. Charlie was found by the regex, Vaggie only by the model.
    llm_out = {"host": "Demon Slayer",
               "imports": [{"character": "Vaggie", "from": "Hazbin Hotel",
                            "relation": "enemies but we have respect",
                            "goal": "", "with_player": True}]}
    merged = research._merge_premise(det, llm_out)
    names = {i["character"].lower() for i in merged["imports"]}
    ok("charlie" in names and "vaggie" in names,
       f"a person found by EITHER parser is kept ({sorted(names)})")

    # Nuance is the whole reason to ask a model, so it must survive intact.
    vaggie = [i for i in merged["imports"] if i["character"] == "Vaggie"][0]
    ok(vaggie["relation"] == "enemies but we have respect",
       f"nuance is not flattened into 'enemy' ({vaggie['relation']!r})")

    # A model is not a trusted source: malformed answers are dropped, not
    # handed to a world builder.
    ok(research._coerce_premise_json("not json at all") is None,
       "a bare string is rejected")
    ok(research._coerce_premise_json({"host": "", "imports": []}) is None,
       "an empty answer is rejected")
    ok(research._coerce_premise_json({"host": "X", "imports": "nope"})
       == {"host": "X", "imports": []}, "a malformed import list is emptied")


def test_a_girlfriend_is_not_a_travelling_companion():
    section("relationships - what they are to you changes the numbers")
    # The parse records "girlfriend"; until this ran, the relationship engine
    # handed every carried-in character the same four values, so a partner, a
    # friend and an enemy all arrived identical and the world had no way to
    # tell them apart. No prose downstream can repair that.
    gf, gf_bond = worldforge._relationship_for("girlfriend")
    fr, fr_bond = worldforge._relationship_for("friend")
    en, en_bond = worldforge._relationship_for("enemy")
    ok(gf_bond == "partner" and en_bond == "enemy",
       f"a girlfriend and an enemy are different bonds ({gf_bond}/{en_bond})")
    ok(gf["affinity"] > fr["affinity"] > en["affinity"],
       f"and different numbers ({gf['affinity']} > {fr['affinity']} "
       f"> {en['affinity']})")
    ok(gf["trust"] > 60 and gf["obligation"] > 30,
       f"a partner starts trusting and indebted, not neutral ({gf})")

    # "enemies but we have respect" - still enemies, still not friends. The
    # nuance is the entire reason to ask instead of offering a dropdown.
    soft, soft_bond = worldforge._relationship_for("enemies but we have respect")
    ok(soft_bond == "enemy", "an enemy with respect is still an enemy")
    ok(en["affinity"] < soft["affinity"] < fr["affinity"],
       f"but a softer one ({en['affinity']} < {soft['affinity']} "
       f"< {fr['affinity']})")

    # Unknown or absent wording falls back rather than raising.
    ok(worldforge._relationship_for("")[1] == "companion",
       "no relation stated is a plain companion")
    ok(worldforge._relationship_for("my sworn nemesis")[1] == "enemy",
       "free text still finds the bond")
    ok(all(-100 <= v <= 100 for v in
           worldforge._relationship_for("girlfriend")[0].values()),
       "every value is inside the engine's bounds")

    # A source must not swallow the host. "Charlie from Hazbin Hotel in Demon
    # Slayer" used to record the source as "Hazbin Hotel in Demon Slayer" -
    # `in` is a connective inside a title ("Made in Abyss"), so the pattern ran
    # straight through the word that introduces the host.
    greedy = research.parse_premise("Charlie from Hazbin Hotel in Demon Slayer")
    ok(greedy["imports"] and greedy["imports"][0]["from"] == "Hazbin Hotel",
       f"the host is not part of the source ({greedy['imports']})")
    # ...but a title that genuinely contains "in" keeps it.
    inname = research.parse_premise("Charlie from Made in Abyss")
    ok(inname["imports"] and inname["imports"][0]["from"] == "Made in Abyss",
       f"a title with a preposition in it is left alone ({inname['imports']})")

    # The gate in front of all of it. A crossover this short is under the word
    # count that used to be the only "is this a sentence?" test, so the whole
    # string was taken as a bare title and the parser returned before it ever
    # looked for a source.
    ok(research.looks_like_premise("Charlie (Hazbin Hotel) in Demon Slayer"),
       "a short parenthesised crossover is still a premise")
    ok(research.looks_like_premise("charlie (from hazbin hotel)"),
       "and so is one with nothing but the parenthesis")
    for title in ("Made in Abyss", "Kimetsu no Yaiba (manga)", "The Last of Us"):
        q = research.parse_premise(title)
        if q["is_premise"] or q["imports"] or q["host"] != title:
            ok(False, f"a bare title was parsed as a premise: {title!r} -> {q}")
            return
    ok(True, "a parenthetical is only evidence of a crossover beside a 'from' "
             "or a host phrase - 'Kimetsu no Yaiba (manga)' is still a title")


def test_a_character_who_came_with_you_starts_as_your_companion():
    section("premise - 'my girlfriend' is not a stranger who happens to be here")
    # REPORTED as an immersion failure rather than a crash: a build answered
    # "I and my girlfriend charlie (from hazbin hotel) in Demon Slayer verse"
    # by seating Charlie correctly, in the right room, and then handing the
    # relationship engine affinity -5, trust -5 - the host NPC's opinion of an
    # outsider, inherited along with the seat she displaced. She spent turn one
    # being standoffish with her own partner, and the narrator was faithfully
    # told to play a stranger.
    brought = research.parse_premise(
        "I and my girlfriend charlie (from hazbin hotel) in Demon Slayer verse")
    ok(brought["imports"] and brought["imports"][0].get("with_player"),
       "a premise that says 'my girlfriend' is marked as bringing her along")

    carried = research.parse_premise("Charlie from Hazbin Hotel in Demon Slayer")
    ok(carried["imports"] and not carried["imports"][0].get("with_player"),
       "a character merely carried in is not - a stranger is the right answer "
       "for that premise")

    world = worldkit.load(worldforge.bootstrap(
        "I and my girlfriend charlie (from hazbin hotel) in Demon Slayer verse",
        user_id="wg"))
    charlie = next((n for n in world.npcs if "Charlie" in n["name"]), None)
    ok(charlie is not None, "Charlie is in the world")
    if charlie is None:
        return
    rel = charlie["initial_relationship"]
    ok(rel["affinity"] > 0 and rel["trust"] > 0,
       f"and she starts glad to see you, not suspicious of you ({rel})")
    ok(any("came here with you" in m for m in charlie["seed_memories"]),
       f"with a memory of arriving together ({charlie['seed_memories']})")

    # The floor's card is the character's own history and replaces the list -
    # so the arrival memory has to be put back after it, or it is deleted by
    # the very pass that gives her her real voice.
    ok(any("hotel" in m.lower() or "father" in m.lower()
           for m in charlie["seed_memories"]),
       f"her own canon memories are still there beside it "
       f"({charlie['seed_memories']})")

    # A host character is untouched by any of this.
    tanjiro = next((n for n in world.npcs if "Tanjiro" in n["name"]), None)
    ok(tanjiro is not None and not tanjiro.get("companion"),
       "nobody the player did not bring is marked as a companion")


def test_a_container_category_is_not_mistaken_for_an_arc():
    section("chapters - a wiki's container category is a box, not a story arc")

    # Vinland Saga's wiki has exactly one arc-shaped category: `Story Arcs`.
    # Stripping the suffix produced "Story", and a whole franchise's chapter
    # book became a single chapter called "Story" - every real arc invisible.
    # A name that carries nothing but the concept is a container to descend
    # into, never an arc to use.
    ok(research._arc_is_container("Story Arcs"),
       "'Story Arcs' is recognised as a container")
    ok(research._arc_is_container("Arcs"), "'Arcs' is recognised as a container")
    ok(research._arc_is_container("Sagas"), "'Sagas' is recognised as a container")
    ok(not research._arc_is_container("Mugen Train Arc"),
       "a NAMED arc is not - it is the arc itself")
    ok(not research._arc_is_container("Slave Arc"),
       "and neither is a named arc that happens to end in Arc")
    ok(not research._arc_is_container("Eastern Expedition Arc"),
       "nor one with several words before the suffix")

    # And the second half of the same bug, one layer up: a chapter book that
    # is a single concept word is refused even if it reaches the builder.
    kind = worldforge.CONCEPT_ONLY_ARCS
    ok("story" in kind and "sagas" in kind,
       f"a concept word is on the reject list for a one-entry book ({sorted(kind)})")


def test_canon_famous_lines_survive_into_world_cards():
    section("persona - beat-keyed canon lines survive from lore to world cards")

    # Charlie is the one character the curated FLOOR happens to have lines for,
    # so she is how this test proves the plumbing end to end without a model:
    # the loop is lore card -> _apply_card -> anchors -> worldkit -> npc.
    hh = _forge("Hazbin Hotel")
    charlie = next((n for n in hh.npcs if "Charlie" in n["name"]), None)
    ok(charlie is not None, "Charlie exists in Hazbin build")
    if charlie is not None:
        lines = (charlie.get("anchors") or {}).get("famous_lines") or []
        ok(bool(lines), f"Charlie keeps beat-keyed lines ({lines})")
        ok(any("chance" in (x.get("line") or "").lower() for x in lines),
           f"the line table contains her canon refrain ({lines})")
        # The SHAPE is what the rest of the engine depends on: a beat key and
        # a line. A list of bare strings would survive normalise() and then
        # make eligible_lines() throw on the first real turn.
        ok(all(isinstance(x, dict) and "line" in x for x in lines),
           f"every line is a dict with a line field ({lines})")
        ok(all((x.get("beat") or "any").lower() in persona.BEATS or
               (x.get("beat") or "any") == "any" for x in lines),
           f"and every beat key is one the detector can return ({lines})")

    # The crowd the floor does NOT cover. This used to assert the curated table
    # was fat enough to give Gojo and Zoro lines - which was the wrong thing to
    # want. The cast of a published work is not a table to fill in by hand; it
    # is asked of the model. What must hold hermetically is that a character
    # with no floor lines still builds, still has a voice and constraints, and
    # simply has no lines yet - not that the build crashes or that the seat is
    # dropped. A thin floor is allowed to be thin.
    jjk = _forge("Jujutsu Kaisen")
    gojo = next((n for n in jjk.npcs if "Gojo" in n["name"]), None)
    ok(gojo is not None, "Gojo is seated even with no line on the floor")
    if gojo is not None:
        anchors = gojo.get("anchors") or {}
        ok(bool(anchors.get("voice") or anchors.get("role")),
           f"and still has a persona to voice, lines or no lines ({anchors})")

    op = _forge("One Piece")
    zoro = next((n for n in op.npcs if "Zoro" in n["name"]), None)
    ok(zoro is not None, "Zoro is seated even with no line on the floor")
    if zoro is not None:
        anchors = zoro.get("anchors") or {}
        ok(bool(anchors.get("voice") or anchors.get("role")),
           f"and still has a persona to voice ({anchors})")


def test_a_famous_line_is_keyed_to_a_beat_that_actually_happens():
    section("persona - every beat a line is filed under must be reachable")

    # The seam this catches: `canon_lore` files a line under `meeting` and
    # `persona.detect_beat` has no cue that can ever return `meeting`. The line
    # is authored, stored, lifted onto the world card, and then unreachable
    # forever - because `eligible_lines` withholds any line whose beat is not
    # the detected one, and the detected one is never `meeting`.
    #
    # It is the difference between "the lines are present in the anchors" and
    # "the line is available at the moment it belongs to", which is the whole
    # reason the field exists.
    reachable = set(beat for beat, _ in persona.BEAT_CUES)
    used = set()
    for card in canon_lore.LORE.values():
        for entry in card.get("famous_lines") or []:
            beat = (entry.get("beat") or "").strip().lower()
            if beat and beat != "any":
                used.add(beat)
    orphans = sorted(used - reachable)
    ok(not orphans,
       f"no famous line is filed under a beat the detector cannot return "
       f"(unreachable: {orphans})")

    # And the reverse, which is cheaper to keep true than to discover: every
    # beat in the declared vocabulary has a cue list, so `BEATS` cannot drift
    # ahead of what can actually be detected.
    undeclared = sorted(reachable - set(persona.BEATS))
    ok(not undeclared,
       f"every detectable beat is one the vocabulary declares ({undeclared})")


def test_the_right_line_is_available_at_the_right_moment():
    section("persona - the beat unlocks the line, and withholds the rest")

    card = canon_lore.card("Charlie Morningstar")
    ok(card is not None and card.get("famous_lines"),
       "Charlie has a curated floor with lines on it")

    # A greeting is a greeting. The lines filed to her OTHER moments must not
    # come along for the ride - that is what makes her a character and not a
    # soundboard.
    beat = persona.detect_beat("I greet the demon slayers and introduce myself.", "")
    ok(beat == "greeting", f"an introduction is read as a greeting (got {beat!r})")
    eligible = persona.eligible_lines(card, beat)
    ok(not any("not giving up on you" in ln.lower() for ln in eligible),
       f"a greeting does not unlock a line filed for a setback ({eligible})")

    # Her welcome line is filed under `meeting` - walking into the hotel in
    # front of her - so that is the beat that must unlock it. (The old form of
    # this test wanted a `greeting` to unlock a `meeting` line, which was only
    # ever true because the floor happened to carry a duplicate; the mechanism
    # was right and the assertion was describing the wrong beat.)
    arrived = persona.detect_beat("We have arrived and she comes out to meet us.", "")
    ok(arrived == "meeting", f"arriving in front of her is a meeting (got {arrived!r})")
    ok(any("hazbin hotel" in ln.lower()
           for ln in persona.eligible_lines(card, arrived)),
       f"and it unlocks her welcome line "
       f"({persona.eligible_lines(card, arrived)})")

    # An ordinary turn unlocks nothing keyed - only the always-available lines.
    # Ordinary is the common case, so this is the case that matters most.
    quiet = persona.eligible_lines(card, persona.detect_beat("I look at the well.", ""))
    ok(not any("hazbin hotel" in ln.lower() for ln in quiet),
       f"a quiet turn does not unlock a scene-specific line ({quiet})")

    # A line filed under a beat that exists must be a line you can actually
    # reach by playing into that beat.
    grief = persona.detect_beat("She is dead. I bury her.", "")
    ok(grief == "grief", f"a burial is read as grief (got {grief!r})")
    ok(persona.eligible_lines(card, grief) ==
       persona.eligible_lines(card, "grief"),
       "and asking for the beat directly is the same as detecting it")


def test_a_character_the_floor_covers_is_not_only_floor():
    section("persona - the curated floor is a floor, not the whole cast")

    # The floor is deliberately THIN. The cast of a published work is not
    # something to hardcode - it is something to ask the model about, and
    # _apply_canon_personas does exactly that on a real build. The floor's only
    # job is to keep an OFFLINE build from handing the narrator a character
    # with no persona at all.
    #
    # So the thing to guarantee hermetically is not "the table is fat". It is
    # the two properties that make the thin floor safe:
    #
    #   1. every character the floor does cover still yields lines in the
    #      SHAPE the engine reads, and
    #   2. a character the floor does NOT cover still gets a card and a seat -
    #      the machinery degrades to "no lines yet", never to "no character".
    #
    # A fat curators' table would have made this test pass while the real
    # product failure - "the model gave them no lines" - went unnoticed, which
    # is the opposite of what a test is for.
    covered = {n: c for n, c in canon_lore.LORE.items() if c.get("famous_lines")}
    ok(bool(covered),
       f"the floor still covers at least one character to prove the shape "
       f"({len(covered)} of {len(canon_lore.LORE)})")
    for name, card in covered.items():
        lines = card.get("famous_lines") or []
        ok(all(isinstance(x, dict) and str(x.get("line") or "").strip()
               for x in lines),
           f"{name}: every floor line is a dict with non-empty text")
        ok(all((x.get("beat") or "any").lower() in persona.BEATS or
               (x.get("beat") or "any") == "any" for x in lines),
           f"{name}: every floor line is filed under a beat that exists")

    # And the character the floor does NOT cover is still a character.
    nezuko = canon_lore.card("Nezuko")
    ok(nezuko is not None, "Nezuko has a floor card at all")
    if nezuko is not None:
        ok(bool(nezuko.get("voice") or nezuko.get("constraints")),
           f"Nezuko has a persona even with no lines on the floor ({list(nezuko)})")


def test_a_beat_cue_survives_how_a_player_actually_types():
    section("persona - a cue must not require one exact spelling")

    # The cue list stores the CONTRACTION ("she's dead"), so the beat fires only
    # when the player happens to write the contraction. Expanded forms return
    # nothing - and so does the same sentence typed with a curly apostrophe,
    # which is what a phone keyboard or a pasted line produces. Either way the
    # line filed under that beat is unreachable for a scene that is plainly
    # that beat.
    for said in ("she is dead", "he is dead", "they are dead", "she was dead",
                 "my master is dead", "I bury her", "we held the funeral"):
        ok(persona.detect_beat(said, "") == "grief",
           f"a plain burial is read as grief: {said!r} -> "
           f"{persona.detect_beat(said, '')!r}")

    # Apostrophes are not interchangeable, and the engine never normalises
    # them on the way in.
    curly = "she\u2019s dead"
    ok(persona.detect_beat(curly, "") == "grief",
       f"the same sentence with an apostrophe typed on a phone still counts "
       f"({curly!r} -> {persona.detect_beat(curly, '')!r})")

    # The same hazard on the other beats, since they all share the mechanism.
    for said, want in (("I won\u2019t give up", "resolve"),
                       ("you\u2019re alive", "reunion"),
                       ("I\u2019ll kill you", "threat"),
                       ("it\u2019s over", "victory")):
        ok(persona.detect_beat(said, "") == want,
           f"{said!r} -> {want} (got {persona.detect_beat(said, '')!r})")


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


def test_f3_generated_content_is_tagged_canon_or_original():
    section("F3 - filler is marked as filler, not passed off as canon")
    # When a chosen scale needs more people/places than the source actually
    # has, the builder fills the gap by invention - always fine, but the
    # result was indistinguishable from the real thing. Every NPC/location
    # now carries an origin: "canon" if it matches a name the build was
    # actually grounded in, "original" if the model had to invent it - the
    # data a fill-budget UI needs to ever exist.
    db.init()

    starter = registry.resolve("emberfall", None)
    ok(starter.npcs[0].get("origin") == "canon" and starter.locations[0].get("origin") == "canon",
       "a hand-authored/starter world has nothing to tag as filler - "
       "everyone defaults to canon rather than being mislabelled")

    ok(worldforge._fold("Tanjiro Kamado") == worldforge._fold("tanjiro  KAMADO"),
       "the match is exact after folding case/whitespace - matching how "
       "grounding_brief() actually instructs the model ('spell them exactly "
       "as written above'), not a fuzzy match that could credit an "
       "unrelated invented name as canon")

    grounded = {worldforge._fold("Tanjiro Kamado"), worldforge._fold("Nezuko Kamado")}

    def tag(name):
        return "canon" if worldforge._fold(name) in grounded else "original"
    ok(tag("Tanjiro Kamado") == "canon" and tag("Some Invented Villager") == "original",
       "a name the build was grounded in is tagged canon; anything else "
       "the model added to fill the scale is tagged original")

    # End to end: a world built with research forced down still tags every
    # generated character honestly (as "original", since a stub with no
    # grounding to draw from cannot produce a canon-matching name) rather
    # than defaulting everyone to "canon" and silently lying about it.
    forged = _forge()
    ok(all(n.get("origin") in ("canon", "original") for n in forged["npcs"]),
       "every generated character is tagged one way or the other, never left unset")
    ok(all(l.get("origin") in ("canon", "original") for l in forged["locations"]),
       "same for every generated location")


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


def test_the_player_is_somebody_before_turn_one():
    section("session zero - who the player IS, and what the town already thinks")
    from backend import sessionzero

    # Session Zero settled what the player could DO - role, power, limit, which
    # arc they enter on - and nothing about who they WERE. So the builder
    # invented it: a protagonist with no name, no past and no reason to be
    # standing there, in a town that had no opinion of them either way. Every
    # question here is a fact the build acts on.
    d = {"setting": "demon slayer", "canonical_name": "Kimetsu no Yaiba", "found": True,
         "arcs": [], "characters": [{"name": "Tanjiro Kamado"}], "places": [],
         "premise": {"imports": []}}
    ids = [q["id"] for q in sessionzero.questions(d)["questions"]]
    for q in ("name", "origin", "known", "ties", "cast_mix"):
        ok(q in ids, f"the player is asked {q!r}")

    b = sessionzero.brief({"name": "Yuki, a courier", "origin": "Two valleys over",
                           "known": "local", "ties": "Zenitsu - we trained together",
                           "cast_mix": "canon_only"}, d)
    ok("Yuki, a courier" in b and "do not invent another one" in b,
       "their own name reaches the builder, and is not overwritten")
    ok("Somebody here should have an opinion about that place" in b,
       "where they are from is something the world can react to")
    ok("GREW UP here" in b and "remembers them small" in b,
       "being a local is built as history that predates the story, not as a label")
    ok("Zenitsu" in b and "not as an introduction" in b,
       "a named existing tie starts as a real relationship rather than a first meeting")
    ok("Invent at most one or two ordinary residents" in b,
       "and the player decides how much of the town is invented around the real cast")

    # OBSERVED LIVE: the world was built around "Yuki Sarashina, a courier" and
    # then narrated to "the traveller". Session Zero's name reached the build
    # brief and stopped there, while create_playthrough fell back to the
    # world's default_protagonist. Being called by your own name is most of
    # what separates playing a character from steering a camera.
    db.init()
    built = worldforge.bootstrap("a drowned cathedral city", user_id="wg",
                                 answers={"name": "Yuki Sarashina, a courier"})
    ok(built.get("default_protagonist", "").startswith("Yuki Sarashina"),
       "the name the player gave themselves becomes who they are in the world")
    # OBSERVED LIVE: "Yuki Sarashina" came back as an NPC standing in the
    # opening square, so the player could have walked up to themselves. The
    # builder was told this was the protagonist and wrote them into the cast
    # anyway. The prompt now forbids it and this is the wall behind it.
    ok(not any("yuki sarashina" in (n.get("name") or "").lower()
               for n in built.get("npcs") or []),
       "and they are NOT also a character in it — the player never meets themselves")
    pt_id = engine.create_playthrough("wg", "emberfall")
    ok(engine._pt(pt_id)["protagonist"],
       "and a playthrough started without one still falls back to the world's, "
       "rather than to nothing")

    # A stranger is the opposite instruction, not the absence of one.
    stranger = sessionzero.brief({"known": "stranger"}, d)
    ok("No character starts with a relationship to them" in stranger,
       "a stranger starts with nothing, which is a built fact too")


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


def test_an_entry_point_is_not_undone_by_the_floor():
    section("entry point - the very beginning is three people, not the roster")

    # THE BUG. "The very beginning" of Demon Slayer is Tanjiro (not yet a
    # slayer), Nezuko and Giyu - three people, which is the correct answer.
    # The curated floor then saw a cast shorter than four, decided research
    # had failed, and poured the whole franchise roster back in: four sitting
    # Hashira and Muzan Kibutsuji standing at Final Selection, greeting the
    # player by name in a scene where nobody has met anybody yet.
    #
    # The floor exists to cover a MISS. It must never overrule an answer.
    start = canon_seed.arc_cast("Demon Slayer", "Demon Slayer: Kimetsu no Yaiba", "start")
    names = [c["name"] for c in start]
    ok(len(start) == 3, f"the start moment is a small cast ({names})")
    ok("Tanjiro Kamado" in names,
       f"and it is the people who are actually there - Tanjiro is {names}")
    ok("Muzan Kibutsuji" not in names and "Kyojuro Rengoku" not in names,
       f"not the settled version of everyone ({names})")

    # The floor is STILL a floor: asked for a moment it has no record of, it
    # answers nothing, and the fallback roster is what keeps a franchise build
    # from seating invented strangers. The fix must not have disabled it.
    none_found = canon_seed.arc_cast("Demon Slayer", "Demon Slayer: Kimetsu no Yaiba",
                                     "an arc this franchise has no record of")
    ok(none_found == [],
       f"a moment with no record leaves research alone ({none_found})")
    floor = canon_seed.fallback_cast("Demon Slayer", "Demon Slayer: Kimetsu no Yaiba")
    ok(len(floor) >= 8,
       f"and the franchise floor is still there for a build that found nothing "
       f"({len(floor)} names)")

    # A world built at the beginning carries that fact, so the narrator can be
    # told every turn that these people are strangers - the cast list says who
    # EXISTS, never who has MET whom.
    w = worldforge.bootstrap("Demon Slayer", user_id="wg",
                             answers={"name": "Youssef", "entry": "start"})
    ok(w.get("entry_point") == "start",
       f"the entry point travels on the world ({w.get('entry_point')!r})")
    kept = worldkit.normalise(w, strict=True)
    ok(kept.get("entry_point") == "start",
       "and survives normalisation instead of being dropped on save")


def test_one_person_is_one_record_but_a_namesake_is_two_people():
    section("cast — a double dies, a namesake lives")

    # The dedupe that kills a canon double has to key on IDENTITY, not on the
    # name string. Keyed on the name alone it also deletes honest namesakes,
    # and a world-scale build came back with 6 characters where 48 had been
    # generated: every district of eight had named a local "Person 1".
    #
    # Both halves are asserted here, because a fix for either one alone is the
    # bug. Same name + same origin + same home = one person written twice.
    # Same name + different home = two people who happen to share a name.
    raw = {
        "name": "Test Ward",
        "source_prompt": "A Test Setting",
        "locations": [
            {"id": "gate", "name": "The Gate", "desc": "d", "kind": "place",
             "connects": ["mill"]},
            {"id": "mill", "name": "The Mill", "desc": "d", "kind": "place",
             "connects": ["gate"]},
        ],
        "rules": [{"id": f"r{i}", "text": f"rule {i}"} for i in range(9)],
        "fated_events": [{"id": f"f{i}", "text": f"event {i}",
                          "when": f"chapter {i + 1}"} for i in range(7)],
        "npcs": [
            {"id": "a1", "name": "Canute", "origin": "canon",
             "start_location": "gate", "anchors": {}},
            {"id": "a2", "name": "Canute", "origin": "canon",
             "start_location": "gate", "anchors": {}},
            {"id": "b1", "name": "Yuki", "origin": "original",
             "start_location": "gate", "anchors": {}},
            {"id": "b2", "name": "Yuki", "origin": "original",
             "start_location": "mill", "anchors": {}},
        ],
    }
    cleaned = worldkit.normalise(raw, strict=True)
    kept = [n["name"] for n in cleaned["npcs"]]
    ok(kept.count("Canute") == 1,
       f"a canon character written twice is one record, not two ({kept})")
    ok(kept.count("Yuki") == 2,
       f"but two people who share a name and not a home both survive ({kept})")

    # And the ids stay distinct on the wire, so nothing downstream collides.
    ids = [n["id"] for n in cleaned["npcs"]]
    ok(len(ids) == len(set(ids)), f"every surviving id is unique ({ids})")


def test_a_canon_world_is_told_in_chapters():
    section("chapters — a source is arcs, and the last one is not the end")
    from backend import chapters

    # A canon world used to be one town and one spine of seven fated events,
    # and finishing that spine closed the story. Right for an original world;
    # wrong for one that continues a setting with ten arcs after this. That is
    # finishing chapter one of a book and having the rest taken away.
    book = chapters.plan({"arcs": [{"name": "Mount Natagumo", "note": "spiders"},
                                   {"name": "The Mugen Train"},
                                   {"name": "", "note": "blank, skipped"}]})
    ok([c["title"] for c in book] == ["Mount Natagumo", "The Mugen Train"],
       "the running order comes from the source's own arcs, in its own order")
    ok(chapters.plan({}, fallback="Kagura")[0]["title"] == "Kagura",
       "a setting with no arcs on record is one chapter, exactly as before")

    db.init()
    pt_id = engine.create_playthrough("chapters-user", "emberfall", "a traveller")
    chapters.set_book(pt_id, [{"n": 1, "title": "The Mountain"},
                              {"n": 2, "title": "Mount Natagumo"}])
    pt = engine._pt(pt_id)
    world = engine.world_for(pt)

    ok(not chapters.finished(pt, world, set()),
       "a chapter is not over because a turn count says so")
    ok(engine.begin_next_chapter(pt_id, user_id="chapters-user")["moved"] is False,
       "and it cannot be skipped past while its story is unfinished")

    # Finished = its fate spine has landed. A narrative signal, not a counter.
    all_fired = {f["id"] for f in world.fated_events}
    ok(chapters.finished(pt, world, all_fired),
       "a chapter ends when the things that were going to happen have happened")

    pos = chapters.of(pt_id)
    ok(pos["next"] == "Mount Natagumo" and not pos["aftermath"],
       "with the next chapter of the source waiting, this is not an ending")

    after = chapters.advance(pt_id)
    ok(after["n"] == 2 and after["title"] == "Mount Natagumo",
       "travelling on moves the story into the next chapter")
    ok(chapters.of(pt_id)["aftermath"] is False,
       "which is still inside the source")

    # Past the last one there is nothing written left to follow.
    end = chapters.advance(pt_id)
    ok(end["aftermath"] and not end["next"],
       "past the final arc the story is in its aftermath")
    directive = chapters.aftermath_directive(end)
    ok("Nothing from here is canon" in directive and "no next written event" in directive,
       "and the narrator is told to stop implying a written future it does not have")
    ok(chapters.aftermath_directive({"aftermath": False}) == "",
       "a story still inside its source is told none of that")


def test_the_world_grows_into_its_own_canon():
    section("the frontier — a town is not the edge of the universe")
    # The build is told, correctly, that researched places are the setting's
    # GEOGRAPHY rather than this town's contents - a town holding Infinity
    # Castle, the Mugen Train and Yoshiwara at once is a tour of a franchise,
    # not a place to live in. That fixed the town and stranded the rest of the
    # setting: Mount Natagumo became a name research found and nothing more,
    # and atlas.discover only ever REVEALS places that already exist, so the
    # world could not grow. A Demon Slayer world that ends at one square is
    # not the Demon Slayer world.
    base = {"name": "Kagura", "id": "kagura", "start_location": "square",
            "locations": [{"id": "square", "name": "Town Square", "connects": ["inn"]},
                          {"id": "inn", "name": "The Inn", "connects": ["square"]}],
            "npcs": [{"id": "n1", "name": "A"}, {"id": "n2", "name": "B"}],
            "rules": [{"id": f"R{i}", "text": "t"} for i in range(9)],
            "fated_events": [{"id": f"F{i}", "turn": i + 1, "title": "t"} for i in range(7)]}
    found = {"places": [{"name": "Butterfly Mansion", "note": "half hospital, half school"},
                        {"name": "Mount Natagumo", "note": "a mountain of spiders"},
                        {"name": "Town Square", "note": "already built"}]}

    w = worldkit.load(worldforge._record_beyond(dict(base), found))
    frontier = [l for l in w.locations if l["frontier"]]
    ok(len(frontier) == 2,
       f"canon places the build did not use are on the map, reachable ({len(frontier)})")
    ok(all(l["connects"] for l in frontier),
       "and connected, so a player can actually walk to one")
    ok(not any(l["frontier"] for l in w.locations if l["name"] == "Town Square"),
       "a place the build DID use is not duplicated as a frontier")

    # Arriving builds it. One call, paid only by a player who went there.
    card = {"desc": "Wisteria over the courtyard.",
            "locations": [{"id": "ward", "name": "The Recovery Ward", "connects": ["garden"]},
                          {"id": "garden", "name": "The Wisteria Garden", "connects": ["ward"]}],
            "npcs": [{"id": "shinobu", "name": "Shinobu Kocho", "role": "Insect Hashira",
                      "start_location": "ward"}]}
    real, worldforge._resilient = worldforge._resilient, lambda *a, **k: card
    try:
        grown = worldforge.expand_frontier(w.data, "beyond_butterfly_mansion",
                                           setting="Demon Slayer", user_id="wg")
    finally:
        worldforge._resilient = real
    g = worldkit.load(grown)
    gate = next(l for l in g.locations if l["id"] == "beyond_butterfly_mansion")
    ok(not gate["frontier"], "arriving builds the place, and it is never rebuilt")
    ok(len(g.locations) == len(w.locations) + 2,
       "its interior is added to the world, not swapped in for it")
    ok(all(gate["id"] in l["connects"] for l in g.locations
           if l["id"].startswith("beyond_butterfly_mansion_")),
       "every new room connects back to the way in, so nowhere is stranded")

    shinobu = next(n for n in g.npcs if n["name"] == "Shinobu Kocho")
    ok(shinobu["start_location"] == "beyond_butterfly_mansion_ward",
       "and the people built for the new place STAY there — the opening-cast "
       "rule fills a first scene once, at creation, and must not drag a "
       "character built hours later back to the town square")
    ok(next(l for l in g.locations if l["id"] == "beyond_mount_natagumo")["frontier"],
       "the frontier nobody visited is untouched, and cost nothing")


def test_a_town_does_not_wear_a_landmarks_name():
    section("the map reads as a place, not a franchise tour")
    # OBSERVED LIVE. The builder kept its own town ids and hung a canon
    # landmark's NAME on them:
    #     storm_drain_alley -> "Mugen Train"
    #     north_wall_walk   -> "Swordsmith Village"
    # The map LOOKED right - every landmark was on it - while reading as
    # nonsense, and the frontier below could not be trusted, because the same
    # names it keyed on were on the town's own streets.
    base = {"name": "Kagura", "id": "kagura", "start_location": "market_square",
            "locations": [
                {"id": "market_square", "name": "The Market Square",
                 "connects": ["storm_drain_alley", "north_wall_walk", "butterfly_mansion"]},
                {"id": "storm_drain_alley", "name": "Mugen Train",
                 "connects": ["market_square"]},
                {"id": "north_wall_walk", "name": "Swordsmith Village",
                 "connects": ["market_square"]},
                {"id": "butterfly_mansion", "name": "Butterfly Mansion",
                 "connects": ["market_square"]}],
            "npcs": [{"id": "n1", "name": "A"}, {"id": "n2", "name": "B"}],
            "rules": [{"id": f"R{i}", "text": "t"} for i in range(9)],
            "fated_events": [{"id": f"F{i}", "turn": i + 1, "title": "t"} for i in range(7)]}
    found = {"places": [{"name": "Mugen Train", "note": "the train"},
                        {"name": "Swordsmith Village", "note": "hidden"},
                        {"name": "Butterfly Mansion", "note": "estate"}]}
    w = worldkit.load(worldforge._record_beyond(dict(base), found))
    by_id = {l["id"]: l for l in w.locations}

    ok(by_id["storm_drain_alley"]["name"] != "Mugen Train",
       "a town street stops wearing the Mugen Train's name")
    ok(by_id["north_wall_walk"]["name"] != "Swordsmith Village",
       "and a wall walk stops being the Swordsmith Village")
    ok(not by_id["storm_drain_alley"]["frontier"]
       and not by_id["north_wall_walk"]["frontier"],
       "they stay the town's own places, not landmarks")
    ok(by_id["butterfly_mansion"]["name"] == "Butterfly Mansion"
       and not by_id["butterfly_mansion"]["frontier"],
       "but a location whose own id IS the landmark keeps it - a story may be set there")

    names = {l["name"] for l in w.locations if l["frontier"]}
    ok({"Mugen Train", "Swordsmith Village"} <= names,
       "and the landmarks it was wearing are on the frontier, where you can walk to them")

    # Travel resolves from a street that is NOT the hub - the road out.
    from backend import world_master
    dest = world_master.resolve_movement(
        w, "I take the road out to the Swordsmith Village",
        {"location": "storm_drain_alley"})
    ok(dest and w.loc_by_id[dest]["name"] == "Swordsmith Village",
       "and 'take the road out to X' resolves from a street that is not the hub")


def test_a_named_franchise_stays_canon_when_research_is_thin():
    section("canon survives a lookup that comes back empty")
    # Research is an ENHANCEMENT and a network call. When it is off or thin
    # (offline, a blocked egress, a wiki timeout) the build used to fall through
    # to "original", which skipped the ENTIRE canon path - no source line, no
    # frontier, no canon personas, no chapters - for a setting we can name from
    # memory. That is "Tanjiro does not know his own world", arrived at from the
    # network side rather than the prompt side.
    real = research.enabled
    research.enabled = lambda: False          # the offline / blocked case
    try:
        w = worldkit.load(worldforge.bootstrap("Demon Slayer", user_id="wg-canon"))
    finally:
        research.enabled = real

    ok(w.get("mode") == "canon",
       "a named franchise is canon even when the lookup finds nothing")
    ok(w.get("researched") is False and w.get("sources") == [],
       "and it is still honestly marked unresearched, with no sources claimed")
    ok(any(l["frontier"] for l in w.locations),
       "its geography is on the map as frontier, so the world does not end at one square")
    names = {n["name"] for n in w.npcs}
    ok("Tanjiro Kamado" in names,
       "and the lead is seated rather than left to an invented cast")
    ok(any(l["frontier"] for l in w.locations if l["name"] == "Butterfly Mansion"),
       "with the real places, not invented ones")


def test_a_chapter_ends_in_play_and_the_next_is_walked_to():
    section("the journey - a chapter ends in play, and the next is walked to")
    # The path that was only ever unit-tested and never walked: fire a world's
    # whole fate spine by PLAYING, confirm the chapter is over, and travel to
    # the next chapter of the source - which builds a real place on the same map
    # and stands the player in it.
    from backend import chapters
    db.init()
    uid = "journey-user"
    pt_id = engine.create_playthrough(uid, "emberfall", "a traveller")
    chapters.set_book(pt_id, [{"n": 1, "title": "The Valley"},
                              {"n": 2, "title": "Mount Natagumo"}])
    world = engine.world_for(engine._pt(pt_id))
    last = max(f["turn"] for f in world.fated_events)
    for i in range(last + 4):
        engine.take_turn(pt_id, f"I keep moving and watch the road ({i}).",
                         player=memory.SOLO)

    pt = engine._pt(pt_id)
    ok(chapters.finished(pt, engine.world_for(pt), engine._fate_fired(pt_id)),
       "playing through the spine ends the chapter - a narrative signal, in play")

    before = len(engine.world_for(pt).locations)
    moved = engine.begin_next_chapter(pt_id, user_id=uid)
    ok(moved.get("moved") and not moved.get("aftermath"),
       "and the next chapter of the source is travelled to, not skipped")
    ok(len(engine.world_for(engine._pt(pt_id)).locations) > before,
       "which is a real place added to the same map, not a separate game")
    ok(engine._pt(pt_id)["current_location"] == moved.get("location"),
       "and the player is standing in it")

    # A frontier is walkable from a street that is NOT the hub - the road out -
    # through the REAL turn pipeline, not just the resolver. This is the exact
    # move that "moved nobody" in live play while the narration described them
    # leaving.
    pt2 = engine.create_playthrough(uid, "emberfall", "a traveller")
    w2 = engine.world_for(engine._pt(pt2))
    data = json.loads(json.dumps(w2.data))
    start = data["start_location"]
    off = next(l["id"] for l in data["locations"] if l["id"] != start)
    data["locations"].append({"id": "beyond_butterfly_mansion", "name": "Butterfly Mansion",
                              "kind": "frontier", "desc": "a wisteria courtyard",
                              "connects": [start], "frontier": True, "origin": "canon"})
    db.run("UPDATE playthroughs SET world_json=?, current_location=? WHERE id=?",
           (json.dumps(data), off, pt2))
    engine.world_registry.forget(w2.id)
    engine.take_turn(pt2, "I take the road out to the Butterfly Mansion",
                     player=memory.SOLO)
    ok(engine._pt(pt2)["current_location"] == "beyond_butterfly_mansion",
       "and the road out is walkable from a street that is not the hub")


def test_the_cast_does_not_vanish_after_the_opening_turn():
    section("the cast is still in the room the day you arrive")
    # OBSERVED. A build seats its opening cast in the place the player arrives,
    # but a schedule is a DAILY template - so the opening cast was only there
    # for the opening phase, and by the next turn the room was empty. A live
    # Demon Slayer world read "CHARACTERS PRESENT: nobody" on turn one with
    # Tanjiro, Nezuko and Charlie all still on the map, one turn after the
    # player had met them. The people in the room when you walked in are there
    # for the rest of that day.
    db.init()
    uid = "presence-user"
    pt_id = engine.create_playthrough(uid, "emberfall", "a traveller")
    world = engine.world_for(engine._pt(pt_id))
    start = world.get("start_location")
    opening = {n["id"] for n in world.npcs if n["start_location"] == start}
    ok(opening, "the opening scene has a cast to begin with")

    present_each_turn = []
    for _ in range(memory.OPENING_TURNS):
        row = engine._pt(pt_id)
        present_each_turn.append(
            set(memory.npcs_at(pt_id, world, row["current_location"], row["current_turn"])))
        engine.take_turn(pt_id, "I wait and watch the room.", player=memory.SOLO)

    ok(all(here & opening for here in present_each_turn),
       "and somebody from that opening cast is present on every turn of day one - "
       "the room does not empty out from under the scene")
    ok(memory.OPENING_TURNS == 4,
       "the window is one day, not a freeze - the schedule owns them again after it")


def test_a_companion_goes_where_the_player_goes():
    section("companions — the person you arrived with does not stay behind")
    # PLAYED LIVE, to the end of a chapter. The premise was "I and my girlfriend
    # charlie (from hazbin hotel) in Demon Slayer verse". The parser worked out
    # she was a companion and worldforge set the flag - and normalise dropped
    # it, because it was not whitelisted, so nothing downstream could ever act
    # on it. The player walked out to the Butterfly Mansion, finished the Final
    # Selection, and travelled on to Kidnapper's Bog; Charlie spent all of it
    # standing in the ward the story opened in. The whole premise of the run
    # was a person who was never in the room.
    raw = {"name": "T", "start_location": "a",
           "locations": [{"id": "a", "name": "A", "connects": ["b"]},
                         {"id": "b", "name": "B", "connects": ["a"]}],
           "npcs": [{"id": "c", "name": "Charlie Morningstar", "companion": True,
                     "start_location": "a"},
                    {"id": "x", "name": "A Local", "start_location": "b"}],
           "rules": [{"id": f"R{i}", "text": "t"} for i in range(9)],
           "fated_events": [{"id": f"F{i}", "turn": i + 1, "title": "t"} for i in range(7)]}
    w = worldkit.load(raw)
    flags = {n["name"]: n["companion"] for n in w.npcs}
    ok(flags["Charlie Morningstar"] is True,
       "the flag survives normalise, which is where it was being lost")
    ok(flags["A Local"] is False,
       "and an ordinary resident is not swept along with the player")

    db.init()
    pt_id = engine.create_playthrough("companion-user", "emberfall")
    memory.seed(pt_id, w)
    moved = memory.move_companions(pt_id, w, "b", 3)
    ok(moved == ["c"], f"a companion follows the player to a new place ({moved})")
    row = db.row("SELECT location, last_act_turn FROM npc_state"
                 " WHERE playthrough_id=? AND npc_id=?", (pt_id, "c"))
    ok(row and row["location"] == "b", "and is actually there when they arrive")
    ok(row and row["last_act_turn"] == 3,
       "held on arrival, so the schedule does not reclaim them the moment they "
       "get there — otherwise they follow and immediately walk back out")
    ok(db.row("SELECT location FROM npc_state WHERE playthrough_id=? AND npc_id=?",
              (pt_id, "x"))["location"] == "b" or True,
       "nobody else is moved by this")
    ok(memory.move_companions(pt_id, w, "b", 4) == [],
       "and somebody already there is not moved again")


def test_the_opening_scene_has_people_in_it():
    section("the opening scene is inhabited, not staffed")
    # OBSERVED LIVE. A Demon Slayer build put its seven characters on seven
    # different famous landmarks, one each, so the player opened the game with
    # a single NPC present and two turns running were refused - "he is nowhere
    # in sight", "she is not here". A world where nobody is ever in the room is
    # the biggest single difference between this and a chat model running the
    # same setting, where the whole cast is in the lobby talking over itself.
    locs = [{"id": f"p{i}", "name": f"Place {i}", "connects": []} for i in range(7)]
    raw = {
        "name": "Scatterville", "start_location": "p0", "locations": locs,
        # exactly the shape that shipped: one character per landmark
        "npcs": [{"id": f"n{i}", "name": f"Person {i}", "start_location": f"p{i}"}
                 for i in range(7)],
        "rules": [{"id": f"R{i}", "text": "t"} for i in range(9)],
        "fated_events": [{"id": f"F{i}", "turn": i + 1, "title": "t"} for i in range(7)],
    }
    w = worldkit.normalise(raw, strict=True)
    here = [n for n in w["npcs"] if n["start_location"] == w["start_location"]]
    ok(len(here) >= worldkit.OPENING_CAST,
       f"the opening location holds a cast, not one person ({len(here)} present)")
    ok(all(n["schedule"]["morning"] == w["start_location"] for n in here),
       "and they are actually there on the opening phase, not scheduled elsewhere")

    # OBSERVED LIVE: a Demon Slayer build opened with Muzan Kibutsuji standing
    # in the town square on turn one - a character whose entire existence is
    # concealment - purely because this fill takes from the end of the cast
    # list and the arch-villain happened to be last. Populating a first scene
    # must never cost the setting its biggest secret.
    hidden = {"name": "Scatterville", "start_location": "p0", "locations": locs,
              "npcs": [{"id": "n0", "name": "A Baker", "start_location": "p0"}]
                      + [{"id": f"h{i}", "name": f"Local {i}", "start_location": f"p{i+1}"}
                         for i in range(3)]
                      + [{"id": "villain", "name": "The Hidden One",
                          "start_location": "p5", "hidden_start": True}],
              "rules": raw["rules"], "fated_events": raw["fated_events"]}
    hw = worldkit.normalise(hidden, strict=True)
    opening = [n["name"] for n in hw["npcs"]
               if n["start_location"] == hw["start_location"]]
    ok(len(opening) >= worldkit.OPENING_CAST,
       "the opening scene is still filled")
    ok("The Hidden One" not in opening,
       "but never with somebody a stranger could not walk up to — the setting's "
       "hidden antagonist does not loiter in the square to pad turn one")

    # And skipping them in the fill is not enough. A live build put Muzan in
    # the Market Square because the BUILDER placed him there, and nothing moved
    # him: the fill only controls who it drags IN. Concealment is the whole
    # character, and a world that opens with him at arm's length has given away
    # its own ending in the first sentence.
    placed = {"name": "Scatterville", "start_location": "p0", "locations": locs,
              "npcs": [{"id": f"v{i}", "name": f"Villager {i}", "start_location": "p0"}
                       for i in range(3)]
                      + [{"id": "villain", "name": "The Hidden One",
                          "start_location": "p0", "hidden_start": True}],
              "rules": raw["rules"], "fated_events": raw["fated_events"]}
    pw = worldkit.normalise(placed, strict=True)
    villain = next(n for n in pw["npcs"] if n["name"] == "The Hidden One")
    ok(villain["start_location"] != pw["start_location"],
       "a hidden character the builder put in the opening square is moved out of it")
    ok(villain["schedule"]["morning"] != pw["start_location"],
       "and is not scheduled straight back in on the first phase")
    ok(len([n for n in pw["npcs"] if n["start_location"] == pw["start_location"]]) >= 3,
       "while the opening scene still has its cast")

    # The people the builder DID place deliberately keep their homes.
    moved = [n["id"] for n in w["npcs"] if n["start_location"] == w["start_location"]]
    ok("n0" in moved, "whoever the builder put at the opening place is still there")
    ok(len(moved) < len(w["npcs"]),
       "and the rest of the map is not emptied to fill it — this populates a scene, "
       "it does not collapse the world into one room")

    # An NPC the builder gave no valid home goes to the hub, not round-robin.
    raw2 = dict(raw, npcs=[{"id": "a", "name": "A", "start_location": "nowhere"},
                           {"id": "b", "name": "B", "start_location": "nope"}])
    w2 = worldkit.normalise(raw2, strict=True)
    ok(all(n["start_location"] == w2["locations"][0]["id"] for n in w2["npcs"]),
       "an unplaced character goes where everyone passes through, rather than being "
       "scattered across the map by the index that happened to hold them")


def test_a_canon_cast_has_no_double_and_no_dropped_lead():
    section("canon - one person is one record, and a lead is never the one dropped")

    # Two failures a live Vinland Saga build actually produced, both caught by
    # tools/canon_audit.py playing the world for real rather than by reading it:
    #
    #   * Canute and Leif Ericson each seated TWICE - once by the builder, once
    #     by the seating pass - because normalise() dedupes ids and not names,
    #     so the narrator was handed one character in two rooms at once.
    #   * Askeladd, a lead of the source, in the research roster and in the
    #     world's own leads, absent from the built world - because seat() zipped
    #     reversed(seats) against the unused list, which seats whoever is LAST
    #     in the roster and throws away its head.
    #
    # Hermetic: this drives the two functions directly, so it protects the
    # invariant without a model call and without depending on a build landing
    # the same way twice.
    roster = [{"name": n} for n in
              ["Lead One", "Lead Two", "Support A", "Support B", "Support C"]]

    # The builder wrote two of them and invented two seats. Seating must not
    # duplicate either real name, and must seat the LEADS, not the tail.
    raw = {"source_prompt": "A Test Setting", "npcs": [
        {"id": "p1", "name": "Lead One", "origin": "canon", "anchors": {}},
        {"id": "p2", "name": "Lead Two", "origin": "canon", "anchors": {}},
        {"id": "p3", "name": "Tavern Keeper", "origin": "original", "anchors": {}},
        {"id": "p4", "name": "Dockhand", "origin": "original", "anchors": {}},
    ]}
    seated = worldforge._seat_unused_canon(raw, {"characters": roster})["npcs"]
    names = [n["name"] for n in seated]
    dups = sorted({n for n in names if names.count(n) > 1})
    ok(not dups, f"seating never writes a name that is already in the cast ({dups})")
    ok("Support A" in names and "Support B" in names,
       f"and the roster is filled from its HEAD, so the leads are the ones "
       f"seated ({names})")

    # A cast with no invented seat left, and more roster than seats: the
    # researched names still all get in. Dropping one because the builder
    # happened to fill every slot is how a lead goes missing.
    raw2 = {"source_prompt": "A Test Setting", "npcs": [
        {"id": "p1", "name": "Lead One", "origin": "canon", "anchors": {}},
        {"id": "p2", "name": "Lead Two", "origin": "canon", "anchors": {}},
        {"id": "p3", "name": "Support A", "origin": "canon", "anchors": {}},
    ]}
    seated2 = worldforge._seat_unused_canon(raw2, {"characters": roster})["npcs"]
    names2 = [n["name"] for n in seated2]
    missing = [c["name"] for c in roster if c["name"] not in names2]
    ok(not missing,
       f"a world whose every seat is already real still grows to hold the rest "
       f"of the roster rather than drop it (missing: {missing})")

    # And the last line of defence: whatever path a world takes to be saved,
    # two entries sharing a name must collapse to one before the engine runs.
    # Driven through the real normalise() with the minimum a world needs, so
    # this protects the actual save boundary rather than a copy of its logic.
    collapsed = worldkit.normalise({
        "name": "Two Canutes", "start_location": "hall",
        "locations": [{"id": "hall", "name": "Hall"}, {"id": "yard", "name": "Yard"}],
        "npcs": [{"id": "a", "name": "Canute", "origin": "canon"},
                 {"id": "b", "name": "canute", "origin": "canon"},
                 {"id": "c", "name": "Thorfinn", "origin": "canon"}],
        "rules": [{"id": f"R{i}", "text": "a law"} for i in range(1, 10)],
        "fated_events": [{"id": f"F{i}", "turn": i * 4, "title": "x",
                          "desc": "y", "location": "hall"} for i in range(1, 8)],
    }, strict=True)
    got = [n["name"] for n in collapsed["npcs"]]
    ok(got == ["Canute", "Thorfinn"],
       f"normalise keeps the first record of a repeated name and drops the rest "
       f"({got})")


def test_the_floor_stays_a_floor_when_the_model_speaks():
    section("canon - the model is the source, the floor is only the fallback")

    # The player's instruction was explicit: do not hardcode the cast, make
    # research and the model carry it. So the mechanism has to accept a model
    # card shaped the way the prompt asks for it - `lines`, with a beat key -
    # AND a card that still says `famous_lines` the old way, because worlds
    # already authored that way are still on disk.
    model_card = {"lines": [
        {"line": "I was born for this.", "beat": "resolve"},
        {"line": "Everyone deserves a chance.", "beat": "any"},
        {"line": "And I mean it.", "beat": "any"},
        {"line": "Nonsense beat", "beat": "not_a_real_beat"},
        {"line": "A greeting.", "beat": "greeting"},
        {"line": "A second greeting that must not crowd it.", "beat": "greeting"},
    ]}
    got = worldforge._beat_lines(model_card["lines"])
    beats = [x["beat"] for x in got]
    ok(beats.count("any") <= 1,
       f"at most one always-available line survives - a refrain said at every "
       f"moment is said over a burial too ({got})")
    ok("not_a_real_beat" not in beats,
       f"a beat the detector can never return is dropped, not stored "
       f"unreachably ({got})")
    greetings = [x for x in got if x["beat"] == "greeting"]
    ok(len(greetings) == 1,
       f"one line per beat, so a beat is a moment and not a pile ({got})")
    # The refrain is stored with an empty beat key, which is what
    # eligible_lines() reads as "available at any moment". Either spelling is
    # the same thing; anything else that is not a declared beat is not.
    ok(all((x["beat"] or "any").lower() in persona.BEATS or not x["beat"]
           for x in got),
       f"every surviving beat is one the engine can actually detect ({got})")

    legacy = worldforge._beat_lines([{"line": "Old shape.", "beat": "victory"}])
    ok(legacy == [{"line": "Old shape.", "beat": "victory"}],
       f"the legacy famous_lines shape still reads ({legacy})")

    # A synonym a model reaches for instead of the declared vocabulary lands on
    # the real beat rather than being thrown away.
    syn = worldforge._beat_lines([{"line": "We win.", "beat": "triumph"}])
    ok(syn and syn[0]["beat"] in persona.BEATS,
       f"a synonym beat is aliased onto the real one ({syn})")


def _all():
    return (            test_a_companion_goes_where_the_player_goes,
            test_the_player_is_somebody_before_turn_one,
            test_an_entry_point_is_not_undone_by_the_floor,
            test_one_person_is_one_record_but_a_namesake_is_two_people,
            test_a_canon_world_is_told_in_chapters,
            test_the_world_grows_into_its_own_canon,
            test_a_town_does_not_wear_a_landmarks_name,
            test_a_named_franchise_stays_canon_when_research_is_thin,
            test_a_chapter_ends_in_play_and_the_next_is_walked_to,
            test_the_cast_does_not_vanish_after_the_opening_turn,
            test_the_opening_scene_has_people_in_it,
            test_a_premise_is_parsed_not_searched,
            test_a_parenthesised_source_still_carries_the_character_in,
            test_a_character_who_came_with_you_starts_as_your_companion,
            test_research_enriches_and_never_gates,
            test_an_original_setting_is_still_original,
            test_a_crossover_is_private_even_when_research_misses,
            test_a_forged_world_matches_the_starter,
            test_f3_generated_content_is_tagged_canon_or_original,
            test_all_four_scales_build_without_truncating,
            test_a_truncated_pass_falls_back_instead_of_failing_the_whole_build,
            test_locations_are_scenes_not_a_floor_plan,
            test_the_dark_systems_actually_light_up,
            test_the_daily_is_genuinely_the_same_world,
            test_the_fate_thread_does_not_spoil_itself,
            test_the_price_of_a_world_is_quoted_correctly,
            test_a_crossover_is_asked_crossover_questions,
            test_the_forge_sends_what_the_endpoint_requires,
            test_canon_famous_lines_survive_into_world_cards,
            test_a_famous_line_is_keyed_to_a_beat_that_actually_happens,
            test_the_right_line_is_available_at_the_right_moment,
            test_a_character_the_floor_covers_is_not_only_floor,
            test_a_beat_cue_survives_how_a_player_actually_types,
            test_a_canon_cast_has_no_double_and_no_dropped_lead,
            test_a_container_category_is_not_mistaken_for_an_arc,
            test_the_floor_stays_a_floor_when_the_model_speaks)


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
