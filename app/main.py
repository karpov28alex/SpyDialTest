import asyncio
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from app.api.routes.admin import router as admin_router
from app.api.routes.admin_analytics import router as admin_analytics_router
from app.api.routes.admin_dialogs import router as admin_dialogs_router
from app.api.routes.admin_explorer import router as admin_explorer_router
from app.api.routes.admin_funnel import router as admin_funnel_router
from app.api.routes.admin_growth import router as admin_growth_router
from app.api.routes.admin_monetization import router as admin_monetization_router
from app.api.routes.admin_user360 import router as admin_user360_router
from app.api.routes.auth import router as auth_router
from app.api.routes.avatar import router as avatar_router
from app.api.routes.user import router as user_router
from app.api.routes.user_intelligence import router as user_intelligence_router
from app.api.routes.webhook import router as webhook_router
from app.api.routes.webhook_compat import router as webhook_compat_router
from app.bot import access_funnel as access_funnel_module
from app.bot import admin_console as admin_console_module
from app.bot.setup import dispatcher
from app.bot.user_intelligence import router as user_intelligence_bot_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import engine
from app.services.funnel_scheduler import funnel_scheduler_loop

settings = get_settings()
configure_logging()
dispatcher.include_router(user_intelligence_bot_router)

_original_user_menu = admin_console_module.user_menu


def _user_menu_with_stats(admin: bool) -> InlineKeyboardMarkup:
    original = _original_user_menu(admin)
    rows = [list(row) for row in original.inline_keyboard]
    stats_row = [InlineKeyboardButton(text="📊 Статистика", callback_data="intel:summary")]
    insert_at = 1 if rows else 0
    rows.insert(insert_at, stats_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


admin_console_module.user_menu = _user_menu_with_stats
access_funnel_module.user_menu = _user_menu_with_stats


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.media_root.mkdir(parents=True, exist_ok=True)
    funnel_task = asyncio.create_task(funnel_scheduler_loop(), name="access-funnel-scheduler")
    try:
        yield
    finally:
        funnel_task.cancel()
        with suppress(asyncio.CancelledError):
            await funnel_task
        await engine.dispose()


app = FastAPI(title="Dialog Spy API", version=settings.app_version, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
)
app.include_router(auth_router)
app.include_router(user_router)
app.include_router(user_intelligence_router)
app.include_router(avatar_router)
app.include_router(admin_dialogs_router)
app.include_router(admin_router)
app.include_router(admin_explorer_router)
app.include_router(admin_monetization_router)
app.include_router(admin_analytics_router)
app.include_router(admin_growth_router)
app.include_router(admin_user360_router)
app.include_router(admin_funnel_router)
app.include_router(webhook_router)
app.include_router(webhook_compat_router)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    correlation_id = request.headers.get("x-correlation-id") or uuid.uuid4().hex
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    if request.url.path in {"/app", "/admin"}:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(Exception)
async def unhandled_error(request: Request, _: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "Внутренняя ошибка",
                "details": {},
                "correlation_id": request.headers.get("x-correlation-id", "unknown"),
            }
        },
    )


@app.get("/health/live")
async def live() -> dict:
    return {"status": "ok", "version": settings.app_version, "git_sha": settings.git_sha}


@app.get("/health/ready")
async def ready() -> dict:
    async with engine.connect() as connection:
        await connection.execute(text("select 1"))
    redis = Redis.from_url(settings.redis_url)
    await redis.ping()
    await redis.aclose()
    return {"status": "ready"}


@app.get("/app", include_in_schema=False)
async def mini_app() -> FileResponse:
    return FileResponse("app/static/miniapp/index.html", headers={"Cache-Control": "no-store"})


@app.get("/app/app.js", include_in_schema=False)
async def mini_app_js() -> FileResponse:
    return FileResponse(
        "app/static/miniapp/app.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/app/style.css", include_in_schema=False)
async def mini_app_css() -> FileResponse:
    return FileResponse(
        "app/static/miniapp/style.css",
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/app/{asset_path:path}", include_in_schema=False)
async def mini_app_asset(asset_path: str):
    path = Path("app/static/miniapp") / asset_path
    if not path.is_file():
        return FileResponse("app/static/miniapp/index.html", headers={"Cache-Control": "no-store"})
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.get("/admin", include_in_schema=False)
async def admin_app() -> FileResponse:
    return FileResponse("app/static/admin/unified.html", headers={"Cache-Control": "no-store"})


@app.get("/admin/{asset_path:path}", include_in_schema=False)
async def admin_asset(asset_path: str):
    path = Path("app/static/admin") / asset_path
    if not path.is_file():
        return FileResponse("app/static/admin/unified.html", headers={"Cache-Control": "no-store"})
    return FileResponse(path, headers={"Cache-Control": "no-store"})
