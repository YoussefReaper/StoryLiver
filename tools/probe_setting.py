"""Probe: what does the builder actually make of the requested premise?

Run:  python tools/probe_setting.py

Offline (mock LLM, research off) so it is free and hermetic. Prints the host
setting it detected, the carried-in characters, the seated cast, the geography,
the frontier, the laws, and the chapter spine - i.e. everything the narrator
will be handed on turn one.
"""
import json
import os
import sys
import tempfile

os.environ["STORYLIVER_LLM_MODE"] = "mock"
os.environ["STORYLIVER_RESEARCH"] = "off"
_TMP = tempfile.mkdtemp(prefix="storyliver-probe-")
os.environ["STORYLIVER_DATA_DIR"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import db, worldforge  # noqa: E402

PREMISE = ("I and my girlfriend charlie (from hazbin hotel) in Demon Slayer verse")

db.init()
world = worldforge.bootstrap(PREMISE, user_id="probe-user", tone="", mode="auto")
if hasattr(world, "data"):
    world = world.data

print("=" * 72)
print("PREMISE :", PREMISE)
print("NAME    :", world.get("name"))
print("TAGLINE :", world.get("tagline"))
print("MODE    :", world.get("mode"))
print("SOURCE  :", world.get("inspired_by"))
print("PROMPT  :", world.get("source_prompt"))
print("FRICTION:", (world.get("friction") or "")[:400])
print("=" * 72)

print("\nCAST (%d):" % len(world.get("npcs", [])))
for n in world.get("npcs", []):
    a = n.get("anchors") or {}
    print(f"  - {n.get('name')!r:32} id={n.get('id')!r:24} origin={n.get('origin')!r}"
          f" from={n.get('from_source')!r}")
    print(f"      role       : {n.get('role')!r}")
    print(f"      start      : {n.get('start_location')!r}")
    print(f"      voice      : {a.get('voice')!r}")
    print(f"      constraints: {a.get('constraints')!r}")
    print(f"      goals      : {a.get('goals')!r}")

print("\nLOCATIONS (%d):" % len(world.get("locations", [])))
for loc in world.get("locations", []):
    print(f"  - {loc.get('id')!r:26} {loc.get('name')!r:30}"
          f" frontier={bool(loc.get('frontier'))} connects={loc.get('connects')}")

print("\nRULES (%d):" % len(world.get("rules", [])))
for r in world.get("rules", []):
    print(f"  - {r.get('id')}: {r.get('text')}")

print("\nFATED EVENTS (%d):" % len(world.get("fated_events", [])))
for f in world.get("fated_events", []):
    print(f"  - T{f.get('turn')} {f.get('title')}: {f.get('desc')}")

print("\nCHAPTERS (%d):" % len(world.get("chapters", [])))
for c in world.get("chapters", []):
    print(f"  - {json.dumps(c)[:220]}")

print("\nSTART LOCATION:", world.get("start_location"))
print("START PHASE/DAY:", world.get("phase_for", lambda t: "?")(0) if callable(world.get("phase_for")) else "?")
print("\nPROBE OK")
