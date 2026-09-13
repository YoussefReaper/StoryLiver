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


# A proxy is an egress point somebody else configured on purpose. When one is
# set, the dossier must route through it: a request pinned to a resolved IP
# connects directly and cannot use a proxy at all, which is how a working
# network produced "unreachable" for every lookup.
def _proxy_for(host: str) -> str:
    """The proxy configured for this host, or "" for a direct connection.

    Reads the same environment httpx itself would, so a deployment that sets
    HTTPS_PROXY (or the lowercase form, or NO_PROXY to exempt a host) gets the
    behaviour it asked for rather than a silently direct connection. Removed
    from the module-level client cache key too, so changing the proxy does not
    silently reuse a connection opened under the old one."""
    import os
    keys = (f"no_proxy", f"NO_PROXY")
    for k in keys:
        for entry in (os.environ.get(k) or "").split(","):
            entry = entry.strip().lower().lstrip(".")
            if entry and (host == entry or host.endswith("." + entry)):
                return ""
    for k in ("https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    return ""


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
        """One connection per host, kept open for the dossier.

        IP-PINNED BY DEFAULT, because that is the SSRF defence `_get_json`
        depends on: validate the address, then connect to that exact address,
        so nothing between the check and the connection can swap it.

        BUT a pinned request goes to the address directly and therefore cannot
        use a proxy - the whole point of a proxy is that it is the thing you
        connect TO, and it makes the outbound connection for you. On a host
        behind one (a corporate egress proxy, a sandbox, a metered relay) the
        pinned socket is refused and every wiki lookup fails with "unreachable",
        which reads as a network problem and is not one.

        So when a proxy is actually configured, the proxy wins and pinning is
        dropped - a proxy is itself a validated egress point, and refusing to
        route through it is not more secure, it is just broken. Verified by the
        two symptoms this had: `502 Bad Gateway` from httpx, and Wikipedia's
        own `403 Please set a user-agent` when the same request went direct."""
        import httpx
        if host not in self._clients:
            pinned = not _proxy_for(host)
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json",
                       "Accept-Encoding": "gzip"}
            if pinned:
                # Only meaningful while pinning - it names the host on a socket
                # already opened to that host's address.
                headers["Host"] = host
            self._clients[host] = httpx.Client(
                timeout=timeout, follow_redirects=False, verify=True,
                headers=headers, trust_env=not pinned,
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

    proxy = _proxy_for(host)
    if proxy:
        # Behind a proxy, the proxy resolves and connects on our behalf, so the
        # address check below cannot pin anything - and trying to would send
        # the request straight to the IP, past the only route out. The host is
        # still validated by `_host_allowed` above, and TLS still verifies the
        # certificate, so the name reached is the name asked for.
        target, sni = url, {}
    else:
        ip = _public_ips(host)[0]
        # Connect to the validated ADDRESS, carrying the hostname in SNI and in
        # the certificate check. Nothing between validating and connecting can
        # swap it.
        target = parsed._replace(netloc=ip).geturl()
        sni = {"sni_hostname": host}

    client = budget.client(host, None, timeout)

    for attempt in range(attempts):
        try:
            r = client.get(target, params=params, extensions=sni)
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


# A category whose entire name is just the CONCEPT ("Story Arcs", "Arcs",
# "Sagas") is a container, not an arc. Reading it as one arc was a live bug:
# Vinland Saga's wiki has exactly one arc-shaped category, `Story Arcs`, whose
# name stripped to "Story" - so the whole chapter book for a whole franchise
# was a single chapter called "Story", and every arc the source actually has
# was invisible. A name this bare carries no information about a story beat,
# so it is descended into rather than used.
_ARC_CONTAINER = re.compile(r"^(story\s*)?arcs?$|^sagas?$|^seasons?$|^events?$", re.I)


def _arc_is_container(name: str) -> bool:
    """A category with no name beyond the concept is a box, not an arc."""
    return bool(_ARC_CONTAINER.match((name or "").strip()))


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

# A name as a human actually types it. _PROPER demands capitalisation, and
# players write "spongbob square pants", "gojo", "thanos" - so a property
# typed in lowercase was invisible to EVERY host pattern and the parser fell
# back to guessing from capitalised words. Used only directly after an
# explicit host trigger ("world of X", "goes to X"), where the trigger - not
# the capitalisation - is what makes the next words a name. It stops at the
# words that end a name (which/who/with/and...) so "spongbob square pants
# which also have thanos" yields the property and not the whole sentence.
_CONNECTIVE_MIN = r"(?:of|the|in|on|at|to|de|von|van|no|le|la|du)"
_STOP = (r"(?:which|who|whom|whose|where|when|while|that|with|and|but|also"
         r"|then|than|have|has|had|is|are|was|were|will|would|can|could"
         r"|should|because|there|their|our|my|his|her|its|goes|go)")
_LOOSE = (r"((?:The\s+)?[A-Za-z][\w'\-]*"
          r"(?:\s+(?:" + _CONNECTIVE_MIN + r"\s+)*"
          r"(?!" + _STOP + r"\b)[A-Za-z0-9][\w'\-]*){0,3})")
# "verse" is how people actually say it. "in the verse of Demon Slayer" was
# not in this list, so no host was extracted at all and a crossover silently
# fell back to research's highest-ranked franchise - which is usually the
# IMPORT's own home, i.e. exactly backwards from what the player meant.
_HOST_PATTERNS = (
    rf"\b(?:world|universe|verse|realm|setting|reality|timeline|continuity"
    rf"|cosmos|series)\s+of\s+{_PROPER}",
    rf"\bset\s+in\s+{_PROPER}",
    rf"\b(?:inside|within|into|in)\b\s+(?:the\s+)?(?:world\s+of\s+)?{_PROPER}",
)

# The same triggers, tolerating lowercase. Tried only after the strict set
# finds nothing, so a capitalised name is never downgraded. "goes to X" is
# here because that is how a player describes arriving somewhere and it was
# not a host phrase at all - "I ... goes to spongbob square pants" parsed no
# host, no imports and no entities, i.e. the whole premise was dropped.
_LOOSE_HOST_PATTERNS = (
    rf"\b(?:world|universe|verse|realm|setting|reality|timeline|continuity"
    rf"|cosmos|series)\s+of\s+{_LOOSE}",
    rf"\b(?:go(?:es|ing)?|travel(?:s|ling|led)?|arrive[sd]?|head(?:s|ing)?"
    rf"|venture[sd]?|move[sd]?|teleport(?:ed|s)?|warp(?:ed|s)?|sent"
    rf"|return(?:s|ed)?|wake)\s+(?:to|into|in)\s+(?:the\s+)?{_LOOSE}",
    rf"\bset\s+in\s+{_LOOSE}",
)

# "X from Y" - a character carried in from somewhere else.
_IMPORT_PATTERN = rf"{_PROPER}\s+from\s+{_PROPER}"

# "charlie (from hazbin hotel)" - the same statement, typed the way most
# people actually type it. `_IMPORT_PATTERN` knows one shape and the player
# wrote the other one, so the character the premise is ABOUT was never carried
# in at all: no seat, no persona, no friction line. The parenthesis is the
# strongest signal in a premise that a source is being named, and it survives
# lowercase typing, which capitalisation-based matching cannot.
_PAREN_SOURCE = re.compile(r"\(\s*(?:from\s+)?([^()\[\]]{2,60}?)\s*\)", re.I)

# Words that are capitalised for being the first word of a title rather than
# for being a name - kept lowercase when a typed-all-lowercase franchise is
# given back its capitals.
_SMALL_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
    "nor", "of", "on", "or", "the", "to", "vs", "with",
}

# Did the player bring this character, or merely ask for them?
#
# "I and my girlfriend charlie (from hazbin hotel)" and "Charlie from Hazbin
# Hotel" name the same character, and they are not the same premise: in the
# first she arrived WITH the player, in the second she is a stranger who
# happens to be in the story. The build treated both as the second - so a
# character the player called their girlfriend was seated correctly, in the
# right room, with a relationship of -5 affinity and -5 trust, i.e. she
# disliked her own partner on turn one. Nothing downstream could recover from
# that, because the relationship engine was faithfully simulating a stranger.
_COMPANION_MARKERS = re.compile(
    r"\b(?:my|our)\s+(?:girlfriend|boyfriend|wife|husband|partner|fianc[eé]e?|"
    r"best\s+friend|friend|brother|sister|son|daughter|companion|buddy|gf|bf)\b"
    r"|\b(?:i|me)\s+and\b|\bwe\b|\bwith\s+me\b|\btogether\b", re.I)

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
    if any(m in low for m in (" i ", " we ", " my ", " me ", " am ", " are ",
                              " is ", " from ", " inside ", " within ",
                              " world of ", " universe of ", " set in ")):
        return True
    # "charlie (from hazbin hotel)" and "Charlie (Hazbin Hotel) in Demon
    # Slayer" are a crossover being described, not a work being named - and
    # both are under the word count, so the rules above missed them and the
    # parser returned early with the whole sentence as the "title". A bare
    # parenthetical is not evidence on its own ("Kimetsu no Yaiba (manga)"),
    # so it counts only with an explicit "from" or a host phrase beside it.
    if re.search(r"\(\s*from\s+", t, re.I):
        return True
    if re.search(r"[(\[]", t) and any(re.search(p, t) for p in _HOST_PATTERNS):
        return True
    return False


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


def _titleish(raw: str) -> str:
    """Give a typed-all-lowercase name its capitals back.

    "hazbin hotel" is how the player typed it and "Hazbin Hotel" is what the
    franchise is called. Idempotent on an already-correct name, and it leaves
    small words alone except at the ends, so "The Last of Us" survives
    unchanged rather than becoming "The Last Of Us"."""
    words = [w for w in re.split(r"\s+", (raw or "").strip()) if w]
    out = []
    for i, w in enumerate(words):
        if 0 < i < len(words) - 1 and w.lower() in _SMALL_WORDS:
            out.append(w.lower())
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


def _name_before(text: str, pos: int) -> str:
    """The character named immediately before a parenthesis.

    The word touching the bracket is the reliable half, and it is the only
    half when the player typed in lowercase: "my girlfriend charlie (from
    hazbin hotel)" has no capitals to go on and "girlfriend" is not part of
    anybody's name. Capitalised words directly in front ARE part of it, so
    "Charlie Morningstar (Hazbin Hotel)" is one person and not a surname.
    """
    toks = re.findall(r"[A-Za-z][\w'\-]*", text[:pos])
    if not toks:
        return ""
    keep = [toks[-1]]
    for t in reversed(toks[:-1]):
        if t[:1].isupper() and t.lower() not in _NOT_PROPER:
            keep.insert(0, t)
        else:
            break
    name = " ".join(keep)
    if not name or name.lower() in _NOT_PROPER or len(name) < 3:
        return ""
    return name


_RELATION_WORDS = (r"girlfriend|boyfriend|partner|wife|husband|fianc[eé]|"
                   r"lover|best friend|close friend|friends|friend|brother|"
                   r"sister|mentor|rival|enemy|ally|companion|sidekick|"
                   r"bodyguard|acquaintance")

_REL_BEFORE = (rf"\b(?:my|our|his|her|their)\s+({_RELATION_WORDS})\b\s*,?\s*"
               rf"({_LOOSE})")
_REL_AFTER = (rf"({_LOOSE})\s*,?\s+who\s+(?:is|are)\s+"
              rf"(?:my|our|his|her|their)\s+({_RELATION_WORDS})\b")
_WITH_LIST = rf"\bwith\s+({_LOOSE}(?:\s*(?:,|and)\s*{_LOOSE})*)"
_GOAL = (r"(?:whose|who(?:'s)?)\s+(?:goal|aim|plan|mission|objective|dream"
         r"|purpose|ambition)\s+is\s+to\s+([^.,;]+)")
_WANTS = r"\bwho\s+wants?\s+to\s+([^.,;]+)"


def _attach_import(out: dict, name: str, relation: str = "",
                   with_player: bool | None = None) -> dict:
    """Record a person the premise named exactly once, merging what we learn
    about them from each phrase that mentions them."""
    for imp in out["imports"]:
        if _norm(imp.get("character", "")) == _norm(name):
            if relation and not imp.get("relation"):
                imp["relation"] = relation
            if with_player:
                imp["with_player"] = True
            return imp
    imp = {"character": name, "from": "", "with_player": bool(with_player)}
    if relation:
        imp["relation"] = relation
    out["imports"].append(imp)
    return imp


def _relations_and_goals(raw: str, out: dict, remember) -> None:
    """Relationships, companion groups and stated goals.

    All of this was being dropped: the parser knew exactly one shape,
    "Character from Source", and a relationship is not a source. So "my
    girlfriend charlie morningstar", "with gojo and geto who are our friends"
    and "thanos whose goal is to remove half the universe" produced nothing at
    all - no seat, no persona, no drive.

    A girlfriend is not a travelling companion and a rival is not a friend.
    If the difference never reaches the build, the world cannot treat them
    differently, which is the whole point of naming them.
    """
    for m in re.finditer(_REL_BEFORE, raw, re.I):
        rel = (m.group(1) or "").lower()
        name = _titleish(_clean_entity(m.group(2)))
        if name:
            _attach_import(out, remember(name), relation=rel, with_player=True)

    for m in re.finditer(_REL_AFTER, raw, re.I):
        name = _titleish(_clean_entity(m.group(1)))
        if name:
            _attach_import(out, remember(name),
                           relation=(m.group(2) or "").lower(), with_player=True)

    for m in re.finditer(_WITH_LIST, raw, re.I):
        rel = ""
        rm = re.search(rf"who\s+(?:is|are)\s+(?:my|our|his|her|their)\s+"
                       rf"({_RELATION_WORDS})\b", raw[m.end():m.end() + 60], re.I)
        if rm:
            rel = rm.group(1).lower()
        for part in re.split(r"\s*(?:,|and)\s*", m.group(1) or ""):
            name = _titleish(_clean_entity(part))
            if name:
                _attach_import(out, remember(name), relation=rel, with_player=True)

    # "thanos whose goal is to X" - a drive the world has to actually run.
    for pat in (_GOAL, _WANTS):
        for m in re.finditer(pat, raw, re.I):
            goal = (m.group(1) or "").strip(" .")
            if not goal:
                continue
            before = raw[:m.start()]
            target = None
            for imp in out["imports"]:
                nm = _norm(imp.get("character", ""))
                if nm and nm in _norm(before):
                    target = imp
            if target is None:
                pm = re.search(rf"({_LOOSE})\s*,?\s+(?:whose|who)\b", before, re.I)
                if pm:
                    name = _titleish(_clean_entity(pm.group(1)))
                    if name:
                        target = _attach_import(out, remember(name))
            if target is not None:
                target["goal"] = goal


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

    # The parenthesised shape, which is how a crossover is usually typed:
    # "charlie (from hazbin hotel)", "Charlie (Hazbin Hotel)". Checked BEFORE
    # the host patterns so the parenthesised name is never mistaken for the
    # world the story is set in.
    for m in _PAREN_SOURCE.finditer(raw):
        character = _titleish(_name_before(raw, m.start()))
        source = _titleish(_clean_entity(m.group(1)))
        if not character or not source:
            continue
        character = remember(character)
        source = remember(source)
        if any(_norm(i["character"]) == _norm(character) for i in out["imports"]):
            continue
        out["imports"].append({"character": character, "from": source})

    # Relationships, companion groups and goals - see _relations_and_goals.
    # Runs after the "X from Y" shapes so a person is recorded once and then
    # enriched, rather than being duplicated by every phrase naming them.
    _relations_and_goals(raw, out, remember)

    for pattern in (*_HOST_PATTERNS, *_LOOSE_HOST_PATTERNS):
        m = re.search(pattern, raw)
        if m:
            host = remember(m.group(1))
            # Not the host if the premise brought that person WITH it.
            # "Charlie Morningstar at the world of spongbob square pants" made
            # Charlie the world she is standing in.
            taken = {x for i in out["imports"]
                     for x in ((i.get("from") or ""), (i.get("character") or ""))
                     if x}
            if host and host not in taken:
                out["host"] = host
                out["host_is_proper"] = True
                break

    # A source that ran straight through the word introducing the host. `in`
    # is a connective inside a title ("Made in Abyss"), so "Charlie from
    # Hazbin Hotel in Demon Slayer" captured the whole run as the source and
    # the host ended up inside it. Trim it back only when the leftover half is
    # itself a franchise the roster can name - otherwise "Made in Abyss" would
    # be cut down to "Made", which is a worse answer than the bug.
    host = (out.get("host") or "").strip()
    if host:
        for imp in out["imports"]:
            src = imp.get("from") or ""
            m = re.match(
                rf"^(.*?)\s+(?:in|inside|within|into)\s+{re.escape(host)}$", src, re.I)
            trimmed = (m.group(1).strip() if m else "")
            if trimmed and canon_seed.known(trimmed) and not canon_seed.known(src):
                imp["from"] = trimmed

    # Everything else that reads as a proper name, so nothing the player typed
    # is silently dropped.
    for m in re.finditer(_PROPER, raw):
        remember(m.group(1))

    # Did the player bring them, or just ask for them? Recorded per import
    # because the build needs to know: the first is a relationship that already
    # exists, the second is a stranger. See _COMPANION_MARKERS.
    came_with = bool(_COMPANION_MARKERS.search(raw))
    for imp in out["imports"]:
        imp["with_player"] = came_with

    out["entities"] = seen
    if not out["host"] and seen:
        # No "world of X" phrasing. The last named property is the likeliest
        # host - "Charlie from Hazbin Hotel, The Last of Us" reads that way -
        # and being wrong here costs an ordering, not a lookup.
        tail = [e for e in seen if e not in [i["character"] for i in out["imports"]]]
        out["host"] = tail[-1] if tail else seen[-1]
        out["host_is_proper"] = True
    return out


PREMISE_SYSTEM = """You read ONE sentence a player typed describing the story they want. You return structured JSON and nothing else - no prose, no commentary, no markdown fences.

Return exactly this shape:
{
  "host": "the world the story is SET IN",
  "imports": [
    {"character": "name, as the player wrote it",
     "from": "that person's home world, or empty if the sentence never says",
     "relation": "who they are to the player, in your own words",
     "goal": "what that person wants, or empty"}
  ]
}

Rules that matter:
- `host` is a PLACE, not a person, and never the home a guest came FROM. "Charlie Morningstar in the world of SpongeBob" has host "SpongeBob"; Charlie is an import.
- `relation` is free text and the nuance is the entire point. "enemies but we have respect" is a DIFFERENT relation from "enemy" and must not be flattened. "my girlfriend" is not "a companion". Keep what the player actually meant.
- One entry per person the sentence really names. Never invent anybody.
- If the sentence names a world AND a character's origin, the world they are GOING TO is the host.
"""


def _coerce_premise_json(data) -> dict | None:
    """Shape-check what the model returned. A model is not a trusted source:
    anything malformed is dropped rather than allowed to build a world."""
    if not isinstance(data, dict):
        return None
    host = str(data.get("host") or "").strip()
    imports = []
    for item in (data.get("imports") or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("character") or "").strip()
        if not name:
            continue
        imports.append({
            "character": name,
            "from": str(item.get("from") or "").strip(),
            "relation": str(item.get("relation") or "").strip(),
            "goal": str(item.get("goal") or "").strip(),
            "with_player": True,
        })
    return {"host": host, "imports": imports} if (host or imports) else None


def _merge_premise(det: dict, llm_out: dict | None) -> dict:
    """The model reads the sentence; the deterministic parse is the floor.

    Neither is trusted alone. The model can silently drop somebody; the regex
    can mislabel them. So a person found by EITHER is kept, and the richer
    record wins field by field - which is how "girlfriend" survives even when
    the model only says "companion".
    """
    if not llm_out:
        return det
    out = dict(det)
    if llm_out.get("host"):
        out["host"] = llm_out["host"]
        out["host_is_proper"] = True

    merged = [dict(i) for i in (llm_out.get("imports") or [])]
    have = {_norm(i.get("character", "")) for i in merged}
    for imp in (det.get("imports") or []):
        if _norm(imp.get("character", "")) not in have:
            merged.append(dict(imp))

    det_by_name = {_norm(i.get("character", "")): i
                   for i in (det.get("imports") or [])}
    for imp in merged:
        d = det_by_name.get(_norm(imp.get("character", "")))
        if not d:
            continue
        for key in ("from", "relation", "goal"):
            if not imp.get(key) and d.get(key):
                imp[key] = d[key]
    out["imports"] = merged

    # Keep `entities` - what gets researched - in step with the merged parse.
    for imp in merged:
        for key in ("character", "from"):
            name = (imp.get(key) or "").strip()
            if name and _norm(name) not in {_norm(x) for x in out["entities"]}:
                out["entities"].append(name)
    return out


def llm_parse_premise(text: str, *, user_id: str = "") -> dict | None:
    """Ask the model to read the premise itself.

    Regex cannot cover how people actually write these - "enemies but we have
    respect", "my girlfriend charlie", "thanos whose goal is to erase half of
    them" - and every missed shape built the wrong world. The model reads the
    sentence ONCE, at world creation, and returns the same shape the
    deterministic parser does.

    Never raises and never blocks: offline, on a bad key, or on any error it
    returns None and the caller keeps the deterministic parse. A premise must
    not become unparseable because a lookup was unavailable.
    """
    from . import config, llm            # deferred: llm sits above this module

    if not (getattr(config, "ANTHROPIC_API_KEY", "")
            or getattr(config, "OPENAI_API_KEY", "")):
        return None
    try:
        out = llm.complete(
            "premise", PREMISE_SYSTEM,
            f"The player wrote:\n{text}\n\nReturn the JSON.",
            user_id=user_id or "worldforge", json_mode=True,
            max_tokens=600, temperature=0.2, stub=lambda: None)
    except Exception:
        return None
    return _coerce_premise_json(out)


def premise_dossier(text: str, *, refresh: bool = False, depth: str = "full",
                    user_id: str = "") -> dict:
    """Research every property named in a premise, not just the whole string.

    The result always carries the parse, so the world builder knows what the
    player asked for EVEN WHEN NOTHING IS FOUND. That is the important half:
    research is an enhancement, and a failed lookup must never turn a canon
    crossover into "build something original"."""
    parsed = parse_premise(text)
    # The model reads the sentence itself; the deterministic parse is the
    # floor that can only improve on. Offline, or on any failure, `parsed`
    # comes back untouched.
    parsed = _merge_premise(parsed, llm_parse_premise(text, user_id=user_id))
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
                    # A container category names the CONCEPT, not a story beat.
                    # `Story Arcs` stripped to "Story" and became the whole
                    # chapter book of the franchise; the real arcs were its
                    # MEMBERS, so a container is descended into instead of
                    # used. A named arc category ("Mugen Train Arc") is still
                    # the arc itself and is taken as-is.
                    specific = [c for c in found_cats if not _arc_is_container(c)]
                    if specific:
                        arc_names = [re.sub(r"\s*Arcs?$", "", c).strip() or c
                                     for c in specific[:16]]
                    else:
                        members = []
                        for cat in found_cats[:3]:
                            full = cat if cat.lower().startswith("category:") \
                                else f"Category:{cat}"
                            members.extend(category_members(host, full, budget, limit=80))
                        arc_names = _dedupe([m for m in members
                                             if m and not _arc_is_container(m)])[:16]
                    if not arc_names:
                        # Nothing arc-shaped on this wiki. Fall through to the
                        # guessed category names rather than shipping a book
                        # with no chapters in it.
                        found_cats = []
                    else:
                        # Alphabetical is not a running order. The arc articles
                        # say which chapter each one starts at; that is the
                        # real one.
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
        #
        # The curated table knows this for six franchises. For every other
        # setting - which is most of them, and all of the ones nobody has
        # hardcoded yet - ask the model, which knows the source. This is the
        # general mechanism; the table is the offline floor under it.
        leads = leads_for(setting, out["canonical_name"], out.get("characters") or [])
        if leads:
            have = {_norm(c["name"]) for c in out["characters"]}
            missing = [{"name": n, "note": ""} for n in leads
                       if _norm(n) not in have]
            if missing:
                out["characters"] = missing + out["characters"]
                out["pinned_by"] = "model"
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


LEADS_SYSTEM = """You are a canon consultant. You are given the name of a published work.
Answer with the small number of characters it is ABOUT - the protagonist and, at
most, the two or three whose stories the work is actually told through.

Not the most popular, not the strongest, not the fan favourites: the people a
reader would name if asked "who is this story about?". The title character
counts. A mentor who dies in the first act does not, however famous. A villain
is only a lead if the work is genuinely told from their side.

Answer with names exactly as the source spells them - the form a fan would
recognise, not a translation or a title. Two to four names.

If you do not know the work, return an empty list rather than guessing - a wrong
name pinned as a lead is worse than no pin at all.

JSON only:
{"leads":["Name","Name"]}
"""


def leads_for(setting: str, canonical: str, characters: list) -> list:
    """Who the source is ABOUT, asked rather than hardcoded.

    `canon_seed.pin_protagonists` guarantees the lead of a franchise it has on
    file - six of them. Every other setting got whatever the wiki ranking
    produced, which for a long-running work is routinely its own protagonist
    missing: a Vinland Saga build came back with Einar, Snake and Ketil and no
    Thorfinn. The pin existed and could not fire, because the table had no row.

    This asks the model instead, so it works for anything the model knows. One
    call per BUILD, cached with the dossier, and it fails soft in every
    direction: offline, no key, bad JSON, an unknown work, an implausible name
    -> the cast is exactly what it would have been without it.
    """
    name = (canonical or setting or "").strip()
    if not name:
        return []
    key = "leads:" + _norm(name)
    cached = _cache_get(key)
    if cached is not None:
        return list(cached.get("leads") or [])

    from . import config, llm
    leads: list = []
    try:
        if config.live_llm():
            out = llm.complete(
                "premise", LEADS_SYSTEM,
                f"WORK: {name}\n\nName the leads. JSON only.",
                user_id="research", json_mode=True, max_tokens=200, temperature=0.2,
                stub=lambda: {"leads": []})
            raw = out.get("leads") if isinstance(out, dict) else None
            if isinstance(raw, list):
                # Plausibility only. A name can be one word or four (most are
                # two or three), so what is rejected is not "has a space" but
                # what a name cannot be: empty, a whole sentence, a list the
                # model padded, or a name already in OUR OWN cast - which would
                # pin nothing while reading as a success.
                seen = {_norm(c.get("name", "")) for c in characters}
                for item in raw[:6]:
                    s = str(item).strip().strip('"').strip()
                    words = s.split()
                    if not (1 <= len(words) <= 4):
                        continue
                    if not (2 <= len(s) <= 48):
                        continue
                    if s.endswith((".", "!", "?", ":", ";")):
                        continue          # a sentence, not a name
                    if _norm(s) in seen:
                        continue          # already in the roster: pins nothing
                    leads.append(s)
    except Exception:
        leads = []

    _cache_put(key, {"leads": leads})
    return leads


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
