"""
Agent Runtime package.

Public API (unchanged from the old single-file module):
  - AgentRuntime   — the runtime class
  - agent_runtime  — the global singleton instance
  - DEFAULT_SUMMARIZE_PROMPT — the default summarization prompt template
"""

import logging
import threading

from backend.agent_runtime.runtime import AgentRuntime
from backend.agent_runtime.summarizer import DEFAULT_SUMMARIZE_PROMPT

log = logging.getLogger(__name__)

# Global singleton — started once at import time (workers launched in __init__)
agent_runtime = AgentRuntime()


_FREE_NOTIFY_DELAY = 6  # seconds — debounce rapid busy→free transitions
_free_notify_timers: dict[str, threading.Timer] = {}
_free_notify_timers_lock = threading.Lock()


def _on_agent_busy_changed(event):
    """Debounce busy→free transitions; send notification after agent stays idle."""
    agent_id = event.get('agent_id')
    if not agent_id:
        return

    if event.get('busy'):
        # Agent became busy again — cancel any pending notification
        with _free_notify_timers_lock:
            timer = _free_notify_timers.pop(agent_id, None)
        if timer:
            timer.cancel()
        return

    # Agent became free — check if there's a pending notification
    with AgentRuntime._free_notify_lock:
        if agent_id not in AgentRuntime._free_notify_pending:
            return

    # Schedule delayed send; cancelled if agent goes busy again before it fires
    with _free_notify_timers_lock:
        old = _free_notify_timers.pop(agent_id, None)
        if old:
            old.cancel()
        t = threading.Timer(_FREE_NOTIFY_DELAY, _send_free_notification, args=(agent_id,))
        t.daemon = True
        _free_notify_timers[agent_id] = t
        t.start()


def _send_free_notification(agent_id: str):
    """Actually deliver the free-notification after the debounce delay."""
    with _free_notify_timers_lock:
        _free_notify_timers.pop(agent_id, None)

    # Re-check: agent may have gone busy again during the delay
    if agent_runtime.is_agent_busy(agent_id):
        log.debug("[AgentFreeNotify] agent=%s went busy again during delay — skipping", agent_id)
        return

    with AgentRuntime._free_notify_lock:
        pending = AgentRuntime._free_notify_pending.pop(agent_id, None)
    if not pending:
        return

    session_id = pending['session_id']
    external_user_id = pending['external_user_id']
    channel_id = pending.get('channel_id')

    log.info("[AgentFreeNotify] agent=%s is free — sending notification to session=%s user=%s",
             agent_id, session_id, external_user_id)

    from models.db import db
    notify_msg = "Hey! I'm done and ready to help again. Is there anything I can do?"
    try:
        db.add_chat_message(session_id, 'assistant', notify_msg,
                            agent_id=agent_id, metadata={"free_notification": True})
    except Exception as e:
        log.error("[AgentFreeNotify] Failed to save notification message: %s", e)

    # Push SSE event so the web chat UI renders the notification immediately
    try:
        from backend.event_stream import event_stream as _es
        _es.emit('message_received', {
            'agent_id': agent_id,
            'session_id': session_id,
            'external_user_id': external_user_id,
            'channel_id': channel_id,
        })
    except Exception as e:
        log.error("[AgentFreeNotify] Failed to emit message_received event: %s", e)

    # Send via channel if applicable
    if channel_id:
        try:
            from backend.channels.registry import channel_manager
            instance = channel_manager._active.get(channel_id)
            if instance and instance.is_running:
                instance.send_message(external_user_id, notify_msg)
        except Exception as e:
            log.error("[AgentFreeNotify] Failed to send via channel=%s: %s", channel_id, e)


def _on_summary_updated(event):
    """After summarization, run the single knowledge pipeline in background.

    Calls ``process_knowledge()`` which extracts entities, inserts inline
    wiki-links into existing KB docs, and emits ``doc_updated``.
    """
    # event_stream.emit() delivers the data dict FLAT (no 'payload' wrapper), so
    # read fields off the event directly. (Falling back to a 'payload' key keeps
    # this resilient if an emitter ever wraps.)
    payload = event.get('payload') or event
    agent_id = payload.get('agent_id')
    session_id = payload.get('session_id')
    summary = payload.get('summary')
    # Latest turns not yet folded into the summary — handed to the Knowledge
    # Organizer as conversation context so freshly-mentioned info isn't lost.
    tail_messages = payload.get('tail_messages') or []
    if not (agent_id and session_id and summary):
        return

    import threading
    from backend.agent_runtime.memory_manager import process_knowledge
    from backend.llm_usage_events import usage_context
    from models.db import db

    agent = db.get_agent(agent_id)
    if not agent:
        return

    def _run_extract():
        with usage_context('memory', agent_id, agent.get('name'), session_id):
            process_knowledge(agent, session_id, summary,
                              AgentRuntime._llm_serializer._llm_lock,
                              recent_messages=tail_messages)

    threading.Thread(target=_run_extract, daemon=True).start()


def _on_doc_updated(event):
    """When KB docs are modified, trigger an evomem sync.

    The evomem binary scans the updated markdown files, parses inline wiki-links,
    and rebuilds the graph database (.evomem.db).
    """
    payload = event.get('payload', {})
    agent_id = payload.get('agent_id')
    modified_slugs = payload.get('modified_slugs', [])
    if not agent_id:
        return
    log.info("[doc_updated] agent=%s modified=%d slug(s) -- running sync",
             agent_id, len(modified_slugs))
    from backend.agent_runtime.evomem_client import sync as evomem_sync
    try:
        evomem_sync(agent_id)
    except Exception as e:
        log.warning("[doc_updated] sync failed for %s: %s", agent_id, e)


# Register event listeners
try:
    from backend.event_stream import event_stream
    event_stream.on('agent_busy_changed', _on_agent_busy_changed)
    event_stream.on('summary_updated', _on_summary_updated)
    event_stream.on('doc_updated', _on_doc_updated)
    # Auto-forward sub-agent/inter-agent replies to the originating agent's session.
    # Must be registered here (not lazily in agent_messaging.py) so it fires
    # regardless of whether agent_messaging tools have been loaded yet.
    from backend.tools.agent_messaging import _on_final_answer
    event_stream.on('final_answer', _on_final_answer)
except Exception:
    pass


__all__ = ['AgentRuntime', 'agent_runtime', 'DEFAULT_SUMMARIZE_PROMPT']
