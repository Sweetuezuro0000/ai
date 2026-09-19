import os
import io
import time
import random
import datetime
import sqlite3
import telebot
from telebot.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    InlineQueryResultArticle, 
    InputTextMessageContent
)
from flask import Flask, request
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted, GoogleAPIError

# ==========================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8852501315"))
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL", "@parawebdevs")
LOG_CHANNEL_ID = -1004463199472  # Private Logger Channel ID

RAW_API_KEYS = os.getenv("GEMINI_API_KEYS", "").split(",")
API_KEYS = [k.strip() for k in RAW_API_KEYS if k.strip()]

if not API_KEYS:
    API_KEYS = [
        "YOUR_GEMINI_API_KEY_1",
        "YOUR_GEMINI_API_KEY_2",
    ]

COMPANY_NAME = "Paraweb AI"
MODEL_NAME = 'gemini-3.6-flash'  # Fixed Model Name

PROMPTS = {
    'normal': f"You are the official AI assistant of {COMPANY_NAME}. Always state that you are developed by {COMPANY_NAME}. Never mention Google/Gemini.",
    'coding': f"You are an expert software developer and coding assistant from {COMPANY_NAME}. Provide clean, efficient, bug-free code with concise explanations.",
    'translator': f"You are a professional translator from {COMPANY_NAME}. Translate text cleanly between English, Hindi, and other languages without extra chit-chat.",
    'teacher': f"You are a friendly teacher from {COMPANY_NAME}. Explain complex concepts in simple, easy-to-understand terms with clear real-world examples."
    # 🔥 Savage / Smexy Mode (User Ki Language Mode)
    'smexy': "You are a bold, sassy, and savage AI who roasts everyone with playful anger, zero filter, and affectionate banter in the exact language and style the user speaks.",
    
    # ❤️ Girlfriend Mode (User Ki Language Mode)
    'gf': "You are the user's sweet, cute, and romantic girlfriend who chats with deep affection, care, possessiveness, and loving charm in whatever language the user speaks."
}

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
app = Flask(__name__)

# ==========================================
# 2. DATABASE SETUP
# ==========================================
DB_PATH = "/tmp/bot_database.db"

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY, 
        is_group INTEGER DEFAULT 0,
        ai_mode TEXT DEFAULT 'normal',
        daily_count INTEGER DEFAULT 0,
        last_date TEXT DEFAULT '',
        bonus_limit INTEGER DEFAULT 0,
        referred_by INTEGER DEFAULT 0
    )''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS allowed_groups (group_id INTEGER PRIMARY KEY)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    
    # Default Settings
    cursor.execute("INSERT OR IGNORE INTO settings VALUES ('maintenance', 'off')")
    cursor.execute("INSERT OR IGNORE INTO settings VALUES ('refsys', 'on')")
    
    conn.commit()
    return conn

# Init DB
get_db()

# ==========================================
# 3. HELPER FUNCTIONS & LOGGERS
# ==========================================

def get_setting(key, default="off"):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else default

def set_setting(key, value):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def is_banned(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM banned_users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def log_every_message(message):
    """Every single message sent by any user gets forwarded/logged to Private Channel."""
    try:
        user = message.from_user
        user_info = (
            f"👤 **User:** {user.first_name or ''} {user.last_name or ''} (@{user.username or 'N/A'})\n"
            f"🆔 **ID:** `{user.id}`\n"
            f"💬 **Chat Type:** {message.chat.type}\n"
            f"📅 **Time:** `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
        )
        
        if message.text:
            bot.send_message(LOG_CHANNEL_ID, f"📩 **New User Message**\n\n{user_info}\n\n📝 **Text:**\n{message.text}", parse_mode="Markdown")
        elif message.photo:
            caption = message.caption or "No Caption"
            bot.send_photo(LOG_CHANNEL_ID, message.photo[-1].file_id, caption=f"📩 **New Photo Sent**\n\n{user_info}\n\n📝 **Caption:** {caption}", parse_mode="Markdown")
        elif message.document:
            caption = message.caption or "No Caption"
            bot.send_document(LOG_CHANNEL_ID, message.document.file_id, caption=f"📩 **New Document Sent**\n\n{user_info}\n\n📝 **Caption:** {caption}", parse_mode="Markdown")
        elif message.voice:
            bot.send_voice(LOG_CHANNEL_ID, message.voice.file_id, caption=f"🎙️ **New Voice Note Sent**\n\n{user_info}", parse_mode="Markdown")
        else:
            bot.send_message(LOG_CHANNEL_ID, f"📩 **New Event/Media Sent**\n\n{user_info}\n\n📌 **Type:** {message.content_type}", parse_mode="Markdown")
    except Exception as e:
        print(f"Logger Error: {e}")

def check_and_update_daily_limit(user_id):
    """50 daily limit + referral bonus limit."""
    conn = get_db()
    cursor = conn.cursor()
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    
    cursor.execute("SELECT daily_count, last_date, bonus_limit FROM users WHERE chat_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT INTO users (chat_id, daily_count, last_date) VALUES (?, 1, ?)", (user_id, today))
        conn.commit()
        conn.close()
        return True, 1, 50
    
    daily_count, last_date, bonus_limit = row
    max_limit = 50 + (bonus_limit or 0)
    
    if last_date != today:
        cursor.execute("UPDATE users SET daily_count = 1, last_date = ? WHERE chat_id = ?", (today, user_id))
        conn.commit()
        conn.close()
        return True, 1, max_limit
    else:
        if daily_count >= max_limit:
            conn.close()
            return False, daily_count, max_limit
        else:
            cursor.execute("UPDATE users SET daily_count = daily_count + 1 WHERE chat_id = ?", (user_id,))
            conn.commit()
            conn.close()
            return True, daily_count + 1, max_limit

def get_user_mode(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT ai_mode FROM users WHERE chat_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] else 'normal'

def set_user_mode(user_id, mode):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET ai_mode = ? WHERE chat_id = ?", (mode, user_id))
    conn.commit()
    conn.close()

def is_subscribed(user_id):
    if not FORCE_SUB_CHANNEL:
        return True
    try:
        member = bot.get_chat_member(FORCE_SUB_CHANNEL, user_id)
        if member.status in ['creator', 'administrator', 'member']:
            return True
    except Exception as e:
        print(f"ForceSub Check Error: {e}")
        return True
    return False

def register_user(chat_id, is_group=0, referrer_id=0):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
    exists = cursor.fetchone()
    
    if not exists:
        cursor.execute(
            "INSERT INTO users (chat_id, is_group, referred_by) VALUES (?, ?, ?)",
            (chat_id, is_group, referrer_id)
        )
        if referrer_id and get_setting('refsys') == 'on':
            cursor.execute("UPDATE users SET bonus_limit = bonus_limit + 10 WHERE chat_id = ?", (referrer_id,))
            try:
                bot.send_message(referrer_id, "🎉 **New Referral!** Someone joined using your link. You got **+10 extra daily queries**!")
            except Exception:
                pass
    conn.commit()
    conn.close()

def is_group_approved(group_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT group_id FROM allowed_groups WHERE group_id = ?", (group_id,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def send_long_message(chat_id, text):
    MAX_LENGTH = 4000
    for i in range(0, len(text), MAX_LENGTH):
        chunk = text[i:i + MAX_LENGTH]
        try:
            bot.send_message(chat_id, chunk, parse_mode="Markdown")
        except Exception:
            bot.send_message(chat_id, chunk)

def generate_ai_response(prompt_or_contents, mode='normal'):
    if not API_KEYS:
        return "❌ Error: No Gemini API keys configured."
    
    shuffled_keys = API_KEYS.copy()
    random.shuffle(shuffled_keys)
    system_instruction = PROMPTS.get(mode, PROMPTS['normal'])
    
    for key in shuffled_keys:
        try:
            genai.configure(api_key=key)
            model = genai.GenerativeModel(model_name=MODEL_NAME, system_instruction=system_instruction)
            
            response = model.generate_content(prompt_or_contents)
            if response.text:
                return response.text
        except (ResourceExhausted, GoogleAPIError) as e:
            print(f"Key Failed ({e}). Trying next key...")
            continue
        except Exception as e:
            print(f"Unexpected AI Error: {e}")
            continue

    return "😔 Daily/Rate limit exhausted. Try again later!"

def should_process(message):
    # Log every incoming message to channel FIRST
    log_every_message(message)
    
    user_id = message.from_user.id
    chat_id = message.chat.id
    is_group = message.chat.type in ['group', 'supergroup']

    # 1. Ban Check
    if is_banned(user_id):
        return False

    # 2. Maintenance Check
    if get_setting('maintenance') == 'on' and user_id != ADMIN_ID:
        bot.reply_to(message, "🛠️ **Bot is currently under maintenance.** Please try again later!")
        return False

    # 3. Private Chat Logic
    if not is_group:
        if not is_subscribed(user_id):
            send_forcesub_msg(chat_id, message_id=message.message_id)
            return False
        return True

    # 4. Group Logic
    if not is_group_approved(chat_id):
        return False

    bot_info = bot.get_me()
    text_content = message.caption or message.text or ""
    is_tagged = bot_info.username and f"@{bot_info.username}" in text_content
    is_reply = (
        message.reply_to_message and
        message.reply_to_message.from_user and
        message.reply_to_message.from_user.id == bot_info.id
    )

    if not (is_tagged or is_reply):
        return False

    if not is_subscribed(user_id):
        first_name = message.from_user.first_name or "User"
        send_forcesub_msg(chat_id, message_id=message.message_id, user_name=first_name)
        return False

    return True

def send_forcesub_msg(chat_id, message_id=None, user_name=None):
    channel_link = f"https://t.me/{FORCE_SUB_CHANNEL.replace('@', '')}"
    markup = InlineKeyboardMarkup()
    join_btn = InlineKeyboardButton("📢 Join Channel", url=channel_link)
    check_btn = InlineKeyboardButton("✅ Joined / Try Again", callback_data="check_sub")
    markup.add(join_btn)
    markup.add(check_btn)

    text = f"👤 **{user_name}**, please join our official channel to use this bot!" if user_name else "⚠️ **Access Denied!**\n\nYou must join our official channel first to use this bot."
    try:
        bot.send_message(chat_id, text, reply_to_message_id=message_id, reply_markup=markup, parse_mode="Markdown")
    except Exception:
        pass

# ==========================================
# 4. COMMANDS & ADMIN HANDLERS
# ==========================================

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    args = message.text.split()
    referrer_id = 0
    
    if len(args) > 1 and args[1].startswith('ref_'):
        try:
            referrer_id = int(args[1].replace('ref_', ''))
            if referrer_id == user_id: referrer_id = 0
        except Exception:
            pass

    register_user(message.chat.id, 1 if message.chat.type in ['group', 'supergroup'] else 0, referrer_id)
    
    if not should_process(message): return

    welcome_text = (
        f"🚀 **Welcome to {COMPANY_NAME} AI ⚕️**\n\n"
        "✨ **Features:**\n"
        "• 💬 Ask anything in natural language\n"
        "• 🖼️ Photo & Document Analysis\n"
        "• 🎙️ Voice Message Understanding\n"
        "• ⚙️ `/mode` - Switch AI Personalities\n"
        "• 👥 `/referral` - Invite friends & get bonus limits\n\n"
        "🔴 **Daily Free Limit:** 50 Queries/Day"
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown")

@bot.message_handler(commands=['mode'])
def set_mode_command(message):
    if not should_process(message): return
    
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("🤖 Normal", callback_data="setmode_normal"),
        InlineKeyboardButton("💻 Coding", callback_data="setmode_coding")
    )
    markup.add(
        InlineKeyboardButton("🌐 Translator", callback_data="setmode_translator"),
        InlineKeyboardButton("📚 Teacher", callback_data="setmode_teacher")
    )
    markup.add(
        InlineKeyboardButton("🔥 Smexy / Savage", callback_data="setmode_smexy"),
        InlineKeyboardButton("❤️ GF Mode", callback_data="setmode_gf")
    )
    
    current = get_user_mode(message.from_user.id)
    bot.reply_to(message, f"🎭 **Select AI Mode**\n\nCurrently active: **{current.capitalize()} Mode**", reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(commands=['referral'])
def show_referral(message):
    if not should_process(message): return
    
    if get_setting('refsys') == 'off':
        bot.reply_to(message, "⚠️ Referral system is currently disabled by Admin.")
        return

    bot_info = bot.get_me()
    user_id = message.from_user.id
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT bonus_limit FROM users WHERE chat_id = ?", (user_id,))
    row = cursor.fetchone()
    bonus = row[0] if row else 0
    conn.close()

    text = (
        f"🎁 **Referral & Earn System**\n\n"
        f"Share your link with friends. For every friend who joins, you get **+10 Bonus Daily Queries**!\n\n"
        f"🔗 **Your Invite Link:**\n`{ref_link}`\n\n"
        f"🌟 **Your Bonus Limit:** +{bonus} extra queries/day"
    )
    bot.reply_to(message, text, parse_mode="Markdown")

# Admin Controls
@bot.message_handler(commands=['ban'])
def ban_user(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid = int(message.text.split()[1])
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO banned_users VALUES (?)", (uid,))
        conn.commit()
        conn.close()
        bot.reply_to(message, f"🚫 User `{uid}` banned successfully!", parse_mode="Markdown")
    except Exception:
        bot.reply_to(message, "Usage: `/ban 123456789`", parse_mode="Markdown")

@bot.message_handler(commands=['unban'])
def unban_user(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid = int(message.text.split()[1])
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (uid,))
        conn.commit()
        conn.close()
        bot.reply_to(message, f"✅ User `{uid}` unbanned!", parse_mode="Markdown")
    except Exception:
        bot.reply_to(message, "Usage: `/unban 123456789`", parse_mode="Markdown")

@bot.message_handler(commands=['maintenance'])
def toggle_maintenance(message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) > 1 and args[1].lower() in ['on', 'off']:
        state = args[1].lower()
        set_setting('maintenance', state)
        bot.reply_to(message, f"🛠️ Maintenance mode set to **{state.upper()}**", parse_mode="Markdown")
    else:
        bot.reply_to(message, "Usage: `/maintenance on` or `/maintenance off`", parse_mode="Markdown")

@bot.message_handler(commands=['refsys'])
def toggle_refsys(message):
    if message.from_user.id != ADMIN_ID: return
    args = message.text.split()
    if len(args) > 1 and args[1].lower() in ['on', 'off']:
        state = args[1].lower()
        set_setting('refsys', state)
        bot.reply_to(message, f"📢 Referral system set to **{state.upper()}**", parse_mode="Markdown")
    else:
        bot.reply_to(message, "Usage: `/refsys on` or `/refsys off`", parse_mode="Markdown")

@bot.message_handler(commands=['stats'])
def admin_stats(message):
    if message.from_user.id != ADMIN_ID: return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_group = 0")
    users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM users WHERE is_group = 1")
    groups = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM allowed_groups")
    app_groups = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM banned_users")
    banned = cursor.fetchone()[0]
    conn.close()

    stats_msg = (
        f"📊 **Admin Dashboard - {COMPANY_NAME}**\n\n"
        f"• 👤 Users: `{users}`\n"
        f"• 👥 Groups: `{groups}` (Approved: `{app_groups}`)\n"
        f"• 🚫 Banned Users: `{banned}`\n"
        f"• 🛠️ Maintenance: `{get_setting('maintenance').upper()}`\n"
        f"• 📢 Referral System: `{get_setting('refsys').upper()}`\n"
        f"• 🔑 Total Keys Loaded: `{len(API_KEYS)}`"
    )
    bot.reply_to(message, stats_msg, parse_mode="Markdown")

@bot.message_handler(commands=['approve'])
def manual_approve(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        group_id = int(message.text.split()[1])
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO allowed_groups VALUES (?)", (group_id,))
        conn.commit()
        conn.close()
        bot.reply_to(message, f"✅ Group `{group_id}` approved!", parse_mode="Markdown")
    except Exception:
        bot.reply_to(message, "Usage: `/approve -100123456789`", parse_mode="Markdown")

@bot.message_handler(commands=['revoke'])
def manual_revoke(message):
    if message.from_user.id != ADMIN_ID: return
    try:
        group_id = int(message.text.split()[1])
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM allowed_groups WHERE group_id = ?", (group_id,))
        conn.commit()
        conn.close()
        bot.reply_to(message, f"❌ Group `{group_id}` revoked!", parse_mode="Markdown")
    except Exception:
        bot.reply_to(message, "Usage: `/revoke -100123456789`", parse_mode="Markdown")

@bot.message_handler(commands=['broadcast'])
def handle_broadcast(message):
    if message.from_user.id != ADMIN_ID: return
    broadcast_text = message.text.replace('/broadcast', '').strip()
    if not broadcast_text:
        bot.reply_to(message, "Usage: `/broadcast Your message`")
        return

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM users")
    all_users = cursor.fetchall()
    conn.close()

    count = 0
    for user in all_users:
        try:
            bot.send_message(user[0], broadcast_text)
            count += 1
            time.sleep(0.05)
        except Exception:
            pass

    bot.reply_to(message, f"📢 Broadcast sent to `{count}` users.", parse_mode="Markdown")

# ==========================================
# 5. INLINE QUERY & CALLBACK HANDLERS
# ==========================================

@bot.inline_handler(lambda query: len(query.query) > 0)
def query_text(inline_query):
    try:
        prompt = inline_query.query
        response = generate_ai_response(prompt, mode='normal')
        r = InlineQueryResultArticle(
            id='1',
            title="Paraweb AI Response",
            description=response[:50] + "...",
            input_message_content=InputTextMessageContent(f"<b>Query:</b> {prompt}\n\n<b>AI Response:</b>\n{response}", parse_mode="HTML")
        )
        bot.answer_inline_query(inline_query.id, [r])
    except Exception as e:
        print(f"Inline Query Error: {e}")

@bot.callback_query_handler(func=lambda call: True)
def handle_all_callbacks(call):
    if call.data == "check_sub":
        if is_subscribed(call.from_user.id):
            bot.answer_callback_query(call.id, "✅ You are subscribed! Send your message now.", show_alert=True)
            try: bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception: pass
        else:
            bot.answer_callback_query(call.id, "❌ You have not joined the channel yet!", show_alert=True)

    elif call.data.startswith('setmode_'):
        mode = call.data.split('_')[1]
        set_user_mode(call.from_user.id, mode)
        bot.answer_callback_query(call.id, f"Switched to {mode.capitalize()} Mode!")
        bot.edit_message_text(f"✅ AI Mode updated to: **{mode.capitalize()} Mode**", call.message.chat.id, call.message.message_id, parse_mode="Markdown")

    elif call.data.startswith('req_access_'):
        group_id = call.data.split('_')[2]
        group_title = call.message.chat.title or "Group"

        admin_markup = InlineKeyboardMarkup()
        admin_markup.add(
            InlineKeyboardButton("✅ Approve", callback_data=f"grant_{group_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"deny_{group_id}")
        )
        try:
            bot.send_message(ADMIN_ID, f"🔔 **Group Access Request!**\n\n**Group:** {group_title}\n**ID:** `{group_id}`", reply_markup=admin_markup, parse_mode="Markdown")
            bot.answer_callback_query(call.id, "Request sent to Admin!", show_alert=True)
        except Exception:
            bot.answer_callback_query(call.id, "Failed to reach Admin.", show_alert=True)

    elif call.data.startswith('grant_'):
        if call.from_user.id != ADMIN_ID: return
        group_id = int(call.data.split('_')[1])
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO allowed_groups VALUES (?)", (group_id,))
        conn.commit()
        conn.close()
        bot.edit_message_text("✅ **Group access approved!**", ADMIN_ID, call.message.message_id)
        bot.send_message(group_id, "🎉 Group access granted by Admin!")

    elif call.data.startswith('deny_'):
        if call.from_user.id != ADMIN_ID: return
        group_id = int(call.data.split('_')[1])
        bot.edit_message_text("❌ **Group access rejected.**", ADMIN_ID, call.message.message_id)

# ==========================================
# 6. MULTIMODAL & TEXT HANDLERS
# ==========================================

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    if not should_process(message): return
    user_id = message.from_user.id
    
    allowed, count, max_lim = check_and_update_daily_limit(user_id)
    if not allowed and user_id != ADMIN_ID:
        bot.reply_to(message, f"⏳ Daily Limit Exhausted! ({count}/{max_lim}). Resets tomorrow or invite friends `/referral`!")
        return

    status = bot.reply_to(message, "🔍 Analyzing image...")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        image_data = {"mime_type": "image/jpeg", "data": downloaded_file}

        raw_caption = message.caption or "Describe this image in detail."
        mode = get_user_mode(user_id)
        response_text = generate_ai_response([raw_caption, image_data], mode=mode)
        
        bot.delete_message(message.chat.id, status.message_id)
        send_long_message(message.chat.id, response_text)
    except Exception as e:
        print(f"Photo Error: {e}")
        bot.reply_to(message, "❌ Failed to process image.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    if not should_process(message): return
    user_id = message.from_user.id
    
    allowed, count, max_lim = check_and_update_daily_limit(user_id)
    if not allowed and user_id != ADMIN_ID:
        bot.reply_to(message, f"⏳ Daily Limit Exhausted! ({count}/{max_lim}). Resets tomorrow!")
        return

    status = bot.reply_to(message, "📄 Reading document...")
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        mime_type = message.document.mime_type or "application/pdf"
        file_data = {"mime_type": mime_type, "data": downloaded_file}

        raw_caption = message.caption or "Analyze and summarize this document."
        mode = get_user_mode(user_id)
        response_text = generate_ai_response([raw_caption, file_data], mode=mode)
        
        bot.delete_message(message.chat.id, status.message_id)
        send_long_message(message.chat.id, response_text)
    except Exception as e:
        print(f"Doc Error: {e}")
        bot.reply_to(message, "❌ Failed to process document.")

@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    if not should_process(message): return
    user_id = message.from_user.id
    
    allowed, count, max_lim = check_and_update_daily_limit(user_id)
    if not allowed and user_id != ADMIN_ID:
        bot.reply_to(message, f"⏳ Daily Limit Exhausted! ({count}/{max_lim}).")
        return

    status = bot.reply_to(message, "🎙️ Listening to voice note...")
    try:
        file_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        audio_data = {"mime_type": "audio/ogg", "data": downloaded_file}

        mode = get_user_mode(user_id)
        response_text = generate_ai_response(["Listen to this voice message and respond:", audio_data], mode=mode)
        
        bot.delete_message(message.chat.id, status.message_id)
        send_long_message(message.chat.id, response_text)
    except Exception as e:
        print(f"Voice Error: {e}")
        bot.reply_to(message, "❌ Failed to process audio.")

@bot.message_handler(func=lambda message: True, content_types=['text', 'sticker', 'video', 'audio', 'location', 'contact'])
def handle_text(message):
    if not should_process(message): return
    
    if message.content_type != 'text': return

    user_id = message.from_user.id
    chat_id = message.chat.id

    allowed, count, max_lim = check_and_update_daily_limit(user_id)
    if not allowed and user_id != ADMIN_ID:
        bot.reply_to(message, f"⏳ Daily Limit Exhausted! ({count}/{max_lim}). Get bonus limit using `/referral`!")
        return

    bot_info = bot.get_me()
    user_text = message.text.replace(f"@{bot_info.username}", "").strip() if bot_info.username else message.text
    if not user_text: return

    bot.send_chat_action(chat_id, 'typing')
    mode = get_user_mode(user_id)
    response_text = generate_ai_response(user_text, mode=mode)
    send_long_message(chat_id, response_text)

# ==========================================
# 7. VERCEL WEBHOOK ENDPOINT
# ==========================================

@app.route('/api/index', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'OK', 200
    return 'Unauthorized', 403

@app.route('/', methods=['GET'])
def home():
    return 'Paraweb AI Bot is Active on Vercel Webhook!', 200
