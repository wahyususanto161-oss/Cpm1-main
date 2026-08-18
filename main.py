import asyncio
import json
import logging
import os
from html import escape

import aiohttp
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
BOT_TOKEN = "MASUKKAN_BOT_TOKEN_BARU_DARI_BOTFATHER_DI_SINI"
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


def _find_record_value(record, keys):
    """Find a player field even when the CPM API uses different key casing."""
    if not isinstance(record, dict):
        return None

    wanted = {str(k).lower().replace("_", "").replace("-", "") for k in keys}

    for key, value in record.items():
        normalized = str(key).lower().replace("_", "").replace("-", "")
        if normalized in wanted and value not in (None, ""):
            return value

    # Some responses may nest player data inside another object.
    for value in record.values():
        if isinstance(value, dict):
            found = _find_record_value(value, keys)
            if found not in (None, ""):
                return found

    return None


def extract_player_details(record):
    """Extract the CPM display name and player ID from the player record."""
    name = _find_record_value(
        record,
        ("Name", "PlayerName", "Player_Name", "Username", "NickName", "Nickname"),
    )
    player_id = _find_record_value(
        record,
        ("PlayerID", "PlayerId", "Player_ID", "player_id", "ID", "Id", "uid"),
    )
    return (
        str(name) if name not in (None, "") else "Unknown",
        str(player_id) if player_id not in (None, "") else "Unknown",
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
        f"🔐 Firebase UID: <code>{uid}</code>\n\n"
        "Pilih fitur:"
    )


async def firebase_login(session, email: str, password: str):
    payload = {
        "email": email,
        "password": password,
        "returnSecureToken": True,
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


async def load_cpm_record(session, firebase_uid: str, email: str, password: str):
    # GetPlayerRecords3 expects the CPM player identifier and credentials.
    payload = {
        "uid": firebase_uid,
        "email": email,
        "password": password,
        "fk": FIREBASE_API_KEY,
    }

    async with session.post(
        CPM_LOAD_URL,
        json=payload,
        headers={"Accept": "application/json, text/plain, */*"},
        timeout=aiohttp.ClientTimeout(total=35),
    ) as resp:
        text = (await resp.text()).strip()

        if resp.status != 200:
            return {
                "ok": False,
                "message": f"CPM server returned HTTP {resp.status}.",
            }

        if not text:
            return {"ok": False, "message": "CPM server returned an empty response."}

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {}

        # Some CPM versions return the player record directly as base64/record.
        if isinstance(data, dict):
            if data.get("error") and not (data.get("base64") or data.get("record")):
                return {"ok": False, "message": str(data["error"])}

            if data.get("base64") or data.get("record"):
                return {
                    "ok": True,
                    "record": data,
                }

        # Keep login successful even when the player-record endpoint uses
        # a different response format; Firebase authentication is already
        # verified at this point.
        return {
            "ok": True,
            "record": data if isinstance(data, dict) else {},
        }


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
                auth["firebase_uid"],
                email,
                password,
            )

        record = record_result.get("record") or {}
        player_name, player_id = extract_player_details(record)

        SESSIONS[message.from_user.id] = {
            "email": auth["email"],
            "id_token": auth["id_token"],
            "refresh_token": auth["refresh_token"],
            "firebase_uid": auth["firebase_uid"],
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
            f"🔐 Firebase UID: <code>{escape(auth['firebase_uid'])}</code>\n\n"
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
        f"📧 Email: <code>{escape(s['email'])}</code>\n"
        f"🔐 Firebase UID: <code>{escape(s['firebase_uid'])}</code>",
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
