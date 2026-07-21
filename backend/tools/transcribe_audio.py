"""
transcribe_audio.py — dedicated audio transcription tool using an audio-capable model.

Agents use this tool to listen to audio (e.g. voice message attachments) rather
than having audio auto-fed to the main LLM. The audio model is selected via a
configurable priority chain:
  1. system config `audio_model_id` (app_settings)
  2. agent's current model

On connection errors, the tool automatically falls back to the next
model in priority order.

The `audio_enabled` flag on the agent gates access to this tool entirely:
when `audio_enabled = 0`, the tool returns an error.

Input audio is normalized to 16kHz mono WAV before being sent — llama.cpp's
multimodal audio projector badly mis-hears 48kHz WAV, and 16kHz mono matches
the Whisper-style encoders these models use.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Optional

from backend.llm_client import LLMClient

# Audio file extensions the tool supports (ffmpeg-readable containers)
_SUPPORTED_AUDIO_EXTS = frozenset({
    ".ogg", ".oga", ".opus",
    ".mp3", ".wav", ".m4a", ".aac",
    ".flac", ".webm",
})


def _resolve_audio_models(agent: dict) -> tuple[list, Optional[str]]:
    """Resolve audio-capable models to use for transcription, ordered by priority.

    Returns a list so the caller can fallback to the next model on connection errors.

    Priority:
      1. System config audio_model_id (app_settings)
      2. Agent's current model

    Returns:
        (models_list, error_string).  Exactly one will be non-None/empty.
        models_list is a deduplicated list of model dicts in priority order.
    """
    from models.db import db

    models = []
    seen_ids = set()

    def _add_model(model):
        """Add model if not seen before (dedup by id, fallback to name)."""
        model_id = model.get("id") or model.get("name", "")
        if model_id and model_id not in seen_ids:
            seen_ids.add(model_id)
            models.append(model)

    # Priority 1: system config
    system_audio_id = db.get_setting("audio_model_id")
    if system_audio_id:
        model = db.get_model_by_id(system_audio_id)
        if model and model.get("enabled"):
            _add_model(model)

    # Priority 2: agent's current model
    _agent_db_id = agent.get("_db_agent_id") or agent.get("id")
    agent_model = db.get_agent_model(_agent_db_id)
    if agent_model:
        _add_model(agent_model)

    if models:
        return models, None

    return [], (
        "No audio-capable model is available. "
        "Please configure a model for this agent or set audio_model_id in System Settings."
    )


def execute(agent: dict, args: dict) -> Any:
    """Transcribe an audio file (or answer a question about it).

    Args:
        agent: Agent context dict (must contain at least 'id').
        args:
            path (str, required): Absolute or relative path to the audio file.
            query (str, optional): Specific question about the audio.
                If omitted, a verbatim transcript is returned.

    Returns:
        str: Plain-text transcript / answer, or an error message.
    """
    # Guard against malformed tool calls where the LLM passes a dict/list
    # instead of a string.
    path = args.get("path")
    path = path.strip() if isinstance(path, str) else ""
    query = args.get("query")
    query = query.strip() if isinstance(query, str) else ""

    # --- Gate: audio_enabled ---
    audio_enabled = agent.get("audio_enabled", 0)
    if not audio_enabled:
        return (
            "Error: Audio processing is not enabled for this agent "
            "(audio_enabled=0). Enable it in the agent's settings to use "
            "the transcribe_audio tool."
        )

    # --- Validate path ---
    if not path:
        return "Error: 'path' parameter is required. Provide the file path to the audio."

    if not os.path.isfile(path):
        return f"Error: File not found: {path}"

    file_size = os.path.getsize(path)
    if file_size > 10 * 1024 * 1024:  # 10 MB
        return f"Error: Audio file is {file_size / (1024*1024):.1f} MB, which exceeds the 10 MB limit."

    ext = os.path.splitext(path)[1].lower()
    if ext not in _SUPPORTED_AUDIO_EXTS:
        return (
            f"Error: Unsupported audio type '{ext or 'unknown'}'. "
            f"Supported formats: OGG, Opus, MP3, WAV, M4A, AAC, FLAC, WebM."
        )

    # --- Read audio and normalize to 16kHz mono WAV ---
    try:
        with open(path, "rb") as f:
            audio_data = f.read()
    except PermissionError:
        return f"Error: Permission denied — cannot read: {path}"
    except Exception as e:
        return f"Error: Failed to read audio: {e}"

    try:
        from backend.audio_utils import convert_to_wav16k
        wav_data = convert_to_wav16k(audio_data)
    except Exception as e:
        return f"Error: Audio conversion failed: {e}"

    audio_b64 = base64.b64encode(wav_data).decode("utf-8")

    # --- Resolve audio models (ordered list for fallback) ---
    audio_models, error = _resolve_audio_models(agent)
    if error:
        return f"Error: {error}"

    # --- Build the audio request ---
    system_prompt = (
        "You are a helpful audio transcription assistant. "
        "Listen to the audio carefully. Transcribe speech verbatim in its "
        "original language. If asked a specific question, answer it directly "
        "based on what you hear."
    )

    if query:
        user_text = (
            f"Please listen to this audio and answer the following question: {query}"
        )
    else:
        user_text = (
            "Please transcribe this audio verbatim, in the language spoken. "
            "Return only the transcript."
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "input_audio", "input_audio": {"data": audio_b64, "format": "wav"}},
            ],
        },
    ]

    # --- Call audio models with fallback on connection errors ---
    result = None
    connection_failures = 0
    last_error = None

    for audio_model in audio_models:
        model_name = audio_model.get("name", audio_model.get("id", "unknown"))
        try:
            client = LLMClient(model_config=audio_model)
            result = client.chat_completion(
                messages=messages,
                enable_thinking=False,  # reasoning eats the budget and adds nothing here
            )
        except Exception as e:
            # Unexpected exception — treat as connection failure and try next
            connection_failures += 1
            last_error = str(e)
            continue

        if result.get("success"):
            break  # Success — use this result

        # Check if this is a connection error we should fallback from
        error_type = result.get("error_type", "")
        error_detail = result.get("error_detail", "")
        if error_type == "connection_error":
            connection_failures += 1
            last_error = error_detail or f"connection to {model_name}"
            continue  # Try next model

        # Non-connection error — fail immediately (auth, rate limit, API error, etc.)
        return f"Error: Audio model call failed ({error_type}): {error_detail}"

    if result is None or not result.get("success"):
        if connection_failures >= len(audio_models):
            return (
                "Error: All audio-capable models failed with connection errors. "
                "Please check your network and LLM server status."
            )
        return f"Error: Audio model call failed: {last_error or 'unknown error'}"

    # Extract text content from the nested API response.
    response_data = result.get("response", {})
    choices = response_data.get("choices", [])
    if not choices:
        return "Error: Audio model returned no choices in response."

    message = choices[0].get("message", {})
    content = message.get("content", "")
    if not content:
        return "Error: Audio model returned an empty response."

    # Strip any thinking tags that may have been included
    from backend.llm_client import strip_thinking_tags
    cleaned, _ = strip_thinking_tags(content)
    return cleaned.strip()
