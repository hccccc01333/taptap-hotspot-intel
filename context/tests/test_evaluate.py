"""context 评测器测试（离线）。"""
import json
import tempfile
import unittest
from pathlib import Path

from context import evaluate


BODY = ('合成活动公告。' * 20 +
        '\n活动时间：2026年10月20日上线。售价1980日元。')


def pack(root: Path, *, fact_text='2026年10月20日上线，售价1980日元',
         quote='活动时间：2026年10月20日上线。', material=True) -> Path:
    folder = root / 'event_one'
    folder.mkdir(parents=True)
    event = {'event_id': 'event_one', 'url': 'https://example.com/a', 'title': '合成事件',
             'source': '示例', 'published_at': '2026-10-20 10:00:00', 'objective': '确认时间'}
    (folder / 'evidence.json').write_text(json.dumps(
        {'event': event, 'article': {'body': BODY, 'url': event['url'],
                                     'title': '合成页', 'fetched_at': '2026-11-15T10:00:00+0800'}},
        ensure_ascii=False), encoding='utf-8')
    parsed = {
        'headline': '合成', 'one_line': '合成一句话',
        'facts': [{'text': fact_text, 'quote': quote}],
        'unknowns': ['奖励未说明'],
        'intelligence': {'summary': '摘要', 'why_taptap': '判断', 'watch_next': ['后续']},
        'material': ({'title': '标题', 'channel': '社区动态', 'draft': '文案',
                      'usage_boundary': '边界'} if material else {}),
    }
    (folder / 'final.json').write_text(json.dumps({'parsed': parsed}, ensure_ascii=False),
                                       encoding='utf-8')
    (root / 'summary.json').write_text(json.dumps(
        {'events': [{'event_id': 'event_one', 'delivered': True}]}, ensure_ascii=False),
        encoding='utf-8')
    return root


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='v4-context-eval-')
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_supported_quote_and_complete_material(self):
        result = evaluate.evaluate_pack(pack(self.root / 'ok'))
        row = result['events'][0]
        self.assertEqual((row['facts_supported'], row['facts_total']), (1, 1))
        self.assertTrue(row['material_complete'])
        self.assertEqual(result['aggregate']['quote_support_rate'], 1.0)

    def test_unsupported_numeric_atom_and_missing_material(self):
        result = evaluate.evaluate_pack(pack(self.root / 'bad',
                                             fact_text='2026年12月31日上线，售价2980日元',
                                             quote='原文没有这句话', material=False))
        row = result['events'][0]
        self.assertEqual(row['facts_supported'], 0)
        self.assertTrue(row['unsupported_numeric_atoms'])
        self.assertFalse(row['material_complete'])

    def test_compare_reports_direction(self):
        baseline = evaluate.evaluate_pack(pack(self.root / 'a'))
        candidate = evaluate.evaluate_pack(pack(self.root / 'b'))
        compare = evaluate.compare_runs(baseline, candidate)
        self.assertEqual(compare['quote_support_delta'], 0.0)
        self.assertTrue(compare['candidate_not_worse'])

    def test_error_event_is_preserved(self):
        folder = self.root / 'broken'
        (folder / 'event_one').mkdir(parents=True)
        (folder / 'summary.json').write_text(json.dumps(
            {'events': [{'event_id': 'event_one'}]}), encoding='utf-8')
        result = evaluate.evaluate_pack(folder)
        self.assertEqual(result['aggregate']['events_with_errors'], 1)


if __name__ == '__main__':
    unittest.main()
