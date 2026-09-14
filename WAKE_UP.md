# Wake up — read this first

Two commits since you left: `aa9ebdd` and `5f2600d`. Tree is clean.
**215 tests pass / 4 fail** — the 4 are environment-gated (Mongo, auth, content), none new.

---

## 🔴 The one thing that blocks you having fun

**There is no API key in this workspace.** No `D:\StoryLiver\.env` at all, and no
Anthropic key either. I checked before starting.

Without one, StoryLiver runs its **offline stub** — the whole product works
(rooms, map, combat, memory, canon gates) but the prose is deterministic and
canned. You will *not* be entertained by it, and no amount of engine work
changes that. **I could not run the playthrough you asked for, and I won't
pretend otherwise.**

One line fixes it:

```
D:\StoryLiver\.env     ->     OPENAI_API_KEY=sk-...
```

Then `python run.py`. Everything below is ready and waiting for that key —
including the new model-driven premise reader, which only activates when a key
exists.

If your account lacks `gpt-5.4-*`, add `STORYLIVER_MODEL_NARRATOR=...` etc.
Any role can point at a `claude-*` id instead.

---

## ✅ Fixed tonight

**The crossover bug you reported.** "Charlie from Hazbin Hotel in the verse of
Demon Slayer" built a world called *Hazbin Hotel*. Two causes, both real:
- `verse` wasn't a recognised host phrase (only `world`/`universe`/`setting`/…), so **no host was parsed at all**.
- `inspired_by` took research's answer **before** the parsed host — and research resolved the guest's home, because it's explicitly named.

Measured old → new: host `None` → `'Demon Slayer'`; `inspired_by` `'Hazbin Hotel'` → `'Demon Slayer'`.

**"The very beginning" seated Rengoku and Shinobu on Mt. Sagiri.** The cast was
one flat franchise roster with no sense of *when* you entered. `canon_seed` now
has `arcs`; the start of Demon Slayer is Tanjiro, Nezuko and Giyu on Mt. Sagiri.
Crucially, an **unknown** moment returns nothing rather than widening the cast —
silence must mean silence.

**Your complex premises now parse.** Measured before → after on
*"I and my girlfriend charlie morningstar goes to spongbob square pants with gojo and geto who are our friends"*:
host `''` → `'spongbob square pants'`; imports `[]` → Charlie (girlfriend), Gojo and Geto (friends).
Root cause: names required capitalisation, so lowercase was invisible.

**Relationships and goals survived.** `relation` is free text — "enemies but we
have respect" is not flattened into "enemy". A girlfriend is not "a companion".

**The premise is now read by a model, as you suggested.** New budget role
`premise`: one call *per world*, never per turn (it's a no-op against the
6-call cap — I verified `budget.record()` returns early outside a turn). The
deterministic parse is the floor it can only improve on: offline, bad key, or
any error → unchanged. Malformed model output is dropped, and a person found by
*either* parser is kept. Regex could never cover infinite phrasings; you were right.

**Charlie's canon.** She is Hellborn — *born*, not made; doesn't eat people,
doesn't burn in sunlight. Stated on her card, because being read as the local
man-eating kind is exactly the mistake you hit. Beat-keyed famous lines added.

**Session Zero now exists.** It had *no UI at all* — the endpoint was never
called and `answers` was never sent, so the advertised onboarding was invisible
and every world was built with no era and no entry point. It now runs before
the build, **every question accepts free text as well as choices**, and typing
clears the selection so you cannot send two contradictory answers. The answers
travel into the build, which is what makes the entry point actually change who
is in the world.

**Relationships now change the numbers, not just the words.** `relation` was
parsed and then **used nowhere** — a partner, a friend and an enemy all arrived
with identical values. Now a girlfriend starts at affinity 78 / trust 72 /
obligation 38, a friend at 45, a rival at 12, an enemy at −38, and each bond
seeds its own memory. "enemies but we have respect" softens an enemy without
turning them into a friend. (A test caught that "enemies" doesn't contain the
substring "enemy" — a plural falling through to *companion* is an enemy
arriving as a friend.)

---

## ⚠️ Not done — being straight with you

- **No live playthrough.** No key. I cannot verify prose quality or that a
  complex scene *feels* right. That's the one thing you asked for that I couldn't do.
- **UI/UX rework: started, not finished.** Session Zero was the worst gap and
  it's now real (above). The app also announces itself to a screen reader now —
  the modal is a dialog, toasts are a live region, the close button has a name —
  locked in by a contract test so it can't regress. Still open: icon-only
  buttons with no accessible name (counted, not hidden), and five built features
  with no UI at all — town memory, trophies, contribute, turn order, run
  history. Plan in `05_NEXT_PLAN.md` / `07_PROBLEMS_AND_GAPS.md`.
- Only 6 franchises have curated canon (Demon Slayer is the only one with eras).
  SpongeBob/Thanos-type settings fall through to research or invention.

---

## Start here

1. Put a key in `.env`. Nothing else matters until that exists.
2. Build: *"I and my girlfriend charlie morningstar goes to spongbob square
   pants with gojo and geto who are our friends"* — it should now parse to
   host=SpongeBob with three correctly-related imports.
3. Read `PROJECT_STATE/00_START_HERE.md` for the full picture.

Everything is committed, so nothing is lost.
