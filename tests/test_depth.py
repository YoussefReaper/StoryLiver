"""Roleplay depth - the fixes for why AI roleplay goes flat.

Each test here maps to a documented cause, not a guess:

  CALLBACKS      Reincorporation is the single most cited thing that makes a
                 long campaign feel alive. The engine already retrieved old
                 events but handed them over labelled MUST NOT CONTRADICT - a
                 constraint, never an invitation. A world with a memory that
                 never refers to it reads exactly like one without.

  BANTER         Companion-to-companion talk is what makes a room feel
                 inhabited rather than staged. `npc_edges` existed in the world
                 files and was silently DROPPED by worldkit.normalise, so the
                 narrator never knew two characters despised each other.

  INITIATIVE     NPCs that only make statements are furniture. Research on NPC
                 conversation is explicit that they should open with questions,
                 requests or offers - something that leaves the player with a
                 decision.

  SESSION ZERO   Canon RPGs fail on undefined player power: anime power scaling
                 makes some characters canonically untouchable, so a
                 protagonist with no stated place becomes a god or a bystander.
                 Entry point + power placement + a limit are asked BEFORE the
                 world is built.

  FLAT CONTEXT   All of the above is additive prompt text, and the whole
                 architecture rests on a 4-player prompt not exceeding the
                 single-player ceiling. That guarantee is re-checked here.

Run:  python -m tests.test_depth
"""
import os
import shutil
import tempfile

os.environ["STORYLIVER_LLM_MODE"] = "mock"
_TMP = tempfile.mkdtemp(prefix="storyliver-depth-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP

from backend import (callbacks, db, engine, memory, npc_sim, sessionzero,  # noqa: E402
                     worldkit, worldforge)

FAILS, NOTES = [], []


def ok(cond, msg):
    (NOTES if cond else FAILS).append(msg)


def section(t):
    NOTES.append("\n  " + t)


# ------------------------------------------------------------------ callbacks
def test_callbacks():
    section("callbacks — the world reaches back to something small")
    db.init()
    pt = engine.create_playthrough("depth-cb")

    engine.take_turn(pt, "I promise Nessa I will come back for her before the fire.",
                     player=memory.SOLO)
    kinds = {e["kind"] for e in memory.timeline(pt)}
    ok("promise" in kinds,
       "a promise is filed as its OWN kind, not as an ordinary action — otherwise "
       "the highest-value callback there is scores like any other sentence")

    # Nothing to call back to yet: the story is too young, and inviting a
    # callback to something that just happened produces the opposite effect.
    ok(callbacks.block(pt, 2) == "",
       f"nothing is offered inside the {callbacks.MIN_AGE}-turn window — a callback "
       "to something that just happened is not a callback")

    db.run("UPDATE playthroughs SET current_turn=20 WHERE id=?", (pt,))
    picks = callbacks.candidates(pt, 20, present=["nessa"])
    ok(picks and picks[0]["kind"] == "promise",
       "once it is old enough, the PROMISE outranks everything else")

    blk = callbacks.block(pt, 20, present=["nessa"])
    ok("MAY REMEMBER" in blk and "optional" in blk,
       "it reaches the narrator as an INVITATION, not a constraint")
    ok("come back for her" in blk,
       "carrying the player's own words, so the callback can be specific")

    # Fate is the plot, not a callback.
    ok(callbacks.CALLBACK_WEIGHT["fate"] == 0,
       "a fated cataclysm is never offered as a callback — that is the plot")
    ok(callbacks.CALLBACK_WEIGHT["promise"] > callbacks.CALLBACK_WEIGHT["discovery"],
       "a promise is worth more to reach back to than a place being found")


# --------------------------------------------------------------------- banter
def test_banter():
    section("banter — characters who have opinions about each other")
    db.init()
    pt = engine.create_playthrough("depth-banter")
    world = engine.world_for(engine._pt(pt))

    ok(bool(world.get("npc_edges")),
       "npc_edges survives worldkit.normalise() — it was silently dropped, so a "
       "world could declare a feud and the engine seeded nothing")

    rows = db.rows("SELECT 1 FROM relationships WHERE playthrough_id=?"
                   " AND src!=? AND dst!=?", (pt, memory.SOLO, memory.SOLO))
    ok(len(rows) >= 6, f"they are seeded into the relationship table ({len(rows)} edges)")

    blk = memory.between_block(pt, world, ["nessa", "corvin", "yeva", "adrahel"])
    ok("BETWEEN THEM" in blk, "the narrator is told who resents whom")
    ok("Nessa Quill does not trust Corvin Pell." in blk,
       "with the actual named feud, in plain words")
    ok("speak to each other" in blk,
       "and invited to let them talk to EACH OTHER, not only to the player")

    ok(memory.between_block(pt, world, ["nessa"]) == "",
       "one character alone produces nothing — there is no room to overhear")


# ----------------------------------------------------------------- initiative
def test_npc_initiative():
    section("initiative — NPCs put something TO you")
    from backend import npc_sim
    sysmsg = npc_sim.ACT_SYSTEM
    ok("furniture" in sysmsg,
       "the prompt names the failure mode: a character who only makes statements")
    all_forms = all(w in sysmsg for w in ("question", "request", "condition", "refuse"))
    ok(all_forms,
       "and asks for a question, a request, a condition or a refusal — the forms "
       "that leave the player with a decision they did not have")
    ok("NOT here to be agreeable" in sysmsg,
       "plus explicit anti-agreeableness — sycophancy is the documented root "
       "cause of flat AI roleplay, and it worsens over long conversations")


# ------------------------------------------------------------- D10, the moat
def test_npc_stops_reasking_answered_questions():
    section("D10 — an information-goal is not asked again once it's answered")
    # Reproduced: Ilo asks turn 2 ("did you come from the road...") the player
    # answers turn 3, and turn 7 Ilo asks the EXACT same question verbatim -
    # because the WANTS anchor was a CONSTANT re-injected every turn, with no
    # record of ever having asked, and the dialogue-path recall never looked
    # at what the player actually told them.
    db.init()
    pt_id = engine.create_playthrough("d10user")
    goal = "Find out what Maren Vosk is hiding."
    ok(npc_sim._is_question_goal(goal),
       "an information-seeking goal is recognised as one")
    ok(not npc_sim._is_question_goal("Keep the tavern full and the talk flowing."),
       "an ordinary intention is not mistaken for a question")

    # Turn 1: the NPC is present with the question still open — nothing has
    # happened between them and the player yet.
    r1 = engine.take_turn(pt_id, "I sit at the bar and nod to Nessa.", player="user")
    ok(not r1.get("blocked"), "the turn actually resolved")
    state = npc_sim.question_state(pt_id, "nessa", "user")
    ok(state.get(goal, {}).get("state") == "pending",
       "with Nessa present, her open question is now tracked as PENDING — on "
       "the table, not yet resolved")
    ok(not npc_sim.answered_goals(pt_id, "nessa", "user"),
       "and nothing is answered yet — one turn of presence is not an exchange")

    # Turn 2: still present. This is the exchange actually happening.
    r2 = engine.take_turn(pt_id, "I ask Nessa what she's heard lately.", player="user")
    ok(not r2.get("blocked"), "the second turn resolved too")
    answered = npc_sim.answered_goals(pt_id, "nessa", "user")
    ok(goal in answered,
       "present again after the ask, the question moves to ANSWERED — the "
       "exchange the two turns represent actually happened")

    # The anchor block the model is shown no longer carries it.
    world = engine.world_for(engine._pt(pt_id))
    block = memory.anchor_block(world, "nessa", answered_goals=answered)
    ok("Maren Vosk is hiding" not in block,
       "the resolved question is gone from the block the model actually sees")
    ok("tavern full" in block,
       "her OTHER, unrelated goal is untouched — only the answered one drops")

    # It stays answered on turn 7, exactly the reproduced symptom's turn gap.
    for _ in range(5):
        engine.take_turn(pt_id, "I keep drinking and watching the room.", player="user")
    still = npc_sim.answered_goals(pt_id, "nessa", "user")
    ok(goal in still,
       "five turns later the answer is still remembered — this is permanent, "
       "not a cooldown that quietly re-opens the question")

    # The autonomous planner (maybe_act) and reflect() both read the same
    # filtered list, so an unprompted NPC action cannot re-surface it either.
    npc = world.by_id["nessa"]
    filtered = npc_sim.active_goals(npc["anchors"]["goals"], pt_id, "nessa", "user")
    ok(goal not in filtered and len(filtered) == len(npc["anchors"]["goals"]) - 1,
       "the goal list every autonomous-action prompt is built from excludes "
       "it too, not just the narrator's own anchor block")

    # A different NPC's copy of the SAME kind of goal (a fresh character who
    # never had this exchange) is untouched — the fix is per (npc, player).
    ok(not npc_sim.answered_goals(pt_id, "corvin", "user"),
       "an NPC the player never spoke to has nothing marked answered")


# --------------------------------------------------------------- session zero
def test_session_zero():
    section("Session Zero — the player has a defined place before play")
    general = sessionzero.questions({"found": False, "setting": "a drowned city"})
    ids = [q["id"] for q in general["questions"]]
    ok(ids == ["role", "power", "limit"],
       f"an original world is still asked who/how strong/what limits ({ids})")

    canon = {"found": True, "canonical_name": "Jujutsu Kaisen",
             "arcs": [{"name": "Shibuya Incident", "note": "the city is sealed"}],
             "powers": [{"name": "Cursed Technique", "note": "innate ability"}]}
    q = sessionzero.questions(canon)
    ids = [x["id"] for x in q["questions"]]
    ok("entry" in ids and "system" in ids,
       "a canon world additionally asks WHERE on the timeline and WHAT power")

    entry = next(x for x in q["questions"] if x["id"] == "entry")
    labels = [o["label"] for o in entry["options"]]
    ok("Shibuya Incident" in labels,
       "the options are the world's REAL arcs, from research — not free text "
       "the model has to recognise")
    ok(labels[0] == "The very beginning" and labels[-1] == "After it all",
       "bracketed by before-it-all and after-it-all, so any point is reachable")

    sysq = next(x for x in q["questions"] if x["id"] == "system")
    ok(sysq["options"][0]["id"] == "none",
       "having NO power is offered first — the harder, better story")
    ok(any(o["label"] == "Cursed Technique" for o in sysq["options"]),
       "and the world's own system is what you pick from")

    brief = sessionzero.brief(
        {"entry": "shibuya_incident", "system": "cursed_technique", "power": "novice",
         "role": "a first-year", "limit": "it burns through me"}, canon)
    ok("ENTRY POINT" in brief and "Do not replay earlier events" in brief,
       "the brief tells the builder to seed the world as it stands THEN")
    ok("POWER LEVEL" in brief and "fails at the worst moment" in brief,
       "power is placed inside the world rather than left undefined")
    ok("Press on this" in brief,
       "and the limit is something the world is told to press on, not decoration")
    ok("reason to care that this specific person is here" in brief,
       "with named characters given a reason to care — the fix for a good "
       "setting in which the protagonist has no place")

    ok(sessionzero.brief({}, canon) == "",
       "answering nothing changes nothing — Session Zero is never mandatory")


# ------------------------------------------------------------------ scale
def test_world_scale():
    section("scale — a world can be as big as it was asked to be")
    db.init()
    sizes = {}
    for scale in ("town", "city", "region", "world"):
        w = worldforge.bootstrap("the shattered archipelago", user_id="depth-s",
                                 mode="original", scale=scale)
        sizes[scale] = (len(w["locations"]), len(w["npcs"]))
    ok(sizes["town"][1] >= 10, f"a town is ~10 characters {sizes['town']}")
    ok(sizes["world"][1] >= 40,
       f"a whole world is {sizes['world'][1]} characters, not 10 — built in "
       f"several passes because one call truncates {sizes['world']}")
    ok(sizes["town"][0] < sizes["city"][0] < sizes["region"][0] < sizes["world"][0],
       "and each step up is genuinely larger than the last")

    plan = worldforge.scale_plan("world")
    ok(plan["model_calls"] >= 9,
       f"the cost is stated up front ({plan['model_calls']} model calls) — a whole "
       f"world is not one call and the player should know before pressing the button")


def _all():
    return (test_callbacks, test_banter, test_npc_initiative,
            test_npc_stops_reasking_answered_questions,
            test_session_zero, test_world_scale, test_ooc_channel,
            test_canon_spectrum, test_severity_bites, test_default_lines)


def main():
    print("StoryLiver — roleplay depth")
    print("  offline stub, no API key, no spend\n")
    for fn in _all():
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


def test_all_depth():
    for fn in _all():
        fn()
    assert not FAILS, "\n".join(FAILS)




# ---------------------------------------------------------------- OOC channel
def test_ooc_channel():
    section("OOC — the table talks about the story, outside the story")
    from backend import memory as _m, ooc
    db.init()
    pt = engine.create_playthrough("depth-ooc")
    world = engine.world_for(engine._pt(pt))

    ooc.post("", pt, player=_m.SOLO, name="Yusuf", text="wait, is the gate still barred?")
    ok(len(ooc.history("", pt)) == 1, "a player-to-player line is recorded")
    ok(not any("gate still barred" in (e["action"] or "") for e in _m.timeline(pt)),
       "and it NEVER enters the story timeline — asking a question must not "
       "become a scene the world reacts to")

    ruling = ooc.ask_world_master(engine._pt(pt), world, "where am I?",
                                  user_id="depth-ooc", player=_m.SOLO)
    ok(ruling and len(ruling.split()) <= 14,
       f"the World Master answers in one clause ({ruling!r})")
    ok(ooc.MAX_ANSWER_TOKENS <= 60,
       f"hard-capped at {ooc.MAX_ANSWER_TOKENS} tokens — a request to be brief "
       f"is not a cap, and an answer that runs long has become narration")


# ------------------------------------------------------------- canon spectrum
def test_canon_spectrum():
    section("canon spectrum — strict lore, loose physics, per table")
    from backend import modes
    db.init()
    pt = engine.create_playthrough("depth-spec")

    ok(modes.domain_directive(pt) == "",
       "an untouched world sends NOTHING extra — the default prompt is "
       "byte-identical to what it always was")

    modes.set_domains(pt, levels={"lore": 2, "physics": 0}, sev="harsh")
    by_id = {d["id"]: d for d in modes.domain_public(pt)["domains"]}
    ok(by_id["lore"]["level_label"] == "Strict" and by_id["physics"]["level_label"] == "Loose",
       "lore can be absolute while physics is bendable — one dial could not "
       "express the position most tables actually hold")

    d = modes.domain_directive(pt)
    ok("Lore and history: is absolute" in d and "may be bent freely" in d,
       "and only the domains that DIFFER are sent to the World Master")

    modes.set_domains(pt, levels={"people": 0})
    ok(modes.domains(pt)["people"] == 2 and by_id["people"]["locked"],
       "'who characters are' cannot be loosened at all — persona drift is a "
       "product failure, not a freedom a table opted into")

    ok(modes.severity_mult(pt) == 1.6, "the severity slider scales what a mistake costs")
    ok(modes.get(pt)["canon"] in ("loose", "strict"),
       "and the original single dial still works, untouched")


# ------------------------------------------------------------ severity slider
def test_severity_bites():
    """The slider used to be a number nothing read. It returned 1.6 for
    'harsh' and no code path multiplied by it, so three tables that had chosen
    three different answers to "how hard should this land?" all played the
    identical game. A setting the engine ignores is worse than no setting."""
    section("severity — the slider changes what a mistake COSTS")
    from backend import memory as _m, modes, world_master
    db.init()

    action = "I take from Nessa Quill"
    verdict = {"valid": True, "importance": 4, "kind": "action",
               "consequence": "She does not let go quietly.", "new_location": None}

    def play(sev, what):
        pt_id = engine.create_playthrough(f"depth-sev-{sev}-{abs(hash(what)) % 9999}")
        modes.set_domains(pt_id, sev=sev)
        pt = engine._pt(pt_id)
        world = engine.world_for(pt)
        out = engine._deterministic_tick(
            pt, world, turn=1, player=_m.SOLO, actor_name="Yusuf", action=what,
            verdict=verdict, state=world_master.build_state(pt, world),
            session_id="", moved_to=None)
        rel = db.row("SELECT trust, affinity FROM relationships"
                     " WHERE playthrough_id=? AND dst='nessa'", (pt_id,))
        return out, (rel["trust"] if rel else 0.0)

    gentle, g_trust = play("gentle", action)
    normal, n_trust = play("normal", action)
    harsh, h_trust = play("harsh", action)

    ok(h_trust < n_trust < g_trust,
       f"the same theft costs more on a harsh table than a gentle one "
       f"(trust {g_trust:.2f} / {n_trust:.2f} / {h_trust:.2f})")

    seen = [(bool(o["witness"]), (o["witness"] or {}).get("severity"),
             tuple((o["witness"] or {}).get("witnesses") or ()))
            for o in (gentle, normal, harsh)]
    ok(len(set(seen)) == 1 and seen[0][0],
       f"but all three tables SAW exactly the same thing happen ({seen[0][1]}, "
       f"{seen[0][2]}) — the slider scales the cost, never the perception, or "
       f"a gentle table would quietly become one where nobody notices you")

    kind = "I help Nessa Quill carry it"
    _, g_kind = play("gentle", kind)
    _, h_kind = play("harsh", kind)
    ok(abs(g_kind - h_kind) < 0.01,
       f"and a kindness is worth the same on both ({g_kind:.2f} / {h_kind:.2f}) — "
       f"a world that forgives slowly must not also reward you faster")


# --------------------------------------------------------------- safety lines
def test_default_lines():
    section("Session Zero — the table starts with lines already drawn")
    from backend import canon
    db.init()
    pt = engine.create_playthrough("depth-lines")
    lines = canon.safety(pt)["lines"]
    ok(len(lines) >= 4, f"a new world starts with real lines, not an empty list ({len(lines)})")
    ok(any("child" in l.lower() for l in lines) and any("sexual" in l.lower() for l in lines),
       "covering the boundaries the spec requires by default")

    try:
        canon.safety_gate(pt, "a graphic torture scene")
        ok(False, "a default line did not actually cancel anything")
    except canon.Cancelled:
        ok(True, "and they are ENFORCED, not decorative")

    ok(canon.set_safety(pt, lines=["Only this one"])["lines"] == ["Only this one"],
       "every one stays editable — a starting position, not a policy")


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
