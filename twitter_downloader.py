"""
Twitter/X Media Downloader
Support: Video (single & multi), Photo, GIF, Audio
Bisa handle sensitive content tanpa cookies.
"""

import re
import asyncio
import logging
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

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
    Support multi-video tweets.
    """
    unique = f"{hash(url) & 0xFFFFFFFF:08x}_{int(time.time())}"
    
    # === STEP 1: savetwitter.net API ===
    try:
        result = await _download_via_savetwitter(url, download_dir, unique, audio_only)
        if result["success"]:
            return result
    except Exception as e:
        logger.warning(f"savetwitter.net failed: {e}")
    
    # === STEP 2: yt-dlp fallback ===
    try:
        result = await _download_via_ytdlp(url, download_dir, unique, audio_only)
        if result["success"]:
            return result
    except Exception as e:
        logger.warning(f"yt-dlp fallback failed: {e}")
    
    return {"success": False, "error": "Gagal mengambil media dari tweet ini."}


async def _download_via_savetwitter(url: str, download_dir: Path, unique: str,
                                     audio_only: bool = False) -> dict:
    """Download via savetwitter.net — support multi-video tweets."""
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
        
        # Split per video block
        blocks = html.split('<div class="tw-video">')
        video_blocks = [b for b in blocks[1:] if b.strip()]  # Skip first empty
        
        if not video_blocks:
            # Mungkin photo-only tweet
            return await _handle_photo_from_html(html, download_dir, unique)
        
        # Parse semua video dari blocks
        videos = []
        for block in video_blocks:
            parsed = _parse_video_block(block)
            if parsed:
                videos.append(parsed)
        
        if not videos:
            return {"success": False, "error": "No video links found"}
        
        # Audio-only: ambil dari video pertama
        if audio_only:
            first = videos[0]
            if first.get('audio_url'):
                return await _download_file(
                    first['audio_url'], download_dir,
                    f"twitter_audio_{unique}.mp4",
                    "audio", first['title'], "unknown", first['duration']
                )
            return {"success": False, "error": "Audio extraction not available"}
        
        # Download semua video
        if len(videos) == 1:
            # Single video
            return await _download_file(
                videos[0]['best_url'], download_dir,
                f"twitter_{unique}.mp4",
                "video", videos[0]['title'], "unknown", videos[0]['duration']
            )
        else:
            # Multi video — download semua
            downloaded = []
            for i, vid in enumerate(videos):
                filename = f"twitter_{unique}_{i}.mp4"
                result = await _download_file(
                    vid['best_url'], download_dir, filename,
                    "video", vid['title'], "unknown", vid['duration']
                )
                if result["success"]:
                    downloaded.append(result)
            
            if not downloaded:
                return {"success": False, "error": "Gagal download semua video"}
            
            if len(downloaded) == 1:
                return downloaded[0]
            
            # Return sebagai multi-video
            total_size = sum(d['size'] for d in downloaded)
            return {
                "success": True,
                "type": "videos",
                "results": downloaded,  # List of individual results
                "title": downloaded[0]['title'],
                "author": "unknown",
                "size": total_size,
                "duration": None,
            }


def _parse_video_block(block: str) -> dict | None:
    """Parse satu video block, return dict dengan best_url, title, duration, dll."""
    # Title
    title_match = re.search(r'<h3>(.*?)</h3>', block)
    title = title_match.group(1).strip()[:120] if title_match else "Twitter Video"
    
    # Duration
    dur_match = re.search(r'<p>(\d+:\d+)</p>', block)
    duration = dur_match.group(1) if dur_match else ""
    
    # Download links — ambil semua MP4 links dengan quality
    mp4_links = re.findall(
        r'href="(https://dl\.snapcdn\.app/get\?token=***"]+)"[^>]*>.*?Download MP4\s*\((\w+)\)',
        block, re.DOTALL
    )
    
    if not mp4_links:
        # Fallback: cari semua snapcdn links
        all_links = re.findall(r'href="(https://dl\.snapcdn\.app/get\?token=***"]+)"', block)
        if all_links:
            return {
                'best_url': all_links[0],
                'quality': 'unknown',
                'title': title,
                'duration': duration,
                'audio_url': None,
            }
        return None
    
    # Sort by quality (highest first)
    parsed_links = []
    for link, quality in mp4_links:
        q_num = int(re.search(r'(\d+)', quality).group(1)) if re.search(r'(\d+)', quality) else 0
        parsed_links.append({'url': link, 'quality': quality, 'q_num': q_num})
    
    parsed_links.sort(key=lambda x: x['q_num'], reverse=True)
    best = parsed_links[0]
    
    # Audio URL
    audio_match = re.search(r'data-audioUrl="([^"]+)"', block)
    audio_url = audio_match.group(1) if audio_match else None
    
    return {
        'best_url': best['url'],
        'quality': best['quality'],
        'title': title,
        'duration': duration,
        'audio_url': audio_url,
    }


async def _handle_photo_from_html(html: str, download_dir: Path, unique: str) -> dict:
    """Handle photo-only tweets dari HTML."""
    # Photo download link
    photo_links = re.findall(
        r'href="(https://dl\.snapcdn\.app/get\?token=***"]+)"[^>]*>.*?Download Photo',
        html, re.DOTALL
    )
    
    if photo_links:
        return await _download_file(
            photo_links[0], download_dir, f"twitter_photo_{unique}.jpg",
            "photo", "Twitter Photo", "unknown", ""
        )
    
    # Fallback: thumbnail
    thumb_match = re.search(r'src="(https://pbs\.twimg\.com/[^"]+)"', html)
    if thumb_match:
        return await _download_file(
            thumb_match.group(1), download_dir, f"twitter_photo_{unique}.jpg",
            "photo", "Twitter Photo", "unknown", ""
        )
    
    return {"success": False, "error": "No media found"}


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
            
            content = resp.content
            if len(content) < 1000:
                return {"success": False, "error": "File too small / empty"}
            
            file_path.write_bytes(content)
        
        # Parse duration
        duration = None
        if duration_str:
            parts = duration_str.split(':')
            if len(parts) == 2:
                try:
                    duration = int(parts[0]) * 60 + int(parts[1])
                except ValueError:
                    pass
        
        return {
            "success": True,
            "type": media_type,
            "path": str(file_path),
            "title": title,
            "author": author,
            "size": len(content),
            "duration": duration,
        }
    except Exception as e:
        logger.error(f"Download error: {e}")
        return {"success": False, "error": str(e)[:200]}


async def _download_via_ytdlp(url: str, download_dir: Path, unique: str,
                               audio_only: bool = False) -> dict:
    """Fallback: yt-dlp untuk non-sensitive tweets."""
    try:
        import yt_dlp
        
        opts = {
            'quiet': True, 'no_warnings': True,
            'socket_timeout': 30, 'retries': 3, 'noplaylist': True,
        }
        
        if audio_only:
            opts.update({
                'format': 'bestaudio/best',
                'outtmpl': str(download_dir / f'twitter_audio_{unique}.%(ext)s'),
                'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            })
        else:
            opts.update({
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best',
                'outtmpl': str(download_dir / f'twitter_{unique}.%(ext)s'),
                'merge_output_format': 'mp4',
            })
        
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _run_ytdlp, url, opts)
        
        # Find file
        target = None
        for f in sorted(download_dir.glob(f"twitter_{unique}*"), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.exists() and f.stat().st_size > 0:
                target = f
                break
        
        if target:
            file_type = "audio" if audio_only or target.suffix == '.mp3' else "video"
            author = info.get("uploader", "") or info.get("uploader_id", "unknown")
            title = info.get("title", "")[:120] or f"Tweet by @{author}"
            return {
                "success": True, "type": file_type, "path": str(target),
                "title": title, "author": author,
                "size": target.stat().st_size, "duration": info.get("duration"),
            }
        
        return {"success": False, "error": "File not found"}
    except Exception as e:
        return {"success": False, "error": str(e)[:200]}


def _run_ytdlp(url: str, opts: dict) -> dict:
    import yt_dlp
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)
