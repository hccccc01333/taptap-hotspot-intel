"""Delivery-layer tests (offline)."""
import json
import tempfile
import unittest
from pathlib import Path

from context import delivery
from context.delivery import load_delivery_index


def final_payload():
    return {
        'parsed': {
            'headline': '合成标题', 'one_line': '一句话结论',
            'facts': [{'text': '10月20日上线', 'quote': '第12赛季计划于10月20日上线'},
                      {'text': '新增12人锦标赛', 'quote': '新模式Solo Showdown是一项12人锦标赛'}],
            'unknowns': ['年份未在正文明确'],
            'intelligence': {'summary': '合成摘要', 'why_taptap': '判断：社区可讨论',
                             'watch_next': ['官方后续']},
            'material': {'title': '素材标题', 'channel': '社区动态', 'draft': '素材正文',
                         'usage_boundary': '不补写未确认信息'},
        },
        'quote_checks': {'facts_supported': 2, 'facts_total': 2},
    }


class DeliveryTests(unittest.TestCase):
    def test_build_delivery_has_three_types_and_checks(self):
        event = {'event_id': 'e1', 'title': '合成事件', 'url': 'https://example.com/e1'}
        evidence = {'article': {'url': event['url'], 'title': '原文', 'fetched_at': '2026-11-23'}}
        package = delivery.build_delivery(event, evidence, final_payload())
        self.assertEqual(package['schema'], delivery.DELIVERY_SCHEMA)
        for section in delivery.DELIVERY_TYPES:
            self.assertTrue(package[section])
        self.assertTrue(package['checks']['passed'])
        self.assertEqual(package['growth_creative']['status'],
                         'draft_needs_operational_confirmation')
        self.assertTrue(package['growth_creative']['preconditions'])
        self.assertTrue(package['growth_creative']['source_facts'])

    def test_missing_material_fails_check(self):
        payload = final_payload()
        payload['parsed']['material'] = {}
        package = delivery.build_delivery(
            {'event_id': 'e1', 'title': 't'}, {'article': {}}, payload)
        self.assertFalse(package['checks']['checks']['material'])
        self.assertFalse(package['checks']['passed'])

    def test_html_escapes_and_contains_tabs(self):
        package = delivery.build_delivery(
            {'event_id': 'e1', 'title': '<script>alert(1)</script>'},
            {'article': {}}, final_payload())
        page = delivery.render_html(package)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script>alert(1)</script>', page)
        for label in ('情报', '素材', '增长创意', '质量检查'):
            self.assertIn(label, page)
        self.assertIn('data-tab="creative"', page)


class DeliveryPackTests(unittest.TestCase):
    def test_build_pack_writes_json_preview_and_index(self):
        from scripts.v4_delivery_pack import build_pack
        with tempfile.TemporaryDirectory(prefix='v4-delivery-') as tmp:
            root = Path(tmp)
            event_id = 'e1'
            folder = root / 'pack' / event_id
            folder.mkdir(parents=True)
            (folder / 'evidence.json').write_text(json.dumps(
                {'event': {'event_id': event_id, 'title': '合成事件',
                           'url': 'https://example.com/e1'},
                 'article': {'body': 'x' * 300, 'url': 'https://example.com/e1'}},
                ensure_ascii=False), encoding='utf-8')
            (folder / 'final.json').write_text(json.dumps(final_payload(), ensure_ascii=False),
                                               encoding='utf-8')
            (root / 'pack' / 'summary.json').write_text(json.dumps(
                {'events': [{'event_id': event_id}]}, ensure_ascii=False), encoding='utf-8')
            result = build_pack(root / 'pack', root / 'out')
            self.assertEqual(len(result['events']), 1)
            self.assertTrue((root / 'out' / event_id / 'delivery.json').is_file())
            self.assertTrue((root / 'out' / event_id / 'preview.html').is_file())
            self.assertTrue((root / 'out' / 'index.html').is_file())
            self.assertIn(event_id, (root / 'out' / 'index.html').read_text(encoding='utf-8'))
            index = load_delivery_index(root / 'out')
            self.assertEqual(index['schema'], 'v4-delivery-index-v0.1')
            self.assertEqual(len(index['deliveries']), 1)
            self.assertEqual(index['deliveries'][0]['event_id'], event_id)
            self.assertTrue(index['deliveries'][0]['checks_passed'])

    def test_missing_index_returns_empty_manifest(self):
        index = load_delivery_index('/path/does/not/exist')
        self.assertEqual(index['deliveries'], [])
        self.assertIn('no delivery index', index['note'])


if __name__ == '__main__':
    unittest.main()
