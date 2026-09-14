# 06 — Missing Inventory (features built but not surfaced, + real UX gaps)

Method: diffed every backend route in `backend/main.py` (170 route shapes) against
every endpoint `frontend/assets/app.js` calls (118 shapes), then **verified each
candidate by hand** before listing it. Verified 2026-09-13.

> ⚠️ **Trap for future gap analysis:** `engine.workspace()` (backend/engine.py:1310)
> **bundles** `state/atlas/graph/knowledge/board/safety/budget/legacy`. So the standalone
> endpoints `/atlas`, `/graph`, `/knowledge`, `/board`, `/drift`, `/legacy`, `/echoes`
> look "unused by the frontend" but the UI **does** show that data — it arrives via
> `/api/playthroughs/{id}/workspace`. Do not report them as missing. Same for
> `/media/*` (img src, not fetch), `/ws/session/*` (constructed at app.js:1331), and
> `/export`, `/card.svg`, `/story.html`, `/forge/worlds/{id}/export` (download links).

---

## A. Built on the backend, ZERO frontend surface (real gaps)

> **Correction, 2026-09-13 — two rows below were over-reported on re-check.**
> **Town memory** and **run history** are NOT missing. The profile modal already
> renders "Towns that remember you" (from `/profile`'s `towns`: faction standing
> and crimes on record) and "Your runs". What is unused is the *standalone*
> endpoints (`/api/town-memory`, `/api/runs/history`) — not the feature. I
> inferred "no UI" from an unused route and did not check the rendered panel.
> **Trophies** genuinely were missing and have now been added to the profile.
> Lesson for the next pass: an unused endpoint proves nothing about the UI;
> grep what the panel actually renders.

| Feature | Backend | Frontend hits | Why it matters |
|---|---|---|---|
| **Session Zero onboarding** | `backend/sessionzero.py` + `GET /api/forge/session-zero` | **0** | README advertises "a new player meets three decisions before a blank input box." The flagship anti-blank-screen onboarding exists and is invisible. |
| **Town memory (cross-run reputation)** | `GET /api/town-memory` → `authority.town_memory()` | **0** | README: "the town remembers you between runs — 60% of standing carries." A headline retention feature with no UI. |
| **Trophies / meta-progression display** | `GET /api/profile/trophies` | **0** (`trophy`/`award` = 0) | Runs and death pay out meta-progression that the player cannot see. |
| **Contribute / "chip in"** | `POST /api/sessions/{id}/contribute` | **0** | README: "if several people chip in, allowances stack up to 5×." No way to do it. |
| **Turn order** | `GET /api/sessions/{id}/turn-order` | **0** | Room turn order is not displayed. |
| **Duplicate Mana/payments path** | `/api/mana/wallet`, `/api/mana/purchase`, `/api/mana/paypal/{create-order,capture}` | **0** | The UI uses the `/api/playthroughs/{id}/purchase*` path instead. Two parallel purchase paths; this one is dead UI-wise. |
| **Run history** | `GET /api/runs/history` | **0** | Past runs not browsable in-app. |

## B. Thin / partial surface (present in code, barely or not exposed)

- **Successions** (3 hits), **rivals** (4), **monuments** (3), **conspiracy** (1) —
  late-game systems with little to no dedicated panel; mostly incidental mentions.
- **Combat conditions** — `POST /api/combat/{id}/condition` unused (declare/resolve are).
- **Death catalogue** (`GET /api/death/catalogue`) unused; UI uses `/death/offered`.
- **Modes tree detail** (`GET /api/modes/tree/{mode_id}`) unused.
- **Forge scales / world factions** — present via other paths; minor.

## C. Confirmed present (do NOT re-report as missing)

Atlas (28 hits), threads/graph (23), knowledge (8), chronicle (30), traitor (18),
whisper, contest (25), voice/speak (26/21), mana (63), factions (37), command palette
(14), themes + density (`data-theme`/`data-density` on `<html>`), X-card, share card,
streak, upload, rooms/sessions, combat declare/resolve, PayPal create-order/capture.

## D. Genuine UX / accessibility gaps (measured)

Counts are literal occurrences across `index.html` (628 lines), `app.js` (6,571),
`styles.css` (3,586):

| Concern | Measurement | Verdict |
|---|---|---|
| **ARIA on dynamic content** | `aria-`: 19 in HTML, **3 in app.js**; `role=`: 3 in HTML, **0 in app.js** | The entire dynamically-rendered app (story feed, character cards, modals, tabs) has essentially **no** roles or ARIA. Screen-reader support is effectively absent. |
| **Alt text** | `alt=`: 0 in HTML, **7 in app.js** | Portraits/SVGs largely unlabelled. |
| **Keyboard navigation** | `tabindex`: 3 total | Very limited custom keyboard support. |
| **Focus visibility** | `:focus-visible`: **3** in CSS (`:focus`: 20) | Focus styling exists but thin; only 3 modern `focus-visible` rules. |
| **Responsive** | **25 `@media`** rules (breakpoints 1320/1240/1120/1040/720/640/420) + `viewport` meta + `prefers-reduced-motion` | ✅ **Genuinely fine.** This is not an unstyled app — correct any "UI is thin" claim. |
| **Upload feedback** | Known from the race-bug fix | No progress indicator, no preview-before-commit, no retry. Only a toast on hard network failure (added by the fix). |

## E. Deliberately not built (README, §Deliberately not built)

Image generation (the "halal" constraint — portraits are uploads only), marketplace,
Free-Style mode. These are **decisions**, not oversights. Don't "fix" them without
a product decision.

## F. Deferred pending UX decisions (RESEARCH.md)

- **P3–P5 UI rebuild** — deferred to `StoryLiver_DESIGN_BRIEF.md`, **which does not exist**.
- **F3 fill-budget slider** — data layer is built (`origin` stamps on every NPC/location
  in `worldkit.normalise`), the slider UI is not.
- **F2 multi-timeline travel device** — not built; needs UX first.
- **canon eras beyond Demon Slayer** — only Demon Slayer has a second era populated.

## G. Not gaps, but commonly mistaken for them

- The **offline stub** makes keyless runs look "broken" — it's deliberate.
- **5 failing tests** are env-gated (network + auth), not product bugs.
- `/api/health`, `/api/budget`, `/`, `/*`, `/robots.txt`, `/media/*` are infra/ops.
