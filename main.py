import os
import asyncio
import logging
import secrets
import tempfile
import shutil
from pathlib import Path

import yt_dlp
import imageio_ffmpeg

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN")
HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME")
PORT = int(os.getenv("PORT", "10000"))

WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "telegram")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", secrets.token_urlsafe(32))

COOKIE_FILE = "/etc/secrets/youtube_cookies.txt"
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
MAX_FILE_SIZE = 49 * 1024 * 1024

# Optional: route yt-dlp through a proxy (e.g. a residential proxy), set on Render as PROXY_URL
PROXY_URL = os.getenv("PROXY_URL")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def find_deno():
    """Find Deno (needed by yt-dlp for YouTube). Returns a path or None."""
    found = shutil.which("deno")
    if found:
        return found
    candidate = Path.home() / ".deno" / "bin" / "deno"
    if candidate.is_file():
        return str(candidate)
    return None


DENO_PATH = find_deno() or "/opt/render/project/src/.deno/bin/deno"


def base_options(folder):
    """Options shared by info extraction and download (no duplicate keys)."""
    options = {
        "noplaylist": True,
        "socket_timeout": 45,
        "retries": 3,
        "quiet": False,
        "no_warnings": False,
        "verbose": os.getenv("YTDLP_VERBOSE", "1") == "1",
        "ffmpeg_location": FFMPEG_PATH,
    }

    # Only pass js_runtimes if Deno was really found; otherwise let yt-dlp auto-detect (deno/node on PATH)
    if DENO_PATH:
        options["js_runtimes"] = {"deno": {"path": DENO_PATH}}

    if PROXY_URL:
        options["proxy"] = PROXY_URL

    load_cookies(options, folder)
    return options


def load_cookies(options, folder):
    if not os.path.isfile(COOKIE_FILE):
        logger.warning("YouTube cookies NOT FOUND: %s", COOKIE_FILE)
        return

    temp_cookie = os.path.join(folder, "cookies.txt")
    shutil.copyfile(COOKIE_FILE, temp_cookie)

    cookie_size = os.path.getsize(temp_cookie)
    if cookie_size == 0:
        logger.error("YouTube cookie file is EMPTY")
        return

    options["cookiefile"] = temp_cookie
    logger.info("YouTube cookies loaded. Size: %s bytes", cookie_size)


def friendly_error(error):
    """Turn raw yt-dlp errors into a clear message for the user."""
    text = str(error)
    if "Sign in to confirm" in text or "not a bot" in text:
        return (
            "یوتیوب درخواست این سرور را به‌عنوان ربات رد کرد.\n"
            "این مشکل معمولاً از IP سرور (دیتاسنتر) است، نه از لینک یا کد.\n\n"
            "جزئیات فنی:\n" + text[:500]
        )
    if "Requested format is not available" in text:
        return "این کیفیت برای این ویدیو در دسترس نیست. کیفیت دیگری را امتحان کن.\n\n" + text[:400]
    if "File is larger than max-filesize" in text or "max-filesize" in text:
        return "حجم فایل از ۴۹ مگابایت بیشتر است. کیفیت پایین‌تر یا MP3 را امتحان کن."
    return text[:1000]


# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! 🚀\n\n"
        "به Trendify Media Downloader خوش آمدی.\n\n"
        "لینک ویدیو را ارسال کن:\n"
        "YouTube | Instagram | TikTok | Facebook | X\n\n"
        "سایر سایت‌های پشتیبانی‌شده توسط yt-dlp نیز قابل بررسی هستند."
    )


# ================= DOWNLOAD INFO =================

def extract_info(url):
    with tempfile.TemporaryDirectory() as temp_dir:
        options = base_options(temp_dir)
        # NOTE: "ignore_no_formats_error" was removed on purpose. With it, a blocked YouTube request
        # returned an empty format list and hid the real error ("Sign in to confirm you're not a bot").

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)

            if not info:
                raise Exception("YouTube returned no video information")

            formats = info.get("formats") or []
            logger.info(
                "Extraction: title=%s | formats=%s",
                info.get("title"),
                len(formats),
            )

            if not formats:
                raise Exception("هیچ فرمت قابل دانلودی پیدا نشد (احتمالاً بلاک یا محدودیت یوتیوب).")

            for fmt in formats:
                logger.info(
                    "FORMAT: id=%s | ext=%s | height=%s | audio=%s | video=%s",
                    fmt.get("format_id"),
                    fmt.get("ext"),
                    fmt.get("height"),
                    fmt.get("acodec"),
                    fmt.get("vcodec"),
                )

            return info


# ================= MESSAGE HANDLER =================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    url = update.message.text.strip()

    if not url.startswith(("https://", "http://")):
        await update.message.reply_text("❌ لطفاً لینک معتبر ارسال کن.")
        return

    msg = await update.message.reply_text("⏳ در حال بررسی لینک...")

    try:
        info = await asyncio.to_thread(extract_info, url)

        title = info.get("title") or "Media"
        context.user_data["url"] = url

        keyboard = [
            [
                InlineKeyboardButton("1080p", callback_data="1080"),
                InlineKeyboardButton("720p", callback_data="720"),
                InlineKeyboardButton("480p", callback_data="480"),
            ],
            [InlineKeyboardButton("🎵 دانلود MP3", callback_data="mp3")],
        ]

        await msg.edit_text(
            f"📌 {title}\n\nکیفیت مورد نظر را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.exception("Media extraction failed")
        await msg.edit_text(f"❌ خطای استخراج:\n\n{friendly_error(e)}")


# ================= DOWNLOAD ENGINE =================

def download_media(url, quality, folder):
    output = str(Path(folder) / "media.%(ext)s")
    options = base_options(folder)

    if quality == "mp3":
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        # FIX: the old format "best[height<=Q]/best" only selects pre-merged files, and on YouTube those
        # are at most 360p (or missing). We need video+audio streams merged by ffmpeg.
        q = quality
        options["format"] = (
            f"bestvideo[height<={q}][ext=mp4]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={q}]+bestaudio/"
            f"best[height<={q}]/best"
        )
        options["merge_output_format"] = "mp4"

    options.update(
        {
            "outtmpl": output,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "max_filesize": MAX_FILE_SIZE,
            "overwrites": True,
        }
    )

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])

    files = [
        p
        for p in Path(folder).glob("media.*")
        if p.is_file() and not p.name.endswith((".part", ".ytdl"))
    ]

    if not files:
        raise Exception("فایل خروجی ساخته نشد (احتمالاً حجم فایل از ۴۹ مگابایت بیشتر است).")

    result = max(files, key=lambda p: p.stat().st_mtime)

    if result.stat().st_size > MAX_FILE_SIZE:
        raise Exception("حجم فایل بیشتر از محدودیت تلگرام است.")

    return str(result)


# ================= BUTTON HANDLER =================

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    quality = query.data
    url = context.user_data.get("url")

    if quality not in ("1080", "720", "480", "mp3"):
        return

    if not url:
        await query.edit_message_text("❌ لینک منقضی شده است. دوباره لینک را بفرست.")
        return

    label = "MP3" if quality == "mp3" else quality + "p"
    await query.edit_message_text(f"⏳ در حال دانلود با کیفیت {label}...")

    # FIX: uploading ~49MB needs longer timeouts than python-telegram-bot's defaults
    upload_timeouts = dict(read_timeout=180, write_timeout=180, connect_timeout=30, pool_timeout=30)

    try:
        with tempfile.TemporaryDirectory() as folder:
            filename = await asyncio.to_thread(download_media, url, quality, folder)

            with open(filename, "rb") as media:
                if quality == "mp3":
                    await context.bot.send_audio(
                        chat_id=query.message.chat_id,
                        audio=media,
                        title=Path(filename).stem,
                        **upload_timeouts,
                    )
                elif Path(filename).suffix.lower() == ".mp4":
                    await context.bot.send_video(
                        chat_id=query.message.chat_id,
                        video=media,
                        supports_streaming=True,
                        **upload_timeouts,
                    )
                else:
                    await context.bot.send_document(
                        chat_id=query.message.chat_id,
                        document=media,
                        **upload_timeouts,
                    )

        await query.edit_message_text("✅ دانلود و ارسال با موفقیت انجام شد.")

    except Exception as error:
        logger.exception("Download failed")
        await query.edit_message_text("❌ دانلود ناموفق بود.\n\n" + friendly_error(error)[:700])


# ================= ERROR HANDLER =================

async def error_handler(update, context):
    logger.error("Unhandled exception", exc_info=context.error)


# ================= MAIN =================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")

    if not HOSTNAME:
        raise RuntimeError(
            "RENDER_EXTERNAL_HOSTNAME is missing. Deploy this application as a Render Web Service."
        )

    # Startup diagnostics: these lines tell you what is wrong without guessing
    logger.info("yt-dlp version: %s", yt_dlp.version.__version__)
    logger.info("Deno path: %s", DENO_PATH or "NOT FOUND (YouTube may fail or use limited clients)")
    logger.info("Proxy: %s", "enabled" if PROXY_URL else "none")
    logger.info("Cookies file present: %s", os.path.isfile(COOKIE_FILE))

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)

    webhook_url = f"https://{HOSTNAME}/{WEBHOOK_PATH}"

    logger.info("Starting Trendify Media Downloader")
    logger.info("Webhook URL: %s", webhook_url)

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
