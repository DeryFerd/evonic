"""
_attachment.py — attachment virtual path resolution for portal_copy.

Intercepts /_attachment/<id> paths and resolves them to local filesystem
paths using the attachment database records, so that portal_copy can read
attachment files as a source (e.g. uploading to a remote workplace).

Path scheme: /_attachment/<attachment_id>

This is Phase 2 of the attachment → workplace bridge. Phase 1 is
read_attachment.py (read-only viewing), Phase 3 is attachment upload.
"""

import os

_ATTACHMENT_PREFIX = "/_attachment/"


def is_attachment_path(file_path: str) -> bool:
    """Return True if file_path starts with the /_attachment/ virtual prefix."""
    return bool(file_path) and file_path.startswith(_ATTACHMENT_PREFIX)


def resolve_attachment_path(agent: dict, file_path: str) -> tuple:
    """Resolve a /_attachment/<id> path to a (backend, real_path) tuple.

    Looks up the attachment record in the database, verifies access,
    and returns a LocalBackend plus the absolute filesystem path so that
    portal_copy's TransferEngine can read the attachment file.

    Args:
        agent: Agent context dict (must contain at least 'id' and 'is_super').
        file_path: Virtual path starting with /_attachment/<attachment_id>.

    Returns:
        (ExecutionBackend, str) on success — backend is a LocalBackend,
          real_path is the absolute filesystem path to the attachment file.
        (None, str) on failure — str is an error message.
    """
    agent = agent or {}
    agent_id = agent.get('id', '')

    if not agent_id:
        return (None, "Attachment paths require an agent context.")

    # Extract attachment ID from /_attachment/<id>[/...]
    sub_path = file_path[len(_ATTACHMENT_PREFIX):].strip().rstrip("/")
    if not sub_path:
        return (None, "Invalid attachment path — no attachment ID specified "
                      "after /_attachment/.")

    # The first path segment is the attachment ID
    attachment_id_str = sub_path.split("/", 1)[0]

    try:
        attachment_id = int(attachment_id_str)
    except (TypeError, ValueError):
        return (None, f"Invalid attachment ID: {attachment_id_str!r} — "
                      f"must be an integer.")

    # Look up attachment in DB
    from models.db import db
    row = db.get_attachment(attachment_id)
    if not row:
        return (None, f"Attachment {attachment_id} not found or expired.")

    # Access control — agent must own the attachment or be super
    if row['agent_id'] != agent_id and not agent.get('is_super'):
        return (None, f"Access denied — attachment {attachment_id} belongs "
                      f"to a different agent.")

    # Verify the file exists on disk
    resolved_path = row.get('file_path')
    if not resolved_path or not os.path.isfile(resolved_path):
        return (None, "Attachment file is missing on disk. It may have been "
                      "moved by save_artifact, manually deleted, or expired "
                      "via retention cleanup.")

    # Return a LocalBackend that operates on raw filesystem paths
    from backend.tools.lib.backends.local_backend import LocalBackend
    backend = LocalBackend()
    return (backend, resolved_path)
