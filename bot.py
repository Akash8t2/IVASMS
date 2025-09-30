#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OTP/SMS Forwarder for http://45.82.67.20 dashboards
Format style: iVasms-like (Vibeꭙ Flow™)
"""

import os
import time
import json
import logging
import requests
import re
import random
import string
from datetime import datetime
from bs4 import BeautifulSoup
from hashlib import sha1

import phonenumbers
import pycountry

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Telegram bot token
CHAT_IDS = os.getenv("CHAT_IDS", "")  # comma separated chat ids
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "6"))  # seconds between polls
SITE_BASE = os.getenv("SITE_BASE", "http://45.82.67.20")
DASH_PATH = os.getenv("DASH_PATH", "/ints/agent/SMSDashboard")
CDR_PATH = os.getenv("CDR_PATH", "/ints/agent/SMSCDRStats")
FULL_URL = os.getenv("FULL_URL")  # optional: override with full URL
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64)")
STATE_FILE = os.getenv("STATE_FILE", "processed_sms_ids.json")
MAX_SEEN = int(os.getenv("MAX_SEEN", "20000"))

# ---------- derived ----------
if FULL_URL:
    POLL_URLS = [FULL_URL]
else:
    base = SITE_BASE.rstrip("/")
    POLL_URLS = [base + DASH_PATH, base + CDR_PATH]

CHAT_IDS_LIST = [c.strip() for c in CHAT_IDS.split(",") if c.strip()]
TELEGRAM_API_SEND = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

# ---------- logging ----------
logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO)
logger = logging.getLogger("otp_forwarder")

# ---------- dedupe ----------
def load_seen():
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, "r") as f:
            arr = json.load(f)
            return set(arr)
    except Exception:
        return set()

def save_seen(seen_set):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(list(seen_set), f)
    except Exception as e:
        logger.warning("Could not save state file: %s", e)

seen = load_seen()

# ---------- regex helpers ----------
PHONE_RE = re.compile(r'(\+?\d{6,15})')
OTP_RE = re.compile(r'\b(\d{4,8})\b')
OTP_RE_ALT = re.compile(r'(\d{3}-\d{3})')

# ---------- parsing ----------
def http_get(url):
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logger.warning("GET %s failed: %s", url, e)
        return ""

def extract_messages_from_html(html):
    out = []
    soup = BeautifulSoup(html, "html.parser")

    # Table rows
    for tr in soup.find_all("tr"):
        text = tr.get_text(separator="\n", strip=True)
        if not text: continue
        phones = PHONE_RE.findall(text)
        phone = phones[0] if phones else ""
        code = OTP_RE_ALT.search(text) or OTP_RE.search(text)
        code_val = code.group(1) if code else ""
        uid = sha1((phone + "|" + text).encode("utf-8")).hexdigest()
        out.append({"id": uid, "number": phone, "text": text, "code": code_val})

    # Deduplicate
    unique = []
    seen_ids = set()
    for e in out:
        if e["id"] in seen_ids: continue
        seen_ids.add(e["id"])
        unique.append(e)
    return unique

# ---------- service & country ----------
def detect_service(text):
    text_low = text.lower()
    if "telegram" in text_low:
        return "📩 Telegram"
    if "facebook" in text_low:
        return "📘 Facebook"
    if "google" in text_low or "gmail" in text_low:
        return "📧 Google"
    if "whatsapp" in text_low:
        return "💚 WhatsApp"
    if "instagram" in text_low:
        return "📷 Instagram"
    return "📩 Unknown"

def get_country(number):
    try:
        pn = phonenumbers.parse(number, None)
        country = pycountry.countries.get(alpha_2=phonenumbers.region_code_for_number(pn))
        if country:
            return country.name
    except Exception:
        return ""
    return ""

def random_tail(length=10):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

# ---------- formatting ----------
def format_for_telegram(entry):
    number = entry.get("number") or "N/A"
    code = entry.get("code") or "N/A"
    text = entry.get("text","").strip()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    service = detect_service(text)
    country = get_country(number) or "Unknown"
    tail = random_tail()

    login_link = f"\nYou can also tap on this link to log in:\nhttps://t.me/login/{code}" if "Telegram" in service else ""

    msg = (
        "˹𝐕𝐢𝐛𝐞ꭙ 𝐅ʟ𝐨𝐰™ ˼:\n"
        "🔔 You have successfully received OTP\n\n"
        f"📞 Number: {number}\n"
        f"🔑 Code: {code}\n"
        f"🏆 Service: {service}\n"
        f"🌎 Country: {country}\n"
        f"⏳ Time: {now}\n\n"
        f"💬 Message:\n{text}\n"
    )
    return msg

# ---------- telegram ----------
def send_to_telegram(chat_id, text):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(TELEGRAM_API_SEND, data=payload, timeout=10)
        if r.status_code != 200:
            logger.warning("Telegram send failed %s -> %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.exception("Failed to send telegram message: %s", e)

# ---------- main loop ----------
def poll_loop():
    logger.info("Polling URLs: %s", POLL_URLS)
    global seen
    while True:
        try:
            for url in POLL_URLS:
                html = http_get(url)
                if not html: continue
                items = extract_messages_from_html(html)
                for entry in reversed(items):
                    uid = entry["id"]
                    if uid in seen: continue
                    seen.add(uid)
                    msg = format_for_telegram(entry)
                    logger.info("Forwarding OTP: %s...", uid[:8])
                    for cid in CHAT_IDS_LIST:
                        send_to_telegram(cid, msg)
                    time.sleep(0.3)
                if len(seen) > MAX_SEEN:
                    seen = set(list(seen)[-MAX_SEEN//2:])
            save_seen(seen)
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            save_seen(seen)
            break
        except Exception as e:
            logger.exception("Error in loop: %s", e)
            time.sleep(5)

if __name__ == "__main__":
    if not BOT_TOKEN or not CHAT_IDS_LIST:
        logger.error("BOT_TOKEN and CHAT_IDS required.")
        raise SystemExit(1)
    poll_loop()
