"""Creative quality gate tests."""
import unittest

from context import creative_quality

FACTS = [{'text': '10月29日联动开启', 'quote': '定档10月29日正式开启'}]
LIB = {'samples': [{'ref_id': 'r1', 'excerpt': '这是一段足够长的参考样本原文用于检测是否被复制进文案中'}]}


def creative(**overrides):
    value = {
        'title': '联动讨论草案', 'hook': '最期待哪个能力？',
        'audience_need': '待验证的玩家需求', 'proposed_action': '在社区发起讨论',
        'copy_draft': '联动定档10月29日，你最期待哪个能力进入对局？欢迎聊聊你的理由。',
        'preconditions': ['确认入口', '核查授权'], 'status': 'draft_needs_operational_confirmation',
        'used_patterns': ['官宣三件套'], 'reference_samples': [{'ref_id': 'r1'}],
    }
    value.update(overrides)
    return value


class QualityTests(unittest.TestCase):
    def test_good_creative_passes(self):
        result = creative_quality.evaluate_creative(creative(), FACTS, LIB)
        self.assertTrue(result['passed'], result)

    def test_unsupported_number_fails(self):
        result = creative_quality.evaluate_creative(
            creative(copy_draft='联动定档11月11日，价值998元。'), FACTS, LIB)
        self.assertFalse(result['passed'])
        self.assertTrue(result['unsupported_numbers'])

    def test_reference_copy_fails(self):
        excerpt = LIB['samples'][0]['excerpt']
        result = creative_quality.evaluate_creative(
            creative(copy_draft='开头' + excerpt + '结尾'), FACTS, LIB)
        self.assertFalse(result['passed'])
        self.assertEqual(result['reference_overlap'], ['r1'])

    def test_missing_preconditions_fails(self):
        result = creative_quality.evaluate_creative(
            creative(preconditions=['only one']), FACTS, LIB)
        self.assertFalse(result['checks']['preconditions'])


if __name__ == '__main__':
    unittest.main()
