"""Tests for the CMP boundary detector (fully LLM-led)."""

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
    p1 = ms.cmp['paths']['A1']
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


# ── Fully LLM-led: every non-empty message reaches classify_boundary ─────────

def test_every_message_reaches_the_llm():
    """No keyword short-circuits at all (user requirement): acks, approval
    particles, follow-ups, overlaps — the LLM owns every routing decision."""
    ms = _session()
    ms.cmp['paths']['A2']['artifacts'] = ['/etc/nginx/nginx.conf']
    for msg in (
        'ok sip',
        'oke lanjutkan sesuai plan itu saja',
        'kenapa hasil konfigurasi server production sekarang masih menunjukkan error timeout',
        'tambahkan gzip compression pada /etc/nginx/nginx.conf untuk semua response text',
        'tolong lanjutkan pekerjaan yang ada di A1 sekarang juga',
    ):
        with _patch_l2l3():
            import backend.task_classifier as tc
            result = detect(ms.cmp, ms, msg)
            assert result['layer'] == 'LLM'
            tc.classify_boundary.assert_called_once()


def test_empty_message_skips_llm():
    ms = _session()
    with _patch_l2l3():
        import backend.task_classifier as tc
        result = detect(ms.cmp, ms, '   ')
        assert result['decision'] == 'continue'
        tc.classify_boundary.assert_not_called()


def test_plan_mode_context_reaches_llm():
    """The removed approval guard's knowledge now travels as prompt context:
    the LLM must see that the active path awaits approval."""
    ms = _session()
    ms.mode = 'plan'
    captured = {}

    def fake_boundary(map_text, active_card, other_cards, user_text):
        captured['active'] = active_card
        return {'decision': 'continue', 'target': None}

    with patch.multiple('backend.task_classifier',
                        classify_task=MagicMock(return_value='complex'),
                        classify_boundary=fake_boundary):
        detect(ms.cmp, ms, 'oke lanjutkan sesuai plan itu saja')
    assert 'AWAITING USER APPROVAL' in captured['active']


def test_kanban_return_regression():
    """Live regression: 'oke, sip, btw yg issue kanban ... udah solved kah?'
    was swallowed by the old approval guard while P4 sat in plan mode."""
    ms = _session()
    ms.mode = 'plan'
    with _patch_l2l3(boundary={'decision': 'return', 'target': 'A1'}):
        import backend.task_classifier as tc
        result = detect(ms.cmp, ms,
                        'oke, sip, btw yg issue kanban state race condition tadi udah solved kah?')
        assert result['layer'] == 'LLM'
        tc.classify_boundary.assert_called_once()
        assert (result['decision'], result['target']) == ('return', 'A1')


def test_generic_title_word_does_not_short_circuit():
    """Live regression (session 75433064): task 1 'cari bug di 3 plugin'
    swallowed task 2 via keyword overlap ('plugin'). Topic decisions must
    reach the LLM."""
    ms = AgentState(mode='execute')
    ms.cmp = store.new_cmp(
        ms, title='tolong cari bug di 3 plugin paling aktif', now_ts=1000)
    store.create_path(ms.cmp, ms, 'other work', now_ts=1500)
    store.switch_to(ms.cmp, ms, 'A1', now_ts=2000)
    ms.mode = 'execute'
    # P1 card is empty (key_facts/artifacts never filled)
    with _patch_l2l3(boundary={'decision': 'dep_branch', 'target': 'A1'}):
        import backend.task_classifier as tc
        result = detect(ms.cmp, ms,
                        'tolong gabungkan chart agent test di token monitor plugin')
        assert result['layer'] == 'LLM'
        tc.classify_boundary.assert_called_once()
        assert result['decision'] == 'dep_branch'


def test_l3_sees_recent_deliverable_tail():
    """The just-delivered reply reaches the L3 prompt — the branch cue for
    'invoice done' → 'rebuild krasan-cli' style borderline cases."""
    ms = _session()
    captured = {}

    def fake_boundary(map_text, active_card, other_cards, user_text):
        captured['active'] = active_card
        return {'decision': 'dep_branch', 'target': 'A2'}

    with patch.multiple('backend.task_classifier',
                        classify_task=MagicMock(return_value='complex'),
                        classify_boundary=fake_boundary):
        result = detect(ms.cmp, ms, LONG_NEW_TASK,
                        recent_tail='Invoice INV-042 untuk Budi Contoh sudah dibuat.')
    assert 'INV-042' in captured['active']
    assert result['decision'] == 'dep_branch'


def test_hook_passes_last_final_to_detector():
    from backend.agent_runtime.cmp import on_turn_boundary

    class Log:
        def get_last_entry(self, types=None):
            if types and 'final' in types:
                return {'type': 'final', 'ts': 4000,
                        'content': 'Invoice sudah selesai dibuat.'}
            return {'type': 'user', 'ts': 5000}

        def get_entries_between_ts(self, a, b):
            return []

        def get_entries_after_ts(self, a):
            return []

    ms = _session()
    with patch('backend.agent_runtime.cmp.detector.detect',
               return_value={'decision': 'continue', 'target': None,
                             'layer': 'L3', 'reason': 'x'}) as det:
        on_turn_boundary({'id': 'a1', 'enable_cmp': 1, 'enable_agent_state': 1},
                         ms, Log(), 'tolong rebuild krasan-cli sekarang setelah invoice tadi')
    assert det.call_args[1]['recent_tail'] == 'Invoice sudah selesai dibuat.'


def test_l3_sees_finished_task_state():
    ms = _session()
    ms.atg = {'status': 'done', 'dag': {'root_goal': 'g'}}
    captured = {}

    def fake_boundary(map_text, active_card, other_cards, user_text):
        captured['active'] = active_card
        return {'decision': 'continue', 'target': None}

    from unittest.mock import MagicMock
    with patch.multiple('backend.task_classifier',
                        classify_task=MagicMock(return_value='complex'),
                        classify_boundary=fake_boundary):
        detect(ms.cmp, ms, LONG_NEW_TASK)
    assert 'FINISHED' in captured['active']


# ── LLM decisions + validation ───────────────────────────────────────────────

def test_l3_decisions_flow_through():
    ms = _session()
    for boundary, expected in [
        ({'decision': 'indep_branch', 'target': None}, ('indep_branch', None)),
        ({'decision': 'return', 'target': 'A1'}, ('return', 'A1')),
        ({'decision': 'dep_branch', 'target': 'A1'}, ('dep_branch', 'A1')),
        ({'decision': 'continue', 'target': None}, ('continue', None)),
    ]:
        with _patch_l2l3(boundary=boundary):
            result = detect(ms.cmp, ms, LONG_NEW_TASK)
            assert (result['decision'], result['target']) == expected
            assert result['layer'] == 'LLM'


def test_l3_invalid_targets_degrade_safely():
    ms = _session()
    # return to unknown/active path → continue
    for target in ('Z9', 'A2'):
        with _patch_l2l3(boundary={'decision': 'return', 'target': target}):
            assert detect(ms.cmp, ms, LONG_NEW_TASK)['decision'] == 'continue'
    # dep_branch on unknown path → independent branch
    with _patch_l2l3(boundary={'decision': 'dep_branch', 'target': 'Z9'}):
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
        ('RETURN:A2', ('return', 'A2')),
        ('DEP_BRANCH:B1', ('dep_branch', 'B1')),
        ('INDEP_BRANCH', ('indep_branch', None)),
        ('I think RETURN:A2 fits best', ('return', 'A2')),  # embedded token
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
