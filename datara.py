import os
import re
import json
import threading
import asyncio
from io import BytesIO
from urllib.parse import unquote

import aiohttp
import requests
from flask import Flask, request, jsonify

# GOOGLE
from google.oauth2.service_account import Credentials
import gspread

# GEMINI
import google.generativeai as genai

# TELEGRAM (PTB 21.x)
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters


# =============================
# CONFIG (YOUR VALUES)
# =============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8441717075:AAGmsAqLYQSCT9EjiCxoJniHj4qxqD_lUYo")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyC7zbNfvfvjtjpu8mJexyAY5JO7qO3I9jk")
PDF_SHEET_ID = os.getenv("PDF_SHEET_ID", "1ME1I3OyFS9VYH2qeqHA5Elt9_f0XXNkkmDgyreVLylo")
INFO_SHEET_ID = os.getenv("INFO_SHEET_ID", "1kUvOq9_HqBVk6dlfnDpMV7FJ9GbGSGXtrrC1zB6O5Oc")

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not GOOGLE_CREDENTIALS_JSON:
    raise RuntimeError("❌ GOOGLE_CREDENTIALS_JSON missing!")


# =============================
# GOOGLE SHEETS INIT
# =============================
creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly"
]
creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
client = gspread.authorize(creds)

pdf_sheet = client.open_by_key(PDF_SHEET_ID).sheet1
info_sheet = client.open_by_key(INFO_SHEET_ID).sheet1


# =============================
# GEMINI INIT
# =============================
genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-2.0-flash"


# =============================
# HELPERS
# =============================
def clean_text(t):
    return re.sub(r"[^a-zA-Z0-9\s]", " ", (t or "")).lower().strip()


def get_drive_file_name(url):
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
        cd = r.headers.get("content-disposition", "")
        if "filename=" in cd:
            return unquote(cd.split("filename=")[1].strip('"'))
    except:
        pass
    return "file.pdf"


def get_drive_download_link(url):
    if "drive.google.com/file/d/" in url:
        fid = url.split("/d/")[1].split("/")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"
    if "drive.google.com/open?id=" in url:
        fid = url.split("id=")[1].split("&")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"
    return url


async def ai_tone(text):
    try:
        prompt = "Rewrite this clearly and politely:\n" + text
        m = genai.GenerativeModel(MODEL_NAME)
        resp = await asyncio.to_thread(m.generate_content, prompt)
        return resp.text.strip()
    except:
        return text.strip()


CASUAL = {
    "hi": "👋 Hey there!",
    "hello": "Hello! 😊",
    "hey": "Hey! 👋",
    "bye": "Goodbye! 👋",
    "thanks": "You're welcome 😊",
}


# =============================
# TELEGRAM HANDLERS
# =============================
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Datara Bot is ready (Webhook Mode).")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_raw = update.message.text or ""
    msg = clean_text(text_raw)

    if msg in CASUAL:
        await update.message.reply_text(CASUAL[msg])
        return

    pdf_data = pdf_sheet.get_all_records()
    info_data = info_sheet.get_all_records()

    found_info = []
    found_pdf = []
    all_keys = []

    # INFO SEARCH
    for row in info_data:
        keys = [clean_text(k) for k in str(row.get("keywords", "")).split(",")]
        ans = row.get("answer") or row.get("info") or ""
        for kw in keys:
            if kw in msg or msg in kw:
                found_info.append((kw, ans))
                break

    if found_info:
        combined = "\n\n".join([f"📘 {k.title()} — {a}" for k, a in found_info])
        final = await ai_tone(combined)
        await update.message.reply_text(final)
        return

    # PDF SEARCH
    for row in pdf_data:
        keys = [clean_text(k) for k in str(row.get("keyword", "")).split(",")]
        all_keys += keys
        for kw in keys:
            if kw in msg or msg in kw:
                url = get_drive_download_link(row["file_url"])
                found_pdf.append((kw, url))
                break

    if found_pdf:
        for kw, url in found_pdf:
            await update.message.reply_text(f"📎 Fetching {kw} PDF...")

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        file_data = BytesIO(await resp.read())
                        await update.message.reply_document(
                            document=file_data,
                            filename=get_drive_file_name(url)
                        )
            except Exception as e:
                await update.message.reply_text(f"⚠ Error: {e}")

        return

    # SIMILAR KEYWORDS
    if len(msg.split()) <= 3:
        from difflib import get_close_matches
        matches = get_close_matches(msg, all_keys, n=4, cutoff=0.45)
        if matches:
            await update.message.reply_text("Did you mean:\n• " + "\n• ".join(matches))
            return

    # GEMINI FALLBACK
    try:
        prompt = f"Answer clearly:\n{text_raw}"
        m = genai.GenerativeModel(MODEL_NAME)
        resp = await asyncio.to_thread(m.generate_content, prompt)
        text = await ai_tone(resp.text)
        await update.message.reply_text(text)
    except:
        await update.message.reply_text("I couldn’t answer right now.")


# =============================
# PTB APPLICATION (in background thread)
# =============================
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

app_loop = None


def start_ptb_thread():
    def runner():
        global app_loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app_loop = loop
        loop.run_until_complete(application.initialize())
        loop.run_until_complete(application.start())
        loop.run_forever()

    threading.Thread(target=runner, daemon=True).start()


# =============================
# FLASK WEBHOOK SERVER
# =============================
app = Flask(__name__)


@app.post("/")
def webhook():
    if app_loop is None:
        return "PTB not ready", 503

    data = request.get_json(force=True)
    update = Update.de_json(data, Bot(BOT_TOKEN))
    asyncio.run_coroutine_threadsafe(application.update_queue.put(update), app_loop)

    return "OK", 200


@app.get("/")
def home():
    return "Datara Bot Webhook Active", 200


@app.get("/set-webhook")
def set_webhook_route():
    if app_loop is None:
        return "PTB not ready", 503

    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        return "Render URL missing", 400

    webhook_url = url.rstrip("/") + "/"
    future = asyncio.run_coroutine_threadsafe(
        application.bot.set_webhook(webhook_url), app_loop
    )
    future.result(10)

    return jsonify({"status": "ok", "webhook": webhook_url})


# =============================
# START EVERYTHING
# =============================
if __name__ == "__main__":
    print("🚀 Starting PTB in background...")
    start_ptb_thread()

    port = int(os.getenv("PORT", 10000))
    print(f"🌐 Flask listening on port {port} (Webhook mode enabled)")
    app.run(host="0.0.0.0", port=port)
