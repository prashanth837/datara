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

# -------------------- MINI WEB SERVER (Render requirement) --------------------
app_web = Flask(__name__)

@app_web.route("/")
def home():
    return "Datara Bot is running on Render!"

def run_web():
    app_web.run(host="0.0.0.0", port=10000)

# -------------------- ENV VARS --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "8441717075:AAGmsAqLYQSCT9EjiCxoJniHj4qxqD_lUYo")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyC7zbNfvfvjtjpu8mJexyAY5JO7qO3I9jk")
PDF_SHEET_ID = os.getenv("PDF_SHEET_ID", "1ME1I3OyFS9VYH2qeqHA5Elt9_f0XXNkkmDgyreVLylo")
INFO_SHEET_ID = os.getenv("INFO_SHEET_ID", "1kUvOq9_HqBVk6dlfnDpMV7FJ9GbGSGXtrrC1zB6O5Oc")

# -------------------- GOOGLE AUTH --------------------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly"
]

json_str = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")

if json_str:
    print("🔐 Loading Google credentials from ENV (Render)...")
    creds = Credentials.from_service_account_info(json.loads(json_str), scopes=SCOPES)
else:
    print("🔐 Loading Google credentials from local file...")
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

def get_drive_file_name(url):
    try:
        r = requests.head(url, allow_redirects=True)
        cd = r.headers.get("content-disposition", "")
        if "filename=" in cd:
            return unquote(cd.split("filename=")[1].strip('"'))
    except:
        pass
    return "document.pdf"

def get_drive_download_link(url):
    if "drive.google.com/file/d/" in url:
        fid = url.split("/d/")[1].split("/")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"
    if "drive.google.com/open?id=" in url:
        fid = url.split("id=")[1].split("&")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"
    return url

async def ai_tone(text: str) -> str:
    try:
        prompt = "Rewrite this politely in one simple sentence:\n\n" + text
        resp = await asyncio.to_thread(model.generate_content, prompt)
        return resp.text.strip()
    except:
        return text

# -------------------- CASUAL REPLIES --------------------
CASUAL_REPLIES = {
    "hi": "👋 Hey there!",
    "hello": "Hello! 😊",
    "hey": "Hey! 👋",
    "thanks": "You're welcome!",
    "thank you": "Anytime! 🤝",
}

# -------------------- HANDLERS --------------------
async def start(update: Update, _):
    await update.message.reply_text(
        "👋 Hi! I’m *Datara Bot*. Ask me anything!",
        parse_mode="Markdown"
    )

async def handle(update: Update, _):
    msg = clean_text(update.message.text or "")

    if msg in CASUAL_REPLIES:
        await update.message.reply_text(CASUAL_REPLIES[msg])
        return

    pdf_data = pdf_sheet.get_all_records()
    info_data = info_sheet.get_all_records()

    found_info = []
    found_pdf = []

    for row in info_data:
        keys = [clean_text(k) for k in str(row.get("keywords", "")).split(",")]
        ans = row.get("answer") or row.get("information") or ""
        if any(k in msg or msg in k for k in keys):
            found_info.append((keys[0], ans))

    for row in pdf_data:
        keys = [clean_text(k) for k in str(row.get("keyword", "")).split(",")]
        if any(k in msg or msg in k for k in keys):
            found_pdf.append((keys[0], get_drive_download_link(row.get("file_url", ""))))

    if found_info:
        text = "\n\n".join([f"📘 *{k.title()}*: {a}" for k, a in found_info])
        await update.message.reply_text(await ai_tone(text), parse_mode="Markdown")

    if found_pdf:
        for k, link in found_pdf:
            try:
                await update.message.reply_text(f"📎 Fetching {k.title()} PDF...")
                r = requests.get(link)
                f = BytesIO(r.content)
                await update.message.reply_document(f, filename=get_drive_file_name(link))
            except Exception as e:
                await update.message.reply_text(f"⚠️ Error: {e}")

    if not found_info and not found_pdf:
        await update.message.reply_text("I’m here, but I didn’t understand 😊")

# -------------------- RUN (Render Safe) --------------------
# -------------------- RUN (dual-mode for local + Render) --------------------
if __name__ == "__main__":
    print("🚀 Datara Bot starting...")

    # Detect if running on Render
    is_render = os.getenv("RENDER", "0") == "1"

    # Start Flask only on Render
    if is_render:
        threading.Thread(target=run_web, daemon=True).start()

    async def main():
        app = ApplicationBuilder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

        print("🤖 Bot polling started!")
        await app.run_polling()

    if is_render:
        # Render requires loop.run_forever()
        loop = asyncio.get_event_loop()
        loop.create_task(main())
        loop.run_forever()
    else:
        # Local machine supports normal asyncio.run()
        asyncio.run(main())

