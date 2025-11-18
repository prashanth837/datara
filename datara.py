import os, re, asyncio, requests, aiohttp
from io import BytesIO
import json
from google.oauth2.service_account import Credentials
from urllib.parse import unquote
import gspread
import google.generativeai as genai
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ============================================================
#  DIRECT STRING VARIABLES
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8441717075:AAGmsAqLYQSCT9EjiCxoJniHj4qxqD_lUYo")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyC7zbNfvfvjtjpu8mJexyAY5JO7qO3I9jk")
PDF_SHEET_ID = os.getenv("PDF_SHEET_ID", "1ME1I3OyFS9VYH2qeqHA5Elt9_f0XXNkkmDgyreVLylo")
INFO_SHEET_ID = os.getenv("INFO_SHEET_ID", "1kUvOq9_HqBVk6dlfnDpMV7FJ9GbGSGXtrrC1zB6O5Oc")

LOCAL_JSON_KEY_FILE = "service_account.json"

# ============================================================
#  GOOGLE CREDENTIAL LOADING
# ============================================================
google_json_env = os.getenv("GOOGLE_CREDENTIALS_JSON")

if google_json_env:
    print("🔐 Using Google credentials from Railway environment variable")
    info = json.loads(google_json_env)
else:
    print("🔐 Using local service_account.json file")
    with open(LOCAL_JSON_KEY_FILE, "r") as f:
        info = json.load(f)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly"
]

creds = Credentials.from_service_account_info(info, scopes=SCOPES)
client = gspread.authorize(creds)

pdf_sheet = client.open_by_key(PDF_SHEET_ID).sheet1
info_sheet = client.open_by_key(INFO_SHEET_ID).sheet1

# ============================================================
#  GEMINI SETUP
# ============================================================
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

# ============================================================
#  HELPERS
# ============================================================
def clean_text(t: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\s]", " ", t).lower().strip()

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

async def ai_tone(text: str) -> str:
    try:
        prompt = "Rewrite this answer in one polite, clear sentence:\n\n" + text
        resp = await asyncio.to_thread(model.generate_content, prompt)
        return resp.text.strip()
    except:
        return text.strip()

# ============================================================
#  CASUAL CHAT
# ============================================================
CASUAL_REPLIES = {
    "hi": "👋 Hey there!",
    "hello": "Hello! 😊 How can I help you today?",
    "hey": "Hey! 👋",
    "bye": "Goodbye! Have a great day!",
    "thanks": "You're welcome 😊",
    "thank you": "Glad to help 🤝",
    "who are you": "I’m Datara Bot 🤖 — your AITS helper!"
}

# ============================================================
#  /start
# ============================================================
async def start(update: Update, _):
    await update.message.reply_text(
        "👋 Hi! I’m Datara Bot, your AITS assistant.\nHow can I help you today?"
    )

# ============================================================
#  MAIN MESSAGE HANDLER
# ============================================================
async def handle(update: Update, ctx):
    raw_msg = update.message.text or ""
    msg = clean_text(raw_msg)
    word_count = len(msg.split())

    # Casual replies
    if msg in CASUAL_REPLIES:
        await update.message.reply_text(CASUAL_REPLIES[msg])
        return

    pdf_data = pdf_sheet.get_all_records()
    info_data = info_sheet.get_all_records()

    found_info = []
    found_pdf = []
    all_pdf_keywords = []

    # 1) INFO DIRECT MATCH
    for row in info_data:
        keywords = [clean_text(k) for k in str(row.get("keywords", "")).split(",")]
        answer = row.get("answer") or row.get("info") or ""
        for kw in keywords:
            if kw in msg or msg in kw:
                found_info.append((kw, answer))
                break

    if found_info:
        txt = "\n\n".join([f"📘 {kw.title()} — {ans}" for kw, ans in found_info])
        final = await ai_tone(txt)
        await update.message.reply_text(final)
        return

    # 2) PDF DIRECT MATCH
    for row in pdf_data:
        keywords = [clean_text(k) for k in str(row.get("keyword", "")).split(",")]
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

                        await update.message.reply_document(
                            file_bytes,
                            filename=get_drive_file_name(link)
                        )
            except Exception as e:
                await update.message.reply_text(f"⚠ it will take some more mins !!")

        return

    # 3) SUGGEST SIMILAR KEYWORDS (only for short messages)
    if word_count <= 3:
        from difflib import get_close_matches
        matches = get_close_matches(msg, all_pdf_keywords, n=4, cutoff=0.45)

        if matches:
            sug = "Did you mean:\n• " + "\n• ".join(matches)
            await update.message.reply_text(sug)
            return

    # 4) GEMINI FALLBACK
    try:
        prompt = f"Answer clearly as a college assistant:\n\n{raw_msg}"
        resp = await asyncio.to_thread(model.generate_content, prompt)
        ai_answer = await ai_tone(resp.text)
        await update.message.reply_text(ai_answer)
    except:
        await update.message.reply_text("I'm here to help but couldn't generate a response.")

# ============================================================
#  RUN BOT
# ============================================================
if __name__ == "__main__":
    print("🚀 Datara Bot is running...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    app.run_polling()
