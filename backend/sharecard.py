"""Shareable story recap - the acquisition loop.

Renders a self-contained SVG card from real session state: the passage that
mattered, who stood where, how many turns, and the room code so whoever sees it
can join. No pixels are fetched, no fonts are loaded, nothing phones home - it
is one file the player owns and can post anywhere.
"""
from __future__ import annotations

import html
import textwrap

from . import db, engine, memory, streaks

W, H = 1200, 630          # the aspect every social card is cropped to

INK = "#0A0D13"
PANEL = "#141924"
VELLUM = "#EAE3D5"
DIM = "#ADA695"
EMBER = "#E8A54B"
EMBER_SOFT = "#F6D19A"
VERDIGRIS = "#4FB3A6"
LINE = "#1D2432"


def _wrap(text: str, width: int, max_lines: int) -> list[str]:
    lines = textwrap.wrap(" ".join((text or "").split()), width=width)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,;: ") + "…"
    return lines


def pick_moment(pt_id, entry_id=None):
    """The passage worth showing: the one asked for, else the last turn that
    actually mattered - fate, a contested action, or a Director beat."""
    if entry_id:
        row = db.row("SELECT * FROM narrative WHERE id=? AND playthrough_id=?", (entry_id, pt_id))
        if row:
            return row
    for kinds in (("fate",), ("contest",), ("narration",), ("opening",)):
        row = db.row(
            "SELECT * FROM narrative WHERE playthrough_id=? AND kind IN (%s)"
            " ORDER BY id DESC LIMIT 1" % ",".join("?" * len(kinds)),
            (pt_id, *kinds))
        if row:
            return row
    return None


def data_for(pt_id, *, entry_id=None, player=memory.SOLO):
    pt = db.row("SELECT * FROM playthroughs WHERE id=?", (pt_id,))
    if not pt:
        raise KeyError("no such playthrough")
    world = engine.world_for(pt)
    moment = pick_moment(pt_id, entry_id)
    st = engine.snapshot(pt_id, player)
    session = db.row("SELECT * FROM sessions WHERE playthrough_id=?", (pt_id,))

    closest = sorted(
        [n for n in st["npcs"] if n["alive"]],
        key=lambda n: -(n["affinity"] + n["trust"]))[:1]
    feared = sorted([n for n in st["npcs"] if n["alive"]], key=lambda n: -n["fear"])[:1]
    dead = [n["name"] for n in st["npcs"] if not n["alive"]]
    passed = [f for f in st["fate"] if f["status"] == "passed"]

    return {
        "world": world.name,
        "tagline": world.get("tagline", ""),
        "turn": st["turn"],
        "day": st["day"],
        "location": st["location_name"],
        "text": (moment["text"] if moment else world.get("premise", "")),
        "kind": moment["kind"] if moment else "opening",
        "code": session["code"] if session else None,
        "ally": closest[0] if closest and (closest[0]["affinity"] + closest[0]["trust"]) > 10 else None,
        "afraid": feared[0] if feared and feared[0]["fear"] >= 20 else None,
        "dead": dead,
        "fate_passed": len(passed),
        "fate_total": len(st["fate"]),
        "streak": streaks.status(pt["user_id"]),
        "personal_only": bool(world.get("personal_only")),
    }


def svg(pt_id, *, entry_id=None, player=memory.SOLO) -> str:
    d = data_for(pt_id, entry_id=entry_id, player=player)
    e = html.escape
    body = _wrap(d["text"], 62, 6)
    line_h = 40
    text_y = 232

    stats = [f"Turn {d['turn']}", f"Day {d['day']}", d["location"]]
    if d["fate_total"]:
        stats.append(f"Fate {d['fate_passed']}/{d['fate_total']}")
    if d["streak"]["current"] > 1:
        stats.append(f"{d['streak']['current']}-day streak")

    facts = []
    if d["ally"]:
        facts.append(("stands with you", d["ally"]["name"], VERDIGRIS))
    if d["afraid"]:
        facts.append(("is afraid of you", d["afraid"]["name"], "#D0563D"))
    if d["dead"]:
        facts.append(("did not make it", d["dead"][0], DIM))

    kicker = {"fate": "FATE", "contest": "CONTESTED", "opening": "THE BEGINNING"}.get(d["kind"], "A MOMENT")

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'font-family="Georgia,\'Iowan Old Style\',serif">',
        '<defs>',
        '<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{PANEL}"/><stop offset="1" stop-color="{INK}"/></linearGradient>',
        '<radialGradient id="glow" cx="0.18" cy="0" r="0.85">'
        f'<stop offset="0" stop-color="{EMBER}" stop-opacity="0.20"/>'
        f'<stop offset="1" stop-color="{EMBER}" stop-opacity="0"/></radialGradient>',
        '</defs>',
        f'<title>StoryLiver — {e(d["world"])}, turn {d["turn"]}</title>',
        f'<desc>{e(" ".join((d["text"] or "").split())[:300])}</desc>',
        f'<rect width="{W}" height="{H}" fill="url(#bg)"/>',
        f'<rect width="{W}" height="{H}" fill="url(#glow)"/>',
        f'<rect x="0" y="0" width="{W}" height="4" fill="{EMBER}"/>',

        # brand
        f'<g transform="translate(64,64)">',
        f'<path d="M0 0h22l12 12v34H0z" fill="none" stroke="{EMBER}" stroke-width="3" stroke-linejoin="round" opacity="0.85"/>',
        f'<path d="M22 0v12h12" fill="none" stroke="{EMBER}" stroke-width="3" stroke-linejoin="round" opacity="0.5"/>',
        f'<path d="M5 30h5l3-8 4.6 15.4 3.5-10.6 2.4 3.2h5" fill="none" stroke="{EMBER_SOFT}" '
        f'stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>',
        '</g>',
        f'<text x="120" y="96" font-family="Helvetica,Arial,sans-serif" font-size="26" font-weight="700" '
        f'fill="{VELLUM}" letter-spacing="-0.4">Story<tspan fill="{EMBER}">Liver</tspan></text>',

        f'<text x="{W-64}" y="96" text-anchor="end" font-family="Helvetica,Arial,sans-serif" '
        f'font-size="15" letter-spacing="4" fill="{EMBER}">{e(kicker)}</text>',

        # world
        f'<text x="64" y="168" font-size="46" fill="{VELLUM}">{e(d["world"])}</text>',
        f'<text x="64" y="200" font-size="21" font-style="italic" fill="{EMBER_SOFT}">{e(d["tagline"])}</text>',
    ]

    for i, line in enumerate(body):
        parts.append(
            f'<text x="64" y="{text_y + 42 + i * line_h}" font-size="27" fill="{VELLUM}">{e(line)}</text>')

    facts_y = text_y + 42 + len(body) * line_h + 34
    parts.append(f'<line x1="64" y1="{facts_y - 26}" x2="{W-64}" y2="{facts_y - 26}" stroke="{LINE}" stroke-width="1"/>')

    x = 64
    for label, who, colour in facts[:3]:
        parts.append(f'<text x="{x}" y="{facts_y}" font-family="Helvetica,Arial,sans-serif" '
                     f'font-size="13" letter-spacing="1.6" fill="{DIM}">{e(label.upper())}</text>')
        parts.append(f'<text x="{x}" y="{facts_y + 28}" font-size="22" fill="{colour}">{e(who)}</text>')
        x += 320

    parts.append(f'<text x="64" y="{H-52}" font-family="Helvetica,Arial,sans-serif" font-size="16" '
                 f'fill="{DIM}">{e("  ·  ".join(stats))}</text>')

    if d["code"]:
        parts.append(f'<rect x="{W-306}" y="{H-96}" width="242" height="52" rx="26" fill="none" '
                     f'stroke="{EMBER}" stroke-opacity="0.5"/>')
        parts.append(f'<text x="{W-185}" y="{H-70}" text-anchor="middle" font-family="Helvetica,Arial,sans-serif" '
                     f'font-size="12" letter-spacing="2.4" fill="{DIM}">JOIN THIS STORY</text>')
        parts.append(f'<text x="{W-185}" y="{H-52}" text-anchor="middle" font-family="Helvetica,Arial,sans-serif" '
                     f'font-size="21" font-weight="700" letter-spacing="4" fill="{EMBER}">{e(d["code"])}</text>')

    if d["personal_only"]:
        parts.append(f'<text x="64" y="{H-24}" font-family="Helvetica,Arial,sans-serif" font-size="12" '
                     f'fill="#4A5265">Inspired-by personal world · not affiliated with or endorsed by any rights holder</text>')

    parts.append('</svg>')
    return "\n".join(parts)


def text_recap(pt_id, *, entry_id=None, player=memory.SOLO) -> str:
    """The one-tap post body. Carries the room code; never carries a guilt hook."""
    d = data_for(pt_id, entry_id=entry_id, player=player)
    bits = [f"{d['world']} — turn {d['turn']}, day {d['day']}."]
    if d["ally"]:
        bits.append(f"{d['ally']['name']} stands with me.")
    if d["afraid"]:
        bits.append(f"{d['afraid']['name']} is afraid of me.")
    if d["dead"]:
        bits.append(f"{d['dead'][0]} didn't make it.")
    snippet = " ".join((d["text"] or "").split())[:180].rstrip()
    body = " ".join(bits)
    tail = f"\n\nPlay the same story — room {d['code']}" if d["code"] else "\n\nStoryLiver"
    return f"{body}\n\n“{snippet}…”{tail}"
