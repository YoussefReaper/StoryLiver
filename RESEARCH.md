# RESEARCH.md — decisions made against evidence, not assertion

Per the Friday brief's mandate to document research loops before building. Each
section states the question, what was actually checked (against this repo,
not assumption), and the decision that followed — plus a correction to one
recommendation in the companion Hermes brief that turned out to be wrong when
checked against the real code.

---

## Correction: the model-routing recommendation was based on stale information

`StoryLiver_UNIFIED_CLAUDE_BUILD.md` (the Hermes session's brief) recommends
routing to `gpt-4o` / `gpt-4o-mini` on a "$5 OpenAI Tier 1" budget. Checked
against `backend/config.py`:

```
MODELS = {
    "world_master": "gpt-5.4-nano",
    "narrator":      "gpt-5.4-mini",
    "narrator_premium": "gpt-5.1",
    "npc":           "gpt-5.4-mini",
    "director":      "gpt-5.4-nano",
    "room":          "gpt-5.4-mini",
}
```

The repo already runs a newer, cheaper family than the one recommended.
`PRICE_PER_MTOK` prices `gpt-4o` at (2.50, 10.00) USD/M tokens against
`gpt-5.4-mini` at (0.75, 4.50) — following the recommendation would have been
a real regression in both quality and cost. Not applied. This is worth
flagging because it suggests the Hermes session was working from a stale
snapshot rather than the live repo — any other recommendation from that
document should be re-checked the same way before acting on it.

---

## Mana pricing: token-metered, without inventing a new number (P1)

**Question:** the owner's instruction was explicit — "the Mana must be for
the tokens and not the turns because it differs. A turn can be 200 tokens or
4000 tokens." The brief additionally demanded margin research before shipping
new numbers: "the owner does NOT want to discover after a month that profit
is negative... state the assumption (model, avg tokens/turn, margin target)."

**What was already there, checked rather than assumed:** `config.py` already
carries real, measured research from an earlier pass — `TARGET_BLENDED_COST_USD
= 0.0040`, with the comment: *"three independent 154-turn test suites
measured $0.00195–0.00265/action... this constant carries roughly a 1.5x
safety buffer over the highest of those."* `MANA_PACKS` already prices Mana
at $0.01333–$0.01663 per unit depending on tier. At the flat `MANA_COST["standard"]
= 1`, the implied margin is `(0.01333 − 0.0040) / 0.01333 ≈ 70%` at the
worst-case (largest, cheapest-per-unit) pack — matching the pricing
comment's own stated "60–72% gross margin, worst case."

**Decision:** reuse this number rather than inventing a new one.
`USD_PER_MANA = TARGET_BLENDED_COST_USD`. A turn is now billed
`max(1, ceil(real_usd_spent / USD_PER_MANA))`, measured from `usage_log`
after every model call the turn actually made has been recorded, rather than
a flat pre-charge before any call ran. A turn that spends exactly the
average action's cost still spends 1 Mana — the baseline is unchanged. A
turn that fires World Master's downstream effects, an NPC's own turn, AND
the Director now correctly costs more than a turn with one quiet Narrator
call, which the flat price could never represent. The margin this was
already measured against is mathematically untouched: it depends on
$/Mana-sold vs $/Mana-of-compute-delivered, and this change makes the second
number *more* accurate, not different in kind.

**Not done:** fractional Mana. Mana stays a whole-number currency (the
existing wallet/pack/UI model assumes integers throughout); a genuinely
cheap turn floors up to 1 rather than billing 0.3. This slightly overcharges
the cheapest turns relative to a perfectly continuous meter, which is the
conservative direction to round in given the margin concern above.

---

## Guest Mana ceiling (P1, other half)

**Checked:** `STARTING_MANA` (40) turned out to feed a legacy per-playthrough
column that gets migrated into the real wallet system and is not the actual
daily allowance lever. The real lever, `FREE_DAILY_MANA` (also 40), had no
guest/account distinction at all — every identity, guest or not, got the
same 40/day.

**Decision:** `GUEST_STARTING_MANA = 10` (env-overridable), applied via
`auth.is_guest()` inside `mana.free_pool()`'s per-payer allowance lookup — a
guest's daily ceiling caps at 10; the full 40 requires an account, with no
code path that grants 40 to an unauthenticated id.

---

## Rate limiting (P2)

**Question:** the owner's clarification was specific — "abuse = abusing the
AI / spamming to burn my tokens, NOT in-world RPG actions." This ruled out
content-based restriction entirely; the only thing to build is a volume wall.

**Checked:** no existing rate-limit mechanism anywhere in the codebase.
`world_master.validate()` already runs on every action regardless — meaning
a general action-rate limit already covers the brief's separately-named
"World Master query throttle," since there is no distinct "ask the World
Master directly" verb exposed to players beyond ordinary actions. No second
mechanism was needed.

**Decision:** a per-user, in-process sliding window (20 actions/minute,
`budget.rate_limit()`), checked before the World Master or Narrator are
asked anything, on both the HTTP action route and the room WebSocket.
Verified directly that it has zero opinion on content — `"GO AWAY YOU SHIT"`
passes through unblocked and is billed like any other turn (test:
`test_action_spam_is_rate_limited_not_in_world_content`).

**Known limitation, stated rather than hidden:** the window is
per-process. A second web worker keeps its own window. This is an accepted
tradeoff, not an oversight — the wall this exists to stop is a script
hammering one process's socket; Mana's own per-turn cost (P1) is what
actually bounds spend across a whole fleet regardless of worker count. A
shared window would need a store (Redis, etc.) this project's deployment
doesn't currently carry. If horizontal scaling is added later, revisit this.

---

## Canon fidelity fallback (D1/D2)

**Question:** reproduced failure — a Demon Slayer "Swordsmith Village" build
invented `Kaname` and `Aiko` (never in Demon Slayer) because a live wiki
category fetch timed out and `research.dossier()` returned an empty cast,
which the model then filled from its own imagination.

**Checked:** `research.dossier()`'s existing `characters_from_wikipedia()`
curated-list path is sound *when it succeeds* — it correctly leads with a
Wikipedia "List of X characters" article rather than an alphabetical wiki
category dump. The actual gap was the *failure* path: nothing ran when that
lookup came back empty or raised.

**Decision:** `backend/canon_seed.py` — a small, hand-curated table for the
handful of franchises this keeps happening against (the ones named in the
brief, plus the user's own crossover example, Hazbin Hotel). Deliberately
not a general-purpose wiki mirror: an unmatched setting still falls through
to research/the model exactly as before, honestly. Wired into every actual
failure mode identified by reading the code, not guessed at — `identify()`
returning nothing, `identify()` raising, and a later step raising before the
cast was assembled all reach the same `_seed_fallback()` helper (verified
each path individually with a monkeypatched failure, not just the common
case).

**A second bug found while building this (D1):** article-*length* ranking
(`by_importance()`'s "top N by article size" heuristic) can rank a real
protagonist below a longer-article side character — reproduced with Demon
Slayer keeping Zenitsu/Inosuke/Shinobu and dropping Tanjiro.
`pin_protagonists()` guarantees the matched franchise's lead(s) a seat,
first, regardless of ranking — a pin *on top of* live research, not a
replacement for it.

---

## Era selection (F1)

**Question:** "Sengoku era → cast = Yoriichi + Michikatsu, not current
Hashira." Checked whether the existing "where do you enter the story"
Session Zero question already covered this — it doesn't: that question
(`sessionzero.py`'s `arcs`-derived "entry" question) picks a point on *one*
continuous timeline with one continuous cast. Sengoku-era Demon Slayer is a
different timeline centuries earlier, sharing almost no names with the
present day. These are genuinely different axes, not the same question
asked two ways.

**Decision:** `canon_seed` entries can carry an `eras` map, each overriding
protagonists/cast/places entirely (a swap, not a merge) — verified directly
that the present-day and Sengoku casts share almost no names, and that
choosing Sengoku correctly excludes Tanjiro and includes Yoriichi in the
actual `grounding_brief()` text the model reads (not just in the underlying
data — the test asserts against the rendered prompt string).

**Scope, stated honestly:** only Demon Slayer has a second era populated
today, as the flagship/reproduction case named in the brief. The mechanism
generalizes — any `canon_seed` entry can add an `eras` map the same way —
but populating it for other franchises is future work, not attempted here
to avoid shipping thin, under-researched era data for settings nobody
specifically reported this failure against.

---

## Fill-budget tagging (F3)

**Question:** when a chosen scale needs more people/places than the source
material has, the model fills the gap by invention. The brief's requested
feature ("let the user choose how many made-up characters to add, tag them
as original") is a genuine new UI surface with real UX decisions attached
(a slider needs a unit, a default, a place in the forge flow) — deferred to
the design brief rather than guessed at here.

**What was actually buildable without guessing at UX:** the *data layer* a
slider would need to mean anything — knowing, per generated NPC and
location, whether it's grounded (canon) or invented (original) to fill the
gap. `worldkit.normalise()` now stamps `origin` on every NPC/location;
`worldforge.bootstrap()` computes it by comparing the final generated name
against the exact grounded set the build was given (case/whitespace-folded,
matching `grounding_brief()`'s own "spell them exactly as written above"
instruction — an exact match is the correct comparison here, not a fuzzy
one, which would risk crediting an unrelated invented name as canon). A
hand-forged or imported world has no such distinction to make and defaults
everyone to `"canon"` — verified against the actual starter world, not
assumed.

---

## D3 — truncation, re-examined past "bump the token cap"

**Question:** the D3 status note said city-scale had been fixed (6000/7000
token caps, temperature 1.0) but region/world were untested, "may still
truncate."

**Checked, rather than re-bumped blindly:** read `_build_districts()` and
`_fill_district()`'s actual output schemas. The district-layout pass's
per-district payload is small (id/name/kind/premise/connects — no
locations or characters), so its size scales lightly with district count;
6000 tokens is comfortably enough even at 8 districts. The per-district
*fill* pass is called once per district with a *fixed* per-district
location/NPC range (`SCALES`'s `locs`/`npcs` tuples actually *shrink* as
scale grows — a world's 8 districts each produce fewer locations than a
town's single district does) — so 7000 tokens per call doesn't need to grow
with world size either. The laws pass's output is explicitly fixed-size by
its own prompt ("Exactly 9-11 rules and exactly 7 fated events") regardless
of how many districts fed it. Built all four scales end to end and
confirmed this empirically (rule/fate counts identical at every scale,
place/NPC counts scaling as `SCALES` predicts, zero truncation).

**The real bug, found by reading the actual failure path rather than
re-checking the token math:** `except llm.LLMError: raise HTTPException(502,
...)`. A region/world build is 6–11 separate model calls; this turned *any
one* truncated response into total failure, discarding every call that had
already succeeded and been paid for. The token-cap bump lowered how often a
pass truncates; it never touched what happens when one still does.
`_resilient()` — one retry at 1.5x budget, then a fallback to the same
procedural stub offline mode already uses, scoped per-pass so a failure on
district 6 of 8 doesn't discard districts 1–5. A missing API key still
raises immediately (checked via `config.key_for()`) rather than being
silently swallowed behind bad procedural content.

---

## What was checked and found to already work (saved from being rebuilt)

- **D7/F4a, language injection** — `modes.LANGUAGE_KEY` was already a
  free-text axis, already wired into `narrator.tone_directive()`. Verified
  end to end; the gap was test coverage, not implementation. No rebuild
  needed — just proved it, and added the test so a future refactor can't
  silently break it unnoticed.
- **D16, the input-parse crash guard** — `engine.take_turn()` already
  strips and rejects empty/whitespace input with a clean 400 before it
  reaches any model. This is exactly the scope the owner's own P2
  clarification asked for ("the ONLY thing the input guard does is reject
  input that would CRASH the parser... valid-but-crude text always
  proceeds and is billed") — already true, checked directly, nothing to add.

---

## What was deliberately not attempted this session

- **F2, the multi-timeline travel device.** A genuinely new core mechanic
  (state per active timeline layer, a travel action, crossing-events shown
  distinctly) whose shape depends on UX decisions not yet made — see the
  design brief's Section 15 and Section 17. Building backend state for an
  undesigned interaction risks locking in the wrong shape.
- **The full frontend/UI rebuild, P3–P5, and the UI half of F4a/F4b/F4c/F3.**
  Redirected explicitly to a design brief (`StoryLiver_DESIGN_BRIEF.md`)
  rather than code, per instruction partway through this session.
