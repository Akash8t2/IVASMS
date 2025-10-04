#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IVASMS -> Telegram forwarder
Single-file, Python-only, dynamic hidden-field login + Mongo dedupe.
Works on Termux or Heroku (use Procfile: worker: python bot.py)
"""

import os
import re
import json
import time
import traceback
import asyncio
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timedelta
from hashlib import sha1

# Telegram libs (python-telegram-bot v20+)
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# MongoDB
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# -------------------------
# Configuration (env vars)
# -------------------------
YOUR_BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("BOT_TOKEN")
USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")
ADMIN_CHAT_IDS = [s.strip() for s in os.getenv("ADMIN_CHAT_IDS", "").split(",") if s.strip()]
# chat ids storage file (local fallback)
CHAT_IDS_FILE = "chat_ids.json"
INITIAL_CHAT_IDS = ["-1003073839183", "-1002907713631"]

LOGIN_URL = "https://www.ivasms.com/login"
BASE_URL = "https://www.ivasms.com/"
SMS_API_ENDPOINT = "https://www.ivasms.com/portal/sms/received/getsms"

POLLING_INTERVAL_SECONDS = int(os.getenv("POLLING_INTERVAL_SECONDS", "2"))
STATE_FILE = "processed_sms_ids.json"

# MongoDB config (you provided this earlier; keep as default but allow override)
MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://BrandedSupportGroup:BRANDED_WORLD@cluster0.v4odcq9.mongodb.net/?retryWrites=true&w=majority")
DB_NAME = os.getenv("DB_NAME", "ivasms_bot")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "processed_sms")

# -------------------------
# Service keyword lists (shortened here; extend if needed)
# You can paste your full SERVICE_KEYWORDS / SERVICE_EMOJIS / COUNTRY_FLAGS if desired.
# -------------------------
SERVICE_KEYWORDS = {
    "Facebook": ["facebook"],
    "Google": ["google", "gmail"],
    "WhatsApp": ["whatsapp"],
    "Telegram": ["telegram"],
    "Instagram": ["instagram"],
    "Unknown": ["unknown"]
}

SERVICE_EMOJIS = {
    "Telegram": "📩", "WhatsApp": "🟢", "Facebook": "📘", "Instagram": "📸", "Unknown": "❓"
}

COUNTRY_FLAGS = {
    "India": "🇮🇳", "Unknown Country": "🏴‍☠️"
}

# -------------------------
# MongoDB initialization
# -------------------------
mongo_client = None
mongo_collection = None
if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_client.server_info()  # test connection
        mongo_collection = mongo_client[DB_NAME][COLLECTION_NAME]
        print("✅ MongoDB connected successfully.")
    except PyMongoError as e:
        print("⚠️ MongoDB connect failed, falling back to JSON. Error:", e)
        mongo_collection = None
else:
    print("⚠️ MONGO_URI not set. Using JSON fallback storage.")
    mongo_collection = None

# -------------------------
# Helper functions
# -------------------------
def load_chat_ids():
    if not os.path.exists(CHAT_IDS_FILE):
        with open(CHAT_IDS_FILE, "w") as f:
            json.dump(INITIAL_CHAT_IDS, f)
        return INITIAL_CHAT_IDS.copy()
    try:
        with open(CHAT_IDS_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return INITIAL_CHAT_IDS.copy()
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
    # JSON fallback
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
    # fallback
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
        await update.message.reply_text("Welcome Admin!\n/ add_chat <id>\n/ remove_chat <id>\n/ list_chats")
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
# Login and fetch logic
# -------------------------
async def login_and_get_session(client: httpx.AsyncClient):
    """
    Perform GET on login page, collect dynamic hidden inputs,
    then POST to login.
    Returns True/False and the final response after login attempt.
    """
    try:
        # GET login page to get cookies & hidden fields
        resp = await client.get(LOGIN_URL)
        soup = BeautifulSoup(resp.text, "html.parser")

        # Build login payload dynamically (include all hidden inputs)
        login_data = {}
        # try common names
        # leave final assignment of email/password to prefer user's env names
        for hidden in soup.find_all("input", {"type": "hidden"}):
            name = hidden.get("name")
            val = hidden.get("value", "")
            if name:
                login_data[name] = val

        # Common field names mapping: try to set typical ones if present
        # Many sites use 'email'/'username'/'user' etc.
        # We'll attempt to set the values on common keys if they exist, else add defaults
        keys_lower = {k.lower(): k for k in login_data.keys()}

        if "email" in keys_lower:
            login_data[keys_lower["email"]] = USERNAME
        elif "username" in keys_lower:
            login_data[keys_lower["username"]] = USERNAME
        elif "user" in keys_lower:
            login_data[keys_lower["user"]] = USERNAME
        else:
            # add common names
            login_data.setdefault("email", USERNAME)
            login_data.setdefault("username", USERNAME)

        if "password" in keys_lower:
            login_data[keys_lower["password"]] = PASSWORD
        elif "pass" in keys_lower:
            login_data[keys_lower["pass"]] = PASSWORD
        else:
            login_data.setdefault("password", PASSWORD)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": LOGIN_URL
        }

        login_resp = await client.post(LOGIN_URL, data=login_data, headers=headers)
        html = login_resp.text.lower()

        # heuristics for success
        if "dashboard" in html or "logout" in html or login_resp.status_code in (301, 302):
            return True, login_resp
        # sometimes the page remains same; check if token changed or an authenticated fragment exists
        if "login" not in str(login_resp.url).lower() and login_resp.status_code == 200:
            return True, login_resp

        # fallback: return False and login_resp for debugging
        return False, login_resp

    except Exception as e:
        print("❌ Exception during login:", e)
        traceback.print_exc()
        return False, None

async def fetch_sms_from_api(client: httpx.AsyncClient, headers: dict, csrf_token: str):
    all_messages = []
    try:
        today = datetime.utcnow()
        start_date = today - timedelta(days=1)
        from_date_str, to_date_str = start_date.strftime('%m/%d/%Y'), today.strftime('%m/%d/%Y')
        first_payload = {'from': from_date_str, 'to': to_date_str, '_token': csrf_token}
        summary_response = await client.post(SMS_API_ENDPOINT, headers=headers, data=first_payload)
        summary_response.raise_for_status()
        summary_soup = BeautifulSoup(summary_response.text, 'html.parser')
        group_divs = summary_soup.find_all('div', {'class': 'pointer'})
        if not group_divs: return []

        group_ids = [re.search(r"getDetials\('(.+?)'\)", div.get('onclick', '')).group(1)
                     for div in group_divs if re.search(r"getDetials\('(.+?)'\)", div.get('onclick', ''))]

        numbers_url = urljoin(BASE_URL, "portal/sms/received/getsms/number")
        sms_url = urljoin(BASE_URL, "portal/sms/received/getsms/number/sms")

        for group_id in group_ids:
            numbers_payload = {'start': from_date_str, 'end': to_date_str, 'range': group_id, '_token': csrf_token}
            numbers_response = await client.post(numbers_url, headers=headers, data=numbers_payload)
            numbers_soup = BeautifulSoup(numbers_response.text, 'html.parser')
            number_divs = numbers_soup.select("div[onclick*='getDetialsNumber']")
            if not number_divs:
                continue
            phone_numbers = [div.text.strip() for div in number_divs]

            for phone_number in phone_numbers:
                sms_payload = {'start': from_date_str, 'end': to_date_str, 'Number': phone_number, 'Range': group_id, '_token': csrf_token}
                sms_response = await client.post(sms_url, headers=headers, data=sms_payload)
                sms_soup = BeautifulSoup(sms_response.text, 'html.parser')
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
                        for service_name, keywords in SERVICE_KEYWORDS.items():
                            if any(keyword in lower_sms_text for keyword in keywords):
                                service = service_name
                                break
                        code_match = re.search(r'(\d{3}-\d{3})', sms_text) or re.search(r'\b(\d{4,8})\b', sms_text)
                        code = code_match.group(1) if code_match else "N/A"
                        unique_id = sha1(f"{phone_number}|{sms_text}".encode()).hexdigest()
                        flag = COUNTRY_FLAGS.get(country_name, "🏴‍☠️")

                        all_messages.append({
                            "id": unique_id,
                            "time": date_str,
                            "number": phone_number,
                            "country": country_name,
                            "flag": flag,
                            "service": service,
                            "code": code,
                            "full_sms": sms_text
                        })
        return all_messages
    except httpx.RequestError as e:
        print("❌ Network issue (httpx):", e)
        return []
    except Exception as e:
        print("❌ Error fetching or processing API data:", e)
        traceback.print_exc()
        return []

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
            f"💬 *Message:*\n"
            f"```\n{full_sms_text}\n```"
        )
        await context.bot.send_message(chat_id=chat_id, text=full_message, parse_mode='MarkdownV2')
    except Exception as e:
        print(f"❌ Error sending message to chat ID {chat_id}: {e}")

# -------------------------
# Job executed periodically
# -------------------------
async def check_sms_job(context: ContextTypes.DEFAULT_TYPE):
    print(f"\n--- [{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Checking for new messages ---")
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            ok, login_resp = await login_and_get_session(client)
            if not ok:
                print("❌ Login failed. Check username/password or site changes.")
                if login_resp is not None:
                    print("Response URL:", login_resp.url)
                    print("Status code:", login_resp.status_code)
                    snippet = login_resp.text[:800].replace("\n", " ")
                    print("Response snippet (truncated):", snippet)
                return

            # Extract csrf token from dashboard page (common meta or hidden input)
            soup = BeautifulSoup(login_resp.text, "html.parser")
            csrf_token = None
            meta = soup.find("meta", {"name": "csrf-token"})
            if meta and meta.get("content"):
                csrf_token = meta.get("content")
            else:
                # try hidden inputs
                hid = soup.find("input", {"name": "_token"}) or soup.find("input", {"name": "csrf_token"})
                if hid and hid.get("value"):
                    csrf_token = hid.get("value")

            if not csrf_token:
                # fallback: some implementations expect _token in the first payload; try empty string
                csrf_token = ""

            messages = await fetch_sms_from_api(client, headers, csrf_token)
            if not messages:
                print("✔️ No new messages found.")
                return

            processed_ids = load_processed_ids()
            chat_ids_to_send = load_chat_ids()
            new_messages_found = 0

            for msg in reversed(messages):
                if msg["id"] not in processed_ids:
                    new_messages_found += 1
                    print(f"✔️ New message from {msg['number']}. Sending to {len(chat_ids_to_send)} chats.")
                    for chat_id in chat_ids_to_send:
                        await send_telegram_message(context, chat_id, msg)
                    save_processed_id(msg["id"])
                else:
                    # optional: print skip
                    pass

            if new_messages_found > 0:
                print(f"✅ Sent {new_messages_found} new messages.")
        except httpx.RequestError as e:
            print("❌ Network or login issue (httpx):", e)
        except Exception as e:
            print("❌ Error in main job:", e)
            traceback.print_exc()

# -------------------------
# Main startup
# -------------------------
def main():
    if not YOUR_BOT_TOKEN:
        print("❌ Set YOUR_BOT_TOKEN env var and restart.")
        return
    if not USERNAME or not PASSWORD:
        print("❌ Set USERNAME and PASSWORD env vars and restart.")
        return

    application = Application.builder().token(YOUR_BOT_TOKEN).build()

    # add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("add_chat", add_chat_command))
    application.add_handler(CommandHandler("remove_chat", remove_chat_command))
    application.add_handler(CommandHandler("list_chats", list_chats_command))

    # schedule job
    job_queue = application.job_queue
    job_queue.run_repeating(check_sms_job, interval=POLLING_INTERVAL_SECONDS, first=1)

    print("🚀 Bot started. Polling every", POLLING_INTERVAL_SECONDS, "seconds.")
    application.run_polling()

if __name__ == "__main__":
    main()
