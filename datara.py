# =========================================================
# IMPORTS
# =========================================================
import os, re, json, threading, asyncio, time
from io import BytesIO

import aiohttp
from flask import Flask, request

from google.oauth2.service_account import Credentials
import gspread
import google.generativeai as genai

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# RAG
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer


# =========================================================
# CONFIG
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PDF_SHEET_ID = os.getenv("PDF_SHEET_ID")
INFO_SHEET_ID = os.getenv("INFO_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = "models/gemini-2.5-flash"

SIM_THRESHOLD = 1.2  # Prevent wrong matches


# =========================================================
# GOOGLE SHEETS
# =========================================================
creds = Credentials.from_service_account_info(
    json.loads(GOOGLE_CREDENTIALS_JSON),
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly"
    ]
)

client = gspread.authorize(creds)
pdf_sheet = client.open_by_key(PDF_SHEET_ID).sheet1
info_sheet = client.open_by_key(INFO_SHEET_ID).sheet1


# =========================================================
# MEMORY
# =========================================================
USER_MEMORY = {}
USER_SUMMARY = {}
MAX_MESSAGES = 8


def save_memory(uid, role, txt):
    USER_MEMORY.setdefault(uid, []).append({"role": role, "text": txt})


async def auto_summarize(uid, model):
    if len(USER_MEMORY.get(uid, [])) < MAX_MESSAGES:
        return

    history = "\n".join(f"{m['role']}: {m['text']}" for m in USER_MEMORY[uid])
    resp = await asyncio.to_thread(model.generate_content, history)

    USER_SUMMARY[uid] = resp.text
    USER_MEMORY[uid] = []


# =========================================================
# RAG ENGINE
# =========================================================
embed_model = SentenceTransformer("all-MiniLM-L6-v2")
VECTOR_INDEX = None
VECTOR_DOCS = []


def build_vector_index():
    global VECTOR_INDEX, VECTOR_DOCS

    docs = []

    for r in info_sheet.get_all_records():
        docs.append({
            "text": f"{r.get('keywords','')} {r.get('answer','')}",
            "type": "info",
            "answer": r.get("answer","")
        })

    for r in pdf_sheet.get_all_records():
        docs.append({
            "text": r.get("keyword",""),
            "type": "pdf",
            "url": r.get("file_url","")
        })

    texts = [d["text"] for d in docs]
    vecs = embed_model.encode(texts)

    index = faiss.IndexFlatL2(vecs.shape[1])
    index.add(np.array(vecs))

    VECTOR_INDEX = index
    VECTOR_DOCS = docs
    print("✅ RAG index rebuilt:", len(docs))


def auto_refresh():
    while True:
        try:
            build_vector_index()
        except Exception as e:
            print("Refresh error:", e)

        time.sleep(120)  # refresh every 2 min


# =========================================================
# HELPERS
# =========================================================
def clean(t):
    return re.sub(r"[^a-z0-9\s]", " ", (t or "").lower()).strip()


def drive_dl(url):
    if "file/d/" in url:
        fid = url.split("/d/")[1].split("/")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"
    return url


async def ai_tone(text):
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        resp = await asyncio.to_thread(model.generate_content, text)
        return resp.text
    except:
        return text


# =========================================================
# TELEGRAM HANDLERS
# =========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Datara RAG Bot Ready")


async def message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    raw = update.message.text
    msg = clean(raw)

    save_memory(uid, "user", raw)

    # ================= RAG SEARCH =================
    qv = embed_model.encode([msg])
    D, I = VECTOR_INDEX.search(np.array(qv), 1)

    if D[0][0] < SIM_THRESHOLD:
        best = VECTOR_DOCS[I[0][0]]

        # INFO
        if best["type"] == "info":
            ans = await ai_tone(best["answer"])
            await update.message.reply_text(ans)
            save_memory(uid, "bot", ans)
            return

        # PDF
        if best["type"] == "pdf":
            url = drive_dl(best["url"])
            await update.message.reply_text("📄 Sending document...")

            async with aiohttp.ClientSession() as s:
                async with s.get(url) as r:
                    f = BytesIO(await r.read())
                    await update.message.reply_document(document=f)
            return

    # ================= FALLBACK =================
    model = genai.GenerativeModel(MODEL_NAME)

    ctx = USER_SUMMARY.get(uid, "")
    prompt = ctx + f"\nUser: {raw}"

    resp = await asyncio.to_thread(model.generate_content, prompt)
    ans = resp.text

    save_memory(uid, "bot", ans)
    await auto_summarize(uid, model)

    await update.message.reply_text(ans)


# =========================================================
# TELEGRAM APP
# =========================================================
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT, message))


# =========================================================
# WEBHOOK SERVER
# =========================================================
app = Flask(__name__)
loop_ref = None


def start_bot():
    def runner():
        global loop_ref
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop_ref = loop

        build_vector_index()
        threading.Thread(target=auto_refresh, daemon=True).start()

        loop.run_until_complete(application.initialize())
        loop.run_until_complete(application.start())
        loop.run_forever()

    threading.Thread(target=runner, daemon=True).start()


@app.post("/")
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, Bot(BOT_TOKEN))
    asyncio.run_coroutine_threadsafe(
        application.update_queue.put(update),
        loop_ref
    )
    return "ok"


# =========================================================
# START
# =========================================================
if __name__ == "__main__":
    start_bot()
    app.run("0.0.0.0", int(os.getenv("PORT", 10000)))
