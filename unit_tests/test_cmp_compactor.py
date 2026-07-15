"""Tests for the CMP compactor (path card generation)."""

import json
from unittest.mock import MagicMock, patch

from backend.agent_runtime.cmp import store
from backend.agent_runtime.cmp.compactor import (
    finalize_active_card,
    generate_card,
    lift_atg_facts,
)
from backend.agent_state import AgentState


class FakeChatlog:
    def __init__(self, entries):
        self.entries = sorted(entries, key=lambda e: e['ts'])

    def get_entries_between_ts(self, after_ts, up_to_ts):
        return [e for e in self.entries if after_ts < e['ts'] <= up_to_ts]

    def get_entries_after_ts(self, after_ts):
        return [e for e in self.entries if e['ts'] > after_ts]


ENTRIES = [
    {'type': 'user', 'ts': 1100, 'content': 'build a company profile site for client A'},
    {'type': 'final', 'ts': 1200, 'content': 'Site scaffolded with SvelteKit; deploy failed.'},
]

ATG_STATE = {
    'status': 'fallback',
    'dag': {'root_goal': 'build client A site',
            'nodes': {
                'n1': {'tool': 'write_file', 'status': 'done', 'goal': 'create page',
                       'record': {'resolved_args': {'file_path': '/projects/client-a/index.html'},
                                  'output_excerpt': 'ok'}},
                'n2': {'tool': 'bash', 'status': 'failed', 'goal': 'deploy the site',
                       'record': {'error': 'missing env DATABASE_URL on host'}},
            }},
}


def _path(segments=((1000, None),)):
    return {'id': 'P1', 'title': 'client A site', 'status': 'active',
            'goal': '', 'outcome': '', 'key_facts': [], 'artifacts': [],
            'depends_on': [], 'segments': [list(s) for s in segments],
            'card_stale': True}


def _scripted_client(payload):
    client = MagicMock()
    client.chat_completion.return_value = {
        'success': True,
        'response': {'choices': [{'message': {'content': payload}}]}}
    return client


# ── LLM-filled cards ─────────────────────────────────────────────────────────

def test_card_filled_and_capped_from_llm():
    card_json = json.dumps({
        'title': 'T' * 100, 'goal': 'Build the site', 'outcome': 'Deploy failed',
        'key_facts': [f'fact {i}' for i in range(10)],
        'artifacts': ['/projects/client-a/'],
    })
    with patch('backend.task_classifier._get_classifier_client',
               return_value=_scripted_client(card_json)):
        card = generate_card(FakeChatlog(ENTRIES), _path())
    assert len(card['title']) == store.TITLE_MAX
    assert len(card['key_facts']) == store.KEY_FACTS_MAX
    assert card['outcome'] == 'Deploy failed'


def test_atg_facts_merged_even_with_llm_card():
    card_json = json.dumps({'title': 'x', 'goal': 'g', 'outcome': 'o',
                            'key_facts': ['llm fact'], 'artifacts': []})
    with patch('backend.task_classifier._get_classifier_client',
               return_value=_scripted_client(card_json)):
        card = generate_card(FakeChatlog(ENTRIES), _path(), atg_state=ATG_STATE)
    facts = ' | '.join(card['key_facts'])
    assert 'llm fact' in facts
    assert 'DATABASE_URL' in facts                       # failure cause lifted
    assert '/projects/client-a/index.html' in card['artifacts']


# ── Fallbacks ────────────────────────────────────────────────────────────────

def test_garbage_llm_output_mechanical_fallback():
    with patch('backend.task_classifier._get_classifier_client',
               return_value=_scripted_client('sorry, I cannot do that')):
        card = generate_card(FakeChatlog(ENTRIES), _path(), atg_state=ATG_STATE)
    assert card['title'] == 'client A site'
    assert 'deploy failed' in card['outcome'].lower()
    assert any('DATABASE_URL' in f for f in card['key_facts'])


def test_llm_exception_never_raises():
    client = MagicMock()
    client.chat_completion.side_effect = RuntimeError('boom')
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        card = generate_card(FakeChatlog(ENTRIES), _path())
    assert card['title']  # mechanical card produced


def test_empty_transcript_skips_llm():
    client = MagicMock()
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        card = generate_card(FakeChatlog([]), _path(), atg_state=ATG_STATE)
    client.chat_completion.assert_not_called()
    assert any('DATABASE_URL' in f for f in card['key_facts'])


# ── ATG lifting ──────────────────────────────────────────────────────────────

def test_lift_atg_facts():
    facts, artifacts, goal = lift_atg_facts(ATG_STATE)
    assert goal == 'build client A site'
    assert any('failed: missing env DATABASE_URL' in f for f in facts)
    assert artifacts == ['/projects/client-a/index.html']
    assert lift_atg_facts(None) == ([], [], "")


# ── token estimates ──────────────────────────────────────────────────────────

def test_path_and_card_token_estimates():
    from backend.agent_runtime.cmp.compactor import (
        card_token_estimate, path_token_estimate)

    entries = [
        {'type': 'user', 'ts': 1100, 'content': 'buatkan laporan invoice ' * 40},
        {'type': 'tool_call', 'ts': 1200, 'function': 'bash',
         'params': {'script': 'ls -la ' * 30}},
        {'type': 'tool_output', 'ts': 1300, 'content': 'X' * 4000},
        {'type': 'final', 'ts': 1400, 'content': 'laporan selesai'},
    ]
    path = {'id': 'A1', 'segments': [[1000, None]],
            'title': 'Laporan invoice', 'goal': 'buat laporan',
            'outcome': 'selesai', 'key_facts': ['fakta a', 'fakta b'],
            'artifacts': ['/out/report.pdf']}

    transcript_tokens = path_token_estimate(FakeChatlog(entries), path)
    card_tokens = card_token_estimate(path)

    # transcript (tool dumps + prose) dwarfs the compact card
    assert transcript_tokens > 500
    assert 0 < card_tokens < 100
    assert transcript_tokens > card_tokens * 5

    # empty path → 0, never raises
    assert path_token_estimate(FakeChatlog([]), {'id': 'x', 'segments': []}) == 0
    assert card_token_estimate({'id': 'x'}) == 0


# ── name_path ────────────────────────────────────────────────────────────────

def test_name_path_uses_llm_naming():
    from backend.agent_runtime.cmp.compactor import name_path
    named_json = json.dumps({'title': 'Commit perubahan', 'action': 'commit changes'})
    with patch('backend.task_classifier._get_classifier_client',
               return_value=_scripted_client(named_json)):
        named = name_path('please commit aja dulu perubahannya')
    assert named == {'title': 'Commit perubahan', 'action': 'commit changes'}


def test_name_path_mechanical_fallback():
    from backend.agent_runtime.cmp.compactor import name_path
    with patch('backend.task_classifier._get_classifier_client',
               return_value=_scripted_client('not json at all')):
        named = name_path('please commit aja dulu perubahannya')
    assert named['title'] == 'please commit aja dulu perubahannya'
    assert named['action'] == 'please commit aja dulu'
    # LLM exception → same fallback, never raises
    client = MagicMock()
    client.chat_completion.side_effect = RuntimeError('boom')
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        assert name_path('tulis blog')['title'] == 'tulis blog'


def test_branch_paths_get_named_at_creation():
    from backend.agent_runtime.cmp import on_turn_boundary

    class Log:
        def get_last_entry(self, types=None):
            return {'type': 'user', 'ts': 5000, 'content': 'x'}

        def get_entries_between_ts(self, a, b):
            return []

        def get_entries_after_ts(self, a):
            return []

    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='first', now_ts=1000)
    with patch('backend.agent_runtime.cmp.detector.detect',
               return_value={'decision': 'indep_branch', 'target': None,
                             'layer': 'LLM', 'reason': 'x'}), \
         patch('backend.agent_runtime.cmp.compactor.name_path',
               return_value={'title': 'Blog robin.blog.com',
                             'action': 'create article'}) as np, \
         patch('backend.agent_runtime.cmp.compactor.generate_card',
               return_value={'title': 'first', 'goal': '', 'outcome': '',
                             'key_facts': [], 'artifacts': []}):
        result = on_turn_boundary({'id': 'a1', 'enable_cmp': 1,
                                   'enable_agent_state': 1}, ms, Log(),
                                  'buatkan tulisan blog untuk robin.blog.com dong')
    np.assert_called_once()
    new_path_record = ms.cmp['paths'][result['target']]
    assert new_path_record['title'] == 'Blog robin.blog.com'
    assert new_path_record['action'] == 'create article'


# ── finalize_active_card ─────────────────────────────────────────────────────

def test_finalize_updates_path_and_clears_stale():
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='client A site', now_ts=1000)
    ms.atg = ATG_STATE
    card_json = json.dumps({'title': 'Client A website', 'goal': 'build it',
                            'outcome': 'deploy failed', 'key_facts': [],
                            'artifacts': []})
    with patch('backend.task_classifier._get_classifier_client',
               return_value=_scripted_client(card_json)):
        finalize_active_card(FakeChatlog(ENTRIES), ms.cmp, ms)
    p1 = ms.cmp['paths']['A1']
    assert p1['title'] == 'Client A website'
    assert p1['card_stale'] is False
    # second call with fresh card is a no-op (no LLM)
    client = MagicMock()
    with patch('backend.task_classifier._get_classifier_client',
               return_value=client):
        finalize_active_card(FakeChatlog(ENTRIES), ms.cmp, ms)
    client.chat_completion.assert_not_called()
