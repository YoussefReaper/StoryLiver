"""Environment, model routing, and the price table cost accounting reads from."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("STORYLIVER_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "storyliver.db"

# --- LLM connection ---------------------------------------------------------
# OpenAI is the default provider (an OpenAI key reaches Tier 1 on a $5 spend,
# no separate Organization Verification needed for the calls this app makes -
# see the README "Models" section for why). A model whose id starts with
# "claude-" is routed to the native Anthropic Messages API instead; set any
# STORYLIVER_MODEL_* below to a claude-* id (and add ANTHROPIC_API_KEY) to
# run that role on Claude, per-role, with no other code change.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
API_KEY = ANTHROPIC_API_KEY or OPENAI_API_KEY          # back-compat: whichever is set
BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "").strip() or None

# "auto" -> real calls when a key exists, deterministic offline stub when it doesn't.
# "mock" forces the stub (used by the test suites so CI costs nothing).
LLM_MODE = os.getenv("STORYLIVER_LLM_MODE", "auto").lower()
REQUEST_TIMEOUT = float(os.getenv("STORYLIVER_LLM_TIMEOUT", "60"))
MAX_RETRIES = int(os.getenv("STORYLIVER_LLM_RETRIES", "2"))

# --- Live canon research (World Bootstrap) ----------------------------------
# Bootstrap looks the setting up on public wikis before building. Off in mock
# mode automatically, so the test suites stay offline and free. Wikimedia asks
# for contact details in the User-Agent; set this on a real deployment.
RESEARCH = os.getenv("STORYLIVER_RESEARCH", "on").lower()
RESEARCH_CONTACT = os.getenv("STORYLIVER_RESEARCH_CONTACT",
                             "https://github.com/storyliver")
RESEARCH_TIMEOUT = float(os.getenv("STORYLIVER_RESEARCH_TIMEOUT", "8"))

# --- Durable storage (MongoDB backup/restore) -------------------------------
# Free hosts (Render's free web service, most trial tiers) give the app no
# persistent disk: every spin-down or redeploy hands it an empty filesystem.
# SQLite stays the ONLY database the app reads and writes while running; when
# MONGODB_URI is set, backend/durable.py periodically snapshots it into
# MongoDB and restores that snapshot the moment a fresh container boots with
# no local file. Off by default, and off in the test suite's mock mode even
# if a stray URI is present, so nothing here ever needs a real cluster to run
# offline and free.
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB = os.getenv("MONGODB_DB", "storyliver")
MONGODB_BACKUP_INTERVAL = float(os.getenv("STORYLIVER_BACKUP_INTERVAL", "300"))




def is_claude_model(model: str) -> bool:
    return (model or "").startswith("claude-")


def live_llm() -> bool:
    if LLM_MODE == "mock":
        return False
    if LLM_MODE == "live":
        return True
    return bool(ANTHROPIC_API_KEY or OPENAI_API_KEY)


def key_for(model: str) -> str:
    """The right credential for whichever provider this model routes to."""
    if is_claude_model(model):
        return ANTHROPIC_API_KEY or OPENAI_API_KEY   # tolerate a stray single key
    return OPENAI_API_KEY or ANTHROPIC_API_KEY


# --- Model routing (per-role, overridable by env) ---------------------------
# Split by what the role actually produces, not one flat tier for everything:
#
# World Master and Director emit compact structured JSON (world-state deltas,
# pacing calls) - correctness and instruction-following matter, prose flair
# doesn't, so they run on GPT-5.4 nano: a March-2026 checkpoint, a full
# generation past the gpt-4.1-nano the MVP shipped with, at a fraction of
# Claude Haiku 4.5's price.
#
# NPC minds and the standard Narrator produce the text a player actually
# reads - persona voice, prose rhythm - so they get the mini tier instead of
# nano: still far cheaper than Haiku 4.5 on both input and output, but a
# meaningfully bigger model.
#
# Deep Prose (opt-in, 4 Mana instead of 1) steps up to GPT-5.1: its per-output-
# token price lands exactly on Claude Sonnet 5's ($10/M) while input runs
# ~37% cheaper - the "better model, same price, same-or-better margin" trade
# the pricing math below is built on.
#
# None of these calls stream, so the Organization-Verification gate OpenAI
# puts on the GPT-5 family only blocks (streaming responses on unverified
# orgs, per OpenAI's own developer-community threads on the 2025 rollout) -
# a paid Tier-1 key works out of the box. See README "Models" for sources.
MODELS = {
    "world_master": os.getenv("STORYLIVER_MODEL_WM", "gpt-5.4-nano"),
    "narrator": os.getenv("STORYLIVER_MODEL_NARRATOR", "gpt-5.4-mini"),
    "narrator_premium": os.getenv("STORYLIVER_MODEL_NARRATOR_PREMIUM", "gpt-5.1"),
    "npc": os.getenv("STORYLIVER_MODEL_NPC", "gpt-5.4-mini"),
    "director": os.getenv("STORYLIVER_MODEL_DIRECTOR", "gpt-5.4-nano"),
}

# USD per 1M tokens (input, output). Verified against OpenAI's and Anthropic's
# published rates, Aug 2026. Unknown models fall back to FALLBACK_PRICE so
# accounting never silently reports zero.
PRICE_PER_MTOK = {
    # OpenAI - api.openai.com/v1/chat/completions (default provider)
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.2": (1.75, 14.00),
    "gpt-5.1": (1.25, 10.00),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    # GPT-5.6 (Aug 2026): cheaper still, but rolling out gradually at time of
    # writing - kept priced here so a deployment that has access can opt in
    # per-role via STORYLIVER_MODEL_* without waiting on this file.
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    # Anthropic - api.anthropic.com/v1/messages (opt in per role, see above)
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
}
FALLBACK_PRICE = (1.00, 5.00)          # assume Haiku-tier rather than under-report

# Prompt-cache read/write multipliers on the base input price (Anthropic).
# A cache write costs a 25% premium over a fresh token; a cache read costs 10%
# of one. Applied in llm.py when the API reports cache token usage.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


def price_for(model: str):
    if model in PRICE_PER_MTOK:
        return PRICE_PER_MTOK[model]
    for known, price in PRICE_PER_MTOK.items():          # tolerate dated suffixes
        if model.startswith(known):
            return price
    return FALLBACK_PRICE


# --- Economy ------------------------------------------------------------
# Mana is usage-based. No subscription, no expiry, and crucially no hard
# playtime block: past the free daily allowance you keep playing on Ember.
#
# Pricing is set against MEASURED per-action cost on the current model
# routing (GPT-5.4 nano/mini standard, GPT-5.1 premium), not the old
# nano-tier numbers. See README "Pricing" for the full worked margin - the
# short version: real cost measured across 154 harness turns is
# $0.0020-0.0027/action, comfortably under the old Claude-routing figure this
# pricing was first set against, so these packs hold 60-72% gross margin
# even in the worst case (100% standard-tier usage, full non-micropayment
# PayPal fee) - the honest floor, not the average - for a compute-heavy AI
# product; see the SaaS margin research cited in the README.
MANA_COST = {"standard": 1, "premium": 4}
FREE_DAILY_MANA = int(os.getenv("STORYLIVER_FREE_DAILY_MANA", "40"))
STARTING_MANA = int(os.getenv("STORYLIVER_STARTING_MANA", "40"))
# Hard safety rail: stop spending real money on one user in one day.
DAILY_COST_CAP_USD = float(os.getenv("STORYLIVER_DAILY_COST_CAP_USD", "1.00"))
# One host can bring friends who pay nothing; several payers stack allowances.
MAX_ALLOWANCE_STACK = int(os.getenv("STORYLIVER_MAX_ALLOWANCE_STACK", "5"))
MAX_PLAYERS = int(os.getenv("STORYLIVER_MAX_PLAYERS", "6"))
FREE_FRIENDS_PER_HOST = int(os.getenv("STORYLIVER_FREE_FRIENDS", "5"))

MANA_PACKS = [
    {"id": "spark", "name": "Spark", "mana": 300, "usd": 4.99,
     "note": "A first real session."},
    {"id": "ember", "name": "Ember", "mana": 1000, "usd": 14.99,
     "note": "The best per-action price for a solo player.", "featured": True},
    {"id": "bonfire", "name": "Bonfire", "mana": 3000, "usd": 39.99,
     "note": "A whole campaign, or a room of friends for a month."},
    {"id": "wildfire", "name": "Wildfire", "mana": 6000, "usd": 79.99,
     "note": "Outlasts a year of a competitor's top subscription tier."},
]

# Measured blended cost per action on the current routing (GPT-5.4 nano for
# World Master + Director, GPT-5.4 mini for NPC + standard Narrator): three
# independent 154-turn test suites measured $0.00195-0.00265/action. This
# constant carries roughly a 1.5x safety buffer over the highest of those,
# so pricing is set against a conservative number, not the best case.
# Surfaced in /api/economy so the cost discipline is inspectable rather than
# asserted; usage_summary() flags a story as over budget if it blows past
# this.
TARGET_BLENDED_COST_USD = 0.0040
