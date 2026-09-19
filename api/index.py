import os
import io
import time
import random
import sqlite3
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted, GoogleAPIError

# ==========================================
# 1. CONFIGURATION & ENVIRONMENT VARIABLES
# ==========================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8852501315"))
FORCE_SUB_CHANNEL = os.getenv("FORCE_SUB_CHANNEL", "@parawebdevs")

# 10 या उससे ज़्यादा API Keys की लिस्ट (कॉमा से सेपरेटेड या ऐरे)
RAW_API_KEYS = os.getenv("GEMINI_API_KEYS", "").split(",")
API_KEYS = [k.strip() for k in RAW_API_KEYS if k.strip()]

# अगर Environment Variables में न हो तो यहाँ नीचे अपनी 10+ Keys डाल सकते हैं
if not API_KEYS:
    API_KEYS = [
        "YOUR_GEMINI_API_KEY_1",
        "YOUR_GEMINI_API_KEY_2",
        "YOUR_GEMINI_API_KEY_3",
        # ... अपनी 10+ Keys यहाँ डालें
    ]

COMPANY_NAME = "Paraweb AI"
MODEL_NAME = 'gemini-2.5-flash'

SYSTEM_PROMPT = f"""
You are the official AI assistant of {COMPANY_NAME}.
1. Always state that you are an AI assistant developed by {COMPANY_NAME}.
2. Never mention 'Google', 'Gemini', or any other underlying platform/company name.
3. Provide accurate, clear, and concise responses in the user's language.
"""

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)
app = Flask(__name__)

# ==========================================
# 2. DATABASE SETUP (Vercel Ephemeral / Turso ready)
# ==========================================
# Vercel पर temp storage /tmp/ में बनती है
DB_PATH = "/tmp/bot_database.db"

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (chat_id INTEGER PRIMARY KEY, is_group INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS allowed_groups (group_id INTEGER PRIMARY KEY)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS message_logs (chat_id INTEGER, timestamp REAL)''')
    conn.commit()
    return conn

conn = get_db()
cursor = conn.cursor()

# ==========================================
# 3. HELPER & CHECKER FUNCTIONS
# ==========================================

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

def is_rate_limited(chat_id):
    now = time.time()
    one_hour_ago = now - 3600
    cursor.execute("DELETE FROM message_logs WHERE timestamp < ?", (one_hour_ago,))
    cursor.execute("SELECT COUNT(*) FROM message_logs WHERE chat_id = ?", (chat_id,))
    count = cursor.fetchone()[0]

    if count >= 50:
        return True

    cursor.execute("INSERT INTO message_logs VALUES (?, ?)", (chat_id, now))
    conn.commit()
    return False

def register_user(chat_id, is_group=0):
    cursor.execute("INSERT OR IGNORE INTO users VALUES (?, ?)", (chat_id, is_group))
    conn.commit()

def is_group_approved(group_id):
    cursor.execute("SELECT group_id FROM allowed_groups WHERE group_id = ?", (group_id,))
    return cursor.fetchone() is not None

def send_long_message(chat_id, text):
    MAX_LENGTH = 4000
    for i in range(0, len(text), MAX_LENGTH):
        chunk = text[i:i + MAX_LENGTH]
        try:
            bot.send_message(chat_id, chunk, parse_mode="Markdown")
        except Exception:
            bot.send_message(chat_id, chunk)

# Multi-Key Rotation Algorithm for 10+ Keys
def generate_ai_response(prompt_or_contents):
    if not API_KEYS:
        return "❌ Error: No Gemini API keys configured."
    
    shuffled_keys = API_KEYS.copy()
    random.shuffle(shuffled_keys)  # Dynamic load balancing
    
    for key in shuffled_keys:
        try:
            genai.configure(api_key=key)
            model = genai.GenerativeModel(model_name=MODEL_NAME, system_instruction=SYSTEM_PROMPT)
            
            if isinstance(prompt_or_contents, list):
                response = model.generate_content(prompt_or_contents)
            else:
                response = model.generate_content(prompt_or_contents)
                
            if response.text:
                return response.text
        except (ResourceExhausted, GoogleAPIError) as e:
            print(f"Key Rate-Limited/Failed. Trying next key... Error: {e}")
            continue
        except Exception as e:
            print(f"Unexpected AI Error: {e}")
            continue

    return "😔 Daily/Rate limit exhausted across all configured API keys. Try again later!"

# ==========================================
# 4. FORCESUB & PROCESS LOGIC
# ==========================================

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
    except Exception as e:
        print(f"Failed to send ForceSub message: {e}")

def should_process(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    is_group = message.chat.type in ['group', 'supergroup']

    if not is_group:
        if not is_subscribed(user_id):
            send_forcesub_msg(chat_id, message_id=message.message_id)
            return False
        return True

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

# ==========================================
# 5. HANDLERS & CALLBACKS
# ==========================================

@bot.callback_query_handler(func=lambda call: call.data == "check_sub")
def handle_check_sub(call):
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "✅ You are subscribed! Send your message now.", show_alert=True)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
    else:
        bot.answer_callback_query(call.id, "❌ You have not joined the channel yet!", show_alert=True)

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    register_user(message.chat.id, 1 if message.chat.type in ['group', 'supergroup'] else 0)
    welcome_text = (
        f"🚀 **Welcome to {COMPANY_NAME} ⚕️**\n\n"
        "✨ **Available Features:**\n"
        "• 💬 AI Chat & Q&A\n"
        "• 🖼️ Photo Analysis\n"
        "• 📄 PDF & Document Reader\n"
        "• 🎙️ Voice Note Processing\n\n"
        "⚙️ **Commands:** `/clear` (Reset memory)"
    )
    bot.reply_to(message, welcome_text, parse_mode="Markdown")

@bot.message_handler(content_types=['photo'])
def handle_photo(message):
    if not should_process(message): return
    chat_id = message.chat.id
    if is_rate_limited(chat_id):
        bot.reply_to(message, "⏳ Hourly limit reached (50 msgs/hr).")
        return

    status = bot.reply_to(message, "🔍 Analyzing image...")
    try:
        file_info = bot.get_file(message.photo[-1].file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        image_data = {"mime_type": "image/jpeg", "data": downloaded_file}

        raw_caption = message.caption or "Describe this image in detail."
        bot_info = bot.get_me()
        prompt = raw_caption.replace(f"@{bot_info.username}", "").strip() if bot_info.username else raw_caption

        response_text = generate_ai_response([prompt, image_data])
        bot.delete_message(chat_id, status.message_id)
        send_long_message(chat_id, response_text)
    except Exception as e:
        print(f"Photo Error: {e}")
        bot.reply_to(message, "❌ Failed to process image.")

@bot.message_handler(content_types=['document'])
def handle_document(message):
    if not should_process(message): return
    chat_id = message.chat.id
    if is_rate_limited(chat_id):
        bot.reply_to(message, "⏳ Hourly limit reached (50 msgs/hr).")
        return

    status = bot.reply_to(message, "📄 Reading file...")
    try:
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        mime_type = message.document.mime_type or "application/pdf"
        file_data = {"mime_type": mime_type, "data": downloaded_file}

        raw_caption = message.caption or "Analyze and summarize this document."
        bot_info = bot.get_me()
        prompt = raw_caption.replace(f"@{bot_info.username}", "").strip() if bot_info.username else raw_caption

        response_text = generate_ai_response([prompt, file_data])
        bot.delete_message(chat_id, status.message_id)
        send_long_message(chat_id, response_text)
    except Exception as e:
        print(f"Doc Error: {e}")
        bot.reply_to(message, "❌ Failed to process document.")

@bot.message_handler(content_types=['voice'])
def handle_voice(message):
    if not should_process(message): return
    chat_id = message.chat.id
    if is_rate_limited(chat_id):
        bot.reply_to(message, "⏳ Hourly limit reached (50 msgs/hr).")
        return

    status = bot.reply_to(message, "🎙️ Processing audio...")
    try:
        file_info = bot.get_file(message.voice.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        audio_data = {"mime_type": "audio/ogg", "data": downloaded_file}

        response_text = generate_ai_response(["Listen to this voice message and respond:", audio_data])
        bot.delete_message(chat_id, status.message_id)
        send_long_message(chat_id, response_text)
    except Exception as e:
        print(f"Voice Error: {e}")
        bot.reply_to(message, "❌ Failed to process audio.")

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    if not should_process(message): return
    chat_id = message.chat.id
    if is_rate_limited(chat_id):
        bot.reply_to(message, "⏳ Hourly limit reached (50 msgs/hr).")
        return

    register_user(chat_id, 1 if message.chat.type in ['group', 'supergroup'] else 0)

    bot_info = bot.get_me()
    user_text = message.text.replace(f"@{bot_info.username}", "").strip() if bot_info.username else message.text

    if not user_text: return

    bot.send_chat_action(chat_id, 'typing')
    response_text = generate_ai_response(user_text)
    send_long_message(chat_id, response_text)

# ==========================================
# 6. VERCEL WEBHOOK ENDPOINT
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
