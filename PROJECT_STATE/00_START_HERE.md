# StoryLiver — Project Continuity Folder

> Last updated: **2026-09-13** (GMT+3). Branch `friday-handoff-fixes`.
> Written so a future AI agent (or the human) can pick this repo up cold and know
> exactly where it stands and what to do next.

---

## ⚠️ READ THIS FIRST — the identity files will mislead you

The global agent profile files loaded into this session
(`~/.workbuddy-ai/SOUL.md`, `IDENTITY.md`, `USER.md`) describe a **different
project**: a SaaS that learns a teacher's document design. **That is NOT this
workspace.** This workspace (`D:\StoryLiver`) is **StoryLiver**, an AI narrative
RPG game (FastAPI backend + vanilla-JS frontend). The same human (Youssef, Giza)
owns both, but the global identity files are about the teacher-SaaS idea, not
this code.

**Do not** let those files steer your understanding of this repo. Trust the code,
`README.md`, `HANDOFF_2026-09-12.md`, `RESEARCH.md`, and this folder. If you
find the global `USER.md`/`SOUL.md` talking about "teacher document design,"
that is the other project — ignore it here.

---

## What is in this folder

| File | Read it when you want to… |
|---|---|
| `01_PRODUCT_AND_ARCHITECTURE.md` | Understand what StoryLiver is and how the engine is built. |
| `02_CURRENT_STATUS.md` | Know the live repo state: branch, uncommitted work, what works, known blockers. |
| `03_HOW_TO_RUN_AND_VERIFY.md` | Get it running and prove it works (env, run, tests, the CDP guard). |
| `04_ENVIRONMENT_GOTCHAS.md` | Avoid the time-sinks this machine/environment has already caused. **Read before doing anything heavy.** |
| `05_NEXT_PLAN.md` | See the prioritized next steps, including the UX/immersion gaps you asked about. |

---

## 30-second orientation

- **What it is:** a turn-based AI text RPG where the AI plays every character and
  the world. You live inside a story; world + characters persist; you can upload
  your own portrait art (no AI image generation — a deliberate "halal" constraint).
- **Stack:** Python 3.10+ FastAPI backend (`backend/`, ~60 modules) + a no-build
  vanilla-JS single-page frontend (`frontend/`). SQLite is the system of record
  (33 tables); Redis (Upstash-compatible) is optional hot-cache.
- **The moat:** a 7-layer deterministic simulation (weather, atlas, relationships,
  awareness, combat, canon guardrail) that runs for **$0** per turn; the model is
  called at most 6 times/turn, enforced in code, not trusted.
- **Money:** Mana economy + live PayPal Orders v2. OpenAI is the default model
  provider (`gpt-5.4-nano`/`gpt-5.4-mini`/`gpt-5.1`), with a deterministic offline
  stub so the whole product runs with **no key**.
- **Current live blocker:** the configured OpenAI key returns **401** (invalid).
  Everything offline works; live prose needs a fresh key. See `02_CURRENT_STATUS.md`.

---

## Golden rules for working in this repo

1. The model call budget is a hard architectural guarantee (`backend/budget.py`).
   Never add a 7th model caller without going through `budget.py`.
2. "Canon is a gate, not a request" — refusals are enforced in code
   (`backend/canon.py`), not by prompting. Don't weaken that.
3. Determinism is load-bearing (tests assert on seeded replay). Don't introduce
   randomness into the `$0` layers without a seed.
4. The frontend is deliberately minimal and **a UI rebuild was deferred** (see
   `05_NEXT_PLAN.md`). Don't assume the current UI is the intended final shape.
