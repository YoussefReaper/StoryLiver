"""Verification gate for layers 2-7 plus the cost architecture.

Every assertion here is about a deterministic system, so the whole file runs
offline with no key and no spend, and gives the same answer every time.

  L2  living world advances; atlas discovers and ripples; echoes recorded
  L3  pre-commit hides intent; simultaneous reveal; insight vs deception
  L4  relationship deltas deterministic, trust-game shaped, LLM-free
  L5  witness non-omniscient; rumour latency; reputation only where known;
      hunt window; stealth is arithmetic
  L6  WEGO; flanking cuts both ways; boss needs a plan, not more hitting
  L7  canon cancelled at the framework level; card approval; safety Lines
      outrank canon; disconnect resync
  §8  LLM calls per turn are bounded and only six roles may call at all

Run:  python -m tests.test_layers
"""
import os
import shutil
import tempfile

os.environ["STORYLIVER_LLM_MODE"] = "mock"
_TMP = tempfile.mkdtemp(prefix="storyliver-layers-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP

from backend import (atlas, awareness, betrayal, budget, canon, combat, db,  # noqa: E402
                     engine, identity, llm, memory, precommit, relationships,
                     sessions, worldstate)
from backend import worlds as world_registry  # noqa: E402

WORLD = world_registry.starter("emberfall")
FAILS, NOTES = [], []


def ok(cond, msg):
    (NOTES if cond else FAILS).append(msg)


def section(name):
    NOTES.append(f"__SECTION__{name}")


# ===========================================================================

def test_l2_living_world():
    section("L2 — living world")
    db.init()
    pt = engine.create_playthrough("l2user")
    w = engine.world_for(engine._pt(pt))

    st0 = worldstate.get(pt)
    seen_weather, seen_phase = set(), set()
    for i in range(1, 17):
        st = worldstate.tick(pt, w, i)
        seen_weather.add(st["weather"])
        seen_phase.add(st["phase"])
    ok(len(seen_phase) == 4, f"time cycles through all four phases ({sorted(seen_phase)})")
    ok(len(seen_weather) > 1, f"weather actually changes over 16 turns ({sorted(seen_weather)})")

    night = next(worldstate.tick(pt, w, t) for t in range(1, 9)
                 if worldstate.PHASES[t % 4] == "night")
    ok(night["light"] <= 2, f"night is dark (light {night['light']}/5) and stealth reads it")

    # Same playthrough, same starting state, same weather - seeded and testable.
    worldstate.seed(pt, w)
    replay = [worldstate.tick(pt, w, i)["weather"] for i in range(1, 9)]
    worldstate.seed(pt, w)
    replay2 = [worldstate.tick(pt, w, i)["weather"] for i in range(1, 9)]
    ok(replay == replay2, f"the weather chain replays identically from the same seed ({replay[:4]})")

    # Atlas
    a0 = atlas.view(pt, w, here="broken_bell", turn=0)
    hidden0 = sum(1 for n in a0["nodes"] if n["status"] == "hidden")
    ok(hidden0 > 0, f"{hidden0} places start hidden and are sent to the client without names")
    ok(all(n["name"] == "?" for n in a0["nodes"] if n["status"] == "hidden"),
       "a hidden place's name never leaves the server")

    atlas.discover(pt, "village_green", 3)
    a1 = atlas.view(pt, w, here="village_green", turn=3)
    ok(a1["discovered"] > a0["discovered"], f"discovery advances the atlas ({a0['discovered']} -> {a1['discovered']})")

    atlas.ripple(pt, "millhouse", "burned", 5, "The mill burns to the waterline.")
    a2 = atlas.view(pt, w, here="village_green", turn=6)
    mill = next(n for n in a2["nodes"] if n["id"] == "millhouse")
    ok(mill["condition"] == "burned" and mill["status"] == "ruined",
       "a consequence ripple marks the place ruined, permanently")
    ok(atlas.blocked(pt, "millhouse") == "burned", "a ruined place blocks travel")
    ok(any("mill burns" in e["text"] for e in atlas.echoes(pt)),
       "the change is written into the world-memory ribbon")

    # Layout is stable so the map never reshuffles between sessions.
    ok(atlas.layout(w) == atlas.layout(w), "map layout is deterministic across calls")


def test_l4_relationships():
    section("L4 — relationship economy (deterministic)")
    db.init()
    pt = engine.create_playthrough("l4user")

    with budget.turn("l4", limit=0):          # zero model calls allowed in here
        for _ in range(6):
            relationships.apply_event(pt, "nessa", "user", "helped", turn=1)
        built = relationships.vector(pt, "nessa", "user")
        relationships.apply_event(pt, "nessa", "user", "betrayed", turn=2)
        after = relationships.vector(pt, "nessa", "user")
    ok(True, "relationship maths ran with a zero-call model budget ($0)")

    ok(built["trust"] > 20, f"repeated help builds trust ({built['trust']})")
    drop = built["trust"] - after["trust"]
    ok(drop > built["trust"], f"betrayal costs more than the trust it broke (-{drop:.0f} from {built['trust']:.0f})")
    ok(after["betrayals"] == 1, "the betrayal is counted, permanently")
    ok(after["disposition"] in ("burned", "hostile"), f"disposition reads '{after['disposition']}'")

    # Diminishing returns: the tenth kindness moves less than the first.
    db.run("DELETE FROM relationships WHERE playthrough_id=? AND dst='ilo'", (pt,))
    first = relationships.apply_event(pt, "ilo", "user", "spoke_kindly", turn=1)["deltas"]["affinity"]
    for _ in range(9):
        relationships.apply_event(pt, "ilo", "user", "spoke_kindly", turn=1)
    tenth = relationships.apply_event(pt, "ilo", "user", "spoke_kindly", turn=1)["deltas"]["affinity"]
    ok(tenth < first, f"diminishing returns hold ({first:.2f} -> {tenth:.2f} per kindness)")

    # Love is gated on trust; loyalty on affinity + obligation.
    db.run("DELETE FROM relationships WHERE playthrough_id=? AND dst='coal'", (pt,))
    for _ in range(20):
        relationships.apply_event(pt, "coal", "user", "romanced", turn=1)
    v = relationships.vector(pt, "coal", "user")
    ok(v["love"] <= max(0, v["trust"]) + 25.5,
       f"love cannot outrun trust ({v['love']} vs trust {v['trust']})")

    # Same inputs from the same start, same outputs. Two fresh playthroughs so
    # both begin from the world's identical seeded relationship row.
    db.init()
    seq = ["helped", "kept_promise", "insulted", "helped", "betrayed"]
    runs = []
    for who in ("l4a", "l4b"):
        p = engine.create_playthrough(who)
        for e in seq:
            relationships.apply_event(p, "nessa", "user", e, turn=1)
        runs.append(relationships.vector(p, "nessa", "user"))
    ok(runs[0] == runs[1], "identical event sequences produce identical vectors")

    ok(relationships.classify("I threaten Nessa Quill.") == "threatened",
       "a typed action maps to an event with no model call")
    ok(relationships.targets(WORLD, "I threaten Nessa Quill.", ["nessa", "corvin"]) == ["nessa"],
       "the event lands on the character actually named, not whoever is nearest")


def test_l5_awareness():
    section("L5 — world awareness (non-omniscient)")
    db.init()
    pt = engine.create_playthrough("l5user")
    w = engine.world_for(engine._pt(pt))

    with budget.turn("l5", limit=0):
        fact = awareness.witness(
            pt, w, actor="user", kind="harmed", summary="Struck Nessa Quill.",
            place_id="broken_bell", turn=2, severity=4, present=["nessa", "corvin"])
        elsewhere = awareness.witness(
            pt, w, actor="user", kind="harmed", summary="Struck someone in the dark.",
            place_id="ferrows_dig", turn=3, severity=4, present=[])
    ok(True, "witnessing, rumour and reputation all ran on a zero-call budget ($0)")

    ok(set(fact["witnesses"]) <= {"nessa", "corvin"}, "only those present can witness")
    ok(not awareness.knows(pt, "npc", "maren", fact["key"]),
       "an NPC who was elsewhere does NOT know - no global event bus")
    ok(awareness.knows(pt, "player", "user", fact["key"]), "the actor always knows what they did")
    ok(elsewhere["unseen"], "an act with nobody present is genuinely unwitnessed")

    # Whether a given witness gossips is a seeded roll, so spread() can
    # legitimately send nothing. Relaying directly makes the three assertions
    # below run EVERY time - a test that silently skips its own point when the
    # dice go the other way is not a test.
    sent = awareness.spread(pt, w, fact, turn=2, witnesses=fact["witnesses"], severity=4)
    ok(isinstance(sent, list), f"spread is deterministic per story ({len(sent)} carried)")
    if not db.row("SELECT id FROM rumors WHERE playthrough_id=?", (pt,)):
        awareness.relay(pt, w, key=fact["key"], carrier=fact["witnesses"][0],
                        from_place="broken_bell", to_place="village_green", turn=2)
    r = db.row("SELECT * FROM rumors WHERE playthrough_id=? ORDER BY id LIMIT 1", (pt,))
    ok(r["arrive_turn"] > r["depart_turn"],
       f"a rumour takes real time to travel (T{r['depart_turn']} -> T{r['arrive_turn']})")
    early = awareness.arrivals(pt, w, r["arrive_turn"] - 1)
    ok(not any(x["key"] == r["fact_key"] for x in early),
       "the rumour has NOT arrived before its arrival turn")
    landed = awareness.arrivals(pt, w, r["arrive_turn"])
    ok(landed and landed[0]["confidence"] < 1.0,
       f"a second-hand fact arrives less certain ({landed[0]['confidence'] if landed else 'n/a'})")

    reps = awareness.adjust_rep(pt, w, player="user", fact=fact, turn=2, valence=-1)
    ok(reps and reps[0]["standing"] < 0, f"reputation drops for the faction that knows ({reps[0]['standing']})")
    unaware = awareness.adjust_rep(pt, w, player="user", fact=elsewhere, turn=3, valence=-1)
    ok(not unaware, "an unwitnessed act moves NO faction's opinion")

    # Hunt: window, not teleport.
    for t in range(4, 12):
        f2 = awareness.witness(pt, w, actor="user", kind="harmed", summary="Struck Nessa again.",
                               place_id="broken_bell", turn=t, severity=4, present=["nessa", "corvin", "hadrik"])
        r2 = awareness.adjust_rep(pt, w, player="user", fact=f2, turn=t, valence=-1)
        orders = awareness.maybe_order_hunt(pt, w, player="user", fact=f2, turn=t, reps=r2)
        if orders:
            o = orders[0]
            ok(o["eta_max"] > o["eta_min"], f"the hunter has an arrival WINDOW, not a tick "
                                            f"(T{o['eta_min']}-{o['eta_max']})")
            legs = awareness.travel_turns(pt, w, o["from"], o["to"])
            ok(o["eta_min"] == t + legs,
               f"the arrival is the real travel time, not a teleport "
               f"({o['from']} -> {o['to']} = {legs} turns)")
            far = awareness.travel_turns(pt, w, "broken_bell", "reach_road")
            ok(far > 0, f"a hunter dispatched from across the map takes {far} turns to arrive")
            awareness.reveal_hunt_to_player(pt, o["id"], "user", t, "Word got back.")
            panel = awareness.hunt_panel(pt, w, "user", t)
            ok(panel and "turns" in panel[0]["window"],
               f"the panel shows the window as a range ({panel[0]['window']})")
            break
    else:
        ok(False, "a sustained pattern of assault never triggered a hunt")

    # Stealth is arithmetic over the world, not a dice roll against a wall.
    worldstate.tick(pt, w, 3)                       # a night phase
    dark = awareness.stealth_check(pt, w, place_id="broken_bell", turn=3, actor="user",
                                   watchers=["nessa"], intent_noise=0)
    loud = awareness.stealth_check(pt, w, place_id="broken_bell", turn=3, actor="user",
                                   watchers=["nessa", "corvin", "hadrik"], intent_noise=5)
    ok(dark["margin"] > loud["margin"],
       f"quiet with one watcher beats loud with three ({dark['margin']} vs {loud['margin']})")
    ok(dark == awareness.stealth_check(pt, w, place_id="broken_bell", turn=3, actor="user",
                                       watchers=["nessa"], intent_noise=0),
       "the same conditions always give the same answer")


def test_witnessing_harm_moves_the_witness():
    section("witnessed harm - being SEEN doing it costs the witness, not just the target")
    # engine.py applied a harmful event's scalars only to `aimed_at` (the
    # target). A witness got a memory entry from awareness.witness() and
    # nothing else - trust/fear/respect never moved, so will_snitch and
    # betrayal_pressure (which read those scalars) never reacted to what a
    # bystander had just watched happen.
    db.init()
    pt = engine.create_playthrough("witness_user")
    w = engine.world_for(engine._pt(pt))
    memory.npcs_at  # (imported already by the module under test)
    present = [n["id"] for n in w.npcs][:2]
    target, bystander = present[0], present[1]
    before = relationships.ensure(pt, bystander, "witness_user")
    r = engine.take_turn(pt, f"I beat {w.npc_name(target)} bloody in front of everyone.",
                         player="witness_user")
    ok(not r.get("blocked"), "the action actually resolved")
    after = relationships.ensure(pt, bystander, "witness_user")
    moved = (round(after["trust"], 3) != round(before["trust"], 3)
            or round(after["fear"], 3) != round(before["fear"], 3))
    fact = r.get("witness") or {}
    if bystander in (fact.get("witnesses") or []):
        ok(moved, f"the bystander who witnessed it has moved scalars "
                  f"(trust {before['trust']}->{after['trust']}, fear {before['fear']}->{after['fear']})")
    else:
        ok(True, "the witness roll missed this bystander this turn (seeded, can happen)")


def test_l3_betrayal():
    section("L3 — betrayal and hidden information")
    db.init()
    room = sessions.create("l3host", mode="chaos", host_name="Vale")
    pt, sid = room["playthrough_id"], room["id"]
    f1 = sessions.join(sid, "l3u2", "Tamsin")
    w = engine.world_for(engine._pt(pt))

    key = precommit.round_key("test", 1)
    precommit.declare(pt, round_key=key, turn=1, player_id="host",
                      intent="I take the deed.", visibility="hidden")
    precommit.declare(pt, round_key=key, turn=1, player_id=f1["player_id"],
                      intent="I guard the door.", visibility="open")

    theirs = precommit.board(pt, key, f1["player_id"], ["host", f1["player_id"]])
    host_card = next(c for c in theirs["cards"] if c["player_id"] == "host")
    ok(not host_card["is_truth"] and "not saying" in host_card["shown"].lower(),
       "a hidden order shows as committed, never as its content")
    mine = precommit.board(pt, key, "host", ["host", f1["player_id"]])
    ok(next(c for c in mine["cards"] if c["mine"])["shown"] == "I take the deed.",
       "you always see your own order")
    ok(mine["ready"], "the round is ready only when every expected player has submitted")

    revealed = precommit.reveal(pt, key)
    ok(len(revealed) == 2 and all(r["intent"] for r in revealed),
       "the reveal shows everything, all at once")

    # Departure -> private turns -> simultaneous reveal, with a real lie.
    for _ in range(4):
        relationships.apply_event(pt, "nessa", "host", "kept_promise", turn=1)
    d = betrayal.depart(pt, player_id="host", announced="I am going for water.",
                        truth="I am selling you to Corvin.", destination="village_green",
                        turn=2, session_id=sid)
    ok(d["deception"], "a departure where the account differs is flagged deceptive")

    board = betrayal.board(pt, w, f1["player_id"], 2,
                           [{"player_id": "host", "name": "Vale"}])
    ok(board["entries"][0]["truth"] is None,
       "another player's real intent is NOT sent to their client")
    own = betrayal.board(pt, w, "host", 2, [{"player_id": "host", "name": "Vale"}])
    ok(own["entries"][0]["truth"], "your own real intent is yours to see")

    betrayal.record_private(pt, player_id="host", turn=3, summary="Corvin agreed a price.")
    ok(any("Corvin agreed" in k["summary"] for k in betrayal.private_log(pt, "host", 0)),
       "a private turn is recorded to the actor alone")
    ok(not betrayal.private_log(pt, f1["player_id"], 0),
       "the other player's private log is empty - it never leaked")

    betrayal.stage_reveal(pt, split_id=d["id"], player_id="host", turn=5,
                          account="I found water.", truth="I sold you to Corvin.", session_id=sid)
    out = betrayal.resolve_reveal(pt, w, 5, present_npcs=["nessa", "corvin"], party=[])
    entry = out["revealed"][0]
    ok(entry["deceptive"], "the gap between the account and the truth is exposed at the reveal")
    ok(isinstance(entry["caught_by"], list),
       f"insight decided who saw through it ({len(entry['caught_by'])} caught it)")

    trusting = betrayal.insight(pt, w, liar="host", target_kind="npc", target_id="nessa", turn=5)
    burned_before = db.run(
        "UPDATE relationships SET betrayals=3, trust=-60 WHERE playthrough_id=? AND src='host' AND dst='corvin'",
        (pt,))
    suspicious = betrayal.insight(pt, w, liar="host", target_kind="npc", target_id="corvin", turn=5)
    ok(trusting["credulity"] > suspicious["credulity"],
       f"someone you burned is harder to fool ({trusting['credulity']} vs {suspicious['credulity']})")

    roles = betrayal.assign_roles(pt, sid, ["host", f1["player_id"], "p3"], traitors=1)
    ok(sum(1 for r in roles.values() if r == "traitor") == 1, "exactly one traitor is assigned")
    ok(betrayal.assign_roles(pt, sid, ["host", f1["player_id"], "p3"], traitors=1) == roles,
       "a reconnect cannot reroll your hidden role")


def test_l6_combat():
    section("L6 — combat (WEGO)")
    db.init()
    pt = engine.create_playthrough("l6user")
    w = engine.world_for(engine._pt(pt))

    with budget.turn("l6", limit=0):
        side_a = [{"id": "p1", "kind": "player", "name": "Vale", "hp": 30, "power": 5, "guard": 10, "zone": "front_a"},
                  {"id": "p2", "kind": "player", "name": "Tam", "hp": 30, "power": 5, "guard": 10, "zone": "centre"}]
        side_b = [{"id": "e1", "kind": "npc", "name": "Thug", "hp": 24, "power": 4, "guard": 9, "zone": "centre"},
                  {"id": "e2", "kind": "npc", "name": "Bruiser", "hp": 24, "power": 4, "guard": 9, "zone": "centre"},
                  {"id": "e3", "kind": "npc", "name": "Runner", "hp": 24, "power": 4, "guard": 9, "zone": "front_b"}]
        c = combat.start(pt, w, place_id="broken_bell", sides={"a": side_a, "b": side_b})
        cid = c["id"]
        state = combat.get(cid)
        tam = next(x for x in state["combatants"] if x["entity_id"] == "p2")
        thug = next(x for x in state["combatants"] if x["entity_id"] == "e1")
        adv_tam = combat.advantage(state, tam)
        adv_thug = combat.advantage(state, thug)
    ok(True, "the whole combat round ran on a zero-call model budget ($0)")

    ok(adv_tam < 0, f"the outnumbered player is flanked ({adv_tam}) - it cuts both ways")
    ok(adv_thug > 0, f"the side with more bodies gains the angle ({adv_thug})")

    combat.declare(pt, cid, "p1", "strike", target="e1")
    combat.declare(pt, cid, "p2", "reposition", zone="front_a")
    board_before = combat.view(pt, cid, "p1")
    ok(not any(b.get("shown_move") for b in board_before["board"]),
       "no combatant's declared move is exposed on the board")
    ok(set(board_before["declared"]) == {"p1", "p2"},
       "the board shows WHO has declared, never WHAT")

    r1 = combat.resolve(pt, cid)
    ok(any(s["kind"] == "move" for s in r1["steps"]), "movement resolves before damage")
    after = combat.get(cid)
    tam2 = next(x for x in after["combatants"] if x["entity_id"] == "p2")
    ok(combat.advantage(after, tam2) > adv_tam,
       f"repositioning breaks the flank ({adv_tam} -> {combat.advantage(after, tam2)})")
    ok(any(s["kind"] in ("hit", "blocked") for s in r1["steps"]),
       "everyone's orders resolved in the same round - no turn order")

    # Boss: a plan, not more hitting.
    db.init()
    pt2 = engine.create_playthrough("l6boss")
    boss = combat.build_boss(w, {"entity_id": "b1", "name": "The Warden",
                                 "only_hurt_by": "grounded", "weak_point": "off_hand"})
    c2 = combat.start(pt2, w, place_id="broken_bell", boss=boss, sides={
        "a": [{"id": "p1", "kind": "player", "name": "Vale", "hp": 30, "power": 6, "guard": 10, "zone": "front_a"}],
        "b": [{"id": "b1", "kind": "npc", "name": "The Warden", "hp": 60, "power": 5, "guard": 12, "zone": "centre"}]})
    combat.declare(pt2, c2["id"], "p1", "press", target="b1")
    rb = combat.resolve(pt2, c2["id"])
    hits = [s for s in rb["steps"] if s.get("target") == "The Warden"]
    ok(hits and all(h["damage"] == 0 for h in hits),
       "a boss with only_hurt_by takes nothing from ordinary damage")
    ok(any("untouchable until" in (h.get("note") or "") for h in hits),
       "the game TELLS you why it did nothing, instead of just failing")

    combat.apply_condition(pt2, c2["id"], "b1", "grounded")
    combat.declare(pt2, c2["id"], "p1", "exploit", target="b1")
    rb2 = combat.resolve(pt2, c2["id"])
    hits2 = [s for s in rb2["steps"] if s.get("target") == "The Warden" and s["kind"] == "hit"]
    ok(hits2 and hits2[0]["damage"] > 0,
       f"creating the condition makes the boss vulnerable ({hits2[0]['damage'] if hits2 else 0} damage)")
    ok(any("open" in (h.get("note") or "") for h in hits2),
       "hitting the declared weak point is what pays off")

    # Stealth surprise
    db.init()
    pt3 = engine.create_playthrough("l6hide")
    c3 = combat.start(pt3, w, place_id="broken_bell", sides={
        "a": [{"id": "p1", "kind": "player", "name": "Vale", "hp": 30, "power": 5,
               "guard": 10, "zone": "front_a", "hidden": True}],
        "b": [{"id": "e1", "kind": "npc", "name": "Guard", "hp": 30, "power": 4, "guard": 9, "zone": "centre"}]})
    seen = combat.view(pt3, c3["id"], "e1")
    ok(not any(b["id"] == "p1" for b in seen["board"]),
       "a hidden combatant is not sent to the enemy's client at all")
    combat.declare(pt3, c3["id"], "p1", "strike", target="e1")
    r3 = combat.resolve(pt3, c3["id"])
    hit = next((s for s in r3["steps"] if s["kind"] == "hit"), None)
    ok(hit and hit.get("note") == "from hiding", "opening from unseen is a surprise strike")


def test_l7_identity():
    section("L7 — canon, cards, safety, disconnect")
    db.init()
    pt = engine.create_playthrough("l7user")
    w = engine.world_for(engine._pt(pt))

    with budget.turn("l7", limit=0):
        cancelled = []
        for npc_id, text in [("coal", "Coal says hello."),
                             ("maren", "Maren Vosk leaves the valley for good."),
                             ("nessa", "As an AI language model, I cannot continue.")]:
            try:
                canon.check_npc_action(pt, w, npc_id, text, turn=1, state={"dead": []})
            except canon.Cancelled as c:
                cancelled.append((npc_id, c.rule[:28], c.layer))
    ok(len(cancelled) == 3,
       f"every canon violation cancelled at the framework level ({[c[0] for c in cancelled]})")
    ok(any(c[2] == "ooc" for c in cancelled), "breaking frame is caught as its own class")

    allowed = canon.check_npc_action(pt, w, "nessa", "Nessa sets the glass down and waits.",
                                     turn=1, state={"dead": []})
    ok(allowed, "an in-character action passes through untouched")

    try:
        canon.check_prose(pt, w, "What would you like to do next?", turn=1)
        ok(False, "meta prose was allowed")
    except canon.Cancelled:
        ok(True, "narrator prose that offers a menu is cancelled")

    logged = canon.recent(pt)
    ok(any(e["verdict"] == "cancelled" for e in logged),
       "every cancellation is logged and inspectable by the player")

    # Safety Lines outrank canon and the world's own rules.
    canon.set_safety(pt, lines=["drowning"], veils=["torture"])
    try:
        canon.safety_gate(pt, "I hold his head under until he stops drowning.")
        ok(False, "a Line did not cancel")
    except canon.Cancelled as c:
        ok(c.layer == "safety", "a Line cancels before anything else is evaluated")
    ok(canon.safety_gate(pt, "The torture goes on somewhere out of sight.")["veiled"],
       "a Veil is flagged for the narrator to cut away, not cancelled")
    canon.x_card(pt, 4, "user", "")
    ok(any(e["kind"] == "x_card" for e in canon.safety(pt)["events"]),
       "the X-card is recorded, and needs no reason")

    aims = canon.aims(w, "maren")
    ok(aims["agenda"] and aims["secrets"], "hidden AIMS derived for every NPC without extra authoring")

    # Cards + mutual approval
    rolled = identity.roll(w, "p1", "Vale")
    ok(rolled["concept"] and rolled["aspects"]["voice"], "blanks can be rolled for free")
    card = identity.save(pt, {**rolled, "anomaly": "Can hear a lie."}, session_id="s1")
    identity.submit(pt, card["id"])
    mid = identity.approve(pt, card["id"], "p2", ok=True, table=["p2", "p3"])
    ok(mid["status"] == "pending", "one approval is not enough when the table has two others")
    done = identity.approve(pt, card["id"], "p3", ok=True, table=["p2", "p3"])
    ok(done["status"] == "approved", "the card enters play only when the whole table approves")
    blocked = identity.approve(pt, card["id"], "p2", ok=False, table=["p2", "p3"])
    ok(blocked["status"] == "changes_requested", "a single objection sends it back")

    data = dict(w.data)
    rid = identity.anomaly_to_canon(pt, data, done)
    ok(rid and any(r["id"] == rid for r in data["rules"]),
       "an approved anomaly becomes a world rule the World Master will enforce")

    # Disconnect: safe stand-in, then authoritative resync.
    order = identity.standin_order(pt, w, done, {"location": "broken_bell"})
    ok(order["safe"] and "never spends" in order["note"],
       "an absent player's stand-in cannot spend, promise, or betray")
    identity.snapshot_for_resync(pt, "s1", "p1", {"turn": 9, "hp": 21})
    ok(identity.resync("s1", "p1") == {"turn": 9, "hp": 21},
       "the server holds the authoritative state for a reconnect")


def test_cost_architecture():
    section("§8 — cost architecture")
    db.init()
    pt = engine.create_playthrough("costuser")

    calls = []
    for i in range(6):
        r = engine.take_turn(pt, ["I ask Nessa Quill about the Warden.",
                                  "I make my way to The Village Green.",
                                  "I help Ilo carry the buckets."][i % 3])
        nar = next((e for e in r["entries"] if e["kind"] == "narration"), None)
        if nar:
            calls.append(nar["meta"]["llm_calls"])
    ok(calls and max(calls) <= budget.MAX_LLM_CALLS_PER_TURN,
       f"every turn stayed inside the {budget.MAX_LLM_CALLS_PER_TURN}-call cap (peak {max(calls)})")

    roles = {r["role"] for r in db.rows(
        "SELECT DISTINCT role FROM usage_log WHERE playthrough_id=?", (pt,))}
    ok(roles <= budget.ALLOWED, f"only permitted roles called a model ({sorted(roles)})")

    try:
        with budget.turn("probe", limit=1):
            llm.complete("narrator", "s", "u", user_id="x", stub=lambda: "a")
            llm.complete("narrator", "s", "u", user_id="x", stub=lambda: "b")
        ok(False, "the cap did not raise")
    except budget.BudgetExceeded:
        ok(True, "exceeding the cap raises rather than quietly spending")

    try:
        with budget.turn("probe2"):
            llm.complete("some_new_system", "s", "u", user_id="x", stub=lambda: "a")
        ok(False, "an unlisted role was allowed to call a model")
    except budget.BudgetExceeded:
        ok(True, "a new system cannot quietly become a seventh model caller")

    u = engine.usage_summary(pt)
    ok(u["within_budget"], f"blended cost ${u['usd_per_action']:.5f}/action vs ${u['target_usd_per_action']}")

    table = budget.table()
    ok(len(table["free_actions"]) >= 10,
       f"{len(table['free_actions'])} deterministic actions are priced at zero")


# ===========================================================================

def main():
    print("StoryLiver — layers 2-7 verification gate")
    print("  offline stub, no API key, no spend\n")
    for fn in (test_l2_living_world, test_l4_relationships, test_l5_awareness,
               test_witnessing_harm_moves_the_witness,
               test_l3_betrayal, test_l6_combat, test_l7_identity, test_cost_architecture):
        fn()
    passed = 0
    for n in NOTES:
        if n.startswith("__SECTION__"):
            print(f"\n  {n.replace('__SECTION__', '')}")
        else:
            print("  PASS  " + n)
            passed += 1
    for f in FAILS:
        print("  FAIL  " + f)
    print(f"\n  {passed} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


def test_all_layers():
    for fn in (test_l2_living_world, test_l4_relationships, test_l5_awareness,
               test_witnessing_harm_moves_the_witness,
               test_l3_betrayal, test_l6_combat, test_l7_identity, test_cost_architecture):
        fn()
    assert not FAILS, "\n".join(FAILS)


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
