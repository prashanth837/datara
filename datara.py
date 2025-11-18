# datara_webhook_final.py
"""
Full webhook-mode Datara bot (PTB 21.x) ready for Render (web service) or local testing.
Features:
- Uses Telegram webhook (no polling) integrated with Flask
- Runs PTB Application in a background asyncio loop (safe for Flask)
- Loads Google service account either from GOOGLE_CREDENTIALS_JSON env OR local
  service_account.json file (so it works locally and on Render)
- Async PDF downloads with aiohttp (chunked streaming)
- Gemini fallback via google.generativeai
- Short-keyword suggestions, strict matching to avoid double replies
- Exposes /set-webhook, /healthz endpoints
Requirements (requirements.txt):
python-telegram-bot==21.x
Flask
aiohttp
gspread
google-auth
google-generativeai
requests
"""

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

# Google / Sheets
from google.oauth2.service_account import Credentials
import gspread

# Gemini
import google.generativeai as genai

# Telegram (PTB 21.x)
from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --------------------------
# CONFIGURATION (fallbacks allowed)
# --------------------------
BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "8441717075:AAGmsAqLYQSCT9EjiCxoJniHj4qxqD_lUYo",
)
GEMINI_API_KEY = os.getenv(
    "GEMINI_API_KEY",
    "AIzaSyC7zbNfvfvjtjpu8mJexyAY5JO7qO3I9jk",
)
PDF_SHEET_ID = os.getenv(
    "PDF_SHEET_ID",
    "1ME1I3OyFS9VYH2qeqHA5Elt9_f0XXNkkmDgyreVLylo",
)
INFO_SHEET_ID = os.getenv(
    "INFO_SHEET_ID",
    "1kUvOq9_HqBVk6dlfnDpMV7FJ9GbGSGXtrrC1zB6O5Oc",
)

# GOOGLE CREDENTIALS: prefer env var, else local service_account.json
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", None)
LOCAL_JSON_FILE = "service_account.json"

# --------------------------
# LOAD GOOGLE CREDENTIALS (env OR local file)
# --------------------------
def load_google_credentials():
    # 1) If env var provided, try to parse it
    if GOOGLE_CREDENTIALS_JSON:
        raw = GOOGLE_CREDENTIALS_JSON.strip()
        # If the env contains a path to a file, attempt to read the file
        if os.path.isfile(raw):
            with open(raw, "r", encoding="utf-8") as f:
                return json.load(f)
        # Otherwise assume it's JSON text
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            # Try to be forgiving: maybe JSON contains literal newline escapes missing
            raise RuntimeError(
                "GOOGLE_CREDENTIALS_JSON is set but not valid JSON. "
                "If you intended to provide a file path, set the env to that path. "
                f"JSON error: {e}"
            ) from e

    # 2) Fallback: try to read local service_account.json (for local testing)
    if os.path.isfile(LOCAL_JSON_FILE):
        with open(LOCAL_JSON_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    # 3) Not found
    raise RuntimeError(
        "Google credentials not found. Set GOOGLE_CREDENTIALS_JSON env (JSON string or path) "
        "or place service_account.json in the project root."
    )


# Attempt load credentials (will raise with clear message if missing)
print("🔐 Loading Google credentials...")
creds_info = load_google_credentials()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
client = gspread.authorize(creds)

pdf_sheet = client.open_by_key(PDF_SHEET_ID).sheet1
info_sheet = client.open_by_key(INFO_SHEET_ID).sheet1

# --------------------------
# Gemini setup
# --------------------------
genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-2.0-flash"

# --------------------------
# Helpers
# --------------------------
def clean_text(t: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\s]", " ", (t or "")).lower().strip()


def get_drive_file_name(url: str) -> str:
    try:
        response = requests.head(url, allow_redirects=True, timeout=10)
        cd = response.headers.get("content-disposition", "")
        if "filename=" in cd:
            filename = cd.split("filename=")[1].strip('"')
            return unquote(filename)
    except Exception:
        pass
    return "file.pdf"


def get_drive_download_link(url: str) -> str:
    if not url:
        return url
    if "drive.google.com/file/d/" in url:
        fid = url.split("/d/")[1].split("/")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"
    if "drive.google.com/open?id=" in url:
        fid = url.split("id=")[1].split("&")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"
    return url


async def ai_tone(text: str) -> str:
    try:
        prompt = (
            "Rewrite this answer in one short, clear, polite sentence for a college assistant bot.\n"
            "Do NOT add greetings or questions.\n\n"
            f"{text}"
        )
        temp = genai.GenerativeModel(MODEL_NAME)
        resp = await asyncio.to_thread(temp.generate_content, prompt)
        return resp.text.strip()
    except Exception:
        return text.strip()


# --------------------------
# Casual replies
# --------------------------
CASUAL_REPLIES = {
    "hi": "👋 Hey there!",
    "hello": "Hello! 😊 How can I help you today?",
    "hey": "Hey! 👋",
    "bye": "Goodbye! Have a great day!",
    "thanks": "You're welcome 😊",
    "thank you": "Glad to help 🤝",
    "who are you": "I’m Datara Bot 🤖 — your AITS helper!",
}

# --------------------------
# Telegram Handlers (async)
# --------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Hi! I’m Datara Bot, your AITS assistant.")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_msg = update.message.text or ""
    msg = clean_text(raw_msg)

    # casual
    if msg in CASUAL_REPLIES:
        await update.message.reply_text(CASUAL_REPLIES[msg])
        return

    # load sheets fresh
    pdf_data = pdf_sheet.get_all_records()
    info_data = info_sheet.get_all_records()

    found_info = []
    found_pdf = []
    all_pdf_keywords = []

    # info search
    for row in info_data:
        keywords = [clean_text(k) for k in str(row.get("keywords", "")).split(",") if k.strip()]
        answer = row.get("answer") or row.get("info") or ""
        for kw in keywords:
            if kw in msg or msg in kw:
                found_info.append((kw, answer))
                break

    if found_info:
        combined = "\n\n".join([f"📘 {kw.title()} — {ans}" for kw, ans in found_info])
        final = await ai_tone(combined)
        await update.message.reply_text(final)
        return

    # pdf search
    for row in pdf_data:
        keywords = [clean_text(k) for k in str(row.get("keyword", "")).split(",") if k.strip()]
        all_pdf_keywords += keywords
        for kw in keywords:
            if kw in msg or msg in kw:
                file_url = get_drive_download_link(row.get("file_url", ""))
                found_pdf.append((kw, file_url))
                break

    if found_pdf:
        for kw, link in found_pdf:
            await update.message.reply_text(f"📎 Fetching {kw.title()} PDF...")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(link) as resp:
                        if resp.status != 200:
                            await update.message.reply_text("⚠ PDF download failed.")
                            return
                        file_bytes = BytesIO()
                        while True:
                            chunk = await resp.content.read(1024 * 256)
                            if not chunk:
                                break
                            file_bytes.write(chunk)
                        file_bytes.seek(0)
                        await update.message.reply_document(file_bytes, filename=get_drive_file_name(link))
            except Exception as e:
                await update.message.reply_text(f"⚠ Error downloading PDF: {e}")
        return

    # suggestions for short queries only
    word_count = len(msg.split())
    if word_count <= 3:
        from difflib import get_close_matches

        matches = get_close_matches(msg, all_pdf_keywords, n=4, cutoff=0.45)
        if matches:
            await update.message.reply_text("Did you mean:\n• " + "\n• ".join(matches))
            return

    # fallback to Gemini
    try:
        prompt = f"You are a helpful AITS assistant. Answer this question clearly:\n\n{raw_msg}"
        temp = genai.GenerativeModel(MODEL_NAME)
        resp = await asyncio.to_thread(temp.generate_content, prompt)
        ai_answer = await ai_tone(resp.text)
        await update.message.reply_text(ai_answer)
    except Exception:
        await update.message.reply_text("I'm here to help, but I couldn't generate an answer right now.")


# --------------------------
# Setup PTB Application (21.x)
# --------------------------
print("🔧 Building PTB Application (webhook-ready)...")
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

# We'll run the Application in its own asyncio loop inside a background thread.
app_loop = None  # will point to that event loop


async def _start_application():
    await application.initialize()
    await application.start()
    print("✅ PTB Application initialized and started in background loop.")
    await asyncio.Event().wait()  # keep running


def start_application_in_thread():
    global app_loop
    def _runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app_loop = loop
        try:
            loop.run_until_complete(_start_application())
        finally:
            loop.run_until_complete(application.shutdown())
            loop.close()

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return t


# --------------------------
# Flask app (webhook receiver)
# --------------------------
flask_app = Flask(__name__)


@flask_app.route("/", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json(force=True)
        bot_for_decode = Bot(token=BOT_TOKEN)
        update_obj = Update.de_json(data, bot_for_decode)

        if app_loop is None:
            return "PTB not ready", 503

        fut = asyncio.run_coroutine_threadsafe(application.update_queue.put(update_obj), app_loop)
        fut.result(timeout=5)
        return "OK", 200
    except Exception as e:
        print("Webhook handling error:", e)
        return "Error", 500


@flask_app.get("/set-webhook")
def set_webhook():
    try:
        webhook_base = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
        if not webhook_base:
            return (
                "No WEBHOOK_URL or RENDER_EXTERNAL_URL found. Provide WEBHOOK_URL env or use ngrok locally.",
                400,
            )
        webhook_url = webhook_base.rstrip("/") + "/"
        if app_loop is None:
            return "PTB not ready", 503
        coro = application.bot.set_webhook(webhook_url)
        fut = asyncio.run_coroutine_threadsafe(coro, app_loop)
        fut.result(timeout=10)
        return jsonify({"status": "ok", "webhook_url": webhook_url}), 200
    except Exception as e:
        print("set_webhook error:", e)
        return f"Error: {e}", 500


@flask_app.get("/healthz")
def healthz():
    return "ok", 200


# --------------------------
# Launch everything
# --------------------------
if __name__ == "__main__":
    print("🚀 Starting PTB Application in background thread...")
    t = start_application_in_thread()

    # Bind Flask to Render's assigned port (or local default)
    port = int(os.environ.get("PORT", 10000))
    print(f"🌐 Flask listening on 0.0.0.0:{port} - ready for webhooks")
    # Use Flask's run for simple deployments; Render expects a port-bound web service.
    flask_app.run(host="0.0.0.0", port=port)
