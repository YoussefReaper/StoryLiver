# Deploying StoryLiver

Backend on Render or Fly (both have a free tier), Redis on Upstash (free tier),
the player on Cloudflare Pages. Or skip all of it and run one process — the app
works fine as a single container with SQLite.

---

## 1. Redis (Upstash, free)

Optional but recommended the moment you run more than one worker.

1. Create a database at [upstash.com](https://upstash.com) → Redis → free tier.
2. Copy the **`rediss://` connection string**.
3. Set it as `REDIS_URL`.

Without it, StoryLiver uses an in-process realtime layer: correct for one
worker, wrong for two. `GET /api/health` reports which one is live:

```json
{ "realtime": { "backend": "redis", "url_set": true, "ok": true } }
```

---

## 2. Backend — Render (free)

`render.yaml` in the repo root is a complete blueprint.

1. Push the repo to GitHub.
2. Render → **New → Blueprint** → pick the repo.
3. Set the secrets it asks for: `OPENAI_API_KEY`, `REDIS_URL`, and
   `STORYLIVER_CORS` (your Pages origin, e.g. `https://storyliver.pages.dev`).

The blueprint mounts a 1 GB disk at `/var/data` so SQLite survives restarts.
**Do not skip the disk** — Render's filesystem is otherwise ephemeral and every
story would vanish on redeploy.

### Backend — Fly.io (free allowance)

```bash
fly launch --no-deploy
```

```bash
fly volumes create storyliver_data --size 1
```

```bash
fly secrets set OPENAI_API_KEY=sk-... REDIS_URL=rediss://...
```

```bash
fly deploy
```

`fly.toml` scales to zero when nobody is playing and wakes on the next request.
A cold start costs a few seconds on the first turn.

### Backend — any container host

`Dockerfile` and `Procfile` are both here. Mount a volume at
`STORYLIVER_DATA_DIR` and you are done.

---

## 3. Frontend — Cloudflare Pages

The player is static files with no build step.

1. Cloudflare → Workers & Pages → **Create → Pages → Connect to Git**.
2. Build command: *(leave empty)*. Output directory: **`frontend`**.
3. Deploy.

Then point the player at your API. Either:

- **Same origin (simplest):** skip Pages entirely — the backend already serves
  `frontend/` at `/`. One deploy, no CORS.
- **Split origins:** add a `_redirects` file so Pages proxies the API:

```
/api/*  https://storyliver.onrender.com/api/:splat  200
/ws/*   https://storyliver.onrender.com/ws/:splat   200
```

WebSockets do not survive a Pages redirect on every plan. If rooms misbehave on
a split deploy, serve everything from the backend origin — that path is tested
and needs no CORS at all.

Set `STORYLIVER_CORS` to your Pages origin when the origins differ.

---

## 4. Environment

| Variable | Required | What it does |
|---|---|---|
| `OPENAI_API_KEY` | yes | The only thing that must be set to go live |
| `OPENAI_BASE_URL` | no | Any OpenAI-compatible endpoint (OpenRouter, Groq, vLLM) |
| `REDIS_URL` | no* | Upstash/Redis. *Required with more than one worker |
| `STORYLIVER_DATA_DIR` | no | SQLite location. Point it at a mounted volume |
| `STORYLIVER_CORS` | no | Comma-separated origins. Defaults to `*` |
| `STORYLIVER_MODEL_*` | no | Per-role model overrides |
| `STORYLIVER_DAILY_COST_CAP_USD` | no | Per-user daily spend rail (default `0.75`) |
| `STORYLIVER_TTS_MODEL` | no | Enables the paid studio voice. Local voice is free and always on |

---

## 5. After deploying

```bash
curl https://your-app/api/health
```

Check three things in the response: `llm` says `live` (not `offline-stub`),
`realtime.backend` says `redis` if you set one, and `starters` lists
`emberfall`.

Then open the site, press **Host a room**, and send the six-character code to
someone. If they are playing inside a minute, the deploy is good.

---

## 6. Scaling notes

- **One worker is fine to start.** Beyond that, set `REDIS_URL` first — the
  turn lock and the pub/sub fan-out both live there, and two workers without it
  will let two players take the same turn.
- **SQLite holds up further than you would think** for turn-based writes, but
  it is the first thing to move if you outgrow one box. Every query goes through
  `backend/db.py`.
- **The daily USD rail is per user, not global.** Add a global cap before you
  advertise anywhere large.
- **Payments are stubbed.** `POST /api/playthroughs/{id}/purchase` grants Mana
  with no charge. Wire a processor there before taking money.
