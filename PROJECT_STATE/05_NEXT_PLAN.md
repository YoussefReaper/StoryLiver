# 05 — Next Plan & Open Questions

Priorities are ordered by leverage. The uncomfortable headline: **the engine is
done; the presentation is not.** Most of what "feels missing" is UI, not simulation.

---

## Your UX / immersion question — direct answer

You asked: *"This is missing lots of stuff, isn't it? Not fully immersive and the
UI is not the best option, so it degrades the UX, right?"*

**Mostly yes, with one correction.**

1. **"Missing lots of stuff" — half true.** The *simulation* is remarkably complete:
   a 7-layer deterministic world (weather/atlas/relationships/awareness/combat/
   canon), multiplayer with per-player NPC memory, the anti-collapse memory core,
   fate events, an economy, and live PayPal. What's genuinely missing vs a shipped
   commercial product is **presentation and a few headline features**, not depth.
   So it's not hollow — it's deep-underneath, thin-on-top.
2. **"Not fully immersive" — correct, partly by design.** There is **no AI image
   generation** (portraits are user-uploaded only — the deliberate "halal"
   constraint). So immersion is prose + the spatial "table" UI + map. To someone
   expecting visuals, it reads more like a sophisticated chatbot than a game. The
   README itself warns about exactly this: *"depth alone still reads as a
   chatbot."*
3. **"The UI degrades the UX" — correct, and it's the #1 product problem.** The
   frontend is a single vanilla-JS file (`frontend/assets/app.js`) with minimal
   CSS. A full UI rebuild (P3–P5) was explicitly **deferred to a design brief that
   was never written** (see gap below). The current UI is functional but not the
   intended final shape, and a deep engine behind a thin UI is precisely the
   failure mode that makes the product feel like "just a chatbot."

**Bottom line:** fix the UI and you unlock the value already built. The upload-race
bug you hit was a symptom of the same thin-frontend reality (a fragile picker with
no error path). Unblock live runs, then invest in the UI — that's the order.

---

## 🔴 P0 — Unblock & stabilize (do first)

1. **Get a valid OpenAI key.** The key in `.env` returns 401 (invalid). Without it,
   only the offline stub runs. Fresh key at platform.openai.com/api-keys →
   `D:\StoryLiver\.env` as `OPENAI_API_KEY=sk-...`. If the account lacks
   `gpt-5.4-*`, override per role via `STORYLIVER_MODEL_NARRATOR=...` etc. This is
   the single thing standing between the repo and a real playable demo.
2. **Commit the upload fix + CDP guard.** `frontend/assets/app.js` (the race fix)
   and `tools/check_upload_race.mjs` are uncommitted, verified work. Commit
   deliberately; add the CDP guard to CI/proof steps.
3. **Re-provision the venv and green the suites.** `pip install -r requirements.txt`
   into the managed venv (it's missing `python-multipart`/`argon2-cffi`/`pymongo`).
   Target: 14 suites. The 5 currently-failing are env-gated (network + auth), not
   product bugs — document them rather than chase ghosts.

## 🟠 P1 — The real product gap: UI / UX

4. **Write the missing design brief first.** `RESEARCH.md` repeatedly defers the
   UI rebuild (P3–P5) and the UX half of several features to
   `StoryLiver_DESIGN_BRIEF.md` — **that file does not exist.** Create it before
   any UI code: it's the spec the work was redirected to. Cover at minimum: the
   spatial "table" layout, mobile responsiveness (the upload bug hit mobile most),
   character-sheet presentation, onboarding (the 60–90s value window), and the
   deferred feature UX (fill-budget slider, multi-timeline travel device).
5. **Mobile-first hardening.** The upload-race bug hurt mobile users most. A
   touch-first, latency-tolerant frontend is where the UX payoff is largest.
6. **Make the engine *visible*.** The README's "the world tells you what it noticed"
   panel (signal-first, numbers-on-demand) is the anti-chatbot move. Ensure it's
   prominent — depth must surface as signal, not a stats dump.

## 🟡 P2 — Quality, growth, ops

7. **Canon floor coverage.** `canon_seed.py`/`canon_lore.py` curates ~32 chars
   across 6 franchises. Add more franchises and populate `eras` maps beyond Demon
   Slayer (only Demon Slayer has a second era today).
8. **PayPal live config + the daily cost rail.** Set `PAYPAL_CLIENT_ID`/
   `PAYPAL_CLIENT_SECRET` + `PAYPAL_ENV=live`; confirm `GET /api/payments/status`
   flips to live and the dev instant-grant returns 409.
9. **Durable backup for free hosts.** `MONGODB_URI` (free Atlas M0) → `durable.py`
   snapshots SQLite → Mongo and restores on boot. Off by default; verify the
   restore path on a fresh container.
10. **Monitoring.** The per-story cost log (`usage_summary()`) and the
    `STORYLIVER_DAILY_COST_CAP_USD` rail exist; wire a dashboard/alert before any
    real traffic.

## ⚪ P3 — Deferred / "deliberately not built" (future, not now)

- **Image generation** — explicitly out of scope (the halal constraint). Portraits
  stay user-uploaded. Don't add it unless the product decision changes.
- **Marketplace** — needs payment infra + moderation that don't exist yet.
- **Free-Style mode** — out of scope.
- **Multi-timeline travel device (F2)** and **fill-budget slider (F3)** — need UX
  decisions; belong in the design brief (#4), not guessed at in code.
- **Voice TTS** (`voice.py`), **share cards** (`sharecard.py`), **streaks**
  (`streaks.py`) — built; polish/extend as desired.

---

## One-line "where to start tomorrow"

Drop a valid OpenAI key in `.env`, run `run.py --port 8123`, play a real turn, then
write `StoryLiver_DESIGN_BRIEF.md` and start the UI rebuild — because the engine is
already the strongest part of this product and the UI is what's hiding it.
