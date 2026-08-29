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
    balance = pt["mana_balance"] if pt else 0
    if led["usd_spent"] >= config.DAILY_COST_CAP_USD:
        mode = RAIL
    elif balance > 0 or free_left > 0:
        mode = FULL
    else:
        mode = EMBER
    return {
        "balance": balance,
        "used": pt["mana_used"] if pt else 0,
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
    if sum(left for _, left in pool) >= cost or pt["mana_balance"] >= cost:
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

    if pt["mana_balance"] >= cost:
        db.run("UPDATE playthroughs SET mana_balance=mana_balance-?, mana_used=mana_used+? WHERE id=?",
               (cost, cost, pt["id"]))
        db.run("INSERT INTO daily_ledger (user_id,day,paid_mana_used) VALUES (?,?,?)"
               " ON CONFLICT(user_id,day) DO UPDATE SET paid_mana_used = paid_mana_used + excluded.paid_mana_used",
               (user_id, db.today(), cost))
        return "mana"

    return "ember"


def grant(pt_id, amount):
    db.run("UPDATE playthroughs SET mana_balance=mana_balance+? WHERE id=?", (int(amount), pt_id))


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
