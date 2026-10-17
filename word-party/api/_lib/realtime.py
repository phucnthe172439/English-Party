"""Realtime push with WebSockets, for always-on servers like Render.

How it works:
  - each browser opens one WebSocket per room,
  - one background loop runs 4 times per second: for each room with
    players, it moves the game forward and builds a view per player,
  - a view is only sent when something on that player's screen changed
    (timers count down in the browser, so they don't count as a change).

Answers, chat and host buttons still use the normal HTTP routes. The loop
notices the change on its next tick (under 0.25s) and pushes it to everyone.

Rooms live in the memory of ONE server process. Do not run more than one
worker or instance with this, or players in the same room may not see each other.
"""
import asyncio
import json

from fastapi import WebSocket, WebSocketDisconnect

from . import game

TICK_SECONDS = 0.25


class Conn:
    def __init__(self, ws, pid):
        self.ws = ws
        self.pid = pid
        self.after = 0      # last chat id this browser has
        self.sig = None     # what we sent last time, to skip duplicates


def signature(view):
    """The view without moving numbers (timers), so we can tell if the screen really changed."""
    v = {k: val for k, val in view.items() if k != "chat"}
    for key in ("time_left", "auto_start_in"):
        if key in v:
            v[key] = True
    if "question" in v:
        v["question"] = {**v["question"], "time_left": True}
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


class Hub:
    def __init__(self, store):
        self.store = store
        self.rooms = {}     # code -> set of Conn
        self.task = None

    async def handle(self, ws: WebSocket, code: str, pid: str, secret: str):
        code = code.upper()
        await ws.accept()
        try:
            room = game.require_room(self.store, code)
            game.require_player(room, pid, secret)
        except game.GameError as e:
            await ws.send_json({"error": e.message, "status": e.status})
            await ws.close(code=4000 + e.status)
            return

        conn = Conn(ws, pid)
        self.rooms.setdefault(code, set()).add(conn)
        if not self.task:
            self.task = asyncio.create_task(self.loop())
        try:
            while True:
                await ws.receive_text()   # the browser sends "ping" so Render stays awake
        except WebSocketDisconnect:
            pass
        finally:
            conns = self.rooms.get(code, set())
            conns.discard(conn)
            if not conns:
                self.rooms.pop(code, None)

    async def loop(self):
        while True:
            await asyncio.sleep(TICK_SECONDS)
            for code, conns in list(self.rooms.items()):
                try:
                    messages = await asyncio.to_thread(self.build, code, list(conns))
                except Exception as e:     # one broken room must not stop the others
                    print(f"room {code} tick failed: {e!r}")
                    continue
                for conn, text in messages:
                    try:
                        await conn.ws.send_text(text)
                    except Exception:
                        conns.discard(conn)

    def build(self, code, conns):
        """Runs in a thread: mark players online, move the game forward, make the views."""
        room = game.load_room(self.store, code)
        if not room:
            return [(c, json.dumps({"error": "This room has closed.", "status": 404})) for c in conns]
        t = game.now()
        for pid in {c.pid for c in conns}:          # connected = online, no ping needed
            self.store.hset(game.room_key(code), f"seen:{pid}", t)
            room.raw[f"seen:{pid}"] = str(t)
        game.advance(self.store, room, t)

        out = []
        for c in conns:
            view = game.build_view(self.store, room, c.pid, t, c.after)
            sig = signature(view)
            if view["chat"] or sig != c.sig:
                c.sig = sig
                if view["chat"]:
                    c.after = view["chat"][-1]["id"]
                out.append((c, json.dumps(view, ensure_ascii=False)))
        return out
