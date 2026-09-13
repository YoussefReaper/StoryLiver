"""Play the exact scenario the player asked for, and record everything.

  Youssef        - the player, an exceptional swordsman
  Gojo Satoru    - present with HIS FULL POWER, and the player's friend
  Demon Slayer   - the host world, entered from the very start

This is not the generic audit. It builds the world with the player's own name,
entry point, ability and relationship, then plays it turn by turn and dumps the
whole transcript plus the engine's own records, so the answer to "what actually
happened" is the engine's, not a summary of it.

    python tools/play_scenario.py --out .probe/scenario.md
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_env():
    p = ROOT / ".env"
    if not p.exists():
        return
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


SETTING = ("Youssef and my friend Gojo Satoru (from Jujutsu Kaisen) "
           "in the world of Demon Slayer")

# Session Zero, exactly as a player would fill it in the UI.
ANSWERS = {
    "name": "Youssef",
    "entry": "start",                       # "from the very start"
    "role": ("a swordsman, exceptionally skilled with a blade - the best "
             "blade in any room he walks into"),
    "power": "peer",                        # peer of the strongest here
    "system": "none",                       # no demon art, no breathing style
    "limit": ("he is still flesh. A blade does not stop a demon's regeneration "
              "and he has no way to finish one for good"),
    "arrival": "torn",                      # arrived recently, witnessed
}

# Played as a person would type. Deliberately varied: it greets, looks around,
# declares intent, asks about a loss, fights, spares, and tries to leave - so
# the transcript shows the world reacting plus the beats the engine detects.
ACTIONS = [
    ("arrive", "I arrive and look for whoever is nearest, and I introduce "
               "myself as Youssef."),
    ("look around", "I take in where we have ended up - the light, the air, "
                    "who is watching."),
    ("speak to Gojo", "I turn to Gojo and ask him if he can feel whatever is "
                      "wrong with this place."),
    ("declare intent", "I tell them what I have come to do, and that I will "
                       "not be stopped by anything here."),
    ("ask about the dead", "I ask who here has lost someone to the demons. I "
                           "tell them I am sorry."),
    ("fight a demon", "I draw my blade and attack the demon."),
    ("mercy", "I lower my blade and let him live."),
    ("carry the wounded", "I pick up whoever is hurt and carry them back "
                          "toward the village."),
    ("ask about the corps", "I ask how someone joins the Demon Slayer Corps."),
    ("nightfall", "I sit down and wait for the night to pass."),
]


def _render(entry):
    kind = entry.get("kind")
    text = (entry.get("text") or "").strip()
    if kind == "you":
        return f"\n> {text}\n"
    if kind == "speech":
        sp = (entry.get("meta") or {}).get("speaker") or {}
        name = sp.get("name") or entry.get("actor")
        return f"\n**{name}:** {text}\n"
    if kind == "narration":
        return f"\n{text}\n"
    label = {"safety": "the table draws a line",
             "refusal": "the world pushes back",
             "fate": "the world, on its own",
             "beat": "the story turns",
             "npc": "somebody moves first"}.get(kind, kind)
    return f"\n_({label})_ {text}\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="play_scenario.md")
    args = ap.parse_args()

    _load_env()
    os.environ["STORYLIVER_RESEARCH"] = "on"
    tmp = tempfile.mkdtemp(prefix="storyliver-scenario-")
    os.environ["STORYLIVER_DATA_DIR"] = tmp

    from backend import (chapters, db, engine, memory, modetree, persona,
                         research, worldforge)

    db.init()
    out = ["# Played scenario", f"\nSetting asked for: **{SETTING}**\n",
           "\nSession Zero answers:\n"]
    for k, v in ANSWERS.items():
        out.append(f"- {k}: {v}")
    out.append("\n---\n")

    dossier = research.dossier("Demon Slayer")
    out.append(f"\nResearch: found={dossier.get('found')} "
               f"canonical=**{dossier.get('canonical_name')}** "
               f"wiki=`{dossier.get('wiki')}`")
    out.append(f"\nArcs: {[a.get('name') for a in (dossier.get('arcs') or [])]}\n")

    # The real build, with the player's Session Zero answers.
    world = worldforge.bootstrap(SETTING, user_id="scenario-user",
                                 mode="canon", answers=dict(ANSWERS))
    if hasattr(world, "data"):
        world = world.data

    out.append(f"\n## The world that was built\n")
    out.append(f"- name: **{world.get('name')}**")
    out.append(f"- inspired_by: `{world.get('inspired_by')}`  mode: `{world.get('mode')}`")
    out.append(f"- protagonist: `{world.get('default_protagonist')}`")
    out.append(f"- start_location: `{world.get('start_location')}`")
    out.append(f"- chapters: {[c.get('title') for c in (world.get('chapters') or [])]}")
    out.append(f"\n**Cast ({len(world.get('npcs') or [])}):**")
    for n in (world.get("npcs") or []):
        role = (n.get("anchors") or {}).get("role") or n.get("role") or ""
        mark = " [CARRIED IN]" if n.get("companion") else ""
        out.append(f"- {n['name']} - {role[:80]}{mark}")
    out.append(f"\n**Fate spine:**")
    for i, f in enumerate(world.get("fated_events") or [], 1):
        out.append(f"{i}. {f.get('desc', '')}")
    out.append("")

    pt_id = engine.create_playthrough(
        "scenario-user", world_id="scenario",
        world_json=json.dumps(world), protagonist=ANSWERS["name"])

    pos = chapters.of(pt_id)
    out.append(f"\nChapter at start: **{pos.get('n')}/{pos.get('total')}** "
               f"- {pos.get('title')}\n")

    # Is the carried-in character actually here, and with his power intact?
    gojo = next((n for n in world.get("npcs") or []
                 if "gojo" in n["name"].lower()), None)
    if gojo:
        a = gojo.get("anchors") or {}
        out.append(f"\n**Gojo, as the world holds him:**")
        out.append(f"- role: {a.get('role') or gojo.get('role')}")
        out.append(f"- voice: {a.get('voice', '')}")
        out.append(f"- from_source: `{gojo.get('from_source')}`")
        out.append(f"- companion: {gojo.get('companion')}  "
                   f"relation: {gojo.get('relation_to_player')}")
        out.append(f"- initial_relationship: {gojo.get('initial_relationship')}")
        out.append(f"- constraints: {a.get('constraints')}")
        out.append(f"- goals: {a.get('goals')}")
        out.append(f"- lines: {[(x.get('beat'), x.get('line')) for x in (a.get('famous_lines') or [])]}")
    else:
        out.append("\n**GOJO IS NOT IN THE WORLD.**\n")
    out.append("\n---\n")

    for label, action in ACTIONS:
        out.append(f"\n## {label}\n")
        try:
            res = engine.take_turn(pt_id, action)
        except Exception as e:                       # noqa: BLE001
            out.append(f"\n> {action}\n\n**(engine raised)** "
                       f"`{type(e).__name__}: {e}`\n")
            continue
        if res.get("blocked"):
            out.append(f"\n> {action}\n\n_(blocked)_ {res.get('reason')}\n")
            continue
        beat = persona.detect_beat(action, "")
        out.append(f"\n> {action}\n\n_(beat: {beat or 'ordinary'})_\n")
        for entry in (res.get("entries") or []):
            out.append(_render(entry))
        if res.get("ending"):
            out.append(f"\n**THE STORY ENDED.** {json.dumps(res['ending'])[:400]}\n")
            break

    out.append("\n---\n\n## What the engine recorded\n")
    out.append("| turn | actor | kind | what happened |")
    out.append("|---|---|---|---|")
    for e in memory.timeline(pt_id):
        cons = (e.get("consequence") or e.get("action") or "").replace("|", "/")
        out.append(f"| {e['turn']} | {e['actor']} | {e['kind']} | {cons[:150]} |")

    pos = chapters.of(pt_id)
    out.append(f"\n**Chapter now:** {pos.get('n')}/{pos.get('total')} "
               f"- {pos.get('title')}  (next: {pos.get('next')!r})")

    Path(args.out).write_text("\n".join(out), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
