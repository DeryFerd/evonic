"""Tests for the CMP boundary-detection cascade (L1 → L2 → L3)."""

from unittest.mock import MagicMock, patch

from backend.agent_runtime.cmp import store
from backend.agent_runtime.cmp.detector import detect
from backend.agent_state import AgentState

LONG_NEW_TASK = ('please build a completely new scraper project under /tmp/scraper '
                 'that collects product prices daily')


def _session():
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(ms, title='client A website', goal='build site',
                           now_ts=1000)
    p1 = ms.cmp['paths']['P1']
    p1['artifacts'] = ['/projects/client-a-web/']
    p1['key_facts'] = ['deploy failed: missing DATABASE_URL']
    store.create_path(ms.cmp, ms, 'server config', goal='configure nginx',
                      now_ts=2000)
    ms.mode = 'execute'
    return ms


def _patch_l2l3(task='complex', boundary=None):
    return patch.multiple(
        'backend.task_classifier',
        classify_task=MagicMock(return_value=task),
        classify_boundary=MagicMock(
            return_value=boundary or {'decision': 'continue', 'target': None}))


# ── L1 short-circuits (LLM must never be called) ─────────────────────────────

def test_short_message_continues_without_llm():
    ms = _session()
    with _patch_l2l3() as _:
        import backend.task_classifier as tc
        result = detect(ms.cmp, ms, 'belum bisa')
        assert result == {'decision': 'continue', 'target': None, 'layer': 'L1'}
        tc.classify_task.assert_not_called()
        tc.classify_boundary.assert_not_called()


def test_approval_in_plan_mode_continues():
    ms = _session()
    ms.mode = 'plan'
    with _patch_l2l3():
        import backend.task_classifier as tc
        # > 6 words AND approval-looking — rule 0 still guards it
        result = detect(ms.cmp, ms,
                        'ok lanjutkan saja rencananya saya setuju dengan semua langkah itu ya')
        assert result['decision'] == 'continue'
        tc.classify_boundary.assert_not_called()


def test_explicit_path_id_is_a_return():
    ms = _session()
    with _patch_l2l3():
        result = detect(ms.cmp, ms,
                        'tolong lanjutkan pekerjaan yang ada di P1 sekarang juga')
        assert result == {'decision': 'return', 'target': 'P1', 'layer': 'L1'}


def test_continuation_marker_continues():
    ms = _session()
    with _patch_l2l3():
        import backend.task_classifier as tc
        result = detect(ms.cmp, ms,
                        'kenapa hasil konfigurasi server production sekarang masih menunjukkan error timeout')
        assert result['decision'] == 'continue'
        tc.classify_boundary.assert_not_called()


def test_working_set_overlap_continues():
    ms = _session()
    ms.cmp['paths']['P2']['artifacts'] = ['/etc/nginx/nginx.conf']
    with _patch_l2l3():
        import backend.task_classifier as tc
        result = detect(ms.cmp, ms,
                        'tambahkan gzip compression pada /etc/nginx/nginx.conf untuk semua response text')
        assert result['decision'] == 'continue'
        assert result['layer'] == 'L1'
        tc.classify_boundary.assert_not_called()


# ── L2 trivial gate ──────────────────────────────────────────────────────────

def test_trivial_task_never_branches():
    ms = _session()
    with _patch_l2l3(task='trivial'):
        import backend.task_classifier as tc
        result = detect(ms.cmp, ms, LONG_NEW_TASK)
        assert result == {'decision': 'continue', 'target': None, 'layer': 'L2'}
        tc.classify_boundary.assert_not_called()


# ── L3 decisions + validation ────────────────────────────────────────────────

def test_l3_decisions_flow_through():
    ms = _session()
    for boundary, expected in [
        ({'decision': 'indep_branch', 'target': None}, ('indep_branch', None)),
        ({'decision': 'return', 'target': 'P1'}, ('return', 'P1')),
        ({'decision': 'dep_branch', 'target': 'P1'}, ('dep_branch', 'P1')),
        ({'decision': 'continue', 'target': None}, ('continue', None)),
    ]:
        with _patch_l2l3(boundary=boundary):
            result = detect(ms.cmp, ms, LONG_NEW_TASK)
            assert (result['decision'], result['target']) == expected
            assert result['layer'] == 'L3'


def test_l3_invalid_targets_degrade_safely():
    ms = _session()
    # return to unknown/active path → continue
    for target in ('P9', 'P2'):
        with _patch_l2l3(boundary={'decision': 'return', 'target': target}):
            assert detect(ms.cmp, ms, LONG_NEW_TASK)['decision'] == 'continue'
    # dep_branch on unknown path → independent branch
    with _patch_l2l3(boundary={'decision': 'dep_branch', 'target': 'P9'}):
        result = detect(ms.cmp, ms, LONG_NEW_TASK)
        assert result['decision'] == 'indep_branch' and result['target'] is None


def test_l3_counts_llm_calls():
    ms = _session()
    with _patch_l2l3(boundary={'decision': 'indep_branch', 'target': None}):
        detect(ms.cmp, ms, LONG_NEW_TASK)
    assert ms.cmp['stats']['detector_llm_calls'] == 1


# ── classify_boundary parse matrix ───────────────────────────────────────────

def test_classify_boundary_parse_matrix():
    from backend.task_classifier import classify_boundary

    def _client(content, key='content'):
        c = MagicMock()
        c.chat_completion.return_value = {
            'success': True,
            'response': {'choices': [{'message': {key: content}}]}}
        return c

    cases = [
        ('CONTINUE', ('continue', None)),
        ('RETURN:P2', ('return', 'P2')),
        ('DEP_BRANCH:P1', ('dep_branch', 'P1')),
        ('INDEP_BRANCH', ('indep_branch', None)),
        ('I think RETURN:P2 fits best', ('return', 'P2')),  # embedded token
        ('gibberish with no token', ('continue', None)),
    ]
    for content, expected in cases:
        with patch('backend.task_classifier._get_classifier_client',
                   return_value=_client(content)):
            r = classify_boundary('map', 'active', 'others', 'a long message here')
            assert (r['decision'], r['target']) == expected, content

    # reasoning_content fallback
    with patch('backend.task_classifier._get_classifier_client',
               return_value=_client('INDEP_BRANCH', key='reasoning_content')):
        assert classify_boundary('m', 'a', 'o', 'msg')['decision'] == 'indep_branch'

    # LLM failure → continue
    failing = MagicMock()
    failing.chat_completion.return_value = {'success': False}
    with patch('backend.task_classifier._get_classifier_client',
               return_value=failing):
        assert classify_boundary('m', 'a', 'o', 'msg')['decision'] == 'continue'
