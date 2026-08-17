import asyncio
import aiohttp
import json
import re
import time
import struct
import hashlib
import logging
import os
from copy import deepcopy
from html import escape
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

import zlib
import base64

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, BotCommand,
)

# ============================================================
#  CONFIG
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8991051291:AAEWtjtdhGeEl8iClIrvXC1Au95bg1csjlA")
OWNER_ID  = 8700382637

RATE_LIMIT_ACTIONS = 10
RATE_LIMIT_SECONDS = 60

FK       = "AIzaSyAe_aOVT1gSfmHKBrorFvX4fRwN5nODXVA"
LOAD_URL = "https://europe-west1-cp-multiplayer.cloudfunctions.net/GetPlayerRecords3"
SAVE_URL = "https://europe-west1-cp-multiplayer.cloudfunctions.net/SavePlayerRecordsPartially8"
RANK_URL = "https://us-central1-cp-multiplayer.cloudfunctions.net/SetUserRating4"

CAR_IDS = [59,133,132,13,53,99,100,102,37,21,48,77,74,2,23,51,163,186,158,55,
           60,61,62,63,64,65,66,67,68,69,70,71,72,73,75,76,78,79,80,81,82,83,
           84,85,86,87,88,89,90,91,92,93,94,95,96,97,98,101,103,104,105,106,
           107,108,109,110,111,112,113,114,115,116,117,118,119,120,121,122,123,
           124,125,126,127,128,129,130,131,134,135,136,137,138,139,140,141,142,
           143,144,145,146,147,148,149,150,151,152,153,154,155,156,157,159,160,
           161,162,164,165,166,167,168,169,170,171,172,173,174,175,176,177,178,
           179,180,181,182,183,184,185,187,188,189,190,191,192,193,194,195,196,
           197,198,199,200,201,202,203,204,205,206,207,208,209,210,211,212,213,
           214,215,216,217,218,219,220,221,222,223,224,225,226,227,228,229,230]

MAX_MONEY = 50_000_000
MAX_COIN  = 500_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("CPM")

# ============================================================
#  STORE
# ============================================================

STORE_PATH = Path("cpm_store.json")

DEFAULT_STORE = {
    "allowed_users": [], "vip_users": [], "admins": {},
    "pending": {}, "banned": [], "expiry": {},
    "stats": {"total_logins": 0, "total_actions": 0, "total_unlocks": 0},
    "admin_log": [], "users": {}, "daily_stats": {},
    "notes": {}, "warnings": {},
    "maintenance": False, "broadcast_history": [],
    "bot_photo": "",
}

def load_store():
    try:
        if STORE_PATH.exists():
            with open(STORE_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for k, v in DEFAULT_STORE.items():
                if k not in data:
                    data[k] = deepcopy(v)
            data["admins"]        = {str(k): v for k, v in data.get("admins", {}).items()}
            data["allowed_users"] = list({int(x) for x in data.get("allowed_users", [])})
            data["vip_users"]     = list({int(x) for x in data.get("vip_users", [])})
            data["banned"]        = list({int(x) for x in data.get("banned", [])})
            data["pending"]       = {str(k): v for k, v in data.get("pending", {}).items()}
            data["expiry"]        = {str(k): v for k, v in data.get("expiry", {}).items()}
            data["users"]         = {str(k): v for k, v in data.get("users", {}).items()}
            data["notes"]         = {str(k): v for k, v in data.get("notes", {}).items()}
            data["warnings"]      = {str(k): v for k, v in data.get("warnings", {}).items()}
            return data
    except Exception as e:
        log.error(f"Store load error: {e}")
    return deepcopy(DEFAULT_STORE)

def save_store(data):
    try:
        with open(STORE_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
        return True
    except Exception as e:
        log.error(f"Store save error: {e}")
        return False

STORE = load_store()
ALLOWED_USERS = STORE.get("allowed_users", [])
BANNED        = STORE.get("banned", [])
VIP_USERS     = STORE.get("vip_users", [])
PENDING       = STORE.get("pending", {})
ADMINS        = STORE.get("admins", {})
ADMIN_LEVELS  = {"moderator": 1, "admin": 2, "superadmin": 3}

def is_allowed(uid):   return uid in ALLOWED_USERS
def is_banned(uid):    return uid in BANNED
def is_pending(uid):   return str(uid) in PENDING
def is_vip(uid):       return uid in VIP_USERS
def is_maintenance():  return STORE.get("maintenance", False)
def admin_level(uid):  return ADMIN_LEVELS.get(ADMINS.get(str(uid), ""), 0)
def admin_role(uid):   return ADMINS.get(str(uid), "")
def has_admin(uid, required="admin"):
    return admin_level(uid) >= ADMIN_LEVELS.get(required, 2)

def is_expired(uid):
    exp = STORE.get("expiry", {}).get(str(uid))
    if not exp: return False
    try:
        return datetime.fromisoformat(exp) < datetime.utcnow()
    except: return False

def check_rate_limit(uid):
    now = time.time()
    key = f"rl_{uid}"
    if key not in STORE:
        STORE[key] = []
    STORE[key] = [t for t in STORE[key] if now - t < RATE_LIMIT_SECONDS]
    if len(STORE[key]) >= RATE_LIMIT_ACTIONS:
        return False, RATE_LIMIT_SECONDS - int(now - STORE[key][0])
    STORE[key].append(now)
    return True, 0

def store_allow(uid, name="", save=True):
    if uid not in ALLOWED_USERS:
        ALLOWED_USERS.append(uid)
        STORE["allowed_users"] = ALLOWED_USERS
    if str(uid) in PENDING:
        del PENDING[str(uid)]
        STORE["pending"] = PENDING
    if uid in BANNED:
        BANNED.remove(uid)
        STORE["banned"] = BANNED
    STORE.setdefault("users", {})[str(uid)] = {"name": name, "added": datetime.utcnow().isoformat()}
    if save: save_store(STORE)

def store_ban(uid):
    if uid in ALLOWED_USERS: ALLOWED_USERS.remove(uid); STORE["allowed_users"] = ALLOWED_USERS
    if uid in VIP_USERS: VIP_USERS.remove(uid); STORE["vip_users"] = VIP_USERS
    if uid not in BANNED: BANNED.append(uid); STORE["banned"] = BANNED
    if str(uid) in PENDING: del PENDING[str(uid)]; STORE["pending"] = PENDING
    save_store(STORE)

def store_unban(uid):
    if uid in BANNED: BANNED.remove(uid); STORE["banned"] = BANNED; save_store(STORE)

def store_remove_user(uid):
    if uid in ALLOWED_USERS: ALLOWED_USERS.remove(uid); STORE["allowed_users"] = ALLOWED_USERS
    if uid in VIP_USERS: VIP_USERS.remove(uid); STORE["vip_users"] = VIP_USERS
    if str(uid) in PENDING: del PENDING[str(uid)]; STORE["pending"] = PENDING
    save_store(STORE)

def store_add_admin(uid, role="admin"):
    ADMINS[str(uid)] = role
    STORE["admins"] = ADMINS
    if uid not in ALLOWED_USERS: store_allow(uid, save=False)
    save_store(STORE)

def store_remove_admin(uid):
    if str(uid) in ADMINS: del ADMINS[str(uid)]; STORE["admins"] = ADMINS; save_store(STORE)

def store_add_pending(uid, name, username=""):
    PENDING[str(uid)] = {"name": name, "username": username, "time": datetime.utcnow().isoformat()}
    STORE["pending"] = PENDING
    save_store(STORE)

def store_add_vip(uid):
    if uid not in VIP_USERS: VIP_USERS.append(uid); STORE["vip_users"] = VIP_USERS; save_store(STORE)

def store_remove_vip(uid):
    if uid in VIP_USERS: VIP_USERS.remove(uid); STORE["vip_users"] = VIP_USERS; save_store(STORE)

def store_set_expiry(uid, days):
    exp = (datetime.utcnow() + timedelta(days=int(days))).isoformat()
    STORE.setdefault("expiry", {})[str(uid)] = exp
    save_store(STORE)

def store_remove_expiry(uid):
    if str(uid) in STORE.get("expiry", {}): del STORE["expiry"][str(uid)]; save_store(STORE)

def admin_log(actor_id, action, target=""):
    STORE.setdefault("admin_log", []).append({
        "time": datetime.utcnow().isoformat(),
        "actor": actor_id,
        "action": action,
        "target": target
    })
    save_store(STORE)

def update_daily_stats(key="actions"):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    STORE.setdefault("daily_stats", {}).setdefault(today, {}).setdefault(key, 0)
    STORE["daily_stats"][today][key] += 1
    save_store(STORE)

# ============================================================
#  CRYPTO / DECODE
# ============================================================

def make_xor_key(uid):
    h = hashlib.md5(uid.encode()).digest()
    return bytes([h[i % 16] for i in range(256)])

def xor_bytes(data, key):
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))

def decompress(data):
    try:
        return zlib.decompress(data, -zlib.MAX_WBITS)
    except:
        try:
            return zlib.decompress(data)
        except:
            if HAS_BROTLI:
                try: return brotli.decompress(data)
                except: pass
            return None

def compress(data):
    return zlib.compress(data)

def decrypt_aes(data, key):
    if not HAS_CRYPTO: return None
    try:
        iv = data[:16]
        ct = data[16:]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return unpad(cipher.decrypt(ct), AES.block_size)
    except: return None

def encrypt_aes(data, key):
    if not HAS_CRYPTO: return None
    try:
        iv = os.urandom(16)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return iv + cipher.encrypt(pad(data, AES.block_size))
    except: return None

def _md5(t): return hashlib.md5(t.encode()).digest()
def _sha1(t): return hashlib.sha1(t.encode()).digest()[:16]

def build_aes_keys(uid, password=None, email=None):
    k1 = _md5(uid)
    k2 = _sha1(uid)
    k3 = _md5(k1.hex() + k2.hex())
    if password:
        k4 = _sha1(password)
        k5 = _md5(k4.hex() + k3.hex())
        return [k1, k2, k3, k4, k5]
    if email:
        k4 = _sha1(email)
        k5 = _md5(k4.hex() + k3.hex())
        return [k1, k2, k3, k4, k5]
    return [k1, k2, k3]

class Reader:
    def __init__(self, data):
        self.buf = data
        self.pos = 0
    def has_bytes(self, n): return self.pos + n <= len(self.buf)
    def read_byte(self):
        if not self.has_bytes(1): return 0
        v = self.buf[self.pos]; self.pos += 1; return v
    def read_int(self):
        if not self.has_bytes(4): return 0
        v = struct.unpack("<i", self.buf[self.pos:self.pos+4])[0]; self.pos += 4; return v
    def read_float(self):
        if not self.has_bytes(4): return 0.0
        v = struct.unpack("<f", self.buf[self.pos:self.pos+4])[0]; self.pos += 4; return v
    def read_string(self):
        if not self.has_bytes(4): return ""
        ln = struct.unpack("<i", self.buf[self.pos:self.pos+4])[0]; self.pos += 4
        if ln <= 0: return ""
        if not self.has_bytes(ln): return ""
        v = self.buf[self.pos:self.pos+ln].decode("utf-8", errors="replace"); self.pos += ln; return v
    def read_list(self, item_fn):
        if not self.has_bytes(4): return []
        ln = struct.unpack("<i", self.buf[self.pos:self.pos+4])[0]; self.pos += 4
        return [item_fn() for _ in range(ln)]
    def read_dict(self):
        d = {}
        if not self.has_bytes(4): return d
        ln = struct.unpack("<i", self.buf[self.pos:self.pos+4])[0]; self.pos += 4
        for _ in range(ln):
            k = self.read_string()
            v = self.read_string()
            d[k] = v
        return d
    def read_equipment(self):
        if not self.has_bytes(4): return []
        ln = struct.unpack("<i", self.buf[self.pos:self.pos+4])[0]; self.pos += 4
        out = []
        for _ in range(ln):
            if not self.has_bytes(1): break
            typ = self.read_byte()
            if typ == 0:
                out.append({"type": 0, "id": self.read_int(), "color": self.read_int()})
            elif typ == 1:
                out.append({"type": 1, "id": self.read_int()})
            elif typ == 2:
                out.append({"type": 2, "id": self.read_int(), "color": self.read_int(), "float": self.read_float()})
            else:
                out.append({"type": typ, "id": self.read_int()})
        return out

def parse_player(buf):
    r = Reader(buf)
    if r.read_byte() == 0: return None
    p = {}
    p["Name"] = r.read_string(); p["money"] = r.read_int()
    p["coin"] = r.read_int(); p["localID"] = r.read_string()
    p["boughtFsos"] = r.read_list(r.read_int)

    def read_friend():
        r.read_byte()
        return {"id": r.read_string(), "Name": r.read_string(), "accountID": r.read_string()}

    p["FriendsID"] = r.read_list(read_friend)
    p["LevelsDoneTime"] = r.read_list(r.read_float)
    p["floats"] = r.read_list(r.read_float)
    p["integers"] = r.read_list(r.read_int)
    p["fcar"] = r.read_list(r.read_int)
    p["favouriteWheels"] = r.read_list(r.read_int)
    p["favouriteVinyls"] = r.read_list(r.read_int)
    p["favouriteEmojis"] = r.read_list(r.read_int)
    p["personEquipmentsMale"] = r.read_equipment()
    p["personEquipmentsFemale"] = r.read_equipment()

    if r.read_byte() == 0:
        p["platesData"] = None
    else:
        def read_vinyl():
            r.read_byte()
            def rv(): return {"x": r.read_float(), "y": r.read_float(), "z": r.read_float()}
            return {"vectors": r.read_list(rv), "v": r.read_list(r.read_string),
                    "floats": r.read_list(r.read_float), "text": r.read_string()}
        def read_plate():
            r.read_byte()
            return {"plateId": r.read_int(), "frontCarId": r.read_int(),
                    "rearCarId": r.read_int(), "vinyls": r.read_list(read_vinyl)}
        p["platesData"] = {"allPlates": r.read_list(read_plate)}

    if r.read_byte() == 0:
        p["carIDnStatus"] = None
    else:
        p["carIDnStatus"] = {
            "carGeneratedIDs": r.read_list(r.read_string),
            "carStatus": r.read_list(r.read_int),
        }

    p["allData"] = r.read_string()
    p["flags"] = r.read_dict()
    p["animations"] = r.read_list(r.read_int)
    p["emojiPacks"] = r.read_list(r.read_int)
    p["wheels"] = r.read_list(r.read_int)
    p["boughtPoliceLights"] = r.read_list(r.read_int)
    p["boughtPoliceSirens"] = r.read_list(r.read_int)
    return p

def try_parse(buf):
    candidates = [buf]
    d1 = decompress(buf)
    if d1:
        candidates.append(d1)
        d2 = decompress(d1)
        if d2: candidates.append(d2)
    for c in candidates:
        if not c: continue
        if len(c) > 0 and c[0] in (17, 23, 24):
            try:
                p = parse_player(c)
                if p and p.get("Name") is not None: return p
            except: pass
        try:
            clean = c[3:] if (len(c) >= 3 and c[0] == 0xef and c[1] == 0xbb) else c
            if clean[0] == 123: return json.loads(clean.decode("utf-8"))
        except: pass
    return None

def decrypt_player_record(base64_text, uid, password=None, email=None):
    try: buf = base64.b64decode(base64_text)
    except: return {"success": False, "message": "Bad base64"}
    if len(buf) < 10: return {"success": False, "message": "Too small"}

    for key in build_aes_keys(uid, password, email):
        dec = decrypt_aes(buf, key)
        if dec:
            parsed = try_parse(dec)
            if parsed: return {"success": True, "record": parsed}

    xk = make_xor_key(uid)
    xdec = xor_bytes(buf, xk)
    parsed = try_parse(xdec)
    if parsed: return {"success": True, "record": parsed}

    for d in [decompress(buf), decompress(xdec)]:
        if d:
            parsed = try_parse(d)
            if parsed: return {"success": True, "record": parsed}

    return {"success": False, "message": "Decryption failed"}

class Writer:
    def __init__(self): self._p = []
    def write_byte(self, v): self._p.append(bytes([v & 0xFF]))
    def write_int(self, v):  self._p.append(struct.pack("<i", int(v or 0)))
    def write_float(self, v): self._p.append(struct.pack("<f", float(v or 0.0)))
    def write_string(self, s):
        t = s.encode("utf-8") if s else b""
        self.write_int(len(t))
        self._p.append(t)
    def write_list(self, lst, fn):
        self.write_int(len(lst))
        for item in lst: fn(item)
    def write_dict(self, d):
        self.write_int(len(d))
        for k, v in d.items():
            self.write_string(k)
            self.write_string(v)
    def write_equipment(self, data):
        self.write_int(len(data))
        for item in data:
            t = item.get("type", 0)
            self.write_byte(t)
            if t == 0:
                self.write_int(item.get("id", 0)); self.write_int(item.get("color", 0))
            elif t == 1:
                self.write_int(item.get("id", 0))
            elif t == 2:
                self.write_int(item.get("id", 0)); self.write_int(item.get("color", 0)); self.write_float(item.get("float", 0.0))
            else:
                self.write_int(item.get("id", 0))
    def write_plates(self, data):
        if data is None:
            self.write_byte(0); return
        self.write_byte(1)
        def wv(v):
            self.write_byte(1)
            self.write_list(v.get("vectors", []), lambda x: (self.write_float(x["x"]), self.write_float(x["y"]), self.write_float(x["z"])))
            self.write_list(v.get("v", []), self.write_string)
            self.write_list(v.get("floats", []), self.write_float)
            self.write_string(v.get("text", ""))
        def wp(p):
            self.write_byte(1)
            self.write_int(p.get("plateId", 0)); self.write_int(p.get("frontCarId", 0)); self.write_int(p.get("rearCarId", 0))
            self.write_list(p.get("vinyls", []), wv)
        self.write_list(data.get("allPlates", []), wp)
    def write_car_id_status(self, data):
        if data is None:
            self.write_byte(0); return
        self.write_byte(1)
        self.write_list(data.get("carGeneratedIDs", []), self.write_string)
        self.write_list(data.get("carStatus", []), self.write_int)
    def to_bytes(self): return b"".join(self._p)

def serialize_player(p):
    w = Writer()
    w.write_byte(1)
    w.write_string(p.get("Name", ""))
    w.write_int(p.get("money", 0))
    w.write_int(p.get("coin", 0))
    w.write_string(p.get("localID", ""))
    w.write_list(p.get("boughtFsos", []), w.write_int)
    def wf(f):
        w.write_byte(1)
        w.write_string(f.get("id", ""))
        w.write_string(f.get("Name", ""))
        w.write_string(f.get("accountID", ""))
    w.write_list(p.get("FriendsID", []), wf)
    w.write_list(p.get("LevelsDoneTime", []), w.write_float)
    w.write_list(p.get("floats", []), w.write_float)
    w.write_list(p.get("integers", []), w.write_int)
    w.write_list(p.get("fcar", []), w.write_int)
    w.write_list(p.get("favouriteWheels", []), w.write_int)
    w.write_list(p.get("favouriteVinyls", []), w.write_int)
    w.write_list(p.get("favouriteEmojis", []), w.write_int)
    w.write_equipment(p.get("personEquipmentsMale", []))
    w.write_equipment(p.get("personEquipmentsFemale", []))
    w.write_plates(p.get("platesData", None))
    w.write_car_id_status(p.get("carIDnStatus", None))
    w.write_string(p.get("allData", ""))
    w.write_dict(p.get("flags", {}))
    w.write_list(p.get("animations", []), w.write_int)
    w.write_list(p.get("emojiPacks", []), w.write_int)
    w.write_list(p.get("wheels", []), w.write_int)
    w.write_list(p.get("boughtPoliceLights", []), w.write_int)
    w.write_list(p.get("boughtPoliceSirens", []), w.write_int)
    return w.to_bytes()
    # ============================================================
#  API
# ============================================================

async def api_load_record(session, uid, password="", email=""):
    """Load a CPM account safely with validation, status checks and retries."""
    uid = str(uid or "").strip()
    password = str(password or "").strip()
    email = str(email or "").strip()

    if not uid:
        return {"success": False, "message": "Game UID tidak boleh kosong."}
    if len(uid) > 128:
        return {"success": False, "message": "Game UID terlalu panjang."}
    if email and ("@" not in email or len(email) > 160):
        return {"success": False, "message": "Format email tidak valid."}

    payload = {
        "uid": uid,
        "password": password,
        "email": email,
        "fk": FK,
    }

    timeout = aiohttp.ClientTimeout(total=35, connect=10, sock_read=25)
    last_error = "Tidak dapat terhubung ke server."

    for attempt in range(3):
        try:
            async with session.post(
                LOAD_URL,
                json=payload,
                timeout=timeout,
                headers={"Accept": "application/json, text/plain, */*"},
            ) as resp:
                text = (await resp.text()).strip()

                if resp.status in (401, 403):
                    return {"success": False, "message": "UID, password, atau email ditolak oleh server."}
                if resp.status == 404:
                    return {"success": False, "message": "Endpoint login tidak ditemukan (404)."}
                if resp.status >= 500:
                    last_error = f"Server sedang bermasalah (HTTP {resp.status})."
                    if attempt < 2:
                        await asyncio.sleep(1.2 * (attempt + 1))
                        continue
                    return {"success": False, "message": last_error}
                if resp.status != 200:
                    return {"success": False, "message": f"Server mengembalikan HTTP {resp.status}."}

                if not text:
                    return {"success": False, "message": "Server mengirim respons kosong."}

                try:
                    data = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    data = {"base64": text}

                # Some APIs return an explicit error object.
                if isinstance(data, dict):
                    api_error = data.get("error") or data.get("message")
                    if api_error and not (data.get("base64") or data.get("record")):
                        return {"success": False, "message": str(api_error)[:500]}

                b64 = data.get("base64") or data.get("record") or text
                if not isinstance(b64, str) or not b64.strip():
                    return {"success": False, "message": "Data akun tidak ditemukan pada respons server."}

                result = decrypt_player_record(b64.strip(), uid, password, email)
                if result.get("success"):
                    return result

                return {
                    "success": False,
                    "message": "Data diterima, tetapi tidak dapat dibuka. Periksa UID/password/email."
                }

        except (asyncio.TimeoutError, aiohttp.ClientConnectionError) as e:
            last_error = "Koneksi ke server timeout/terputus."
            if attempt < 2:
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
        except aiohttp.ClientError as e:
            last_error = f"Koneksi gagal: {type(e).__name__}"
            if attempt < 2:
                await asyncio.sleep(1.0 * (attempt + 1))
                continue
        except Exception:
            log.exception("Unexpected login error")
            last_error = "Terjadi kesalahan saat memproses login."

    return {"success": False, "message": last_error}

async def api_save_record(session, uid, record, password="", email=""):
    raw = serialize_player(record)
    if not raw:
        return {"success": False, "message": "Serialize failed"}
    comp = compress(raw)
    b64 = base64.b64encode(comp).decode()
    payload = {
        "uid": uid,
        "password": password,
        "email": email,
        "fk": FK,
        "base64": b64,
    }
    try:
        async with session.post(SAVE_URL, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            text = await resp.text()
            return {"success": True, "response": text}
    except Exception as e:
        return {"success": False, "message": str(e)}

async def api_set_rank(session, uid, rank):
    payload = {"uid": uid, "rank": rank, "fk": FK}
    try:
        async with session.post(RANK_URL, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            return {"success": resp.status == 200, "response": await resp.text()}
    except Exception as e:
        return {"success": False, "message": str(e)}

# ============================================================
#  BOT SETUP
# ============================================================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ============================================================
#  FSM STATES
# ============================================================

class MenuState(StatesGroup):
    main = State()
    account = State()
    stats = State()
    cars = State()
    unlocks = State()
    admin = State()

class InputState(StatesGroup):
    custom_money = State()
    custom_coins = State()
    set_name = State()
    set_id = State()
    change_email = State()
    change_password = State()
    clone_target = State()
    copy_plates_target = State()
    buy_car_id = State()
    police_car_id = State()
    vinyls_car_id = State()
    race_wins = State()
    race_loses = State()
    king_rank = State()
    broadcast = State()
    add_user = State()
    ban_user = State()
    unban_user = State()
    add_admin = State()
    remove_admin = State()
    add_vip = State()
    remove_vip = State()
    set_expiry = State()
    login_uid = State()
    login_pass = State()
    login_email = State()

# ============================================================
#  KEYBOARDS — MODERN UI
# ============================================================

def _btn(text, callback_data):
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def kb_main(is_admin=False):
    buttons = [
        [_btn("🔐  LOGIN ACCOUNT", "do_login")],
        [_btn("💾  SAVE ACCOUNT", "do_save")],
        [_btn("👤  ACCOUNT", "menu_account"), _btn("📊  STATS", "menu_stats")],
        [_btn("🚗  GARAGE", "menu_cars"), _btn("🎁  UNLOCKS", "menu_unlocks")],
    ]
    if is_admin:
        buttons.append([_btn("🛡️  ADMIN PANEL", "menu_admin")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_account():
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("📋 Info", "acc_info"), _btn("✏️ Name", "acc_set_name")],
        [_btn("🆔 Local ID", "acc_set_id"), _btn("📧 Email", "acc_change_email")],
        [_btn("🔑 Password", "acc_change_password")],
        [_btn("📦 Clone Account", "acc_clone"), _btn("🪪 Copy Plates", "acc_copy_plates")],
        [_btn("🏠 Main Menu", "menu_main")],
    ])


def kb_stats():
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("💰 Money 50M", "stat_money_max"), _btn("🪙 Coins 500K", "stat_coins_max")],
        [_btn("💵 Custom Money", "stat_money_custom"), _btn("🪙 Custom Coins", "stat_coins_custom")],
        [_btn("🏆 Race Wins", "stat_race_wins"), _btn("💀 Race Loses", "stat_race_loses")],
        [_btn("👑 King Rank", "stat_king_rank")],
        [_btn("🏠 Main Menu", "menu_main")],
    ])


def kb_cars():
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🚘 Unlock All Cars", "car_unlock_all"), _btn("➕ Buy by ID", "car_buy_id")],
        [_btn("🛞 Bumpers All", "car_bumpers_all"), _btn("🛞 Single", "car_bumpers_single")],
        [_btn("✨ Chrome All", "car_chrome_all"), _btn("✨ Single", "car_chrome_single")],
        [_btn("🎨 Preset All", "car_preset_all"), _btn("🎨 Single", "car_preset_single")],
        [_btn("🚓 Police All", "car_police_all"), _btn("🚓 Single", "car_police_single")],
        [_btn("📋 Clone All", "car_clone_all"), _btn("📋 Single", "car_clone_single")],
        [_btn("🖌️ Vinyls All", "car_vinyls_all"), _btn("🖌️ Single", "car_vinyls_single")],
        [_btn("🏠 Main Menu", "menu_main")],
    ])


def kb_unlocks():
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("⚡ W16", "unl_w16"), _btn("💨 Smoke", "unl_smoke")],
        [_btn("⛽ Fuel", "unl_fuel"), _btn("🛡️ No Damage", "unl_nodamage")],
        [_btn("📯 Horns", "unl_horns"), _btn("🎬 Animations", "unl_animations")],
        [_btn("✨ Perks", "unl_perks"), _btn("💡 Headlights", "unl_headlights")],
        [_btn("🏠 Paid House", "unl_paidhouse"), _btn("🏘️ All Houses", "unl_allhouses")],
        [_btn("🚨 Sirens", "unl_sirens"), _btn("🎯 All Levels", "unl_alllevels")],
        [_btn("👕 All Clothes", "unl_allclothes")],
        [_btn("🏠 Main Menu", "menu_main")],
    ])


def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("➕ Add User", "adm_add_user"), _btn("➖ Remove User", "adm_remove_user")],
        [_btn("🚫 Ban", "adm_ban"), _btn("✅ Unban", "adm_unban")],
        [_btn("💎 Add VIP", "adm_add_vip"), _btn("🗑️ Remove VIP", "adm_remove_vip")],
        [_btn("🛡️ Add Admin", "adm_add_admin"), _btn("🗑️ Remove Admin", "adm_remove_admin")],
        [_btn("📢 Broadcast", "adm_broadcast"), _btn("📊 Stats", "adm_stats")],
        [_btn("🔧 Maintenance", "adm_maintenance"), _btn("📜 Logs", "adm_logs")],
        [_btn("🏠 Main Menu", "menu_main")],
    ])


def kb_cancel():
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("❌ Cancel", "cancel")]])


def kb_login_skip(field):
    if field == "password":
        return InlineKeyboardMarkup(inline_keyboard=[
            [_btn("⏭️ Skip Password", "login_skip_password")],
            [_btn("❌ Cancel", "cancel")]
        ])
    if field == "email":
        return InlineKeyboardMarkup(inline_keyboard=[
            [_btn("⏭️ Skip Email", "login_skip_email")],
            [_btn("◀️ Back", "login_back_password"), _btn("❌ Cancel", "cancel")]
        ])
    return kb_cancel()


def login_progress(step, title, hint):
    bars = {1: "●○○", 2: "●●○", 3: "●●●"}
    return (
        "<b>╭━━ 🔐 SECURE LOGIN ━━╮</b>\n"
        f"<b>│ {title}</b>\n"
        f"<b>│ Progress: {bars.get(step, '○○○')}  {step}/3</b>\n"
        "<b>╰━━━━━━━━━━━━━━━━━━╯</b>\n\n"
        f"{hint}\n\n"
        "<i>🔒 Data login hanya digunakan untuk memuat akun pada sesi ini.</i>"
    )


def modern_home_text(name, logged_in=False, rec=None):
    name = escape(str(name or "User"))
    if logged_in and rec:
        account = escape(str(rec.get("Name") or "Unknown"))
        money = rec.get("money", 0)
        coins = rec.get("coin", 0)
        return (
            "<b>╭━━ 🚀 CPM ACCOUNT MANAGER ━━╮</b>\n"
            f"<b>│ 👋 Welcome, {name}</b>\n"
            "<b>│ 🟢 Account Connected</b>\n"
            "<b>╰━━━━━━━━━━━━━━━━━━━━━━╯</b>\n\n"
            f"👤 <b>{account}</b>\n"
            f"💰 <code>{money:,}</code>   🪙 <code>{coins:,}</code>\n\n"
            "<b>⚡ QUICK ACTIONS</b>\n"
            "Login untuk memuat akun, lalu gunakan menu di bawah untuk mengelola data."
        )
    return (
        "<b>╭━━ 🚀 CPM ACCOUNT MANAGER ━━╮</b>\n"
        f"<b>│ 👋 Welcome, {name}</b>\n"
        "<b>│ 🔴 Account Not Connected</b>\n"
        "<b>╰━━━━━━━━━━━━━━━━━━━━━━╯</b>\n\n"
        "🔐 <b>Login</b> untuk menghubungkan akun CPM.\n"
        "💾 <b>Save</b> untuk menyimpan perubahan.\n\n"
        "<i>Fast • Clean • Secure</i>"
    )

# ============================================================
#  CHECKS
# ============================================================

async def check_user(message):
    uid = message.from_user.id
    if is_banned(uid):
        await message.answer("You are banned from using this bot.")
        return False
    if is_maintenance() and uid != OWNER_ID and not has_admin(uid, "admin"):
        await message.answer("Bot is under maintenance. Please try again later.")
        return False
    if not is_allowed(uid) and uid != OWNER_ID:
        await message.answer("You are not authorized. Request access with /start")
        return False
    if is_expired(uid):
        await message.answer("Your access has expired. Contact admin.")
        return False
    ok, wait = check_rate_limit(uid)
    if not ok:
        await message.answer(f"Rate limit! Wait {wait}s.")
        return False
    return True

async def check_callback(call):
    uid = call.from_user.id
    if is_banned(uid):
        await call.answer("Banned", show_alert=True)
        return False
    if is_maintenance() and uid != OWNER_ID and not has_admin(uid, "admin"):
        await call.answer("Maintenance", show_alert=True)
        return False
    if not is_allowed(uid) and uid != OWNER_ID:
        await call.answer("Not authorized", show_alert=True)
        return False
    return True

# ============================================================
#  HANDLERS
# ============================================================

@router.message(CommandStart())
async def cmd_start(message, state):
    uid = message.from_user.id
    name = message.from_user.full_name
    if is_banned(uid):
        await message.answer("You are banned.")
        return
    if uid == OWNER_ID:
        store_allow(uid, name)
        store_add_admin(uid, "superadmin")
    if uid == OWNER_ID or is_allowed(uid):
        await message.answer(f"Welcome <b>{escape(name)}</b>! Choose a menu:", reply_markup=kb_main(is_admin=has_admin(uid)))
        return
    if is_pending(uid):
        await message.answer("Your request is pending approval.")
        return
    store_add_pending(uid, name, message.from_user.username or "")
    await message.answer("Access request sent to admins. Please wait.")
    for aid in ADMINS:
        try:
            await bot.send_message(int(aid), f"New request from {escape(name)} (<code>{uid}</code>)")
        except: pass

@router.message(Command("help"))
async def cmd_help(message):
    if not await check_user(message): return
    text = """<b>Help</b>
/start - Main menu
/help - This message
Login first, then use menus to modify your account."""
    await message.answer(text)

# Main Menu

@router.callback_query(F.data == "menu_main")
async def cb_main(call, state):
    if not await check_callback(call): return
    await state.set_state(MenuState.main)
    uid = call.from_user.id
    await call.message.edit_text(
        "<b>🏠 MAIN MENU</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔐 <b>Login</b> — muat akun CPM dengan panduan 3 langkah.\n"
        "💾 <b>Save Account</b> — simpan perubahan akun.\n"
        "📊 <b>Stats</b> • 🚗 <b>Cars</b> • 🎁 <b>Unlocks</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Login terlebih dahulu sebelum memakai fitur akun.</i>",
        reply_markup=kb_main(is_admin=has_admin(uid))
    )

@router.callback_query(F.data == "menu_account")
async def cb_menu_account(call, state):
    if not await check_callback(call): return
    await state.set_state(MenuState.account)
    await call.message.edit_text("<b>╭━━ 👤 ACCOUNT ━━╮</b>\n<b>╰━━━━━━━━━━━━━━╯</b>\n\nKelola identitas dan data akun.", reply_markup=kb_account())

@router.callback_query(F.data == "menu_stats")
async def cb_menu_stats(call, state):
    if not await check_callback(call): return
    await state.set_state(MenuState.stats)
    await call.message.edit_text("<b>╭━━ 📊 STATS & MONEY ━━╮</b>\n<b>╰━━━━━━━━━━━━━━━━━━╯</b>\n\nAtur saldo, statistik balapan, dan rank.", reply_markup=kb_stats())

@router.callback_query(F.data == "menu_cars")
async def cb_menu_cars(call, state):
    if not await check_callback(call): return
    await state.set_state(MenuState.cars)
    await call.message.edit_text("<b>╭━━ 🚗 GARAGE ━━╮</b>\n<b>╰━━━━━━━━━━━━━━╯</b>\n\nKelola kendaraan dan garage.", reply_markup=kb_cars())

@router.callback_query(F.data == "menu_unlocks")
async def cb_menu_unlocks(call, state):
    if not await check_callback(call): return
    await state.set_state(MenuState.unlocks)
    await call.message.edit_text("<b>╭━━ 🎁 UNLOCKS & EXTRAS ━━╮</b>\n<b>╰━━━━━━━━━━━━━━━━━━━━╯</b>\n\nBuka fitur tambahan akun dengan cepat.", reply_markup=kb_unlocks())

@router.callback_query(F.data == "menu_admin")
async def cb_menu_admin(call, state):
    if not await check_callback(call): return
    if not has_admin(call.from_user.id):
        await call.answer("No access", show_alert=True)
        return
    await state.set_state(MenuState.admin)
    await call.message.edit_text("<b>╭━━ 🛡️ ADMIN PANEL ━━╮</b>\n<b>╰━━━━━━━━━━━━━━━━━━╯</b>\n\nKelola user, VIP, admin, broadcast, maintenance, dan logs.", reply_markup=kb_admin())

# Login flow

@router.callback_query(F.data == "do_login")
async def cb_do_login(call, state):
    if not await check_callback(call):
        return
    await state.clear()
    await state.set_state(InputState.login_uid)
    await call.answer()
    await call.message.edit_text(
        login_progress(
            1,
            "Login Game Account",
            "Masukkan <b>Game UID</b> Anda.\n\n"
            "Contoh: <code>123456789</code>\n"
            "Pastikan UID tidak mengandung spasi."
        ),
        reply_markup=kb_cancel()
    )

@router.message(InputState.login_uid)
async def inp_login_uid(message, state):
    if not await check_user(message):
        return

    uid = (message.text or "").strip()
    if not uid:
        await message.answer("⚠️ Game UID masih kosong. Silakan kirim UID Anda.")
        return
    if len(uid) > 128:
        await message.answer("⚠️ Game UID terlalu panjang. Periksa kembali UID Anda.")
        return
    if any(ch.isspace() for ch in uid):
        await message.answer("⚠️ UID tidak boleh mengandung spasi. Kirim ulang UID yang benar.")
        return

    await state.update_data(login_uid=uid, login_password="", login_email="")
    await state.set_state(InputState.login_pass)
    await message.answer(
        login_progress(
            2,
            "Password",
            "Masukkan <b>Password</b> akun Anda.\n"
            "Jika akun tidak memakai password, tekan <b>Skip Password</b>."
        ),
        reply_markup=kb_login_skip("password")
    )

@router.message(InputState.login_pass)
async def inp_login_pass(message, state):
    if not await check_user(message):
        return

    pw = (message.text or "").strip()
    if pw == "-":
        pw = ""

    if len(pw) > 256:
        await message.answer("⚠️ Password terlalu panjang. Silakan periksa kembali.")
        return

    await state.update_data(login_password=pw)
    await state.set_state(InputState.login_email)
    await message.answer(
        login_progress(
            3,
            "Email",
            "Masukkan <b>Email</b> akun Anda.\n"
            "Jika tidak diperlukan, tekan <b>Skip Email</b>."
        ),
        reply_markup=kb_login_skip("email")
    )

@router.callback_query(F.data == "login_skip_password")
async def cb_login_skip_password(call, state):
    if not await check_callback(call):
        return
    await state.update_data(login_password="")
    await state.set_state(InputState.login_email)
    await call.answer()
    await call.message.edit_text(
        login_progress(
            3,
            "Email",
            "Masukkan <b>Email</b> akun Anda.\n"
            "Jika tidak diperlukan, tekan <b>Skip Email</b>."
        ),
        reply_markup=kb_login_skip("email")
    )

@router.callback_query(F.data == "login_back_password")
async def cb_login_back_password(call, state):
    if not await check_callback(call):
        return
    await state.set_state(InputState.login_pass)
    await call.answer()
    await call.message.edit_text(
        login_progress(
            2,
            "Password",
            "Masukkan <b>Password</b> akun Anda.\n"
            "Jika akun tidak memakai password, tekan <b>Skip Password</b>."
        ),
        reply_markup=kb_login_skip("password")
    )

async def finish_login(message, state, email):
    data = await state.get_data()
    uid = data.get("login_uid", "")
    pw = data.get("login_password", "")

    if not uid:
        await message.answer("⚠️ Sesi login tidak lengkap. Silakan tekan Login lagi.")
        await state.set_state(MenuState.main)
        return

    email = (email or "").strip()
    if email and ("@" not in email or len(email) > 160):
        await message.answer("⚠️ Format email tidak valid. Silakan masukkan email yang benar atau tekan Skip Email.")
        return

    loading = await message.answer(
        "<b>🔄 Menghubungkan ke server...</b>\n"
        "Memuat data akun dan memverifikasi respons.\n"
        "<i>Mohon tunggu, jangan kirim pesan lain dulu.</i>"
    )

    try:
        connector = aiohttp.TCPConnector(ssl=True, limit=10)
        async with aiohttp.ClientSession(connector=connector) as session:
            res = await api_load_record(session, uid, pw, email)
    except Exception:
        log.exception("Login session error")
        res = {"success": False, "message": "Sesi koneksi gagal. Silakan coba lagi."}

    try:
        await loading.delete()
    except Exception:
        pass

    if not res.get("success"):
        msg = escape(str(res.get("message", "Login gagal."))[:500])
        await message.answer(
            "<b>❌ Login gagal</b>\n\n"
            f"{msg}\n\n"
            "<b>Tips:</b> pastikan UID benar, lalu coba lagi. "
            "Jika server sedang lambat, tunggu beberapa detik dan ulangi.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Login Lagi", callback_data="do_login")],
                [InlineKeyboardButton(text="🏠 Main Menu", callback_data="cancel")]
            ])
        )
        await state.set_state(MenuState.main)
        return

    rec = res.get("record") or {}
    await state.update_data(record=rec, uid=uid, password=pw, email=email)
    STORE.setdefault("stats", {})["total_logins"] = STORE.get("stats", {}).get("total_logins", 0) + 1
    update_daily_stats("logins")

    name = escape(str(rec.get("Name") or "Unknown"))
    money = rec.get("money", 0)
    coins = rec.get("coin", 0)

    await message.answer(
        "<b>✅ LOGIN BERHASIL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 Account: <b>{name}</b>\n"
        f"💰 Money: <code>{money:,}</code>\n"
        f"🪙 Coins: <code>{coins:,}</code>\n"
        f"🆔 UID: <code>{escape(uid)}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Akun sudah dimuat. Silakan pilih fitur di bawah.",
        reply_markup=kb_main(is_admin=has_admin(message.from_user.id))
    )
    await state.set_state(MenuState.main)
    save_store(STORE)

@router.message(InputState.login_email)
async def inp_login_email(message, state):
    if not await check_user(message):
        return
    em = (message.text or "").strip()
    if em == "-":
        em = ""
    await finish_login(message, state, em)

@router.callback_query(F.data == "login_skip_email")
async def cb_login_skip_email(call, state):
    if not await check_callback(call):
        return
    await call.answer()
    await finish_login(call.message, state, "")

@router.callback_query(F.data == "do_save")
async def cb_do_save(call, state):
    if not await check_callback(call): return
    data = await state.get_data()
    rec = data.get("record")
    uid = data.get("uid")
    pw = data.get("password", "")
    em = data.get("email", "")
    if not rec or not uid:
        await call.answer("Login first!", show_alert=True)
        return
    await call.message.edit_text("Saving account...")
    async with aiohttp.ClientSession() as session:
        res = await api_save_record(session, uid, rec, pw, em)
    if res.get("success"):
        await call.message.edit_text("Account saved successfully!")
    else:
        await call.message.edit_text(f"Save failed: {escape(res.get('message',''))}")

# Account handlers

@router.callback_query(F.data == "acc_info")
async def cb_acc_info(call, state):
    if not await check_callback(call): return
    data = await state.get_data()
    rec = data.get("record")
    if not rec:
        await call.answer("Login first!", show_alert=True)
        return
    text = f"""<b>Account Info</b>
Name: <code>{escape(rec.get('Name',''))}</code>
Money: <code>{rec.get('money',0):,}</code>
Coins: <code>{rec.get('coin',0):,}</code>
ID: <code>{escape(rec.get('localID',''))}</code>
Cars: <code>{len(rec.get('boughtFsos',[]))}</code>
Friends: <code>{len(rec.get('FriendsID',[]))}</code>
Animations: <code>{len(rec.get('animations',[]))}</code>
Wheels: <code>{len(rec.get('wheels',[]))}</code>"""
    await call.message.edit_text(text, reply_markup=kb_account())

@router.callback_query(F.data == "acc_set_name")
async def cb_acc_set_name(call, state):
    if not await check_callback(call): return
    await state.set_state(InputState.set_name)
    await call.message.edit_text("Send new name:", reply_markup=kb_cancel())

@router.message(InputState.set_name)
async def inp_set_name(message, state):
    if not await check_user(message): return
    data = await state.get_data()
    rec = data.get("record")
    if not rec:
        await message.answer("Login first!")
        await state.clear(); return
    rec["Name"] = message.text[:32]
    await state.update_data(record=rec)
    await message.answer(f"Name set to: <b>{escape(rec['Name'])}</b>", reply_markup=kb_account())
    await state.set_state(MenuState.account)

@router.callback_query(F.data == "acc_set_id")
async def cb_acc_set_id(call, state):
    if not await check_callback(call): return
    await state.set_state(InputState.set_id)
    await call.message.edit_text("Send new Local ID:", reply_markup=kb_cancel())

@router.message(InputState.set_id)
async def inp_set_id(message, state):
    if not await check_user(message): return
    data = await state.get_data()
    rec = data.get("record")
    if not rec:
        await message.answer("Login first!")
        await state.clear(); return
    rec["localID"] = message.text[:64]
    await state.update_data(record=rec)
    await message.answer(f"ID set to: <code>{escape(rec['localID'])}</code>", reply_markup=kb_account())
    await state.set_state(MenuState.account)

@router.callback_query(F.data == "acc_change_email")
async def cb_acc_change_email(call, state):
    if not await check_callback(call): return
    await state.set_state(InputState.change_email)
    await call.message.edit_text("Send new email:", reply_markup=kb_cancel())

@router.message(InputState.change_email)
async def inp_change_email(message, state):
    if not await check_user(message): return
    await state.update_data(email=message.text[:128])
    await message.answer("Email updated for next save.", reply_markup=kb_account())
    await state.set_state(MenuState.account)

@router.callback_query(F.data == "acc_change_password")
async def cb_acc_change_password(call, state):
    if not await check_callback(call): return
    await state.set_state(InputState.change_password)
    await call.message.edit_text("Send new password:", reply_markup=kb_cancel())

@router.message(InputState.change_password)
async def inp_change_password(message, state):
    if not await check_user(message): return
    await state.update_data(password=message.text[:64])
    await message.answer("Password updated for next save.", reply_markup=kb_account())
    await state.set_state(MenuState.account)

@router.callback_query
@router.callback_query(F.data == "unl_allhouses")
async def cb_unl_allhouses(call, state):
    if not await check_callback(call): return
    data = await state.get_data()
    rec = data.get("record")
    if not rec:
        await call.answer("Login first!", show_alert=True); return
    flags = rec.get("flags", {}); flags["allhouses"] = "1"; rec["flags"] = flags
    await state.update_data(record=rec)
    await call.answer("All Houses unlocked", show_alert=True)

@router.callback_query(F.data == "unl_sirens")
async def cb_unl_sirens(call, state):
    if not await check_callback(call): return
    data = await state.get_data()
    rec = data.get("record")
    if not rec:
        await call.answer("Login first!", show_alert=True); return
    rec["boughtPoliceLights"] = list(range(1,50))
    rec["boughtPoliceSirens"] = list(range(1,50))
    await state.update_data(record=rec)
    await call.answer("Sirens unlocked", show_alert=True)

@router.callback_query(F.data == "unl_alllevels")
async def cb_unl_alllevels(call, state):
    if not await check_callback(call): return
    data = await state.get_data()
    rec = data.get("record")
    if not rec:
        await call.answer("Login first!", show_alert=True); return
    rec["LevelsDoneTime"] = [1.0]*50
    await state.update_data(record=rec)
    await call.answer("All Levels unlocked", show_alert=True)

@router.callback_query(F.data == "unl_allclothes")
async def cb_unl_allclothes(call, state):
    if not await check_callback(call): return
    data = await state.get_data()
    rec = data.get("record")
    if not rec:
        await call.answer("Login first!", show_alert=True); return
    male = []
    female = []
    for i in range(1,200):
        male.append({"type": 0, "id": i, "color": 0})
        female.append({"type": 0, "id": i, "color": 0})
    rec["personEquipmentsMale"] = male
    rec["personEquipmentsFemale"] = female
    await state.update_data(record=rec)
    await call.answer("All Clothes unlocked", show_alert=True)

# Admin handlers

@router.callback_query(F.data == "adm_add_user")
async def cb_adm_add_user(call, state):
    if not await check_callback(call): return
    if not has_admin(call.from_user.id, "admin"):
        await call.answer("No access", show_alert=True); return
    await state.set_state(InputState.add_user)
    await call.message.edit_text("Send User ID to add:", reply_markup=kb_cancel())

@router.message(InputState.add_user)
async def inp_add_user(message, state):
    if not await check_user(message): return
    try: uid = int(message.text.strip())
    except: await message.answer("Invalid ID!"); return
    store_allow(uid)
    admin_log(message.from_user.id, "add_user", str(uid))
    await message.answer(f"User {uid} added.", reply_markup=kb_admin())
    await state.set_state(MenuState.admin)

@router.callback_query(F.data == "adm_remove_user")
async def cb_adm_remove_user(call, state):
    if not await check_callback(call): return
    if not has_admin(call.from_user.id, "admin"):
        await call.answer("No access", show_alert=True); return
    await state.set_state(InputState.add_user)
    await call.message.edit_text("Send User ID to remove:", reply_markup=kb_cancel())

@router.callback_query(F.data == "adm_ban")
async def cb_adm_ban(call, state):
    if not await check_callback(call): return
    if not has_admin(call.from_user.id, "admin"):
        await call.answer("No access", show_alert=True); return
    await state.set_state(InputState.ban_user)
    await call.message.edit_text("Send User ID to ban:", reply_markup=kb_cancel())

@router.message(InputState.ban_user)
async def inp_ban_user(message, state):
    if not await check_user(message): return
    try: uid = int(message.text.strip())
    except: await message.answer("Invalid ID!"); return
    store_ban(uid)
    admin_log(message.from_user.id, "ban", str(uid))
    await message.answer(f"User {uid} banned.", reply_markup=kb_admin())
    await state.set_state(MenuState.admin)

@router.callback_query(F.data == "adm_unban")
async def cb_adm_unban(call, state):
    if not await check_callback(call): return
    if not has_admin(call.from_user.id, "admin"):
        await call.answer("No access", show_alert=True); return
    await state.set_state(InputState.unban_user)
    await call.message.edit_text("Send User ID to unban:", reply_markup=kb_cancel())

@router.message(InputState.unban_user)
async def inp_unban_user(message, state):
    if not await check_user(message): return
    try: uid = int(message.text.strip())
    except: await message.answer("Invalid ID!"); return
    store_unban(uid)
    admin_log(message.from_user.id, "unban", str(uid))
    await message.answer(f"User {uid} unbanned.", reply_markup=kb_admin())
    await state.set_state(MenuState.admin)

@router.callback_query(F.data == "adm_add_vip")
async def cb_adm_add_vip(call, state):
    if not await check_callback(call): return
    if not has_admin(call.from_user.id, "admin"):
        await call.answer("No access", show_alert=True); return
    await state.set_state(InputState.add_vip)
    await call.message.edit_text("Send User ID to add VIP:", reply_markup=kb_cancel())

@router.message(InputState.add_vip)
async def inp_add_vip(message, state):
    if not await check_user(message): return
    try: uid = int(message.text.strip())
    except: await message.answer("Invalid ID!"); return
    store_add_vip(uid)
    admin_log(message.from_user.id, "add_vip", str(uid))
    await message.answer(f"User {uid} is now VIP.", reply_markup=kb_admin())
    await state.set_state(MenuState.admin)

@router.callback_query(F.data == "adm_remove_vip")
async def cb_adm_remove_vip(call, state):
    if not await check_callback(call): return
    if not has_admin(call.from_user.id, "admin"):
        await call.answer("No access", show_alert=True); return
    await state.set_state(InputState.remove_vip)
    await call.message.edit_text("Send User ID to remove VIP:", reply_markup=kb_cancel())

@router.message(InputState.remove_vip)
async def inp_remove_vip(message, state):
    if not await check_user(message): return
    try: uid = int(message.text.strip())
    except: await message.answer("Invalid ID!"); return
    store_remove_vip(uid)
    admin_log(message.from_user.id, "remove_vip", str(uid))
    await message.answer(f"User {uid} VIP removed.", reply_markup=kb_admin())
    await state.set_state(MenuState.admin)

@router.callback_query(F.data == "adm_add_admin")
async def cb_adm_add_admin(call, state):
    if not await check_callback(call): return
    if not has_admin(call.from_user.id, "superadmin"):
        await call.answer("Superadmin only", show_alert=True); return
    await state.set_state(InputState.add_admin)
    await call.message.edit_text("Send User ID to add as admin:", reply_markup=kb_cancel())

@router.message(InputState.add_admin)
async def inp_add_admin(message, state):
    if not await check_user(message): return
    try: uid = int(message.text.strip())
    except: await message.answer("Invalid ID!"); return
    store_add_admin(uid, "admin")
    admin_log(message.from_user.id, "add_admin", str(uid))
    await message.answer(f"User {uid} is now admin.", reply_markup=kb_admin())
    await state.set_state(MenuState.admin)

@router.callback_query(F.data == "adm_remove_admin")
async def cb_adm_remove_admin(call, state):
    if not await check_callback(call): return
    if not has_admin(call.from_user.id, "superadmin"):
        await call.answer("Superadmin only", show_alert=True); return
    await state.set_state(InputState.remove_admin)
    await call.message.edit_text("Send User ID to remove admin:", reply_markup=kb_cancel())

@router.message(InputState.remove_admin)
async def inp_remove_admin(message, state):
    if not await check_user(message): return
    try: uid = int(message.text.strip())
    except: await message.answer("Invalid ID!"); return
    store_remove_admin(uid)
    admin_log(message.from_user.id, "remove_admin", str(uid))
    await message.answer(f"User {uid} admin removed.", reply_markup=kb_admin())
    await state.set_state(MenuState.admin)

@router.callback_query(F.data == "adm_broadcast")
async def cb_adm_broadcast(call, state):
    if not await check_callback(call): return
    if not has_admin(call.from_user.id, "admin"):
        await call.answer("No access", show_alert=True); return
    await state.set_state(InputState.broadcast)
    await call.message.edit_text("Send broadcast message:", reply_markup=kb_cancel())

@router.message(InputState.broadcast)
async def inp_broadcast(message, state):
    if not await check_user(message): return
    text = message.text
    sent = 0
    failed = 0
    for uid in ALLOWED_USERS:
        try:
            await bot.send_message(uid, f"<b>Broadcast</b>\n{text}")
            sent += 1
        except: failed += 1
    add_broadcast_history(message.from_user.id, "text", text, sent, failed)
    await message.answer(f"Broadcast sent: {sent} ok, {failed} failed.", reply_markup=kb_admin())
    await state.set_state(MenuState.admin)

@router.callback_query(F.data == "adm_stats")
async def cb_adm_stats(call, state):
    if not await check_callback(call): return
    if not has_admin(call.from_user.id, "admin"):
        await call.answer("No access", show_alert=True); return
    stats = STORE.get("stats", {})
    today = datetime.utcnow().strftime("%Y-%m-%d")
    daily = STORE.get("daily_stats", {}).get(today, {})
    text = f"""<b>Bot Stats</b>
Total Logins: {stats.get('total_logins',0)}
Total Actions: {stats.get('total_actions',0)}
Total Unlocks: {stats.get('total_unlocks',0)}
Allowed Users: {len(ALLOWED_USERS)}
VIP Users: {len(VIP_USERS)}
Banned: {len(BANNED)}
Pending: {len(PENDING)}
Admins: {len(ADMINS)}
Today Actions: {daily.get('actions',0)}
Today Unlocks: {daily.get('unlocks',0)}"""
    await call.message.edit_text(text, reply_markup=kb_admin())

@router.callback_query(F.data == "adm_maintenance")
async def cb_adm_maintenance(call, state):
    if not await check_callback(call): return
    if not has_admin(call.from_user.id, "superadmin"):
        await call.answer("Superadmin only", show_alert=True); return
    STORE["maintenance"] = not STORE.get("maintenance", False)
    save_store(STORE)
    status = "ON" if STORE["maintenance"] else "OFF"
    await call.answer(f"Maintenance {status}", show_alert=True)
    await call.message.edit_text(f"<b>Admin Panel</b>\nMaintenance: {status}", reply_markup=kb_admin())

@router.callback_query(F.data == "adm_logs")
async def cb_adm_logs(call, state):
    if not await check_callback(call): return
    if not has_admin(call.from_user.id, "admin"):
        await call.answer("No access", show_alert=True); return
    logs = STORE.get("admin_log", [])[-20:]
    text = "<b>Recent Admin Logs</b>\n"
    for log_entry in logs:
        text += f"\n{escape(log_entry.get('action',''))} by {log_entry.get('actor','')} -> {escape(log_entry.get('target',''))}"
    if len(text) > 4000: text = text[:4000]
    await call.message.edit_text(text, reply_markup=kb_admin())

# Cancel handler

@router.callback_query(F.data == "cancel")
async def cb_cancel(call, state):
    await state.set_state(MenuState.main)
    uid = call.from_user.id
    await call.message.edit_text(modern_home_text(call.from_user.full_name), reply_markup=kb_main(is_admin=has_admin(uid)))

# ============================================================
#  MAIN
# ============================================================

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
