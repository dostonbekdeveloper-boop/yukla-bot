import asyncio
import contextlib
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message
from dotenv import load_dotenv
from yt_dlp.utils import DownloadError

import downloader

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Bepul Render instansida 512 MB RAM bor — bir vaqtda ko'p yuklama chiqarmaslik uchun.
DOWNLOAD_SLOTS = asyncio.Semaphore(int(os.environ.get("MAX_CONCURRENT", "2")))

router = Router()

WELCOME = (
    "🎬 Salom! Men @MediaYuklaBot — Instagram, TikTok va YouTube'dan "
    "video va musiqa yuklab beraman.\n\n"
    "Foydalanish: shunchaki havolani yuboring.\n"
    "Masalan: https://youtu.be/dQw4w9WgXcQ\n\n"
    "Har bir havola uchun ikkita fayl qaytaraman:\n"
    "🎬 Video (mp4)\n"
    "🎵 Musiqa (mp3)\n\n"
    "⚠️ Cheklovlar:\n"
    "• Telegram 50 MB dan katta faylni yuborishga ruxsat bermaydi\n"
    "• Shaxsiy (private) va login talab qiladigan videolar yuklanmaydi\n"
    "• Instagram ba'zan kirishni cheklab qo'yadi\n\n"
    "Buyruqlar: /start — bu xabar, /help — yordam"
)


def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def video_caption(media: downloader.Media) -> str:
    lines = [media.title]
    if media.uploader:
        lines.append(f"👤 {media.uploader}")
    meta = f"📡 {media.platform}"
    if duration := fmt_duration(media.duration):
        meta += f" • ⏱ {duration}"
    lines.append(meta)
    return "\n".join(lines)[:1024]


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(WELCOME)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(WELCOME)


async def pulse(bot: Bot, chat_id: int, action: str) -> None:
    """Telegram 'chat action' 5 sekunddan keyin o'chadi — yuklash davomida yangilab turamiz."""
    while True:
        with contextlib.suppress(Exception):
            await bot.send_chat_action(chat_id, action)
        await asyncio.sleep(4)


async def send_files(message: Message, media: downloader.Media) -> None:
    if media.video:
        size = media.video.stat().st_size
        if size <= downloader.TELEGRAM_UPLOAD_LIMIT:
            await message.answer_video(
                video=FSInputFile(media.video, filename=downloader.safe_filename(media.title, ".mp4")),
                caption=video_caption(media),
                duration=media.duration,
                supports_streaming=True,
            )
        else:
            await message.answer(
                f"⚠️ Video {size // 1_000_000} MB — Telegram'ning 50 MB limitidan katta, "
                "shuning uchun videoni yubora olmadim."
            )

    if media.audio:
        size = media.audio.stat().st_size
        if size <= downloader.TELEGRAM_UPLOAD_LIMIT:
            await message.answer_audio(
                audio=FSInputFile(media.audio, filename=downloader.safe_filename(media.title, ".mp3")),
                title=media.title[:64],
                performer=media.uploader[:64],
                duration=media.duration,
            )
        else:
            await message.answer(f"⚠️ Mp3 {size // 1_000_000} MB — 50 MB limitidan katta.")


@router.message(F.text | F.caption)
async def handle_link(message: Message, bot: Bot) -> None:
    # Foydalanuvchi havolani rasm/video ostiga yozib yoki post'ni forward qilib yuborishi mumkin.
    url = downloader.extract_url(message.text or message.caption or "")
    if not url:
        await message.answer(
            "🔗 Menga havola yuboring.\n\n"
            "Masalan: https://www.tiktok.com/@user/video/1234567890\n"
            "Yoki /start bosib yo'riqnoma ko'ring."
        )
        return

    platform = downloader.detect_platform(url)
    if not platform:
        await message.answer(
            "❌ Bu saytni qo'llab-quvvatlamayman.\n\n"
            "Faqat: Instagram, TikTok, YouTube."
        )
        return

    status = await message.answer(f"⏳ {platform} dan yuklab olinmoqda...\nBu bir necha soniya vaqt olishi mumkin.")
    pulse_task = asyncio.create_task(pulse(bot, message.chat.id, ChatAction.UPLOAD_VIDEO))

    try:
        async with DOWNLOAD_SLOTS:
            with TemporaryDirectory(prefix="mediabot_") as tmp:
                try:
                    media = await asyncio.to_thread(downloader.download, url, Path(tmp))
                except DownloadError as exc:
                    log.warning("Download failed for %s: %s", url, exc)
                    await status.edit_text(downloader.friendly_error(exc))
                    return
                except Exception:
                    # FFmpeg yo'q, disk to'lgan, tarmoq uzilgan va h.k.
                    log.exception("Unexpected error while downloading %s", url)
                    await status.edit_text(
                        "😔 Kutilmagan xatolik yuz berdi.\n"
                        "Bir necha daqiqadan so'ng qayta urinib ko'ring."
                    )
                    return

                if not media.video and not media.audio:
                    await status.edit_text(
                        "😔 Havola ochildi, lekin ichidan video yoki audio topilmadi. "
                        "Bu rasm bo'lishi yoki video o'chirilgan bo'lishi mumkin."
                    )
                    return

                await status.edit_text("📤 Fayllar yuborilmoqda...")
                try:
                    await send_files(message, media)
                except Exception:
                    log.exception("Failed to send media for %s", url)
                    await message.answer("😔 Fayllarni yuborishda xatolik yuz berdi. Qayta urinib ko'ring.")
                with contextlib.suppress(Exception):
                    await status.delete()
    finally:
        pulse_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pulse_task


@router.message()
async def handle_other(message: Message) -> None:
    """Matn/caption bo'lmagan xabarlar (rasm, stiker, ovozli) uchun."""
    await message.answer("🔗 Menga Instagram, TikTok yoki YouTube havolasini yuboring.")


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def run_http_server(port: int) -> None:
    """Render Web Service $PORT da javob berishini talab qiladi."""
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("HTTP server %s portda ishga tushdi", port)


async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    me = await bot.get_me()
    log.info("Bot ishga tushdi: @%s | FFmpeg: %s", me.username, downloader.FFMPEG)

    await run_http_server(int(os.environ.get("PORT", "8080")))

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
