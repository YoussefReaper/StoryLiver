"""Live canon research - security, correctness, and the offline guarantee.

The network is not touched here. Everything that WOULD go to the network is
either exercised through its guards (which reject before any socket opens) or
driven with a fake transport, so this suite stays as hermetic and free as the
rest of the test tree.

What is actually being proved:

  SECURITY - the fetcher is the one place this server makes an outbound
  request, so it is the one place that can be turned into SSRF. The allowlist,
  the resolved-IP check and the HTTPS requirement are each tested on their own,
  because any one of them alone is bypassable.

  CORRECTNESS - the ranking decisions that separate a world built from real
  canon from one built from a disambiguation page: picking the right article,
  resolving a disambiguation, refusing a wiki that is about something else,
  and stripping article furniture out of a character list.

  SAFETY - mock mode must never reach the network, or the whole test tree
  silently starts depending on someone else's uptime.

Run:  python -m tests.test_research
"""
import os
import shutil
import socket
import tempfile

os.environ["STORYLIVER_LLM_MODE"] = "mock"
_TMP = tempfile.mkdtemp(prefix="storyliver-research-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP

from backend import canon_seed, db, research, worldforge  # noqa: E402
from backend.research import (BlockedHost, ResearchError, _Budget,  # noqa: E402
                              _get_json, _host_allowed, _public_ips,
                              _rank, _slug_candidates, _wiki_is_about)

FAILS, NOTES = [], []


def ok(cond, msg):
    (NOTES if cond else FAILS).append(msg)


def section(t):
    NOTES.append("\n  " + t)


# ------------------------------------------------------------------ security
def test_allowlist():
    section("SSRF — only the sources we chose, by name")
    for host in ("en.wikipedia.org", "naruto.fandom.com", "wikipedia.org",
                 "commons.wikimedia.org"):
        ok(_host_allowed(host), f"allowed: {host}")
    for host in ("evil.com", "wikipedia.org.attacker.test",
                 "fandom.com.attacker.net", "169.254.169.254", "localhost", ""):
        ok(not _host_allowed(host), f"refused: {host or '(empty)'}")


def test_resolved_ip_guard():
    section("SSRF — the resolved ADDRESS is checked, not just the name")
    for host in ("localhost", "127.0.0.1"):
        try:
            _public_ips(host)
            ok(False, f"{host} resolved to a private address and was allowed")
        except (BlockedHost, ResearchError):
            ok(True, f"{host} resolves privately and is refused before connecting")

    # This is the DNS-rebinding case: a name that passes an allowlist but
    # points somewhere internal. Checking the IP is what catches it.
    orig = socket.getaddrinfo
    socket.getaddrinfo = lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))]
    try:
        _public_ips("en.wikipedia.org")
        ok(False, "a rebound allowlisted host reached link-local")
    except BlockedHost:
        ok(True, "an allowlisted name pointing at cloud metadata is still blocked")
    finally:
        socket.getaddrinfo = orig

    socket.getaddrinfo = lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
    try:
        _public_ips("en.wikipedia.org")
        ok(False, "a rebound allowlisted host reached a private range")
    except BlockedHost:
        ok(True, "…and one pointing into RFC1918 is blocked too")
    finally:
        socket.getaddrinfo = orig


def test_fetcher_guards():
    section("SSRF — the fetcher refuses before opening a socket")
    cases = [("http://en.wikipedia.org/w/api.php", "plain HTTP is refused"),
             ("https://evil.com/api.php", "an off-allowlist host is refused"),
             ("https://169.254.169.254/latest/meta-data/",
              "the cloud metadata endpoint is refused")]
    for url, msg in cases:
        try:
            _get_json(url, {}, _Budget())
            ok(False, f"NOT refused: {url}")
        except (BlockedHost, ResearchError):
            ok(True, msg)

    budget = _Budget(limit=1)
    budget.take()
    try:
        _get_json("https://en.wikipedia.org/w/api.php", {}, budget)
        ok(False, "the request budget did not hold")
    except ResearchError:
        ok(True, "a dossier cannot exceed its request budget")


# --------------------------------------------------------------- correctness
def test_article_ranking():
    section("correctness — picking the article that IS the work")
    titles = ["List of Naruto characters", "Naruto", "Naruto (TV series)",
              "Naruto: Shippuden", "Naruto Uzumaki"]
    ok(_rank("Naruto", titles) == "Naruto",
       "the work beats its character list, its episode list and its protagonist")

    titles2 = ["Demon Slayer: Kimetsu no Yaiba", "Demon Slayer (disambiguation)",
               "List of Demon Slayer episodes", "Demon Slayer: Kimetsu no Yaiba season 1"]
    ok(_rank("Demon Slayer", titles2) == "Demon Slayer: Kimetsu no Yaiba",
       "a subtitled canonical title wins on token overlap")
    ok(_rank("x", ["Category:Foo", "File:Bar.png"]) is None,
       "namespace pages are never candidates")


def test_wiki_identity():
    section("correctness — refusing a wiki that is about something else")
    cases = [
        ("Narutopedia", "Naruto", "Naruto", "naruto", True,
         "a portmanteau name is accepted when the slug is an exact match"),
        ("Demon Wiki", "Demon Slayer", "Demon Slayer: Kimetsu no Yaiba", "demon", False,
         "a partial-word wiki is REFUSED — this is the one that silently poisons a world"),
        ("Kimetsu no Yaiba Wiki", "Demon Slayer", "Demon Slayer: Kimetsu no Yaiba",
         "kimetsu-no-yaiba", True, "the real wiki matches on the canonical subtitle"),
        ("Wookieepedia", "Star Wars", "Star Wars", "starwars", True,
         "an exact slug carries a name that shares nothing"),
        ("Cars Wiki", "Star Wars", "Star Wars", "cars", False,
         "a near-miss slug on an unrelated wiki is refused"),
    ]
    for site, setting, canon, slug, want, msg in cases:
        ok(_wiki_is_about(site, setting, canon, slug) is want, msg)


def test_slug_generation():
    section("correctness — guessing the subdomain")
    slugs = _slug_candidates("Demon Slayer", "Demon Slayer: Kimetsu no Yaiba")
    ok(slugs.index("kimetsu-no-yaiba") < slugs.index("demon-slayer"),
       "the subtitle is tried FIRST — for anime the wiki is named after it")
    ok("witcher" in _slug_candidates("The Witcher", "The Witcher"),
       "a leading article is stripped, because subdomains drop it")


def test_grounding_brief():
    section("correctness — what the generator is told")
    d = {"found": True, "canonical_name": "Demon Slayer: Kimetsu no Yaiba",
         "summary": "A manga about a boy whose family is killed.",
         "characters": [{"name": "Tanjiro Kamado", "note": "the protagonist"},
                        {"name": "", "note": "blank"},
                        {"name": "Nezuko Kamado", "note": ""}],
         "places": [{"name": "Mount Natagumo", "note": ""}],
         "factions": [], "sources": []}
    brief = research.grounding_brief(d)
    ok("Tanjiro Kamado" in brief and "Nezuko Kamado" in brief,
       "real names reach the world architect")
    ok("\n- \n" not in brief and "- :" not in brief,
       "a blank entry never becomes an instruction")
    ok("REAL ORGANISATIONS" not in brief,
       "an empty section is omitted rather than left as a stub heading")
    ok("Do NOT copy any sentence" in brief,
       "the generator is told to write its own prose — facts cross over, text does not")
    ok(research.grounding_brief({"found": False}) == "",
       "an unresearched setting adds nothing to the prompt")


def test_the_brief_never_licenses_invention():
    section("canon fidelity — the brief no longer tells the model to invent")
    # The reported bug: worlds came back full of characters and places that
    # were not in the setting. The brief itself was the cause. It ended with
    # "Invent freely for anything not listed" while a world-scale build asks
    # for 48-64 people against a roster of 9-14 - a forty-person shortfall
    # handed over with an explicit licence to fill it however it liked.
    d = {"found": True, "canonical_name": "Kimetsu no Yaiba", "summary": "",
         "characters": canon_seed.fallback_cast("demon slayer"),
         "places": canon_seed.fallback_places("demon slayer"),
         "factions": [], "sources": []}

    brief = research.grounding_brief(d, need_npcs=12, need_locs=10)
    ok("Invent freely" not in brief,
       "the words that caused it are gone from the prompt entirely")
    ok("must be used before you invent" in brief,
       "the roster must be exhausted before anything is invented")
    ok("never invent a relative" in brief.lower() or "never invent a relative" in brief,
       "and inventing a cousin/student/successor of a real character is named and refused")

    # The shortfall is stated as a NUMBER rather than left for the model to
    # discover, and what may fill it is constrained to background residents.
    big = research.grounding_brief(d, need_npcs=64, need_locs=48)
    ok("ORDINARY BACKGROUND RESIDENT" in big,
       "when the roster genuinely cannot cover the ask, the filler is scoped to extras")
    low = big.lower()
    ok("never a new hero" in low and "never a new villain" in low,
       "an invented person can never be a new hero or villain competing with the real cast")
    n_chars = len(d["characters"])
    ok(f"about {64 - n_chars} extra people" in big,
       "the exact shortfall is named, so the model is not left to guess how many to add")

    # And when the roster DOES cover the ask, invention is refused outright.
    small = research.grounding_brief(d, need_npcs=3, need_locs=2)
    ok("Do not invent anyone new." in small,
       "a roster that covers the ask forbids invention rather than merely discouraging it")


def test_canon_supply_reaches_the_prompt():
    section("canon fidelity — enough real names survive to fill the world")
    # The second half of the same bug: the caps were tuned when a world was
    # one call asking for 10-12 people. Every real name cut here was a name
    # the builder then had to invent a replacement for.
    ok(research.BUCKET_KEEP["characters"] >= 24,
       "research keeps enough of a researched cast to fill a large world")
    ok(research.BRIEF_KEEP["characters"] >= 24,
       "and the brief shows them rather than truncating at 14")

    many = [{"name": f"Real Person {i}", "note": ""} for i in range(30)]
    brief = research.grounding_brief(
        {"found": True, "characters": many, "places": [], "factions": [],
         "canonical_name": "", "summary": "", "sources": []},
        need_npcs=30, need_locs=0)
    shown = sum(1 for line in brief.splitlines() if line.startswith("- Real Person"))
    ok(shown >= 24, f"a 30-strong researched cast reaches the prompt ({shown} names, was capped at 14)")


def test_canon_is_dealt_across_districts_not_raced_for():
    section("canon fidelity — each district gets its own slice of the roster")
    # A world is 8 districts, each asked for 6-8 people, each previously handed
    # the IDENTICAL roster with no idea what the others were doing.
    found = {"characters": [{"name": f"C{i}", "note": ""} for i in range(9)],
             "places": [{"name": f"P{i}", "note": ""} for i in range(6)]}
    shares = worldforge._allocate_canon(found, 4)
    ok(len(shares) == 4, "one share per district")

    dealt = [c["name"] for s in shares for c in s["characters"]]
    ok(sorted(dealt) == sorted(f"C{i}" for i in range(9)),
       "every real character is dealt exactly once across the whole world")
    ok(len(dealt) == len(set(dealt)),
       "and no two districts are handed the same person to place")

    # The district's own prompt names its people and disowns everyone else's.
    text = worldforge._district_roster(shares[0])
    ok("must appear here" in text, "a district is told its share must actually appear")
    ok("belongs to a DIFFERENT district" in text,
       "and that the rest of the roster is not its to spend")
    ok(worldforge._district_roster({}) == "",
       "a district with no share adds nothing to its prompt")


def test_unused_canon_is_seated_not_merely_labelled():
    section("canon fidelity — real names that were skipped take back their seats")
    # F3 tagged invented content "original", which made the problem visible
    # without making it smaller: the world could still come back with the real
    # cast half-missing and a crowd of strangers holding the speaking parts.
    found = {"characters": [{"name": "Tanjiro Kamado", "note": "carries his sister"},
                            {"name": "Nezuko Kamado", "note": "turned demon"},
                            {"name": "Giyu Tomioka", "note": "Water Hashira"}],
             "places": [{"name": "Butterfly Mansion", "note": "half hospital"}]}
    raw = {
        "npcs": [
            {"id": "n1", "name": "Tanjiro Kamado", "role": "slayer", "origin": "canon"},
            {"id": "n2", "name": "Kaname Aoi", "role": "a tea seller", "origin": "original",
             "anchors": {"goals": ["sell more tea"]}, "seed_memories": ["opened the stall"]},
            {"id": "n3", "name": "Aiko Sato", "role": "a courier", "origin": "original"},
        ],
        "locations": [
            {"id": "l1", "name": "The Rusty Kettle", "desc": "a tea house", "origin": "original"},
        ],
    }
    out = worldforge._seat_unused_canon(raw, found)
    names = [n["name"] for n in out["npcs"]]
    ok("Nezuko Kamado" in names and "Giyu Tomioka" in names,
       "researched characters the builder skipped are seated in the world")
    ok("Kaname Aoi" not in names and "Aiko Sato" not in names,
       "and the invented strangers holding those seats are gone")
    ok(names.count("Tanjiro Kamado") == 1,
       "someone already present is not seated a second time")
    ok(all(n["origin"] == "canon" for n in out["npcs"]),
       "the seated characters are tagged canon, because now they really are")

    seated = next(n for n in out["npcs"] if n["name"] == "Giyu Tomioka")
    # Seating no longer writes the persona itself. It used to, from a
    # hand-written if-chain covering sixteen characters by name, which meant
    # everybody else in fiction got "VOICE: plain, direct, unhurried" and
    # "CONSTRAINTS: is an ordinary mortal person". _apply_canon_personas now
    # fills this in from what the model knows, for any character in any
    # setting - so what seating must guarantee is the identity and a true
    # role, and that no invented persona is left behind wearing a real name.
    ok(seated["anchors"]["name"] == "Giyu Tomioka",
       "the seat carries the real character's identity")
    ok("Water Hashira" in seated["role"],
       "and what research actually knows about them, as their role")
    ok(not seated.get("seed_memories"),
       "the previous occupant's memories do not travel with the seat")
    ok(out["locations"][0]["name"] == "Butterfly Mansion",
       "the same holds for places")


def test_the_narrator_stays_inside_the_story():
    section("immersion — the register failures a played turn actually produced")
    from backend import narrator

    # ALL THREE OBSERVED IN ONE LIVE PASSAGE, with the right cast present and
    # every character in voice. Nothing here is a hallucination or a contra-
    # diction, which is why none of the existing rules caught any of it - these
    # are the tells that make good prose still read as machine-written.
    rules = narrator.SYSTEM

    # 1. "You turn to her, still feeling the lingering weight of her words.
    #     'What do you make of this place, Charlie?'" - the narrator wrote the
    #    player's feelings AND their dialogue, replaying an action they had
    #    already taken.
    ok("NEVER put words in the player's mouth" in rules,
       "the narrator may not write the player's dialogue")
    ok('"you say"' in rules and '"you ask"' in rules,
       "and the exact construction that shipped is named")
    ok("write what it" in rules and "MET" in rules,
       "a finished action is answered, not performed again")

    # 2. "Charlie turns to the traveller and asks what they think..." - the
    #    closing line dropped second person and handed the turn back as a
    #    question, which is the menu rule wearing a costume.
    ok('The player is "you" to the last word' in rules and "the traveller" in rules,
       "second person holds to the last line")
    ok("smuggle" in rules and "asking what you think" in rules,
       "and a character asking the player what they think is refused as the "
       "menu it is")

    # 3. "...as the characters embody their beliefs." The narrator stepping
    #    outside the story to admire it. Breaks no other rule: invents nothing,
    #    contradicts nothing, and is the loudest tell of the three.
    ok("Stay inside the story" in rules and "what any of it means" in rules,
       "no authorial voice above the world")
    ok("the characters" in narrator.BANNED and "embody their beliefs" in narrator.BANNED,
       "and the phrases it reached for are banned outright, because a prompt "
       "is a request and this one has already been ignored once")


def test_arcs_are_put_in_the_order_the_source_tells_them():
    section("chapters — canon order, from the source's own chapter numbers")
    # A live build opened with Mugen Train third and Mount Natagumo seventh.
    # Canon runs Natagumo first. The names were right - they come from the
    # wiki - but a wiki lists categories ALPHABETICALLY, and asking a model to
    # re-sort them is approximate. Approximate is wrong here: anyone who knows
    # the series sees a wrong running order at a glance.
    #
    # The arc articles carry it exactly, in their infoboxes:
    #     |chapters = [[Chapter 54|54]] - [[Chapter 66|66]]
    def page(title, chapter):
        return {"title": title, "revisions": [{"slots": {"main": {
            "content": f"{{{{Story Arc\n|chapters = [[Chapter {chapter}|{chapter}]]\n}}}}"}}}]}

    pages = [page("Final Selection Arc", 6), page("Asakusa Arc", 13),
             page("Mount Natagumo Arc", 31), page("Rehabilitation Training Arc", 45),
             page("Mugen Train Arc", 54), page("Entertainment District Arc", 71),
             page("Swordsmith Village Arc", 98), page("Infinity Castle Arc", 137)]
    alphabetical = ["Asakusa", "Entertainment District", "Final Selection",
                    "Infinity Castle", "Mount Natagumo", "Mugen Train",
                    "Rehabilitation Training", "Swordsmith Village"]

    real = research._api
    research._api = lambda *a, **k: {"query": {"pages": pages}}
    try:
        got = research.order_arcs("h", alphabetical, _Budget())
    finally:
        research._api = real

    ok(got == ["Final Selection", "Asakusa", "Mount Natagumo",
               "Rehabilitation Training", "Mugen Train", "Entertainment District",
               "Swordsmith Village", "Infinity Castle"],
       f"the real Demon Slayer running order, from chapter numbers ({got[:4]}…)")

    # An arc with no chapter number falls back to its stated ordinal, and one
    # with neither keeps its place rather than being moved on a guess.
    mixed = [page("Alpha Arc", 10),
             {"title": "Beta Arc", "revisions": [{"slots": {"main": {
                 "content": "is the third story arc of something"}}}]},
             {"title": "Gamma Arc", "revisions": [{"slots": {"main": {
                 "content": "no ordering information at all"}}}]}]
    research._api = lambda *a, **k: {"query": {"pages": mixed}}
    try:
        got2 = research.order_arcs("h", ["Gamma", "Beta", "Alpha"], _Budget())
    finally:
        research._api = real
    ok(got2[0] == "Alpha",
       "a real chapter number outranks an ordinal in prose")
    ok(got2.index("Beta") < got2.index("Gamma"),
       "an ordinal still beats nothing")
    ok(got2[-1] == "Gamma",
       "and an arc the wiki cannot place is left at the end rather than "
       "reordered on a guess")

    # If the request fails entirely, the names survive untouched.
    research._api = lambda *a, **k: (_ for _ in ()).throw(ResearchError("down"))
    try:
        ok(research.order_arcs("h", alphabetical, _Budget()) == alphabetical,
           "a failed lookup leaves the list exactly as it was")
    finally:
        research._api = real


def test_a_wiki_is_asked_what_categories_it_has():
    section("fandom — read the wiki's own categories instead of guessing names")
    # THE CAUSE of every Fandom bucket coming back empty in production. The
    # code probed a fixed list of guessed category names - "Category:Arcs",
    # "Category:Locations", "Category:Organizations" - and the Demon Slayer
    # wiki has none of them. Checked against the live wiki: 783 categories, no
    # "Arcs" anywhere, and the arcs are filed as categories in their own right:
    # "Mugen Train Arc", "Final Selection Arc", "Mount Natagumo Arc".
    real = ["Characters", "Male Characters", "Female Characters", "Alive",
            "Anime Images", "Asakusa Arc", "Mugen Train Arc", "Final Selection Arc",
            "Locations", "Organizations", "Demon Slayer Corps", "Families",
            "Abilities", "Breathing Styles", "Achievement Badges"]

    ok(research.categories_for("characters", real) == [
        "Characters", "Male Characters", "Female Characters"],
       "the character categories this wiki actually has")
    arcs = research.categories_for("arcs", real)
    ok(arcs == ["Asakusa Arc", "Mugen Train Arc", "Final Selection Arc"],
       f"every arc is found, because on this wiki each arc IS a category ({arcs})")
    ok(research.categories_for("places", real) == ["Locations"],
       "and the place category, under whichever of several names it uses")
    ok("Demon Slayer Corps" in research.categories_for("factions", real),
       "a setting-specific faction category is matched on shape, not on a guess")
    ok(research.categories_for("powers", real) == ["Abilities", "Breathing Styles"],
       "as is a setting-specific power system")

    noise = research.categories_for("characters", real) + research.categories_for("places", real)
    ok("Anime Images" not in noise and "Achievement Badges" not in noise
       and "Alive" not in noise,
       "wiki housekeeping categories are not mistaken for content")

    ok(research.categories_for("nonsense", real) == [],
       "an unknown bucket matches nothing rather than everything")
    ok(len(research.categories_for("factions", real)) <= 4,
       "container buckets take a handful; only arcs are unbounded, because "
       "there each category is a whole chapter of the story")


def test_chapters_survive_a_wiki_that_never_answers():
    section("chapters — the running order does not depend on Fandom being reachable")
    # THE ACTUAL CAUSE, found by reading a live dossier rather than guessing.
    # A production lookup for Demon Slayer returns:
    #
    #     found: true, wiki: "", characters: 11,
    #     arcs: [], places: 0, factions: 0, powers: 0
    #
    # The Fandom wiki is never found, so every Fandom-sourced bucket is dark
    # and only Wikipedia's curated cast survives. Chapters are built from arcs,
    # so every real setting fell back to a single chapter named after its town -
    # the whole feature absent, on every world anyone would actually build.
    # Reordering the buckets, which is what I tried first, fixed a starvation
    # that was not happening.
    asked = {}

    def spy(role, system, user, **kw):
        asked["system"], asked["user"] = system, user
        return {"arcs": [{"name": "Final Selection", "note": "the trial on the mountain"},
                         {"name": "Mount Natagumo", "note": "the spider family"},
                         {"name": ""}]}

    real, worldforge._resilient = worldforge._resilient, spy
    try:
        arcs = worldforge.canon_chapters("Demon Slayer", user_id="u_test")
    finally:
        worldforge._resilient = real

    ok([a["name"] for a in arcs] == ["Final Selection", "Mount Natagumo"],
       "the model supplies the running order a wiki could not")
    ok("the names a fan would recognise" in asked["system"],
       "asked for the arc names the audience actually uses, not invented titles")
    ok(worldforge.canon_chapters("", user_id="u_test") == [],
       "and nothing is asked for a setting with no name")

    # Offline the stub answers empty, and a world is simply one chapter again -
    # the old behaviour, rather than a broken one.
    real, worldforge._resilient = worldforge._resilient, lambda *a, **k: {"arcs": []}
    try:
        ok(worldforge.canon_chapters("Demon Slayer", user_id="u_test") == [],
           "offline it claims nothing rather than inventing a running order")
    finally:
        worldforge._resilient = real


def test_the_chapter_list_is_not_starved_by_the_request_budget():
    section("chapters — arcs are fetched before the budget runs out")
    # OBSERVED LIVE. A Demon Slayer build came back with no arcs at all, so
    # chapters fell back to a single chapter named after the town - the whole
    # chapter feature silently absent on the setting it was built for.
    #
    # Not a lookup failure: `arcs` was simply LAST in CATEGORY_SETS, behind
    # characters, places, factions and powers, and the fetch loop breaks out as
    # soon as the per-dossier request budget runs low. It reliably never got
    # there. Raising the character and place keep-counts earlier made it worse,
    # because ranking more names costs more requests.
    order = list(research.CATEGORY_SETS)
    ok(order[0] == "characters",
       "the cast is still fetched first — it is what a world is made of")
    ok(order.index("arcs") == 1,
       f"and the arc list is second, not last, because the world's whole chapter "
       f"structure depends on it ({order})")
    ok(order.index("arcs") < order.index("factions")
       and order.index("arcs") < order.index("powers"),
       "ahead of the buckets a world can do without")

    # It is also the cheapest bucket: no per-entity describe() pass, which only
    # characters get. Moving it up costs almost nothing.
    ok("arcs" in research.CATEGORY_SETS and research.CATEGORY_SETS["arcs"],
       "and it still looks in the categories a wiki actually files arcs under")


def test_the_small_details_a_fan_would_notice():
    section("canon — the conditional details, and their condition")
    from backend import persona
    # Charlie's horns are not a standing feature: they are hidden in her hair
    # and come out when she turns lethal. A card that says "has horns" is worse
    # than saying nothing, because it puts them on show in every scene - and
    # getting exactly this wrong is what makes a fan stop believing the world.
    ok("mannerisms" in worldforge.CANON_PERSONA_SYSTEM,
       "the persona card asks for the small details at all")
    spec = worldforge.CANON_PERSONA_SYSTEM
    ok("only come out when" in spec and "horns" in spec,
       "with the horns example spelled out, condition and all")
    ok('"Charlie has horns" is wrong' in spec,
       "and the wrong version named as wrong, not merely left unmentioned")
    ok("normally hidden, absent or sheathed" in spec,
       "at least one must be physical and conditional, not only behavioural")

    card = {"characters": [{"name": "Charlie", "voice": "bright",
                            "mannerisms": ["Her horns stay hidden in her hair until she "
                                           "turns lethal", "She clasps her hands when "
                                           "explaining"]}]}
    real, worldforge._resilient = worldforge._resilient, lambda *a, **k: card
    try:
        out = worldforge._apply_canon_personas(
            {"npcs": [{"id": "c", "name": "Charlie", "origin": "canon"}]},
            "Hazbin Hotel", user_id="u_test")
    finally:
        worldforge._resilient = real
    tells = out["npcs"][0]["anchors"]["mannerisms"]
    ok(any("horns" in t for t in tells), "and they reach the character's card")

    block = persona.identity_block({**out["npcs"][0]["anchors"], "name": "Charlie",
                                    "role": "princess"})
    ok("horns" in block, "and the narrator's identity block")
    ok("only when their condition is met" in block,
       "labelled as conditional, so a tell is not printed as a standing "
       "description in every scene — which is the failure, not the fix")


def test_a_spoken_line_becomes_a_plate_the_way_people_are_named():
    section("plates — three reasons the signature element never fired in play")
    from backend import narrator
    present = ["Charlie", "Inosuke Hashibira", "Zenitsu Agatsuma",
               "Tanjiro Kamado", "Nezuko Kamado"]

    # 1. Exact-name matching. The cast is "Inosuke Hashibira"; everybody in the
    # story calls him Inosuke, so the narrator writes "@Inosuke:" - and every
    # one of those lines was folded silently back into prose.
    got = narrator.split_speech("@Inosuke: Tch. Fake-out trash.", present)
    ok(got[0]["kind"] == "speech" and got[0]["name"] == "Inosuke Hashibira",
       "a first name resolves to the character it belongs to")
    ok(narrator.split_speech("@Kamado: which one?", present)[0]["kind"] == "narration",
       "but an ambiguous one — two Kamados in the room — is credited to nobody")
    ok(narrator.split_speech("@Nobody At All: hello", present)[0]["kind"] == "narration",
       "and a speaker who is not present still folds back into prose")

    # 2. The sigil. Given the rule, the model reliably writes the line on its
    # own and drops the "@": "Giyu Tomioka: The courier notice is missing."
    # The gate was never the punctuation - it is that the name belongs to
    # somebody in the room.
    plain = narrator.split_speech("Charlie: There has to be a better way.", present)
    ok(plain[0]["kind"] == "speech" and plain[0]["name"] == "Charlie",
       "a marked line without the sigil is still a marked line")
    prose = narrator.split_speech(
        "The market keeps moving: carts, voices, gulls.", present)
    ok(prose[0]["kind"] == "narration",
       "while ordinary prose containing a colon is left alone")

    # 3. The plate rule is stated where it will be read, and as a requirement.
    ok("THE LINE THAT LANDS" in narrator.SYSTEM,
       "the rule has its own block near the top rather than a bullet buried in a list")
    ok("This is not\noptional" in narrator.SYSTEM or "not optional" in narrator.SYSTEM,
       "and says it is not optional — written as a permission, it was read as one")


def test_an_empty_room_is_told_it_is_empty():
    section("immersion — the narrator cannot invent people into an empty room")
    # OBSERVED LIVE. The engine reported "0 here" and the passage had Nezuko
    # walk in and Tanjiro speak a line. The prompt's fault: an empty present
    # list rendered as the heading "CHARACTERS PRESENT (obey these exactly):"
    # followed by the parenthetical "(nobody else is present)", and the model
    # filled the vacuum. Two things then broke quietly - the witness layer had
    # recorded nobody in the room, so nothing those two "saw" would ever be
    # remembered, and split_speech refused to plate a speaker who was not
    # there, which is why the line came back as flat prose instead of a plate.
    import inspect
    from backend import narrator

    src = inspect.getsource(narrator.narrate)
    ok("nobody else is present" not in src,
       "the weak parenthetical the model ignored is gone")
    ok("You are alone in this place" in src,
       "an empty room is stated plainly")
    ok("No character may appear, speak, arrive or be addressed" in src,
       "and the thing that actually went wrong is forbidden by name")
    ok("if present else" in src.replace("\n", " ").replace("  ", " ") or "if present" in src,
       "the branch keys off who is actually present, not off whether the "
       "anchor string happened to be non-empty — the placeholder made that "
       "condition permanently true")


def test_the_character_the_player_named_actually_turns_up():
    section("canon fidelity — a crossover import is guaranteed a seat")
    # REPORTED: "I started a Demon Slayer world with Charlie and Charlie wasn't
    # even there." The host's cast was structurally enforced - pinned, then
    # seated if the builder skipped anyone - while the carried-in character was
    # a paragraph of encouragement in the build brief and nothing more. The one
    # character the player explicitly asked for had the weakest guarantee in
    # the pipeline.
    imports = [{"character": "Charlie Morningstar", "from": "Hazbin Hotel"}]

    raw = {"npcs": [{"id": f"n{i}", "name": f"Invented {i}", "role": "villager",
                     "origin": "original"} for i in range(4)]}
    out = worldforge._seat_imports(raw, imports)
    names = [n["name"] for n in out["npcs"]]
    ok("Charlie Morningstar" in names,
       "the character the player named is in the world even when the builder skipped her")
    charlie = next(n for n in out["npcs"] if n["name"] == "Charlie Morningstar")
    ok(charlie["origin"] == "canon",
       "and is not filed as somebody this world invented")
    ok(charlie["from_source"] == "Hazbin Hotel",
       "her own source travels with her, so her persona is looked up under Hazbin Hotel "
       "rather than under the world she was dropped into")

    # THE CASE THAT ACTUALLY SHIPPED. A crossover into a well-researched
    # setting fills every slot with a real character of the HOST world, so
    # there is no invented seat left to displace - and the first version of
    # this function gave up and silently dropped the one person the player had
    # asked for by name. Twice, on the live build.
    full = {"npcs": [{"id": f"n{i}", "name": n, "origin": "canon"} for i, n in enumerate(
        ["Tanjiro Kamado", "Nezuko Kamado", "Zenitsu Agatsuma",
         "Inosuke Hashibira", "Shinobu Kocho", "Muzan Kibutsuji"])],
        "start_location": "market_square"}
    out3 = worldforge._seat_imports(full, imports)
    charlie = [n for n in out3["npcs"] if n["name"] == "Charlie Morningstar"]
    ok(len(charlie) == 1,
       "a cast with no invented seats left still gets the carried-in character — "
       "she is added rather than dropped")
    ok(charlie and charlie[0]["start_location"] == "market_square",
       "and she starts where the player does, because 'with Charlie' means with her")
    ok(" " not in charlie[0]["id"],
       "her generated id is a usable slug")

    # OBSERVED LIVE: the world came back with TWO Charlies in different rooms.
    # The premise names "Charlie"; the builder wrote "Charlie Morningstar"; the
    # two did not compare equal, so seating added a second one beside the first.
    # A full name and the name somebody is called are the same person.
    dup = {"start_location": "square", "npcs": [
        {"id": "a", "name": "Charlie Morningstar", "origin": "original",
         "start_location": "tea_house"},
        {"id": "b", "name": "Tanjiro Kamado", "origin": "canon"}]}
    out4 = worldforge._seat_imports(dup, [{"character": "Charlie", "from": "Hazbin Hotel"}])
    charlies = [n for n in out4["npcs"] if "Charlie" in n["name"]]
    ok(len(charlies) == 1, f"one Charlie, not two ({len(charlies)})")
    ok(charlies[0]["name"] == "Charlie Morningstar",
       "and the builder's fuller name is the one kept")
    ok(charlies[0]["start_location"] == "square",
       "moved to stand with the player rather than left across town")
    ok(charlies[0]["origin"] == "canon",
       "and re-filed as canon rather than as somebody this world invented")

    # Already built by the builder: kept, but re-filed as canon rather than local.
    raw2 = {"npcs": [{"id": "n1", "name": "Charlie Morningstar", "role": "an innkeeper",
                      "origin": "original"}]}
    out2 = worldforge._seat_imports(raw2, imports)
    ok(len(out2["npcs"]) == 1 and out2["npcs"][0]["origin"] == "canon",
       "a character the builder did include is not seated twice, just re-filed")

    # And the persona call groups by source rather than asking one franchise
    # about another franchise's character.
    asked = {}

    def spy(role, system, user, **kw):
        asked["user"] = user
        return {"characters": []}

    real, worldforge._resilient = worldforge._resilient, spy
    try:
        worldforge._apply_canon_personas(
            {"npcs": [{"id": "n1", "name": "Tanjiro Kamado", "origin": "canon"},
                      {"id": "n2", "name": "Charlie Morningstar", "origin": "canon",
                       "from_source": "Hazbin Hotel"}]},
            "Demon Slayer", user_id="u_test")
    finally:
        worldforge._resilient = real
    prompt = asked["user"]
    ok("SOURCE: Hazbin Hotel" in prompt and "SOURCE: Demon Slayer" in prompt,
       "each character is asked about under their own source")
    ok(prompt.index("Hazbin Hotel") < prompt.index("Charlie Morningstar"),
       "Charlie is listed under Hazbin Hotel, not under the host world")


def test_a_canon_character_is_not_handed_over_as_an_ordinary_mortal():
    section("canon fidelity — the persona the narrator is told to obey")
    # THE REPORTED BUG, reproduced at the layer that caused it. Research
    # flattens a character to one roster line; the builder invents a persona
    # from that line; memory.anchor_block then hands the invention to the
    # narrator under "CHARACTERS PRESENT (obey these exactly)". So a build
    # produced - and the narrator correctly obeyed:
    #
    #     Nezuko Kamado - a quiet village girl
    #       VOICE: Soft-spoken and kind.
    #       CONSTRAINTS: Is an ordinary mortal person.
    #
    # for a character who is mute, is a demon, and is carried in a box. The
    # anti-drift machinery was pinning the wrong person.
    from backend import memory, worldkit

    raw = {
        "npcs": [
            {"id": "n1", "name": "Nezuko Kamado", "role": "a quiet village girl",
             "origin": "canon",
             "anchors": {"voice": "Soft-spoken and kind.",
                         "constraints": ["Is an ordinary mortal person."]}},
            {"id": "n2", "name": "A Stall Keeper", "role": "sells rope",
             "origin": "original"},
        ],
    }
    card = {"characters": [{
        "name": "Nezuko Kamado", "role": "Tanjiro's sister, turned demon",
        "voice": "Does not speak. Muffled sounds through a bamboo muzzle.",
        "constraints": ["Is a demon, not a human.", "Cannot speak at all."],
        "goals": ["Protect her brother."], "taboos": ["Never harms a human."],
        "memories": ["I remember the smell of the snow that morning."]}]}

    import backend.worldforge as wf
    real, wf._resilient = wf._resilient, lambda *a, **k: card
    try:
        out = wf._apply_canon_personas(raw, "Demon Slayer", user_id="u_test")
    finally:
        wf._resilient = real

    nez = out["npcs"][0]["anchors"]
    ok("ordinary mortal" not in " ".join(nez["constraints"]).lower(),
       "the invented 'ordinary mortal person' is gone from a character who is a demon")
    ok(any("demon" in c.lower() for c in nez["constraints"]),
       "and what she actually is replaces it")
    ok("not speak" in nez["voice"].lower(),
       "a mute character is no longer described to the narrator as soft-spoken")
    ok(out["npcs"][0]["seed_memories"],
       "she arrives carrying something of her own")

    ok(out["npcs"][1]["role"] == "sells rope" and not out["npcs"][1].get("anchors"),
       "an invented background resident is left alone — this only speaks for real people")

    # Offline, and for a character the model does not know, nothing is claimed.
    raw2 = {"npcs": [{"id": "n1", "name": "Someone Obscure", "origin": "canon",
                      "role": "a clerk", "anchors": {"voice": "Quiet."}}]}
    real, wf._resilient = wf._resilient, lambda *a, **k: {"characters": []}
    try:
        out2 = wf._apply_canon_personas(raw2, "Demon Slayer", user_id="u_test")
    finally:
        wf._resilient = real
    ok(out2["npcs"][0]["anchors"]["voice"] == "Quiet.",
       "a character the model does not know keeps what the builder wrote, rather than being overwritten with a guess")

    # It must not fire when there is nothing to seat, or nothing to seat into.
    untouched = {"npcs": [{"id": "n1", "name": "Someone", "origin": "original"}]}
    ok(worldforge._seat_unused_canon(dict(untouched), {})["npcs"][0]["name"] == "Someone",
       "an ungrounded build is left exactly as the model wrote it")


def test_character_list_furniture():
    section("correctness — an article's furniture is not a character")
    import re as _re
    from backend.research import _dedupe
    headings = ["Naruto Uzumaki", "Sasuke Uchiha", "Creation and conception",
                "External links", "See also", "Secondary characters", "Team 7",
                "Reception and legacy", "Sakura Haruno"]
    kept = []
    furniture = ("reception", "see also", "reference", "main", "other", "minor",
                 "recurring", "list", "note", "cast", "creation", "conception",
                 "development", "character", "media", "introduce", "overview",
                 "background", "summary", "antagonist", "protagonist",
                 "supporting", "secondary", "villain", "team", "group",
                 "organi", "clan", "family", "appear", "adaptation",
                 "reaction", "analysis", "external", "link", "further",
                 "bibliograph", "appendix", "content", "source", "index",
                 "gallery", "trivia", "concept", "casting", "voice", "legacy",
                 "merchand", "film", "television", "novel", "comic", "game",
                 "series")
    for line in headings:
        low = line.lower()
        if (2 <= len(line.split()) <= 4 and line[:1].isupper()
                and not any(w in low for w in furniture)
                and not _re.search(r"\d", line)):
            kept.append(line)
    ok(kept == ["Naruto Uzumaki", "Sasuke Uchiha", "Sakura Haruno"],
       f"only real people survive the filter ({kept})")


# ------------------------------------------------------------------- offline
def test_offline_is_hermetic():
    section("offline — mock mode never reaches the network")
    ok(not research.enabled(),
       "research reports itself DISABLED under STORYLIVER_LLM_MODE=mock")

    orig = socket.getaddrinfo
    touched = []

    def trap(*a, **k):
        touched.append(a[0] if a else "?")
        raise AssertionError("network touched")

    socket.getaddrinfo = trap
    try:
        d = research.dossier("Demon Slayer")
        ok(not d["found"] and d["note"] == "research disabled",
           "a dossier in mock mode returns empty rather than dialling out")

        db.init()
        world = worldforge.bootstrap("Demon Slayer", user_id="research-test")
        ok(bool(world.get("name")),
           "and a world still BUILDS — research is an enhancement, never a dependency")
        ok(world.get("researched") is False and world.get("sources") == [],
           "it is honestly marked unresearched, with no sources claimed")
        ok(world.get("personal_only") is True,
           "the copyright boundary is untouched: an IP setting is still personal-only")
    finally:
        socket.getaddrinfo = orig
    ok(not touched, f"zero DNS lookups attempted ({len(touched)})")


def test_canon_seed_fallback():
    section("canon fidelity — a broken network must not mean invented names")
    # D2, reproduced: "Swordsmith Village" (Demon Slayer) built made-up NPCs
    # (Kaname, Aiko) because a category fetch timed out and dossier() returned
    # an empty cast, which the model then filled from its own imagination. A
    # small curated table for the handful of franchises this happens to
    # constantly beats an empty cast in every failure mode.
    db.init()
    import backend.research as R
    real_enabled, real_identify, real_summarise = R.enabled, R.identify, R.summarise
    real_curated, real_find_wiki = R.characters_from_wikipedia, R.find_wiki
    try:
        R.enabled = lambda: True

        # (a) the very first lookup fails outright.
        def boom(*a, **k):
            raise R.ResearchError("egress timeout")
        R.identify = boom
        d = research.dossier("Demon Slayer", refresh=True)
        ok(d["found"] and len(d["characters"]) >= 4,
           f"identify() raising still yields a real cast ({len(d['characters'])} characters)")
        ok(any("Tanjiro" in c["name"] for c in d["characters"]),
           "and the protagonist is in it")
        ok(len(d["places"]) >= 2,
           f"and real places, not an empty REAL PLACES section ({len(d['places'])})")
        ok(any("Butterfly" in p["name"] for p in d["places"]),
           "grounded in an actual canon location")

        # (b) identify() succeeds, everything downstream comes back empty —
        # the exact Swordsmith Village failure mode (a timed-out category fetch).
        R.identify = lambda setting, budget: {"title": "Demon Slayer: Kimetsu no Yaiba"}
        R.summarise = lambda title, budget: {"title": title, "summary": "A manga series.",
                                             "url": "", "license": ""}
        R.characters_from_wikipedia = lambda *a, **k: []
        R.find_wiki = lambda *a, **k: None
        d2 = research.dossier("demon slayer", refresh=True)
        ok(d2["found"] and len(d2["characters"]) >= 4,
           f"an empty live cast falls back to the seed roster ({len(d2['characters'])} characters)")

        # (c) live research succeeds but ranks the protagonist out of the cast —
        # a real, reproducible failure: article-length ranking can put a short
        # protagonist page below a longer side-character one.
        R.characters_from_wikipedia = lambda *a, **k: [
            {"name": "Zenitsu Agatsuma", "note": ""}, {"name": "Inosuke Hashibira", "note": ""},
            {"name": "Shinobu Kocho", "note": ""}, {"name": "Giyu Tomioka", "note": ""}]
        d3 = research.dossier("demon slayer", refresh=True)
        ok(any("Tanjiro" in c["name"] for c in d3["characters"]),
           "a real cast missing the protagonist gets them PINNED in, not just left out")
        ok(d3["characters"][0]["name"].startswith("Tanjiro"),
           "and pinned first — the lead is not buried after four side characters")

        # (d) a setting with no seed entry gets no fabricated help — this must
        # not become a crutch for every possible setting, only the handful
        # that keep reproducing the failure.
        R.identify = boom
        d4 = research.dossier("a wholly original setting nobody wrote", refresh=True)
        ok(not d4["found"] and not d4["characters"],
           "an unmatched original setting fails honestly rather than inventing a cast")
    finally:
        R.enabled, R.identify, R.summarise = real_enabled, real_identify, real_summarise
        R.characters_from_wikipedia, R.find_wiki = real_curated, real_find_wiki

    ok(canon_seed.match("HAZBIN HOTEL", "") is not None,
       "matching is case/spacing-insensitive")
    ok(canon_seed.match("", "") is None, "an empty setting matches nothing")


def test_era_selection_swaps_the_whole_cast():
    section("F1 — an era overrides the cast entirely, not adds to it")
    # "Sengoku era → cast = Yoriichi + Michikatsu, not current Hashira." A
    # different era of the same setting can share almost no names with the
    # default - Sengoku-era Demon Slayer predates Tanjiro's generation by
    # centuries. Asked for it, the grounding brief the builder actually
    # reads must carry Yoriichi, not Zenitsu.
    options = canon_seed.era_options("demon slayer", "Demon Slayer: Kimetsu no Yaiba")
    ids = {o["id"] for o in options}
    ok(ids == {"present", "sengoku"}, f"both eras are offered ({ids})")

    present_cast = {c["name"] for c in canon_seed.fallback_cast("demon slayer", era="present")}
    sengoku_cast = {c["name"] for c in canon_seed.fallback_cast("demon slayer", era="sengoku")}
    ok("Tanjiro Kamado" in present_cast and "Tanjiro Kamado" not in sengoku_cast,
       "present-day protagonist is NOT in the Sengoku cast")
    ok("Yoriichi Tsugikuni" in sengoku_cast and "Yoriichi Tsugikuni" not in present_cast,
       "and the Sengoku protagonist is not in the present-day cast — this "
       "is a swap, not a merge")
    ok(not (present_cast & sengoku_cast) or "Muzan Kibutsuji" in (present_cast & sengoku_cast),
       "the two eras share almost no names, as the setting actually implies")

    pinned = canon_seed.pin_protagonists(
        "demon slayer", "Demon Slayer: Kimetsu no Yaiba",
        [{"name": "Michikatsu Tsugikuni", "note": ""}], era="sengoku")
    ok(pinned[0]["name"] == "Yoriichi Tsugikuni",
       "the era's OWN protagonist is pinned first, not the default setting's")

    # No era given, or an unmatched setting: nothing changes, exactly as
    # before F1 existed.
    ok(canon_seed.fallback_cast("demon slayer") == canon_seed.fallback_cast("demon slayer", era=""),
       "no era requested falls back to the setting's default cast")
    ok(canon_seed.era_options("a setting nobody wrote") == [],
       "an unmatched setting offers no era question at all")

    # The grounding brief the builder actually reads carries the swap.
    from backend import sessionzero
    dossier = {"setting": "demon slayer", "canonical_name": "Demon Slayer: Kimetsu no Yaiba",
              "found": True, "characters": sorted(
                  ({"name": n, "note": ""} for n in sengoku_cast), key=lambda c: c["name"]),
              "places": [], "factions": [], "sources": []}
    b = sessionzero.brief({"era": "sengoku"}, dossier)
    ok("ERA: The Sengoku era" in b,
       "the chosen era reaches the builder as an explicit instruction")
    ok("do not mix in anyone" in b,
       "warning it away from blending eras, which a model would otherwise "
       "happily do given both casts share a franchise name")


def test_cache_shape():
    section("cache — a setting is researched once")
    db.init()
    research._cache_put("research:probe", {"found": True, "setting": "probe"})
    got = research._cache_get("research:probe")
    ok(got and got["setting"] == "probe", "a dossier round-trips through the cache")
    ok(research._cache_get("research:never-seen") is None,
       "an unknown setting is a miss, not a crash")


def test_confidence_gate():
    section("correctness — an ORIGINAL setting must find nothing")
    from backend.research import _confident
    # Wikipedia's search always returns something, so the gate - not the
    # search - is what decides whether a setting is real.
    real = [("Demon Slayer", "Demon Slayer: Kimetsu no Yaiba"),
            ("Naruto", "Naruto"),
            ("The Witcher", "The Witcher"),
            ("Star Wars", "Star Wars"),
            ("Attack on Titan", "Attack on Titan")]
    for setting, title in real:
        ok(_confident(setting, title), f"recognised: {setting!r} -> {title!r}")

    # These are the ACTUAL top hits Wikipedia returns for those phrases.
    invented = [("a frozen post-collapse Earth", "Earth"),
                ("a frozen post-collapse Earth", "Apocalyptic and post-apocalyptic fiction"),
                ("a village that forgets its own name", "Name"),
                ("the last lighthouse on a drowned coast", "Lighthouse"),
                ("a city built inside a whale", "Whale")]
    for setting, title in invented:
        ok(not _confident(setting, title),
           f"refused: {setting[:34]!r} is NOT {title!r}")

    ok(not _confident("a frozen post-collapse Earth", "Earth"),
       "a one-word title inside a long phrase is not a match — the substring "
       "test only runs in the safe direction")


def test_two_modes():
    section("modes — the player chooses; the system does not guess")
    db.init()
    # "original" must not look ANYTHING up, even for a famous name.
    orig = socket.getaddrinfo
    touched = []

    def trap(*a, **k):
        touched.append(a[0] if a else "?")
        raise AssertionError("network touched in original mode")

    socket.getaddrinfo = trap
    try:
        w = worldforge.bootstrap("Demon Slayer", user_id="mode-orig", mode="original")
        ok(w["mode"] == "original", "a famous name in ORIGINAL mode stays original")
        ok(w.get("researched") is False and not w.get("sources"),
           "nothing is looked up and nothing is attributed")
        ok(not touched, "zero network calls — 'build my own' means exactly that")
    finally:
        socket.getaddrinfo = orig

    # Mode is validated, not trusted.
    w2 = worldforge.bootstrap("somewhere quiet", user_id="mode-bad", mode="nonsense")
    ok(w2["mode"] in ("original", "canon"),
       "an unknown mode falls back to a valid one rather than erroring")

    # In mock mode nothing resolves, so auto correctly lands on original.
    w3 = worldforge.bootstrap("Naruto", user_id="mode-auto", mode="auto")
    ok(w3["mode"] == "original",
       "AUTO with nothing found is original — an original world is the other "
       "mode, not a failed canon one")


def _all():
    return (test_allowlist, test_resolved_ip_guard, test_fetcher_guards,
            test_article_ranking, test_wiki_identity, test_slug_generation,
            test_grounding_brief, test_the_brief_never_licenses_invention,
            test_canon_supply_reaches_the_prompt,
            test_canon_is_dealt_across_districts_not_raced_for,
            test_unused_canon_is_seated_not_merely_labelled,
            test_a_canon_character_is_not_handed_over_as_an_ordinary_mortal,
            test_the_character_the_player_named_actually_turns_up,
            test_an_empty_room_is_told_it_is_empty,
            test_a_spoken_line_becomes_a_plate_the_way_people_are_named,
            test_the_small_details_a_fan_would_notice,
            test_the_chapter_list_is_not_starved_by_the_request_budget,
            test_chapters_survive_a_wiki_that_never_answers,
            test_a_wiki_is_asked_what_categories_it_has,
            test_arcs_are_put_in_the_order_the_source_tells_them,
            test_the_narrator_stays_inside_the_story,
            test_character_list_furniture,
            test_confidence_gate, test_two_modes, test_offline_is_hermetic,
            test_canon_seed_fallback, test_era_selection_swaps_the_whole_cast,
            test_cache_shape)


def main():
    print("StoryLiver — live canon research")
    print("  no network touched, no key, no spend\n")
    for fn in _all():
        fn()
    passed = 0
    for n in NOTES:
        if n.startswith("\n"):
            print(n)
        else:
            print("  PASS  " + n); passed += 1
    for f in FAILS:
        print("  FAIL  " + f)
    print(f"\n  {passed} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


def test_all_research():
    for fn in _all():
        fn()
    assert not FAILS, "\n".join(FAILS)


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
