"""
Twitter/X Media Downloader
Uses yt-dlp (primary) for reliable extraction.
Support: Video, Photo (single & multi-image), GIF, Audio
"""

import re
import asyncio
import logging
import time
from pathlib import Path

import yt_dlp

logger = logging.getLogger(__name__)


def extract_tweet_id(url: str) -> str | None:
    """Extract tweet ID dari URL Twitter/X."""
    patterns = [
        r'(?:twitter\.com|x\.com)/\w+/status/(\d+)',
        r'(?:twitter\.com|x\.com)/\w+/statuses/(\d+)',
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def _make_opts(download_dir: Path, unique: str, audio_only: bool = False) -> dict:
    """Build yt-dlp options untuk Twitter."""
    if audio_only:
        return {
            'format': 'bestaudio/best',
            'outtmpl': str(download_dir / f'twitter_audio_{unique}.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
            'retries': 3,
            'noplaylist': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        }
    else:
        return {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best',
            'outtmpl': str(download_dir / f'twitter_{unique}.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
            'retries': 3,
            'merge_output_format': 'mp4',
            'noplaylist': True,
        }


def _run_ytdlp(url: str, opts: dict) -> dict:
    """Blocking yt-dlp call."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


async def download_twitter(url: str, download_dir: Path, audio_only: bool = False) -> dict:
    """
    Download media dari Twitter/X post.
    Primary: yt-dlp (paling reliable).
    Fallback: parse halaman untuk foto.
    """
    unique = f"{hash(url) & 0xFFFFFFFF:08x}_{int(time.time())}"
    
    # === VIDEO / GIF / AUDIO ===
    try:
        opts = _make_opts(download_dir, unique, audio_only)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _run_ytdlp, url, opts)
        
        # Cari file hasil download
        target_file = _find_latest_file(download_dir, "twitter", unique)
        
        if target_file and target_file.exists() and target_file.stat().st_size > 0:
            file_type = _detect_type(target_file, audio_only)
            author = info.get("uploader", "") or info.get("uploader_id", "unknown")
            title = info.get("title", "")[:120] or f"Tweet by @{author}"
            
            return {
                "success": True,
                "type": file_type,
                "path": str(target_file),
                "title": title,
                "author": author,
                "size": target_file.stat().st_size,
                "duration": info.get("duration"),
            }
    except Exception as e:
        err_msg = str(e).lower()
        logger.warning(f"yt-dlp video/audio failed: {e}")
        
        # Jika error karena "no video" atau "not a video", coba sebagai foto
        if any(x in err_msg for x in ["no video", "no media", "not a video", "no images"]):
            pass  # lanjut ke photo handler
        else:
            # Error lain — mungkin tweet foto-only, coba juga
            pass
    
    # === PHOTO (single / multi-image) ===
    # yt-dlp kadang gagal download foto Twitter, jadi kita extract dari metadata
    try:
        photo_result = await _download_photos(url, download_dir, unique)
        if photo_result["success"]:
            return photo_result
    except Exception as e:
        logger.warning(f"Photo download failed: {e}")
    
    # === LAST RESORT: coba ambil info saja ===
    try:
        opts = _make_opts(download_dir, unique, False)
        opts['skip_download'] = True
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _run_ytdlp, url, opts)
        
        # Check apakah ada photo di entries
        if info.get("entries"):
            entry = list(info["entries"])[0] if isinstance(info["entries"], list) else info["entries"]
            urls = entry.get("urls") or entry.get("url")
            if urls:
                photo_urls = urls if isinstance(urls, list) else [urls]
                return await _download_photo_list(photo_urls, download_dir, unique,
                                                   info.get("uploader", "unknown"),
                                                   info.get("title", "Twitter Photo"))
    except Exception as e:
        logger.warning(f"Last resort failed: {e}")
    
    return {"success": False, "error": "Gagal mengambil media dari tweet ini. Pastikan tweet bersifat publik."}


async def _download_photos(url: str, download_dir: Path, unique: str) -> dict:
    """Download foto dari Twitter — extract semua image URL."""
    # Strategy: gunakan yt-dlp dengan format best untuk extract image URLs
    opts = {
        'format': 'best',
        'outtmpl': str(download_dir / f'twitter_photo_{unique}.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 30,
        'retries': 3,
        'noplaylist': True,
        'write_thumbnail': False,
    }
    
    loop = asyncio.get_event_loop()
    
    try:
        info = await loop.run_in_executor(None, _run_ytdlp, url, opts)
    except Exception as e:
        logger.warning(f"yt-dlp photo extract failed: {e}")
        # Fallback: coba extract URL dari page metadata
        return await _scrape_photo_urls(url, download_dir, unique)
    
    # Cari file yang didownload
    downloaded = []
    
    # Cek file dengan pattern twitter_photo_*
    for f in sorted(download_dir.glob(f"twitter_photo_{unique}*"), 
                    key=lambda x: x.stat().st_mtime, reverse=True):
        if f.exists() and f.stat().st_size > 0:
            suffix = f.suffix.lower()
            if suffix in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
                downloaded.append(str(f))
    
    if not downloaded:
        # Cek file dengan pattern twitter_*
        for f in sorted(download_dir.glob(f"twitter_{unique}*"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            if f.exists() and f.stat().st_size > 0:
                downloaded.append(str(f))
    
    if downloaded:
        author = info.get("uploader", "") or info.get("uploader_id", "unknown")
        title = info.get("title", "")[:120] or f"Tweet by @{author}"
        total_size = sum(Path(p).stat().st_size for p in downloaded)
        
        if len(downloaded) == 1:
            return {
                "success": True, "type": "photo",
                "path": downloaded[0],
                "title": title, "author": author,
                "size": total_size, "duration": None,
            }
        else:
            return {
                "success": True, "type": "photos",
                "paths": downloaded,
                "title": title, "author": author,
                "size": total_size, "duration": None,
            }
    
    return {"success": False, "error": "No photos found"}


async def _scrape_photo_urls(url: str, download_dir: Path, unique: str) -> dict:
    """
    Scrape foto langsung dari Twitter page.
    Twitter embeds photo URLs di OG tags / page HTML.
    """
    try:
        import httpx
        
        # Pakai Twitter embed/oembed untuk dapatkan info
        # Atau scrape langsung dari page
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml',
                'Accept-Language': 'en-US,en;q=0.9',
            })
            
            if resp.status_code != 200:
                return {"success": False, "error": f"HTTP {resp.status_code}"}
            
            html = resp.text
            
            # Extract image URLs dari HTML
            # Twitter menaruh di og:image dan di data attributes
            photo_urls = []
            
            # Method 1: og:image
            og_matches = re.findall(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
            for m in og_matches:
                if 'pbs.twimg.com' in m and 'profile' not in m:
                    # Tambah ?format=jpg&name=4096x4096 untuk resolusi tinggi
                    clean_url = re.sub(r'&?name=\w+', '', m)
                    clean_url = re.sub(r'\?format=(\w+)', '', clean_url)
                    photo_urls.append(f"{clean_url}?format=jpg&name=4096x4096")
            
            # Method 2: pbs.twimg.com media URLs
            media_matches = re.findall(r'https://pbs\.twimg\.com/media/[^"\'?\s]+', html)
            for m in media_matches:
                clean = m.split('?')[0]
                full_url = f"{clean}?format=jpg&name=4096x4096"
                if full_url not in photo_urls:
                    photo_urls.append(full_url)
            
            # Deduplicate
            seen = set()
            unique_urls = []
            for u in photo_urls:
                base = u.split('?')[0]
                if base not in seen:
                    seen.add(base)
                    unique_urls.append(u)
            
            if unique_urls:
                return await _download_photo_list(unique_urls, download_dir, unique, "unknown", "Twitter Photo")
            
            return {"success": False, "error": "No photo URLs found in page"}
    
    except Exception as e:
        logger.error(f"Photo scrape error: {e}")
        return {"success": False, "error": str(e)[:200]}


async def _download_photo_list(photo_urls: list, download_dir: Path, unique: str,
                                author: str, title: str) -> dict:
    """Download list of photo URLs."""
    try:
        import httpx
        downloaded = []
        
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            for i, photo_url in enumerate(photo_urls[:10]):
                try:
                    resp = await client.get(photo_url, headers={
                        'User-Agent': 'Mozilla/5.0'
                    })
                    if resp.status_code == 200 and len(resp.content) > 1000:
                        file_path = download_dir / f"twitter_photo_{unique}_{i}.jpg"
                        file_path.write_bytes(resp.content)
                        downloaded.append(str(file_path))
                except Exception as e:
                    logger.warning(f"Photo {i} download failed: {e}")
        
        if not downloaded:
            return {"success": False, "error": "Gagal download foto"}
        
        total_size = sum(Path(p).stat().st_size for p in downloaded)
        
        if len(downloaded) == 1:
            return {
                "success": True, "type": "photo",
                "path": downloaded[0],
                "title": title, "author": author,
                "size": total_size, "duration": None,
            }
        else:
            return {
                "success": True, "type": "photos",
                "paths": downloaded,
                "title": title, "author": author,
                "size": total_size, "duration": None,
            }
    except Exception as e:
        logger.error(f"Photo list download error: {e}")
        return {"success": False, "error": str(e)[:200]}


def _find_latest_file(download_dir: Path, prefix: str, unique: str) -> Path | None:
    """Cari file terbaru yang match pattern."""
    target = None
    target_mtime = 0
    
    # Cari dengan unique suffix dulu
    for f in download_dir.glob(f"{prefix}_{unique}*"):
        if f.exists() and f.stat().st_size > 0:
            mt = f.stat().st_mtime
            if mt > target_mtime:
                target_mtime = mt
                target = f
    
    # Fallback: cari prefix terbaru
    if not target:
        for f in download_dir.glob(f"{prefix}_*"):
            if f.exists() and f.stat().st_size > 0:
                mt = f.stat().st_mtime
                if mt > target_mtime:
                    target_mtime = mt
                    target = f
    
    return target


def _detect_type(file_path: Path, audio_only: bool) -> str:
    """Deteksi tipe file."""
    if audio_only:
        return "audio"
    
    suffix = file_path.suffix.lower()
    if suffix == '.mp3':
        return "audio"
    if suffix in ('.jpg', '.jpeg', '.png', '.webp'):
        return "photo"
    if suffix == '.gif':
        return "gif"
    
    # Untuk .mp4, cek apakah ada video stream
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
        return "audio"
    except Exception:
        return "video" if suffix in ('.mp4', '.webm', '.mkv') else "audio"
