#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IVASMS -> Telegram Forwarder
- Fetches SMS from IVASMS dashboard
- Forwards to registered chat IDs
- Prevents duplicate forwards using MongoDB (with JSON fallback)
- Telegram command handlers to add/remove/list chat IDs
"""

import os
import re
import json
import time
import traceback
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

# -----------------------------
# Config (from environment)
# -----------------------------
YOUR_BOT_TOKEN = os.getenv("YOUR_BOT_TOKEN")  # Telegram bot token
ADMIN_CHAT_IDS = [s.strip() for s in os.getenv("ADMIN_CHAT_IDS", "").split(",") if s.strip()]
INITIAL_CHAT_IDS = ["-1003073839183", "-1002907713631"]
LOGIN_URL = "https://www.ivasms.com/login"
BASE_URL = "https://www.ivasms.com/"
SMS_API_ENDPOINT = "https://www.ivasms.com/portal/sms/received/getsms"
USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")

POLLING_INTERVAL_SECONDS = int(os.getenv("POLLING_INTERVAL_SECONDS", "2"))
STATE_FILE = "processed_sms_ids.json"
CHAT_IDS_FILE = "chat_ids.json"

# -----------------------------
# Country flags, keywords etc.
# (You already had these — keep as-is)
# -----------------------------
COUNTRY_FLAGS = {  
    "Afghanistan": "🇦🇫", "Albania": "🇦🇱", "Algeria": "🇩🇿", "Andorra": "🇦🇩", "Angola": "🇦🇴",  
    "Argentina": "🇦🇷", "Armenia": "🇦🇲", "Australia": "🇦🇺", "Austria": "🇦🇹", "Azerbaijan": "🇦🇿",  
    "Bahrain": "🇧🇭", "Bangladesh": "🇧🇩", "Belarus": "🇧🇾", "Belgium": "🇧🇪", "Benin": "🇧🇯",  
    "Bhutan": "🇧🇹", "Bolivia": "🇧🇴", "Brazil": "🇧🇷", "Bulgaria": "🇧🇬", "Burkina Faso": "🇧🇫",  
    "Cambodia": "🇰🇭", "Cameroon": "🇨🇲", "Canada": "🇨🇦", "Chad": "🇹🇩", "Chile": "🇨 ",  
    "China": "🇨🇳", "Colombia": "🇨🇴", "Congo": "🇨🇬", "Croatia": "🇭🇷", "Cuba": "🇨🇺",  
    "Cyprus": "🇨🇾", "Czech Republic": "🇨🇿", "Denmark": "🇩🇰", "Egypt": "🇪🇬", "Estonia": "🇪🇪",  
    "Ethiopia": "🇪🇹", "Finland": "🇫🇮", "France": "🇫🇷", "Gabon": "🇬🇦", "Gambia": "🇬🇲",  
    "Georgia": "🇬🇪", "Germany": "🇩🇪", "Ghana": "🇬🇭", "Greece": "🇬🇷", "Guatemala": "🇬🇹",  
    "Guinea": "🇬🇳", "Haiti": "🇭🇹", "Honduras": "🇭🇳", "Hong Kong": "🇭🇰", "Hungary": "🇭🇺",  
    "Iceland": "🇮🇸", "India": "🇮🇳", "Indonesia": "🇮🇩", "Iran": "🇮🇷", "Iraq": "🇮🇶",  
    "Ireland": "🇮🇪", "Israel": "🇮🇱", "Italy": "🇮🇹", "IVORY COAST": "🇨🇮", "Ivory Coast": "🇨🇮", "Jamaica": "🇯🇲",  
    "Japan": "🇯🇵", "Jordan": "🇯🇴", "Kazakhstan": "🇰🇿", "Kenya": "🇰🇪", "Kuwait": "🇰🇼",  
    "Kyrgyzstan": "🇰🇬", "Laos": "🇱🇦", "Latvia": "🇱🇻", "Lebanon": "🇱🇧", "Liberia": "🇱🇷",  
    "Libya": "🇱🇾", "Lithuania": "🇱🇹", "Luxembourg": "🇱🇺", "Madagascar": "🇲🇬", "Malaysia": "🇲🇾",  
    "Mali": "🇲🇱", "Malta": "🇲🇹", "Mexico": "🇲🇽", "Moldova": "🇲🇩", "Monaco": "🇲🇨",  
    "Mongolia": "🇲🇳", "Montenegro": "🇲🇪", "Morocco": "🇲🇦", "Mozambique": "🇲🇿", "Myanmar": "🇲🇲",  
    "Namibia": "🇳🇦", "Nepal": "🇳🇵", "Netherlands": "🇳🇱", "New Zealand": "🇳🇿", "Nicaragua": "🇳🇮",  
    "Niger": "🇳🇪", "Nigeria": "🇳🇬", "North Korea": "🇰🇵", "North Macedonia": "🇲🇰", "Norway": "🇳🇴",  
    "Oman": "🇴🇲", "Pakistan": "🇵🇰", "Panama": "🇵🇦", "Paraguay": "🇵🇾", "Peru": "🇵🇪",  
    "Philippines": "🇵🇭", "Poland": "🇵🇱", "Portugal": "🇵🇹", "Qatar": "🇶🇦", "Romania": "🇷🇴",  
    "Russia": "🇷🇺", "Rwanda": "🇷🇼", "Saudi Arabia": "🇸🇦", "Senegal": "🇸🇳", "Serbia": "🇷🇸",  
    "Sierra Leone": "🇸🇱", "Singapore": "🇸🇬", "Slovakia": "🇸🇰", "Slovenia": "🇸🇮", "Somalia": "🇸🇴",  
    "South Africa": "🇿🇦", "South Korea": "🇰🇷", "Spain": "🇪🇸", "Sri Lanka": "🇱🇰", "Sudan": "🇸🇩",  
    "Sweden": "🇸🇪", "Switzerland": "🇨🇭", "Syria": "🇸🇾", "Taiwan": "🇹🇼", "Tajikistan": "🇹🇯",  
    "Tanzania": "🇹🇿", "Thailand": "🇹🇭", "TOGO": "🇹🇬", "Tunisia": "🇹🇳", "Turkey": "🇹🇷",  
    "Turkmenistan": "🇹🇲", "Uganda": "🇺🇬", "Ukraine": "🇺🇦", "United Arab Emirates": "🇦🇪", "United Kingdom": "🇬🇧",  
    "United States": "🇺🇸", "Uruguay": "🇺🇾", "Uzbekistan": "🇺🇿", "Venezuela": "🇻🇪", "Vietnam": "🇻🇳",  
    "Yemen": "🇾🇪", "Zambia": "🇿🇲", "Zimbabwe": "🇿🇼", "Unknown Country": "🏴‍☠️"  
}  
# Minimal insertion: to keep file short in this message, include your full dict in actual file.
# For safety paste the full COUNTRY_FLAGS and SERVICE_KEYWORDS and SERVICE_EMOJIS from your original script here.

SERVICE_KEYWORDS = {  
    "Facebook": ["facebook"],  
    "Google": ["google", "gmail"],  
    "WhatsApp": ["whatsapp"],  
    "Telegram": ["telegram"],  
    "Instagram": ["instagram"],  
    "Amazon": ["amazon"],  
    "Netflix": ["netflix"],  
    "LinkedIn": ["linkedin"],  
    "Microsoft": ["microsoft", "outlook", "live.com"],  
    "Apple": ["apple", "icloud"],  
    "Twitter": ["twitter"],  
    "Snapchat": ["snapchat"],  
    "TikTok": ["tiktok"],  
    "Discord": ["discord"],  
    "Signal": ["signal"],  
    "Viber": ["viber"],  
    "IMO": ["imo"],  
    "PayPal": ["paypal"],  
    "Binance": ["binance"],  
    "Uber": ["uber"],  
    "Bolt": ["bolt"],  
    "Airbnb": ["airbnb"],  
    "Yahoo": ["yahoo"],  
    "Steam": ["steam"],  
    "Blizzard": ["blizzard"],  
    "Foodpanda": ["foodpanda"],  
    "Pathao": ["pathao"],  
    # Newly added service keywords  
    "Messenger": ["messenger", "meta"],  
    "Gmail": ["gmail", "google"],  
    "YouTube": ["youtube", "google"],  
    "X": ["x", "twitter"],  
    "eBay": ["ebay"],  
    "AliExpress": ["aliexpress"],  
    "Alibaba": ["alibaba"],  
    "Flipkart": ["flipkart"],  
    "Outlook": ["outlook", "microsoft"],  
    "Skype": ["skype", "microsoft"],  
    "Spotify": ["spotify"],  
    "iCloud": ["icloud", "apple"],  
    "Stripe": ["stripe"],  
    "Cash App": ["cash app", "square cash"],  
    "Venmo": ["venmo"],  
    "Zelle": ["zelle"],  
    "Wise": ["wise", "transferwise"],  
    "Coinbase": ["coinbase"],  
    "KuCoin": ["kucoin"],  
    "Bybit": ["bybit"],  
    "OKX": ["okx"],  
    "Huobi": ["huobi"],  
    "Kraken": ["kraken"],  
    "MetaMask": ["metamask"],  
    "Epic Games": ["epic games", "epicgames"],  
    "PlayStation": ["playstation", "psn"],  
    "Xbox": ["xbox", "microsoft"],  
    "Twitch": ["twitch"],  
    "Reddit": ["reddit"],  
    "ProtonMail": ["protonmail", "proton"],  
    "Zoho": ["zoho"],  
    "Quora": ["quora"],  
    "StackOverflow": ["stackoverflow"],  
    "LinkedIn": ["linkedin"],  
    "Indeed": ["indeed"],  
    "Upwork": ["upwork"],  
    "Fiverr": ["fiverr"],  
    "Glassdoor": ["glassdoor"],  
    "Airbnb": ["airbnb"],  
    "Booking.com": ["booking.com", "booking"],  
    "Careem": ["careem"],  
    "Swiggy": ["swiggy"],  
    "Zomato": ["zomato"],  
    "McDonald's": ["mcdonalds", "mcdonald's"],  
    "KFC": ["kfc"],  
    "Nike": ["nike"],  
    "Adidas": ["adidas"],  
    "Shein": ["shein"],  
    "OnlyFans": ["onlyfans"],  
    "Tinder": ["tinder"],  
    "Bumble": ["bumble"],  
    "Grindr": ["grindr"],  
    "Line": ["line"],  
    "WeChat": ["wechat"],  
    "VK": ["vk", "vkontakte"],  
    "Unknown": ["unknown"] # Fallback, likely won't have specific keywords  
}  
SERVICE_EMOJIS = {  
    "Telegram": "📩", "WhatsApp": "🟢", "Facebook": "📘", "Instagram": "📸", "Messenger": "💬",  
    "Google": "🔍", "Gmail": "✉️", "YouTube": "▶️", "Twitter": "🐦", "X": "❌",  
    "TikTok": "🎵", "Snapchat": "👻", "Amazon": "🛒", "eBay": "📦", "AliExpress": "📦",  
    "Alibaba": "🏭", "Flipkart": "📦", "Microsoft": "🪟", "Outlook": "📧", "Skype": "📞",  
    "Netflix": "🎬", "Spotify": "🎶", "Apple": "🍏", "iCloud": "☁️", "PayPal": "💰",  
    "Stripe": "💳", "Cash App": "💵", "Venmo": "💸", "Zelle": "🏦", "Wise": "🌐",  
    "Binance": "🪙", "Coinbase": "🪙", "KuCoin": "🪙", "Bybit": "📈", "OKX": "🟠",  
    "Huobi": "🔥", "Kraken": "🐙", "MetaMask": "🦊", "Discord": "🗨️", "Steam": "🎮",  
    "Epic Games": "🕹️", "PlayStation": "🎮", "Xbox": "🎮", "Twitch": "📺", "Reddit": "👽",  
    "Yahoo": "🟣", "ProtonMail": "🔐", "Zoho": "📬", "Quora": "❓", "StackOverflow": "🧑‍💻",  
    "LinkedIn": "💼", "Indeed": "📋", "Upwork": "🧑‍💻", "Fiverr": "💻", "Glassdoor": "🔎",  
    "Airbnb": "🏠", "Booking.com": "🛏️", "Uber": "🚗", "Lyft": "🚕", "Bolt": "🚖",  
    "Careem": "🚗", "Swiggy": "🍔", "Zomato": "🍽️", "Foodpanda": "🍱",  
    "McDonald's": "🍟", "KFC": "🍗", "Nike": "👟", "Adidas": "👟", "Shein": "👗",  
    "OnlyFans": "🔞", "Tinder": "🔥", "Bumble": "🐝", "Grindr": "😈", "Signal": "🔐",  
    "Viber": "📞", "Line": "💬", "WeChat": "💬", "VK": "🌐", "Unknown": "❓"  
}  

# -----------------------------
# MongoDB Setup
# -----------------------------
MONGO_URI = os.getenv(
    "MONGO_URI",
    "mongodb+srv://BrandedSupportGroup:BRANDED_WORLD@cluster0.v4odcq9.mongodb.net/?retryWrites=true&w=majority"
)
DB_NAME = os.getenv("DB_NAME", "ivasms_bot")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "processed_sms")

mongo_client = None
mongo_collection = None
if MONGO_URI:
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo_client.server_info()  # will throw if cannot connect
        mongo_collection = mongo_client[DB_NAME][COLLECTION_NAME]
        print("✅ MongoDB connected successfully.")
    except PyMongoError as e:
        print(f"⚠️ MongoDB connection failed, falling back to JSON file storage. Error: {e}")
        mongo_collection = None
else:
    print("⚠️ MONGO_URI not set; using local JSON fallback.")
    mongo_collection = None

# -----------------------------
# Chat ID file helpers
# -----------------------------
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
            json.dump(chat_ids, f, indent=4)
    except Exception as e:
        print("❌ Failed to save chat ids:", e)

# -----------------------------
# Processed IDs storage (Mongo or JSON file)
# -----------------------------
def load_processed_ids():
    """Return set of processed IDs (from Mongo if available, else from JSON file)."""
    if mongo_collection:
        try:
            return {doc["_id"] for doc in mongo_collection.find({}, {"_id": 1})}
        except PyMongoError as e:
            print("⚠️ MongoDB read error:", e)
            return set()
    # Fallback to file
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_processed_id(sms_id: str):
    """Save processed id (Mongo or JSON file)."""
    if mongo_collection:
        try:
            mongo_collection.update_one({"_id": sms_id}, {"$set": {"processed_at": datetime.utcnow()}}, upsert=True)
            return
        except PyMongoError as e:
            print("⚠️ MongoDB write error:", e)
    # Fallback file write (atomic-ish)
    try:
        processed = load_processed_ids()
        processed.add(sms_id)
        with open(STATE_FILE, "w") as f:
            json.dump(list(processed), f)
    except Exception as e:
        print("❌ Failed to save processed id to file:", e)

# -----------------------------
# Markdown escape helper
# -----------------------------
def escape_markdown(text):
    escape_chars = r'\_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

# -----------------------------
# Telegram command handlers
# -----------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if str(user_id) in ADMIN_CHAT_IDS:
        await update.message.reply_text(
            "Welcome Admin!\n"
            "Commands:\n"
            "/add_chat <chat_id>\n"
            "/remove_chat <chat_id>\n"
            "/list_chats"
        )
    else:
        await update.message.reply_text("Sorry, you are not authorized to use this bot.")

async def add_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if str(user_id) not in ADMIN_CHAT_IDS:
        await update.message.reply_text("Only admins can use this command.")
        return
    try:
        new_chat_id = context.args[0]
        chat_ids = load_chat_ids()
        if new_chat_id not in chat_ids:
            chat_ids.append(new_chat_id)
            save_chat_ids(chat_ids)
            await update.message.reply_text(f"✅ Chat ID {new_chat_id} added.")
        else:
            await update.message.reply_text("⚠️ Chat ID already present.")
    except Exception:
        await update.message.reply_text("Usage: /add_chat <chat_id>")

async def remove_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if str(user_id) not in ADMIN_CHAT_IDS:
        await update.message.reply_text("Only admins can use this command.")
        return
    try:
        chat_id = context.args[0]
        chat_ids = load_chat_ids()
        if chat_id in chat_ids:
            chat_ids.remove(chat_id)
            save_chat_ids(chat_ids)
            await update.message.reply_text(f"✅ Chat ID {chat_id} removed.")
        else:
            await update.message.reply_text("Chat ID not found.")
    except Exception:
        await update.message.reply_text("Usage: /remove_chat <chat_id>")

async def list_chats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if str(user_id) not in ADMIN_CHAT_IDS:
        await update.message.reply_text("Only admins can use this command.")
        return
    chat_ids = load_chat_ids()
    if chat_ids:
        msg = "Registered chat IDs:\n" + "\n".join(f"- `{escape_markdown(str(c))}`" for c in chat_ids)
        try:
            await update.message.reply_text(msg, parse_mode='MarkdownV2')
        except Exception:
            await update.message.reply_text("Registered chat IDs:\n" + "\n".join(chat_ids))
    else:
        await update.message.reply_text("No chat IDs registered.")

# -----------------------------
# Fetching, parsing, sending
# -----------------------------
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
        if not group_divs:
            return []

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

# -----------------------------
# Main job - scheduled
# -----------------------------
async def check_sms_job(context: ContextTypes.DEFAULT_TYPE):
    print(f"\n--- [{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] Checking for new messages ---")
    headers = {'User-Agent': 'Mozilla/5.0'}

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            login_page_res = await client.get(LOGIN_URL, headers=headers)
            soup = BeautifulSoup(login_page_res.text, 'html.parser')
            token_input = soup.find('input', {'name': '_token'})
            login_data = {'email': USERNAME, 'password': PASSWORD}
            if token_input:
                login_data['_token'] = token_input['value']

            login_res = await client.post(LOGIN_URL, data=login_data, headers=headers)
            if "login" in str(login_res.url):
                print("❌ Login failed. Check username/password.")
                return
            dashboard_soup = BeautifulSoup(login_res.text, 'html.parser')
            csrf_token_meta = dashboard_soup.find('meta', {'name': 'csrf-token'})
            if not csrf_token_meta:
                print("❌ CSRF token not found.")
                return
            csrf_token = csrf_token_meta.get('content')
            headers['Referer'] = str(login_res.url)

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
                    print(f"✔️ New message found from: {msg['number']}.")
                    for chat_id in chat_ids_to_send:
                        await send_telegram_message(context, chat_id, msg)
                    save_processed_id(msg["id"])

            if new_messages_found > 0:
                print(f"✅ Total {new_messages_found} new messages sent to Telegram.")
        except httpx.RequestError as e:
            print("❌ Network or login issue (httpx):", e)
        except Exception as e:
            print("❌ A problem occurred in the main process:", e)
            traceback.print_exc()

# -----------------------------
# Start the bot
# -----------------------------
def main():
    print("🚀 iVasms to Telegram Bot is starting...")
    if not ADMIN_CHAT_IDS:
        print("\n!!! 🔴 WARNING: ADMIN_CHAT_IDS is empty. Set ADMIN_CHAT_IDS env var (comma separated user IDs). !!!\n")
        # Note: We do not exit; admins may still be set later.

    if not YOUR_BOT_TOKEN:
        print("❌ YOUR_BOT_TOKEN is not set. Set environment variable YOUR_BOT_TOKEN and restart.")
        return

    application = Application.builder().token(YOUR_BOT_TOKEN).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("add_chat", add_chat_command))
    application.add_handler(CommandHandler("remove_chat", remove_chat_command))
    application.add_handler(CommandHandler("list_chats", list_chats_command))

    # Schedule job
    job_queue = application.job_queue
    job_queue.run_repeating(check_sms_job, interval=POLLING_INTERVAL_SECONDS, first=1)

    print(f"🚀 Checking for new messages every {POLLING_INTERVAL_SECONDS} seconds.")
    application.run_polling()

if __name__ == "__main__":
    main()
