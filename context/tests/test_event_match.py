"""event_match 候选逻辑测试（离线）。"""
import tempfile
import unittest
from pathlib import Path

from context import engine, event_match
from context.tests.test_engine import build_pack


def event(event_id, title):
    return {'event_id': event_id, 'title': title,
            'source_url': f'https://example.com/{event_id}'}


def fact(event_id, text, quote, status='active'):
    return {'event_id': event_id, 'text': text, 'quote': quote, 'status': status,
            'source_url': f'https://example.com/{event_id}'}


class SameEventTests(unittest.TestCase):
    def test_same_event_candidates_are_flagged_not_merged(self):
        events = [event('a', '《游戏A》新版本10月20日上线 加入新角色'),
                  event('b', '《游戏A》10月20日上线 新版本加入新角色详情'),
                  event('c', '《游戏A》反作弊系统更新 不放弃其他平台')]
        facts = [
            fact('a', '10月20日上线', '新版本将于10月20日上线'),
            fact('a', '加入新角色', '本次更新加入新角色'),
            fact('b', '10月20日上线', '新版本将于10月20日上线'),
            fact('b', '加入新角色', '本次更新加入新角色'),
            fact('c', '反作弊更新', '更新采用内核级反作弊'),
        ]
        candidates = event_match.same_event_candidates(events, facts, subject='游戏A')
        pairs = {(item['left_event_id'], item['right_event_id']) for item in candidates}
        self.assertIn(('a', 'b'), pairs)
        self.assertNotIn(('a', 'c'), pairs)
        self.assertNotIn(('b', 'c'), pairs)
        self.assertTrue(all(item['auto_merge'] is False for item in candidates))

    def test_unrelated_events_do_not_candidate(self):
        events = [event('a', '《游戏A》赛季更新'), event('b', '《游戏A》反作弊公告')]
        facts = [fact('a', '新赛季', '第12赛季上线'), fact('b', '反作弊', '采用内核反作弊')]
        self.assertEqual(event_match.same_event_candidates(events, facts, subject='游戏A'), [])


class CrossLanguageTests(unittest.TestCase):
    def test_same_game_same_publication_day_is_metadata_candidate(self):
        events = [
            {'event_id': 'a', 'title': '《游戏A》第12赛季上线', 'published_at': '2026-10-20 11:00:00'},
            {'event_id': 'b', 'title': 'Game A gets a huge update',
             'published_at': '2026-10-21 09:00:00'},
        ]
        facts = [fact('a', '10月20日上线', '第12赛季计划于10月20日上线')]
        candidates = event_match.same_event_candidates(events, facts, subject='游戏A',
                                                       threshold=0.30)
        self.assertEqual(len(candidates), 1)
        item = candidates[0]
        self.assertEqual(item['publication_proximity'], 1.0)
        self.assertTrue(item['metadata_only'])
        self.assertFalse(item['auto_merge'])

    def test_distant_publication_dates_do_not_candidate(self):
        events = [
            {'event_id': 'a', 'title': '《游戏A》第12赛季上线', 'published_at': '2026-10-20'},
            {'event_id': 'b', 'title': 'Game A gets a huge update',
             'published_at': '2026-11-20'},
        ]
        facts = [fact('a', '10月20日上线', '第12赛季计划于10月20日上线')]
        self.assertEqual(event_match.same_event_candidates(
            events, facts, subject='游戏A', threshold=0.30), [])


class FactConflictTests(unittest.TestCase):
    def test_similar_facts_with_different_prices_are_candidates(self):
        facts = [fact('a', '售价1980日元', '售价1980日元'),
                 fact('b', '售价2980日元', '售价2980日元')]
        candidates = event_match.fact_conflict_candidates(facts)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['left_atoms'], ['1980'])
        self.assertEqual(candidates[0]['right_atoms'], ['2980'])
        self.assertIn('unresolved', candidates[0]['resolution'])

    def test_same_atoms_or_same_event_do_not_conflict(self):
        facts = [fact('a', '售价1980日元', '售价1980日元'),
                 fact('b', '售价1980日元', '售价1980日元')]
        self.assertEqual(event_match.fact_conflict_candidates(facts), [])
        same_event = [fact('a', '售价1980日元', '售价1980日元'),
                      fact('a', '售价2980日元', '售价2980日元')]
        self.assertEqual(event_match.fact_conflict_candidates(same_event), [])

    def test_inactive_facts_are_ignored(self):
        facts = [fact('a', '售价1980日元', '售价1980日元'),
                 fact('b', '售价2980日元', '售价2980日元', status='stale')]
        self.assertEqual(event_match.fact_conflict_candidates(facts), [])


class GroupReportTests(unittest.TestCase):
    def test_group_report_uses_group_detail(self):
        with tempfile.TemporaryDirectory(prefix='event-match-') as tmp:
            root = Path(tmp)
            pack = build_pack(root / 'pack', event_id='event_a')
            engine.ingest_pack(pack, root / 'db.sqlite3')
            key = engine.game_key_from_title('《初音未来 连线消除》10月29日登陆')
            report = event_match.group_match_report(key, root / 'db.sqlite3')
            self.assertEqual(report['group_key'], key)
            self.assertEqual(len(report['events']), 1)
            self.assertTrue(report['note'])
            self.assertIn('same_event_candidates', report)
            self.assertIn('semantic_same_event_candidates', report)
            self.assertEqual(report['semantic_same_event_candidates'], [])
            self.assertIn('未配置', report['semantic_note'])
            self.assertIn('fact_conflict_candidates', report)


if __name__ == '__main__':
    unittest.main()
