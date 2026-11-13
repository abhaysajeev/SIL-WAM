"""
Media files we hold: store, upload to Meta once, hand out, expire.

The flow for a broadcast image from the agent app:

    POST /agent-api/v1/media      → create_outbound(): check the real file type and size,
                                     store our copy, upload ONCE to Meta → MediaAsset row
    POST /agent-api/v1/broadcasts → header_media_for(): check it fits the template, return
                                     the campaign's header_media — the same shape the web
                                     panel stores, plus "asset_id"

Every recipient of that broadcast is then sent the same Meta media id, and every agent
chat bubble for it points at the same asset. One file, one upload, N messages.

**The file type is read from the bytes, not trusted from the request.** A phone (or a
script) can label anything image/jpeg; Meta would reject a mislabelled file at send time,
per recipient, long after the agent pressed Send. Sniffing the signature here fails it
while the agent is still looking at the upload.
"""
import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core import template_body
from app.core.config import settings
from app.models.media_asset import MediaAsset
from app.services import media_store, meta_media

logger = logging.getLogger(__name__)

META_MEDIA_TTL = timedelta(days=30)          # Meta keeps an uploaded media id this long
META_REFRESH_MARGIN = timedelta(hours=24)    # re-upload when less than this remains

_EXT = {"image/jpeg": "jpg", "image/png": "png", "video/mp4": "mp4",
        "application/pdf": "pdf"}


class MediaError(Exception):
    """A reason safe to show the person who uploaded. `code` maps to an agent API code."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def sniff_mime(data: bytes) -> str | None:
    """The MIME type the bytes actually are, for the formats Meta accepts as headers."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:5] == b"%PDF-":
        return "application/pdf"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "video/mp4"
    return None


def max_upload_bytes() -> int:
    return max(spec["max_bytes"] for spec in meta_media.MEDIA_FORMATS.values())


def limits() -> list[dict]:
    """The per-format rules, for GET /limits — so the app can check before uploading."""
    return [{"format": fmt, "mimes": list(spec["mimes"]), "max_bytes": spec["max_bytes"]}
            for fmt, spec in meta_media.MEDIA_FORMATS.items()]


def create_outbound(db: Session, account, company_id, data: bytes, filename: str | None,
                    *, agent_row_id=None, user_id=None) -> MediaAsset:
    """
    Validate, store our copy, upload to Meta once, record. Commits nothing — the caller
    commits. Raises MediaError.
    """
    if not data:
        raise MediaError("file_unreadable", "The file is empty.")
    mime = sniff_mime(data)
    if mime is None:
        accepted = "JPEG or PNG image, MP4 video, or PDF document"
        raise MediaError("unsupported_media_type",
                         f"This file type is not supported. Send a {accepted}.")
    try:
        fmt = meta_media.validate_upload(data, mime)
    except meta_media.MediaUploadError as exc:
        raise MediaError("media_too_large", str(exc),
                         max_bytes=meta_media.MEDIA_FORMATS.get(
                             meta_media.format_for_mime(mime) or "", {}).get("max_bytes"))

    now = datetime.now(timezone.utc)
    asset_id = uuid.uuid4()
    ext = _EXT.get(mime, "bin")
    key = f"company/{company_id}/{now:%Y/%m}/{asset_id}.{ext}"
    safe_name = os.path.basename(filename or "") or f"{asset_id}.{ext}"

    store = media_store.get_store()
    store.put(key, data, mime)
    try:
        meta_id = meta_media.upload_media(account, data, safe_name, mime)
    except meta_media.MediaUploadError as exc:
        store.delete(key)
        raise MediaError("media_upload_failed", str(exc))
    except Exception:
        store.delete(key)
        raise

    asset = MediaAsset(
        id=asset_id, company_id=company_id,
        uploaded_by_agent_id=agent_row_id, uploaded_by_user_id=user_id,
        direction="outbound", format=fmt, mime=mime, size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(), filename=safe_name, storage_key=key,
        meta_media_id=meta_id, meta_media_expires_at=now + META_MEDIA_TTL,
        expires_at=now + timedelta(days=settings.MEDIA_RETENTION_DAYS),
    )
    db.add(asset)
    db.flush()
    return asset


def get_live(db: Session, company_id, asset_id) -> MediaAsset | None:
    """The asset if it belongs to this company, whether or not it has expired."""
    try:
        aid = uuid.UUID(str(asset_id))
    except (ValueError, TypeError):
        return None
    return (db.query(MediaAsset)
            .filter(MediaAsset.id == aid, MediaAsset.company_id == company_id)
            .first())


def ensure_meta_fresh(db: Session, account, asset: MediaAsset) -> None:
    """Re-upload from our copy if Meta's id is missing or about to lapse."""
    now = datetime.now(timezone.utc)
    if asset.meta_media_id and asset.meta_media_expires_at \
            and asset.meta_media_expires_at - now > META_REFRESH_MARGIN:
        return
    data = b"".join(media_store.get_store().open(asset.storage_key))
    try:
        asset.meta_media_id = meta_media.upload_media(account, data, asset.filename, asset.mime)
    except meta_media.MediaUploadError as exc:
        raise MediaError("media_upload_failed", str(exc))
    asset.meta_media_expires_at = now + META_MEDIA_TTL


def header_media_for(db: Session, account, template, asset_id) -> dict | None:
    """
    The campaign `header_media` for this template, or None for a template without a
    media header. Raises MediaError when the file is missing, expired or the wrong kind.
    """
    needed = template_body.header_format(template.components)
    is_media = needed in template_body.MEDIA_HEADER_FORMATS
    if not is_media:
        if asset_id:
            raise MediaError("header_media_not_allowed",
                             f"Template '{template.name}' has no image, video or document "
                             f"header, so it takes no file.")
        return None
    if not asset_id:
        raise MediaError("header_media_required",
                         f"Template '{template.name}' needs a {str(needed).lower()} — "
                         f"upload one first.", format=needed)

    asset = get_live(db, template.company_id, asset_id)
    if asset is None or asset.direction != "outbound":
        raise MediaError("media_not_found", "That file was not found. Upload it again.")
    if asset.deleted_at is not None or asset.expires_at <= datetime.now(timezone.utc):
        raise MediaError("media_expired", "That file has expired. Upload it again.")
    if asset.format != needed:
        raise MediaError("header_media_mismatch",
                         f"Template '{template.name}' needs a {str(needed).lower()}, "
                         f"but the file is a {asset.format.lower()}.",
                         expected=needed, supplied=asset.format)

    ensure_meta_fresh(db, account, asset)
    return {
        "format": asset.format, "source": "upload", "media_id": asset.meta_media_id,
        "source_url": None, "filename": asset.filename, "mime": asset.mime,
        "attached_at": datetime.now(timezone.utc).isoformat(),
        "asset_id": str(asset.id),
    }


def expire_due(db: Session, now: datetime | None = None, batch: int = 500) -> int:
    """Delete stored files past their expiry. Returns how many were removed."""
    now = now or datetime.now(timezone.utc)
    due = (db.query(MediaAsset)
           .filter(MediaAsset.deleted_at.is_(None), MediaAsset.expires_at <= now)
           .limit(batch).all())
    store = media_store.get_store()
    for asset in due:
        try:
            store.delete(asset.storage_key)
        except Exception as exc:
            logger.warning("Could not delete media %s: %s", asset.id, exc)
            continue
        asset.deleted_at = now
    db.commit()
    return len(due)


# ── Inbound media: copy every customer attachment while Meta still has it ─────
#
# Meta's media id for an inbound file is downloadable for about 7 days, and each download
# URL lasts minutes. So the file is copied to our store straight away (this sweep runs
# every few seconds) rather than when an agent happens to open the chat. The message's
# media JSON gains `asset_id` and the message's rev is bumped, so the device learns the
# attachment is ready through its normal sync.

INBOUND_FETCH_DAYS = 7
INBOUND_MAX_ATTEMPTS = 6


def pending_inbound(db: Session, limit: int = 20) -> list:
    from sqlalchemy import text
    return db.execute(text("""
        SELECT m.id, m.thread_id, m.media, t.company_id
          FROM agent_messages m JOIN agent_threads t ON t.id = m.thread_id
         WHERE m.direction = 'inbound'
           AND m.media IS NOT NULL
           AND m.media->>'media_id' IS NOT NULL
           AND m.media->>'asset_id' IS NULL
           AND COALESCE((m.media->>'fetch_attempts')::int, 0) < :max_attempts
           AND m.created_at > now() - make_interval(days => :days)
         ORDER BY m.created_at
         LIMIT :limit
         FOR UPDATE OF m SKIP LOCKED
    """), {"max_attempts": INBOUND_MAX_ATTEMPTS, "days": INBOUND_FETCH_DAYS,
           "limit": limit}).fetchall()


def fetch_inbound(db: Session) -> int:
    """Copy pending inbound attachments. Commits per message. Returns how many tried."""
    from app.models.agent_timeline import AgentMessage
    from app.models.whatsapp import WhatsAppAccount
    from app.services import agent_timeline

    rows = pending_inbound(db)
    for row in rows:
        media = dict(row.media or {})
        msg = db.query(AgentMessage).filter(AgentMessage.id == row.id).first()
        account = (db.query(WhatsAppAccount)
                   .filter(WhatsAppAccount.company_id == row.company_id).first())
        try:
            if account is None:
                raise MediaError("no_account", "No WhatsApp account for this company")
            data, mime = meta_media.fetch_media(account, media["media_id"])
            mime = (mime or "").split(";")[0].strip() or "application/octet-stream"
            fmt = str(media.get("type") or "document").upper()
            now = datetime.now(timezone.utc)
            asset_id = uuid.uuid4()
            ext = _EXT.get(mime) or (mime.split("/")[-1][:8] if "/" in mime else "bin")
            key = f"company/{row.company_id}/inbound/{now:%Y/%m}/{asset_id}.{ext}"
            media_store.get_store().put(key, data, mime)
            db.add(MediaAsset(
                id=asset_id, company_id=row.company_id, direction="inbound", format=fmt,
                mime=mime, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                filename=os.path.basename(media.get("filename") or "") or None,
                storage_key=key,
                expires_at=now + timedelta(days=settings.MEDIA_RETENTION_DAYS)))
            media.update({"asset_id": str(asset_id), "mime_type": mime,
                          "size_bytes": len(data)})
            media.pop("fetch_error", None)
        except Exception as exc:
            media["fetch_attempts"] = int(media.get("fetch_attempts") or 0) + 1
            media["fetch_error"] = str(exc)[:200]
            logger.warning("Inbound media %s fetch failed: %s", media.get("media_id"), exc)
        msg.media = media
        msg.rev = agent_timeline.bump_rev(db, msg.thread_id)
        db.commit()
    return len(rows)
