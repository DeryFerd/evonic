#!/usr/bin/env python3
"""
build_training_dataset.py — aggregate session_archive.db into a training-ready JSONL.

The archive (shared/db/session_archive.db) stores byte-exact LLM I/O captured at
inference time (one row per LLM API call). This script reconstructs one sample PER
AGENT TURN in OpenAI chat-messages format, ready for SFT:

    {
      "messages": [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "..."},
        {"role": "assistant", "reasoning_content": "CoT...", "content": "...",
         "tool_calls": [...]},
        {"role": "tool", "tool_call_id": "...", "content": "..."},
        ...
        {"role": "assistant", "reasoning_content": "CoT...", "content": "final"}
      ],
      "tools": [ ...function schemas the model could call... ],
      "meta": {"agent_id", "agent_kind", "parent_agent_id", "session_id",
               "model", "turn_index", "num_calls", "finish_reason", "usage"}
    }

Reconstruction (per turn, calls ordered by call_index):
  - messages = the LAST call's request.messages (the final, complete context the model
    actually saw — includes every prior assistant tool_call, tool result, and any
    system re-injection), then the final assistant response is appended.
  - Per-step CoT is recovered by matching tool_call ids → reasoning_content from each
    call's raw response (robust to mid-turn system re-injection; not order-dependent).
  - Anthropic-style payloads (system as a top-level field) are accommodated by
    prepending a system message when messages[0] is not already a system message.

Usage:
    python scripts/build_training_dataset.py
    python scripts/build_training_dataset.py --db shared/db/session_archive.db \
        --out dataset.jsonl --kind main,explorer --no-reasoning --min-calls 1
"""

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DB = os.path.join(_REPO_ROOT, "shared", "db", "session_archive.db")


def _loads(s: Optional[str]) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None


def _extract_assistant(response: Any) -> Optional[Dict[str, Any]]:
    """Build an OpenAI-style assistant message from a raw provider response."""
    if not isinstance(response, dict):
        return None
    # OpenAI-compatible
    choices = response.get("choices")
    if choices:
        msg = (choices[0] or {}).get("message", {}) or {}
        out: Dict[str, Any] = {"role": "assistant"}
        out["content"] = msg.get("content")
        cot = msg.get("reasoning_content") or msg.get("reasoning")
        if cot:
            out["reasoning_content"] = cot
        if msg.get("tool_calls"):
            out["tool_calls"] = msg["tool_calls"]
        return out
    # Anthropic-style: content is a list of blocks
    content = response.get("content")
    if isinstance(content, list):
        text_parts, tool_calls = [], []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {"name": block.get("name"),
                                 "arguments": json.dumps(block.get("input", {}),
                                                         ensure_ascii=False)},
                })
        out = {"role": "assistant", "content": "\n".join(text_parts) or None}
        if tool_calls:
            out["tool_calls"] = tool_calls
        return out
    return None


def _tool_call_ids(msg: Dict[str, Any]) -> List[str]:
    ids = []
    for tc in (msg.get("tool_calls") or []):
        if isinstance(tc, dict) and tc.get("id"):
            ids.append(tc["id"])
    return ids


def _build_turn_sample(calls: List[sqlite3.Row], meta: Dict[str, Any],
                       include_reasoning: bool) -> Optional[Dict[str, Any]]:
    """Reconstruct one OpenAI chat-messages sample from a turn's ordered calls."""
    parsed = []
    for c in calls:
        req = _loads(c["request_json"])
        resp = _loads(c["response_json"])
        if req is None:
            continue
        parsed.append((c, req, resp))
    if not parsed:
        return None

    last_c, last_req, last_resp = parsed[-1]
    messages = list(last_req.get("messages") or [])

    # Anthropic: system lives outside messages — prepend it if missing.
    sys_field = last_req.get("system")
    if sys_field and not (messages and messages[0].get("role") == "system"):
        sys_text = sys_field if isinstance(sys_field, str) else json.dumps(sys_field, ensure_ascii=False)
        messages = [{"role": "system", "content": sys_text}] + messages

    # Append the final assistant response to complete the turn.
    final_asst = _extract_assistant(last_resp)
    if final_asst is None:
        return None
    messages = messages + [final_asst]

    # Recover per-step CoT: tool_call id -> reasoning_content from each call's response.
    if include_reasoning:
        cot_by_id: Dict[str, str] = {}
        for _c, _req, _resp in parsed:
            asst = _extract_assistant(_resp)
            if not asst:
                continue
            cot = asst.get("reasoning_content")
            if not cot:
                continue
            for tcid in _tool_call_ids(asst):
                cot_by_id[tcid] = cot
        for m in messages:
            if m.get("role") == "assistant" and not m.get("reasoning_content"):
                for tcid in _tool_call_ids(m):
                    if tcid in cot_by_id:
                        m["reasoning_content"] = cot_by_id[tcid]
                        break
    else:
        for m in messages:
            m.pop("reasoning_content", None)

    tools = last_req.get("tools")

    # usage: sum across the turn's calls
    pt = sum((c["prompt_tokens"] or 0) for c in calls)
    ct = sum((c["completion_tokens"] or 0) for c in calls)
    sample = {
        "messages": messages,
        "tools": tools,
        "meta": {
            **meta,
            "turn_index": last_c["turn_index"],
            "num_calls": len(calls),
            "model": last_c["model"],
            "finish_reason": last_c["finish_reason"],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                      "total_tokens": pt + ct},
        },
    }
    return sample


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=_DEFAULT_DB, help="path to session_archive.db")
    ap.add_argument("--out", default=os.path.join(_REPO_ROOT, "dataset.jsonl"),
                    help="output JSONL path")
    ap.add_argument("--kind", default="",
                    help="comma-separated agent_kind filter (main,sub,explorer,organizer). Empty = all")
    ap.add_argument("--min-calls", type=int, default=1,
                    help="skip turns with fewer than N LLM calls")
    ap.add_argument("--no-reasoning", action="store_true",
                    help="strip reasoning_content (CoT) from assistant messages")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"error: archive DB not found: {args.db}", file=sys.stderr)
        print("       (run agents with EVONIC_SESSION_ARCHIVE=1 first)", file=sys.stderr)
        return 1

    kinds = {k.strip() for k in args.kind.split(",") if k.strip()}
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    sessions = conn.execute(
        "SELECT id, session_id, agent_id, agent_kind, parent_agent_id, external_user_id "
        "FROM archive_sessions ORDER BY id ASC"
    ).fetchall()

    n_samples = 0
    n_turns_skipped = 0
    stats_by_kind: Dict[str, int] = defaultdict(int)

    with open(args.out, "w", encoding="utf-8") as out:
        for s in sessions:
            if kinds and (s["agent_kind"] or "main") not in kinds:
                continue
            rows = conn.execute(
                "SELECT * FROM archive_llm_calls WHERE archive_id = ? ORDER BY call_index ASC",
                (s["id"],),
            ).fetchall()
            if not rows:
                continue
            # Group this session's calls by turn_index (preserve order).
            turns: "defaultdict[Any, List[sqlite3.Row]]" = defaultdict(list)
            order: List[Any] = []
            for r in rows:
                ti = r["turn_index"]
                if ti not in turns:
                    order.append(ti)
                turns[ti].append(r)

            base_meta = {
                "agent_id": s["agent_id"],
                "agent_kind": s["agent_kind"] or "main",
                "parent_agent_id": s["parent_agent_id"],
                "session_id": s["session_id"],
                "external_user_id": s["external_user_id"],
            }
            for ti in order:
                calls = turns[ti]
                if len(calls) < args.min_calls:
                    n_turns_skipped += 1
                    continue
                sample = _build_turn_sample(calls, base_meta, not args.no_reasoning)
                if sample is None:
                    n_turns_skipped += 1
                    continue
                out.write(json.dumps(sample, ensure_ascii=False) + "\n")
                n_samples += 1
                stats_by_kind[base_meta["agent_kind"]] += 1

    conn.close()
    print(f"Wrote {n_samples} samples to {args.out}")
    if stats_by_kind:
        print("  by agent_kind: " + ", ".join(f"{k}={v}" for k, v in sorted(stats_by_kind.items())))
    if n_turns_skipped:
        print(f"  skipped {n_turns_skipped} turn(s) (below --min-calls or unparseable)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
