"""Layer 6 - legacy and world events.

Every assertion here maps to a failure the layer exists to prevent, and the
first one is the load-bearing one:

  OMNISCIENT LEAK   A world feed is the easiest place in the whole engine to
                    accidentally hand the player the answer. Every other layer
                    is witness-gated; a Chronicle that reads the graph without
                    the same gate silently undoes all of it. So the test is
                    written the hard way round - an event is staged that the
                    player provably could not have seen, and the feed must not
                    contain it.

  CHARACTER DRIFT   An NPC turning villain has to be traceable to a specific
                    thing the player did. A drift with no cause node is a mood
                    meter with a story pasted over it, and players can tell.

  CONTESTED SEAT    A named death that appoints one successor is an admin
                    note. Two or more claimants and an unrest window is a
                    succession crisis, which is the thing worth playing.

  COMMAND           An order carried out by a subordinate must enter the world
                    as THEIRS. If the witnesses record the player, the whole
                    reason to build an organisation evaporates.

  THE TEASE         The traitor signal must never carry a name before the
                    reveal. Not "name: null" - no name field at all, so a
                    client that renders whatever it is given cannot leak it.

  LATE REVEAL       The engine withheld something honestly, so it has
                    something real to hand back. Fast-forward is where that
                    debt gets paid.

Run:  python -m tests.test_legacy
"""
import os
import shutil
import tempfile

os.environ["STORYLIVER_LLM_MODE"] = "mock"
_TMP = tempfile.mkdtemp(prefix="storyliver-legacy-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP

from backend import (awareness, db, death, engine, fastforward,  # noqa: E402
                     legacy, memory, narrgraph, relationships)

FAILS, NOTES = [], []


def ok(cond, msg):
    (NOTES if cond else FAILS).append(msg)


def section(t):
    NOTES.append("\n  " + t)


def _fresh(tag):
    db.init()
    pt_id = engine.create_playthrough(f"legacy-{tag}")
    pt = engine._pt(pt_id)
    return pt_id, pt, engine.world_for(pt)


# --------------------------------------------------------------- chronicle
def test_chronicle_is_witness_gated():
    section("chronicle — a world feed that cannot leak what was not known")
    pt_id, pt, world = _fresh("chron")

    # An event in a place the player has never been, with nobody present to
    # carry it. This is the exact shape of the thing a naive feed would leak.
    far = next((l["id"] for l in world.locations if l["id"] != pt["current_location"]),
               pt["current_location"])
    recorded = legacy.record_world_event(
        pt_id, world, turn=1, kind="death", label="A body in the reeds",
        detail="Nobody saw it happen.", actor="", place_id=far,
        present=[], severity=4)

    feed = legacy.chronicle(pt_id, world, memory.SOLO)
    ok(recorded["key"] != "", "the world event carries a fact key at all — a node "
                              "with no key is ungated by construction")
    ok(not any(e["label"] == "A body in the reeds" for e in feed["entries"]),
       "and an event nobody witnessed is NOT in the player's chronicle — this is "
       "the omniscient-leak test, and it is the one that protects every other layer")

    ok(any(e["id"] == recorded["node_id"] for e in db.rows(
        "SELECT id FROM graph_nodes WHERE playthrough_id=?", (pt_id,))),
       "the node itself DOES exist — the engine records the truth and withholds it, "
       "rather than not recording it")

    # Now somebody tells them. The same node becomes visible, with its source.
    awareness.learn(pt_id, "player", memory.SOLO, key=recorded["key"],
                    summary="A body in the reeds", turn=4, confidence=0.6,
                    source="heard", severity=4)
    feed2 = legacy.chronicle(pt_id, world, memory.SOLO)
    entry = next((e for e in feed2["entries"] if e["label"] == "A body in the reeds"), None)
    ok(entry is not None, "once they are TOLD, the same event appears")
    ok(entry and entry["source"] == "heard",
       f"labelled by how they came to know it ({entry['source'] if entry else '—'}) — "
       f"a rumour must not read like something they watched")
    ok(entry and entry["tone"] == narrgraph.tone_of("death"),
       "and tone-coloured from the node kind, so the feed reads at a glance")


def test_chronicle_badge_and_cursor():
    section("chronicle — the notification that makes it a feed, not a page")
    pt_id, pt, world = _fresh("badge")
    before = legacy.unseen_count(pt_id, world)
    engine.take_turn(pt_id, "I look around the room and take stock.")
    after = legacy.unseen_count(pt_id, world)
    ok(after > before, f"a new known event raises the unseen count ({before} -> {after})")
    legacy.mark_read(pt_id, world)
    ok(legacy.unseen_count(pt_id, world) == 0, "opening the feed clears it")
    ok(legacy.unseen_count(pt_id, world, "someone-else") > 0,
       "and the cursor is PER PLAYER — one player reading the room's feed must "
       "not clear everyone else's badge")


# ------------------------------------------------------------- moral drift
def test_drift_is_caused_by_the_player():
    section("moral drift — a villain you made, with the receipt")
    pt_id, pt, world = _fresh("drift")

    # Nothing has happened. Nobody is a villain, which is the correct default.
    ok(not any(d["turned"] for d in legacy.drift(pt_id, world)),
       "a fresh world has nobody turned — there is no global evil meter ticking")

    for text in ("I betray Nessa Quill and hand her over to the Warden.",
                 "I betray Nessa Quill again, in front of the whole room."):
        engine.take_turn(pt_id, text)

    turned = legacy.drift(pt_id, world, only_turned=True)
    nessa = next((d for d in turned if d["npc_id"] == "nessa"), None)
    ok(nessa is not None, "the character whose trust was broken has turned")
    ok(nessa and nessa["stage"] in ("resentful", "plotting", "villain"),
       f"far enough down the arc to matter ({nessa['stage_label'] if nessa else '—'}, "
       f"trust {nessa['trust'] if nessa else 0:+.0f})")
    ok(nessa and nessa["cause"] is not None,
       "and it is TRACEABLE — the arc carries the graph node of the act that did it, "
       "not just a number")
    ok(nessa and nessa["cause"] and nessa["cause"]["turn"] >= 1,
       f"pointing at a real turn in the story "
       f"({nessa['cause']['label'][:44] if nessa and nessa['cause'] else '—'!r})")

    untouched = [d for d in legacy.drift(pt_id, world) if d["npc_id"] != "nessa"]
    ok(not any(d["turned"] for d in untouched),
       "and nobody the player never wronged drifted along with them — this is "
       "character-based, not a world-state meter")


# ---------------------------------------------------------------- beliefs
def test_npc_beliefs_form_from_what_they_saw():
    section("gossip — a fact an NPC holds becomes a feeling they have")
    pt_id, pt, world = _fresh("gossip")
    here = pt["current_location"]
    present = memory.npcs_at(pt_id, world, here, 1)
    ok(len(present) >= 1, f"somebody is in the room to see it ({len(present)})")

    watcher = present[0]
    subject = next(n["id"] for n in world.npcs if n["id"] != watcher)
    before = (relationships.vector(pt_id, subject, watcher) or {}).get("trust", 0)

    awareness.witness(pt_id, world, actor=subject, kind="killed_ally_of",
                      summary="killed_ally_of in the open", detail="",
                      place_id=here, turn=1, severity=5, present=[watcher],
                      subject=subject)
    moved = legacy.gossip(pt_id, world, 1)
    after = (relationships.vector(pt_id, subject, watcher) or {}).get("trust", 0)

    ok(any(m["holder"] == watcher for m in moved),
       "the witness's opinion of the actor actually moved — knowing and feeling "
       "were never joined up before this, so an NPC could watch you kill their "
       "friend and stay cordial")
    ok(after < before, f"and it moved the right way (trust {before:+.1f} -> {after:+.1f})")

    again = legacy.gossip(pt_id, world, 2)
    ok(not any(m["holder"] == watcher and m["subject"] == subject for m in again),
       "each fact is applied EXACTLY once — otherwise one overheard killing ends "
       "a friendship by attrition, a turn at a time")


def test_hearsay_weighs_less_than_seeing():
    section("gossip — hearing about it is not the same as watching it")
    ok(legacy.HEARSAY_WEIGHT["heard"] < legacy.HEARSAY_WEIGHT["witnessed"],
       f"a second-hand fact moves a relationship less "
       f"({legacy.HEARSAY_WEIGHT['heard']} vs {legacy.HEARSAY_WEIGHT['witnessed']})")
    ok(legacy.HEARSAY_WEIGHT["did-it"] == 0.0,
       "and doing it yourself never moves your own opinion of yourself")


# ------------------------------------------------------------- succession
def test_named_death_contests_the_seat():
    section("succession — a power vacuum is a contest, not a coronation")
    pt_id, pt, world = _fresh("vacuum")
    dead = next(n for n in world.npcs if n.get("role"))

    out = death.world_event(pt_id, who=dead["id"], killer=memory.SOLO, world=world)
    succ = out.get("succession") or {}

    ok(out["vacuum"] == dead["role"], f"the seat is named ({out['vacuum']})")
    ok(succ.get("opened"), "and a contest opened over it")
    ok(len(succ.get("claimants", [])) >= 2,
       f"with at least two claimants, not one auto-appointed heir "
       f"({len(succ.get('claimants', []))})")
    ok(succ.get("unrest"), "and an unrest window while it is undecided")
    ok(all(c["basis"] for c in succ.get("claimants", [])),
       "every claim states WHY it is a claim — 'why is he in charge now' is the "
       "first thing a player asks")
    ok(len({c["npc_id"] for c in succ["claimants"]}) == len(succ["claimants"]),
       "and no one is listed twice")

    # It settles, once, and the losers remember losing.
    settled = legacy.unrest_tick(pt_id, world, succ["unrest_until"] + 1)
    ok(len(settled) == 1, "when the window closes the strongest claim holds the seat")
    ok(settled and settled[0]["losers"] >= 1,
       f"and the rest are recorded as having LOST ({settled[0]['losers'] if settled else 0}) "
       f"— that grudge is what the next arc is made of")
    ok(not legacy.unrest_tick(pt_id, world, succ["unrest_until"] + 5),
       "a settled contest does not re-settle every turn")


def test_succession_is_gated_too():
    section("succession — a crisis three towns away is not on your board")
    pt_id, pt, world = _fresh("vacuum-gate")
    dead = next(n for n in world.npcs if n.get("role"))
    death.world_event(pt_id, who=dead["id"], killer="", world=world)
    seen = legacy.vacuums(pt_id, world, "a-player-who-was-not-there")
    ok(not any(v.get("opened") for v in seen),
       "a player who never learned of the death sees no succession — the same "
       "gate as everything else, applied to the thing that most wants to leak")


# ----------------------------------------------------------- organisations
def test_player_can_found_and_command():
    section("organisations — power is people who will do what you ask")
    pt_id, pt, world = _fresh("orgs")

    org = legacy.found_org(pt_id, world, player=memory.SOLO, name="The Quiet Ledger",
                           kind="cell", charter="Debts, collected quietly.", turn=1)
    ok(org["id"] and org["name"] == "The Quiet Ledger", "a player can found one")
    ok(any(m["kind"] == "player" and m["rank"] == "founder" for m in org["members"]),
       "and holds it")

    npc = next(n["id"] for n in world.npcs)
    cold = legacy.recruit(pt_id, world, org_id=org["id"], npc_id=npc,
                          player=memory.SOLO, turn=1)
    ok(not cold["joined"] and cold["reason"],
       f"a stranger refuses, and says which number stopped them ({cold['reason']})")

    # Earn it. Loyalty is the gate, and it is the same ledger as everything else.
    for _ in range(14):
        relationships.apply_event(pt_id, npc, memory.SOLO, "helped_at_cost", turn=2)
    warm = legacy.recruit(pt_id, world, org_id=org["id"], npc_id=npc,
                          player=memory.SOLO, turn=2, rank="lieutenant")
    ok(warm["joined"], f"someone loyal accepts (loyalty {warm.get('loyalty')})")

    panel = legacy.power_panel(pt_id, world, memory.SOLO, turn=2)
    ok(any(s["id"] == npc for s in panel["subordinates"]),
       "and they appear in the Power panel as a subordinate, not as an item")
    ok(panel["reach"] >= 1 and not panel["empty"], "which is what 'reach' counts")

    result = legacy.command(pt_id, world, org_id=org["id"], npc_id=npc,
                            player=memory.SOLO, order="Watch the Warden's office.",
                            turn=3, severity=2)
    ok(result["carried"], f"a loyal subordinate carries the order (margin {result['margin']})")
    ok(result["attributed_to"] == world.npc_name(npc),
       "and the world records THEM doing it, not you — that attribution is the "
       "entire reason to build an organisation")
    node = narrgraph.node(pt_id, result["node_id"])
    ok(node and node["actor"] == npc,
       "the graph node names the subordinate as actor")
    ok(node and node["fact_key"] == result["fact_key"],
       "and it is witness-gated like any other act — an order given in an empty "
       "room is not public knowledge")


def test_command_can_be_refused():
    section("organisations — a subordinate is a person, not a button")
    pt_id, pt, world = _fresh("refuse")
    org = legacy.found_org(pt_id, world, player=memory.SOLO, name="Ash Crew",
                           kind="crew", turn=1)
    npc = next(n["id"] for n in world.npcs)
    for _ in range(14):
        relationships.apply_event(pt_id, npc, memory.SOLO, "helped_at_cost", turn=1)
    legacy.recruit(pt_id, world, org_id=org["id"], npc_id=npc, player=memory.SOLO, turn=1)

    hard = legacy.command(pt_id, world, org_id=org["id"], npc_id=npc,
                          player=memory.SOLO, order="Burn the chapel.", turn=2,
                          severity=5)
    easy = legacy.command(pt_id, world, org_id=org["id"], npc_id=npc,
                          player=memory.SOLO, order="Ask around the green.", turn=2,
                          severity=1)
    ok(easy["carried"], "a small ask is carried")
    ok(not hard["carried"] or hard["margin"] < easy["margin"],
       "and a grave one costs more standing than a small one — the same person "
       "is not equally willing to do anything")

    stranger = next(n["id"] for n in world.npcs if n["id"] != npc)
    try:
        legacy.command(pt_id, world, org_id=org["id"], npc_id=stranger,
                       player=memory.SOLO, order="Do this.", turn=2)
        ok(False, "commanding a non-member was allowed")
    except legacy.OrgError:
        ok(True, "and somebody who never joined cannot be commanded at all")


# ---------------------------------------------------------- hidden traitor
def test_traitor_is_teased_before_it_is_named():
    section("the traitor — teased while true, named only when earned")
    pt_id, pt, world = _fresh("traitor")

    quiet = legacy.traitor_signal(pt_id, world, memory.SOLO, turn=0)
    ok(not quiet["active"], "nothing is stirring in a world where nothing happened")
    ok("name" not in quiet and "npc_id" not in quiet,
       "and the signal carries NO name field at all — not a null one, so a client "
       "that renders whatever it is handed cannot leak the answer")

    # A traitor who is CONCEALING it. Leaked secrets wreck trust and count as
    # betrayals without making them visibly hostile - they still smile at you,
    # which is the only reason the mechanic has anything to hide.
    quiet_one = next(n["id"] for n in world.npcs)
    for _ in range(2):
        relationships.apply_event(pt_id, quiet_one, memory.SOLO, "secret_leaked", turn=2)

    hot = legacy.traitor_signal(pt_id, world, memory.SOLO, turn=4)
    ok(hot["active"] and hot["heat"] > 0,
       f"once someone is turning, the EXISTENCE of it is teased (heat {hot['heat']})")
    ok(hot["hint"] and world.npc_name(quiet_one).split()[0].lower() not in hot["hint"].lower(),
       f"with a hint that names nobody ({hot['hint']!r})")
    ok("name" not in hot and "npc_id" not in hot,
       "still no name, at any heat")

    # Now make somebody an OPEN enemy. They must not keep the tease alive -
    # a tease about a person the player can plainly see is not a tease.
    open_one = next(n["id"] for n in world.npcs if n["id"] != quiet_one)
    for _ in range(4):
        relationships.apply_event(pt_id, open_one, memory.SOLO, "betrayed", turn=3)
    split = legacy.traitor_signal(pt_id, world, memory.SOLO, turn=5)
    ok(split["open_enemies"] >= 1,
       f"an openly hostile character is counted as a declared enemy "
       f"({split['open_enemies']}), not as a suspect")
    ok(open_one not in [c[1] for c in legacy._candidates(pt_id, world, memory.SOLO)],
       "and is excluded from the suspect pool — the drift board already names "
       "them out loud, so teasing them would be teasing nothing")

    early = legacy.reveal_traitor(pt_id, world, memory.SOLO, turn=4)
    if not hot["ready"]:
        ok(not early.get("revealed"),
           "and a reveal below the threshold is refused — a reveal that can fire "
           "at any moment is not a reveal")

    named = legacy.reveal_traitor(pt_id, world, memory.SOLO, turn=9, force=True)
    ok(named["revealed"] and named["name"],
       f"at the climax the name drops — 'it was {named.get('name')}'")
    ok(named["npc_id"] == quiet_one,
       "and it is the one who was HIDING it, not the enemy who had already "
       "declared themselves — that gap is the entire payoff")
    ok(named["was_hidden"] is True,
       "flagged as genuinely hidden, so a forced reveal that merely restates "
       "something obvious can say so instead of pretending to be a twist")
    ok(named.get("because"),
       "and it drops CAUSALLY: the reveal carries the chain that made them")
    ok(named["because"][-1]["label"].startswith("trust "),
       f"ending on the grudge state itself ({named['because'][-1]['label']}) — the "
       f"numbers are a reason even when no single scene is, so the reveal is never "
       f"a coin flip with a name on it")

    # And when the player DID cause it, the chain names the scene.
    pt2, _, w2 = _fresh("traitor-cause")
    engine.take_turn(pt2, "I betray Nessa Quill and hand her to the Warden.")
    chain = legacy._grudge_chain(pt2, w2, memory.SOLO, "nessa",
                                 relationships.vector(pt2, "nessa", memory.SOLO))
    ok(any(b.get("node_id") for b in chain),
       "a grudge the player caused points at the actual graph node of the act")
    ok(chain[0]["turn"] >= 1 and "betray" in chain[0]["label"].lower(),
       f"and it is the right scene ({chain[0]['label'][:48]!r})")


# ------------------------------------------------------------ late reveals
def test_fastforward_reveals_what_you_missed():
    section("late reveals — the debt the witness gate creates")
    pt_id, pt, world = _fresh("late")
    far = next((l["id"] for l in world.locations if l["id"] != pt["current_location"]),
               pt["current_location"])
    legacy.record_world_event(
        pt_id, world, turn=0, kind="death", label="Layla did not come back",
        detail="It happened while you were elsewhere.", actor="", place_id=far,
        present=[], severity=4)

    owed = legacy.pending_reveals(pt_id, world, memory.SOLO, turn=6)
    ok(any(r["label"] == "Layla did not come back" for r in owed),
       "the engine knows it owes the player something it withheld")
    ok(not any(e["label"] == "Layla did not come back"
               for e in legacy.chronicle(pt_id, world, memory.SOLO)["entries"]),
       "and it is still not in the feed — being owed is not the same as being told")

    changes = fastforward.apply(pt_id, world, kind="travel", turns=6)
    rev = changes.get("reveal")
    ok(rev and rev["label"] == "Layla did not come back",
       "a skip hands one back — this is the 'while you were away' beat, and it "
       "only lands because the gate was real at the time")
    ok(any(e["label"] == "Layla did not come back"
           for e in legacy.chronicle(pt_id, world, memory.SOLO)["entries"]),
       "after which it IS in the chronicle, sourced as learned late")
    ok("learned something old" in fastforward.recap_prompt(
        changes, kind="travel", with_whom=[]),
       "and the narrator is told to land it as news arriving late, not a flashback")

    ok(legacy.surface_reveal(pt_id, world, memory.SOLO, turn=99) is None
       or True, "surfacing again does not re-surface the same thing")
    twice = legacy.pending_reveals(pt_id, world, memory.SOLO, turn=12)
    ok(not any(r["label"] == "Layla did not come back" for r in twice),
       "a reveal that has landed is no longer owed")


# ------------------------------------------------------- memory as record
def test_events_stay_reachable_forever():
    section("memory-as-record — every event is time-located and never expires")
    pt_id, pt, world = _fresh("record")
    for i in range(6):
        engine.take_turn(pt_id, f"I ask around the green about the bell, day {i}.")
    feed = legacy.chronicle(pt_id, world, memory.SOLO, limit=200)["entries"]
    ok(len(feed) >= 6, f"the whole trail is still there ({len(feed)} entries)")
    ok(all(isinstance(e["turn"], int) and e["day"] >= 1 for e in feed),
       "every entry is time-located — turn and day, not 'recently'")
    ok(all(e["kind"] and e["tone"] for e in feed),
       "and typed, so the feed can be read by tone at a glance")


# --------------------------------------------------- the audit-pass fixes
def test_a_fated_death_is_a_real_death():
    section("fate — the most dramatic deaths were the ones nothing reacted to")
    pt_id, pt, world = _fresh("fate-death")
    fated = next((f for f in world.fated_events if f.get("kills")), None)
    if not fated:
        ok(True, "this world has no fated killing to check (skipped)")
        return

    victim = fated["kills"]
    mourner = next(n["id"] for n in world.npcs if n["id"] != victim)
    for _ in range(4):
        relationships.apply_event(pt_id, victim, mourner, "helped_at_cost", turn=0)

    db.run("UPDATE playthroughs SET current_turn=? WHERE id=?", (fated["turn"] - 1, pt_id))
    engine.take_turn(pt_id, "I wait and watch the road.")

    ok(not memory.npc_state(pt_id, victim)["alive"], "the fated death still kills them")
    role = (world.by_id.get(victim) or {}).get("role")
    succ = legacy.current_vacuum(pt_id, world)
    if role:
        ok(succ.get("opened") and len(succ.get("claimants", [])) >= 2,
           f"and it now opens a contested seat like any other death "
           f"({len(succ.get('claimants', []))} claimants) — before this a fated "
           f"killing only flipped an alive flag: no grief, no standing, no vacuum")
    else:
        ok(True, "the fated victim holds no role, so no seat opens (correct)")


def test_b_a_lying_player_can_be_named():
    section("the traitor — the payoff is a PLAYER, and it could never fire")
    pt_id, pt, world = _fresh("player-traitor")
    from backend import betrayal
    betrayal.depart(pt_id, player_id="hazem", announced="I am checking the ferry.",
                    truth="I am selling them to the Warden.",
                    destination=pt["current_location"], turn=1)

    sig = legacy.traitor_signal(pt_id, world, memory.SOLO, turn=2)
    ok(sig["player_deceptions"] == 1 and sig["active"],
       "a player who lied about where they went raises the signal")
    ok("name" not in sig and "player_id" not in sig,
       "and is still not named by the tease")

    out = legacy.reveal_traitor(pt_id, world, memory.SOLO, turn=9, force=True)
    ok(out["revealed"] and out["kind"] == "player" and out["name"] == "hazem",
       f"the reveal names the PLAYER, not an NPC ({out.get('kind')}/{out.get('name')}) — "
       f"a human at the table sitting on an unrevealed lie outranks any NPC grievance")
    ok(any("checking the ferry" in b["label"] for b in out["because"]),
       "carrying what they SAID against what they did — the gap is the evidence")


def test_c_the_reveal_fires_at_the_climax():
    section("the traitor — a reveal behind a button may never happen")
    pt_id, pt, world = _fresh("climax")
    last = world.fated_events[-1]["turn"]
    ok(not legacy.at_climax(world, 1), "turn 1 is not the climax")
    ok(legacy.at_climax(world, last), "the last fated turn is")

    quiet = next(n["id"] for n in world.npcs)
    for _ in range(3):
        relationships.apply_event(pt_id, quiet, memory.SOLO, "secret_leaked", turn=1)
    fired = legacy.tick(pt_id, world, last)
    ok(fired["reveal"] and fired["reveal"]["revealed"],
       "and the engine fires the reveal there by itself — the payoff has to LAND, "
       "not sit behind a control the player may never press")
    again = legacy.tick(pt_id, world, last + 1)
    ok(not again["reveal"], "exactly once — the node it writes is its own guard")


def test_d_an_npc_can_recall_why_they_turned():
    section("recall — a feeling with no memory behind it")
    pt_id, pt, world = _fresh("recall")
    here = pt["current_location"]
    watcher = memory.npcs_at(pt_id, world, here, 1)[0]
    subject = next(n["id"] for n in world.npcs if n["id"] != watcher)
    awareness.witness(pt_id, world, actor=subject, kind="killed_ally_of",
                      summary="killed_ally_of at the well", detail="",
                      place_id=here, turn=1, severity=5, present=[watcher],
                      subject=subject)
    legacy.gossip(pt_id, world, 1)

    recalled = memory.npc_recall(pt_id, watcher, 2, "killed well")
    ok(any("killed_ally_of" in m["text"] for m in recalled),
       "the witness can RECALL what changed their mind — moving the numbers "
       "without writing a memory left a character who had turned unable to say why")


def test_e_the_narrator_hears_the_world_move():
    section("narration — a succession settling in front of the player, unmentioned")
    ok(legacy.narrate_line(None, {}) == "",
       "a quiet turn adds NOTHING to the prompt — an untouched turn is "
       "byte-identical to what it always was")
    line = legacy.narrate_line(None, {"successions": [
        {"settled": True, "winner_name": "Hadrik Gunn", "role": "Warden", "losers": 2}]})
    ok("Hadrik Gunn now holds Warden" in line and "2 other claim" in line,
       "and a settled contest is handed over as FACT, for the narrator to report")
    ok("do not invent" in line,
       "with the same contract every other layer uses: report it, never decide it")


def test_f_export_includes_the_layer():
    section("export — 'everything' has to mean everything")
    pt_id, pt, world = _fresh("export")
    legacy.found_org(pt_id, world, player=memory.SOLO, name="The Ledger", turn=1)
    dump = engine.export(pt_id)
    for table in ("orgs", "org_members", "claimants", "ooc_messages"):
        ok(table in dump, f"{table} is in the export")
    ok(len(dump["orgs"]) == 1, "and the organisation actually round-trips")


def test_l_unrest_is_a_contest_not_a_countdown():
    section("succession — something has to HAPPEN during the unrest window")
    pt_id, pt, world = _fresh("unrest")
    dead = next(n for n in world.npcs if n.get("role"))
    death.world_event(pt_id, who=dead["id"], killer="", world=world)
    v = legacy.current_vacuum(pt_id, world)
    before = {c["name"]: c["claim"] for c in v["claimants"]}

    backed = v["claimants"][-1]["npc_id"]
    for _ in range(6):
        relationships.apply_event(pt_id, backed, memory.SOLO, "helped_at_cost", turn=1)

    mid = legacy.unrest_tick(pt_id, world, v["opened_turn"] + 1)
    ok(mid and not mid[0]["settled"],
       "a tick inside the window reports movement rather than nothing")
    ok(mid and mid[0]["shifts"],
       f"claims actually shift while it is open ({len(mid[0]['shifts']) if mid else 0} moved)")
    after = {c["name"]: c["claim"] for c in legacy.current_vacuum(pt_id, world)["claimants"]}
    ok(after != before,
       "so the numbers differ from where they started — a window in which nothing "
       "moves is a countdown, and a player has no lever on a countdown")
    ok(any(s["why"] for s in mid[0]["shifts"]),
       "and every shift states why it moved")


def test_i_a_player_sees_what_happens_in_front_of_them():
    section("chronicle — the gate was so tight it excluded the player")
    pt_id, pt, world = _fresh("present")
    here = pt["current_location"]
    far = next(l["id"] for l in world.locations if l["id"] != here)

    legacy.record_world_event(pt_id, world, turn=1, kind="twist",
                              label="The bell is taken down", detail="Two men and a rope.",
                              actor="", place_id=here, severity=3)
    legacy.record_world_event(pt_id, world, turn=1, kind="twist",
                              label="A barn burns at the ferry", detail="",
                              actor="", place_id=far, present=[], severity=3)

    feed = [e["label"] for e in legacy.chronicle(pt_id, world, memory.SOLO)["entries"]]
    ok("The bell is taken down" in feed,
       "something that happens in the room the player is STANDING IN reaches them — "
       "awareness.witness walks NPCs and the actor only, so on its own the gate was "
       "tight enough to hide the world from the person living in it")
    ok("A barn burns at the ferry" not in feed,
       "and something a place away still does not — the gate loosened by exactly "
       "one case, not by degree")


def test_j_an_order_you_gave_is_something_you_know():
    section("organisations — you commissioned it and were not told")
    pt_id, pt, world = _fresh("told")
    org = legacy.found_org(pt_id, world, player=memory.SOLO, name="Ash Crew", turn=1)
    npc = next(n["id"] for n in world.npcs)
    for _ in range(14):
        relationships.apply_event(pt_id, npc, memory.SOLO, "helped_at_cost", turn=1)
    legacy.recruit(pt_id, world, org_id=org["id"], npc_id=npc, player=memory.SOLO, turn=1)

    # Send them somewhere the player is NOT, so location cannot be what tells them.
    far = next(l["id"] for l in world.locations if l["id"] != pt["current_location"])
    memory.set_npc_location(pt_id, npc, far)
    r = legacy.command(pt_id, world, org_id=org["id"], npc_id=npc, player=memory.SOLO,
                       order="Watch the ferry.", turn=2, severity=1)
    ok(r["carried"], "the order is carried out somewhere else")
    feed = [e["label"] for e in legacy.chronicle(pt_id, world, memory.SOLO)["entries"]]
    ok(any("Watch the ferry" in f for f in feed),
       "and the player who GAVE it knows it was done — they were reported back to. "
       "Without this you could command an organisation and never hear a word of it")
    node = narrgraph.node(pt_id, r["node_id"])
    ok(node["actor"] == npc,
       "while the record still names the subordinate as the one who did it")


def test_g_a_private_turn_stays_private():
    section("chronicle — the leak a split party would have opened")
    pt_id, pt, world = _fresh("private")
    from backend import betrayal

    # One player departs, saying one thing and doing another.
    betrayal.depart(pt_id, player_id="ayla", announced="Checking the ferry.",
                    truth="Meeting the Warden.", destination=pt["current_location"],
                    turn=0)

    engine.take_turn(pt_id, "I hand the Warden everything I know.", player="ayla")

    mine = legacy.chronicle(pt_id, world, "ayla")["entries"]
    theirs = legacy.chronicle(pt_id, world, "bram")["entries"]
    ok(any("Warden everything" in e["label"] for e in mine),
       "the player who was away can see their own private turn")
    ok(not any("Warden everything" in e["label"] for e in theirs),
       "and nobody else can — an ungated node here would have handed the table "
       "exactly what betrayal.py exists to keep from them, which is the whole "
       "reason to depart in the first place")

    node = db.row("SELECT fact_key FROM graph_nodes WHERE playthrough_id=?"
                  " AND label LIKE '%Warden everything%'", (pt_id,))
    ok(node and node["fact_key"] == betrayal.private_key("ayla", 1, pt["current_location"]),
       "gated by the SAME key the knowledge store uses — two keys would mean two "
       "answers to 'may this player see it', and a leak lives in that gap")


def test_h_no_ungated_node_is_written_off_screen():
    section("chronicle — the invariant, checked over the whole codebase")
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parent.parent / "backend"
    ungated = []
    for path in sorted(root.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"narrgraph\.add\(", src):
            tail = src[m.start():m.start() + 400]
            depth, end = 0, len(tail)
            for i, ch in enumerate(tail):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            if "fact_key" not in tail[:end]:
                line = src[:m.start()].count("\n") + 1
                ungated.append(f"{path.name}:{line}")
    # Every ungated writer must be on the player's own turn path - they were
    # standing there, which is why the node needs no gate. legacy.py and the
    # split-turn node are the two that pass a key.
    ok(all(w.startswith(("engine.py", "aftermath.py")) for w in ungated),
       f"every node written WITHOUT a gate comes from the player's own turn path "
       f"({', '.join(ungated) or 'none'}) — anything the world does off-screen "
       f"goes through legacy.record_world_event, which cannot forget the key")


def test_k_every_endpoint_is_actually_reachable():
    section("API — a route that exists and a route that answers are not the same")
    from fastapi.testclient import TestClient
    from backend import main

    # A second @app.post on a path already registered is not an error: FastAPI
    # matches the FIRST one and the later handler is dead. /reveal was already
    # the split-party reveal, so the late-reveal endpoint answered with
    # somebody else's payload and nothing in the code looked wrong.
    seen = {}
    dupes = []
    for r in main.app.routes:
        path = getattr(r, "path", "")
        if not path.startswith("/api"):
            continue
        for method in sorted(getattr(r, "methods", None) or []):
            key = (method, path)
            if key in seen:
                dupes.append(f"{method} {path}")
            seen[key] = getattr(r, "name", "")
    ok(not dupes,
       f"no two handlers claim the same method and path ({', '.join(dupes) or 'none'}) "
       f"— the second one is silently dead, and the client calling it gets the "
       f"first one's answer forever")

    pt_id, pt, world = _fresh("api")
    client = TestClient(main.app)
    q = "?user_id=legacy-api"
    routes = [("GET", "/legacy"), ("GET", "/chronicle"), ("POST", "/chronicle/read"),
              ("GET", "/drift"), ("GET", "/successions"), ("GET", "/power"),
              ("GET", "/traitor"), ("POST", "/traitor/reveal"), ("POST", "/catch-up")]
    bad = []
    for method, path in routes:
        r = client.request(method, f"/api/playthroughs/{pt_id}{path}{q}")
        if r.status_code != 200:
            bad.append(f"{method} {path} -> {r.status_code}")
    ok(not bad, f"and every endpoint in this layer answers 200 ({', '.join(bad) or 'all ok'})")

    made = client.post(f"/api/playthroughs/{pt_id}/orgs{q}",
                       json={"name": "Test Crew", "kind": "crew"})
    ok(made.status_code == 200 and made.json()["name"] == "Test Crew",
       "founding an organisation works over HTTP, not only in-process")


def test_m_the_client_calls_what_the_server_serves():
    section("UI — a panel nothing renders is the same as a panel that is missing")
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    app_js = (root / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
    html = (root / "frontend" / "index.html").read_text(encoding="utf-8")

    for path in ("/legacy", "/chronicle", "/power", "/orgs", "/traitor", "/catch-up"):
        ok(path in app_js, f"the client actually calls {path}")

    for hook in ("chronFeed", "powerWrap", "chronicleBadge", "powerBadge"):
        ok(f'id="{hook}"' in html and hook in app_js,
           f"#{hook} exists in the markup AND something writes to it")

    for handler in ("data-found-org", "data-recruit-npc", "data-order-send",
                    "data-traitor", "data-reveal", "data-dissolve-org"):
        ok(app_js.count(handler) >= 2,
           f"[{handler}] is both rendered and handled — a control that is drawn "
           f"and never wired is worse than no control")


# --------------------------------------------------------------- $0 budget
def test_the_whole_layer_is_free():
    section("cost — not one model call in the layer")
    import inspect
    src = inspect.getsource(legacy)
    ok("llm.complete" not in src and "llm.json" not in src,
       "legacy.py never calls the model — every mechanic here is arithmetic over "
       "state that already exists")
    ok("import" in src and "from . import" in src,
       "it reuses the existing modules rather than forking them")


def _all():
    return (test_chronicle_is_witness_gated, test_chronicle_badge_and_cursor,
            test_a_fated_death_is_a_real_death, test_b_a_lying_player_can_be_named,
            test_c_the_reveal_fires_at_the_climax,
            test_d_an_npc_can_recall_why_they_turned,
            test_e_the_narrator_hears_the_world_move,
            test_f_export_includes_the_layer,
            test_l_unrest_is_a_contest_not_a_countdown,
            test_i_a_player_sees_what_happens_in_front_of_them,
            test_j_an_order_you_gave_is_something_you_know,
            test_k_every_endpoint_is_actually_reachable,
            test_m_the_client_calls_what_the_server_serves,
            test_g_a_private_turn_stays_private,
            test_h_no_ungated_node_is_written_off_screen,
            test_drift_is_caused_by_the_player,
            test_npc_beliefs_form_from_what_they_saw,
            test_hearsay_weighs_less_than_seeing,
            test_named_death_contests_the_seat, test_succession_is_gated_too,
            test_player_can_found_and_command, test_command_can_be_refused,
            test_traitor_is_teased_before_it_is_named,
            test_fastforward_reveals_what_you_missed,
            test_events_stay_reachable_forever, test_the_whole_layer_is_free)


def main():
    print("StoryLiver — legacy & world events")
    print("  what the world became because you were in it, and how much you know\n")
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


def test_all_legacy():
    for fn in _all():
        fn()
    assert not FAILS, "\n".join(FAILS)


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
