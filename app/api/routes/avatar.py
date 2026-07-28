from io import BytesIO

from aiogram.exceptions import TelegramAPIError
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from sqlalchemy import select

from app.api.deps import CurrentUser, SessionDep
from app.bot.setup import bot
from app.db.models import Dialog

router = APIRouter(prefix="/api", tags=["user"])


@router.get("/dialogs/{dialog_id}/avatar", include_in_schema=False)
async def dialog_avatar(dialog_id: int, user: CurrentUser, session: SessionDep) -> Response:
    """Return the peer's current Telegram avatar for an owned dialog."""
    dialog = await session.scalar(
        select(Dialog).where(Dialog.id == dialog_id, Dialog.owner_user_id == user.id)
    )
    if not dialog or not dialog.peer_telegram_id:
        raise HTTPException(status_code=404, detail="Avatar not available")

    try:
        photos = await bot.get_user_profile_photos(dialog.peer_telegram_id, limit=1)
        if not photos.photos:
            raise HTTPException(status_code=404, detail="Avatar not available")

        photo = photos.photos[0][-1]
        tg_file = await bot.get_file(photo.file_id)
        if not tg_file.file_path:
            raise HTTPException(status_code=404, detail="Avatar file not available")

        output = BytesIO()
        await bot.download_file(tg_file.file_path, destination=output)
    except HTTPException:
        raise
    except TelegramAPIError as exc:
        raise HTTPException(status_code=404, detail="Avatar not available") from exc

    return Response(
        output.getvalue(),
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=900"},
    )
