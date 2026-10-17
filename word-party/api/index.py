"""HTTP API for Word Party.

Works in two places with the same code:
  - Render (or any always-on server): rooms in memory + WebSocket push.
  - Vercel (serverless): rooms in Upstash Redis + browsers poll every second.
Every route is thin: read the body, call a function in _lib/game.py, return JSON.
"""
import os
import sys
from typing import List, Optional

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from _lib import game
from _lib.store import get_store
from _lib.words import TOPICS

app = FastAPI(title="Word Party")
store = get_store()

# Serverless can't keep a game loop alive, so WebSockets are only on for always-on servers.
REALTIME = not os.getenv("VERCEL")


@app.exception_handler(game.GameError)
def game_error(_: Request, exc: game.GameError):
    return JSONResponse({"error": exc.message}, status_code=exc.status)


class Profile(BaseModel):
    name: str
    avatar: str


class CreateBody(Profile):
    topics: List[str] = []


class Auth(BaseModel):
    pid: str
    secret: str


class TopicsBody(Auth):
    topics: List[str]


class AnswerBody(Auth):
    answer: str


class ChatBody(Auth):
    text: str


@app.get("/api/topics")
def topics():
    return {
        "topics": [{"id": k, "name": t["name"], "vi": t["vi"], "icon": t["icon"], "count": len(t["words"])}
                   for k, t in TOPICS.items()],
        "avatars": game.AVATARS,
        "levels": game.LEVELS,
        "penalty": game.WRONG_PENALTY,
        "realtime": REALTIME,
    }


@app.get("/api/health")
def health():
    return {"ok": True, "realtime": REALTIME}


@app.post("/api/rooms")
def create_room(body: CreateBody):
    game.clean_name(body.name)
    code = game.create_room(store, public=False, topics=body.topics)
    return game.join_room(store, code, body.name, body.avatar)


@app.post("/api/quickmatch")
def quick_match(body: Profile):
    return game.quick_match(store, body.name, body.avatar)


@app.post("/api/rooms/{code}/join")
def join(code: str, body: Profile):
    return game.join_room(store, code.upper(), body.name, body.avatar)


@app.get("/api/rooms/{code}/state")
def state(code: str, pid: str, secret: str, after: int = 0, ping: int = 0):
    return game.get_state(store, code.upper(), pid, secret, after, bool(ping))


@app.post("/api/rooms/{code}/topics")
def topics_change(code: str, body: TopicsBody):
    game.set_topics(store, code.upper(), body.pid, body.secret, body.topics)
    return {"ok": True}


@app.post("/api/rooms/{code}/start")
def start(code: str, body: Auth):
    game.start(store, code.upper(), body.pid, body.secret)
    return {"ok": True}


@app.post("/api/rooms/{code}/answer")
def answer(code: str, body: AnswerBody):
    return game.submit_answer(store, code.upper(), body.pid, body.secret, body.answer)


@app.post("/api/rooms/{code}/chat")
def chat(code: str, body: ChatBody):
    game.send_chat(store, code.upper(), body.pid, body.secret, body.text)
    return {"ok": True}


@app.post("/api/rooms/{code}/again")
def again(code: str, body: Auth):
    game.play_again(store, code.upper(), body.pid, body.secret)
    return {"ok": True}


@app.post("/api/rooms/{code}/leave")
def leave(code: str, body: Auth):
    game.leave_room(store, code.upper(), body.pid, body.secret)
    return {"ok": True}


if REALTIME:
    from _lib.realtime import Hub
    hub = Hub(store)

    @app.websocket("/api/rooms/{code}/ws")
    async def room_socket(ws: WebSocket, code: str, pid: str = "", secret: str = ""):
        await hub.handle(ws, code, pid, secret)


# Render and local dev: this server also serves the web page. On Vercel the CDN does it.
PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "..", "public")
if os.path.isdir(PUBLIC_DIR) and not os.getenv("VERCEL"):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=PUBLIC_DIR, html=True), name="public")
