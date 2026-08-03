import hashlib
import shutil
from pathlib import Path

from aiogram import Bot

from app.core.config import Settings


async def copy_or_download_telegram_file(
    bot: Bot,
    file_path: str,
    destination: Path,
    settings: Settings,
) -> None:
    """Store a Telegram file from cloud API or a shared local Bot API volume."""
    destination.parent.mkdir(parents=True, exist_ok=True)

    if settings.telegram_api_is_local:
        source = Path(file_path)
        if source.is_file():
            shutil.copyfile(source, destination)
            return

    await bot.download_file(file_path, destination=destination)


async def download_telegram_file(bot: Bot, file_id: str, storage_key: str, settings: Settings) -> tuple[str, int]:
    destination = settings.media_root / storage_key
    telegram_file = await bot.get_file(file_id)
    if not telegram_file.file_path:
        raise RuntimeError("Telegram getFile response does not contain file_path")

    await copy_or_download_telegram_file(
        bot,
        telegram_file.file_path,
        destination,
        settings,
    )

    digest = hashlib.sha256()
    size = 0
    with destination.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def safe_media_path(settings: Settings, storage_key: str) -> Path:
    root = settings.media_root.resolve()
    candidate = (root / storage_key).resolve()
    if root not in candidate.parents:
        raise ValueError("Unsafe storage key")
    return candidate
