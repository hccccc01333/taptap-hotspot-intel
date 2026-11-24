"""Model-generated creative tests (fake client, offline)."""
import json
import unittest

from context import creative_model, delivery

LIB = {
    'schema': 'taptap-reference-library-v0.1',
    'samples': [{'ref_id': 'r1', 'title': '样本', 'url': 'https://example.com/r1',
                 'excerpt': '这是不应出现在提示里的样本原文', 'rights_status': '需核查'}],
    'patterns': [{'id': 'official_3items', 'name': '官宣三件套',
                  'structure': ['授权/时间', '三项内容'], 'source_refs': ['r1']}],
}
EVENT = {'event_id': 'e1', 'title': '合成联动事件'}
INTEL = {'one_line': '联动定档10月29日', 'summary': '官方宣布联动。'}
MATERIAL = {'draft': '联动速览文案', 'usage_boundary': '不补写未确认信息'}
FACTS = [{'text': '10月29日联动开启', 'quote': '定档10月29日正式开启'},
         {'text': '包含新角色与主题地图', 'quote': '包含联动专属新角色、全新主题地图'}]


class FakeClient:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def complete(self, prompt, *, model, max_tokens, event_id, session):
        self.calls.append({'prompt': prompt, 'model': model})
        return {'content': self.content, 'model': model, 'id': 'fake',
                'usage': {'completion_tokens': 10}}


def valid_payload():
    return {
        'title': '联动互动话题草案', 'hook': '最期待哪个角色能力进入对局？',
        'audience_need': '待验证：玩家是否愿意讨论联动能力',
        'proposed_action': '在 TapTap 社区发起讨论，入口待确认',
        'copy_draft': '联动定档10月29日，你最期待哪个能力进入对局？',
        'preconditions': ['确认入口', '核查素材授权'],
        'status': 'draft_needs_operational_confirmation',
        'used_pattern_ids': ['official_3items'],
    }


class CreativeModelTests(unittest.TestCase):
    def test_valid_model_output_is_accepted(self):
        client = FakeClient(json.dumps(valid_payload(), ensure_ascii=False))
        result = creative_model.generate_with_model(EVENT, INTEL, MATERIAL, FACTS, LIB, client)
        self.assertTrue(result['ok'])
        creative = result['creative']
        self.assertEqual(creative['generator'], 'model')
        self.assertEqual(creative['status'], 'draft_needs_operational_confirmation')
        self.assertNotIn('这是不应出现的样本原文', client.calls[0]['prompt'])

    def test_invalid_json_and_missing_fields_fail(self):
        result = creative_model.generate_with_model(
            EVENT, INTEL, MATERIAL, FACTS, LIB, FakeClient('不是 JSON'))
        self.assertFalse(result['ok'])
        missing = dict(valid_payload()); missing.pop('preconditions')
        result = creative_model.generate_with_model(
            EVENT, INTEL, MATERIAL, FACTS, LIB,
            FakeClient(json.dumps(missing, ensure_ascii=False)))
        self.assertFalse(result['ok'])
        self.assertIn('missing', result['error'])

    def test_unsupported_number_is_rejected(self):
        payload = valid_payload()
        payload['copy_draft'] = '联动定档11月11日，价值998元'
        result = creative_model.generate_with_model(
            EVENT, INTEL, MATERIAL, FACTS, LIB,
            FakeClient(json.dumps(payload, ensure_ascii=False)))
        self.assertFalse(result['ok'])
        self.assertIn('unsupported numeric', result['error'])

    def test_delivery_falls_back_when_model_fails(self):
        def broken_generator(*args):
            return {'ok': False, 'error': 'model down'}
        package = delivery.build_delivery(
            {**EVENT, 'url': 'https://example.com/e1'}, {'article': {}},
            {'parsed': {'facts': FACTS, 'unknowns': ['入口未确认'],
                        'intelligence': {**INTEL, 'why_taptap': '判断'},
                        'material': {**MATERIAL, 'title': '标题', 'channel': '社区动态'}},
             'quote_checks': {'facts_supported': 2, 'facts_total': 2}},
            reference_library=LIB, creative_generator=broken_generator)
        self.assertTrue(package['checks']['passed'])
        self.assertFalse(package['creative_generation']['ok'])
        self.assertEqual(package['growth_creative']['status'],
                         'draft_needs_operational_confirmation')


if __name__ == '__main__':
    unittest.main()
