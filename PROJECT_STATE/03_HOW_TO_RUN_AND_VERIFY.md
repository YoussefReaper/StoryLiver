# 03 — How to Run & Verify

## Python environment (read `04_ENVIRONMENT_GOTCHAS.md` first)

The repo runs on the **managed** Python venv, not a system Python:

```
VENV_PY = C:\Users\3maar\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe
```

The venv **drifted behind `requirements.txt`** — it was missing declared deps
(`python-multipart`, `argon2-cffi`, `pymongo`). If the app won't import or auth/
durable suites fail, reinstall from the repo root (the only requirements file; there
is **no** `backend/requirements-dev.txt`):

```bash
"C:\Users\3maar\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe" -m pip install -r D:/StoryLiver/requirements.txt
```

`python-multipart` is load-bearing: without it the app raises
`RuntimeError: Form data requires "python-multipart"` at import.

## Run it (no key required)

```bash
cd D:/StoryLiver
"C:\Users\3maar\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe" run.py --port 8123
```

Opens **http://127.0.0.1:8123**. No build step, no Node, no DB to provision.
**Without an API key it falls back to a deterministic offline narrator** — you can
exercise the whole product (rooms, Forge, economy, export, upload) before spending
a cent. A valid key is only needed for live model prose (see blocker in `02`).

Quick health check:
```
GET http://127.0.0.1:8123/api/health   # live vs offline-stub, model routing, realtime backend, voice
```

## Prove it works (offline suites)

```bash
PY="C:\Users\3maar\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"
cd D:/StoryLiver
$PY -m tests.test_anticollapse
$PY -m tests.test_multiplayer
$PY -m tests.test_layers
$PY -m tests.test_full
$PY -m tests.test_journey
$PY -m tests.test_workstreams
$PY -m tests.test_research
$PY -m tests.test_durable
$PY -m tests.test_ui_contract
```

Mock mode is hermetic when deps are present and no network env (`MONGODB_URI`,
`RESEARCH` live) is set. Target: 14 suites green. As of 2026-09-13, 211 pass / 5
fail where the 5 are env-gated (network + auth), not product bugs — see `02`.

## The upload regression guard (CDP, zero-dep)

Drives the **real** app in headless Chrome, intercepts the file chooser, and asserts
the picker survives a slow upload:

```bash
NODE="C:/Users/3maar/.workbuddy-ai/binaries/node/versions/22.22.2-2/node.exe"
# server must be running on :8123 first
$NODE tools/check_upload_race.mjs --url http://127.0.0.1:8123/
```

Asserts 4 cases (fast link → `/media/…`; slow link +1500 ms latency → `/media/…`;
dismissed picker → `null`; dismissed picker w/o `cancel` event → `null`). This is
the regression test for the fix in `02`. Run it after any change to `askForArt()` /
`uploadImage()` in `frontend/assets/app.js`.

## Deploy

`render.yaml`, `fly.toml`, `Dockerfile` are included (backend). Cloudflare Pages
for the player. `deploy/README.md` covers Render/Fly/Upstash/Cloudflare. The single
container serves both. Free-host no-disk survival: set `MONGODB_URI` (free Atlas M0)
so `durable.py` snapshots SQLite → Mongo and restores on boot.

## Other endpoints worth knowing

`GET /api/ethics` · `GET /api/economy` · `GET /api/streak` · `GET /api/payments/status`
· `GET /api/docs` (interactive Swagger). `WS /ws/session/{id}` for the room socket.
