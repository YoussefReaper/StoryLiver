# 01 — Product & Architecture

## What StoryLiver is

A turn-based AI text RPG. The AI plays **every** character and the world itself
(solo, or a room of up to 6). Ships one starter world, **Emberfall** (a valley
village 3 days before it burns), but the product is the **World Forge** — forge a
world by hand or name any setting and watch it build (9–11 places, 10–12
characters with full persona anchors, 9 enforced laws, 7 fated events).

Tagline from `README.md`: *"Don't read the story. Live in it."* The stated
competitors are AI Dungeon, Character.AI, FableAI, Kin — and the wedge is **the AI
forgets** (the anti-collapse memory core) plus a real simulation underneath the
prose.

## The three architectural guarantees (read these, they explain every design choice)

1. **Context does not grow with turn count** — turn 500 costs what turn 5 costs.
2. **Context does not grow with player count** — 4 players share one passage/turn.
3. **The model is called at most 6 times/turn**, enforced in `backend/llm.py`
   (`complete`) + `backend/budget.py`, not trusted to self-limit.

## The 7-layer simulation (the actual moat — all $0/model-free)

| Layer | System | Module |
|---|---|---|
| 2 | World vector (time/weather/light/noise/unrest), seeded Markov | `worldstate.py`, `atlas.py` |
| 3 | Pre-commit declare-hidden / reveal-simultaneously | `precommit.py` |
| 3/5 | Betrayal: private knowledge, insight-vs-deception | `betrayal.py` |
| 4 | Relationship economy (7 scalars per npc↔player, trust-game deltas) | `relationships.py` |
| 5 | Witness / rumour relay / factions / hunts / stealth | `awareness.py` |
| 6 | Combat: WEGO resolve, flanking, zones, boss gates | `combat.py` |
| 7 | Canon guardrail, AIMS, card approval, safety Lines/X-card | `canon.py`, `identity.py` |

The model is called for only six things: narrator prose, an ambiguous NPC
decision, a plot twist, a narrated rumour, an OOC reply, character-card autofill.
`budget.py` enforces both the list and the per-turn cap.

## The memory core (anti-collapse)

Three orthogonal stores; the raw transcript is **never** one of them and never
enters a prompt:
1. **Timeline events** — append-only `{turn, actor, action, consequence, rule_ref}`,
   retrieved by a Generative-Agents score (recency + importance + relevance).
2. **Relationship vectors** — `player↔npc` and `npc↔npc`, 4 scalars each
   (affinity/trust/fear/obligation), bounded ±100, capped ±25/turn.
3. **Persona anchors** — a constant block per character, injected verbatim every
   turn, never summarised. This is why turn 300 sounds like turn 1.

## Model routing (default provider = OpenAI)

| Role | Model | Why |
|---|---|---|
| World Master | `gpt-5.4-nano` | structured JSON, correctness over prose |
| Narrator | `gpt-5.4-mini` | the text the player reads |
| NPC minds | `gpt-5.4-mini` | observe→reflect→plan→act→whisper, per player |
| Director | `gpt-5.4-nano` | tension scoring + beats |
| Deep Prose (opt-in, 4 Mana) | `gpt-5.1` | premium narrator |

Non-streaming chat-completions only (OpenAI gates GPT-5 *streaming* behind org
verification; non-streaming works on a $5 Tier-1 key). Any role can be pointed at
a `claude-*` id instead (native Anthropic Messages API, not a shim). Offline stub
means the product runs keyless.

## Key backend modules (from `README.md` layout, verified present)

`engine.py` (turn orchestration), `budget.py` (cost valve), `rt.py` (realtime),
`sessions.py` (rooms/codes/roles), `world_master.py`, `narrator.py`, `npc_sim.py`,
`director.py`, `memory.py`, `atlas.py`, `awareness.py`, `combat.py`, `canon.py`,
`identity.py` (character cards + approval), `worldkit.py`, `worldforge.py`
(player worlds + AI bootstrap + copyright boundary), `research.py` (wiki lookup),
`canon_seed.py` / `canon_lore.py` (curated canon floor for ~32 chars across Demon
Slayer/Hazbin/JJK/AoT/Naruto/One Piece), `mana.py` (economy), `payments.py`
(PayPal Orders v2), `durable.py` (Mongo snapshot for free hosts with no disk),
`db.py` (SQLite, 33 tables), `uploads.py` (content-hashed portrait storage),
`sharecard.py` (SVG story card), `streaks.py` (retention w/o dark patterns).

## Frontend

`frontend/index.html` + `frontend/assets/{styles.css, app.js, mark.svg}`.
Server-rendered static; no build step, no Node in the runtime path. The "table"
UI: left = you (card, vitals, relationship pips, fate thread); centre = four views
(Story / Atlas / Threads / Known) + a Fight view; right = the living (every
character with relationship bars). Progressive disclosure: casual = buttons; `/`
= command palette.

## Deliberately NOT built (per `README.md`)

- **Image generation** — portraits are user-uploaded only (the "halal" constraint:
  no AI art). This is why the upload flow matters and why the upload-race bug was
  a real defect, not a nice-to-have.
- **Marketplace** — needs payment infra + moderation that don't exist yet.
- **Free-Style mode** — out of scope.

## The copyright boundary

Name someone else's fiction → you get an **inspired-by personal world**: original
characters/places, forced `personal_only` + private, never publishable, never
given the franchise name, labelled non-affiliated. Worlds you want to share must be
original. Enforced in code, not just UI.

## Money

- Mana packs (Spark $4.99 / Ember $14.99 / Bonfire $39.99 / Wildfire $79.99),
  ~69–72% worst-case gross margin vs measured cost.
- 40 free actions/day forever; out of Mana drops to lean Ember tier (free,
  unlimited, same memory depth). Mana never expires, no subscription.
- PayPal Orders v2 live (`backend/payments.py`); server reads price from a table,
  never the client; grants Mana only after `COMPLETED` capture that matches amount.
- Daily USD cost rail (`STORYLIVER_DAILY_COST_CAP_USD`, default $1.00).

## Ethical moat

"Never train on your stories"; export everything any time; no guilt-keeping dark
patterns; characters are stated to be fiction. The streak keeps your longest run
forever and costs you nothing on a break. See `README.md` "The ethical moat."
