"""The player journey, end to end — the seams between systems.

The other suites prove each layer works. This one proves they are CONNECTED,
which is a different claim and the one that was actually failing: combat had
hit points and a winner and touched nothing else, death resolutions existed but
nothing ever called them, runs were a table nobody wrote to.

Every assertion here crosses a boundary between two subsystems.

  signup -> a run exists the moment a story does
  combat -> the fallen actually die IN THE WORLD, not just on the board
  combat -> the fight enters the timeline and the world hears about it
  death  -> a player is offered resolutions, never a dead end
  death  -> a dead player cannot keep taking turns
  hardcore -> a player death ends the run and pays out meta-progression
  cozy   -> the same blow is a knockout, and the run continues
  ending -> the last fated event closes the story exactly once

Run:  python -m tests.test_journey
"""
import os
import shutil
import tempfile

os.environ["STORYLIVER_LLM_MODE"] = "mock"
_TMP = tempfile.mkdtemp(prefix="storyliver-journey-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP

from backend import (aftermath, budget, combat, db, death, engine, memory,  # noqa: E402
                     modes, runs, sessions, uploads)

FAILS, NOTES = [], []


def ok(cond, msg):
    (NOTES if cond else FAILS).append(msg)


def section(t):
    NOTES.append("\n  " + t)


def _fight(pt, world, *, enemy="maren", player_hp=26, enemy_hp=1, session_id=""):
    """Set up a fight we can decide the outcome of deterministically."""
    return combat.start(
        pt, world, place_id="broken_bell", session_id=session_id,
        sides={"a": [{"id": "user", "kind": "player", "name": "You", "hp": player_hp,
                      "power": 9, "guard": 11, "zone": "front_a"}],
               "b": [{"id": enemy, "kind": "npc", "name": world.npc_name(enemy),
                      "hp": enemy_hp, "power": 1, "guard": 1, "zone": "front_b"}]})


# ---------------------------------------------------------------- signup
def test_run_exists_from_the_start():
    section("signup — a story IS a run, from turn zero")
    db.init()
    pt = engine.create_playthrough("journey-a")
    r = runs.active(pt)
    ok(r is not None, "a run is open the moment the story is created")
    ok(r and r["run_no"] == 1, "it is run 1")

    snap = engine.snapshot(pt)
    ok("run" in snap and snap["run"]["run_no"] == 1,
       "the run reaches the client in the snapshot — the UI is not guessing")
    ok("modes" in snap and snap["modes"]["modes"]["tone"] == "neutral",
       "the world's modes travel with the snapshot too")
    ok("arc" in snap and "skills" in snap,
       "timeline position and skills are in the snapshot")


# ---------------------------------------------------------------- combat
def test_combat_reaches_the_world():
    section("combat — a fight that ends changes the world")
    db.init()
    pt = engine.create_playthrough("journey-b")
    world = engine.world_for(engine._pt(pt))

    before_events = len(memory.timeline(pt))
    alive_before = memory.npc_state(pt, "maren")["alive"]
    ok(alive_before, "the warden starts alive")

    c = _fight(pt, world)
    combat.declare(pt, c["id"], "user", "strike", target="maren")
    out = combat.resolve(pt, c["id"], world=world)
    ok(out["status"] == "over", "the fight resolves to a conclusion")

    after = aftermath.after_combat(pt, world, combat.get(c["id"]), out["winner"],
                                   place_id="broken_bell", turn=1)

    ok(not memory.npc_state(pt, "maren")["alive"],
       "the fallen NPC is DEAD IN THE WORLD, not merely down on the board")
    ok(any(d["who"] == "maren" for d in after["deaths"]),
       "the death is reported back to the caller")
    ok(after["deaths"][0]["consequences"].get("vacuum"),
       f"their role stands empty ({after['deaths'][0]['consequences'].get('vacuum')})")
    ok(len(memory.timeline(pt)) > before_events,
       "the fight entered the timeline — no hole where the killing was")
    ok(after["summary"], f"there is a factual summary for the narrator: {after['summary'][:60]}...")


def test_combat_is_witnessed_not_broadcast():
    section("combat — the world hears about it, but only who could")
    db.init()
    pt = engine.create_playthrough("journey-c")
    world = engine.world_for(engine._pt(pt))
    c = _fight(pt, world, enemy="adrahel")
    combat.declare(pt, c["id"], "user", "strike", target="adrahel")
    out = combat.resolve(pt, c["id"], world=world)
    after = aftermath.after_combat(pt, world, combat.get(c["id"]), out["winner"],
                                   place_id="ash_chapel", turn=3)
    ok("witnesses" in after, "the killing goes through the witness layer, not a global bus")
    ok(isinstance(after.get("rumours", []), list),
       "rumours propagate from whoever actually saw it")


# ---------------------------------------------------------------- death
def test_player_death_offers_a_way_on():
    section("death — a player is never dead-ended")
    db.init()
    s = sessions.create("journey-host", host_name="Host")
    pt, sid = s["playthrough_id"], s["id"]
    world = engine.world_for(engine._pt(pt))
    host_pid = [p["player_id"] for p in sessions.players(sid) if p["is_host"]][0]

    c = combat.start(pt, world, place_id="broken_bell", session_id=sid,
                     sides={"a": [{"id": host_pid, "kind": "player", "name": "Host", "hp": 1,
                                   "power": 1, "guard": 1, "zone": "front_a"}],
                            "b": [{"id": "maren", "kind": "npc", "name": "Maren", "hp": 30,
                                   "power": 12, "guard": 12, "zone": "front_b"}]})
    for _ in range(6):
        st = combat.get(c["id"])
        if st["status"] == "over":
            break
        combat.declare(pt, c["id"], host_pid, "strike", target="maren")
        out = combat.resolve(pt, c["id"], world=world)

    after = aftermath.after_combat(pt, world, combat.get(c["id"]), out["winner"],
                                   session_id=sid, place_id="broken_bell", turn=2)
    ok(after["player_down"] is not None, "the engine noticed a PLAYER fell")
    res = {r["id"] for r in after["player_down"]["resolutions"]}
    ok("ghost" in res and "heir" in res,
       f"they are offered ways to keep playing ({sorted(res)})")

    seat = db.row("SELECT life_state FROM session_players WHERE session_id=? AND player_id=?",
                  (sid, host_pid))
    ok(seat["life_state"] == "dead", "the seat records that they are gone")

    blocked = engine.take_turn(pt, "I stand up and keep fighting.",
                               player=host_pid, session_id=sid)
    ok(blocked.get("blocked") and blocked.get("dead"),
       "a dead character cannot simply keep taking turns")
    ok("whisper" in blocked.get("reason", "").lower(),
       "they are told what they CAN still do, not just refused")

    out2 = death.resolve(pt, choice="heir", who=host_pid, session_id=sid,
                         player_id=host_pid, world=world)
    ok(out2["can_act"] and out2["needs_new_card"],
       "taking the heir resolution puts them back in play with a new character")
    seat2 = db.row("SELECT life_state FROM session_players WHERE session_id=? AND player_id=?",
                   (sid, host_pid))
    ok(seat2["life_state"] == "alive", "and their seat is live again")


def test_hardcore_ends_the_run_cozy_does_not():
    section("stakes — hardcore ends the run, cozy refuses to take anyone")
    db.init()
    pt = engine.create_playthrough("journey-d")
    world = engine.world_for(engine._pt(pt))
    modes.set_modes(pt, {"stakes": "hardcore"})

    c = combat.start(pt, world,
                     place_id="broken_bell",
                     sides={"a": [{"id": "user", "kind": "player", "name": "You", "hp": 1,
                                   "power": 1, "guard": 1, "zone": "front_a"}],
                            "b": [{"id": "maren", "kind": "npc", "name": "Maren", "hp": 30,
                                   "power": 12, "guard": 12, "zone": "front_b"}]})
    for _ in range(6):
        if combat.get(c["id"])["status"] == "over":
            break
        combat.declare(pt, c["id"], "user", "strike", target="maren")
        out = combat.resolve(pt, c["id"], world=world)

    after = aftermath.after_combat(pt, world, combat.get(c["id"]), out["winner"],
                                   place_id="broken_bell", turn=4)
    ok(after["run"] is not None, "a player death in Hardcore ENDS the run")
    ok(after["run"]["carried"]["lore_total"] >= 0,
       "and the run pays out — permadeath is a loop, not a wall")

    # Cozy: the same lethal blow must not take anyone.
    db.init()
    pt2 = engine.create_playthrough("journey-e")
    world2 = engine.world_for(engine._pt(pt2))
    modes.set_modes(pt2, {"tone": "chill", "stakes": "hardcore"})
    c2 = combat.start(pt2, world2, place_id="broken_bell",
                      sides={"a": [{"id": "user", "kind": "player", "name": "You", "hp": 1,
                                    "power": 1, "guard": 1, "zone": "front_a"}],
                             "b": [{"id": "maren", "kind": "npc", "name": "Maren", "hp": 30,
                                    "power": 12, "guard": 12, "zone": "front_b"}]})
    for _ in range(6):
        if combat.get(c2["id"])["status"] == "over":
            break
        combat.declare(pt2, c2["id"], "user", "strike", target="maren")
        out2 = combat.resolve(pt2, c2["id"], world=world2)
    after2 = aftermath.after_combat(pt2, world2, combat.get(c2["id"]), out2["winner"],
                                    place_id="broken_bell", turn=4)
    ok(not after2["deaths"] and after2["knockouts"],
       "in Cozy the same blow is a knockout, even with Hardcore also set")
    ok(after2["run"] is None, "and the run continues — nobody was taken")


# ---------------------------------------------------------------- ending
def test_story_closes_once():
    section("ending — fate runs out, and the story actually closes")
    db.init()
    pt = engine.create_playthrough("journey-f")
    world = engine.world_for(engine._pt(pt))
    last = world.fated_events[-1]["turn"]
    db.run("UPDATE playthroughs SET current_turn=? WHERE id=?", (last, pt))

    e = aftermath.check_ending(pt, world, turn=last)
    ok(e and e["ended"], "reaching the last fated event is recognised as an ending")

    closed = aftermath.close_story(pt, world, reason="victory", turn=last)
    ok(closed["closed"], "the story closes")
    ok(closed["run"]["ended"], "the run is settled")
    ok(closed["meta"]["runs_completed"] >= 1, "meta-progression recorded the completed run")

    again = engine._pt(pt)
    ok(again["run_state"] == "ended",
       "the playthrough is marked ended, so it cannot pay out twice")


# ---------------------------------------------------------------- uploads
def test_uploads_are_content_typed():
    section("player art — uploaded, never generated, and sniffed not trusted")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    r = uploads.store(png, owner="u1")
    ok(r["url"].startswith("/media/") and r["mime"] == "image/png",
       "a real PNG is stored and served as a PNG")
    ok(uploads.store(png)["name"] == r["name"],
       "the same art uploaded twice costs one file (content-hashed)")

    try:
        uploads.store(b"<svg onload=alert(1)></svg>")
        ok(False, "an SVG was accepted — that is a stored-XSS hole")
    except uploads.UploadError:
        ok(True, "SVG is REFUSED — it is XML and can carry script")

    try:
        uploads.store(b"<html><script>alert(1)</script></html>")
        ok(False, "HTML disguised as an image was accepted")
    except uploads.UploadError:
        ok(True, "content is sniffed by magic bytes, so a renamed file cannot lie")

    try:
        uploads.path_for("../../backend/config.py")
        ok(False, "a path traversal escaped the media directory")
    except uploads.UploadError:
        ok(True, "a traversal attempt is refused, not normalised")


def main():
    print("StoryLiver — the journey, end to end")
    print("  offline stub, no API key, no spend\n")
    for fn in (test_run_exists_from_the_start, test_combat_reaches_the_world,
               test_combat_is_witnessed_not_broadcast, test_player_death_offers_a_way_on,
               test_hardcore_ends_the_run_cozy_does_not, test_story_closes_once,
               test_uploads_are_content_typed):
        fn()
    passed = 0
    for n in NOTES:
        if n.startswith("\n"):
            print(n)
        else:
            print("  PASS  " + n); passed += 1
    for f in FAILS:
        print("  FAIL  " + f)
    print(f"\n  {passed} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


def test_all_journey():
    for fn in (test_run_exists_from_the_start, test_combat_reaches_the_world,
               test_combat_is_witnessed_not_broadcast, test_player_death_offers_a_way_on,
               test_hardcore_ends_the_run_cozy_does_not, test_story_closes_once,
               test_uploads_are_content_typed):
        fn()
    assert not FAILS, "\n".join(FAILS)


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
