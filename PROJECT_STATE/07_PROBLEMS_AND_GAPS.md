# 07 — Reported Problems & Gaps (canon, crossover, Session Zero)

Every item below was **reproduced by reading the code**, not inferred from the
report. File:line references are to the tree as of 2026-09-13, branch
`friday-handoff-fixes`.

Companion: `06_MISSING_INVENTORY.md` (features built but never surfaced).

---

## P1 ✅ FIXED 2026-09-13 — Crossover picked the WRONG host world

**Status: fixed and regression-tested.**
- `research.py` `_HOST_PATTERNS` now accepts `verse` (plus `realm`, `cosmos`, `series`).
- `worldforge.py` resolves the parsed **host before** research's `canonical_name`,
  and forbids an import's own home from ever becoming the host.
- `persona_source` reuses that same resolved host, so the cards and the narrator
  cannot disagree about which world this is.
- Test: `tests/test_worldgen.py::test_a_crossover_is_set_in_the_host_world_not_the_guests_home`.
- Measured old→new: host parse `None` → `'Demon Slayer'`; `inspired_by`
  `'Hazbin Hotel'` → `'Demon Slayer'`.

**Report:** *"I and my girlfriend Charlie from Hazbin Hotel in the verse of Demon
Slayer" → world = Hazbin Hotel, not Demon Slayer.*

**Two compounding causes, both real (kept for the record):**

**(a) "verse" is not a recognised host phrase.**
`backend/research.py:800-804`
```python
_HOST_PATTERNS = (
    rf"\b(?:world|universe|setting|reality|timeline|continuity)\s+of\s+{_PROPER}",
    rf"\bset\s+in\s+{_PROPER}",
    rf"\b(?:inside|within|into|in)\b\s+(?:the\s+)?(?:world\s+of\s+)?{_PROPER}",
)
```
`verse` is absent from the alternation. For *"in the verse of Demon Slayer"*:
pattern 1 fails (no `verse`), and pattern 3's `_PROPER` requires a capitalised
start, so lowercase `verse` fails too → **no host is extracted at all.**

**(b) Even when a host IS parsed, research outranks it.**
`backend/worldforge.py:1738-1741`
```python
source_name = (found.get("canonical_name") or "").strip()   # research wins FIRST
if not source_name and parsed.get("host_is_proper"):
    source_name = (parsed.get("host") or "").strip()
raw["inspired_by"] = source_name or (setting if personal else "")
```
In a crossover the **host must win**, and "Hazbin Hotel" is explicitly named (and
is in `canon_seed`), so research resolves it cleanly and it becomes
`inspired_by` — which is what the narrator, World Master and persona lookup all
read (`engine.py:133`, `narrator.py:251`, `world_master.py:278`,
`worldforge.py:1786`).

**Fix direction:** add `verse` (and `world`, `realm`, `cosmos`, `series`) to
`_HOST_PATTERNS`; and invert the precedence so a parsed **host** outranks
`canonical_name`, with the import's own source (`Charlie → Hazbin Hotel`)
explicitly excluded from ever being the host.

---

## P2 🔴 "Canon" is not canon at the very start — wrong cast, invented places

**Report:** *selected the very start → "a place with Rengoku and Shinobu and the
place is called Kamado village??"*

**Cause (a) — the cast is a flat franchise roster with no arc filtering.**
`backend/canon_seed.py:29-56` — Demon Slayer's default (`present`) cast is:
Tanjiro, Nezuko, Zenitsu, Inosuke, Kanao, **Giyu, Shinobu, Rengoku**, Muzan.
Rengoku does not appear until the Mugen Train arc; Shinobu likewise joins much
later. Nothing narrows the roster by **arc/entry point**, so "The very beginning"
seats Hashira who, in canon, are nowhere near Mt. Sagiri. Selecting a start point
changes *where you enter*, not *who exists yet*.

**Cause (b) — place names get invented from character surnames.**
The real seed place is `Kamado Household` (`canon_seed.py:33`). "Kamado **village**"
is the surname-plus-generic-noun pattern the builder falls back on when a scale
needs more places than the source supplies — the same family of bug as the
09-12 fix where town streets wore landmark names. A fabricated place that *sounds*
canon is worse than an obviously invented one, because it reads as a canon error.

**Fix direction:** make the cast **arc-aware** (each seeded arc/entry point carries
its own `cast`/`places`, like `eras` already does — `canon_seed.py:59-78` proves the
mechanism works), and stop generating place names by suffixing a character
surname. Gate invented places behind a visible "original" marker (the F3 `origin`
tag already exists in `worldkit.normalise`).

---

## P3 🟠 Session Zero forces choices — no free talk

**Report:** *"the questions for where am I aren't specific enough, it must be free
talk not just choices."*

**Confirmed:** `backend/sessionzero.py` — the entry-point question is `"kind":
"choice"` with fixed `options` (lines 109-110, incl. `{"id": "start", "label": "The
very beginning"}`), and the era question is choice-only (98-99). There are a few
`"kind": "text"` questions (45, 70, 202, 225, 255) and one `"kind": "art"` (218),
but **every question that decides where/when you are is multiple-choice only.**

**Fix direction:** every `choice` question should also accept free text — either
"choose one of these **or describe it yourself**", or a per-question `allow_free:
true` flag. The premise already supports free text; Session Zero should not be the
one place that forbids it.

---

## P4 🟠 Only Demon Slayer is "refined" — canon handling is hardcoded per franchise

**Report:** *"why only demon slayer world is the more refined? it must be a
canonical work, not demon slayer work!"*

**Confirmed:** `backend/canon_seed.py` hardcodes exactly **six** franchises —
`demon_slayer`, `jujutsu_kaisen`, `attack_on_titan`, `naruto`, `one_piece`,
`hazbin_hotel`. Only `demon_slayer` has an `eras` map (`canon_seed.py:59`), as
RESEARCH.md §F1 states: *"only Demon Slayer has a second era populated today."*
Everything else falls through to live research or model invention.

So the "refined" treatment is a per-franchise hand-curated table, not a general
capability. Any real IP not in those six gets no canon floor, no era support, no
arc-appropriate cast.

**Fix direction:** the curated table is a *floor*, not the mechanism. The general
path is: when research succeeds, derive arc-aware cast/places/eras from the
**source itself** (chapters/arcs are already fetched — `canon_chapters`,
`worldforge.py:1726`), and reserve the hand table for when research fails.
Short term: broaden coverage; long term: make it data-driven.

---

## P5 🟡 Related smaller gaps found while tracing the above

- `_HOST_PATTERNS` also lacks `realm`, `cosmos`, `series`, `universe` variants
  like "the DS-verse" / "DS universe" hyphenated forms.
- `research.identify()` (`research.py:268`) resolves **one** setting at a time and
  returns `{"title": resolved, "score": 0}` — no notion of "host vs import", so a
  crossover has no first-class representation downstream.
- The 09-12 fix that made `inspired_by` record the host setting
  (`HANDOFF_2026-09-12.md` §5) is **incomplete for this input shape** — it
  corrected the *verbatim sentence* case, not the *wrong franchise* case.

---

## Consolidated backlog (this file + `06_MISSING_INVENTORY.md`)

**Correctness / canon (highest value)**
1. P1 — host-world selection in crossovers (research vs parsed host precedence; `verse`).
2. P2a — arc-aware cast, so "the very beginning" doesn't seat later-arc characters.
3. P2b — stop surname-derived invented place names; mark invented content.
4. P4 — make canon refinement generic, not Demon-Slayer-only.

**UX / Session Zero**
5. P3 — allow free text on every Session Zero question.
6. Wire the **zero-UI features**: Session Zero onboarding, town memory, trophies,
   contribute, turn order, run history (see `06_MISSING_INVENTORY.md` §A).
7. Accessibility: ARIA/roles on dynamic content, alt text, focus-visible, keyboard
   (§D of `06`).

**Unblock**
8. Valid OpenAI key (401) — blocks all live verification of the above.
9. Write the missing `StoryLiver_DESIGN_BRIEF.md` before any UI rebuild.
