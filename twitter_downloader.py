"""
Twitter/X Media Downloader
Support: Video, Photo (single & multi-image), GIF, Audio
Bisa handle sensitive content tanpa cookies.
"""

import re
import asyncio
import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Third-party API untuk bypass sensitive content
SAVETWITTER_API = "https://savetwitter.net/api/ajaxSearch"


def extract_tweet_id(url: str) -> str | None:
    """Extract tweet ID dari URL Twitter/X."""
    patterns = [
        r'(?:twitter\.com|x\.com)/(?:\w+/status|i/status)/(\d+)',
        r'(?:twitter\.com|x\.com)/\w+/statuses/(\d+)',
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


async def download_twitter(url: str, download_dir: Path, audio_only: bool = False) -> dict:
    """
    Download media dari Twitter/X post.
    Bisa handle sensitive content tanpa cookies.
    
    Strategy:
    1. savetwitter.net API (bisa bypass sensitive content)
    2. yt-dlp fallback (untuk non-sensitive)
    """
    unique = f"{hash(url) & 0xFFFFFFFF:08x}_{int(time.time())}"
    
    # === STEP 1: savetwitter.net (primary — works for sensitive content) ===
    try:
        result = await _download_via_savetwitter(url, download_dir, unique, audio_only)
        if result["success"]:
            return result
    except Exception as e:
        logger.warning(f"savetwitter.net failed: {e}")
    
    # === STEP 2: yt-dlp fallback (non-sensitive tweets) ===
    try:
        import yt_dlp
        result = await _download_via_ytdlp(url, download_dir, unique, audio_only)
        if result["success"]:
            return result
    except Exception as e:
        logger.warning(f"yt-dlp fallback failed: {e}")
    
    return {"success": False, "error": "Gagal mengambil media dari tweet ini. Pastikan link valid."}


async def _download_via_savetwitter(url: str, download_dir: Path, unique: str,
                                     audio_only: bool = False) -> dict:
    """Download via savetwitter.net API — bisa bypass sensitive content."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
        resp = await client.post(SAVETWITTER_API, data={'q': url, 'lang': 'en'})
        
        if resp.status_code != 200:
            return {"success": False, "error": f"API returned {resp.status_code}"}
        
        data = resp.json()
        html = data.get('data', '')
        
        if not html:
            return {"success": False, "error": "No data from API"}
        
        # Parse title dari HTML
        title_match = re.search(r'<h3>(.*?)</h3>', html)
        title = title_match.group(1).strip()[:120] if title_match else "Twitter Media"
        
        # Parse duration
        dur_match = re.search(r'<p>(\d+:\d+)</p>', html)
        duration_str = dur_match.group(1) if dur_match else ""
        
        # === VIDEO LINKS ===
        video_links = []
        # Pattern: href="...snapcdn..." ... Download MP4 (QUALITY)
        video_matches = re.findall(
            r'href="(https?://dl\.snapcdn\.app/get\?token=[^"]+)"[^>]*>.*?Download MP4\s*\((\d+p?)\)',
            html, re.DOTALL
        )
        for link, quality in video_matches:
            # Extract quality number
            q_num = int(re.search(r'(\d+)', quality).group(1)) if re.search(r'(\d+)', quality) else 0
            video_links.append({'url': link, 'quality': quality, 'q_num': q_num})
        
        # Sort by quality (highest first)
        video_links.sort(key=lambda x: x['q_num'], reverse=True)
        
        # === PHOTO LINK ===
        photo_links = re.findall(
            r'href="(https?://dl\.snapcdn\.app/get\?token=[^"]+)"[^>]*>.*?Download Photo',
            html, re.DOTALL
        )
        
        # === THUMBNAIL (fallback for photo) ===
        thumb_match = re.search(r'<img src="(https://pbs\.twimg\.com/[^"]+)"', html)
        thumb_url = thumb_match.group(1) if thumb_match else None
        
        # === AUDIO (MP3 convert data) ===
        audio_url_match = re.search(r'data-audioUrl="([^"]+)"', html)
        audio_url = audio_url_match.group(1) if audio_url_match else None
        
        # === DOWNLOAD ===
        
        # Audio-only mode
        if audio_only:
            if audio_url:
                return await _download_file(audio_url, download_dir, f"twitter_audio_{unique}.mp4",
                                            "audio", title, "unknown", duration_str)
            elif video_links:
                # Download video lalu convert (fallback)
                return {"success": False, "error": "Audio extraction not available for this tweet"}
            return {"success": False, "error": "No audio found"}
        
        # Video download (highest quality)
        if video_links:
            best = video_links[0]
            result = await _download_file(best['url'], download_dir, f"twitter_{unique}.mp4",
                                          "video", title, "unknown", duration_str)
            if result["success"]:
                result["quality"] = best['quality']
                return result
        
        # Photo download
        if photo_links:
            result = await _download_file(photo_links[0], download_dir, f"twitter_{unique}.jpg",
                                          "photo", title, "unknown", duration_str)
            if result["success"]:
                return result
        
        # Fallback: download thumbnail sebagai photo
        if thumb_url:
            result = await _download_file(thumb_url, download_dir, f"twitter_{unique}.jpg",
                                          "photo", title, "unknown", duration_str)
            if result["success"]:
                return result
        
        return {"success": False, "error": "No downloadable media found"}


async def _download_file(url: str, download_dir: Path, filename: str,
                          media_type: str, title: str, author: str, duration_str: str) -> dict:
    """Download file dari URL."""
    try:
        file_path = download_dir / filename
        
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            })
            
            if resp.status_code != 200:
                return {"success": False, "error": f"Download failed (HTTP {resp.status_code})"}
            
            file_path.write_bytes(resp.content)
        
        # Parse duration ke detik
        duration = None
        if duration_str:
            parts = duration_str.split(':')
            if len(parts) == 2:
                duration = int(parts[0]) * 60 + int(parts[1])
        
        return {
            "success": True,
            "type": media_type,
            "path": str(file_path),
            "title": title,
            "author": author,
            "size": file_path.stat().st_size,
            "duration": duration,
        }
    except Exception as e:
        logger.error(f"Download error: {e}")
        return {"success": False, "error": str(e)[:200]}


async def _download_via_ytdlp(url: str, download_dir: Path, unique: str,
                               audio_only: bool = False) -> dict:
    """Fallback: download via yt-dlp (untuk non-sensitive tweets)."""
    try:
        import yt_dlp
        
        opts = {
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
            'retries': 3,
            'noplaylist': True,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            },
        }
        
        if audio_only:
            opts.update({
                'format': 'bestaudio/best',
                'outtmpl': str(download_dir / f'twitter_audio_{unique}.%(ext)s'),
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
        else:
            opts.update({
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best',
                'outtmpl': str(download_dir / f'twitter_{unique}.%(ext)s'),
                'merge_output_format': 'mp4',
            })
        
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _run_ytdlp, url, opts)
        
        # Find downloaded file
        target = None
        target_mtime = 0
        for f in download_dir.glob(f"twitter_{unique}*"):
            if f.exists() and f.stat().st_size > 0:
                mt = f.stat().st_mtime
                if mt > target_mtime:
                    target_mtime = mt
                    target = f
        
        if not target:
            for f in download_dir.glob("twitter_*"):
                if f.exists() and f.stat().st_size > 0:
                    mt = f.stat().st_mtime
                    if mt > target_mtime:
                        target_mtime = mt
                        target = f
        
        if target:
            file_type = _detect_type(target, audio_only)
            author = info.get("uploader", "") or info.get("uploader_id", "unknown")
            title = info.get("title", "")[:120] or f"Tweet by @{author}"
            
            return {
                "success": True,
                "type": file_type,
                "path": str(target),
                "title": title,
                "author": author,
                "size": target.stat().st_size,
                "duration": info.get("duration"),
            }
        
        return {"success": False, "error": "File not found after download"}
    
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


def _run_ytdlp(url: str, opts: dict) -> dict:
    """Blocking yt-dlp call."""
    import yt_dlp
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


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
    return "video"
