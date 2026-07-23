"""POST /documents (§14): PDF upload or URL → ingested_documents + trigger.

The route does the mechanical half of ingestion — store the original, extract
plain text, record the row, enqueue the ingestion trigger — and returns 202
immediately. The proposal itself is an asynchronous turn; the app sees its
reply through the normal history sync, exactly like a proactive turn.

Auth is the routes_history shape: bearer token → roster → tenant.
"""

import logging
import re
import tempfile
import time
from pathlib import Path

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from inkling.api.routes_chat import _stream
from inkling.api.v1_contract import SSE_HEADERS, UNAUTHORIZED_BODY
from inkling.db.queries import documents as documents_q
from inkling.db.queries import triggers as triggers_q
from inkling.lib import extract
from inkling.turns import ingress

log = logging.getLogger(__name__)

router = APIRouter()

MAX_UPLOAD_BYTES = extract.MAX_PDF_BYTES
# Photos ride the same route but go to the model as real image blocks, so the
# cap is the API's per-image limit, not the PDF text-extraction cap.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(name: str) -> str:
    """Flatten whatever the client called the file into one path segment."""
    return _UNSAFE.sub("_", Path(name).name).strip("._") or "document"


def _store(
    uploads_dir: str, user_id: str, filename: str, data: bytes, *,
    required: bool = False,
) -> str:
    """Keep the original under EXPORT_GIT_DIR/uploads/<user>/ (§14).

    Unconfigured (dev, tests without the export dir) degrades to not storing —
    except when `required` (photos: the stored file IS what the ingestion turn
    reads, there is no extracted text to fall back on), which falls back to
    the system temp dir rather than silently producing a blind turn.
    """
    if not uploads_dir:
        if not required:
            return "(not stored: EXPORT_GIT_DIR unset)"
        uploads_dir = str(Path(tempfile.gettempdir()) / "inkling")
    directory = Path(uploads_dir) / "uploads" / user_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{int(time.time())}-{_safe_name(filename)}"
    path.write_bytes(data)
    return str(path)


@router.post("/documents")
async def upload_document(
    request: Request, authorization: str | None = Header(default=None)
):
    app = request.app
    user_id = app.state.roster.user_for_header(authorization)
    if user_id is None:
        return Response(content=UNAUTHORIZED_BODY, status_code=401)

    content_type = request.headers.get("content-type", "")
    try:
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("file")
            if upload is None or isinstance(upload, str):
                return JSONResponse(
                    {"error": "multipart body needs a 'file' part"}, status_code=400
                )
            data = await upload.read()
            if len(data) > MAX_UPLOAD_BYTES:
                return JSONResponse({"error": "file too large"}, status_code=413)
            media_type = (upload.content_type or "").split(";")[0].strip().lower()
            caption_field = form.get("caption")
            caption = caption_field.strip() if isinstance(caption_field, str) else ""
            if media_type in IMAGE_MEDIA_TYPES:
                if len(data) > MAX_IMAGE_BYTES:
                    return JSONResponse(
                        {"error": "photo too large (5MB limit)"}, status_code=413
                    )
                title = _safe_name(upload.filename or "photo.jpg")
                kind = "photo"
                # No server-side extraction: the ingestion turn gets the
                # stored original as a real image block, so storage is the
                # data path, not an archive nicety.
                text = ""
                storage_path = _store(
                    app.state.uploads_dir, user_id, title, data, required=True
                )
            else:
                title = _safe_name(upload.filename or "document.pdf")
                text = extract.pdf_text(data)
                kind = "pdf"
                storage_path = _store(app.state.uploads_dir, user_id, title, data)
        elif content_type.startswith("application/json"):
            body = await request.json()
            url = body.get("url") if isinstance(body, dict) else None
            if not isinstance(url, str) or not url.strip():
                return JSONResponse(
                    {"error": "JSON body needs a 'url' string"}, status_code=400
                )
            url = url.strip()
            title, text = await extract.url_text(url)
            kind = "url"
            storage_path = url  # the URL is its own original
        else:
            return JSONResponse(
                {"error": "send multipart/form-data (file) or application/json (url)"},
                status_code=415,
            )
    except extract.ExtractionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)

    async with app.state.db.with_tenant(user_id) as conn:
        extracted: dict = {"kind": kind, "text": text, "chars": len(text)}
        if kind == "photo":
            extracted["media_type"] = media_type
            if caption:
                extracted["caption"] = caption
        row = await documents_q.insert(
            conn,
            user_id=user_id,
            title=title,
            storage_path=storage_path,
            extracted=extracted,
        )
    document_id = int(row["id"])

    await ingress.submit(
        app.state.db,
        user_id=user_id,
        source="ingestion",
        payload={
            "document_id": document_id,
            "title": title,
            "text": (
                (
                    f"New photo taken: '{title}'. "
                    + (f"The user sent it with this note: \"{caption}\". " if caption else "")
                    + "Look at it and propose."
                )
                if kind == "photo"
                else f"New document uploaded: '{title}'. Extract and propose."
            ),
        },
        source_ref=f"document:{document_id}",
        idempotency_key=f"ingestion:{document_id}",
    )
    app.state.supervisor.nudge(user_id)
    log.info(
        "document %s (%s, %d chars) queued for ingestion for user %s",
        document_id, kind, len(text), user_id,
    )
    return JSONResponse(
        {"document_id": document_id, "title": title, "status": "queued"},
        status_code=202,
    )


@router.get("/documents/{document_id}/stream")
async def document_stream(
    document_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Attach to the ingestion turn's live stream (v1 /chat SSE shape).

    The upload 202s before the turn runs; this is how the app renders the
    proposal like any chat reply — typing dots, live tokens, done — instead
    of waiting for a history sync. Same broker + turn_events machinery as
    POST /chat: safe to drop and reconnect, replay comes from the DB.
    """
    app = request.app
    user_id = app.state.roster.user_for_header(authorization)
    if user_id is None:
        return Response(content=UNAUTHORIZED_BODY, status_code=401)

    # Subscribe before resolving the trigger so no event can slip between.
    queue = app.state.broker.open(user_id)
    try:
        async with app.state.db.with_tenant(user_id) as conn:
            trigger = await triggers_q.by_idempotency_key(
                conn, user_id, f"ingestion:{document_id}"
            )
    except Exception:
        app.state.broker.close(user_id, queue)
        raise
    if trigger is None:
        app.state.broker.close(user_id, queue)
        return JSONResponse({"error": "no such document"}, status_code=404)

    return StreamingResponse(
        _stream(app, user_id, int(trigger["id"]), queue),
        headers=dict(SSE_HEADERS),
    )


@router.get("/documents/{document_id}/original")
async def document_original(
    document_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Serve the stored original (photos: the image itself).

    The app caches photos locally but can refetch after a cache clear or
    reinstall. RLS scopes the row lookup, so another user's id is a plain 404.
    """
    app = request.app
    user_id = app.state.roster.user_for_header(authorization)
    if user_id is None:
        return Response(content=UNAUTHORIZED_BODY, status_code=401)

    async with app.state.db.with_tenant(user_id) as conn:
        doc = await documents_q.get(conn, document_id)
    if doc is None:
        return JSONResponse({"error": "no such document"}, status_code=404)
    path = Path(doc["storage_path"])
    if not path.is_file():
        return JSONResponse({"error": "original not stored"}, status_code=404)
    media_type = (doc["extracted"] or {}).get("media_type") or "application/octet-stream"
    return FileResponse(path, media_type=media_type)
