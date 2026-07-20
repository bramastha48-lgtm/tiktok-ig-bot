"""
Telegram Bot: Universal Video Downloader (No Watermark)
Support: TikTok, Instagram, YouTube Shorts, Twitter/X, Pinterest

Cara pakai:
  1. pip install -r requirements.txt
  2. Set BOT_TOKEN di environment variable
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
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DOWNLOAD_DIR = Path(tempfile.mkdtemp(prefix="vidl_"))
MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram limit 50MB

# ===== URL PATTERNS =====
PATTERNS = {
    'tiktok': re.compile(r'https?://(?:www\.)?(?:tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com|t\.tiktok\.com)/[^\s]+', re.I),
    'instagram': re.compile(r'https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:reel|p|tv|stories)/[^\s]+', re.I),
    'youtube': re.compile(r'https?://(?:www\.)?(?:youtube\.com/shorts/|youtu\.be/|youtube\.com/watch\?v=)[^\s]+', re.I),
    'twitter': re.compile(r'https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+[^\s]*', re.I),
    'pinterest': re.compile(r'https?://(?:www\.)?(?:pinterest\.com|pin\.it)/[^\s]+', re.I),
}


def detect_url(text: str) -> tuple:
    """Deteksi platform dan URL dari text"""
    for platform, pattern in PATTERNS.items():
        match = pattern.search(text)
        if match:
            return (platform, match.group(0))
    return (None, None)


# ===== DOWNLOAD FUNCTIONS =====

async def download_tiktok(url: str, audio_only: bool = False) -> dict:
    """Download TikTok video/audio tanpa watermark"""
    try:
        # Method 1: tikwm.com API
        api_url = "https://www.tikwm.com/api/"
        params = {"url": url, "hd": 1}

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(api_url, params=params)
            data = resp.json()

        if data.get("code") == 0 and data.get("data"):
            video_data = data["data"]

            if audio_only:
                # Download video lalu extract audio
                video_url = video_data.get("hdplay") or video_data.get("play")
                if video_url:
                    if not video_url.startswith("http"):
                        video_url = "https://www.tikwm.com" + video_url

                    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                        vresp = await client.get(video_url)
                        if vresp.status_code == 200:
                            # Save video sementara
                            tmp_video = DOWNLOAD_DIR / f"tmp_{hash(url) & 0xFFFFFFFF:08x}.mp4"
                            tmp_video.write_bytes(vresp.content)

                            # Convert ke MP3
                            mp3_path = DOWNLOAD_DIR / f"tiktok_audio_{hash(url) & 0xFFFFFFFF:08x}.mp3"
                            success = await convert_to_mp3(str(tmp_video), str(mp3_path))
                            tmp_video.unlink(missing_ok=True)

                            if success and mp3_path.exists():
                                return {
                                    "success": True, "path": str(mp3_path), "type": "audio",
                                    "title": video_data.get("title", "TikTok Audio")[:100],
                                    "author": video_data.get("author", {}).get("unique_id", "unknown"),
                                    "size": mp3_path.stat().st_size
                                }

            else:
                # Download video
                video_url = video_data.get("hdplay") or video_data.get("play")
                if video_url:
                    if not video_url.startswith("http"):
                        video_url = "https://www.tikwm.com" + video_url

                    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                        vresp = await client.get(video_url)
                        if vresp.status_code == 200:
                            file_path = DOWNLOAD_DIR / f"tiktok_{hash(url) & 0xFFFFFFFF:08x}.mp4"
                            file_path.write_bytes(vresp.content)
                            return {
                                "success": True, "path": str(file_path), "type": "video",
                                "title": video_data.get("title", "TikTok Video")[:100],
                                "author": video_data.get("author", {}).get("unique_id", "unknown"),
                                "size": len(vresp.content)
                            }

        # Fallback: yt-dlp
        return await download_with_ytdlp(url, "tiktok", audio_only)

    except Exception as e:
        logger.error(f"TikTok error: {e}")
        return await download_with_ytdlp(url, "tiktok", audio_only)


async def download_instagram(url: str) -> dict:
    """Download Instagram (video + carousel photos)"""
    try:
        # yt-dlp handles Instagram well
        result = await download_with_ytdlp(url, "instagram", False)

        # Jika gagal, coba tanpa login
        if not result["success"]:
            ydl_opts = {
                'format': 'best[ext=mp4]/best',
                'outtmpl': str(DOWNLOAD_DIR / 'ig_%(id)s.%(ext)s'),
                'quiet': True,
                'no_warnings': True,
                'socket_timeout': 30,
                'retries': 3,
            }
            result = await download_with_ytdlp_opts(url, ydl_opts, "instagram")

        return result
    except Exception as e:
        logger.error(f"Instagram error: {e}")
        return {"success": False, "error": str(e)[:200]}


async def download_youtube(url: str) -> dict:
    """Download YouTube Shorts"""
    return await download_with_ytdlp(url, "youtube", False)


async def download_twitter(url: str) -> dict:
    """Download Twitter/X video"""
    return await download_with_ytdlp(url, "twitter", False)


async def download_pinterest(url: str) -> dict:
    """Download Pinterest video/foto"""
    return await download_with_ytdlp(url, "pinterest", False)


async def convert_to_mp3(input_path: str, output_path: str) -> bool:
    """Convert video ke MP3 menggunakan ffmpeg via yt-dlp"""
    try:
        opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'outtmpl': output_path.replace('.mp3', ''),
            'quiet': True,
        }
        # Jika file sudah ada, langsung convert
        import subprocess
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-i', input_path, '-vn', '-ab', '192k',
            '-ar', '44100', '-y', output_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
        return Path(output_path).exists()
    except Exception as e:
        logger.error(f"Convert error: {e}")
        return False


async def download_with_ytdlp(url: str, platform: str, audio_only: bool) -> dict:
    """Download menggunakan yt-dlp"""
    ext = "mp3" if audio_only else "mp4"
    file_path = DOWNLOAD_DIR / f"{platform}_{hash(url) & 0xFFFFFFFF:08x}.{ext}"

    ydl_opts = {
        'format': 'bestaudio/best' if audio_only else 'best[ext=mp4]/best',
        'outtmpl': str(file_path),
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
        'retries': 3,
        'merge_output_format': 'mp4',
    }

    if audio_only:
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]

    if platform == "tiktok":
        ydl_opts['http_headers'] = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }

    return await download_with_ytdlp_opts(url, ydl_opts, platform)


async def download_with_ytdlp_opts(url: str, opts: dict, platform: str) -> dict:
    """Run yt-dlp dengan options"""
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _run_ytdlp, url, opts)

        # Cari file yang di-download
        for f in DOWNLOAD_DIR.glob(f"{platform}_*"):
            if f.exists() and f.stat().st_size > 0:
                file_type = "audio" if f.suffix == ".mp3" else "video"
                return {
                    "success": True, "path": str(f), "type": file_type,
                    "title": info.get("title", f"{platform.title()} Media")[:100],
                    "author": info.get("uploader", "unknown"),
                    "size": f.stat().st_size
                }

        return {"success": False, "error": "File tidak ditemukan setelah download"}
    except Exception as e:
        logger.error(f"yt-dlp error ({platform}): {e}")
        return {"success": False, "error": str(e)[:200]}


def _run_ytdlp(url: str, opts: dict) -> dict:
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


# ===== TELEGRAM HANDLERS =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 *Universal Video Downloader Bot*\n\n"
        "Kirim link, saya download tanpa watermark!\n\n"
        "✅ TikTok (video & audio)\n"
        "✅ Instagram (reel & carousel)\n"
        "✅ YouTube Shorts\n"
        "✅ Twitter/X video\n"
        "✅ Pinterest video/foto\n\n"
        "📌 *Commands:*\n"
        "• `/audio <link>` — download audio TikTok (MP3)\n"
        "• `/help` — bantuan",
        parse_mode='Markdown'
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Cara Pakai:*\n\n"
        "1️⃣ Copy link video\n"
        "2️⃣ Kirim ke bot\n"
        "3️⃣ Tunggu, media dikirim!\n\n"
        "🎵 *Download Audio TikTok:*\n"
        "`/audio https://vm.tiktok.com/xxx`\n\n"
        "🔗 *Link yang didukung:*\n"
        "• TikTok: `tiktok.com/...` atau `vm.tiktok.com/...`\n"
        "• Instagram: `instagram.com/reel/...` atau `/p/...`\n"
        "• YouTube: `youtube.com/shorts/...` atau `youtu.be/...`\n"
        "• Twitter/X: `x.com/user/status/...`\n"
        "• Pinterest: `pinterest.com/...` atau `pin.it/...`",
        parse_mode='Markdown'
    )


async def audio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk /audio — download audio TikTok"""
    if not context.args:
        await update.message.reply_text("🎵 Gunakan: `/audio <link TikTok>`", parse_mode='Markdown')
        return

    url = context.args[0]
    if 'tiktok' not in url and 'vm.tiktok' not in url:
        await update.message.reply_text("❌ Hanya support link TikTok untuk audio.")
        return

    status_msg = await update.message.reply_text("🎵 Sedang extract audio...")

    result = await download_tiktok(url, audio_only=True)

    if result["success"]:
        file_path = Path(result["path"])
        caption = f"🎵 *{result['title']}*\n👤 @{result['author']}"

        with open(file_path, 'rb') as audio:
            await update.message.reply_audio(
                audio=audio, caption=caption, parse_mode='Markdown',
                title=result["title"], performer=result["author"]
            )

        await status_msg.delete()
        file_path.unlink(missing_ok=True)
    else:
        await status_msg.edit_text(f"❌ Gagal: {result.get('error', 'Unknown')}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler utama — detect link dan download"""
    text = update.message.text or ""
    platform, url = detect_url(text)

    if not platform:
        return

    # Emoji per platform
    emojis = {
        'tiktok': '🎵', 'instagram': '📸', 'youtube': '▶️',
        'twitter': '🐦', 'pinterest': '📌'
    }
    emoji = emojis.get(platform, '🎬')

    status_msg = await update.message.reply_text(f"{emoji} Sedang mendownload dari {platform.title()}...")

    try:
        if platform == 'tiktok':
            result = await download_tiktok(url, audio_only=False)
        elif platform == 'instagram':
            result = await download_instagram(url)
        elif platform == 'youtube':
            result = await download_youtube(url)
        elif platform == 'twitter':
            result = await download_twitter(url)
        elif platform == 'pinterest':
            result = await download_pinterest(url)
        else:
            result = {"success": False, "error": "Platform tidak didukung"}

        if result["success"]:
            file_path = Path(result["path"])
            file_size = result["size"]

            if file_size > MAX_FILE_SIZE:
                await status_msg.edit_text(
                    f"❌ File terlalu besar ({file_size // 1024 // 1024}MB). Limit Telegram 50MB."
                )
                return

            caption = f"{emoji} *{result['title']}*\n👤 @{result['author']}"

            if result.get("type") == "audio":
                with open(file_path, 'rb') as f:
                    await update.message.reply_audio(
                        audio=f, caption=caption, parse_mode='Markdown',
                        title=result["title"], performer=result["author"]
                    )
            else:
                with open(file_path, 'rb') as f:
                    await update.message.reply_video(
                        video=f, caption=caption, parse_mode='Markdown',
                        supports_streaming=True
                    )

            await status_msg.delete()
            file_path.unlink(missing_ok=True)

        else:
            await status_msg.edit_text(
                f"❌ Gagal mendownload.\n\n"
                f"Error: {result.get('error', 'Unknown')[:200]}\n\n"
                f"Pastikan link valid dan konten bersifat publik."
            )

    except Exception as e:
        logger.error(f"Handler error: {e}")
        await status_msg.edit_text(f"❌ Error: {str(e)[:200]}")


def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN belum di-set!")
        print("   export BOT_TOKEN=your_token_here")
        return

    print("🚀 Bot starting...")
    print(f"📁 Download dir: {DOWNLOAD_DIR}")
    print("✅ Support: TikTok, Instagram, YouTube Shorts, Twitter/X, Pinterest")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("audio", audio_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Bot berjalan!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
