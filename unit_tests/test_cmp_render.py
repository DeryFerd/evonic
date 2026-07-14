"""Tests for CMP map + card rendering."""

from backend.agent_runtime.cmp import store
from backend.agent_runtime.cmp.render import RENDER_MAX_CHARS, render_cmp_section, render_map
from backend.agent_state import AgentState


def _session():
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='Company web profile', goal='build site',
                           now_ts=1000)
    p1 = ms.cmp['paths']['P1']
    p1['outcome'] = 'landing page failed at deploy'
    p1['key_facts'] = ['repo: /projects/client-a-web',
                       'deploy failed: missing env DATABASE_URL']
    p1['artifacts'] = ['/projects/client-a-web/']
    store.create_path(ms.cmp, ms, 'Invoice for client A', goal='make invoice',
                      depends_on=['P1'], now_ts=2000)
    return ms


def test_map_shows_topology_status_position():
    ms = _session()
    mermaid = render_map(ms.cmp)
    assert 'flowchart TD' in mermaid
    assert 'U((user))' in mermaid
    assert '[ACTIVE]' in mermaid and 'P2' in mermaid
    assert '[dormant]' in mermaid
    assert 'P2 -. depends .-> P1' in mermaid


def test_section_tiers():
    ms = _session()
    section = render_cmp_section(ms.cmp)
    # active path: full card
    assert 'P2 — Invoice for client A' in section
    assert 'goal: make invoice' in section
    # dependency ancestor pinned compactly even though dormant
    assert 'P1 — Company web profile' in section
    assert 'dependency of active path' in section
    assert 'DATABASE_URL' in section  # key fact preserved (interface)
    # navigation hint
    assert 'switch_path' in section


def test_archived_non_ancestor_is_map_only():
    ms = _session()
    store.create_path(ms.cmp, ms, 'Server config', now_ts=3000)
    # archive P2 (not an ancestor of P3)
    ms.cmp['paths']['P2']['status'] = 'archived'
    section = render_cmp_section(ms.cmp)
    assert 'P2["Invoice for client A' in section          # map node stays
    assert 'goal: make invoice' not in section            # card content gone


def test_dormant_non_ancestor_one_line():
    ms = _session()
    store.create_path(ms.cmp, ms, 'Server config', now_ts=3000)
    section = render_cmp_section(ms.cmp)
    assert 'Other recent paths:' in section
    assert '- P2 Invoice for client A' in section


def test_render_cap():
    ms = _session()
    for i in range(15):
        store.create_path(ms.cmp, ms, f'task {i} ' + 'x' * 50,
                          goal='g' * 290, now_ts=3000 + i)
        ms.cmp['paths'][ms.cmp['active_id']]['key_facts'] = ['f' * 190] * 6
    section = render_cmp_section(ms.cmp)
    assert len(section) <= RENDER_MAX_CHARS + 50


def test_empty_cmp_renders_nothing():
    assert render_cmp_section(None) == ''
    assert render_cmp_section({}) == ''
