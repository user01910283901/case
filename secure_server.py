#!/usr/bin/env python3
import json
import mimetypes
import os
import random
import re
import threading
import time
import uuid
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent
INDEX_PATH = BASE_DIR / "index.html"
ICONS_DIR = BASE_DIR / "Icons"
DATA_PATH = BASE_DIR / "progress" / "progress.json"

HOST = os.getenv("CASE_HOST", "0.0.0.0")
PORT = int(os.getenv("PORT") or os.getenv("CASE_PORT", "8080"))
RATE_LIMIT_RPM = int(os.getenv("CASE_RATE_LIMIT_RPM", "120"))
DEV_TOKEN = os.getenv("CASE_DEV_TOKEN", "")

PASSIVE_MS = 300000
PASSIVE_ADD = 5
SELL_MS = 180000

ITEMS = {
    "bear": {"s": 15},
    "heart": {"s": 15},
    "flower": {"s": 25},
    "gift": {"s": 50},
    "cake": {"s": 50},
    "rocket": {"s": 50},
    "gold_cup": {"s": 100},
    "candy": {"s": 280},
    "gold_muscle": {"s": 18751},
    "pink_bear": {"s": 3995},
    "clock": {"s": 4791},
    "emerald_bear": {"s": 310},
    "emerald_muscle": {"s": 320},
    "emerald_kettle": {"s": 400},
    "crystal_cube": {"s": 610},
    "crystal_candy": {"s": 612},
    "crystal_cake": {"s": 700},
}

CASES = {
    "wood": {"p": 5, "d": [{"i": "bear", "c": 5}, {"i": "heart", "c": 5}, {"i": "flower", "c": 1}]},
    "metal": {"p": 16, "d": [{"i": "flower", "c": 5}, {"i": "gift", "c": 5}, {"i": "cake", "c": 1}]},
    "gold": {"p": 40, "d": [{"i": "cake", "c": 5}, {"i": "rocket", "c": 5}, {"i": "gold_cup", "c": 1}]},
    "diamond": {"p": 400, "d": [{"i": "gold_cup", "c": 5}, {"i": "rocket", "c": 2}, {"i": "gift", "c": 4}, {"i": "candy", "c": 1}]},
    "emerald": {"p": 300, "d": [{"i": "emerald_bear", "c": 10}, {"i": "emerald_muscle", "c": 4}, {"i": "emerald_kettle", "c": 1}]},
    "crystal": {"p": 600, "d": [{"i": "crystal_cube", "c": 10}, {"i": "crystal_candy", "c": 6}, {"i": "crystal_cake", "c": 1}]},
    "divine": {"p": 3000, "d": [{"i": "clock", "c": 5}, {"i": "pink_bear", "c": 5}, {"i": "gold_muscle", "c": 1}]},
}

WELCOME_REWARD = {"1": 200, "2": 100, "3": 40}
NICK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,19}$")

STATE_LOCK = threading.Lock()
STATE = {"profiles": {}, "active_by_client": {}}
RATE_LOCK = threading.Lock()
RATE_BUCKET = {}


def now_ms() -> int:
    return int(time.time() * 1000)


def ensure_storage() -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_PATH.exists():
        DATA_PATH.write_text(json.dumps({"profiles": {}, "active_by_client": {}}, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state() -> dict:
    ensure_storage()
    try:
        raw = DATA_PATH.read_text(encoding="utf-8")
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("invalid root")
        if not isinstance(obj.get("profiles"), dict):
            obj["profiles"] = {}
        if not isinstance(obj.get("active_by_client"), dict):
            obj["active_by_client"] = {}
        for pid, prof in list(obj["profiles"].items()):
            if not isinstance(prof, dict):
                del obj["profiles"][pid]
                continue
            shape_profile(prof, pid)
        return obj
    except Exception:
        return {"profiles": {}, "active_by_client": {}}


def save_state(state: dict) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DATA_PATH)


def shape_profile(profile: dict, pid: str) -> None:
    ts = now_ms()
    profile["id"] = str(profile.get("id") or pid)
    profile["nick"] = str(profile.get("nick") or "Player")
    profile["stars"] = int(profile.get("stars", 15))
    profile["inventory"] = profile.get("inventory") if isinstance(profile.get("inventory"), list) else []
    profile["createdAt"] = int(profile.get("createdAt", ts))
    profile["lastPassiveAt"] = int(profile.get("lastPassiveAt", profile["createdAt"]))
    profile["totalEarned"] = int(profile.get("totalEarned", 0))
    profile["totalSpent"] = int(profile.get("totalSpent", 0))
    profile["opened"] = int(profile.get("opened", 0))
    profile["bought"] = int(profile.get("bought", 0))
    profile["welcomeClaimed"] = bool(profile.get("welcomeClaimed", False))
    profile["welcomeChoice"] = str(profile.get("welcomeChoice", "")) if profile.get("welcomeChoice") else None
    for item in profile["inventory"]:
        if not isinstance(item, dict):
            continue
        item["id"] = str(item.get("id") or make_inv_id())
        item["caseKey"] = str(item.get("caseKey") or "wood")
        item["boughtAt"] = int(item.get("boughtAt") or ts)
        item["source"] = str(item.get("source") or "buy")
        if "giftFrom" in item and item["giftFrom"] is not None:
            item["giftFrom"] = str(item["giftFrom"])


def make_public_state(state: dict, client_id: str) -> dict:
    profiles = {}
    for pid, prof in state["profiles"].items():
        shape_profile(prof, pid)
        profiles[pid] = deepcopy(prof)
    active = state["active_by_client"].get(client_id)
    if active not in profiles:
        active = None
    return {"activeProfileId": active, "profiles": profiles}


def ensure_rate_limit(ip: str) -> tuple[bool, str]:
    ts = int(time.time())
    with RATE_LOCK:
        arr = RATE_BUCKET.get(ip, [])
        arr = [x for x in arr if ts - x < 60]
        if len(arr) >= RATE_LIMIT_RPM:
            RATE_BUCKET[ip] = arr
            return False, "Слишком много запросов. Подождите минуту."
        arr.append(ts)
        RATE_BUCKET[ip] = arr
    return True, ""


def get_profile_for_client(state: dict, client_id: str) -> tuple[str | None, dict | None]:
    pid = state["active_by_client"].get(client_id)
    if not pid:
        return None, None
    prof = state["profiles"].get(pid)
    if not prof:
        return None, None
    shape_profile(prof, pid)
    return pid, prof


def make_profile(state: dict, nick: str) -> dict:
    pid = new_profile_id(state)
    ts = now_ms()
    return {
        "id": pid,
        "nick": nick,
        "stars": 15,
        "inventory": [],
        "createdAt": ts,
        "lastPassiveAt": ts,
        "totalEarned": 0,
        "totalSpent": 0,
        "opened": 0,
        "bought": 0,
        "welcomeClaimed": False,
        "welcomeChoice": None,
    }


def new_profile_id(state: dict) -> str:
    while True:
        pid = str(random.randint(100000, 999999))
        if pid not in state["profiles"]:
            return pid


def make_inv_id() -> str:
    return "c_" + uuid.uuid4().hex[:12]


def passive_tick(profile: dict, visible: bool = True) -> int:
    if not visible:
        profile["lastPassiveAt"] = now_ms()
        return 0
    now = now_ms()
    passed = max(0, now - int(profile.get("lastPassiveAt", now)))
    n = passed // PASSIVE_MS
    if n <= 0:
        return 0
    add = n * PASSIVE_ADD
    profile["stars"] += add
    profile["totalEarned"] += add
    profile["lastPassiveAt"] += n * PASSIVE_MS
    return int(add)


def weighted_pool(case_key: str) -> list[str]:
    case = CASES.get(case_key)
    if not case:
        return []
    pool = []
    for d in case["d"]:
        pool.extend([d["i"]] * int(d["c"]))
    return pool


def find_inventory_index(profile: dict, inv_id: str) -> int:
    for i, item in enumerate(profile["inventory"]):
        if str(item.get("id")) == inv_id:
            return i
    return -1


class Handler(BaseHTTPRequestHandler):
    server_version = "CaseStars/2.0"

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            self._handle_api_get(path)
            return
        self._handle_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            self._send_json(404, {"ok": False, "error": "Not Found"})
            return
        ok, msg = ensure_rate_limit(self.client_address[0])
        if not ok:
            self._send_json(429, {"ok": False, "error": msg})
            return
        self._handle_api_post(path)

    def log_message(self, fmt, *args):
        return

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _handle_static(self, path: str):
        p = unquote(path)
        if p in ("/", ""):
            file_path = INDEX_PATH
        else:
            rel = p.lstrip("/")
            file_path = (BASE_DIR / rel).resolve()
            if not str(file_path).startswith(str(BASE_DIR)):
                self.send_error(403)
                return
            if file_path.name == DATA_PATH.name and file_path.parent == DATA_PATH.parent:
                self.send_error(403)
                return
        if not file_path.exists() or not file_path.is_file():
            self.send_error(404)
            return
        ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        try:
            raw = file_path.read_bytes()
        except Exception:
            self.send_error(500)
            return
        self.send_response(200)
        if path.startswith("/api/"):
            self._cors_headers()
        self.send_header("Content-Type", ctype)
        if file_path == INDEX_PATH:
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle_api_get(self, path: str):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if path == "/api/health":
            self._send_json(200, {"ok": True, "version": "2.0"})
            return

        if path == "/api/state":
            client_id = str((qs.get("client_id") or [""])[0]).strip()
            if not client_id:
                self._send_json(400, {"ok": False, "error": "client_id обязателен"})
                return
            with STATE_LOCK:
                st = make_public_state(STATE, client_id)
            self._send_json(200, {"ok": True, "state": st})
            return

        self._send_json(404, {"ok": False, "error": "Unknown endpoint"})

    def _handle_api_post(self, path: str):
        payload = self._read_json()
        client_id = str(payload.get("client_id") or "").strip()

        if path == "/api/create":
            nick = str(payload.get("nick") or "").strip()
            if not client_id:
                self._send_json(400, {"ok": False, "error": "client_id обязателен"})
                return
            if not NICK_RE.match(nick):
                self._send_json(400, {"ok": False, "error": "Ник 3-20 символов: A-Z, 0-9, _."})
                return
            with STATE_LOCK:
                for prof in STATE["profiles"].values():
                    if str(prof.get("nick", "")).lower() == nick.lower():
                        self._send_json(409, {"ok": False, "error": "Такой ник уже занят."})
                        return
                profile = make_profile(STATE, nick)
                STATE["profiles"][profile["id"]] = profile
                STATE["active_by_client"][client_id] = profile["id"]
                save_state(STATE)
                st = make_public_state(STATE, client_id)
            self._send_json(200, {"ok": True, "state": st})
            return

        if path == "/api/tick":
            visible = bool(payload.get("visible", True))
            if not client_id:
                self._send_json(400, {"ok": False, "error": "client_id обязателен"})
                return
            with STATE_LOCK:
                _, prof = get_profile_for_client(STATE, client_id)
                if not prof:
                    self._send_json(404, {"ok": False, "error": "Профиль не найден"})
                    return
                gained = passive_tick(prof, visible=visible)
                if gained > 0:
                    save_state(STATE)
                st = make_public_state(STATE, client_id)
            self._send_json(200, {"ok": True, "gained": gained, "state": st})
            return

        if path == "/api/buy":
            case_key = str(payload.get("case_key") or "").strip()
            if case_key not in CASES:
                self._send_json(400, {"ok": False, "error": "Неизвестный кейс"})
                return
            if not client_id:
                self._send_json(400, {"ok": False, "error": "client_id обязателен"})
                return
            with STATE_LOCK:
                _, prof = get_profile_for_client(STATE, client_id)
                if not prof:
                    self._send_json(404, {"ok": False, "error": "Профиль не найден"})
                    return
                passive_tick(prof, visible=True)
                price = int(CASES[case_key]["p"])
                if prof["stars"] < price:
                    self._send_json(400, {"ok": False, "error": "Недостаточно звезд."})
                    return
                prof["stars"] -= price
                prof["totalSpent"] += price
                prof["bought"] += 1
                prof["inventory"].append(
                    {
                        "id": make_inv_id(),
                        "caseKey": case_key,
                        "boughtAt": now_ms(),
                        "source": "buy",
                    }
                )
                save_state(STATE)
                st = make_public_state(STATE, client_id)
            self._send_json(200, {"ok": True, "state": st})
            return

        if path == "/api/open":
            inv_id = str(payload.get("inventory_id") or "").strip()
            if not inv_id:
                self._send_json(400, {"ok": False, "error": "inventory_id обязателен"})
                return
            if not client_id:
                self._send_json(400, {"ok": False, "error": "client_id обязателен"})
                return
            with STATE_LOCK:
                _, prof = get_profile_for_client(STATE, client_id)
                if not prof:
                    self._send_json(404, {"ok": False, "error": "Профиль не найден"})
                    return
                idx = find_inventory_index(prof, inv_id)
                if idx == -1:
                    self._send_json(404, {"ok": False, "error": "Кейс не найден"})
                    return
                item = prof["inventory"][idx]
                case_key = str(item.get("caseKey") or "")
                pool = weighted_pool(case_key)
                if not pool:
                    self._send_json(400, {"ok": False, "error": "Некорректный кейс"})
                    return
                winner = random.choice(pool)
                reward = int(ITEMS[winner]["s"])
                prof["inventory"].pop(idx)
                prof["stars"] += reward
                prof["totalEarned"] += reward
                prof["opened"] += 1
                save_state(STATE)
                st = make_public_state(STATE, client_id)
            self._send_json(200, {"ok": True, "winner": winner, "state": st})
            return

        if path == "/api/gift":
            inv_id = str(payload.get("inventory_id") or "").strip()
            target_id = str(payload.get("target_id") or "").strip()
            if not inv_id or not target_id:
                self._send_json(400, {"ok": False, "error": "inventory_id и target_id обязательны"})
                return
            if not client_id:
                self._send_json(400, {"ok": False, "error": "client_id обязателен"})
                return
            with STATE_LOCK:
                pid, from_prof = get_profile_for_client(STATE, client_id)
                if not from_prof or not pid:
                    self._send_json(404, {"ok": False, "error": "Профиль не найден"})
                    return
                if target_id == pid:
                    self._send_json(400, {"ok": False, "error": "Нельзя подарить кейс самому себе."})
                    return
                to_prof = STATE["profiles"].get(target_id)
                if not to_prof:
                    self._send_json(404, {"ok": False, "error": "ID не найден."})
                    return
                idx = find_inventory_index(from_prof, inv_id)
                if idx == -1:
                    self._send_json(404, {"ok": False, "error": "Кейс не найден"})
                    return
                sent = from_prof["inventory"].pop(idx)
                shape_profile(to_prof, target_id)
                to_prof["inventory"].append(
                    {
                        "id": make_inv_id(),
                        "caseKey": sent.get("caseKey"),
                        "boughtAt": now_ms(),
                        "source": "gift",
                        "giftFrom": pid,
                    }
                )
                save_state(STATE)
                st = make_public_state(STATE, client_id)
            self._send_json(200, {"ok": True, "state": st})
            return

        if path == "/api/sell":
            inv_id = str(payload.get("inventory_id") or "").strip()
            if not inv_id:
                self._send_json(400, {"ok": False, "error": "inventory_id обязателен"})
                return
            if not client_id:
                self._send_json(400, {"ok": False, "error": "client_id обязателен"})
                return
            with STATE_LOCK:
                _, prof = get_profile_for_client(STATE, client_id)
                if not prof:
                    self._send_json(404, {"ok": False, "error": "Профиль не найден"})
                    return
                idx = find_inventory_index(prof, inv_id)
                if idx == -1:
                    self._send_json(404, {"ok": False, "error": "Кейс не найден"})
                    return
                item = prof["inventory"][idx]
                bought_at = int(item.get("boughtAt", 0))
                if now_ms() - bought_at > SELL_MS:
                    self._send_json(400, {"ok": False, "error": "Продажа доступна только 3 минуты."})
                    return
                case_key = str(item.get("caseKey") or "")
                if case_key not in CASES:
                    self._send_json(400, {"ok": False, "error": "Некорректный кейс"})
                    return
                prof["inventory"].pop(idx)
                prof["stars"] += int(CASES[case_key]["p"])
                save_state(STATE)
                st = make_public_state(STATE, client_id)
            self._send_json(200, {"ok": True, "state": st})
            return

        if path == "/api/welcome":
            choice = str(payload.get("choice") or "").strip()
            if choice not in WELCOME_REWARD:
                self._send_json(400, {"ok": False, "error": "choice должен быть 1, 2 или 3"})
                return
            if not client_id:
                self._send_json(400, {"ok": False, "error": "client_id обязателен"})
                return
            with STATE_LOCK:
                _, prof = get_profile_for_client(STATE, client_id)
                if not prof:
                    self._send_json(404, {"ok": False, "error": "Профиль не найден"})
                    return
                if prof.get("welcomeClaimed"):
                    self._send_json(400, {"ok": False, "error": "Подарок уже выбран"})
                    return
                reward = int(WELCOME_REWARD[choice])
                prof["welcomeClaimed"] = True
                prof["welcomeChoice"] = choice
                prof["welcomeAt"] = now_ms()
                prof["stars"] += reward
                prof["totalEarned"] += reward
                save_state(STATE)
                st = make_public_state(STATE, client_id)
            self._send_json(200, {"ok": True, "reward": reward, "state": st})
            return

        if path == "/api/admin/balance_all":
            mode = str(payload.get("mode") or "").strip().lower()
            amount_raw = payload.get("amount")
            token = str(payload.get("dev_token") or "")
            if DEV_TOKEN and token != DEV_TOKEN:
                self._send_json(403, {"ok": False, "error": "Неверный dev_token"})
                return
            if mode not in {"add", "set"}:
                self._send_json(400, {"ok": False, "error": "mode должен быть add или set"})
                return
            try:
                amount = int(amount_raw)
            except Exception:
                self._send_json(400, {"ok": False, "error": "amount должен быть числом"})
                return
            with STATE_LOCK:
                affected = 0
                for pid, prof in STATE["profiles"].items():
                    shape_profile(prof, pid)
                    if mode == "add":
                        prof["stars"] = max(0, int(prof["stars"]) + amount)
                    else:
                        prof["stars"] = max(0, amount)
                    affected += 1
                save_state(STATE)
                st = make_public_state(STATE, client_id) if client_id else {"activeProfileId": None, "profiles": deepcopy(STATE["profiles"])}
            self._send_json(200, {"ok": True, "affected": affected, "state": st})
            return

        self._send_json(404, {"ok": False, "error": "Unknown endpoint"})


def run() -> None:
    global STATE
    with STATE_LOCK:
        STATE = load_state()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Case Stars server on http://{HOST}:{PORT}")
    print(f"Data file: {DATA_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
