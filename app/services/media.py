import hashlib
from pathlib import Path

from aiogram import Bot

from app.core.config import Settings


async def download_telegram_file(bot: Bot, file_id: str, storage_key: str, settings: Settings) -> tuple[str, int]:
    destination = settings.media_root / storage_key
    destination.parent.mkdir(parents=True, exist_ok=True)
    telegram_file = await bot.get_file(file_id)
    await bot.download_file(telegram_file.file_path, destination=destination)
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
