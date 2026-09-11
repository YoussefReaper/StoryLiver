"""Layout invariants the test suite could not see.

Every one of these was a REAL bug that shipped past 391 passing assertions,
because the suite only ever asked the DOM questions ("is `hidden` set?", "does
this element exist?") and never asked the BROWSER what it actually painted.
An element can carry `hidden` and still be fully visible; a panel can exist and
still be 800px taller than the box meant to contain it.

These are static checks over the real stylesheet - no browser needed - chosen
because each one maps to a specific failure that was visible on screen:

  [hidden]      Nine of thirteen hidden elements were still rendering, so the
                landing page, the game shell, the modal and the command
                palette all painted on top of one another. The browser's own
                `[hidden] { display: none }` is the weakest rule there is, and
                any author rule setting `display` beats it.

  min-height:0  `.table` grew to its content (1088px) inside a 292px stage,
                because grid and flex items default to `min-height: auto` and
                refuse to shrink below their content. The feed painted over
                the composer.

  cascade order An override with the same specificity only wins if it comes
                LATER in the file. A mobile rule hiding the world clock sat in
                an earlier media block than the base `.worldclock` definition,
                so it silently lost and the clock printed through the Mana
                chip on every phone.

Run:  python -m tests.test_ui_contract
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "frontend" / "assets" / "styles.css").read_text(encoding="utf-8")
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")

FAILS, NOTES = [], []


def ok(cond, msg):
    (NOTES if cond else FAILS).append(msg)


def section(t):
    NOTES.append("\n  " + t)


def _rule_line(pattern):
    """Line number of the first rule matching `pattern`, or -1."""
    for i, line in enumerate(CSS.split("\n"), 1):
        if re.search(pattern, line):
            return i
    return -1


def test_hidden_actually_hides():
    section("[hidden] — the attribute the whole app toggles screens with")
    ok(re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", CSS),
       "a global [hidden] { display: none !important } rule exists — without it, "
       "any class that sets `display` outranks the browser's own hidden handling")

    # It has to come before the layout rules it is protecting against, or at
    # least carry !important (it does). Being early is how it stays readable.
    line = _rule_line(r"^\[hidden\]")
    ok(0 < line < 60,
       f"and it is declared up top as a base rule (line {line}), not buried")

    used = len(re.findall(r"\shidden(?=[\s>=])", HTML))
    ok(used >= 5,
       f"the markup genuinely relies on it ({used} elements ship with `hidden`)")


def test_scroll_containers_can_shrink():
    section("min-height:0 — so a scroll region scrolls instead of overflowing")
    for sel in (r"\.table\s*\{", r"\.rail\s*\{"):
        name = sel.split("\\")[1].split("\\")[0]
        m = re.search(sel + r"([^}]*)\}", CSS, re.S)
        ok(m and "min-height: 0" in m.group(1),
           f".{name} sets min-height: 0 — a grid/flex item defaults to "
           f"min-height:auto and will not shrink below its content")

    m = re.search(r"\.stage\s*\{([^}]*)\}", CSS, re.S)
    ok(m and "min-height: 0" in m.group(1),
       ".stage sets min-height: 0")
    ok(m and "overflow: hidden" in m.group(1),
       ".stage clips — a hard backstop so a future child that forgets "
       "min-height:0 cannot paint over the composer again")

    m2 = re.search(r"\.feed\s*\{([^}]*)\}", CSS, re.S)
    ok(m2 and "overflow-y: auto" in m2.group(1),
       ".feed is the element that actually scrolls")


def test_mobile_overrides_come_after_base_rules():
    section("cascade — an override must come LATER than what it overrides")
    base = _rule_line(r"^\.worldclock\s*\{")
    override = _rule_line(r"^\s*\.topbar \.worldclock\s*\{\s*display:\s*none")
    ok(base > 0 and override > 0,
       f"both the base .worldclock rule (line {base}) and the mobile "
       f"override (line {override}) are present")
    ok(override > base,
       f"the mobile override at line {override} comes AFTER the base rule at "
       f"line {base} — this is the exact ordering that was wrong, and the "
       f"clock printed through the Mana chip on every phone because of it")

    # There are several 640px blocks; the topbar rules live in the LATE one,
    # which is the whole point of this test - so check them all, not the first.
    blocks = re.findall(r"@media \(max-width: 640px\)[^{]*\{(.*?)\n\}", CSS, re.S)
    ok(any(".topbar {" in b and "overflow: hidden" in b for b in blocks),
       f"the mobile topbar clips as a backstop rather than letting content "
       f"bleed (checked {len(blocks)} media blocks)")


def test_responsive_panels_stay_reachable():
    section("responsive — narrow screens must not lose the rails for good")
    ok(".mobile-tabs" in CSS and 'class="mobile-tabs"' in HTML,
       "a mobile tab bar exists in BOTH the stylesheet and the markup — the "
       "rails are display:none below 1040px, so without it the awareness "
       "panel would be permanently unreachable on a laptop")
    ok(re.search(r"\.app\.pane-(left|right)\s+\.rail-", CSS),
       "the pane classes that reveal a rail are defined")
    ok(re.search(r"\.app\.pane-(left|right)\s+\.composer\s*\{[^}]*display:\s*none", CSS),
       "the composer yields while a panel is open — it otherwise claims a "
       "third of a phone screen and clips the panel mid-row")


def test_assets_are_cache_busted():
    section("deploy — a stale stylesheet must not survive a deploy")
    main = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    ok("_asset_version" in main,
       "asset URLs carry a content hash, so a browser cannot serve a "
       "pre-deploy stylesheet against post-deploy markup")
    ok(re.search(r'Cache-Control["\']?\s*:\s*["\']no-store', main),
       "the HTML itself is no-store — it is the one document that has to be "
       "re-read for a client to learn the new asset hashes at all")
    # The rebuilt panels are ES modules under /app, and a module's imports are
    # resolved by the BROWSER relative to the importing file - so the ?v= hash
    # on the entry point cannot reach forge.js or viewport.js. They were served
    # by the catch-all with no cache header at all.
    ok(re.search(r"Cache-Control[\"']?\s*:\s*[\"']no-cache", main),
       "and the un-hashed ES module tree revalidates rather than trusting the "
       "browser's heuristic cache, which can otherwise pair a new entry point "
       "with a months-old import")


# The palette splits cleanly in two: tokens that name a SURFACE and tokens
# that name INK on a surface. Both themes redefine both halves, so a rule that
# takes its text colour from the surface half is invisible in every theme at
# once - which is exactly how it shipped.
GROUNDS = ("--void", "--ink", "--panel", "--panel-2", "--panel-3",
           "--line", "--line-soft")


def test_text_is_never_painted_in_a_ground_colour():
    section("contrast — a label painted in a background token is invisible")
    # `.mode-chip` set `background: var(--panel); color: var(--ink)`. Those are
    # one shade apart in ink AND one shade apart in parchment, so the name of
    # every unselected option ("Loose", "Strict") did not render in either
    # theme. Only the blurb showed, because `small` names --muted explicitly.
    # Nothing caught it: the element existed, carried text, and had a colour.
    offenders = []
    for sel, block in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS):
        colour = re.search(r"(?<!-)\bcolor:\s*var\((--[a-z0-9-]+)\)", block)
        ground = re.search(r"\bbackground(?:-color)?:\s*var\((--[a-z0-9-]+)\)", block)
        if not colour or not ground:
            continue
        if colour.group(1) in GROUNDS and ground.group(1) in GROUNDS:
            offenders.append(f"{sel.strip().splitlines()[-1].strip()} "
                             f"({colour.group(1)} on {ground.group(1)})")
    ok(not offenders,
       "no rule paints text in a surface token on top of another surface token"
       + (" — found " + "; ".join(offenders[:4]) if offenders else ""))

    m = re.search(r"\.mode-chip\s*\{([^}]*)\}", CSS, re.S)
    ok(m and "color: var(--vellum)" in m.group(1),
       "and the chip that shipped the bug names a real foreground token")
    m_on = re.search(r"\.mode-chip\.on\s*\{([^}]*)\}", CSS, re.S)
    ok(m_on and "color:" in m_on.group(1),
       "its selected state, which paints a LIGHT ground in both themes, sets "
       "its own text colour rather than inheriting the one meant for a dark chip")


def test_every_colour_token_exists():
    section("tokens — var(--nope) paints nothing and raises nothing")
    # A misspelt custom property is the quietest failure CSS has: no error, no
    # warning, no paint. `.lib-x:hover { color: var(--danger) }` was written
    # against a palette whose red is called `--crimson`, so the one affordance
    # for removing a character from your library had no hover state at all.
    # Every other check here reads the rules that exist; this one reads the
    # names they use.
    declared = set(re.findall(r"(--[a-z0-9-]+)\s*:", CSS))
    used = set(re.findall(r"var\(\s*(--[a-z0-9-]+)", CSS))
    # Set from a style attribute in app.js rather than declared in the sheet.
    inline = set(re.findall(r"--([a-z0-9-]+)\s*:", JS))
    missing = sorted(u for u in used - declared if u.lstrip("-") not in inline)
    ok(not missing,
       "every var() in the stylesheet names a token that is actually declared"
       + (" — found " + ", ".join(missing) if missing else ""))


# Classes that carry no styling of their own on purpose: JS uses them as
# selectors, or they sit beside a base class that does the painting.
SEMANTIC_ONLY = {
    "entry-narration",   # selected by app.js to find prose nodes
    "fate-card", "obj-card", "party-card", "tension-card", "you-card",
    "acct-name", "ib-facts",
}


def test_every_class_written_is_a_class_that_paints():
    section("classes — a name no rule matches is a control with no clothes")
    # `.input` was written on 36 controls and matched NO rule anywhere. Every
    # one of them was painted by an ancestor (`.field input`, `.big-field
    # textarea`), so a control that happened to sit somewhere else rendered as
    # a raw browser widget - the profile's "About you" was a white box in a
    # dark theme, because `.field input` does not cover a textarea. Same family
    # as var(--danger) and `.alert.bad`: a name that paints nothing, raises
    # nothing, and is invisible to every test that only asks whether an element
    # exists.
    css = CSS + (ROOT / "frontend" / "app" / "forge.css").read_text(encoding="utf-8")
    declared = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
    used = {}
    for src, where in ((JS, "app.js"), (HTML, "index.html")):
        # Only static class attributes - a template-built name cannot be
        # checked here without guessing what it interpolates to.
        for m in re.finditer(r'class="([^"$]*)"', src):
            for cl in m.group(1).split():
                used.setdefault(cl, where)
    missing = sorted(c for c in used
                     if c not in declared and c not in SEMANTIC_ONLY)
    ok(not missing,
       "every class name written on an element is matched by a rule, or is "
       "listed as deliberately semantic"
       + (" — found " + ", ".join(f"{c} ({used[c]})" for c in missing[:6])
          if missing else ""))
    ok("input" in declared,
       "and `.input`, the most-written class in the app, is one of them")


def test_a_built_world_is_not_given_a_database_id_it_does_not_have():
    section("save — a freshly built world can actually be saved")
    # THE BUG: every save of a newly built world came back 404 "no such world"
    # in red, so a world could be built and then never kept.
    #
    # /forge/bootstrap BUILDS a world; it does not save one. But normalise()
    # always stamps an id derived from the world's NAME, so a fresh Emberfall
    # arrives carrying id "emberfall" - which looks like an id and is not one.
    # The rebuilt forge's bridge passed that straight in as the editor's
    # world_id, so the first save took worldforge.save's `if world_id` UPDATE
    # branch, matched no row owned by this user, and raised KeyError -> 404.
    bridge = JS[JS.find("onWorldBuilt:"):]
    bridge = bridge[:bridge.find("\n  },")]
    ok("openEditor(world, world.id" not in bridge,
       "the built world's name-slug is never passed off as a database id")
    ok("payload.saved" in bridge,
       "an id is used only when the payload actually reports a saved world")

    # The older bootstrap path always had this right; the rebuilt one is now
    # held to the same rule, so the two cannot drift apart again.
    ok("openEditor(r.world, (r.saved && r.saved.id) || null" in JS,
       "and the legacy path it diverged from still reads the same way")


def test_the_engine_never_explains_itself_to_the_player():
    section("voice — a refusal speaks in the world's voice, never the system's")
    # OBSERVED LIVE. Asked to turn to a character who was not in the scene, the
    # feed printed a good in-world refusal and then, underneath it, the World
    # Master's own reason field:
    #
    #     "Charlie is not present in the current state."
    #
    # The World Master is a model handed a JSON state, and it sometimes answers
    # in that register. Its prompt now forbids it, but a prompt is a request,
    # so the client refuses to render a reason that still reads like one.
    ok("readsLikeMachine" in JS,
       "the client has a gate for reasons that read like state rather than speech")
    ok("current state" in JS[JS.find("MACHINE_TELLS"):JS.find("MACHINE_TELLS") + 400],
       "and the exact phrasing that shipped is one of the things it catches")

    guard = JS[JS.find("case 'refusal'"):]
    guard = guard[:guard.find("case '")] if guard.find("case '") > 0 else guard[:900]
    ok("readsLikeMachine(meta.reason)" in guard,
       "the refusal entry runs its reason through that gate before printing it")


def _all():
    return (test_the_engine_never_explains_itself_to_the_player,
            test_a_built_world_is_not_given_a_database_id_it_does_not_have,
            test_hidden_actually_hides, test_scroll_containers_can_shrink,
            test_mobile_overrides_come_after_base_rules,
            test_responsive_panels_stay_reachable, test_assets_are_cache_busted,
            test_text_is_never_painted_in_a_ground_colour,
            test_every_colour_token_exists,
            test_every_class_written_is_a_class_that_paints)


def main():
    print("StoryLiver — layout invariants")
    print("  static checks over the real stylesheet, no browser needed\n")
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


def test_all_ui_contract():
    for fn in _all():
        fn()
    assert not FAILS, "\n".join(FAILS)


if __name__ == "__main__":
    raise SystemExit(main())
