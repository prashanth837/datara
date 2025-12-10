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



# ======================================================
# MEMORY + SUMMARY
# ======================================================
USER_MEMORY = {}        # {user_id:[{role,msg}]}
USER_SUMMARY = {}       # {user_id:"summary text"}
MAX_MESSAGES = 8        # compress every 8 msgs



def save_memory(user_id, role, text):
    if user_id not in USER_MEMORY:
        USER_MEMORY[user_id] = []
    USER_MEMORY[user_id].append({"role": role, "text": text})


async def auto_summarize(user_id, model):
    if len(USER_MEMORY.get(user_id, [])) < MAX_MESSAGES:
        return

    history = ""
    for m in USER_MEMORY[user_id]:
        history += f"{m['role']}: {m['text']}\n"

    prompt = (
        "Summarize the following chat history in 3 short lines. "
        "Focus on main discussion only, remove greetings:\n\n" + history
    )

    resp = await asyncio.to_thread(model.generate_content, prompt)
    summary = resp.text.strip()

    USER_SUMMARY[user_id] = summary
    USER_MEMORY[user_id] = []   # keep next fresh messages



# =============================
# CONFIG
# =============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8441717075:AAGmsAqLYQSCT9EjiCxoJniHj4qxqD_lUYo")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("❌ GEMINI_API_KEY missing!")

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
MODEL_NAME = "models/gemini-2.5-flash"



# =============================
# HELPERS
# =============================
def clean_text(t: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\s]", " ", (t or "")).lower().strip()


def get_drive_file_name(url: str) -> str:
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
        cd = r.headers.get("content-disposition", "")
        if "filename=" in cd:
            return unquote(cd.split("filename=")[1].strip('"'))
    except:
        pass
    return "file.pdf"


def get_drive_download_link(url: str) -> str:
    if "drive.google.com/file/d/" in url:
        fid = url.split("/d/")[1].split("/")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"
    if "drive.google.com/open?id=" in url:
        fid = url.split("id=")[1].split("&")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"
    return url




# =============================
# CASUAL
# =============================
CASUAL = {
    "hi": "👋 Hey there..! how can i help you?",
    "hlo": "hello..!! how can i help you..?",
    "hello": "Hello! 😊",
    "hey": "Hey! 👋",
    "bye": "Goodbye! 👋",
    "thanks": "You're welcome 😊",
    "thank you": "you're welcome",
    "who are you": (
        "I am Datara Bot 🤖, an AI chatbot working under the Department of Data Science. "
        "Please let me know how I may assist you."
    ),
}



# =============================
# HANDLERS
# =============================
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Datara Bot is ready (Webhook Mode).")



async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text_raw = update.message.text or ""
    msg = clean_text(text_raw)

    # memory store
    user_id = update.message.from_user.id
    save_memory(user_id, "user", text_raw)



    # casual
    if msg in CASUAL:
        reply = CASUAL[msg]
        await update.message.reply_text(reply)
        save_memory(user_id,"bot",reply)
        return



    pdf_data = pdf_sheet.get_all_records()
    info_data = info_sheet.get_all_records()

    found_info = []
    found_pdf = []
    all_keys = []



    # info search
    for row in info_data:
        keys = [clean_text(k) for k in str(row.get("keywords", "")).split(",")]
        all_keys.extend(keys)
        ans = row.get("answer") or row.get("info") or ""

        for kw in keys:
            if kw and (kw in msg or msg in kw):
                found_info.append(ans)
                break

    if found_info:
        answer = "\n\n".join(found_info)
        await update.message.reply_text(answer)
        save_memory(user_id,"bot",answer)
        return



    # pdf search
    for row in pdf_data:
        keys = [clean_text(k) for k in str(row.get("keyword", "")).split(",")]
        all_keys.extend(keys)
        for kw in keys:
            if kw and (kw in msg or msg in kw):
                found_pdf.append(get_drive_download_link(row["file_url"]))
                break

    if found_pdf:
        for url in found_pdf:
            await update.message.reply_text("📎 Fetching PDF...")
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url) as resp:
                        file_bytes = BytesIO(await resp.read())
                        await update.message.reply_document(
                            document=file_bytes,
                            filename=get_drive_file_name(url),
                        )
            except Exception as e:
                await update.message.reply_text(f"⚠ Error: {e}")
        return



    # suggestions
    from difflib import get_close_matches
    matches = get_close_matches(msg, list(set(all_keys)), n=3, cutoff=0.55)
    if matches:
        text=f"Did you mean:\n• " + "\n• ".join(matches)
        await update.message.reply_text(text)
        save_memory(user_id,"bot",text)
        return



    # =======================================================
    # GEMINI FALLBACK WITH CHAT MEMORY + AUTO-SUMMARY
    # =======================================================
    try:
        model = genai.GenerativeModel(MODEL_NAME)

        # build context
        context = ""
        if user_id in USER_SUMMARY:
            context += "Summary:\n" + USER_SUMMARY[user_id] + "\n\n"

        for x in USER_MEMORY.get(user_id, []):
            context += f"{x['role']}: {x['text']}\n"

        prompt = (
            "Continue conversation based on context. Short and formal.\n\n"
            + context + f"\nUser: {text_raw}"
        )

        resp = await asyncio.to_thread(model.generate_content, prompt)
        answer = resp.text.strip()

        save_memory(user_id,"bot",answer)

        # compress
        await auto_summarize(user_id, model)

        await update.message.reply_text(answer)

    except:
        text="I'm here for data science content, please try again."
        await update.message.reply_text(text)
        save_memory(user_id,"bot",text)



# =============================
# PTB
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
        print("PTB bot started")
        loop.run_forever()

    threading.Thread(target=runner, daemon=True).start()



# =============================
# FLASK
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



if __name__ == "__main__":
    start_ptb_thread()
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
