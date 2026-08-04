"""
Twitter/X Media Downloader
Support: Video, Photo (single & multi-image), GIF
Uses external API for reliable extraction.
"""

import re
import asyncio
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# API endpoints untuk extract Twitter media
# Primary: fxtwitter (public, no auth needed)
# Fallback: vxtwitter
FXTWITTER_API = "https://api.fxtwitter.com/{tweet_id}"
VXTWITTER_API = "https://api.vxtwitter.com/Post/status/{tweet_id}"


def extract_tweet_id(url: str) -> str | None:
    """Extract tweet ID dari URL Twitter/X."""
    # Match: x.com/user/status/1234567890 atau twitter.com/user/status/1234567890
    patterns = [
        r'(?:twitter\.com|x\.com)/\w+/status/(\d+)',
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
    
    Returns dict:
      - success: bool
      - type: "video" | "photo" | "photos" | "audio" | "gif"
      - path: str (single file)
      - paths: list[str] (multiple photos)
      - title: str
      - author: str
      - size: int
      - duration: float|None
    """
    tweet_id = extract_tweet_id(url)
    if not tweet_id:
        return {"success": False, "error": "URL Twitter/X tidak valid"}

    # Try fxtwitter API first, fallback to vxtwitter
    for api_template in [FXTWITTER_API, VXTWITTER_API]:
        try:
            result = await _fetch_from_api(api_template, tweet_id, url, download_dir, audio_only)
            if result["success"]:
                return result
        except Exception as e:
            logger.warning(f"API {api_template} failed: {e}")
            continue

    return {"success": False, "error": "Gagal mengambil media dari tweet ini. Pastikan tweet bersifat publik."}


async def _fetch_from_api(api_template: str, tweet_id: str, url: str, 
                          download_dir: Path, audio_only: bool) -> dict:
    """Fetch tweet data dari API dan download media."""
    api_url = api_template.format(tweet_id=tweet_id)
    
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(api_url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })
        
        if resp.status_code != 200:
            return {"success": False, "error": f"API returned {resp.status_code}"}
        
        data = resp.json()
    
    # Parse response — fxtwitter format
    tweet = data.get("tweet", data)
    
    author = tweet.get("author", {}).get("name", "") or tweet.get("user_name", "unknown")
    author_handle = tweet.get("author", {}).get("screen_name", "") or tweet.get("user_screen_name", "")
    text = tweet.get("text", "")[:120]
    title = text or f"Tweet by @{author_handle}"
    
    # Get media
    media_list = tweet.get("media", {})
    photos = media_list.get("photos", []) or []
    video = media_list.get("video", {}) or {}
    
    # Check for external media (some tweets embed media differently)
    if not photos and not video:
        # Try alternate field names
        media_entities = tweet.get("mediaDetails", []) or tweet.get("entities", {}).get("media", [])
        for item in media_entities:
            media_type = item.get("type", "")
            if media_type == "photo":
                photo_url = item.get("media_url_https") or item.get("url")
                if photo_url:
                    photos.append({"url": photo_url})
            elif media_type in ("video", "animated_gif"):
                variants = item.get("video_info", {}).get("variants", [])
                # Get highest bitrate mp4
                best = None
                for v in variants:
                    if v.get("content_type") == "video/mp4":
                        if not best or (v.get("bitrate", 0) > best.get("bitrate", 0)):
                            best = v
                if best:
                    video = {"url": best.get("url"), "type": media_type}
    
    # Audio-only mode
    if audio_only and video:
        return await _download_video_as_audio(video, url, download_dir, title, author_handle)
    
    # Download VIDEO
    if video and not photos:
        video_url = video.get("url")
        if not video_url:
            return {"success": False, "error": "URL video tidak ditemukan"}
        
        is_gif = video.get("type") == "animated_gif"
        return await _download_video(video_url, url, download_dir, title, author_handle, is_gif)
    
    # Download PHOTOS (single or multiple)
    if photos:
        return await _download_photos(photos, url, download_dir, title, author_handle)
    
    # Has video + photos (mixed media tweet) — prefer video
    if video:
        video_url = video.get("url")
        if video_url:
            return await _download_video(video_url, url, download_dir, title, author_handle, False)
    
    return {"success": False, "error": "Tidak ada media yang ditemukan di tweet ini"}


async def _download_video(video_url: str, orig_url: str, download_dir: Path,
                          title: str, author: str, is_gif: bool) -> dict:
    """Download video/GIF dari URL."""
    try:
        unique = f"{hash(orig_url) & 0xFFFFFFFF:08x}"
        ext = "mp4"
        file_path = download_dir / f"twitter_{unique}.{ext}"
        
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(video_url)
            if resp.status_code != 200:
                return {"success": False, "error": f"Download gagal (HTTP {resp.status_code})"}
            
            file_path.write_bytes(resp.content)
        
        return {
            "success": True,
            "type": "gif" if is_gif else "video",
            "path": str(file_path),
            "title": title,
            "author": author,
            "size": len(resp.content),
            "duration": None,
        }
    except Exception as e:
        logger.error(f"Twitter video download error: {e}")
        return {"success": False, "error": str(e)[:200]}


async def _download_video_as_audio(video: dict, orig_url: str, download_dir: Path,
                                    title: str, author: str) -> dict:
    """Download video dan convert ke MP3."""
    video_url = video.get("url")
    if not video_url:
        return {"success": False, "error": "URL video tidak ditemukan"}
    
    try:
        unique = f"{hash(orig_url) & 0xFFFFFFFF:08x}"
        tmp_video = download_dir / f"twitter_tmp_{unique}.mp4"
        mp3_path = download_dir / f"twitter_audio_{unique}.mp3"
        
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(video_url)
            if resp.status_code != 200:
                return {"success": False, "error": f"Download gagal (HTTP {resp.status_code})"}
            tmp_video.write_bytes(resp.content)
        
        # Convert to MP3
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-i', str(tmp_video), '-vn', '-ab', '192k',
            '-ar', '44100', '-y', str(mp3_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
        tmp_video.unlink(missing_ok=True)
        
        if mp3_path.exists() and mp3_path.stat().st_size > 0:
            return {
                "success": True,
                "type": "audio",
                "path": str(mp3_path),
                "title": title,
                "author": author,
                "size": mp3_path.stat().st_size,
                "duration": None,
            }
        
        return {"success": False, "error": "Gagal convert ke audio"}
    except Exception as e:
        logger.error(f"Twitter audio conversion error: {e}")
        return {"success": False, "error": str(e)[:200]}


async def _download_photos(photos: list, orig_url: str, download_dir: Path,
                           title: str, author: str) -> dict:
    """Download single atau multiple photos."""
    try:
        downloaded = []
        unique = f"{hash(orig_url) & 0xFFFFFFFF:08x}"
        
        for i, photo in enumerate(photos[:10]):  # Max 10 foto
            photo_url = photo.get("url") if isinstance(photo, dict) else str(photo)
            if not photo_url:
                continue
            
            # Pastikan format high-res
            # Twitter: append ?format=jpg&name=4096x4096 untuk resolusi maksimal
            if "?" in photo_url:
                photo_url = re.sub(r'&?name=\w+', '', photo_url)
                photo_url += "&name=4096x4096"
            else:
                photo_url += "?format=jpg&name=4096x4096"
            
            file_path = download_dir / f"twitter_photo_{unique}_{i}.jpg"
            
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(photo_url)
                if resp.status_code == 200 and len(resp.content) > 1000:
                    file_path.write_bytes(resp.content)
                    downloaded.append(str(file_path))
                else:
                    logger.warning(f"Photo {i} download failed: HTTP {resp.status_code}")
        
        if not downloaded:
            return {"success": False, "error": "Gagal mendownload foto"}
        
        total_size = sum(Path(p).stat().st_size for p in downloaded)
        
        if len(downloaded) == 1:
            return {
                "success": True,
                "type": "photo",
                "path": downloaded[0],
                "title": title,
                "author": author,
                "size": total_size,
                "duration": None,
            }
        else:
            return {
                "success": True,
                "type": "photos",
                "paths": downloaded,
                "title": title,
                "author": author,
                "size": total_size,
                "duration": None,
            }
    except Exception as e:
        logger.error(f"Twitter photo download error: {e}")
        return {"success": False, "error": str(e)[:200]}
