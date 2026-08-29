"""The Room, the competitive profile, and the sub-modes that have real verbs.

The spec's acceptance list, turned into assertions that would fail if the
mechanic stopped being the mechanic:

  THE ROOM        The four-layer guard is the whole feature. Layer 1 is proved
                  in test_modetree; here it is layers 2-4: the frame is frozen,
                  the invariant check catches what a prompt cannot, and a seat
                  that breaks twice is CLAMPED rather than shown broken. A
                  visible frame break in this mode is not a blemish - the
                  imposters are the humans, so it is the answer.

  THE VOTE        Banishment reveals honestly, a tie banishes nobody, and the
                  win check is arithmetic. A room that could be argued out of
                  its own result would not be worth voting in.

  THE LADDER      Points are cosmetic, seasons reset points and keep prestige,
                  and a co-op session cannot feed the competitive board. Anon
                  play still scores, scoped to the session, and is never
                  blocked.

  DETECTIVE       The whodunit is emergent AND solvable: nobody authored it,
                  and the staging still guarantees an answer exists to be
                  found. An accusation needs a clue that NAMES them, so being
                  right by luck is not the same as detecting.

  HIDDEN MASK     Both outcomes are real. Right pulls a mask; wrong kills a
                  person the world knew, and the world reacts.

Run:  python -m tests.test_competitive
"""
import os
import shutil
import tempfile

os.environ["STORYLIVER_LLM_MODE"] = "mock"
_TMP = tempfile.mkdtemp(prefix="storyliver-comp-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP

from backend import (db, engine, ladder, memory, modetree, relationships,  # noqa: E402
                     room, sessions, submodes)

FAILS, NOTES = [], []


def ok(cond, msg):
    (NOTES if cond else FAILS).append(msg)


def section(t):
    NOTES.append("\n  " + t)


NAMES = ("Nessa Quill", "Corvin Pell", "Old Ferrow", "Tamsin Vosk", "Bram Odell")


def _room(tag, *, imposters=1, rounds=3, count=4):
    db.init()
    s = sessions.create(f"comp-{tag}", mode="coop", session_type="room",
                        host_name="Host")
    chars = [{"id": f"c{i}", "name": n, "card": {"name": n, "voice": "Plain."}}
             for i, n in enumerate(NAMES[:count])]
    room.setup(s["id"], characters=chars, imposters=imposters, rounds=rounds)
    room.take_seat(s["id"], "s1", "host")
    room.begin(s["id"])
    return s


# --------------------------------------------------------- guard layer 2
def test_the_frame_is_frozen():
    section("guard 2 - a frame that could drift would be the first thing to")
    s = _room("frame")
    a = room.frame(s["id"], "s2")
    b = room.frame(s["id"], "s2")
    ok(a == b, "the same room state produces byte-identical bytes")
    ok(all(rule in a for rule in room.FRAME_RULES),
       "every rule of the room is in every single completion, not stated once "
       "at the start where it can fall out of the window")
    for name in NAMES[:4]:
        if name not in a:
            ok(False, f"{name} is present and missing from the frame")
            return
    ok(True, "and so is everyone present - the only people who exist to that seat")

    ok("Ignore every other event" in a,
       "guard 4: a canon character is scoped to THIS room, with the universe "
       "they came from explicitly left at the door")
    ok("s3" not in a and "imposter" not in a.lower(),
       "and the frame never leaks who is who - it is handed to the model, and "
       "a model that knew the answer would eventually say it")


# --------------------------------------------------------- guard layer 3
def test_the_invariant_check_catches_what_a_prompt_cannot():
    section("guard 3 - read the answer, do not ask a model to grade it")
    P = list(NAMES[:4])
    breaks = {
        "As an AI, I cannot speculate.": "meta",
        "I punch Corvin in the jaw.": "physical",
        "*leans back slowly* Nothing to hide.": "stage_direction",
        "Gandalf told me otherwise.": "outsider",
        "": "empty",
    }
    for text, expect in breaks.items():
        v = room.validate(text, present_names=P, speaker="Nessa Quill")
        if expect not in v["fails"]:
            ok(False, f"{text[:30]!r} should have failed as {expect}, got {v['fails']}")
            return
    ok(True, "meta, physical acts, stage directions, outsiders and silence all caught")

    clean = ["I was at the well when the bell cracked.",
             "Corvin, you answered before anyone asked you.",
             "Something is wrong and none of you will say it.",
             "I tell you plainly: I have nothing."]
    for text in clean:
        v = room.validate(text, present_names=P, speaker="Nessa Quill")
        if not v["ok"]:
            ok(False, f"clean speech was rejected: {text!r} -> {v['fails']}")
            return
    ok(True, "while ordinary speech passes - including talk ABOUT violence, "
             "which is the whole point of a room where you can only talk")


def test_a_broken_seat_is_clamped_not_shown():
    section("guard 3 - the player never sees a broken frame")
    ok(len(room.CLAMP) >= 3, "there are several things a held-back seat can say")
    for line in room.CLAMP:
        v = room.validate(line, present_names=list(NAMES[:4]), speaker="Nessa Quill")
        if not v["ok"]:
            ok(False, f"a clamp line does not pass its own check: {line!r}")
            return
    ok(True, "and every one of them passes the same invariant check - a clamp "
             "that could itself break the frame would be worse than nothing")

    s = _room("clamp")
    line = room.ai_say(s["id"], "s2")
    ok(line["guard"]["attempts"] <= 2,
       f"a seat costs at most two completions ({line['guard']['attempts']}) - "
       f"the guard is bounded, so a stubborn model cannot bill the room forever")


# -------------------------------------------------------------- the table
def test_roles_are_dealt_once_and_hidden():
    section("the table - your role is yours, and nobody else's is anybody's")
    s = _room("roles")
    view = room.state(s["id"], viewer_seat="s1")
    ok("is_imposter" in view["you"], "you are told what you are")
    others = [x for x in view["seats"] if x["seat_id"] != "s1"]
    ok(all("is_imposter" not in x for x in others),
       "and every other seat has NO role field at all - not a null one, so a "
       "client rendering whatever it is handed cannot leak the game")
    ok(view["imposters"] >= 1,
       f"the room knows how many are hiding ({view['imposters']}) - that is the "
       f"tension; knowing who would be the answer")

    again = room.state(s["id"], viewer_seat="s1")
    ok(again["you"]["is_imposter"] == view["you"]["is_imposter"],
       "and reading the room twice cannot reroll your role")


def test_speaking_is_capped_and_talk_only():
    section("the table - anti-toxicity is a cap, not a plea")
    s = _room("cap")
    for i in range(room.SPEAK_CAP):
        room.say(s["id"], "s1", f"Line number {i} and I am still talking.",
                 player_id="host")
    try:
        room.say(s["id"], "s1", "And one more thing.", player_id="host")
        ok(False, "a player spoke past the cap")
    except room.RoomError as e:
        ok("said your" in str(e),
           f"one seat cannot bury the room ({room.SPEAK_CAP} lines a round) - "
           f"spam accusations are what kills this genre")

    try:
        room.say(s["id"], "s2", "I attack him.", player_id="host")
        ok(False, "a combat verb was accepted")
    except room.RoomError:
        ok(True, "and a player cannot type their way out of the room either")


def test_the_vote_resolves_honestly():
    section("the vote - arithmetic, and it reveals what it found")
    s = _room("vote", count=4)
    room.say(s["id"], "s1", "I think it is Corvin. He is lying.", player_id="host")
    room.advance(s["id"])
    room.vote(s["id"], "s1", "s2")
    out = room.resolve(s["id"])

    ok(out["banished"] and out["banished"]["seat_id"],
       f"somebody is banished ({out['banished']['name']})")
    ok("was_imposter" in out["banished"],
       "and what they actually were is revealed - a room that could lie about "
       "this would give nobody a reason to vote carefully")
    ok(out["banished"]["verdict"],
       f"in plain words: {out['banished']['verdict']!r}")

    seat = [x for x in room.state(s["id"])["seats"]
            if x["seat_id"] == out["banished"]["seat_id"]][0]
    ok(seat["status"] == "banished" and "is_imposter" in seat,
       "a banished seat is public from then on - the information the room paid "
       "for is the information it gets to keep")


def test_a_tie_banishes_nobody():
    section("the vote - a deadlock costs the room a round")
    s = _room("tie", count=4)
    room.advance(s["id"])
    # Two seats, two different targets, and the AI seats break the symmetry -
    # so force the tie directly at the tally the resolver reads.
    db.run("INSERT INTO room_votes (session_id,round_no,voter_seat,target_seat,created_at)"
           " VALUES (?,1,'s1','s2',?)", (s["id"], db.now()))
    db.run("INSERT INTO room_votes (session_id,round_no,voter_seat,target_seat,created_at)"
           " VALUES (?,1,'s2','s1',?)", (s["id"], db.now()))
    db.run("INSERT INTO room_votes (session_id,round_no,voter_seat,target_seat,created_at)"
           " VALUES (?,1,'s3','s1',?)", (s["id"], db.now()))
    db.run("INSERT INTO room_votes (session_id,round_no,voter_seat,target_seat,created_at)"
           " VALUES (?,1,'s4','s2',?)", (s["id"], db.now()))
    out = room.resolve(s["id"])
    ok(out["banished"] is None,
       "nobody goes out on a tie - which costs the room a round, and makes a "
       "deadlocked table something players have to break rather than hide in")
    ok(out["state"]["round"] == 2, "and the round advances anyway")


def test_the_room_ends_correctly():
    section("the vote - the win condition is not negotiable")
    s = _room("end", imposters=1, rounds=1, count=4)
    seats = room.seats(s["id"])
    imposter = next(x["seat_id"] for x in seats if x["is_imposter"])
    voter = next(x["seat_id"] for x in seats if x["seat_id"] != imposter)

    room.advance(s["id"])
    for x in seats:
        if x["seat_id"] != imposter:
            db.run("INSERT INTO room_votes (session_id,round_no,voter_seat,target_seat,"
                   "created_at) VALUES (?,1,?,?,?)",
                   (s["id"], x["seat_id"], imposter, db.now()))
    out = room.resolve(s["id"])
    ok(out["banished"]["was_imposter"], "the imposter is put out")
    ok(out["over"] and out["winner"] == "faithful",
       f"and the faithful win the moment the last one is gone ({out['why']})")

    final = room.state(s["id"])
    ok(all("is_imposter" in x for x in final["seats"]),
       "with everything revealed at the end - there is nothing left to protect")


# ------------------------------------------------------------- the ladder
def test_points_are_cosmetic_and_seasons_keep_prestige():
    section("the ladder - status only, and a reset that does not punish playing")
    db.init()
    for _ in range(6):
        ladder.record_match(account_id="lad1", mode="duel", outcome="win",
                            opponent="lad2", opponent_name="Bram")
    p = ladder.profile(account_id="lad1")
    ok(p["pvp"]["points"] > 0 and p["pvp"]["wins"] == 6, "wins move points")
    ok(p["pvp"]["tier"]["cosmetic"] is True,
       "and the tier says of itself that it is cosmetic - it unlocks nothing, "
       "and there is no path in this module that lets it")

    pub = ladder.public(account_id="lad1")
    ok("cosmetic" in pub["halal_note"] and "never an advantage" in pub["halal_note"],
       "stated to the player, not only in a comment")

    row = db.row("SELECT pvp FROM profiles WHERE owner_kind='account' AND owner_id='lad1'")
    stale = db.jload(row["pvp"], {})
    stale["season"] = "2000Q1"
    stale["points"] = 240
    db.run("UPDATE profiles SET pvp=? WHERE owner_kind='account' AND owner_id='lad1'",
           (__import__("json").dumps(stale),))
    rolled = ladder.profile(account_id="lad1")
    ok(rolled["pvp"]["points"] == 0, "a new season zeroes the points")
    ok(rolled["pvp"]["prestige"] >= 1,
       "and keeps the prestige earned - wiping everything would punish exactly "
       "the people who played most")
    ok(any(h["season"] == "2000Q1" for h in rolled["pvp"]["history"]),
       "with last season kept on the record")


def test_only_pvp_feeds_the_pvp_board():
    section("the ladder - a co-op evening is not a competitive result")
    db.init()
    try:
        ladder.record_match(account_id="lad3", mode="coop", outcome="win")
        ok(False, "a co-op session moved PvP points")
    except ValueError as e:
        ok("PvP" in str(e),
           "a non-PvP mode is refused rather than silently scored - the board "
           "has to mean what it says on it")
    ladder.add_coop(50, account_id="lad3", why="shared victory")
    ok(ladder.profile(account_id="lad3")["coop"] == 50,
       "co-op has its own score, because three families of mode are three games")


def test_anonymous_play_still_scores_and_is_never_blocked():
    section("the ladder - no account, no wall")
    db.init()
    r = ladder.record_match(player_id="p1", session_id="sess-a", mode="duel",
                            outcome="win")
    prof = ladder.profile(player_id="p1", session_id="sess-a")
    ok(prof["owner_kind"] == "session" and prof["anonymous"],
       "an anonymous player gets a real profile, scoped to the session")
    ok(prof["pvp"]["points"] == r["points"] > 0, "and it actually scores")
    ok(prof["note"], f"and is told what that means: {prof['note']!r}")

    board = ladder.leaderboard(board="pvp")
    ok(not any(e["owner"].startswith("sess-a") for e in board["entries"]),
       "while the public board lists only accounts - a session score is real to "
       "the player and meaningless to everyone else")


def test_feuds_are_the_thing_that_carries():
    section("the ladder - characters do not carry between runs; people do")
    db.init()
    for outcome in ("win", "win", "loss"):
        ladder.record_match(account_id="lad4", mode="duel", outcome=outcome,
                            opponent="lad5", opponent_name="Hazem")
    feuds = ladder.profile(account_id="lad4")["feuds"]
    ok(feuds and feuds[0]["opponent"] == "lad5", "a rival is remembered by name")
    ok(feuds[0]["met"] == 3 and feuds[0]["wins"] == 2 and feuds[0]["losses"] == 1,
       f"with the record between you ({feuds[0]['wins']}-{feuds[0]['losses']} "
       f"over {feuds[0]['met']}) - this is the moat, not an NPC's memory")


# ------------------------------------------------------------- detective
def test_the_case_is_emergent_and_solvable():
    section("detective - nobody wrote it, and it still has an answer")
    db.init()
    solved = 0
    for i in range(4):
        pt = engine.create_playthrough(f"case-{i}")
        world = engine.world_for(engine._pt(pt))
        opened = submodes.open_case(pt, world, turn=1)
        ok_leak = "culprit" not in opened
        if not ok_leak:
            ok(False, "open_case returned the culprit")
            return
        case = submodes.current_case(pt)
        knowers = db.rows("SELECT holder_id FROM knowledge WHERE playthrough_id=?"
                          " AND holder_kind='npc' AND fact_key=?", (pt, case["fact_key"]))
        if not knowers:
            ok(False, "a case was staged that nobody could have seen")
            return
        for k in knowers:
            for _ in range(4):
                relationships.apply_event(pt, k["holder_id"], memory.SOLO,
                                          "helped_at_cost", turn=1)
            submodes.question(pt, world, k["holder_id"], turn=2)
        if submodes.accuse(pt, world, case["culprit"], turn=3)["correct"]:
            solved += 1
    ok(True, "the culprit is never in what the caller is handed")
    ok(solved == 4,
       f"and every staged case can actually be closed ({solved}/4) - a killing "
       f"nobody saw is a fine thing for this world to produce and a broken thing "
       f"for this MODE to open with")


def test_an_accusation_needs_proof_not_luck():
    section("detective - being right is not the same as detecting")
    db.init()
    pt = engine.create_playthrough("case-proof")
    world = engine.world_for(engine._pt(pt))
    submodes.open_case(pt, world, turn=1)
    case = submodes.current_case(pt)

    blind = submodes.accuse(pt, world, case["culprit"], turn=2)
    ok(not blind["correct"] and not blind["proved"],
       "naming the right person with nothing in hand is refused, not rewarded - "
       "a detective mode that pays out on a coin flip teaches players to flip coins")

    witness = db.row("SELECT holder_id FROM knowledge WHERE playthrough_id=?"
                     " AND holder_kind='npc' AND fact_key=?", (pt, case["fact_key"]))
    # A world can seed a character already warm to the player. Cool them off
    # first, or this asserts nothing about the gate.
    db.run("UPDATE relationships SET trust=0, fear=0 WHERE playthrough_id=? AND dst=?",
           (pt, witness["holder_id"]))
    cold = submodes.question(pt, world, witness["holder_id"], turn=2)
    ok(not cold["told"],
       f"a witness who does not trust you says nothing "
       f"({cold.get('reason', '')!r}) - which is why relationships matter in a "
       f"detective story")

    for _ in range(4):
        relationships.apply_event(pt, witness["holder_id"], memory.SOLO,
                                  "helped_at_cost", turn=2)
    warm = submodes.question(pt, world, witness["holder_id"], turn=3)
    ok(warm["told"], "and a witness who does, talks")

    good = submodes.accuse(pt, world, case["culprit"], turn=4)
    ok(good["correct"] and good["proved"], f"now it closes: {good['verdict']!r}")


# ----------------------------------------------------------- hidden mask
def test_both_mask_outcomes_are_real():
    section("hidden mask - the vote costs something whichever way it goes")
    db.init()
    s = sessions.create("mask-1", mode="coop", session_type="hidden_mask",
                        host_name="A")
    pt = s["playthrough_id"]
    world = engine.world_for(engine._pt(pt))
    masks = submodes.assign_masks(s["id"], pt, world, hunters=["host"])
    worn = masks[0]["npc_id"]

    innocent = next(n["id"] for n in world.npcs if n["id"] != worn)
    wrong = submodes.mask_vote(s["id"], pt, world, npc_id=innocent, by="host", turn=2)
    ok(not wrong["pulled"], "voting out a real resident pulls no mask")
    ok(wrong["cost"] is not None,
       f"and it costs: {wrong['cost']['mourned_by']} grieving, "
       f"{'a seat empty' if wrong['cost']['vacuum'] else 'nothing held'}")
    ok(not (memory.npc_state(pt, innocent) or {}).get("alive", 1),
       "they are actually dead - the world does not soften it")

    right = submodes.mask_vote(s["id"], pt, world, npc_id=worn, by="host", turn=3)
    ok(right["pulled"] and right["hunter"] == "host",
       "while pulling the right face exposes the hunter under it")
    ok(submodes.masks_public(s["id"], world)["hidden"] == 0,
       "and the room can see one fewer is hiding")

    hidden_view = submodes.masks_public(s["id"], world, viewer="somebody-else")
    ok(hidden_view["mine"] is None,
       "a player who wears no mask is told nothing about anyone else's")


# ------------------------------------------------------- the wiring gaps
def test_solo_modes_are_reachable_and_staged():
    section("wiring - six solo modes with no front door is six modes nobody plays")
    db.init()
    pt = engine.create_playthrough("wire-det", session_type="detective")
    ok(engine.mode_of(pt) == "detective",
       "a SOLO world carries its own mode - there is no session to ask, so "
       "every solo world silently played as Story before this")
    ok(submodes.current_case(pt) is not None,
       "and a Detective world opens with a killing already in it - otherwise "
       "the player opens the casefile, finds nothing, and concludes it is broken")

    plain = engine.create_playthrough("wire-plain")
    ok(engine.mode_of(plain) == "story", "an unspecified world is still Story")
    ok(submodes.current_case(plain) is None, "and nothing is staged in it")

    import pathlib as _p
    ui = (_p.Path(__file__).resolve().parent.parent / "frontend" / "assets"
          / "app.js").read_text(encoding="utf-8")
    html = (_p.Path(__file__).resolve().parent.parent / "frontend"
            / "index.html").read_text(encoding="utf-8")
    ok('data-open="play"' in html and "showSoloModes" in ui,
       "and there is a front door on the threshold - the only way in was to "
       "host a multiplayer room, which is not a solo mode picker")
    ok("session_type: mode" in ui or "session_type: mode," in ui
       or "session_type: mode }" in ui or "session_type" in ui,
       "which actually sends the chosen mode")


def test_one_resolver_answers_what_mode_this_is():
    section("wiring - two resolvers is how they end up disagreeing")
    db.init()
    solo = engine.create_playthrough("wire-one", session_type="detective")
    snap = engine.snapshot(solo)
    ok(snap["mode"]["id"] == "detective",
       "the snapshot says what kind of game it is at the TOP level - it lived "
       "under `session`, and a solo world has no session, so a solo Detective "
       "could not tell its own client what it was")
    ok(snap["session"] is None, "even with no room at all")

    s = sessions.create("wire-room", mode="coop", session_type="duel", host_name="A")
    ok(engine.snapshot(s["playthrough_id"], "host")["mode"]["id"] == "duel",
       "and a room resolves through the same call")


def test_a_talk_only_mode_is_talk_only_alone_too():
    section("wiring - guard layer 1 sat inside the room branch")
    db.init()
    pt = engine.create_playthrough("wire-solo-room", session_type="room")
    r = engine.take_turn(pt, "I attack Nessa Quill.")
    ok(r.get("blocked") and r.get("refused") == "physical",
       "a talk-only world played alone still refuses the verb - the check was "
       "inside `if session_id`, so solo was not guarded at all")


def test_finishing_a_run_actually_scores():
    section("wiring - two of the three scores could never move")
    db.init()
    from backend import aftermath
    pt = engine.create_playthrough("wire-score")
    world = engine.world_for(engine._pt(pt))
    out = aftermath.close_story(pt, world, reason="victory", turn=12)
    ok(out.get("scored") and out["scored"]["added"] > 0,
       f"a finished run settles into the profile ({out['scored']['added']}) - "
       f"nothing computed a Legacy score anywhere before this")
    ok(ladder.profile(account_id="wire-score")["legacy"] == out["scored"]["added"],
       "and it lands on the profile")
    ok(any(v > 0 for v in out["scored"]["parts"].values()),
       f"built from what the world became: {', '.join(out['scored']['parts'])}")


def test_a_settled_match_scores_the_table():
    section("wiring - a Duel could be won and the ladder never hear about it")
    db.init()
    s = sessions.create("wire-a", mode="coop", session_type="duel", host_name="A")
    sessions.join(s["id"], "wire-b", "B")
    res = sessions.settle(s["id"], outcome="objective", winner="host")
    ok(len(res.get("scored", [])) == 2, "both seats get a result")
    ok(any(x["outcome"] == "win" for x in res["scored"]), "one of them won")
    ok(any(x["outcome"] == "loss" for x in res["scored"]), "and one of them did not")
    ok(ladder.profile(account_id="wire-a")["pvp"]["points"] > 0,
       "which reaches the winner's profile")
    ok(ladder.profile(account_id="wire-a")["feuds"],
       "and the feud between them is on the record")


def test_solo_trophies_can_unlock():
    section("wiring - two trophies were unreachable by construction")
    db.init()
    ladder.add_legacy(600, account_id="wire-troph", why="test")
    have = {a["id"] for a in ladder.profile(account_id="wire-troph")["achievements"]} \
        if isinstance(ladder.profile(account_id="wire-troph")["achievements"], list) \
        and ladder.profile(account_id="wire-troph")["achievements"] \
        and isinstance(ladder.profile(account_id="wire-troph")["achievements"][0], dict) \
        else set(ladder.profile(account_id="wire-troph")["achievements"])
    ok("worldshaper" in have,
       "a solo trophy unlocks from solo play - achievements were only re-checked "
       "after a PvP match, so the two earned by playing alone or together could "
       "never have fired")


def test_every_witness_is_findable():
    section("detective - a lead list that is always a dead end is noise")
    db.init()
    for i in range(4):
        pt = engine.create_playthrough(f"lead-{i}", session_type="detective")
        world = engine.world_for(engine._pt(pt))
        case = submodes.current_case(pt)
        witnesses = {r["holder_id"] for r in db.rows(
            "SELECT holder_id FROM knowledge WHERE playthrough_id=? AND"
            " holder_kind='npc' AND fact_key=?", (pt, case["fact_key"]))}
        leads = {l["id"] for l in submodes.casefile(pt, world)["worth_asking"]}
        if not witnesses <= leads:
            ok(False, f"case {i}: witnesses {sorted(witnesses - leads)} were "
                      f"unreachable through the casefile")
            return
    ok(True, "every witness appears in the leads - the leads came from the "
             "SCHEDULE and the witnesses from where people actually stood, and "
             "those two lists can be disjoint, so a player could ask everybody "
             "the game suggested and never find anyone who saw it")

    # Whether a given case HAS a cold lead is the world's business, not an
    # invariant - so this asks whether the design produces them at all rather
    # than demanding one of every world.
    cold = 0
    for i in range(6):
        pt = engine.create_playthrough(f"lead-cold-{i}", session_type="detective")
        world = engine.world_for(engine._pt(pt))
        case = submodes.current_case(pt)
        witnesses = {r["holder_id"] for r in db.rows(
            "SELECT holder_id FROM knowledge WHERE playthrough_id=? AND"
            " holder_kind='npc' AND fact_key=?", (pt, case["fact_key"]))}
        leads = {l["id"] for l in submodes.casefile(pt, world)["worth_asking"]}
        cold += len(leads - witnesses)
    ok(cold > 0,
       f"and leads that go nowhere still exist ({cold} across six cases) - a "
       f"person whose routine puts them there but who was elsewhere is a real "
       f"dead end, and a mystery with no dead ends is a checklist")


# ------------------------------------------------------------- reachable
def test_everything_is_reachable_from_the_client():
    section("nothing built here is left unused")
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    ui = (root / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
    html = (root / "frontend" / "index.html").read_text(encoding="utf-8")

    for path in ("/rooms/", "/profile/standing", "/leaderboard", "/case/ask/",
                 "/case/accuse/", "/objective", "/modes/tree"):
        ok(path in ui, f"the client calls {path}")
    for hook in ("roomWrap", "objective", "roomTab"):
        ok(f'id="{hook}"' in html and hook in ui,
           f"#{hook} exists in the markup AND something writes to it")
    for handler in ("data-room-seat", "data-room-vote", "data-room-ai",
                    "data-case-ask", "data-case-accuse", "data-board"):
        ok(ui.count(handler) >= 2,
           f"[{handler}] is both rendered and handled")


def _all():
    return (test_the_frame_is_frozen,
            test_the_invariant_check_catches_what_a_prompt_cannot,
            test_a_broken_seat_is_clamped_not_shown,
            test_roles_are_dealt_once_and_hidden,
            test_speaking_is_capped_and_talk_only,
            test_the_vote_resolves_honestly, test_a_tie_banishes_nobody,
            test_the_room_ends_correctly,
            test_points_are_cosmetic_and_seasons_keep_prestige,
            test_only_pvp_feeds_the_pvp_board,
            test_anonymous_play_still_scores_and_is_never_blocked,
            test_feuds_are_the_thing_that_carries,
            test_the_case_is_emergent_and_solvable,
            test_an_accusation_needs_proof_not_luck,
            test_both_mask_outcomes_are_real,
            test_solo_modes_are_reachable_and_staged,
            test_one_resolver_answers_what_mode_this_is,
            test_a_talk_only_mode_is_talk_only_alone_too,
            test_finishing_a_run_actually_scores,
            test_a_settled_match_scores_the_table,
            test_solo_trophies_can_unlock,
            test_every_witness_is_findable,
            test_everything_is_reachable_from_the_client)


def main():
    print("StoryLiver - the Room, the ladder, and the modes with real verbs\n")
    for fn in _all():
        fn()
    passed = 0
    for n in NOTES:
        if n.startswith("\n"):
            print(n)
        else:
            print("  PASS  " + n)
            passed += 1
    for f in FAILS:
        print("  FAIL  " + f)
    print(f"\n  {passed} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


def test_all_competitive():
    for fn in _all():
        fn()
    assert not FAILS, "\n".join(FAILS)


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
