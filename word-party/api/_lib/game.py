"""Game rules for Word Party.

Serverless has no game loop running in the background. So every request:
  1. loads the room from the store,
  2. moves the game forward based on the clock (advance),
  3. saves it and returns what this player should see.

Data for one room lives in ONE Redis hash "room:{CODE}":
  meta               -> JSON with status, questions, host... (see create_room)
  p:{pid}            -> JSON player profile {name, avatar, secret, joined}
  seen:{pid}         -> last time the player pinged (for online / offline)
  s:{round}:{pid}    -> score of the player in that round
  ok:{round}:{q}:{pid} -> set when the player solved question q
  chat_seq           -> last chat message id
Chat messages live in a list "chat:{CODE}".
"""
import copy
import json
import random
import re
import secrets
import time

from .words import TOPICS

TOTAL_QUESTIONS = 20
MIN_PLAYERS = 2          # change to 1 if you want to test alone
MAX_PLAYERS = 8
WRONG_PENALTY = 20
HINT_PENALTY = 20        # level 2: each letter the game shows takes 20 points off the prize
MIN_HINT_POINTS = 20     # level 2: a correct answer always gives at least this much
ACTIVE_SECONDS = 15      # no ping for 15s = offline
COUNTDOWN_SECONDS = 4
REVEAL_SECONDS = 4
QUICK_MATCH_WAIT = 15    # quick match rooms start by themselves after 15s
PUBLIC_RESET_SECONDS = 30
ROOM_TTL = 3 * 60 * 60   # rooms disappear 3 hours after the last change

LEVELS = {
    1: {"kind": "pick", "name": "Quick pick", "points": 50, "time": 10, "count": 7},
    2: {"kind": "letters", "name": "Missing letters", "points": 100, "time": 20, "count": 7, "hint_every": 4},
    3: {"kind": "race", "name": "Word race", "points": 150, "time": 20, "count": 6},
}

AVATARS = ["🐶", "🐱", "🐼", "🦊", "🐸", "🐵", "🐰", "🐯", "🦁", "🐨", "🐷", "🐙"]
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O or 1/I, easy to read for kids

BAD_WORDS = [
    "fuck", "shit", "bitch", "dick", "ass", "asshole", "damn", "idiot", "stupid", "bastard",
    "đm", "dm", "dmm", "vcl", "vl", "đéo", "địt", "lồn", "cặc", "ngu", "óc chó",
]
BAD_RE = re.compile(r"\b(" + "|".join(map(re.escape, BAD_WORDS)) + r")\b", re.IGNORECASE)


class GameError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------- small helpers ----------

def now():
    return time.time()


def norm(text):
    return " ".join(str(text).lower().split())


def squash(text):
    """'Police-Officer ' -> 'policeofficer'. Used to compare answers kindly."""
    return re.sub(r"[^a-z]", "", str(text).lower())


def censor(text):
    return BAD_RE.sub("***", text)


def room_key(code):
    return f"room:{code}"


def chat_key(code):
    return f"chat:{code}"


def clean_topics(topics):
    result = []
    for t in topics or []:
        if t in TOPICS and t not in result:
            result.append(t)
    return result


def clean_name(name):
    name = re.sub(r"[<>]", "", " ".join(str(name).split()))
    if not 2 <= len(name) <= 12:
        raise GameError("Your nickname needs 2 to 12 letters.")
    if BAD_RE.search(name):
        raise GameError("Pick a kinder nickname.")
    return name


# ---------- room object ----------

class Room:
    def __init__(self, code, raw):
        self.code = code
        self.raw = raw
        self.meta = json.loads(raw["meta"])
        self.players = {k[2:]: json.loads(v) for k, v in raw.items() if k.startswith("p:")}

    def is_active(self, pid, t):
        return t - float(self.raw.get(f"seen:{pid}", 0)) < ACTIVE_SECONDS

    def active_ids(self, t):
        ids = [pid for pid in self.players if self.is_active(pid, t)]
        return sorted(ids, key=lambda pid: self.players[pid]["joined"])

    def score(self, round_no, pid):
        return int(self.raw.get(f"s:{round_no}:{pid}", 0))

    def solved(self, round_no, q_index, pid):
        return f"ok:{round_no}:{q_index}:{pid}" in self.raw


def load_room(store, code):
    raw = store.hgetall(room_key(code))
    if not raw or "meta" not in raw:
        return None
    return Room(code, raw)


def require_room(store, code):
    room = load_room(store, str(code).upper())
    if not room:
        raise GameError("Room not found. Check the code and try again.", 404)
    return room


def require_player(room, pid, secret):
    player = room.players.get(pid or "")
    if not player or player["secret"] != secret:
        raise GameError("Join the room first.", 403)
    return player


# ---------- saving meta safely ----------

def save_meta(store, room, meta, wait=True):
    """Save meta only if nobody changed it since we loaded it (optimistic lock).

    A short Redis lock makes "check version + write" safe when two players
    click at the same moment.
    """
    lock = f"lock:{room.code}"
    for _ in range(10 if wait else 1):
        if store.set(lock, "1", ex=3, nx=True):
            break
        if wait:
            time.sleep(0.1)
    else:
        return False
    try:
        current = json.loads(store.hget(room_key(room.code), "meta") or "{}")
        if current.get("v") != room.meta.get("v"):
            return False
        meta["v"] = current["v"] + 1
        store.hset(room_key(room.code), "meta", json.dumps(meta))
        store.expire(room_key(room.code), ROOM_TTL)
        room.meta = meta
        return True
    finally:
        store.delete(lock)


def mutate_meta(store, code, pid, secret, change):
    """Load room, let `change(room, meta)` edit meta, save. Retries if someone else saved first."""
    for _ in range(3):
        room = require_room(store, code)
        require_player(room, pid, secret)
        advance(store, room, now())
        meta = copy.deepcopy(room.meta)
        change(room, meta)
        if save_meta(store, room, meta):
            return
    raise GameError("The room is busy. Try again.")


# ---------- questions ----------

def make_options(word, pool):
    same_topic = [w["en"] for w in pool if w["topic"] == word["topic"] and w["en"] != word["en"]]
    others = same_topic if len(same_topic) >= 3 else [w["en"] for w in pool if w["en"] != word["en"]]
    options = random.sample(others, 3) + [word["en"]]
    random.shuffle(options)
    return options


def pick_hidden(word):
    """Hide about half of the letters. The list order is also the hint order."""
    positions = [i for i, ch in enumerate(word) if ch.isalpha()]
    return random.sample(positions, max(1, len(positions) // 2))


def build_questions(topic_ids):
    pool, seen = [], set()
    for tid in topic_ids:
        topic = TOPICS[tid]
        for en, vi, emoji in topic["words"]:
            if en not in seen:
                seen.add(en)
                pool.append({"en": en, "vi": vi, "emoji": emoji or topic["icon"], "topic": tid})

    picked = random.sample(pool, TOTAL_QUESTIONS)
    # Longest words go to level 1 (picking is easy), shortest go to level 3 (typing is hard).
    picked.sort(key=lambda w: len(w["en"]), reverse=True)

    questions, start = [], 0
    for level, info in LEVELS.items():
        group = picked[start:start + info["count"]]
        start += info["count"]
        random.shuffle(group)
        for w in group:
            q = {"level": level, **w}
            if info["kind"] == "pick":
                q["options"] = make_options(w, pool)
            if info["kind"] == "letters":
                q["hidden"] = pick_hidden(w["en"])
            questions.append(q)
    return questions


def hints_shown(q, elapsed):
    """How many hidden letters the game has shown so far (always keeps 1 blank)."""
    return min(int(elapsed // LEVELS[2]["hint_every"]), len(q["hidden"]) - 1)


def points_for(q, elapsed):
    """Prize for a correct answer right now. Level 2 loses points for every hint shown."""
    points = LEVELS[q["level"]]["points"]
    if LEVELS[q["level"]]["kind"] == "letters":
        points = max(MIN_HINT_POINTS, points - HINT_PENALTY * hints_shown(q, elapsed))
    return points


def letters_view(code, round_no, q_index, q, elapsed):
    """Pattern with blanks + letter tiles. One more letter is shown every few seconds."""
    word, hidden = q["en"], q["hidden"]
    shown = hints_shown(q, elapsed)
    blanks = set(hidden[shown:])
    pattern = [None if i in blanks else ch for i, ch in enumerate(word)]

    rng = random.Random(f"{code}-{round_no}-{q_index}")   # same tiles order on every poll
    tiles = [word[i] for i in hidden] + rng.sample("abcdefghijklmnopqrstuvwxyz", 2)
    rng.shuffle(tiles)
    for i in hidden[:shown]:
        tiles.remove(word[i])
    return {"pattern": pattern, "tiles": tiles, "hints": shown}


# ---------- the game clock ----------

def start_game(room, m, t):
    m.update(
        round=m["round"] + 1, questions=build_questions(m["topics"]), in_game=room.active_ids(t),
        status="countdown", q=0, phase_at=t, auto_start_at=None,
    )


def reset_to_lobby(m):
    m.update(status="lobby", q=0, phase_at=0, auto_start_at=None, in_game=[], questions=[])


def step(room, m, t):
    """Do ONE change if it is time for it. Returns True if something changed."""
    active = room.active_ids(t)
    if active and m["host"] not in active:
        m["host"] = active[0]          # host left: the next player becomes host
        return True

    status, phase_at = m["status"], m["phase_at"]

    if status == "lobby":
        if not m["public"]:
            return False
        enough = len(active) >= MIN_PLAYERS
        if enough and not m["auto_start_at"]:
            m["auto_start_at"] = t + QUICK_MATCH_WAIT
            return True
        if not enough and m["auto_start_at"]:
            m["auto_start_at"] = None
            return True
        if m["auto_start_at"] and t >= m["auto_start_at"]:
            start_game(room, m, t)
            return True
        return False

    if status == "countdown":
        end = phase_at + COUNTDOWN_SECONDS
        if t < end:
            return False
        m.update(status="question", q=0, phase_at=end)
        return True

    if status == "question":
        playing = [p for p in m["in_game"] if p in active]
        if not playing:
            m.update(status="finished", phase_at=t)
            return True
        end = phase_at + LEVELS[m["questions"][m["q"]]["level"]]["time"]
        all_done = all(room.solved(m["round"], m["q"], p) for p in playing)
        if t >= end or all_done:
            m.update(status="reveal", phase_at=min(t, end))
            return True
        return False

    if status == "reveal":
        end = phase_at + REVEAL_SECONDS
        if t < end:
            return False
        if m["q"] + 1 >= len(m["questions"]):
            m.update(status="finished", phase_at=end)
        else:
            m.update(status="question", q=m["q"] + 1, phase_at=end)
        return True

    if status == "finished" and m["public"] and t >= phase_at + PUBLIC_RESET_SECONDS:
        reset_to_lobby(m)
        return True
    return False


def advance(store, room, t):
    meta = copy.deepcopy(room.meta)
    changed = False
    for _ in range(60):                # catch up many steps if nobody asked for a while
        if not step(room, meta, t):
            break
        changed = True
    if changed and not save_meta(store, room, meta, wait=False):
        room.meta = meta               # someone else is saving; still show the fresh state


# ---------- chat ----------

def push_chat(store, code, msg):
    msg["id"] = store.hincrby(room_key(code), "chat_seq", 1)
    msg["t"] = now()
    store.rpush(chat_key(code), json.dumps(msg, ensure_ascii=False))
    store.ltrim(chat_key(code), -40, -1)
    store.expire(chat_key(code), ROOM_TTL)


def read_chat(store, room, after):
    if int(room.raw.get("chat_seq", 0)) <= after:
        return []                      # nothing new, save one Redis call
    msgs = [json.loads(x) for x in store.lrange(chat_key(room.code), -30, -1)]
    return [msg for msg in msgs if msg["id"] > after]


def send_chat(store, code, pid, secret, text):
    room = require_room(store, code)
    player = require_player(room, pid, secret)
    text = " ".join(str(text).split())[:120]
    if not text:
        raise GameError("Type a message first.")
    m = room.meta
    if m["status"] == "question":
        answer = norm(m["questions"][m["q"]]["en"])
        if re.search(rf"\b{re.escape(answer)}\b", norm(text)) or squash(answer) == squash(text):
            raise GameError("Shh! No answers in the chat 🤫")
    push_chat(store, room.code, {"pid": pid, "name": player["name"], "avatar": player["avatar"], "text": censor(text)})


# ---------- player actions ----------

def create_room(store, public=False, topics=None):
    for _ in range(20):
        code = "".join(random.choice(CODE_CHARS) for _ in range(5))
        if not store.hget(room_key(code), "meta"):
            break
    meta = {
        "v": 1, "public": public, "host": None, "topics": clean_topics(topics) or list(TOPICS),
        "status": "lobby", "round": 0, "q": 0, "phase_at": 0, "auto_start_at": None,
        "in_game": [], "questions": [], "created": now(),
    }
    store.hset(room_key(code), "meta", json.dumps(meta, ensure_ascii=False))
    store.expire(room_key(code), ROOM_TTL)
    if public:
        store.sadd("public_rooms", code)
    return code


def join_room(store, code, name, avatar):
    room = require_room(store, code)
    t = now()
    name = clean_name(name)
    avatar = avatar if avatar in AVATARS else AVATARS[0]
    active = room.active_ids(t)
    if len(active) >= MAX_PLAYERS:
        raise GameError(f"This room is full ({MAX_PLAYERS} players). Try another one.")

    taken = {room.players[p]["name"].lower() for p in active}
    final, n = name, 2
    while final.lower() in taken:      # two kids named "Bin" -> "Bin 2"
        final = f"{name[:9]} {n}"
        n += 1

    pid, secret = secrets.token_hex(4), secrets.token_hex(12)
    key = room_key(room.code)
    store.hset(key, f"p:{pid}", json.dumps({"name": final, "avatar": avatar, "secret": secret, "joined": t}, ensure_ascii=False))
    store.hset(key, f"seen:{pid}", t)
    push_chat(store, room.code, {"system": True, "text": f"{avatar} {final} joined the room"})
    return {"code": room.code, "pid": pid, "secret": secret, "name": final}


def quick_match(store, name, avatar):
    clean_name(name)                   # check the name before touching rooms
    t = now()
    for code in store.smembers("public_rooms")[:15]:
        room = load_room(store, code)
        if not room:
            store.srem("public_rooms", code)   # room expired
            continue
        if room.meta["status"] == "lobby" and len(room.active_ids(t)) < MAX_PLAYERS:
            return join_room(store, code, name, avatar)
    return join_room(store, create_room(store, public=True), name, avatar)


def leave_room(store, code, pid, secret):
    room = require_room(store, code)
    player = require_player(room, pid, secret)
    store.hdel(room_key(room.code), f"seen:{pid}")
    push_chat(store, room.code, {"system": True, "text": f"{player['avatar']} {player['name']} left the room"})


def start(store, code, pid, secret):
    def change(room, m):
        if m["status"] != "lobby":
            raise GameError("The game already started.")
        if m["host"] != pid:
            raise GameError("Only the host can start the game.")
        if len(room.active_ids(now())) < MIN_PLAYERS:
            raise GameError(f"You need at least {MIN_PLAYERS} players. Share the room code!")
        start_game(room, m, now())
    mutate_meta(store, code, pid, secret, change)


def set_topics(store, code, pid, secret, topics):
    topics = clean_topics(topics)
    if not topics:
        raise GameError("Pick at least one topic.")

    def change(room, m):
        if m["status"] != "lobby":
            raise GameError("You can change topics before the game starts.")
        if m["host"] != pid:
            raise GameError("Only the host can change topics.")
        m["topics"] = topics
    mutate_meta(store, code, pid, secret, change)


def play_again(store, code, pid, secret):
    def change(room, m):
        if m["status"] != "finished":
            raise GameError("The game is not over yet.")
        if m["host"] != pid:
            raise GameError("Only the host can start a new game.")
        reset_to_lobby(m)
    mutate_meta(store, code, pid, secret, change)


def submit_answer(store, code, pid, secret, answer):
    room = require_room(store, code)
    require_player(room, pid, secret)
    t = now()
    advance(store, room, t)
    m = room.meta
    if m["status"] != "question":
        return {"result": "late"}
    if pid not in m["in_game"]:
        raise GameError("Wait for the next game to play.")

    qi, rnd = m["q"], m["round"]
    if room.solved(rnd, qi, pid):
        return {"result": "done"}

    key, score_field = room_key(room.code), f"s:{rnd}:{pid}"
    points = points_for(m["questions"][qi], t - m["phase_at"])

    if squash(answer) and squash(answer) == squash(m["questions"][qi]["en"]):
        took = round(t - m["phase_at"], 1)
        if not store.hsetnx(key, f"ok:{rnd}:{qi}:{pid}", took):
            return {"result": "done"}  # double click, already counted
        return {"result": "correct", "points": points, "score": store.hincrby(key, score_field, points)}

    # Wrong answer: minus 20, but the score never goes under 0
    new = store.hincrby(key, score_field, -WRONG_PENALTY)
    lost = WRONG_PENALTY
    if new < 0:
        store.hset(key, score_field, 0)
        lost, new = WRONG_PENALTY + new, 0
    return {"result": "wrong", "lost": lost, "score": new}


# ---------- what the player sees ----------

def get_state(store, code, pid, secret, after=0, ping=False):
    room = require_room(store, code)
    require_player(room, pid, secret)
    t = now()
    if ping:
        store.hset(room_key(room.code), f"seen:{pid}", t)
        room.raw[f"seen:{pid}"] = str(t)
    advance(store, room, t)
    return build_view(store, room, pid, t, after)


def question_view(room, m, pid, t):
    i = m["q"]
    q = m["questions"][i]
    level = LEVELS[q["level"]]
    elapsed = t - m["phase_at"]
    v = {
        "index": i, "total": len(m["questions"]), "level": q["level"], "kind": level["kind"],
        "level_name": level["name"], "points": points_for(q, elapsed), "penalty": WRONG_PENALTY,
        "vi": q["vi"], "emoji": q["emoji"], "topic": TOPICS[q["topic"]]["name"],
        "my_solved": room.solved(m["round"], i, pid),
    }
    if m["status"] == "question":
        v["time_total"] = level["time"]
        v["time_left"] = max(0, level["time"] - elapsed)
        if level["kind"] == "pick":
            v["options"] = q["options"]
        elif level["kind"] == "letters":
            v.update(letters_view(room.code, m["round"], i, q, elapsed))
        else:
            v["mask"] = ["_" if ch.isalpha() else ch for ch in q["en"]]
    else:                              # reveal: now it is safe to send the answer
        v["time_total"] = REVEAL_SECONDS
        v["time_left"] = max(0, REVEAL_SECONDS - elapsed)
        v["answer"] = q["en"]
    return v


def build_view(store, room, pid, t, after):
    m = room.meta
    rnd, status = m["round"], m["status"]
    in_play = status in ("question", "reveal")
    show_scores = status != "lobby"
    active = room.active_ids(t)

    ids = active + [p for p in m["in_game"] if p not in active and p in room.players]
    rows = []
    for p in ids:
        info = room.players[p]
        rows.append({
            "pid": p, "name": info["name"], "avatar": info["avatar"], "host": p == m["host"],
            "online": p in active, "playing": p in m["in_game"],
            "score": room.score(rnd, p) if show_scores else 0,
            "solved": in_play and room.solved(rnd, m["q"], p),
        })
    if show_scores:
        rows.sort(key=lambda r: (not r["playing"], -r["score"]))

    view = {
        "code": room.code, "public": m["public"], "status": status, "round": rnd, "me": pid,
        "host": m["host"], "topics": m["topics"], "players": rows, "min_players": MIN_PLAYERS,
        "max_players": MAX_PLAYERS, "playing": pid in m["in_game"],
        "my_score": room.score(rnd, pid) if show_scores else 0,
    }
    if status == "lobby" and m["auto_start_at"]:
        view["auto_start_in"] = max(0, m["auto_start_at"] - t)
    if status == "countdown":
        view["time_left"] = max(0, m["phase_at"] + COUNTDOWN_SECONDS - t)
    if in_play:
        view["question"] = question_view(room, m, pid, t)
    if status == "finished":
        view["max_score"] = sum(LEVELS[q["level"]]["points"] for q in m["questions"])
        view["review"] = [
            {"en": q["en"], "vi": q["vi"], "emoji": q["emoji"]}
            for i, q in enumerate(m["questions"]) if not room.solved(rnd, i, pid)
        ] if pid in m["in_game"] else []
    view["chat"] = read_chat(store, room, after)
    return view