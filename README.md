# 🎬 Universal Video Downloader Bot

Telegram bot untuk download video/foto/audio dari berbagai platform tanpa watermark.

## ✅ Support Platform

| Platform | Video | Audio | Foto |
|----------|-------|-------|------|
| TikTok | ✅ No watermark | ✅ MP3 | - |
| Instagram | ✅ Reel | - | ✅ Carousel |
| YouTube | ✅ Shorts | - | - |
| Twitter/X | ✅ | - | - |
| Pinterest | ✅ | - | ✅ |

## 📌 Commands

- `/start` — Mulai bot
- `/help` — Bantuan
- `/audio <link>` — Download audio TikTok (MP3)

## 🚀 Setup

### 1. Buat Bot Telegram
1. Chat @BotFather
2. `/newbot` → ikuti instruksi
3. Simpan BOT_TOKEN

### 2. Install
```bash
pip install -r requirements.txt
```

### 3. Jalankan
```bash
export BOT_TOKEN="***"
python bot.py
```

## 🌐 Deploy 24/7 (Railway)
1. Push ke GitHub
2. Login https://railway.app
3. New Project → Deploy from GitHub
4. Add variable: `BOT_TOKEN` = token BotFather
5. Deploy ✅

## 💡 Cara Pakai
1. Copy link video dari TikTok/Instagram/YouTube/Twitter/Pinterest
2. Kirim link ke bot
3. Tunggu, media dikirim tanpa watermark!
