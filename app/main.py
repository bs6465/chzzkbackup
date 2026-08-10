from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import __version__, config
from .bookmarks import (
    BookmarkNotFoundError,
    BookmarkService,
    BookmarkValidationError,
    InactiveRecordingError,
)
from .chzzk_api import test_tokens as test_chzzk_tokens
from .clipper import InsufficientClipStorageError, clip_worker
from .db import db
from .encoder import EncodeWorker
from .logger import logger
from .maintenance import MaintenanceWorker
from .media_library import MediaIndexer, load_chat_rows, rename_media_item, within_final_root
from .platforms import get_channel_name, normalize_channel_input, platform_label, supported_platforms
from .recorder import RecorderSupervisor
from .retention import parse_datetime, retention
from .twitcasting_api import test_token as test_twitcasting_token
from .utils import (
    disk_status,
    ensure_storage_dirs,
    format_bytes,
    format_duration,
    kst_display,
    mask_secret,
    sanitize_name,
    utc_now_iso,
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
CLIP_TIME_RE = re.compile(r"^(?P<hours>\d+):(?P<minutes>[0-5]\d):(?P<seconds>[0-5]\d)$")


class LiveBookmarkCreate(BaseModel):
    content: str = ""


class MediaBookmarkCreate(BaseModel):
    display_offset_seconds: float
    content: str = ""


class BookmarkUpdate(BaseModel):
    display_offset_seconds: float | None = None
    content: str | None = None
    use_current_live_time: bool = False


class BookmarkShiftUpdate(BaseModel):
    shift_seconds: float


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    config.CLIP_TEMP_DIR.mkdir(parents=True, exist_ok=True)
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
    recovered_clip_jobs = clip_worker.recover_interrupted_jobs()
    if any(recovered_sessions.values()):
        logger.warning("Recovered interrupted recording session(s): %s", recovered_sessions)
    if any(recovered_jobs.values()):
        logger.warning("Recovered interrupted encode job(s): %s", recovered_jobs)
    if any(recovered_clip_jobs.values()):
        logger.warning("Recovered interrupted clip job(s): %s", recovered_clip_jobs)
    recorder.start()
    encoder.start()
    clip_worker.start()
    maintenance.start()
    media_indexer.start()
    logger.info("ChzzkBackup started")
    try:
        yield
    finally:
        await recorder.stop()
        await encoder.stop()
        await clip_worker.stop()
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
            "clipper": bool(clip_worker._task and not clip_worker._task.done()),
        },
        "storage": {"ok": storage_ok},
        "indexer": media_indexer.status(),
        "clipper": clip_worker.status(),
        "retention": retention.status(),
    }


def status_context() -> dict:
    tokens = db.get_tokens()
    twitcasting_token = db.get_twitcasting_token()
    return {
        "channels": db.get_channels(),
        "active_sessions": db.active_sessions(),
        "recent_sessions": db.recent_completed_sessions(),
        "encode_jobs": db.operational_encode_jobs(),
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
        "clip_jobs": clip_worker.jobs(limit=10),
        "retention_status": retention.status(),
        "version": __version__,
    }


def retention_context() -> dict:
    snapshot = retention.evaluate()
    policies = retention.policies()
    for policy in policies:
        policy["max_gib"] = (
            f"{int(policy['max_bytes']) / 1024**3:g}"
            if policy.get("max_bytes") is not None
            else ""
        )
        policy["preview"] = snapshot["by_policy"].get(
            str(policy["policy_key"]),
            {"candidate_count": 0, "candidate_bytes": 0, "deferred_count": 0},
        )
    return {
        "retention_policies": policies,
        "retention_snapshot": snapshot,
        "trash_items": retention.trash_items(),
        "deletion_history": retention.recent_deletions(),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            **status_context(),
            **retention_context(),
            "logs": db.recent_logs(30),
        },
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
    retention_filter: str = "",
    page: int = 1,
):
    page = max(1, page)
    all_items, _ = db.list_media(
        q=q.strip(), platform=platform, channel=channel, date_from=date_from,
        date_to=date_to, sort=sort, page=1, page_size=1_000_000,
    )
    snapshot = retention.evaluate()
    filtered_items = []
    for item in all_items:
        state = snapshot["items"].get(int(item["id"]), {})
        item["retention"] = state
        if retention_filter == "inherit" and state.get("override") != "inherit":
            continue
        if retention_filter == "forever" and state.get("category") != "forever":
            continue
        if retention_filter == "scheduled" and state.get("category") != "scheduled":
            continue
        if retention_filter == "expiring" and state.get("category") != "expiring" and not state.get("candidate"):
            continue
        filtered_items.append(item)
    total = len(filtered_items)
    offset = (page - 1) * config.MEDIA_PAGE_SIZE
    items = filtered_items[offset : offset + config.MEDIA_PAGE_SIZE]
    pages = max(1, (total + config.MEDIA_PAGE_SIZE - 1) // config.MEDIA_PAGE_SIZE)
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            "items": items, "total": total, "page": page, "pages": pages,
            "q": q, "platform": platform, "channel": channel,
            "date_from": date_from, "date_to": date_to, "sort": sort,
            "retention_filter": retention_filter,
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
    item["retention"] = retention.evaluate()["items"].get(media_id, {})
    return templates.TemplateResponse(
        request,
        "player.html",
        {
            "item": item,
            "clip_jobs": clip_worker.jobs(media_id=media_id),
            "version": __version__,
        },
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


def bookmark_result(operation):
    try:
        return operation()
    except BookmarkNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except BookmarkValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except InactiveRecordingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/recordings/{session_id}/bookmarks")
async def recording_bookmarks(session_id: int):
    return bookmark_result(lambda: BookmarkService(db).live_collection(session_id))


@app.post("/recordings/{session_id}/bookmarks", status_code=201)
async def create_recording_bookmark(session_id: int, payload: LiveBookmarkCreate):
    return bookmark_result(
        lambda: BookmarkService(db).create_live_bookmark(session_id, payload.content)
    )


@app.get("/media/{media_id}/bookmarks")
async def media_bookmarks(media_id: int):
    available_media_or_404(media_id)
    return bookmark_result(lambda: BookmarkService(db).media_collection(media_id))


@app.post("/media/{media_id}/bookmarks", status_code=201)
async def create_media_bookmark(media_id: int, payload: MediaBookmarkCreate):
    available_media_or_404(media_id)
    return bookmark_result(
        lambda: BookmarkService(db).create_media_bookmark(
            media_id, payload.display_offset_seconds, payload.content
        )
    )


@app.patch("/bookmarks/{bookmark_id}")
async def update_bookmark(bookmark_id: int, payload: BookmarkUpdate):
    fields = payload.model_fields_set
    return bookmark_result(
        lambda: BookmarkService(db).update_bookmark(
            bookmark_id,
            content=payload.content,
            content_provided="content" in fields,
            display_offset_seconds=payload.display_offset_seconds,
            offset_provided="display_offset_seconds" in fields,
            use_current_live_time=payload.use_current_live_time,
        )
    )


@app.delete("/bookmarks/{bookmark_id}")
async def delete_bookmark(bookmark_id: int):
    return bookmark_result(lambda: BookmarkService(db).delete_bookmark(bookmark_id))


@app.put("/media/{media_id}/bookmark-shift")
async def update_media_bookmark_shift(media_id: int, payload: BookmarkShiftUpdate):
    available_media_or_404(media_id)
    return bookmark_result(
        lambda: BookmarkService(db).set_media_shift(media_id, payload.shift_seconds)
    )


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


def optional_positive_int(value: str, label: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label} must be an integer") from exc
    if parsed <= 0:
        raise HTTPException(status_code=400, detail=f"{label} must be positive")
    return parsed


def optional_gib_bytes(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail="Maximum GiB must be a number") from exc
    if parsed <= 0:
        raise HTTPException(status_code=400, detail="Maximum GiB must be positive")
    return int(parsed * Decimal(1024**3))


def parse_policy_form(
    retention_days: str,
    max_items: str,
    max_gib: str,
) -> tuple[int | None, int | None, int | None]:
    return (
        optional_positive_int(retention_days, "Retention days"),
        optional_positive_int(max_items, "Maximum item count"),
        optional_gib_bytes(max_gib),
    )


@app.post("/retention/policies/preview", response_class=HTMLResponse)
async def preview_retention_policy(
    request: Request,
    policy_key: str = Form(...),
    retention_days: str = Form(""),
    max_items: str = Form(""),
    max_gib: str = Form(""),
):
    days, items, max_bytes = parse_policy_form(
        retention_days, max_items, max_gib
    )
    preview = retention.preview_policy(
        policy_key,
        retention_days=days,
        max_items=items,
        max_bytes=max_bytes,
    )
    return templates.TemplateResponse(
        request,
        "partials/retention_preview.html",
        {"preview": preview},
    )


@app.post("/retention/policies")
async def save_retention_policy(
    policy_key: str = Form(...),
    retention_days: str = Form(""),
    max_items: str = Form(""),
    max_gib: str = Form(""),
):
    days, items, max_bytes = parse_policy_form(
        retention_days, max_items, max_gib
    )
    try:
        retention.set_policy(
            policy_key,
            retention_days=days,
            max_items=items,
            max_bytes=max_bytes,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("Retention policy updated: %s", policy_key)
    return RedirectResponse("/#retention-settings", status_code=303)


@app.post("/retention/run")
async def run_retention_now():
    result = await asyncio.to_thread(retention.run_cleanup)
    logger.info("Manual retention cleanup completed: %s", result)
    return RedirectResponse("/#retention-settings", status_code=303)


@app.post("/media/retention")
async def update_media_retention_bulk(
    media_ids: list[int] = Form(...),
    retention_override: str = Form(...),
    expires_on: str = Form(""),
):
    try:
        retention.set_media_override(
            media_ids,
            retention_override,
            expires_on.strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "Bulk media retention updated: %s item(s) -> %s",
        len(media_ids),
        retention_override,
    )
    return RedirectResponse("/library", status_code=303)


@app.post("/media/{media_id}/retention")
async def update_media_retention(
    media_id: int,
    retention_override: str = Form(...),
    expires_on: str = Form(""),
):
    try:
        retention.set_media_override(
            [media_id],
            retention_override,
            expires_on.strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("Media retention updated: %s -> %s", media_id, retention_override)
    return RedirectResponse(f"/library/{media_id}", status_code=303)


@app.post("/retention/trash/{media_id}/restore")
async def restore_retention_trash(media_id: int):
    try:
        retention.restore_media(media_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse("/#retention-settings", status_code=303)


@app.post("/retention/trash/purge-all")
async def purge_all_retention_trash():
    retention.purge_all()
    return RedirectResponse("/#retention-settings", status_code=303)


@app.post("/retention/trash/{media_id}/purge")
async def purge_retention_trash(media_id: int):
    if not retention.purge_media(media_id):
        raise HTTPException(status_code=404, detail="Trash item not found")
    return RedirectResponse("/#retention-settings", status_code=303)


def parse_clip_time(value: str) -> int:
    match = CLIP_TIME_RE.fullmatch(value.strip())
    if not match:
        raise HTTPException(status_code=400, detail="Time must use HH:MM:SS")
    return (
        int(match["hours"]) * 3600
        + int(match["minutes"]) * 60
        + int(match["seconds"])
    )


def safe_next_url(value: str, fallback: str = "/") -> str:
    return value if value.startswith("/") and not value.startswith("//") else fallback


@app.post("/media/{media_id}/clips")
async def create_clip(
    media_id: int,
    start_time: str = Form(...),
    end_time: str = Form(...),
):
    start_seconds = parse_clip_time(start_time)
    end_seconds = parse_clip_time(end_time)
    try:
        await clip_worker.create_job(media_id, start_seconds, end_seconds)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InsufficientClipStorageError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(f"/library/{media_id}#clip-controls", status_code=303)


@app.get("/clips/{job_id}/download")
async def download_clip(job_id: int):
    job = clip_worker.get_job(job_id)
    if not job or job.get("status") != "completed" or not job.get("output_path"):
        raise HTTPException(status_code=404, detail="Clip result not found")
    expires_at = parse_datetime(job.get("expires_at"))
    if not expires_at or expires_at <= parse_datetime(utc_now_iso()):
        clip_worker.cleanup_expired()
        raise HTTPException(status_code=404, detail="Clip result expired")
    path = Path(str(job["output_path"]))
    try:
        path.resolve().relative_to(config.CLIP_TEMP_DIR.resolve())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Clip result not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Clip result file not found")
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=str(job["download_name"]),
    )


@app.post("/clips/{job_id}/cancel")
async def cancel_clip(job_id: int, next_url: str = Form("/")):
    if not await clip_worker.cancel_job(job_id):
        raise HTTPException(status_code=409, detail="Clip job cannot be canceled")
    return RedirectResponse(safe_next_url(next_url), status_code=303)


@app.post("/clips/{job_id}/delete")
async def delete_clip_result(job_id: int, next_url: str = Form("/")):
    if not clip_worker.delete_result(job_id):
        raise HTTPException(status_code=409, detail="Clip result cannot be deleted")
    return RedirectResponse(safe_next_url(next_url), status_code=303)


@app.get("/partials/clips/{media_id}", response_class=HTMLResponse)
async def partial_media_clips(request: Request, media_id: int):
    return templates.TemplateResponse(
        request,
        "partials/clip_jobs.html",
        {
            "clip_jobs": clip_worker.jobs(media_id=media_id),
            "clip_next_url": f"/library/{media_id}#clip-controls",
        },
    )


@app.get("/partials/status", response_class=HTMLResponse)
async def partial_status(request: Request):
    return templates.TemplateResponse(request, "partials/status.html", status_context())


@app.get("/partials/recent-work", response_class=HTMLResponse)
async def partial_recent_work(request: Request):
    return templates.TemplateResponse(
        request,
        "partials/recent_work.html",
        {"recent_sessions": db.recent_completed_sessions()},
    )


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
