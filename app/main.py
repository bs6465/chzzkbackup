from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, config
from .chzzk_api import test_tokens as test_chzzk_tokens
from .db import db
from .encoder import EncodeWorker
from .logger import logger
from .maintenance import MaintenanceWorker
from .media_library import MediaIndexer, load_chat_rows, rename_media_item, within_final_root
from .platforms import get_channel_name, normalize_channel_input, platform_label, supported_platforms
from .recorder import RecorderSupervisor
from .twitcasting_api import test_token as test_twitcasting_token
from .utils import (
    disk_status,
    ensure_storage_dirs,
    format_bytes,
    format_duration,
    kst_display,
    mask_secret,
    sanitize_name,
)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.filters["bytes"] = format_bytes
templates.env.filters["duration"] = format_duration
templates.env.filters["kst_datetime"] = kst_display
templates.env.filters["platform_label"] = platform_label

recorder = RecorderSupervisor()
encoder = EncodeWorker()
maintenance = MaintenanceWorker()
media_indexer = MediaIndexer()


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    config.FINAL_ROOT.mkdir(parents=True, exist_ok=True)
    recovered_sessions = db.recover_interrupted_sessions()
    for session in db.query_all(
        "SELECT * FROM recording_sessions WHERE status IN ('queued','failed') AND EXISTS (SELECT 1 FROM recording_segments WHERE session_id=recording_sessions.id)"
    ):
        try:
            recorder._merge_segment_chats(
                db.recording_segments(int(session["id"])),
                Path(session["chat_jsonl_path"]), Path(session["chat_csv_path"]),
            )
        except Exception as exc:
            logger.warning("Recovered chat merge failed for session %s: %s", session["id"], exc)
    recovered_jobs = db.recover_interrupted_encode_jobs()
    if any(recovered_sessions.values()):
        logger.warning("Recovered interrupted recording session(s): %s", recovered_sessions)
    if any(recovered_jobs.values()):
        logger.warning("Recovered interrupted encode job(s): %s", recovered_jobs)
    recorder.start()
    encoder.start()
    maintenance.start()
    media_indexer.start()
    logger.info("ChzzkBackup started")
    try:
        yield
    finally:
        await recorder.stop()
        await encoder.stop()
        await maintenance.stop()
        await media_indexer.stop()
        logger.info("ChzzkBackup stopped")


app = FastAPI(title="ChzzkBackup", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/health")
async def health():
    storage_ok = config.FINAL_ROOT.exists() and config.TEMP_DIR.exists()
    return {
        "ok": storage_ok,
        "version": __version__,
        "workers": {
            "recorder": bool(recorder._task and not recorder._task.done()),
            "encoder": bool(encoder._task and not encoder._task.done()),
            "maintenance": bool(maintenance._task and not maintenance._task.done()),
            "indexer": bool(media_indexer._task and not media_indexer._task.done()),
        },
        "storage": {"ok": storage_ok},
        "indexer": media_indexer.status(),
    }


def status_context() -> dict:
    tokens = db.get_tokens()
    twitcasting_token = db.get_twitcasting_token()
    return {
        "channels": db.get_channels(),
        "active_sessions": db.active_sessions(),
        "recent_sessions": db.recent_sessions(),
        "encode_jobs": db.encode_jobs(),
        "tokens_masked": {
            "NID_SES": mask_secret(tokens.get("NID_SES")),
            "NID_AUT": mask_secret(tokens.get("NID_AUT")),
        },
        "twitcasting_token_masked": mask_secret(twitcasting_token),
        "platforms": supported_platforms(),
        "temp_disk": disk_status(config.TEMP_DIR),
        "final_disk": disk_status(config.FINAL_ROOT),
        "config": config,
        "media_summary": db.media_summary(),
        "indexer_status": media_indexer.status(),
        "version": __version__,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {**status_context(), "logs": db.recent_logs(30)},
    )


@app.get("/library", response_class=HTMLResponse)
async def library(
    request: Request,
    q: str = "",
    platform: str = "",
    channel: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "desc",
    page: int = 1,
):
    page = max(1, page)
    items, total = db.list_media(
        q=q.strip(), platform=platform, channel=channel, date_from=date_from,
        date_to=date_to, sort=sort, page=page,
    )
    pages = max(1, (total + config.MEDIA_PAGE_SIZE - 1) // config.MEDIA_PAGE_SIZE)
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "items": items, "total": total, "page": page, "pages": pages,
            "q": q, "platform": platform, "channel": channel,
            "date_from": date_from, "date_to": date_to, "sort": sort,
            "channels": db.media_channels(), "platforms": supported_platforms(),
            "version": __version__,
        },
    )


def available_media_or_404(media_id: int) -> dict:
    item = db.get_media_item(media_id)
    if not item or item.get("status") != "available":
        raise HTTPException(status_code=404, detail="Media not found")
    path = Path(item["video_path"])
    if not within_final_root(path) or not path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    return item


@app.get("/library/{media_id}", response_class=HTMLResponse)
async def watch_media(request: Request, media_id: int):
    item = available_media_or_404(media_id)
    return templates.TemplateResponse(
        request, "player.html", {"item": item, "version": __version__}
    )


@app.get("/media/{media_id}/video")
async def media_video(media_id: int):
    item = available_media_or_404(media_id)
    return FileResponse(Path(item["video_path"]), media_type="video/mp4")


@app.get("/media/{media_id}/download")
async def media_download(media_id: int):
    item = available_media_or_404(media_id)
    path = Path(item["video_path"])
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/media/{media_id}/thumbnail")
async def media_thumbnail(media_id: int):
    item = available_media_or_404(media_id)
    raw = item.get("thumbnail_path")
    if not raw or not Path(raw).is_file():
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(Path(raw), media_type="image/webp")


@app.get("/media/{media_id}/chat")
async def media_chat(media_id: int):
    item = available_media_or_404(media_id)
    return JSONResponse(load_chat_rows(item))


@app.post("/media/{media_id}/rename")
async def rename_media(media_id: int, title: str = Form(...)):
    available_media_or_404(media_id)
    try:
        renamed = rename_media_item(media_id, title)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not renamed:
        raise HTTPException(status_code=404, detail="Media not found")
    logger.info("Media renamed: %s -> %s", media_id, renamed["title"])
    return RedirectResponse(f"/library/{media_id}", status_code=303)


@app.get("/partials/status", response_class=HTMLResponse)
async def partial_status(request: Request):
    return templates.TemplateResponse(request, "partials/status.html", status_context())


@app.get("/partials/logs", response_class=HTMLResponse)
async def partial_logs(request: Request):
    return templates.TemplateResponse(request, "partials/logs.html", {"logs": db.recent_logs(80)})


@app.post("/channels")
async def create_channel(channel_id: str = Form(...), platform: str = Form("chzzk")):
    try:
        channel = normalize_channel_input(platform, channel_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    tokens = db.get_tokens()
    twitcasting_token = db.get_twitcasting_token()
    name = await get_channel_name(channel, tokens, twitcasting_token) or channel.display_id
    name = sanitize_name(name, channel.display_id)
    ensure_storage_dirs(name, channel.platform)
    db.upsert_channel(
        channel.internal_id,
        name,
        active=True,
        platform=channel.platform,
        display_id=channel.display_id,
    )
    logger.info("Channel registered: %s %s (%s)", channel.platform, name, channel.display_id)
    return RedirectResponse("/", status_code=303)


@app.post("/channels/{channel_id}/toggle")
async def toggle_channel(channel_id: str):
    channel = db.get_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    db.set_channel_active(channel_id, not bool(channel["active"]))
    logger.info("Channel toggled: %s -> %s", channel_id, not bool(channel["active"]))
    return RedirectResponse("/", status_code=303)


@app.post("/channels/{channel_id}/delete")
async def delete_channel(channel_id: str):
    channel = db.get_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    db.delete_channel(channel_id)
    logger.info("Channel deleted: %s", channel_id)
    return RedirectResponse("/", status_code=303)


@app.post("/channels/{channel_id}/rename")
async def rename_channel(channel_id: str, name: str = Form(...)):
    channel = db.get_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    safe_name = sanitize_name(name, channel_id)
    ensure_storage_dirs(safe_name, str(channel.get("platform") or "chzzk"))
    db.rename_channel(channel_id, safe_name)
    logger.info("Channel renamed: %s -> %s", channel_id, safe_name)
    return RedirectResponse("/", status_code=303)


@app.post("/tokens")
async def save_tokens(nid_ses: str = Form(""), nid_aut: str = Form("")):
    db.set_tokens(nid_ses, nid_aut)
    logger.info("Naver tokens updated")
    return RedirectResponse("/", status_code=303)


@app.post("/tokens/test")
async def tokens_test(channel_id: str = Form("")):
    ok, message = await test_chzzk_tokens(db.get_tokens(), channel_id.strip() or None)
    level = "info" if ok else "warning"
    getattr(logger, level)("Token test: %s", message)
    return RedirectResponse("/", status_code=303)


@app.post("/tokens/twitcasting")
async def save_twitcasting_token(access_token: str = Form("")):
    db.set_twitcasting_token(access_token)
    logger.info("TwitCasting token updated")
    return RedirectResponse("/", status_code=303)


@app.post("/tokens/twitcasting/test")
async def twitcasting_token_test(screen_id: str = Form("")):
    normalized_screen_id = None
    if screen_id.strip():
        try:
            normalized_screen_id = normalize_channel_input("twitcasting", screen_id).display_id
        except ValueError as exc:
            logger.warning("TwitCasting token test: %s", exc)
            return RedirectResponse("/", status_code=303)
    ok, message = await test_twitcasting_token(db.get_twitcasting_token(), normalized_screen_id)
    level = "info" if ok else "warning"
    getattr(logger, level)("TwitCasting token test: %s", message)
    return RedirectResponse("/", status_code=303)


@app.post("/recordings/{session_id}/stop")
async def stop_recording(session_id: int):
    stopped = await recorder.stop_session(session_id)
    if not stopped:
        raise HTTPException(status_code=404, detail="Active recording not found")
    return RedirectResponse("/", status_code=303)


@app.post("/recordings/{session_id}/rename")
async def rename_recording(session_id: int, title: str = Form(...)):
    session = db.rename_session_title(session_id, title)
    if not session:
        raise HTTPException(status_code=404, detail="Recording session not found")
    logger.info("Recording renamed: session %s -> %s", session_id, session["live_title"])
    return RedirectResponse("/", status_code=303)
