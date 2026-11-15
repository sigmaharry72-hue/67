"""
Chest counter leaderboard server (standard library only, no installs needed).

It stores one entry per player: a display name and a chest count. Players are
identified by a random secret id that their counter makes up; only a hash of it
is stored here, and it is never shown to anyone, so nobody can overwrite
someone else's entry.

Run locally to try it:     python leaderboard_server.py
Then in the counter set:   LEADERBOARD_URL = "http://localhost:8080"

Settings (environment variables, all optional):
  PORT        port to listen on                    (default 8080)
  DATA_FILE   where the scores are saved           (default leaderboard_data.json
                                                    next to this script)

Endpoints:
  POST /sync   {id, name, count, n}  -> saves the player's entry and returns the top list
               (a blank name leaves the board and just returns the top list)
  GET  /top?n=10                     -> the top list, read-only
  GET  /                             -> "ok" (for host health checks)

Scores are self-reported by each counter, so this is an honor-system board.
"""
import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.environ.get("DATA_FILE", os.path.join(HERE, "leaderboard_data.json"))
PORT = int(os.environ.get("PORT", "8080"))

MAX_PLAYERS = 5000             # stop accepting new players beyond this
MAX_COUNT = 10_000_000         # counts are clamped to this
NAME_MAX = 16
TOP_DEFAULT = 10
TOP_MAX = 50
MIN_SECONDS_BETWEEN_UPDATES = 2   # per player: faster updates are ignored
MAX_BODY_BYTES = 2048
# names that can never be on the board (test accounts etc.); comma separated, not case sensitive.
# Change here or set the BLOCKED_NAMES environment variable on the host.
BLOCKED_NAMES = {n.strip().lower() for n in os.environ.get("BLOCKED_NAMES", "67 mate,olly is fat").split(",") if n.strip()}
# Players put on the board when the server starts, until their own counter checks in
# (then the real count replaces this one). (name, count)
SEED_PLAYERS = [("Harry", 2261), ("Big Bloody T", 2195), ("stevstev", 122)]
IDLE_DAYS = 90                 # players who haven't checked in for this long are dropped

lock = threading.Lock()
players = {}   # sha256(player id) -> {"name", "count", "reached", "updated"}


def load():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, rec in data.items():
            players[key] = {
                "name": clean_name(rec["name"]),
                "count": clamp_count(rec["count"]),
                "reached": float(rec.get("reached", 0)),
                "updated": float(rec.get("updated", 0)),
                "uid": str(rec.get("uid", key)),
            }
    except FileNotFoundError:
        pass
    except Exception as e:                      # a damaged file shouldn't stop the server
        print("could not read %s (%s); starting empty" % (DATA_FILE, e))


def save():
    tmp = DATA_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(players, f)
        os.replace(tmp, DATA_FILE)              # atomic: never leaves a half-written file
    except Exception as e:
        print("could not save %s: %s" % (DATA_FILE, e))


def clean_name(value):
    s = " ".join(str(value).split())
    s = "".join(ch for ch in s if ch.isprintable())
    return s[:NAME_MAX].strip()


def clamp_count(value):
    return max(0, min(MAX_COUNT, int(value)))


def ranked():
    # highest count first; on a tie, whoever reached that count first ranks higher
    return sorted(players.items(), key=lambda kv: (-kv[1]["count"], kv[1]["reached"], kv[0]))


def build_reply(my_key, n):
    order = ranked()
    top = [{"name": rec["name"], "count": rec["count"], "me": key == my_key}
           for key, rec in order[:n]]
    you = None
    if my_key in players:
        for i, (key, rec) in enumerate(order):
            if key == my_key:
                you = {"rank": i + 1, "count": rec["count"], "total": len(order)}
                break
    reply = {"top": top, "you": you, "total": len(order)}
    if you and you["rank"] > n:                 # outside the top list: send your own row too
        rec = players[my_key]
        reply["me_row"] = {"name": rec["name"], "count": rec["count"], "rank": you["rank"]}
    return reply


def clamp_n(value):
    try:
        return max(1, min(TOP_MAX, int(value)))
    except (TypeError, ValueError):
        return TOP_DEFAULT


def sync(body):
    """Returns (http_status, reply_dict)."""
    pid = body.get("id")
    if not isinstance(pid, str) or not (16 <= len(pid) <= 64) or not re.fullmatch(r"[0-9a-zA-Z_\-]+", pid):
        return 400, {"error": "bad id"}
    count = body.get("count")
    if isinstance(count, bool) or not isinstance(count, (int, float)):
        return 400, {"error": "bad count"}
    name = clean_name(body.get("name", ""))
    uid = hashlib.sha256(pid.encode("utf-8")).hexdigest()
    # the key includes the name: two PCs that ended up with the same id (copied profile)
    # stay two separate players instead of overwriting each other
    key = hashlib.sha256((pid + "|" + name.lower()).encode("utf-8")).hexdigest() if name else uid
    n = clamp_n(body.get("n", TOP_DEFAULT))
    now = time.time()
    count = clamp_count(count)

    with lock:
        if name.lower() in BLOCKED_NAMES:           # blocked name: make sure it is off the board
            if key in players:
                del players[key]
                save()
            return 200, build_reply(key, n)
        if name:
            rec = players.get(key)
            if rec is None:                     # take over a pre-added entry with the same name
                sk = next((k for k, r in players.items()
                           if r.get("uid") == "seed" and r["name"].lower() == name.lower()), None)
                if sk:
                    rec = players[key] = players.pop(sk)
                    rec["uid"] = uid
                    rec["updated"] = now - MIN_SECONDS_BETWEEN_UPDATES
            if rec is None:
                if len(players) >= MAX_PLAYERS:
                    return 503, {"error": "leaderboard is full"}
                players[key] = {"name": name, "count": count, "reached": now, "updated": now, "uid": uid}
                save()
            elif now - rec["updated"] >= MIN_SECONDS_BETWEEN_UPDATES:
                if count != rec["count"]:
                    rec["reached"] = now
                rec["count"] = count
                rec["name"] = name
                rec["updated"] = now
                save()
        else:                                   # blank name = the player left the board
            gone = [k for k, r in players.items() if r.get("uid") == uid]
            for k in gone:
                del players[k]
            if gone:
                save()
        if name:                                # a renamed player's old entry goes after 15 minutes
            old = [k for k, r in players.items()
                   if r.get("uid") == uid and k != key and now - r["updated"] > 900]
            for k in old:
                del players[k]
            if old:
                save()
        return 200, build_reply(key, n)


def drop_idle():
    cutoff = time.time() - IDLE_DAYS * 86400
    with lock:
        stale = [k for k, rec in players.items()
                 if rec["updated"] < cutoff or rec["name"].lower() in BLOCKED_NAMES]
        for k in stale:
            del players[k]
        if stale:
            save()


class Handler(BaseHTTPRequestHandler):
    server_version = "ChestBoard/1"

    def log_message(self, fmt, *args):          # keep the console quiet (no ids in logs)
        pass

    def send_json(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            data = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif url.path == "/top":
            n = clamp_n(parse_qs(url.query).get("n", [TOP_DEFAULT])[0])
            with lock:
                reply = build_reply(None, n)
            reply.pop("you", None)
            self.send_json(200, reply)
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/sync":
            self.send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_json(400, {"error": "bad request size"})
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            self.send_json(400, {"error": "bad json"})
            return
        status, reply = sync(body)
        self.send_json(status, reply)


def seed_players():
    now = time.time()
    with lock:
        have = {r["name"].lower() for r in players.values()}
        for name, count in SEED_PLAYERS:
            if name.lower() in have or name.lower() in BLOCKED_NAMES:
                continue
            key = hashlib.sha256(("seed|" + name.lower()).encode("utf-8")).hexdigest()
            players[key] = {"name": clean_name(name), "count": clamp_count(count),
                            "reached": now, "updated": now - 60, "uid": "seed"}
        save()


def main():
    load()
    drop_idle()
    seed_players()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print("Leaderboard server running on port %d (%d players loaded)" % (PORT, len(players)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
