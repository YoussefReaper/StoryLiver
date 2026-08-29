"""SQLite persistence. One connection per call keeps this thread-safe under
FastAPI's threadpool without any session machinery."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playthroughs (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  world_id TEXT NOT NULL,
  title TEXT NOT NULL,
  protagonist TEXT NOT NULL,
  current_turn INTEGER NOT NULL DEFAULT 0,
  current_location TEXT NOT NULL,
  mana_balance INTEGER NOT NULL DEFAULT 0,
  mana_used INTEGER NOT NULL DEFAULT 0,
  tension REAL NOT NULL DEFAULT 0.25,
  last_beat_turn INTEGER NOT NULL DEFAULT -99,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- (a) append-only timeline events: the canonical record of what happened.
CREATE TABLE IF NOT EXISTS timeline_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  turn INTEGER NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  consequence TEXT NOT NULL,
  rule_ref TEXT,
  kind TEXT NOT NULL DEFAULT 'action',
  importance INTEGER NOT NULL DEFAULT 3,
  location TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_timeline_pt ON timeline_events(playthrough_id, turn);

-- (b) relationship vectors, user<->npc and npc<->npc.
CREATE TABLE IF NOT EXISTS relationships (
  playthrough_id TEXT NOT NULL,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  affinity REAL NOT NULL DEFAULT 0,
  trust REAL NOT NULL DEFAULT 0,
  fear REAL NOT NULL DEFAULT 0,
  obligation REAL NOT NULL DEFAULT 0,
  last_interaction_turn INTEGER NOT NULL DEFAULT -1,
  PRIMARY KEY (playthrough_id, src, dst)
);

-- NPC minds (Generative Agents): observations, reflections, plans.
CREATE TABLE IF NOT EXISTS npc_state (
  playthrough_id TEXT NOT NULL,
  npc_id TEXT NOT NULL,
  location TEXT NOT NULL,
  plan TEXT NOT NULL DEFAULT '[]',
  reflections TEXT NOT NULL DEFAULT '[]',
  last_reflect_turn INTEGER NOT NULL DEFAULT -99,
  last_act_turn INTEGER NOT NULL DEFAULT -99,
  alive INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (playthrough_id, npc_id)
);

-- Keyed by (npc, player): an NPC remembers different things about each player.
-- player_id '*' is a shared memory the character holds regardless of who asks.
CREATE TABLE IF NOT EXISTS npc_memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  npc_id TEXT NOT NULL,
  player_id TEXT NOT NULL DEFAULT '*',
  turn INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'observation',
  text TEXT NOT NULL,
  importance INTEGER NOT NULL DEFAULT 3,
  last_access_turn INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_npcmem ON npc_memories(playthrough_id, npc_id, player_id, turn);

-- Per-(npc, player) mind: what this character concluded about THIS player.
CREATE TABLE IF NOT EXISTS npc_player (
  playthrough_id TEXT NOT NULL,
  npc_id TEXT NOT NULL,
  player_id TEXT NOT NULL,
  plan TEXT NOT NULL DEFAULT '[]',
  reflections TEXT NOT NULL DEFAULT '[]',
  last_reflect_turn INTEGER NOT NULL DEFAULT -99,
  last_act_turn INTEGER NOT NULL DEFAULT -99,
  PRIMARY KEY (playthrough_id, npc_id, player_id)
);

-- The rendered feed. Presentation only; never fed back into a prompt.
CREATE TABLE IF NOT EXISTS narrative (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  turn INTEGER NOT NULL,
  kind TEXT NOT NULL,
  actor TEXT,
  text TEXT NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_narrative_pt ON narrative(playthrough_id, id);

CREATE TABLE IF NOT EXISTS usage_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT,
  user_id TEXT NOT NULL,
  role TEXT NOT NULL,
  model TEXT NOT NULL,
  in_tokens INTEGER NOT NULL,
  out_tokens INTEGER NOT NULL,
  usd REAL NOT NULL,
  day TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_day ON usage_log(user_id, day);

CREATE TABLE IF NOT EXISTS daily_ledger (
  user_id TEXT NOT NULL,
  day TEXT NOT NULL,
  free_mana_used INTEGER NOT NULL DEFAULT 0,
  paid_mana_used INTEGER NOT NULL DEFAULT 0,
  usd_spent REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
);

-- ---------------------------------------------------------------- multiplayer

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  host_user_id TEXT NOT NULL,
  playthrough_id TEXT NOT NULL,
  world_id TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'coop',
  status TEXT NOT NULL DEFAULT 'open',
  max_players INTEGER NOT NULL DEFAULT 6,
  settings TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sessions_code ON sessions(code);

CREATE TABLE IF NOT EXISTS session_players (
  session_id TEXT NOT NULL,
  player_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'player',
  is_host INTEGER NOT NULL DEFAULT 0,
  pays INTEGER NOT NULL DEFAULT 0,
  goal TEXT NOT NULL DEFAULT '',
  joined_at TEXT NOT NULL,
  PRIMARY KEY (session_id, player_id)
);

CREATE TABLE IF NOT EXISTS whispers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  playthrough_id TEXT NOT NULL,
  turn INTEGER NOT NULL,
  from_player TEXT NOT NULL,
  to_kind TEXT NOT NULL,
  to_id TEXT NOT NULL,
  text TEXT NOT NULL,
  reply TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_whispers ON whispers(session_id, from_player, id);

-- ------------------------------------------------------------ player worlds

CREATE TABLE IF NOT EXISTS worlds (
  id TEXT PRIMARY KEY,
  owner_user_id TEXT NOT NULL,
  name TEXT NOT NULL,
  tagline TEXT NOT NULL DEFAULT '',
  origin TEXT NOT NULL DEFAULT 'forged',
  source_prompt TEXT NOT NULL DEFAULT '',
  personal_only INTEGER NOT NULL DEFAULT 0,
  visibility TEXT NOT NULL DEFAULT 'private',
  json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_worlds_owner ON worlds(owner_user_id);

-- --------------------------------------------------------------- retention

CREATE TABLE IF NOT EXISTS streaks (
  user_id TEXT PRIMARY KEY,
  current INTEGER NOT NULL DEFAULT 0,
  longest INTEGER NOT NULL DEFAULT 0,
  last_day TEXT NOT NULL DEFAULT '',
  total_days INTEGER NOT NULL DEFAULT 0,
  milestones TEXT NOT NULL DEFAULT '[]'
);

-- ====================================================================== L2
-- Living world: the world-state vector, and the atlas of places.

CREATE TABLE IF NOT EXISTS world_state (
  playthrough_id TEXT PRIMARY KEY,
  turn INTEGER NOT NULL DEFAULT 0,
  day INTEGER NOT NULL DEFAULT 1,
  phase TEXT NOT NULL DEFAULT 'morning',
  weather TEXT NOT NULL DEFAULT 'clear',
  wind INTEGER NOT NULL DEFAULT 1,
  temperature INTEGER NOT NULL DEFAULT 12,
  light INTEGER NOT NULL DEFAULT 3,
  noise INTEGER NOT NULL DEFAULT 2,
  unrest INTEGER NOT NULL DEFAULT 0,
  flags TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS atlas_places (
  playthrough_id TEXT NOT NULL,
  place_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'hidden',
  discovered_turn INTEGER NOT NULL DEFAULT -1,
  visits INTEGER NOT NULL DEFAULT 0,
  last_seen_turn INTEGER NOT NULL DEFAULT -1,
  condition TEXT NOT NULL DEFAULT 'intact',
  note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (playthrough_id, place_id)
);

CREATE TABLE IF NOT EXISTS world_echoes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  turn INTEGER NOT NULL,
  place_id TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'change',
  text TEXT NOT NULL,
  magnitude INTEGER NOT NULL DEFAULT 2,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_echoes ON world_echoes(playthrough_id, id);

-- ====================================================================== L0
-- Narrative graph: explicit nodes/edges so the branch view is real, not derived.

CREATE TABLE IF NOT EXISTS graph_nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  turn INTEGER NOT NULL,
  kind TEXT NOT NULL,
  label TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  place_id TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT '',
  weight INTEGER NOT NULL DEFAULT 2,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_gnodes ON graph_nodes(playthrough_id, id);

CREATE TABLE IF NOT EXISTS graph_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  src INTEGER NOT NULL,
  dst INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT 'then'
);

-- ====================================================================== L4
-- Faction / reputation ledger (indirect reciprocity).

CREATE TABLE IF NOT EXISTS faction_rep (
  playthrough_id TEXT NOT NULL,
  faction_id TEXT NOT NULL,
  player_id TEXT NOT NULL,
  standing REAL NOT NULL DEFAULT 0,
  fear REAL NOT NULL DEFAULT 0,
  known_events INTEGER NOT NULL DEFAULT 0,
  last_turn INTEGER NOT NULL DEFAULT -1,
  PRIMARY KEY (playthrough_id, faction_id, player_id)
);

-- ====================================================================== L5
-- Knowledge, rumor relay, hunts.

CREATE TABLE IF NOT EXISTS knowledge (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  holder_kind TEXT NOT NULL,
  holder_id TEXT NOT NULL,
  fact_key TEXT NOT NULL,
  summary TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  place_id TEXT NOT NULL DEFAULT '',
  turn_learned INTEGER NOT NULL,
  confidence REAL NOT NULL DEFAULT 1.0,
  source TEXT NOT NULL DEFAULT 'witnessed',
  severity INTEGER NOT NULL DEFAULT 2,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_know ON knowledge(playthrough_id, holder_kind, holder_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_know ON knowledge(playthrough_id, holder_kind, holder_id, fact_key);

CREATE TABLE IF NOT EXISTS rumors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  fact_key TEXT NOT NULL,
  carrier TEXT NOT NULL,
  from_place TEXT NOT NULL,
  to_place TEXT NOT NULL,
  depart_turn INTEGER NOT NULL,
  arrive_turn INTEGER NOT NULL,
  distortion REAL NOT NULL DEFAULT 0,
  delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_rumors ON rumors(playthrough_id, delivered, arrive_turn);

CREATE TABLE IF NOT EXISTS hunts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  faction_id TEXT NOT NULL,
  hunter TEXT NOT NULL,
  target_kind TEXT NOT NULL DEFAULT 'player',
  target_id TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  ordered_turn INTEGER NOT NULL,
  from_place TEXT NOT NULL,
  to_place TEXT NOT NULL,
  eta_min INTEGER NOT NULL,
  eta_max INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'enroute',
  severity INTEGER NOT NULL DEFAULT 3
);
CREATE INDEX IF NOT EXISTS ix_hunts ON hunts(playthrough_id, status);

-- ====================================================================== L3/5/6
-- Pre-commit: everyone declares, nothing resolves until the last submit.

CREATE TABLE IF NOT EXISTS commits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  round_key TEXT NOT NULL,
  turn INTEGER NOT NULL,
  player_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'action',
  intent TEXT NOT NULL,
  visibility TEXT NOT NULL DEFAULT 'open',
  payload TEXT NOT NULL DEFAULT '{}',
  locked INTEGER NOT NULL DEFAULT 0,
  revealed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_commits ON commits(playthrough_id, round_key);

-- ====================================================================== L3
-- Betrayal: splits, private knowledge, the conspiracy board.

CREATE TABLE IF NOT EXISTS splits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  player_id TEXT NOT NULL,
  announced TEXT NOT NULL DEFAULT '',
  truth TEXT NOT NULL DEFAULT '',
  destination TEXT NOT NULL DEFAULT '',
  private_turns INTEGER NOT NULL DEFAULT 3,
  turns_taken INTEGER NOT NULL DEFAULT 0,
  started_turn INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'away',
  deception INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- ====================================================================== L6
CREATE TABLE IF NOT EXISTS combats (
  id TEXT PRIMARY KEY,
  playthrough_id TEXT NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  place_id TEXT NOT NULL,
  round INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'declare',
  surprise TEXT NOT NULL DEFAULT '',
  boss TEXT NOT NULL DEFAULT '{}',
  log TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS combatants (
  combat_id TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'npc',
  side TEXT NOT NULL,
  name TEXT NOT NULL,
  hp INTEGER NOT NULL DEFAULT 20,
  max_hp INTEGER NOT NULL DEFAULT 20,
  guard INTEGER NOT NULL DEFAULT 10,
  power INTEGER NOT NULL DEFAULT 4,
  zone TEXT NOT NULL DEFAULT 'centre',
  status TEXT NOT NULL DEFAULT '[]',
  hidden INTEGER NOT NULL DEFAULT 0,
  down INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (combat_id, entity_id)
);

-- ====================================================================== L7
CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY,
  playthrough_id TEXT NOT NULL,
  session_id TEXT NOT NULL DEFAULT '',
  player_id TEXT NOT NULL,
  name TEXT NOT NULL,
  concept TEXT NOT NULL DEFAULT '',
  aspects TEXT NOT NULL DEFAULT '{}',
  anomaly TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  approvals TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS safety (
  playthrough_id TEXT PRIMARY KEY,
  lines TEXT NOT NULL DEFAULT '[]',
  veils TEXT NOT NULL DEFAULT '[]',
  events TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS canon_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  turn INTEGER NOT NULL,
  actor TEXT NOT NULL,
  attempted TEXT NOT NULL,
  verdict TEXT NOT NULL,
  rule TEXT NOT NULL DEFAULT '',
  layer TEXT NOT NULL DEFAULT 'canon',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_canonlog ON canon_log(playthrough_id, id);

CREATE TABLE IF NOT EXISTS prefs (
  user_id TEXT PRIMARY KEY,
  data TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT ''
);

-- --------------------------------------------------------------- payments

CREATE TABLE IF NOT EXISTS payments (
  order_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL DEFAULT 'paypal',
  user_id TEXT NOT NULL,
  playthrough_id TEXT NOT NULL,
  pack_id TEXT NOT NULL,
  mana INTEGER NOT NULL,
  usd REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'created',
  raw TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  captured_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_payments_user ON payments(user_id, created_at);

-- ====================================================== runs & meta-progression
-- A run is one entry into a world: start state, escalating stakes, an end
-- (death / victory / retire). Meta-progression is what survives a run, and is
-- what makes HARDCORE permadeath a loop instead of a punishment.
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  playthrough_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  run_no INTEGER NOT NULL DEFAULT 1,
  world_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  ended_reason TEXT NOT NULL DEFAULT '',
  turns INTEGER NOT NULL DEFAULT 0,
  peak_turn INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_runs_user ON runs(user_id, started_at);

-- Meta-progression is per (user, world): unlocks, remembered lore, standing.
CREATE TABLE IF NOT EXISTS meta_progress (
  user_id TEXT NOT NULL,
  world_id TEXT NOT NULL,
  runs_completed INTEGER NOT NULL DEFAULT 0,
  deepest_turn INTEGER NOT NULL DEFAULT 0,
  unlocks TEXT NOT NULL DEFAULT '[]',
  known_lore TEXT NOT NULL DEFAULT '[]',
  echoes TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (user_id, world_id)
);

-- ============================================================ party lifecycle
-- Invite needs every current player's approval; kick needs every OTHER
-- player's. Both are consensus gates, same shape as card approval.
CREATE TABLE IF NOT EXISTS party_motions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  target_id TEXT NOT NULL DEFAULT '',
  target_name TEXT NOT NULL DEFAULT '',
  proposed_by TEXT NOT NULL,
  needed TEXT NOT NULL DEFAULT '[]',
  approvals TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL,
  resolved_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_motions_session ON party_motions(session_id, status);

-- A kicked player's state is ARCHIVED, never deleted, so a re-invite can
-- restore it. The purge itself is what "cease from existence" means.
CREATE TABLE IF NOT EXISTS party_archive (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  playthrough_id TEXT NOT NULL,
  player_id TEXT NOT NULL,
  cease TEXT NOT NULL DEFAULT 'strict',
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_archive_session ON party_archive(session_id, player_id);

-- ============================================================ canon timeline
-- Ordered arcs for a world. Players may choose their ENTRY POINT rather than
-- always starting at the beginning.
CREATE TABLE IF NOT EXISTS world_arcs (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL,
  ord INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  seeds TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_arcs_world ON world_arcs(world_id, ord);

-- =================================================== trust & safety (takedown)
-- The UGC posture only holds in practice if a takedown route actually exists.
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL DEFAULT '',
  playthrough_id TEXT NOT NULL DEFAULT '',
  reporter TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open',
  resolution TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  resolved_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_reports_status ON reports(status, created_at);

-- ============================================ institutional authority (C2/C3)
-- What an authority holds against a player: the running ledger behind the
-- warrant tier. Standing (in faction_rep) decides the TIER; this records the
-- case itself - how many crimes, what they were, and who is handling it.
CREATE TABLE IF NOT EXISTS bounties (
  playthrough_id TEXT NOT NULL,
  faction_id TEXT NOT NULL,
  player_id TEXT NOT NULL,
  bounty INTEGER NOT NULL DEFAULT 0,
  crimes INTEGER NOT NULL DEFAULT 0,
  officer_id TEXT NOT NULL DEFAULT '',
  log TEXT NOT NULL DEFAULT '[]',
  settled_turn INTEGER NOT NULL DEFAULT -1,
  updated_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (playthrough_id, faction_id, player_id)
);

-- ======================================================= legacy & world events
-- Player-founded organisations. Power in this engine is not an inventory of
-- objects; it is people who will do what you ask. An org is the structure that
-- makes that addressable: you command THROUGH members, and the world attributes
-- the act to the member who carried it out, not to you.
CREATE TABLE IF NOT EXISTS orgs (
  id TEXT PRIMARY KEY,
  playthrough_id TEXT NOT NULL,
  founder TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'cell',
  charter TEXT NOT NULL DEFAULT '',
  seat TEXT NOT NULL DEFAULT '',
  founded_turn INTEGER NOT NULL DEFAULT 0,
  dissolved INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orgs ON orgs(playthrough_id, dissolved);

CREATE TABLE IF NOT EXISTS org_members (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  org_id TEXT NOT NULL,
  member_kind TEXT NOT NULL DEFAULT 'npc',
  member_id TEXT NOT NULL,
  rank TEXT NOT NULL DEFAULT 'member',
  seniority REAL NOT NULL DEFAULT 1,
  orders_carried INTEGER NOT NULL DEFAULT 0,
  orders_refused INTEGER NOT NULL DEFAULT 0,
  joined_turn INTEGER NOT NULL DEFAULT 0,
  UNIQUE (org_id, member_id)
);
CREATE INDEX IF NOT EXISTS ix_org_members ON org_members(playthrough_id, org_id);

-- A named death does not hand the seat to one obvious heir. It opens a
-- CONTEST: several people with a real claim, an unrest window while it is
-- undecided, and a winner decided by what the world actually is.
CREATE TABLE IF NOT EXISTS claimants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playthrough_id TEXT NOT NULL,
  vacuum_key TEXT NOT NULL,
  role TEXT NOT NULL,
  seat TEXT NOT NULL DEFAULT '',
  dead_id TEXT NOT NULL DEFAULT '',
  claimant_id TEXT NOT NULL,
  claim REAL NOT NULL DEFAULT 0,
  basis TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'claiming',
  opened_turn INTEGER NOT NULL DEFAULT 0,
  unrest_until INTEGER NOT NULL DEFAULT 0,
  fact_key TEXT NOT NULL DEFAULT '',
  UNIQUE (playthrough_id, vacuum_key, claimant_id)
);
CREATE INDEX IF NOT EXISTS ix_claimants ON claimants(playthrough_id, vacuum_key);

-- ============================================================== OOC channel
-- The table talking ABOUT the story, kept out of the story. Nothing here
-- enters the timeline, moves a relationship, or is witnessed - so asking
-- "is the gate still barred?" does not become a scene the world reacts to.
CREATE TABLE IF NOT EXISTS ooc_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL DEFAULT '',
  playthrough_id TEXT NOT NULL,
  player_id TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  to_wm INTEGER NOT NULL DEFAULT 0,
  reply TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ooc ON ooc_messages(playthrough_id, id);

-- ===================================================== canon Session Zero
-- What the player answered about their own place in a canon world: entry
-- point on the timeline, where they sit in the power system, what limits
-- them. Kept on the WORLD so re-entering it does not ask again.
CREATE TABLE IF NOT EXISTS session_zero (
  world_id TEXT PRIMARY KEY,
  answers TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT ''
);

-- ============================================================ the Mana wallet
-- Mana belongs to the PERSON, not to one story. A host who buys a pack can
-- spend it in any world and any room they play in - which is what a wallet
-- means, and what players expect. It also fixes a real bug: buying from the
-- threshold, where no playthrough exists yet, used to POST to
-- /playthroughs/null/purchase and 404.
--
-- playthroughs.mana_balance stays as a legacy column and is folded into the
-- owner's wallet once, on first boot after this change, so nobody loses Mana
-- they already had.
CREATE TABLE IF NOT EXISTS wallets (
  user_id TEXT PRIMARY KEY,
  balance INTEGER NOT NULL DEFAULT 0,
  purchased INTEGER NOT NULL DEFAULT 0,
  spent INTEGER NOT NULL DEFAULT 0,
  migrated INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT ''
);

-- ===================================================== accounts (Workstream D)
-- A real account, replacing the client-supplied id that anyone could spoof.
-- The password hash is Argon2id; there is no column that could hold a
-- plaintext password even by accident.
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY,
  email TEXT UNIQUE,
  display_name TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT 'password',
  avatar_url TEXT NOT NULL DEFAULT '',
  bio TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  last_seen TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_accounts_email ON accounts(email);

-- Sessions are server-side rows, not self-describing tokens: revoking one has
-- to actually revoke it, which a stateless JWT cannot do.
CREATE TABLE IF NOT EXISTS auth_sessions (
  token_hash TEXT PRIMARY KEY,
  account_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_used TEXT NOT NULL DEFAULT '',
  user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_auth_sessions_acct ON auth_sessions(account_id);

-- C4: what a town remembers about an ACCOUNT across runs. Keyed to the
-- authenticated id, so clearing browser storage cannot launder a reputation.
-- ============================================== live canon research (bootstrap)
-- What a public wiki told us about a named setting. Cached because canon does
-- not move fast and a player should never wait twice for the same lookup.
CREATE TABLE IF NOT EXISTS research_cache (
  key TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS town_memory (
  account_id TEXT NOT NULL,
  world_id TEXT NOT NULL,
  faction_id TEXT NOT NULL,
  standing REAL NOT NULL DEFAULT 0,
  crimes INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (account_id, world_id, faction_id)
);
"""

# Columns added after the MVP shipped. Applied to existing databases in place.
MIGRATIONS = [
    ("npc_memories", "player_id", "TEXT NOT NULL DEFAULT '*'"),
    ("playthroughs", "session_id", "TEXT NOT NULL DEFAULT ''"),
    ("playthroughs", "world_json", "TEXT NOT NULL DEFAULT ''"),
    ("relationships", "love", "REAL NOT NULL DEFAULT 0"),
    ("relationships", "loyalty", "REAL NOT NULL DEFAULT 0"),
    ("relationships", "respect", "REAL NOT NULL DEFAULT 0"),
    ("relationships", "interactions", "INTEGER NOT NULL DEFAULT 0"),
    ("relationships", "betrayals", "INTEGER NOT NULL DEFAULT 0"),
    ("relationships", "kept_promises", "INTEGER NOT NULL DEFAULT 0"),
    # --- §8 premium is a HOST decision for the whole room, never per-action.
    # Without this any player could fire Deep Prose and drain the host wallet.
    ("sessions", "premium_allowed", "INTEGER NOT NULL DEFAULT 0"),
    # --- world modes (§WORLD MODES): composable, consensus-editable.
    ("playthroughs", "modes", "TEXT NOT NULL DEFAULT '{}'"),
    # --- roguelike run framing + meta-progression (§0).
    ("playthroughs", "run_no", "INTEGER NOT NULL DEFAULT 1"),
    ("playthroughs", "run_state", "TEXT NOT NULL DEFAULT 'active'"),
    # --- canon timeline entry point / AU premise (§PACING/TIMELINE).
    ("playthroughs", "arc_id", "TEXT NOT NULL DEFAULT ''"),
    ("playthroughs", "au_premise", "TEXT NOT NULL DEFAULT ''"),
    # --- uploaded avatar, never generated (§CHARACTER CARD DISPLAY + halal).
    ("cards", "avatar_url", "TEXT NOT NULL DEFAULT ''"),
    ("session_players", "avatar_url", "TEXT NOT NULL DEFAULT ''"),
    # --- the stable identity block (§CANON-CHARACTER FIDELITY).
    ("cards", "identity", "TEXT NOT NULL DEFAULT '{}'"),
    ("cards", "on_death", "TEXT NOT NULL DEFAULT ''"),
    # --- death resolution + party lifecycle state.
    ("session_players", "life_state", "TEXT NOT NULL DEFAULT 'alive'"),
    ("session_players", "resolution", "TEXT NOT NULL DEFAULT ''"),
    # A kicked player is marked, never row-deleted: their seat has to stay
    # addressable so a re-invite can restore the archive against it.
    ("session_players", "left_at", "TEXT NOT NULL DEFAULT ''"),
    # --- legacy layer. A graph node carries the fact it is gated by, so the
    # Chronicle can be witness-gated with a join instead of a guess: a node
    # with no key is the player's own trail, a node with one is only visible
    # to whoever actually learned that fact.
    ("graph_nodes", "fact_key", "TEXT NOT NULL DEFAULT ''"),
    # A fact an NPC holds is turned into a FEELING exactly once. Without this
    # flag the gossip tick would re-apply the same rumour every turn and a
    # single overheard killing would end a friendship by attrition.
    ("knowledge", "applied", "INTEGER NOT NULL DEFAULT 0"),
    # The event that broke it. Moral drift has to be traceable to a specific
    # thing the player did, or it is a mood meter with a story pasted on.
    ("relationships", "cause_node", "INTEGER NOT NULL DEFAULT 0"),
    ("relationships", "cause_turn", "INTEGER NOT NULL DEFAULT -1"),
    # --- the mode tree. `mode` already existed and means something narrower
    # (how the table treats each other); this is what KIND of game it is, and
    # it decides whether the world survives the session at all.
    ("sessions", "session_type", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "seed", "INTEGER NOT NULL DEFAULT 0"),
    # A disposable world that has been settled. Kept as a column rather than a
    # row deletion so the result stays readable after the world is gone.
    ("sessions", "outcome", "TEXT NOT NULL DEFAULT ''"),
    ("sessions", "ended_at", "TEXT NOT NULL DEFAULT ''"),
    ("session_players", "team", "TEXT NOT NULL DEFAULT ''"),
    ("session_players", "seat_role", "TEXT NOT NULL DEFAULT ''"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


_local = threading.local()


def _connect():
    """One connection per thread, reused. The deterministic layers issue dozens
    of small queries per turn; paying connection setup for each one dominated
    the turn cost. WAL plus a per-thread handle keeps the same safety."""
    c = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def _handle():
    c = getattr(_local, "conn", None)
    if c is None:
        c = _local.conn = _connect()
    return c


def close_thread():
    c = getattr(_local, "conn", None)
    if c is not None:
        try:
            c.close()
        finally:
            _local.conn = None


@contextmanager
def conn():
    c = _handle()
    try:
        yield c
        c.commit()
    except Exception:
        try:
            c.rollback()
        except sqlite3.Error:
            close_thread()
        raise


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def rows(sql, args=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def row(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def run(sql, args=()):
    with conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def jload(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else [])
    except (json.JSONDecodeError, TypeError):
        return default if default is not None else []
