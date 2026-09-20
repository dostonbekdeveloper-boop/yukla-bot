"""yt-dlp va FFmpeg yordamida Instagram/TikTok/YouTube'dan media yuklab olish."""

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import imageio_ffmpeg
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

# Telegram Bot API 50 MB dan katta fayl yubora olmaydi.
TELEGRAM_UPLOAD_LIMIT = 50_000_000

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi"}

URL_RE = re.compile(r"https?://[^\s<>\"']+")

PLATFORMS = {
    "instagram.com": "Instagram",
    "instagr.am": "Instagram",
    "tiktok.com": "TikTok",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
}

FORMAT_SELECTOR = (
    "bv*[height<=720][ext=mp4]+ba[ext=m4a]"
    "/b[height<=720][ext=mp4]"
    "/bv*[height<=720]+ba"
    "/b[height<=720]"
    "/b"
)


def _ffmpeg_path() -> str:
    path = imageio_ffmpeg.get_ffmpeg_exe()
    if os.name == "posix":
        # imageio-ffmpeg binary'si ba'zi o'rnatishlarda execute huquqisiz keladi.
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


FFMPEG = _ffmpeg_path()


@dataclass
class Media:
    video: Path | None
    audio: Path | None
    title: str
    uploader: str
    duration: int | None
    platform: str


def extract_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    if not match:
        return None
    # Gap oxiridagi nuqta/vergul URL'ga yopishib qolmasin.
    return match.group(0).rstrip(".,;:!?\"'<>")


def detect_platform(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for domain, name in PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def safe_filename(title: str, ext: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)[:80] or "media"
    return f"{cleaned}{ext}"


def _base_opts(workdir: Path) -> dict:
    opts = {
        "ffmpeg_location": FFMPEG,
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "restrictfilenames": True,
        "max_filesize": TELEGRAM_UPLOAD_LIMIT,
    }
    # Instagram login talab qila boshlasa, cookies faylini .env orqali bering.
    # Har yuklashda o'qiladi — load_dotenv() import tartibiga bog'liq bo'lmasligi uchun.
    if cookies := os.environ.get("COOKIES_FILE"):
        opts["cookiefile"] = cookies
    return opts


def download(url: str, workdir: Path) -> Media:
    """Videoni yuklaydi va undan mp3 chiqaradi. Sinxron — thread'da chaqiring."""
    opts = _base_opts(workdir)
    opts.update(
        {
            "format": FORMAT_SELECTOR,
            "merge_output_format": "mp4",
            # keepvideo — YoutubeDL parametri, postprocessor parametri EMAS.
            # Asl video fayl o'chirilmasin, shunda mp4 va mp3 ni bir so'rovda olamiz.
            "keepvideo": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
    )

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # Instagram carousel bir nechta entry qaytarishi mumkin — birinchisini olamiz.
    if info.get("entries"):
        info = next((e for e in info["entries"] if e), info)

    video = next((p for p in sorted(workdir.iterdir()) if p.suffix.lower() in VIDEO_EXTS), None)
    audio = next((p for p in sorted(workdir.iterdir()) if p.suffix.lower() == ".mp3"), None)

    return Media(
        video=video,
        audio=audio,
        title=(info.get("title") or info.get("id") or "media").strip(),
        uploader=(info.get("uploader") or info.get("channel") or "").strip(),
        # Ba'zi extractor'lar float qaytaradi, Telegram API esa butun son talab qiladi.
        duration=int(info["duration"]) if info.get("duration") else None,
        platform=detect_platform(url) or "media",
    )


def friendly_error(exc: DownloadError) -> str:
    msg = str(exc).lower()

    if "larger than max-filesize" in msg or "file is too large" in msg:
        return (
            "⚠️ Video Telegram'ning 50 MB limitidan katta.\n\n"
            "Afsuski, oddiy bot bundan katta faylni yubora olmaydi. "
            "Qisqaroq videoni urinib ko'ring."
        )
    if "unsupported url" in msg:
        return (
            "❌ Bu havola qo'llab-quvvatlanmaydi.\n\n"
            "Faqat Instagram, TikTok va YouTube havolalari bilan ishlayman."
        )
    if "login" in msg or "cookies" in msg or "authentication" in msg:
        return (
            "🔒 Bu kontentga kirish uchun login kerak.\n\n"
            "Instagram va ba'zi TikTok videolari endi faqat ro'yxatdan o'tgan "
            "foydalanuvchilarga ko'rsatiladi. Bunday havolalarni yuklab ololmayman."
        )
    if "private" in msg:
        return "🔒 Bu video shaxsiy (private) — faqat egasi ko'ra oladi."
    if "not available" in msg or "unavailable" in msg or "removed" in msg or "404" in msg:
        return "❌ Video topilmadi. U o'chirilgan yoki havola noto'g'ri bo'lishi mumkin."
    if "age" in msg and ("restrict" in msg or "confirm" in msg):
        return "🔞 Bu video yosh cheklovli (age-restricted) — yuklab olib bo'lmaydi."
    if "timed out" in msg or "timeout" in msg:
        return "⏱ Server javob bermadi. Bir ozdan so'ng qayta urinib ko'ring."

    return (
        "😔 Yuklab olishda xatolik yuz berdi.\n\n"
        "Havola ochiq va ishlayotganini tekshiring, so'ng qayta urinib ko'ring."
    )