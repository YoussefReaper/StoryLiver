"""FastAPI app: REST + WebSocket + the static player. Single process, no build step."""
from __future__ import annotations

import asyncio
import json

from fastapi import (FastAPI, File, HTTPException, Query, Request, UploadFile,
                     WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from typing import Optional

from . import (aftermath, arcs, atlas, auth, authority, awareness, betrayal, budget,
               canon, combat, durable, ladder, legacy, legibility, modetree,
               ooc, research, room, sessionzero, submodes, uploads,
               config, db,
               death, engine, fastforward, identity, llm, mana, memory, modes,
               narrgraph,
               party, payments, persona, precommit, relationships, rt, runs, sessions,
               narrator, sharecard, streaks, trust, voice, world_master, worldforge,
               worldkit, worldstate)
from . import worlds as world_registry

FRONTEND = config.ROOT / "frontend"

app = FastAPI(title="StoryLiver", version="2.0.0", docs_url="/api/docs")

# The player can be served from Cloudflare Pages while the API lives on Render.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in
                   __import__("os").getenv("STORYLIVER_CORS", "*").split(",") if o.strip()],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    # MUST run before db.init(): sqlite3.connect() creates an empty file the
    # instant it is called, so this is the only moment a fresh container can
    # still be told "there is no local database yet, restore one."
    durable.restore_if_needed()
    db.init()


@app.on_event("startup")
async def _start_durable_backup():
    # A SEPARATE async handler, deliberately: asyncio.create_task needs a
    # running event loop, and the sync handler above runs off-loop in a
    # threadpool. Starlette runs startup handlers in registration order, so
    # restore + db.init() above are guaranteed complete before this fires.
    durable.start_background_backup()


@app.on_event("shutdown")
async def _stop_durable_backup():
    # Render sends SIGTERM before killing a spun-down free instance; this is
    # the one chance to flush anything that changed since the last timer tick.
    await durable.stop_background_backup()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class NewPlaythrough(BaseModel):
    user_id: str = Field(min_length=4, max_length=64)
    world_id: str = "emberfall"
    protagonist: str | None = None
    title: str | None = None
    # Which of the six solo modes. Empty is Story, which is what every solo
    # world was before there was anywhere to say otherwise.
    session_type: str = ""


class Action(BaseModel):
    action: str = Field(min_length=1, max_length=600)
    premium: bool = False


class Purchase(BaseModel):
    pack_id: str


class NewSession(BaseModel):
    user_id: str = Field(min_length=4, max_length=64)
    world_id: str = "emberfall"
    mode: str = "coop"
    host_name: str = "Host"
    protagonist: str | None = None
    title: str | None = None
    # Which of the nineteen sub-modes. Empty maps to the nearest match for the
    # room mode, so a client that predates the tree still opens a playable room.
    session_type: str = ""


class JoinSession(BaseModel):
    user_id: str = Field(min_length=4, max_length=64)
    code: str = Field(min_length=4, max_length=12)
    name: str = Field(min_length=1, max_length=40)
    role: str = "player"
    goal: str = ""


class Whisper(BaseModel):
    player_id: str
    target_kind: str = "npc"
    target_id: str
    text: str = Field(min_length=1, max_length=600)


class Contest(BaseModel):
    challenger_id: str
    defender_id: str
    challenger_action: str = Field(min_length=1, max_length=400)
    defender_action: str = Field(min_length=1, max_length=400)


class Bootstrap(BaseModel):
    user_id: str = Field(min_length=4, max_length=64)
    setting: str = Field(min_length=2, max_length=160)
    tone: str = Field(default="", max_length=160)
    save: bool = True
    # "original" builds from imagination, "canon" reads the real setting and
    # continues it, "auto" looks first and decides. The player's choice.
    mode: str = Field(default="auto", pattern="^(auto|original|canon)$")
    # How big a world to build. A single model call can only carry so much
    # before it starts truncating, so anything past "town" is generated in
    # several passes and stitched - see worldforge.SCALES.
    scale: str = Field(default="town", pattern="^(town|city|region|world)$")
    # Session Zero: entry point on the timeline, where the player sits in the
    # world's power system, and what limits them.
    answers: dict = Field(default_factory=dict)


class SaveWorld(BaseModel):
    user_id: str = Field(min_length=4, max_length=64)
    world: dict
    world_id: str | None = None
    visibility: str = "private"


class Speak(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    voice: str | None = None



class Declare(BaseModel):
    player_id: str
    round_key: str
    intent: str = Field(min_length=1, max_length=600)
    kind: str = "action"
    visibility: str = "open"
    announced: str = ""


class Depart(BaseModel):
    player_id: str
    announced: str = Field(min_length=1, max_length=400)
    truth: str = Field(default="", max_length=400)
    destination: str = ""
    private_turns: int = 3


class ReturnHome(BaseModel):
    player_id: str
    account: str = Field(min_length=1, max_length=400)
    truth: str = Field(min_length=1, max_length=600)


class CombatStart(BaseModel):
    place_id: str = ""
    enemies: list[str] = []
    allies: list[str] = []
    boss: dict | None = None
    surprise: str = ""


class CombatDeclare(BaseModel):
    player_id: str
    move: str
    target: str | None = None
    zone: str | None = None


class SafetyUpdate(BaseModel):
    lines: list[str] | None = None
    veils: list[str] | None = None


class XCard(BaseModel):
    player_id: str
    note: str = ""


class CardDraft(BaseModel):
    user_id: str = Field(min_length=4, max_length=64)
    player_id: str = "user"
    name: str = ""
    concept: str = ""
    aspects: dict = {}
    anomaly: str = ""
    autofill: bool = False
    card_id: str | None = None


class CardVote(BaseModel):
    voter: str
    ok: bool = True
    note: str = ""


class Prefs(BaseModel):
    user_id: str = Field(min_length=4, max_length=64)
    data: dict


def _require_paying_account(request: Request, user_id: str) -> str:
    """Real money needs a real owner.

    A guest can play the entire game - that low-friction entry is a genuine
    advantage over competitors and forcing a login at the door would throw it
    away. What a guest cannot do is BUY, because a purchase has to attach to
    something that survives a cleared browser and can be matched to a PayPal
    buyer if anything is ever disputed."""
    account_id = auth.session_account(request.cookies.get(auth.COOKIE, ""))
    if not account_id:
        raise HTTPException(401, "sign in to buy Mana — a purchase needs an account "
                                 "so it survives this browser")
    if user_id and user_id != account_id:
        raise HTTPException(403, "that story belongs to a different account")
    return account_id


def _require_account(request: Request) -> str:
    """The authenticated account behind this request, or 401.

    Deliberately cookie-only: a client-supplied user_id is exactly the
    spoofable thing accounts exist to replace, so it is not accepted here."""
    account_id = auth.session_account(request.cookies.get(auth.COOKIE, ""))
    if not account_id:
        raise HTTPException(401, "sign in to do that")
    return account_id


def _account_id_or_none(request: Request):
    return auth.session_account(request.cookies.get(auth.COOKIE, ""))


def _account_mana(account_id: str) -> dict:
    """Mana across every story this account owns."""
    row = db.row("SELECT COALESCE(SUM(mana_balance),0) AS bal,"
                 " COALESCE(SUM(mana_used),0) AS used"
                 " FROM playthroughs WHERE user_id=?", (account_id,))
    return {"balance": row["bal"] if row else 0, "used": row["used"] if row else 0}


def _own(pt_id, user_id):
    pt = db.row("SELECT * FROM playthroughs WHERE id=?", (pt_id,))
    if not pt:
        raise HTTPException(404, "playthrough not found")
    if user_id and pt["user_id"] != user_id:
        if not db.row("SELECT 1 FROM session_players WHERE session_id=? AND user_id=?",
                      (pt["session_id"], user_id)):
            raise HTTPException(403, "not your playthrough")
    return pt


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"ok": True, "version": app.version,
            "llm": "live" if config.live_llm() else "offline-stub",
            "models": config.MODELS, "realtime": rt.health(),
            "voice": voice.available(), "payments": payments.status(),
            "durable": {"enabled": durable.enabled()},
            "starters": [w.id for w in world_registry.starters()]}


@app.get("/api/economy")
def economy():
    return mana.economics()


@app.get("/api/ethics")
def ethics():
    """The moat, stated where a user can read it and a competitor would have to
    change their business model to copy it."""
    return {
        "promises": [
            {"id": "no_training",
             "title": "We never train on your stories",
             "body": "Your play is sent to a model to generate the next passage and nothing else. "
                     "It is not collected into a training set, not sold, and not used to build a "
                     "personality profile of you."},
            {"id": "export",
             "title": "Export everything, any time",
             "body": "One button gives you the full JSON: every timeline event, every relationship "
                     "number, every private memory an NPC holds. No account needed to take it with you."},
            {"id": "no_guilt",
             "title": "Nothing here guilts you into staying",
             "body": "No character messages you to say they miss you. No streak-loss notification. "
                     "No timed reward you lose by leaving. Your longest streak is kept forever."},
            {"id": "no_lock",
             "title": "You are never locked out",
             "body": "Out of Mana means a leaner narrator, not a closed door. No subscription, no "
                     "expiry, no playtime cap, and memory is never a paid feature."},
            {"id": "not_a_person",
             "title": "The characters are characters",
             "body": "Every NPC is fiction running on a model, and the interface says so. Nothing here "
                     "claims to be your friend, your therapist, or a person who needs you."},
            {"id": "copyright",
             "title": "Worlds built from someone else's fiction stay private",
             "body": "A world you bootstrap from an existing story is an inspired-by personal world. "
                     "It is marked personal, forced to private, can never be published or shared to a "
                     "public listing, and is not affiliated with or endorsed by any rights holder. "
                     "Worlds meant to be shared must be original."},
        ],
        "data_retention": "Everything lives in your own deployment's SQLite file. Delete a story and "
                          "it is gone, including every NPC memory attached to it.",
    }


@app.get("/api/worlds")
def list_worlds(user_id: str = Query(default="")):
    out = [{**w.summary(), "kind": "starter"} for w in world_registry.starters()]
    if user_id:
        out += [{"id": r["id"], "name": r["name"], "tagline": r["tagline"],
                 "origin": r["origin"], "personal_only": r["personal_only"],
                 "visibility": r["visibility"], "updated_at": r["updated_at"], "kind": "mine"}
                for r in worldforge.listing(user_id)]
    return out


# ---------------------------------------------------------------------------
# Solo play
# ---------------------------------------------------------------------------

@app.get("/api/playthroughs")
def list_playthroughs(user_id: str = Query(min_length=4)):
    engine.ensure_user(user_id)
    rows = db.rows(
        "SELECT id,title,world_id,world_json,protagonist,current_turn,current_location,mana_balance,"
        "session_id,updated_at,created_at FROM playthroughs WHERE user_id=? ORDER BY updated_at DESC",
        (user_id,))
    for r in rows:
        try:
            world = world_registry.resolve(r["world_id"], r["world_json"] or None)
            r["location_name"] = world.loc_name(r["current_location"])
            r["day"] = world.day_for(r["current_turn"])
            r["world_name"] = world.name
        except (KeyError, worldkit.WorldError):
            r["location_name"], r["day"], r["world_name"] = "?", 1, r["world_id"]
        r.pop("world_json", None)
    return {"playthroughs": rows, "mana": mana.status(user_id),
            "streak": streaks.status(user_id),
            "sessions": [sessions.public(sessions.get(s["id"])) for s in sessions.for_user(user_id)]}


@app.post("/api/playthroughs")
def new_playthrough(body: NewPlaythrough):
    if not world_registry.exists(body.world_id):
        raise HTTPException(400, "unknown world")
    try:
        pt_id = engine.create_playthrough(body.user_id, body.world_id, body.protagonist,
                                          body.title, session_type=body.session_type)
    except modetree.ModeError as e:
        raise HTTPException(400, str(e))
    return {"id": pt_id, "mode": modetree.public(engine.mode_of(pt_id)),
            "state": engine.snapshot(pt_id), "feed": engine.feed(pt_id)}


@app.get("/api/playthroughs/{pt_id}")
def get_playthrough(pt_id: str, user_id: str = Query(default=""), player: str = Query(default=memory.SOLO)):
    _own(pt_id, user_id)
    return {"state": engine.snapshot(pt_id, player), "feed": engine.feed(pt_id)}


@app.delete("/api/playthroughs/{pt_id}")
def delete_playthrough(pt_id: str, user_id: str = Query(min_length=4)):
    pt = _own(pt_id, user_id)
    if pt["user_id"] != user_id:
        raise HTTPException(403, "only the host can delete a story")
    for t in ("timeline_events", "relationships", "npc_state", "npc_player", "npc_memories",
              "narrative", "whispers"):
        db.run(f"DELETE FROM {t} WHERE playthrough_id=?", (pt_id,))
    db.run("DELETE FROM sessions WHERE playthrough_id=?", (pt_id,))
    db.run("DELETE FROM playthroughs WHERE id=?", (pt_id,))
    rt.invalidate_playthrough(pt_id)
    return {"deleted": pt_id}


@app.post("/api/playthroughs/{pt_id}/action")
def act(pt_id: str, body: Action, user_id: str = Query(default=""),
        player: str = Query(default=memory.SOLO)):
    _own(pt_id, user_id)
    try:
        result = engine.take_turn(pt_id, body.action, premium=body.premium, player=player)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _broadcast(pt_id, result)
    return result


@app.get("/api/playthroughs/{pt_id}/timeline")
def timeline(pt_id: str, user_id: str = Query(default="")):
    _own(pt_id, user_id)
    return {"events": memory.timeline(pt_id, 1000)}


@app.get("/api/playthroughs/{pt_id}/npc/{npc_id}")
def npc_detail(pt_id: str, npc_id: str, user_id: str = Query(default=""),
               player: str = Query(default=memory.SOLO)):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    if npc_id not in world.by_id:
        raise HTTPException(404, "no such character")
    npc = world.by_id[npc_id]
    st = memory.npc_state(pt_id, npc_id) or {}
    ps = memory.npc_player_state(pt_id, npc_id, player)
    mems = db.rows(
        "SELECT turn,kind,text,importance,player_id FROM npc_memories WHERE playthrough_id=?"
        " AND npc_id=? AND player_id IN (?,?) ORDER BY id DESC LIMIT 60",
        (pt_id, npc_id, player, memory.SHARED))
    r = memory.rel_to(pt_id, npc_id, player) or {}
    outgoing = db.rows("SELECT * FROM relationships WHERE playthrough_id=? AND src=?", (pt_id, npc_id))
    others = db.rows(
        "SELECT player_id, COUNT(*) n FROM npc_memories WHERE playthrough_id=? AND npc_id=?"
        " AND player_id NOT IN (?,?) GROUP BY player_id", (pt_id, npc_id, player, memory.SHARED))
    return {
        "id": npc_id, "name": npc["name"], "role": npc["role"], "anchors": npc["anchors"],
        "anchor_block": memory.anchor_block(world, npc_id),
        "alive": bool(st.get("alive", 1)),
        "location": st.get("location"), "plan": db.jload(ps["plan"], []),
        "reflections": db.jload(ps["reflections"], []),
        "memories": mems,
        "relationship_to_user": {k: r.get(k, 0) for k in memory.REL_KEYS},
        "opinions": [{"of": o["dst"], "name": world.npc_name(o["dst"]),
                      **{k: o[k] for k in memory.REL_KEYS}} for o in outgoing
                     if o["dst"] in world.by_id],
        "remembers_others": [{"player_id": o["player_id"], "memories": o["n"]} for o in others],
    }


@app.get("/api/playthroughs/{pt_id}/usage")
def usage(pt_id: str, user_id: str = Query(default="")):
    _own(pt_id, user_id)
    return engine.usage_summary(pt_id)


@app.get("/api/playthroughs/{pt_id}/export")
def export(pt_id: str, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    payload = json.dumps(engine.export(pt_id), indent=2, default=str)
    name = f"storyliver-{pt['world_id']}-{pt_id}.json"
    return Response(payload, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/api/playthroughs/{pt_id}/purchase")
def purchase(pt_id: str, body: Purchase, user_id: str = Query(default="")):
    """Dev-only instant grant. Live once PAYPAL_CLIENT_ID/SECRET are set - use
    /paypal/create-order + /paypal/capture instead; this endpoint stays
    available only so a local `STORYLIVER_LLM_MODE=mock` install still has a
    working Mana top-up with nothing configured."""
    _own(pt_id, user_id)
    if payments.configured():
        raise HTTPException(409, "real payments are configured - use /purchase/paypal/create-order")
    pack = next((p for p in config.MANA_PACKS if p["id"] == body.pack_id), None)
    if not pack:
        raise HTTPException(400, "unknown pack")
    mana.grant(pt_id, pack["mana"])
    return {"granted": pack["mana"], "pack": pack, "state": engine.snapshot(pt_id), "stubbed": True}


@app.post("/api/playthroughs/{pt_id}/purchase/paypal/create-order")
def paypal_create_order(pt_id: str, body: Purchase, request: Request,
                        user_id: str = Query(min_length=4)):
    """The browser's PayPal button calls this to get an order id, then opens
    PayPal's own approval popup for it. We never see card details here."""
    user_id = _require_paying_account(request, user_id)
    _own(pt_id, user_id)
    if not payments.configured():
        raise HTTPException(503, "PayPal is not configured on this deployment")
    try:
        out = payments.create_order(user_id=user_id, playthrough_id=pt_id, pack_id=body.pack_id)
    except payments.PaymentError as e:
        raise HTTPException(400, str(e))
    return out


@app.post("/api/playthroughs/{pt_id}/purchase/paypal/capture")
def paypal_capture(pt_id: str, request: Request, order_id: str = Query(min_length=1),
                   user_id: str = Query(min_length=4)):
    """Called after the player approves in the PayPal popup. Grants Mana only
    once PayPal's own capture response confirms COMPLETED and the amount
    matches - never on the client's say-so."""
    user_id = _require_paying_account(request, user_id)
    _own(pt_id, user_id)
    if not payments.configured():
        raise HTTPException(503, "PayPal is not configured on this deployment")
    try:
        result = payments.capture_order(order_id, expected_user_id=user_id)
    except payments.PaymentError as e:
        raise HTTPException(402, str(e))
    if result["playthrough_id"] != pt_id:
        raise HTTPException(409, "this order was opened for a different story")
    if not result["already_captured"]:
        mana.grant(pt_id, result["mana"])
    return {"granted": result["mana"], "pack_id": result["pack_id"],
            "already_captured": result["already_captured"], "state": engine.snapshot(pt_id)}


@app.get("/api/payments/status")
def payments_status():
    return payments.status()


@app.get("/api/payments/history")
def payments_history(user_id: str = Query(min_length=4)):
    return {"payments": payments.history(user_id)}


# ---------------------------------------------------------------------------
# Retention + sharing
# ---------------------------------------------------------------------------

@app.get("/api/streak")
def streak(user_id: str = Query(min_length=4)):
    return streaks.status(user_id)


@app.get("/api/playthroughs/{pt_id}/card.svg")
def card_svg(pt_id: str, user_id: str = Query(default=""), entry_id: int | None = None,
             player: str = Query(default=memory.SOLO), download: bool = False):
    _own(pt_id, user_id)
    svg = sharecard.svg(pt_id, entry_id=entry_id, player=player)
    headers = {"Cache-Control": "no-store"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="storyliver-{pt_id}.svg"'
    return Response(svg, media_type="image/svg+xml", headers=headers)


@app.get("/api/playthroughs/{pt_id}/card")
def card_data(pt_id: str, user_id: str = Query(default=""), entry_id: int | None = None,
              player: str = Query(default=memory.SOLO)):
    _own(pt_id, user_id)
    return {"data": sharecard.data_for(pt_id, entry_id=entry_id, player=player),
            "text": sharecard.text_recap(pt_id, entry_id=entry_id, player=player)}


@app.get("/api/voice")
def voice_info():
    return voice.available()


@app.post("/api/playthroughs/{pt_id}/speak")
def speak(pt_id: str, body: Speak, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    info = voice.available()
    if not info["studio"]:
        raise HTTPException(400, "studio voice is not configured; the player reads aloud locally for free")
    mode, note = mana.preview(pt["user_id"], pt, premium=False)
    if mode == mana.RAIL:
        raise HTTPException(402, note)
    try:
        audio = voice.speak(body.text, user_id=pt["user_id"], playthrough_id=pt_id, voice=body.voice)
    except Exception as e:
        raise HTTPException(502, str(e))
    for _ in range(voice.TTS_MANA):
        mana.commit(pt["user_id"], pt)
    return Response(audio, media_type=voice.media_type(), headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# World Forge
# ---------------------------------------------------------------------------

@app.get("/api/forge/blank")
def forge_blank(name: str = Query(default="A new world")):
    return {"world": worldforge.blank(name)}


@app.post("/api/forge/bootstrap")
def forge_bootstrap(body: Bootstrap):
    engine.ensure_user(body.user_id)
    try:
        world = worldforge.bootstrap(body.setting, user_id=body.user_id,
                                tone=body.tone, mode=body.mode, scale=body.scale,
                                answers=body.answers)
    except (ValueError, worldkit.WorldError) as e:
        raise HTTPException(400, str(e))
    except llm.LLMError as e:
        # LLMError is a RuntimeError, so it used to escape as a bare 500 with
        # nothing the player could act on. Surface what actually went wrong -
        # a bad key, a rate limit and a timeout need different responses.
        raise HTTPException(502, f"the model could not build that world: {e}")
    payload = {"world": world, "personal_only": bool(world.get("personal_only")),
               "notice": None}
    if world.get("personal_only"):
        payload["notice"] = (
            f"“{body.setting}” names existing copyrighted fiction, so this is an inspired-by "
            "personal world: original characters and places in that spirit, built for your own "
            "private play. It is kept private, can never be published or shared to a public "
            "listing, and is not affiliated with or endorsed by any rights holder. Worlds you "
            "want to share publicly need to be original."
        )
    if body.save:
        saved = worldforge.save(body.user_id, world)
        payload["saved"] = saved
    return payload


@app.get("/api/forge/worlds")
def forge_list(user_id: str = Query(min_length=4)):
    return {"worlds": worldforge.listing(user_id)}


@app.get("/api/forge/worlds/{world_id}")
def forge_get(world_id: str, user_id: str = Query(default="")):
    try:
        return worldforge.get(world_id, user_id or None)
    except KeyError:
        raise HTTPException(404, "no such world")
    except PermissionError as e:
        raise HTTPException(403, str(e))


@app.post("/api/forge/worlds")
def forge_save(body: SaveWorld):
    engine.ensure_user(body.user_id)
    try:
        return worldforge.save(body.user_id, body.world, world_id=body.world_id,
                               visibility=body.visibility)
    except worldkit.WorldError as e:
        raise HTTPException(400, str(e))
    except KeyError:
        raise HTTPException(404, "no such world")


@app.delete("/api/forge/worlds/{world_id}")
def forge_delete(world_id: str, user_id: str = Query(min_length=4)):
    try:
        worldforge.delete(world_id, user_id)
    except KeyError:
        raise HTTPException(404, "no such world")
    return {"deleted": world_id}


@app.get("/api/forge/worlds/{world_id}/export")
def forge_export(world_id: str, user_id: str = Query(default="")):
    try:
        data = worldforge.get(world_id, user_id or None)
    except KeyError:
        raise HTTPException(404, "no such world")
    except PermissionError as e:
        raise HTTPException(403, str(e))
    payload = json.dumps(data["world"], indent=2)
    return Response(payload, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="world-{world_id}.json"'})


# ---------------------------------------------------------------------------
# Rooms
# ---------------------------------------------------------------------------

@app.post("/api/sessions")
def create_session(body: NewSession):
    if not world_registry.exists(body.world_id):
        raise HTTPException(400, "unknown world")
    try:
        s = sessions.create(body.user_id, world_id=body.world_id, mode=body.mode,
                            host_name=body.host_name, protagonist=body.protagonist,
                            title=body.title, session_type=body.session_type)
    except (ValueError, modetree.ModeError) as e:
        raise HTTPException(400, str(e))
    return {"session": sessions.public(s), "player_id": "host",
            "state": engine.snapshot(s["playthrough_id"], "host"),
            "feed": engine.feed(s["playthrough_id"])}


@app.post("/api/sessions/join")
def join_session(body: JoinSession):
    s = sessions.by_code(body.code)
    if not s:
        raise HTTPException(404, "no room with that code")
    try:
        me = sessions.join(s["id"], body.user_id, body.name, role=body.role, goal=body.goal)
    except ValueError as e:
        raise HTTPException(400, str(e))
    full = sessions.get(s["id"])
    return {"session": sessions.public(full), "player_id": me["player_id"], "role": me["role"],
            "state": engine.snapshot(s["playthrough_id"], me["player_id"]),
            "feed": engine.feed(s["playthrough_id"])}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, player: str = Query(default="")):
    try:
        s = sessions.get(session_id)
    except KeyError:
        raise HTTPException(404, "no such session")
    return {"session": sessions.public(s),
            "state": engine.snapshot(s["playthrough_id"], player or memory.SOLO),
            "feed": engine.feed(s["playthrough_id"])}


@app.post("/api/sessions/{session_id}/contribute")
def session_contribute(session_id: str, player: str = Query(min_length=1), pays: bool = True):
    return dict(sessions.contribute(session_id, player, pays))


@app.post("/api/sessions/{session_id}/goal")
def session_goal(session_id: str, player: str = Query(min_length=1), goal: str = Query(default="")):
    return dict(sessions.set_goal(session_id, player, goal))


@app.post("/api/sessions/{session_id}/close")
def session_close(session_id: str, user_id: str = Query(min_length=4)):
    s = sessions.get(session_id)
    if s["host_user_id"] != user_id:
        raise HTTPException(403, "only the host can close the room")
    sessions.close(session_id)
    return {"closed": session_id}


@app.post("/api/sessions/{session_id}/whisper")
def session_whisper(session_id: str, body: Whisper):
    s = sessions.get(session_id)
    try:
        out = engine.whisper(s["playthrough_id"], player=body.player_id,
                             target_kind=body.target_kind, target_id=body.target_id,
                             text=body.text,
                             actor_name=(sessions.player_row(session_id, body.player_id) or {}).get("name"),
                             session_id=session_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.target_kind == "player":
        _publish(session_id, {"type": "whisper", "to": body.target_id,
                              "from": body.player_id, "payload": out})
    return out


@app.get("/api/sessions/{session_id}/whispers")
def session_whispers(session_id: str, player: str = Query(min_length=1)):
    return {"whispers": sessions.whispers_for(session_id, player)}


@app.post("/api/sessions/{session_id}/contest")
def session_contest(session_id: str, body: Contest):
    s = sessions.get(session_id)
    if s["mode"] != "chaos":
        raise HTTPException(400, "contested actions need a chaos room")
    ch = sessions.player_row(session_id, body.challenger_id)
    de = sessions.player_row(session_id, body.defender_id)
    if not ch or not de:
        raise HTTPException(404, "player not in this room")
    result = engine.contest(
        s["playthrough_id"],
        {"player_id": ch["player_id"], "name": ch["name"], "action": body.challenger_action},
        {"player_id": de["player_id"], "name": de["name"], "action": body.defender_action},
        session_id=session_id)
    _broadcast(s["playthrough_id"], result, session_id=session_id)
    return result


# ---------------------------------------------------------------------------
# Layer 0 - the visual workspace, in one round trip
# ---------------------------------------------------------------------------

@app.get("/api/playthroughs/{pt_id}/workspace")
def workspace(pt_id: str, user_id: str = Query(default=""),
              player: str = Query(default=memory.SOLO)):
    _own(pt_id, user_id)
    return engine.workspace(pt_id, player)


@app.get("/api/playthroughs/{pt_id}/atlas")
def atlas_view(pt_id: str, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    return atlas.view(pt_id, world, here=pt["current_location"], turn=pt["current_turn"])


@app.get("/api/playthroughs/{pt_id}/graph")
def graph_view(pt_id: str, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    return narrgraph.view(pt_id, engine.world_for(pt), pt["current_turn"])


@app.get("/api/playthroughs/{pt_id}/echoes")
def echoes(pt_id: str, user_id: str = Query(default="")):
    _own(pt_id, user_id)
    return {"echoes": atlas.echoes(pt_id, 80)}


@app.get("/api/prefs")
def get_prefs(user_id: str = Query(min_length=4)):
    row = db.row("SELECT data FROM prefs WHERE user_id=?", (user_id,))
    return db.jload(row["data"], {}) if row else {}


@app.post("/api/prefs")
def set_prefs(body: Prefs):
    db.run("INSERT INTO prefs (user_id,data,updated_at) VALUES (?,?,?)"
           " ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
           (body.user_id, json.dumps(body.data), db.now()))
    return body.data


@app.get("/api/budget")
def budget_table():
    return budget.table()


# ---------------------------------------------------------------------------
# Layer 5 - knowledge, stealth
# ---------------------------------------------------------------------------

@app.get("/api/playthroughs/{pt_id}/knowledge")
def knowledge(pt_id: str, user_id: str = Query(default=""),
              player: str = Query(default=memory.SOLO)):
    pt = _own(pt_id, user_id)
    return awareness.public_state(pt_id, engine.world_for(pt), player, pt["current_turn"])


@app.get("/api/playthroughs/{pt_id}/stealth")
def stealth(pt_id: str, user_id: str = Query(default=""),
            player: str = Query(default=memory.SOLO), noise: int = 1):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    present = memory.npcs_at(pt_id, world, pt["current_location"], pt["current_turn"])
    return awareness.stealth_check(
        pt_id, world, place_id=pt["current_location"], turn=pt["current_turn"],
        actor=player, watchers=present, intent_noise=max(0, min(5, noise)))


# ---------------------------------------------------------------------------
# Layer 3 - pre-commit and betrayal
# ---------------------------------------------------------------------------

@app.post("/api/playthroughs/{pt_id}/declare")
def declare(pt_id: str, body: Declare, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    out = precommit.declare(pt_id, round_key=body.round_key, turn=pt["current_turn"],
                            player_id=body.player_id, intent=body.intent, kind=body.kind,
                            visibility=body.visibility, announced=body.announced,
                            session_id=pt["session_id"])
    if not out.get("ok"):
        raise HTTPException(409, out.get("reason", "cannot declare"))
    return out


@app.get("/api/playthroughs/{pt_id}/board")
def commit_board(pt_id: str, round_key: str, player: str = Query(default=memory.SOLO),
                 user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    party, _ = engine._party_context(pt["session_id"])
    expected = [p["player_id"] for p in party] + [player]
    return precommit.board(pt_id, round_key, player, sorted(set(expected)))


@app.post("/api/playthroughs/{pt_id}/depart")
def depart(pt_id: str, body: Depart, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    out = betrayal.depart(pt_id, player_id=body.player_id, announced=body.announced,
                          truth=body.truth or body.announced, destination=body.destination,
                          turn=pt["current_turn"], private_turns=max(1, min(8, body.private_turns)),
                          session_id=pt["session_id"])
    if not out.get("ok"):
        raise HTTPException(409, out["reason"])
    _publish(pt["session_id"], {"type": "party", "event": "departed",
                                "player_id": body.player_id, "said": body.announced})
    return out


@app.post("/api/playthroughs/{pt_id}/return")
def return_home(pt_id: str, body: ReturnHome, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    split = betrayal.active(pt_id, body.player_id)
    if not split:
        raise HTTPException(409, "you are not away")
    return betrayal.stage_reveal(pt_id, split_id=split["id"], player_id=body.player_id,
                                 turn=pt["current_turn"], account=body.account,
                                 truth=body.truth, session_id=pt["session_id"])


@app.post("/api/playthroughs/{pt_id}/reveal")
def reveal(pt_id: str, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    present = memory.npcs_at(pt_id, world, pt["current_location"], pt["current_turn"])
    party, _ = engine._party_context(pt["session_id"])
    out = betrayal.resolve_reveal(pt_id, world, pt["current_turn"],
                                  present_npcs=present, party=party)
    for entry in out["revealed"]:
        engine._render(pt_id, pt["current_turn"], "reveal",
                       entry["actual"], actor=entry["player_id"], meta=entry)
    _publish(pt["session_id"], {"type": "reveal", "payload": out})
    return out


@app.get("/api/playthroughs/{pt_id}/conspiracy")
def conspiracy(pt_id: str, player: str = Query(default=memory.SOLO),
               user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    party, _ = engine._party_context(pt["session_id"])
    return betrayal.board(pt_id, engine.world_for(pt), player, pt["current_turn"], party)


# ---------------------------------------------------------------------------
# Layer 6 - combat
# ---------------------------------------------------------------------------

@app.post("/api/playthroughs/{pt_id}/combat")
def combat_start(pt_id: str, body: CombatStart, user_id: str = Query(default=""),
                 player: str = Query(default=memory.SOLO)):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    place = body.place_id or pt["current_location"]
    party, _ = engine._party_context(pt["session_id"])
    side_a = [{"id": player, "kind": "player", "name": "You", "hp": 26, "power": 5, "guard": 11,
               "zone": "front_a"}]
    for p in party:
        side_a.append({"id": p["player_id"], "kind": "player", "name": p["name"],
                       "hp": 26, "power": 5, "guard": 11, "zone": "front_a"})
    for ally in body.allies:
        if ally in world.by_id:
            side_a.append({"id": ally, "kind": "npc", "name": world.npc_name(ally),
                           "hp": 20, "power": 4, "guard": 10, "zone": "back_a"})
    enemies = body.enemies or memory.npcs_at(pt_id, world, place, pt["current_turn"])[:3]
    side_b = [{"id": e, "kind": "npc", "name": world.npc_name(e) if e in world.by_id else e,
               "hp": 22, "power": 4, "guard": 10, "zone": "front_b"} for e in enemies]
    if not side_b:
        raise HTTPException(400, "nobody here to fight")
    boss = combat.build_boss(world, body.boss) if body.boss else None
    c = combat.start(pt_id, world, place_id=place, sides={"a": side_a, "b": side_b},
                     session_id=pt["session_id"], boss=boss, surprise=body.surprise)
    _publish(pt["session_id"], {"type": "combat", "event": "start", "combat_id": c["id"]})
    return combat.view(pt_id, c["id"], player)


@app.get("/api/combat/{combat_id}")
def combat_view(combat_id: str, player: str = Query(default=memory.SOLO)):
    c = combat.get(combat_id)
    if not c:
        raise HTTPException(404, "no such fight")
    return combat.view(c["playthrough_id"], combat_id, player)


@app.post("/api/combat/{combat_id}/declare")
def combat_declare(combat_id: str, body: CombatDeclare):
    c = combat.get(combat_id)
    if not c:
        raise HTTPException(404, "no such fight")
    out = combat.declare(c["playthrough_id"], combat_id, body.player_id, body.move,
                         body.target, body.zone, session_id=c["session_id"])
    if not out.get("ok"):
        raise HTTPException(409, out.get("reason", "cannot declare"))
    _publish(c["session_id"], {"type": "combat", "event": "declared",
                               "combat_id": combat_id, "player_id": body.player_id})
    return combat.view(c["playthrough_id"], combat_id, body.player_id)


@app.post("/api/combat/{combat_id}/resolve")
def combat_resolve(combat_id: str, player: str = Query(default=memory.SOLO)):
    c = combat.get(combat_id)
    if not c:
        raise HTTPException(404, "no such fight")
    pt = db.row("SELECT * FROM playthroughs WHERE id=?", (c["playthrough_id"],))
    world = engine.world_for(pt)
    out = combat.resolve(c["playthrough_id"], combat_id, world=world)
    engine._render(c["playthrough_id"], pt["current_turn"], "combat",
                   _combat_prose(out), actor="world",
                   meta={"combat_id": combat_id, "round": out["round"],
                         "steps": out["steps"], "status": out["status"],
                         "winner": out.get("winner")})

    # A fight that ends has to land in the world: the fallen actually die, the
    # timeline records it, whoever could see it learns it, and a player who
    # went down is offered a way to keep playing. Without this the combat
    # subsystem is sealed off and a killing has no consequence at all.
    if out["status"] == "over":
        after = aftermath.after_combat(
            c["playthrough_id"], world, combat.get(combat_id), out.get("winner"),
            session_id=c["session_id"], place_id=pt["current_location"],
            turn=pt["current_turn"])
        out["aftermath"] = after
        engine._render(c["playthrough_id"], pt["current_turn"], "aftermath",
                       after["summary"], actor="world",
                       meta={"deaths": [d["who"] for d in after["deaths"]],
                             "knockouts": [k["who"] for k in after["knockouts"]],
                             "hunts": bool(after.get("hunts")),
                             "player_down": bool(after.get("player_down"))})
        _publish(c["session_id"], {"type": "aftermath", "combat_id": combat_id,
                                   "summary": after["summary"],
                                   "player_down": bool(after.get("player_down"))})
        out["state"] = engine.snapshot(c["playthrough_id"], player)
    _publish(c["session_id"], {"type": "combat", "event": "resolved",
                               "combat_id": combat_id, "round": out["round"],
                               "steps": out["steps"], "status": out["status"]})
    return out


ZONE_WORDS = {"back_a": "your rear", "front_a": "your line", "centre": "the middle",
              "front_b": "their line", "back_b": "their rear"}


def _verb(who: str, third: str, second: str) -> str:
    """"You comes at" is the kind of seam that makes a game feel unfinished."""
    return second if who.lower() in ("you", "your side") else third


def _combat_prose(out) -> str:
    """Deterministic round summary. The cinematic version costs Mana and is
    opt-in; this one is free and always correct."""
    lines = []
    for s in out["steps"]:
        who = s.get("who", "")
        if s["kind"] == "hit":
            flank = " with the angle" if s.get("flanking") else (" while flanked" if s.get("flanked") else "")
            note = f" - {s['note']}" if s.get("note") else ""
            move = s["move"]
            verb = _verb(who, f"{move}s", move)
            lines.append(f"{who} {verb} {s['target']} for {s['damage']}{flank}{note}.")
        elif s["kind"] == "blocked":
            lines.append(f"{who} {_verb(who, 'comes', 'come')} at {s['target']} and "
                         f"{_verb(who, 'gets', 'get')} nothing through.")
        elif s["kind"] == "move":
            lines.append(f"{who} {_verb(who, 'shifts', 'shift')} to {ZONE_WORDS.get(s['to'], s['to'])}.")
        elif s["kind"] == "down":
            lines.append(f"{who} {_verb(who, 'goes', 'go')} down.")
        elif s["kind"] == "aid":
            lines.append(f"{who} {_verb(who, 'steadies', 'steady')} {s['target']}.")
        elif s["kind"] == "feint":
            lines.append(f"{who} {_verb(who, 'draws', 'draw')} {s['target']} out of position.")
    return " ".join(lines) or "Everyone holds."


@app.post("/api/combat/{combat_id}/condition")
def combat_condition(combat_id: str, entity_id: str, condition: str):
    c = combat.get(combat_id)
    if not c:
        raise HTTPException(404, "no such fight")
    combat.apply_condition(c["playthrough_id"], combat_id, entity_id, condition)
    return combat.view(c["playthrough_id"], combat_id, entity_id)


# ---------------------------------------------------------------------------
# Layer 7 - safety, cards, canon
# ---------------------------------------------------------------------------

@app.get("/api/playthroughs/{pt_id}/safety")
def get_safety(pt_id: str, user_id: str = Query(default="")):
    _own(pt_id, user_id)
    return canon.safety(pt_id)


@app.post("/api/playthroughs/{pt_id}/safety")
def update_safety(pt_id: str, body: SafetyUpdate, user_id: str = Query(default="")):
    _own(pt_id, user_id)
    out = canon.set_safety(pt_id, lines=body.lines, veils=body.veils)
    _publish(db.row("SELECT session_id FROM playthroughs WHERE id=?", (pt_id,))["session_id"],
             {"type": "safety", "payload": out})
    return out


@app.post("/api/playthroughs/{pt_id}/xcard")
def xcard(pt_id: str, body: XCard, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    out = canon.x_card(pt_id, pt["current_turn"], body.player_id, body.note)
    engine._render(pt_id, pt["current_turn"], "safety",
                   "Someone touched the X-card. The scene is struck and we move on.",
                   actor="table", meta={"by": body.player_id})
    _publish(pt["session_id"], {"type": "xcard", "payload": out})
    return out


@app.get("/api/playthroughs/{pt_id}/canonlog")
def canonlog(pt_id: str, user_id: str = Query(default="")):
    _own(pt_id, user_id)
    return {"entries": canon.recent(pt_id, 60)}


@app.get("/api/playthroughs/{pt_id}/cards")
def list_cards(pt_id: str, user_id: str = Query(default="")):
    _own(pt_id, user_id)
    return {"cards": identity.for_playthrough(pt_id)}


@app.post("/api/playthroughs/{pt_id}/cards")
def save_card(pt_id: str, body: CardDraft, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    draft = {"player_id": body.player_id, "name": body.name, "concept": body.concept,
             "aspects": body.aspects, "anomaly": body.anomaly}
    if body.autofill:
        with budget.turn(f"card:{pt_id}", limit=1):
            draft = identity.autofill(world, draft, user_id=body.user_id, pt_id=pt_id)
    card = identity.save(pt_id, draft, session_id=pt["session_id"], card_id=body.card_id)
    return card


@app.post("/api/playthroughs/{pt_id}/cards/{card_id}/submit")
def submit_card(pt_id: str, card_id: str, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    card = identity.submit(pt_id, card_id)
    _publish(pt["session_id"], {"type": "card", "event": "submitted", "card": card})
    return card


@app.post("/api/playthroughs/{pt_id}/cards/{card_id}/vote")
def vote_card(pt_id: str, card_id: str, body: CardVote, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    party, _ = engine._party_context(pt["session_id"])
    table = [p["player_id"] for p in party]
    card = identity.approve(pt_id, card_id, body.voter, ok=body.ok, note=body.note, table=table)
    if card and card["status"] == "approved" and card["anomaly"]:
        world = engine.world_for(pt)
        data = dict(world.data)
        if identity.anomaly_to_canon(pt_id, data, card):
            db.run("UPDATE playthroughs SET world_json=? WHERE id=?", (json.dumps(data), pt_id))
    _publish(pt["session_id"], {"type": "card", "event": "vote", "card": card})
    return card


@app.get("/api/playthroughs/{pt_id}/card-roll")
def card_roll(pt_id: str, player: str = Query(default="user"), name: str = "",
              user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    return identity.roll(engine.world_for(pt), player, name)


# ---------------------------------------------------------------------------
# Broadcast helpers
# ---------------------------------------------------------------------------

def _publish(session_id: str, message: dict):
    """Fire-and-forget from sync request handlers."""
    if not session_id:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(rt.publish(session_id, message))
        return
    loop.create_task(rt.publish(session_id, message))


def _broadcast(pt_id: str, result: dict, session_id: str | None = None):
    if session_id is None:
        pt = db.row("SELECT session_id FROM playthroughs WHERE id=?", (pt_id,))
        session_id = pt["session_id"] if pt else ""
    if not session_id:
        return
    _publish(session_id, {"type": "turn", "entries": result.get("entries", []),
                          "turn": result.get("state", {}).get("turn")})


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws/session/{session_id}")
async def ws_session(ws: WebSocket, session_id: str, player: str = Query(default=""),
                     user_id: str = Query(default="")):
    await ws.accept()
    try:
        s = await asyncio.to_thread(sessions.get, session_id)
    except KeyError:
        await ws.send_json({"type": "error", "error": "no such session"})
        await ws.close()
        return

    me = await asyncio.to_thread(sessions.player_row, session_id, player)
    if not me:
        await ws.send_json({"type": "error", "error": "join the room first"})
        await ws.close()
        return

    pt_id = s["playthrough_id"]
    await asyncio.to_thread(rt.presence_touch, session_id, player,
                            {"name": me["name"], "role": me["role"], "joined": me["joined_at"]})

    async def push_presence():
        """Carries the roster as well as who is online, so a socket that was
        already open learns the newcomer's name without a reload."""
        full = await asyncio.to_thread(sessions.get, session_id)
        await rt.publish(session_id, {
            "type": "presence",
            "players": await asyncio.to_thread(rt.presence_list, session_id),
            "session": sessions.public(full),
        })

    await ws.send_json({
        "type": "hello",
        "session": sessions.public(await asyncio.to_thread(sessions.get, session_id)),
        "player_id": player, "role": me["role"],
        "state": await asyncio.to_thread(engine.snapshot, pt_id, player),
        "feed": await asyncio.to_thread(engine.feed, pt_id),
    })

    async def pump():
        """Everything published to this room, filtered for this socket."""
        async for msg in rt.subscribe(session_id):
            if msg.get("type") == "whisper" and msg.get("to") != player and msg.get("from") != player:
                continue
            await ws.send_json(msg)

    # Subscribe before announcing, so the joiner also receives their own arrival.
    pump_task = asyncio.create_task(pump())
    await asyncio.sleep(0)
    await push_presence()
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")

            if kind == "ping":
                await asyncio.to_thread(rt.presence_touch, session_id, player,
                                        {"name": me["name"], "role": me["role"],
                                         "joined": me["joined_at"]})
                await ws.send_json({"type": "pong"})
                continue

            if kind == "action":
                if me["role"] == "spectator":
                    await ws.send_json({"type": "error", "error": "spectators watch; they do not act"})
                    continue
                # Actions in one room serialise: the turn lock is the queue.
                got = await asyncio.to_thread(rt.acquire_turn, session_id, player)
                if not got:
                    holder = await asyncio.to_thread(rt.turn_holder, session_id)
                    await ws.send_json({"type": "queued", "holder": holder,
                                        "message": "Another player's turn is resolving."})
                    continue
                await rt.publish(session_id, {"type": "thinking", "player_id": player,
                                              "name": me["name"]})
                try:
                    result = await asyncio.to_thread(
                        engine.take_turn, pt_id, (msg.get("action") or "")[:600],
                        premium=bool(msg.get("premium")), player=player,
                        actor_name=me["name"], session_id=session_id)
                except Exception as exc:
                    await ws.send_json({"type": "error", "error": str(exc)[:200]})
                    continue
                finally:
                    await asyncio.to_thread(rt.release_turn, session_id)

                if result.get("blocked"):
                    await ws.send_json({"type": "blocked", "reason": result["reason"]})
                    continue
                await rt.publish(session_id, {
                    "type": "turn", "entries": result["entries"],
                    "by": player, "by_name": me["name"],
                    "turn": result["state"]["turn"], "note": result.get("note", ""),
                })
                # The climax reveal fires inside the deterministic tick and is
                # the whole table's beat, not just the acting player's. Sent as
                # its own message so it lands as a reveal rather than as one
                # more line in a turn nobody re-reads.
                fired = (result.get("tick") or {}).get("legacy") or {}
                if fired.get("reveal"):
                    await rt.publish(session_id, {"type": "traitor",
                                                  "reveal": fired["reveal"]})
                continue

            if kind == "whisper":
                try:
                    out = await asyncio.to_thread(
                        engine.whisper, pt_id, player=player,
                        target_kind=msg.get("target_kind", "npc"),
                        target_id=msg.get("target_id", ""), text=(msg.get("text") or "")[:600],
                        actor_name=me["name"], session_id=session_id)
                except Exception as exc:
                    await ws.send_json({"type": "error", "error": str(exc)[:200]})
                    continue
                if out["kind"] == "player":
                    # Delivered only to the addressee and the sender.
                    await rt.publish(session_id, {"type": "whisper", "to": out["to"],
                                                  "from": player, "from_name": me["name"],
                                                  "payload": out})
                else:
                    await ws.send_json({"type": "whisper", "to": player, "from": player,
                                        "payload": out})
                continue

            if kind == "state":
                await ws.send_json({"type": "state",
                                    "state": await asyncio.to_thread(engine.snapshot, pt_id, player)})
                continue

    except WebSocketDisconnect:
        pass
    finally:
        pump_task.cancel()
        await asyncio.to_thread(rt.presence_drop, session_id, player)
        try:
            await push_presence()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Static player
# ---------------------------------------------------------------------------

app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")


_ASSET_TAGS: dict = {}


def _asset_version(name: str) -> str:
    """A content hash for an asset, recomputed only when the file's mtime moves.

    There is no build step here, so nothing else stamps a version onto the
    asset URLs - which means a browser happily serves a cached stylesheet from
    before a deploy and the app renders with the OLD CSS. That is not a
    theoretical risk: it is exactly how a fixed layout bug appears to survive
    a fix. Hashing the content means the URL changes when (and only when) the
    file does, so caches are correct rather than merely long."""
    path = FRONTEND / "assets" / name
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return "0"
    cached = _ASSET_TAGS.get(name)
    if cached and cached[0] == stamp:
        return cached[1]
    import hashlib
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    _ASSET_TAGS[name] = (stamp, digest)
    return digest


@app.get("/")
def index():
    """Serve the player with content-hashed asset URLs.

    The HTML itself is sent no-store: it is tiny, and it is the one document
    that has to be re-read for a client to learn about new asset hashes at
    all. The assets it points at are immutable per hash and cached hard by
    StaticFiles."""
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    html = html.replace("/assets/styles.css",
                        f"/assets/styles.css?v={_asset_version('styles.css')}")
    html = html.replace("/assets/app.js",
                        f"/assets/app.js?v={_asset_version('app.js')}")
    return Response(html, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow:\n"


# ---------------------------------------------------------------------------
# World modes - the dials that re-tune every other layer
# ---------------------------------------------------------------------------

@app.get("/api/modes/catalogue")
def modes_catalogue():
    return modes.catalogue()


@app.get("/api/playthroughs/{pt_id}/modes")
def modes_get(pt_id: str, user_id: str = Query(min_length=4)):
    _own(pt_id, user_id)
    return modes.public(pt_id)


class ModePatch(BaseModel):
    modes: dict


@app.post("/api/playthroughs/{pt_id}/modes")
def modes_set(pt_id: str, body: ModePatch, user_id: str = Query(min_length=4)):
    _own(pt_id, user_id)
    try:
        modes.set_modes(pt_id, body.modes)
    except modes.UnknownMode as e:
        raise HTTPException(400, str(e))
    return modes.public(pt_id)


# ---------------------------------------------------------------------------
# Deep Prose is a room decision the HOST owns
# ---------------------------------------------------------------------------

class PremiumFlag(BaseModel):
    allowed: bool


@app.post("/api/sessions/{session_id}/premium")
def session_premium(session_id: str, body: PremiumFlag, user_id: str = Query(min_length=4)):
    try:
        return sessions.set_premium_allowed(session_id, user_id, body.allowed)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(404, str(e))


# ---------------------------------------------------------------------------
# Runs and meta-progression
# ---------------------------------------------------------------------------

@app.get("/api/playthroughs/{pt_id}/run")
def run_status(pt_id: str, user_id: str = Query(min_length=4)):
    pt = _own(pt_id, user_id)
    return runs.public(pt_id, pt["user_id"], pt["world_id"])


@app.post("/api/playthroughs/{pt_id}/run/start")
def run_start(pt_id: str, user_id: str = Query(min_length=4)):
    pt = _own(pt_id, user_id)
    return runs.start(pt_id, pt["user_id"], world_id=pt["world_id"])


class RunEnd(BaseModel):
    reason: str = "retired"


@app.post("/api/playthroughs/{pt_id}/run/end")
def run_end(pt_id: str, body: RunEnd, user_id: str = Query(min_length=4)):
    pt = _own(pt_id, user_id)
    try:
        return runs.end(pt_id, reason=body.reason, world_id=pt["world_id"],
                        turn=pt["current_turn"])
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/runs/history")
def run_history(user_id: str = Query(min_length=4)):
    return {"runs": runs.history(user_id)}


# ---------------------------------------------------------------------------
# Death and its resolutions
# ---------------------------------------------------------------------------

@app.get("/api/death/catalogue")
def death_catalogue():
    return death.catalogue()


class Strike(BaseModel):
    who: str
    killer: str = ""
    cause: str = ""
    player_id: str = ""


@app.post("/api/playthroughs/{pt_id}/death/strike")
def death_strike(pt_id: str, body: Strike, user_id: str = Query(min_length=4)):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    return death.strike(pt_id, who=body.who, killer=body.killer, cause=body.cause,
                        session_id=pt["session_id"], player_id=body.player_id, world=world)


class ResolveDeath(BaseModel):
    choice: str
    who: str
    player_id: str = ""


@app.post("/api/playthroughs/{pt_id}/death/resolve")
def death_resolve(pt_id: str, body: ResolveDeath, user_id: str = Query(min_length=4)):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    try:
        out = death.resolve(pt_id, choice=body.choice, who=body.who,
                            session_id=pt["session_id"], player_id=body.player_id,
                            world=world)
    except death.NotOffered as e:
        raise HTTPException(400, str(e))
    if out["ends_run"]:
        out["run"] = runs.end(pt_id, reason="died", world_id=pt["world_id"],
                              turn=pt["current_turn"])
    out["state"] = engine.snapshot(pt_id)
    return out


# ---------------------------------------------------------------------------
# Training-arc skip / fast-forward
# ---------------------------------------------------------------------------

@app.get("/api/fastforward/catalogue")
def ff_catalogue():
    return fastforward.catalogue()


class Skip(BaseModel):
    kind: str = "training"
    turns: Optional[int] = None
    with_whom: list = []


@app.post("/api/playthroughs/{pt_id}/fastforward/plan")
def ff_plan(pt_id: str, body: Skip, user_id: str = Query(min_length=4)):
    _own(pt_id, user_id)
    try:
        return fastforward.plan(pt_id, kind=body.kind, turns=body.turns,
                                with_whom=body.with_whom)
    except fastforward.SkipError as e:
        raise HTTPException(400, str(e))


@app.post("/api/playthroughs/{pt_id}/fastforward")
def ff_apply(pt_id: str, body: Skip, user_id: str = Query(min_length=4)):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    try:
        with budget.turn("ff:" + pt_id):
            changes = fastforward.apply(pt_id, world, kind=body.kind, turns=body.turns,
                                        with_whom=body.with_whom)
            fresh = db.row("SELECT * FROM playthroughs WHERE id=?", (pt_id,))
            state = world_master.build_state(fresh, world)
            recap = narrator.narrate(
                fresh, world, "",
                {"state": state,
                 "consequence": fastforward.recap_prompt(changes, kind=body.kind,
                                                         with_whom=body.with_whom)},
                user_id=pt["user_id"], kind="fastforward")
    except fastforward.SkipError as e:
        raise HTTPException(400, str(e))
    return {"changes": changes, "recap": recap, "skills": fastforward.skills(pt_id),
            "state": engine.snapshot(pt_id)}


@app.get("/api/playthroughs/{pt_id}/skills")
def ff_skills(pt_id: str, user_id: str = Query(min_length=4)):
    _own(pt_id, user_id)
    return {"skills": fastforward.skills(pt_id)}


# ---------------------------------------------------------------------------
# Timeline, entry point, AU
# ---------------------------------------------------------------------------

@app.get("/api/worlds/{world_id}/timeline")
def timeline_get(world_id: str):
    return {"arcs": arcs.timeline(world_id)}


class TimelineBody(BaseModel):
    arcs: list


@app.post("/api/worlds/{world_id}/timeline")
def timeline_set(world_id: str, body: TimelineBody):
    return {"arcs": arcs.set_timeline(world_id, body.arcs)}


# Named /arc, not /timeline: this playthrough already has a /timeline route
# for its event log, and two GETs on one path means the second is dead.
@app.get("/api/playthroughs/{pt_id}/arc")
def timeline_position(pt_id: str, user_id: str = Query(min_length=4)):
    pt = _own(pt_id, user_id)
    return arcs.position(pt_id, pt["world_id"])


class EntryPoint(BaseModel):
    arc_id: str


@app.post("/api/playthroughs/{pt_id}/timeline/entry")
def timeline_entry(pt_id: str, body: EntryPoint, user_id: str = Query(min_length=4)):
    _own(pt_id, user_id)
    try:
        out = arcs.choose_entry(pt_id, body.arc_id)
    except arcs.ArcError as e:
        raise HTTPException(400, str(e))
    out["state"] = engine.snapshot(pt_id)
    return out


class AUBody(BaseModel):
    premise: str = ""


@app.post("/api/playthroughs/{pt_id}/au")
def au_set(pt_id: str, body: AUBody, user_id: str = Query(min_length=4)):
    _own(pt_id, user_id)
    return arcs.set_au(pt_id, body.premise)


# ---------------------------------------------------------------------------
# Party lifecycle - invite, kick, cease
# ---------------------------------------------------------------------------

class Motion(BaseModel):
    kind: str
    proposed_by: str
    target_id: str = ""
    target_name: str = ""


@app.post("/api/sessions/{session_id}/party/propose")
def party_propose(session_id: str, body: Motion):
    try:
        return party.propose(session_id, kind=body.kind, proposed_by=body.proposed_by,
                             target_id=body.target_id, target_name=body.target_name)
    except party.MotionError as e:
        raise HTTPException(400, str(e))


@app.get("/api/sessions/{session_id}/party/motions")
def party_list(session_id: str):
    return {"motions": party.open_motions(session_id)}


class Vote(BaseModel):
    player_id: str
    approve: bool = True


@app.post("/api/sessions/{session_id}/party/motions/{motion_id}/vote")
def party_vote(session_id: str, motion_id: str, body: Vote):
    try:
        m = party.vote(motion_id, body.player_id, body.approve)
    except party.MotionError as e:
        raise HTTPException(400, str(e))

    # A passed kick is carried out immediately - a motion that passes but needs
    # a second button to take effect is a motion that silently never happens.
    if m and m["kind"] == "kick" and m["status"] == "passed":
        session = sessions.get(session_id)
        m["removal"] = party.remove(session_id, m["target_id"],
                                    playthrough_id=session["playthrough_id"],
                                    cease="strict")
    return m


class Cease(BaseModel):
    cease: str = "strict"


@app.post("/api/sessions/{session_id}/party/{player_id}/remove")
def party_remove(session_id: str, player_id: str, body: Cease):
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(404, "no such room")
    try:
        return party.remove(session_id, player_id,
                            playthrough_id=session["playthrough_id"], cease=body.cease)
    except party.MotionError as e:
        raise HTTPException(400, str(e))


@app.get("/api/sessions/{session_id}/party/archives")
def party_archives(session_id: str):
    return {"archives": party.archives(session_id)}


@app.post("/api/sessions/{session_id}/party/archives/{archive_id}/restore")
def party_restore(session_id: str, archive_id: int):
    try:
        return party.restore(archive_id)
    except party.MotionError as e:
        raise HTTPException(404, str(e))


# ---------------------------------------------------------------------------
# Trust & safety - the takedown route that makes the UGC posture real
# ---------------------------------------------------------------------------

@app.get("/api/policy")
def policy():
    return trust.policy()


class ReportBody(BaseModel):
    reason: str
    world_id: str = ""
    playthrough_id: str = ""
    detail: str = ""


@app.post("/api/reports")
def report_file(body: ReportBody, user_id: str = Query(default="")):
    try:
        return trust.file_report(reason=body.reason, world_id=body.world_id,
                                 playthrough_id=body.playthrough_id,
                                 reporter=user_id, detail=body.detail)
    except trust.ReportError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# Character identity block
# ---------------------------------------------------------------------------

@app.get("/api/persona/beats")
def persona_beats():
    return {"beats": list(persona.BEATS)}


class CardIdentity(BaseModel):
    identity: dict = {}
    avatar_url: str = ""
    on_death: str = ""


@app.post("/api/cards/{card_id}/identity")
def card_identity(card_id: str, body: CardIdentity):
    row = db.row("SELECT * FROM cards WHERE id=?", (card_id,))
    if not row:
        raise HTTPException(404, "no such card")
    identity_json = json.dumps(persona.normalise(body.identity))
    db.run("UPDATE cards SET identity=?, avatar_url=?, updated_at=? WHERE id=?",
           (identity_json, body.avatar_url or row["avatar_url"], db.now(), card_id))
    if body.on_death:
        try:
            death.set_preference(card_id, body.on_death)
        except death.NotOffered as e:
            raise HTTPException(400, str(e))
    card = db.row("SELECT * FROM cards WHERE id=?", (card_id,))
    parsed = db.jload(card["identity"], {})
    return {"card_id": card_id, "identity": parsed,
            "avatar_url": card["avatar_url"], "on_death": card["on_death"],
            "completeness": persona.card_completeness(parsed),
            "preview": persona.identity_block(parsed)}


# ---------------------------------------------------------------------------
# Player-uploaded art. Never generated - see backend/uploads.py.
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def upload_image(file: UploadFile = File(...), user_id: str = Query(default="")):
    data = await file.read()
    try:
        return uploads.store(data, owner=user_id)
    except uploads.UploadError as e:
        raise HTTPException(400, str(e))


@app.get("/media/{name}")
def media(name: str):
    try:
        path = uploads.path_for(name)
    except uploads.UploadError:
        raise HTTPException(404, "no such file")
    # Content-hashed names are immutable, so they cache forever. The nosniff
    # header stops a browser from second-guessing the type we declare.
    return FileResponse(path, media_type=uploads.mime_for(name), headers={
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    })


# ---------------------------------------------------------------------------
# Combat aftermath - resolving a death that happened in a fight
# ---------------------------------------------------------------------------

@app.get("/api/playthroughs/{pt_id}/death/offered")
def death_offered(pt_id: str, user_id: str = Query(min_length=4)):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    return {"resolutions": death.offered(pt_id, world=world),
            "permadeath": modes.permadeath_on(pt_id)}


# ---------------------------------------------------------------------------
# Story ending
# ---------------------------------------------------------------------------

@app.post("/api/playthroughs/{pt_id}/close")
def story_close(pt_id: str, body: RunEnd, user_id: str = Query(min_length=4)):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    try:
        return aftermath.close_story(pt_id, world, reason=body.reason,
                                     turn=pt["current_turn"])
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# Workstream A - the legibility layer
# ---------------------------------------------------------------------------

@app.get("/api/playthroughs/{pt_id}/awareness")
def awareness_panel(pt_id: str, user_id: str = Query(default=""),
                    player: str = Query(default=memory.SOLO)):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    prefs = db.row("SELECT data FROM prefs WHERE user_id=?", (f"hud:{pt_id}:{player}",))
    overrides = db.jload(prefs["data"], {}) if prefs else {}
    return legibility.panel(pt_id, world, player, pt["current_turn"],
                            location=pt["current_location"], overrides=overrides)


class HudPrefs(BaseModel):
    shows: dict = {}


@app.post("/api/playthroughs/{pt_id}/awareness/prefs")
def awareness_prefs(pt_id: str, body: HudPrefs, user_id: str = Query(default=""),
                    player: str = Query(default=memory.SOLO)):
    """Every surface element is toggleable, independent of mode."""
    _own(pt_id, user_id)
    clean = {k: bool(v) for k, v in (body.shows or {}).items()
             if k in ("pulse", "faces", "factions", "board")}
    db.run("INSERT INTO prefs (user_id,data,updated_at) VALUES (?,?,?)"
           " ON CONFLICT(user_id) DO UPDATE SET data=excluded.data,"
           " updated_at=excluded.updated_at",
           (f"hud:{pt_id}:{player}", json.dumps(clean), db.now()))
    return {"shows": clean}


@app.get("/api/tooltips")
def hud_tooltips():
    return legibility.tooltips()


# ---------------------------------------------------------------------------
# Workstream C - the institution
# ---------------------------------------------------------------------------

@app.get("/api/playthroughs/{pt_id}/authority")
def authority_panel(pt_id: str, user_id: str = Query(default=""),
                    player: str = Query(default=memory.SOLO)):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    return authority.public(pt_id, world, player, pt["current_turn"])


class Settle(BaseModel):
    faction_id: str
    how: str = "pay"


@app.post("/api/playthroughs/{pt_id}/authority/settle")
def authority_settle(pt_id: str, body: Settle, user_id: str = Query(default=""),
                     player: str = Query(default=memory.SOLO)):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    if body.how not in ("pay", "atone"):
        raise HTTPException(400, "settle by 'pay' or 'atone'")

    # Paying is a Mana transaction; atoning is an in-world act and costs
    # nothing but time. Charge BEFORE settling so a failed charge cannot
    # clear a warrant for free.
    cost = 0
    if body.how == "pay":
        rows = authority.ledger(pt_id, world, player)
        row = next((r for r in rows if r["faction"] == body.faction_id), None)
        if not row:
            raise HTTPException(404, "no such authority")
        cost = row["fine_mana"]
        if cost:
            # Check the balance BEFORE clearing anything: a fine that fails to
            # charge must not still lift the warrant.
            fresh = db.row("SELECT mana_balance FROM playthroughs WHERE id=?", (pt_id,))
            if (fresh or {}).get("mana_balance", 0) < cost:
                raise HTTPException(
                    402, f"paying this off costs {cost} Mana — or settle it in the world for free")
            mana.grant(pt_id, -cost)

    out = authority.settle(pt_id, world, player=player, faction_id=body.faction_id,
                           how=body.how, turn=pt["current_turn"])
    if not out["settled"]:
        raise HTTPException(400, out.get("reason", "nothing to settle"))
    out["mana_spent"] = cost
    out["state"] = engine.snapshot(pt_id, player)
    return out


@app.get("/api/worlds/{world_id}/factions")
def world_factions(world_id: str):
    w = world_registry.get(world_id)
    return {"factions": awareness.factions(w),
            "authorities": [f["id"] for f in authority.authorities(w)]}


@app.get("/api/town-memory")
def town_memory(user_id: str = Query(min_length=4), world_id: str = Query(default="")):
    """What the towns you have played in still believe about you."""
    return {"remembered": authority.town_memory(user_id, world_id or None),
            "guest": auth.is_guest(user_id)}


# ---------------------------------------------------------------------------
# Workstream D - accounts
# ---------------------------------------------------------------------------

class Register(BaseModel):
    email: str
    password: str
    display_name: str = ""
    # The id the browser was playing under. Signing up must not cost you the
    # story you signed up to keep.
    guest_id: str = ""


class Login(BaseModel):
    email: str
    password: str
    guest_id: str = ""


def _set_session_cookie(response: Response, request: Request, token: str):
    response.set_cookie(auth.COOKIE, token,
                        **auth.cookie_kwargs(request.url.scheme == "https"))


def _claim_quietly(guest_id: str, account_id: str) -> dict:
    """Carrying a guest's work onto their new account must never be the thing
    that fails a signup - if it cannot be done, they are still signed in."""
    if not guest_id:
        return {"claimed": False}
    try:
        return auth.claim(guest_id, account_id)
    except auth.AuthError as e:
        return {"claimed": False, "reason": str(e)}


@app.get("/api/auth/claimable")
def auth_claimable(user_id: str = Query(default="")):
    """What a guest would bring with them if they signed up right now."""
    return auth.claimable(user_id)


@app.post("/api/auth/register")
def auth_register(body: Register, request: Request, response: Response):
    try:
        account = auth.register(body.email, body.password, body.display_name)
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    token = auth.start_session(account["id"],
                               user_agent=request.headers.get("user-agent", ""))
    _set_session_cookie(response, request, token)
    claimed = _claim_quietly(body.guest_id, account["id"])
    return {"account": account, "claimed": claimed}


@app.post("/api/auth/login")
def auth_login(body: Login, request: Request, response: Response):
    try:
        out = auth.login(body.email, body.password,
                         user_agent=request.headers.get("user-agent", ""))
    except auth.AuthError as e:
        raise HTTPException(401, str(e))
    _set_session_cookie(response, request, out["token"])
    claimed = _claim_quietly(body.guest_id, out["account"]["id"])
    return {"account": out["account"], "claimed": claimed}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    auth.logout(request.cookies.get(auth.COOKIE, ""))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def auth_me(request: Request):
    account_id = auth.session_account(request.cookies.get(auth.COOKIE, ""))
    if not account_id:
        return {"account": None, "guest": True}
    return {"account": auth.public(account_id), "guest": False}


class ProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    bio: Optional[str] = None
    avatar_url: Optional[str] = None


@app.get("/api/profile")
def profile_get(request: Request):
    account_id = _require_account(request)
    acct = auth.public(account_id)
    return {
        "account": acct,
        "mana": _account_mana(account_id),
        "runs": runs.history(account_id, 10),
        "streak": streaks.status(account_id),
        "cards": db.rows("SELECT id,name,concept,avatar_url FROM cards WHERE player_id=?"
                         " ORDER BY updated_at DESC LIMIT 20", (account_id,)),
        "towns": authority.town_memory(account_id),
        "sessions": auth.sessions_for(account_id),
    }


@app.put("/api/profile")
def profile_update(body: ProfileUpdate, request: Request):
    account_id = _require_account(request)
    try:
        return {"account": auth.update_profile(
            account_id, display_name=body.display_name, bio=body.bio,
            avatar_url=body.avatar_url)}
    except auth.AuthError as e:
        raise HTTPException(400, str(e))


class PasswordChange(BaseModel):
    current: str
    new: str


@app.post("/api/auth/password")
def auth_password(body: PasswordChange, request: Request, response: Response):
    account_id = _require_account(request)
    try:
        out = auth.change_password(account_id, body.current, body.new)
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    response.delete_cookie(auth.COOKIE, path="/")
    return out


# ---------------------------------------------------------------------------
# Live canon research (World Bootstrap)
# ---------------------------------------------------------------------------

@app.get("/api/forge/research")
def forge_research(setting: str = Query(min_length=2, max_length=120),
                   refresh: bool = Query(default=False),
                   depth: str = Query(default="quick")):
    """What the web knows about a setting, before committing to building it.

    Exposed so a player can SEE what will ground their world - and so they can
    tell the difference between "we found the real canon" and "we are about to
    invent this", which is exactly the distinction an ungrounded build hides."""
    # Parsed, not searched. A premise names properties; searching the sentence
    # found nothing and told the player their canon request was unknown.
    d = research.premise_dossier(setting, refresh=refresh, depth=depth)
    parsed = d.get("premise") or {}
    return {
        "setting": d["setting"], "found": d["found"], "cached": d.get("cached", False),
        # What we understood them to be asking for. Shown whether or not any
        # lookup succeeded, because understanding the request and grounding it
        # are two different things and the player should see both.
        "premise": {
            "is_premise": parsed.get("is_premise", False),
            "host": parsed.get("host", ""),
            "imports": parsed.get("imports", []),
            "entities": parsed.get("entities", []),
        },
        "grounded": [name for name, ent in (d.get("entities") or {}).items()
                     if ent.get("found")],
        "ungrounded": [name for name, ent in (d.get("entities") or {}).items()
                       if not ent.get("found")],
        "canonical_name": d["canonical_name"], "wiki": d["wiki"],
        "summary": d["summary"][:600],
        "characters": [c["name"] for c in d["characters"][:14]],
        "places": [p["name"] for p in d["places"][:10]],
        "factions": [f["name"] for f in d["factions"][:8]],
        "sources": research.attribution(d),
        "note": d["note"], "enabled": research.enabled(),
    }


# ---------------------------------------------------------------------------
# The Mana wallet - account-scoped, not story-scoped
# ---------------------------------------------------------------------------
# Mana belongs to the person. A host who buys a pack spends it in every world
# and every room they run, and can buy BEFORE they have a story at all - which
# the old per-story routes could not do (the client posted to
# /playthroughs/null/purchase and got a 404).

@app.get("/api/mana/wallet")
def mana_wallet(user_id: str = Query(min_length=4)):
    w = mana.wallet(user_id)
    mana.absorb_playthrough_balances(user_id)
    w = mana.wallet(user_id)
    return {"balance": int(w["balance"]), "purchased": int(w["purchased"]),
            "spent": int(w["spent"]),
            "status": mana.status(user_id),
            "packs": config.MANA_PACKS,
            "guest": auth.is_guest(user_id)}


@app.post("/api/mana/purchase")
def mana_purchase(body: Purchase, user_id: str = Query(min_length=4)):
    """Dev-only instant grant, mirroring the per-story route this replaces.
    Refuses once real payments are configured."""
    if payments.configured():
        raise HTTPException(409, "real payments are configured - use /api/mana/paypal/create-order")
    pack = next((p for p in config.MANA_PACKS if p["id"] == body.pack_id), None)
    if not pack:
        raise HTTPException(400, "unknown pack")
    mana.credit(user_id, pack["mana"])
    return {"granted": pack["mana"], "pack": pack,
            "balance": mana.balance(user_id), "stubbed": True}


@app.post("/api/mana/paypal/create-order")
def mana_paypal_create(body: Purchase, request: Request,
                       user_id: str = Query(min_length=4)):
    user_id = _require_paying_account(request, user_id)
    if not payments.configured():
        raise HTTPException(503, "PayPal is not configured on this deployment")
    try:
        # The order is opened against the ACCOUNT, not a story. payments.py
        # still wants a playthrough_id for its own record, so it gets a
        # stable wallet marker instead of a real one.
        return payments.create_order(user_id=user_id, playthrough_id=f"wallet:{user_id}",
                                     pack_id=body.pack_id)
    except payments.PaymentError as e:
        raise HTTPException(400, str(e))


@app.post("/api/mana/paypal/capture")
def mana_paypal_capture(request: Request, order_id: str = Query(min_length=1),
                        user_id: str = Query(min_length=4)):
    user_id = _require_paying_account(request, user_id)
    if not payments.configured():
        raise HTTPException(503, "PayPal is not configured on this deployment")
    try:
        result = payments.capture_order(order_id, expected_user_id=user_id)
    except payments.PaymentError as e:
        raise HTTPException(402, str(e))
    if not result["already_captured"]:
        mana.credit(user_id, result["mana"])
    return {"granted": result["mana"], "pack_id": result["pack_id"],
            "already_captured": result["already_captured"],
            "balance": mana.balance(user_id)}


@app.get("/api/forge/scales")
def forge_scales():
    """What each world size actually costs and produces, BEFORE committing.

    A "whole world" is nine model calls, not one - the player should know
    that before pressing the button, not discover it on their bill."""
    return {"scales": [worldforge.scale_plan(k) for k in worldforge.SCALES]}


@app.get("/api/forge/session-zero")
def forge_session_zero(setting: str = Query(min_length=2, max_length=160),
                       mode: str = Query(default="auto")):
    """The questions to ask BEFORE building a world.

    For a canon setting these are grounded in what was actually researched -
    the world's real arcs and its real power system - so the player picks
    "start at the Mugen Train arc" instead of typing a guess. Research is
    cached, so opening this and then building costs one lookup, not two."""
    found = (research.dossier(setting) if mode != "original"
             else {"found": False, "setting": setting})
    return sessionzero.questions(found)


# ---------------------------------------------------------------------------
# OOC - the table talking about the story, kept out of the story
# ---------------------------------------------------------------------------

class OocPost(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    name: str = Field(default="", max_length=40)
    to_wm: bool = False


@app.get("/api/playthroughs/{pt_id}/ooc")
def ooc_history(pt_id: str, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    return {"messages": ooc.history(pt["session_id"], pt_id)}


@app.post("/api/playthroughs/{pt_id}/ooc")
def ooc_post(pt_id: str, body: OocPost, user_id: str = Query(default=""),
             player: str = Query(default=memory.SOLO)):
    """Free between players. A question to the World Master costs one short,
    hard-capped model call - the only recurring cost on this channel."""
    pt = _own(pt_id, user_id)
    try:
        msg = ooc.post(pt["session_id"], pt_id, player=player,
                       name=body.name or player, text=body.text, to_wm=body.to_wm)
    except ValueError as e:
        raise HTTPException(400, str(e))

    if body.to_wm:
        world = engine.world_for(pt)
        try:
            with budget.turn(f"ooc:{pt_id}", limit=1):
                reply = ooc.ask_world_master(pt, world, body.text,
                                             user_id=pt["user_id"], player=player)
            msg = ooc.answer_and_record(msg["id"], reply)
        except llm.LLMError as e:
            msg["reply"] = f"(the World Master could not answer: {e})"

    _publish(pt["session_id"], {"type": "ooc", "message": msg})
    return msg


class DomainPatch(BaseModel):
    levels: dict = Field(default_factory=dict)
    severity: Optional[str] = None


@app.get("/api/playthroughs/{pt_id}/canon-spectrum")
def canon_spectrum(pt_id: str, user_id: str = Query(default="")):
    _own(pt_id, user_id)
    return modes.domain_public(pt_id)


@app.post("/api/playthroughs/{pt_id}/canon-spectrum")
def set_canon_spectrum(pt_id: str, body: DomainPatch, user_id: str = Query(default="")):
    """Strictness per domain, plus how hard consequences land. 'Who characters
    are' is deliberately not settable below strict - a character breaking their
    own persona is a product failure, not a freedom a table opted into."""
    _own(pt_id, user_id)
    try:
        return modes.set_domains(pt_id, levels=body.levels or None, sev=body.severity)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------------------
# The mode tree - what kind of game this is
# ---------------------------------------------------------------------------

@app.get("/api/modes/tree")
def mode_tree():
    """The whole tree, grouped by family. The first question a host answers is
    which of the three they want, so it is served in that shape."""
    return modetree.catalogue()


@app.get("/api/modes/tree/{mode_id}")
def mode_tree_one(mode_id: str):
    try:
        return modetree.public(mode_id)
    except modetree.ModeError as e:
        raise HTTPException(404, str(e))


@app.get("/api/sessions/{session_id}/turn-order")
def turn_order(session_id: str, player: str = Query(default="")):
    """Whose turn it is, under this mode's policy. Deterministic from the seat
    list and the turn number, so a client can render the queue without asking
    on every tick - and a disconnect cannot lose the order."""
    s = db.row("SELECT playthrough_id FROM sessions WHERE id=?", (session_id,))
    if not s:
        raise HTTPException(404, "no such room")
    pt = db.row("SELECT current_turn FROM playthroughs WHERE id=?", (s["playthrough_id"],))
    turn = pt["current_turn"] if pt else 0
    mode_id = sessions.type_of(session_id)
    seats = sessions.seats(session_id)
    return {
        "mode": modetree.public(mode_id),
        "seats": seats,
        "turn": turn,
        "whose": (seats[turn % len(seats)] if seats
                  and modetree.turn_policy(mode_id) == "round_robin" else None),
        "you": modetree.may_act(mode_id, seats=seats, player_id=player, turn=turn)
        if player else None,
    }


# ---------------------------------------------------------------------------
# P8 The Room - seats, talk, the vote
# ---------------------------------------------------------------------------

def _room_session(session_id):
    row = db.row("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not row:
        raise HTTPException(404, "no such room")
    if sessions.type_of(session_id) != "room":
        raise HTTPException(400, "that room is not playing The Room")
    return row


class RoomSetup(BaseModel):
    characters: list = Field(default_factory=list)
    imposters: int = Field(default=1, ge=1, le=4)
    rounds: int = Field(default=room.DEFAULT_ROUNDS, ge=1, le=12)


@app.post("/api/rooms/{session_id}/setup")
def room_setup(session_id: str, body: RoomSetup, user_id: str = Query(default="")):
    """Seat the table. Who is an imposter is decided here and never again."""
    row = _room_session(session_id)
    if user_id and row["host_user_id"] != user_id:
        raise HTTPException(403, "only the host sets the table")
    try:
        out = room.setup(session_id, characters=body.characters,
                         imposters=body.imposters, rounds=body.rounds)
    except room.RoomError as e:
        raise HTTPException(400, str(e))
    _publish(session_id, {"type": "room", "event": "setup"})
    return out


@app.post("/api/rooms/{session_id}/seat/{seat_id}")
def room_take_seat(session_id: str, seat_id: str, player: str = Query(...)):
    _room_session(session_id)
    try:
        out = room.take_seat(session_id, seat_id, player)
    except room.RoomError as e:
        raise HTTPException(400, str(e))
    _publish(session_id, {"type": "room", "event": "seated", "seat": out})
    return out


@app.post("/api/rooms/{session_id}/begin")
def room_begin(session_id: str, user_id: str = Query(default="")):
    row = _room_session(session_id)
    if user_id and row["host_user_id"] != user_id:
        raise HTTPException(403, "only the host starts it")
    try:
        out = room.begin(session_id)
    except room.RoomError as e:
        raise HTTPException(400, str(e))
    _publish(session_id, {"type": "room", "event": "begin", "state": out})
    return out


class RoomSay(BaseModel):
    text: str = Field(min_length=1, max_length=room.MAX_SAY)


@app.post("/api/rooms/{session_id}/say")
def room_say(session_id: str, body: RoomSay, player: str = Query(...)):
    """A player speaks as their character. No model call - a human in a seat
    is the only character in the game that never drifts."""
    _room_session(session_id)
    seat_id = room.seat_of(session_id, player)
    if not seat_id:
        raise HTTPException(400, "you have no seat")
    try:
        line = room.say(session_id, seat_id, body.text, player_id=player)
    except room.RoomError as e:
        raise HTTPException(400, str(e))
    _publish(session_id, {"type": "room", "event": "line", "line": line})
    return line


@app.post("/api/rooms/{session_id}/ai/{seat_id}")
def room_ai(session_id: str, seat_id: str, user_id: str = Query(default="")):
    """An AI seat takes its turn. One completion, guarded on both sides, and it
    clamps rather than showing a broken frame."""
    row = _room_session(session_id)
    try:
        with budget.turn(f"room:{session_id}", limit=2):
            line = room.ai_say(session_id, seat_id, user_id=user_id or row["host_user_id"])
    except room.RoomError as e:
        raise HTTPException(400, str(e))
    except llm.LLMError as e:
        raise HTTPException(502, f"that seat could not answer: {e}")
    _publish(session_id, {"type": "room", "event": "line", "line": line})
    return line


class RoomTestify(BaseModel):
    text: str = Field(min_length=1, max_length=room.MAX_SAY)


@app.post("/api/rooms/{session_id}/testify")
def room_testify(session_id: str, body: RoomTestify, player: str = Query(...)):
    """A banished player's one line a round, from outside the table.

    Elimination that leaves somebody watching in silence is what stops people
    joining these games; this is influence without a vote."""
    _room_session(session_id)
    seat_id = room.seat_of(session_id, player)
    if not seat_id:
        raise HTTPException(400, "you have no seat")
    try:
        line = room.testify(session_id, seat_id, body.text, player_id=player)
    except room.RoomError as e:
        raise HTTPException(400, str(e))
    _publish(session_id, {"type": "room", "event": "line", "line": line})
    return line


@app.post("/api/rooms/{session_id}/advance")
def room_advance(session_id: str):
    _room_session(session_id)
    try:
        out = room.advance(session_id)
    except room.RoomError as e:
        raise HTTPException(400, str(e))
    _publish(session_id, {"type": "room", "event": "phase", "state": out})
    return out


class RoomVote(BaseModel):
    target: str = Field(min_length=1, max_length=12)


@app.post("/api/rooms/{session_id}/vote")
def room_vote(session_id: str, body: RoomVote, player: str = Query(...)):
    _room_session(session_id)
    seat_id = room.seat_of(session_id, player)
    if not seat_id:
        raise HTTPException(400, "you have no seat")
    try:
        out = room.vote(session_id, seat_id, body.target)
    except room.RoomError as e:
        raise HTTPException(400, str(e))
    _publish(session_id, {"type": "room", "event": "vote", "tally": out})
    return out


@app.post("/api/rooms/{session_id}/resolve")
def room_resolve(session_id: str):
    """Banish the most-voted seat and reveal what they were."""
    _room_session(session_id)
    try:
        out = room.resolve(session_id)
    except room.RoomError as e:
        raise HTTPException(400, str(e))
    _publish(session_id, {"type": "room", "event": "resolved", "result": out})
    if out.get("over"):
        _settle_room(session_id, out)
    return out


def _settle_room(session_id, out):
    """A finished Room is a finished PvP match: the world is torn down and the
    result reaches every profile at the table."""
    winner = out.get("winner", "")
    for seat in room.seats(session_id):
        if seat["occupant"] == "ai":
            continue
        won = (winner == "imposters") == bool(seat["is_imposter"])
        try:
            ladder.record_match(player_id=seat["occupant"], session_id=session_id,
                                mode="room", outcome="win" if won else "loss")
        except ValueError:
            continue
    try:
        sessions.settle(session_id, outcome=winner, winner=winner)
    except KeyError:
        pass


@app.get("/api/rooms/{session_id}")
def room_state(session_id: str, player: str = Query(default="")):
    """What one seat may see. Your own role is yours; everyone else's is hidden
    until they are banished or it is over - enforced here, never in a client."""
    _room_session(session_id)
    return room.public(session_id, player)


# ---------------------------------------------------------------------------
# The competitive profile, seasons and boards
# ---------------------------------------------------------------------------

def _owner(user_id: str, player: str, session_id: str):
    """An account if there is one, otherwise a session-scoped identity. Play is
    never blocked to make somebody sign up."""
    acct = db.row("SELECT id FROM accounts WHERE id=?", (user_id,)) if user_id else None
    return (user_id if acct else "", player, session_id)


@app.get("/api/profile/standing")
def get_standing(user_id: str = Query(default=""), player: str = Query(default=memory.SOLO),
                 session_id: str = Query(default="")):
    """The COMPETITIVE profile - scores, season, rank, feuds.

    Not /api/profile: that path is the account profile (mana, runs, cards) and
    was registered first, so this handler would have been dead and the client
    would have silently received the wrong shape forever."""
    a, p, s = _owner(user_id, player, session_id)
    return ladder.public(a, p, s)


@app.get("/api/profile/trophies")
def get_trophies(user_id: str = Query(default=""), player: str = Query(default=memory.SOLO),
                 session_id: str = Query(default="")):
    a, p, s = _owner(user_id, player, session_id)
    return ladder.trophy_room(a, p, s)


@app.get("/api/leaderboard")
def get_leaderboard(board: str = Query(default="pvp"), mode: str = Query(default=""),
                    limit: int = Query(default=50, ge=1, le=200)):
    try:
        return ladder.leaderboard(board=board, mode=mode, limit=limit)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/leaderboard/daily")
def get_daily_board(day: str = Query(default=""), limit: int = Query(default=50, ge=1, le=200)):
    return ladder.daily_board(day=day, limit=limit)


class DailySubmit(BaseModel):
    name: str = Field(default="", max_length=40)


@app.post("/api/playthroughs/{pt_id}/daily/submit")
def submit_daily(pt_id: str, body: DailySubmit, user_id: str = Query(default=""),
                 player: str = Query(default=memory.SOLO)):
    """Put today's run on the board. Everyone played the same world, so this is
    the one comparison here that is genuinely like for like."""
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    scored = submodes.daily_score(pt_id, world, player)
    out = ladder.submit_daily(owner_id=user_id or f"{pt_id}:{player}",
                              name=body.name or player, score=scored["score"],
                              turns=scored["turns"])
    return {**out, "score_detail": scored}


# ---------------------------------------------------------------------------
# Sub-mode mechanics - the objective, and the modes that have their own verbs
# ---------------------------------------------------------------------------

class Contest(BaseModel):
    place_id: str = Field(min_length=1, max_length=60)


@app.post("/api/playthroughs/{pt_id}/contest")
def contest_node(pt_id: str, body: Contest, user_id: str = Query(default=""),
                 player: str = Query(default=memory.SOLO)):
    """Make a move on a place. Public by construction - the people who see you
    take it are the people whose opinion decides whether you keep it."""
    pt = _own(pt_id, user_id)
    try:
        out = submodes.contest(pt_id, engine.world_for(pt), place_id=body.place_id,
                               player=player, session_id=pt["session_id"],
                               turn=pt["current_turn"])
    except submodes.SubmodeError as e:
        raise HTTPException(400, str(e))
    _publish(pt["session_id"], {"type": "contest", "result": out})
    return out


@app.post("/api/sessions/{session_id}/sides")
def deal_sides(session_id: str, user_id: str = Query(default="")):
    """Deal sides for Teams, Hunt or Battle Royale. Once, and deterministically."""
    row = db.row("SELECT host_user_id FROM sessions WHERE id=?", (session_id,))
    if not row:
        raise HTTPException(404, "no such room")
    if user_id and row["host_user_id"] != user_id:
        raise HTTPException(403, "only the host deals sides")
    mode_id = sessions.type_of(session_id)
    dealt = submodes.assign_sides(session_id, mode_id)
    if not dealt:
        raise HTTPException(400, f"{mode_id} does not have sides")
    _publish(session_id, {"type": "sides", "sides": submodes.sides(session_id)})
    return submodes.sides(session_id)


@app.get("/api/sessions/{session_id}/sides")
def get_sides(session_id: str):
    return submodes.sides(session_id)


@app.post("/api/playthroughs/{pt_id}/raid/open")
def open_raid(pt_id: str, user_id: str = Query(default="")):
    """Give a Raid something to actually raid."""
    pt = _own(pt_id, user_id)
    try:
        out = submodes.open_raid(pt_id, engine.world_for(pt), turn=pt["current_turn"])
    except submodes.SubmodeError as e:
        raise HTTPException(400, str(e))
    _publish(pt["session_id"], {"type": "raid", "result": out})
    return out


@app.get("/api/playthroughs/{pt_id}/objective")
def get_objective(pt_id: str, user_id: str = Query(default=""),
                  player: str = Query(default=memory.SOLO)):
    """What you are trying to do here, and how far along you are. Read out of
    engine state rather than a parallel counter."""
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    # engine.mode_of is the single resolver. Deriving it here a second way is
    # how a solo Detective world was told its objective was Story.
    mode_id = engine.mode_of(pt)
    return submodes.public(pt_id, world, mode_id, player=player,
                           session_id=pt["session_id"])


@app.post("/api/playthroughs/{pt_id}/case/open")
def case_open(pt_id: str, user_id: str = Query(default="")):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    try:
        return submodes.open_case(pt_id, world, turn=pt["current_turn"])
    except submodes.SubmodeError as e:
        raise HTTPException(400, str(e))


@app.get("/api/playthroughs/{pt_id}/case")
def case_file(pt_id: str, user_id: str = Query(default=""),
              player: str = Query(default=memory.SOLO)):
    pt = _own(pt_id, user_id)
    return submodes.casefile(pt_id, engine.world_for(pt), player)


@app.post("/api/playthroughs/{pt_id}/case/ask/{npc_id}")
def case_ask(pt_id: str, npc_id: str, user_id: str = Query(default=""),
             player: str = Query(default=memory.SOLO)):
    """Ask somebody what they saw. Gated on both sides: they must know it, and
    they must be willing to tell YOU."""
    pt = _own(pt_id, user_id)
    try:
        return submodes.question(pt_id, engine.world_for(pt), npc_id,
                                 player=player, turn=pt["current_turn"])
    except submodes.SubmodeError as e:
        raise HTTPException(400, str(e))


@app.post("/api/playthroughs/{pt_id}/case/accuse/{npc_id}")
def case_accuse(pt_id: str, npc_id: str, user_id: str = Query(default=""),
                player: str = Query(default=memory.SOLO)):
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    try:
        out = submodes.accuse(pt_id, world, npc_id, player=player,
                              turn=pt["current_turn"])
    except submodes.SubmodeError as e:
        raise HTTPException(400, str(e))
    if out.get("correct"):
        ladder.add_legacy(120, account_id=user_id, player_id=player,
                          session_id=pt["session_id"], why="closed a case")
    return out


@app.post("/api/playthroughs/{pt_id}/masks/vote/{npc_id}")
def mask_vote(pt_id: str, npc_id: str, user_id: str = Query(default=""),
              player: str = Query(default=memory.SOLO)):
    """Vote a face out. Right, and a mask comes off. Wrong, and somebody who
    actually lived here is dead and the world knows who called for it."""
    pt = _own(pt_id, user_id)
    world = engine.world_for(pt)
    out = submodes.mask_vote(pt["session_id"], pt_id, world, npc_id=npc_id,
                             by=player, turn=pt["current_turn"])
    _publish(pt["session_id"], {"type": "mask", "result": out})
    return out


# ---------------------------------------------------------------------------
# Layer 6 - legacy, the world chronicle, succession, orgs, the traitor
# ---------------------------------------------------------------------------

def _pt_world(pt_id, user_id):
    pt = _own(pt_id, user_id)
    return pt, engine.world_for(pt), pt["current_turn"]


@app.get("/api/playthroughs/{pt_id}/legacy")
def legacy_all(pt_id: str, user_id: str = Query(default=""),
               player: str = Query(default=memory.SOLO)):
    pt, world, turn = _pt_world(pt_id, user_id)
    return legacy.public(pt_id, world, player, turn=turn)


@app.get("/api/playthroughs/{pt_id}/chronicle")
def chronicle(pt_id: str, user_id: str = Query(default=""),
              player: str = Query(default=memory.SOLO),
              limit: int = Query(default=60, ge=1, le=200)):
    """The world feed, witness-gated. It can only return events this player
    actually learned - there is no flag that widens it."""
    pt, world, turn = _pt_world(pt_id, user_id)
    out = legacy.chronicle(pt_id, world, player, limit=limit)
    out["unseen"] = legacy.unseen_count(pt_id, world, player)
    return out


@app.post("/api/playthroughs/{pt_id}/chronicle/read")
def chronicle_read(pt_id: str, user_id: str = Query(default=""),
                   player: str = Query(default=memory.SOLO)):
    pt, world, _ = _pt_world(pt_id, user_id)
    return legacy.mark_read(pt_id, world, player)


@app.get("/api/playthroughs/{pt_id}/drift")
def drift(pt_id: str, user_id: str = Query(default=""),
          player: str = Query(default=memory.SOLO),
          turned_only: bool = Query(default=False)):
    """Who has turned, how far, and the event that did it."""
    pt, world, _ = _pt_world(pt_id, user_id)
    return {"drift": legacy.drift(pt_id, world, player, only_turned=turned_only)}


@app.get("/api/playthroughs/{pt_id}/successions")
def successions(pt_id: str, user_id: str = Query(default=""),
                player: str = Query(default=memory.SOLO)):
    pt, world, _ = _pt_world(pt_id, user_id)
    return {"vacuums": legacy.vacuums(pt_id, world, player)}


@app.get("/api/playthroughs/{pt_id}/power")
def power(pt_id: str, user_id: str = Query(default=""),
          player: str = Query(default=memory.SOLO)):
    pt, world, turn = _pt_world(pt_id, user_id)
    return legacy.power_panel(pt_id, world, player, turn=turn)


class OrgNew(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kind: str = Field(default="cell", max_length=20)
    charter: str = Field(default="", max_length=400)
    # One of the seven, or none. A doctrine is what the house says it is for.
    doctrine: str = Field(default="", max_length=20)


@app.post("/api/playthroughs/{pt_id}/orgs")
def found_org(pt_id: str, body: OrgNew, user_id: str = Query(default=""),
              player: str = Query(default=memory.SOLO)):
    pt, world, turn = _pt_world(pt_id, user_id)
    try:
        out = legacy.found_org(pt_id, world, player=player, name=body.name,
                               kind=body.kind, charter=body.charter, turn=turn,
                               doctrine=body.doctrine)
    except legacy.OrgError as e:
        raise HTTPException(400, str(e))
    _publish(pt["session_id"], {"type": "org", "event": "founded", "org": out})
    return out


class OrgRecruit(BaseModel):
    npc_id: str = Field(min_length=1, max_length=60)
    rank: str = Field(default="member", max_length=20)


@app.post("/api/playthroughs/{pt_id}/orgs/{org_id}/recruit")
def org_recruit(pt_id: str, org_id: str, body: OrgRecruit,
                user_id: str = Query(default=""),
                player: str = Query(default=memory.SOLO)):
    pt, world, turn = _pt_world(pt_id, user_id)
    try:
        return legacy.recruit(pt_id, world, org_id=org_id, npc_id=body.npc_id,
                              player=player, turn=turn, rank=body.rank)
    except legacy.OrgError as e:
        raise HTTPException(400, str(e))


class OrgOrder(BaseModel):
    npc_id: str = Field(min_length=1, max_length=60)
    order: str = Field(min_length=1, max_length=300)
    severity: int = Field(default=3, ge=1, le=5)


@app.post("/api/playthroughs/{pt_id}/orgs/{org_id}/command")
def org_command(pt_id: str, org_id: str, body: OrgOrder,
                user_id: str = Query(default=""),
                player: str = Query(default=memory.SOLO)):
    """Give an order through a subordinate. The world witnesses THEM."""
    pt, world, turn = _pt_world(pt_id, user_id)
    try:
        out = legacy.command(pt_id, world, org_id=org_id, npc_id=body.npc_id,
                             player=player, order=body.order, turn=turn,
                             severity=body.severity)
    except legacy.OrgError as e:
        raise HTTPException(400, str(e))
    if out.get("carried"):
        _publish(pt["session_id"], {"type": "org", "event": "order", "result": out})
    return out


@app.delete("/api/playthroughs/{pt_id}/orgs/{org_id}")
def org_dissolve(pt_id: str, org_id: str, user_id: str = Query(default=""),
                 player: str = Query(default=memory.SOLO)):
    _own(pt_id, user_id)
    try:
        return legacy.dissolve(pt_id, org_id, player)
    except legacy.OrgError as e:
        raise HTTPException(400, str(e))


class Infiltration(BaseModel):
    npc_id: str = Field(min_length=1, max_length=60)


@app.get("/api/playthroughs/{pt_id}/rivals")
def rival_houses(pt_id: str, user_id: str = Query(default=""),
                 player: str = Query(default=memory.SOLO)):
    """Organisations you do not lead - including the ones the WORLD founded
    when a succession left people behind."""
    pt = _own(pt_id, user_id)
    return {"rivals": legacy.rival_orgs(pt_id, engine.world_for(pt), player)}


@app.post("/api/playthroughs/{pt_id}/orgs/{org_id}/infiltrate")
def org_infiltrate(pt_id: str, org_id: str, body: Infiltration,
                   user_id: str = Query(default=""),
                   player: str = Query(default=memory.SOLO)):
    """Place one of yours inside somebody else's house. Never published - an
    infiltration that announced itself would defeat its own purpose."""
    pt = _own(pt_id, user_id)
    try:
        return legacy.infiltrate(pt_id, engine.world_for(pt), org_id=org_id,
                                 npc_id=body.npc_id, player=player,
                                 turn=pt["current_turn"])
    except legacy.OrgError as e:
        raise HTTPException(400, str(e))


@app.get("/api/playthroughs/{pt_id}/monuments")
def monuments(pt_id: str, user_id: str = Query(default=""),
              player: str = Query(default=memory.SOLO)):
    """What the dead left standing. Death is not a reset, and an heir told
    nothing about what the last life built has inherited a number."""
    pt = _own(pt_id, user_id)
    return {"monuments": legacy.monuments(pt_id, engine.world_for(pt), player)}


@app.get("/api/playthroughs/{pt_id}/traitor")
def traitor(pt_id: str, user_id: str = Query(default=""),
            player: str = Query(default=memory.SOLO)):
    """The tease. Never carries a name - that is the entire contract."""
    pt, world, turn = _pt_world(pt_id, user_id)
    return legacy.traitor_signal(pt_id, world, player, turn=turn,
                                 session_id=pt["session_id"])


@app.post("/api/playthroughs/{pt_id}/traitor/reveal")
def traitor_reveal(pt_id: str, user_id: str = Query(default=""),
                   player: str = Query(default=memory.SOLO),
                   force: bool = Query(default=False)):
    """Name them. Refuses below the threshold unless a climax forces it."""
    pt, world, turn = _pt_world(pt_id, user_id)
    out = legacy.reveal_traitor(pt_id, world, player, turn=turn, force=force)
    if out.get("revealed"):
        _publish(pt["session_id"], {"type": "traitor", "reveal": out})
    return out


@app.post("/api/playthroughs/{pt_id}/catch-up")
def surface_reveal(pt_id: str, user_id: str = Query(default=""),
                   player: str = Query(default=memory.SOLO)):
    """Hand back one thing that happened while nobody told them.

    Not /reveal: that path is already taken by the simultaneous split reveal,
    and FastAPI matches the first route it registered - this one was shadowed
    and unreachable, which is a route the client would have called forever
    while quietly getting somebody else's answer."""
    pt, world, turn = _pt_world(pt_id, user_id)
    out = legacy.surface_reveal(pt_id, world, player, turn=turn)
    if not out:
        return {"revealed": False, "note": "Nothing is owed to you right now."}
    _publish(pt["session_id"], {"type": "chronicle", "reveal": out})
    return {"revealed": True, **out}


@app.get("/{path:path}")
def static_files(path: str):
    if path.startswith("api/") or path.startswith("ws/"):
        raise HTTPException(404, "no such endpoint")
    target = (FRONTEND / path).resolve()
    if target.is_file() and str(target).startswith(str(FRONTEND.resolve())):
        return FileResponse(target)
    return FileResponse(FRONTEND / "index.html")
