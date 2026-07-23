"""Barcode decode + food-product lookup, the photo turn's two tools (§14).

Both are model-facing tool backends: they return TEXT the model reads, and
they never raise to the tool layer — a failed scan or lookup is an honest
sentence, not a crashed turn. Decode is local (zxing-cpp); product data comes
from Open Food Facts (free, keyless, the barcode is the only thing sent).
"""

import io
import logging

import httpx

log = logging.getLogger(__name__)

LOOKUP_TIMEOUT_SECONDS = 8.0
_OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
_USER_AGENT = "Inkling/1.0 (personal assistant; single user)"


def decode_all(data: bytes) -> list[tuple[str, str]]:
    """Every barcode found in the image bytes, as (format, text) pairs.

    Retail symbologies (UPC/EAN — the product code) sort first: packaging
    often carries several codes at once (the live Häagen-Dazs box had a
    DataMatrix production code right next to the UPC). The model gets all of
    them and judges; empty list means nothing machine-decoded.
    """
    try:
        import zxingcpp
        from PIL import Image, ImageOps

        image = Image.open(io.BytesIO(data))
        # Phone photos carry EXIF rotation; the decoder sees raw pixels.
        image = ImageOps.exif_transpose(image)
        results = zxingcpp.read_barcodes(image)
    except Exception:
        log.warning("barcode decode failed", exc_info=True)
        return []
    retail = ("EAN-13", "UPC-A", "EAN-8", "UPC-E")
    decoded = [
        (str(result.format), (result.text or "").strip())
        for result in results
        if (result.text or "").strip()
    ]
    return sorted(decoded, key=lambda pair: pair[0] not in retail)


async def lookup(code: str) -> str:
    """Plain-text product + nutrition summary for a barcode.

    Nutrition is reported in BOTH bases Open Food Facts carries (per serving
    and per 100 g), separately labeled — which one the user actually ate is
    their call, so the model must be able to quote both.
    """
    code = code.strip()
    if not code.isdigit():
        return f"'{code}' does not look like a barcode (digits only)."
    try:
        async with httpx.AsyncClient(
            timeout=LOOKUP_TIMEOUT_SECONDS, headers={"User-Agent": _USER_AGENT}
        ) as client:
            response = await client.get(_OFF_URL.format(code=code))
            # Unknown products come back as 404 (body still says status 0);
            # that is a clean not-found, not a transport failure.
            if response.status_code != 404:
                response.raise_for_status()
            body = response.json()
    except Exception as exc:
        log.warning("open food facts lookup failed for %s: %s", code, exc)
        return f"product lookup failed for barcode {code} ({type(exc).__name__})."

    product = body.get("product") or {}
    if body.get("status") != 1 or not product:
        return f"no product found for barcode {code} in Open Food Facts."

    lines = []
    name = " — ".join(
        part.strip()
        for part in (product.get("brands"), product.get("product_name"))
        if part and part.strip()
    )
    lines.append(f"Product: {name or '(unnamed)'} (barcode {code})")
    if product.get("quantity"):
        lines.append(f"Package size: {product['quantity']}")

    nutriments = product.get("nutriments") or {}

    def block(suffix: str, label: str) -> list[str]:
        rows = []
        for field, key in [
            ("Calories", "energy-kcal"), ("Protein", "proteins"),
            ("Carbs", "carbohydrates"), ("Fat", "fat"), ("Sugars", "sugars"),
        ]:
            value = nutriments.get(f"{key}_{suffix}")
            if value is None:
                continue
            unit = " kcal" if key == "energy-kcal" else " g"
            rows.append(f"  - {field}: {value}{unit}")
        return [label] + rows if rows else []

    serving = product.get("serving_size")
    serving_label = f"Per serving ({serving}):" if serving else "Per serving:"
    lines += block("serving", serving_label)
    lines += block("100g", "Per 100 g:")
    if len(lines) <= 2:
        lines.append("(no nutrition data on record)")
    return "\n".join(lines)
