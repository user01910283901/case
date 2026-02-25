import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
PROGRESS_DIR = ROOT / "progress"
PROGRESS_FILE = PROGRESS_DIR / "progress.json"
PASSIVE_MS = 300000
PASSIVE_ADD = 5
SELL_MS = 180000
REQ_LIMIT_PER_MIN = 120

CASES = {
    "wood": {"price": 5, "drops": [{"item": "bear", "count": 5}, {"item": "heart", "count": 5}, {"item": "flower", "count": 1}]},
    "metal": {"price": 16, "drops": [{"item": "flower", "count": 5}, {"item": "gift", "count": 5}, {"item": "cake", "count": 1}]},
    "gold": {"price": 40, "drops": [{"item": "cake", "count": 5}, {"item": "rocket", "count": 5}, {"item": "gold_cup", "count": 1}]},
    "diamond": {"price": 400, "drops": [{"item": "gold_cup", "count": 5}, {"item": "rocket", "count": 2}, {"item": "gift", "count": 4}, {"item": "candy", "count": 1}]},
    "divine": {"price": 15000, "drops": [{"item": "clock", "count": 5}, {"item": "pink_bear", "count": 5}, {"item": "gold_muscle", "count": 1}]},
}

ITEM_STARS = {
    "bear": 15,
    "heart": 15,
    "flower": 25,
    "gift": 50,
    "cake": 50,
    "rocket": 50,
    "gold_cup": 100,
    "candy": 280,
    "gold_muscle": 18751,
    "pink_bear": 3995,
    "clock": 4791,
}

STATE_LOCK = threading.Lock()
RATE_LOCK = threading.Lock()
RATE = {}


def now_ms():
    return int(time.time() * 1000)


def ensure_progress():
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    if not PROGRESS_FILE.exists():
        PROGRESS_FILE.write_text(json.dumps({"profiles": {}, "active_by_client": {}}, ensure_ascii=False, indent=2), encoding="utf-8")


def read_state():
    ensure_progress()
    try:
        data = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {"profiles": {}, "active_by_client": {}}
    if not isinstance(data, dict):
        data = {"profiles": {}, "active_by_client": {}}
    data.setdefault("profiles", {})
    data.setdefault("active_by_client", {})
    return data


def write_state(data):
    ensure_progress()
    PROGRESS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def shape_profile(p):
    p.setdefault("inventory", [])
    p.setdefault("stars", 15)
    p.setdefault("createdAt", now_ms())
    p.setdefault("lastPassiveAt", p["createdAt"])
    p.setdefault("totalEarned", 0)
    p.setdefault("totalSpent", 0)
    p.setdefault("opened", 0)
    p.setdefault("bought", 0)


def random_item(case_key):
    pool = []
    for d in CASES[case_key]["drops"]:
        pool.extend([d["item"]] * d["count"])
    return random.choice(pool)


def normalize_client_id(client_id):
    if not isinstance(client_id, str):
        return ""
    x = client_id.strip()
    if len(x) < 8 or len(x) > 80:
        return ""
    for ch in x:
        if ch.isalnum() or ch in "-_":
            continue
        return ""
    return x


def generate_profile_id(profiles):
    while True:
        pid = str(random.randint(100000, 999999))
        if pid not in profiles:
            return pid


def set_online_touch(profile, t):
    if t > int(profile.get("lastPassiveAt", t)):
        profile["lastPassiveAt"] = t


def apply_visible_tick(profile, t):
    last = int(profile.get("lastPassiveAt", t))
    passed = t - last
    n = passed // PASSIVE_MS
    gain = 0
    if n > 0:
        gain = n * PASSIVE_ADD
        profile["stars"] += gain
        profile["totalEarned"] += gain
        profile["lastPassiveAt"] = last + n * PASSIVE_MS
    return gain


def active_profile(state, client_id):
    profile_id = state["active_by_client"].get(client_id)
    if not profile_id:
        return None
    p = state["profiles"].get(profile_id)
    if p:
        shape_profile(p)
    return p


def state_for_client(state, client_id):
    profile_id = state["active_by_client"].get(client_id)
    for p in state["profiles"].values():
        shape_profile(p)
    return {
        "activeProfileId": profile_id if profile_id in state["profiles"] else None,
        "profiles": state["profiles"],
    }


def too_many_requests(ip):
    t = time.time()
    with RATE_LOCK:
        arr = RATE.get(ip, [])
        arr = [x for x in arr if t - x < 60]
        if len(arr) >= REQ_LIMIT_PER_MIN:
            RATE[ip] = arr
            return True
        arr.append(t)
        RATE[ip] = arr
        return False


def json_response(handler, status, payload):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def parse_json(handler):
    try:
        n = int(handler.headers.get("Content-Length", "0"))
    except Exception:
        n = 0
    if n <= 0 or n > 1024 * 64:
        return {}
    raw = handler.rfile.read(n)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def safe_path(url_path):
    p = unquote(url_path.split("?", 1)[0]).lstrip("/")
    if not p:
        p = "index.html"
    target = (ROOT / p).resolve()
    try:
        target.relative_to(ROOT)
    except Exception:
        return None
    return target


class Handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()

    def do_GET(self):
        if too_many_requests(self.client_address[0]):
            return json_response(self, 429, {"ok": False, "error": "Rate limit"})

        u = urlparse(self.path)
        if u.path == "/api/health":
            return json_response(self, 200, {"ok": True, "server": "secure_server"})
        if u.path == "/api/state":
            q = parse_qs(u.query)
            client_id = normalize_client_id((q.get("client_id") or [""])[0])
            if not client_id:
                return json_response(self, 400, {"ok": False, "error": "Bad client_id"})
            with STATE_LOCK:
                state = read_state()
                payload = state_for_client(state, client_id)
            return json_response(self, 200, {"ok": True, "state": payload})

        f = safe_path(u.path)
        if not f or not f.exists() or not f.is_file():
            self.send_error(404)
            return
        ctype = "text/plain; charset=utf-8"
        if f.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        elif f.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif f.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif f.suffix == ".png":
            ctype = "image/png"
        elif f.suffix == ".svg":
            ctype = "image/svg+xml"
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if too_many_requests(self.client_address[0]):
            return json_response(self, 429, {"ok": False, "error": "Rate limit"})

        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            self.send_error(404)
            return

        body = parse_json(self)
        client_id = normalize_client_id(body.get("client_id", ""))
        if not client_id:
            return json_response(self, 400, {"ok": False, "error": "Bad client_id"})

        t = now_ms()
        with STATE_LOCK:
            state = read_state()

            if u.path == "/api/create":
                nick = str(body.get("nick", "")).strip()
                if not nick or len(nick) < 3 or len(nick) > 20:
                    return json_response(self, 400, {"ok": False, "error": "Bad nick"})
                for ch in nick:
                    if not (ch.isascii() and (ch.isalpha() or ch.isdigit() or ch == "_")):
                        return json_response(self, 400, {"ok": False, "error": "Nick only A-Z 0-9 _"})
                low = nick.lower()
                if any(str(p.get("nick", "")).lower() == low for p in state["profiles"].values()):
                    return json_response(self, 409, {"ok": False, "error": "Nick exists"})

                profile_id = generate_profile_id(state["profiles"])
                profile = {
                    "id": profile_id,
                    "nick": nick,
                    "stars": 15,
                    "inventory": [],
                    "createdAt": t,
                    "lastPassiveAt": t,
                    "totalEarned": 0,
                    "totalSpent": 0,
                    "opened": 0,
                    "bought": 0,
                }
                state["profiles"][profile_id] = profile
                state["active_by_client"][client_id] = profile_id
                write_state(state)
                return json_response(self, 200, {"ok": True, "state": state_for_client(state, client_id)})

            profile = active_profile(state, client_id)
            if not profile:
                return json_response(self, 401, {"ok": False, "error": "No active profile"})

            if u.path == "/api/tick":
                visible = bool(body.get("visible", False))
                gained = 0
                if visible:
                    gained = apply_visible_tick(profile, t)
                else:
                    set_online_touch(profile, t)
                write_state(state)
                return json_response(self, 200, {"ok": True, "gained": gained, "state": state_for_client(state, client_id)})

            if u.path == "/api/buy":
                case_key = str(body.get("case_key", ""))
                if case_key not in CASES:
                    return json_response(self, 400, {"ok": False, "error": "Bad case"})
                set_online_touch(profile, t)
                price = CASES[case_key]["price"]
                if profile["stars"] < price:
                    return json_response(self, 400, {"ok": False, "error": "Not enough stars"})
                profile["stars"] -= price
                profile["totalSpent"] += price
                profile["bought"] += 1
                inv_id = f"{random.randint(100000, 999999):06d}{int(time.time() * 1000)}"
                profile["inventory"].append({"id": inv_id, "caseKey": case_key, "boughtAt": t, "source": "buy"})
                write_state(state)
                return json_response(self, 200, {"ok": True, "state": state_for_client(state, client_id)})

            if u.path == "/api/open":
                inv_id = str(body.get("inventory_id", ""))
                set_online_touch(profile, t)
                idx = next((i for i, x in enumerate(profile["inventory"]) if x.get("id") == inv_id), -1)
                if idx < 0:
                    return json_response(self, 404, {"ok": False, "error": "Case not found"})
                inv = profile["inventory"][idx]
                case_key = inv.get("caseKey")
                if case_key not in CASES:
                    return json_response(self, 400, {"ok": False, "error": "Bad case state"})
                winner = random_item(case_key)
                prize = ITEM_STARS[winner]
                profile["inventory"].pop(idx)
                profile["stars"] += prize
                profile["totalEarned"] += prize
                profile["opened"] += 1
                write_state(state)
                return json_response(self, 200, {"ok": True, "winner": winner, "stars": prize, "state": state_for_client(state, client_id)})

            if u.path == "/api/sell":
                inv_id = str(body.get("inventory_id", ""))
                set_online_touch(profile, t)
                idx = next((i for i, x in enumerate(profile["inventory"]) if x.get("id") == inv_id), -1)
                if idx < 0:
                    return json_response(self, 404, {"ok": False, "error": "Case not found"})
                inv = profile["inventory"][idx]
                if t - int(inv.get("boughtAt", t)) > SELL_MS:
                    return json_response(self, 400, {"ok": False, "error": "Sell window closed"})
                case_key = inv.get("caseKey")
                if case_key not in CASES:
                    return json_response(self, 400, {"ok": False, "error": "Bad case state"})
                price = CASES[case_key]["price"]
                profile["inventory"].pop(idx)
                profile["stars"] += price
                write_state(state)
                return json_response(self, 200, {"ok": True, "state": state_for_client(state, client_id)})

            if u.path == "/api/gift":
                inv_id = str(body.get("inventory_id", ""))
                target_id = str(body.get("target_id", "")).strip()
                set_online_touch(profile, t)
                if not target_id.isdigit() or len(target_id) != 6 or target_id not in state["profiles"]:
                    return json_response(self, 400, {"ok": False, "error": "Target not found"})
                if target_id == profile["id"]:
                    return json_response(self, 400, {"ok": False, "error": "Cannot gift to self"})

                idx = next((i for i, x in enumerate(profile["inventory"]) if x.get("id") == inv_id), -1)
                if idx < 0:
                    return json_response(self, 404, {"ok": False, "error": "Case not found"})
                inv = profile["inventory"].pop(idx)
                target = state["profiles"][target_id]
                shape_profile(target)
                new_id = f"{random.randint(100000, 999999):06d}{int(time.time() * 1000)}"
                target["inventory"].append({
                    "id": new_id,
                    "caseKey": inv.get("caseKey"),
                    "boughtAt": t,
                    "source": "gift",
                    "giftFrom": profile["id"],
                })
                write_state(state)
                return json_response(self, 200, {"ok": True, "state": state_for_client(state, client_id)})

            return json_response(self, 404, {"ok": False, "error": "Unknown endpoint"})

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ensure_progress()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Secure server started on http://127.0.0.1:{port}")
    server.serve_forever()
