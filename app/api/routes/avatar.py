from io import BytesIO

from aiogram.exceptions import TelegramAPIError
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select

from app.bot.setup import bot
from app.core.config import Settings, get_settings
from app.core.security import decode_token
from app.db.models import Dialog
from app.db.session import get_session

router = APIRouter(prefix="/api", tags=["user"])


@router.get("/avatar/{token}", include_in_schema=False)
async def dialog_avatar(token: str, session=Depends(get_session), settings: Settings = Depends(get_settings)) -> Response:
    try:
        subject = decode_token(token, "dialog_avatar", settings)
        user_id_text, dialog_id_text = subject.split(":", 1)
        user_id, dialog_id = int(user_id_text), int(dialog_id_text)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=403, detail="Invalid avatar token") from exc

    dialog = await session.scalar(select(Dialog).where(Dialog.id == dialog_id, Dialog.owner_user_id == user_id))
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

    return Response(output.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=900"})
