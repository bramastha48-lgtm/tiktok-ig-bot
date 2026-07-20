# 🎬 TikTok & Instagram Video Downloader Bot

Telegram bot untuk download video TikTok (tanpa watermark) dan Instagram Reels.

## Fitur
- ✅ TikTok tanpa watermark (HD)
- ✅ Instagram Reels
- ✅ Auto detect link
- ✅ Kirim video langsung di Telegram

## Setup

### 1. Buat Bot Telegram
1. Chat @BotFather di Telegram
2. Kirim `/newbot`
3. Ikuti instruksi, dapatkan **BOT_TOKEN**

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Set Bot Token
```bash
# Linux/Mac
export BOT_TOKEN="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

# Windows
set BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
```

### 4. Jalankan
```bash
python bot.py
```

## Cara Pakai
1. Buka bot di Telegram
2. Kirim `/start`
3. Copy link TikTok/Instagram
4. Kirim link ke bot
5. Tunggu, video akan dikirim tanpa watermark!

## Contoh Link
- `https://www.tiktok.com/@user/video/123456`
- `https://vm.tiktok.com/xxxxxxx`
- `https://www.instagram.com/reel/xxxxxxx`
- `https://www.instagram.com/p/xxxxxxx`

## Deploy 24/7 (Opsional)
```bash
# Pakai nohup
nohup python bot.py &

# Atau pakai systemd (Linux)
# Buat file /etc/systemd/system/tiktok-bot.service
```
