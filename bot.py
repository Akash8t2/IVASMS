#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IVASMS -> Telegram forwarder (Cloudscraper edition)
- Uses cloudscraper to bypass Cloudflare/WAF challenges (403 "Just a moment...")
- Runs on Termux or Heroku
- Stores processed IDs in MongoDB (with JSON fallback)
- Telegram admin commands to manage chat IDs
"""

import os
import re
import json
import time
import traceback
import asyncio
from hashlib import sha1
from datetime import datetime, timedelta
from urllib.parse import urljoin
from bs4 import BeautifulSoup

# Telegram (async)
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# MongoDB
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# cloudscraper (blocking). We'll call it inside asyncio.to_thread
import cloudscraper

# -------------------------
# Configuration (env vars)
# -------------------------
YOUR_BOT_TOKEN = os.getenv("YOUR_BOT_TOKEN") or os.getenv("BOT_TOKEN")
USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")
ADMIN_CHAT_IDS = [s.strip() for s in os.getenv("ADMIN_CHAT_IDS", "").split(",") if s.strip()]

CHAT_IDS_FILE = "chat_ids.json"
INITIAL_CHAT_IDS = ["-1003073839183", "-1002907713631"]

LOGIN_URL = "https://www.ivasms.com/login"
BASE_URL = "https://www.ivasms.com/"
SMS_API_ENDPOINT = "https://www.ivasms.com/portal/sms/received/getsms"

POLLING_INTERVAL_SECONDS = int(os.getenv("POLLING_INTERVAL_SECONDS", "2"))
STATE_FILE = "processed_sms_ids.json"

MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://BrandedSupportGroup:BRANDED_WORLD@cluster0.v4odcq9.mongodb.net/?retryWrites=true&w=majority")
DB_NAME = os.getenv("DB_NAME", "ivasms_bot")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "processed_sms")

# Minimal keyword lists (extend/replace with your full ones)
SERVICE_KEYWORDS = {
    "Facebook": ["facebook"], "Google": ["google", "gmail"], "WhatsApp": ["whatsapp"],
    "Telegram": ["telegram"], "Instagram": ["instagram"], "Unknown": ["unknown"]
}
SERVICE_EMOJIS = {"Telegram": "📩", "WhatsApp": "🟢", "Facebook": "📘", "Instagram": "📸", "Unknown": "❓"}
COUNTRY_FLAGS = {"India": "🇮🇳", "Unknown Country": "🏴‍☠️"}

# -------------------------
# MongoDB init
# -------------------------
mongo_client = None
mongo_collection = None
if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_client.server_info()
        mongo_collection = mongo_client[DB_NAME][COLLECTION_NAME]
        print("✅ MongoDB connected successfully.")
    except PyMongoError as e:
        print("⚠️ MongoDB connect failed, falling back to JSON. Error:", e)
        mongo_collection = None
else:
    mongo_collection = None

# -------------------------
# Chat ID helpers & processed ids
# -------------------------
def load_chat_ids():
    if not os.path.exists(CHAT_IDS_FILE):
        with open(CHAT_IDS_FILE, "w") as f:
            json.dump(INITIAL_CHAT_IDS, f)
        return INITIAL_CHAT_IDS.copy()
    try:
        with open(CHAT_IDS_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, list) else INITIAL_CHAT_IDS.copy()
    except Exception:
        return INITIAL_CHAT_IDS.copy()

def save_chat_ids(chat_ids):
    try:
        with open(CHAT_IDS_FILE, "w") as f:
            json.dump(chat_ids, f, indent=2)
    except Exception as e:
        print("❌ Failed to save chat ids:", e)

def load_processed_ids():
    if mongo_collection:
        try:
            return {doc["_id"] for doc in mongo_collection.find({}, {"_id": 1})}
        except PyMongoError as e:
            print("⚠️ Mongo read error:", e)
            return set()
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_processed_id(sms_id: str):
    if mongo_collection:
        try:
            mongo_collection.update_one({"_id": sms_id}, {"$set": {"processed_at": datetime.utcnow()}}, upsert=True)
            return
        except PyMongoError as e:
            print("⚠️ Mongo write error:", e)
    try:
        s = load_processed_ids()
        s.add(sms_id)
        with open(STATE_FILE, "w") as f:
            json.dump(list(s), f)
    except Exception as e:
        print("❌ Failed to save processed id to file:", e)

def escape_markdown(text):
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

# -------------------------
# Telegram handlers
# -------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if str(uid) in ADMIN_CHAT_IDS:
        await update.message.reply_text("Welcome Admin!\nCommands: /add_chat <id>, /remove_chat <id>, /list_chats")
    else:
        await update.message.reply_text("You are not authorized.")

async def add_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if str(uid) not in ADMIN_CHAT_IDS:
        await update.message.reply_text("Only admins can use this.")
        return
    try:
        new_id = context.args[0]
        chat_ids = load_chat_ids()
        if new_id not in chat_ids:
            chat_ids.append(new_id)
            save_chat_ids(chat_ids)
            await update.message.reply_text(f"Added {new_id}")
        else:
            await update.message.reply_text("Already present.")
    except Exception:
        await update.message.reply_text("Usage: /add_chat <chat_id>")

async def remove_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if str(uid) not in ADMIN_CHAT_IDS:
        await update.message.reply_text("Only admins can use this.")
        return
    try:
        rid = context.args[0]
        chat_ids = load_chat_ids()
        if rid in chat_ids:
            chat_ids.remove(rid)
            save_chat_ids(chat_ids)
            await update.message.reply_text(f"Removed {rid}")
        else:
            await update.message.reply_text("Not found.")
    except Exception:
        await update.message.reply_text("Usage: /remove_chat <chat_id>")

async def list_chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if str(uid) not in ADMIN_CHAT_IDS:
        await update.message.reply_text("Only admins can use this.")
        return
    chat_ids = load_chat_ids()
    if chat_ids:
        try:
            msg = "Registered chat IDs:\n" + "\n".join(f"- `{escape_markdown(str(c))}`" for c in chat_ids)
            await update.message.reply_text(msg, parse_mode='MarkdownV2')
        except Exception:
            await update.message.reply_text("Registered chat IDs:\n" + "\n".join(chat_ids))
    else:
        await update.message.reply_text("No chat IDs registered.")

# -------------------------
# Cloudscraper blocking helpers (run in thread)
# -------------------------
def create_scraper_session():
    # cloudscraper.create_scraper() uses requests.Session underneath and handles Cloudflare JS challenge
    s = cloudscraper.create_scraper(allow_brotli=True)
    # set a realistic user-agent
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Referer": LOGIN_URL
    })
    return s

def blocking_login_and_fetch(scraper, username, password):
    """
    Uses cloudscraper session to GET login, POST credentials with dynamic hidden fields,
    and returns (ok_bool, final_html_text)
    """
    try:
        resp = scraper.get(LOGIN_URL, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        payload = {}
        for hidden in soup.find_all("input", {"type": "hidden"}):
            name = hidden.get("name")
            val = hidden.get("value", "")
            if name:
                payload[name] = val
        # set credentials into common keys
        lowered = {k.lower(): k for k in payload.keys()}
        if "email" in lowered:
            payload[lowered["email"]] = username
        elif "username" in lowered:
            payload[lowered["username"]] = username
        else:
            payload["email"] = username

        if "password" in lowered:
            payload[lowered["password"]] = password
        elif "pass" in lowered:
            payload[lowered["pass"]] = password
        else:
            payload["password"] = password

        post = scraper.post(LOGIN_URL, data=payload, timeout=30, allow_redirects=True)
        html = post.text.lower()
        # heuristics for success
        if "dashboard" in html or "logout" in html or post.status_code in (301,302):
            return True, post.text
        # sometimes cloudscraper solves challenge and returns the dashboard after subsequent get
        if "just a moment" in html and post.status_code == 200:
            # challenge page — cloudscraper may auto-wait, but if still here, do another GET
            follow = scraper.get(BASE_URL, timeout=30)
            h = follow.text.lower()
            if "dashboard" in h or "logout" in h:
                return True, follow.text
        # fallback false
        return False, post.text
    except Exception as e:
        return False, f"exception: {e}"

def blocking_fetch_sms(scraper, csrf_token):
    """
    Use cloudscraper (requests-like) to call the SMS endpoints (synchronous).
    Returns list of message dicts similar to previous async version.
    """
    messages = []
    try:
        today = datetime.utcnow()
        start_date = today - timedelta(days=1)
        from_date_str, to_date_str = start_date.strftime('%m/%d/%Y'), today.strftime('%m/%d/%Y')
        first_payload = {'from': from_date_str, 'to': to_date_str, '_token': csrf_token}
        summary_res = scraper.post(SMS_API_ENDPOINT, data=first_payload, timeout=30)
        summary_res.raise_for_status()
        summary_soup = BeautifulSoup(summary_res.text, "html.parser")
        group_divs = summary_soup.find_all('div', {'class': 'pointer'})
        if not group_divs:
            return []

        group_ids = [re.search(r"getDetials\('(.+?)'\)", div.get('onclick', '')).group(1)
                     for div in group_divs if re.search(r"getDetials\('(.+?)'\)", div.get('onclick', ''))]

        numbers_url = urljoin(BASE_URL, "portal/sms/received/getsms/number")
        sms_url = urljoin(BASE_URL, "portal/sms/received/getsms/number/sms")

        for group_id in group_ids:
            numbers_payload = {'start': from_date_str, 'end': to_date_str, 'range': group_id, '_token': csrf_token}
            numbers_res = scraper.post(numbers_url, data=numbers_payload, timeout=30)
            numbers_soup = BeautifulSoup(numbers_res.text, "html.parser")
            number_divs = numbers_soup.select("div[onclick*='getDetialsNumber']")
            if not number_divs:
                continue
            phone_numbers = [div.text.strip() for div in number_divs]

            for phone_number in phone_numbers:
                sms_payload = {'start': from_date_str, 'end': to_date_str, 'Number': phone_number, 'Range': group_id, '_token': csrf_token}
                sms_res = scraper.post(sms_url, data=sms_payload, timeout=30)
                sms_soup = BeautifulSoup(sms_res.text, "html.parser")
                final_sms_cards = sms_soup.find_all('div', class_='card-body')

                for card in final_sms_cards:
                    sms_text_p = card.find('p', class_='mb-0')
                    if sms_text_p:
                        sms_text = sms_text_p.get_text(separator='\n').strip()
                        date_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
                        country_name_match = re.match(r'([a-zA-Z\s]+)', group_id)
                        country_name = country_name_match.group(1).strip() if country_name_match else group_id.strip()
                        service = "Unknown"
                        lower_sms_text = sms_text.lower()
                        for sname, keywords in SERVICE_KEYWORDS.items():
                            if any(k in lower_sms_text for k in keywords):
                                service = sname
                                break
                        code_match = re.search(r'(\d{3}-\d{3})', sms_text) or re.search(r'\b(\d{4,8})\b', sms_text)
                        code = code_match.group(1) if code_match else "N/A"
                        unique_id = sha1(f"{phone_number}|{sms_text}".encode()).hexdigest()
                        flag = COUNTRY_FLAGS.get(country_name, "🏴‍☠️")
                        messages.append({
                            "id": unique_id, "time": date_str, "number": phone_number,
                            "country": country_name, "flag": flag, "service": service,
                            "code": code, "full_sms": sms_text
                        })
        return messages
    except Exception as e:
        print("❌ Blocking fetch SMS error:", e)
        traceback.print_exc()
        return []

# -------------------------
# Async wrappers (call blocking functions in thread)
# -------------------------
async def login_with_cloudscraper():
    return await asyncio.to_thread(_login_thread)

def _login_thread():
    s = create_scraper_session()
    ok, html_or_text = blocking_login_and_fetch(s, USERNAME, PASSWORD)
    return (ok, html_or_text, s)  # return session object so it can be reused

async def fetch_sms_with_cloudscraper(session, csrf_token):
    return await asyncio.to_thread(blocking_fetch_sms, session, csrf_token)

# -------------------------
# send message (async)
# -------------------------
async def send_telegram_message(context: ContextTypes.DEFAULT_TYPE, chat_id: str, message_data: dict):
    try:
        time_str = message_data.get("time", "N/A")
        number_str = message_data.get("number", "N/A")
        country_name = message_data.get("country", "N/A")
        flag_emoji = message_data.get("flag", "🏴‍☠️")
        service_name = message_data.get("service", "N/A")
        code_str = message_data.get("code", "N/A")
        full_sms_text = message_data.get("full_sms", "N/A")
        service_emoji = SERVICE_EMOJIS.get(service_name, "❓")
        full_message = (
            f"🔔 *You have successfully received OTP*\n\n"
            f"📞 *Number:* `{escape_markdown(number_str)}`\n"
            f"🔑 *Code:* `{escape_markdown(code_str)}`\n"
            f"🏆 *Service:* {service_emoji} {escape_markdown(service_name)}\n"
            f"🌎 *Country:* {escape_markdown(country_name)} {flag_emoji}\n"
            f"⏳ *Time:* `{escape_markdown(time_str)}`\n\n"
            f"💬 *Message:*\n```\n{full_sms_text}\n```"
        )
        await context.bot.send_message(chat_id=chat_id, text=full_message, parse_mode='MarkdownV2')
    except Exception as e:
        print(f"❌ Error sending message to chat ID {chat_id}: {e}")

# -------------------------
# Main periodic job
# -------------------------
async def check_sms_job(context: ContextTypes.DEFAULT_TYPE):
    print(f"\n--- [{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Checking for new messages ---")
    try:
        ok, html_or_text, session = await login_with_cloudscraper()
        if not ok:
            print("❌ Login failed (cloudscraper). Response snippet (truncated):")
            snippet = (html_or_text or "")[:800].replace("\n", " ")
            print(snippet)
            return

        # extract csrf token from returned HTML
        soup = BeautifulSoup(html_or_text, "html.parser")
        csrf = None
        meta = soup.find("meta", {"name": "csrf-token"})
        if meta and meta.get("content"):
            csrf = meta.get("content")
        else:
            hid = soup.find("input", {"name": "_token"}) or soup.find("input", {"name": "csrf_token"})
            if hid and hid.get("value"):
                csrf = hid.get("value")
        if not csrf:
            csrf = ""  # some endpoints may accept empty value

        messages = await fetch_sms_with_cloudscraper(session, csrf)
        if not messages:
            print("✔️ No new messages found.")
            return

        processed_ids = load_processed_ids()
        chat_ids = load_chat_ids()
        new_found = 0
        for msg in reversed(messages):
            if msg["id"] not in processed_ids:
                new_found += 1
                print(f"✔️ New message from {msg['number']}. Sending...")
                for cid in chat_ids:
                    await send_telegram_message(context, cid, msg)
                save_processed_id(msg["id"])
        if new_found > 0:
            print(f"✅ Sent {new_found} new messages.")
    except Exception as e:
        print("❌ Error in check_sms_job:", e)
        traceback.print_exc()

# -------------------------
# Startup
# -------------------------
def main():
    if not YOUR_BOT_TOKEN:
        print("❌ Set YOUR_BOT_TOKEN env var.")
        return
    if not USERNAME or not PASSWORD:
        print("❌ Set USERNAME and PASSWORD env vars.")
        return

    application = Application.builder().token(YOUR_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("add_chat", add_chat_command))
    application.add_handler(CommandHandler("remove_chat", remove_chat_command))
    application.add_handler(CommandHandler("list_chats", list_chats_command))

    job_queue = application.job_queue
    job_queue.run_repeating(check_sms_job, interval=POLLING_INTERVAL_SECONDS, first=1)

    print("🚀 Bot started. Polling every", POLLING_INTERVAL_SECONDS, "seconds.")
    application.run_polling()

if __name__ == "__main__":
    main()
