import os
import re
import json
import threading
import asyncio
from io import BytesIO
from urllib.parse import unquote

import aiohttp
import requests
from flask import Flask, request

# GOOGLE
from google.oauth2.service_account import Credentials
import gspread

# GEMINI
import google.generativeai as genai

# TELEGRAM
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# RAG (FAISS)
from sentence_transformers import SentenceTransformer
import faiss
import numpy as np


# ======================================================
# MEMORY
# ======================================================
USER_MEMORY = {}
USER_SUMMARY = {}
MAX_MESSAGES = 8


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

    prompt = "Summarize in 3 lines:\n" + history
    resp = await asyncio.to_thread(model.generate_content, prompt)

    USER_SUMMARY[user_id] = resp.text.strip()
    USER_MEMORY[user_id] = []


# =============================
# CONFIG
# =============================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

PDF_SHEET_ID = os.getenv("PDF_SHEET_ID")
INFO_SHEET_ID = os.getenv("INFO_SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# =============================
# GOOGLE SHEETS
# =============================
creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
creds = Credentials.from_service_account_info(
    creds_info,
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly"
    ]
)

client = gspread.authorize(creds)

pdf_sheet = client.open_by_key(PDF_SHEET_ID).sheet1
info_sheet = client.open_by_key(INFO_SHEET_ID).sheet1


# =============================
# GEMINI
# =============================
genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = "gemini-1.5-flash"


# =============================
# RAG SETUP (FAISS)
# =============================
embed_model = SentenceTransformer('all-MiniLM-L6-v2')

info_data = info_sheet.get_all_records()

DOCUMENTS = []
for row in info_data:
    text = row.get("answer") or row.get("info") or ""
    if text:
        DOCUMENTS.append(text)

doc_embeddings = embed_model.encode(DOCUMENTS)

dimension = doc_embeddings.shape[1]
faiss_index = faiss.IndexFlatL2(dimension)
faiss_index.add(np.array(doc_embeddings).astype("float32"))

print(f"✅ FAISS ready with {len(DOCUMENTS)} docs")


# =============================
# HELPERS
# =============================
def clean_text(t):
    return re.sub(r"[^a-zA-Z0-9\s]", " ", (t or "")).lower().strip()


def get_drive_download_link(url):
    if "drive.google.com/file/d/" in url:
        fid = url.split("/d/")[1].split("/")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"
    return url


# =============================
# HANDLER
# =============================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text_raw = update.message.text or ""
    user_id = update.message.from_user.id

    save_memory(user_id, "user", text_raw)

    # =============================
    # 🔍 RAG SEARCH
    # =============================
    query_embedding = embed_model.encode([text_raw])

    D, I = faiss_index.search(
        np.array(query_embedding).astype("float32"), k=3
    )

    top_score = D[0][0]

    if top_score < 1.2:
        context_text = "\n\n".join([DOCUMENTS[i] for i in I[0]])

        prompt = f"""
        Answer ONLY using this context:

        {context_text}

        Question: {text_raw}
        """

        model = genai.GenerativeModel(MODEL_NAME)
        resp = await asyncio.to_thread(model.generate_content, prompt)

        answer = resp.text.strip()

        await update.message.reply_text(answer)
        save_memory(user_id, "bot", answer)

        await auto_summarize(user_id, model)
        return

    # =============================
    # 📄 PDF SEARCH (UNCHANGED)
    # =============================
    pdf_data = pdf_sheet.get_all_records()

    for row in pdf_data:
        if text_raw.lower() in str(row.get("keyword", "")).lower():
            url = get_drive_download_link(row["file_url"])

            await update.message.reply_text("📎 Fetching PDF...")

            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    file_bytes = BytesIO(await resp.read())
                    await update.message.reply_document(file_bytes)

            return

    # =============================
    # 🤖 FALLBACK (GEMINI)
    # =============================
    model = genai.GenerativeModel(MODEL_NAME)

    resp = await asyncio.to_thread(
        model.generate_content,
        f"Answer shortly:\n{text_raw}"
    )

    answer = resp.text.strip()

    await update.message.reply_text(answer)
    save_memory(user_id, "bot", answer)


# =============================
# TELEGRAM
# =============================
application = Application.builder().token(BOT_TOKEN).build()
application.add_handler(MessageHandler(filters.TEXT, message_handler))


# =============================
# THREAD
# =============================
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
# FLASK (UNCHANGED)
# =============================
app = Flask(__name__)


@app.post("/webhook")
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, Bot(BOT_TOKEN))
    asyncio.run_coroutine_threadsafe(application.update_queue.put(update), app_loop)
    return "OK", 200


@app.get("/")
def home():
    return "Bot Running", 200


if __name__ == "__main__":
    start_ptb_thread()
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)