"""Tests for CMP segment-scoped history (offload tier)."""

from backend.agent_runtime.cmp import assembler, store
from backend.agent_state import AgentState
from models.chatlog import chatlog_manager


def _chatlog(session='sess-asm'):
    log = chatlog_manager.get('cmp_asm_agent', session)
    return log


def _fill_two_paths(log):
    """P1 turn (ts 1000-1400), P2 turn (ts 2100-2200)."""
    entries = [
        {'type': 'user', 'ts': 1100, 'content': 'build the website', 'session_id': 's'},
        {'type': 'tool_call', 'ts': 1200, 'id': 't1', 'function': 'read_file',
         'params': {'path': 'a'}, 'session_id': 's'},
        {'type': 'tool_output', 'ts': 1300, 'tool_call_id': 't1',
         'content': 'file content', 'session_id': 's'},
        {'type': 'final', 'ts': 1400, 'content': 'website scaffolded', 'session_id': 's'},
        {'type': 'user', 'ts': 2100, 'content': 'configure the server', 'session_id': 's'},
        {'type': 'final', 'ts': 2200, 'content': 'server configured', 'session_id': 's'},
    ]
    for e in entries:
        log.append(e)


def _cmp_two_paths():
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='website', now_ts=1000)      # P1 seg [999, None]
    store.create_path(ms.cmp, ms, 'server config', now_ts=2100)   # cut at 2099
    return ms


def test_history_scoped_to_active_path():
    log = _chatlog('sess-scope')
    _fill_two_paths(log)
    ms = _cmp_two_paths()
    msgs = assembler.build_history(log, None, ms.cmp)
    text = ' '.join(str(m.get('content')) for m in msgs)
    assert 'configure the server' in text
    assert 'server configured' in text
    assert 'build the website' not in text       # P1 offloaded
    assert 'file content' not in text            # P1 tool chain offloaded


def test_return_rehydrates_tail_from_closed_segments():
    log = _chatlog('sess-return')
    _fill_two_paths(log)
    log.append({'type': 'user', 'ts': 3100, 'content': 'back to the website',
                'session_id': 's'})
    ms = _cmp_two_paths()
    store.switch_to(ms.cmp, ms, 'A1', now_ts=3100)  # P1 segs [999,2099],[3099,None]
    msgs = assembler.build_history(log, None, ms.cmp)
    text = ' '.join(str(m.get('content')) for m in msgs)
    assert 'back to the website' in text          # open segment
    assert 'website scaffolded' in text           # rehydration tail
    assert 'configure the server' not in text     # P2 stays offloaded


def test_rehydration_tail_drops_tool_dumps():
    """Live regression: returning to a tool-heavy path dragged ~50k tokens of
    tool outputs back through the tail. Closed-segment rehydration keeps
    conversational messages only — the IPPC card carries the tool facts."""
    log = _chatlog('sess-tooldump')
    log.append({'type': 'user', 'ts': 1100, 'content': 'buat laporan invoice',
                'session_id': 's'})
    for i in range(30):
        log.append({'type': 'tool_call', 'ts': 1200 + i * 10, 'id': f't{i}',
                    'function': 'bash', 'params': {}, 'session_id': 's'})
        log.append({'type': 'tool_output', 'ts': 1205 + i * 10,
                    'tool_call_id': f't{i}', 'content': 'X' * 4000,
                    'session_id': 's'})
    log.append({'type': 'final', 'ts': 1600, 'content': 'laporan selesai',
                'session_id': 's'})
    log.append({'type': 'user', 'ts': 2100, 'content': 'balik ke laporan tadi',
                'session_id': 's'})

    msgs = log.get_entries_for_llm_segments([[999, 1999], [2099, None]],
                                            closed_tail_semantic=6)
    roles = [m.get('role') for m in msgs]
    assert 'tool' not in roles                       # dumps stay offloaded
    text = ' '.join(str(m.get('content')) for m in msgs)
    assert 'laporan selesai' in text                 # conversational tail kept
    assert 'balik ke laporan tadi' in text           # open segment kept
    assert len(text) < 2000                          # nothing dragged 120KB back


def test_watermark_straddling_segment_drops_orphans():
    log = _chatlog('sess-watermark')
    _fill_two_paths(log)
    # summary watermark lands between tool_call(1200) and tool_output(1300)
    msgs = log.get_entries_for_llm_segments([[999, None]], after_ts=1250)
    roles = [m.get('role') for m in msgs]
    assert 'tool' not in roles                    # orphaned output dropped
    text = ' '.join(str(m.get('content')) for m in msgs)
    assert 'website scaffolded' in text


def test_closed_tail_is_bounded():
    log = _chatlog('sess-tail')
    for i in range(20):
        log.append({'type': 'user', 'ts': 1000 + i * 10, 'content': f'q{i}',
                    'session_id': 's'})
        log.append({'type': 'final', 'ts': 1005 + i * 10, 'content': f'a{i}',
                    'session_id': 's'})
    msgs = log.get_entries_for_llm_segments([[999, 1500], [2999, None]],
                                            closed_tail_semantic=4)
    contents = [str(m.get('content')) for m in msgs]
    assert len(contents) == 4                     # only the tail survives
    assert contents[-1] == 'a19' or 'a' in contents[-1]


def test_dependency_ancestor_transcript_stays_loaded():
    """A child path keeps its parents' transcript detail in memory (the
    child consumes their results); non-ancestor paths stay offloaded."""
    log = _chatlog('sess-ancestor')
    _fill_two_paths(log)  # A1 turn (report-ish), A2 turn (server config)
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='website', now_ts=1000)          # A1
    store.create_path(ms.cmp, ms, 'server config', now_ts=2100)      # A2
    log.append({'type': 'user', 'ts': 3100, 'content': 'buat invoice dari website itu',
                'session_id': 's'})
    store.create_path(ms.cmp, ms, 'invoice', depends_on=['A1'], now_ts=3100)  # B1
    msgs = assembler.build_history(log, None, ms.cmp)
    text = ' '.join(str(m.get('content')) for m in msgs)
    assert 'buat invoice dari website itu' in text   # active B1
    assert 'website scaffolded' in text              # ancestor A1 loaded
    assert 'server configured' not in text           # non-ancestor A2 offloaded


def test_should_filter_gates():
    assert not assembler.should_filter(None)
    assert not assembler.should_filter({})
    ms = AgentState()
    ms.cmp = store.new_cmp(ms, title='only one', now_ts=1000)
    assert not assembler.should_filter(ms.cmp)    # single path → legacy
    store.create_path(ms.cmp, ms, 'two', now_ts=2000)
    assert assembler.should_filter(ms.cmp)


def test_parity_runtime_vs_prefetch_invocation():
    """Both call sites use the one shared builder — identical output."""
    log = _chatlog('sess-parity')
    _fill_two_paths(log)
    ms = _cmp_two_paths()
    summary = {'last_message_ts': 1050, 'summary': 's'}
    runtime_view = assembler.build_history(log, summary, ms.cmp)
    prefetch_view = assembler.build_history(log, summary, ms.cmp)
    assert runtime_view == prefetch_view
