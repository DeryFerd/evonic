"""Unit tests for the Knowledge Organizer KB pipeline (memory_manager).

Covers the pure helpers (doc listing, slug resolution, JSON parsing) and the
``_author_docs`` integration with the organizer sub-agent mocked: the
create-collision guard (never mint a duplicate), update-by-slug for name variants,
skip-on-failure, and the server-local /_self/kb config used so Grep/Read/Glob hit
the server KB even on a remote workplace.
"""

import glob
import os
import threading
from unittest.mock import patch

import pytest

import backend.agent_runtime.notifier as notifier_mod
import backend.subagent_manager as subagent_mod
import backend.agent_report_to as report_mod
import backend.event_stream as event_mod
import backend.tools._workspace as workspace_mod
from backend.agent_runtime import evomem_writer as W
from backend.agent_runtime import memory_manager as M

AGENT = "kb_org_test"


@pytest.fixture
def brain(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EVONIC_MEMORY_ENGINE", "evomem")
    monkeypatch.setenv("EVOMEM_KB_ORGANIZER", "on")
    monkeypatch.setenv("EVOMEM_KB_ORGANIZER_DEBOUNCE_SECONDS", "0")  # don't debounce in tests
    M._organizer_running.clear()
    M._organizer_last_start.clear()
    return tmp_path


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_kb_doc_listing_surfaces_alias(brain):
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="Daerah di Jakarta.",
                 body="Gambir adalah daerah di [[Jakarta]].", aliases=["Stasiun Gambir"])
    text, name_to_slug = M._kb_doc_listing(AGENT)
    assert "[gambir] Gambir" in text
    assert "aka Stasiun Gambir" in text
    assert name_to_slug["gambir"] == "gambir"
    assert name_to_slug["stasiun gambir"] == "gambir"  # alias maps to the canonical doc


def test_kb_doc_listing_empty(brain):
    text, name_to_slug = M._kb_doc_listing(AGENT)
    assert text == "(none yet)" and name_to_slug == {}


def test_resolve_doc_slug2_variants(brain):
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="d", body="b")
    n2s = {"gambir": "gambir", "stasiun gambir": "gambir"}
    # alias/title variant resolves to the canonical doc
    assert M._resolve_doc_slug2(AGENT, "", "Stasiun Gambir", "", n2s) == "gambir"
    # exact title
    assert M._resolve_doc_slug2(AGENT, "", "Gambir", "", n2s) == "gambir"
    # slug hint wins
    assert M._resolve_doc_slug2(AGENT, "gambir", "Whatever", "", {}) == "gambir"
    # unrelated subject → create (None)
    assert M._resolve_doc_slug2(AGENT, "", "Bandung", "", n2s) is None


def test_format_recent_messages():
    msgs = [
        {"role": "user", "content": "Aku ke Jakarta naik Kereta Bima"},
        {"role": "assistant", "content": "Wah, asik!"},
        {"role": "user", "content": "Besok Minggu 28 Juni 2026 ada acara lagi"},
    ]
    out = M._format_recent_messages(msgs, n=6)
    assert "User: Aku ke Jakarta naik Kereta Bima" in out
    assert "Assistant: Wah, asik!" in out
    assert "Besok Minggu 28 Juni 2026" in out
    # honors the tail window and tolerates 'type' (jsonl) shape
    assert M._format_recent_messages([{"type": "user", "content": "hi"}]) == "User: hi"
    assert M._format_recent_messages([]) == ""
    assert M._format_recent_messages(None) == ""


def test_parse_organizer_docs():
    p = M._parse_organizer_docs
    op = '{"action": "create", "title": "X", "type": "note", "description": "d", "body": "b"}'
    # canonical
    assert p('{"docs": [%s]}' % op)[0]["title"] == "X"
    assert p('{"docs": []}') == []
    # fenced + prose before AND after (the real failure the user saw)
    assert p("```json\n{\"docs\": [%s]}\n```" % op)[0]["title"] == "X"
    assert p('Sure! Here is the result:\n{"docs": [%s]}\nI have filed it.' % op)[0]["title"] == "X"
    # prose contains an UNRELATED brace object before the real ops — must skip it
    assert p('ctx {"unrelated": true}. result: {"docs": [%s]}' % op)[0]["title"] == "X"
    # trailing comma (common LLM slip)
    assert p('{"docs": [%s,]}' % op)[0]["title"] == "X"
    # missing wrapper: bare array of ops, or a single bare op
    assert p('[%s]' % op)[0]["title"] == "X"
    assert p(op)[0]["title"] == "X"
    # thinking tags around it
    assert p('<think>let me decide</think>\n{"docs": [%s]}' % op)[0]["title"] == "X"
    # genuinely unusable
    assert p("not json at all") is None
    assert p('{"items": []}') is None
    assert p('') is None
    assert p(None) is None


# ── _author_docs integration (organizer mocked) ──────────────────────────────

def _run_author(ops):
    """Drive _author_docs with the organizer returning ``ops`` (list or None)."""
    with patch.object(M, "_get_active_collection", return_value=""), \
         patch.object(M, "_kb_organizer_infra_ok", return_value=True), \
         patch.object(M, "_spawn_kb_organizer", return_value=ops), \
         patch.object(W, "mark_dirty"), \
         patch.object(M, "_emit_doc_updated"):
        M._author_docs({"id": AGENT}, "s", "some source", threading.Lock())


def _kb_glob(pattern):
    return glob.glob(os.path.join(f"agents/{AGENT}/kb", pattern))


def test_collision_guard_blocks_duplicate_on_mislabeled_create(brain):
    """Organizer wrongly says CREATE for an entity that already exists (exact title)
    → the collision guard converts it to an update; no duplicate file is minted."""
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="d",
                 body="Gambir adalah daerah di Jakarta.")
    _run_author([{"action": "create", "title": "Gambir", "type": "place",
                  "description": "d", "body": "User menambah foto ![f](http://x/g.jpg)."}])
    assert len(_kb_glob("gambir*.md")) == 1                    # no duplicate
    assert "![f](http://x/g.jpg)" in W.read_doc(AGENT, "gambir")["body"]


def test_update_by_slug_for_name_variant(brain):
    """Organizer returns update with the existing slug for a name variant
    ("Stasiun Gambir" → gambir) → appends to the existing doc, no new file."""
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="d",
                 body="Gambir adalah daerah.")
    _run_author([{"action": "update", "slug": "gambir", "title": "Stasiun Gambir",
                  "type": "place", "description": "d", "body": "Foto baru ![f](http://x/g.jpg)."}])
    assert not os.path.exists(f"agents/{AGENT}/kb/stasiun-gambir.md")
    assert len(_kb_glob("gambir*.md")) == 1
    assert "![f](http://x/g.jpg)" in W.read_doc(AGENT, "gambir")["body"]


def test_skip_when_organizer_returns_none(brain):
    """Organizer failure → skip entirely (never a dupe-prone fallback); no crash."""
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="d", body="orig body")
    before = W.read_doc(AGENT, "gambir")["body"]
    _run_author(None)
    assert len(_kb_glob("*.md")) == 1
    assert W.read_doc(AGENT, "gambir")["body"] == before      # untouched


# ── rename (fix typo'd doc name) ─────────────────────────────────────────────

def test_rename_doc(brain):
    """evomem_writer.rename_doc fixes a misspelled doc name, keeping the old name
    as an alias and preserving the body."""
    W.upsert_doc(AGENT, title="Yonix", doc_type="person", description="teman",
                 body="Yonix teman SD [[Robin]].")
    new = W.rename_doc(AGENT, "yonix", "Yondix")
    assert new == "yondix"
    assert not os.path.exists(f"agents/{AGENT}/kb/yonix.md")     # old file gone
    d = W.read_doc(AGENT, "yondix")
    assert d["frontmatter"]["title"] == "Yondix"
    assert "Yonix" in (d["frontmatter"].get("aliases") or [])   # old name → alias
    assert "teman SD [[Robin]]" in d["body"]                     # body preserved


def test_author_docs_rename_action(brain):
    """The organizer's 'rename' op renames the doc and folds in new prose."""
    W.upsert_doc(AGENT, title="Yonix", doc_type="person", description="d", body="orig.")
    _run_author([{"action": "rename", "slug": "yonix", "new_title": "Yondix",
                  "body": "Teman SD di [[Magelang]]."}])
    assert not os.path.exists(f"agents/{AGENT}/kb/yonix.md")
    d = W.read_doc(AGENT, "yondix")
    assert d is not None and d["frontmatter"]["title"] == "Yondix"
    assert "Teman SD di [[Magelang]]" in d["body"]               # new info folded in


# ── single-instance + debounce ───────────────────────────────────────────────

def test_organizer_single_instance_and_debounce():
    M._organizer_running.clear()
    M._organizer_last_start.clear()
    a = "agentX"
    # single instance: a second claim is refused while one is running
    assert M._organizer_try_claim(a, 0) is True
    assert M._organizer_try_claim(a, 0) is False
    M._organizer_release(a)
    # debounce: after release, an immediate re-claim within the window is refused
    M._organizer_last_start.clear()
    assert M._organizer_try_claim(a, 100) is True
    M._organizer_release(a)
    assert M._organizer_try_claim(a, 100) is False
    # a different agent is unaffected
    assert M._organizer_try_claim("agentY", 100) is True
    M._organizer_running.clear()
    M._organizer_last_start.clear()


def test_on_summary_updated_reads_flat_event(monkeypatch):
    """event_stream delivers the data dict flat — the handler must read fields off
    the event directly (not event['payload']), or it never reaches process_knowledge."""
    import backend.agent_runtime as AR
    from models.db import db
    seen = {}
    # get_agent is only reached if agent_id/summary were parsed from the event;
    # returning None makes the handler stop before spawning a thread.
    monkeypatch.setattr(db, "get_agent", lambda aid: seen.__setitem__("aid", aid))
    AR._on_summary_updated({"agent_id": "aisyah", "session_id": "s1", "summary": "sum"})
    assert seen.get("aid") == "aisyah"


def test_author_docs_skips_when_organizer_busy(brain):
    """A second summary while an organizer is already running spawns nothing."""
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="d", body="b")
    M._organizer_running.add(AGENT)                  # simulate an in-flight organizer
    called = {"spawn": False}

    def _spy(*a, **k):
        called["spawn"] = True
        return []

    try:
        with patch.object(M, "_get_active_collection", return_value=""), \
             patch.object(M, "_kb_organizer_infra_ok", return_value=True), \
             patch.object(M, "_spawn_kb_organizer", _spy):
            M._author_docs({"id": AGENT}, "s", "src", threading.Lock())
    finally:
        M._organizer_running.discard(AGENT)
    assert called["spawn"] is False                  # no double-spawn
    assert len(_kb_glob("*.md")) == 1                # nothing written


# ── server-local /_self/kb config (remote-workplace correctness) ─────────────

class _FakeES:
    def __init__(self):
        self.cbs = []

    def on(self, name, cb):
        self.cbs.append(cb)

    def off(self, name, cb):
        if cb in self.cbs:
            self.cbs.remove(cb)


def test_spawn_organizer_forces_server_local_kb_config(tmp_path):
    """The organizer config pins server-local execution at /_self/kb so its file
    tools work even when the parent agent runs on a remote workplace."""
    kb_dir = tmp_path / "kb"
    kb_dir.mkdir()
    captured = {}
    es = _FakeES()

    def _fake_spawn(parent, build_config, id_kind="explorer"):
        captured["cfg"] = build_config("eid")
        captured["id_kind"] = id_kind
        return "eid"

    def _fake_notify(**kwargs):
        captured.setdefault("messages", []).append(kwargs.get("message", ""))
        for cb in list(es.cbs):
            cb({"agent_id": "eid", "answer": '{"docs": []}'})
        return {"success": True, "session_id": "sess"}

    agent = {"id": "a1", "name": "A", "user_id": "u", "channel_id": "c",
             "workplace_id": "remote-wp", "sandbox_enabled": 1, "run_as_user": "someuser"}

    with patch.object(workspace_mod, "resolve_self_path", return_value=str(kb_dir)), \
         patch.object(subagent_mod.subagent_manager, "spawn_explorer", _fake_spawn), \
         patch.object(subagent_mod.subagent_manager, "destroy", return_value=None), \
         patch.object(notifier_mod, "notify_agent", _fake_notify), \
         patch.object(report_mod, "resolve_report_to_for_subagent_spawn",
                      return_value=(None, None, None)), \
         patch.object(event_mod, "event_stream", es):
        result = M._spawn_kb_organizer(agent, "the summary", "User: ke Jakarta naik Bima")

    assert result == []                                       # parsed {"docs": []}
    cfg = captured["cfg"]
    assert cfg["workspace"] == str(kb_dir)                     # server-local KB
    assert cfg["workplace_id"] is None                        # not the remote workplace
    assert cfg["sandbox_enabled"] == 0
    assert cfg["run_as_user"] is None
    assert cfg["system_prompt"] == M._KB_ORGANIZER_SYSTEM_PROMPT
    assert captured["id_kind"] == "organizer"   # spawned as ..._organizer_N (UI label)
    assert cfg.get("is_kb_organizer") is True
    # The task hands the agent the recent conversation + summary, and does NOT
    # pre-list docs (the agent explores the vault itself).
    msg = captured["messages"][0]
    assert "RECENT CONVERSATION" in msg and "User: ke Jakarta naik Bima" in msg
    assert "the summary" in msg
    assert "KNOWN DOCS" not in msg


def test_author_docs_passes_recent_messages_to_organizer(brain):
    """_author_docs formats tail_messages and hands them to the organizer."""
    W.upsert_doc(AGENT, title="Gambir", doc_type="place", description="d", body="b")
    seen = {}

    def _capture(agent, source_text, recent_text=""):
        seen["recent"] = recent_text
        return []  # no ops

    with patch.object(M, "_get_active_collection", return_value=""), \
         patch.object(M, "_kb_organizer_infra_ok", return_value=True), \
         patch.object(M, "_spawn_kb_organizer", _capture):
        M._author_docs({"id": AGENT}, "s", "summary text", threading.Lock(),
                       recent_messages=[{"role": "user", "content": "halo dari Jakarta"}])
    assert "User: halo dari Jakarta" in seen["recent"]
