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
               canon, combat, legibility, research, uploads,
               config, db,
               death, engine, fastforward, identity, mana, memory, modes, narrgraph,
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
    db.init()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class NewPlaythrough(BaseModel):
    user_id: str = Field(min_length=4, max_length=64)
    world_id: str = "emberfall"
    protagonist: str | None = None
    title: str | None = None


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
    pt_id = engine.create_playthrough(body.user_id, body.world_id, body.protagonist, body.title)
    return {"id": pt_id, "state": engine.snapshot(pt_id), "feed": engine.feed(pt_id)}


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
                                tone=body.tone, mode=body.mode)
    except (ValueError, worldkit.WorldError) as e:
        raise HTTPException(400, str(e))
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
                            title=body.title)
    except ValueError as e:
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


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


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
    d = research.dossier(setting, refresh=refresh, depth=depth)
    return {
        "setting": d["setting"], "found": d["found"], "cached": d.get("cached", False),
        "canonical_name": d["canonical_name"], "wiki": d["wiki"],
        "summary": d["summary"][:600],
        "characters": [c["name"] for c in d["characters"][:14]],
        "places": [p["name"] for p in d["places"][:10]],
        "factions": [f["name"] for f in d["factions"][:8]],
        "sources": research.attribution(d),
        "note": d["note"], "enabled": research.enabled(),
    }


@app.get("/{path:path}")
def static_files(path: str):
    if path.startswith("api/") or path.startswith("ws/"):
        raise HTTPException(404, "no such endpoint")
    target = (FRONTEND / path).resolve()
    if target.is_file() and str(target).startswith(str(FRONTEND.resolve())):
        return FileResponse(target)
    return FileResponse(FRONTEND / "index.html")
