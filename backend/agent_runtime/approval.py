"""
Human-in-the-loop approval registry for tool calls blocked by the heuristic safety system.

When a tool returns level='requires_approval', the LLM loop creates a PendingApproval,
emits an 'approval_required' event, then blocks on decision_event.wait() until the user
approves or rejects via the web UI or Telegram.
"""

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class PendingApproval:
    approval_id: str
    session_id: str
    agent_id: str
    tool_call_id: str
    tool_name: str
    tool_args: dict
    safety_result: dict
    created_at: float = field(default_factory=time.time)
    decision_event: threading.Event = field(default_factory=threading.Event)
    decision: Optional[str] = None  # 'approve' or 'reject'


class ApprovalRegistry:
    """Thread-safe registry for pending tool-call approvals. One per session at a time."""

    def __init__(self):
        self._pending: Dict[str, PendingApproval] = {}  # approval_id -> PendingApproval
        self._by_session: Dict[str, str] = {}           # session_id -> approval_id
        self._lock = threading.Lock()

    def create(self, session_id: str, agent_id: str, tool_call_id: str,
               tool_name: str, tool_args: dict, safety_result: dict) -> PendingApproval:
        """Register a new pending approval and return it."""
        approval_id = str(uuid.uuid4())
        pa = PendingApproval(
            approval_id=approval_id,
            session_id=session_id,
            agent_id=agent_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            tool_args=tool_args,
            safety_result=safety_result,
        )
        with self._lock:
            self._cleanup_expired_locked()
            self._pending[approval_id] = pa
            self._by_session[session_id] = approval_id
        return pa

    def resolve(self, approval_id: str, decision: str) -> bool:
        """Set the decision and signal the waiting loop thread. Returns False if not found."""
        with self._lock:
            pa = self._pending.get(approval_id)
            if pa is None or pa.decision is not None:
                return False
            pa.decision = decision
        pa.decision_event.set()
        return True

    def get(self, approval_id: str) -> Optional[PendingApproval]:
        with self._lock:
            return self._pending.get(approval_id)

    def list_pending(self) -> list:
        """Return all still-unresolved approvals as plain dicts.

        This is the freshest source of truth for what is actively blocking an
        agent right now (same registry the /chat/approve endpoint checks). Used
        by the web UI's polling safety-net so the approval modal is delivered
        even if the live SSE 'approval_required' event was lost in transit.
        """
        with self._lock:
            items = list(self._pending.values())
        out = []
        for pa in items:
            if pa.decision is not None:
                continue  # already resolved, awaiting cleanup
            sr = pa.safety_result or {}
            out.append({
                'approval_id': pa.approval_id,
                'agent_id': pa.agent_id,
                'session_id': pa.session_id,
                'tool': pa.tool_name,
                'args': pa.tool_args,
                'approval_info': sr.get('approval_info', {}),
                'reasons': sr.get('reasons', []),
                'score': sr.get('score'),
                'created_at': pa.created_at,
            })
        return out

    def get_by_session(self, session_id: str) -> Optional[PendingApproval]:
        with self._lock:
            approval_id = self._by_session.get(session_id)
            if approval_id:
                return self._pending.get(approval_id)
            return None

    def remove(self, approval_id: str):
        with self._lock:
            pa = self._pending.pop(approval_id, None)
            if pa:
                self._by_session.pop(pa.session_id, None)

    def _cleanup_expired_locked(self, timeout_seconds: int = 600):
        """Remove stale entries (must hold _lock)."""
        now = time.time()
        stale = [aid for aid, pa in self._pending.items()
                 if now - pa.created_at > timeout_seconds]
        for aid in stale:
            pa = self._pending.pop(aid)
            self._by_session.pop(pa.session_id, None)


approval_registry = ApprovalRegistry()
