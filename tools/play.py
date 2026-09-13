"""Play a story through, for real, and write down what happened.

    python tools/play.py                      # the default setting, spool mode
    python tools/play.py --turns 8 --out out.md
    python tools/play.py --premise "..." --actions actions.txt

The engine is driven through its real turn pipeline - the same
`engine.take_turn` the HTTP API calls - so everything that gates a live game
(movement, cast presence, fate, relationships, the mode dials) is in the path.
The only thing this swaps is the model: in spool mode every completion comes
from `spool/responses.json`, which is how a transcript gets written with real
prose and no key (see llm.py).

Each run writes:
  * the transcript, in order, as the player would read it
  * prompts/<n>-<role>.txt - the exact prompt behind every call
  * spool/misses.json      - whatever the spool could not answer yet
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

DEFAULT_PREMISE = "I and my girlfriend charlie (from hazbin hotel) in Demon Slayer verse"

# The opening of a session. Written as a player would type it: nothing here
# knows the engine's nouns, and every one of these is a thing a person does on
# arriving somewhere with somebody they brought with them.
DEFAULT_ACTIONS = [
    "I take Charlie's hand and look around at where we have ended up.",
    "I ask Charlie if she is alright, quietly, so nobody else hears.",
    "I ask Charlie what she makes of this place.",
    "I look for whoever is nearest and introduce myself and Charlie.",
    "I ask the nearest person where we are and what year it is.",
    "I tell them the truth about what Charlie is.",
    "I walk to the Butterfly Mansion.",
    "I ask the people there for help and shelter for the night.",
    "I ask Charlie how she is holding up after all of that.",
    "I stay close to Charlie and wait to see what the night brings.",
]


def _render(entry):
    kind = entry.get("kind")
    text = (entry.get("text") or "").strip()
    if kind == "you":
        return f"\n> {text}\n"
    if kind == "speech":
        name = (entry.get("meta") or {}).get("speaker", {}).get("name") or entry.get("actor")
        return f"\n**{name}:** {text}\n"
    if kind == "safety":
        return f"\n_(the table draws a line)_ {text}\n"
    if kind == "refusal":
        return f"\n_(the world pushes back)_ {text}\n"
    return f"\n{text}\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--premise", default=DEFAULT_PREMISE)
    ap.add_argument("--turns", type=int, default=len(DEFAULT_ACTIONS))
    ap.add_argument("--actions", default="", help="a file, one action per line")
    ap.add_argument("--out", default="playtest.md")
    ap.add_argument("--spool", default=str(ROOT / "spool"))
    ap.add_argument("--mode", default="spool", choices=("spool", "mock"))
    ap.add_argument("--stub-roles", default="npc,director",
                    help="roles left on the free offline stub in spool mode")
    ap.add_argument("--session-type", default="")
    args = ap.parse_args()

    os.environ["STORYLIVER_LLM_MODE"] = args.mode
    os.environ["STORYLIVER_RESEARCH"] = "off"
    os.environ["STORYLIVER_SPOOL_DIR"] = args.spool
    os.environ["STORYLIVER_SPOOL_STUB_ROLES"] = args.stub_roles
    os.environ["STORYLIVER_SPOOL_BY_ACTION"] = "1"
    _tmp = tempfile.mkdtemp(prefix="storyliver-play-")
    os.environ["STORYLIVER_DATA_DIR"] = _tmp

    from backend import db, engine, llm, memory, worldforge  # noqa: E402

    actions = DEFAULT_ACTIONS
    if args.actions:
        actions = [ln.strip() for ln in Path(args.actions).read_text(
            encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    actions = actions[:args.turns]

    # Every prompt, in order, exactly as the model would have received it.
    prompts_dir = ROOT / "prompts"
    shutil.rmtree(prompts_dir, ignore_errors=True)
    prompts_dir.mkdir(parents=True, exist_ok=True)
    seen = {"n": 0}
    real_complete = llm.complete

    def spy(role, system, user, **kw):
        seen["n"] += 1
        (prompts_dir / f"{seen['n']:03d}-{role}.txt").write_text(
            f"role: {role}\nmodel: {kw.get('model')}\njson_mode: {kw.get('json_mode')}\n"
            f"{'=' * 72}\nSYSTEM\n{'=' * 72}\n{system}\n"
            f"{'=' * 72}\nUSER\n{'=' * 72}\n{user}\n", encoding="utf-8")
        return real_complete(role, system, user, **kw)

    llm.complete = spy

    db.init()
    world = worldforge.bootstrap(args.premise, user_id="playtest-user")
    if hasattr(world, "data"):
        world = world.data

    pt_id = engine.create_playthrough(
        "playtest-user", world_id="playtest", world_json=json.dumps(world),
        protagonist="You", session_type=args.session_type)

    out = [f"# Playtest - {world.get('name')}",
           f"\nPremise: `{args.premise}`\n",
           f"Source: **{world.get('inspired_by')}**  |  mode: {world.get('mode')}",
           f"\nCast: {', '.join(n['name'] for n in world.get('npcs', []))}",
           f"\nFriction: {world.get('friction') or '(none)'}\n", "---"]

    for i, action in enumerate(actions, 1):
        try:
            res = engine.take_turn(pt_id, action)
        except Exception as e:                     # noqa: BLE001 - report, do not hide
            out.append(f"\n> {action}\n\n**(engine raised)** `{type(e).__name__}: {e}`\n")
            continue
        if res.get("blocked"):
            out.append(f"\n> {action}\n\n_(blocked)_ {res.get('reason')}\n")
            continue
        out.append(f"\n> {action}\n")
        for entry in res.get("entries", []):
            out.append(_render(entry))
        if res.get("ending"):
            out.append(f"\n---\n\n**THE STORY ENDED.** {json.dumps(res['ending'])[:400]}\n")
            break

    misses = llm.spool_misses()
    miss_file = llm.spool_write_misses()
    out.append(f"\n---\n\n_{len(actions)} actions played. "
               f"{seen['n']} model calls, {len(misses)} unanswered._\n")

    Path(args.out).write_text("\n".join(out), encoding="utf-8")
    shutil.rmtree(_tmp, ignore_errors=True)

    print(f"transcript -> {args.out}")
    print(f"prompts    -> {prompts_dir} ({seen['n']} calls)")
    print(f"misses     -> {miss_file} ({len(misses)})")
    for m in misses[:40]:
        print("   MISS", m)


if __name__ == "__main__":
    main()
