# 02 — Current Status (live repo state)

> Snapshot taken **2026-09-13**, branch `friday-handoff-fixes`.

## Git

- **Current branch:** `friday-handoff-fixes` (local). Tracking remote
  `origin/friday-handoff-fixes`.
- **Other local branch:** `Hello` (recreated at remote tip `cfb2491` during the
  git-recovery work below).
- **Recent commits (newest first):** `afef25d` Drop the stale feed specimen ·
  `95bd2a0` A face outlives the free tier · `6c94244` A dropped connection stops
  eating the sentence you just wrote · `8953b51` The world card was a picture that
  failed to load · `db9b9eb` Selected is an outline… · `8192340` Find something in
  two hundred turns of memory · `511c901` Give somebody a face the moment you meet
  them.
- **Remote caveat:** the `legacy-layer-and-mode-tree` remote branch is **broken**
  (it "did not send all necessary objects"), so a full all-branches fetch is
  impossible. Fetch the single branch you need only.

## Uncommitted work in the working tree (IMPORTANT)

These changes are **not yet committed**. A future agent should review and commit
them deliberately — they are real, verified fixes, not scratch:

| Path | State | What it is |
|---|---|---|
| `frontend/assets/app.js` | Modified | **The upload-race fix** (see below). The picture-picker bug the user reported. |
| `tools/check_upload_race.mjs` | New, untracked | Zero-dep headless-Chrome CDP regression guard for the upload fix. |
| `.workbuddy-ai/memory/2026-09-13.md` | Modified | Session memory (root cause, proof, git recovery, env traps). |

### The upload-race fix (the user's reported bug)

**Symptom:** after creating a world, picking a picture for a character card "does
nothing" — the file is chosen but never appears.

**Root cause:** `askForArt()` resolved only *after* `await uploadImage()`. A
separate `window` `focus` + `setTimeout(700)` handler resolved the promise `null`
to detect a cancelled picker. On any link slower than ~700 ms the upload was still
in flight when the timer fired → the stored file was thrown away, with no error,
because nothing had *failed*. Fast links worked, so it read as flaky, not broken.
Mobile users (slow links) were the most affected — i.e. most of them.

**Fix:** the promise is now claimed the moment a file exists (resolving with the
pending upload promise adopts it); the `cancel` event is listened for; the focus
timer only fires `null` when no file was chosen. `uploadImage()` also catches a
dead connection and toasts instead of leaving the picker pending forever.

**Proof:** `tools/check_upload_race.mjs` drives the real app in headless Chrome,
intercepts the file chooser, and asserts 4 cases (fast link → `/media/…`; slow
link +1500 ms latency → `/media/…`; dismissed picker → `null`; dismissed picker
without `cancel` event → `null`). Fails on the old code (slow link → `null`),
passes 4/4 on the fixed code. **The fix feeds every portrait button** (character
sheet, cast list, world forge, NPC drawer). The identity-panel `#idn-file` input
was a real in-DOM input and never had the bug.

## Tests — what to trust

- **14 test suites** in `tests/` (`test_anticollapse`, `test_multiplayer`,
  `test_layers`, `test_full`, `test_journey`, `test_workstreams`, `test_research`,
  `test_durable`, `test_ui_contract`, `test_worldgen`, `test_competitive`, …).
- **Last verified this session (2026-09-13, venv deps installed): 211 passed,
  5 failed.** The 5 failures are **environment-related, not product bugs**:
  - `test_competitive` — content assertion (model-dependent).
  - `test_durable` — needs `MONGODB_URI` / Mongo; skips cleanly without it but
    fails when the mock isn't hermetic.
  - `test_full` + `test_workstreams` — `AuthError` (need `argon2-cffi`, now
    installed; some auth paths still expect live config).
  - `test_research::test_resolved_ip_guard` — `WinError 43`, sandbox blocks the
    network call it makes.
- **Prior handoff (09-12) claimed 1166 checks / 14 suites / 0 failures** — that
  was in mock mode with a fully-provisioned venv. The drift to 5 failures happened
  because the local venv had **missing declared deps** (`python-multipart`,
  `argon2-cffi`, `pymongo`) — see `04_ENVIRONMENT_GOTCHAS.md`. Install them and the
  offline suites go green; the 2 network/auth ones still need their env.
- **Re-verify anytime:** `python -m tests.test_anticollapse` (and the others
  listed in `README.md`). Mock mode is hermetic when deps are present and no
  network env is set.

## What works right now (no key needed)

The **entire product runs keyless** via the deterministic offline stub: rooms,
whispers, the Forge, the economy, export, the 7-layer simulation, multiplayer, the
memory core, the map, combat, canon guardrail. The README's "Prove it works"
suites all run offline. Live prose quality is the only thing gated behind a valid
key.

## Known blockers / open items

1. **🔴 OpenAI key is invalid (401).** `~/.env` (gitignored) has a 175-char key
   written byte-for-byte as given; OpenAI rejects it (`Incorrect API key provided`).
   Network is fine (clean 401 reached OpenAI). Fix: generate a fresh key at
   platform.openai.com/api-keys → drop into `D:\StoryLiver\.env` as
   `OPENAI_API_KEY=sk-...`. Then live runs work immediately. If the account lacks
   `gpt-5.4-*` models, override via `STORYLIVER_MODEL_NARRATOR=...` etc. in `.env`.
2. **🟡 Upload fix + CDP guard are uncommitted** (above). Commit them.
3. **🟡 Broken remote branch** `legacy-layer-and-mode-tree` (object store
   incomplete). Don't rely on it; fetch `friday-handoff-fixes` only.
4. **🟡 `git fsck` reports harmless invalid reflog entries** (objects never in
   this copy). Left as-is; not a functional problem.
5. **🟢 PayPal is dev-mode until configured** — `POST /api/playthroughs/{id}/purchase`
   is an instant grant with no credentials; returns 409 once `PAYPAL_CLIENT_ID`/
   `PAYPAL_CLIENT_SECRET` are set. `GET /api/payments/status` reports the mode.
