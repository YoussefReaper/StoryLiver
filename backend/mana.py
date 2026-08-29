"""Mana - usage-based, gentle, and never a locked door.

The rule the whole economy is built around (Pain #5): you are never stopped
from playing. Spend Mana for the full three-model experience; when it runs out
you fall back to Ember - a cheaper narrator and a quieter world - which is free
and unlimited, forever. No subscription, no expiry, no playtime cap.

The only hard stop in the system is a per-user daily USD rail, which exists to
protect the company from a runaway loop, not to sell anything. It sits far
above what a person can reach by playing.
"""
from . import config, db

FULL, EMBER, RAIL = "full", "ember", "rail"


def ledger(user_id):
    row = db.row("SELECT * FROM daily_ledger WHERE user_id=? AND day=?", (user_id, db.today()))
    return row or {"user_id": user_id, "day": db.today(), "free_mana_used": 0,
                   "paid_mana_used": 0, "usd_spent": 0.0}


def status(user_id, pt=None, payer_ids=None):
    led = ledger(user_id)
    pool = free_pool(payer_ids or [user_id])
    free_left = sum(left for _, left in pool)
    # The wallet, not the story. `pt` is still accepted so every existing
    # caller keeps working unchanged, but it no longer decides the balance.
    absorb_playthrough_balances(user_id)
    w = wallet(user_id)
    balance = int(w["balance"])
    if led["usd_spent"] >= config.DAILY_COST_CAP_USD:
        mode = RAIL
    elif balance > 0 or free_left > 0:
        mode = FULL
    else:
        mode = EMBER
    return {
        "balance": balance,
        "used": int(w["spent"]),
        "purchased": int(w["purchased"]),
        "free_daily": config.FREE_DAILY_MANA * max(1, len(pool)),
        "payers": len(pool),
        "free_left": free_left,
        "mode": mode,
        "usd_today": round(led["usd_spent"], 5),
        "usd_cap": config.DAILY_COST_CAP_USD,
        "standard_cost": config.MANA_COST["standard"],
        "premium_cost": config.MANA_COST["premium"],
        "never_blocked": mode != RAIL,
    }


def free_pool(payer_ids):
    """Allowances stack when several paying players party up, to a 5x ceiling.
    Friends a host brings along contribute nothing and pay nothing."""
    payers = list(dict.fromkeys(payer_ids or []))[:config.MAX_ALLOWANCE_STACK]
    remaining = []
    for uid in payers:
        led = ledger(uid)
        remaining.append((uid, max(0, config.FREE_DAILY_MANA - led["free_mana_used"])))
    return remaining


def preview(user_id, pt, premium=False, payer_ids=None):
    """What would this action cost, and in what mode would it run? Reads only -
    nothing is deducted until the World Master has actually allowed the action."""
    if ledger(user_id)["usd_spent"] >= config.DAILY_COST_CAP_USD:
        return RAIL, ("You have hit today's safety limit on this account. It resets at midnight UTC. "
                      "This is a spend guard on our side, not a paywall - nothing was charged to you.")

    cost = config.MANA_COST["premium" if premium else "standard"]
    pool = free_pool(payer_ids or [user_id])
    payers = payer_ids or [user_id]
    if sum(left for _, left in pool) >= cost or payer_with_funds(payers, cost):
        return FULL, ""
    return EMBER, ("The wallet is out of Mana, so the story continues in Ember: a leaner narrator, a "
                   "quieter world. Nothing is locked, nothing is lost, and the memory stays exactly as deep.")


def commit(user_id, pt, premium=False, payer_ids=None):
    """Deduct for an action that actually happened. Refused actions never
    reach here - the world pushing back is free."""
    cost = config.MANA_COST["premium" if premium else "standard"]
    for uid, left in free_pool(payer_ids or [user_id]):
        if left >= cost:
            db.run("INSERT INTO daily_ledger (user_id,day,free_mana_used) VALUES (?,?,?)"
                   " ON CONFLICT(user_id,day) DO UPDATE SET free_mana_used = free_mana_used + excluded.free_mana_used",
                   (uid, db.today(), cost))
            return "free"

    # Spend from a PAYER's wallet. In a room that is the host, so their pack
    # carries across every world and every room they run - which is the whole
    # point of a wallet, and what "host pays" was always supposed to mean.
    payer = payer_with_funds(payer_ids or [user_id], cost)
    if payer and debit(payer, cost):
        db.run("INSERT INTO daily_ledger (user_id,day,paid_mana_used) VALUES (?,?,?)"
               " ON CONFLICT(user_id,day) DO UPDATE SET paid_mana_used = paid_mana_used + excluded.paid_mana_used",
               (payer, db.today(), cost))
        return "mana"

    return "ember"


def grant(pt_id, amount):
    """Legacy shim: grant by STORY id, resolved to that story's owner wallet.
    Kept so existing callers (the authority fine, tests) work unchanged."""
    row = db.row("SELECT user_id FROM playthroughs WHERE id=?", (pt_id,))
    if not row:
        return
    amount = int(amount)
    if amount >= 0:
        credit(row["user_id"], amount, purchased=False)
    else:
        debit(row["user_id"], -amount)


def economics():
    """Surfaced in the UI so the cost story is inspectable, not asserted."""
    per_pack = []
    for p in config.MANA_PACKS:
        per_pack.append({**p, "usd_per_action": round(p["usd"] / p["mana"], 5)})
    return {
        "packs": per_pack,
        "mana_cost": config.MANA_COST,
        "free_daily": config.FREE_DAILY_MANA,
        "max_allowance_stack": config.MAX_ALLOWANCE_STACK,
        "free_friends_per_host": config.FREE_FRIENDS_PER_HOST,
        "max_players": config.MAX_PLAYERS,
        "target_blended_cost_usd": config.TARGET_BLENDED_COST_USD,
        "models": config.MODELS,
        "no_subscription": True,
        "no_expiry": True,
        "no_playtime_cap": True,
        "host_pays_for_friends": True,
    }


# ---------------------------------------------------------------------------
# The wallet - Mana belongs to the person, not to one story
# ---------------------------------------------------------------------------
# Every story used to carry its own mana_balance, which meant a pack bought in
# one world was unreachable from the next, and buying before you had a story
# was impossible at all. A wallet is what a player already assumes they have.


def wallet(user_id):
    row = db.row("SELECT * FROM wallets WHERE user_id=?", (user_id,))
    if row:
        return row
    db.run("INSERT OR IGNORE INTO wallets (user_id, updated_at) VALUES (?,?)",
           (user_id, db.now()))
    return db.row("SELECT * FROM wallets WHERE user_id=?", (user_id,))


def balance(user_id) -> int:
    return int(wallet(user_id)["balance"])


def credit(user_id, amount: int, *, purchased=True) -> dict:
    """Add Mana to a person's wallet."""
    amount = int(amount)
    db.run("INSERT INTO wallets (user_id, balance, purchased, updated_at) VALUES (?,?,?,?)"
           " ON CONFLICT(user_id) DO UPDATE SET balance = balance + excluded.balance,"
           " purchased = purchased + excluded.purchased, updated_at = excluded.updated_at",
           (user_id, amount, amount if purchased else 0, db.now()))
    return wallet(user_id)


def debit(user_id, amount: int) -> bool:
    """Spend, but never below zero - the balance check and the write are one
    statement so two turns landing together cannot overdraw the same wallet."""
    amount = int(amount)
    with db.conn() as c:
        cur = c.execute(
            "UPDATE wallets SET balance = balance - ?, spent = spent + ?, updated_at = ?"
            " WHERE user_id = ? AND balance >= ?",
            (amount, amount, db.now(), user_id, amount))
        return cur.rowcount > 0


def absorb_playthrough_balances(user_id) -> int:
    """Fold a player's legacy per-story Mana into their wallet, exactly once.

    Runs on demand rather than as a schema migration because the old column
    is still written by nothing and read by nothing else - this is the only
    path that needs it, and doing it lazily means an old database upgrades
    itself the first time its owner is seen."""
    w = wallet(user_id)
    if w["migrated"]:
        return 0
    rows = db.rows("SELECT id, mana_balance FROM playthroughs WHERE user_id=? AND mana_balance>0",
                   (user_id,))
    total = sum(int(r["mana_balance"]) for r in rows)
    if total:
        credit(user_id, total, purchased=False)
        for r in rows:
            db.run("UPDATE playthroughs SET mana_balance=0 WHERE id=?", (r["id"],))
    db.run("UPDATE wallets SET migrated=1, updated_at=? WHERE user_id=?", (db.now(), user_id))
    return total


def payer_with_funds(payer_ids, cost):
    """Which payer's wallet can cover this. Host-pays: the host is normally
    the only payer, and their wallet follows them into every room."""
    for uid in list(dict.fromkeys(payer_ids or [])):
        if balance(uid) >= cost:
            return uid
    return None
