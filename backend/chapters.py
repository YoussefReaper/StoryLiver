"""Chapters - a canon story told in the order it was told, then let go of.

A world used to be one town and one spine of seven fated events, and when that
spine finished the story was simply over. For an ORIGINAL world that is right.
For a world that continues somebody else's setting it is wrong in a way players
notice immediately: Demon Slayer is not one town, it is Mount Natagumo, then
the Mugen Train, then the Entertainment District. Finishing the first place and
being told "the end" is finishing chapter one of a book and having the rest
taken away.

So a canon world is a sequence of CHAPTERS:

  * Each chapter is a real arc of the source, in the source's own order, built
    when you reach it and not before. Research already scrapes these from the
    wiki's own arc categories, which is the closest thing to an official
    running order that exists.
  * A chapter ENDS when its fate spine has finished - the seven things that
    were going to happen have happened. That is a narrative signal rather than
    a turn count, so a chapter ends when its story does.
  * Ending one is an offer, never a shove. Travel now, or stay: there is
    usually someone you still want to talk to, and a story that yanks you out
    of a room the moment the plot resolves is a story that does not trust you.
  * Earlier chapters stay on the map. They are ordinary places you can walk
    back to, because the people there remember you and that is the whole point
    of the engine underneath.
  * After the LAST canon chapter there is no more canon to follow. What comes
    next is the aftermath: no fate spine, no next chapter, an open world that
    is yours. This is the honest label as well as the better game - past the
    end of the source, nothing that happens is canon, and the world stops
    pretending otherwise.

Everything here is deterministic and free except the build of a chapter you
actually travel to, which is one call, paid once.
"""
from __future__ import annotations

import json

from . import db


def plan(dossier: dict, fallback: str = "") -> list:
    """The chapter running order for a world, from the source's own arcs.

    Research collects these out of the wiki's arc/saga categories, which is as
    close to an authoritative running order as a setting has. A world with no
    arcs on record gets a single chapter, which is exactly how it behaved
    before chapters existed."""
    arcs = [a for a in (dossier.get("arcs") or [])
            if isinstance(a, dict) and (a.get("name") or "").strip()]
    out = []
    for i, a in enumerate(arcs[:12]):
        out.append({
            "n": i + 1,
            "title": a["name"].strip()[:80],
            "blurb": (a.get("note") or "").strip()[:200],
        })
    if not out:
        out = [{"n": 1, "title": (fallback or "The story").strip()[:80], "blurb": ""}]
    return out


def of(pt) -> dict:
    """Where this playthrough sits in its own running order."""
    if isinstance(pt, str):
        pt = db.row("SELECT * FROM playthroughs WHERE id=?", (pt,)) or {}
    book = db.jload(pt.get("chapters") or "", []) or []
    at = int(pt.get("chapter") or 1)
    return {
        "n": at,
        "total": len(book),
        "title": (book[at - 1]["title"] if 0 < at <= len(book) else ""),
        "next": (book[at]["title"] if at < len(book) else ""),
        "book": book,
        # Past the last chapter of the source there is nothing left to follow.
        "aftermath": bool(book) and at > len(book),
    }


def set_book(pt_id: str, book: list) -> None:
    db.run("UPDATE playthroughs SET chapters=?, chapter=COALESCE(chapter,1) WHERE id=?",
           (json.dumps(book or []), pt_id))


def finished(pt, world, fired: set) -> bool:
    """True when this chapter's story has actually finished.

    The fate spine IS the chapter: when the last of it has landed there is no
    more written story here, only the place and the people. Counted from what
    has FIRED rather than from the turn number, because fate no longer runs on
    a counter."""
    fate = world.fated_events or []
    return bool(fate) and all(f["id"] in fired for f in fate)


def advance(pt_id: str) -> dict:
    """Move to the next chapter. The offer has already been accepted."""
    row = db.row("SELECT chapter, chapters FROM playthroughs WHERE id=?", (pt_id,))
    at = int((row or {}).get("chapter") or 1) + 1
    db.run("UPDATE playthroughs SET chapter=? WHERE id=?", (at, pt_id))
    return of(pt_id)


def aftermath_directive(pos: dict) -> str:
    """What the narrator is told once the source has run out.

    Not "the game is over" - the opposite. Everything from here is unwritten,
    which is the one stretch of a canon world where the player is genuinely
    the only author of what happens next."""
    if not pos.get("aftermath"):
        return ""
    return (
        "\nPAST THE END OF THE SOURCE. Every arc this setting had has been "
        "played. Nothing from here is canon and nothing is fated: there is no "
        "next written event, and no outcome anybody is owed. The world keeps "
        "its people, its grudges and its memory of this player, and goes on "
        "from wherever they left it. Write consequences, not prophecy.\n"
    )
