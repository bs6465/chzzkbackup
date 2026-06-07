from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config
from .chzzk_api import get_channel_name, test_tokens
from .db import db
from .encoder import EncodeWorker
from .logger import logger
from .maintenance import MaintenanceWorker
from .recorder import RecorderSupervisor
from .utils import (
    SAFE_CHANNEL_ID,
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

recorder = RecorderSupervisor()
encoder = EncodeWorker()
maintenance = MaintenanceWorker()


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
    config.FINAL_ROOT.mkdir(parents=True, exist_ok=True)
    recovered_sessions = db.recover_interrupted_sessions()
    recovered_jobs = db.recover_interrupted_encode_jobs()
    if any(recovered_sessions.values()):
        logger.warning("Recovered interrupted recording session(s): %s", recovered_sessions)
    if any(recovered_jobs.values()):
        logger.warning("Recovered interrupted encode job(s): %s", recovered_jobs)
    recorder.start()
    encoder.start()
    maintenance.start()
    logger.info("ChzzkBackup started")
    try:
        yield
    finally:
        await recorder.stop()
        await encoder.stop()
        await maintenance.stop()
        logger.info("ChzzkBackup stopped")


app = FastAPI(title="ChzzkBackup", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def status_context() -> dict:
    tokens = db.get_tokens()
    return {
        "channels": db.get_channels(),
        "active_sessions": db.active_sessions(),
        "recent_sessions": db.recent_sessions(),
        "encode_jobs": db.encode_jobs(),
        "tokens_masked": {
            "NID_SES": mask_secret(tokens.get("NID_SES")),
            "NID_AUT": mask_secret(tokens.get("NID_AUT")),
        },
        "temp_disk": disk_status(config.TEMP_DIR),
        "final_disk": disk_status(config.FINAL_ROOT),
        "config": config,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {**status_context(), "logs": db.recent_logs(30)},
    )


@app.get("/partials/status", response_class=HTMLResponse)
async def partial_status(request: Request):
    return templates.TemplateResponse(request, "partials/status.html", status_context())


@app.get("/partials/logs", response_class=HTMLResponse)
async def partial_logs(request: Request):
    return templates.TemplateResponse(request, "partials/logs.html", {"logs": db.recent_logs(80)})


@app.post("/channels")
async def create_channel(channel_id: str = Form(...)):
    channel_id = channel_id.strip()
    if not SAFE_CHANNEL_ID.fullmatch(channel_id):
        raise HTTPException(status_code=400, detail="Invalid channel ID")
    tokens = db.get_tokens()
    name = await get_channel_name(channel_id, tokens) or channel_id
    name = sanitize_name(name, channel_id)
    ensure_storage_dirs(name)
    db.upsert_channel(channel_id, name, active=True)
    logger.info("Channel registered: %s (%s)", name, channel_id)
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
    ensure_storage_dirs(safe_name)
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
    ok, message = await test_tokens(db.get_tokens(), channel_id.strip() or None)
    level = "info" if ok else "warning"
    getattr(logger, level)("Token test: %s", message)
    return RedirectResponse("/", status_code=303)


@app.post("/recordings/{session_id}/stop")
async def stop_recording(session_id: int):
    stopped = await recorder.stop_session(session_id)
    if not stopped:
        raise HTTPException(status_code=404, detail="Active recording not found")
    return RedirectResponse("/", status_code=303)
