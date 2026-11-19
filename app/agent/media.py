"""
Media from and for the agent app.

    POST /agent-api/v1/media        upload a file (a broadcast's header image) → media_id
    GET  /agent-api/v1/media/{id}   download a file the agent may see

**Upload once, use many times.** The returned `media_id` is OUR id for the file. The
server keeps a copy and has already uploaded it to Meta once; a broadcast that names it
sends that same Meta upload to every recipient. Uploading is a separate step from
sending so a wrong or oversized file is refused while the agent is still choosing it.

**Files are never public.** GET streams the file through the app after checking who is
asking; the storage behind it (MinIO) has no public address.
"""
from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from datetime import datetime
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent import errors
from app.agent.deps import AgentContext, get_agent_session
from app.core.database import get_db
from app.models.whatsapp import WhatsAppAccount
from app.services import agent_timeline, media_assets, media_store
from app.utils.error_logger import log_error

router = APIRouter(prefix="/media", tags=["Agent App"])

_READ_CHUNK = 1024 * 1024


class MediaOut(BaseModel):
    media_id:   str
    format:     str          # IMAGE | VIDEO | DOCUMENT
    mime:       str
    size_bytes: int
    filename:   str | None
    expires_at: datetime     # after this the file is deleted and cannot be sent


async def _read_capped(file: UploadFile, cap: int) -> bytes:
    """Read at most cap+1 bytes, so an oversized upload never sits whole in memory."""
    buf = bytearray()
    while chunk := await file.read(_READ_CHUNK):
        buf.extend(chunk)
        if len(buf) > cap:
            break
    return bytes(buf)


@router.post("", response_model=MediaOut, status_code=201)
async def upload_media(
    request: Request,
    file: UploadFile = File(...),
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    if not ctx.company_id:
        raise errors.not_an_agent()

    cap = media_assets.max_upload_bytes()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap + 64 * 1024:
        raise errors.media("media_too_large",
                           f"The file is larger than the {cap // 1048576} MB limit.",
                           max_bytes=cap)
    data = await _read_capped(file, cap)
    if len(data) > cap:
        raise errors.media("media_too_large",
                           f"The file is larger than the {cap // 1048576} MB limit.",
                           max_bytes=cap)

    account = (db.query(WhatsAppAccount)
               .filter(WhatsAppAccount.company_id == ctx.company_id).first())
    if account is None or not account.access_token_encrypted:
        raise errors.no_whatsapp_account()

    try:
        asset = media_assets.create_outbound(
            db, account, ctx.company_id, data, file.filename,
            agent_row_id=ctx.agent_row_id, user_id=ctx.user_id)
        db.commit()
    except media_assets.MediaError as exc:
        db.rollback()
        raise errors.media(exc.code, exc.message, **exc.extra)
    except Exception as exc:
        db.rollback()
        log_error("Agent media upload failed", "POST /agent-api/v1/media", exc,
                  user=ctx.user_id)
        raise errors.send_failed("The file could not be stored. Try again.")

    return MediaOut(media_id=str(asset.id), format=asset.format, mime=asset.mime,
                    size_bytes=asset.size_bytes, filename=asset.filename,
                    expires_at=asset.expires_at)


def _may_view(db: Session, ctx: AgentContext, asset) -> bool:
    """
    The uploader, or a participant of a chat that shows this file — the same rule as
    reading the chat itself (agent_timeline.VISIBLE_TO_AGENT_SQL).
    """
    if asset.uploaded_by_agent_id and str(asset.uploaded_by_agent_id) == str(ctx.agent_row_id):
        return True
    return db.execute(
        text("""SELECT 1 FROM agent_messages m
                  JOIN agent_threads t ON t.id = m.thread_id
                 WHERE t.company_id = :c AND m.media->>'asset_id' = :a
                   AND """ + agent_timeline.VISIBLE_TO_AGENT_SQL + """
                 LIMIT 1"""),
        {"c": ctx.company_id, "a": str(asset.id), "agent_id": ctx.agent_row_id},
    ).first() is not None


@router.get("/{media_id}")
def download_media(
    media_id: str,
    ctx: AgentContext = Depends(get_agent_session),
    db: Session = Depends(get_db),
):
    if not ctx.company_id:
        raise errors.not_an_agent()
    asset = media_assets.get_live(db, ctx.company_id, media_id)
    # Another company's id, an unknown id and one this agent may not see all read the
    # same: not found. Never confirm that a file exists to someone who cannot open it.
    if asset is None or not _may_view(db, ctx, asset):
        raise errors.media("media_not_found", "That file was not found.")
    if asset.deleted_at is not None:
        raise errors.media("media_expired", "This file is no longer available.")
    try:
        stream = media_store.get_store().open(asset.storage_key)
    except FileNotFoundError:
        raise errors.media("media_expired", "This file is no longer available.")

    disposition = "inline" if asset.format in ("IMAGE", "VIDEO") else "attachment"
    filename = (asset.filename or str(asset.id)).replace('"', "")
    return StreamingResponse(
        stream, media_type=asset.mime,
        headers={"Content-Length": str(asset.size_bytes),
                 "Content-Disposition": f'{disposition}; filename="{filename}"',
                 "Cache-Control": "private, max-age=3600",
                 "X-Content-Type-Options": "nosniff"},
    )
