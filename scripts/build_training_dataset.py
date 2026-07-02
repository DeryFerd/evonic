#!/usr/bin/env python3
"""
build_training_dataset.py — build training-ready JSONL datasets from Evonic's
archived LLM I/O.

Two independent sources, written to SEPARATE files (no cross-source dedup):

  1. session_archive.db  — curated, committed sessions (main on /clear, single-turn
                           sub-agents on turn-end). Always complete turns.
  2. llm_traces/*.jsonl  — raw per-call staging logs under agents/<id>/llm_traces/.
                           Captures everything inference produced, including sessions
                           not yet /clear'd and multi-turn sub-agents not in the DB.

Both are reconstructed into one sample PER AGENT TURN, in OpenAI chat-messages format
(chain-of-thought preserved as `reasoning_content`). A completeness guard drops any
turn whose final call is still a tool_call (an in-progress turn from a live session) —
so only turns that ended with a real answer are emitted.

Reconstruction (per turn, calls in order):
  - messages = the LAST call's request.messages (the final, complete context the model
    actually saw — includes every prior assistant tool_call, tool result, and any
    system re-injection), then the final assistant response is appended.
  - Per-step CoT is recovered by matching tool_call ids → reasoning_content from each
    call's raw response (robust to mid-turn system re-injection; not order-dependent).
  - Anthropic-style payloads (system as a top-level field) are accommodated.

Usage:
    python scripts/build_training_dataset.py                      # both → two files
    python scripts/build_training_dataset.py --source db
    python scripts/build_training_dataset.py --source traces --out-traces traces.jsonl
    python scripts/build_training_dataset.py --kind explorer,organizer --no-reasoning
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_DB = os.path.join(_REPO_ROOT, "shared", "db", "session_archive.db")
_DEFAULT_AGENTS = os.path.join(_REPO_ROOT, "agents")


_SLASH_CMD_RE = re.compile(r'^/[\w-]+')


def _filter_slash_commands(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Remove user messages that look like slash commands and their assistant responses.

    Slash commands bypass the LLM and should never appear in training data.
    Only filters messages where content starts with /command (matching the
    parse_command() pattern in backend/slash_commands.py).
    """
    filtered: List[Dict[str, Any]] = []
    skip_next_assistant = False
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if role == "user" and isinstance(content, str) and _SLASH_CMD_RE.match(content.strip()):
            skip_next_assistant = True
            continue
        if skip_next_assistant and role == "assistant" and not msg.get("tool_calls"):
            skip_next_assistant = False
            continue
        skip_next_assistant = False
        filtered.append(msg)
    return filtered


def _loads(s: Any) -> Any:
    if s is None or isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def _extract_assistant(response: Any) -> Optional[Dict[str, Any]]:
    """Build an OpenAI-style assistant message from a raw provider response."""
    if not isinstance(response, dict):
        return None
    choices = response.get("choices")
    if choices:
        msg = (choices[0] or {}).get("message", {}) or {}
        out: Dict[str, Any] = {"role": "assistant", "content": msg.get("content")}
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
                    "id": block.get("id"), "type": "function",
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
    return [tc["id"] for tc in (msg.get("tool_calls") or [])
            if isinstance(tc, dict) and tc.get("id")]


def _build_turn_sample(records: List[Dict[str, Any]], meta: Dict[str, Any],
                       include_reasoning: bool,
                       filter_slash: bool = True) -> Optional[Dict[str, Any]]:
    """Reconstruct one OpenAI chat-messages sample from a turn's ordered records.

    Each record: {request, response, turn_index, model, finish_reason, pt, ct}.
    Returns None if the turn is empty or incomplete (final call still a tool_call).
    """
    parsed = [r for r in records if r.get("request")]
    if not parsed:
        return None

    last = parsed[-1]
    last_req = last["request"]
    messages = list(last_req.get("messages") or [])

    # Anthropic: system lives outside messages — prepend it if missing.
    sys_field = last_req.get("system")
    if sys_field and not (messages and messages[0].get("role") == "system"):
        sys_text = sys_field if isinstance(sys_field, str) else json.dumps(sys_field, ensure_ascii=False)
        messages = [{"role": "system", "content": sys_text}] + messages

    final_asst = _extract_assistant(last["response"])
    if final_asst is None:
        return None
    # Completeness guard: a finished turn ends with a real answer, not a pending
    # tool call. Drops in-progress turns pulled from live (un-cleared) trace files.
    if final_asst.get("tool_calls"):
        return None

    messages = messages + [final_asst]

    if include_reasoning:
        cot_by_id: Dict[str, str] = {}
        for r in parsed:
            asst = _extract_assistant(r.get("response"))
            if asst and asst.get("reasoning_content"):
                for tcid in _tool_call_ids(asst):
                    cot_by_id[tcid] = asst["reasoning_content"]
        for m in messages:
            if m.get("role") == "assistant" and not m.get("reasoning_content"):
                for tcid in _tool_call_ids(m):
                    if tcid in cot_by_id:
                        m["reasoning_content"] = cot_by_id[tcid]
                        break
    else:
        for m in messages:
            m.pop("reasoning_content", None)

    if filter_slash:
        messages = _filter_slash_commands(messages)
        if not messages or not any(m.get("role") == "user" for m in messages):
            return None

    pt = sum((r.get("pt") or 0) for r in parsed)
    ct = sum((r.get("ct") or 0) for r in parsed)
    return {
        "messages": messages,
        "tools": last_req.get("tools"),
        "meta": {
            **meta,
            "turn_index": last.get("turn_index"),
            "num_calls": len(parsed),
            "model": last.get("model"),
            "finish_reason": last.get("finish_reason"),
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
        },
    }


def _emit_turns(records_by_turn: Dict[Any, List[Dict[str, Any]]], turn_order: List[Any],
                base_meta: Dict[str, Any], out, args, stats: Dict[str, int]) -> None:
    """Build + write one sample per turn for a single session."""
    for ti in turn_order:
        recs = records_by_turn[ti]
        if len(recs) < args.min_calls:
            stats["skipped"] += 1
            continue
        sample = _build_turn_sample(recs, base_meta, not args.no_reasoning,
                                    filter_slash=not args.keep_slash_commands)
        if sample is None:
            stats["skipped"] += 1
            continue
        out.write(json.dumps(sample, ensure_ascii=False) + "\n")
        stats["samples"] += 1
        stats[f"kind:{base_meta.get('agent_kind', 'main')}"] += 1


def build_from_db(db_path: str, out_path: str, kinds, args) -> Dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sessions = conn.execute(
        "SELECT id, session_id, agent_id, agent_kind, parent_agent_id, external_user_id "
        "FROM archive_sessions ORDER BY id ASC"
    ).fetchall()
    stats: Dict[str, int] = defaultdict(int)
    with open(out_path, "w", encoding="utf-8") as out:
        for s in sessions:
            kind = s["agent_kind"] or "main"
            if kinds and kind not in kinds:
                continue
            rows = conn.execute(
                "SELECT * FROM archive_llm_calls WHERE archive_id = ? ORDER BY call_index ASC",
                (s["id"],),
            ).fetchall()
            by_turn: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
            order: List[Any] = []
            for r in rows:
                ti = r["turn_index"]
                if ti not in by_turn:
                    order.append(ti)
                by_turn[ti].append({
                    "request": _loads(r["request_json"]), "response": _loads(r["response_json"]),
                    "turn_index": ti, "model": r["model"], "finish_reason": r["finish_reason"],
                    "pt": r["prompt_tokens"], "ct": r["completion_tokens"],
                })
            base_meta = {
                "source": "db", "agent_id": s["agent_id"], "agent_kind": kind,
                "parent_agent_id": s["parent_agent_id"], "session_id": s["session_id"],
                "external_user_id": s["external_user_id"],
            }
            _emit_turns(by_turn, order, base_meta, out, args, stats)
    conn.close()
    return stats


def build_from_traces(agents_dir: str, out_path: str, kinds, args) -> Dict[str, int]:
    stats: Dict[str, int] = defaultdict(int)
    files = sorted(glob.glob(os.path.join(agents_dir, "*", "llm_traces", "*.jsonl")))
    with open(out_path, "w", encoding="utf-8") as out:
        for fp in files:
            # One file = one session. Read all records.
            recs: List[Dict[str, Any]] = []
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            recs.append(json.loads(line))
                        except (json.JSONDecodeError, ValueError):
                            continue
            except OSError:
                continue
            if not recs:
                continue
            first = recs[0]
            kind = first.get("agent_kind") or "main"
            if kinds and kind not in kinds:
                continue
            by_turn: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
            order: List[Any] = []
            for r in recs:
                ti = r.get("turn_index")
                if ti not in by_turn:
                    order.append(ti)
                usage = r.get("usage") or {}
                by_turn[ti].append({
                    "request": r.get("request"), "response": r.get("response"),
                    "turn_index": ti, "model": r.get("model"),
                    "finish_reason": r.get("finish_reason"),
                    "pt": usage.get("prompt_tokens"), "ct": usage.get("completion_tokens"),
                })
            base_meta = {
                "source": "trace", "agent_id": first.get("agent_id"), "agent_kind": kind,
                "parent_agent_id": first.get("parent_agent_id"),
                "session_id": first.get("session_id"), "external_user_id": None,
            }
            _emit_turns(by_turn, order, base_meta, out, args, stats)
    return stats


def _report(label: str, out_path: str, stats: Dict[str, int]) -> None:
    print(f"[{label}] wrote {stats.get('samples', 0)} samples → {out_path}")
    kinds = {k.split(":", 1)[1]: v for k, v in stats.items() if k.startswith("kind:")}
    if kinds:
        print("  by agent_kind: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    if stats.get("skipped"):
        print(f"  skipped {stats['skipped']} turn(s) (incomplete, below --min-calls, or unparseable)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["db", "traces", "all"], default="all",
                    help="which source(s) to build from (default: all → two files)")
    ap.add_argument("--db", default=_DEFAULT_DB, help="path to session_archive.db")
    ap.add_argument("--agents-dir", default=_DEFAULT_AGENTS,
                    help="agents/ dir containing <id>/llm_traces/*.jsonl")
    ap.add_argument("--out-db", default=os.path.join(_REPO_ROOT, "dataset_archive.jsonl"),
                    help="output JSONL for the session_archive.db dataset")
    ap.add_argument("--out-traces", default=os.path.join(_REPO_ROOT, "dataset_traces.jsonl"),
                    help="output JSONL for the llm_traces dataset")
    ap.add_argument("--kind", default="",
                    help="comma-separated agent_kind filter (main,sub,explorer,organizer)")
    ap.add_argument("--min-calls", type=int, default=1,
                    help="skip turns with fewer than N LLM calls")
    ap.add_argument("--no-reasoning", action="store_true",
                    help="strip reasoning_content (CoT) from assistant messages")
    ap.add_argument("--keep-slash-commands", action="store_true",
                    help="keep slash command messages (default: filter them out)")
    args = ap.parse_args()

    kinds = {k.strip() for k in args.kind.split(",") if k.strip()}
    did_any = False

    if args.source in ("db", "all"):
        if not os.path.exists(args.db):
            print(f"warning: archive DB not found: {args.db} — skipping db source", file=sys.stderr)
        else:
            _report("db", args.out_db, build_from_db(args.db, args.out_db, kinds, args))
            did_any = True

    if args.source in ("traces", "all"):
        if not os.path.isdir(args.agents_dir):
            print(f"warning: agents dir not found: {args.agents_dir} — skipping traces source",
                  file=sys.stderr)
        else:
            _report("traces", args.out_traces, build_from_traces(args.agents_dir, args.out_traces, kinds, args))
            did_any = True

    if not did_any:
        print("error: no source available to build from", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
