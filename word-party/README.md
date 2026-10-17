# Word Party 🎉

A multiplayer English word game for kids. Players see a Vietnamese word and find the English one.

- Guest login: nickname + animal avatar, no signup
- Create room (pick topics), join by 5-letter code, share link, quick match
- 20 questions in 3 levels: Quick pick (50), Missing letters (100), Word race (150)
- Wrong try = −20 points (score never goes below 0)
- Live chat, with answer blocking and a simple bad-word filter
- Python (FastAPI) backend, plain HTML/CSS/JS frontend
- Free hosting: **Render** (recommended, WebSocket push) or Vercel + Upstash Redis (polling)

## Project map

```
api/index.py          HTTP routes (thin, just calls game.py)
api/_lib/game.py      ALL game rules: rooms, levels, scoring, chat, quick match
api/_lib/store.py     In-memory (Render, laptop) or Upstash Redis (Vercel)
api/_lib/realtime.py  WebSocket hub + game loop (Render, laptop)
api/_lib/words.py     6 topics x 40 words (english, vietnamese, emoji)
public/index.html     Page layout
public/style.css      The cute look
public/app.js         Screen drawing + polling
render.yaml           Render setup (one click deploy)
vercel.json           Vercel setup (sends /api/* to the Python function)
```

Folders starting with `_` inside `api/` are not turned into separate Vercel functions, that's why the logic lives in `api/_lib/`.

## Run on your laptop

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt uvicorn
uvicorn api.index:app --reload --port 8000
```

Open http://localhost:8000. To test 2 players, open a second **normal window** and an **incognito window** (each browser tab is one player). No Redis needed locally.

Want to test alone? Set `MIN_PLAYERS = 1` in `api/_lib/game.py`.

## Deploy to Render (free, recommended)

1. Push this folder to a GitHub repo.
2. On render.com: **New → Blueprint**, pick the repo. Render reads `render.yaml` and creates the service on the free plan.
   (Or **New → Web Service** by hand: build `pip install -r requirements.txt`, start `uvicorn api.index:app --host 0.0.0.0 --port $PORT`.)
3. Wait for the build, open the `.onrender.com` link. Done, no database needed.

Good to know about the free plan:
- After 15 minutes with no visitors the server sleeps. The next visitor waits about a minute while it wakes up. A running game keeps it awake (the browser sends a small ping every 25s).
- Rooms live in memory, so a redeploy or restart clears them. That's fine for a game: kids just make a new room.
- Keep **one** worker/instance. If you need more power later, move to the paid Starter instance (no sleeping), not more instances.

### No Git? Deploy with Docker instead

You need Docker Desktop and a free Docker Hub account (`yourname` below).

```bash
docker build --platform linux/amd64 -t yourname/word-party:v1 .
docker login
docker push yourname/word-party:v1
```

Then on Render: **New → Web Service → Existing image**, paste `docker.io/yourname/word-party:v1`, choose the Free plan, deploy.

To update the game later: build and push a new tag (`v2`), then in Render open **Settings → Image URL**, change the tag, and deploy. Image services don't update by themselves.

Test the image on your laptop first: `docker run -p 8000:10000 yourname/word-party:v1`, then open http://localhost:8000.

## Deploy to Vercel (free, backup option)

1. Push this folder to a GitHub repo.
2. On vercel.com: **Add New → Project**, import the repo. Framework preset: **Other**. No build command.
3. In the project: **Storage → Create / Connect → Upstash (Redis)**, free plan. Vercel adds the `KV_REST_API_URL` and `KV_REST_API_TOKEN` env vars for you. (If you create the database on upstash.com instead, add `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` yourself.)
4. **Redeploy** so the function sees the new env vars.

Without Redis on Vercel, rooms will randomly "disappear" because each server copy has its own memory.

## How it works (short version)

The game rules don't care where they run. `advance()` in `game.py` moves the game forward by the clock, and `build_view()` makes what each player sees.

- **Render / laptop:** a loop in `realtime.py` runs 4 times per second, calls `advance()`, and pushes a new view through the WebSocket only when that player's screen changed. Timers count down in the browser, so pushes are rare.
- **Vercel:** no background loop is possible, so every browser asks for the state once a second (polling), and rooms live in Redis.
- If a WebSocket drops (bad wifi), the browser switches to polling and reconnects by itself.

The timer is decided by the server, and answers are only sent to browsers after the question ends, so nobody can cheat from DevTools.

## Easy changes

| What | Where |
|---|---|
| Points, timers, penalty, questions per level | `LEVELS`, `WRONG_PENALTY` in `game.py` |
| Add words or a new topic | `words.py` |
| Avatars | `AVATARS` in `game.py` |
| Bad words list | `BAD_WORDS` in `game.py` |
| Colors | top of `style.css` |
