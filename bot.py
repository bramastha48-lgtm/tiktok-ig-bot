"""
Telegram Bot: Universal Video Downloader (No Watermark)
Support: TikTok, Instagram, YouTube Shorts, Twitter/X, Pinterest

Fitur:
  - Auto detect link → download video + kirim info file
  - Tombol "Download Audio" di bawah video
  - /audio universal (semua platform)
  - Progress download real-time
  - Support Instagram photo/carousel
"""

import os
import re
import asyncio
import tempfile
import logging
import time
from pathlib import Path
from functools import partial

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from telegram.error import BadRequest
import yt_dlp
import httpx
import hashlib

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Config
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DOWNLOAD_DIR = Path(tempfile.mkdtemp(prefix="vidl_"))
MAX_FILE_SIZE = 50 * 1024 * 1024  # Telegram limit 50MB
PROGRESS_INTERVAL = 2  # detik antara update progress

# Cache URL untuk callback (Telegram callback_data limit = 64 bytes)
# key = short_hash, value = (platform, url)
_url_cache: dict[str, tuple[str, str]] = {}


def _cache_url(platform: str, url: str) -> str:
    """Simpan URL di cache, return short key untuk callback_data."""
    # Pakai 8 char dari hash — cukup unik untuk bot kecil
    key = hashlib.md5(url.encode()).hexdigest()[:8]
    _url_cache[key] = (platform, url)
    # Bersihkan cache jika terlalu besar (>1000 entry)
    if len(_url_cache) > 1000:
        # Hapus entry tertua (FIFO-ish)
        for old_key in list(_url_cache.keys())[:500]:
            _url_cache.pop(old_key, None)
    return key


def _get_cached_url(key: str):
    """Ambil (platform, url) dari cache berdasarkan short key."""
    return _url_cache.get(key)


# ===== URL PATTERNS =====
PATTERNS = {
    'tiktok': re.compile(
        r'https?://(?:www\.)?(?:tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com|t\.tiktok\.com)/[^\s]+',
        re.I
    ),
    'instagram': re.compile(
        r'https?://(?:www\.)?(?:instagram\.com|instagr\.am)/(?:reel|p|tv|stories)/[^\s]+',
        re.I
    ),
    'youtube': re.compile(
        r'https?://(?:www\.)?(?:youtube\.com/shorts/|youtu\.be/|youtube\.com/watch\?v=)[^\s]+',
        re.I
    ),
    'twitter': re.compile(
        r'https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+[^\s]*',
        re.I
    ),
    'pinterest': re.compile(
        r'https?://(?:www\.)?(?:pinterest\.com|pin\.it)/[^\s]+',
        re.I
    ),
}

# Emoji per platform
EMOJIS = {
    'tiktok': '🎵', 'instagram': '📸', 'youtube': '▶️',
    'twitter': '🐦', 'pinterest': '📌'
}


def detect_url(text: str):
    """Deteksi platform dan URL dari text. Return (platform, url) atau (None, None)."""
    if not text:
        return (None, None)
    for platform, pattern in PATTERNS.items():
        match = pattern.search(text)
        if match:
            return (platform, match.group(0))
    return (None, None)


def safe_caption(emoji: str, title: str, author: str, extra_info: str = "") -> str:
    """Buat caption plain text — aman dari Markdown parse error."""
    clean_title = (title or "Media").strip()[:120]
    clean_author = (author or "unknown").strip()
    caption = f"{emoji} {clean_title}\n👤 @{clean_author}"
    if extra_info:
        caption += f"\n{extra_info}"
    return caption


def format_size(size_bytes: int) -> str:
    """Format bytes ke human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


def format_duration(seconds: float) -> str:
    """Format detik ke MM:SS."""
    if not seconds:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"0:{seconds:02d}"
    else:
        return f"{seconds // 60}:{seconds % 60:02d}"


def build_info_text(info: dict, file_size: int) -> str:
    """Bangun string info file dari metadata yt-dlp."""
    parts = []
    dur = info.get("duration")
    if dur:
        parts.append(f"⏱️ {format_duration(dur)}")
    parts.append(f"📦 {format_size(file_size)}")
    res = info.get("resolution") or info.get("format_note", "")
    if res:
        parts.append(f"🖥️ {res}")
    return " | ".join(parts)


# ===== PROGRESS TRACKER =====

class ProgressTracker:
    """Track download progress dan update Telegram message."""

    def __init__(self, bot, chat_id: int, status_msg_id: int, emoji: str, platform: str):
        self.bot = bot
        self.chat_id = chat_id
        self.status_msg_id = status_msg_id
        self.emoji = emoji
        self.platform = platform
        self.last_update = 0.0
        self._loop = asyncio.get_event_loop()

    def hook(self, d: dict):
        """yt-dlp progress hook — dipanggil dari thread, jadi pakai run_coroutine_threadsafe."""
        if d['status'] != 'downloading':
            return

        now = time.time()
        if now - self.last_update < PROGRESS_INTERVAL:
            return
        self.last_update = now

        percent_str = d.get('_percent_str', '0%').strip()
        speed = d.get('_speed_str', '').strip()
        eta = d.get('_eta_str', '').strip()

        text = f"{self.emoji} Mendownload dari {self.platform.title()}...\n"
        text += f"📊 {percent_str}"
        if speed:
            text += f" | {speed}"
        if eta:
            text += f" | ETA {eta}"

        asyncio.run_coroutine_threadsafe(
            self._safe_edit(text), self._loop
        )

    async def _safe_edit(self, text: str):
        """Edit message, ignore error jika gagal."""
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.status_msg_id,
                text=text
            )
        except (BadRequest, Exception):
            pass  # rate limit atau message not modified, abaikan


# ===== DOWNLOAD FUNCTIONS =====

async def download_tiktok(url: str, audio_only: bool = False, progress_hook=None) -> dict:
    """Download TikTok video/audio/photo tanpa watermark."""
    try:
        api_url = "https://www.tikwm.com/api/"
        params = {"url": url, "hd": 1}

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(api_url, params=params)
            data = resp.json()

        if data.get("code") == 0 and data.get("data"):
            video_data = data["data"]
            author = video_data.get("author", {}).get("unique_id", "unknown")
            title = video_data.get("title", "TikTok")[:120]
            duration = video_data.get("duration")

            # === PHOTO POST (slideshow) ===
            images = video_data.get("images")
            if images and isinstance(images, list) and len(images) > 0:
                logger.info(f"TikTok photo post detected: {len(images)} images")
                downloaded = []
                for i, img_url in enumerate(images[:10]):  # max 10 foto
                    if not isinstance(img_url, str):
                        continue
                    if not img_url.startswith("http"):
                        img_url = "https://www.tikwm.com" + img_url
                    try:
                        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                            img_resp = await client.get(img_url)
                            if img_resp.status_code == 200:
                                img_path = DOWNLOAD_DIR / f"tiktok_photo_{hash(url) & 0xFFFFFFFF:08x}_{i}.jpg"
                                img_path.write_bytes(img_resp.content)
                                downloaded.append(str(img_path))
                    except Exception as e:
                        logger.error(f"Download image {i} error: {e}")

                if downloaded:
                    result = {
                        "success": True, "type": "photos",
                        "paths": downloaded,
                        "title": title, "author": author,
                        "size": sum(Path(p).stat().st_size for p in downloaded),
                        "duration": duration,
                    }
                    # Audio-only: extract musik dari slideshow
                    if audio_only:
                        audio_url = video_data.get("play") or video_data.get("hdplay")
                        if audio_url:
                            if not audio_url.startswith("http"):
                                audio_url = "https://www.tikwm.com" + audio_url
                            try:
                                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                                    aresp = await client.get(audio_url)
                                    if aresp.status_code == 200:
                                        tmp = DOWNLOAD_DIR / f"tmp_photo_audio_{hash(url) & 0xFFFFFFFF:08x}.mp4"
                                        tmp.write_bytes(aresp.content)
                                        mp3 = DOWNLOAD_DIR / f"tiktok_audio_{hash(url) & 0xFFFFFFFF:08x}.mp3"
                                        ok = await convert_to_mp3(str(tmp), str(mp3))
                                        tmp.unlink(missing_ok=True)
                                        if ok and mp3.exists():
                                            return {
                                                "success": True, "path": str(mp3), "type": "audio",
                                                "title": title, "author": author,
                                                "size": mp3.stat().st_size, "duration": duration,
                                            }
                            except Exception as e:
                                logger.error(f"Photo audio extract error: {e}")
                    return result

            # === VIDEO POST ===
            video_url = video_data.get("hdplay") or video_data.get("play")
            if video_url:
                if not video_url.startswith("http"):
                    video_url = "https://www.tikwm.com" + video_url

                async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                    vresp = await client.get(video_url)
                    if vresp.status_code == 200:
                        content = vresp.content

                        if audio_only:
                            tmp_video = DOWNLOAD_DIR / f"tmp_{hash(url) & 0xFFFFFFFF:08x}.mp4"
                            tmp_video.write_bytes(content)
                            mp3_path = DOWNLOAD_DIR / f"tiktok_audio_{hash(url) & 0xFFFFFFFF:08x}.mp3"
                            success = await convert_to_mp3(str(tmp_video), str(mp3_path))
                            tmp_video.unlink(missing_ok=True)
                            if success and mp3_path.exists():
                                return {
                                    "success": True, "path": str(mp3_path), "type": "audio",
                                    "title": title, "author": author,
                                    "size": mp3_path.stat().st_size, "duration": duration,
                                }
                        else:
                            file_path = DOWNLOAD_DIR / f"tiktok_{hash(url) & 0xFFFFFFFF:08x}.mp4"
                            file_path.write_bytes(content)
                            return {
                                "success": True, "path": str(file_path), "type": "video",
                                "title": title, "author": author,
                                "size": len(content), "duration": duration,
                            }

        # Fallback ke yt-dlp
        return await download_with_ytdlp(url, "tiktok", audio_only, progress_hook)

    except Exception as e:
        logger.error(f"TikTok error: {e}")
        return await download_with_ytdlp(url, "tiktok", audio_only)


async def download_instagram(url: str) -> dict:
    """Download Instagram (video, reel, carousel photos)."""
    try:
        # Coba sebagai video/reel
        result = await download_with_ytdlp(url, "instagram", False)

        # Jika "no video", coba sebagai gambar
        if not result["success"]:
            err = result.get("error", "").lower()
            if "no video" in err or "no media" in err:
                logger.info("Instagram: no video, trying as image...")
                result = await download_instagram_image(url)

        return result
    except Exception as e:
        logger.error(f"Instagram error: {e}")
        return {"success": False, "error": str(e)[:200]}


async def download_instagram_image(url: str) -> dict:
    """Download Instagram photo post sebagai gambar."""
    try:
        ydl_opts = {
            'format': 'best',
            'outtmpl': str(DOWNLOAD_DIR / 'ig_img_%(id)s.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
            'retries': 3,
        }

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _run_ytdlp, url, ydl_opts)

        # Cari file gambar
        for ext in ['*.jpg', '*.jpeg', '*.png', '*.webp']:
            for f in DOWNLOAD_DIR.glob(f"ig_img_{ext}"):
                if f.exists() and f.stat().st_size > 0:
                    return {
                        "success": True, "path": str(f), "type": "photo",
                        "title": info.get("title", "Instagram Photo")[:120],
                        "author": info.get("uploader", "unknown"),
                        "size": f.stat().st_size,
                        "duration": None,
                    }

        # Cek semua file ig_img_*
        for f in DOWNLOAD_DIR.glob("ig_img_*"):
            if f.exists() and f.stat().st_size > 0:
                file_type = "photo" if f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp') else "video"
                return {
                    "success": True, "path": str(f), "type": file_type,
                    "title": info.get("title", "Instagram Media")[:120],
                    "author": info.get("uploader", "unknown"),
                    "size": f.stat().st_size,
                    "duration": info.get("duration"),
                }

        return {"success": False, "error": "Tidak bisa download media dari post ini"}
    except Exception as e:
        logger.error(f"Instagram image error: {e}")
        return {"success": False, "error": str(e)[:200]}


async def download_generic(url: str, platform: str, audio_only: bool = False) -> dict:
    """Download dari platform lain via yt-dlp."""
    return await download_with_ytdlp(url, platform, audio_only)


async def convert_to_mp3(input_path: str, output_path: str) -> bool:
    """Convert video/audio ke MP3 menggunakan ffmpeg."""
    try:
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


async def download_with_ytdlp(
    url: str, platform: str, audio_only: bool,
    progress_hook=None
) -> dict:
    """Download menggunakan yt-dlp."""
    ext = "mp3" if audio_only else "mp4"
    # Pakai unique suffix supaya tidak match file lama
    unique = f"{hash(url) & 0xFFFFFFFF:08x}_{int(time.time())}"
    file_path = DOWNLOAD_DIR / f"{platform}_{unique}.{ext}"

    if audio_only:
        fmt = 'bestaudio/best'
    else:
        # HARUS ada video: pilih format yang punya video stream
        # bestvideo+bestaudio > best[ext=mp4] > best (yang bisa audio-only)
        fmt = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best'

    ydl_opts = {
        'format': fmt,
        'outtmpl': str(file_path),
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
        'retries': 3,
        'merge_output_format': 'mp4',
        'noplaylist': True,
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

    if progress_hook:
        ydl_opts['progress_hooks'] = [progress_hook]

    return await download_with_ytdlp_opts(url, ydl_opts, platform)


async def download_with_ytdlp_opts(url: str, opts: dict, platform: str) -> dict:
    """Run yt-dlp dengan options."""
    try:
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _run_ytdlp, url, opts)

        # Cari file yang didownload — cari paling baru
        target_file = None
        target_mtime = 0
        for f in DOWNLOAD_DIR.glob(f"{platform}_*"):
            if f.exists() and f.stat().st_size > 0:
                mt = f.stat().st_mtime
                if mt > target_mtime:
                    target_mtime = mt
                    target_file = f

        if target_file:
            # Deteksi tipe berdasarkan isi file, bukan cuma ekstensi
            file_type = _detect_file_type(target_file)
            return {
                "success": True, "path": str(target_file), "type": file_type,
                "title": info.get("title", f"{platform.title()} Media")[:120],
                "author": info.get("uploader", "unknown"),
                "size": target_file.stat().st_size,
                "duration": info.get("duration"),
            }

        return {"success": False, "error": "File tidak ditemukan setelah download"}
    except Exception as e:
        logger.error(f"yt-dlp error ({platform}): {e}")
        return {"success": False, "error": str(e)[:200]}


def _run_ytdlp(url: str, opts: dict) -> dict:
    """Blocking yt-dlp call (jalan di thread pool)."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


def _detect_file_type(file_path: Path) -> str:
    """Deteksi apakah file berisi video atau audio-only."""
    suffix = file_path.suffix.lower()
    if suffix == '.mp3':
        return "audio"
    if suffix in ('.jpg', '.jpeg', '.png', '.webp'):
        return "photo"
    # Untuk .mp4/.webm/.mkv, cek apakah ada video stream
    try:
        import subprocess
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_type',
             '-of', 'csv=p=0', str(file_path)],
            capture_output=True, text=True, timeout=5
        )
        if 'video' in result.stdout:
            return "video"
        else:
            return "audio"  # mp4 container tapi isinya audio-only
    except Exception:
        # ffprobe tidak ada? fallback ke ekstensi
        return "video" if suffix in ('.mp4', '.webm', '.mkv', '.avi') else "audio"


# ===== DOWNLOAD ORCHESTRATOR =====

async def do_download(url: str, platform: str, audio_only: bool,
                      bot=None, chat_id: int = None,
                      status_msg_id: int = None) -> dict:
    """
    Orchestrator: download dengan progress tracking.
    Return dict result dari download function.
    """
    emoji = EMOJIS.get(platform, '🎬')

    # Siapkan progress hook jika bot tersedia
    progress_hook = None
    if bot and chat_id and status_msg_id:
        tracker = ProgressTracker(bot, chat_id, status_msg_id, emoji, platform)
        progress_hook = tracker.hook

    if platform == 'tiktok':
        return await download_tiktok(url, audio_only=audio_only, progress_hook=progress_hook)
    elif platform == 'instagram':
        return await download_instagram(url)
    else:
        return await download_with_ytdlp(url, platform, audio_only, progress_hook)


# ===== TELEGRAM HANDLERS =====

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 *Universal Video Downloader Bot*\n\n"
        "Kirim link, saya download tanpa watermark!\n\n"
        "✅ TikTok (video & audio)\n"
        "✅ Instagram (reel, carousel & foto)\n"
        "✅ YouTube Shorts\n"
        "✅ Twitter/X video\n"
        "✅ Pinterest video/foto\n\n"
        "📌 *Commands:*\n"
        "• `/audio <link>` — download audio dari platform manapun\n"
        "• `/help` — bantuan",
        parse_mode='Markdown'
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Cara Pakai:*\n\n"
        "1️⃣ Copy link video/foto\n"
        "2️⃣ Kirim ke bot\n"
        "3️⃣ Tunggu proses, media dikirim!\n\n"
        "🎵 *Download Audio:*\n"
        "`/audio https://vm.tiktok.com/xxx`\n"
        "Bisa dari TikTok, IG, YouTube, Twitter, dll.\n\n"
        "🔗 *Link yang didukung:*\n"
        "• TikTok: `tiktok.com/...` atau `vm.tiktok.com/...`\n"
        "• Instagram: `instagram.com/reel/...` atau `/p/...`\n"
        "• YouTube: `youtube.com/shorts/...` atau `youtu.be/...`\n"
        "• Twitter/X: `x.com/user/status/...`\n"
        "• Pinterest: `pinterest.com/...` atau `pin.it/...`",
        parse_mode='Markdown'
    )


async def audio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler /audio — universal audio download dari platform manapun."""
    if not context.args:
        await update.message.reply_text(
            "🎵 Gunakan: `/audio <link>`\n\n"
            "Support semua platform: TikTok, IG, YouTube, Twitter, Pinterest",
            parse_mode='Markdown'
        )
        return

    input_text = " ".join(context.args)
    platform, url = detect_url(input_text)

    if not platform:
        await update.message.reply_text(
            "❌ Link tidak dikenali.\n"
            "Pastikan link valid dari TikTok, IG, YouTube, Twitter, atau Pinterest."
        )
        return

    emoji = EMOJIS.get(platform, '🎵')
    status_msg = await update.message.reply_text(f"{emoji} Menyiapkan audio dari {platform.title()}...")

    try:
        result = await do_download(
            url, platform, audio_only=True,
            bot=context.bot, chat_id=update.effective_chat.id,
            status_msg_id=status_msg.message_id
        )

        if result["success"]:
            file_path = Path(result["path"])

            # Update progress selesai
            await safe_edit_msg(
                context.bot, update.effective_chat.id,
                status_msg.message_id,
                f"{emoji} Mengirim audio..."
            )

            caption = safe_caption(emoji, result["title"], result["author"],
                                   f"📦 {format_size(result['size'])}")

            with open(file_path, 'rb') as f:
                await update.message.reply_audio(
                    audio=f, caption=caption,
                    title=result.get("title", "Audio"),
                    performer=result.get("author", "unknown")
                )

            await status_msg.delete()
            file_path.unlink(missing_ok=True)
        else:
            await status_msg.edit_text(
                f"❌ Gagal download audio.\n\n"
                f"Error: {result.get('error', 'Unknown')[:200]}\n\n"
                f"Pastikan link valid dan konten bersifat publik."
            )

    except Exception as e:
        logger.error(f"Audio handler error: {e}")
        await status_msg.edit_text(f"❌ Error: {str(e)[:200]}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler utama — detect link dan download video."""
    text = update.message.text or ""
    platform, url = detect_url(text)

    if not platform:
        return

    emoji = EMOJIS.get(platform, '🎬')
    status_msg = await update.message.reply_text(
        f"{emoji} Mendownload dari {platform.title()}..."
    )

    try:
        result = await do_download(
            url, platform, audio_only=False,
            bot=context.bot, chat_id=update.effective_chat.id,
            status_msg_id=status_msg.message_id
        )

        if result["success"]:
            file_size = result["size"]

            # Cek limit Telegram (untuk single file)
            if file_size > MAX_FILE_SIZE and result.get("type") != "photos":
                await status_msg.edit_text(
                    f"❌ File terlalu besar ({format_size(file_size)}). "
                    f"Limit Telegram 50MB."
                )
                return

            # Info text
            info_text = build_info_text(result, file_size)
            caption = safe_caption(emoji, result["title"], result["author"], info_text)

            # Tombol download audio (jika video/photo punya audio)
            keyboard = None
            if result.get("type") in ("video", "photos"):
                cache_key = _cache_url(platform, url)
                cb_data = f"a|{cache_key}"  # short! fits in 64 bytes
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎵 Download Audio", callback_data=cb_data)
                ]])

            # Kirim media
            if result.get("type") == "photos":
                # TikTok photo slideshow — kirim sebagai media group
                paths = result["paths"]
                caption = safe_caption(emoji, result["title"], result["author"], info_text)
                media_group = []
                opened_files = []  # track open files untuk di-close nanti
                for i, p in enumerate(paths):
                    img_path = Path(p)
                    if img_path.exists():
                        f = open(img_path, 'rb')
                        opened_files.append(f)
                        cap = caption if i == 0 else ""
                        media_group.append(InputMediaPhoto(media=f, caption=cap))
                if media_group:
                    await update.message.reply_media_group(media=media_group)
                for f in opened_files:
                    f.close()
                # Cleanup foto
                for p in paths:
                    Path(p).unlink(missing_ok=True)
                # Kirim tombol audio terpisah (reply_media_group tidak support reply_markup)
                if keyboard:
                    await update.message.reply_text(
                        "🎵 Download musik/slideshow audio:",
                        reply_markup=keyboard
                    )

            elif result.get("type") == "photo":
                file_path = Path(result["path"])
                with open(file_path, 'rb') as f:
                    await update.message.reply_photo(
                        photo=f, caption=caption
                    )
                file_path.unlink(missing_ok=True)
            else:
                file_path = Path(result["path"])
                with open(file_path, 'rb') as f:
                    await update.message.reply_video(
                        video=f, caption=caption,
                        supports_streaming=True,
                        reply_markup=keyboard
                    )
                file_path.unlink(missing_ok=True)

            await status_msg.delete()

        else:
            await status_msg.edit_text(
                f"❌ Gagal mendownload.\n\n"
                f"Error: {result.get('error', 'Unknown')[:200]}\n\n"
                f"Pastikan link valid dan konten bersifat publik."
            )

    except Exception as e:
        logger.error(f"Handler error: {e}")
        await status_msg.edit_text(f"❌ Error: {str(e)[:200]}")


async def handle_audio_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler tombol 'Download Audio' dari inline keyboard."""
    query = update.callback_query

    # Parse callback data
    data = query.data or ""
    parts = data.split("|", 1)
    if len(parts) != 2 or parts[0] != "a":
        await query.answer("Data tidak valid.", show_alert=True)
        return

    cached = _get_cached_url(parts[1])
    if not cached:
        await query.answer("Link sudah expired. Kirim ulang link-nya ya.", show_alert=True)
        return

    platform, url = cached

    await query.answer("🎵 Memproses audio...")
    status_msg = await query.message.reply_text(f"🎵 Extract audio dari {platform.title()}...")

    try:
        result = await do_download(
            url, platform, audio_only=True,
            bot=context.bot, chat_id=update.effective_chat.id,
            status_msg_id=status_msg.message_id
        )

        if result["success"]:
            file_path = Path(result["path"])
            emoji = EMOJIS.get(platform, '🎵')
            caption = safe_caption(emoji, result["title"], result["author"],
                                   f"📦 {format_size(result['size'])}")

            await safe_edit_msg(
                context.bot, update.effective_chat.id,
                status_msg.message_id, f"{emoji} Mengirim audio..."
            )

            with open(file_path, 'rb') as f:
                await query.message.reply_audio(
                    audio=f, caption=caption,
                    title=result.get("title", "Audio"),
                    performer=result.get("author", "unknown")
                )

            await status_msg.delete()
            file_path.unlink(missing_ok=True)
        else:
            await status_msg.edit_text(
                f"❌ Gagal extract audio.\n"
                f"Error: {result.get('error', 'Unknown')[:200]}"
            )

    except Exception as e:
        logger.error(f"Audio callback error: {e}")
        await status_msg.edit_text(f"❌ Error: {str(e)[:200]}")


async def safe_edit_msg(bot, chat_id: int, msg_id: int, text: str):
    """Edit message dengan error handling."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=msg_id, text=text
        )
    except (BadRequest, Exception):
        pass


# ===== MAIN =====

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN belum di-set!")
        print("   export BOT_TOKEN=your_token_here")
        return

    print("🚀 Bot starting...")
    print(f"📁 Download dir: {DOWNLOAD_DIR}")
    print("✅ Support: TikTok, Instagram, YouTube Shorts, Twitter/X, Pinterest")
    print("✅ Fitur: Universal audio, Progress bar, Inline audio button")

    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("audio", audio_cmd))

    # Callback handler untuk tombol audio
    app.add_handler(CallbackQueryHandler(handle_audio_callback, pattern=r"^a\|"))

    # Message handler (harus terakhir, paling rendah priority)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Bot berjalan!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
