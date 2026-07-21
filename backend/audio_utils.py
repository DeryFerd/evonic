"""
Audio conversion utilities for Evonic platform.

Provides shared audio format conversion (any ffmpeg-readable input -> 16kHz
mono WAV) used by the transcribe_audio tool to prepare voice messages for
audio-capable LLM APIs.

16kHz mono is deliberate: llama.cpp's multimodal audio projector badly
mis-hears 48kHz WAV (verified experimentally with Telegram voice notes),
while 16kHz mono matches the Whisper-style encoders these models use.
"""

import logging
import os
import subprocess
import uuid

_logger = logging.getLogger(__name__)

# How long ffmpeg may run before we consider it stalled (guards against
# pathological input).  30 seconds is generous -- typical conversion
# of a <10 MB file finishes in under 1 second.
_FFMPEG_TIMEOUT_SECONDS = 30

# Maximum size in bytes we will attempt to convert (10 MB).
# Files larger than this are rejected upstream before reaching us,
# but this guard provides defence-in-depth against edge cases.
_MAX_CONVERT_BYTES = 10 * 1024 * 1024  # 10 MB

# Output shaping: 16-bit PCM, 16kHz, mono.
_WAV16K_ARGS = ["-f", "wav", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1"]


def convert_to_wav16k(audio_bytes: bytes) -> bytes:
    """Convert any ffmpeg-readable audio to 16kHz mono WAV.

    Uses stdin/stdout pipes to avoid writing temporary files to the
    project directory.  Falls back to named temp files in ``/tmp``
    only when pipe mode fails (some ffmpeg builds / input types
    require seekable input).

    Args:
        audio_bytes: Raw audio bytes (OGG/Opus, MP3, WAV, M4A, ...).

    Returns:
        WAV audio bytes (16-bit PCM, 16kHz, mono).

    Raises:
        RuntimeError: If ffmpeg is not available or conversion fails.
        ValueError: If input is empty or exceeds size limit.
    """
    if not audio_bytes:
        raise ValueError("Cannot convert empty audio data")

    if len(audio_bytes) > _MAX_CONVERT_BYTES:
        raise ValueError(
            f"Audio data too large: {len(audio_bytes)} bytes "
            f"(max {_MAX_CONVERT_BYTES})"
        )

    # Try pipe mode first (no temp files).
    try:
        return _convert_via_pipe(audio_bytes)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg audio->WAV conversion timed out after "
            f"{_FFMPEG_TIMEOUT_SECONDS}s"
        )
    except FileNotFoundError:
        raise RuntimeError(
            "ffmpeg is not installed -- cannot convert audio to WAV. "
            "Install ffmpeg on the server to enable voice message processing."
        )
    except Exception:
        # Pipe mode failed -- fall back to temp files.
        _logger.debug(
            "Audio->WAV pipe conversion failed, falling back to temp files",
            exc_info=True,
        )

    # Temp-file fallback.  NEVER write temp files into the project
    # directory -- use /tmp exclusively.
    return _convert_via_tempfiles(audio_bytes)


def _convert_via_pipe(audio_bytes: bytes) -> bytes:
    """Convert audio->16kHz mono WAV using stdin/stdout pipes (no temp files)."""
    result = subprocess.run(
        [
            "ffmpeg",
            "-i", "pipe:0",       # read from stdin
            *_WAV16K_ARGS,
            "pipe:1",                # write to stdout
            "-loglevel", "error",   # suppress verbose ffmpeg output
            "-y",                     # overwrite output (N/A for pipes but safe)
        ],
        input=audio_bytes,
        capture_output=True,
        timeout=_FFMPEG_TIMEOUT_SECONDS,
        check=True,
    )
    return result.stdout


def _convert_via_tempfiles(audio_bytes: bytes) -> bytes:
    """Convert audio->16kHz mono WAV using named temp files in /tmp.

    This is the fallback for ffmpeg builds or inputs that cannot
    be read from a pipe (seekable requirement).

    CRITICAL: Temp files are written ONLY to /tmp -- NEVER inside the
    Evonic project directory.
    """
    uid = uuid.uuid4().hex[:12]
    in_path = os.path.join("/tmp", f"evonic_audio_{uid}.in")
    wav_path = os.path.join("/tmp", f"evonic_audio_{uid}.wav")

    try:
        # Write input bytes to temp file.
        with open(in_path, "wb") as f:
            f.write(audio_bytes)

        # Convert via ffmpeg.
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-i", in_path,
                    *_WAV16K_ARGS,
                    wav_path,
                    "-loglevel", "error",
                    "-y",
                ],
                capture_output=True,
                timeout=_FFMPEG_TIMEOUT_SECONDS,
                check=True,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"ffmpeg audio->WAV conversion timed out after "
                f"{_FFMPEG_TIMEOUT_SECONDS}s"
            )
        except FileNotFoundError:
            raise RuntimeError(
                "ffmpeg is not installed -- cannot convert audio to WAV. "
                "Install ffmpeg on the server to enable voice message processing."
            )

        # Read back the WAV output.
        with open(wav_path, "rb") as f:
            wav_bytes = f.read()

        return wav_bytes

    finally:
        # Best-effort cleanup of temp files.
        for path in (in_path, wav_path):
            try:
                if os.path.isfile(path):
                    os.unlink(path)
            except OSError:
                pass
