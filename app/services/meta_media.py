"""
app/services/meta_media.py — uploading media to Meta.

Two uploads that are easy to confuse, because Meta uses the word "upload" for both
and they hit different endpoints, return different opaque strings, and are consumed
in different places:

  upload_resumable() → a **header handle**
      Used only when CREATING or EDITING a message template. Meta needs a sample of
      the media so a human reviewer can see what the header will look like. The handle
      goes into components[HEADER].example.header_handle and is never used again.

  upload_media() → a **media id**
      Used when SENDING a message whose media we hold as bytes. Valid 30 days.
      Not needed for the Lizo flow — that client supplies a public URL and Meta
      fetches it directly — but it is what a broadcast uses: the campaign image is
      uploaded once and every recipient's send references the same id.

fetch_media() reads one back. It is the only download path in the codebase, and it
exists because a media id cannot be shown in an <img> tag — the bytes have to be
proxied through us or the access token goes to the browser.

`erpnext_client.upload_to_meta` does the same job as upload_media() but with the MIME
type hardcoded to application/pdf. It is deliberately left alone: it sits on the live
ERPNext invoice pipeline, and generalising it in place would put a working production
path at risk to save one small function. Prefer this module for anything new.
"""
import ipaddress
import logging
import os
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote, urlparse

import httpx

from app.core.config import settings
from app.models.whatsapp import WhatsAppAccount
from app.utils.whatsapp_crypto import decrypt_token

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v22.0"

# What Meta accepts in a template header, per format. Enforced before we spend a
# round trip — Meta's own rejection message for a bad type is not self-explanatory.
# Sizes are Meta's documented caps.
MEDIA_FORMATS: dict[str, dict] = {
    "IMAGE":    {"mimes": ("image/jpeg", "image/png"), "max_bytes": 5  * 1024 * 1024},
    "DOCUMENT": {"mimes": ("application/pdf",),        "max_bytes": 100 * 1024 * 1024},
    "VIDEO":    {"mimes": ("video/mp4",),              "max_bytes": 16 * 1024 * 1024},
}

# Formats that carry media rather than text. Used by callers to decide whether a
# header needs a handle (on create) or a URL (on send).
MEDIA_HEADER_FORMATS = tuple(MEDIA_FORMATS)

# Ceiling for a download whose Content-Type we do not recognise: read no more than
# the largest file any format allows, then let validate_upload reject it properly.
_LARGEST_ALLOWED = max(spec["max_bytes"] for spec in MEDIA_FORMATS.values())


class MediaUploadError(Exception):
    """Raised with a message safe to show the user in the template builder."""


def format_for_mime(mime: str) -> str | None:
    """Reverse-lookup the header format a MIME type belongs to, or None if unsupported."""
    for fmt, spec in MEDIA_FORMATS.items():
        if mime in spec["mimes"]:
            return fmt
    return None


def validate_upload(file_bytes: bytes, mime: str) -> str:
    """
    Check a candidate header media file and return its Meta header format.

    Raises MediaUploadError with a human-readable reason — the caller turns it
    straight into a 400 the builder modal can display.
    """
    fmt = format_for_mime(mime)
    if not fmt:
        accepted = ", ".join(sorted(m for s in MEDIA_FORMATS.values() for m in s["mimes"]))
        raise MediaUploadError(f"Unsupported file type '{mime}'. Accepted: {accepted}")

    max_bytes = MEDIA_FORMATS[fmt]["max_bytes"]
    if len(file_bytes) > max_bytes:
        raise MediaUploadError(
            f"{fmt.title()} is {len(file_bytes) / 1048576:.1f} MB — "
            f"Meta's limit is {max_bytes // 1048576} MB."
        )
    if not file_bytes:
        raise MediaUploadError("File is empty.")
    return fmt


def upload_resumable(
    account: WhatsAppAccount,
    file_bytes: bytes,
    filename: str,
    mime: str,
) -> str:
    """
    Upload a template header sample and return Meta's header handle.

    Two steps, both required:
      1. POST /{app_id}/uploads   → an upload session id ("upload:<opaque>")
      2. POST /{session_id}       → the raw bytes, returning {"h": "<handle>"}

    Step 2 authenticates with `Authorization: OAuth <token>` rather than `Bearer` —
    this is not a typo, it is what the Resumable Upload API requires.

    Raises MediaUploadError on any failure.
    """
    if not settings.FB_APP_ID:
        raise MediaUploadError(
            "FB_APP_ID is not configured — template media uploads need the Meta app id."
        )

    validate_upload(file_bytes, mime)
    access_token = decrypt_token(account.access_token_encrypted)

    try:
        with httpx.Client(timeout=120) as client:
            session_res = client.post(
                f"{GRAPH_BASE}/{settings.FB_APP_ID}/uploads",
                params={
                    "file_name": filename,
                    "file_length": len(file_bytes),
                    "file_type": mime,
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )
            session_data = session_res.json()
            session_id = session_data.get("id")
            if not session_id:
                raise MediaUploadError(_meta_message(session_data, session_res.status_code))

            upload_res = client.post(
                f"{GRAPH_BASE}/{session_id}",
                headers={
                    "Authorization": f"OAuth {access_token}",
                    "file_offset": "0",
                },
                content=file_bytes,
            )
            upload_data = upload_res.json()
    except MediaUploadError:
        raise
    except Exception as exc:
        logger.exception("Resumable upload failed for %s", filename)
        raise MediaUploadError(f"Could not reach Meta to upload the file: {exc}") from exc

    handle = upload_data.get("h")
    if not handle:
        raise MediaUploadError(_meta_message(upload_data, upload_res.status_code))

    logger.info("Resumable upload OK: filename=%s mime=%s", filename, mime)
    return handle


def upload_media(
    account: WhatsAppAccount,
    file_bytes: bytes,
    filename: str,
    mime: str,
) -> str:
    """
    Upload media for sending and return the media id. Meta retains it for 30 days.

    The generalised twin of erpnext_client.upload_to_meta — see the module docstring
    for why that one is not simply replaced by this.
    """
    validate_upload(file_bytes, mime)
    access_token = decrypt_token(account.access_token_encrypted)

    try:
        with httpx.Client(timeout=120) as client:
            res = client.post(
                f"{GRAPH_BASE}/{account.phone_number_id}/media",
                headers={"Authorization": f"Bearer {access_token}"},
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (filename, file_bytes, mime)},
            )
        data = res.json()
    except Exception as exc:
        logger.exception("Media upload failed for %s", filename)
        raise MediaUploadError(f"Could not reach Meta to upload the file: {exc}") from exc

    media_id = data.get("id")
    if not media_id:
        raise MediaUploadError(_meta_message(data, res.status_code))
    return media_id


def _assert_fetchable(url: str) -> str:
    """
    Refuse a URL we should not be making a request to, and return its hostname.

    The address comes from a form, and the request leaves from inside our network,
    so an unguarded fetch is a way to read things only the server can reach — cloud
    metadata endpoints, the database host, an internal admin panel. Every resolved
    address has to be a public one.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise MediaUploadError("The link must be a full http(s) URL")

    try:
        resolved = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise MediaUploadError(f"Could not find '{parsed.hostname}' — check the address")

    for info in resolved:
        ip = ipaddress.ip_address(info[4][0])
        # is_global is false for loopback, link-local, private ranges and the
        # 169.254.169.254 metadata address, which is the one that matters most.
        if not ip.is_global:
            raise MediaUploadError(
                f"'{parsed.hostname}' resolves to a private address. The file has to be "
                "somewhere Meta could reach too, not only this server."
            )
    return parsed.hostname


def _filename_from_url(url: str) -> str:
    """A usable filename from the URL path, since a remote server may not send one."""
    name = os.path.basename(unquote(urlparse(url).path or "")).strip()
    return name or "header-file"


def fetch_url(url: str, timeout: float = 30.0) -> tuple[bytes, str, str]:
    """
    Download a user-supplied media URL once, returning (bytes, mime, filename).

    This is what makes a pasted link cost the same as an upload: the bytes are
    fetched here, uploaded to Meta once, and every send afterwards references the
    media id. The alternative — handing Meta the URL — has Meta fetch it once per
    recipient, so a 2,000-person campaign is 2,000 requests against someone else's
    web server and 2,000 chances for a customer to receive nothing.

    Redirects are followed manually, three hops at most, because each hop is a new
    address that has to pass the same check — following them automatically would let
    a public URL redirect to an internal one.
    """
    seen = url
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            for _hop in range(4):
                _assert_fetchable(seen)
                with client.stream(
                    "GET", seen, headers={"User-Agent": "SIL-WAM/1.0"}
                ) as res:
                    if res.status_code in (301, 302, 303, 307, 308):
                        location = res.headers.get("location")
                        if not location:
                            raise MediaUploadError("The link redirects to nowhere")
                        seen = str(httpx.URL(seen).join(location))
                        continue
                    if res.status_code != 200:
                        raise MediaUploadError(
                            f"That link returned HTTP {res.status_code}. It has to be "
                            "publicly readable — a link needing a login will not work."
                        )

                    mime = (res.headers.get("content-type") or "").split(";")[0].strip().lower()
                    # Cap the read by what this file type is even allowed to be, so a
                    # wrong address cannot pull a gigabyte into memory before the
                    # size check that would have rejected it.
                    fmt = format_for_mime(mime)
                    cap = MEDIA_FORMATS[fmt]["max_bytes"] if fmt else _LARGEST_ALLOWED

                    chunks, total = [], 0
                    for chunk in res.iter_bytes():
                        total += len(chunk)
                        if total > cap:
                            raise MediaUploadError(
                                f"That file is over {cap // 1048576} MB, which is Meta's "
                                "limit for this type."
                            )
                        chunks.append(chunk)

                    return b"".join(chunks), mime, _filename_from_url(seen)

            raise MediaUploadError("That link redirects too many times")
    except MediaUploadError:
        raise
    except Exception as exc:
        logger.exception("Could not fetch media URL %s", url)
        raise MediaUploadError(f"Could not download that link: {exc}") from exc


def check_url_reachable(url: str, timeout: float = 6.0) -> str | None:
    """
    Confirm a per-row media URL can actually be fetched. Returns None if fine, or a
    short reason to show the person who uploaded the CSV.

    Deliberately a *check*, not a download. In per-row mode each address becomes one
    message, so Meta fetching it at send costs exactly one request — the same one we
    would have made ourselves. What we cannot leave to send time is *discovery*: a
    dead URL found then is a failed message inside a running campaign, whereas found
    here it is a row in the skipped-rows report before anything goes out.

    HEAD first, falling back to a one-byte ranged GET, because a great many static
    file servers answer HEAD with 405 while serving GET perfectly well. Treating that
    405 as "unreachable" would reject working images.
    """
    try:
        _assert_fetchable(url)
    except MediaUploadError as exc:
        return str(exc)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, max_redirects=3) as client:
            headers = {"User-Agent": "SIL-WAM/1.0"}
            res = client.head(url, headers=headers)
            if res.status_code in (403, 405, 501):
                res = client.get(url, headers={**headers, "Range": "bytes=0-0"})
    except Exception as exc:
        return f"Could not be reached ({type(exc).__name__})"

    if res.status_code >= 400:
        return f"Returned HTTP {res.status_code}"

    # Only judge the type when the server actually declares one. A missing
    # Content-Type is common on plain file hosts and is not evidence of a problem.
    mime = (res.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime and not format_for_mime(mime):
        return f"Is a {mime} file, which WhatsApp cannot use as a media header"
    return None


def check_urls_reachable(urls, timeout: float = 5.0, workers: int = 24) -> dict:
    """
    Check many URLs at once, returning {url: reason-or-None}.

    Deduplicated before the network work: a CSV of 2,000 rows commonly references far
    fewer distinct images, and a batch that checked every row would spend most of its
    time re-asking about the same address. Run in parallel so an import of 200 rows
    costs about one round trip rather than two hundred.

    The width and the timeout together bound the worst case — a batch of 200 entirely
    dead addresses, each burning the full timeout — at roughly 200/24 × 5s. Widening
    further would shorten that but starts to look like a burst of traffic to whoever
    is hosting the images.
    """
    distinct = {u for u in urls if u}
    if not distinct:
        return {}

    results: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(distinct))) as pool:
        futures = {pool.submit(check_url_reachable, u, timeout): u for u in distinct}
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                results[url] = fut.result()
            except Exception as exc:                 # a worker must never sink a batch
                logger.exception("URL check crashed for %s", url)
                results[url] = f"Could not be checked ({type(exc).__name__})"
    return results


def fetch_media(account: WhatsAppAccount, media_id: str) -> tuple[bytes, str]:
    """
    Download a media file we hold only as an id, returning (bytes, mime).

    Two round trips, because Meta splits them: GET /{media_id} returns a short-lived
    CDN URL, and that URL then has to be fetched with the same bearer token. The URL
    expires in minutes and leaks the access token if handed to a browser, which is why
    callers proxy the bytes rather than redirecting.
    """
    access_token = decrypt_token(account.access_token_encrypted)
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        with httpx.Client(timeout=60) as client:
            meta_res = client.get(f"{GRAPH_BASE}/{media_id}", headers=headers)
            meta_json = meta_res.json()

            url = (meta_json or {}).get("url")
            if not url:
                raise MediaUploadError(_meta_message(meta_json, meta_res.status_code))

            # The CDN rejects a request without a browser-shaped User-Agent even when
            # the token is valid — httpx's default is not enough.
            file_res = client.get(url, headers={**headers, "User-Agent": "SIL-WAM/1.0"})
    except MediaUploadError:
        raise
    except Exception as exc:
        logger.exception("Media download failed for %s", media_id)
        raise MediaUploadError(f"Could not reach Meta to fetch the file: {exc}") from exc

    if file_res.status_code != 200:
        raise MediaUploadError(f"Meta returned HTTP {file_res.status_code} for the media file")

    mime = (
        meta_json.get("mime_type")
        or file_res.headers.get("content-type")
        or "application/octet-stream"
    )
    return file_res.content, mime


def _meta_message(data: dict, status_code: int) -> str:
    """Pull the most useful line out of a Graph API error body."""
    err = (data or {}).get("error") or {}
    return (
        err.get("error_user_msg")
        or err.get("message")
        or f"Meta returned HTTP {status_code}"
    )
