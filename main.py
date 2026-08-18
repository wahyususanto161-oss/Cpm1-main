import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
import zlib
from html import escape

import aiohttp

try:
    import brotli
    HAS_BROTLI = True
except Exception:
    brotli = None
    HAS_BROTLI = False

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    HAS_CRYPTO = True
except Exception:
    AES = None
    unpad = None
    HAS_CRYPTO = False
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ============================================================
# CONFIG
# ============================================================
# Set BOT_TOKEN in your environment before running.
BOT_TOKEN = "8991051291:AAEWtjtdhGeEl8iClIrvXC1Au95bg1csjlA"
FIREBASE_API_KEY = os.environ.get(
    "FIREBASE_API_KEY",
    "AIzaSyAe_aOVT1gSfmHKBrorFvX4fRwN5nODXVA",
)

# CPM1 / Car Parking Multiplayer player-record API
CPM_LOAD_URL = (
    "https://europe-west1-cp-multiplayer.cloudfunctions.net/"
    "GetPlayerRecords3"
)

# Firebase Authentication REST API
FIREBASE_LOGIN_URL = (
    "https://identitytoolkit.googleapis.com/v1/"
    "accounts:signInWithPassword"
)
FIREBASE_UPDATE_URL = (
    "https://identitytoolkit.googleapis.com/v1/"
    "accounts:update"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("CPM1-BOT")

# ============================================================
# BOT / STATE
# ============================================================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


class LoginState(StatesGroup):
    email = State()
    password = State()


class ChangeEmailState(StatesGroup):
    new_email = State()
    current_password = State()


class ChangePasswordState(StatesGroup):
    current_password = State()
    new_password = State()


# Per-user authenticated CPM session.
# Tokens are kept only in MemoryStorage and are not written to disk.
SESSIONS = {}


def kb_home():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔐 Login CPM1", callback_data="login")],
            [
                InlineKeyboardButton(
                    text="📧 Change Email",
                    callback_data="change_email",
                ),
                InlineKeyboardButton(
                    text="🔑 Change Password",
                    callback_data="change_password",
                ),
            ],
            [InlineKeyboardButton(text="👤 Account Info", callback_data="info")],
            [InlineKeyboardButton(text="🚪 Logout", callback_data="logout")],
        ]
    )


def kb_cancel():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="cancel")]
        ]
    )


def make_xor_key(uid: str) -> bytes:
    chars = list(uid or "")
    if len(chars) >= 9:
        chars[1], chars[8] = chars[8], chars[1]
    if len(chars) >= 3:
        chars.pop(2)
    if len(chars) >= 5:
        chars.append(chars[4])
    return "".join(chars).encode("utf-8")


def xor_bytes(data: bytes, key: bytes) -> bytes:
    if not key:
        return data
    return bytes(data[i] ^ key[i % len(key)] for i in range(len(data)))


def decompress(data: bytes):
    if HAS_BROTLI:
        try:
            return brotli.decompress(data)
        except Exception:
            pass
    try:
        return zlib.decompress(data, zlib.MAX_WBITS | 16)
    except Exception:
        pass
    try:
        return zlib.decompress(data)
    except Exception:
        return None


def decrypt_aes(data: bytes, key: bytes):
    if not HAS_CRYPTO or not key:
        return None
    try:
        cipher = AES.new(key[:16], AES.MODE_CBC, b"\x00" * 16)
        return unpad(cipher.decrypt(data), 16)
    except Exception:
        return None


def _md5(value: str):
    return hashlib.md5((value or "").encode()).digest()


def _sha1(value: str):
    return hashlib.sha1((value or "").encode()).digest()[:16]


def build_aes_keys(uid, password=None, email=None):
    keys = [_md5("olzhas_carparking")]
    if password:
        keys += [_md5(password), _sha1(password)]
    if uid:
        keys += [_md5(uid), _sha1(uid)]
    if email:
        keys.append(_md5(email))
    return keys


class CPMReader:
    def __init__(self, data: bytes):
        self.buf = data
        self.pos = 0

    def has_bytes(self, n):
        return self.pos + n <= len(self.buf)

    def read_byte(self):
        if not self.has_bytes(1):
            return 0
        value = self.buf[self.pos]
        self.pos += 1
        return value

    def read_int(self):
        if not self.has_bytes(4):
            self.pos = len(self.buf)
            return 0
        value = struct.unpack_from("<i", self.buf, self.pos)[0]
        self.pos += 4
        return value

    def read_string(self):
        marker = self.read_int()
        if marker in (0, -1):
            return ""
        length = (-marker) - 1 if marker < -1 else marker
        if marker < -1:
            self.read_int()
        if length > 1_000_000:
            length = 1_000_000
        if not self.has_bytes(length):
            return ""
        value = self.buf[self.pos:self.pos + length].decode("utf-8", errors="replace")
        self.pos += length
        return value.replace("\x00", "").strip()


def parse_player_identity(buf: bytes):
    try:
        r = CPMReader(buf)
        if r.read_byte() == 0:
            return None
        name = r.read_string()
        r.read_int()  # money
        r.read_int()  # coin
        local_id = r.read_string()
        if not name and not local_id:
            return None
        return {"Name": name, "localID": local_id}
    except Exception:
        return None


def try_parse_player_record(buf: bytes):
    candidates = [buf]
    d1 = decompress(buf)
    if d1:
        candidates.append(d1)
        d2 = decompress(d1)
        if d2:
            candidates.append(d2)

    for candidate in candidates:
        if not candidate:
            continue
        if candidate[0] in (17, 23, 24):
            parsed = parse_player_identity(candidate)
            if parsed:
                return parsed
        try:
            clean = candidate[3:] if candidate[:3] == b"\xef\xbb\xbf" else candidate
            if clean and clean[0] == 123:
                obj = json.loads(clean.decode("utf-8"))
                if isinstance(obj, dict):
                    return obj
        except Exception:
            pass
    return None


def decrypt_player_record(base64_text, uid, password=None, email=None):
    try:
        buf = base64.b64decode(base64_text)
    except Exception:
        return {"success": False, "message": "Bad base64"}
    if len(buf) < 10:
        return {"success": False, "message": "Too small"}

    direct = try_parse_player_record(buf)
    if direct:
        return {"success": True, "record": direct}

    if uid:
        try:
            xp = xor_bytes(buf, make_xor_key(uid))
            d = decompress(xp)
            if d:
                parsed = try_parse_player_record(d)
                if parsed:
                    return {"success": True, "record": parsed}
        except Exception:
            pass

    for key in build_aes_keys(uid or "", password, email):
        plain = decrypt_aes(buf, key)
        if plain:
            parsed = try_parse_player_record(plain)
            if parsed:
                return {"success": True, "record": parsed}

    return {"success": False, "message": "Could not decrypt player record"}


def extract_player_details(record):
    if not isinstance(record, dict):
        return "Unknown", "Unknown"
    name = record.get("Name") or record.get("PlayerName") or record.get("name")
    player_id = (
        record.get("localID")
        or record.get("LocalID")
        or record.get("PlayerID")
        or record.get("PlayerId")
        or record.get("playerID")
    )
    return (
        str(name).strip() if name not in (None, "") else "Unknown",
        str(player_id).strip() if player_id not in (None, "") else "Unknown",
    )

def home_text(user_id: int) -> str:
    s = SESSIONS.get(user_id)
    if not s:
        return (
            "<b>🎮 CPM1 ACCOUNT BOT</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Status: <b>Not logged in</b>\n\n"
            "Login menggunakan <b>Email + Password</b> akun CPM1."
        )

    email = escape(s["email"])
    player = escape(str(s.get("player_name") or "Unknown"))
    player_id = escape(str(s.get("player_id") or "Unknown"))
    return (
        "<b>🎮 CPM1 ACCOUNT BOT</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Status: <b>✅ Logged in</b>\n\n"
        f"👤 Nama Akun CPM: <b>{player}</b>\n"
        f"🆔 Player ID: <code>{player_id}</code>\n"
        f"📧 Email: <code>{email}</code>\n"
        "Pilih fitur:"
    )


async def firebase_login(session, email: str, password: str):
    payload = {
        "email": email,
        "password": password,
        "returnSecureToken": True,
        "clientType": "CLIENT_TYPE_ANDROID",
    }

    async with session.post(
        FIREBASE_LOGIN_URL,
        params={"key": FIREBASE_API_KEY},
        json=payload,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        text = await resp.text()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {}

        if resp.status != 200:
            message = (
                data.get("error", {}).get("message")
                if isinstance(data, dict)
                else None
            )
            return {
                "ok": False,
                "message": message or f"Firebase login failed (HTTP {resp.status}).",
            }

        return {
            "ok": True,
            "id_token": data.get("idToken", ""),
            "refresh_token": data.get("refreshToken", ""),
            "firebase_uid": data.get("localId", ""),
            "email": data.get("email", email),
        }


async def firebase_update(
    session,
    id_token: str,
    *,
    email: str | None = None,
    password: str | None = None,
):
    payload = {
        "idToken": id_token,
        "returnSecureToken": True,
    }

    if email is not None:
        payload["email"] = email
    if password is not None:
        payload["password"] = password

    async with session.post(
        FIREBASE_UPDATE_URL,
        params={"key": FIREBASE_API_KEY},
        json=payload,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        text = await resp.text()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {}

        if resp.status != 200:
            message = (
                data.get("error", {}).get("message")
                if isinstance(data, dict)
                else None
            )
            return {
                "ok": False,
                "message": message or f"Account update failed (HTTP {resp.status}).",
            }

        return {
            "ok": True,
            "id_token": data.get("idToken", id_token),
            "refresh_token": data.get("refreshToken", ""),
            "email": data.get("email", email or ""),
            "firebase_uid": data.get("localId", ""),
        }


async def load_cpm_record(session, id_token: str, firebase_uid: str,
                          email: str, password: str):
    game_headers = {
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
        "User-Agent": "UnityPlayer/2022.3.62f2 (UnityWebRequest/1.0, libcurl/8.10.1-DEV)",
        "X-Unity-Version": "2022.3.62f2",
        "Authorization": f"Bearer {id_token}",
    }

    async with session.post(
        CPM_LOAD_URL,
        json={"data": None},
        headers=game_headers,
        timeout=aiohttp.ClientTimeout(total=35),
    ) as resp:
        body = (await resp.text()).strip()
        if resp.status != 200:
            log.error("GetPlayerRecords3 HTTP=%s body=%s", resp.status, body[:500])
            return {"ok": False, "message": f"CPM server returned HTTP {resp.status}."}

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            log.error("GetPlayerRecords3 invalid JSON: %s", body[:500])
            return {"ok": False, "message": "CPM server returned invalid JSON."}

        result = data.get("result") if isinstance(data, dict) else None
        if not result:
            log.error("GetPlayerRecords3 missing result: %s", body[:500])
            return {"ok": False, "message": "CPM player record was not returned."}

        decoded = decrypt_player_record(result, firebase_uid, password, email)
        if not decoded.get("success"):
            log.error("CPM record decode failed: %s", decoded.get("message"))
            return {"ok": False, "message": "CPM player record could not be decoded."}

        record = decoded.get("record") or {}
        log.info("CPM record loaded: Name=%r localID=%r",
                 record.get("Name"), record.get("localID"))
        return {"ok": True, "record": record}
@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(home_text(message.from_user.id), reply_markup=kb_home())


@router.callback_query(F.data == "login")
async def cb_login(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(LoginState.email)
    await call.answer()
    await call.message.edit_text(
        "<b>🔐 LOGIN CPM1</b>\n\n"
        "Masukkan <b>email CPM1</b> Anda.",
        reply_markup=kb_cancel(),
    )


@router.message(LoginState.email)
async def login_email(message: Message, state: FSMContext):
    email = (message.text or "").strip().lower()

    if "@" not in email or "." not in email:
        await message.answer(
            "❌ Format email tidak valid.\nKirim email yang benar.",
            reply_markup=kb_cancel(),
        )
        return

    await state.update_data(email=email)
    await state.set_state(LoginState.password)

    await message.answer(
        "<b>🔑 PASSWORD CPM1</b>\n\n"
        "Masukkan password akun CPM1.\n"
        "<i>Pesan password akan dihapus setelah diterima jika Telegram mengizinkannya.</i>",
        reply_markup=kb_cancel(),
    )


@router.message(LoginState.password)
async def login_password(message: Message, state: FSMContext):
    password = (message.text or "").strip()
    data = await state.get_data()
    email = data.get("email", "")

    try:
        await message.delete()
    except Exception:
        pass

    if not password:
        await message.answer("❌ Password tidak boleh kosong.")
        return

    loading = await message.answer("⏳ Memverifikasi login CPM1...")

    try:
        async with aiohttp.ClientSession() as session:
            auth = await firebase_login(session, email, password)

            if not auth["ok"]:
                await loading.edit_text(
                    "<b>❌ LOGIN GAGAL</b>\n\n"
                    f"{escape(auth['message'])}",
                    reply_markup=kb_home(),
                )
                await state.clear()
                return

            record_result = await load_cpm_record(
                session,
                auth["id_token"],
                auth["firebase_uid"],
                email,
                password,
            )

        if not record_result.get("ok"):
            await state.clear()
            await loading.edit_text(
                "<b>❌ LOGIN CPM1 BERHASIL, TETAPI DATA PROFIL GAGAL DIAMBIL</b>\\n"
                "━━━━━━━━━━━━━━━━━━\\n"
                f"📧 Email: <code>{escape(auth['email'])}</code>\\n"
                f"⚠️ {escape(str(record_result.get('message', 'Unknown error')))}",
                reply_markup=kb_home(),
            )
            return

        record = record_result.get("record") or {}
        player_name, player_id = extract_player_details(record)

        SESSIONS[message.from_user.id] = {
            "email": auth["email"],
            "id_token": auth["id_token"],
            "refresh_token": auth["refresh_token"],
            "firebase_uid": auth["firebase_uid"],
            "password": password,
            "player_name": player_name,
            "player_id": player_id,
        }

        await state.clear()
        await loading.edit_text(
            "<b>✅ LOGIN CPM1 BERHASIL</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👤 Nama Akun CPM: <b>{escape(str(player_name))}</b>\n"
            f"🆔 Player ID: <code>{escape(str(player_id))}</code>\n"
            f"📧 Email: <code>{escape(auth['email'])}</code>\n"
            "Sekarang Change Email dan Change Password sudah tersedia.",
            reply_markup=kb_home(),
        )

    except asyncio.TimeoutError:
        await loading.edit_text(
            "❌ Koneksi timeout. Silakan coba login lagi.",
            reply_markup=kb_home(),
        )
        await state.clear()
    except Exception as exc:
        log.exception("Login error")
        await loading.edit_text(
            "❌ Terjadi kesalahan saat login.\n"
            f"<code>{escape(str(exc)[:300])}</code>",
            reply_markup=kb_home(),
        )
        await state.clear()


@router.callback_query(F.data == "change_email")
async def cb_change_email(call: CallbackQuery, state: FSMContext):
    user_id = call.from_user.id
    if user_id not in SESSIONS:
        await call.answer("Login CPM1 terlebih dahulu.", show_alert=True)
        return

    await state.clear()
    await state.set_state(ChangeEmailState.new_email)
    await call.answer()
    await call.message.edit_text(
        "<b>📧 CHANGE EMAIL</b>\n\n"
        "Masukkan email baru akun CPM1.",
        reply_markup=kb_cancel(),
    )


@router.message(ChangeEmailState.new_email)
async def change_email_input(message: Message, state: FSMContext):
    new_email = (message.text or "").strip().lower()

    if "@" not in new_email or "." not in new_email:
        await message.answer("❌ Format email baru tidak valid.")
        return

    await state.update_data(new_email=new_email)
    await state.set_state(ChangeEmailState.current_password)

    await message.answer(
        "<b>🔐 VERIFIKASI</b>\n\n"
        "Masukkan password CPM1 saat ini untuk mengonfirmasi perubahan email.",
        reply_markup=kb_cancel(),
    )


@router.message(ChangeEmailState.current_password)
async def change_email_password(message: Message, state: FSMContext):
    current_password = (message.text or "").strip()
    user_id = message.from_user.id
    session_data = SESSIONS.get(user_id)

    try:
        await message.delete()
    except Exception:
        pass

    if not session_data:
        await state.clear()
        await message.answer("❌ Sesi login sudah tidak ada.", reply_markup=kb_home())
        return

    data = await state.get_data()
    new_email = data.get("new_email", "")

    loading = await message.answer("⏳ Memverifikasi password dan mengubah email...")

    try:
        async with aiohttp.ClientSession() as session:
            # Re-authenticate before changing the email.
            auth = await firebase_login(
                session,
                session_data["email"],
                current_password,
            )

            if not auth["ok"]:
                await loading.edit_text(
                    "❌ Password saat ini salah atau login sudah kedaluwarsa.",
                    reply_markup=kb_home(),
                )
                await state.clear()
                return

            result = await firebase_update(
                session,
                auth["id_token"],
                email=new_email,
            )

        if not result["ok"]:
            await loading.edit_text(
                "<b>❌ CHANGE EMAIL GAGAL</b>\n\n"
                f"{escape(result['message'])}",
                reply_markup=kb_home(),
            )
            await state.clear()
            return

        session_data.update(
            {
                "email": result.get("email") or new_email,
                "id_token": result.get("id_token", auth["id_token"]),
                "refresh_token": result.get(
                    "refresh_token",
                    auth.get("refresh_token", ""),
                ),
            }
        )
        SESSIONS[user_id] = session_data

        await state.clear()
        await loading.edit_text(
            "<b>✅ EMAIL BERHASIL DIUBAH</b>\n\n"
            f"📧 Email baru: <code>{escape(new_email)}</code>\n\n"
            "Gunakan email baru saat login berikutnya.",
            reply_markup=kb_home(),
        )

    except Exception as exc:
        log.exception("Change email error")
        await state.clear()
        await loading.edit_text(
            "❌ Gagal mengubah email.\n"
            f"<code>{escape(str(exc)[:300])}</code>",
            reply_markup=kb_home(),
        )


@router.callback_query(F.data == "change_password")
async def cb_change_password(call: CallbackQuery, state: FSMContext):
    if call.from_user.id not in SESSIONS:
        await call.answer("Login CPM1 terlebih dahulu.", show_alert=True)
        return

    await state.clear()
    await state.set_state(ChangePasswordState.current_password)
    await call.answer()
    await call.message.edit_text(
        "<b>🔑 CHANGE PASSWORD</b>\n\n"
        "Masukkan password CPM1 saat ini.",
        reply_markup=kb_cancel(),
    )


@router.message(ChangePasswordState.current_password)
async def change_password_current(message: Message, state: FSMContext):
    current_password = (message.text or "").strip()

    try:
        await message.delete()
    except Exception:
        pass

    if not current_password:
        await message.answer("❌ Password saat ini tidak boleh kosong.")
        return

    await state.update_data(current_password=current_password)
    await state.set_state(ChangePasswordState.new_password)

    await message.answer(
        "<b>🆕 PASSWORD BARU</b>\n\n"
        "Masukkan password baru CPM1.\n"
        "Minimal 6 karakter.",
        reply_markup=kb_cancel(),
    )


@router.message(ChangePasswordState.new_password)
async def change_password_new(message: Message, state: FSMContext):
    new_password = (message.text or "").strip()
    user_id = message.from_user.id
    session_data = SESSIONS.get(user_id)

    try:
        await message.delete()
    except Exception:
        pass

    if len(new_password) < 6:
        await message.answer("❌ Password baru minimal 6 karakter.")
        return

    data = await state.get_data()
    current_password = data.get("current_password", "")
    if new_password == current_password:
        await message.answer("❌ Password baru harus berbeda dari password saat ini.")
        return

    if not session_data:
        await state.clear()
        await message.answer("❌ Sesi login sudah tidak ada.", reply_markup=kb_home())
        return

    data = await state.get_data()
    current_password = data.get("current_password", "")

    loading = await message.answer("⏳ Memverifikasi dan mengubah password...")

    try:
        async with aiohttp.ClientSession() as session:
            auth = await firebase_login(
                session,
                session_data["email"],
                current_password,
            )

            if not auth["ok"]:
                await loading.edit_text(
                    "❌ Password saat ini salah.",
                    reply_markup=kb_home(),
                )
                await state.clear()
                return

            result = await firebase_update(
                session,
                auth["id_token"],
                password=new_password,
            )

        if not result["ok"]:
            await loading.edit_text(
                "<b>❌ CHANGE PASSWORD GAGAL</b>\n\n"
                f"{escape(result['message'])}",
                reply_markup=kb_home(),
            )
            await state.clear()
            return

        session_data.update(
            {
                "id_token": result.get("id_token", auth["id_token"]),
                "refresh_token": result.get(
                    "refresh_token",
                    auth.get("refresh_token", ""),
                ),
            }
        )
        SESSIONS[user_id] = session_data

        await state.clear()
        await loading.edit_text(
            "<b>✅ PASSWORD BERHASIL DIUBAH</b>\n\n"
            "Password CPM1 baru sudah disimpan oleh server.\n"
            "Gunakan password baru saat login berikutnya.",
            reply_markup=kb_home(),
        )

    except Exception as exc:
        log.exception("Change password error")
        await state.clear()
        await loading.edit_text(
            "❌ Gagal mengubah password.\n"
            f"<code>{escape(str(exc)[:300])}</code>",
            reply_markup=kb_home(),
        )


@router.callback_query(F.data == "info")
async def cb_info(call: CallbackQuery):
    s = SESSIONS.get(call.from_user.id)

    if not s:
        await call.answer("Login CPM1 terlebih dahulu.", show_alert=True)
        return

    await call.answer()
    await call.message.edit_text(
        "<b>👤 CPM1 ACCOUNT INFO</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 Nama Akun CPM: <b>{escape(str(s.get('player_name') or 'Unknown'))}</b>\n"
        f"🆔 Player ID: <code>{escape(str(s.get('player_id') or 'Unknown'))}</code>\n"
        f"📧 Email: <code>{escape(s['email'])}</code>",
        reply_markup=kb_home(),
    )


@router.callback_query(F.data == "logout")
async def cb_logout(call: CallbackQuery, state: FSMContext):
    SESSIONS.pop(call.from_user.id, None)
    await state.clear()
    await call.answer("Logout berhasil.")
    await call.message.edit_text(
        "<b>🚪 LOGOUT BERHASIL</b>\n\n"
        "Sesi CPM1 sudah dihapus dari memori bot.",
        reply_markup=kb_home(),
    )


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.answer()
    await call.message.edit_text(
        home_text(call.from_user.id),
        reply_markup=kb_home(),
    )


async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN belum diatur. Set environment variable BOT_TOKEN."
        )

    log.info("CPM1 bot starting...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
