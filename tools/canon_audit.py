"""Checkpointed, source-aware live play audit for any StoryLiver premise.

Examples:
  python tools/canon_audit.py --setting "Demon Slayer" --entry start
  python tools/canon_audit.py --setting "Jujutsu Kaisen" --entry "during Shibuya"
  python tools/canon_audit.py --setting "Charlie from Hazbin Hotel in Demon Slayer" --entry start

The report distinguishes source-backed claims from model output. Plausible prose,
a known name, or a line on a card is never called proof by itself.
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

DEFAULT_ACTIONS = [
    ("introduction", "I introduce myself and ask where I am and what is happening right now."),
    ("observation", "I quietly study the place, the people here, and anything unusual I can verify."),
    ("knowledge", "I ask the nearest person what has already happened and what they personally know."),
    ("goal", "I explain what I want and ask what they intend to do next."),
    ("pressure", "I challenge the most doubtful claim and ask for something concrete that proves it."),
    ("choice", "I make a careful choice that helps without assuming knowledge I have not earned."),
]


def _load_env():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _write(path: Path, result: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")


def _markdown(result: dict) -> str:
    world = result.get("world") or {}
    entry = result.get("entry") or {}
    lines = [f"# Live canon audit - {world.get('name') or result['setting']}", "",
             f"- Premise: `{result['setting']}`",
             f"- Requested entry: `{result['answers'].get('entry', 'start')}`",
             f"- Continuity: `{result['answers'].get('continuity', '') or '(unspecified)'}`",
             f"- Evidence status: **{entry.get('status', 'missing')}**",
             f"- Resolved moment: {entry.get('moment', '') or '(unresolved)'}",
             f"- World mode/source: `{world.get('mode', '')}` / `{world.get('inspired_by', '')}`",
             "", "## Source-backed contract", ""]
    opening = entry.get("opening") or {}
    lines.append(f"Opening: {opening.get('text', '(none)')}")
    for field, label in (("characters", "Local characters"), ("places", "Places"),
                         ("past", "Already happened"), ("facts", "Established facts"),
                         ("future", "Future source events")):
        rows = entry.get(field) or []
        lines.append(f"\n### {label}")
        if not rows:
            lines.append("- (none verified)")
        for row in rows:
            name = (row.get("name") + ": ") if row.get("name") else ""
            lines.append(f"- {name}{row.get('text', '')} `[{row.get('source_id', '')}]`")
    if entry.get("uncertainties"):
        lines += ["", "### Explicit uncertainties"] + [f"- {x}" for x in entry["uncertainties"]]

    lines += ["", "## World-state checks", "",
              f"- Seated cast: {', '.join(result.get('cast') or [])}",
              f"- Locations: {', '.join(result.get('locations') or [])}",
              f"- Evidence-backed fated events: {result.get('sourced_fate', 0)} / {result.get('fate_count', 0)}",
              f"- Persona cards carrying source proof: {result.get('proved_personas', 0)} / {len(result.get('cast') or [])}",
              "", "## Played scenes", ""]
    for turn in result.get("turns") or []:
        lines += [f"### {turn['label']}", "", f"> {turn['action']}", ""]
        if turn.get("error"):
            lines.append(f"**Error:** `{turn['error']}`")
            continue
        for entry_row in turn.get("entries") or []:
            kind = entry_row.get("kind", "")
            actor = entry_row.get("actor", "")
            text = entry_row.get("text", "")
            lines.append(f"- **{kind}{' / ' + actor if actor else ''}:** {text}")
    lines += ["", "## Findings", ""]
    for finding in result.get("findings") or []:
        lines.append(f"- **{finding['status'].upper()}** {finding['check']}: {finding['detail']}")
    lines += ["", f"Model calls observed: {result.get('calls', 0)}"]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setting", required=True)
    parser.add_argument("--entry", default="start")
    parser.add_argument("--continuity", default="")
    parser.add_argument("--era", default="")
    parser.add_argument("--name", default="Canon Auditor")
    parser.add_argument("--role", default="an outsider with no unearned local knowledge")
    parser.add_argument("--turns", type=int, default=len(DEFAULT_ACTIONS))
    parser.add_argument("--actions", default="", help="UTF-8 file containing label<TAB>action")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    _load_env()
    os.environ["STORYLIVER_RESEARCH"] = "on"
    temp = tempfile.mkdtemp(prefix="storyliver-audit-")
    os.environ["STORYLIVER_DATA_DIR"] = temp

    from backend import db, engine, llm, research, worldforge  # noqa: E402

    actions = DEFAULT_ACTIONS
    if args.actions:
        actions = []
        for line in Path(args.actions).read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            label, sep, action = line.partition("\t")
            if sep and action.strip():
                actions.append((label.strip(), action.strip()))
    actions = actions[:max(0, args.turns)]
    answers = {"entry": args.entry, "continuity": args.continuity, "era": args.era,
               "name": args.name, "role": args.role, "known": "stranger",
               "cast_mix": "canon_only"}
    answers = {k: v for k, v in answers.items() if v}
    json_path = Path(args.out).with_suffix(".json")
    md_path = Path(args.out).with_suffix(".md")
    result = {"setting": args.setting, "answers": answers, "phase": "starting",
              "calls": 0, "turns": [], "findings": []}
    _write(json_path, result)

    db.init()
    real_complete = llm.complete

    def spy(role, system, user, **kwargs):
        result["calls"] += 1
        _write(json_path, result)
        return real_complete(role, system, user, **kwargs)

    llm.complete = spy
    try:
        result["phase"] = "research"
        _write(json_path, result)
        dossier = research.premise_dossier(args.setting, depth="full",
                                             user_id="canon-audit-user")
        result["research"] = {k: dossier.get(k) for k in
                              ("found", "canonical_name", "wiki", "coverage", "note")}
        result["phase"] = "building"
        _write(json_path, result)
        world = worldforge.bootstrap(args.setting, user_id="canon-audit-user",
                                     mode="canon", answers=answers)
        entry = world.get("canon_entry") or {}
        result["entry"] = entry
        result["world"] = {k: world.get(k) for k in
                           ("name", "mode", "inspired_by", "entry_point", "start_location")}
        result["cast"] = [n.get("name", "") for n in world.get("npcs") or []]
        result["locations"] = [p.get("name", "") for p in world.get("locations") or []]
        result["fate_count"] = len(world.get("fated_events") or [])
        result["sourced_fate"] = sum(bool(f.get("source_id")) for f in world.get("fated_events") or [])
        result["proved_personas"] = sum(bool(n.get("persona_evidence")) for n in world.get("npcs") or [])
        result["phase"] = "playing"
        _write(json_path, result)

        local = {x.get("name", "").casefold() for x in entry.get("characters") or [] if x.get("local")}
        seated = {x.casefold() for x in result["cast"]}
        missing = sorted(local - seated)
        result["findings"].append({"check": "entry contract present",
            "status": "pass" if entry.get("status") == "supported" else "warn",
            "detail": f"status={entry.get('status', 'missing')}; {len(entry.get('sources') or [])} cited revisions"})
        result["findings"].append({"check": "local cast seated",
            "status": "pass" if not missing else "fail",
            "detail": "all verified local people seated" if not missing else "missing: " + ", ".join(missing)})
        result["findings"].append({"check": "fate provenance",
            "status": "pass" if result["fate_count"] == result["sourced_fate"] else "fail",
            "detail": f"{result['sourced_fate']} of {result['fate_count']} events carry source evidence"})

        pt_id = engine.create_playthrough("canon-audit-user", world_id="canon-audit",
                                          world_json=json.dumps(world), protagonist=args.name)
        for index, (label, action) in enumerate(actions, 1):
            record = {"n": index, "label": label, "action": action}
            try:
                response = engine.take_turn(pt_id, action)
                record["blocked"] = bool(response.get("blocked"))
                record["entries"] = [{"kind": e.get("kind", ""), "actor": e.get("actor", ""),
                                      "text": (e.get("text") or "").strip()}
                                     for e in response.get("entries") or []]
            except Exception as exc:  # audit must checkpoint the failure
                record["error"] = f"{type(exc).__name__}: {exc}"
            result["turns"].append(record)
            _write(json_path, result)
        result["phase"] = "complete"
    finally:
        llm.complete = real_complete
        _write(json_path, result)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(_markdown(result), encoding="utf-8")
        shutil.rmtree(temp, ignore_errors=True)

    print(f"audit -> {md_path}")
    print(f"raw   -> {json_path}")
    print(f"phase -> {result['phase']}")
    print(f"calls -> {result['calls']}")


if __name__ == "__main__":
    main()
