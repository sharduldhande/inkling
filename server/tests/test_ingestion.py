"""Document ingestion (§14): upload → extract → propose → confirm → batch.

Runs as runtime_role against the Neon test branch, the shape production uses.
URL fetches are monkeypatched — the suite never touches the network.
"""

import zlib
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from inkling.api.auth import Roster
from inkling.api.broker import Broker
from inkling.api.server import create_app
from inkling.db.client import Database
from inkling.db.queries import users as users_q
from inkling.inference.gateway import StubGateway
from inkling.lib import extract
from inkling.turns import ingress
from inkling.turns.commands import parse_commands
from inkling.turns.context import assemble_context
from inkling.turns.ingestion import assemble_ingestion_context, is_ingestion_trigger
from inkling.turns.validate import (
    MAX_COMMITMENTS_PER_TURN,
    MAX_EVENTS_PER_TURN,
    validate_commands,
)
from inkling.turns.worker import Worker

from tests.conftest import USER_A

TOKEN_A = "token-for-a"
NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture(autouse=True)
async def clean(migrated: str, dsn: str):
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "TRUNCATE turn_events, turns, triggers, transcript, audit_log, "
            "events, docs, commitment_occurrences, commitments, "
            "ingested_documents, deliveries RESTART IDENTITY CASCADE"
        )
        conn.commit()
    yield


@pytest_asyncio.fixture
async def client(db: Database, tmp_path):
    """API only (quiet): triggers are recorded, tests drive turns explicitly."""
    app = create_app(
        db, Roster({USER_A: TOKEN_A}), broker=Broker(),
        uploads_dir=str(tmp_path),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN_A}"}


def _tiny_pdf(text: str = "Essay due 2026-08-01. Exam 2026-08-15.") -> bytes:
    """A minimal one-page PDF with real extractable text."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    compressed = zlib.compress(stream)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(compressed)
        + compressed + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += (
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
        % (len(objects) + 1, xref_at)
    )
    return bytes(out)


async def _rows(db: Database, sql: str, args=()):
    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute(sql, args)
        return await cur.fetchall()


async def _seed_document(db: Database, *, title: str = "syllabus.pdf") -> int:
    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute(
            "INSERT INTO ingested_documents (user_id, title, storage_path, extracted) "
            "VALUES (%s, %s, '/tmp/x', %s) RETURNING id",
            (USER_A, title, '{"kind": "pdf", "text": "HW1 due 2026-08-01"}'),
        )
        return int((await cur.fetchone())["id"])


async def _validate(db: Database, raw: list[dict], **flags):
    commands = parse_commands(raw)
    assert len(commands) == len(raw)
    async with db.with_tenant(USER_A) as conn:
        user = await users_q.get(conn, USER_A)
        return await validate_commands(conn, user=user, commands=commands, **flags)


# --- extraction (unit) -------------------------------------------------------


def test_pdf_extraction_reads_the_text():
    text = extract.pdf_text(_tiny_pdf())
    assert "Essay due 2026-08-01" in text


def test_garbage_pdf_is_an_extraction_error():
    with pytest.raises(extract.ExtractionError):
        extract.pdf_text(b"this is not a pdf")


def test_strip_html_keeps_text_drops_script():
    title, text = extract.strip_html(
        "<html><head><title>Study Plan</title><script>evil()</script></head>"
        "<body><h1>Day 1</h1><p>Two pointers &amp; arrays</p></body></html>"
    )
    assert title == "Study Plan"
    assert "Day 1" in text and "Two pointers & arrays" in text
    assert "evil" not in text


# --- POST /documents ---------------------------------------------------------


async def test_pdf_upload_creates_row_and_ingestion_trigger(client, db: Database):
    r = await client.post(
        "/documents",
        files={"file": ("ISyE syllabus.pdf", _tiny_pdf(), "application/pdf")},
        headers=auth(),
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "queued"

    [doc] = await _rows(db, "SELECT * FROM ingested_documents")
    assert doc["title"] == "ISyE_syllabus.pdf"
    assert "Essay due 2026-08-01" in doc["extracted"]["text"]
    assert doc["confirmed_at"] is None
    assert doc["storage_path"].endswith("ISyE_syllabus.pdf")

    [trig] = await _rows(db, "SELECT * FROM triggers")
    assert trig["source"] == "ingestion"
    assert trig["priority"] == 2
    assert trig["payload"]["document_id"] == body["document_id"]


async def test_url_ingestion_via_stubbed_fetch(client, db: Database, monkeypatch):
    async def fake_url_text(url):
        assert url == "https://leetcode.com/studyplan/top-interview-150/"
        return "Top Interview 150", "Day 1: arrays. Day 2: two pointers."

    monkeypatch.setattr(extract, "url_text", fake_url_text)
    r = await client.post(
        "/documents",
        json={"url": "https://leetcode.com/studyplan/top-interview-150/"},
        headers=auth(),
    )
    assert r.status_code == 202

    [doc] = await _rows(db, "SELECT * FROM ingested_documents")
    assert doc["title"] == "Top Interview 150"
    assert doc["extracted"]["kind"] == "url"
    assert len(await _rows(db, "SELECT 1 FROM triggers WHERE source='ingestion'")) == 1


async def test_upload_requires_auth(client):
    r = await client.post("/documents", json={"url": "https://x.test/"})
    assert r.status_code == 401


async def test_unextractable_upload_is_422_and_writes_nothing(client, db: Database):
    r = await client.post(
        "/documents",
        files={"file": ("junk.pdf", b"not a pdf at all", "application/pdf")},
        headers=auth(),
    )
    assert r.status_code == 422
    assert await _rows(db, "SELECT 1 FROM ingested_documents") == []
    assert await _rows(db, "SELECT 1 FROM triggers") == []


# --- photos ------------------------------------------------------------------


# A real 1x1 transparent PNG: small, valid, and enough for the route, which
# never parses image bytes.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf"
    b"\xc0\xf0\x1f\x00\x05\x05\x02\x00\x0f\xf5<U\x00\x00\x00\x00IEND\xaeB`\x82"
)


async def _seed_photo(db: Database, path: str) -> int:
    async with db.with_tenant(USER_A) as conn:
        cur = await conn.execute(
            "INSERT INTO ingested_documents (user_id, title, storage_path, extracted) "
            "VALUES (%s, 'photo.png', %s, %s) RETURNING id",
            (USER_A, path,
             '{"kind": "photo", "text": "", "media_type": "image/png"}'),
        )
        return int((await cur.fetchone())["id"])


async def test_photo_upload_creates_photo_row_and_trigger(client, db: Database):
    r = await client.post(
        "/documents",
        files={"file": ("IMG 0042.png", _TINY_PNG, "image/png")},
        headers=auth(),
    )
    assert r.status_code == 202

    [doc] = await _rows(db, "SELECT * FROM ingested_documents")
    assert doc["extracted"]["kind"] == "photo"
    assert doc["extracted"]["media_type"] == "image/png"
    assert doc["extracted"]["text"] == ""
    # Photos MUST be stored — the turn reads these bytes back.
    from pathlib import Path
    assert Path(doc["storage_path"]).read_bytes() == _TINY_PNG

    [trig] = await _rows(db, "SELECT * FROM triggers")
    assert trig["source"] == "ingestion"
    assert "photo" in trig["payload"]["text"]


async def test_photo_caption_reaches_row_and_trigger(client, db: Database):
    r = await client.post(
        "/documents",
        files={"file": ("IMG.png", _TINY_PNG, "image/png")},
        data={"caption": "log calories for this"},
        headers=auth(),
    )
    assert r.status_code == 202

    [doc] = await _rows(db, "SELECT * FROM ingested_documents")
    assert doc["extracted"]["caption"] == "log calories for this"
    [trig] = await _rows(db, "SELECT * FROM triggers")
    assert 'this note: "log calories for this"' in trig["payload"]["text"]


async def test_document_stream_replays_the_proposal(client, db: Database):
    r = await client.post(
        "/documents",
        files={"file": ("IMG.png", _TINY_PNG, "image/png")},
        headers=auth(),
    )
    document_id = r.json()["document_id"]

    gateway = StubGateway(reply="A concert poster. Confirm?")
    worker = Worker(db, USER_A, gateway)
    assert await worker.tick() is True

    s = await client.get(f"/documents/{document_id}/stream", headers=auth())
    assert s.status_code == 200
    import json as json_mod
    frames = [
        json_mod.loads(line[6:])
        for line in s.text.splitlines()
        if line.startswith("data: ")
    ]
    reply = "".join(f.get("text", "") for f in frames)
    assert "A concert poster." in reply
    assert {"done": True} in frames

    missing = await client.get("/documents/999999/stream", headers=auth())
    assert missing.status_code == 404


async def test_document_original_serves_the_photo_bytes(client, db: Database):
    r = await client.post(
        "/documents",
        files={"file": ("IMG.png", _TINY_PNG, "image/png")},
        headers=auth(),
    )
    document_id = r.json()["document_id"]

    o = await client.get(f"/documents/{document_id}/original", headers=auth())
    assert o.status_code == 200
    assert o.headers["content-type"].startswith("image/png")
    assert o.content == _TINY_PNG

    assert (await client.get("/documents/999999/original", headers=auth())).status_code == 404
    assert (await client.get(f"/documents/{document_id}/original")).status_code == 401


async def test_oversized_photo_is_413(client, db: Database):
    from inkling.api.routes_documents import MAX_IMAGE_BYTES

    r = await client.post(
        "/documents",
        files={"file": ("big.jpg", b"x" * (MAX_IMAGE_BYTES + 1), "image/jpeg")},
        headers=auth(),
    )
    assert r.status_code == 413
    assert await _rows(db, "SELECT 1 FROM ingested_documents") == []


async def test_photo_ingestion_context_attaches_the_image(db: Database, tmp_path):
    import base64

    path = tmp_path / "photo.png"
    path.write_bytes(_TINY_PNG)
    doc_id = await _seed_photo(db, str(path))

    got = await assemble_ingestion_context(
        db, user_id=USER_A,
        trigger={"id": 1, "source": "ingestion",
                 "source_ref": f"document:{doc_id}",
                 "payload": {"document_id": doc_id, "title": "photo.png",
                             "text": "New photo taken."}},
    )

    assert "photo the user just took" in got.system
    assert "PROPOSAL only" in got.system
    [image] = got.images
    assert image.media_type == "image/png"
    assert base64.b64decode(image.data_base64) == _TINY_PNG


async def test_photo_ingestion_degrades_when_original_is_gone(db: Database):
    doc_id = await _seed_photo(db, "/nonexistent/gone.png")
    got = await assemble_ingestion_context(
        db, user_id=USER_A,
        trigger={"id": 1, "source": "ingestion",
                 "source_ref": f"document:{doc_id}",
                 "payload": {"document_id": doc_id, "title": "photo.png",
                             "text": "New photo taken."}},
    )
    assert got.images == ()
    assert "retake" in got.context


async def test_barcode_tools_exist_only_on_photo_turns(tmp_path):
    from inkling.inference.gateway import ImageAttachment
    from inkling.turns.worker import make_barcode_handlers
    from tests.test_barcode import BARCODE_PNG

    # No images (every chat/wakeup turn) → the tools don't exist at all.
    assert make_barcode_handlers(()) == {}

    import base64
    image = ImageAttachment(
        media_type="image/png",
        data_base64=base64.b64encode(BARCODE_PNG).decode("ascii"),
    )
    handlers = make_barcode_handlers((image,))
    assert set(handlers) == {"scan_barcode", "lookup_product"}
    assert await handlers["scan_barcode"]({}) == "EAN-13: 4006381333931"
    assert "barcode" in await handlers["lookup_product"]({})  # arg hint


async def test_photo_turn_passes_images_to_the_gateway(db: Database, tmp_path):
    path = tmp_path / "photo.png"
    path.write_bytes(_TINY_PNG)
    doc_id = await _seed_photo(db, str(path))
    await ingress.submit(
        db, user_id=USER_A, source="ingestion",
        payload={"document_id": doc_id, "title": "photo.png",
                 "text": "New photo taken."},
        source_ref=f"document:{doc_id}",
    )
    gateway = StubGateway(reply="A poster for a concert on 2026-08-01. Confirm?")
    worker = Worker(db, USER_A, gateway)
    assert await worker.tick() is True

    [call] = gateway.calls
    assert len(call["images"]) == 1
    assert call["images"][0].media_type == "image/png"


# --- the ingestion turn ------------------------------------------------------


async def test_ingestion_context_carries_document_and_commitments(db: Database):
    doc_id = await _seed_document(db)
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "INSERT INTO commitments (user_id, title, due_at, source) "
            "VALUES (%s, 'existing HW0', %s, 'conversation')",
            (USER_A, NOW + timedelta(days=3)),
        )
    trigger = {
        "id": 1, "source": "ingestion", "source_ref": f"document:{doc_id}",
        "payload": {"document_id": doc_id, "title": "syllabus.pdf", "text": "x"},
    }
    assert is_ingestion_trigger(trigger)

    got = await assemble_ingestion_context(db, user_id=USER_A, trigger=trigger)

    assert "HW1 due 2026-08-01" in got.context
    assert "existing HW0" in got.context
    assert "PROPOSAL only" in got.system


async def test_ingestion_turn_commands_are_rejected_but_notify_passes(db: Database):
    doc_id = await _seed_document(db)
    sub = await ingress.submit(
        db, user_id=USER_A, source="ingestion",
        payload={"document_id": doc_id, "title": "syllabus.pdf", "text": "x"},
        source_ref=f"document:{doc_id}",
    )
    gateway = StubGateway(
        reply="Found 2 deadlines. Confirm?",
        commands=parse_commands([
            {"type": "UpsertCommitment", "title": "HW1",
             "due_at": (NOW + timedelta(days=10)).isoformat()},
            {"type": "Notify", "body": "Syllabus processed — open Inkling to confirm."},
        ]),
    )
    worker = Worker(db, USER_A, gateway)
    assert await worker.tick() is True

    # The proposal reached the transcript; the commitment did NOT apply.
    assert await _rows(db, "SELECT 1 FROM commitments") == []
    [delivery] = await _rows(db, "SELECT * FROM deliveries")
    assert "confirm" in delivery["body"].lower()
    [line] = await _rows(
        db, "SELECT text FROM transcript WHERE role='assistant'"
    )
    assert "only propose" in line["text"]  # the honest rejection note
    trig = await _rows(db, "SELECT status FROM triggers WHERE id=%s", (sub.trigger_id,))
    assert trig[0]["status"] == "done"


async def test_chat_context_carries_pending_ingestion_until_confirmed(db: Database):
    doc_id = await _seed_document(db, title="ISyE syllabus")
    got = await assemble_context(
        db, user_id=USER_A,
        trigger={"id": 1, "source": "chat", "source_ref": None,
                 "payload": {"text": "yes, schedule it"}},
    )
    assert "## Pending ingestion" in got.context
    assert f"document_id {doc_id}" in got.context

    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "UPDATE ingested_documents SET confirmed_at = now() WHERE id = %s",
            (doc_id,),
        )
    got = await assemble_context(
        db, user_id=USER_A,
        trigger={"id": 2, "source": "chat", "source_ref": None,
                 "payload": {"text": "thanks"}},
    )
    assert "## Pending ingestion" not in got.context


# --- confirmation ------------------------------------------------------------


async def test_confirm_marker_sets_confirmed_at_and_applies_batch(db: Database):
    doc_id = await _seed_document(db)
    verdict = await _validate(db, [
        {"type": "UpsertCommitment", "title": "HW1",
         "due_at": (NOW + timedelta(days=10)).isoformat()},
        {"type": "AppendEvent", "kind": "note",
         "note": "plan day 1/2: arrays",
         "occurred_at": (NOW + timedelta(days=1)).isoformat()},
        {"type": "AppendEvent", "kind": "ingestion",
         "note": "user confirmed the syllabus schedule",
         "document_id": doc_id},
    ])
    assert verdict.rejected == []

    from inkling.turns.apply import apply_command

    async with db.with_tenant(USER_A) as conn:
        user = await users_q.get(conn, USER_A)
        for i, cmd in enumerate(verdict.accepted):
            await apply_command(conn, cmd, user=user, turn_id=1, index=i)

    [doc] = await _rows(db, "SELECT * FROM ingested_documents")
    assert doc["confirmed_at"] is not None
    [commitment] = await _rows(db, "SELECT * FROM commitments")
    assert commitment["title"] == "HW1"
    [marker] = await _rows(db, "SELECT * FROM events WHERE kind='ingestion'")
    assert marker["source"] == f"ingestion:{doc_id}"


async def test_double_confirm_is_rejected(db: Database):
    doc_id = await _seed_document(db)
    async with db.with_tenant(USER_A) as conn:
        await conn.execute(
            "UPDATE ingested_documents SET confirmed_at = now() WHERE id = %s",
            (doc_id,),
        )
    verdict = await _validate(db, [
        {"type": "AppendEvent", "kind": "ingestion", "note": "x",
         "document_id": doc_id},
    ])
    assert "already confirmed" in verdict.rejected[0].reason


async def test_document_id_requires_ingestion_kind(db: Database):
    doc_id = await _seed_document(db)
    verdict = await _validate(db, [
        {"type": "AppendEvent", "kind": "note", "note": "x",
         "document_id": doc_id},
    ])
    assert "kind='ingestion'" in verdict.rejected[0].reason


# --- caps --------------------------------------------------------------------


async def test_commitment_batch_over_cap_rejects_the_excess(db: Database):
    raw = [
        {"type": "UpsertCommitment", "title": f"item {i}",
         "due_at": (NOW + timedelta(days=i + 1)).isoformat()}
        for i in range(MAX_COMMITMENTS_PER_TURN + 5)
    ]
    verdict = await _validate(db, raw)
    assert len(verdict.accepted) == MAX_COMMITMENTS_PER_TURN
    assert len(verdict.rejected) == 5
    assert "per-turn cap" in verdict.rejected[0].reason


async def test_event_batch_over_cap_rejects_the_excess(db: Database):
    raw = [
        {"type": "AppendEvent", "kind": "note", "note": f"n{i}"}
        for i in range(MAX_EVENTS_PER_TURN + 3)
    ]
    verdict = await _validate(db, raw)
    assert len(verdict.accepted) == MAX_EVENTS_PER_TURN
    assert len(verdict.rejected) == 3


async def test_future_pacing_note_allowed_but_size_capped(db: Database):
    ok = await _validate(db, [
        {"type": "AppendEvent", "kind": "note",
         "note": "plan day 3/43: linked lists",
         "occurred_at": (NOW + timedelta(days=3)).isoformat()},
    ])
    assert ok.rejected == []

    fat = await _validate(db, [
        {"type": "AppendEvent", "kind": "note", "note": "x" * 400,
         "occurred_at": (NOW + timedelta(days=3)).isoformat()},
    ])
    assert "pacing note" in fat.rejected[0].reason

    checkin = await _validate(db, [
        {"type": "AppendEvent", "kind": "checkin", "note": "will do",
         "occurred_at": (NOW + timedelta(days=3)).isoformat()},
    ])
    assert "not what will" in checkin.rejected[0].reason
