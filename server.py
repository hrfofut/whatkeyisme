#!/usr/bin/env python3
"""
Now Playing · Key — tiny local server.

  * Serves index.html at http://127.0.0.1:5000 (Spotify only allows 127.0.0.1, not "localhost").
  * Saves your Spotify Client ID + GetSongBPM API key to config.json (via the setup screen).
  * /api/key?title=...&artist=...  looks a song up on GetSongBPM and returns its key / BPM.
    (Done server-side so your GetSongBPM key stays private and browser CORS isn't an issue.)

No dependencies — standard-library Python 3.8+.

    python3 server.py            # then open http://127.0.0.1:5000
    PORT=8888 python3 server.py  # different port (register the matching redirect URI)
"""
import difflib
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(HERE, "index.html")
CONFIG_PATH = os.path.join(HERE, "config.json")
CACHE_PATH = os.path.join(HERE, "key_cache.json")

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "5000"))

# GetSongBPM has used both hostnames; we try them in order and remember whichever answers.
UPSTREAMS = [h.rstrip("/") for h in os.environ.get(
    "GETSONGBPM_HOSTS", "https://api.getsong.co,https://api.getsongbpm.com").split(",") if h.strip()]
UA = "Mozilla/5.0 (X11; Linux x86_64) NowPlayingKey/1.0"
NOT_FOUND_TTL = 3 * 24 * 3600  # re-try songs that weren't found after 3 days

_lock = threading.Lock()
_good_host = None


# ---------------------------------------------------------------- config / cache
def load_config():
    cfg = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except (OSError, ValueError):
        pass
    if os.environ.get("SPOTIFY_CLIENT_ID"):
        cfg["spotify_client_id"] = os.environ["SPOTIFY_CLIENT_ID"]
    if os.environ.get("GETSONGBPM_API_KEY"):
        cfg["getsongbpm_api_key"] = os.environ["GETSONGBPM_API_KEY"]
    return cfg


def save_config(updates):
    with _lock:
        cfg = {}
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except (OSError, ValueError):
            pass
        cfg.update(updates)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)


def _load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


_cache = _load_cache()


def cache_get(k):
    with _lock:
        v = _cache.get(k)
    if not v:
        return None
    if not v.get("found") and time.time() - v.get("at", 0) > NOT_FOUND_TTL:
        return None
    return v


def cache_put(k, v):
    with _lock:
        _cache[k] = v
        try:
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_cache, f)
            os.replace(tmp, CACHE_PATH)
        except OSError:
            pass


# ---------------------------------------------------------------- matching helpers
def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^the ", "", s)


_PAREN_JUNK = re.compile(
    r"\s*[\(\[][^\)\]]*\b(feat|ft|with|remaster(ed)?|version|edit|mono|stereo|live|deluxe|bonus|"
    r"explicit|clean|from|anniversary|re-?recorded)\b[^\)\]]*[\)\]]", re.I)
_DASH_JUNK = re.compile(
    r"\s+-\s+.*\b(remaster(ed)?|version|edit|mono|stereo|live|mix|from|single|anniversary|"
    r"re-?recorded|bonus)\b.*$", re.I)


def clean_title(t):
    out = _DASH_JUNK.sub("", _PAREN_JUNK.sub("", t or "")).strip()
    return out or (t or "").strip()


def ratio(a, b):
    if not a or not b:
        return 0.0
    r = difflib.SequenceMatcher(None, a, b).ratio()
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        r = max(r, 0.9)
    return r


def results_of(data):
    s = data.get("search") if isinstance(data, dict) else None
    return s if isinstance(s, list) else []


def artist_names(item):
    a = item.get("artist")
    if isinstance(a, dict):
        return [a.get("name", "")]
    if isinstance(a, list):
        return [x.get("name", "") if isinstance(x, dict) else str(x) for x in a]
    return [str(a)] if a else []


def title_of(item):
    return item.get("song_title") or item.get("title") or ""


def best_match(items, want_title, want_artists):
    wt = norm(clean_title(want_title))
    wa = [norm(a) for a in want_artists if a]
    best = None
    for it in items:
        if not isinstance(it, dict):
            continue
        tr = ratio(norm(clean_title(title_of(it))), wt)
        ar = max((ratio(norm(n), w) for n in artist_names(it) for w in wa), default=0.0)
        if ar < 0.8 or tr < 0.6:
            continue
        sc = tr + ar
        if best is None or sc > best[0]:
            best = (sc, tr, ar, it)
    return best


# ---------------------------------------------------------------- GetSongBPM client
class UpstreamError(Exception):
    pass


def gsb_get(path, params, api_key):
    global _good_host
    hosts = ([_good_host] if _good_host else []) + [h for h in UPSTREAMS if h != _good_host]
    errors = []
    for host in hosts:
        url = host + path + "?" + urllib.parse.urlencode({**params, "api_key": api_key})
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept": "application/json", "X-API-KEY": api_key})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            _good_host = host
            return data
        except urllib.error.HTTPError as e:
            errors.append(f"{host} → HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
            errors.append(f"{host} → {getattr(e, 'reason', e)}")
    raise UpstreamError("; ".join(errors) or "no upstream hosts configured")


def lookup_key(title, artists, api_key):
    primary = artists[0] if artists else ""
    clean = clean_title(title)
    attempts = []
    if primary:
        attempts.append(("both", f"song:{clean} artist:{primary}"))
    attempts.append(("song", clean))
    for extra in artists[1:3]:
        attempts.append(("both", f"song:{clean} artist:{extra}"))

    for typ, lookup in attempts:
        data = gsb_get("/search/", {"type": typ, "lookup": lookup}, api_key)
        m = best_match(results_of(data), title, artists)
        if not m:
            continue
        _, tr, ar, it = m
        song_id = it.get("song_id") or it.get("id")
        if not it.get("key_of") and song_id:  # search hit without key → fetch full song record
            try:
                detail = gsb_get("/song/", {"id": song_id}, api_key).get("song") or {}
                it = {**it, **{k: v for k, v in detail.items() if v}}
            except UpstreamError:
                pass
        if not (it.get("key_of") or it.get("camelot") or it.get("open_key")):
            continue
        return {
            "found": True,
            "key_of": it.get("key_of") or "",
            "camelot": it.get("camelot") or it.get("open_key") or "",
            "tempo": it.get("tempo") or "",
            "time_sig": it.get("time_sig") or "",
            "matched_title": title_of(it),
            "matched_artist": ", ".join(n for n in artist_names(it) if n),
            "uri": it.get("song_uri") or it.get("uri") or "https://getsongbpm.com",
            "confidence": "high" if tr >= 0.9 and ar >= 0.9 else "medium",
            "at": time.time(),
        }
    return {"found": False, "at": time.time()}


# ---------------------------------------------------------------- HTTP handler
class Handler(BaseHTTPRequestHandler):
    server_version = "NowPlayingKey/1.0"

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            sys.stderr.write("  %s\n" % (fmt % args))

    # Only answer requests addressed to us on loopback (blocks DNS-rebinding tricks).
    def _host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{PORT}", f"[::1]:{PORT}", f"localhost:{PORT}")

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        return origin is None or origin in (f"http://127.0.0.1:{PORT}", f"http://[::1]:{PORT}")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad_host"})
        url = urllib.parse.urlsplit(self.path)
        path = url.path

        if path in ("/", "/index.html", "/callback"):
            try:
                with open(INDEX_PATH, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                return self._send(500, {"error": "index.html missing next to server.py"})

        if path == "/api/config":
            cfg = load_config()
            return self._send(200, {
                "clientId": cfg.get("spotify_client_id", ""),
                "hasKeyApi": bool(cfg.get("getsongbpm_api_key")),
            })

        if path == "/api/key":
            if not self._origin_ok():
                return self._send(403, {"error": "bad_origin"})
            q = urllib.parse.parse_qs(url.query)
            title = (q.get("title") or [""])[0].strip()
            artists = [a.strip() for a in (q.get("artists") or q.get("artist") or [""])[0].split("|") if a.strip()]
            if not title:
                return self._send(400, {"error": "missing_title", "message": "title is required"})
            api_key = load_config().get("getsongbpm_api_key")
            if not api_key:
                return self._send(200, {"error": "no_api_key", "message": "No GetSongBPM API key saved yet."})
            ck = norm(title) + "|" + norm(artists[0] if artists else "")
            hit = cache_get(ck)
            if hit:
                return self._send(200, {**hit, "cached": True})
            try:
                res = lookup_key(title, artists, api_key)
            except UpstreamError as e:
                return self._send(200, {"error": "upstream", "message": f"GetSongBPM unreachable or key rejected ({e})"})
            cache_put(ck, res)
            return self._send(200, res)

        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._send(403, {"error": "forbidden"})
        if urllib.parse.urlsplit(self.path).path != "/api/config":
            return self._send(404, {"error": "not_found"})
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 10_000)
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad_json"})
        updates = {}
        for k in ("spotify_client_id", "getsongbpm_api_key"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                if not re.fullmatch(r"[A-Za-z0-9_\-]{8,128}", v.strip()):
                    return self._send(400, {"error": "bad_value", "message": f"{k} doesn't look right"})
                updates[k] = v.strip()
        if updates:
            save_config(updates)
        return self._send(200, {"ok": True})


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    cfg = load_config()
    print(f"\n  ♪  Now Playing · Key  →  http://{HOST}:{PORT}\n")
    print(f"     Spotify redirect URI to register:  http://{HOST}:{PORT}/callback")
    print(f"     Spotify Client ID:   {'saved' if cfg.get('spotify_client_id') else 'not set (use the setup screen)'}")
    print(f"     GetSongBPM API key:  {'saved' if cfg.get('getsongbpm_api_key') else 'not set (key lookups disabled)'}")
    print("\n  Ctrl+C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  bye ♪")


if __name__ == "__main__":
    main()
