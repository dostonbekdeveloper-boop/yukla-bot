import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
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
    "🔗 Havola yuboring — video (mp4) + musiqa (mp3) qaytaraman.\n"
    "Masalan: https://youtu.be/dQw4w9WgXcQ\n\n"
    "🎵 Yoki shunchaki qo'shiq nomini yozing — ro'yxat chiqadi, "
    "raqamni bosibsiz va mp3 keladi.\n"
    "Masalan: Yashashga qo'yinglar\n\n"
    "⚠️ Cheklovlar:\n"
    "• Telegram 50 MB dan katta faylni yuborishga ruxsat bermaydi\n"
    "• Shaxsiy (private) va login talab qiladigan videolar yuklanmaydi\n"
    "• Instagram ba'zan kirishni cheklab qo'yadi\n\n"
    "Buyruqlar: /start — bu xabar, /help — yordam, /search — qo'shiq qidirish"
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


async def _fetch_and_send(
    message: Message,
    bot: Bot,
    status: Message,
    label: str,
    empty_text: str,
    work: Callable[[Path], downloader.Media],
    action: str = ChatAction.UPLOAD_VIDEO,
) -> None:
    pulse_task = asyncio.create_task(pulse(bot, message.chat.id, action))
    try:
        async with DOWNLOAD_SLOTS:
            with TemporaryDirectory(prefix="mediabot_") as tmp:
                try:
                    media = await asyncio.to_thread(work, Path(tmp))
                except DownloadError as exc:
                    log.warning("Download failed for %s: %s", label, exc)
                    await status.edit_text(downloader.friendly_error(exc))
                    return
                except Exception:
                    log.exception("Unexpected error while processing %s", label)
                    await status.edit_text(
                        "😔 Kutilmagan xatolik yuz berdi.\n"
                        "Bir necha daqiqadan so'ng qayta urinib ko'ring."
                    )
                    return

                if not media.video and not media.audio:
                    await status.edit_text(empty_text)
                    return

                await status.edit_text("📤 Fayllar yuborilmoqda...")
                try:
                    await send_files(message, media)
                except Exception:
                    log.exception("Failed to send media for %s", label)
                    await message.answer("😔 Fayllarni yuborishda xatolik yuz berdi. Qayta urinib ko'ring.")
                with contextlib.suppress(Exception):
                    await status.delete()
    finally:
        pulse_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pulse_task


PAGE_SIZE = 8

# Har chat uchun oxirgi qidiruv: (so'rov, natijalar). Restart'da tozalanadi.
search_cache: dict[int, tuple[str, list[dict]]] = {}


def results_text(query: str, results: list[dict], page: int) -> str:
    lines = [query, ""]
    start = page * PAGE_SIZE
    for i, item in enumerate(results[start : start + PAGE_SIZE], start=start + 1):
        lines.append(f"{i}. {item['title']}  {fmt_duration(item['duration'])}")
    return "\n".join(lines)[:4096]


def results_keyboard(results: list[dict], page: int) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    chunk = results[start : start + PAGE_SIZE]
    rows = [
        [
            InlineKeyboardButton(text=str(start + j + 1), callback_data=f"song:{start + j}")
            for j in range(i, min(i + 5, len(chunk)))
        ]
        for i in range(0, len(chunk), 5)
    ]
    if start + PAGE_SIZE < len(results):
        rows.append([InlineKeyboardButton(text="➡️ Keyingi", callback_data=f"page:{page + 1}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def run_search(message: Message, query: str) -> None:
    status = await message.answer(f"🔎 «{query}» qidirilmoqda...")
    try:
        results = await asyncio.to_thread(downloader.search_songs, query)
    except DownloadError as exc:
        log.warning("Search failed for %r: %s", query, exc)
        await status.edit_text(downloader.friendly_error(exc))
        return
    except Exception:
        log.exception("Unexpected error while searching %r", query)
        await status.edit_text(
            "😔 Kutilmagan xatolik yuz berdi.\n"
            "Bir necha daqiqadan so'ng qayta urinib ko'ring."
        )
        return

    if not results:
        await status.edit_text(f"😔 «{query}» bo'yicha hech narsa topilmadi.")
        return

    search_cache[message.chat.id] = (query, results)
    with contextlib.suppress(Exception):
        await status.delete()
    await message.answer(
        results_text(query, results, 0),
        reply_markup=results_keyboard(results, 0),
    )


# handle_text'dan OLDIN ro'yxatdan o'tishi kerak: "/search ..." ham matn bo'lgani
# uchun aks holda F.text filtri uni ushlab qoladi.
@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer(
            "🔎 Qo'shiq nomini yozing.\n\n"
            "Masalan: /search Sevara Yorqinim\n"
            "Yoki shunchaki qo'shiq nomini yozing — komandasiz ham ishlaydi."
        )
        return
    await run_search(message, query)


@router.callback_query(F.data.startswith("page:"))
async def cb_page(call: CallbackQuery) -> None:
    cached = search_cache.get(call.message.chat.id)
    if not cached:
        await call.answer("Qidiruv eskirgan — qo'shiq nomini qayta yozing.")
        return
    page = int(call.data.split(":")[1])
    query, results = cached
    with contextlib.suppress(Exception):
        await call.message.edit_text(
            results_text(query, results, page),
            reply_markup=results_keyboard(results, page),
        )
    await call.answer()


@router.callback_query(F.data.startswith("song:"))
async def cb_song(call: CallbackQuery, bot: Bot) -> None:
    cached = search_cache.get(call.message.chat.id)
    idx = int(call.data.split(":")[1])
    if not cached or idx >= len(cached[1]):
        await call.answer("Qidiruv eskirgan — qo'shiq nomini qayta yozing.")
        return
    item = cached[1][idx]
    await call.answer()
    status = await call.message.answer(f"⏳ «{item['title']}» yuklanmoqda...")
    await _fetch_and_send(
        call.message,
        bot,
        status,
        item["id"],
        "😔 Audio topilmadi. Boshqa raqamni tanlab ko'ring.",
        lambda workdir: downloader.download_audio(item["id"], workdir),
        action=ChatAction.UPLOAD_AUDIO,
    )


@router.message(F.text | F.caption)
async def handle_text(message: Message, bot: Bot) -> None:
    text = message.text or message.caption or ""
    url = downloader.extract_url(text)

    # Havola bo'lmasa — xabarni qo'shiq nomi deb qabul qilamiz.
    if not url:
        query = text.strip()
        if len(query) < 2:
            await message.answer(
                "🔗 Havola yoki 🎵 qo'shiq nomini yuboring.\n\n"
                "Masalan: https://www.tiktok.com/@user/video/1234567890\n"
                "Yoki: Yashashga qo'yinglar"
            )
            return
        await run_search(message, query)
        return

    # Foydalanuvchi havolani rasm/video ostiga yozib yoki post'ni forward qilib yuborishi mumkin.
    platform = downloader.detect_platform(url)
    if not platform:
        await message.answer(
            "❌ Bu saytni qo'llab-quvvatlamayman.\n\n"
            "Faqat: Instagram, TikTok, YouTube."
        )
        return

    status = await message.answer(f"⏳ {platform} dan yuklab olinmoqda...\nBu bir necha soniya vaqt olishi mumkin.")
    await _fetch_and_send(
        message,
        bot,
        status,
        url,
        "😔 Havola ochildi, lekin ichidan video yoki audio topilmadi. "
        "Bu rasm bo'lishi yoki video o'chirilgan bo'lishi mumkin.",
        lambda workdir: downloader.download(url, workdir),
    )


@router.message()
async def handle_other(message: Message) -> None:
    """Matn/caption bo'lmagan xabarlar (rasm, stiker, ovozli) uchun."""
    await message.answer("🔗 Havola yoki 🎵 qo'shiq nomini yuboring.")


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