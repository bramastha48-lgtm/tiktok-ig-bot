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
import twitter_downloader

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
# Cache slideshow data: key = short_hash, value = dict with paths, audio, metadata
_slideshow_cache: dict[str, dict] = {}


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


def _cache_slideshow(data: dict) -> str:
    """Simpan slideshow data, return short key."""
    key = hashlib.md5(str(time.time()).encode()).hexdigest()[:8]
    _slideshow_cache[key] = data
    # Cleanup lama
    if len(_slideshow_cache) > 100:
        for old_key in list(_slideshow_cache.keys())[:50]:
            old = _slideshow_cache.pop(old_key, None)
            if old:
                _cleanup_slideshow_files(old)
    return key


def _get_slideshow(key: str):
    return _slideshow_cache.get(key)


def _cleanup_slideshow_files(data: dict):
    """Hapus file temporary slideshow."""
    for p in data.get("paths", []):
        Path(p).unlink(missing_ok=True)
    if data.get("audio_path"):
        Path(data["audio_path"]).unlink(missing_ok=True)


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
        r'https?://(?:www\.)?(?:twitter\.com|x\.com)/\w+/status/\d+[^\s]*|https?://t\.co/[^\s]+',
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
                    # Download audio untuk photo slideshow
                    audio_path = None
                    # Cek beberapa sumber audio dari TikWM
                    audio_url = (
                        video_data.get("play") or
                        video_data.get("hdplay") or
                        (video_data.get("music", {}) or {}).get("play") or
                        (video_data.get("music_info", {}) or {}).get("play") or
                        video_data.get("music")
                    )
                    logger.info(f"TikWM photo audio URL: {audio_url}")

                    if audio_url and isinstance(audio_url, str) and audio_url.startswith("http"):
                        try:
                            async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
                                aresp = await client.get(audio_url)
                                if aresp.status_code == 200 and len(aresp.content) > 1000:
                                    tmp_a = DOWNLOAD_DIR / f"tmp_paudio_{hash(url) & 0xFFFFFFFF:08x}.mp4"
                                    tmp_a.write_bytes(aresp.content)
                                    mp3 = DOWNLOAD_DIR / f"tiktok_paudio_{hash(url) & 0xFFFFFFFF:08x}.mp3"
                                    ok = await convert_to_mp3(str(tmp_a), str(mp3))
                                    tmp_a.unlink(missing_ok=True)
                                    if ok and mp3.exists():
                                        audio_path = str(mp3)
                                        logger.info(f"Photo audio downloaded: {audio_path}")
                        except Exception as e:
                            logger.error(f"Photo audio download error: {e}")

                    # Fallback: download audio via yt-dlp
                    if not audio_path:
                        logger.info("TikWM audio not found, trying yt-dlp for audio...")
                        try:
                            ydl_opts = {
                                'format': 'bestaudio/best',
                                'outtmpl': str(DOWNLOAD_DIR / f'tt_fallback_audio_{hash(url) & 0xFFFFFFFF:08x}.%(ext)s'),
                                'quiet': True,
                                'no_warnings': True,
                                'socket_timeout': 30,
                                'postprocessors': [{
                                    'key': 'FFmpegExtractAudio',
                                    'preferredcodec': 'mp3',
                                    'preferredquality': '192',
                                }],
                            }
                            loop = asyncio.get_event_loop()
                            await loop.run_in_executor(None, _run_ytdlp, url, ydl_opts)
                            # Cari file mp3 hasil download
                            for f in DOWNLOAD_DIR.glob('tt_fallback_audio_*'):
                                if f.exists() and f.suffix == '.mp3' and f.stat().st_size > 0:
                                    audio_path = str(f)
                                    logger.info(f"Fallback audio downloaded: {audio_path}")
                                    break
                        except Exception as e:
                            logger.error(f"Fallback audio download error: {e}")

                    result = {
                        "success": True, "type": "photos",
                        "paths": downloaded,
                        "audio_path": audio_path,
                        "title": title, "author": author,
                        "size": sum(Path(p).stat().st_size for p in downloaded),
                        "duration": duration,
                    }
                    # Audio-only mode
                    if audio_only and audio_path:
                        return {
                            "success": True, "path": audio_path, "type": "audio",
                            "title": title, "author": author,
                            "size": Path(audio_path).stat().st_size, "duration": duration,
                        }
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


async def download_twitter(url: str, audio_only: bool = False) -> dict:
    """Download Twitter/X media (video, photo, multi-photo, GIF)."""
    try:
        return await twitter_downloader.download_twitter(url, DOWNLOAD_DIR, audio_only=audio_only)
    except Exception as e:
        logger.error(f"Twitter download error: {e}")
        return await download_with_ytdlp(url, "twitter", audio_only)


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


async def photos_to_video(image_paths: list, audio_path: str, output_path: str,
                          duration_per_image: float = 3.0) -> bool:
    """Gabungkan foto + audio jadi video slideshow."""
    try:
        # Step 0: Validasi file ada
        valid_images = [p for p in image_paths if Path(p).exists() and Path(p).stat().st_size > 0]
        if not valid_images:
            logger.error("No valid images for slideshow")
            return False
        if not Path(audio_path).exists():
            logger.error(f"Audio file not found: {audio_path}")
            return False

        # Step 1: Convert semua gambar ke JPEG (hindari masalah WEBP/PNG/transparency)
        jpeg_dir = DOWNLOAD_DIR / f"jpeg_{hash(output_path) & 0xFFFFFFFF:08x}"
        jpeg_dir.mkdir(exist_ok=True)
        jpeg_paths = []
        for i, img in enumerate(valid_images):
            jpeg_path = jpeg_dir / f"img_{i:03d}.jpg"
            proc = await asyncio.create_subprocess_exec(
                'ffmpeg', '-i', img, '-vf',
                'scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2',
                '-pix_fmt', 'yuv420p', '-q:v', '2', '-y', str(jpeg_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0 and jpeg_path.exists():
                jpeg_paths.append(str(jpeg_path))
            else:
                logger.error(f"Convert image {i} failed: {stderr.decode()[:200]}")

        if not jpeg_paths:
            logger.error("No images converted to JPEG")
            return False

        # Step 2: Hitung durasi audio (async)
        probe_proc = await asyncio.create_subprocess_exec(
            'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
            '-of', 'csv=p=0', audio_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL
        )
        probe_out, _ = await probe_proc.communicate()
        try:
            audio_dur = float(probe_out.decode().strip())
        except ValueError:
            audio_dur = 30.0

        # Step 3: Buat concat file list
        dur_per = audio_dur / len(jpeg_paths)
        list_file = jpeg_dir / "concat.txt"
        with open(list_file, 'w') as f:
            for img in jpeg_paths:
                # Escape single quotes di path
                safe_path = img.replace("'", "'\''")
                f.write(f"file '{safe_path}'\n")
                f.write(f"duration {dur_per:.2f}\n")
            # Repeat last image
            safe_last = jpeg_paths[-1].replace("'", "'\''")
            f.write(f"file '{safe_last}'\n")

        # Step 4: Buat video
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-f', 'concat', '-safe', '0', '-i', str(list_file),
            '-i', audio_path,
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k',
            '-shortest', '-movflags', '+faststart',
            '-y', output_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(f"ffmpeg slideshow error: {stderr.decode()[:500]}")

        # Cleanup jpeg dir
        import shutil
        shutil.rmtree(jpeg_dir, ignore_errors=True)

        return Path(output_path).exists() and Path(output_path).stat().st_size > 0
    except Exception as e:
        logger.error(f"Photos to video error: {e}")
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
    elif platform == 'twitter':
        return await download_twitter(url, audio_only=audio_only)
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
        "✅ Twitter/X (video, foto, multi-foto, GIF)\n"
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
        "• Twitter/X: `x.com/user/status/...` (video, foto, GIF)\n"
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
            if result.get("type") in ("video", "photos", "gif"):
                cache_key = _cache_url(platform, url)
                cb_data = f"a|{cache_key}"  # short! fits in 64 bytes
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🎵 Download Audio", callback_data=cb_data)
                ]])

            # Kirim media
            # === TWITTER MULTI-PHOTO → kirim sebagai album ===
            if result.get("type") == "photos" and platform == "twitter":
                media_group = []
                opened_files = []
                for i, p in enumerate(result.get("paths", [])):
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
                for p in result.get("paths", []):
                    Path(p).unlink(missing_ok=True)
                await status_msg.delete()
                return

            # === TWITTER GIF → kirim sebagai video ===
            elif result.get("type") == "gif":
                file_path = Path(result["path"])
                with open(file_path, 'rb') as f:
                    await update.message.reply_video(
                        video=f, caption=caption,
                        supports_streaming=True,
                        reply_markup=keyboard
                    )
                file_path.unlink(missing_ok=True)
                await status_msg.delete()
                return

            # === TikTok photo slideshow ===
            elif result.get("type") == "photos":
                # TikTok photo slideshow — tampilkan 3 opsi
                slide_key = _cache_slideshow({
                    "paths": result["paths"],
                    "audio_path": result.get("audio_path"),
                    "title": result["title"],
                    "author": result["author"],
                    "size": result["size"],
                })
                caption = safe_caption(emoji, result["title"], result["author"], info_text)
                # Kirim preview foto pertama + 3 tombol opsi
                first_photo = Path(result["paths"][0]) if result["paths"] else None
                slide_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎬 Gabung jadi Video", callback_data=f"sv|{slide_key}")],
                    [
                        InlineKeyboardButton("📸 Foto Only", callback_data=f"sp|{slide_key}"),
                        InlineKeyboardButton("🎵 Audio Only", callback_data=f"sa|{slide_key}"),
                    ],
                ])
                if first_photo and first_photo.exists():
                    with open(first_photo, 'rb') as f:
                        await update.message.reply_photo(
                            photo=f, caption=caption,
                            reply_markup=slide_kb
                        )
                else:
                    await update.message.reply_text(caption, reply_markup=slide_kb)

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
    """Handler tombol 'Download Audio' dari inline keyboard (video posts)."""
    query = update.callback_query
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


async def handle_slideshow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler tombol slideshow: sv=video, sp=photos, sa=audio"""
    query = update.callback_query
    data = query.data or ""
    parts = data.split("|", 1)
    if len(parts) != 2:
        await query.answer("Data tidak valid.", show_alert=True)
        return

    action, slide_key = parts
    slide = _get_slideshow(slide_key)
    if not slide:
        await query.answer("Data expired. Kirim ulang link-nya ya.", show_alert=True)
        return

    paths = slide["paths"]
    audio_path = slide.get("audio_path")
    title = slide.get("title", "TikTok Slideshow")
    author = slide.get("author", "unknown")

    # === FOTO ONLY ===
    if action == "sp":
        await query.answer("📸 Mengirim foto...")
        media_group = []
        opened = []
        for i, p in enumerate(paths):
            img = Path(p)
            if img.exists():
                f = open(img, 'rb')
                opened.append(f)
                cap = safe_caption("📸", title, author) if i == 0 else ""
                media_group.append(InputMediaPhoto(media=f, caption=cap))
        if media_group:
            await query.message.reply_media_group(media=media_group)
        for f in opened:
            f.close()
        _cleanup_slideshow_files(slide)
        _slideshow_cache.pop(slide_key, None)

    # === AUDIO ONLY ===
    elif action == "sa":
        if not audio_path or not Path(audio_path).exists():
            await query.answer(
                "Audio tidak tersedia untuk post ini.",
                show_alert=True
            )
            return
        await query.answer("🎵 Mengirim audio...")
        caption = safe_caption("🎵", title, author, f"📦 {format_size(Path(audio_path).stat().st_size)}")
        with open(audio_path, 'rb') as f:
            await query.message.reply_audio(
                audio=f, caption=caption,
                title=title, performer=author
            )
        _cleanup_slideshow_files(slide)
        _slideshow_cache.pop(slide_key, None)

    # === GABUNG JADI VIDEO ===
    elif action == "sv":
        if not audio_path or not Path(audio_path).exists():
            await query.answer(
                "Audio tidak tersedia. Coba opsi Foto Only atau Audio Only.",
                show_alert=True
            )
            return
        await query.answer("🎬 Membuat video...")
        status_msg = await query.message.reply_text("🎬 Menggabungkan foto + audio jadi video...")

        video_path = str(DOWNLOAD_DIR / f"slideshow_{slide_key}.mp4")
        try:
            success = await asyncio.wait_for(
                photos_to_video(paths, audio_path, video_path),
                timeout=120  # 2 menit max
            )
        except asyncio.TimeoutError:
            success = False
            logger.error("Slideshow video creation timed out")

        if success:
            vid_file = Path(video_path)
            if vid_file.stat().st_size > MAX_FILE_SIZE:
                await status_msg.edit_text(
                    f"❌ Video terlalu besar ({format_size(vid_file.stat().st_size)}). Limit 50MB."
                )
                vid_file.unlink(missing_ok=True)
            else:
                caption = safe_caption("🎬", title, author,
                                       f"📦 {format_size(vid_file.stat().st_size)}")
                await safe_edit_msg(context.bot, update.effective_chat.id,
                                    status_msg.message_id, "🎬 Mengirim video...")
                with open(vid_file, 'rb') as f:
                    await query.message.reply_video(
                        video=f, caption=caption, supports_streaming=True
                    )
                await status_msg.delete()
                vid_file.unlink(missing_ok=True)
        else:
            await status_msg.edit_text("❌ Gagal membuat video. Coba lagi nanti.")

        _cleanup_slideshow_files(slide)
        _slideshow_cache.pop(slide_key, None)


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

    # Callback handler untuk tombol audio (video posts)
    app.add_handler(CallbackQueryHandler(handle_audio_callback, pattern=r"^a\|"))
    # Callback handler untuk slideshow (photo posts)
    app.add_handler(CallbackQueryHandler(handle_slideshow_callback, pattern=r"^s[vpa]\|"))

    # Message handler (harus terakhir, paling rendah priority)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Bot berjalan!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

