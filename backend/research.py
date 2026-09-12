"""Live canon research - the World Bootstrap's eyes.

Bootstrap used to build a named world purely from what the model already knew.
For a famous setting that looks fine, but it is guessing: the model cannot tell
you whether it is confusing two characters, it has no idea what it does not
know, and anything after its training cut-off simply does not exist to it. This
module goes and looks.

WHAT IT FETCHES, AND WHY THOSE SOURCES
Wikipedia and Fandom, both of which run MediaWiki and expose a real JSON API.
That matters more than it sounds: we ask the API for structured fields rather
than scraping rendered HTML, so we get titles, categories and plain-text
extracts instead of a pile of markup to regex at. Both are CC-BY-SA, so
attribution is recorded on every world built this way.

WHAT IT DELIBERATELY DOES NOT DO
It does not copy prose into the game. What crosses from the web into a world is
a DOSSIER OF FACTS - names, roles, places, in-world vocabulary - plus one short
grounding summary that is capped hard and never handed to the narrator. Facts
are not copyrightable; paragraphs are. This keeps the existing copyright posture
intact (an IP-derived world is still personal_only and still forced private)
while making the world materially more accurate.

SECURITY: this is the one place the server fetches a URL, so it is the one
place that can be turned into a server-side request forgery. Three controls,
because any one of them alone is bypassable:

  1. A fixed host allowlist. The player supplies a SETTING NAME, never a URL,
     so there is no user-controlled host to begin with - but the allowlist means
     even a bug upstream cannot make this fetch somewhere else.
  2. Every resolved IP is checked against private, loopback, link-local and
     carrier-grade-NAT ranges before we connect. A hostname allowlist alone
     falls to DNS rebinding: a domain that resolves to a public IP at check time
     and 169.254.169.254 at connect time passes a name check and hits the cloud
     metadata endpoint.
  3. The connection is PINNED to the IP we validated, with the hostname carried
     in SNI and certificate verification. That closes the gap between "we
     checked the name" and "we opened the socket", which is where rebinding
     lives.

Redirects are not followed. Responses are size-capped and read incrementally, so
a hostile or broken endpoint cannot stream us out of memory.

MANNERS: Wikimedia asks for a descriptive User-Agent with contact details,
serial rather than parallel requests, `maxlag` so we back off when their
replication is behind, and respect for `Retry-After` on 429. All four are here.
Results are cached in SQLite so the same setting is never researched twice.

COST: zero model calls. This is HTTP and parsing. It runs BEFORE the two
existing bootstrap calls and changes what they are told, not how many there are.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
from urllib.parse import quote, urlparse

from . import canon_seed, config, db

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

# Hosts we will talk to. Suffix match, so "en.wikipedia.org" and
# "naruto.fandom.com" both pass while "wikipedia.org.evil.test" does not.
ALLOWED_SUFFIXES = (
    ".wikipedia.org",
    ".wikimedia.org",
    ".fandom.com",
    "wikipedia.org",
    "wikidata.org",
)

MAX_BYTES = 2 * 1024 * 1024          # a MediaWiki JSON reply is far under this
MAX_REQUESTS = 16                    # hard ceiling per dossier, so latency is bounded
CACHE_DAYS = 30                      # canon does not move fast
EXTRACT_CHARS = 1200                 # grounding summary cap - facts, not prose
SNIPPET_CHARS = 180                  # per-entity note cap

CONTACT = config.RESEARCH_CONTACT
USER_AGENT = f"StoryLiver-WorldForge/1.0 ({CONTACT}) python-httpx"


class ResearchError(RuntimeError):
    pass


class BlockedHost(ResearchError):
    pass


def enabled() -> bool:
    """Off in mock mode so the test suite stays hermetic, offline and $0, and
    off entirely if someone deploys without egress."""
    if config.RESEARCH in ("0", "off", "false", "no"):
        return False
    return config.LLM_MODE != "mock"


# ---------------------------------------------------------------------------
# SSRF-safe fetching
# ---------------------------------------------------------------------------

def _host_allowed(host: str) -> bool:
    host = (host or "").lower().strip(".")
    return any(host == s.lstrip(".") or host.endswith(s)
               for s in ALLOWED_SUFFIXES)


def _public_ips(host: str) -> list:
    """Resolve, then reject anything that is not a normal public address.

    Checked BEFORE connecting and then pinned, because a name that passes a
    check and a name that is connected to are not guaranteed to be the same
    address - that gap is exactly what DNS rebinding exploits."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ResearchError(f"cannot resolve {host}: {e}")

    good = []
    for family, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise BlockedHost(f"{host} resolves to a non-public address ({ip})")
        # 100.64.0.0/10 - carrier-grade NAT, routable inside a provider and a
        # real path to internal services on some hosts.
        if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
            raise BlockedHost(f"{host} resolves into carrier-grade NAT ({ip})")
        good.append(str(ip))
    if not good:
        raise ResearchError(f"no usable address for {host}")
    return good


class _Budget:
    """A dossier gets a fixed number of requests, and ONE connection per host.

    The ceiling stops an odd setting walking a wiki forever while a player
    waits. The connection pool matters just as much: a fresh client per request
    means a fresh DNS lookup and a fresh TLS handshake every time, which was
    the bulk of the wall clock on a ten-request dossier."""

    def __init__(self, limit=MAX_REQUESTS):
        self.left = limit
        self._clients = {}

    def take(self):
        if self.left <= 0:
            raise ResearchError("research budget exhausted")
        self.left -= 1

    def client(self, host, ip, timeout):
        """One pinned, verified connection per host, kept open for the dossier."""
        import httpx
        if host not in self._clients:
            self._clients[host] = httpx.Client(
                timeout=timeout, follow_redirects=False, verify=True,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                         "Host": host, "Accept-Encoding": "gzip"},
                limits=httpx.Limits(max_connections=2, keepalive_expiry=30.0))
        return self._clients[host]

    def close(self):
        for c in self._clients.values():
            try:
                c.close()
            except Exception:
                pass
        self._clients.clear()


def _get_json(url: str, params: dict, budget: _Budget, *, timeout=None,
              attempts=3) -> dict:
    import httpx

    timeout = config.RESEARCH_TIMEOUT if timeout is None else timeout
    budget.take()
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise BlockedHost("research is HTTPS-only")
    host = parsed.hostname or ""
    if not _host_allowed(host):
        raise BlockedHost(f"{host} is not an allowed research source")

    ip = _public_ips(host)[0]
    # Connect to the validated ADDRESS, carrying the hostname in SNI and in the
    # certificate check. Nothing between validating and connecting can swap it.
    pinned = parsed._replace(netloc=ip).geturl()

    client = budget.client(host, ip, timeout)

    for attempt in range(attempts):
        try:
            r = client.get(pinned, params=params,
                           extensions={"sni_hostname": host})
        except httpx.HTTPError as e:
            if attempt == attempts - 1:
                raise ResearchError(f"{host} unreachable: {e}")
            time.sleep(0.6 * (attempt + 1))
            continue

        # Wikimedia signals overload properly; honour it rather than hammering.
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", "2") or 2)
            time.sleep(min(wait, 5.0))
            continue
        if r.status_code in (301, 302, 303, 307, 308):
            raise ResearchError("redirect refused - the source moved")
        if r.status_code >= 400:
            raise ResearchError(f"{host} returned {r.status_code}")

        if len(r.content) > MAX_BYTES:
            raise ResearchError("response too large")
        try:
            data = r.json()
        except ValueError:
            raise ResearchError(f"{host} did not return JSON")
        # maxlag: their replication is behind, so back off and retry.
        if isinstance(data, dict) and data.get("error", {}).get("code") == "maxlag":
            time.sleep(1.5)
            continue
        return data

    raise ResearchError(f"{host} kept asking us to wait")


def _api(host: str, params: dict, budget: _Budget, *, quick=False) -> dict:
    """One MediaWiki Action API call, with the etiquette parameters set."""
    base = {"format": "json", "formatversion": "2", "maxlag": "5"}
    # A "quick" probe is cheap and expected to fail OFTEN - it is guessing
    # subdomains - but one of those guesses decides whether the entire Fandom
    # half of research happens at all. At attempts=1 and a 3s ceiling it was
    # losing that one on a COLD run: fresh DNS, fresh TLS, no pooled
    # connection. Reproduced exactly - a build against a warm cache came back
    # with the wiki, 18 places and 12 arcs, and the same build against an empty
    # one came back with none of it, every time. The wiki itself answers in
    # under 300ms once anything is warm.
    return _get_json(f"https://{host}/w/api.php" if "wikipedia" in host
                     else f"https://{host}/api.php",
                     {**base, **params}, budget,
                     attempts=2 if quick else 3,
                     timeout=8.0 if quick else None)


# ---------------------------------------------------------------------------
# Wikipedia: identify the work
# ---------------------------------------------------------------------------

STOPWORDS = {"the", "a", "an", "of", "and", "no", "wiki", "fandom", "series",
             "manga", "anime", "game", "franchise", "universe", "encyclopedia",
             "in", "on", "at", "to", "for", "with", "its", "it", "that", "this",
             "own", "from", "by", "as", "is", "are", "was", "were"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _tokens(s: str) -> set:
    return {t for t in _norm(s).split() if t and t not in STOPWORDS}


def identify(setting: str, budget: _Budget) -> dict | None:
    """Find the Wikipedia article that IS this setting.

    Ranked rather than first-hit, because a search for a work returns the work,
    its episode lists, its films and its characters, and taking result [0] lands
    on the wrong one often enough to matter. Disambiguation pages are then
    RESOLVED rather than accepted - "Demon Slayer" is a disambiguation page, and
    treating it as the answer produces a world built from "may refer to:"."""
    data = _api("en.wikipedia.org", {
        "action": "query", "list": "search", "srsearch": setting,
        "srlimit": 10, "srnamespace": 0,
    }, budget)
    hits = (data.get("query") or {}).get("search") or []
    if not hits:
        return None

    best = _rank(setting, [h.get("title", "") for h in hits], hits)
    if not best or not _confident(setting, best):
        # Wikipedia's search ALWAYS returns something. "a frozen post-collapse
        # Earth" comes back as "Apocalyptic fiction", "Frozen 2", "Earth" -
        # all real articles, none of them this setting. Accepting the top hit
        # would ground an ORIGINAL world in an unrelated article and quietly
        # make it worse than not researching at all. An original setting
        # SHOULD find nothing; that is the correct answer, not a failure.
        return None

    resolved = _resolve_disambiguation(setting, best, budget)
    return {"title": resolved, "score": 0}


def _confident(setting: str, title: str) -> bool:
    """Is this article plausibly the SAME THING the player named?"""
    want, got = _tokens(setting), _tokens(title)
    if not want:
        return False
    n = _norm(setting)
    t = _norm(title)
    # `n in t` is the safe direction: "demon slayer" inside "demon slayer:
    # kimetsu no yaiba" is a real match. The REVERSE is not - "earth" sits
    # inside "a frozen post-collapse earth" and means nothing, which is
    # exactly how an original setting gets grounded on the wrong article.
    if n and (n == t or n in t):
        return True
    # Otherwise most of what the player typed has to appear in the title.
    return len(want & got) >= max(1, int(round(len(want) * 0.6)))


def _rank(setting: str, titles: list, hits: list | None = None) -> str | None:
    want = _norm(setting)
    want_tokens = _tokens(setting)
    counts = {h.get("title", ""): h.get("wordcount", 0) for h in (hits or [])}

    best, best_score = None, -1e9
    for title in titles:
        if not title or title.startswith(("Category:", "File:", "Template:")):
            continue
        t = _norm(title)
        score = 0.0
        if t == want:
            score += 100
        elif want and (want in t or t in want):
            score += 45
        # Token overlap catches "Demon Slayer: Kimetsu no Yaiba" for "demon slayer".
        overlap = len(want_tokens & _tokens(title))
        score += 14 * overlap
        # Pages ABOUT the work beat pages derived from it.
        low = title.lower()
        for bad, penalty in (("list of", 50), ("episode", 40), ("season", 34),
                             ("soundtrack", 34), ("discography", 34),
                             ("(film", 14), ("(video game", 14), ("(disambiguation", 8)):
            if bad in low:
                score -= penalty
        score += min(18, counts.get(title, 0) / 1200.0)
        if score > best_score:
            best, best_score = title, score
    return best


def _resolve_disambiguation(setting: str, title: str, budget: _Budget) -> str:
    """A disambiguation page is a signpost, not a destination.

    Detected properly via pageprops rather than by sniffing for "may refer to",
    which is a phrasing that varies. When we land on one, the page's own links
    are the candidate list and the same ranking picks from them."""
    if budget.left <= 2:
        return title
    try:
        data = _api("en.wikipedia.org", {
            "action": "query", "prop": "pageprops|links", "titles": title,
            "ppprop": "disambiguation", "pllimit": 40, "plnamespace": 0,
        }, budget)
    except ResearchError:
        return title

    pages = (data.get("query") or {}).get("pages") or []
    if not pages:
        return title
    page = pages[0]
    if "disambiguation" not in (page.get("pageprops") or {}):
        return title

    links = [l.get("title", "") for l in (page.get("links") or [])]
    return _rank(setting, links) or title


def summarise(title: str, budget: _Budget) -> dict:
    """The intro, as plain text, hard-capped. This grounds the generator in what
    the setting actually IS; it never reaches the narrator."""
    data = _api("en.wikipedia.org", {
        "action": "query", "prop": "extracts|info", "titles": title,
        "exintro": "1", "explaintext": "1", "inprop": "url",
    }, budget)
    pages = (data.get("query") or {}).get("pages") or []
    if not pages:
        return {}
    p = pages[0]
    text = re.sub(r"\s+", " ", p.get("extract", "") or "").strip()
    return {"title": p.get("title", title),
            "summary": text[:EXTRACT_CHARS],
            "url": p.get("fullurl", ""),
            "source": "Wikipedia", "license": "CC BY-SA 4.0"}


def characters_from_wikipedia(setting: str, canonical: str, budget: _Budget) -> list:
    """The fallback when no Fandom wiki is found or trusted.

    Most works of any size have a "List of <work> characters" article, and its
    section headings are the character names - which is exactly the fact we
    want and nothing more."""
    if budget.left <= 2:
        return []
    try:
        data = _api("en.wikipedia.org", {
            "action": "query", "list": "search",
            "srsearch": f"List of {canonical or setting} characters",
            "srlimit": 3, "srnamespace": 0,
        }, budget)
    except ResearchError:
        return []
    hits = [h.get("title", "") for h in (data.get("query") or {}).get("search") or []]
    page = next((t for t in hits if t.lower().startswith("list of")), None)
    if not page:
        return []
    try:
        sec = _api("en.wikipedia.org", {"action": "parse", "page": page,
                                        "prop": "sections"}, budget)
    except ResearchError:
        return []
    names = []
    for s in (sec.get("parse") or {}).get("sections") or []:
        line = re.sub(r"\s*\(.*?\)\s*", "", (s.get("line") or "")).strip()
        # Real names, not "Reception" or "See also".
        low = line.lower()
        # A character list article is mostly people, but it also has
        # "Creation and conception", "Secondary characters" and team groupings.
        # Those are real headings and completely wrong as character names.
        furniture = ("reception", "see also", "reference", "main", "other",
                     "minor", "recurring", "list", "note", "cast", "creation",
                     "conception", "development", "character", "media",
                     "introduce", "overview", "background", "summary",
                     "antagonist", "protagonist", "supporting", "secondary",
                     "villain", "team", "group", "organi", "clan", "family",
                     "appear", "adaptation", "reaction", "analysis",
                     "external", "link", "further", "bibliograph", "appendix",
                     "content", "source", "index", "gallery", "trivia",
                     "concept", "casting", "voice", "legacy", "merchand",
                     "film", "television", "novel", "comic", "game", "series")
        if (2 <= len(line.split()) <= 4 and line[:1].isupper()
                and not any(w in low for w in furniture)
                and not re.search(r"\d", line)):
            names.append(line)
    # Was 14. Wikipedia's cast list is the main cast by construction, so a
    # name cut here is a real character the builder had to replace with an
    # invented one.
    clean = _dedupe(names)[:26]
    return [{"name": n, "note": ""} for n in clean] if len(clean) >= 3 else []


# ---------------------------------------------------------------------------
# Fandom: the deep canon
# ---------------------------------------------------------------------------

def _slug_candidates(setting: str, wiki_title: str = "") -> list:
    """Fandom subdomains are guessable, but only if you try the CANONICAL title
    and its subtitle too. "Demon Slayer" alone finds `demon.fandom.com`, which
    is a real wiki about something else entirely; the actual one is keyed to
    the subtitle, "Kimetsu no Yaiba"."""
    seeds = []
    if wiki_title:
        clean = re.sub(r"\s*\(.*?\)\s*", "", wiki_title).strip()
        parts = [x.strip() for x in re.split(r"[:–—]", clean) if len(x.strip()) > 3]
        # Subtitle first: for anime and manga the wiki is almost always named
        # after it ("Kimetsu no Yaiba Wiki"), not the western title, and a
        # western-title guess can land on a real but unrelated wiki.
        seeds.extend(parts[1:])
        seeds.append(clean)
        seeds.extend(parts[:1])
    seeds.append(setting)

    out = []
    for seed in seeds:
        base = _norm(seed)
        if not base:
            continue
        # Leading articles are dropped from most wiki subdomains.
        stripped = re.sub(r"^(the|a|an)\s+", "", base)
        for form in (base, stripped):
            for cand in (form.replace(" ", ""), form.replace(" ", "-")):
                if cand and cand not in out:
                    out.append(cand)
    return out[:7]


def _wiki_is_about(sitename: str, setting: str, canonical: str, slug: str = "") -> bool:
    """Guard against confidently fetching the wrong wiki.

    A slug guess can land on a real, busy, completely unrelated wiki, and
    everything downstream would then look like it worked. Requiring the site's
    own name to share vocabulary with the setting is what stops a Demon Slayer
    world being built out of a different franchise's characters."""
    site = _tokens(sitename)
    if not site:
        return False

    # An EXACT slug match is evidence in itself: we guessed the whole name and
    # a real wiki answered. `naruto` -> "Narutopedia" passes here; `demon` for
    # "Demon Slayer" does not, because it is not the whole name.
    if slug:
        flat = slug.replace("-", "")
        for candidate in (canonical, setting):
            cand_flat = _norm(candidate).replace(" ", "")
            stripped = re.sub(r"^(the|a|an)", "", cand_flat)
            if flat and flat in (cand_flat, stripped):
                return True

    for candidate in (canonical, setting):
        want = _tokens(candidate)
        if not want:
            continue
        overlap = len(site & want)
        # A short name has to match in FULL. At 50% a two-word setting accepts
        # any wiki sharing one word, which is how "Demon Wiki" passes for
        # "Demon Slayer" - a real wiki about entirely the wrong thing.
        needed = len(want) if len(want) <= 2 else max(2, int(round(len(want) * 0.5)))
        if overlap and overlap >= needed:
            return True
        if _norm(candidate) and _norm(candidate) in _norm(sitename):
            return True
    return False


def find_wiki(setting: str, wiki_title: str, budget: _Budget,
              *, trace: list | None = None) -> str | None:
    """Probe candidate subdomains, and only accept one that is demonstrably
    about this setting.

    `trace` collects WHY each candidate was rejected. Without it this function
    returns None for six different reasons - unreachable, blocked, 403, wrong
    sitename, budget gone, no candidates - and reports all of them as an empty
    string. A production dossier came back `wiki: ""` for weeks; the wiki was
    answering in under 300ms the whole time, and two separate fixes went in
    against causes that were never happening because there was nothing to read.
    A failure that cannot say why is a failure that gets guessed at."""
    cands = _slug_candidates(setting, wiki_title)
    if trace is not None and not cands:
        trace.append("no subdomain candidates for that name")
    for slug in cands:
        if budget.left <= 5:
            if trace is not None:
                trace.append(f"budget exhausted before trying {slug}")
            break
        host = f"{slug}.fandom.com"
        try:
            # Probes are cheap and expected to fail, so no retry budget.
            data = _api(host, {"action": "query", "meta": "siteinfo",
                               "siprop": "general"}, budget, quick=True)
        except ResearchError as e:
            if trace is not None:
                trace.append(f"{host}: {e}")
            continue
        sitename = ((data.get("query") or {}).get("general") or {}).get("sitename", "")
        if sitename and _wiki_is_about(sitename, setting, wiki_title, slug):
            return host
        if trace is not None:
            trace.append(f"{host}: answered as {sitename!r}, which is not this setting"
                         if sitename else f"{host}: no sitename in reply")
    return None


# What each bucket's categories actually LOOK LIKE, rather than what we hoped
# they were called. Every wiki files things its own way, and guessing fixed
# names failed silently: the Demon Slayer wiki has no "Category:Arcs" at all -
# it has 783 categories including "Mugen Train Arc", "Final Selection Arc" and
# "Mount Natagumo Arc", each an arc in its own right. Asking the wiki what
# categories it HAS and matching them is the difference between reading a wiki
# and hoping it is shaped like the last one.
CATEGORY_PATTERNS = {
    "characters": re.compile(r"^(characters|(male|female|human|demon) characters)$", re.I),
    "arcs": re.compile(r"(^|\s)arcs?$|^(story arcs|sagas)$", re.I),
    "places": re.compile(r"^(locations|places|buildings|countries|villages|cities)$", re.I),
    "factions": re.compile(r"(organi[sz]ations|groups|corps|families|clans)$", re.I),
    "powers": re.compile(r"(abilities|powers|techniques|breathing styles|"
                         r"combat styles|cursed techniques|magic)$", re.I),
}


_ARC_CHAPTERS = re.compile(r"\|\s*chapters?\s*=[^\n]*?(\d+)", re.I)
_ARC_EPISODES = re.compile(r"\|\s*episodes?\s*=[^\n]*?(\d+)", re.I)
_ARC_ORDINAL = re.compile(
    r"\bis the\s+(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"eleventh|twelfth|thirteenth|fourteenth|fifteenth)\b", re.I)
_ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh",
             "eighth", "ninth", "tenth", "eleventh", "twelfth", "thirteenth",
             "fourteenth", "fifteenth"]


def order_arcs(host: str, names: list, budget: _Budget) -> list:
    """Put arc names into the order the SOURCE tells them, not alphabetical.

    A wiki lists categories alphabetically, so "Mugen Train" came back third
    and "Mount Natagumo" seventh when canon runs Natagumo first. Asking a model
    to sort them is approximate, and approximate is wrong: a reader who knows
    the series sees a wrong running order instantly.

    The arc articles carry the answer exactly. Their infoboxes have

        |chapters = [[Chapter 54|54]] - [[Chapter 66|66]]

    so the first chapter number sorts them perfectly, and the prose says "is
    the seventh story arc" as a second witness. One batched request for every
    arc page, and the order is the source's own."""
    titles = [n if n.lower().endswith(" arc") else f"{n} Arc" for n in names if n]
    if not titles or budget.left <= 2:
        return list(names)
    try:
        data = _api(host, {"action": "query", "prop": "revisions",
                           "rvprop": "content", "rvslots": "main",
                           "titles": "|".join(titles[:24])}, budget)
    except ResearchError:
        return list(names)

    rank: dict = {}
    for page in ((data.get("query") or {}).get("pages") or []):
        title = str(page.get("title") or "")
        try:
            text = page["revisions"][0]["slots"]["main"]["content"]
        except (KeyError, IndexError, TypeError):
            continue
        m = _ARC_CHAPTERS.search(text) or _ARC_EPISODES.search(text)
        if m:
            rank[title] = int(m.group(1))
            continue
        o = _ARC_ORDINAL.search(text)
        if o:
            # Ordinals are a separate, coarser scale: keep them after anything
            # with a real chapter number rather than interleaving the two.
            rank[title] = 100000 + _ORDINALS.index(o.group(1).lower())

    if not rank:
        return list(names)

    def key(i_name):
        i, name = i_name
        title = name if name.lower().endswith(" arc") else f"{name} Arc"
        # Anything the wiki could not place keeps its original position, after
        # everything it could - never silently reordered on a guess.
        return (rank.get(title, 10 ** 9), i)

    return [n for _, n in sorted(enumerate(names), key=key)]


def all_categories(host: str, budget: _Budget, *, pages: int = 2) -> list:
    """Every category this wiki actually has, cheaply.

    One `allcategories` call returns up to 500, which covers most wikis in one
    or two requests - far fewer than probing a handful of guessed category
    names one at a time and getting nothing back."""
    out, cont = [], None
    for _ in range(max(1, pages)):
        if budget.left <= 3:
            break
        params = {"action": "query", "list": "allcategories",
                  "aclimit": "500", "acmin": "2"}
        if cont:
            params["accontinue"] = cont
        try:
            data = _api(host, params, budget)
        except ResearchError:
            break
        out.extend(c.get("category") if isinstance(c, dict) else str(c)
                   for c in ((data.get("query") or {}).get("allcategories") or []))
        cont = ((data.get("continue") or {}).get("accontinue"))
        if not cont:
            break
    return [c for c in out if c]


def categories_for(bucket: str, available: list) -> list:
    """The categories on THIS wiki that belong to this bucket."""
    pat = CATEGORY_PATTERNS.get(bucket)
    if not pat:
        return []
    hits = [c for c in available if pat.search(c)]
    # An arc category IS an arc - "Mugen Train Arc" names the thing itself -
    # so there can be dozens and they are all wanted. The other buckets are
    # containers, and a handful is plenty.
    return hits if bucket == "arcs" else hits[:4]


CATEGORY_SETS = {
    "characters": ("Category:Characters", "Category:Male Characters",
                   "Category:Female Characters"),
    # SECOND, not last. This decides the world's CHAPTERS - the running order a
    # canon story is told in - and it used to sit at the bottom of this dict,
    # behind places, factions and powers, with the loop below breaking out the
    # moment the request budget ran low. It reliably never got fetched: a live
    # Demon Slayer build came back with no arcs at all and fell back to a
    # single chapter named after the town, which is the whole chapter feature
    # silently absent. Arcs are also the cheapest bucket here - no per-entity
    # describe() pass - so moving them up costs almost nothing.
    "arcs": ("Category:Arcs", "Category:Story Arcs", "Category:Sagas",
             "Category:Seasons", "Category:Events"),
    "places": ("Category:Locations", "Category:Places", "Category:Locations by type"),
    "factions": ("Category:Organizations", "Category:Factions", "Category:Groups"),
    # THE POWER SYSTEM is what actually limits a canon world. Power-scaling
    # research is explicit that SYSTEMIC scaling - breaking a series' own
    # system down (cursed energy, Haki, breathing styles) and placing everyone
    # inside it - is what works, and that the failure mode of canon RPGs is a
    # protagonist whose power is undefined relative to the cast.
    "powers": ("Category:Abilities", "Category:Powers", "Category:Techniques",
               "Category:Magic", "Category:Combat Styles", "Category:Cursed Techniques"),
}

# How many of each bucket to keep. Characters carry the world; a power list of
# 40 techniques is noise the player will never read.
# How much canon survives ranking. These were 16/12, tuned when a world was
# one model call asking for 10-12 characters. A world-scale build asks for
# 48-64, so every name cut here became a name the builder invented instead.
BUCKET_KEEP = {"characters": 26, "places": 18, "factions": 14,
               "powers": 10, "arcs": 12}

# How much of the dossier reaches the prompt. Separate from BUCKET_KEEP so the
# dossier can hold more than any single pass shows.
BRIEF_KEEP = {"characters": 26, "places": 18, "factions": 12}


def category_members(host: str, category: str, budget: _Budget, limit=40) -> list:
    try:
        data = _api(host, {"action": "query", "list": "categorymembers",
                           "cmtitle": category, "cmlimit": limit,
                           "cmnamespace": 0}, budget)
    except ResearchError:
        return []
    return [m.get("title", "") for m in
            (data.get("query") or {}).get("categorymembers") or [] if m.get("title")]


def by_importance(host: str, titles: list, budget: _Budget, keep=16) -> list:
    """Category listings come back ALPHABETICALLY, which is the worst possible
    order for this: asking a wiki for its characters returns "A (First
    Raikage)", "Abiru", "Ada" - real names, and entirely the wrong ones.

    Article LENGTH is a good proxy for how central a character is, and
    `prop=info` reports it. One batched call turns an alphabetical list into a
    main-cast list."""
    if not titles:
        return []
    sizes = {}
    for chunk in (titles[i:i + 50] for i in range(0, min(len(titles), 100), 50)):
        if budget.left <= 2:
            break
        try:
            data = _api(host, {"action": "query", "prop": "info",
                               "titles": "|".join(chunk)}, budget)
        except ResearchError:
            break
        for pg in (data.get("query") or {}).get("pages") or []:
            if pg.get("title"):
                sizes[pg["title"]] = int(pg.get("length") or 0)
    if not sizes:
        return titles[:keep]
    return sorted(titles, key=lambda t: -sizes.get(t, 0))[:keep]


def describe(host: str, titles: list, budget: _Budget) -> dict:
    """One batched call for up to 20 short descriptions. Batching is the
    difference between one request and twenty."""
    if not titles:
        return {}
    try:
        data = _api(host, {"action": "query", "prop": "extracts",
                           "titles": "|".join(titles[:20]),
                           "exintro": "1", "explaintext": "1",
                           "exlimit": "20"}, budget)
    except ResearchError:
        return {}
    out = {}
    for p in (data.get("query") or {}).get("pages") or []:
        text = re.sub(r"\s+", " ", p.get("extract", "") or "").strip()
        if text:
            out[p.get("title", "")] = text[:SNIPPET_CHARS]
    return out


# ---------------------------------------------------------------------------
# The dossier
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Premise parsing - a player writes a sentence, not a title
# ---------------------------------------------------------------------------
# The identifier was built on one assumption: `setting` is the NAME OF ONE
# WORK. Real players do not type "The Last of Us". They type
#
#     "I and Charlie from Hazbin Hotel are inside the world of The Last of Us"
#
# which names two real properties and a character, and describes a crossover.
# Against that sentence the confidence gate needed 60% of thirteen tokens to
# appear in an article title, which no title on earth does - so a wholly canon
# premise came back "Nothing found for that name."
#
# So a premise is PARSED rather than searched. The parts are pulled out
# deterministically, each is researched on its own, and what comes back is a
# host world plus the characters the player wants carried into it.

# A proper-noun run. Connectives are allowed INSIDE a name ("Attack on Titan",
# "The Lord of the Rings") but only when a capitalised word follows them, so
# "Jujutsu Kaisen in the world of..." stops at Kaisen rather than swallowing
# the rest of the sentence. Smart quotes are normalised on the way in, so the
# character class stays plain ASCII and readable.
_CONNECTIVE = r"(?:of|the|and|in|on|at|to|de|von|van|no|le|la|du)"
_PROPER = (r"((?:The\s+)?[A-Z][\w'\-]*"
           r"(?:\s+(?:" + _CONNECTIVE + r"\s+)*[A-Z0-9][\w'\-]*)*)")
_HOST_PATTERNS = (
    rf"\b(?:world|universe|setting|reality|timeline|continuity)\s+of\s+{_PROPER}",
    rf"\bset\s+in\s+{_PROPER}",
    rf"\b(?:inside|within|into|in)\b\s+(?:the\s+)?(?:world\s+of\s+)?{_PROPER}",
)

# "X from Y" - a character carried in from somewhere else.
_IMPORT_PATTERN = rf"{_PROPER}\s+from\s+{_PROPER}"

# Words that begin a sentence and are capitalised for that reason alone.
_NOT_PROPER = {
    "i", "we", "my", "me", "you", "the", "a", "an", "and", "but", "so", "then",
    "it", "its", "this", "that", "there", "here", "what", "when", "where", "who",
    "how", "why", "am", "is", "are", "was", "were", "be", "being", "been",
    "have", "has", "had", "do", "does", "did", "can", "could", "would", "will",
    "let", "make", "makes", "want", "wants", "play", "playing", "story", "world",
    "universe", "setting", "character", "characters", "everyone", "someone",
}


# Only these are stripped from the FRONT of a captured name. Articles are
# deliberately absent: "The Last of Us" and "The Lord of the Rings" carry their
# article as part of the title, while "I and Charlie" is a pronoun and a
# conjunction that a regex cannot tell from a name.
_LEAD_STRIP = {
    "i", "we", "my", "me", "you", "and", "but", "so", "then", "it", "its",
    "this", "that", "am", "is", "are", "was", "were", "let", "play", "playing",
    "want", "wants", "as",
}


def looks_like_premise(text: str) -> bool:
    """A sentence, or a title?

    A title is short and has no verb. Anything with a first-person pronoun, a
    linking verb, or more than about six words is somebody describing what they
    want rather than naming a work - and describing is the case the old path
    could not represent at all."""
    t = (text or "").strip()
    if not t:
        return False
    words = t.split()
    if len(words) > 6:
        return True
    low = " " + t.lower() + " "
    return any(m in low for m in (" i ", " we ", " my ", " me ", " am ", " are ",
                                  " is ", " from ", " inside ", " within ",
                                  " world of ", " universe of ", " set in "))


def _clean_entity(raw: str) -> str:
    name = re.sub(r"\s+", " ", (raw or "").strip(" .,!?;:\"'"))
    # Trailing connective words the pattern may have swept up.
    name = re.sub(r"\s+(?:are|is|am|was|were|and|but|who|which|that)$", "", name,
                  flags=re.I)
    # "I and Charlie" is one capitalised run to a regex and two words to a
    # person. Strip leading words that are capitalised only because they opened
    # the sentence, until something that reads like a name is in front.
    parts = name.split()
    while parts and parts[0].lower() in _LEAD_STRIP:
        parts.pop(0)
    name = " ".join(parts)
    if not name or name.lower() in _NOT_PROPER or len(name) < 3:
        return ""
    return name


def parse_premise(text: str) -> dict:
    """Pull the named properties out of what the player actually wrote.

    Deterministic and $0 - no model call. Returns the host world, the
    characters being carried in and where they are from, and every proper name
    found, so the builder can be told about all of them even when a lookup
    finds nothing."""
    raw = (text or "").strip().replace("\u2019", "'").replace("\u2018", "'")
    out = {"raw": raw, "is_premise": looks_like_premise(raw),
           "host": "", "imports": [], "entities": [],
           # Did we EXTRACT a proper name, or merely echo the input back?
           # "a frozen post-collapse Earth" is a host string and not evidence
           # of anybody's property; treating the two the same made every
           # original setting look like canon and quietly forced it private.
           "host_is_proper": False}
    if not raw:
        return out
    if not out["is_premise"]:
        # A bare title is its own host and needs no parsing. Whether it is a
        # real property is research's question, not the parser's.
        out["host"] = raw
        out["entities"] = [raw]
        return out

    seen = []

    def remember(name):
        name = _clean_entity(name)
        if name and _norm(name) not in {_norm(x) for x in seen}:
            seen.append(name)
        return name

    for who, where in re.findall(_IMPORT_PATTERN, raw):
        character, source = remember(who), remember(where)
        if character and source:
            out["imports"].append({"character": character, "from": source})

    for pattern in _HOST_PATTERNS:
        m = re.search(pattern, raw)
        if m:
            host = remember(m.group(1))
            if host and host not in [i["from"] for i in out["imports"]]:
                out["host"] = host
                out["host_is_proper"] = True
                break

    # Everything else that reads as a proper name, so nothing the player typed
    # is silently dropped.
    for m in re.finditer(_PROPER, raw):
        remember(m.group(1))

    out["entities"] = seen
    if not out["host"] and seen:
        # No "world of X" phrasing. The last named property is the likeliest
        # host - "Charlie from Hazbin Hotel, The Last of Us" reads that way -
        # and being wrong here costs an ordering, not a lookup.
        tail = [e for e in seen if e not in [i["character"] for i in out["imports"]]]
        out["host"] = tail[-1] if tail else seen[-1]
        out["host_is_proper"] = True
    return out


def premise_dossier(text: str, *, refresh: bool = False, depth: str = "full") -> dict:
    """Research every property named in a premise, not just the whole string.

    The result always carries the parse, so the world builder knows what the
    player asked for EVEN WHEN NOTHING IS FOUND. That is the important half:
    research is an enhancement, and a failed lookup must never turn a canon
    crossover into "build something original"."""
    parsed = parse_premise(text)
    out = {**_empty(text, ""), "premise": parsed, "entities": {}}

    if not parsed["entities"]:
        out["note"] = "no named properties in that"
        return out

    # Bounded: a premise naming six franchises would otherwise be six full
    # research passes and a minute of latency.
    lookups = []
    if parsed["host"]:
        lookups.append(parsed["host"])
    for imp in parsed["imports"]:
        for name in (imp["from"], imp["character"]):
            if name not in lookups:
                lookups.append(name)
    for name in parsed["entities"]:
        if name not in lookups:
            lookups.append(name)

    host_d = None
    for name in lookups[:4]:
        d = dossier(name, refresh=refresh, depth=depth)
        out["entities"][name] = d
        if d.get("found"):
            out["sources"].extend(d.get("sources") or [])
            if host_d is None and name == parsed["host"]:
                host_d = d

    # The host's own dossier is promoted to the top level so every existing
    # caller (grounding, attribution, Session Zero) keeps working unchanged.
    if host_d is None:
        host_d = next((d for d in out["entities"].values() if d.get("found")), None)
    if host_d:
        for key in ("canonical_name", "summary", "wiki", "characters", "places",
                    "factions", "powers", "arcs"):
            out[key] = host_d.get(key) or out.get(key)
    out["found"] = any(d.get("found") for d in out["entities"].values())
    out["note"] = "" if out["found"] else "nothing found for any of those names"
    return out


def premise_brief(d: dict) -> str:
    """What the world architect is told about a crossover.

    Written so that a lookup MISS still produces a usable instruction: the
    model already knows most fiction, and the old path threw that away by
    telling it to build something original the moment research came back
    empty."""
    parsed = d.get("premise") or {}
    if not parsed.get("raw"):
        return ""
    lines = [f"WHAT THE PLAYER ASKED FOR, IN THEIR WORDS:\n  {parsed['raw']}"]

    if parsed.get("host"):
        lines.append(
            f"HOST WORLD: {parsed['host']}. This is where the story happens - its "
            f"places, its rules, its dangers.")
    for imp in parsed.get("imports") or []:
        lines.append(
            f"CARRIED IN: {imp['character']}, from {imp['from']}. They are HERE now, "
            f"in the host world, as themselves - the same voice, the same values, "
            f"the same limits. Do not rewrite them into a local character and do "
            f"not explain how they arrived.")

    unfound = [name for name, ent in (d.get("entities") or {}).items()
               if not ent.get("found")]
    if unfound:
        # The whole point of the rewrite. A name we could not look up is still
        # a name the model very likely knows.
        lines.append(
            "NOT FOUND IN RESEARCH, BUT NAMED BY THE PLAYER: "
            + ", ".join(unfound)
            + ". Use everything you already know about them. If you genuinely do "
              "not know one, build it faithfully from what the player wrote "
              "rather than replacing it with something of your own.")
    return "\n\n".join(lines)


def dossier(setting: str, *, refresh: bool = False, depth: str = "full") -> dict:
    """Everything we could learn about a named setting, cached.

    Never raises: research is an ENHANCEMENT. If the network is down, the
    setting is invented, or a wiki is missing, bootstrap must still produce a
    playable world from the model's own knowledge - just a less grounded one."""
    setting = (setting or "").strip()
    if not setting:
        return _empty(setting, "no setting given")
    if not enabled():
        return _empty(setting, "research disabled")

    # The type-ahead preview and the actual build want different things. QUICK
    # answers "is this a real setting?" from Wikipedia alone in a few requests;
    # FULL goes on to find the wiki and pull its categories. Cached separately,
    # so a fast preview never becomes the shallow basis for a built world.
    depth = "quick" if depth == "quick" else "full"
    key = f"research:{depth}:" + _norm(setting)
    if not refresh:
        cached = _cache_get(key)
        if cached is not None:
            cached["cached"] = True
            return cached

    budget = _Budget()
    out = _empty(setting, "")
    out["depth"] = depth
    try:
        found = identify(setting, budget)
        if not found:
            # identify() itself can fail before any cast-building code runs -
            # a DNS/egress problem on the very first lookup. The seed table
            # is checked here too, so a network outage doesn't have to mean
            # an empty cast for a franchise this file already knows.
            out["note"] = "nothing found under that name"
            out = _seed_fallback(out, setting)
            _cache_put(key, out)
            return out

        page = summarise(found["title"], budget)
        out["canonical_name"] = page.get("title", found["title"])
        out["summary"] = page.get("summary", "")
        if page.get("url"):
            out["sources"].append({"title": page["title"], "url": page["url"],
                                   "source": "Wikipedia", "license": page["license"]})

        # Curated main cast BEFORE any wiki probing: this is the highest-value
        # data in the dossier, and probing wrong subdomains must never be what
        # starves it.
        curated = characters_from_wikipedia(setting, out["canonical_name"], budget)

        wiki_trace: list = []
        host = (find_wiki(setting, out["canonical_name"], budget, trace=wiki_trace)
                if depth == "full" else None)
        # Recorded on the dossier whether or not it worked, because "no wiki"
        # with no reason attached is what made this undiagnosable.
        if not host:
            out["wiki_note"] = "; ".join(wiki_trace)[:400] or "no fandom wiki probed"
        if host:
            out["wiki"] = host
            out["sources"].append({"title": f"{out['canonical_name']} Wiki",
                                   "url": f"https://{host}",
                                   "source": "Fandom", "license": "CC BY-SA 3.0"})
            # Ask the wiki what it HAS before asking it for anything. Guessing
            # fixed category names is how every Fandom bucket came back empty:
            # this wiki has 783 categories and not one of them is "Arcs".
            available = all_categories(host, budget)
            for bucket, guesses in CATEGORY_SETS.items():
                found_cats = categories_for(bucket, available) if available else []

                # An arc category IS the arc. "Mugen Train Arc" does not need
                # its members fetched to tell you the Mugen Train arc exists,
                # and fetching them would return every character who appeared
                # in it instead.
                if bucket == "arcs" and found_cats:
                    arc_names = [re.sub(r"\s*Arcs?$", "", c).strip() or c
                                 for c in found_cats[:16]]
                    # Alphabetical is not a running order. The arc articles say
                    # which chapter each one starts at; that is the real one.
                    arc_names = order_arcs(host, arc_names, budget)
                    out[bucket] = [{"name": n, "note": ""} for n in arc_names[:12]]
                    continue

                names = []
                for cat in (found_cats or list(guesses)):
                    cat = cat if cat.lower().startswith("category:") else f"Category:{cat}"
                    names.extend(category_members(host, cat, budget,
                                                  limit=200 if bucket == "characters" else 120))
                    if len(names) >= 30 or budget.left <= 4:
                        break
                # Rank before truncating: the top 18 alphabetically is noise,
                # the top 18 by article size is the cast.
                names = by_importance(host, _dedupe(names), budget,
                                      keep=BUCKET_KEEP.get(bucket, 12))
                notes = describe(host, names, budget) if bucket == "characters" else {}
                out[bucket] = [{"name": n, "note": notes.get(n, "")} for n in names]
                if budget.left <= 1:
                    break

        # The curated list LEADS. A wiki category is exhaustive and
        # alphabetical, so for a large franchise its first page never reaches
        # the protagonist; Wikipedia's list is the main cast by construction.
        if curated:
            seen = {_norm(c["name"]) for c in curated}
            out["characters"] = curated + [c for c in out["characters"]
                                           if _norm(c["name"]) not in seen]

        # FALLBACK: live research found the setting but came back with too
        # thin a cast/place list to build from (a wiki timeout, a blocked
        # host, an obscure subdomain) - this is what used to fall through to
        # the model inventing names (Kaname, Aiko - never in Demon Slayer) and
        # locations. A curated local roster for the handful of franchises
        # this happens to constantly beats an empty bucket every time.
        if len(out["characters"]) < 4:
            seeded = canon_seed.fallback_cast(setting, out["canonical_name"])
            if seeded:
                have = {_norm(c["name"]) for c in out["characters"]}
                out["characters"] = out["characters"] + [
                    c for c in seeded if _norm(c["name"]) not in have]
        if len(out["places"]) < 2:
            seeded_places = canon_seed.fallback_places(setting, out["canonical_name"])
            if seeded_places:
                have = {_norm(p["name"]) for p in out["places"]}
                out["places"] = out["places"] + [
                    p for p in seeded_places if _norm(p["name"]) not in have]

        # PIN: even a healthy cast can rank the real protagonist below a
        # shorter-article side character - a Tanjiro-less Demon Slayer build
        # is not Demon Slayer regardless of how the wiki ranked it.
        out["characters"] = canon_seed.pin_protagonists(
            setting, out["canonical_name"], out["characters"])

        out["found"] = bool(out["summary"] or out["characters"])
        out["fetched_at"] = db.now()
    except ResearchError as e:
        out["note"] = str(e)
        # A raised failure can land here from ANYWHERE in the try block above
        # - including identify() itself, before the fallback logic that runs
        # on a clean `None` return never got a chance to fire.
        out = _seed_fallback(out, setting)
    except Exception as e:                     # never break a world build
        out["note"] = f"research failed: {type(e).__name__}"
        out = _seed_fallback(out, setting)

    finally:
        budget.close()

    _cache_put(key, out)
    return out


def _seed_fallback(out, setting):
    """Applied wherever live research produced too little to build from -
    identify() returning nothing, identify() raising, or any later step in
    the dossier raising before the cast was assembled. Mutates and returns
    `out` so every call site can just `out = _seed_fallback(out, setting)`.

    Covers characters AND places: the same failure (a category-fetch timeout)
    empties both buckets, and grounding_brief() has a REAL PLACES section that
    goes just as silent as REAL CHARACTERS when this isn't filled."""
    canonical = out.get("canonical_name", "")
    seeded_cast = canon_seed.fallback_cast(setting, canonical)
    seeded_places = canon_seed.fallback_places(setting, canonical)
    if seeded_cast:
        out["characters"] = canon_seed.pin_protagonists(setting, canonical, seeded_cast)
        out["found"] = True
        if not out.get("note"):
            out["note"] = "identified locally; live lookup found nothing"
    if seeded_places and not out.get("places"):
        out["places"] = seeded_places
    return out


def _dedupe(names):
    seen, out = set(), []
    for n in names:
        n = (n or "").strip()
        k = _norm(n)
        # Wiki category listings are full of maintenance and index pages.
        if not k or k in seen or n.startswith(("List of", "Category:", "File:")):
            continue
        seen.add(k)
        out.append(n)
    return out


def _empty(setting, note):
    return {"setting": setting, "canonical_name": "", "found": False,
            "summary": "", "wiki": "", "characters": [], "places": [],
            "factions": [], "powers": [], "arcs": [], "sources": [],
            "note": note, "cached": False, "depth": "full", "fetched_at": ""}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_get(key):
    row = db.row("SELECT payload, fetched_at FROM research_cache WHERE key=?", (key,))
    if not row:
        return None
    try:
        age_days = (time.time() - float(row["fetched_at"])) / 86400.0
    except (TypeError, ValueError):
        age_days = 999
    if age_days > CACHE_DAYS:
        db.run("DELETE FROM research_cache WHERE key=?", (key,))
        return None
    return db.jload(row["payload"], None)


def _cache_put(key, payload):
    db.run("INSERT INTO research_cache (key,payload,fetched_at) VALUES (?,?,?)"
           " ON CONFLICT(key) DO UPDATE SET payload=excluded.payload,"
           " fetched_at=excluded.fetched_at",
           (key, json.dumps(payload), str(time.time())))


# ---------------------------------------------------------------------------
# What the generator is told
# ---------------------------------------------------------------------------

def grounding_brief(d: dict, *, need_npcs: int = 0, need_locs: int = 0) -> str:
    """Turn a dossier into instructions for the world architect.

    Names and roles only. The wording is deliberate: the model is told these are
    REAL and must be used, which is the whole point - an ungrounded build
    invents 'Tanjiro Kamada' and a grounded one does not.

    `need_npcs`/`need_locs` are how many the CURRENT pass was asked for. They
    exist because the old brief ended with "Invent freely for anything not
    listed" while a world-scale build asks for 48-64 characters against a
    roster of 9-14 - so the model was being handed a shortfall of forty people
    and an explicit licence to make them up. Telling it the budget, and that
    the roster must be exhausted first, is the difference between a cast that
    is mostly real and one that is mostly invented."""
    if not d.get("found"):
        return ""
    lines = ["RESEARCHED CANON (gathered live from public wikis - these are REAL "
             "names from this setting, not suggestions):"]
    if d.get("canonical_name"):
        lines.append(f"CANONICAL TITLE: {d['canonical_name']}")
    if d.get("summary"):
        lines.append(f"WHAT IT IS: {d['summary'][:700]}")

    for bucket, label in (("characters", "REAL CHARACTERS"),
                          ("places", "REAL PLACES"),
                          ("factions", "REAL ORGANISATIONS")):
        items = d.get(bucket) or []
        if not items:
            continue
        rendered = []
        # Was 14 across the board. A researched franchise routinely yields more
        # real names than that, and every one truncated here is a name the
        # builder then had to invent a replacement for.
        for it in items[:BRIEF_KEEP.get(bucket, 14)]:
            name = (it.get("name") or "").strip()
            if not name:                      # a blank entry teaches nothing
                continue
            rendered.append(f"- {name}" + (f": {it['note'][:110]}" if it.get("note") else ""))
        if not rendered:
            continue
        lines.append(f"{label}:\n" + "\n".join(rendered))

    n_chars = len([c for c in (d.get("characters") or []) if (c.get("name") or "").strip()])
    n_places = len([p for p in (d.get("places") or []) if (p.get("name") or "").strip()])
    budget = [
        "HOW TO USE THAT LIST - this is a hard rule, not a preference:",
        f"1. PEOPLE: every one of those {n_chars} real characters must be used before you "
        "invent a single new person. Spell them exactly as written above.",
        f"2. PLACES: the {n_places} real places are the setting's WHOLE MAP, and you are "
        "building ONE settlement on it. Build only what a person can walk between in an "
        "evening: the streets, rooms and thresholds of this one place. A landmark that "
        "is a journey away in the source does NOT belong inside it - a town containing "
        "the Mugen Train, the Butterfly Mansion and the Ubuyashiki Estate at once is a "
        "map of the whole series folded into one square. Those stay where they are; the "
        "player travels to them later, and leaving them out costs nothing. Never hang a "
        "famous name on an ordinary room to get it used either - a tavern called "
        "Yoshiwara and a clinic called Mount Kumotori are lies a reader spots instantly. "
        "Name the streets here the way the people who live here would.",
        "3. Never rename, re-spell, translate or 'improve' a real name, and never invent "
        "a relative, student, rival or successor of a real character.",
    ]
    # The shortfall is the whole problem, so name it rather than leaving the
    # model to discover it and fill the gap however it likes.
    short_n = max(0, need_npcs - n_chars)
    short_l = max(0, need_locs - n_places)
    if short_n or short_l:
        budget.append(
            f"4. This pass asks for more than the roster holds, so you must invent about "
            f"{short_n} extra people and {short_l} extra places - and ONLY that many. "
            "Everyone you invent is an ORDINARY BACKGROUND RESIDENT of this setting: a "
            "stallholder, a courier, a gate guard, someone's aunt. Never a new hero, "
            "never a new villain, never a new named power or technique, never anyone who "
            "could rival or outrank a real character."
        )
    else:
        budget.append(
            "4. The roster covers everything this pass asks for. Do not invent anyone new."
        )
    budget.append(
        "5. Do NOT copy any sentence from the research text - write your own descriptions "
        "of these real people and places."
    )
    budget.append(
        "6. Keep the source's PERIOD and its technology. Whatever era, dress, lighting, "
        "weapons and transport the source uses, this world uses. A detail from the wrong "
        "century - neon over a Taisho street, a phone in a sword age - is as wrong as a "
        "made-up name, and a reader notices it faster."
    )
    budget.append(
        "7. The real places above are the setting's GEOGRAPHY, not this location's "
        "contents. Build somewhere a person can actually walk around in one evening: "
        "streets, rooms, thresholds, the buildings that belong to each other. Famous "
        "landmarks from across the whole source do not all sit inside one town, and "
        "listing them as if they did reads as a tour of the franchise rather than a "
        "place to live in. Use the ones that genuinely belong here; the rest are "
        "elsewhere, and can be travelled to."
    )
    lines.append("\n".join(budget))
    return "\n\n".join(lines)


def attribution(d: dict) -> list:
    """Recorded on the world, and shown to the player. CC BY-SA asks for it, and
    a player deserves to know where the world came from."""
    return [{"title": s.get("title", ""), "url": s.get("url", ""),
             "source": s.get("source", ""), "license": s.get("license", "")}
            for s in (d.get("sources") or [])]
