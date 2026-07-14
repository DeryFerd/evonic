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
