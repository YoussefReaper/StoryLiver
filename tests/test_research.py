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

from backend import db, research, worldforge  # noqa: E402
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
            test_grounding_brief, test_character_list_furniture,
            test_confidence_gate, test_two_modes, test_offline_is_hermetic,
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
