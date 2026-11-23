"""context 事件/时间线/上下文卡片的离线测试（不联网、不调模型）。"""
import json
import tempfile
import unittest
from pathlib import Path

from context import engine


BODY_V1 = (
    '合成活动公告正文。' * 20 +
    '\nKOMODO宣布《初音未来 连线消除》将于2026年10月29日登陆PS4、PS5与Steam。'
    '\n含税售价1980日元，支持简体中文。'
)
BODY_V2 = BODY_V1.replace('2026年10月29日', '2026年11月5日').replace(
    '含税售价1980日元，支持简体中文。', '售价与语言信息待官方更新。')


def build_pack(root: Path, *, body=BODY_V1, quote='2026年10月29日登陆PS4、PS5与Steam',
               event_id='miku_test') -> Path:
    folder = root / event_id
    folder.mkdir(parents=True)
    event = {'event_id': event_id, 'url': 'https://example.com/miku',
             'title': '《初音未来 连线消除》10月29日登陆PS4、5与Steam',
             'source': '示例来源', 'published_at': '2026-10-22 11:21:21',
             'objective': '确认发售时间、平台、价格'}
    (folder / 'evidence.json').write_text(json.dumps(
        {'event': event, 'article': {'title': '合成页', 'body': body,
                                     'url': event['url'],
                                     'fetched_at': '2026-11-15T10:00:00+0800'}},
        ensure_ascii=False), encoding='utf-8')
    final = {'parsed': {
        'headline': '合成草稿', 'one_line': '合成一句话',
        'facts': [{'text': '发售日期与平台', 'quote': quote},
                  {'text': '售价与语言', 'quote': '含税售价1980日元，支持简体中文'}],
        'unknowns': ['具体奖励规则未说明', '其他平台是否同步未说明'],
        'intelligence': {'summary': '合成摘要', 'why_taptap': '判断：行业动态',
                         'watch_next': ['官方后续']},
        'material': {'title': '速览', 'channel': '社区动态', 'draft': '文案',
                     'usage_boundary': '不得补写未确认信息'}},
        'quote_checks': {'facts_total': 2, 'facts_supported': 2,
                         'facts': [{'index': 1, 'supported': True},
                                   {'index': 2, 'supported': True}]}}
    (folder / 'final.json').write_text(json.dumps(final, ensure_ascii=False), encoding='utf-8')
    (root / 'summary.json').write_text(json.dumps(
        {'events': [{'event_id': event_id, 'title': event['title'], 'url': event['url'],
                     'delivered': True}]}, ensure_ascii=False), encoding='utf-8')
    return root


class ContextEngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='v4-context-test-')
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db = self.root / 'context.sqlite3'

    def test_ingest_creates_event_versions_facts_card_and_timeline(self):
        pack = build_pack(self.root / 'pack')
        result = engine.ingest_pack(pack, self.db)
        self.assertEqual(result['events'], 1)
        self.assertEqual(result['versions'], 1)
        self.assertEqual(result['facts'], 2)
        self.assertEqual(result['cards'], 1)
        detail = engine.event_detail('miku_test', self.db)
        self.assertEqual(detail['event']['event_id'], 'miku_test')
        self.assertEqual(len(detail['versions']), 1)
        self.assertEqual({fact['status'] for fact in detail['facts']}, {'active'})
        self.assertEqual([fact['quote_supported'] for fact in detail['facts']], [1, 1])
        card = detail['context_card']
        self.assertEqual(card['source_category'], 'public_http_observation')
        self.assertIn('2026年10月29日', card['dates'])
        self.assertIn('PS4', card['platforms'])
        self.assertTrue(any('1980' in price for price in card['prices']))
        self.assertEqual(len(card['unknowns']), 2)
        self.assertEqual(card['relevance_hypothesis']['kind'], 'judgement_not_fact')
        kinds = [entry['kind'] for entry in detail['timeline']]
        self.assertIn('event_created', kinds)
        self.assertIn('source_observed', kinds)
        self.assertIn('fact_recorded', kinds)
        self.assertIn('context_card_built', kinds)

    def test_reingest_same_version_is_idempotent(self):
        pack = build_pack(self.root / 'pack')
        engine.ingest_pack(pack, self.db)
        again = engine.ingest_pack(pack, self.db)
        self.assertEqual(again, {'events': 0, 'versions': 0, 'facts': 0,
                                 'stale': 0, 'cards': 0})
        detail = engine.event_detail('miku_test', self.db)
        self.assertEqual(len(detail['facts']), 2)
        self.assertEqual(len(detail['versions']), 1)

    def test_new_page_version_marks_missing_quote_stale(self):
        pack = build_pack(self.root / 'pack_v1')
        engine.ingest_pack(pack, self.db)
        # 用同一事件目录名、不同正文重新打包；旧引文在新版本中消失。
        pack2 = build_pack(self.root / 'pack_v2', body=BODY_V2,
                           quote='2026年11月5日登陆PS4、PS5与Steam',
                           event_id='miku_test')
        result = engine.ingest_pack(pack2, self.db)
        self.assertEqual(result['versions'], 1)
        self.assertGreaterEqual(result['stale'], 1)
        detail = engine.event_detail('miku_test', self.db)
        self.assertEqual(len(detail['versions']), 2)
        stale = [fact for fact in detail['facts'] if fact['status'] == 'stale']
        self.assertTrue(stale, '旧版本中消失的引文应标记 stale')
        self.assertTrue(all(fact['stale_at'] for fact in stale))
        self.assertIn('fact_stale', [entry['kind'] for entry in detail['timeline']])
        cards = [entry for entry in detail['timeline'] if entry['kind'] == 'context_card_built']
        self.assertGreaterEqual(len(cards), 2)

    def test_short_body_is_skipped_without_failure(self):
        pack = build_pack(self.root / 'pack', body='太短')
        result = engine.ingest_pack(pack, self.db)
        self.assertEqual(result['events'], 0)
        self.assertEqual(engine.event_detail('miku_test', self.db), {})


    def test_same_game_multi_source_group_and_conflict_candidates(self):
        # 两个不同事件（不同 event_id/正文日期），标题指向同一游戏，应归入同一 group。
        pack1 = build_pack(self.root / 'p1', event_id='event_a', body=BODY_V1,
                           quote='2026年10月29日登陆PS4、PS5与Steam')
        pack2 = build_pack(self.root / 'p2', event_id='event_b', body=BODY_V2,
                           quote='2026年11月5日登陆PS4、PS5与Steam')
        engine.ingest_pack(pack1, self.db)
        engine.ingest_pack(pack2, self.db)
        key = engine.game_key_from_title('《初音未来 连线消除》10月29日登陆PS4、5与Steam')
        detail = engine.group_detail(key, self.db)
        self.assertEqual(detail['group']['group_key'], key)
        self.assertEqual(len(detail['events']), 2)
        self.assertEqual(len(detail['facts']), 3)  # 相同价格事实去重，日期事实保留两条
        self.assertTrue(detail['conflict_candidates'])
        kinds = {item['kind'] for item in detail['conflict_candidates']}
        self.assertIn('date_candidates', kinds)
        self.assertTrue(detail['timeline'])
        self.assertGreaterEqual(len(detail['timeline']), 4)
        listed = engine.groups(self.db)
        self.assertEqual(listed[0]['group_key'], key)
        self.assertEqual(listed[0]['events'], 2)

    def test_group_is_idempotent(self):
        pack = build_pack(self.root / 'p', event_id='event_a')
        engine.ingest_pack(pack, self.db)
        engine.ingest_pack(pack, self.db)
        key = engine.game_key_from_title('《初音未来 连线消除》10月29日登陆PS4、5与Steam')
        detail = engine.group_detail(key, self.db)
        self.assertEqual(len(detail['events']), 1)
        self.assertEqual(len(detail['facts']), 2)


    def test_metadata_only_stub_registers_source_and_timeline(self):
        stub = engine.add_event_stub(
            event_id='steam_meta', title='THE FINALS gets a huge update',
            source_url='https://example.com/steam', published_at='2026-10-21 09:46:34',
            game_key='thefinals', db_path=self.db)
        self.assertEqual(stub['game_key'], 'thefinals')
        detail = engine.event_detail('steam_meta', self.db)
        self.assertEqual(detail['event']['published_at'], '2026-10-21 09:46:34')
        self.assertIn('source_metadata_only',
                      [entry['kind'] for entry in detail['timeline']])
        self.assertEqual(engine.group_facts('thefinals', self.db), [])


if __name__ == '__main__':
    unittest.main()
