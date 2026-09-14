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
import unicodedata
from urllib.parse import quote, urljoin, urlparse

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
MAX_REQUESTS = 64                    # full dossiers: discovery AND evidence, bounded
CACHE_DAYS = 30
RESEARCH_VERSION = 3               # old untyped/name-only caches are not evidence
# A latency heuristic, NOT a claim that article count proves source quality.
# Identity checks still apply, and the selected continuity is resolved separately.
SUBSTANTIAL_ARTICLES = 300
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
    def __init__(self, limit=MAX_REQUESTS, seconds=150):
        self.left = limit
        self.limit = limit
        self.deadline = time.monotonic() + seconds
        self._clients = {}

    def take(self):
        if self.left <= 0 or time.monotonic() >= self.deadline:
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
              attempts=3, wiki_probe=False) -> dict:
    import httpx

    timeout = config.RESEARCH_TIMEOUT if timeout is None else timeout
    budget.take()
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise BlockedHost("research is HTTPS-only on the standard port")
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
            if wiki_probe:
                dest = urlparse(urljoin(url, r.headers.get("location", "")))
                moved = (dest.hostname or "").lower()
                if (dest.scheme == "https" and not dest.username and not dest.password
                        and dest.port in (None, 443)
                        and re.fullmatch(r"[a-z0-9][a-z0-9-]*\.fandom\.com", moved)
                        and moved != host):
                    # Discovery only. The next request revalidates and pins
                    # the new host; never follow an unchecked HTTP redirect.
                    return {"_redirect_host": moved}
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
    text = unicodedata.normalize("NFKD", str(s or "")).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", " ", text, flags=re.UNICODE).strip()


def name_key(s: str) -> str:
    """Identity matching preserves non-Latin names and folds accents/apostrophes."""
    return _norm(s).replace(" ", "")


def _tokens(s: str) -> set:
    return {t for t in _norm(s).split() if t and t not in STOPWORDS}


def slugify(s: str) -> str:
    """A stable id for an arc name, so Session Zero can offer `mugen_train_arc`
    as an option id and the entry point can be matched back to the arc's REAL
    name later. `sessionzero` and `worldforge` both referenced this via
    `hasattr(research, "slugify")` and quietly fell through to an inline
    copy when it was missing - so the two sides could disagree about the
    spelling of the same arc. One implementation, used by both."""
    return _norm(s).replace(" ", "_")


def resolve_arc(name: str, arcs: list, era: str = "") -> str:
    """The arc's REAL name for whatever the player picked or typed.

    An option arrives as a slug ("mugen_train_arc"); a typed answer arrives as
    prose ("the night the wall falls"). Matching is done against the researched
    arc list first - by slug, then by whole-name fold, then by containment - so
    a choice made from a GENERIC verse's own researched arcs resolves the same
    way a hardcoded franchise's does. Returns "" when nothing matches, which
    callers read as "the player described a moment we have no canned cast for"
    rather than silently widening back to the whole franchise."""
    n = (name or "").strip()
    if not n:
        return ""
    if not arcs:
        return n
    knock = slugify(n)
    flat = _norm(n)
    # 1. exact slug (how an option id is built)
    for a in arcs:
        label = str(a.get("name") if isinstance(a, dict) else a or "").strip()
        if label and slugify(label) == knock:
            return label
    # 2. exact folded name
    for a in arcs:
        label = str(a.get("name") if isinstance(a, dict) else a or "").strip()
        if label and _norm(label) == flat:
            return label
    # A unique whole-token match only. Short substrings such as "war" must
    # not select an unrelated arc, and ambiguous choices remain free text.
    matches = []
    for a in arcs:
        label = str(a.get("name", "") if isinstance(a, dict) else a or "").strip()
        ln = re.sub(r"\s+(?:arc|saga)$", "", _norm(label))
        if len(ln) >= 4 and re.search(r"(?<!\w)" + re.escape(ln) + r"(?!\w)", flat):
            matches.append(label)
    return matches[0] if len(matches) == 1 else n


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
    # Section headings can also be countries, institutions and shelves. Apply
    # the SAME filter as the wiki bucket before merging or caching.
    clean = _keep_character_names(names, limit=40)
    url = "https://en.wikipedia.org/wiki/" + quote(page.replace(" ", "_"), safe="()_")
    return [{"name": n, "note": "", "source_url": url,
             "provenance": "wikipedia_heading"} for n in clean]


# ---------------------------------------------------------------------------
# Fandom: the deep canon
# ---------------------------------------------------------------------------

def _slug_candidates(setting: str, wiki_title: str = "") -> list:
    """Fandom subdomains are guessable, but only if you try the CANONICAL title
    and its subtitle too. "Demon Slayer" alone finds `demon.fandom.com`, which
    is a real wiki about something else entirely; the actual one is keyed to
    the subtitle, "Kimetsu no Yaiba".

    Straight initialisms are additional candidates, not an abbreviation table.
    Moved/abbreviated wikis are primarily discovered from validated redirects."""
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

    def add(cand: str) -> None:
        if cand and len(cand) >= 3 and cand not in out:
            out.append(cand)

    for seed in seeds:
        base = _norm(seed)
        if not base:
            continue
        words = [w for w in base.split() if w]
        # Full names first; a short initialism is less discriminating.
        stripped = re.sub(r"^(the|a|an)\s+", "", base)
        for form in (base, stripped):
            for cand in (form.replace(" ", ""), form.replace(" ", "-")):
                if re.fullmatch(r"[a-z0-9-]+", cand):
                    add(cand)
        if len(words) >= 3:
            add("".join(w[0] for w in words if w not in {"the", "of", "and"}))
    return out[:12]


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


def _probe_wiki(host: str, budget: _Budget, *, attempts=2) -> tuple:
    """Discover a moved wiki without bypassing the normal transport guards."""
    try:
        data = _get_json(
            f"https://{host}/api.php",
            {"format": "json", "formatversion": "2", "maxlag": "5",
             "action": "query", "meta": "siteinfo", "siprop": "general|statistics"},
            budget, timeout=min(config.RESEARCH_TIMEOUT, 8),
            attempts=attempts, wiki_probe=True)
        if data.get("_redirect_host"):
            return ("", 0, data["_redirect_host"])
        query = data.get("query") or {}
        stats = query.get("statistics") or {}
        return ((query.get("general") or {}).get("sitename", ""),
                int(stats.get("articles") or stats.get("pages") or 0), "")
    except (ResearchError, ValueError, TypeError):
        return ("", 0, "")


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
    A failure that cannot say why is a failure that gets guessed at.

    The FIRST acceptable wiki is not necessarily the right one. A franchise
    often has several: the main wiki, plus a thin sub-wiki for one adaptation.
    Fullmetal Alchemist answers at BOTH `fma.fandom.com` (1017 articles) and
    `fullmetal-alchemist-brotherhood.fandom.com` (78) - and returning whichever
    was guessed first gave a dossier two characters and no arcs, because the
    episode sub-wiki was "about" the setting by every boolean the old test
    asked. So every passing candidate is RANKED by how much it actually holds,
    and the substantial wiki wins. Content, not candidate order, decides.

    Probes share a bounded request/deadline budget with evidence retrieval.
    Stop at a substantial identity-matched wiki to preserve that budget. Size
    is a latency heuristic; it does not verify an adaptation or its facts."""
    cands = _slug_candidates(setting, wiki_title)
    if trace is not None and not cands:
        trace.append("no subdomain candidates for that name")
    passed = []
    seen_hosts = set()
    # Reserve a few requests for the category buckets the dossier fetches next;
    # a wiki found at the cost of an empty dossier is not a win.
    floor = min(5, budget.left)

    def survey(slug: str) -> str:
        """Probe one slug; append to `passed` and return any redirect target
        that should be surveyed in turn."""
        if slug in seen_hosts:
            return ""
        seen_hosts.add(slug)
        if budget.left <= floor:
            if trace is not None:
                trace.append(f"budget floor reached before trying {slug}")
            return ""
        host = f"{slug}.fandom.com"
        sitename, articles, moved = _probe_wiki(host, budget)
        if moved:
            if trace is not None:
                trace.append(f"{host}: moved to {moved}")
            return moved
        if not sitename:
            if trace is not None:
                trace.append(f"{host}: no answer")
            return ""
        if not _wiki_is_about(sitename, setting, wiki_title, slug):
            if trace is not None:
                trace.append(f"{host}: answered as {sitename!r}, not this setting")
            return ""
        if trace is not None:
            trace.append(f"{host}: accepted, {articles} articles")
        passed.append((articles, len(passed), host, slug))
        return ""

    # A redirect can point at a slug we never guessed, so the queue grows as
    # redirects are discovered. Bounded so a redirect loop cannot spin.
    queue = list(cands)
    hops = 0
    while queue and hops < 12 and budget.left > floor:
        slug = queue.pop(0)
        target = survey(slug)
        if target:
            hops += 1
            # A redirect is the strongest signal available and its target is
            # almost always the real wiki, so it goes to the FRONT.
            queue.insert(0, target.split(".")[0])
            continue
        if passed and max(p[0] for p in passed) >= SUBSTANTIAL_ARTICLES:
            if trace is not None:
                trace.append("stopped early: a substantial wiki was found")
            break
    if not passed:
        return None
    # Biggest wiki first; ties keep the order they were guessed in, which puts
    # the most specific name ahead of a generic one.
    passed.sort(key=lambda t: (-t[0], t[1]))
    if trace is not None and len(passed) > 1:
        trace.append("chose {} over {}".format(
            passed[0][2], ", ".join(h for _, _, h, _ in passed[1:])))
    return passed[0][2]


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
#
# THE SAME IS TRUE OF A CATEGORY THAT NAMES A *GROUPING OF THINGS*, and this
# was the second half of the same bug. A JJK build shipped a chapter book of
# `Battles by Story; Chapters by Story; Episodes by; Events by Story` - four
# containers that matched none of the patterns above, so all four were taken
# as arcs. They are not arcs; they are the shelves the arcs are filed on.
# The shape to reject is a category term followed by a preposition: "Battles
# by Story", "Locations by type", "Episodes by Arc" (the JJK wiki's own
# spelling - the tail is the container word, not a story beat).
_ARC_CONTAINER = re.compile(
    r"^(story\s*)?arcs?$|^sagas?$|^seasons?$|^events?$"
    r"|^(chapters?|episodes?|volumes?|battles?|fights?|events?|locations?|places?"
    r"|characters?|people|techniques?|abilities?|powers?|items?|media"
    r"|images?|galleries|lists?)\s+(by|of)\b"
    r"|^(chapters?|episodes?|volumes?|battles?|fights?|locations?|places?"
    r"|characters?|techniques?|abilities?|powers?|items?|media|lists?)$",
    re.I)


def _order_arc_containers(cats: list) -> list:
    """Containers worth descending into, most promising first.

    A wiki files its arcs under several shelves and most are empty: the JJK
    wiki has `Battles by Story Arc`, `Chapters by Story Arc` and `Episodes by
    Arc` (all zero members) alongside `Story Arcs`, which holds the ten real
    arcs. Opening them in listed order spent the whole budget on the empty
    ones. A shelf whose name is about the STORY rather than about a kind of
    page is the one that holds arcs, so those rank first."""
    def rank(name: str):
        n = (name or "").lower()
        story = 0 if re.search(r"\b(story|stories|arc|arcs|saga|sagas)\b", n) else 1
        # A shelf about battles/episodes/chapters files PAGES, not arcs.
        kind_page = 1 if re.match(
            r"^\s*(battles?|episodes?|chapters?|volumes?|events?|media)\b", n) else 0
        return (kind_page, story, len(n))
    return sorted(cats, key=rank)


def _arc_is_container(name: str) -> bool:
    """A category with no name beyond the concept is a box, not an arc.

    Also rejects the `X by Y` shape (`Battles by Story`), a dangling
    preposition (`Episodes by`), and - the JJK wiki's own spelling - a
    category term plus a container tail (`Episodes by Arc`, `Battles by Story
    Arc`). In that last form the trailing "Arc" is the WORD, not a story beat:
    the category files every episode that belongs to SOME arc, which is the
    opposite of naming one. A name is a real arc only when what remains after
    stripping the trailing "Arc"/"Saga"/"Season" is a proper noun of its own.

    That last test is what makes this general rather than a blocklist: "Story",
    "Battles", "Chapters by" and "Events by Story Arc" all leave a bare
    category term behind, while "Mugen Train", "Culling Game", "War" and
    "Return to Shiganshina" each leave a real name."""
    n = (name or "").strip().rstrip(":").strip()
    if not n:
        return True
    if _ARC_CONTAINER.match(n):
        return True
    if re.match(r"^[a-z]+\s+by$", n, re.I):
        return True
    # Strip a trailing story-unit suffix and judge what is left.
    stem = re.sub(r"\s*(arc|saga|season|story)\s*$", "", n, flags=re.I).strip()
    if stem == n:                      # no suffix to strip: judge as-is
        return False
    if not stem:                       # the whole name WAS the concept word
        return True
    # "Battles by Story", "Locations by type" -> a shelf, not a beat. The test
    # is whether the part BEFORE the preposition is itself a container term;
    # "Clash of the Titans" is a proper name that merely contains "of", and
    # rejecting it would throw away a real arc.
    pre = re.split(r"\b(?:by|of)\b", stem, 1, flags=re.I)[0].strip()
    if pre and re.match(
            r"^(the\s+)?(story|stories|chapter|chapters|episode|episodes|volume"
            r"|volumes|battle|battles|fight|fights|event|events|character"
            r"|characters|list|lists|media|image|images|location|locations"
            r"|place|places|technique|techniques|ability|abilities|power"
            r"|powers|item|items)$", pre, re.I):
        return True
    # "Episodes by Arc" left over once the suffix is gone -> "Episodes by",
    # which is the dangling-preposition case again.
    if re.match(r"^[a-z]+\s+by$", stem, re.I):
        return True
    # A stem that is only category vocabulary still names no story.
    return bool(re.match(
        r"^(the\s+)?(story|stories|chapter|chapters|episode|episodes|volume"
        r"|volumes|battle|battles|fight|fights|event|events|character"
        r"|characters|list|lists|media|image|images|location|locations)$",
        stem, re.I))


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


# A NAME, NOT A SHELF. A wiki category listing mixes real people with the
# pages they are filed beside, and the alternation is what makes it dangerous:
# an Attack on Titan build seated
#
#   Attack on Titan Character Encyclopedia FINAL/Civilians
#   Attack on Titan Character Encyclopedia/Civilians
#   Attack on Titan Character Encyclopedia FINAL/Survey Corps
#
# as three of its characters - a bookshelf, a bookshelf, and a branch of the
# military - and then handed them to the narrator as people to voice. The same
# pass seated `Faculty and staff` for Jujutsu Kaisen. These read as canon
# because they ARE the source's own words; they are just not anybody's name.
#
# The test is deliberately conservative and shape-based, not a blocklist of
# titles: something that names a whole work (`... Encyclopedia`, `... Series`),
# that is filed under a page (`Parent/Child`), or that is plainly an
# administrative grouping is not a person. A real character can have a slash
# in their page title ("Kokichi Muta / Mechamaru"), so a slash alone is not
# disqualifying - it is the WORK and GROUPING words that are.
# NOUNS THAT ARE NOT NAMES. If a title's HEAD - its last word, ignoring bracket
# tails like "Pieck Finger (Female Titan)" - is one of these, the title names a
# class of thing, not a person: "Survey Corps", "Military Police Brigade",
# "Nine Titans". Real surnames are not in this set, which is why the test can
# be this blunt where the earlier "a capitalised word must follow" guard had to
# be clever and got it wrong twelve different ways.
_NOT_A_SURNAME = {
    "corps", "brigade", "regiment", "battalion", "squad", "army", "navy",
    "staff", "faculty", "personnel", "civilians", "soldiers", "characters",
    "organizations", "organisations", "families", "members", "groups",
    "allies", "enemies", "antagonists", "protagonists", "people", "others",
    "miscellaneous", "students", "teachers", "villagers", "titans",
    "island", "district", "village", "kingdom", "continent", "locations",
}
# This is only a cheap shape filter. Source infobox/category evidence, not a
# franchise-specific blacklist or a claim about surnames, decides entity type.
# THE FIRST WORD IS THE OTHER HALF OF THE TEST. "List of Characters" and "Minor
# characters" are caught by the head word, but "Faculty and staff" heads on
# "staff" only after the conjunction, and "Civilians" is a lone head word.
# These markers name a LIST rather than a person and are safe at any position.
_GROUPING_MARKER = re.compile(
    r"^(?:the\s+)?(?:list\s+of|miscellaneous|misc)(?:\s|$)"
    r"|^(?:minor|major|supporting|recurring|secondary|background|other|all|former)"
    r"\s+(?:characters?|members|people|staff)$", re.I)
# A conjunction joining two bare lower-case nouns is a category, never a name
# ("Faculty and staff"); a real name's connective joins proper nouns ("Dot
# Pixis", "Kokichi Muta / Mechamaru").
_BARE_CONJUNCTION = re.compile(r"\b[a-z]{3,}\s+(?:and|or|of|&)\s+[a-z]{3,}\b")
_CHARACTER_GROUPING = _GROUPING_MARKER       # kept as the public name
_CHARACTER_NOT_A_PERSON = re.compile(
    r"\b(encyclopedias?|art\s*books?|databooks?|guide\s*books?|companions?"
    r"|antholog(?:y|ies)|magazines?|manga|anime|films?|movies?|episodes?"
    r"|chapters?|volumes?|soundtracks?|albums?|games?|novels?|series"
    r"|franchise|sound\s*dramas?|ovas?|specials?|seasons?|arcs?)\b", re.I)
_CHARACTER_PAGE_NOISE = re.compile(
    r"^(?:category|template|file|image|list\s+of|portal|help|project)\b"
    r"|\bdisambiguation\b", re.I)


def _head_of(n: str) -> str:
    """The last meaningful word of a title, minus bracket tails and the
    possessive. "Pieck Finger (Female Titan)" -> "finger"; "Survey Corps" ->
    "corps"; "Nine Titans" -> "titans"."""
    stem = re.sub(r"\s*[\(\[].*?[\)\]]\s*$", "", n).strip()
    parts = [w for w in re.split(r"[\s/]+", stem) if w]
    if not parts:
        return ""
    last = parts[-1]
    last = re.sub(r"^[^\w']+|[^\w']+$", "", last)          # stray punctuation
    if last.endswith("'s") or last.endswith("\u2019s"):
        last = last[:-2]
    return last.lower()


def looks_like_a_character_name(name: str) -> bool:
    """Whether a wiki page title can plausibly be a PERSON in this cast.

    Not a claim about canon - that is the model's job and the roster's - just
    a filter on the SHAPE of a title, because a listing full of real names
    makes the few shelves embedded in it look real too. Reject the shelves
    without touching anyone who could be a person: "Levi", "Annie Leonhart",
    "Ka'qaquq" and "Bug-Eyes" pass; "Survey Corps", "Faculty and staff",
    "Military Police Brigade", "Civilians", "Minor characters" and "Nine
    Titans" do not.

    The first version of this test asked whether a capitalised word FOLLOWED
    the grouping word, on the theory that a real person's title keeps going
    ("Survey Corps Regiment"). Every one of the twelve bad titles in the suite
    has a second capitalised word TOO - "Faculty AND STAFF", "Military POLICE
    Brigade", "Nine TITANS" - so the guard spared all of them. The shape test
    that actually holds is about the HEAD word and the markers, not case."""
    n = (name or "").strip()
    if not n or len(n) > 60:
        return False
    if _CHARACTER_PAGE_NOISE.search(n):
        return False
    if _CHARACTER_NOT_A_PERSON.search(n):
        return False
    if _GROUPING_MARKER.search(n):          # "List of ...", "Minor characters"
        return False
    if _BARE_CONJUNCTION.search(n):         # "Faculty and staff"
        return False
    head = _head_of(n)
    if head in _NOT_A_SURNAME:              # "Survey Corps", "Nine Titans"
        return False
    if re.match(r"^(?:the\s+)?[a-z\s]+$", n):   # all lowercase: a concept
        return False
    # At least one capitalised word, and not just connective tissue.
    words = [w for w in re.split(r"[\s/]+", n) if w]
    proper = [w for w in words if w[:1].isupper()]
    return bool(proper)


def _keep_character_names(names: list, limit: int = 0) -> list:
    """Filter a raw listing down to plausible people, deduped by name fold.

    Dedupe matters as much as the filter here: a JJK build seated both
    `Maki Zen'in` and `Maki Zenin`, and both `Kokichi Muta` and `Kokichi Muta
    / Mechamaru` - the same person twice under two spellings of one wiki
    redirect, which the engine's own "one person is one record" law forbids."""
    import re as _re

    def fold(s: str) -> str:
        return _re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    seen, out = set(), []
    for n in names:
        if not looks_like_a_character_name(n):
            continue
        # Take the first segment of a "Name / Alias" page title: the alias is
        # not a second person.
        primary = re.split(r"\s*/\s*", n)[0].strip() or n
        k = fold(primary)
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(primary)
        if limit and len(out) >= limit:
            break
    return out


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
         r"|should|because|there|their|our|my|his|her|its|goes|go|from"
         r"|enter|enters|entered|seek|seeks|seeking)")
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


# PLURALS ARE NOT OPTIONAL HERE. "me and sukuna are enemies" is the natural way
# to state a mutual relationship, and "enemies" does not contain "enemy", so the
# whole clause was invisible to the parser and Sukuna was never carried in at
# all. `worldforge._RELATION_PROFILES` already knew about the plural; this list,
# which decides whether the clause is even seen, did not. Longest alternatives
# come first, because a regex alternation takes the first that matches and
# "enemy" would otherwise claim the "enem" of "enemies" and leave "ies" behind.
_RELATION_WORDS = (r"girlfriends|boyfriends|girlfriend|boyfriend|"
                   r"best friends|best friend|close friends|close friend|"
                   r"partners|partner|wives|wife|husbands|husband|"
                   r"fianc[eé]es|fianc[eé]e|fianc[eé]s|fianc[eé]|"
                   r"lovers|lover|friends|friend|brothers|brother|"
                   r"sisters|sister|siblings|sibling|twins|twin|"
                   r"mentors|mentor|teachers|teacher|students|student|"
                   r"rivals|rival|enemies|enemy|nemeses|nemesis|foes|foe|"
                   r"allies|ally|companions|companion|sidekicks|sidekick|"
                   r"bodyguards|bodyguard|acquaintances|acquaintance")

# WORDS A PERSON'S NAME CANNOT CONTAIN.
#
# Every relationship pattern here has to decide where a name ends, and each one
# got it wrong differently: "my girlfriend nezuko kamado end up in the verse of
# jujutsu kaisen" produced a partner called "Nezuko Kamado End Up", and a
# second, phantom partner whose name was fourteen words of the sentence. The
# fix is not a better stop list inside each pattern - that has to be got right
# in every pattern separately, and was not. It is ONE test, applied to every
# person capture: a name is the run of words that contains none of these.
#
# Verbs and pronouns are the whole trick. A premise is a sentence, so the words
# that end a name are the words that carry the sentence on, and a player cannot
# type a name that contains one.
_NOT_IN_A_NAME = {
    "i", "me", "my", "mine", "we", "us", "our", "ours", "you", "your", "he",
    "him", "his", "she", "hers", "they", "them", "their", "it", "its",
    "am", "is", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "can", "could", "will", "would", "should",
    "must", "may", "might",
    "go", "goes", "going", "went", "gone", "end", "ends", "ended", "ending",
    "up", "down", "out", "off", "over", "back",
    "arrive", "arrives", "arrived", "enter", "enters", "entered", "entering",
    "travel", "travels", "travelling", "traveling", "travelled", "traveled",
    "head", "heads", "heading", "wake", "wakes", "woke", "move", "moves",
    "moved", "send", "sends", "sent", "return", "returns", "returned",
    "get", "gets", "got", "take", "takes", "took", "meet", "meets", "met",
    "want", "wants", "wanted", "seek", "seeks", "seeking", "try", "tries",
    "plan", "plans", "intend", "intends", "make", "makes", "made", "play",
    "plays", "playing", "start", "starts", "begin", "begins", "let", "lets",
    "live", "lives", "lived", "become", "becomes", "became", "join", "joins",
    "fight", "fights", "kill", "kills", "save", "saves", "help", "helps",
    "who", "whom", "whose", "which", "that", "where", "when", "while", "what",
    "why", "how", "with", "without", "but", "also", "then", "than", "because",
    "there", "here", "and", "or", "so", "if", "as", "at", "by", "for", "into",
    "onto", "from", "about", "against", "between", "during", "after", "before",
    # "in", "on" and "to" belong to real WORLD titles ("Attack on Titan") and
    # never to a person, and this test is only ever asked about people. Left
    # out, they let "MY GIRLFRIEND NEZUKO IN NARUTO" seat a character called
    # "NEZUKO in NARUTO".
    "in", "on", "to",
    "world", "worlds", "universe", "verse", "realm", "setting", "reality",
    "timeline", "continuity", "story", "series", "franchise", "character",
    "characters", "everyone", "someone", "anyone", "nobody", "people",
}

# Connectives a real name can carry INSIDE it - "Joan of Arc", "Ludwig van
# Beethoven", "Monkey D. Luffy". Allowed only between two name words, never at
# either end, so "of jujutsu kaisen" cannot start one.
_NAME_CONNECTIVES = {"of", "de", "del", "della", "da", "van", "von", "der",
                     "den", "la", "le", "du", "bin", "ibn", "al", "no"}

# A name is at most four words. Longer than that and it is a clause.
_NAME_MAX_WORDS = 4


def _name_word(word: str) -> str:
    """The comparable core of a word: letters and apostrophes, folded."""
    return re.sub(r"[^a-z']", "", (word or "").lower())


def _trim_name(text: str, *, from_end: bool = False) -> str:
    """The person named at one END of a fragment, and nothing else.

    Which end matters, and it is the phrase that decides. "my girlfriend NEZUKO
    KAMADO end up in the verse of..." anchors the name at the START of what
    follows the label, so the name is read left to right and stops at "end".
    "...with gojo satoru and GETO who are our friends" anchors it at the END of
    what precedes the clause, so the name is read right to left and stops at
    "and". Reading from the wrong end is how one capture became fourteen words:
    the pattern matched, and nothing afterwards asked where the name actually
    was inside it.
    """
    words = [w for w in re.split(r"\s+", (text or "").strip()) if w]
    if from_end:
        words = list(reversed(words))
    kept: list = []
    for w in words:
        low = _name_word(w)
        if not low:
            break
        if low in _NAME_CONNECTIVES:
            # Only ever between name words; a trailing one is trimmed below.
            if not kept:
                break
            kept.append(w)
            continue
        if low in _NOT_IN_A_NAME:
            break
        kept.append(w)
        if len(kept) >= _NAME_MAX_WORDS:
            break
    while kept and _name_word(kept[-1]) in _NAME_CONNECTIVES:
        kept.pop()
    if from_end:
        kept.reverse()
    return " ".join(kept)


def is_person_like(name: str) -> bool:
    """Could this string be somebody's name at all?

    The last gate before a capture becomes a seat in a world. Deliberately
    about SHAPE, not about canon: "Sukuna", "Charlie Morningstar" and "Joan of
    Arc" pass; "Nezuko Kamado End Up", "Arrive in Naruto with Sakura" and any
    fragment of a sentence do not.
    """
    n = (name or "").strip()
    if not (2 <= len(n) <= 48):
        return False
    # NO PERSON HAS AN UNDERSCORE IN THEIR NAME. A schema key reached a live
    # cast list as a character called "relationship_edges", seated with an id,
    # a persona call and a place in the room. An identifier is not a name, and
    # this is the cheapest possible way to say so.
    if "_" in n:
        return False
    words = [w for w in re.split(r"\s+", n) if w]
    if not (1 <= len(words) <= _NAME_MAX_WORDS):
        return False
    for i, w in enumerate(words):
        low = _name_word(w)
        if not low:
            return False
        if low in _NAME_CONNECTIVES:
            if i == 0 or i == len(words) - 1:
                return False
            continue
        if low in _NOT_IN_A_NAME:
            return False
    # A RELATION LABEL IS NOT PART OF ANYBODY'S NAME. "my best friend's
    # brother's girlfriend" parsed to a character literally called "S
    # Brother's Girlfriend In", which then got a seat, a persona call and a
    # portrait. Whatever else a name is, it does not contain the word
    # "girlfriend".
    if re.search(rf"(?<!\w)(?:{_RELATION_WORDS})(?!\w)", n, re.I):
        return False
    return True


# Named groups throughout, because the positional ones were wrong and silently
# so. `_PROPER` and `_LOOSE` each contain a capturing group of their own, so
# wrapping one in another pair of brackets shifts every later group by one -
# which is exactly what happened here: the relation was read out of the group
# holding the NAME, so "nezuko, who is my girlfriend" recorded a girlfriend
# whose stated relation was the string "nezuko". That falls through every
# profile in `_relationship_for` to the generic 45/45 companion band, so the
# whole feature quietly did nothing for this phrasing. A named group cannot
# drift when the pattern around it changes.
_REL_BEFORE = (rf"\b(?:my|our|his|her|their)\s+(?P<rel>{_RELATION_WORDS})\b"
               rf"\s*,?\s*(?P<name>[^,;.]{{2,80}})")
# The name is whatever precedes the clause; `_trim_name(from_end=True)` finds
# where it actually starts. The pattern itself must not try, because under
# `re.I` the capitalisation in `_PROPER` means nothing and it ran backwards
# through half the sentence.
_REL_AFTER = (rf"(?P<name>[^,;.]{{2,80}}?)\s*,?\s+who\s+(?:is|are)\s+"
              rf"(?:my|our|his|her|their)\s+(?P<rel>{_RELATION_WORDS})\b")
# "me and sukuna are enemies but we have respect" - a relationship stated as a
# fact about the pair rather than as a label on one of them. It was not a shape
# the parser knew, so Sukuna was not carried in at all and the enmity never
# reached the engine. The relation runs to the end of its clause on purpose:
# "enemies but we have respect" is a different relationship from "enemies", and
# `_relationship_for` reads the nuance out of the whole phrase.
_REL_PAIR = (rf"\b(?:me|i|us|we)\s+and\s+(?P<name>[^,;.]{{2,60}}?)\s+"
             rf"(?:are|is|were|was)\s+(?P<rel>(?:{_RELATION_WORDS})[^,;.]{{0,48}})")
_REL_PAIR_REVERSED = (
    rf"\b(?P<name>[^,;.]{{2,60}}?)\s+and\s+(?:me|i|us|we)\s+"
    rf"(?:are|is|were|was)\s+(?P<rel>(?:{_RELATION_WORDS})[^,;.]{{0,48}})")
# Capture the whole companion phrase, then parse it deliberately. Reusing
# `_LOOSE` here made source words and relationship labels become names: "with
# our friends Satoru Gojo ... from Jujutsu Kaisen" yielded a person literally
# called "Our Friends Satoru Gojo".
_WITH_LIST = (r"\bwith\s+(.+?)(?=(?:,\s*(?:while|but|whereas|and)\b)"
              r"|\s+(?:while|whereas)\b|[.;]|$)")
_GOAL = (r"(?:whose|who(?:'s)?)\s+(?:goal|aim|plan|mission|objective|dream"
         r"|purpose|ambition)\s+is\s+to\s+([^.,;]+)")
_WANTS = r"\bwho\s+wants?\s+to\s+([^.,;]+)"
_SEEKS = r"\b(?:seeks?|tries?\s+to|plans?\s+to|intends?\s+to)\s+([^.,;]+)"


def _person_name(raw: str, *, from_end: bool = False) -> str:
    """Clean a person-shaped capture without swallowing source/relationship text.

    `from_end` says which side of the fragment the name is anchored to - see
    `_trim_name`. Whatever survives must still look like a name, so a capture
    that trimmed down to a verb or a bare article yields "" rather than a seat.
    """
    # WHICH SIDE OF THE CLAUSE WORD THE NAME IS ON depends on which end we are
    # reading from. Splitting on "who" and always keeping the HEAD is right for
    # a left-anchored name and exactly wrong for a right-anchored one: asked
    # who "and thanos whose goal is..." is about, the head of the split is
    # "...with gojo satoru and geto", so the goal was handed to Geto. Read from
    # the end, the relevant fragment is the one AFTER the last clause word.
    # "from" and "who" cut in opposite directions and must not be treated as
    # one list. "from" separates a person from their SOURCE, so the person is
    # always on its left - read from either end. "who/whose" open a clause, and
    # the person can sit on either side of one ("geto WHO are our friends";
    # "...who are our friends, and THANOS whose goal is..."), so a right-
    # anchored read takes what follows the last of them. Collapsing both into a
    # single split seated the franchise "One Piece" as a character.
    text = re.split(r"\bfrom\b", raw or "", 1, flags=re.I)[0]
    parts = re.split(r"\b(?:who|whose|while|whereas)\b", text, flags=re.I)
    text = parts[-1] if from_end else parts[0]
    # Repeatedly, not once: "my my my girlfriend girlfriend" is what a stuck
    # key produces, and stripping a single label left a character named
    # "Girlfriend". A label is never a name, however many of them there are.
    text = text.strip()
    while True:
        stripped = re.sub(rf"^(?:(?:my|our|his|her|their)\s+)?"
                          rf"(?:{_RELATION_WORDS})\b\s*", "", text, flags=re.I)
        if stripped == text:
            break
        text = stripped.strip()
    name = _titleish(_clean_entity(_trim_name(text, from_end=from_end)))
    # A relation label on its own is not somebody's name.
    if re.fullmatch(rf"(?:{_RELATION_WORDS})", name, re.I):
        return ""
    return name if is_person_like(name) else ""


def _split_people(raw: str) -> list:
    """Split a shared-source character group into individual people.

    `_IMPORT_PATTERN` quite reasonably sees the capitalised run "Satoru Gojo
    and Suguru Geto" as one entity. In a character slot, however, a top-level
    comma/and joins people, not one person's name. This split happens only in
    that slot; titles such as "Dungeons and Dragons" remain untouched elsewhere.
    """
    parts = [_person_name(x) for x in re.split(r"\s*(?:,|\band\b)\s*", raw or "",
                                               flags=re.I)]
    parts = [x for x in parts if x]
    return parts or ([_person_name(raw)] if _person_name(raw) else [])


def _same_person(left: str, right: str) -> bool:
    """Conservative alias match for duplicate parser captures.

    It accepts an exact name, an unambiguous surname/mononym suffix, or a clean
    name polluted only by a source suffix ("Suguru Geto from Jujutsu"). It does
    not fuzzy-match spelling, because merging two distinct canon characters is
    worse than retaining an uncertain duplicate.
    """
    a, b = _norm(left), _norm(right)
    if not a or not b:
        return False
    if a == b:
        return True
    aw, bw = a.split(), b.split()
    if len(aw) == 1 and len(bw) > 1:
        return aw[0] == bw[-1]
    if len(bw) == 1 and len(aw) > 1:
        return bw[0] == aw[-1]
    return (a.startswith(b + " from ") or b.startswith(a + " from ")
            or a.startswith("our friends " + b)
            or b.startswith("our friends " + a))


# Hard ceilings on what one premise may produce. A premise is free text from
# the internet, and every one of these was reachable: "with person0, person1,
# ... person59 who are my friends" seated sixty people; a 400-character run
# after "whose goal is to" became a 400-character goal carried into every
# prompt for the rest of the run. None of it crashed, which is exactly why it
# needed a number rather than a hope.
_MAX_IMPORTS = 12
_MAX_RELATION_CHARS = 60
_MAX_GOAL_CHARS = 160
_MAX_HOST_CHARS = 80


def _attach_import(out: dict, name: str, relation: str = "",
                   with_player: bool | None = None) -> dict | None:
    """Record a person the premise named exactly once, merging what we learn
    about them from each phrase that mentions them.

    THE PLAUSIBILITY GATE LIVES HERE, not in the callers. Every pattern that
    finds a person used to decide for itself whether the capture was a name,
    and the ones that forgot seated "NEZUKO in NARUTO", "One Piece" and "S
    Brother's Girlfriend In" as characters. One door into the list, one test on
    the door.
    """
    name = (name or "").strip()
    if not is_person_like(name):
        return None
    relation = " ".join((relation or "").split())[:_MAX_RELATION_CHARS]
    matches = [imp for imp in out["imports"]
               if _same_person(imp.get("character", ""), name)]
    if len(matches) == 1:
        imp = matches[0]
        if relation and not imp.get("relation"):
            imp["relation"] = relation
        if with_player:
            imp["with_player"] = True
        return imp
    if len(out["imports"]) >= _MAX_IMPORTS:
        return None
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
        rel = (m.group("rel") or "").lower()
        # The name follows the label, so it is read from the START of what
        # comes after it and stops at the first word a name cannot contain.
        name = _person_name(m.group("name"))
        # "who are our friends, and Thanos ..." closes a preceding relation
        # clause; it does not declare Thanos another friend.
        prefix = raw[max(0, m.start() - 12):m.start()].lower()
        if name and "who are" not in prefix and "who is" not in prefix:
            _attach_import(out, remember(name), relation=rel, with_player=True)

    for m in re.finditer(_REL_AFTER, raw, re.I):
        # The name PRECEDES the clause, so it is read from the END backwards:
        # in "...with gojo satoru and geto who are our friends" the person this
        # clause is about is Geto, not the whole phrase leading up to him.
        name = _person_name(m.group("name"), from_end=True)
        if name:
            _attach_import(out, remember(name),
                           relation=(m.group("rel") or "").lower(),
                           with_player=True)

    # "me and sukuna are enemies but we have respect" - and the mirror image.
    for pattern in (_REL_PAIR, _REL_PAIR_REVERSED):
        for m in re.finditer(pattern, raw, re.I):
            from_end = pattern is _REL_PAIR_REVERSED
            name = _person_name(m.group("name"), from_end=from_end)
            if name:
                _attach_import(out, remember(name),
                               relation=" ".join((m.group("rel") or "").lower().split()),
                               with_player=True)

    for m in re.finditer(_WITH_LIST, raw, re.I):
        phrase = (m.group(1) or "").strip()
        rel = ""
        # Relation may lead the list ("with our friends Gojo and Geto") or
        # trail it ("with Gojo and Geto who are our friends"). Remove the
        # label before splitting so it cannot become part of a person's name.
        lead = re.match(rf"(?:(?:my|our|his|her|their)\s+)?"
                        rf"({_RELATION_WORDS})\s+", phrase, re.I)
        if lead:
            rel = (lead.group(1) or "").lower()
            phrase = phrase[lead.end():]
        tail = re.search(rf"\s+who\s+(?:is|are)\s+"
                         rf"(?:my|our|his|her|their)\s+({_RELATION_WORDS})\b",
                         phrase, re.I)
        if tail:
            rel = (tail.group(1) or "").lower()
            phrase = phrase[:tail.start()]

        for part in re.split(r"\s*(?:,|\band\b)\s*", phrase, flags=re.I):
            name = _person_name(part)
            if name:
                _attach_import(out, remember(name), relation=rel, with_player=True)

    # "thanos whose goal is to X" / "Thanos seeks X" - a drive the world has
    # to actually run rather than a clause the builder quietly forgets.
    #
    # WHOSE goal it is, is decided by who stands NEXT TO the clause. The old
    # loop walked every import already parsed and kept the last one whose name
    # appeared anywhere earlier in the sentence, which is list order, not word
    # order: "...with gojo satoru and geto who are our friends, and thanos
    # whose goal is to erase half of tokyo" gave Thanos's goal to GOJO, and
    # then the world ran a Gojo who wanted to erase half of Tokyo. The subject
    # of "whose" is the name immediately before it. Nothing else is.
    for pat in (_GOAL, _WANTS, _SEEKS):
        for m in re.finditer(pat, raw, re.I):
            goal = " ".join((m.group(1) or "").strip(" .").split())
            if not goal:
                continue
            before = raw[:m.start()]
            name = _person_name(before, from_end=True)
            if not name:
                continue
            # A name the sentence has already introduced is that same person;
            # `_attach_import` merges rather than seating them twice, and
            # returns None when the capture was not a person after all.
            target = _attach_import(out, remember(name))
            if target is not None:
                target["goal"] = goal[:_MAX_GOAL_CHARS]


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
        source = remember(where)
        if not source:
            continue
        for person in _split_people(who):
            character = remember(person)
            if not character:
                continue
            target = _attach_import(out, character, with_player=False)
            if target is not None:
                target["from"] = source

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
        target = _attach_import(out, character, with_player=False)
        if target is not None and not target.get("from"):
            target["from"] = source

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

    # A source that ran straight through the explicit host boundary. This does
    # not need a franchise table: when the parser independently extracted the
    # exact trailing host, the preceding text is the source by construction.
    # With no explicit host, "Made in Abyss" is untouched.
    host = (out.get("host") or "").strip()
    if host:
        for imp in out["imports"]:
            src = imp.get("from") or ""
            m = re.match(
                rf"^(.*?)\s+(?:in|inside|within|into)\s+{re.escape(host)}$", src, re.I)
            trimmed = (m.group(1).strip() if m else "")
            if trimmed:
                imp["from"] = trimmed

    # Everything else that reads as a proper name, so nothing the player typed
    # is silently dropped.
    for m in re.finditer(_PROPER, raw):
        remember(m.group(1))

    # Did the player bring EACH person, or merely ask for them? A global marker
    # is wrong for mixed premises: "my girlfriend Charlie ..., while Thanos
    # seeks ..." made Thanos the player's companion merely because Charlie was.
    # Relation and `with` parsing already mark their own people. This narrow
    # fallback covers the direct "I and Charlie" shape only.
    for imp in out["imports"]:
        if imp.get("with_player"):
            continue
        name = re.escape(str(imp.get("character") or ""))
        if name and re.search(rf"\b(?:i|me)\s+and\s+(?:my\s+)?{name}\b", raw, re.I):
            imp["with_player"] = True
        else:
            imp["with_player"] = False

    if not out["host"] and seen:
        # No "world of X" phrasing. The last named property is the likeliest
        # host - "Charlie from Hazbin Hotel, The Last of Us" reads that way -
        # and being wrong here costs an ordering, not a lookup.
        tail = [e for e in seen if e not in [i["character"] for i in out["imports"]]]
        out["host"] = tail[-1] if tail else seen[-1]
        out["host_is_proper"] = True

    # A HOST IS A TITLE, NOT A PARAGRAPH. Every host pattern is anchored on a
    # trigger word and then runs forward, so a premise that repeats the trigger
    # ("THE WORLD OF THE WORLD OF THE WORLD OF DEMON SLAYER") hands back most of
    # the sentence - which is then used as a wiki search term and as the name of
    # the world in the UI. Trimmed to a title's worth of words.
    host = " ".join(str(out.get("host") or "").split())
    if len(host) > _MAX_HOST_CHARS:
        cut = host[:_MAX_HOST_CHARS].rsplit(" ", 1)[0]
        host = cut or host[:_MAX_HOST_CHARS]
    out["host"] = host

    # Only actual research targets survive. Broad proper-name scanning above is
    # useful for host fallback, but its intermediate fragments are not entities.
    entities = []
    for name in [out.get("host", ""), *[
            value for imp in out["imports"] for value in
            (imp.get("character", ""), imp.get("from", ""))]]:
        name = str(name or "").strip()
        if name and _norm(name) not in {_norm(x) for x in entities}:
            entities.append(name)
    out["entities"] = entities
    return out


PREMISE_SYSTEM = """You read ONE sentence a player typed describing the story they want. You return structured JSON and nothing else - no prose, no commentary, no markdown fences.

Return exactly this shape:
{
  "host": "the world the story is SET IN",
  "imports": [
    {"character": "name, as the player wrote it",
     "from": "that person's home world, or empty if the sentence never says",
     "relation": "who they are to the player, in your own words",
     "goal": "what that person wants, or empty",
     "with_player": true}
  ]
}

Rules that matter:
- `host` is a PLACE, not a person, and never the home a guest came FROM. "Charlie Morningstar in the world of SpongeBob" has host "SpongeBob"; Charlie is an import.
- `relation` is free text and the nuance is the entire point. "enemies but we have respect" is a DIFFERENT relation from "enemy" and must not be flattened. "my girlfriend" is not "a companion". Keep what the player actually meant.
- One entry per person the sentence really names. Never invent anybody.
- `with_player` is true only when that person arrives/travels with the player. A person merely active in the requested plot is false. In "my girlfriend Charlie comes with me, while Thanos seeks the formula", Charlie is true and Thanos is false.
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
        # THE MODEL GOES THROUGH THE SAME DOOR. Its output is untrusted data
        # like any other, and the same caps apply: a model handed a wall of
        # gibberish can echo a sentence back as a character name, and nothing
        # downstream would know the difference between that and a real one.
        if not is_person_like(name) or len(imports) >= _MAX_IMPORTS:
            continue
        imports.append({
            # The model echoes the player's own typing, so a premise written in
            # lowercase seats "gojo satoru" and "nezuko kamado" and the cast
            # list reads as though nobody proofread it. The deterministic parse
            # already capitalises; this makes both paths agree.
            "character": _titleish(name),
            "from": str(item.get("from") or "").strip()[:_MAX_HOST_CHARS],
            "relation": " ".join(str(item.get("relation") or "").split())[:_MAX_RELATION_CHARS],
            "goal": " ".join(str(item.get("goal") or "").split())[:_MAX_GOAL_CHARS],
            "with_player": bool(item.get("with_player", False)),
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
    # A HOST IS A TITLE, AND THE LONGER ANSWER IS NOT THE BETTER ONE. The
    # model's host used to win outright, so a reply of "Tokyo in the Jujutsu
    # Kaisen verse" replaced the deterministic parse's "jujutsu kaisen" - and
    # that phrase is what then went to the wiki, found nothing, and left the
    # entry contract unverified with not one fated event sourced. Everything
    # downstream of canon hangs off this single string.
    #
    # So the model wins when it found something the parser did not, and loses
    # when it merely wrapped the parser's answer in a sentence: a host that
    # CONTAINS the deterministic host is that host, described.
    llm_host = " ".join(str(llm_out.get("host") or "").split())
    det_host = " ".join(str(det.get("host") or "").split())
    if llm_host:
        wraps = bool(det_host) and _norm(det_host) in _norm(llm_host) \
            and _norm(det_host) != _norm(llm_host)
        too_long = len(llm_host.split()) > 6
        out["host"] = det_host if (wraps or (too_long and det_host)) else llm_host
        out["host_is_proper"] = True

    merged = [dict(i) for i in (llm_out.get("imports") or [])]
    for d in (det.get("imports") or []):
        matches = [imp for imp in merged
                   if _same_person(imp.get("character", ""), d.get("character", ""))]
        if len(matches) == 1:
            imp = matches[0]
            # Deterministic relation text was literally present in the player's
            # sentence, so it outranks a model flattening "girlfriend" into
            # "companion". The model still supplies nuance the parser missed.
            if d.get("relation"):
                imp["relation"] = d["relation"]
            for key in ("from", "goal"):
                if not imp.get(key) and d.get(key):
                    imp[key] = d[key]
            imp["with_player"] = bool(imp.get("with_player") or d.get("with_player"))
            continue

        # A malformed deterministic group can overlap several correctly split
        # model entities ("Satoru Gojo and Suguru Geto"). Distribute shared
        # source/relation metadata, then discard the group rather than seating a
        # third fictional person with the combined name.
        dnorm = _norm(d.get("character", ""))
        contained = [imp for imp in merged
                     if _norm(imp.get("character", ""))
                     and _norm(imp.get("character", "")) in dnorm]
        if len(contained) > 1:
            for imp in contained:
                for key in ("from", "relation", "goal"):
                    if not imp.get(key) and d.get(key):
                        imp[key] = d[key]
                imp["with_player"] = bool(imp.get("with_player") or d.get("with_player"))
            continue
        merged.append(dict(d))

    out["imports"] = merged

    # `entities` is a research queue, not a bag of every capitalised phrase.
    # Rebuild it from the resolved host and imports so discarded parser debris
    # ("Our Friends Satoru Gojo") cannot still consume a lookup budget.
    entities = []
    for name in [out.get("host", ""), *[
            value for imp in merged for value in
            (imp.get("character", ""), imp.get("from", ""))]]:
        name = str(name or "").strip()
        if name and _norm(name) not in {_norm(x) for x in entities}:
            entities.append(name)
    out["entities"] = entities
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
        if depth == "full" and d.get("found") and d.get("wiki"):
            evidence_budget = _Budget(18, seconds=75)
            try:
                from . import canon_evidence
                d = canon_evidence.enrich(d, evidence_budget)
            except Exception:
                d = {**d, "coverage": "partial", "evidence": d.get("evidence") or []}
            finally:
                evidence_budget.close()
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
                    "factions", "powers", "arcs", "unplaced_arcs", "evidence",
                    "coverage"):
            if key in host_d:
                out[key] = host_d.get(key)
        # Entry extraction sees host evidence only. Persona extraction may also
        # read each imported character's own source, never the host's memory of
        # them. Deduplicate by revision identity.
        all_evidence = {}
        for ent in out["entities"].values():
            for rec in ent.get("evidence") or []:
                if rec.get("id"):
                    all_evidence[rec["id"]] = rec
        out["persona_evidence"] = list(all_evidence.values())
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
        # A player-supplied name must survive a lookup miss, but a miss is not
        # permission to turn model memory into asserted canon. Preserve identity
        # and relationship wording; keep every other source fact unresolved.
        lines.append(
            "NAMED BY THE PLAYER BUT NOT VERIFIED BY RESEARCH: "
            + ", ".join(unfound)
            + ". Preserve these names, origins, relationships and goals exactly as the "
              "player stated them. Do not add biography, abilities, quotes or source events "
              "from memory; mark or narrate those details as unknown until evidence exists.")
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
                        # Try EVERY container, cheapest-looking first, and stop
                        # as soon as one yields arcs. Taking only found_cats[:3]
                        # was how the JJK wiki lost its whole chapter book: its
                        # first three arc-shaped categories are the empty
                        # shelves (`Battles by Story Arc`, `Chapters by Story
                        # Arc`, `Episodes by Arc`, all 0 members), while the one
                        # that actually holds the ten real arcs - `Story Arcs` -
                        # sat fourth and was never opened.
                        members = []
                        for cat in _order_arc_containers(found_cats)[:6]:
                            full = cat if cat.lower().startswith("category:") \
                                else f"Category:{cat}"
                            got = category_members(host, full, budget, limit=80)
                            if got:
                                members.extend(got)
                                if len(members) >= 16:
                                    break
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
                # the top 18 by article size is the cast. The listing is
                # filtered to plausible PEOPLE first - a shelf is not a
                # character, and ranking made that worse, because a long
                # "Character Encyclopedia" page outranks a short real one.
                if bucket == "characters":
                    names = _keep_character_names(names)
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
    payload = db.jload(row["payload"], None)
    try:
        age_days = (time.time() - float(row["fetched_at"])) / 86400.0
    except (TypeError, ValueError):
        age_days = 999
    # How long THIS entry may stand depends on whether it was a real answer or
    # a thin one - see _cache_ttl_days. Re-read on every hit, so an entry
    # written before this rule existed is judged by the same test.
    if age_days > _cache_ttl_days(payload):
        db.run("DELETE FROM research_cache WHERE key=?", (key,))
        return None
    return payload


# A dossier that came back THIN is not canon, it is a bad draw. Caching it for
# the full CACHE_DAYS is how one transient network wobble becomes a permanent
# property of a setting: a JJK build was cached with `no arcs, no wiki,`
# `note: 'en.wikipedia.org unreachable'` on a day Wikipedia blinked, and for
# the next 30 days every JJK world was built from the thin curated table while
# the wiki - which has ten arc categories and was answering fine - was never
# consulted again. The player sees "only Demon Slayer is refined" and there is
# no way to tell that the cause is a cached hiccup.
#
# So a MISS is cached briefly, to stop a genuinely unknown setting hammering
# the sources on every keystroke, and a HIT is cached for the long term. The
# test of a hit is whether it actually produced the canon this feature exists
# to fetch: a wiki host AND the shape of a real roster.
THIN_CACHE_HOURS = 6


def _cache_ttl_days(payload) -> float:
    """How long this dossier is allowed to stand. See THIN_CACHE_HOURS."""
    if not isinstance(payload, dict):
        return float(CACHE_DAYS)
    if payload.get("note") or payload.get("thin"):
        return THIN_CACHE_HOURS / 24.0
    if not payload.get("found") or not payload.get("canonical_name"):
        return THIN_CACHE_HOURS / 24.0
    # No wiki host is not by itself a miss - plenty of real properties have no
    # Fandom wiki - but no wiki AND a roster too small to be a cast is.
    has_roster = len(payload.get("characters") or []) >= 5
    if not payload.get("wiki") and not has_roster:
        return THIN_CACHE_HOURS / 24.0
    return float(CACHE_DAYS)


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
