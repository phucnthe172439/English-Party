"""Where the game keeps its data.

On Vercel we use Upstash Redis, because many server copies must share the
same rooms. On your laptop (no Redis env vars) we use a simple in-memory
store, so you can run the game without any setup.

Both classes have the same small list of methods, so the game code does
not care which one it gets.
"""
import os
import threading
import time


def _redis_slice(items, start, end):
    """Redis style list slice: end is included and negative numbers count from the back."""
    n = len(items)
    if start < 0:
        start += n
    if end < 0:
        end += n
    return items[max(start, 0):end + 1]


class MemoryStore:
    def __init__(self):
        self.data = {}
        self.expires_at = {}
        self.lock = threading.RLock()

    def _alive(self, key):
        exp = self.expires_at.get(key)
        if exp and exp < time.time():
            self.data.pop(key, None)
            self.expires_at.pop(key, None)
        return key in self.data

    # plain keys
    def get(self, key):
        with self.lock:
            return self.data.get(key) if self._alive(key) else None

    def set(self, key, value, ex=None, nx=False):
        with self.lock:
            if nx and self._alive(key):
                return False
            self.data[key] = str(value)
            if ex:
                self.expires_at[key] = time.time() + ex
            return True

    def delete(self, key):
        with self.lock:
            self.data.pop(key, None)

    def expire(self, key, seconds):
        with self.lock:
            if key in self.data:
                self.expires_at[key] = time.time() + seconds

    # hashes
    def hgetall(self, key):
        with self.lock:
            return dict(self.data.get(key, {})) if self._alive(key) else {}

    def hget(self, key, field):
        with self.lock:
            return self.hgetall(key).get(field)

    def hset(self, key, field, value):
        with self.lock:
            self.data.setdefault(key, {})[field] = str(value)

    def hsetnx(self, key, field, value):
        with self.lock:
            h = self.data.setdefault(key, {})
            if field in h:
                return False
            h[field] = str(value)
            return True

    def hincrby(self, key, field, amount):
        with self.lock:
            h = self.data.setdefault(key, {})
            h[field] = str(int(h.get(field, 0)) + amount)
            return int(h[field])

    def hdel(self, key, field):
        with self.lock:
            self.data.get(key, {}).pop(field, None)

    # sets
    def sadd(self, key, member):
        with self.lock:
            self.data.setdefault(key, set()).add(member)

    def srem(self, key, member):
        with self.lock:
            self.data.get(key, set()).discard(member)

    def smembers(self, key):
        with self.lock:
            return list(self.data.get(key, set()))

    # lists
    def rpush(self, key, value):
        with self.lock:
            self.data.setdefault(key, []).append(value)

    def ltrim(self, key, start, end):
        with self.lock:
            self.data[key] = _redis_slice(self.data.get(key, []), start, end)

    def lrange(self, key, start, end):
        with self.lock:
            return _redis_slice(self.data.get(key, []), start, end)


class RedisStore:
    """Thin wrapper over Upstash Redis (HTTP based, works great on serverless)."""

    def __init__(self, url, token):
        from upstash_redis import Redis
        self.r = Redis(url=url, token=token)

    def get(self, key):
        return self.r.get(key)

    def set(self, key, value, ex=None, nx=False):
        return bool(self.r.set(key, str(value), ex=ex, nx=nx))

    def delete(self, key):
        self.r.delete(key)

    def expire(self, key, seconds):
        self.r.expire(key, seconds)

    def hgetall(self, key):
        return self.r.hgetall(key) or {}

    def hget(self, key, field):
        return self.r.hget(key, field)

    def hset(self, key, field, value):
        self.r.hset(key, field, str(value))

    def hsetnx(self, key, field, value):
        return bool(self.r.hsetnx(key, field, str(value)))

    def hincrby(self, key, field, amount):
        return int(self.r.hincrby(key, field, amount))

    def hdel(self, key, field):
        self.r.hdel(key, field)

    def sadd(self, key, member):
        self.r.sadd(key, member)

    def srem(self, key, member):
        self.r.srem(key, member)

    def smembers(self, key):
        return list(self.r.smembers(key) or [])

    def rpush(self, key, value):
        self.r.rpush(key, value)

    def ltrim(self, key, start, end):
        self.r.ltrim(key, start, end)

    def lrange(self, key, start, end):
        return self.r.lrange(key, start, end) or []


def get_store():
    # Upstash gives UPSTASH_*; the Vercel marketplace integration gives KV_*
    url = os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN")
    if url and token:
        return RedisStore(url, token)
    if os.getenv("VERCEL"):
        print("WARNING: no Redis env vars found. Rooms will not work across server copies.")
    return MemoryStore()
