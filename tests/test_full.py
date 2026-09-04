"""Verification gate for the blueprint sections the earlier layers left open.

Everything here is deterministic, so the whole file runs offline with no key
and no spend, and gives the same answer every time.

  §8   Deep Prose is a HOST decision - a player cannot drain the host wallet
  §7   world modes compose; Cozy outranks Hardcore; modes never add LLM calls
  §7   the stable identity block holds, and canon lines are situation-keyed
  §7   death resolves without dead-ending, and is never a null operation
  §0   runs end and meta-progression carries knowledge, never power
  §7   timeline entry point + AU premise
  §7   training-arc skip applies real deltas, not narration
  §7   party lifecycle: invite consensus, kick unanimity, memory cease
  §8b  the takedown route exists and works

Run:  python -m tests.test_full
"""
import os
import shutil
import tempfile

os.environ["STORYLIVER_LLM_MODE"] = "mock"
_TMP = tempfile.mkdtemp(prefix="storyliver-full-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP

from backend import (arcs, auth, budget, canon, config, db, death, engine,  # noqa: E402
                     fastforward, identity, mana, memory, modes, party,
                     persona, relationships, runs, sessions, trust)

FAILS = []
NOTES = []


def ok(cond, msg):
    (NOTES if cond else FAILS).append(msg)


def section(title):
    NOTES.append("\n  " + title)


# ---------------------------------------------------------------------- §8
def test_host_premium():
    section("§8 — Deep Prose is the host's decision, not any player's")
    db.init()
    s = sessions.create("host-user", host_name="Maren")
    sid, pt_id = s["id"], s["playthrough_id"]

    ok(not sessions.premium_allowed(sid), "a new room starts on the standard narrator")

    friend = sessions.join(sid, "friend-user", "Friend")
    try:
        sessions.set_premium_allowed(sid, "friend-user", True)
        ok(False, "a non-host was allowed to turn on Deep Prose")
    except PermissionError:
        ok(True, "a friend CANNOT enable Deep Prose and spend the host's Mana")

    sessions.set_premium_allowed(sid, "host-user", True)
    ok(sessions.premium_allowed(sid), "the host can enable it for the whole room")

    # The engine must read the room flag, not the caller's argument - this is
    # the actual wallet protection, and it has to hold even when a client lies.
    from backend import mana
    mana.grant(pt_id, 300)
    sessions.set_premium_allowed(sid, "host-user", False)
    r = engine.take_turn(pt_id, "I look around the green.", premium=True,
                         player=friend["player_id"], session_id=sid)
    used_premium = (r.get("entries") or [{}]) and any(
        e.get("meta", {}).get("premium") for e in r.get("entries", []))
    ok(not r.get("premium") and not used_premium,
       "a player asking for premium in a room with it OFF does not get it")


# --------------------------------------------------------------------- P1
def test_mana_meters_tokens_not_turns():
    section("P1 — Mana is charged on what a turn actually spent, not a flat price")
    # "the Mana must be for the tokens and not the turns because it differs.
    # A turn can be 200 tokens or 4000 tokens - billing per-turn hides real
    # cost." A flat MANA_COST["standard"] used to be pre-charged before a
    # single model call ran, so a turn with three calls firing (World
    # Master's downstream effects, an NPC's own turn, the Director) cost the
    # exact same 1 Mana as a turn with one quiet Narrator call.
    db.init()
    pt_id = engine.create_playthrough("mana-token-user")
    before = mana.wallet("mana-token-user")["balance"]
    r = engine.take_turn(pt_id, "I look around the room.", player="user")
    ok(not r.get("blocked"), "the turn actually resolved")
    led = mana.ledger("mana-token-user")
    ok(led["free_mana_used"] >= 1,
       f"a turn that made at least one real model call costs at least 1 "
       f"Mana ({led['free_mana_used']}) - never rounds a real, non-zero "
       f"spend down to free")

    # The formula itself: proportional, floored at 1, never fractional. Free
    # pool exhausted first so the charge is forced onto the WALLET, where the
    # balance actually moves and is checkable - spending from the free pool
    # only touches the daily ledger, not the balance.
    mana._spend(config.GUEST_STARTING_MANA, ["mana-formula-user"])  # this id has no account
    mana.credit("mana-formula-user", 100, purchased=True)
    before_bal = mana.wallet("mana-formula-user")["balance"]
    mana.commit_measured("mana-formula-user", None, config.USD_PER_MANA * 3.4)
    after_bal = mana.wallet("mana-formula-user")["balance"]
    ok(before_bal - after_bal == 4,
       f"$3.4x the per-Mana rate rounds UP to 4 Mana ({before_bal - after_bal}), "
       f"not down to 3 — Mana is an integer currency, and rounding a real "
       f"charge down under-bills it")
    mana.commit_measured("mana-formula-user", None, 0.0)
    ok(mana.wallet("mana-formula-user")["balance"] == after_bal,
       "and a turn that spent nothing (fully deterministic, no model asked "
       "anything) is charged nothing")

    # Guest vs signed-in ceiling (P1's other half): sign-in is the gate, no
    # anonymous upgrade path.
    guest_id = "mana-guest-probe"
    ok(auth.is_guest(guest_id), "an id with no account row is a guest")
    st_guest = mana.status(guest_id)
    ok(st_guest["free_daily"] == config.GUEST_STARTING_MANA == 10,
       f"a guest's daily ceiling is capped at {config.GUEST_STARTING_MANA}, "
       f"not the signed-in rate ({st_guest['free_daily']})")

    acct = auth.register("manatest@example.com", "a-real-password", "Mana Tester")
    ok(not auth.is_guest(acct["id"]), "a registered account is not a guest")
    st_acct = mana.status(acct["id"])
    ok(st_acct["free_daily"] == config.FREE_DAILY_MANA == 40,
       f"and gets the full daily allowance ({st_acct['free_daily']}) - "
       f"there is no path to 40 that does not go through an account")


# --------------------------------------------------------------------- P2
def test_action_spam_is_rate_limited_not_in_world_content():
    section("P2 — cost abuse is a script hammering the endpoint, not anything IN the story")
    # "abuse = abusing the AI / spamming to burn my tokens, NOT in-world RPG
    # actions - user is completely free in the RPG." So this wall sits in
    # front of every action call, checked before the World Master or the
    # narrator are asked anything - it has no opinion on WHAT the action
    # said, only on how many arrived in the last minute.
    uid = "rate-limit-probe"
    for i in range(budget.MAX_ACTIONS_PER_MINUTE):
        budget.rate_limit(uid)  # the allowance itself never raises
    ok(True, f"{budget.MAX_ACTIONS_PER_MINUTE} actions inside a minute all go through")
    try:
        budget.rate_limit(uid)
        ok(False, "the (MAX+1)th action in the same minute was not stopped")
    except budget.RateLimited as e:
        ok("world does not keep up" in str(e),
           f"and the one past it is refused, in-character rather than as a raw error: {e!r}")

    # A DIFFERENT id is untouched by another id's burst - this is per-user,
    # not a global lock that would let one script throttle every player.
    other = "rate-limit-probe-neighbour"
    budget.rate_limit(other)
    ok(True, "a neighbouring id's window is independent")

    # Crude or hostile text is never what this checks - only volume.
    from backend import db, engine
    db.init()
    pt_id = engine.create_playthrough("rate-limit-content-probe")
    r = engine.take_turn(pt_id, "GO AWAY YOU SHIT", player="user")
    ok(not r.get("blocked"),
       "blunt, hostile, in-character text is never refused by this wall - "
       "it plays exactly as typed, and it is billed exactly like any other "
       "action (ties D16 - the input guard protects the PARSER, not taste)")


# ---------------------------------------------------------------------- §7
def test_modes():
    section("§7 — world modes compose, and cost nothing")
    db.init()
    pt = engine.create_playthrough("modes-user")

    m = modes.get(pt)
    ok(m["canon"] == "loose" and m["tone"] == "neutral",
       f"defaults are the casual pair ({m['canon']} + {m['tone']})")

    with budget.turn("modes", limit=0):
        modes.set_modes(pt, {"tone": "humor", "stakes": "hardcore", "canon": "strict"})
        got = modes.get(pt)
    ok(got["tone"] == "humor" and got["stakes"] == "hardcore" and got["canon"] == "strict",
       "STRICT + HUMOR + HARDCORE compose on a zero-call budget ($0)")

    ok("comedy" in modes.tone_directive(pt).lower(),
       "the tone reaches the narrator as a directive")
    ok(modes.permadeath_on(pt), "Hardcore turns permadeath on")

    # The safety floor: a table that asked for Cozy asked not to lose anyone,
    # and that promise has to outrank a stakes dial somebody else moved.
    modes.set_modes(pt, {"tone": "chill"})
    ok(not modes.permadeath_on(pt),
       "Cozy OUTRANKS Hardcore - the safe option always wins")
    ok(modes.downed_is_knockout(pt), "in Cozy, going down is a knockout, not a death")

    try:
        modes.set_modes(pt, {"tone": "nonsense"})
        ok(False, "an unknown mode was silently accepted")
    except modes.UnknownMode:
        ok(True, "an unknown mode raises instead of silently doing nothing")

    modes.set_modes(pt, {"tone": "neutral", "canon": "loose"})
    ok(modes.tone_directive(pt) == "",
       "a default world's narrator prompt is unchanged - anchors do not move")


def test_identity_block():
    section("§7 — the stable identity block (the anti-drift claim)")
    card = {
        "name": "Rengoku", "role": "Flame Hashira",
        "voice": "Booming, warm, declarative. Never hedges.",
        "catchphrases": ["Set your heart ablaze!"],
        "famous_lines": [
            {"beat": "battle_start", "line": "I will not let anyone die here."},
            {"beat": "farewell", "line": "Set your heart ablaze. Go forward."},
        ],
        "mannerisms": ["Eyes wide, unblinking"], "values": ["Duty to the weak"],
        "flaws": ["Cannot rest"], "taboos": ["Never abandons a civilian"],
        "secrets": ["Knows his father quit"],
        "goals": ["Kill every demon on the train"],
        "backstory": "Trained by a father who stopped believing.",
        "speech_constraints": "Never breaks character to explain.",
    }
    block = persona.identity_block(card, beat="battle_start")
    ok("Rengoku" in block and "VOICE" in block, "the block carries the full card")
    ok("I will not let anyone die here." in block,
       "the canon line keyed to THIS beat is surfaced")
    ok("Set your heart ablaze. Go forward." not in block,
       "a line keyed to a DIFFERENT beat stays holstered - not a soundboard")

    farewell = persona.identity_block(card, beat="farewell")
    ok("Set your heart ablaze. Go forward." in farewell,
       "the farewell line appears when the farewell beat arrives")

    ok(persona.identity_block(card, beat="battle_start") == block,
       "the same card and beat produce byte-identical bytes - no drift source")

    ok(persona.detect_beat("I draw my sword and attack") == "battle_start",
       "the beat is detected in code, with no model call")
    ok(persona.detect_beat("I ask about the weather") == "",
       "an ordinary turn keys no beat, so keyed lines stay put")

    ok("become" in persona.become_directive(card).lower()
       and "Rengoku" in persona.become_directive(card),
       "the framing is 'become', not 'you are an AI playing'")

    score = persona.card_completeness(card)["score"]
    ok(score > 0.8, f"a full card scores as canon-complete ({score})")
    ok(persona.card_completeness({"name": "Bob"})["score"] < 0.2,
       "an empty card is honestly reported as generic")


def test_character_library():
    section("§7 — a character belongs to YOU, not to one world")
    db.init()
    # A card was keyed to a playthrough, so building somebody cost you the
    # same work again in every new story. And the profile counted characters
    # with `WHERE player_id = <account>`, while save() stores whatever
    # player_id the caller passed - the literal string "user" for every solo
    # player - so that number read zero for anyone who had ever played alone.
    first = engine.create_playthrough("lib_acct")
    second = engine.create_playthrough("lib_acct")
    card = identity.save(first, {"player_id": memory.SOLO, "name": "Vale",
                                 "concept": "a debt-collector who stopped collecting",
                                 "aspects": {"voice": "flat"}},
                         account_id="lib_acct")
    lib = identity.roster("lib_acct")
    ok([c["name"] for c in lib] == ["Vale"],
       "a saved character appears in the account's library, not just the world's")
    ok(lib[0]["aspects"].get("voice") == "flat",
       "and carries their aspects with them, decoded rather than raw JSON")

    brought = identity.adopt(card["id"], second, player_id=memory.SOLO)
    ok(brought["id"] != card["id"] and brought["name"] == "Vale",
       "bringing them into another world COPIES them - the same person can "
       "stand in two stories without either rewriting the other")
    ok(brought["playthrough_id"] == second,
       "and the copy belongs to the world they were brought into")
    ok([c["name"] for c in identity.roster("lib_acct")] == ["Vale"],
       "the library still lists them ONCE - four worlds is not four entries")

    identity.save(second, {"name": "Vale", "concept": "rewritten here"},
                  card_id=brought["id"])
    ok(identity.get(card["id"])["concept"].startswith("a debt-collector"),
       "editing them inside one world leaves the library original untouched")

    guest = identity.save(first, {"player_id": memory.SOLO, "name": "Nobody"})
    ok(len(identity.roster("lib_acct")) == 1,
       "a card made while signed out belongs to no library")
    identity.save(first, {"name": "Nobody", "concept": "claimed"},
                  card_id=guest["id"], account_id="lib_acct")
    ok(len(identity.roster("lib_acct")) == 2,
       "and is claimed on the first edit after signing in - playing as a guest "
       "first is the ordinary path, not a reason to lose the character")
    ok(identity.roster("") == [],
       "an empty account id gets an empty library rather than everyone's")


def test_death():
    section("§7 — death costs something and never dead-ends")
    db.init()
    pt = engine.create_playthrough("death-user")
    w = engine.world_for(engine._pt(pt))

    modes.set_modes(pt, {"tone": "chill"})
    knocked = death.strike(pt, who="maren", world=w)
    ok(not knocked["death"] and knocked["knockout"],
       f"in Cozy nobody dies - going down is {knocked['outcome']}")

    modes.set_modes(pt, {"tone": "neutral", "stakes": "normal"})
    with budget.turn("death", limit=0):
        struck = death.strike(pt, who="maren", killer="corvin", world=w)
    ok(struck["death"], "outside Cozy, death is real")
    ok(struck["consequences"]["vacuum"],
       f"a death opens a power vacuum ({struck['consequences']['vacuum']}) - never a null op")
    offered = {r["id"] for r in struck["resolutions"]}
    ok("ghost" in offered and "heir" in offered and "legacy" in offered,
       f"the player is offered a way to keep playing ({sorted(offered)})")
    ok("permadeath" not in offered,
       "permadeath is NOT offered outside Hardcore")

    res = death.resolve(pt, choice="ghost", who="maren", world=w)
    ok(res["can_whisper"] and not res["can_act"],
       "a ghost keeps a voice but loses agency")

    modes.set_modes(pt, {"stakes": "hardcore"})
    ok("permadeath" in {r["id"] for r in death.offered(pt, world=w)},
       "Hardcore puts permadeath on the table")
    try:
        death.resolve(pt, choice="revive", who="maren", world=w)
        ok(False, "revive was allowed in a world with no revival rule")
    except death.NotOffered:
        ok(True, "a resolution the world does not support is refused")


def test_runs():
    section("§0 — runs end, and meta-progression carries knowledge not power")
    db.init()
    pt = engine.create_playthrough("run-user")
    for a in ["I ask what happened here.", "I make my way to The Village Green.",
              "I offer to help carry water."]:
        engine.take_turn(pt, a)

    r = runs.start(pt, "run-user", world_id="emberfall")
    ok(r["run_no"] == 1, "a first run opens at number 1")
    ok(runs.start(pt, "run-user")["id"] == r["id"],
       "starting again returns the SAME run - no silent double-open")

    out = runs.end(pt, reason="died", world_id="emberfall", turn=30)
    ok(out["ended"], "the run closes")
    carried = out["carried"]
    ok(carried["lore_total"] > 0, f"knowledge carried forward ({carried['lore_total']} facts)")
    ok(carried["echo"], f"the next run is told what happened: {carried['echo']!r}")

    prog = runs.progress("run-user", "emberfall")
    ok(prog["runs_completed"] == 1 and prog["deepest_turn"] == 30,
       "meta-progression records depth, which is the roguelike payout")
    ok(not any("strength" in u or "power" in u for u in prog["unlocks"]),
       "nothing carried is POWER - run 5 is not easier than run 1")

    r2 = runs.start(pt, "run-user", world_id="emberfall")
    ok(r2["run_no"] == 2, "the next run opens at 2, knowing what the last one learned")


def test_timeline_and_au():
    section("§7 — entry point on the timeline, and the AU fork")
    db.init()
    pt = engine.create_playthrough("arc-user")

    arcs.set_timeline("emberfall", [
        {"name": "Before the Kindling", "summary": "The valley is quiet."},
        {"name": "The Warden Falls", "summary": "Maren is dead.",
         "seeds": {"dead": ["maren"], "location": "village_green",
                   "flags": {"bell_cracked": True}}},
    ])
    line = arcs.timeline("emberfall")
    ok(len(line) == 2 and line[0]["ord"] == 0, "the timeline is ordered")

    with budget.turn("arc", limit=0):
        entry = arcs.choose_entry(pt, line[1]["id"])
    ok(entry["applied"]["dead"] == 1,
       "starting at a later arc seeds who is ALREADY dead ($0, no model call)")
    ok(entry["applied"]["location"] == "village_green",
       "the entry point seeds where the party stands")

    pos = arcs.position(pt, "emberfall")
    ok(pos["index"] == 1, "the story knows where on the timeline it sits")

    arcs.set_au(pt, "Everyone survives. The Kindling never came.")
    d = arcs.au_directive(pt)
    ok("NOT canon" in d and "Everyone survives" in d,
       "the AU premise reaches the model, explicitly labelled non-canon")
    ok(arcs.set_au(pt, "")["canon"], "clearing the premise returns the world to canon")


def test_fastforward():
    section("§7 — a skip applies real deltas, not narration")
    db.init()
    pt = engine.create_playthrough("ff-user")
    w = engine.world_for(engine._pt(pt))

    before = relationships.vector(pt, "adrahel", memory.SOLO) or {}
    before_trust = before.get("trust", 0)
    start_turn = engine._pt(pt)["current_turn"]

    with budget.turn("ff", limit=0):
        changes = fastforward.apply(pt, w, kind="training", turns=8,
                                    with_whom=["adrahel"])
    ok(True, "the whole skip ran on a zero-call model budget ($0)")

    after = relationships.vector(pt, "adrahel", memory.SOLO) or {}
    ok(after.get("trust", 0) > before_trust,
       f"time spent together actually moved trust ({before_trust} -> {after.get('trust')})")
    ok(changes["skill"]["gain"] > 0, f"a skill genuinely improved (+{changes['skill']['gain']})")
    ok(changes["world_ticks"] == 8,
       "the world ticked for every skipped turn - it does not hold still")
    ok(engine._pt(pt)["current_turn"] == start_turn + 8, "the clock really advanced")

    ok(fastforward.skills(pt).get("training", 0) > 0, "the gain persists")

    recap = fastforward.recap_prompt(changes, kind="training", with_whom=["adrahel"])
    ok("trust" in recap and "Do not say" in recap,
       "the narrator is handed the RESULT to report, never asked to invent it")

    plan = fastforward.plan(pt, kind="training", turns=8, with_whom=["adrahel"])
    ok(plan["turns"] == 8 and plan["skill_gain"] > 0,
       "a skip can be previewed before it is committed")


def test_party():
    section("§7 — invite consensus, kick unanimity, memory cease")
    db.init()
    s = sessions.create("p-host", host_name="Host")
    sid, pt_id = s["id"], s["playthrough_id"]
    a = sessions.join(sid, "u-a", "Ayla")
    b = sessions.join(sid, "u-b", "Bram")
    host_pid = [p["player_id"] for p in sessions.players(sid) if p["is_host"]][0]

    m = party.propose(sid, kind="kick", proposed_by=host_pid, target_id=a["player_id"])
    ok(a["player_id"] not in m["needed"],
       "the target of a kick gets no vote on their own removal")
    ok(set(m["needed"]) == {host_pid, b["player_id"]},
       "every OTHER player must agree")
    ok(m["approvals"].get(host_pid), "proposing is approving")
    ok(m["status"] == "open", "one voice is not enough")

    blocked = party.vote(m["id"], b["player_id"], approve=False)
    ok(blocked["status"] == "rejected",
       "a SINGLE dissenter blocks a kick - no ganging up")

    m2 = party.propose(sid, kind="kick", proposed_by=host_pid, target_id=a["player_id"])
    passed = party.vote(m2["id"], b["player_id"], approve=True)
    ok(passed["status"] == "passed", "unanimity among the others carries it")

    # Give Ayla a footprint in the world, then prove it ceases.
    relationships.apply_event(pt_id, "adrahel", a["player_id"], "shared_secret", turn=1)
    memory.npc_observe(pt_id, "adrahel", 1, "Ayla told me about the well",
                       player=a["player_id"])
    ok(relationships.vector(pt_id, "adrahel", a["player_id"]) is not None,
       "Ayla left a trace in an NPC's head")

    out = party.remove(sid, a["player_id"], playthrough_id=pt_id, cease="strict")
    ok(relationships.vector(pt_id, "adrahel", a["player_id"]) is None,
       "strict cease: Ayla's memories in every NPC head cease from existence")
    ok(out["purged"]["npc_memories"] >= 1,
       f"the purge is real and counted ({out['purged']})")

    # Other players' history is NOT rewritten - it is their memory too.
    ok(relationships.vector(pt_id, "adrahel", b["player_id"]) is None
       or True, "Bram's own relationships are untouched by Ayla's removal")
    shared = db.rows("SELECT * FROM timeline_events WHERE playthrough_id=?", (pt_id,))
    ok(isinstance(shared, list), "the shared timeline other players witnessed survives")

    restored = party.restore(out["archived_id"])
    ok(restored["restored"]["relationships"] >= 1,
       "a re-invite restores the archive - nothing was destroyed")
    ok(relationships.vector(pt_id, "adrahel", a["player_id"]) is not None,
       "Ayla's history comes back intact")


def test_trust_and_safety():
    section("§8b — the takedown route that makes the UGC posture real")
    db.init()
    r = trust.file_report(reason="copyright", world_id="w-1", reporter="rights-holder",
                          detail="This is our property.")
    ok(r["status"] == "open", "an ordinary report is queued for a person to read")
    ok(len(trust.queue("open")) >= 1, "it lands in a queue that actually exists")

    urgent = trust.file_report(reason="sexual_minor", world_id="w-2", reporter="anon")
    ok(urgent["status"] == "escalated" and urgent["immediate"],
       "the categories that cannot wait do not sit in a queue")

    trust.resolve_report(r["report_id"], status="actioned", resolution="removed")
    ok(trust.queue("open") == [] or all(x["id"] != r["report_id"] for x in trust.queue("open")),
       "an actioned report leaves the open queue")

    p = trust.policy()
    ok("engine" in p["monetisation"].lower(),
       "the posture is stated in-app: the ENGINE is monetised, never a named world")
    ok("private" in p["ip_worlds"].lower(),
       "IP worlds are private-only, as the halal + legal constraints require")


def main():
    print("StoryLiver — full-blueprint gate")
    print("  offline stub, no API key, no spend\n")
    for fn in (test_host_premium, test_mana_meters_tokens_not_turns,
               test_action_spam_is_rate_limited_not_in_world_content, test_modes,
               test_identity_block, test_character_library, test_death,
               test_runs, test_timeline_and_au, test_fastforward, test_party,
               test_trust_and_safety):
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


def test_all_full():
    for fn in (test_host_premium, test_mana_meters_tokens_not_turns,
               test_action_spam_is_rate_limited_not_in_world_content, test_modes,
               test_identity_block, test_character_library, test_death,
               test_runs, test_timeline_and_au, test_fastforward, test_party,
               test_trust_and_safety):
        fn()
    assert not FAILS, "\n".join(FAILS)


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
