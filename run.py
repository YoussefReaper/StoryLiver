#!/usr/bin/env python
"""Start StoryLiver. Reads .env, then serves the API and the player on one port.

    python run.py            # http://127.0.0.1:8000
    python run.py --port 9000 --reload
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env():
    """Minimal .env reader so there is no extra dependency to install."""
    path = ROOT / ".env"
    if not path.exists():
        return False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
    return True


def main():
    ap = argparse.ArgumentParser(description="Run StoryLiver")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true", help="restart on code changes")
    args = ap.parse_args()

    had_env = load_env()
    sys.path.insert(0, str(ROOT))

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print("Missing dependencies. Run:\n\n    pip install -r requirements.txt\n")
        return 1

    from backend import config

    print("\n  StoryLiver")
    print("  " + "-" * 52)
    if config.live_llm():
        print(f"  models   world master  {config.MODELS['world_master']}")
        print(f"           narrator      {config.MODELS['narrator']}"
              f"  (deep: {config.MODELS['narrator_premium']})")
        print(f"           npc + director {config.MODELS['npc']}")
        print(f"  endpoint {config.BASE_URL}")
    else:
        print("  NO API KEY — running the offline narrator.")
        print("  Everything works; the prose is a deterministic stub.")
        if not had_env:
            print("  To go live:  cp .env.example .env   then add OPENAI_API_KEY")
        else:
            print("  To go live:  add OPENAI_API_KEY to .env and restart")
    print(f"  data     {config.DB_PATH}")
    print("  " + "-" * 52)
    print(f"  Open     http://{args.host}:{args.port}\n")

    import uvicorn
    uvicorn.run("backend.main:app", host=args.host, port=args.port,
                reload=args.reload, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
