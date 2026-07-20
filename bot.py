"""
Telegram Bot: Auto Download TikTok & Instagram Videos (No Watermark)
Cara pakai:
  1. pip install -r requirements.txt
  2. Set BOT_TOKEN di .env atau environment variable
  3. python bot.py
"""

import os
import re
import asyncio
import tempfile
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp
import httpx

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Config
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DOWNLOAD_DIR = Path(tempfile.mkdtemp(prefix="ttdl_"))
MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram limit 50MB

# Regex patterns
TIKTOK_PATTERN = re.compile(
    r'https?://(?:www\.)?(?:tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)/[^\s]+',
    re.IGNORECASE
)
INSTAGRAM_PATTERN = re.compile(
    r'https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:reel|p|tv)/[^\s]+',
    re.IGNORECASE
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /start"""
    await update.message.reply_text(
        "🎬 *Video Downloader Bot*\n\n"
        "Kirim link TikTok atau Instagram, saya akan download video tanpa watermark!\n\n"
        "✅ TikTok (tanpa watermark)\n"
        "✅ Instagram Reels\n\n"
        "Kirim link langsung, atau ketik /help untuk info.",
        parse_mode='Markdown'
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /help"""
    await update.message.reply_text(
        "📖 *Cara Pakai:*\n\n"
        "1️⃣ Copy link video TikTok/Instagram\n"
        "2️⃣ Kirim link ke bot ini\n"
        "3️⃣ Tunggu sebentar, video akan dikirim\n\n"
        "🔗 *Contoh link yang didukung:*\n"
        "• `https://www.tiktok.com/@user/video/123`\n"
        "• `https://vm.tiktok.com/xxxxx`\n"
        "• `https://www.instagram.com/reel/xxxxx`\n"
        "• `https://www.instagram.com/p/xxxxx`\n\n"
        "⚡ Tanpa watermark, kualitas asli!",
        parse_mode='Markdown'
    )


async def download_tiktok(url: str) -> dict:
    """Download video TikTok tanpa watermark menggunakan tikwm.com API"""
    try:
        # Method 1: tikwm.com API (no watermark)
        api_url = "https://www.tikwm.com/api/"
        params = {"url": url, "hd": 1}

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(api_url, params=params)
            data = resp.json()

        if data.get("code") == 0 and data.get("data"):
            video_data = data["data"]
            video_url = video_data.get("hdplay") or video_data.get("play")
            if video_url:
                if not video_url.startswith("http"):
                    video_url = "https://www.tikwm.com" + video_url

                # Download video file
                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                    video_resp = await client.get(video_url)
                    if video_resp.status_code == 200:
                        file_path = DOWNLOAD_DIR / f"tiktok_{hash(url) & 0xFFFFFFFF:08x}.mp4"
                        file_path.write_bytes(video_resp.content)

                        title = video_data.get("title", "TikTok Video")
                        author = video_data.get("author", {}).get("unique_id", "unknown")

                        return {
                            "success": True,
                            "path": str(file_path),
                            "title": title[:100],
                            "author": author,
                            "size": len(video_resp.content)
                        }

        # Method 2: Fallback ke yt-dlp
        return await download_with_ytdlp(url, "tiktok")

    except Exception as e:
        logger.error(f"TikTok download error: {e}")
        return await download_with_ytdlp(url, "tiktok")


async def download_instagram(url: str) -> dict:
    """Download video Instagram Reels"""
    return await download_with_ytdlp(url, "instagram")


async def download_with_ytdlp(url: str, platform: str) -> dict:
    """Download menggunakan yt-dlp sebagai fallback"""
    try:
        file_path = DOWNLOAD_DIR / f"{platform}_{hash(url) & 0xFFFFFFFF:08x}.mp4"

        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': str(file_path),
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
            'retries': 3,
            'merge_output_format': 'mp4',
        }

        if platform == "tiktok":
            ydl_opts['http_headers'] = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _run_ytdlp, url, ydl_opts)

        if file_path.exists():
            size = file_path.stat().st_size
            return {
                "success": True,
                "path": str(file_path),
                "title": info.get("title", f"{platform.title()} Video")[:100],
                "author": info.get("uploader", "unknown"),
                "size": size
            }

        # Check for alternate filename
        for f in DOWNLOAD_DIR.glob(f"{platform}_*"):
            if f.exists() and f.stat().st_size > 0:
                return {
                    "success": True,
                    "path": str(f),
                    "title": info.get("title", f"{platform.title()} Video")[:100],
                    "author": info.get("uploader", "unknown"),
                    "size": f.stat().st_size
                }

        return {"success": False, "error": "File tidak ditemukan setelah download"}

    except Exception as e:
        logger.error(f"yt-dlp error ({platform}): {e}")
        return {"success": False, "error": str(e)[:200]}


def _run_ytdlp(url: str, opts: dict) -> dict:
    """Run yt-dlp di thread terpisah"""
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


def detect_url(text: str) -> tuple:
    """Deteksi URL TikTok atau Instagram dari text"""
    tiktok_match = TIKTOK_PATTERN.search(text)
    if tiktok_match:
        return ("tiktok", tiktok_match.group(0))

    ig_match = INSTAGRAM_PATTERN.search(text)
    if ig_match:
        return ("instagram", ig_match.group(0))

    # Check for any URL that might be TikTok/Instagram
    url_match = re.search(r'https?://[^\s]+', text)
    if url_match:
        url = url_match.group(0).lower()
        if 'tiktok' in url or 'douyin' in url:
            return ("tiktok", url_match.group(0))
        if 'instagram' in url or 'instagr' in url:
            return ("instagram", url_match.group(0))

    return (None, None)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler utama untuk pesan yang mengandung link"""
    text = update.message.text or ""
    platform, url = detect_url(text)

    if not platform:
        return  # Bukan link TikTok/Instagram, ignore

    # Kirim pesan "sedang memproses"
    status_msg = await update.message.reply_text("⏳ Sedang mendownload video...")

    try:
        # Download berdasarkan platform
        if platform == "tiktok":
            result = await download_tiktok(url)
        else:
            result = await download_instagram(url)

        if result["success"]:
            file_path = Path(result["path"])
            file_size = result["size"]

            if file_size > MAX_FILE_SIZE:
                await status_msg.edit_text(
                    f"❌ Video terlalu besar ({file_size // 1024 // 1024}MB). "
                    f"Telegram limit 50MB."
                )
                return

            # Kirim video
            caption = f"🎬 *{result['title']}\n👤 @{result['author']}*"
            
            with open(file_path, 'rb') as video:
                await update.message.reply_video(
                    video=video,
                    caption=caption,
                    parse_mode='Markdown',
                    supports_streaming=True
                )

            await status_msg.delete()

            # Cleanup
            file_path.unlink(missing_ok=True)

        else:
            error = result.get("error", "Unknown error")
            await status_msg.edit_text(
                f"❌ Gagal mendownload video.\n\n"
                f"Error: {error[:200]}\n\n"
                f"Pastikan link valid dan video bersifat publik."
            )

    except Exception as e:
        logger.error(f"Handler error: {e}")
        await status_msg.edit_text(f"❌ Terjadi error: {str(e)[:200]}")


async def cleanup_task():
    """Cleanup file lama setiap 10 menit"""
    while True:
        await asyncio.sleep(600)
        try:
            for f in DOWNLOAD_DIR.glob("*"):
                if f.is_file():
                    age = asyncio.get_event_loop().time() - f.stat().st_mtime
                    if age > 600:  # 10 menit
                        f.unlink(missing_ok=True)
        except Exception:
            pass


def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN belum di-set!")
        print("   Set environment variable BOT_TOKEN atau edit bot.py")
        print("   Contoh: export BOT_TOKEN=123456:ABC-DEF")
        return

    print(f"🚀 Bot starting...")
    print(f"📁 Download dir: {DOWNLOAD_DIR}")

    # Build application
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Run
    print("✅ Bot berjalan! Kirim /start di Telegram.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
