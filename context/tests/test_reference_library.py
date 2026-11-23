"""Reference library tests (offline, no model/network)."""
import json
import tempfile
import unittest
from pathlib import Path

from context import delivery, reference_library


def library_payload():
    return {
        'schema': reference_library.REFERENCE_SCHEMA,
        'samples': [
            {'ref_id': 'r1', 'platform': 'taptap', 'title': '官方联动公告',
             'url': 'https://example.com/r1', 'excerpt': '官方宣布联动定档。',
             'rights_status': '公开观察；引用前核查授权'},
            {'ref_id': 'r2', 'platform': 'taptap', 'title': '玩家讨论帖',
             'url': 'https://example.com/r2', 'excerpt': '玩家在讨论对局。',
             'rights_status': '公开观察；引用前核查授权'},
        ],
        'patterns': [
            {'id': 'official_3items', 'name': '官宣三件套',
             'structure': ['授权/时间', '三项内容', '口号'],
             'source_refs': ['r1']},
            {'id': 'nostalgia_hook', 'name': '童年回忆切入',
             'structure': ['IP记忆点', '能力对应'], 'source_refs': ['r1']},
            {'id': 'community_question', 'name': '玩家问题式互动',
             'structure': ['具体问题', '讨论引导'], 'source_refs': ['r2']},
        ],
    }


def final_payload():
    return {
        'parsed': {
            'facts': [{'text': '10月29日联动开启', 'quote': '定档10月29日正式开启'},
                      {'text': '包含新角色与主题地图', 'quote': '包含联动专属新角色、全新主题地图'}],
            'unknowns': ['角色技能未公布'],
            'intelligence': {'one_line': '联动定档10月29日', 'summary': '官方宣布联动内容。',
                             'why_taptap': '判断：童年 IP 有讨论点'},
            'material': {'title': '联动速览', 'channel': '社区动态', 'draft': '文案',
                         'usage_boundary': '不补写未确认信息'},
        },
        'quote_checks': {'facts_supported': 2, 'facts_total': 2},
    }


class ReferenceLibraryTests(unittest.TestCase):
    def test_load_valid_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ref.json'
            path.write_text(json.dumps(library_payload(), ensure_ascii=False), encoding='utf-8')
            library = reference_library.load_library(path)
            self.assertEqual(len(library['samples']), 2)
            self.assertEqual(len(library['patterns']), 3)

    def test_invalid_schema_or_unknown_ref_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = library_payload()
            bad['schema'] = 'other'
            path = Path(tmp) / 'bad.json'
            path.write_text(json.dumps(bad, ensure_ascii=False), encoding='utf-8')
            with self.assertRaises(ValueError):
                reference_library.load_library(path)
            bad = library_payload()
            bad['patterns'][0]['source_refs'] = ['missing']
            path.write_text(json.dumps(bad, ensure_ascii=False), encoding='utf-8')
            with self.assertRaises(ValueError):
                reference_library.load_library(path)

    def test_creative_uses_patterns_and_keeps_source_refs(self):
        creative = reference_library.build_creative_with_references(
            final_payload()['parsed']['intelligence'],
            final_payload()['parsed']['material'],
            final_payload()['parsed']['facts'],
            {'event_id': 'e1', 'title': '合成事件'}, library_payload())
        self.assertIn('官宣三件套', creative['used_patterns'])
        self.assertIn('童年回忆切入', creative['used_patterns'])
        self.assertTrue(creative['reference_samples'])
        self.assertFalse(any(sample['excerpt'] in creative['copy_draft']
                             for sample in library_payload()['samples']))
        self.assertEqual(creative['status'], 'draft_needs_operational_confirmation')

    def test_delivery_with_reference_library_records_metadata(self):
        package = delivery.build_delivery(
            {'event_id': 'e1', 'title': '合成事件', 'url': 'https://example.com/e1'},
            {'article': {'url': 'https://example.com/e1'}},
            final_payload(), reference_library=library_payload())
        self.assertTrue(package['checks']['passed'])
        self.assertEqual(package['reference_library']['samples'], 2)
        self.assertTrue(package['growth_creative']['reference_samples'])


if __name__ == '__main__':
    unittest.main()
