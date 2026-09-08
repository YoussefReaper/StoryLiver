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
    ok("anchors" not in seated and "seed_memories" not in seated,
       "a real character does not inherit the invented person's goals and memories")
    ok("Water Hashira" in seated["role"],
       "they arrive with what research actually knows about them")
    ok(out["locations"][0]["name"] == "Butterfly Mansion",
       "the same holds for places")

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
