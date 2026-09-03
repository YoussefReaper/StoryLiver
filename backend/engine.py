"""Turn orchestration - the pipeline the whole product is.

  action -> canon gate -> World Master validates -> diff applied to shared state
         -> deterministic layers tick (world, atlas, relationships, awareness,
            factions, hunts, graph)  [all $0]
         -> an NPC may act -> the Director may turn the story
         -> Narrator writes ONE passage -> canon gate -> broadcast

Everything between the two gates is arithmetic. The model is called at most
`budget.MAX_LLM_CALLS_PER_TURN` times and only for the six things a model is
actually better at; the cap is enforced in llm.complete, not trusted here.
"""
from __future__ import annotations

import json
import uuid

from . import (arcs, atlas, authority, awareness, betrayal, budget, callbacks, canon,
               config,
               db, director,
               fastforward, identity, legacy, mana, memory, modes, modetree,
               narrator, narrgraph,
               npc_sim, precommit, relationships, rt, runs, streaks, world_master,
               worldstate)
from . import worlds as world_registry


def _pt(pt_id):
    return db.row("SELECT * FROM playthroughs WHERE id=?", (pt_id,))


def world_for(pt):
    return world_registry.resolve(pt["world_id"], pt["world_json"] or None)


def _feed(pt_id, turn, kind, text, actor=None, meta=None):
    return db.run(
        "INSERT INTO narrative (playthrough_id,turn,kind,actor,text,meta,created_at) VALUES (?,?,?,?,?,?,?)",
        (pt_id, turn, kind, actor, text, json.dumps(meta or {}), db.now()),
    )


def _render(pt_id, turn, kind, text, actor=None, meta=None):
    rid = _feed(pt_id, turn, kind, text, actor, meta)
    return {"id": rid, "turn": turn, "kind": kind, "actor": actor, "text": text, "meta": meta or {}}


def ensure_user(user_id):
    if not db.row("SELECT id FROM users WHERE id=?", (user_id,)):
        db.run("INSERT INTO users (id,created_at) VALUES (?,?)", (user_id, db.now()))
    return user_id


def mode_of(pt) -> str:
    """Which of the nineteen sub-modes this world is playing.

    ONE resolver. The alternative is the engine asking the session and the
    client asking the playthrough, and the two disagreeing on a Tuesday. A
    room's setting wins because a room is the stronger statement; a solo world
    carries its own; anything older falls back to the default rather than
    raising at the worst possible moment."""
    if isinstance(pt, str):
        pt = _pt(pt) or {}
    if pt.get("session_id"):
        from . import sessions as _sessions
        return _sessions.type_of(pt["session_id"])
    return modetree.normalise(pt.get("session_type") or "")


def mode_setup(pt_id, world, mode_id, *, turn=0) -> dict:
    """What has to be DONE to a world before its mode is playable.

    A Detective world with no killing in it is one where the player opens the
    casefile, finds nothing, and reasonably concludes the mode is broken. Run
    once at creation, and guarded so it cannot run twice."""
    from . import submodes
    out = {}
    if mode_id == "detective" and not submodes.current_case(pt_id):
        try:
            out["case"] = submodes.open_case(pt_id, world, turn=turn)
        except submodes.SubmodeError as exc:
            # A world too small or too empty for a case is a real answer. Said
            # out loud rather than swallowed, so the client can offer another
            # world instead of showing an empty casefile.
            out["case_error"] = str(exc)
    return out


def create_playthrough(user_id, world_id="emberfall", protagonist=None, title=None,
                       session_id="", world_json=None, session_type="", seed=0):
    world = world_registry.resolve(world_id, world_json)
    ensure_user(user_id)
    pt_id = uuid.uuid4().hex[:12]
    pinned = world_json or ("" if world.id in world_registry.STARTERS_RAW
                            else json.dumps(world.data))
    start = world.get("start_location")
    db.run(
        "INSERT INTO playthroughs (id,user_id,world_id,title,protagonist,current_turn,current_location,"
        "mana_balance,mana_used,tension,last_beat_turn,created_at,updated_at,session_id,world_json,"
        "session_type,seed) VALUES (?,?,?,?,?,0,?,?,0,0.25,-99,?,?,?,?,?,?)",
        (pt_id, user_id, world.id, title or world.name,
         protagonist or world.get("default_protagonist"), start,
         config.STARTING_MANA, db.now(), db.now(), session_id, pinned,
         modetree.normalise(session_type or "") if not session_id else "",
         int(seed or 0)),
    )
    memory.seed(pt_id, world)
    worldstate.seed(pt_id, world)
    atlas.seed(pt_id, world, start)
    canon.safety(pt_id)
    memory.add_event(pt_id, 0, "world", world.get("arrival"),
                     "You are an outsider here, with no claim and no history.",
                     kind="world", importance=4, location=start)
    narrgraph.add(pt_id, 0, "arrival", "You arrive", detail=world.get("arrival"),
                  place_id=start, weight=3)
    # A story IS a run. Opening it here means the run frame is never in a
    # half-created state, and the first thing a player sees already knows
    # which run this is and what the last one left behind.
    runs.start(pt_id, user_id, world_id=world.id)
    # Derive this world's arcs if nobody has yet, so the entry-point picker
    # works on starter worlds too and not only on forged ones.
    arcs.ensure_timeline(world)
    # C4 - the town already has an opinion of you, if you have an account and
    # a history here. A guest starts clean every time, which is the honest
    # trade for not having an identity that can be verified.
    # Kept, not discarded. A town that already has an opinion of you is the
    # payoff for having played here before, and it was computed and dropped.
    remembered = authority.seed_from_memory(pt_id, world, account_id=user_id,
                                            player=memory.SOLO)
    if remembered.get("seeded"):
        db.run("INSERT INTO prefs (user_id,data,updated_at) VALUES (?,?,?)"
               " ON CONFLICT(user_id) DO UPDATE SET data=excluded.data,"
               " updated_at=excluded.updated_at",
               (f"returned:{pt_id}", json.dumps(remembered), db.now()))
    _feed(pt_id, 0, "opening", world.get("opening") or world.get("premise"), actor=None,
          meta={"location": start})
    # The world dials this mode implies. Applied once, here, so Ironman
    # actually IS one life rather than a label over a world where death still
    # has resolutions. They stay editable - a mode sets a starting position.
    resolved_mode = modetree.normalise(session_type or "") if session_type else ""
    if resolved_mode:
        dials = modetree.dials_for(resolved_mode)
        if dials:
            modes.set_modes(pt_id, dials)

    # Whatever this mode needs staged before it is playable. Solo modes only:
    # a room stages itself when the host sets the table.
    if session_type and not session_id:
        mode_setup(pt_id, world, resolved_mode, turn=0)
    return pt_id


# --------------------------------------------------------------------------

def _apply_fate(pt, world, turn, entries):
    fired = []
    # A Sandbox promises no main quest. Firing the seven fated events on
    # schedule anyway makes it a Story with the label filed off.
    if modetree.suppresses_fate(mode_of(pt)):
        return fired
    for f in world.fated_events:
        if f["turn"] != turn:
            continue
        memory.add_event(pt["id"], turn, "fate", f["title"], f["desc"],
                         rule_ref="fate", kind="fate", importance=5, location=f["location"])
        npc_sim.observe_fate(pt["id"], turn, f)
        narrgraph.add(pt["id"], turn, "fate", f["title"], detail=f["desc"],
                      place_id=f.get("location", ""), weight=5)
        if f.get("kills"):
            # This used to be the ONLY thing a fated death did: flip a flag.
            # No grief, no standing, no empty seat - so the most dramatic
            # deaths in the game were the ones the world reacted to least.
            # Routed through the same consequence path a fought death takes.
            # Not through death.strike(): a Cozy world converts a death into a
            # knockout there, and fate is by definition not negotiable.
            from . import death as _death
            memory.kill_npc(pt["id"], f["kills"])
            fate_consequences = _death.world_event(
                pt["id"], who=f["kills"], killer="", world=world)
            f["consequences"] = fate_consequences
        if f.get("ruins"):
            atlas.ripple(pt["id"], f["ruins"], f.get("condition", "ruined"), turn,
                         f["desc"], magnitude=5)
        else:
            atlas.echo(pt["id"], turn, f["desc"], place_id=f.get("location", ""),
                       kind="fate", magnitude=5)
        entries.append(_render(pt["id"], turn, "fate", f["desc"],
                               meta={"fate_id": f["id"], "title": f["title"], "immutable": True}))
        fired.append(f)
    return fired


def _party_context(session_id, exclude=None):
    if not session_id:
        return [], []
    rows = db.rows("SELECT * FROM session_players WHERE session_id=? AND role!='spectator'",
                   (session_id,))
    party = [{"player_id": r["player_id"], "name": r["name"], "goal": r["goal"]}
             for r in rows if r["player_id"] != exclude]
    payers = [r["user_id"] for r in rows if r["pays"]]
    return party, payers


def _player_places(pt_id, session_id, default_place):
    """Where every player currently is - one shared location today, but the hunt
    layer asks per player so split parties already work."""
    places = {}
    rows = db.rows("SELECT player_id FROM session_players WHERE session_id=?", (session_id,)) \
        if session_id else [{"player_id": memory.SOLO}]
    for r in rows:
        split = betrayal.active(pt_id, r["player_id"])
        places[r["player_id"]] = (split["destination"] if split and split["destination"]
                                  else default_place)
    return places


# --------------------------------------------------------------------------
# The deterministic tick - every layer, every turn, at zero model cost
# --------------------------------------------------------------------------

def _deterministic_tick(pt, world, *, turn, player, actor_name, action, verdict,
                        state, session_id, moved_to):
    out = {"relationship": [], "witness": None, "rumours": [], "reputation": [],
           "hunts": [], "arrivals": [], "discovered": None, "world": None}

    out["world"] = worldstate.tick(pt["id"], world, turn,
                                   events=[verdict.get("kind", "action")])

    if moved_to:
        first = atlas.discover(pt["id"], moved_to, turn)
        for neighbour in world.connects(moved_to):
            atlas.rumour(pt["id"], neighbour, turn)
        if first:
            out["discovered"] = {"place": moved_to, "name": world.loc_name(moved_to)}
            narrgraph.add(pt["id"], turn, "discovery", f"Found {world.loc_name(moved_to)}",
                          place_id=moved_to, weight=3)
            atlas.echo(pt["id"], turn, f"You found {world.loc_name(moved_to)}.",
                       place_id=moved_to, kind="discovery", magnitude=2)

    # A promise is the single highest-value thing a world can remember you for,
    # and the timeline had no way to mark one - it filed "I swear I'll come back"
    # as an ordinary action and scored it like one. Recorded as its own kind so
    # it stays reachable long after the moment.
    if callbacks.looks_like_promise(action):
        callbacks.note_promise(pt["id"], turn, player, action, location=pt["current_location"])

    # Relationships move from a deterministic read of what was done, TO WHOM.
    event = relationships.classify(action)
    aimed_at = relationships.targets(world, action, state["present"])
    # How hard this table asked consequences to land. It scales what a mistake
    # COSTS and nothing else - a gentle table is not a table where people stop
    # noticing, and it never touches a gain, because a world that forgives
    # slowly must not also reward you faster.
    harsh = modes.severity_mult(pt["id"])
    if event and aimed_at:
        weight = harsh if relationships.is_harmful(event) else 1.0
        # The node this turn wrote, so a character who drifts because of it can
        # be shown the exact moment rather than a number. Written before the
        # deltas so the ledger has something real to point at.
        cause_node = 0
        if relationships.is_harmful(event) or relationships.is_bonding(event):
            cause_node = narrgraph.add(
                pt["id"], turn,
                "rupture" if relationships.is_harmful(event) else "bond",
                f"{actor_name or 'You'}: {action[:80]}",
                detail=verdict.get("consequence", "")[:200],
                place_id=pt["current_location"], actor=player, weight=4)
        for npc_id in aimed_at[:3]:
            applied = relationships.apply_event(pt["id"], npc_id, player, event,
                                                turn=turn, weight=weight,
                                                note=event.replace("_", " "),
                                                cause_node=cause_node)
            if applied:
                out["relationship"].append(applied)
    out["targets"] = aimed_at
    relationships.decay(pt["id"], turn)

    # Witness -> rumour -> reputation -> hunt. Nobody learns what they did not see.
    severity = int(verdict.get("importance", 3))
    # Deliberately NOT used for the witness roll or for the >=3 gates below:
    # the slider changes what a thing costs, never whether it was seen. Two
    # tables watching the same action see the same thing happen.
    cost_sev = max(1, min(5, round(severity * harsh)))
    if severity >= 3 and event not in (None, "spoke_kindly", "listened"):
        fact = awareness.witness(
            pt["id"], world, actor=player, kind=event or "action",
            summary=f"{actor_name or 'A traveller'}: {action[:120]}",
            detail=verdict.get("consequence", "")[:200],
            place_id=pt["current_location"], turn=turn, severity=severity,
            present=state["present"], subject=player)
        out["witness"] = fact
        # Being SEEN doing harm has to cost something to the people who saw
        # it, not just to whoever it landed on - otherwise a witness's
        # scalars (which will_snitch/betrayal_pressure read) never react to
        # what they watched, and a room can watch a beating and feel nothing.
        if fact["witnesses"] and relationships.is_harmful(event or ""):
            for wit_id in fact["witnesses"]:
                if wit_id in aimed_at:
                    continue  # the target already got the real event, harsher
                applied = relationships.apply_event(
                    pt["id"], wit_id, player, "witnessed_violence",
                    turn=turn, weight=harsh, note="witnessed it")
                if applied:
                    out["relationship"].append(applied)
        if fact["witnesses"]:
            out["rumours"] = awareness.spread(pt["id"], world, fact, turn=turn,
                                              witnesses=fact["witnesses"],
                                              severity=cost_sev)
            valence = -1 if event in ("harmed", "threatened", "betrayed", "took_from",
                                      "insulted", "killed_ally_of") else 1
            out["reputation"] = awareness.adjust_rep(pt["id"], world, player=player,
                                                     fact=fact, turn=turn, valence=valence)
            if valence < 0:
                out["hunts"] = awareness.maybe_order_hunt(
                    pt["id"], world, player=player, fact=fact, turn=turn,
                    reps=out["reputation"])

    out["arrivals"] = awareness.arrivals(pt["id"], world, turn)
    out["hunt_events"] = awareness.hunt_tick(
        pt["id"], world, turn,
        player_places=_player_places(pt["id"], session_id, pt["current_location"]))

    # A hunt that arrives where you are is something you now know about.
    for h in out["hunt_events"]:
        if h["kind"] == "arrived":
            awareness.reveal_hunt_to_player(
                pt["id"], h["id"], player, turn,
                f"{world.npc_name(h['hunter'])} came looking for you.")
            narrgraph.add(pt["id"], turn, "twist", "They found you",
                          detail=h.get("reason", ""), place_id=h["to_place"], weight=4)
    # A rumour landing where you are is how you hear you are hunted.
    for a in out["arrivals"]:
        if a["place"] == pt["current_location"] and a["severity"] >= 3:
            awareness.learn(pt["id"], "player", player, key=a["key"], summary=a["summary"],
                            turn=turn, confidence=a["confidence"], source="overheard",
                            severity=a["severity"])

    # The institution. An officer on patrol perceives through the same witness
    # math as anyone else - no privileged sight - and a crime an authority
    # actually KNOWS about moves the warrant ladder. Standing has already been
    # adjusted above, so this reads the result rather than punishing twice.
    out["authority"] = []
    if severity >= 3 and event in ("harmed", "threatened", "betrayed", "took_from",
                                   "killed_ally_of", "insulted"):
        seen = authority.patrol_tick(
            pt["id"], world, turn, place_id=pt["current_location"], actor=player,
            summary=f"{actor_name or 'Someone'}: {action[:100]}", severity=severity)
        for hit in seen["witnessed_by_authority"]:
            out["authority"].append(authority.register_crime(
                pt["id"], world, player=player, faction_id=hit["faction"],
                severity=cost_sev, summary=action[:140], turn=turn))

    # Institutions forget slowly, but they do forget - otherwise one bad turn
    # is a life sentence, which reads as the world being broken, not strict.
    out["authority_decay"] = authority.decay(pt["id"], world, player, turn)

    # Layer 6, and it runs LAST on purpose: beliefs form out of facts that have
    # already been distributed this turn, so a character reacts to what they
    # just saw rather than to what they will see next turn.
    out["legacy"] = legacy.tick(pt["id"], world, turn, player=player)
    return out


# --------------------------------------------------------------------------

def take_turn(pt_id, action, *, premium=False, player=memory.SOLO, actor_name=None,
              session_id=""):
    pt = _pt(pt_id)
    if not pt:
        raise KeyError("playthrough not found")
    world = world_for(pt)
    user_id = pt["user_id"]
    action = (action or "").strip()
    if not action:
        raise ValueError("empty action")

    session_id = session_id or pt["session_id"]

    # What KIND of game this is decides two things before anything else runs:
    # whether this verb is allowed at all, and whether it is even this
    # player's turn. Both are enforced HERE rather than at each entry point,
    # so the HTTP path and the socket path cannot drift apart on the answer.
    mode_id = mode_of(pt)

    # Guard layer 1, and it is checked for SOLO worlds too. It used to sit
    # inside the room branch, so a talk-only mode played alone was not
    # talk-only at all.
    verdict_action = modetree.check_action(mode_id, action)
    if not verdict_action["allowed"]:
        return {"blocked": True, "reason": verdict_action["reason"],
                "refused": verdict_action["kind"], "entries": [],
                "state": snapshot(pt_id, player)}

    if session_id:
        from . import sessions as _sessions

        # Guard layer 1. A talk-only room refuses the verb at the parser, not
        # in a prompt: a model told not to narrate combat will eventually
        # narrate combat, and a parser that rejects the input cannot.
        whose = _sessions.may_act(session_id, player, pt["current_turn"])
        if not whose["ok"]:
            return {"blocked": True,
                    "reason": f"Not your turn - {whose.get('reason', 'wait for the table')}.",
                    "whose_turn": whose.get("whose"), "entries": [],
                    "state": snapshot(pt_id, player)}

    party, payers = _party_context(session_id, exclude=player)
    payer_ids = payers or [user_id]

    # §8 - Deep Prose is a ROOM setting the host owns, never a per-action flag
    # any player can fire. The narrator is shared across the table, so premium
    # was always all-or-nothing; leaving it per-action just meant a friend
    # could spend the host's Mana at 4x. In a room, the host's flag decides.
    # Solo (no session) keeps the caller's choice - it is the player's own
    # wallet either way.
    if session_id:
        from . import sessions          # local: sessions imports engine
        premium = sessions.premium_allowed(session_id)
        seat = db.row("SELECT life_state, resolution FROM session_players"
                      " WHERE session_id=? AND player_id=?", (session_id, player))
        # Death has resolutions, and some of them (ghost, spirit) keep a voice
        # without a hand. Those players whisper; they do not take turns.
        if seat and seat["life_state"] == "dead":
            return {"blocked": True, "dead": True,
                    "reason": "Your character is gone. You can still whisper - "
                              "or take one of the resolutions offered.",
                    "resolution": seat["resolution"], "entries": [],
                    "state": snapshot(pt_id, player)}

    mode, note = mana.preview(user_id, pt, premium, payer_ids)
    if mode == mana.RAIL:
        return {"blocked": True, "reason": note, "entries": [], "state": snapshot(pt_id, player)}
    use_premium = premium and mode == mana.FULL

    entries = []
    with budget.turn(f"turn:{pt_id}:{pt['current_turn'] + 1}"):
        # 0. The table's Lines outrank everything, including what the player typed.
        try:
            canon.safety_gate(pt_id, action)
        except canon.Cancelled as c:
            canon.log(pt_id, turn=pt["current_turn"], actor=player, attempted=action,
                      verdict="cancelled", rule=c.rule, layer="safety")
            entries.append(_render(pt_id, pt["current_turn"], "safety",
                                   "That is a line this table drew. The scene moves on.",
                                   actor="table", meta={"rule": c.rule, "layer": "safety"}))
            return {"blocked": False, "rejected": True, "safety": True, "note": "",
                    "entries": entries, "state": snapshot(pt_id, player)}

        # 1. World Master ---------------------------------------------------
        verdict = world_master.validate(pt, world, action, user_id=user_id,
                                        player=player, party=party)
        if not verdict["valid"]:
            entries.append(_render(pt_id, pt["current_turn"], "you", action, actor=player,
                                   meta={"name": actor_name or "You"}))
            text = narrator.refusal(pt, world, action, verdict, user_id=user_id)
            memory.add_event(pt_id, pt["current_turn"], actor_name or "user", action,
                             f"Refused: {verdict['reason']}", rule_ref=verdict.get("rule_ref"),
                             kind="rejection", importance=2, location=pt["current_location"])
            entries.append(_render(pt_id, pt["current_turn"], "refusal", text, actor="world",
                                   meta={"reason": verdict["reason"],
                                         "rule_ref": verdict.get("rule_ref"),
                                         "checked_by": verdict.get("checked_by"),
                                         "player": player}))
            return {"blocked": False, "rejected": True, "note": note, "entries": entries,
                    "state": snapshot(pt_id, player)}

        # 2. Commit ---------------------------------------------------------
        mana.commit(user_id, pt, use_premium, payer_ids)
        turn = pt["current_turn"] + 1
        new_loc = verdict.get("new_location") or pt["current_location"]
        moved_to = None
        if new_loc != pt["current_location"]:
            if new_loc in world.connects(pt["current_location"]) and not atlas.blocked(pt_id, new_loc):
                moved_to = new_loc
            else:
                new_loc = pt["current_location"]
        db.run("UPDATE playthroughs SET current_turn=?, current_location=?, updated_at=? WHERE id=?",
               (turn, new_loc, db.now(), pt_id))
        rt.cache_drop(f"sl:pt:{pt_id}:snapshot")
        pt = _pt(pt_id)
        entries.append(_render(pt_id, turn, "you", action, actor=player,
                               meta={"name": actor_name or "You"}))

        applied = memory.apply_deltas(pt_id, verdict["relationship_deltas"], turn, player)
        memory.add_event(pt_id, turn, actor_name or "user", action, verdict["consequence"],
                         rule_ref=verdict.get("rule_ref"), kind="action",
                         importance=verdict.get("importance", 3), location=new_loc)
        # A turn taken while SPLIT OFF is private, and the Chronicle reads the
        # graph. An ungated node here would have handed every other player at
        # the table exactly what betrayal.py exists to keep from them - the
        # whole point of departing is that nobody sees what you did until the
        # reveal. Gated with the same private key the knowledge store uses, so
        # only this player's chronicle can resolve it.
        away = betrayal.active(pt_id, player)
        action_key = betrayal.private_key(player, turn, new_loc) if away else ""
        narrgraph.add(pt_id, turn, "action", action[:90], detail=verdict.get("consequence", ""),
                      place_id=new_loc, actor=actor_name or "you",
                      weight=verdict.get("importance", 2), fact_key=action_key)

        state = world_master.build_state(pt, world, player)
        npc_sim.observe_turn(pt_id, turn, state["present"], action, verdict["consequence"],
                             verdict.get("importance", 3), player=player,
                             actor_name=actor_name or "the traveller")
        fired_fate = _apply_fate(pt, world, turn, entries)
        state = world_master.build_state(_pt(pt_id), world, player)

        # 3. Every deterministic layer, $0 -----------------------------------
        ticked = _deterministic_tick(
            pt, world, turn=turn, player=player, actor_name=actor_name, action=action,
            verdict=verdict, state=state, session_id=session_id, moved_to=moved_to)

        # A private turn while split off does not enter the shared record.
        split_progress = betrayal.note_private_turn(pt_id, player)
        if split_progress:
            betrayal.record_private(pt_id, player_id=player, turn=turn,
                                    summary=action[:160], detail=verdict.get("consequence", ""),
                                    place_id=new_loc)

        # 4. NPC agency - a model call only when the numbers say it matters ---
        npc_action = None
        if mode == mana.FULL and not fired_fate and budget.affordable("npc"):
            actor_id = npc_sim.pick_actor(pt, state, player)
            if actor_id:
                npc_action = npc_sim.maybe_act(pt, world, actor_id, user_id=user_id,
                                               player=player,
                                               actor_name=actor_name or "the traveller")
                if npc_action:
                    try:
                        canon.check_npc_action(pt_id, world, actor_id, npc_action["action"],
                                               turn=turn, state=state)
                    except canon.Cancelled as c:
                        canon.log(pt_id, turn=turn, actor=actor_id,
                                  attempted=npc_action["action"], verdict="cancelled",
                                  rule=c.rule, layer=c.layer)
                        npc_action = None

        # 5. Director --------------------------------------------------------
        beat, scores = (None, director.tension_score(pt, world, state, player))
        if mode == mana.FULL and not fired_fate and not npc_action and budget.affordable("director"):
            beat, scores = director.propose(pt, world, state, user_id=user_id, player=player)

        # 6. One passage ------------------------------------------------------
        verdict["state"] = state
        verdict["world_line"] = worldstate.line(pt_id, world, new_loc)
        # What the WORLD did this turn, independent of the player. A succession
        # that settles while the player is standing in the room and goes
        # unmentioned reads as the world not being there at all.
        verdict["legacy_line"] = legacy.narrate_line(
            world, ticked.get("legacy") or {}, player=player)
        try:
            text = narrator.narrate(
                pt, world, action, verdict, user_id=user_id, premium=use_premium,
                beat=(beat or {}).get("beat"),
                npc_action=(npc_action or {}).get("action"),
                player=player, actor_name=actor_name if session_id else None)
            text = canon.check_prose(pt_id, world, text, turn=turn)
        except canon.Cancelled:
            text = (f"{verdict.get('consequence') or action.strip()} "
                    f"{worldstate.line(pt_id, world, new_loc).capitalize()}.")

        entries.append(_render(pt_id, turn, "narration", text, actor="narrator", meta={
            "premium": use_premium, "mode": mode, "player": player,
            "actor_name": actor_name,
            "relationship_changes": applied,
            "relationship_events": ticked["relationship"],
            "npc_initiated": ({"npc": npc_action["npc"], "name": npc_action["name"],
                               "action": npc_action["action"]} if npc_action else None),
            "director_beat": ({"kind": beat["kind"], "beat": beat["beat"], "npc": beat.get("npc")}
                              if beat else None),
            "tension": scores["tension"],
            "world": {"phase": ticked["world"]["phase"], "weather": ticked["world"]["weather"],
                      "day": ticked["world"]["day"]},
            "discovered": ticked["discovered"],
            "unseen": bool(ticked["witness"] and ticked["witness"]["unseen"]),
            "reputation": ticked["reputation"],
            "hunts_ordered": ticked["hunts"],
            "llm_calls": len(budget.used()),
        }))

        if npc_action:
            memory.add_event(pt_id, turn, npc_action["name"], npc_action["action"],
                             "Acted without being asked.", kind="npc",
                             importance=npc_action["importance"], location=new_loc)
            memory.apply_deltas(pt_id, [npc_action["delta"]], turn, player)
            memory.npc_observe(pt_id, npc_action["npc"], turn,
                               f"I made a move on {actor_name or 'the traveller'}: "
                               f"{npc_action['action']}", importance=4, player=player)
            narrgraph.add(pt_id, turn, "bond", f"{npc_action['name']} moved first",
                          detail=npc_action["action"], place_id=new_loc, weight=3)
        if beat:
            memory.add_event(pt_id, turn, "director", f"[{beat['kind']}] {beat['beat']}",
                             "The story turned without anyone choosing it.",
                             kind="beat", importance=4, location=new_loc)
            db.run("UPDATE playthroughs SET last_beat_turn=?, tension=? WHERE id=?",
                   (turn, float(beat.get("tension", scores["tension"])), pt_id))
            narrgraph.add(pt_id, turn, "beat", beat["beat"][:90], detail=beat["kind"],
                          place_id=new_loc, weight=4)

        # 7. One reflection, cheapest model ----------------------------------
        if mode == mana.FULL and state["present"] and budget.affordable("npc"):
            who = state["present"][turn % len(state["present"])]
            npc_sim.reflect(_pt(pt_id), world, who, user_id=user_id, player=player)

    streaks.touch(user_id)

    # A story that has passed its last fated event is over. Settling the run
    # HERE, exactly once, is what turns "the turn counter kept going" into an
    # actual ending with a payout - and the `closed` flag is idempotent, so a
    # story cannot pay out twice.
    ending = None
    if (world.fated_events and turn >= world.fated_events[-1]["turn"]
            and not modetree.suppresses_fate(mode_id)):
        row = _pt(pt_id)
        if row["run_state"] != "ended":
            from . import aftermath
            ending = aftermath.close_story(pt_id, world, reason="victory", turn=turn)
            # A DISPOSABLE world is torn down when it is decided. The rows stay
            # - a player who just lost is owed the ability to read it back -
            # but the room closes and the next session seeds a new world that
            # reads none of this. That is the whole competitive guarantee: no
            # first-mover advantage carried in from a previous match.
            if session_id and modetree.is_disposable(mode_id):
                from . import sessions as _sessions
                ending["settled"] = _sessions.settle(
                    session_id, outcome="objective", winner=player)

    return {"blocked": False, "rejected": False, "note": note, "entries": entries,
            "ending": ending,
            "state": snapshot(pt_id, player), "tick": {
                "discovered": ticked["discovered"], "arrivals": ticked["arrivals"],
                "reputation": ticked["reputation"], "hunts": ticked["hunts"],
                "relationship": ticked["relationship"],
                # Layer 6. The climax reveal fires inside the tick, so without
                # this the biggest beat in the campaign would reach the prose
                # and nothing else - no modal, no notification, nothing the
                # player could go back and read.
                "legacy": ticked.get("legacy") or {}}}


# --------------------------------------------------------------------------

def whisper(pt_id, *, player, target_kind, target_id, text, actor_name=None, session_id=""):
    pt = _pt(pt_id)
    world = world_for(pt)
    text = (text or "").strip()
    if not text:
        raise ValueError("empty whisper")
    canon.safety_gate(pt_id, text)

    row_id = db.run(
        "INSERT INTO whispers (session_id,playthrough_id,turn,from_player,to_kind,to_id,text,created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (session_id or pt["session_id"], pt_id, pt["current_turn"], player,
         target_kind, target_id, text, db.now()))

    if target_kind == "player":
        return {"id": row_id, "kind": "player", "to": target_id, "text": text, "reply": None}

    if target_id not in world.by_id:
        raise ValueError("no such character")
    st = memory.npc_state(pt_id, target_id)
    if not st or not st["alive"]:
        return {"id": row_id, "kind": "npc", "to": target_id, "text": text,
                "reply": f"{world.npc_name(target_id)} is dead. The dead do not answer."}

    with budget.turn(f"whisper:{pt_id}", limit=2):
        out = npc_sim.whisper(pt, world, target_id, text, user_id=pt["user_id"],
                              player=player, speaker=actor_name or "the traveller")
        try:
            out["reply"] = canon.check_npc_action(pt_id, world, target_id, out["reply"],
                                                  turn=pt["current_turn"])
        except canon.Cancelled as c:
            canon.log(pt_id, turn=pt["current_turn"], actor=target_id,
                      attempted=out["reply"], verdict="cancelled", rule=c.rule, layer=c.layer)
            out["reply"] = f"{world.npc_name(target_id)} looks at you and says nothing at all."

    # Telling someone a secret privately is a deterministic relationship event.
    relationships.apply_event(pt_id, target_id, player, "shared_secret",
                              turn=pt["current_turn"], note="told them privately")
    db.run("UPDATE whispers SET reply=? WHERE id=?", (out["reply"], row_id))
    return {"id": row_id, "kind": "npc", "to": target_id,
            "name": world.npc_name(target_id), "text": text,
            "reply": out["reply"], "delta": out["delta"]}


def contest(pt_id, challenger, defender, *, session_id=""):
    pt = _pt(pt_id)
    world = world_for(pt)
    with budget.turn(f"contest:{pt_id}", limit=2):
        result = world_master.arbitrate(pt, world, {"challenger": challenger, "defender": defender},
                                        user_id=pt["user_id"])
    turn = pt["current_turn"]
    winner = result["winner"]
    win_side = challenger if challenger["player_id"] == winner else defender
    lose_side = defender if win_side is challenger else challenger

    memory.add_event(pt_id, turn, "contest",
                     f"{win_side['name']} vs {lose_side['name']}: {win_side['action']}",
                     f"{result['consequence']} {result['loser_cost']}",
                     kind="contest", importance=5, location=pt["current_location"])
    narrgraph.add(pt_id, turn, "contest", f"{win_side['name']} over {lose_side['name']}",
                  detail=result["consequence"], place_id=pt["current_location"], weight=5)
    entry = _render(pt_id, turn, "contest", result["consequence"], actor="world", meta={
        "winner": winner, "winner_name": win_side["name"], "loser_name": lose_side["name"],
        "reason": result["reason"], "loser_cost": result["loser_cost"],
        "rolls": result["rolls"],
    })
    return {"result": result, "entries": [entry], "state": snapshot(pt_id, winner)}


# --------------------------------------------------------------------------

def _returned(pt_id) -> dict:
    """What a previous run left behind here, for the client to open with.

    Only ever non-empty for an authenticated account with history in this
    world - a guest starts clean every time, which is the honest trade for
    not having an identity that can be checked."""
    row = db.row("SELECT data FROM prefs WHERE user_id=?", (f"returned:{pt_id}",))
    return db.jload(row["data"], {}) if row else {}


def _fate_entry(f, turn, next_turn) -> dict:
    """One line of the Fate Thread, redacted to what the player may know.

    Fate being FIXED is the promise; fate being READABLE was never part of it.
    A passed event is history and is named. Everything ahead is a mark on the
    thread with a turn on it - you know something lands, and when, and that
    you cannot stop it. You do not know what.

    `kills` never crosses this line at any status: it names the character who
    dies, and it was going out on turn one."""
    status = ("passed" if f["turn"] <= turn
              else ("next" if f["turn"] == next_turn else "sealed"))
    # The id is a SLUG - "F5_the_warden_falls" - so it carries the title it was
    # meant to hide. An unreached event gets a positional id instead, which is
    # all a client needs to key a list row on.
    entry = {"id": f["id"] if status == "passed" else f"sealed_{f['turn']}",
             "turn": f["turn"], "status": status}
    if status == "passed":
        entry["title"] = f["title"]
        entry["desc"] = f.get("desc", "")
        entry["location"] = f.get("location", "")
        return entry
    # Ahead of the player. Deliberately no title, no description, no location -
    # a place name is a spoiler too when only one thing happens there.
    entry["title"] = ""
    entry["desc"] = ""
    entry["imminent"] = status == "next"
    return entry


def snapshot(pt_id, player=memory.SOLO):
    pt = _pt(pt_id)
    world = world_for(pt)
    turn = pt["current_turn"]
    states = {s["npc_id"]: s for s in memory.all_npc_states(pt_id)}
    present = memory.npcs_at(pt_id, world, pt["current_location"], turn)
    rels = relationships.all_for(pt_id, player)

    npcs = []
    for npc in world.npcs:
        st = states.get(npc["id"], {})
        r = rels.get(npc["id"], {})
        ps = memory.npc_player_state(pt_id, npc["id"], player)
        refl = db.jload(ps["reflections"], [])
        loc = st.get("location", npc["start_location"])
        npcs.append({
            "id": npc["id"], "name": npc["name"], "role": npc["role"],
            "voice": npc["anchors"]["voice"],
            "goals": npc["anchors"]["goals"], "taboos": npc["anchors"]["taboos"],
            "constraints": npc["anchors"]["constraints"],
            "alive": bool(st.get("alive", 1)),
            "location": loc, "location_name": world.loc_name(loc),
            "present": npc["id"] in present,
            "affinity": r.get("affinity", 0), "trust": r.get("trust", 0),
            "fear": r.get("fear", 0), "obligation": r.get("obligation", 0),
            "love": r.get("love", 0), "loyalty": r.get("loyalty", 0),
            "respect": r.get("respect", 0),
            "disposition": r.get("disposition", "stranger"),
            "will_cover": r.get("will_cover", False),
            "betrayal_pressure": r.get("betrayal_pressure", 0.0),
            "last_interaction_turn": r.get("last_interaction_turn", -1),
            "plan": db.jload(ps["plan"], []),
            "latest_reflection": refl[-1]["text"] if refl else None,
            "reflection_count": len(refl),
        })

    next_turn = min((x["turn"] for x in world.fated_events if x["turn"] > turn), default=-1)
    fate = [_fate_entry(f, turn, next_turn) for f in world.fated_events]

    scores = director.tension_score(pt, world, {"turn": turn, "present": present}, player)
    session = db.row("SELECT * FROM sessions WHERE playthrough_id=?", (pt_id,))
    payers = None
    if session:
        _, payers = _party_context(session["id"])

    return {
        "id": pt["id"],
        "world": world.summary(),
        "title": pt["title"], "protagonist": pt["protagonist"],
        "turn": turn, "day": world.day_for(turn), "phase": world.phase_for(turn),
        "location": pt["current_location"],
        "location_name": world.loc_name(pt["current_location"]),
        "location_desc": world.loc_by_id[pt["current_location"]]["desc"],
        "exits": [{"id": e, "name": world.loc_name(e),
                   "blocked": atlas.blocked(pt_id, e)}
                  for e in world.connects(pt["current_location"])],
        "npcs": npcs, "fate": fate, "tension": scores["tension"],
        "mana": mana.status(pt["user_id"], pt, payers or [pt["user_id"]]),
        "rules": world.rules,
        "player": player,
        "weather": worldstate.public(pt_id, world, pt["current_location"]),
        # The snapshot carried the ROOM mode (co-op / chaos) and not the
        # sub-mode, so a client in a Duel was told it was in a co-op room -
        # and therefore could not tell the player the one thing that matters
        # most about a PvP world: that it ends when the match does.
        "session": ({"id": session["id"], "code": session["code"], "mode": session["mode"],
                     "status": session["status"],
                     "session_type": mode_of(pt),
                     "mode_spec": modetree.public(mode_of(pt)),
                     "seats": modetree.seat_order(db.rows(
                         "SELECT player_id, role, joined_at, left_at FROM session_players"
                         " WHERE session_id=?", (session["id"],))),
                     "premium_allowed": bool(session.get("premium_allowed", 0))}
                    if session else None),
        # The dials and the run frame travel with every snapshot. Without
        # these the client has no way to know what kind of world it is in -
        # whether death is permanent, which run this is, or whether Deep Prose
        # is even available - and every panel that shows them would be guessing.
        "modes": modes.public(pt_id),
        # What this town already believed about you when you walked in.
        "returned": _returned(pt_id),
        # What KIND of game this is, at the top level rather than nested under
        # `session` - a solo world has no session row, so a solo Detective was
        # unable to tell its own client what it was.
        "mode": modetree.public(mode_of(pt)),
        "run": runs.public(pt_id, pt["user_id"], pt["world_id"]),
        "arc": arcs.position(pt_id, pt["world_id"]),
        "skills": fastforward.skills(pt_id, player),
        "combat_id": (db.row("SELECT id FROM combats WHERE playthrough_id=? AND status!='over'"
                             " ORDER BY rowid DESC LIMIT 1", (pt_id,)) or {}).get("id"),
        "ended": turn >= world.fated_events[-1]["turn"],
    }


def workspace(pt_id, player=memory.SOLO, *, session_id=""):
    """Everything the visual shell needs, in one round trip."""
    pt = _pt(pt_id)
    world = world_for(pt)
    turn = pt["current_turn"]
    party, _ = _party_context(session_id or pt["session_id"])
    return {
        "state": snapshot(pt_id, player),
        "atlas": atlas.view(pt_id, world, here=pt["current_location"], turn=turn),
        "graph": narrgraph.view(pt_id, world, turn),
        "knowledge": awareness.public_state(pt_id, world, player, turn),
        "board": betrayal.board(pt_id, world, player, turn, party),
        "safety": canon.safety(pt_id),
        "budget": budget.table(),
        # Layer 6 travels with the workspace so the Chronicle badge, the drift
        # list and the Power panel are populated on open rather than after a
        # second round trip - all of it already witness-gated to this player.
        "legacy": legacy.public(pt_id, world, player, turn=turn),
    }


def feed(pt_id, after_id=0, limit=200):
    return db.rows(
        "SELECT * FROM narrative WHERE playthrough_id=? AND id>? ORDER BY id LIMIT ?",
        (pt_id, after_id, limit))


def export(pt_id):
    pt = _pt(pt_id)
    world = world_for(pt)
    tables = ("timeline_events", "relationships", "npc_state", "npc_player", "npc_memories",
              "narrative", "whispers", "atlas_places", "world_echoes", "graph_nodes",
              "graph_edges", "faction_rep", "knowledge", "rumors", "hunts", "splits",
              "canon_log", "cards",
              # Layer 6 and the OOC channel. "Export everything" has to mean
              # everything, or the promise is a smaller one than it sounds.
              "orgs", "org_members", "claimants", "ooc_messages")
    out = {
        "storyliver_export_version": 3,
        "exported_at": db.now(),
        "playthrough": pt,
        "world_id": pt["world_id"],
        "world": world.data,
        "snapshot": snapshot(pt_id),
        "world_state": worldstate.get(pt_id),
        "safety": canon.safety(pt_id),
        "usage": db.rows("SELECT role,model,in_tokens,out_tokens,usd,created_at FROM usage_log"
                         " WHERE playthrough_id=?", (pt_id,)),
    }
    for t in tables:
        out[t] = db.rows(f"SELECT * FROM {t} WHERE playthrough_id=?", (pt_id,))
    return out


def usage_summary(pt_id):
    rows = db.rows(
        "SELECT role, model, COUNT(*) n, SUM(in_tokens) tin, SUM(out_tokens) tout, SUM(usd) usd"
        " FROM usage_log WHERE playthrough_id=? GROUP BY role, model", (pt_id,))
    pt = _pt(pt_id)
    actions = max(1, pt["current_turn"])
    total = sum(r["usd"] or 0 for r in rows)
    return {"by_role": rows, "total_usd": round(total, 6), "actions": actions,
            "usd_per_action": round(total / actions, 6),
            "target_usd_per_action": config.TARGET_BLENDED_COST_USD,
            "within_budget": (total / actions) <= config.TARGET_BLENDED_COST_USD,
            "calls_per_turn_cap": budget.MAX_LLM_CALLS_PER_TURN}
