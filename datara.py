##############################################################
#   DATARA BOT – FINAL WEBHOOK VERSION (RENDER + PTB 21.x)   #
##############################################################

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

from google.oauth2.service_account import Credentials
import gspread

import google.generativeai as genai

from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

##############################################################
# CONFIGURATION (your fallbacks)
##############################################################

BOT_TOKEN = os.getenv("BOT_TOKEN", "8441717075:AAGmsAqLYQSCT9EjiCxoJniHj4qxqD_lUYo")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyC7zbNfvfvjtjpu8mJexyAY5JO7qO3I9jk")
PDF_SHEET_ID = os.getenv("PDF_SHEET_ID", "1ME1I3OyFS9VYH2qeqHA5Elt9_f0XXNkkmDgyreVLylo")
INFO_SHEET_ID = os.getenv("INFO_SHEET_ID", "1kUvOq9_HqBVk6dlfnDpMV7FJ9GbGSGXtrrC1zB6O5Oc")

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not GOOGLE_CREDENTIALS_JSON:
    raise RuntimeError("Missing GOOGLE_CREDENTIALS_JSON env var")

##############################################################
# GOOGLE AUTH + SHEETS
##############################################################

creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
creds = Credentials.from_service_account_info(
    creds_info,
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ],
)
client = gspread.authorize(creds)
pdf_sheet = client.open_by_key(PDF_SHEET_ID).sheet1
info_sheet = client.open_by_key(INFO_SHEET_ID).sheet1

##############################################################
# GEMINI
##############################################################
genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-2.0-flash"

async def ai_tone(text: str):
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        resp = await asyncio.to_thread(model.generate_content, text)
        return resp.text.strip()
    except:
        return text

##############################################################
# HELPERS
##############################################################

def clean(t): return re.sub(r"[^a-zA-Z0-9\s]", " ", t).lower().strip()

def get_drive_name(url):
    try:
        h = requests.head(url, allow_redirects=True, timeout=10)
        cd = h.headers.get("content-disposition", "")
        if "filename=" in cd:
            return unquote(cd.split("filename=")[1].strip('"'))
    except:
        pass
    return "file.pdf"

def drive_dl(url):
    if "drive.google.com/file/d/" in url:
        f = url.split("/d/")[1].split("/")[0]
        return f"https://drive.google.com/uc?export=download&id={f}"
    if "drive.google.com/open?id=" in url:
        f = url.split("id=")[1].split("&")[0]
        return f"https://drive.google.com/uc?export=download&id={f}"
    return url

##############################################################
# TELEGRAM BOT (PTB 21.x)
##############################################################

application = Application.builder().token(BOT_TOKEN).build()

async def start(update: Update, ctx):
    await update.message.reply_text("👋 Datara Bot is ready via webhook mode!")

async def handler(update: Update, ctx):
    raw = update.message.text or ""
    msg = clean(raw)

    # casual
    if msg in ("hi", "hello", "hey"):
        await update.message.reply_text("👋 Hello! How can I help?")
        return

    pdf_data = pdf_sheet.get_all_records()
    info_data = info_sheet.get_all_records()

    found_info = []
    found_pdf = []
    all_keywords = []

    ##### INFO SEARCH
    for row in info_data:
        keys = [clean(k) for k in str(row.get("keywords", "")).split(",")]
        ans = row.get("answer") or row.get("info") or ""
        for k in keys:
            if k in msg or msg in k:
                found_info.append((k, ans))
                break

    if found_info:
        combined = "\n\n".join(f"📘 {kw} — {ans}" for kw, ans in found_info)
        final = await ai_tone(combined)
        await update.message.reply_text(final)
        return

    ##### PDF SEARCH
    for row in pdf_data:
        keys = [clean(k) for k in str(row.get("keyword", "")).split(",")]
        all_keywords += keys
        for k in keys:
            if k in msg or msg in k:
                found_pdf.append((k, drive_dl(row["file_url"])))
                break

    if found_pdf:
        for kw, link in found_pdf:
            await update.message.reply_text(f"📎 Fetching {kw} PDF...")
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(link) as r:
                        if r.status != 200:
                            await update.message.reply_text("Download failed.")
                            return
                        data = BytesIO(await r.read())
                        await update.message.reply_document(
                            document=data,
                            filename=get_drive_name(link)
                        )
            except Exception as e:
                await update.message.reply_text(str(e))
        return

    ##### CLOSE MATCHES
    if len(msg.split()) <= 3:
        from difflib import get_close_matches
        m = get_close_matches(msg, all_keywords, n=4, cutoff=0.45)
        if m:
            await update.message.reply_text("Did you mean:\n• " + "\n• ".join(m))
            return

    ##### GEMINI fallback
    model = genai.GenerativeModel(MODEL_NAME)
    resp = await asyncio.to_thread(
        model.generate_content,
        f"Answer clearly: {raw}",
    )
    ans = await ai_tone(resp.text)
    await update.message.reply_text(ans)

# register handlers
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handler))

##############################################################
# RUN PTB IN BACKGROUND THREAD
##############################################################

app_loop = None

async def ptb_main():
    await application.initialize()
    await application.start()
    print("PTB READY ✔")
    await asyncio.Event().wait()

def start_ptb():
    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        global app_loop
        app_loop = loop
        loop.run_until_complete(ptb_main())

    threading.Thread(target=runner, daemon=True).start()

##############################################################
# FLASK WEBHOOK ENDPOINTS (SYNC ONLY)
##############################################################

app = Flask(__name__)

@app.get("/")
def home():
    return "Datara Bot running (webhook mode)", 200

@app.post("/")
def webhook():
    if not app_loop:
        return "PTB NOT READY", 503

    data = request.get_json(force=True)
    bot_obj = Bot(BOT_TOKEN)
    update_obj = Update.de_json(data, bot_obj)

    fut = asyncio.run_coroutine_threadsafe(
        application.update_queue.put(update_obj),
        app_loop
    )
    fut.result(timeout=5)
    return "OK", 200

@app.get("/set-webhook")
def set_webhook():
    # Render gives RENDER_EXTERNAL_URL automatically
    base = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if not base:
        return "Missing WEBHOOK_URL or RENDER_EXTERNAL_URL", 400

    url = base.rstrip("/") + "/"

    if not app_loop:
        return "PTB NOT READY", 503

    fut = asyncio.run_coroutine_threadsafe(
        application.bot.set_webhook(url),
        app_loop
    )
    fut.result(timeout=10)
    return jsonify({"webhook_url": url, "status": "OK"}), 200

##############################################################
# START EVERYTHING
##############################################################

if __name__ == "__main__":
    print("🚀 Starting Datara Bot (webhook mode)...")
    start_ptb()

    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
