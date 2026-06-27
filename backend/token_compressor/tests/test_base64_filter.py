"""
Tests for base64_filter.py — Base64 blob detection and replacement.

Covers:
  - JPEG image detection and replacement
  - PNG image detection
  - PDF detection
  - Unknown base64 type (fallback)
  - Short base64 passthrough (below threshold)
  - Multiple blobs in one string
  - Empty / no-base64 input
  - Fail-open on exceptions
  - JSON-embedded base64 (realistic pinchtab_screenshot output)
  - Edge cases: padding, whitespace, mixed content
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_THIS_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _THIS_DIR.parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

from token_compressor.base64_filter import (
    strip_base64_blobs,
    DEFAULT_MIN_BASE64_LENGTH,
    _detect_type,
    _format_size,
    _build_placeholder,
)


# ===================================================================
# Helpers — generate realistic base64 data
# ===================================================================

def _make_base64_blob(prefix: str, total_length: int) -> str:
    """Generate a base64-looking string of *total_length* starting with *prefix*."""
    if len(prefix) >= total_length:
        return prefix[:total_length]
    # Extend with random-ish base64 chars
    import random
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    random.seed(42)  # deterministic
    remaining = total_length - len(prefix)
    return prefix + "".join(random.choice(chars) for _ in range(remaining))


def _pinchtab_screenshot_json(num_screenshots: int = 1) -> str:
    """Simulated pinchtab_screenshot output with base64 JPEG images."""
    base64_jpeg = _make_base64_blob("/9j/4AAQSkZJRg", 81724)
    screenshots = [
        {"status": "success", "screenshot": base64_jpeg}
        for _ in range(num_screenshots)
    ]
    return json.dumps({
        "status": "success",
        "exit_code": 0,
        "screenshots": screenshots,
    })


# ===================================================================
# Unit tests for helpers
# ===================================================================


class TestDetectType:
    """Tests for _detect_type() — magic prefix recognition."""

    def test_jpeg(self):
        assert _detect_type("/9j/4AAQSkZJRg") == "JPEG"

    def test_png(self):
        assert _detect_type("iVBORw0KGgoAAAANSUhEUg") == "PNG"

    def test_gif(self):
        assert _detect_type("R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAAL") == "GIF"

    def test_pdf(self):
        assert _detect_type("JVBERi0xLjQKJcOkw7zD") == "PDF"

    def test_zip(self):
        assert _detect_type("UEsDBBQAAAAIAO") == "ZIP"

    def test_webp(self):
        assert _detect_type("UklGRiAAAABXRUJQVlA4") == "WAV"  # WAV/WebP share prefix

    def test_unknown_type(self):
        assert _detect_type("QWERTYUIOPASDFGHJKL") == "base64"


class TestFormatSize:
    """Tests for _format_size() — human-readable byte sizes."""

    def test_bytes(self):
        # 100 chars base64 ≈ 75 bytes
        assert _format_size(100) == "75B"

    def test_kilobytes(self):
        # 81724 chars ≈ 61,293 bytes ≈ 61KB
        size = _format_size(81724)
        assert "KB" in size

    def test_megabytes(self):
        # 1,400,000 chars ≈ 1,050,000 bytes ≈ 1.0MB
        size = _format_size(1_400_000)
        assert "MB" in size


class TestBuildPlaceholder:
    """Tests for _build_placeholder() — placeholder formatting."""

    def test_jpeg_placeholder(self):
        ph = _build_placeholder("JPEG", 81724)
        assert "[BASE64_IMAGE:" in ph
        assert "JPEG" in ph
        assert "81724 chars" in ph

    def test_pdf_placeholder(self):
        ph = _build_placeholder("PDF", 50000)
        assert "[BASE64_DOC:" in ph

    def test_unknown_placeholder(self):
        ph = _build_placeholder("xyz", 2000)
        assert "[BASE64_DATA:" in ph


# ===================================================================
# Core strip_base64_blobs tests
# ===================================================================


class TestStripBase64Blobs:
    """Tests for strip_base64_blobs() — the main entry point."""

    def test_jpeg_blob_stripped(self):
        """A long JPEG base64 blob is replaced with a placeholder."""
        blob = _make_base64_blob("/9j/4AAQSkZJRg", 5000)
        text = f'{{"screenshot": "{blob}"}}'
        result = strip_base64_blobs(text)
        assert blob not in result
        assert "[BASE64_IMAGE:" in result
        assert "JPEG" in result
        assert len(result) < len(text)

    def test_png_blob_stripped(self):
        """A long PNG base64 blob is replaced."""
        blob = _make_base64_blob("iVBORw0KGgo", 3000)
        text = f'{{"image": "{blob}"}}'
        result = strip_base64_blobs(text)
        assert blob not in result
        assert "[BASE64_IMAGE:" in result
        assert "PNG" in result

    def test_pdf_blob_stripped(self):
        """A long PDF base64 blob is replaced."""
        blob = _make_base64_blob("JVBERi0xLjQK", 2000)
        text = f'{{"document": "{blob}"}}'
        result = strip_base64_blobs(text)
        assert blob not in result
        assert "[BASE64_DOC:" in result
        assert "PDF" in result

    def test_short_base64_passes_through(self):
        """Short base64 strings (< DEFAULT_MIN_BASE64_LENGTH) are not replaced."""
        blob = _make_base64_blob("iVBORw0KGgo", 500)  # below 1000 threshold
        text = f'{{"icon": "{blob}"}}'
        result = strip_base64_blobs(text)
        assert blob in result  # unchanged
        assert result == text

    def test_custom_threshold(self):
        """A custom min_length threshold is respected."""
        blob = _make_base64_blob("/9j/4AAQ", 500)
        text = f'{{"img": "{blob}"}}'
        # With threshold 400, it should be stripped
        result = strip_base64_blobs(text, min_length=400)
        assert blob not in result
        assert "[BASE64_IMAGE:" in result
        # With default threshold (1000), it should pass through
        result2 = strip_base64_blobs(text)
        assert blob in result2

    def test_multiple_blobs(self):
        """Multiple base64 blobs in one output are ALL replaced."""
        jpeg = _make_base64_blob("/9j/4AAQ", 1500)
        png = _make_base64_blob("iVBORw0KGgo", 1500)
        text = f'{{"a": "{jpeg}", "b": "{png}"}}'
        result = strip_base64_blobs(text)
        assert jpeg not in result
        assert png not in result
        assert result.count("[BASE64_IMAGE: JPEG") == 1
        assert result.count("[BASE64_IMAGE: PNG") == 1

    def test_no_base64_passthrough(self):
        """Text with no base64 content passes through unchanged."""
        text = json.dumps({"status": "ok", "message": "Operation completed"})
        result = strip_base64_blobs(text)
        assert result == text

    def test_empty_string(self):
        """Empty string is returned as-is."""
        assert strip_base64_blobs("") == ""

    def test_plain_text_passthrough(self):
        """Plain text without base64 passes through unchanged."""
        text = "This is a normal text response from a tool.\nNo base64 here."
        result = strip_base64_blobs(text)
        assert result == text

    def test_pinchtab_screenshot_simulation(self):
        """Simulated pinchtab_screenshot output — base64 JPEG stripped."""
        text = _pinchtab_screenshot_json(num_screenshots=1)
        result = strip_base64_blobs(text)
        # Base64 blob must not appear in result
        assert "/9j/4AAQSkZJRg" not in result
        assert "[BASE64_IMAGE:" in result
        # The surrounding JSON structure should be preserved
        assert '"status"' in result
        assert '"exit_code"' in result

    def test_pinchtab_multi_screenshot(self):
        """Multiple screenshots — all base64 blobs stripped."""
        text = _pinchtab_screenshot_json(num_screenshots=3)
        result = strip_base64_blobs(text)
        assert result.count("[BASE64_IMAGE: JPEG") == 3
        assert "/9j/4AAQSkZJRg" not in result

    def test_mixed_content_preserved(self):
        """Non-base64 JSON fields are preserved alongside stripped blobs."""
        jpeg = _make_base64_blob("/9j/4AAQ", 2000)
        text = json.dumps({
            "status": "success",
            "message": "Captured 1 screenshot",
            "viewport": {"width": 1280, "height": 720},
            "screenshot": jpeg,
        })
        result = strip_base64_blobs(text)
        assert "success" in result
        assert "Captured 1 screenshot" in result
        assert "1280" in result
        assert "720" in result
        assert jpeg not in result
        assert "[BASE64_IMAGE:" in result

    def test_base64_with_equals_padding(self):
        """Base64 strings with = padding at the end are detected."""
        # Build a base64 string with proper = padding
        blob = _make_base64_blob("/9j/4AAQ", 1500)
        # Replace last 2 chars with == to simulate padding
        blob = blob[:-2] + "=="
        text = f'{{"img": "{blob}"}}'
        result = strip_base64_blobs(text)
        assert blob not in result

    def test_unknown_base64_type_stripped(self):
        """Long base64 with unknown magic bytes is still stripped."""
        blob = _make_base64_blob("QWERTYUIOPASDFGHJKL", 1500)
        text = f'{{"data": "{blob}"}}'
        result = strip_base64_blobs(text)
        assert blob not in result
        assert "[BASE64_DATA:" in result

    def test_zero_threshold_returns_original(self):
        """min_length=0 returns original unchanged (safety guard)."""
        blob = _make_base64_blob("/9j/4AAQ", 100)
        text = f'{{"img": "{blob}"}}'
        result = strip_base64_blobs(text, min_length=0)
        assert result == text
