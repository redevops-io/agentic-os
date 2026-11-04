"""The shipped UI placeholder image (`agentic_os/projects_ui/hero.jpg`) must not carry third-party provenance.

The image shipped in R0 had an embedded C2PA manifest naming "Black Forest Labs API" as claim generator and an
IPTC `trainedAlgorithmicMedia` source type; the terms of that service were never read. It was replaced by
`scripts/make_neutral_hero.py` (gradient + shapes drawn by code). Set HERO_JPG to point the check at another file.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from agentic_os.projects_api import SampleProjectionProvider, create_app

HERO = Path(os.environ.get("HERO_JPG") or Path(__file__).resolve().parents[1] / "agentic_os" / "projects_ui" / "hero.jpg")
FORBIDDEN = (b"c2pa", b"jumb", b"Black Forest", b"trainedAlgorithmicMedia", b"xmp", b"Exif", b"Adobe", b"midjourney")


def _segments(b: bytes):
    assert b[:2] == b"\xff\xd8", "not a JPEG (no SOI)"
    i = 2
    while i < len(b) and b[i] == 0xFF:
        marker = b[i + 1]
        if marker == 0xDA:                      # start of scan: metadata segments are all before it
            return
        length = int.from_bytes(b[i + 2:i + 4], "big")
        yield marker, b[i + 4:i + 2 + length]
        i += 2 + length


def test_hero_is_a_plain_jpeg_without_metadata_segments():
    b = HERO.read_bytes()
    markers = [m for m, _ in _segments(b)]
    assert 0xC0 in markers or 0xC2 in markers, "no frame header"
    # APP1 (EXIF/XMP) .. APP15; APP11 is where a C2PA/JUMBF manifest lives. Only JFIF (APP0) is expected.
    assert not [m for m in markers if 0xE1 <= m <= 0xEF], [hex(m) for m in markers]
    sof = next(d for m, d in _segments(b) if m in (0xC0, 0xC2))
    assert (int.from_bytes(sof[3:5], "big"), int.from_bytes(sof[1:3], "big")) == (1024, 576)


def test_hero_has_no_generator_or_provenance_strings():
    b = HERO.read_bytes().lower()
    assert [k for k in FORBIDDEN if k.lower() in b] == []


def test_ui_still_serves_the_hero_preview():
    client = TestClient(create_app(SampleProjectionProvider()))
    r = client.get("/hero.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content == (Path(__file__).resolve().parents[1] / "agentic_os" / "projects_ui" / "hero.jpg").read_bytes()
    # the sample "Generated hero asset" context item still points at it
    apps = client.get("/api/projects/customer-ops/overview")
    assert apps.status_code == 200
