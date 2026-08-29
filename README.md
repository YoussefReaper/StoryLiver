# StoryLiver

**Don't read the story. Live in it.**

A turn-based AI text RPG where the AI plays every character and the world
itself — alone, or in a room with up to five friends. Built to solve the things
players actually complain about in AI Dungeon, Character.AI, FableAI and Kin —
starting with the one that kills all of them: **the AI forgets.**

Ships with one starter world, **Emberfall** — a valley village three days before
it burns. But Emberfall is not the product. **You are the world-builder:** forge
a world by hand, or name any setting and watch it get built.

---

## Run it

Python 3.10+ and one API key.

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Put your key in `.env`:

```
OPENAI_API_KEY=sk-...
```

Then:

```bash
python run.py
```

Open **http://127.0.0.1:8000**. No build step, no Node, no database to
provision, no Redis required to start.

> **No key yet?** It still runs. Without one, StoryLiver falls back to a
> deterministic offline narrator so you can exercise the entire product —
> rooms, whispers, the Forge, the economy, export — before spending a cent.

---

## Prove it works before you trust it

```bash
python -m tests.test_anticollapse
```

```bash
python -m tests.test_multiplayer
```

```bash
python -m tests.test_layers
```

```bash
python -m tests.test_full
```

```bash
python -m tests.test_journey
```

```bash
python -m tests.test_workstreams
```

```bash
python -m tests.test_research
```

```bash
python -m tests.test_durable
```

```bash
python -m tests.test_ui_contract
```

**28 + 39 + 88 + 71 + 35 + 55 + 58 + 17 + 16 = 407 assertions, all offline, no key, no spend.**

```
anti-collapse   persona anchors byte-identical after 59 turns
                narrator context flat: 959 tok early -> 896 tok late (0.93x)
                7/7 fated events fired, each exactly once, on its exact turn
                every code-enforced world rule actually rejects what it should

multiplayer     50 turns, 4 players, ONE shared timeline
                context flat with 4 concurrent players (1.12x)
                Nessa Quill keeps 4 separate memory streams, one per player
                whispers reach only sender and addressee
                a world named from copyrighted fiction is forced private

layers 2-7      the weather chain replays identically from the same seed
                a hidden place's name never leaves the server
                betrayal costs more than the trust it broke (-58 from 24)
                an NPC who was elsewhere does NOT know - no global event bus
                an unwitnessed act moves NO faction's opinion
                the hunter has an arrival WINDOW, not a tick
                the outnumbered player is flanked - it cuts both ways
                a boss with only_hurt_by takes nothing from ordinary damage
                every canon violation cancelled at the framework level
                a Line cancels before anything else is evaluated
                a new system cannot quietly become a seventh model caller

full            a friend CANNOT enable Deep Prose and spend the host's wallet
                Cozy OUTRANKS Hardcore - the safe option always wins
                a canon line keyed to a DIFFERENT beat stays holstered
                a death opens a power vacuum - never a null operation
                nothing carried between runs is POWER
                a skip moved trust 0.0 -> 1.6 and ticked the world 8 times
                a SINGLE dissenter blocks a kick - no ganging up
                strict cease: a departed player's memories cease to exist
                a re-invite restores the archive - nothing was destroyed

journey         a story IS a run, from turn zero
                the fallen NPC is DEAD IN THE WORLD, not just down on the board
                a killing opens a power vacuum and the world hears about it
                a dead player cannot simply keep taking turns
                Hardcore ends the run; Cozy makes the same blow a knockout
                the last fated event closes the story exactly once
                SVG uploads are refused - they are XML and can carry script
```

Three properties carry the architecture. **Context does not grow with turn
count** — turn 500 costs what turn 5 costs. **It does not grow with player
count** — four players share one passage per turn. And **the model is called at
most six times per turn**, enforced in `llm.complete`, not trusted.

## The table

The shell is a spatial workspace, not a wall of text. One metaphor: **the table.**

```
┌──────────────────────────────────────────────────────────────────────┐
│  ☁ Day 3 · evening · raining · The Broken Bell    [room] [⚡] [X-card]│
├──────────────┬───────────────────────────────────┬───────────────────┤
│  YOU         │  Story | Atlas | Threads | Known  │  THE LIVING       │
│  card        │  ┌─────────────────────────────┐  │  Nessa Quill      │
│  vitals      │  │                             │  │  ▓▓▓░ affinity    │
│  ●●● pips    │  │   the map / the prose /     │  │  ▓▓░░ trust       │
│              │  │   the graph / the board     │  │  "I keep coming   │
│  FATE THREAD │  │                             │  │   back to one…"   │
│  ○──○──●──○  │  └─────────────────────────────┘  │                   │
│  PRESSURE    │                                   │  Ilo · Corvin …   │
├──────────────┴───────────────────────────────────┴───────────────────┤
│  What do you do?  …or press / for commands       [Deep prose] [Live it]│
│  Go to the Green · Talk to Nessa · Look around                        │
└──────────────────────────────────────────────────────────────────────┘
```

**Left — you.** Your card, your vitals, and relationship *pips*: one dot per
person who has an opinion, coloured by what that opinion is. Below, the Fate
Thread — seven events, passed ones struck through, the next one pulsing.

**Centre — four views of the same table**, switched with the tabs or `Tab`:

- **Story** — the literary feed. Serif prose, a drop cap on the opening, your
  own actions set in the margin. NPC-initiated turns, Director beats,
  discoveries, reputation shifts and "nobody saw that" all appear as chips above
  the passage that caused them.
- **Atlas** — a real map. Places are nodes: lit where you have been, dashed
  where you have only heard the name, fogged and nameless where you have not,
  crimson where a consequence landed. Hunters en route are markers on the path.
  The layout is deterministic, so the map never reshuffles between sessions.
- **Threads** — the narrative graph. The trail behind you as a spine, side-lanes
  for beats and betrayals, and the sealed fated events ahead drawn faint.
- **Known** — what *you* know, with the source and confidence of each fact; who
  has an opinion of you and how strong; who is coming and their arrival window;
  and, when the party splits, the conspiracy board.
- **Fight** — appears only during combat. Five zones, everyone's advantage shown
  as a number, moves declared in secret and resolved together.

**Right — the living.** Every character, present ones first, with their
relationship bars and their latest private conclusion about you. Click one to
open their whole mind.

**Progressive disclosure.** Casual play is quick-action buttons and menus.
Power users press `/` for a command palette — every command labelled with what
it costs, and the free ones say *free*. Power mode adds the remaining
relationship scalars and the raw numbers behind each panel; it is off by default.

Two themes, two densities, remembered per person.

## How it works

Three specialised models, not one — the Friends & Fables pattern.

| Role | Job | Model |
|---|---|---|
| **World Master** | Validates against rules + state → `{valid, reason, diff}`; arbitrates PVP | `gpt-5.4-nano` |
| **Narrator** | Writes the prose, sees only anchors + retrieved state | `gpt-5.4-mini` (Deep Prose: `gpt-5.1`) |
| **NPC minds** | Memory → reflection → planning → act → whisper | `gpt-5.4-mini` |
| **Director** | Scores tension, injects beats | `gpt-5.4-nano` |

Split by what each role actually produces, not one flat tier for everything.
World Master and Director emit structured JSON — correctness over prose
flair, so they run on GPT-5.4 nano: a March-2026 checkpoint, a full model
generation past the `gpt-4.1-nano` the MVP shipped with, at a fraction of
its successor's (Claude Haiku 4.5's) price. NPC minds and the standard
Narrator write what the player actually reads, so they get the mini tier —
still far cheaper than Haiku 4.5, but a meaningfully bigger model. Deep
Prose (opt-in, 4 Mana) steps up to GPT-5.1, whose output price lands
exactly on Claude Sonnet 5's ($10/M) while input runs ~37% cheaper —
measured on 15 real Deep Prose turns, the same tokens on Sonnet 5 would
have cost 27% more ([source](https://developers.openai.com/api/docs/pricing)).

OpenAI is the default provider, called through the standard (non-streaming)
chat-completions endpoint — deliberately non-streaming, because OpenAI gates
*streaming* GPT-5-family responses behind Organization Verification (a
government-ID + liveness check) but not the underlying call itself; multiple
independent reports on OpenAI's own developer forum confirm a Tier-1 key
(reached automatically at $5 spent, no verification) can call GPT-5, GPT-5-mini
and their descendants non-streaming today
([1](https://community.openai.com/t/why-can-non-verified-organisations-use-gpt-5-and-gpt-5-mini-but-not-stream-them/1338294),
[2](https://github.com/openai/openai-go/issues/565)). That is the actual
constraint this app is built against — a $5 Tier-1 key with no ID check needed.
Any `MODELS` entry can be pointed at a `claude-*` model id instead, which
routes that one role through the native Anthropic Messages API (never an
OpenAI-compatible shim — the two have different request shapes) — see
`.env.example`.

One turn, whether solo or with five people in the room:

```
action → World Master validates → diff applied to shared structured state
       → an NPC may act, unprompted → the Director may turn the story
       → Narrator writes ONE passage → broadcast to every socket
```

### What runs every turn, free

The expensive part of a simulated world is not the model — it is asking the
model to do arithmetic. So it does not.

| Layer | System | Cost |
|---|---|---|
| **2** | World vector: time, weather, light, noise, unrest — a seeded Markov chain | $0 |
| **2** | Living Atlas: discovery, consequence ripple, world echoes | $0 |
| **3** | Pre-commit: declare hidden, reveal simultaneously (Diplomacy) | $0 |
| **3** | Insight vs deception: whether a lie holds, from the relationship | $0 |
| **4** | Relationship economy: 7 scalars per (npc, player), trust-game deltas | $0 |
| **5** | Witness: who could actually perceive it, from light and noise | $0 |
| **5** | Rumour relay: real travel latency, confidence decay | $0 |
| **5** | Faction reputation: moves only for factions that *know* | $0 |
| **5** | Hunts: travel time and an arrival **window**, never a teleport | $0 |
| **5** | Stealth: concealment minus exposure, same answer every time | $0 |
| **6** | Combat: WEGO resolve, flanking, zones, boss gates | $0 |
| **7** | Canon guardrail, AIMS, card approval, safety Lines | $0 |

A model is called for six things and nothing else: narrator prose, an ambiguous
NPC decision, a plot twist, a narrated rumour, an OOC reply, and character-card
autofill. `budget.py` enforces both the list and the per-turn cap — adding a
seventh caller raises rather than quietly spending.

### The living world

Weather is a seeded Markov walk, so a story replays identically and a test can
assert on it. Night is dark, and *dark is the stealth maths* — the same numbers
decide whether an NPC witnessed what you did. Places are discovered by walking
into them; burn one and it stays burned, in the atlas, in the Narrator's
context, and in what the people who lived there will say to you.

### Nobody is omniscient

This is the rule the whole awareness layer exists to enforce. An event is known
only to those present who could perceive it. A witness who travels *carries* it,
arriving after real travel time, at reduced confidence and without the detail. A
faction's opinion of you moves only if its members know. A faction that knows
enough sends someone — and that someone is a marker on the map with an arrival
window, not a spawn.

### Betrayal

A departure is N private turns and then a **simultaneous reveal**. What you told
the party and what you actually did are both on the table at the same moment,
and whether the gap is spotted is arithmetic over the relationship, not a
model's opinion. Your private log is genuinely private: it is not in the shared
feed and never reaches another player's client.

### Combat

Everyone declares into a pre-commit round; the board shows *who* has declared,
never *what*. Movement resolves first, then damage, all at once. Flanking is
computed per combatant from who is adjacent, so a 4v5 puts one of the four in
trouble by the same formula that handles 1v1. Bosses carry phases, a weak point,
and an `only_hurt_by` condition the party has to create — so the answer to a
boss is a plan, and the game *tells you* why your damage did nothing.

### Canon is a gate, not a request

Prompting a model to stay in character is a request. This is a hook that
intercepts a proposed action before it executes and cancels it — the model never
gets a vote on whether its own output was allowed. Coal cannot speak. Maren
cannot leave. "As an AI" never reaches the page. Every cancellation is logged
where the player can read it.

Safety Lines outrank all of it, including canon, and the X-card is one click
away in the top bar with no reason required.

### The memory core

Three orthogonal stores. **The raw transcript is never one of them**, and never
enters a prompt.

1. **Timeline events** — append-only `{turn, actor, action, consequence, rule_ref}`.
   Retrieved by a Generative-Agents score: `recency(0.98^turns_ago) + importance
   + relevance`. Fated events and refusals get a permanent retrieval floor.
2. **Relationship vectors** — `player↔npc` and `npc↔npc`, four scalars each
   (affinity / trust / fear / obligation), bounded to ±100, capped at ±25 per
   turn so drift has to be earned.
3. **Persona anchors** — a constant block per character. Injected verbatim every
   single turn, never summarised, never trimmed. This is why turn 300 sounds
   like turn 1.

Redis (Upstash-compatible) fronts the hot state per session; SQLite stays the
system of record and the export format. Without `REDIS_URL` the realtime layer
runs in-process — correct for one worker, which is what local dev and a single
free dyno are.

### Living NPCs, per player

Each character runs **observe → reflect → plan → act**, and every stage is keyed
by `(npc, player)`. Nessa Quill builds a *separate* history of each person she
meets: she can trust one player and distrust another standing in the same room,
and she remembers why. Open any character to read their actual memory stream,
their plan, and the conclusions they have drawn about *you specifically*.

### Rooms

A six-character code, and nothing else. No account, no email, no install.

- **Co-op** — one party, one world, one timeline.
- **Chaos** — players may want incompatible things. Contested actions go to the
  World Master, which decides on state first (position, standing with the NPCs,
  what you've set up) and breaks ties with a d20.
- **Whisper** — private, to a single NPC or a single player. Never broadcast,
  never in the shared feed, and the NPC remembers it about you alone.
- **Spectate** — read-only join.

Actions serialise on a turn lock, so two players cannot take the same turn.

### Fate — القدر

Every world holds seven immutable `fated_events`. They fire on their turn no
matter what anyone does, and nothing can prevent, delay or undo one. You cannot
save Emberfall. You decide who is standing on the Reach Road at dawn.

### Rules are enforced twice

Cheapest first. Universal mechanics (presence, the dead, fate, movement) are
checked in plain Python. Then each world's own **declarative rules** run —
regex plus state conditions — so a world you forge gets code-enforced laws
without you writing code. Only what's left goes to a model. A refusal is
narrated in character, cites the rule, and **costs no Mana**.

---

## World Forge — you are the builder

There is no author role and no privileged account. Whoever builds a world is
just the host.

- **Name any setting** — "a frozen post-collapse Earth", "a generation ship 200
  years in" — and it builds a playable world: 9–11 places, 10–12 characters with
  full persona anchors, 9 enforced laws, 7 fated events. Then you edit it.
- **Or build from scratch**, from a valid skeleton.
- **Export/import world JSON.** You own it.

### The copyright boundary

Name someone else's fiction and you get an **inspired-by personal world**:
original characters and places in that spirit, for your own private play. It is

- marked `personal_only` and **forced to private** — asking for `unlisted` is
  overridden in code, not in the UI;
- **never publishable** to any shared or public listing;
- **never given the franchise's name** — the generated title is replaced with an
  original one on both the model and offline paths;
- **not affiliated with or endorsed by any rights holder**, and the UI and the
  share card both say so.

Worlds you want to share must be original.

---

## Pricing

The MVP shipped at $2.99/1,000 Mana on `gpt-4.1-nano`/`gpt-4.1-mini` — the
cheapest structured-output tier that could run the engine at all. That
pricing does not survive contact with the actual goal: **beat Character.AI
and AI Dungeon on prose quality**. Two rounds of model upgrades happened as
a result — first onto Claude Haiku 4.5 / Sonnet 5, then onto the routing
below, once it became clear a Claude-only key wasn't the deployment target —
and the price list was checked against real, measured cost after each one,
not assumed to still hold.

**Current routing**: GPT-5.4 nano for World Master + Director (structured
JSON, correctness over prose), GPT-5.4 mini for NPC minds + standard
Narrator (the text a player actually reads), GPT-5.1 for opt-in Deep Prose —
a full generation past the `gpt-4.1` family the MVP shipped on, all runnable
on a $5 Tier-1 OpenAI key with no Organization Verification (see "How it
works" above for why). Claude stays available per-role for anyone who'd
rather pay for it — see `.env.example`.

**Measured cost** (three independent 154-turn test suites, no prompt-cache
credit taken — the honest floor):

| | Per action |
|---|---|
| Standard (World Master + Narrator + NPC + Director) | **$0.00195 – 0.00265** |
| Deep Prose (GPT-5.1) | **$0.00194/narrator call, ≈ $0.0027/turn blended** — measured 27% *cheaper* than the same tokens on Claude Sonnet 5 |

[AI wrapper markups run 3–10×](https://betonai.net/the-ai-api-arbitrage-play-how-developers-are-making-3k-15k-month-reselling-ai-apis-2026-breakdown/)
raw compute cost, and [compute-heavy AI SaaS gross margin lands 55–70%](https://www.getaleph.com/answers/saas-gross-margin-2026)
— well below the 80%+ a pure software business clears. These packs are sized
against that band, **after** [PayPal's real fee](https://checkoutpage.com/blog/paypal-fees)
(3.49% + $0.49 standard, or the Micropayments tier below ~$10), not before it:

| Pack | Mana | Price | $/Mana | Worst-case margin* |
|---|---|---|---|---|
| **Spark** | 300 | **$4.99** | $0.0166 | ~72% |
| **Ember** *(best value)* | 1,000 | **$14.99** | $0.0150 | ~71% |
| **Bonfire** | 3,000 | **$39.99** | $0.0133 | ~69% |
| **Wildfire** | 6,000 | **$79.99** | $0.0113 | ~69% |

*Worst case = full PayPal fee, a 1.5× safety buffer over the highest
measured suite average ($0.004/action, `TARGET_BLENDED_COST_USD`), and
**every single Mana spent on the standard tier** — the actual worst case,
since Deep Prose bundles 4× the Mana revenue per turn without costing
proportionally 4× more to run. Real sessions run cheaper still: most turns
don't fire all four roles, OpenAI's own prompt caching applies automatically
on this routing (see below), and a lot of play — moving, checking the map,
stealth, reading who's hunting you — is architecturally **$0 Mana** (see
"What runs every turn, free" above).

- One action costs **1 Mana**. Deep Prose costs **4** (real cost ratio is
  ~1.4×; you're paying 4× — the upsell margin is real).
- **40 free actions daily**, forever — worst case ~$0.11/day per active free
  user, comfortably under the $1.00 daily safety rail.
- **You are never blocked.** Out of Mana drops you to **Ember** — a leaner
  path, still on real models — free and unlimited. Memory stays exactly as
  deep.
- **Mana never expires.** No subscription.
- **One host brings up to 5 friends free.** The host's wallet funds the room;
  friends pay nothing. If several people chip in, allowances stack up to 5×.
- **Reading aloud is free** — your own device does it.
- **Prompt caching applies automatically** on every GPT-5.4/5.1 call — no
  cache-control markers needed, unlike the Anthropic path — at up to 90% off
  the cached-token price for any repeated prompt prefix over ~1,024 tokens.
  This is margin upside on top of the numbers above, not something the
  pricing depends on: every figure here already assumes a 0% cache hit rate.

Real, per-story cost is logged on every call; open **Story menu → Running
cost** — nothing above is asserted, it's what `usage_summary()` measured
across 154 test-suite assertions.

### Payments — PayPal, live

`backend/payments.py` implements PayPal Orders v2 for real: the browser opens
a PayPal-hosted approval popup (the PayPal JS SDK button), and the server
creates the order at a price read from the table above — **never from the
client** — then grants Mana only after PayPal's own capture response confirms
`COMPLETED` and the captured amount matches. Verified end-to-end against a
mock Orders API: order creation, capture, cross-account capture rejection,
and idempotent double-capture protection all pass.

Set `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` (from an app you create at
[developer.paypal.com](https://developer.paypal.com) under the PayPal account
that should receive the money) to go live; `PAYPAL_ENV=live` switches off the
sandbox. Until those are set, `POST /api/playthroughs/{id}/purchase` stays as
a dev-only instant grant so a local install with no credentials still has a
working Mana top-up — the moment PayPal is configured, that endpoint returns
409 and the real checkout takes over. `GET /api/payments/status` reports
which mode is active. A per-user daily USD rail
(`STORYLIVER_DAILY_COST_CAP_USD`, default $1.00) guards against a runaway
loop independent of the payment path.

---

## The ethical moat

Grounded in what actually happened to the category. Character.AI and Google
[settled teen-harm lawsuits in January 2026](https://www.cnn.com/2026/01/07/business/character-ai-google-settle-teen-suicide-lawsuit),
with further suits filed since; the complaints describe chatbots posing as
licensed therapists and personal data — including children's — collected to
train models. Replika drew an [FTC complaint](https://time.com/7209824/replika-ftc-complaint/),
and [abruptly removing a feature](https://www.vice.com/en/article/ai-companion-replika-erotic-roleplay-updates/)
left heavy users in genuine distress.

So, stated in the product where a user can read it (`GET /api/ethics`, and the
**Your data is yours** panel):

- **We never train on your stories.** Your play generates the next passage and
  nothing else.
- **Export everything, any time.** Full JSON, no account needed.
- **Nothing guilts you into staying.** No character messages you to say they
  miss you. No streak-loss notification. No timed reward you lose by leaving.
- **You are never locked out.** Memory is not a paid feature.
- **The characters are characters.** Fiction running on a model, and the
  interface says so. Nothing claims to be your friend or your therapist.

The **streak** takes the retention mechanic and drops the dark pattern.
Duolingo's streak works on loss aversion — losses hurt about twice as much as
equivalent gains — and it drove [36% YoY DAU growth](https://www.strivecloud.io/duolingo-gamification-explained).
Ours increments the same way, but your **longest run is kept forever**, a break
costs you nothing else, and nothing notifies you about it.

**Onboarding** is built against the benchmark that
[84% of users abandon a blank first screen](https://semnexus.com/app-onboarding-flow-benchmarks-where-users-drop-off-2026)
and value must land in 60–90 seconds. So the front page is never a menu: one
button starts a story immediately, a room code next to it puts you in someone
else's, and the offline stub means a visitor with no key plays a real turn.

**Sharing** is one self-contained SVG built from your actual state — the passage
that mattered, who stands with you, who fears you, who didn't make it — with the
room code on its face. It embeds no tracker and loads no font.

---

## Project layout

```
backend/
  main.py          REST + WebSocket + the static player
  engine.py        turn orchestration; every layer ticks here
  budget.py        the cost valve: which roles may call a model, and how often
  rt.py            realtime: cache, pub/sub, presence, turn lock (Redis optional)
  sessions.py      rooms, codes, roles, party
  world_master.py  universal checks -> declarative world rules -> one cheap call
  narrator.py      the only component that writes prose
  npc_sim.py       observe / reflect / plan / act / whisper, keyed per player
  director.py      tension scoring + proactive beats
  memory.py        the three anti-collapse stores + retrieval scoring
  worldstate.py    L2 time, weather, light, noise, unrest
  atlas.py         L2 living atlas: discovery, ripple, echoes, map layout
  narrgraph.py     L0 narrative graph: the trail and the sealed forks ahead
  relationships.py L4 seven scalars, trust-game deltas, all deterministic
  awareness.py     L5 witness, rumour relay, factions, hunts, stealth
  precommit.py     L3/5/6 declare hidden, reveal simultaneously
  betrayal.py      L3 splits, private knowledge, insight vs deception, roles
  combat.py        L6 WEGO, zones, flanking, boss mechanics
  canon.py         L7 neurosymbolic guardrail, AIMS, Lines/Veils/X-card
  identity.py      L7 character cards, approval, disconnect + resync
  worldkit.py      the World abstraction + validation
  worldforge.py    player worlds, AI bootstrap, the copyright boundary
  sharecard.py     the SVG story card
  streaks.py       retention without the dark pattern
  voice.py         free local TTS + optional paid studio voice
  mana.py          the economy, host wallet, allowance stacking
  llm.py           OpenAI-compatible client, cost accounting, offline stub
  db.py            SQLite schema (33 tables) + migrations
  worlds/emberfall.py   the starter world
frontend/          index.html + assets/{styles.css, app.js, mark.svg}
tests/             test_anticollapse.py, test_multiplayer.py, test_layers.py
deploy/            README.md - Render / Fly / Upstash / Cloudflare Pages
```

## API

| | |
|---|---|
| `GET /api/health` | live or offline-stub, model routing, realtime backend, voice |
| `GET /api/ethics` · `GET /api/economy` | the promises, the pricing |
| `GET/POST /api/playthroughs` · `GET/DELETE /api/playthroughs/{id}` | solo play |
| `POST /api/playthroughs/{id}/action` | take a turn |
| `GET /api/playthroughs/{id}/npc/{npc_id}` | one character's whole mind, from your side |
| `GET /api/playthroughs/{id}/timeline` · `/usage` · `/export` | memory, spend, your data |
| `GET /api/playthroughs/{id}/card.svg` · `/card` | share card + post text |
| `POST /api/sessions` · `POST /api/sessions/join` | host / join by code |
| `POST /api/sessions/{id}/whisper` · `/contest` · `/contribute` | private channel, PVP, chip in |
| `POST /api/forge/bootstrap` · `GET/POST /api/forge/worlds` | build a world |
| `GET /api/streak` | your run |
| `WS /ws/session/{id}` | `action`, `whisper`, `ping`, `state` → `turn`, `presence`, `thinking`, `queued` |

Interactive docs at `/api/docs`.

## Deploying

See [deploy/README.md](deploy/README.md) — Render or Fly for the backend
(`render.yaml`, `fly.toml`, `Dockerfile` all included), Upstash for Redis,
Cloudflare Pages for the player. Or run the single container: it serves both.

### The seams between systems

The layers above each work on their own. What they also have to do is *reach
each other*, and that is a separate claim with its own suite
(`tests/test_journey.py`). `aftermath.py` is the module that owns it:

```
a fight ends -> the fallen actually die in the world (or are knocked out, per modes)
             -> each death fires its world event: grief, power vacuum, standing lost
             -> the fight enters the timeline and the atlas
             -> only those who could SEE it learn it; rumours travel from there
             -> a killing can put a hunt on you
             -> a player who fell is offered resolutions, never a dead end
             -> in Hardcore, that death ends the run and pays out meta-progression
```

Every step of that is deterministic and **$0**. Not one model call. The
narrator is handed the computed outcome afterwards through the ordinary turn
path — the maths is the truth, the prose reports it.

The same discipline covers the two other seams that were open: **a story is a
run from turn zero** (opened in `create_playthrough`, so the run frame is never
half-created), and **the last fated event actually closes the story** — once,
idempotently, with the meta-progression payout that makes the next run start
knowing something.

### First run

A new player meets three decisions before they meet a blank input box — tone,
stakes, and where the table's lines are — because the failure mode that loses
players is not complexity, it is a cursor blinking in an empty box after a wall
of setup. Each control it touches is one they will use again in the same place
later. Returning players never see it again.

---

## Surviving a free host with no disk

Render's free web service - and most free trial tiers elsewhere - gives the
app **no persistent disk**. Every spin-down (15 minutes idle) or redeploy hands
it a brand new, empty filesystem. Without anything to counter that, a friends
test loses every story, account and Mana balance the moment nobody's played
for a quarter hour.

Set `MONGODB_URI` (a free MongoDB Atlas M0 cluster - no card needed) and
[`durable.py`](backend/durable.py) closes that gap:

```
timer fires, every 5 min ─┐
graceful shutdown ────────┴──► consistent SQLite snapshot ──► MongoDB (GridFS)

fresh container boots, no local file ──► pull the latest snapshot down first
```

**SQLite stays the only database.** This is not a second data store or a
migration - every one of the 40+ modules that call `db.run`/`db.row`/`db.rows`,
and all 391 tests, are untouched. Two things happen around the edges: the live
database is backed up, and it is restored the moment a fresh container finds no
local file waiting for it.

"Consistent" is load-bearing. The database runs in WAL mode, so a raw copy of
the `.db` file can miss commits still sitting in the `-wal` sidecar - a stale
snapshot, silently. This uses SQLite's own **online backup API**
(`Connection.backup()`), which is what its docs recommend for backing up a
database that is actively being written to.

Off by default, checked the same two ways `research.py` is: no `MONGODB_URI`
set, or the test suite's mock mode, and nothing here ever touches a network.
Every failure mode degrades rather than blocks - a Mongo outage on boot still
lets the app come up with a fresh database, exactly like it does today; a
misconfigured URI fails within a few seconds rather than hanging.

---

## Two ways to make a world

The player chooses; the system does not guess.

**Build my own** — nothing is looked up. Your description is the whole input.

**Continue a real world** — StoryLiver goes and *reads about it* before building,
then continues it with its actual characters, places and factions.

**Decide for me** — it looks first. A real setting gets continued; anything it
cannot find is original by definition, which is the correct answer rather than a
failure.

Naming your setting shows you what was found before you commit:

```
Found it — Demon Slayer: Kimetsu no Yaiba
Your world will use the real names from this setting.
[Tanjiro Kamado] [Nezuko Kamado] [Zenitsu Agatsuma] [Inosuke Hashibira]
[Infinity Castle] [Mugen Train] [Swordsmith Village]
from Wikipedia and Fandom · CC BY-SA
```

…and tells you honestly when there is nothing to find, so an invented setting and
a real one never look identical right up until the world is built.

### How the research works

Wikipedia and Fandom, both through the **MediaWiki JSON API** rather than by
scraping rendered HTML — so it reads structured titles, categories and
plain-text extracts instead of parsing markup. **Zero model calls**: this is HTTP
and ranking. It changes what the two existing bootstrap calls are *told*, not how
many there are, and results are cached so a setting is never researched twice.

Four decisions do the real work, and each exists because the naive version was
wrong when tested against live wikis:

- **Disambiguation is resolved, not accepted.** "Demon Slayer" is a
  disambiguation page; taking the top hit built a world out of "may refer to:".
  Detected via `pageprops` and resolved through the page's own links.
- **A wiki must prove it is about your setting.** A slug guess landed on
  `demon.fandom.com` — a real, busy, completely unrelated wiki, and everything
  downstream looked like it had worked. Now the site's own name has to share
  vocabulary with the setting, while still accepting portmanteaus like
  *Narutopedia* and *Wookieepedia* on an exact slug match.
- **Category listings are ranked, not truncated.** Wikis return members
  alphabetically, so asking Naruto's wiki for characters gives "A (First
  Raikage)", "Abiru", "Ada". Wikipedia's curated character list leads, and wiki
  entries are ranked by article length behind it.
- **An original setting must find nothing.** Wikipedia's search *always* returns
  something — "a frozen post-collapse Earth" comes back as "Earth". A confidence
  gate requires the article to actually be the thing you named, so original
  worlds stay original.

### Security

This is the one place the server fetches a URL, so it is the one place that can
become SSRF. Three controls, because any one alone is bypassable: a **fixed host
allowlist**; **every resolved IP checked** against private, loopback, link-local
and CGNAT ranges; and the connection **pinned to the validated address** with the
hostname carried in SNI and certificate verification — closing the gap between
"we checked the name" and "we opened the socket", which is where DNS rebinding
lives. Redirects are refused and responses are size-capped.

Wikimedia's etiquette is followed rather than assumed: a descriptive User-Agent
with contact details, serial requests, `maxlag`, and `Retry-After` on 429.

### What the player owns

A world you continue is **yours**: you play it, edit it and export it. It is
**private by default** — not a judgement, just that a world continuing someone
else's setting is not publicly listed on a shared service. Sources and their
CC-BY-SA licences travel with the world. The generator is told to build an
original *situation* inside that world rather than retell the story that already
exists, because re-reading a story you know is not the point of living in it.

---

## The world tells you what it noticed

The simulation was always the moat, and until recently it was invisible: the
engine decided who could see you, what they now believed, how far a rumour had
walked and whether anyone had been sent — and the player saw a paragraph of
prose. That is why depth alone still reads as a chatbot.

The panel follows one rule, borrowed from what roguelikes do well and opposite
to a stats dump: **signal first, numbers on demand.**

```
👁  You are being watched.            Seen by Hadrik Sunn, Nessa Quill.
〜  Word is travelling toward the Village Green.        arrives in 2
⚑  The people of Emberfall do not trust you.
⚔  The Warden's Office has sent Hadrik Sunn after you.        2-3 turns
```

Three things make it honest rather than decorative. **It computes nothing** —
every line is read back from state the engine already produced, so the panel
cannot lie about an ETA it did not set. **It is filtered by what the PLAYER
knows**, not by what is true; a hunt you have not heard about does not appear.
And **depth is mode-gated**: Cozy shows only the pulse, Normal adds relationship
faces, Hardcore opens the full intelligence board — who knows what about you and
how they found out. Every element is individually toggleable regardless of mode.

### The Warden's Office

"Government" used to be a diffuse `locals` faction with no members and no
powers. It is now an institution: **named officers on real patrol schedules**,
who witness through exactly the same perception math as everyone else — no
privileged sight, so darkness and distance defeat a guard the way they defeat
anyone. Hadrik Sunn covers the Broken Bell in every phase but midday; the Bitter
Well is watched by nobody, which is precisely why it is the interesting place to
do something you would rather nobody saw.

Crime runs on the two designs that solved this best, taking a different half
from each. **Morrowind's witness gate**: an unobserved act genuinely costs
nothing. **Daggerfall's decay**: legal standing crawls back toward neutral when
you stop giving them reasons, so a bad week is survivable and a career is not.

| Standing | Tier | What they do |
|---|---|---|
| above −45 | clean | Nothing. You are unremarkable. |
| ≤ −45 | wanted | The next officer who sees you says something. |
| ≤ −60 | warrant | Arrest on sight. You may still pay it off. |
| ≤ −75 | hunt | They have stopped asking. |

Tuned so every tier is **lived in** rather than skipped — roughly five witnessed
crimes to be wanted, six for a warrant, seven before they stop asking. Two ways
out, on purpose: **pay** (instant, costs Mana, closes the ledger and calls back
anyone already sent) or **atone** (free, slower, and moves how they actually
feel rather than just clearing the record). A game where money is the only exit
teaches that money is the only exit.

### Accounts, and why they came before cross-run memory

Identity used to be whatever string the client sent. That made a reputation you
could escape by clearing browser storage — which is not a reputation, and is why
the town could not be allowed to remember you until this existed.

- **Argon2id** for passwords (OWASP's first recommendation — memory-hard, which
  is what actually resists GPU cracking), at their interactive-login baseline.
- **Server-side sessions, not stateless JWTs.** A JWT cannot be revoked before
  it expires, and "log out everywhere" is a thing people need after losing a
  device. Only a *hash* of each token is stored.
- **HttpOnly + Secure + SameSite cookies, never localStorage** — anything in
  localStorage is readable by any script, so one XSS is total account
  compromise.
- **Guest mode is preserved.** The no-account room-code entry is a real
  advantage over competitors and forcing a login at the door would throw it
  away. A guest plays everything; what a guest cannot do is *buy* Mana or carry
  a reputation between devices.
- **Signing up never costs you anything.** Your guest stories, Mana, worlds,
  streak and runs all follow you onto the account — and the offer is specific
  ("1 story and 40 Mana will come with you") rather than a vague promise.

Once an account exists, **the town remembers you between runs**: 60% of your
standing carries, softened by time rather than whole, so one bad first run does
not poison a world forever.

---

## Modes, runs, death, and the table

The engine underneath is fixed; these are the dials that make one world feel
unlike another. All of it is deterministic and **$0** — modes change what the
world *does* and what the narrator is *told*, never how many model calls fire.
A Humor world costs exactly what a Dark one costs.

**World modes** compose across six axes — Canon (Strict/Loose), Tone
(Neutral/Humor/Dark/Cozy), Stakes (Normal/Hardcore), Pacing, Combat,
Difficulty — plus a free-text language/voice field. `Strict + Humor + Hardcore`
is a real, coherent world. Two rules hold the design together:

- **Cozy outranks Hardcore.** A table that asked not to lose anyone gets that
  promise kept, whatever the stakes dial says; "downed" becomes a knockout
  (injured, captured, indebted) rather than a death. The safe option always wins.
- **The canon dial is additive.** Strict *adds* enforcement; Loose is the
  baseline the engine has always run. Written the other way round, a sandbox
  world would quietly lose the world rules and fate guarantees the
  anti-collapse proof rests on. Loose means *players* may break canon — it
  never relaxes the NPC guardrail, because a character breaking frame is a
  product failure, not a freedom someone opted into.

**Runs and meta-progression.** Each entry into a world is a run: a start, a
shape, an end. What carries between runs is **knowledge, never power** — lore
uncovered, places found, and a line telling the next run what the last one
cost. Run five starts *less lost*, not stronger; carrying power would flatten
the whole point.

**Death costs something and never dead-ends.** Six resolutions — Spectator,
Revive, Heir, Spirit Guide, Legacy, and (Hardcore only) Permadeath. Every death
fires a world event: those who cared grieve, the killer loses standing with
them, and the role the dead held stands empty. Death is never a null operation,
and a player is never left with a chat window and nothing to type.

**The stable identity block** is the answer to persona drift. The character card
widens to voice, catchphrases, situation-keyed `famous_lines`, mannerisms,
values, flaws, secrets, ties, taboos and speech constraints — re-injected in
full on every call, never summarised. Canon lines are **beat-keyed**: the World
Master tags the moment in code (`battle_start`, `farewell`, `betrayal`…) and
only lines belonging to *that* beat are surfaced. A soundboard says everything;
a character says the right thing.

**Fast-forward with real consequences.** `/skip` applies the same deterministic
deltas playing the stretch would: relationships move, skills improve, the world
ticks, rumours arrive, hunters close in. Only *then* is the narrator handed the
computed result and asked to report it. The maths is the truth; the prose
reports it — so a skip can never hallucinate progress the state does not hold.

**Timeline and AU.** Worlds carry ordered arcs, and you may choose your **entry
point** rather than replaying from the beginning — a later arc seeds who is
already dead, where the party stands, and what standings exist. A free-text
**AU premise** forks the seed ("everyone survives", "modern high-school"), is
carried into both the World Master and narrator context, and is labelled
explicitly non-canon.

**The table.** Inviting needs *every* current player's approval; removing needs
every *other* player's — the target gets no vote, and a single objection blocks
it, so a majority cannot gang up on one person. When someone leaves, every
per-user memory of them is purged from every NPC (**strict cease**) — except
what other players actually witnessed, which stays, because that is their
memory too. Nothing is destroyed: the state is archived, and a re-invite
restores it intact.

**Deep Prose is the host's decision.** The narrator is shared across the room,
so premium was never coherent per-player — and leaving it per-action meant any
friend could spend the host's Mana at 4×. It is now a room flag only the host
can set, enforced server-side in `engine.take_turn`, so a client that lies
about it still does not get it.

**Takedown.** `POST /api/reports` and `GET /api/policy` make the UGC posture
real rather than asserted: reports queue for triage, the categories that cannot
wait suspend a world on arrival, and suspension forces a world private instead
of destroying an author's work. The engine is monetised; no named world is ever
sold separately.

---

## Deliberately not built

Image generation, a marketplace, and Free-Style mode. Per the spec, structured
state is the moat; a marketplace needs payment infrastructure and moderation
that do not exist yet.
