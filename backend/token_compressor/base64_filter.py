"""
base64_filter.py — Universal base64 blob detection and replacement for RTK.

Scans tool output text for long base64-encoded data (images, PDFs, binary
attachments) and replaces them with compact placeholders.  This runs as a
post-processing step after per-command TOML-filter compression, ensuring
that ALL tool outputs (not just shell commands) are protected against
base64 flooding in the LLM context window.

Usage:
    from backend.token_compressor.base64_filter import strip_base64_blobs

    compressed = strip_base64_blobs(raw_output)
"""

from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Minimum character length for a base64 sequence to be considered a blob.
# Shorter runs (e.g. short base64-encoded IDs, tokens) pass through unchanged.
DEFAULT_MIN_BASE64_LENGTH: int = 1_000

# Base64 character alphabet regex
_BASE64_CHAR: str = r"[A-Za-z0-9+/]"
_BASE64_PADDING: str = r"=*"

# Known base64 magic prefixes and their type labels.
# Key: regex pattern for the first few base64 chars after decoding
# Value: human-readable type label
_MAGIC_PATTERNS: list[tuple[str, str]] = [
    # Images by magic bytes (base64-encoded)
    (r"/9j/", "JPEG"),          # JPEG: FF D8 FF
    (r"iVBORw0KGgo", "PNG"),   # PNG: 89 50 4E 47 0D 0A 1A 0A
    (r"R0lGOD", "GIF"),        # GIF: GIF89a or GIF87a
    (r"UklGR", "WebP"),        # WebP/RIFF: 52 49 46 46
    (r"Qk0", "BMP"),           # BMP: 42 4D
    (r"SUkq", "TIFF"),         # TIFF (II): 49 49
    (r"TU0AKg", "TIFF"),       # TIFF (MM): 4D 4D
    (r"PD94bWwg", "SVG"),      # SVG/XML: <?xml
    (r"PHN2Zy", "SVG"),        # SVG: <svg

    # Documents
    (r"JVBERi0", "PDF"),       # PDF: %PDF-
    (r"UEsDBBQ", "ZIP"),       # ZIP/DOCX/XLSX: PK..
    (r"0M8R4KGxGuE", "DOC"),   # MS Office OLE

    # Audio
    (r"SUQz", "MP3"),          # MP3: ID3 header (varies)
    (r"//uQx", "MP3"),         # MP3 sync
    (r"T2dnUw", "OGG"),        # OGG: OggS
    (r"UklGRi", "WAV"),        # WAV/RIFF: same as WebP prefix

    # Video
    (r"AAAAIGZ0eXB", "MP4"),   # MP4: ....ftyp
    (r"GkXf", "WebM"),         # WebM: 1A 45 DF A3
]

# Compile magic patterns once at module load
_COMPILED_MAGIC: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^" + pat, re.IGNORECASE), label)
    for pat, label in _MAGIC_PATTERNS
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def strip_base64_blobs(
    text: str,
    min_length: int = DEFAULT_MIN_BASE64_LENGTH,
) -> str:
    """Scan *text* for long base64 sequences and replace them with placeholders.

    Base64 sequences shorter than *min_length* are left untouched.
    This is intentional — short base64 strings (tokens, IDs, hashes) are
    not blobs and should pass through to the LLM.

    Args:
        text: Raw tool output text (typically JSON-serialized).
        min_length: Minimum character length to trigger replacement.

    Returns:
        Text with long base64 sequences replaced by compact placeholders.
    """
    if not text or min_length < 1:
        return text

    try:
        return _strip_base64_blobs_impl(text, min_length)
    except Exception:
        logger.exception(
            "base64_filter.strip_base64_blobs: unhandled exception — "
            "returning original text (fail-open)."
        )
        return text


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------

# Regex to find long runs of base64 characters.
# We build it dynamically to include the min_length threshold.
def _build_base64_regex(min_len: int) -> re.Pattern:
    """Build a regex that matches base64 sequences of at least *min_len* chars."""
    return re.compile(
        rf"({_BASE64_CHAR}{{{min_len},}})"
        rf"({_BASE64_PADDING})"
    )


def _detect_type(base64_str: str) -> str:
    """Detect the data type from the first few base64 characters.

    Args:
        base64_str: The base64-encoded string (start portion is sufficient).

    Returns:
        A human-readable type label like "JPEG", "PNG", or "base64" for unknown.
    """
    # Check against known magic bytes
    for pat, label in _COMPILED_MAGIC:
        if pat.search(base64_str):
            return label

    return "base64"


def _format_size(chars: int) -> str:
    """Format a character count as a human-readable size string.

    Args:
        chars: Number of characters in the base64 sequence.

    Returns:
        A formatted size string like "84KB" or "1.2MB".
    """
    # Base64 encodes 3 bytes into 4 chars → chars * 3/4 = approx bytes
    approx_bytes = chars * 3 // 4

    if approx_bytes >= 1_000_000:
        return f"{approx_bytes / 1_000_000:.1f}MB"
    elif approx_bytes >= 1_000:
        return f"{approx_bytes // 1_000}KB"
    else:
        return f"{approx_bytes}B"


def _build_placeholder(blob_type: str, chars: int) -> str:
    """Build a compact placeholder string for a detected base64 blob.

    Args:
        blob_type: Human-readable type label (e.g. "JPEG").
        chars: Character count of the base64 sequence.

    Returns:
        A placeholder string like "[BASE64_IMAGE: JPEG, 84KB, 81724 chars]"
    """
    size_str = _format_size(chars)

    if blob_type in ("JPEG", "PNG", "GIF", "WebP", "BMP", "TIFF", "SVG"):
        category = "IMAGE"
    elif blob_type in ("PDF", "DOC", "ZIP"):
        category = "DOC"
    elif blob_type in ("MP3", "OGG", "WAV"):
        category = "AUDIO"
    elif blob_type in ("MP4", "WebM"):
        category = "VIDEO"
    else:
        category = "DATA"

    return f"[BASE64_{category}: {blob_type}, {size_str}, {chars} chars]"


def _strip_base64_blobs_impl(text: str, min_length: int) -> str:
    """Core implementation: find and replace long base64 sequences.

    Strategy:
    1. Find all base64-looking sequences of >= min_length characters.
    2. For each match, determine the data type from the magic prefix.
    3. Replace with a compact placeholder.
    4. Process matches from end to start to preserve positions.

    Args:
        text: The full text to scan.
        min_length: Minimum base64 sequence length.

    Returns:
        Text with long base64 sequences replaced.
    """
    pat = _build_base64_regex(min_length)

    # Find all matches — iterate left-to-right
    matches: list[Tuple[int, int, str]] = []  # (start, end, placeholder)
    for m in pat.finditer(text):
        blob = m.group(0)
        blob_type = _detect_type(blob)
        placeholder = _build_placeholder(blob_type, len(blob))

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "base64_filter: replacing %s blob at pos %d (%d chars → placeholder)",
                blob_type, m.start(), len(blob),
            )

        matches.append((m.start(), m.end(), placeholder))

    if not matches:
        return text

    # Build result by replacing from end to start (preserves indices)
    result = text
    for start, end, placeholder in reversed(matches):
        result = result[:start] + placeholder + result[end:]

    total_replaced = len(matches)
    total_chars_saved = sum(
        (end - start) - len(placeholder)
        for start, end, placeholder in matches
    )
    logger.info(
        "base64_filter: stripped %d base64 blob(s), saved %d chars",
        total_replaced, total_chars_saved,
    )

    return result
