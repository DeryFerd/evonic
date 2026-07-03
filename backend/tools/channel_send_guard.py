"""
Cross-channel send safety guard.

Enforces global rate limits and debounce rules for all external channel
message sends (WhatsApp, Telegram, Discord). This is a SHARED guard —
all agents share the same rate limit and debounce timers.

Rules:
- Global rate limit: max 20 messages per 60 seconds (across ALL agents)
- Debounce: 2-second minimum gap between consecutive sends (soft delay —
  blocks until slot available, never rejects)
"""

import time
import threading
from typing import List

from backend.logging_config import get_logger

_logger = get_logger(__name__)

# Global rate limit: 20 messages per 60 seconds (across ALL agents)
_CHANNEL_SEND_RATE_MAX = 20
_CHANNEL_SEND_RATE_WINDOW = 60  # seconds

# Debounce: 2-second minimum gap between consecutive sends
_CHANNEL_SEND_DEBOUNCE = 2  # seconds

_channel_send_timestamps: List[float] = []
_channel_send_lock = threading.Lock()
_last_send_time: float = 0.0


def wait_for_send_slot(agent_id: str) -> None:
    """Block until rate limit and debounce allow a send.

    Returns immediately if the send is allowed.
    Blocks (sleeps) if rate-limited or debounced — never rejects.

    Args:
        agent_id: The agent attempting to send (for logging).
    """
    global _last_send_time
    while True:
        with _channel_send_lock:
            now = time.time()

            # Debounce check — enforce minimum gap between sends
            if now - _last_send_time < _CHANNEL_SEND_DEBOUNCE:
                sleep_time = _CHANNEL_SEND_DEBOUNCE - (now - _last_send_time)
                _logger.debug(
                    "Channel send debounce: agent %s waiting %.2fs",
                    agent_id, sleep_time,
                )
                time.sleep(sleep_time)
                continue

            # Rate limit check — prune expired timestamps
            _channel_send_timestamps[:] = [
                t for t in _channel_send_timestamps
                if now - t < _CHANNEL_SEND_RATE_WINDOW
            ]

            if len(_channel_send_timestamps) >= _CHANNEL_SEND_RATE_MAX:
                oldest = min(_channel_send_timestamps)
                sleep_time = (oldest + _CHANNEL_SEND_RATE_WINDOW) - now + 0.1
                sleep_time = max(sleep_time, 0.1)
                _logger.debug(
                    "Channel send rate limit: agent %s waiting %.2fs "
                    "(%d/%d in window)",
                    agent_id, sleep_time,
                    len(_channel_send_timestamps), _CHANNEL_SEND_RATE_MAX,
                )
                time.sleep(sleep_time)
                continue

            # Allowed — record timestamp
            _channel_send_timestamps.append(now)
            _last_send_time = now
        return


def _cleanup_timestamps() -> None:
    """Remove expired timestamps from the sliding window."""
    with _channel_send_lock:
        now = time.time()
        _channel_send_timestamps[:] = [
            t for t in _channel_send_timestamps
            if now - t < _CHANNEL_SEND_RATE_WINDOW
        ]


def _start_cleanup_daemon(interval: int = 300) -> None:
    """Launch a daemon thread that periodically cleans stale timestamps."""
    def _loop():
        while True:
            time.sleep(interval)
            try:
                _cleanup_timestamps()
            except Exception:
                pass

    threading.Thread(
        target=_loop, daemon=True, name="channel-send-guard-cleanup",
    ).start()


# Start cleanup daemon at module load
_start_cleanup_daemon()
