"""Document text extraction (§14): PDFs via pypdf, web pages via tag-strip.

Extraction is deliberately dumb — the model downstream is the parser. This
module's only job is to turn bytes into bounded plain text, and to fail with
a reason string rather than an exception the route would 500 on.
"""

import html
import io
import logging
import re

import httpx
from pypdf import PdfReader

log = logging.getLogger(__name__)

# ~100KB of text (§14 plan cap): far beyond any syllabus, small enough that a
# scanned book can't blow the ingestion turn's context.
TEXT_CAP_CHARS = 100_000
URL_TIMEOUT_SECONDS = 15.0
MAX_PDF_BYTES = 15 * 1024 * 1024

_TAG_BLOCKS = re.compile(
    r"<(script|style|noscript|head)\b.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAGS = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_BLANK = re.compile(r"\n{3,}")


class ExtractionError(Exception):
    """Bytes that could not become text; str(exc) is user-facing."""


def pdf_text(data: bytes) -> str:
    if len(data) > MAX_PDF_BYTES:
        raise ExtractionError(
            f"PDF is {len(data) // (1024 * 1024)}MB; the limit is "
            f"{MAX_PDF_BYTES // (1024 * 1024)}MB"
        )
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise ExtractionError(f"could not parse the PDF: {exc}") from exc
    text = _tidy("\n\n".join(pages))
    if not text.strip():
        raise ExtractionError(
            "the PDF contains no extractable text (scanned image?)"
        )
    return text[:TEXT_CAP_CHARS]


def strip_html(raw: str) -> tuple[str, str]:
    """(title, text) from an HTML document — regex-grade, model cleans up."""
    title_match = _TITLE.search(raw)
    title = html.unescape(title_match.group(1)).strip() if title_match else ""
    body = _TAG_BLOCKS.sub(" ", raw)
    body = _TAGS.sub("\n", body)
    return title, _tidy(html.unescape(body))[:TEXT_CAP_CHARS]


async def url_text(url: str) -> tuple[str, str]:
    """(title, text) for a web page. Raises ExtractionError, never httpx."""
    if not url.startswith(("http://", "https://")):
        raise ExtractionError("url must start with http:// or https://")
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=URL_TIMEOUT_SECONDS
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExtractionError(f"could not fetch the page: {exc}") from exc

    content_type = response.headers.get("content-type", "")
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        return url.rsplit("/", 1)[-1] or url, pdf_text(response.content)
    title, text = strip_html(response.text[: TEXT_CAP_CHARS * 4])
    if not text.strip():
        raise ExtractionError("the page contains no readable text")
    return title or url, text


def _tidy(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    return _BLANK.sub("\n\n", "\n".join(line for line in lines if line is not None)).strip()
