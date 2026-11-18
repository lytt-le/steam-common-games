#!/usr/bin/env python3
"""Steam Common Games — pick Steam friends, see which games you all own.

Runs a small local web server (stdlib only) that talks to the Steam Web API
and serves a UI at http://127.0.0.1:8765. Your API key is stored in
~/.config/steam-common-games/config.json (chmod 600) and never leaves this
machine except in requests to api.steampowered.com.
"""
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST, PORT = "127.0.0.1", int(os.environ.get("PORT", "8765"))
HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "steam-common-games" / "config.json"
API = "https://api.steampowered.com"
CACHE_TTL = 15 * 60  # seconds

# Auto-shutdown: each open tab pings every few seconds. Background tabs can be
# throttled to ~1 timer/minute, so a tab only counts as gone after TAB_TIMEOUT
# (or immediately when it says goodbye on close). Once no tabs remain for
# EMPTY_GRACE seconds the server exits.
TAB_TIMEOUT = 90
EMPTY_GRACE = 10
STARTUP_GRACE = 60
_tabs: dict[str, float] = {}
_tabs_lock = threading.Lock()

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


# ---------------------------------------------------------------- config
def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------- steam api
class SteamError(Exception):
    def __init__(self, msg, status=502):
        super().__init__(msg)
        self.status = status


def steam_get(path: str, **params) -> dict:
    key = load_config().get("api_key")
    if not key:
        raise SteamError("No Steam API key configured", 400)
    url = f"{API}/{path}/?" + urllib.parse.urlencode({"key": key, "format": "json", **params})
    req = urllib.request.Request(url, headers={"User-Agent": "steam-common-games/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 401 and "GetFriendList" in path:
            raise SteamError("Your friends list is private — set Friends List to Public in Steam privacy settings", 403)
        if e.code in (401, 403):
            raise SteamError("Steam rejected the request (check your API key)", 403)
        if e.code == 429:
            raise SteamError("Steam is rate-limiting requests — wait a minute and try again", 429)
        raise SteamError(f"Steam API error {e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise SteamError(f"Couldn't reach Steam: {e}")


def cached(key: str, fn, force=False):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not force and now - hit[0] < CACHE_TTL:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def resolve_steam_id(text: str) -> str:
    """Accept a SteamID64, profile URL, or vanity name."""
    text = text.strip().rstrip("/")
    m = re.search(r"(\d{17})", text)
    if m and ("/profiles/" in text or text.isdigit()):
        return m.group(1)
    m = re.search(r"/id/([^/?#]+)", text)
    vanity = m.group(1) if m else text
    data = steam_get("ISteamUser/ResolveVanityURL/v1", vanityurl=vanity).get("response", {})
    if data.get("success") != 1:
        raise SteamError(f"Couldn't find a Steam profile for “{vanity}”", 404)
    return data["steamid"]


def player_summaries(ids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(ids), 100):
        chunk = ",".join(ids[i:i + 100])
        out += steam_get("ISteamUser/GetPlayerSummaries/v2", steamids=chunk).get("response", {}).get("players", [])
    return out


def summarize(p: dict) -> dict:
    return {
        "steamid": p["steamid"],
        "name": p.get("personaname", "?"),
        "avatar": p.get("avatarmedium") or p.get("avatar"),
        "state": p.get("personastate", 0),
        "ingame": p.get("gameextrainfo"),
        "profile": p.get("profileurl"),
        "public": p.get("communityvisibilitystate") == 3,
    }


def get_friends(force=False) -> dict:
    me = load_config().get("steam_id")
    if not me:
        raise SteamError("No Steam ID configured", 400)

    def fetch():
        fl = steam_get("ISteamUser/GetFriendList/v1", steamid=me, relationship="friend")
        ids = [f["steamid"] for f in fl.get("friendslist", {}).get("friends", [])]
        people = {p["steamid"]: summarize(p) for p in player_summaries([me] + ids)}
        return {
            "me": people.get(me),
            "friends": sorted((people[i] for i in ids if i in people), key=lambda p: p["name"].lower()),
        }

    return cached(f"friends:{me}", fetch, force)


def get_games(steamid: str, force=False) -> dict:
    def fetch():
        r = steam_get("IPlayerService/GetOwnedGames/v1", steamid=steamid,
                      include_appinfo=1, include_played_free_games=1).get("response", {})
        if "games" not in r:
            # Steam returns an empty object when the library is private.
            return {"steamid": steamid, "private": r.get("game_count") is None, "games": []}
        return {
            "steamid": steamid,
            "private": False,
            "games": [{"appid": g["appid"], "name": g.get("name", f"App {g['appid']}"),
                       "icon": g.get("img_icon_url"), "playtime": g.get("playtime_forever", 0)}
                      for g in r["games"]],
        }

    return cached(f"games:{steamid}", fetch, force)


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if "--verbose" in sys.argv:
            super().log_message(fmt, *args)

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def handle_api(self, fn):
        try:
            self.send_json(fn())
        except SteamError as e:
            self.send_json({"error": str(e)}, e.status)
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": f"Unexpected error: {e}"}, 500)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        force = q.get("refresh", ["0"])[0] == "1"
        if u.path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/ping":
            tab = q.get("tab", [""])[0][:64]
            if tab:
                with _tabs_lock:
                    _tabs[tab] = time.time()
            self.send_json({"ok": True})
        elif u.path == "/api/config":
            cfg = load_config()
            self.send_json({"configured": bool(cfg.get("api_key") and cfg.get("steam_id")),
                            "steam_id": cfg.get("steam_id")})
        elif u.path == "/api/friends":
            self.handle_api(lambda: get_friends(force))
        elif u.path == "/api/games":
            ids = [i for i in q.get("ids", [""])[0].split(",") if re.fullmatch(r"\d{17}", i)]
            if not ids:
                return self.send_json({"error": "ids required"}, 400)

            def fetch_all():
                with ThreadPoolExecutor(max_workers=6) as ex:
                    results = list(ex.map(lambda i: _safe_games(i, force), ids))
                return {"libraries": results}

            self.handle_api(fetch_all)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        # Only accept same-origin requests so other websites can't rewrite the config.
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"):
            return self.send_json({"error": "forbidden"}, 403)
        if u.path == "/api/bye":
            tab = urllib.parse.parse_qs(u.query).get("tab", [""])[0]
            with _tabs_lock:
                _tabs.pop(tab, None)
            return self.send_json({"ok": True})
        if u.path != "/api/config":
            return self.send_json({"error": "not found"}, 404)
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self.send_json({"error": "bad json"}, 400)

        def apply():
            cfg = load_config()
            key = (data.get("api_key") or "").strip()
            if key:
                if not re.fullmatch(r"[0-9A-Fa-f]{32}", key):
                    raise SteamError("That doesn't look like a Steam API key (32 hex characters)", 400)
                cfg["api_key"] = key
                save_config(cfg)
            who = (data.get("steam_id") or "").strip()
            if who:
                cfg["steam_id"] = resolve_steam_id(who)
                save_config(cfg)
            with _cache_lock:
                _cache.clear()
            return {"configured": bool(cfg.get("api_key") and cfg.get("steam_id")), "steam_id": cfg.get("steam_id")}

        self.handle_api(apply)


def _safe_games(steamid, force):
    try:
        return get_games(steamid, force)
    except SteamError as e:
        return {"steamid": steamid, "private": False, "games": [], "error": str(e)}


def watch_tabs(srv):
    started = time.time()
    empty_since = None
    while True:
        time.sleep(2)
        now = time.time()
        with _tabs_lock:
            for t, seen in list(_tabs.items()):
                if now - seen > TAB_TIMEOUT:
                    del _tabs[t]
            alive = bool(_tabs)
        if alive:
            empty_since = None
            continue
        if now - started < STARTUP_GRACE:
            continue
        empty_since = empty_since or now
        if now - empty_since >= EMPTY_GRACE:
            print("No open tabs — shutting down.")
            srv.shutdown()
            return


def main():
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        # Already running — just open the browser on the existing instance.
        webbrowser.open(f"http://{HOST}:{PORT}/")
        return
    url = f"http://{HOST}:{PORT}/"
    print(f"Steam Common Games running at {url}  (Ctrl+C to stop)")
    if "--keep-alive" not in sys.argv:
        threading.Thread(target=watch_tabs, args=(srv,), daemon=True).start()
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
