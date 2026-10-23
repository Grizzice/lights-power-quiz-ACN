#!/usr/bin/env python3
"""
Live multiplayer quiz in the style of Kahoot. Python 3 standard library only.

    python server.py                 this computer only, host screen at http://localhost:8000/host
    python server.py --lan           reachable by phones on the same Wi-Fi
    python server.py --tunnel        public link via Cloudflare (needs cloudflared; blocked on Accenture's network)
    python server.py --port 9000     use another port
    PORT=10000 python server.py      cloud mode, as on Render (see render.yaml and README.md)

Players open the join link on a phone or laptop and type the game PIN shown on
the host screen. Each time the host page creates a game it gets a fresh PIN.
Questions live in quiz.json, images in public/img.
"""
import argparse
import csv
import hashlib
import io
import json
import os
import queue
import random
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
sys.path.insert(0, os.path.join(ROOT, "vendor"))
try:
    import segno  # QR codes for the lobby screen
except Exception:  # the game still works without a QR code
    segno = None

with open(os.path.join(ROOT, "quiz.json"), encoding="utf-8") as f:
    QUIZ = json.load(f)
QUESTIONS = QUIZ["questions"]
TITLE = QUIZ.get("title", "Quiz")
ANSWER_SECS = float(QUIZ.get("seconds_per_question", 10))
INTRO_SECS = float(QUIZ.get("intro_seconds", 4))

POLL_SECS = 20          # how long a state request waits for something to change
IDLE_SECS = 35          # players unheard of this long no longer hold up "everyone answered"
GAME_TTL = 8 * 3600     # forget games nobody has touched for this long
MAX_PLAYERS = 500
MAX_GAMES = 200         # oldest idle game is dropped beyond this (protects a public server)
PUBLIC_URL = None       # set from --public-url, --lan, --tunnel or the cloud host, shown on the host screen

LOCK = threading.RLock()
GAMES = {}


def digest(view):
    return hashlib.md5(json.dumps(view, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:12]

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".json": "application/json"}


def qsecs(q):
    return float(q.get("seconds", ANSWER_SECS))


def clean_name(raw):
    name = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", "", str(raw or ""))).strip()
    return name[:16].strip()


def csv_safe(text):
    # stop Excel reading a nickname such as "=1+1" as a formula
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


class Game:
    def __init__(self):
        while True:
            self.pin = str(random.randint(100000, 999999))
            if self.pin not in GAMES:
                break
        self.host_token = secrets.token_urlsafe(18)
        self.players = {}       # token -> player dict
        self.kicked = set()
        self.joins = 0
        self.touched = time.time()
        # Waiting requests sleep on this and wake only when a version they depend on moves,
        # so one player's answer doesn't make every other phone recompute its screen.
        self.cond = threading.Condition(LOCK)
        self.ver = 0            # changes that touch every screen
        self.host_ver = 0       # changes only the host screen shows
        self.reset()

    def bump(self, who=None):
        """Record a change for everyone (None), the host ("host") or one player (token)."""
        if who is None:
            self.ver += 1
        elif who == "host":
            self.host_ver += 1
        elif who in self.players:
            self.players[who]["ver"] += 1
        self.cond.notify_all()

    def reset(self):
        self.phase = "lobby"    # lobby > intro > question > reveal > scoreboard > ... > podium
        self.qi = -1
        self.deadline = None    # wall-clock end of a timed phase (intro, question)
        self.q_started = None
        self.answers = {}       # token -> (choice, seconds taken), current question only
        for p in self.players.values():
            p.update(score=0, streak=0, correct=0, last=None, points={})
        self.rank()

    # ---- players -------------------------------------------------------
    def add_player(self, name):
        self.joins += 1
        token = secrets.token_urlsafe(18)
        self.players[token] = {"name": name, "score": 0, "streak": 0, "correct": 0, "last": None,
                               "points": {}, "joined": self.joins, "seen": time.time(), "ver": 0}
        self.rank()
        return token

    def find_name(self, name):
        for token, p in self.players.items():
            if p["name"].lower() == name.lower():
                return token
        return None

    def rank(self):
        self.ranking = sorted(self.players, key=lambda t: (-self.players[t]["score"], self.players[t]["joined"]))

    def standing(self, token):
        p = self.players[token]
        place = 1 + sum(1 for o in self.players.values() if o["score"] > p["score"])
        ahead = None
        if token in self.ranking:
            i = self.ranking.index(token)
            if i > 0:
                o = self.players[self.ranking[i - 1]]
                ahead = {"name": o["name"], "gap": o["score"] - p["score"]}
        return place, ahead

    # ---- flow ----------------------------------------------------------
    def q(self):
        return QUESTIONS[self.qi]

    def to_intro(self, i):
        self.qi, self.phase, self.answers = i, "intro", {}
        self.deadline = time.time() + INTRO_SECS

    def to_question(self):
        self.phase = "question"
        self.q_started = time.time()
        self.deadline = self.q_started + qsecs(self.q())

    def to_reveal(self):
        q, secs = self.q(), qsecs(self.q())
        for token, p in self.players.items():
            a = self.answers.get(token)
            if a is None:
                p["streak"], pts = 0, 0
                res = {"answered": False, "correct": False}
            else:
                choice, took = a
                ok = choice in q["correct"]
                # Kahoot's formula: full 1000 for an instant answer, 500 at the buzzer
                pts = round(1000 * (1 - min(took, secs) / secs / 2)) if ok else 0
                p["streak"] = p["streak"] + 1 if ok else 0
                p["correct"] += 1 if ok else 0
                res = {"answered": True, "correct": ok}
            p["score"] += pts
            p["points"][self.qi] = pts
            res.update(points=pts, streak=p["streak"])
            p["last"] = res
        self.rank()
        self.phase, self.deadline = "reveal", None

    def host_next(self):
        if self.phase == "lobby" and self.players:
            self.to_intro(0)
        elif self.phase == "intro":
            self.to_question()
        elif self.phase == "question":
            self.to_reveal()
        elif self.phase == "reveal":
            self.phase = "podium" if self.qi + 1 >= len(QUESTIONS) else "scoreboard"
        elif self.phase == "scoreboard":
            self.to_intro(self.qi + 1)

    def host_action(self, action, name=""):
        self.touched = time.time()
        if action == "next":
            self.host_next()
        elif action == "kick":
            token = self.find_name(name) if name else None
            if token:
                self.kicked.add(token)
                self.answers.pop(token, None)
                del self.players[token]
                self.rank()
                if self.phase == "question" and self.everyone_answered():
                    self.to_reveal()
        elif action == "end":
            self.phase, self.deadline = "podium", None
        elif action == "restart":
            self.reset()
        self.bump()

    def everyone_answered(self):
        now = time.time()
        live = [t for t, p in self.players.items() if now - p["seen"] < IDLE_SECS]
        return bool(live) and all(t in self.answers for t in live)

    # ---- what each screen sees -----------------------------------------
    def host_view(self):
        v = {"role": "host", "pin": self.pin, "title": TITLE, "phase": self.phase, "qi": self.qi,
             "total": len(QUESTIONS), "count": len(self.players), "join": PUBLIC_URL}
        if self.phase == "lobby":
            v["players"] = [self.players[t]["name"] for t in sorted(self.players, key=lambda t: self.players[t]["joined"])]
        if self.phase in ("intro", "question", "reveal"):
            q = self.q()
            v["q"] = {"text": q["q"], "image": q.get("image"), "answers": q["answers"], "secs": qsecs(q)}
            v["answered"] = len(self.answers)
            if self.phase == "intro" and self.qi + 1 < len(QUESTIONS):
                v["next_image"] = QUESTIONS[self.qi + 1].get("image")
        if self.phase == "reveal":
            dist = [0] * len(q["answers"])
            for choice, _ in self.answers.values():
                dist[choice] += 1
            v["dist"], v["correct"] = dist, q["correct"]
        if self.phase == "scoreboard":
            v["board"] = [{"name": self.players[t]["name"], "score": self.players[t]["score"],
                           "gain": self.players[t]["points"].get(self.qi, 0), "streak": self.players[t]["streak"]}
                          for t in self.ranking[:5]]
        if self.phase == "podium":
            v["final"] = [{"name": self.players[t]["name"], "score": self.players[t]["score"],
                           "correct": self.players[t]["correct"], "place": self.standing(t)[0]}
                          for t in self.ranking]
        return v

    def player_view(self, token):
        p = self.players[token]
        v = {"role": "player", "title": TITLE, "phase": self.phase, "qi": self.qi, "total": len(QUESTIONS),
             "name": p["name"], "score": p["score"], "pin": self.pin}
        if self.phase in ("intro", "question"):
            q = self.q()
            v["q"] = {"text": q["q"], "n": len(q["answers"])}
        if self.phase == "question":
            v["q"]["answers"] = q["answers"]
            a = self.answers.get(token)
            v["answered"] = a is not None
            v["choice"] = a[0] if a else None
        if self.phase in ("reveal", "scoreboard", "podium"):
            v["place"], v["ahead"] = self.standing(token)
            v["players"] = len(self.players)
            v["last"] = p["last"]
            v["correct"] = p["correct"]
        return v


def ticker():
    """Moves timed phases on and clears out old games."""
    while True:
        time.sleep(0.1)
        with LOCK:
            now = time.time()
            for pin, g in list(GAMES.items()):
                if g.deadline and now >= g.deadline:
                    g.to_question() if g.phase == "intro" else g.to_reveal()
                    g.bump()
                if now - g.touched > GAME_TTL:
                    del GAMES[pin]
                    g.bump()   # waiting screens see the game has gone


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "QuizServer/1.0"
    timeout = 90

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, TimeoutError, OSError):
            pass

    # ---- helpers -------------------------------------------------------
    def send_bytes(self, data, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, obj, code=200):
        self.send_bytes(json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8", code,
                        {"Cache-Control": "no-store"})

    def static(self, rel):
        path = os.path.normpath(os.path.join(PUBLIC, rel))
        if not path.startswith(PUBLIC + os.sep) or not os.path.isfile(path):
            return self.send_bytes(b"Not found", "text/plain", 404)
        ext = os.path.splitext(path)[1].lower()
        with open(path, "rb") as f:
            data = f.read()
        cache = "public, max-age=3600" if ext in (".jpg", ".jpeg", ".png", ".svg") else "no-cache"
        self.send_bytes(data, MIME.get(ext, "application/octet-stream"), extra={"Cache-Control": cache})

    # ---- routes --------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = u.path
        if path == "/api/state":
            return self.api_state(qs)
        if path == "/api/check":
            with LOCK:
                g = GAMES.get(qs.get("pin", ""))
                ok = g is not None and g.phase != "podium"
            return self.send_json({"ok": ok})
        if path == "/api/info":
            return self.send_json({"title": TITLE, "join_base": PUBLIC_URL, "questions": len(QUESTIONS),
                                   "images": [q.get("image") for q in QUESTIONS if q.get("image")]})
        if path == "/api/qr.svg":
            data = qs.get("data", "")[:300]
            if not segno or not data:
                return self.send_bytes(b"", "image/svg+xml", 404)
            buf = io.BytesIO()
            segno.make(data, error="m").save(buf, kind="svg", scale=8, border=2, dark="#000", light="#fff")
            return self.send_bytes(buf.getvalue(), "image/svg+xml")
        if path == "/api/results.csv":
            return self.api_results(qs)
        if path in ("/", "/play", "/join"):
            return self.static("play.html")
        if path == "/host":
            return self.static("host.html")
        if path == "/favicon.ico":
            return self.send_bytes(b"", "image/x-icon", 204)
        return self.static(path.lstrip("/"))

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 20000:
                return self.send_json({"error": "Request too large"}, 413)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "Bad request"}, 400)
        route = {"/api/create": self.api_create, "/api/join": self.api_join,
                 "/api/answer": self.api_answer, "/api/host": self.api_host}.get(urlparse(self.path).path)
        if not route:
            return self.send_json({"error": "Not found"}, 404)
        return route(body)

    # ---- API -----------------------------------------------------------
    def api_state(self, qs):
        pin, token, etag = qs.get("pin", ""), qs.get("token", ""), qs.get("etag", "")
        end, seen_key = time.time() + POLL_SECS, None
        with LOCK:
            while True:
                g = GAMES.get(pin)
                if g is None:
                    view = {"phase": "gone"}
                    tag = digest(view)
                    break
                is_host, p = token == g.host_token, g.players.get(token)
                if is_host:
                    g.touched = time.time()
                elif p is not None:
                    p["seen"] = time.time()
                key = (g.ver, g.host_ver if is_host else p["ver"] if p is not None else None)
                if key != seen_key:   # only rebuild the view when something it depends on changed
                    seen_key = key
                    view = g.host_view() if is_host else g.player_view(token) if p is not None \
                        else {"phase": "kicked" if token in g.kicked else "unknown"}
                    tag = digest(view)
                    if tag != etag:
                        break
                left = end - time.time()
                if left <= 0:
                    break
                g.cond.wait(left)
            ms = int(max(0.0, g.deadline - time.time()) * 1000) if g is not None and g.deadline else None
        return self.send_json({"etag": tag, "view": view if tag != etag else None, "ms": ms})

    def api_create(self, body):
        with LOCK:
            if len(GAMES) >= MAX_GAMES:
                oldest = min(GAMES.values(), key=lambda x: x.touched)
                del GAMES[oldest.pin]
                oldest.bump()
            g = Game()
            GAMES[g.pin] = g
        return self.send_json({"pin": g.pin, "token": g.host_token})

    def api_join(self, body):
        pin, name = str(body.get("pin", "")).strip(), clean_name(body.get("name"))
        if not name:
            return self.send_json({"error": "Please enter a nickname"}, 400)
        err = None
        with LOCK:
            g = GAMES.get(pin)
            token = g.find_name(name) if g is not None else None
            if g is None or g.phase == "podium":
                err = ("We didn't recognise that game PIN", 404)
            elif token:
                # a nickname can be taken back only if its owner has gone quiet (lost phone, cleared browser)
                if time.time() - g.players[token]["seen"] < IDLE_SECS:
                    err = ("That nickname is taken. If it's yours, wait 30 seconds and try again", 409)
                else:
                    g.players[token]["seen"] = time.time()
            elif len(g.players) >= MAX_PLAYERS:
                err = ("This game is full", 409)
            else:
                token = g.add_player(name)
                # lobby: only the host's name list changes; later on, everyone's ranking does
                g.bump("host" if g.phase in ("lobby", "intro", "question") else None)
            if g is not None:
                g.touched = time.time()
        if err:
            return self.send_json({"error": err[0]}, err[1])
        return self.send_json({"token": token, "name": name, "pin": pin})

    def api_answer(self, body):
        try:
            qi, choice = int(body.get("qi")), int(body.get("choice"))
        except (TypeError, ValueError):
            return self.send_json({"error": "Bad answer"}, 400)
        with LOCK:
            g = GAMES.get(str(body.get("pin", "")))
            token = body.get("token", "")
            if g is None or token not in g.players:
                found = False
            else:
                found = True
                ok = g.phase == "question" and qi == g.qi and token not in g.answers \
                    and 0 <= choice < len(g.q()["answers"])
                if ok:
                    g.answers[token] = (choice, time.time() - g.q_started)
                    g.players[token]["seen"] = time.time()
                    if g.everyone_answered():
                        g.to_reveal()
                        g.bump()
                    else:
                        g.bump("host")
                        g.bump(token)
        if not found:
            return self.send_json({"error": "Not in this game"}, 404)
        return self.send_json({"ok": ok})

    def api_host(self, body):
        with LOCK:
            g = GAMES.get(str(body.get("pin", "")))
            allowed = g is not None and body.get("token") == g.host_token
            if allowed:
                g.host_action(body.get("action"), clean_name(body.get("name")))
                phase = g.phase
        if not allowed:
            return self.send_json({"error": "Not the host of this game"}, 403)
        return self.send_json({"ok": True, "phase": phase})

    def api_results(self, qs):
        with LOCK:
            g = GAMES.get(qs.get("pin", ""))
            if g is None or qs.get("token") != g.host_token:
                g = None
            else:
                out = io.StringIO()
                w = csv.writer(out)
                w.writerow(["Place", "Nickname", "Score", "Correct"] + [f"Q{i + 1}" for i in range(len(QUESTIONS))])
                for t in g.ranking:
                    p = g.players[t]
                    w.writerow([g.standing(t)[0], csv_safe(p["name"]), p["score"], p["correct"]]
                               + [p["points"].get(i, "") for i in range(len(QUESTIONS))])
        if g is None:
            return self.send_bytes(b"Not found", "text/plain", 404)
        name = f"quiz-results-{g.pin}.csv"
        return self.send_bytes(("﻿" + out.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                               extra={"Content-Disposition": f'attachment; filename="{name}"'})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def start_tunnel(port):
    """Open a free Cloudflare quick tunnel to this server and return (process, public https URL)."""
    exe = next((p for p in (os.path.join(ROOT, "tools", "cloudflared.exe"), os.path.join(ROOT, "tools", "cloudflared"))
                if os.path.isfile(p)), None) or shutil.which("cloudflared")
    if not exe:
        print("  cloudflared was not found in the tools folder, so there is no public link.")
        return None, None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                            creationflags=flags)
    lines = queue.Queue()

    def drain():  # keep reading so cloudflared never blocks on a full pipe
        for line in proc.stdout:
            lines.put(line)
    threading.Thread(target=drain, daemon=True).start()

    url, ready, end = None, False, time.time() + 45
    while time.time() < end and not ready:
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m and not url:
            url = m.group(0)
        if url and "Registered tunnel connection" in line:
            ready = True
    if not url:
        print("  Could not open the public link. The network may be blocking Cloudflare tunnels.")
        proc.terminate()
        return None, None
    return proc, url


def main():
    global PUBLIC_URL
    ap = argparse.ArgumentParser(description="Live multiplayer quiz server")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--lan", action="store_true", help="listen on the local network, not just this computer")
    ap.add_argument("--tunnel", action="store_true", help="make a public https link with Cloudflare (tools/cloudflared.exe)")
    ap.add_argument("--public-url", help="the address players should use, shown on the host screen")
    ap.add_argument("--no-browser", action="store_true", help="don't open the host screen automatically")
    args = ap.parse_args()

    if os.environ.get("PORT"):
        # running on a cloud host such as Render: listen where it says, link to its public address
        PUBLIC_URL = (args.public_url or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/") or None
        srv = Server(("0.0.0.0", int(os.environ["PORT"])), Handler)
        threading.Thread(target=ticker, daemon=True).start()
        print(f"{TITLE}: {len(QUESTIONS)} questions. Host screen at {PUBLIC_URL or ''}/host", flush=True)
        srv.serve_forever()
        return

    bind = "0.0.0.0" if args.lan else "127.0.0.1"
    srv = None
    for port in range(args.port, args.port + 10):  # step past a port another program is using
        try:
            srv = Server((bind, port), Handler)
            break
        except OSError:
            continue
    if srv is None:
        sys.exit(f"Ports {args.port}-{args.port + 9} are all in use.")
    threading.Thread(target=ticker, daemon=True).start()
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    print(f"\n  {TITLE}: {len(QUESTIONS)} questions, {ANSWER_SECS:g} seconds each\n")
    tunnel = None
    if args.tunnel:
        print("  Opening the public link (takes about 10 seconds)...")
        sys.stdout.flush()
        tunnel, PUBLIC_URL = start_tunnel(port)
    if args.public_url:
        PUBLIC_URL = args.public_url.rstrip("/")
    elif args.lan and not PUBLIC_URL:
        PUBLIC_URL = f"http://{lan_ip()}:{port}"
    host_url = f"http://localhost:{port}/host"
    print(f"  HOST SCREEN   {host_url}   (open on the laptop you project from)")
    print(f"  PLAYERS JOIN  {PUBLIC_URL or f'http://localhost:{port}  (this computer only)'}")
    print("\n  Keep this window open during the quiz. Close it, or press Ctrl+C, to stop.\n")
    sys.stdout.flush()
    if not args.no_browser:
        webbrowser.open(host_url)
    try:
        while True:
            time.sleep(1)
            if tunnel is not None and tunnel.poll() is not None:
                print("  The public link dropped. Restarting it, the host screen will show the new link...")
                sys.stdout.flush()
                tunnel, url = start_tunnel(port)
                if url:
                    with LOCK:
                        PUBLIC_URL = url
                        for g in GAMES.values():
                            g.bump()
                    print(f"  PLAYERS JOIN  {PUBLIC_URL}")
                    sys.stdout.flush()
                else:
                    tunnel = None
    except KeyboardInterrupt:
        pass
    finally:
        if tunnel is not None:
            tunnel.terminate()


if __name__ == "__main__":
    main()
