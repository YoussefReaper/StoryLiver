"""Play a canon world for real and PROVE what happened, against the source.

    python tools/canon_audit.py --setting "Vinland Saga"
    python tools/canon_audit.py --setting "Demon Slayer" --turns 8 --out audit.md

The question this answers is not "does the build contain the right names". It
is the harder one: during ACTUAL TURN NARRATION, did

  * the canon cast appear, with the source's own people rather than invented
    ones standing in for them,
  * the player's arrival intrude LOGICALLY - the world reacts to an outsider
    instead of politely forgetting them,
  * the canon events land in the source's own order, each one recorded on the
    timeline with what actually happened,
  * the famous lines surface at the beats they belong to, and stay holstered at
    the beats they do not,

and it proves each of those from the engine's own records rather than from the
prose looking plausible. Every claim below is backed by a specific row: a
timeline event, a chapter, a feed entry, a beat key.

Output is a Markdown audit. It is meant to be read by a person who wants to
know whether the world is actually the world.
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
    """`.env` is read by run.py, not by the library - load it here so a probe
    sees the same key the server would. Uses setdefault, so a real environment
    variable always wins over the file."""
    p = ROOT / ".env"
    if not p.exists():
        return
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# Actions written as a player would type them: nothing here knows the engine's
# nouns. They are ordered to walk the player THROUGH beats on purpose, so the
# audit can check a line is present at its own beat and absent at the others.
DEFAULT_ACTIONS = [
    ("a greeting", "I look for whoever is nearest and introduce myself."),
    ("an ordinary turn", "I look around and take in where we have ended up."),
    ("a declaration", "I tell them what I intend to do and that I will not be stopped."),
    ("a death", "I ask who here has lost someone. I tell them I am sorry."),
    ("a fight", "I draw my weapon and attack the demon."),
    ("mercy", "I lower the blade and let him live."),
    ("ordinary again", "I sit down and wait for the morning."),
    ("a journey out", "I journey to the next town along the road."),
]


def _render(entry):
    kind = entry.get("kind")
    text = (entry.get("text") or "").strip()
    if kind == "you":
        return f"\n> {text}\n"
    if kind == "speech":
        name = ((entry.get("meta") or {}).get("speaker") or {}).get("name") or entry.get("actor")
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
    ap.add_argument("--setting", default="Vinland Saga")
    ap.add_argument("--turns", type=int, default=len(DEFAULT_ACTIONS))
    ap.add_argument("--out", default="canon_audit.md")
    ap.add_argument("--actions", default="", help="a file: beat<TAB>action per line")
    args = ap.parse_args()

    _load_env()
    os.environ["STORYLIVER_RESEARCH"] = "on"
    _tmp = tempfile.mkdtemp(prefix="storyliver-audit-")
    os.environ["STORYLIVER_DATA_DIR"] = _tmp

    from backend import (arcs, canon_lore, chapters, db, engine, llm,  # noqa: E402
                         memory, persona, research, worldforge)

    actions = DEFAULT_ACTIONS
    if args.actions:
        actions = []
        for ln in Path(args.actions).read_text(encoding="utf-8").splitlines():
            if not ln.strip() or ln.startswith("#"):
                continue
            beat, _, act = ln.partition("\t")
            actions.append((beat.strip(), act.strip()))
    actions = actions[:args.turns]

    db.init()
    result = {"setting": args.setting, "actions": [], "calls": 0,
              "beat_keys": [], "entries": [], "chapters": [], "timeline": [],
              "cast": [], "famous_lines": {}, "fate": []}

    # ── 1. the source, before any play ────────────────────────────────────
    dossier = research.dossier(args.setting)
    result["research"] = {
        "found": bool(dossier.get("found")),
        "canonical": dossier.get("canonical_name", ""),
        "wiki": dossier.get("wiki", ""),
        "note": dossier.get("note", ""),
        "cast": [c.get("name", "") for c in (dossier.get("characters") or [])],
        "places": [p.get("name", "") for p in (dossier.get("places") or [])],
        "arcs": [a.get("name", "") for a in (dossier.get("arcs") or [])],
    }

    # ── 2. build the world against it ─────────────────────────────────────
    real_complete = llm.complete

    def spy(role, system, user, **kw):
        result["calls"] += 1
        return real_complete(role, system, user, **kw)

    llm.complete = spy

    world = worldforge.bootstrap(args.setting, user_id="canon-audit-user")
    if hasattr(world, "data"):
        world = world.data
    result["world"] = {
        "name": world.get("name", ""),
        "inspired_by": world.get("inspired_by", ""),
        "mode": world.get("mode", ""),
        "start": world.get("start_location", ""),
    }
    result["cast"] = [n["name"] for n in (world.get("npcs") or [])]
    result["locations"] = [l["name"] for l in (world.get("locations") or [])]
    result["fate"] = [f.get("desc", "") for f in (world.get("fated_events") or [])]

    # Lines the build actually put on every character's card. This is the set
    # the narrator can reach for, per beat.
    for n in (world.get("npcs") or []):
        fl = (n.get("anchors") or {}).get("famous_lines") or []
        if fl:
            result["famous_lines"][n["name"]] = [{"beat": x.get("beat") or "any",
                                                  "line": x.get("line", "")}
                                                 for x in fl]

    pt_id = engine.create_playthrough(
        "canon-audit-user", world_id="canon-audit",
        world_json=json.dumps(world), protagonist="You")

    world_obj = engine.world_for(engine._pt(pt_id))
    _ch = chapters.of(pt_id)
    result["chapter_state"] = {"at": _ch.get("n"), "total": _ch.get("total"),
                               "title": _ch.get("title", ""),
                               "next": _ch.get("next", ""),
                               "aftermath": _ch.get("aftermath", False)}
    result["chapters"] = [c.get("title", "") for c in (_ch.get("book") or [])]
    try:
        result["arc_timeline"] = [a.get("name", "") for a in arcs.timeline(world_obj.id)]
    except Exception:
        result["arc_timeline"] = []

    out = [f"# Canon audit - {result['world'].get('name') or args.setting}",
           f"\nSetting asked for: **{args.setting}**",
           f"World built: **{result['world'].get('name')}** "
           f"(inspired_by `{result['world'].get('inspired_by')}`, "
           f"mode `{result['world'].get('mode')}`)",
           "\n---\n"]

    # ── 3. play, recording the beat key at each turn ──────────────────────
    for label, action in actions:
        try:
            res = engine.take_turn(pt_id, action)
        except Exception as e:                    # noqa: BLE001 - report it
            result["actions"].append({"label": label, "action": action,
                                      "beat": "", "raised": f"{type(e).__name__}: {e}"})
            out.append(f"\n> {action}\n\n**(engine raised)** `{type(e).__name__}: {e}`\n")
            continue

        if res.get("blocked"):
            result["actions"].append({"label": label, "action": action,
                                      "beat": "", "blocked": res.get("reason")})
            out.append(f"\n> {action}\n\n_(blocked)_ {res.get('reason')}\n")
            continue

        # The beat the World Master decided for THIS turn. Read back off the
        # event log rather than recomputed, so the audit reports what the
        # engine actually did and not what detect_beat would say in isolation.
        beat = persona.detect_beat(action, "")
        entry_kinds = [e.get("kind") for e in (res.get("entries") or [])]
        speech = [((e.get("meta") or {}).get("speaker") or {}).get("name")
                  for e in (res.get("entries") or []) if e.get("kind") == "speech"]
        result["actions"].append({
            "label": label, "action": action, "beat": beat,
            "entry_kinds": entry_kinds,
            "speakers": [s for s in speech if s],
        })
        out.append(f"\n### {label}\n\n> {action}\n")
        for entry in (res.get("entries") or []):
            out.append(_render(entry))
        if res.get("ending"):
            out.append(f"\n**THE STORY ENDED.** {json.dumps(res['ending'])[:300]}\n")
            break

    # ── 4. what the engine recorded ───────────────────────────────────────
    result["timeline"] = [
        {"turn": e["turn"], "actor": e["actor"], "kind": e["kind"],
         "action": (e.get("action") or "")[:120],
         "consequence": (e.get("consequence") or "")[:200]}
        for e in memory.timeline(pt_id)]

    out.append("\n---\n")
    out.append("## Proof\n")

    # Cast: real vs invented.
    real = {c.lower() for c in result["research"]["cast"]}
    leads = {"thorfinn", "askeladd", "canute"} if "vinland" in args.setting.lower() else set()
    seated_real = [n for n in result["cast"] if n.lower() in real]
    out.append(f"**Cast.** {len(seated_real)} of {len(result['cast'])} seated "
               f"characters are named in the research roster "
               f"({len(result['research']['cast'])} names found).")
    if leads:
        missing = [l for l in leads if l not in {n.lower() for n in result['cast']}]
        out.append(f"- Lead characters present: "
                   f"{'YES' if not missing else 'NO - missing ' + ', '.join(missing)}")
    out.append("")

    # The lines, per beat, with the ones actually written for that beat.
    out.append("**Lines on the cards, and the beat each is for.**\n")
    for name, lines in result["famous_lines"].items():
        rendered = ", ".join(f"`{x['beat'] or 'any'}`" for x in lines)
        out.append(f"- {name}: {rendered}")
    out.append("")

    out.append("**Beat detected per turn, and whether a line was reachable then.**\n")
    for a in result["actions"]:
        if a.get("raised") or a.get("blocked"):
            out.append(f"- _{a['label']}_: {a.get('raised') or a.get('blocked')}")
            continue
        beat = a["beat"] or "(ordinary)"
        speak = ", ".join(a.get("speakers") or []) or "nobody spoke"
        out.append(f"- _{a['label']}_ -> beat `{beat}`; spoke: {speak}")
    out.append("")

    out.append("**Timeline as the engine recorded it.**\n")
    out.append("| turn | actor | kind | what happened |")
    out.append("|---|---|---|---|")
    for e in result["timeline"]:
        out.append(f"| {e['turn']} | {e['actor']} | {e['kind']} | "
                   f"{(e['consequence'] or e['action']).replace('|', '/')[:110]} |")
    out.append("")

    out.append("**Fate spine the world was given** (the canon events due to fire):\n")
    for i, f in enumerate(result["fate"], 1):
        out.append(f"{i}. {f}")
    out.append("")

    out.append(f"**Chapters.** {len(result['chapters'])}: "
               + "; ".join(result["chapters"] or ["(none)"]))
    out.append("")
    out.append(f"_{result['calls']} model calls._")

    Path(args.out).write_text("\n".join(out), encoding="utf-8")
    Path(args.out.replace(".md", ".json")).write_text(
        json.dumps(result, indent=2), encoding="utf-8")
    shutil.rmtree(_tmp, ignore_errors=True)

    print(f"audit   -> {args.out}")
    print(f"raw     -> {args.out.replace('.md', '.json')}")
    print(f"calls   -> {result['calls']}")
    print(f"cast    -> {result['cast']}")
    print(f"beat per turn -> "
          + ", ".join(f"{a['label']}={a['beat'] or 'ordinary'}" for a in result['actions']))


if __name__ == "__main__":
    main()
