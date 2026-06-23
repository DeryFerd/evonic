"""
Tool: send_file — send a file as an attachment to the user via
the active messaging channel (Telegram, WhatsApp, etc.).

Agents can use this to deliver PDFs, images, documents, spreadsheets,
or any output file directly to the user through the active channel.

Usage:
  - file_path (required): absolute or relative path to the file
  - caption (optional): text caption to send alongside the file
  - mime_type (optional): MIME type; auto-detected if omitted
"""

import os

try:
    from config import SANDBOX_WORKSPACE as _WORKSPACE_ROOT
except ImportError:
    _WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def execute(agent: dict, args: dict) -> dict:
    session_id = agent.get("session_id", "default")
    if not session_id or session_id == "default":
        return {"error": "No active session — cannot send file without a session"}

    file_path = (args.get("file_path") or "").strip()
    caption = args.get("caption") or None
    mime_type = args.get("mime_type") or None

    if not file_path:
        return {
            "error": 'The "file_path" parameter is required. '
                     'Provide the path to the file to send, '
                     'e.g. file_path="output/report.pdf"'
        }

    # Resolve relative paths against the workspace root
    if not os.path.isabs(file_path):
        file_path = os.path.join(_WORKSPACE_ROOT, file_path)

    # Validate file exists and is readable
    if not os.path.exists(file_path):
        return {"error": f'File not found: "{file_path}"'}
    if not os.path.isfile(file_path):
        return {"error": f'Path is not a file: "{file_path}"'}

    try:
        file_size = os.path.getsize(file_path)
    except OSError as e:
        return {"error": f'Cannot access file "{file_path}": {e}'}

    # Send via channel — lazy import to avoid circular deps
    try:
        from backend.agent_runtime import agent_runtime

        success = agent_runtime.send_file_as_bot(
            session_id, file_path, caption, mime_type
        )
    except Exception as e:
        return {"error": f"Failed to send file: {e}"}

    if not success:
        return {"error": "Failed to send file — channel may be unavailable"}

    return {
        "result": "File sent successfully",
        "file_path": file_path,
        "file_name": os.path.basename(file_path),
        "file_size": file_size,
    }
