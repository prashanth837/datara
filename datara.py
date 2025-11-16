import os, re, asyncio, requests
from io import BytesIO
import json
from google.oauth2.service_account import Credentials
from urllib.parse import unquote
import gspread
import google.generativeai as genai
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

# ============================================================
#  DIRECT STRING VARIABLES (LOCAL HARD-CODED VALUES)
#  → Replace values inside "" ONLY
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8441717075:AAGmsAqLYQSCT9EjiCxoJniHj4qxqD_lUYo")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyC7zbNfvfvjtjpu8mJexyAY5JO7qO3I9jk")
PDF_SHEET_ID = os.getenv("PDF_SHEET_ID", "1ME1I3OyFS9VYH2qeqHA5Elt9_f0XXNkkmDgyreVLylo")
INFO_SHEET_ID = os.getenv("INFO_SHEET_ID", "1kUvOq9_HqBVk6dlfnDpMV7FJ9GbGSGXtrrC1zB6O5Oc")

# Local JSON file name (must be in project folder)
LOCAL_JSON_KEY_FILE = "service_account.json"


# ============================================================
#  GOOGLE CREDENTIAL LOADING (LOCAL + RAILWAY AUTO SWITCH)
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
#  HELPER FUNCTIONS
# ============================================================
def clean_text(t: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\s]", " ", t).lower().strip()


def get_drive_file_name(file_url: str) -> str:
    """Extract real file name from Google Drive download headers."""
    try:
        response = requests.head(file_url, allow_redirects=True, timeout=10)
        cd = response.headers.get("content-disposition", "")
        if "filename=" in cd:
            filename = cd.split("filename=")[1].strip('"')
            return unquote(filename)
    except:
        pass
    return "file_from_drive.pdf"


def get_drive_download_link(url: str) -> str:
    """Convert any Google Drive link into direct download link."""
    if "drive.google.com/file/d/" in url:
        fid = url.split("/d/")[1].split("/")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"

    if "drive.google.com/open?id=" in url:
        fid = url.split("id=")[1].split("&")[0]
        return f"https://drive.google.com/uc?export=download&id={fid}"

    return url


async def ai_tone(text: str) -> str:
    """Make Gemini rewrite the answer smoothly."""
    try:
        prompt = (
            "Rewrite this answer in one short, clear, polite sentence for a college assistant bot.\n"
            "Do NOT add greetings or questions.\n\n"
            f"{text}"
        )
        temp = genai.GenerativeModel("gemini-2.0-flash")
        resp = await asyncio.to_thread(temp.generate_content, prompt)
        return resp.text.strip()
    except:
        return text.strip()


# ============================================================
#  CASUAL REPLIES
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
#  TELEGRAM COMMANDS
# ============================================================
async def start(update: Update, _):
    await update.message.reply_text(
        "👋 Hi! I’m Datara Bot, your AITS assistant.\nHow can I help you today?",
        parse_mode="Markdown"
    )


# ============================================================
#  MAIN MESSAGE HANDLER
# ============================================================
async def handle(update: Update, ctx):
    raw_msg = update.message.text or ""
    msg = clean_text(raw_msg)

    # Casual chat replies
    if msg in CASUAL_REPLIES:
        await update.message.reply_text(CASUAL_REPLIES[msg])
        return

    # Spreadsheet data
    pdf_data = pdf_sheet.get_all_records()
    info_data = info_sheet.get_all_records()

    found_info = []
    found_pdf = []
    all_pdf_keywords = []

    # Info search
    for row in info_data:
        keywords = [clean_text(k) for k in str(row.get("keywords", "")).split(",") if k.strip()]
        answer = row.get("answer") or row.get("info") or ""
        for kw in keywords:
            if kw in msg or msg in kw:
                found_info.append((kw, answer))
                break

    # PDF search
    for row in pdf_data:
        keywords = [clean_text(k) for k in str(row.get("keyword", "")).split(",") if k.strip()]
        all_pdf_keywords += keywords
        for kw in keywords:
            if kw in msg or msg in kw:
                file_url = get_drive_download_link(row.get("file_url", ""))
                found_pdf.append((kw, file_url))
                break

    # --- Send Information ---
    if found_info:
        combined = "\n\n".join([f"📘 {kw.title()} — {ans}" for kw, ans in found_info])
        final = await ai_tone(combined)
        await update.message.reply_text(final, parse_mode="Markdown")

    # --- Send PDFs with REAL file name ---
    if found_pdf:
        for kw, link in found_pdf:
            await update.message.reply_text(f"📎 Fetching {kw.title()} PDF...", parse_mode="Markdown")
            try:
                response = requests.get(link, timeout=30)
                response.raise_for_status()

                file_data = BytesIO(response.content)
                filename = get_drive_file_name(link)

                await update.message.reply_document(
                    document=file_data,
                    filename=filename
                )

            except Exception as e:
                await update.message.reply_text(f"⚠ Error downloading PDF: {e}")

    # Suggest similar PDFs
    if not found_pdf:
        from difflib import get_close_matches
        matches = get_close_matches(msg, all_pdf_keywords, n=4, cutoff=0.45)
        if matches:
            await update.message.reply_text(
                "Did you mean: " + ", ".join([f"{m}" for m in matches]),
                parse_mode="Markdown"
            )

    # Fallback general message
    if not found_info and not found_pdf:
        await update.message.reply_text("Sorry, I couldn't find anything related. Try another keyword!")


# ============================================================
#  RUN BOT
# ============================================================
# ============================================================
#  RUN BOT
# ============================================================
if __name__ == "__main__":
    print("🚀 Datara Bot is running...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    app.run_polling()   # ✅ correct polling method
