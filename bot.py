import asyncio
import logging
import os
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

DOWNLOADS_DIR = Path("downloads")
MAX_DURATION_SECONDS = 20 * 60
MAX_TELEGRAM_FILE_SIZE = 49 * 1024 * 1024
SUPPORTED_FORMATS = {"mp3", "m4a"}
YOUTUBE_EXTRACTOR_ARGS = {"youtube": {"player_client": ["android"]}}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


class AudioRequest(StatesGroup):
    waiting_for_format = State()


def build_format_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="MP3", callback_data="format:mp3"),
                InlineKeyboardButton(text="M4A", callback_data="format:m4a"),
            ]
        ]
    )


def is_youtube_url(text: str) -> bool:
    try:
        parsed = urlparse(text.strip())
    except ValueError:
        return False

    host = parsed.netloc.lower()
    return parsed.scheme in {"http", "https"} and (
        host == "youtu.be"
        or host.endswith(".youtu.be")
        or host == "youtube.com"
        or host.endswith(".youtube.com")
    )


def get_ffmpeg_path() -> str:
    configured_path = os.getenv("FFMPEG_LOCATION")
    ffmpeg_path = configured_path or shutil.which("ffmpeg")

    if not ffmpeg_path:
        raise RuntimeError("ffmpeg не найден. Установите ffmpeg и перезапустите бота.")

    return ffmpeg_path


def build_ydl_options(output_template: Path, audio_format: str) -> dict:
    ffmpeg_location = get_ffmpeg_path()

    # yt-dlp скачивает лучший доступный аудиопоток и передает его в ffmpeg.
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(output_template),
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "ffmpeg_location": ffmpeg_location,
        # Android client часто избегает HTTP 403 на YouTube SABR-потоках.
        "extractor_args": YOUTUBE_EXTRACTOR_ARGS,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "192" if audio_format == "mp3" else "0",
            }
        ],
    }

    return options


def check_video_availability(url: str) -> dict:
    with YoutubeDL(
        {
            "quiet": False,
            "no_warnings": False,
            "noplaylist": True,
            "extractor_args": YOUTUBE_EXTRACTOR_ARGS,
        }
    ) as ydl:
        info = ydl.extract_info(url, download=False)

    # Проверяем длительность до скачивания, чтобы не тратить время и место на слишком длинные видео.
    duration = info.get("duration")
    if duration and duration > MAX_DURATION_SECONDS:
        raise ValueError("Видео длиннее 20 минут. Отправьте ссылку на более короткое видео.")

    return info


def find_converted_file(work_dir: Path, file_id: str, audio_format: str) -> Path:
    expected_file = work_dir / f"{file_id}.{audio_format}"
    if expected_file.exists():
        return expected_file

    matches = list(work_dir.glob(f"{file_id}*.{audio_format}"))
    if matches:
        return matches[0]

    raise RuntimeError("Не удалось найти готовый аудиофайл после конвертации.")


def download_and_convert_audio(url: str, audio_format: str) -> Path:
    if audio_format not in SUPPORTED_FORMATS:
        raise ValueError("Неподдерживаемый формат аудио.")

    # Каждый запрос получает отдельную временную папку, которую потом можно удалить целиком.
    DOWNLOADS_DIR.mkdir(exist_ok=True)
    work_dir = DOWNLOADS_DIR / str(uuid.uuid4())
    work_dir.mkdir(parents=True, exist_ok=True)

    file_id = "audio"
    output_template = work_dir / f"{file_id}.%(ext)s"

    try:
        check_video_availability(url)

        with YoutubeDL(build_ydl_options(output_template, audio_format)) as ydl:
            ydl.download([url])

        result_file = find_converted_file(work_dir, file_id, audio_format)
        if result_file.stat().st_size > MAX_TELEGRAM_FILE_SIZE:
            raise ValueError("Итоговый файл слишком большой для отправки в Telegram.")

        return result_file
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def cleanup_file_parent(file_path: Path) -> None:
    shutil.rmtree(file_path.parent, ignore_errors=True)


async def run_blocking_download(url: str, audio_format: str) -> Path:
    return await asyncio.to_thread(download_and_convert_audio, url, audio_format)


async def start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Отправьте ссылку на YouTube-видео, на использование которого у вас есть права, "
        "разрешение автора или свободная лицензия.\n\n"
        "После этого выберите формат аудио: MP3 или M4A."
    )


async def youtube_link_handler(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not is_youtube_url(url):
        await message.answer("Это не похоже на ссылку YouTube. Отправьте ссылку youtube.com или youtu.be.")
        return

    await state.update_data(youtube_url=url)
    await state.set_state(AudioRequest.waiting_for_format)
    await message.answer("Выберите формат аудио:", reply_markup=build_format_keyboard())


async def invalid_text_handler(message: Message) -> None:
    await message.answer("Отправьте корректную ссылку на YouTube-видео.")


async def format_callback_handler(callback: CallbackQuery, state: FSMContext) -> None:
    audio_format = (callback.data or "").replace("format:", "", 1)
    data = await state.get_data()
    url = data.get("youtube_url")

    if audio_format not in SUPPORTED_FORMATS:
        await callback.answer("Неподдерживаемый формат.", show_alert=True)
        return

    if not url:
        await callback.message.answer("Ссылка не найдена. Отправьте YouTube-ссылку заново.")
        await state.clear()
        await callback.answer()
        return

    await callback.answer()
    await callback.message.answer("Обрабатываю видео, это может занять немного времени...")

    result_file = None
    try:
        result_file = await run_blocking_download(url, audio_format)
        audio = FSInputFile(result_file)
        await callback.message.answer_audio(audio=audio)
        await state.clear()
    except DownloadError:
        logger.exception("yt-dlp не смог скачать аудио")
        await callback.message.answer("Не удалось скачать аудио. Видео недоступно или ссылка некорректна.")
    except RuntimeError as error:
        await callback.message.answer(str(error))
    except ValueError as error:
        await callback.message.answer(str(error))
    except TelegramBadRequest as error:
        if "too large" in error.message.lower():
            await callback.message.answer("Файл слишком большой для отправки в Telegram.")
        else:
            await callback.message.answer(f"Telegram не принял файл: {error.message}")
    except Exception:
        logger.exception("Неожиданная ошибка при обработке видео")
        await callback.message.answer("Произошла ошибка при скачивании или конвертации. Попробуйте другое видео.")
    finally:
        # Пользовательские файлы не хранятся постоянно.
        if result_file:
            cleanup_file_parent(result_file)


def create_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.register(start_handler, CommandStart())
    dp.message.register(youtube_link_handler, F.text.func(is_youtube_url))
    dp.message.register(invalid_text_handler, F.text)
    dp.callback_query.register(format_callback_handler, F.data.startswith("format:"))
    return dp


async def main() -> None:
    if not BOT_TOKEN:
        print("Ошибка: переменная окружения BOT_TOKEN не задана. Добавьте BOT_TOKEN в .env или Environment Variables.")
        return

    DOWNLOADS_DIR.mkdir(exist_ok=True)

    bot = Bot(token=BOT_TOKEN)
    dp = create_dispatcher()

    print("Бот запущен в режиме polling.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
