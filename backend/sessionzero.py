"""Canon Session Zero - the questions that decide what kind of story this is.

Naming a setting is not enough information to build a world anyone wants to
play. "Jujutsu Kaisen" could mean a first-year at the Tokyo school before
anything goes wrong, or a civilian in the middle of Shibuya. Same canon,
completely different game - and until now the engine guessed, which is how you
get a world that is technically about the right setting and about nothing in
particular.

The research is specific about what has to be settled before play:

  WHERE ON THE TIMELINE. Canon RPGs live or die on entry point - who is alive,
  what has already happened, what everyone already knows. Fanfiction-RPG design
  calls the fixed points the "Stations of the Canon"; StoryLiver already models
  them as fated events, so the only missing piece was letting the player choose
  which station they arrive at.

  WHERE YOU SIT IN THE POWER SYSTEM. This is the one that breaks canon games.
  Anime power scaling establishes rigid hierarchies where some characters are
  canonically untouchable; a protagonist with undefined power relative to that
  cast produces either a god or a bystander, both boring. Power-scaling
  research favours SYSTEMIC placement - locate the player inside the series'
  own system (grade, rank, breathing style, devil fruit) rather than inventing
  a number.

  WHAT LIMITS YOU. A constraint is what makes a power interesting. Asking for
  one costs a sentence and shapes every scene afterwards.

Everything here is deterministic and $0. It assembles QUESTIONS from a dossier
that has already been fetched, and folds the ANSWERS into the brief the world
builder receives. No extra model call.
"""
from __future__ import annotations

from . import canon_seed, db, research

# A canon world where research found a power system or a timeline gets the
# specific questions. Everything else gets the general ones, which are still
# worth asking - an original world has a power level too.
GENERAL = [
    {
        "id": "role",
        "q": "Who are you here?",
        "why": "Everything else follows from this - what you can do, who talks to you, what is at stake.",
        "kind": "text",
        "placeholder": "a first-year student · a courier who sees too much · the warden's daughter",
    },
    {
        "id": "power",
        "q": "How strong are you, compared to the people in this world?",
        "why": "The single thing that decides whether a scene is tense or decorative. Being weak is a better story than being unbeatable.",
        "kind": "choice",
        "options": [
            {"id": "ordinary", "label": "Ordinary",
             "blurb": "No abilities. Everything dangerous is genuinely dangerous."},
            {"id": "novice", "label": "Beginner",
             "blurb": "You have the power, barely. It fails under pressure."},
            {"id": "capable", "label": "Capable",
             "blurb": "You hold your own against ordinary threats, not named ones."},
            {"id": "strong", "label": "Strong",
             "blurb": "Recognised. Named characters take you seriously."},
            {"id": "peer", "label": "A peer of the greats",
             "blurb": "You stand with the strongest. The world has to escalate to threaten you."},
        ],
    },
    {
        "id": "limit",
        "q": "What limits you?",
        "why": "A power with no cost is a cheat code. This is what the world will press on.",
        "kind": "text",
        "placeholder": "it burns through me · only works on what I can see · my family must never find out",
    },
]


def questions(dossier: dict) -> dict:
    """The Session Zero form for this setting, grounded in what was researched.

    Options come from the world's ACTUAL arcs and powers where research found
    them, so the player is choosing "start at the Mugen Train arc" rather than
    typing a guess and hoping the model recognises it."""
    found = bool(dossier.get("found"))
    out = {"canon": found, "setting": dossier.get("canonical_name") or dossier.get("setting", ""),
           "questions": []}

    # F1 - era, before entry. Era picks the TIMELINE ("the Sengoku era" -
    # centuries before the present-day cast exists at all); entry picks a
    # point WITHIN one timeline ("the Mugen Train arc"). Only asked when
    # canon_seed actually has more than one era on record for this setting -
    # every other setting is unaffected, exactly as before.
    eras = canon_seed.era_options(dossier.get("setting", ""), dossier.get("canonical_name", ""))
    if eras:
        out["questions"].append({
            "id": "era",
            "q": "Which era?",
            "why": "A different era can mean an almost entirely different cast - this "
                   "changes who you can actually meet, not just when.",
            "kind": "choice",
            "options": [{"id": e["id"], "label": e["label"], "blurb": e["blurb"]} for e in eras],
        })

    arcs = [a for a in (dossier.get("arcs") or []) if a.get("name")][:10]
    if arcs:
        out["questions"].append({
            "id": "entry",
            "q": "Where in the story do you come in?",
            "why": "This sets who is alive, what has already happened, and what you are "
                   "expected to know. You are not required to start at the beginning.",
            "kind": "choice",
            "options": ([{"id": "start", "label": "The very beginning",
                          "blurb": "Before any of it. You watch it happen."}]
                        + [{"id": research.slugify(a["name"]) if hasattr(research, "slugify")
                            else a["name"].lower().replace(" ", "_"),
                            "label": a["name"], "blurb": (a.get("note") or "")[:90]}
                           for a in arcs]
                        + [{"id": "after", "label": "After it all",
                            "blurb": "The dust has settled. Live in what is left."}]),
        })

    powers = [p for p in (dossier.get("powers") or []) if p.get("name")][:10]
    if powers:
        out["questions"].append({
            "id": "system",
            "q": "What is yours?",
            "why": "The world runs on its own rules. Pick where you sit inside them "
                   "rather than inventing something beside them.",
            "kind": "choice",
            "options": ([{"id": "none", "label": "Nothing",
                          "blurb": "You have no ability at all. This is a harder, better story."}]
                        + [{"id": p["name"].lower().replace(" ", "_"),
                            "label": p["name"], "blurb": (p.get("note") or "")[:90]}
                           for p in powers]),
        })

    # A CROSSOVER is not a setting question, it is an arrival question. When
    # the premise carries somebody in from somewhere else - "Charlie from
    # Hazbin Hotel, inside The Last of Us" - the two things the builder gets
    # wrong on its own are how they got here and whether anyone knows what
    # they are. Neither is answerable from the host world's research, so
    # neither was being asked.
    premise = dossier.get("premise") or {}
    imports = [i for i in (premise.get("imports") or dossier.get("imports") or [])
               if i.get("character")]
    if imports:
        who = ", ".join(i["character"] for i in imports[:3])
        out["crossover"] = True
        out["questions"].append({
            "id": "arrival",
            "q": f"How did {who} get here?",
            "why": "The world has to react to an arrival it has no explanation for. "
                   "Pick one and it becomes a fact the setting is built around.",
            "kind": "choice",
            "options": [
                {"id": "always", "label": "They have always been here",
                 "blurb": "No arrival. The world remembers them as part of it, "
                          "and nobody finds them strange."},
                {"id": "torn", "label": "Torn through, recently",
                 "blurb": "They landed. It was witnessed, it is unexplained, and "
                          "somebody is already looking into it."},
                {"id": "hidden", "label": "Arrived, and hiding it",
                 "blurb": "They know where they came from. Nobody else does, and "
                          "being found out costs something."},
            ],
        })
        out["questions"].append({
            "id": "keeps",
            "q": f"Does {who} keep what they could do?",
            "why": "A power from another world either works here or it does not, "
                   "and the answer changes every encounter in the game.",
            "kind": "choice",
            "options": [
                {"id": "full", "label": "Everything, intact",
                 "blurb": "They are as powerful here as they were there - and this "
                          "world has no answer for it yet."},
                {"id": "weakened", "label": "Weakened, and it costs",
                 "blurb": "It still works. It works badly, it draws attention, and "
                          "it takes something out of them."},
                {"id": "none", "label": "Nothing carried over",
                 "blurb": "The person, not the power. They are who they are with "
                          "none of what they had."},
            ],
        })

    # WHO THE PLAYER IS, and what the world already thinks of them.
    #
    # The questions above settle what the player can DO. None of them settle
    # who they ARE, so the builder invented it: a world would come back with a
    # protagonist who had no name, no past, no reason to be standing there, and
    # a town that had no opinion of them either way. Every one of these is a
    # fact the build can act on, and three of them change the world's state
    # before turn one rather than only its prose.
    out["questions"].extend([
        {
            "id": "name",
            # Not "Who are you here?" - that is the `role` question below, and
            # the live forge rendered the two of them four rows apart with
            # effectively identical wording. This one asks for a name.
            "q": "What are you called?",
            "why": "A name and a few words of who you are. The cast uses this - "
                   "what they call you is the first thing that makes you a person "
                   "in this world rather than a camera moving through it.",
            "kind": "text",
            "placeholder": "Yuki, a courier who reads more than she admits",
        },
        {
            "id": "origin",
            "q": "Where did you come from?",
            "why": "Where somebody is from decides who trusts them on sight. It is "
                   "also the first thing anybody asks a face they do not know.",
            "kind": "text",
            "placeholder": "Two valleys over, and nobody here has been there",
        },
        {
            "id": "known",
            "q": "Do the people here already know you?",
            "why": "This is the single biggest difference in how a world opens. A "
                   "stranger earns every door; somebody who grew up here starts "
                   "with history they cannot put down.",
            "kind": "choice",
            "options": [
                {"id": "stranger", "label": "Nobody knows your face",
                 "blurb": "You arrived. Everything you get here, you get by earning it."},
                {"id": "known", "label": "Known, and ordinary",
                 "blurb": "You have been around long enough to be unremarkable. "
                          "People nod. Nobody watches you."},
                {"id": "local", "label": "You grew up here",
                 "blurb": "They knew you as a child. Some of them have been waiting "
                          "years to say something to you."},
                {"id": "notorious", "label": "They know exactly who you are",
                 "blurb": "Your name arrived before you did, and not everyone is "
                          "glad about it."},
            ],
        },
        {
            "id": "ties",
            "q": "Is there anyone here you already know?",
            "why": "Names one real relationship the world starts with instead of "
                   "making you build every bond from zero. Say who, and what you "
                   "are to each other.",
            "kind": "text",
            "placeholder": "Zenitsu — we trained together, and he still owes me",
        },
    ])

    # Only where there is a real cast to balance against. An original world has
    # nothing but invented people in it, so asking how many of them to invent
    # alongside the canon ones is a question about nothing.
    if dossier.get("found") or (dossier.get("characters") or []):
        out["questions"].append({
            "id": "cast_mix",
            "q": "Who should fill this world?",
            "why": "A canon world has a limited real cast. This decides whether the "
                   "rest of the town is invented locals or whether the story stays "
                   "tight on the people the source actually wrote.",
            "kind": "choice",
            "options": [
                {"id": "canon_only", "label": "The real cast, and almost nobody else",
                 "blurb": "Only the people from the source. A smaller, denser world "
                          "where everyone you meet is someone you have heard of."},
                {"id": "balanced", "label": "The real cast, plus locals",
                 "blurb": "The source's people, and ordinary residents around them "
                          "to make the place feel lived in."},
                {"id": "populated", "label": "A full town around them",
                 "blurb": "Plenty of invented residents with their own business, "
                          "and the real cast moving through it."},
            ],
        })

    out["questions"].extend(GENERAL)
    return out


def brief(answers: dict, dossier: dict) -> str:
    """Fold the answers into the world-builder's brief.

    Written as instructions about the PLAYER, not the world, because that is
    what the builder gets wrong on its own: it will happily produce a good
    setting in which the protagonist has no defined place."""
    if not answers:
        return ""
    a = {k: str(v).strip() for k, v in answers.items() if str(v or "").strip()}
    if not a:
        return ""

    lines = ["THE PLAYER'S PLACE IN THIS WORLD (build around these, do not overrule them):"]
    if a.get("era"):
        eras = canon_seed.era_options(dossier.get("setting", ""), dossier.get("canonical_name", ""))
        label = next((e["label"] for e in eras if e["id"] == a["era"]), a["era"])
        lines.append(f"- ERA: {label}. The REAL PLACES and REAL CHARACTERS listed above "
                     f"already belong to this era specifically - do not mix in anyone or "
                     f"anywhere from a different era of this setting.")
    if a.get("entry") and a["entry"] not in ("start",):
        where = "after everything the source covers" if a["entry"] == "after" else a["entry"].replace("_", " ")
        lines.append(f"- ENTRY POINT: the story begins {where}. Seed the world as it stands "
                     f"THEN - who is already dead, what has already happened, what is common "
                     f"knowledge. Do not replay earlier events as if they are still ahead.")
    if a.get("role"):
        lines.append(f"- WHO THEY ARE: {a['role']}")
    if a.get("system") and a["system"] != "none":
        lines.append(f"- THEIR ABILITY: {a['system'].replace('_', ' ')}, expressed through "
                     f"this world's own system rather than a power invented beside it.")
    elif a.get("system") == "none":
        lines.append("- THEY HAVE NO ABILITY. Do not quietly give them one. Their leverage is "
                     "knowledge, nerve and other people.")
    if a.get("power"):
        band = {
            "ordinary": "an ordinary person. Named characters could kill them without effort, "
                        "and both sides know it.",
            "novice": "barely capable. Their ability works, then fails at the worst moment.",
            "capable": "genuinely competent against ordinary threats, outclassed by named ones.",
            "strong": "strong enough to be recognised. Named characters treat them as real.",
            "peer": "a peer of the strongest here. The world must escalate to threaten them.",
        }.get(a["power"], a["power"])
        lines.append(f"- POWER LEVEL: {band}")
    if a.get("limit"):
        lines.append(f"- WHAT LIMITS THEM: {a['limit']}. Press on this. A limit that never "
                     f"costs anything is decoration.")

    if a.get("arrival"):
        how = {
            "always": "they have ALWAYS been here. Write them into this world's history "
                      "and its people's memories; nobody finds them strange, and there "
                      "is no arrival to explain.",
            "torn": "they arrived RECENTLY and it was witnessed. It is unexplained, it "
                    "is talked about, and at least one faction is already looking into "
                    "it. Give that investigation a name and a person running it.",
            "hidden": "they arrived and are HIDING it. They know where they came from; "
                      "nobody else does. Give somebody a reason to be suspicious and "
                      "something concrete that would expose them.",
        }.get(a["arrival"], a["arrival"])
        lines.append(f"- HOW THEY GOT HERE: {how}")
    if a.get("keeps"):
        band = {
            "full": "everything they could do still works, at full strength. This world "
                    "has no counter for it yet - build the reaction, not a nerf.",
            "weakened": "what they could do still works, badly. It costs them, it is "
                        "conspicuous, and it fails when it matters most.",
            "none": "nothing carried over. They are exactly who they are with none of "
                    "what they had, and they know it.",
        }.get(a["keeps"], a["keeps"])
        lines.append(f"- WHAT CARRIED OVER: {band}")

    if a.get("name"):
        lines.append(f"- WHO THEY ARE: {a['name'][:160]}. This is the PLAYER, not a "
                     f"character you write. Use this name for them; do not invent another "
                     f"one, and do NOT put them in the cast - a live build listed the "
                     f"player as an NPC standing in the opening square, so the player "
                     f"could walk up to themselves.")
    if a.get("origin"):
        lines.append(f"- WHERE THEY CAME FROM: {a['origin'][:160]}. Somebody here should "
                     f"have an opinion about that place.")
    if a.get("known"):
        standing = {
            "stranger": "NOBODY here knows their face. No character starts with a "
                        "relationship to them, every door is closed until it is earned, "
                        "and at least one person should treat a new face as a problem.",
            "known": "they have been around long enough to be unremarkable. People nod. "
                     "Two or three characters know them by name and think nothing of it.",
            "local": "they GREW UP here. Give three characters a shared history with them "
                     "that predates the story - somebody who taught them, somebody who "
                     "remembers them small, somebody they wronged and never fixed.",
            "notorious": "their name got here before they did. At least two characters "
                         "have already decided what they think, one of them wrongly, and "
                         "one faction has a standing position on them.",
        }.get(a["known"], a["known"])
        lines.append(f"- WHAT THIS PLACE ALREADY KNOWS: {standing}")
    if a.get("ties"):
        lines.append(f"- SOMEBODY THEY ALREADY KNOW: {a['ties'][:200]}. Build that "
                     f"relationship in as an existing fact with history behind it, not as "
                     f"an introduction, and set their starting feeling accordingly.")
    if a.get("cast_mix"):
        mix = {
            "canon_only": "Keep the cast almost entirely to the source's real people. "
                          "Invent at most one or two ordinary residents, and only where a "
                          "scene would otherwise be empty.",
            "balanced": "The source's real people, with a handful of ordinary invented "
                        "residents around them so the place feels inhabited.",
            "populated": "A full town: plenty of invented residents with their own "
                         "business and grievances, and the source's real people moving "
                         "through it.",
        }.get(a["cast_mix"], a["cast_mix"])
        lines.append(f"- WHO FILLS THIS WORLD: {mix}")

    lines.append("Give at least two named characters a reason to care that this specific person "
                 "is here - a use for them, a suspicion of them, or a grudge.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persistence - the answers belong to the world, so a re-entry keeps them
# ---------------------------------------------------------------------------

def save(world_id: str, answers: dict) -> dict:
    import json
    db.run("INSERT INTO session_zero (world_id, answers, updated_at) VALUES (?,?,?)"
           " ON CONFLICT(world_id) DO UPDATE SET answers=excluded.answers,"
           " updated_at=excluded.updated_at",
           (world_id, json.dumps(answers or {}), db.now()))
    return get(world_id)


def get(world_id: str) -> dict:
    row = db.row("SELECT answers FROM session_zero WHERE world_id=?", (world_id,))
    return db.jload(row["answers"], {}) if row else {}
