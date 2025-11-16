import os, re, asyncio, requests, threading
from io import BytesIO
import json
import gspread
from urllib.parse import unquote
from google.oauth2.service_account import Credentials
import google.generativeai as genai
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from flask import Flask

# -------------------- MINI WEB SERVER (Required for Render Free) --------------------
app_web = Flask(__name__)

@app_web.route("/")
def home():
    return "Datara Bot is running on Render!"

def run_web():
    app_web.run(host="0.0.0.0", port=10000)


# -------------------- DIRECT STRING VARIABLES --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8441717075:AAGmsAqLYQSCT9EjiCxoJniHj4qxqD_lUYo")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyC7zbNfvfvjtjpu8mJexyAY5JO7qO3I9jk")
PDF_SHEET_ID = os.getenv("PDF_SHEET_ID", "1ME1I3OyFS9VYH2qeqHA5Elt9_f0XXNkkmDgyreVLylo")
INFO_SHEET_ID = os.getenv("INFO_SHEET_ID", "1kUvOq9_HqBVk6dlfnDpMV7FJ9GbGSGXtrrC1zB6O5Oc")

# -------------------- GOOGLE SCOPES --------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly"
]

# -------------------- LOAD GOOGLE CREDENTIALS --------------------
json_str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")

if json_str:
    print("🔐 Loading Google credentials from ENV (Render)...")
    info = json.loads(json_str)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
else:
    print("🔐 Loading Google credentials from local file (Local Development)...")
    creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)

client = gspread.authorize(creds)

pdf_sheet = client.open_by_key(PDF_SHEET_ID).sheet1
info_sheet = client.open_by_key(INFO_SHEET_ID).sheet1

# -------------------- GEMINI --------------------
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")


# -------------------- HELPERS --------------------
def clean_text(t: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\s]", " ", t).lower().strip()


def get_drive_file_name(file_url: str) -> str:
    try:
        response = requests.head(file_url, allow_redirects=True, timeout=10)
        cd = response.headers.get("content-disposition", "")
        if "filename=" in cd:
            filename = cd.split("filename=")[1].strip('"')
            return unquote(filename)
    except:
        pass
    return "document.pdf"


def get_drive_download_link(url: str) -> str:
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
            "Rewrite this in one clear and polite sentence. "
            "No greetings, no questions. If unsure, return same.\n\n"
            f"{text}"
        )
        resp = await asyncio.to_thread(model.generate_content, prompt)
        cleaned = resp.text.strip()

        banned = [
            "i am an ai", "assistant", "need more information",
            "option", "perhaps", "clarify", "could you",
            "the text does not", "i'm not sure", "no context",
            "data science department"
        ]

        if any(b in cleaned.lower() for b in banned) or len(cleaned) < 5:
            return text.strip()

        return cleaned

    except Exception:
        return text.strip()

# -------------------- CASUAL REPLIES --------------------
CASUAL_REPLIES = {
    "hi": "👋 Hey there! How are you doing?",
    "hello": "Hello! 😊 How can I help you today?",
    "hey": "Hey! 👋 What can I do for you?",
    "bye": "Goodbye! 👋 Have a great day!",
    "thanks": "You're welcome! 😊",
    "thank you": "Anytime! 🤝",
    "good morning": "Good morning ☀️!",
    "good night": "Good night 🌙 Sleep well!",
    "who are you": "I’m Datara Bot 🤖 — your helpful assistant!"
}


# -------------------- TELEGRAM BOT --------------------
async def start(update: Update, _):
    await update.message.reply_text(
        "👋 Hi! I’m *Datara Bot*, your assistant.\nAsk me anything!",
        parse_mode="Markdown"
    )


async def handle(update: Update, ctx):
    raw_msg = update.message.text or ""
    msg = clean_text(raw_msg)

    if msg in CASUAL_REPLIES:
        await update.message.reply_text(CASUAL_REPLIES[msg])
        return

    pdf_data = pdf_sheet.get_all_records()
    info_data = info_sheet.get_all_records()

    found_info = []
    found_pdf = []

    # --- Info responses ---
    for row in info_data:
        keywords = [clean_text(k) for k in str(row.get("keywords", "")).split(",") if k.strip()]
        answer = row.get("answer") or row.get("information") or row.get("info") or ""

        for kw in keywords:
            if kw in msg or msg in kw:
                found_info.append((kw, answer))
                break

    # --- PDF responses ---
    for row in pdf_data:
        keywords = [clean_text(k) for k in str(row.get("keyword", "")).split(",") if k.strip()]

        for kw in keywords:
            if kw in msg or msg in kw:
                file_url = get_drive_download_link(row.get("file_url", ""))
                found_pdf.append((kw, file_url))
                break

    # --- Send info ---
    if found_info:
        text = "\n\n".join([f"📘 *{kw.title()}*: {ans}" for kw, ans in found_info])
        final_text = await ai_tone(text)
        await update.message.reply_text(final_text, parse_mode="Markdown")

    # --- Send PDFs ---
    if found_pdf:
        for kw, link in found_pdf:
            try:
                await update.message.reply_text(
                    f"📎 Fetching *{kw.title()}* PDF...", parse_mode="Markdown"
                )
                response = requests.get(link, timeout=30)
                response.raise_for_status()
                file_data = BytesIO(response.content)
                filename = get_drive_file_name(link)
                await update.message.reply_document(document=file_data, filename=filename)
            except Exception as e:
                await update.message.reply_text(f"⚠️ Error downloading PDF for {kw}: {e}")

    # --- If no info or PDF ---
    if not found_info and not found_pdf:
        await update.message.reply_text("I didn’t fully get that, but I’m here to help 😊")


# -------------------- RUN --------------------
# -------------------- RUN --------------------
# -------------------- RUN --------------------
if __name__ == "__main__":
    print("🚀 Datara Bot running on Render!")

    # Start Flask Web Server
    threading.Thread(target=run_web, daemon=True).start()

    async def main():
        print("🤖 Starting Telegram polling...")

        application = ApplicationBuilder().token(BOT_TOKEN).build()

        application.add_handler(CommandHandler("start", start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

        await application.initialize()
        await application.start()

        try:
            await application.updater.start_polling()
            print("✅ Bot is live and polling!")
        except Exception as e:
            print("🔥 ERROR in polling:", e)

        # Keep bot alive forever
        await asyncio.Event().wait()

    try:
        asyncio.run(main())
    except Exception as e:
        print("🔥 MAIN LOOP ERROR:", e)
