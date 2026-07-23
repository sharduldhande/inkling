"""Barcode decode + Open Food Facts lookup (photo-turn tools, §14).

DB-free: decode is pure bytes-in/codes-out, and lookup's HTTP is stubbed —
the suite never touches the network.
"""

import base64

import httpx
import pytest

from inkling.lib import barcode

# EAN-13 4006381333931, generated once with zxingcpp.create_barcode +
# write_barcode_to_image(scale=3) and embedded for determinism.
BARCODE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAVMAAAClCAAAAADEnEEYAAABuklEQVR4nO3SQWrDMBRA"
    "we/c/87qIkS4pEnAPAiFmYXAkiyLh481Hxwz6zHumTlN7qX7/Pxe2v58PJ+5D9k71+vP"
    "nd+dS5PP13iz881Nnt1ex+QiTXua9jTtadrTtKdpT9Oepj1Ne5r2NO1p2tO0p2lP056m"
    "PU17mvY07Wna07SnaU/TnqY9TXua9jTtadrTtKdpT9Oepj1Ne5r2NO1p2tO0p2lP056m"
    "PU17mvY07Wna07SnaU/TnqY9TXua9jTtadrTtKdpT9Oepj1Ne5r2NO1p2tO0p2lP056m"
    "PU17mvY07Wna07SnaU/TnqY9TXua9jTtadrTtKdpT9Oepj1Ne5r2NO1p2tO0p2lP056m"
    "PU17mvY07Wna07SnaU/TnqY9TXua9jTtadrTtKdpT9Oepj1Ne5r2NO1p2tO0p2lP056m"
    "PU17mvY07Wna07SnaU/TnqY9TXua9jTtadrTtKdpT9Oepj1Ne5r2NO1p2tO0p2lP056m"
    "PU17mvY07Wna07SnaU/TnqY9TXua9jTtado71scdM+sxftfxT27iP+1p2tO0p2lP056m"
    "PU17mvY07Wna07SnaU/TnqaT+wFZDSdH0HHCJQAAAABJRU5ErkJggg=="
)


def test_decode_reads_the_ean13():
    assert barcode.decode_all(BARCODE_PNG) == [("EAN-13", "4006381333931")]


def test_decode_garbage_is_empty_not_fatal():
    assert barcode.decode_all(b"definitely not an image") == []


def _stub_get(monkeypatch, status_code: int, body: dict):
    async def fake_get(self, url):
        return httpx.Response(
            status_code, json=body, request=httpx.Request("GET", url)
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


async def test_lookup_reports_both_nutrition_bases(monkeypatch):
    _stub_get(monkeypatch, 200, {
        "status": 1,
        "product": {
            "brands": "Häagen-Dazs",
            "product_name": "Coffee almond crunch bars",
            "quantity": "11 FLoz",
            "serving_size": "1 bar (85 g)",
            "nutriments": {
                "energy-kcal_serving": 310, "proteins_serving": 4.2,
                "carbohydrates_serving": 24, "fat_serving": 23,
                "energy-kcal_100g": 360, "proteins_100g": 5,
                "carbohydrates_100g": 28, "fat_100g": 27, "sugars_100g": 26,
            },
        },
    })
    text = await barcode.lookup("074570651481")
    assert "Häagen-Dazs" in text
    assert "Per serving (1 bar (85 g)):" in text
    assert "310 kcal" in text
    assert "Per 100 g:" in text
    assert "360 kcal" in text


async def test_lookup_unknown_product_is_honest(monkeypatch):
    # OFF answers unknown codes with a 404 whose body still parses.
    _stub_get(monkeypatch, 404, {"status": 0})
    text = await barcode.lookup("12002133")
    assert "no product found for barcode 12002133" in text


async def test_lookup_transport_failure_is_a_sentence(monkeypatch):
    async def boom(self, url):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx.AsyncClient, "get", boom)
    text = await barcode.lookup("074570651481")
    assert "lookup failed" in text


async def test_lookup_rejects_non_digits_without_network():
    assert "digits only" in await barcode.lookup("abc123")
