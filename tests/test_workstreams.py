"""Acceptance gate for the Legibility / Onboarding / Authority / Auth spec.

Every assertion here is one of the spec's own acceptance criteria, run against
the live engine rather than against a mock of it.

  A  a witnessed public crime surfaces as a signal, drops standing, and (at
     threshold) shows an honest ETA; mode gates the depth
  C1 a guard on patrol at the Broken Bell witnesses a public crime
  C2 standing <= -45 is wanted, <= -60 is a warrant; paying or atoning clears it
  C3 escalating crimes escalate the tier, and an officer is named
  C4 commit a crime, end the run, start a new one on the same ACCOUNT -> the
     town remembers; a guest is not remembered
  D  register -> login -> me; the password is not retrievable; buying requires
     an account; the profile is editable

Run:  python -m tests.test_workstreams
"""
import os
import shutil
import tempfile

os.environ["STORYLIVER_LLM_MODE"] = "mock"
_TMP = tempfile.mkdtemp(prefix="storyliver-ws-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP

from backend import (auth, authority, awareness, budget, db, engine,  # noqa: E402
                     legibility, memory, modes)

FAILS, NOTES = [], []


def ok(cond, msg):
    (NOTES if cond else FAILS).append(msg)


def section(t):
    NOTES.append("\n  " + t)


def _standing(pt, faction, value, known=2):
    """Set standing the way the engine does - the row has to exist first."""
    awareness.rep(pt, faction, memory.SOLO)
    db.run("UPDATE faction_rep SET standing=?, known_events=? WHERE playthrough_id=?"
           " AND faction_id=? AND player_id=?", (value, known, pt, faction, memory.SOLO))


# ------------------------------------------------------------------ A
def test_a_legibility():
    section("A — the sim becomes readable")
    db.init()
    pt = engine.create_playthrough("ws-a")
    w = engine.world_for(engine._pt(pt))

    quiet = legibility.panel(pt, w, memory.SOLO, 0, location="broken_bell")
    ok(quiet["pulse"] and quiet["pulse"][0]["kind"] == "quiet",
       "a quiet world says so, rather than showing an empty panel")

    with budget.turn("ws-a", limit=0):
        authority.patrol_tick(pt, w, 0, place_id="broken_bell", actor=memory.SOLO,
                              summary="threatened Nessa in the taproom", severity=4)
    ok(True, "the whole legibility path ran on a zero-call model budget ($0)")

    _standing(pt, "warden_office", -50.0)
    p = legibility.panel(pt, w, memory.SOLO, 1, location="broken_bell")
    kinds = {s["kind"] for s in p["pulse"]}
    ok("witness" in kinds, "a witnessed act surfaces as 'someone is watching'")
    watcher = next(s for s in p["pulse"] if s["kind"] == "witness")
    ok("Hadrik" in watcher["detail"], f"and it NAMES who saw ({watcher['detail']})")
    ok("standing" in kinds, "the faction's opinion of you is surfaced")
    stand = next(s for s in p["pulse"] if s["kind"] == "standing")
    ok("turn you in" in stand["text"], f"in plain words, not a number ({stand['text']})")

    faces = legibility.faces(pt, w, memory.SOLO, present_only=False)
    hadrik = next((f for f in faces if f["id"] == "hadrik"), None)
    ok(hadrik is not None and hadrik["knows_about_you"] >= 1,
       "the witness's own face card records that they know something")
    ok(all(0 <= f["trust_pips"] <= 5 for f in faces),
       "trust renders as pips, not raw scalars")

    # The bands line up with the warrant ladder on purpose: 'hunted' begins at
    # -60, which is exactly where a warrant is issued, so the word the player
    # reads and the thing the institution does cannot drift apart.
    ok(legibility.band(-70)[0] == "hunted" and legibility.band(-50)[0] == "hated"
       and legibility.band(0)[0] == "unknown" and legibility.band(50)[0] == "trusted",
       "standing bands are named in one place, and align with the warrant tiers")


def test_a5_mode_gating():
    section("A5 — depth is gated by the mode the player already chose")
    db.init()
    pt = engine.create_playthrough("ws-a5")
    w = engine.world_for(engine._pt(pt))

    modes.set_modes(pt, {"tone": "chill"})
    chill = legibility.panel(pt, w, memory.SOLO, 1)
    ok(chill["level"] == "chill" and chill["shows"]["pulse"]
       and not chill["shows"]["faces"] and not chill["shows"]["board"],
       "CHILL shows only the pulse")

    modes.set_modes(pt, {"tone": "neutral", "stakes": "normal"})
    normal = legibility.panel(pt, w, memory.SOLO, 1)
    ok(normal["shows"]["faces"] and not normal["shows"]["board"],
       "NORMAL adds relationship faces but not the full board")

    modes.set_modes(pt, {"stakes": "hardcore"})
    hard = legibility.panel(pt, w, memory.SOLO, 1)
    ok(hard["shows"]["board"] and hard["board"] is not None,
       "HARDCORE opens the full intelligence board")

    forced = legibility.panel(pt, w, memory.SOLO, 1, overrides={"board": False})
    ok(not forced["shows"]["board"],
       "and every element stays individually toggleable regardless of mode")


# ------------------------------------------------------------------ C
def test_c1_patrol():
    section("C1 — a guard on patrol actually witnesses")
    db.init()
    pt = engine.create_playthrough("ws-c1")
    w = engine.world_for(engine._pt(pt))

    ok([f["id"] for f in awareness.factions(w)] != ["locals"],
       "Emberfall now declares real factions instead of falling back to 'locals'")
    au = authority.authorities(w)
    ok(len(au) == 1 and au[0]["id"] == "warden_office",
       "exactly one faction holds legal power (law >= 3)")

    out = authority.patrol_tick(pt, w, 0, place_id="broken_bell", actor=memory.SOLO,
                                summary="robbed the taproom in daylight", severity=4)
    seen = out["witnessed_by_authority"]
    ok(bool(seen), "a public crime at the Broken Bell is witnessed by the authority")
    ok("Hadrik Sunn" in seen[0]["officers"],
       f"by the officer whose patrol covers it ({seen[0]['officers']})")

    # The schedule has to matter, or stealth is meaningless.
    unwatched = authority.patrol_tick(pt, w, 0, place_id="bitter_well", actor=memory.SOLO,
                                      summary="did it out at the well instead", severity=4)
    ok(not unwatched["witnessed_by_authority"],
       "the same act at an unpatrolled place is NOT seen — the schedule is real")


def test_c2_c3_ladder():
    section("C2/C3 — the warrant ladder, and a named officer")
    db.init()
    pt = engine.create_playthrough("ws-c2")
    w = engine.world_for(engine._pt(pt))

    ok(authority.tier_for(-10) == "clean" and authority.tier_for(-45) == "wanted"
       and authority.tier_for(-60) == "warrant" and authority.tier_for(-80) == "hunt",
       "the thresholds are the spec's: -45 wanted, -60 warrant, -75 kill-on-sight")

    _standing(pt, "warden_office", -46.0)
    row = authority.ledger(pt, w, memory.SOLO)[0]
    ok(row["tier"] == "wanted" and row["response"]["action"] == "warning",
       "at -46 you are wanted, and the response is a warning")
    ok(row["officer"] and row["officer"]["name"],
       f"a NAMED officer is on the case ({(row['officer'] or {}).get('name')})")
    first_officer = row["officer"]["name"]

    _standing(pt, "warden_office", -62.0)
    row2 = authority.ledger(pt, w, memory.SOLO)[0]
    ok(row2["tier"] == "warrant" and row2["response"]["action"] == "arrest",
       "escalating past -60 becomes a warrant, and the response escalates with it")
    ok(row2["officer"]["name"] == first_officer,
       "the SAME officer stays on your case — that is the point of naming them")

    reg = authority.register_crime(pt, w, player=memory.SOLO, faction_id="warden_office",
                                   severity=4, summary="set a barn alight", turn=3)
    ok(reg["registered"] and reg["crimes"] >= 1, "the crime enters the ledger")

    paid = authority.settle(pt, w, player=memory.SOLO, faction_id="warden_office",
                            how="pay", turn=4)
    ok(paid["settled"] and paid["tier"] == "wanted",
       "paying clears the warrant but not the memory of it")

    _standing(pt, "warden_office", -62.0)
    atoned = authority.settle(pt, w, player=memory.SOLO, faction_id="warden_office",
                              how="atone", turn=5)
    ok(atoned["settled"] and atoned["standing"] > -62.0,
       f"atoning moves how they actually feel ({atoned['standing']}), not just the ledger")

    before = awareness.rep(pt, "warden_office", memory.SOLO)["standing"]
    authority.decay(pt, w, memory.SOLO, 6)
    after = awareness.rep(pt, "warden_office", memory.SOLO)["standing"]
    ok(abs(after) < abs(before),
       f"standing drifts back toward neutral on its own ({before:.1f} -> {after:.1f})")


def test_c4_cross_run_memory():
    section("C4 — the town remembers you, across runs, per account")
    db.init()
    acct = auth.register("c4@storyliver.test", "a-properly-long-password", "Wanted")

    pt = engine.create_playthrough(acct["id"])
    w = engine.world_for(engine._pt(pt))
    _standing(pt, "warden_office", -58.0, known=3)
    authority.register_crime(pt, w, player=memory.SOLO, faction_id="warden_office",
                             severity=4, summary="arson", turn=5)
    kept = authority.remember_across_runs(pt, w, account_id=acct["id"],
                                          player=memory.SOLO, turn=5)
    ok(kept["persisted"], "a run's standing is folded into the town's memory")

    pt2 = engine.create_playthrough(acct["id"])
    row = authority.ledger(pt2, engine.world_for(engine._pt(pt2)), memory.SOLO)[0]
    ok(row["standing"] < -20,
       f"a NEW run on the same account starts already disliked ({row['standing']})")
    ok(row["crimes"] >= 1, "and the crime count carries")
    ok(row["standing"] > -58.0,
       "but softened, not carried whole — time and distance count for something")

    guest_pt = engine.create_playthrough("guest-client-string")
    grow = authority.ledger(guest_pt, engine.world_for(engine._pt(guest_pt)),
                            memory.SOLO)[0]
    ok(grow["standing"] == 0 and grow["tier"] == "clean",
       "a guest starts clean — memory needs an identity that can be verified")


# ------------------------------------------------------------------ D
def test_d_auth():
    section("D — a real account, not a spoofable string")
    db.init()
    acct = auth.register("d@storyliver.test", "another-long-password", "Yusuf")
    ok(acct["display_name"] == "Yusuf" and acct["email"] == "d@storyliver.test",
       "register returns a profile")
    ok("password_hash" not in acct and "password" not in acct,
       "the public profile cannot leak a hash")

    raw = db.row("SELECT * FROM accounts WHERE id=?", (acct["id"],))
    ok("another-long-password" not in str(dict(raw)),
       "the plaintext password is nowhere in the row")
    ok(raw["password_hash"].startswith("$argon2id$"),
       "it is stored as Argon2id — OWASP's first recommendation")

    out = auth.login("d@storyliver.test", "another-long-password")
    ok(out["token"] and len(out["token"]) > 30, "login issues a session token")
    ok(auth.session_account(out["token"]) == acct["id"],
       "and the token resolves back to the account")

    stored = db.row("SELECT token_hash FROM auth_sessions WHERE account_id=?", (acct["id"],))
    ok(out["token"] not in stored["token_hash"],
       "only a HASH of the token is stored — a database leak is not live sessions")

    for bad in ("wrong-password", ""):
        try:
            auth.login("d@storyliver.test", bad)
            ok(False, f"a bad password ({bad!r}) was accepted")
        except auth.AuthError:
            ok(True, f"a bad password ({bad or 'empty'!r}) is refused")

    try:
        auth.login("nobody@storyliver.test", "whatever-long-enough")
        ok(False, "an unknown email was accepted")
    except auth.AuthError as e:
        ok("wrong email or password" in str(e),
           "an unknown email gives the SAME message — no account enumeration")

    try:
        auth.register("d@storyliver.test", "yet-another-password")
        ok(False, "a duplicate email was allowed")
    except auth.AuthError:
        ok(True, "an email cannot be registered twice")

    try:
        auth.register("e@storyliver.test", "short")
        ok(False, "a 5-character password was accepted")
    except auth.AuthError:
        ok(True, "a too-short password is refused")

    prof = auth.update_profile(acct["id"], display_name="Yusuf A", bio="I run the table.")
    ok(prof["display_name"] == "Yusuf A" and prof["bio"] == "I run the table.",
       "the profile is editable")

    auth.change_password(acct["id"], "another-long-password", "a-brand-new-password")
    ok(auth.session_account(out["token"]) is None,
       "changing the password ends every existing session — the point of doing it")
    ok(auth.login("d@storyliver.test", "a-brand-new-password")["token"],
       "and the new password works")

    ok(auth.is_guest("some-client-supplied-string"), "an unbacked id is a guest")
    ok(not auth.is_guest(acct["id"]), "a real account is not")

    tok = auth.login("d@storyliver.test", "a-brand-new-password")["token"]
    auth.logout(tok)
    ok(auth.session_account(tok) is None, "logout revokes immediately")


def test_d_guest_claiming():
    section("D — signing up must not cost you what you already made")
    db.init()
    from backend import mana
    guest = "guest-browser-id-01"
    pt = engine.create_playthrough(guest)
    mana.grant(pt, 140)
    engine.create_playthrough(guest)

    offer = auth.claimable(guest)
    ok(offer["any"] and offer["stories"] == 2,
       f"the offer is specific before you sign up ({offer['stories']} stories, {offer['mana']} Mana)")

    acct = auth.register("claim@storyliver.test", "a-properly-long-password", "Claimer")
    moved = auth.claim(guest, acct["id"])
    ok(moved["claimed"] and moved["moved"].get("playthroughs") == 2,
       "both stories follow the player onto the account")
    ok(not db.rows("SELECT id FROM playthroughs WHERE user_id=?", (guest,)),
       "and nothing is left stranded under the old browser id")
    ok(len(db.rows("SELECT id FROM playthroughs WHERE user_id=?", (acct["id"],))) == 2,
       "the account owns them now")

    other = auth.register("other@storyliver.test", "a-properly-long-password", "Other")
    try:
        auth.claim(other["id"], acct["id"])
        ok(False, "one account could claim another — that is account takeover")
    except auth.AuthError:
        ok(True, "claiming only works FROM a guest — never from a real account")


def main():
    print("StoryLiver — legibility, onboarding, authority, auth")
    print("  offline stub, no API key, no spend\n")
    for fn in (test_a_legibility, test_a5_mode_gating, test_c1_patrol,
               test_c2_c3_ladder, test_c4_cross_run_memory, test_d_auth,
               test_d_guest_claiming):
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


def test_all_workstreams():
    for fn in (test_a_legibility, test_a5_mode_gating, test_c1_patrol,
               test_c2_c3_ladder, test_c4_cross_run_memory, test_d_auth,
               test_d_guest_claiming):
        fn()
    assert not FAILS, "\n".join(FAILS)


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_TMP, ignore_errors=True)
    raise SystemExit(code)
