import os
import re
import json
import asyncio
from io import BytesIO
from urllib.parse import unquote

import aiohttp
import requests
from flask import Flask, request, jsonify

# Google Sheets
from google.oauth2.service_account import Credentials
import gspread

# Gemini
import google.generativeai as genai

# Telegram PTB 21.x
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG (with fallbacks you wanted)
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8441717075:AAGmsAqLYQSCT9EjiCxoJniHj4qxqD_lUYo")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyC7zbNfvfvjtjpu8mJexyAY5JO7qO3I9jk")
PDF_SHEET_ID = os.getenv("PDF_SHEET_ID", "1ME1I3OyFS9VYH2qeqHA5Elt9_f0XXNkkmDgyreVLylo")
INFO_SHEET_ID = os.getenv("INFO_SHEET_ID", "1kUvOq9_HqBVk6dlfnDpMV7FJ9GbGSGXtrrC1zB6O5Oc")

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not GOOGLE_CREDENTIALS_JSON:
    raise RuntimeError("❌ Missing GOOGLE_CREDENTIALS_JSON env variable.")


# ============================================================
# GOOGLE SHEETS ACCESS
# ============================================================
creds = Credentials.from_service_account_info(
    json.loads(GOOGLE_CREDENTIALS_JSON),
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ],
)
client = gspread.authorize(creds)

pdf_sheet = client.open_by_key(PDF_SHEET_ID).sheet1
info_sheet = client.open_by_key(INFO_SHEET_ID).sheet1


# ============================================================
# GEMINI SETUP
# ============================================================
genai.configure(api_key=GEMINI_API_KEY)
MODEL = genai.GenerativeModel("gemini-2.0-flash")


# ============================================================
# HELPERS
# ============================================================
def clean(t):
    return re.sub(r"[^a-zA-Z0-9\s]", " ", t.lower().strip())


def drive_file_name(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
        cd = r.headers.get("content-disposition", "")
        if "filename=" in cd:
            return unquote(cd.split("filename=")[1].strip('"'))
    except:
        pass
    return "file.pdf"


def drive_link(url):
    if "drive.google.com/file/d/" in url:
        fid = url.split("/d/")[1].split("/")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"
    if "drive.google.com/open?id=" in url:
        fid = url.split("id=")[1].split("&")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"
    return url


async def ai_polish(text):
    try:
        p = "Rewrite clearly and politely in one sentence:\n" + text
        resp = await asyncio.to_thread(MODEL.generate_content, p)
        return resp.text.strip()
    except:
        return text


# Casual replies
CASUAL = {
    "hi": "👋 Hey!",
    "hello": "Hello! 😊",
    "hey": "Hey 👋",
    "bye": "Goodbye!",
    "thanks": "You're welcome 😊"
}


# ============================================================
# TELEGRAM HANDLERS (ASYNC)
# ============================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Datara Bot ready (webhook mode).")


async def main_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = update.message.text or ""
    msg = clean(raw)

    if msg in CASUAL:
        return await update.message.reply_text(CASUAL[msg])

    pdf_data = pdf_sheet.get_all_records()
    info_data = info_sheet.get_all_records()

    found_info = []
    found_pdf = []
    all_pdf_keys = []

    # INFO SEARCH
    for row in info_data:
        keys = [clean(k) for k in row.get("keywords", "").split(",")]
        ans = row.get("answer") or ""
        for k in keys:
            if k in msg or msg in k:
                found_info.append(ans)
                break

    if found_info:
        final = await ai_polish("\n".join(found_info))
        return await update.message.reply_text(final)

    # PDF SEARCH
    for row in pdf_data:
        keys = [clean(k) for k in row.get("keyword", "").split(",")]
        all_pdf_keys += keys
        for k in keys:
            if k in msg or msg in k:
                found_pdf.append(drive_link(row["file_url"]))
                break

    if found_pdf:
        for link in found_pdf:
            await update.message.reply_text("📎 Fetching PDF…")
            async with aiohttp.ClientSession() as s:
                async with s.get(link) as r:
                    if r.status != 200:
                        return await update.message.reply_text("⚠ PDF download failed.")
                    buf = BytesIO(await r.read())
                    await update.message.reply_document(buf, filename=drive_file_name(link))
        return

    # SUGGEST CLOSE MATCHES
    from difflib import get_close_matches
    matches = get_close_matches(msg, all_pdf_keys, n=4, cutoff=0.45)
    if matches:
        return await update.message.reply_text("Did you mean:\n• " + "\n• ".join(matches))

    # GEMINI FALLBACK
    resp = await asyncio.to_thread(MODEL.generate_content, raw)
    return await update.message.reply_text(await ai_polish(resp.text))


# ============================================================
# BUILD APPLICATION (NO THREADS, NO QUEUES!)
# ============================================================
application = (
    Application.builder()
    .token(BOT_TOKEN)
    .updater(None)  # disable old polling
    .build()
)

application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, main_handler))


# ============================================================
# FLASK WEBHOOK SERVER
# ============================================================
app = Flask(__name__)


@app.post("/")
def receive_update():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.process_update(update)
    return "OK", 200


@app.get("/set-webhook")
async def set_webhook():
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return "Missing RENDER_EXTERNAL_URL", 400

    webhook_url = url.rstrip("/") + "/"
    await application.bot.set_webhook(webhook_url)
    return jsonify({"webhook_set_to": webhook_url})


@app.get("/")
def root():
    return "Datara Webhook Online", 200


# ============================================================
# START FLASK SERVER
# ============================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    print("🚀 Datara Webhook bot running on port", port)
    application.initialize()
    application.start()
    app.run(host="0.0.0.0", port=port)
