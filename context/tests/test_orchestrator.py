"""orchestrator 的离线测试：假模型、无网络。"""
import json
import unittest

from context import orchestrator


BODY = (
    '合成活动正文。' * 30 +
    '\n活动时间：2026年10月20日上线。\n售价1980日元（含税）。\n'
    '新增12人锦标赛。\n支持简体中文。\n'
)
QUOTES = ['活动时间：2026年10月20日上线。', '售价1980日元（含税）。',
          '新增12人锦标赛。', '支持简体中文。']


def payload(count=4, *, material=True, extra_number=False):
    facts = [{'text': '合成事实', 'quote': quote}
             for quote in QUOTES[:count]]
    if extra_number:
        facts.append({'text': '售价 2980 日元', 'quote': QUOTES[1]})
    return {
        'headline': '合成', 'one_line': '一句话',
        'facts': facts, 'unknowns': ['奖励未说明'],
        'intelligence': {'summary': '摘要', 'why_taptap': '判断', 'watch_next': ['后续']},
        'material': ({'title': '标题', 'channel': '社区动态', 'draft': '文案',
                      'usage_boundary': '边界'} if material else {}),
    }


class FakeClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def complete(self, prompt, *, model, max_tokens, event_id, session):
        self.calls.append({'prompt': prompt, 'model': model})
        value = self.payloads.pop(0)
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return {'content': content, 'model': model, 'id': 'fake',
                'usage': {'prompt_tokens': 100, 'completion_tokens': 50,
                          'total_tokens': 150}}


POLICY = orchestrator.DeliveryPolicy(min_facts=4, max_repairs=1)


class OrchestratorTests(unittest.TestCase):
    def test_good_draft_is_ready_without_repair_or_final(self):
        client = FakeClient([payload()])
        trace = orchestrator.run_orchestrated(
            event_id='e1', base_prompt='prompt', body=BODY, client=client, policy=POLICY)
        self.assertEqual(trace.status, 'ready_for_final')
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(trace.repair_count, 0)
        self.assertFalse(trace.gaps)
        self.assertEqual(trace.usage_total['completion_tokens'], 50)

    def test_failed_gate_triggers_one_bounded_repair(self):
        client = FakeClient([payload(count=1, material=False), payload()])
        trace = orchestrator.run_orchestrated(
            event_id='e1', base_prompt='prompt', body=BODY, client=client, policy=POLICY)
        self.assertEqual(trace.status, 'ready_for_final')
        self.assertEqual(trace.repair_count, 1)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([step['stage'] for step in trace.steps], ['draft', 'repair'])
        self.assertIn('facts_below_min', trace.steps[0]['gaps'][0])
        self.assertTrue(trace.parsed['facts'])

    def test_two_failed_attempts_stop_for_review(self):
        client = FakeClient([payload(count=1), payload(count=1)])
        trace = orchestrator.run_orchestrated(
            event_id='e1', base_prompt='prompt', body=BODY, client=client, policy=POLICY)
        self.assertEqual(trace.status, 'needs_review')
        self.assertEqual(trace.repair_count, 1)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(trace.gaps)

    def test_final_model_called_only_after_gates_pass(self):
        client = FakeClient([payload(), payload()])
        trace = orchestrator.run_orchestrated(
            event_id='e1', base_prompt='prompt', body=BODY, client=client, policy=POLICY,
            final_model='longcat-2.5-free')
        self.assertEqual(trace.status, 'final_delivered')
        self.assertEqual([call['model'] for call in client.calls],
                         ['beam-free', 'longcat-2.5-free'])
        self.assertEqual([step['stage'] for step in trace.steps], ['draft', 'final'])

    def test_context_card_is_built_only_for_a_passing_final(self):
        client = FakeClient([payload(), payload()])
        calls = {'built': 0, 'prompted': 0}

        def build_card(parsed):
            calls['built'] += 1
            return {'platforms': ['PC'], 'facts_seen': len(parsed.get('facts') or [])}

        def build_prompt(card):
            calls['prompted'] += 1
            return 'CONTEXT_PROMPT ' + json.dumps(card, ensure_ascii=False)

        trace = orchestrator.run_orchestrated(
            event_id='e1', base_prompt='prompt', body=BODY, client=client, policy=POLICY,
            final_model='longcat-2.5-free', context_card_builder=build_card,
            context_prompt_builder=build_prompt)
        self.assertEqual(trace.status, 'final_delivered')
        self.assertEqual(calls, {'built': 1, 'prompted': 1})
        self.assertEqual(trace.final_prompt_mode, 'context_card')
        self.assertEqual(trace.context_card['platforms'], ['PC'])
        self.assertTrue(client.calls[1]['prompt'].startswith('CONTEXT_PROMPT'))
        self.assertIn('context', [step['stage'] for step in trace.steps])

    def test_context_builder_not_called_without_final(self):
        client = FakeClient([payload()])
        called = []
        orchestrator.run_orchestrated(
            event_id='e1', base_prompt='prompt', body=BODY, client=client, policy=POLICY,
            context_card_builder=lambda parsed: called.append(1) or {},
            context_prompt_builder=lambda card: 'x')
        self.assertEqual(called, [])

    def test_final_parse_failure_is_recorded_not_retried(self):
        client = FakeClient([payload(), '不是 JSON'])
        trace = orchestrator.run_orchestrated(
            event_id='e1', base_prompt='prompt', body=BODY, client=client, policy=POLICY,
            final_model='longcat-2.5-free')
        self.assertEqual(trace.status, 'final_failed')
        self.assertIn('final_parse_failed', trace.error)
        self.assertEqual(len(client.calls), 2)

    def test_unsupported_numeric_atom_is_a_gap(self):
        client = FakeClient([payload(extra_number=True), payload(extra_number=True)])
        trace = orchestrator.run_orchestrated(
            event_id='e1', base_prompt='prompt', body=BODY, client=client, policy=POLICY)
        self.assertEqual(trace.status, 'needs_review')
        self.assertTrue(any('unsupported_numeric' in gap for gap in trace.gaps))

    def test_model_route_selects_healthy_fallback_and_records_it(self):
        client = FakeClient([payload()])
        health = [
            orchestrator.ModelHealth('beam-free', 'failed', 'HTTP 403') if hasattr(orchestrator, 'ModelHealth')
            else None,
        ]
        # Import the real health record from model_router to avoid duplicating the type.
        from context.model_router import ModelHealth
        health = [ModelHealth('beam-free', 'failed', 'HTTP 403'),
                  ModelHealth('hy4-flash-free', 'ok', 'ok')]
        trace = orchestrator.run_orchestrated(
            event_id='e1', base_prompt='prompt', body=BODY, client=client, policy=POLICY,
            candidate_models=('beam-free', 'hy4-flash-free'), model_health=health)
        self.assertEqual(trace.status, 'ready_for_final')
        self.assertEqual(trace.model_route['model'], 'hy4-flash-free')
        self.assertEqual(client.calls[0]['model'], 'hy4-flash-free')
        self.assertEqual(trace.steps[0]['stage'], 'model_route')

    def test_model_route_without_healthy_model_makes_no_call(self):
        from context.model_router import ModelHealth
        client = FakeClient([])
        trace = orchestrator.run_orchestrated(
            event_id='e1', base_prompt='prompt', body=BODY, client=client, policy=POLICY,
            candidate_models=('beam-free',),
            model_health=[ModelHealth('beam-free', 'failed', 'HTTP 403')])
        self.assertEqual(trace.status, 'failed')
        self.assertIn('no_healthy_model', trace.error)
        self.assertEqual(client.calls, [])

    def test_runtime_failure_falls_back_to_next_healthy_model(self):
        from context.model_router import ModelHealth

        class FlakyClient:
            def __init__(self):
                self.calls = []

            def complete(self, prompt, *, model, max_tokens, event_id, session):
                self.calls.append(model)
                if model == 'hy4-flash-free':
                    raise ConnectionError('reset')
                return {'content': json.dumps(payload(), ensure_ascii=False),
                        'model': model, 'id': 'fake',
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1,
                                  'total_tokens': 2}}

        client = FlakyClient()
        health = [ModelHealth('hy4-flash-free', 'ok', 'ok'),
                  ModelHealth('beam-free', 'ok', 'ok')]
        trace = orchestrator.run_orchestrated(
            event_id='e1', base_prompt='prompt', body=BODY, client=client, policy=POLICY,
            candidate_models=('hy4-flash-free', 'beam-free'), model_health=health)
        self.assertEqual(trace.status, 'ready_for_final')
        self.assertEqual(client.calls, ['hy4-flash-free', 'beam-free'])
        self.assertIn('model_fallback', [step['stage'] for step in trace.steps])

    def test_policy_validation(self):
        with self.assertRaises(ValueError):
            orchestrator.DeliveryPolicy(min_facts=0)
        with self.assertRaises(ValueError):
            orchestrator.DeliveryPolicy(max_repairs=-1)


if __name__ == '__main__':
    unittest.main()
