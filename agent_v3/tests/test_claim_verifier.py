"""Alpha25 P0: deterministic claim-vs-evidence verification before usability.

Fixtures marked "real case" are verbatim excerpts from the saved payloads of the
2026-10-12 quality-first experiment (data/v3/experiments/quality-first-600-20261012
runtime databases of case-260d02fea3f8-C and case-be8b588b7497-C), trimmed only
at their ends. They are the two acceptance cases named in the user review.
"""
import json
import unittest
from unittest.mock import patch

from agent_v2.store import dump, now_iso
from agent_v3.store import Store
from agent_v3.claim_verifier import verify_creative, verify_material, verify_signal, summarize


# --- real case: 《月圆之夜》C run, saved game_signal 3 ------------------------
MOONNIGHT_SIGNAL_EXTRA_CLAUSES = {
    'observed_change': '新卡池、新种族、新机制齐登场，新赛季内容扩充，玩法再升级',
    'facts': [{'evidence_id': 'evidence_c784e50e0c245cfe24df', 'quote': '新卡池、新种族、新机制齐登场✨'}],
}

# --- real case: 《月圆之夜》C run, saved game_signal 1 (fully quoted) ---------
MOONNIGHT_SIGNAL_QUOTED = {
    'observed_change': '1V1对战模式全新S5赛季【追忆与决心】将于10月15日正式上线',
    'facts': [{'evidence_id': 'evidence_c784e50e0c245cfe24df',
               'quote': '《月圆之夜》1V1对战模式全新S5赛季【追忆与决心】将于 10 月 15 日正式上线！'}],
}
MOONNIGHT_QUOTES = [
    {'evidence_id': 'evidence_c784e50e0c245cfe24df',
     'quote': '《月圆之夜》1V1对战模式全新S5赛季【追忆与决心】将于 10 月 15 日正式上线！'},
    {'evidence_id': 'evidence_c784e50e0c245cfe24df', 'quote': '全新卡池【追忆与决心】开放，推出共计65+张新卡牌！'},
]

# --- real case: 《月圆之夜》C run, saved copy material (declared link slot) ---
MOONNIGHT_COPY_MATERIAL = {
    'kind': 'copy', 'origin': 'original',
    'title': '《月圆之夜》S5赛季「遇见年轻时的自己」社区讨论引导 / 传播文案',
    'content': '如果对局中遇见年轻时的自己，你会打出哪张牌？《月圆之夜》1V1对战S5赛季「追忆与决心」10月15日上线——65+张新卡牌、新种族、新机制登场。来TapTap聊聊你的新赛季卡组思路。{{TapTap承接链接}}',
    'rights_status': '2. {{TapTap承接链接}}为待替换占位符，上线前须核实入口后替换。',
}

# --- real case: 《明日方舟》C run, saved game_adaptation material -------------
# The delivered template simplified the participation path to "带话题发帖即可",
# left the deadline as 截止X月X日 and used a generic #游戏名SideStory# slot; the
# only quote was the post title line.
ARKNIGHTS_TITLE_ONLY_MATERIAL = {
    'kind': 'game_adaptation', 'origin': 'original',
    'title': '「新版本开启+投稿活动」联动公告模板',
    'content': '【SideStory开启】游戏名全新SideStory「名称」已上线！同步开启投稿征集——二创、攻略、解析、体验感想均可参与，优秀稿件赢周边好礼，截止X月X日。带话题#游戏名SideStory#发帖即可参与→',
    'rights_status': '原创改编草案，叙事灵感来自官方公告描述，非玩家原话。',
}
ARKNIGHTS_TITLE_QUOTE = {'evidence_id': 'evidence_1f6cd6d0ba37d9928961',
                         'quote': '《明日方舟》全新SideStory「昨日海」开启！投稿赢周边好礼！'}
ARKNIGHTS_TITLES = {'evidence_1f6cd6d0ba37d9928961': '《明日方舟》全新SideStory「昨日海」开启！投稿赢周边好礼！'}


class ClaimVerifierTests(unittest.TestCase):
    def codes(self, verdict):
        return {reason['code'] for reason in verdict['reasons']}

    def test_real_case_arknights_template_material_is_insufficient(self):
        verdict = verify_material(ARKNIGHTS_TITLE_ONLY_MATERIAL, [ARKNIGHTS_TITLE_QUOTE], ARKNIGHTS_TITLES)
        self.assertEqual(verdict['status'], 'insufficient')
        self.assertIn('placeholder_unknown_date', self.codes(verdict))
        self.assertIn('placeholder_generic_slot', self.codes(verdict))
        self.assertIn('sufficiency_not_grounded', self.codes(verdict))
        self.assertIn('rule_detail_title_only', self.codes(verdict))
        self.assertNotIn('PRIVATE', json.dumps(verdict, ensure_ascii=False))

    def test_real_case_moonnight_signal_extra_clauses_are_insufficient(self):
        verdict = verify_signal(MOONNIGHT_SIGNAL_EXTRA_CLAUSES)
        self.assertEqual(verdict['status'], 'insufficient')
        self.assertIn('claim_clause_not_quoted', self.codes(verdict))
        detail = json.dumps(verdict['reasons'], ensure_ascii=False)
        self.assertIn('新赛季内容扩充', detail)
        self.assertIn('玩法再升级', detail)

    def test_fully_quoted_signal_is_not_flagged(self):
        verdict = verify_signal(MOONNIGHT_SIGNAL_QUOTED)
        self.assertEqual(verdict['status'], 'supported')
        self.assertEqual(verdict['reasons'], [])

    def test_declared_link_slot_is_draft_not_insufficient(self):
        verdict = verify_material(MOONNIGHT_COPY_MATERIAL, MOONNIGHT_QUOTES)
        self.assertEqual(verdict['status'], 'draft')
        self.assertEqual(self.codes(verdict), {'placeholder_link_slot'})

    def test_real_case_copy_creative_keeps_grounded_numbers_across_spacing(self):
        creative = {'copy': '《月圆之夜》1V1对战S5赛季「追忆与决心」10月15日上线——65+张新卡牌。'}
        verdict = verify_creative(creative, MOONNIGHT_QUOTES)
        self.assertEqual(verdict['status'], 'supported')

    def test_unquoted_detail_tokens_are_flagged_with_the_tokens(self):
        material = {'content': '活动截止10月30日，共送出66张卡牌。', 'kind': 'copy', 'origin': 'original'}
        verdict = verify_material(material, MOONNIGHT_QUOTES)
        self.assertEqual(verdict['status'], 'insufficient')
        self.assertIn('detail_not_quoted', self.codes(verdict))
        self.assertIn('10月30日', json.dumps(verdict['reasons'], ensure_ascii=False))

    def test_sufficiency_claim_needs_the_same_mechanism_in_a_quote(self):
        text = {'content': '带话题发帖即可参与活动，作品请原创首发。'}
        unflagged = verify_material(text, [{'evidence_id': 'e1', 'quote': '带话题发帖即可参与活动，优秀作品有奖。'}])
        self.assertEqual(unflagged['status'], 'supported')
        flagged = verify_material(text, [{'evidence_id': 'e1',
            'quote': '点击话题进入活动专题页，点击「参与活动」投稿即可；发帖请遵守社区行为准则。'}])
        self.assertIn('sufficiency_not_grounded', self.codes(flagged))

    def test_rule_details_need_a_body_level_quote_not_only_the_title(self):
        text = {'content': '活动参与截止时间为10月31日，具体步骤见专题页。'}
        only_title = verify_material(text, [ARKNIGHTS_TITLE_QUOTE], ARKNIGHTS_TITLES)
        self.assertIn('rule_detail_title_only', self.codes(only_title))
        with_body = verify_material(text, [{'evidence_id': 'e1',
            'quote': '即日起至10月31日23:59，点击话题进入活动专题页参与，具体步骤以专题页为准。'}])
        self.assertNotIn('rule_detail_title_only', self.codes(with_body))

    def test_missing_quotes_are_insufficient_not_silently_passed(self):
        verdict = verify_material({'content': '没有任何引用的素材'}, [])
        self.assertEqual(verdict['status'], 'insufficient')
        self.assertIn('quote_missing', self.codes(verdict))

    def test_summarize_counts_statuses(self):
        counts = summarize([{'status': 'supported'}, None, {'status': 'insufficient'}, {'status': 'draft'}])
        self.assertEqual(counts, {'supported': 1, 'draft': 1, 'insufficient': 1})


class MaterialGateTests(unittest.TestCase):
    def tearDown(self):
        self.store.close()

    def rows(self, statuses):
        return [{'material_id': 'material_%d' % i, 'kind': 'copy', 'origin': 'original',
                 'evidence_review': dump({'status': status, 'version': 'claim-verifier-v3.25'}) if status else None,
                 'risk_assessment': {'material_allowed': True}, 'tags': [], 'evidence_ids': [], 'source_versions': {}}
                for i, status in enumerate(statuses)]

    def test_usable_bank_excludes_insufficient_and_keeps_drafts_and_legacy(self):
        self.store = Store(':memory:')
        self.store.assets = lambda *args, **kwargs: self.rows(['supported', 'draft', None, 'insufficient'])
        usable = self.store.usable_materials(limit=10)
        self.assertEqual([m['material_id'] for m in usable], ['material_0', 'material_1', 'material_2'])
        self.assertEqual(self.store.material_evidence_status(self.rows(['insufficient'])[0]), 'insufficient')

    def test_saved_verdict_persists_updates_and_never_promotes_legacy_rows(self):
        self.store = Store(':memory:')
        run = self.store.create_run('claim verifier fixture')
        item = {'kind': 'copy', 'origin': 'original', 'title': '占位文案', 'content': '截止X月X日带话题#游戏名SideStory#发帖即可参与→',
                'rights_status': '草案', 'tags': [], 'evidence_ids': []}
        item['evidence_review'] = verify_material(item, [ARKNIGHTS_TITLE_QUOTE], ARKNIGHTS_TITLES)
        material_id = self.store.save_material(item, run)
        row = self.store.conn.execute('SELECT evidence_review FROM material WHERE material_id=?', (material_id,)).fetchone()
        self.assertEqual(json.loads(row[0])['status'], 'insufficient')
        # a later save of the same identity with a passing verdict updates the stored status
        item['evidence_review'] = {'status': 'supported', 'version': 'claim-verifier-v3.25', 'reasons': []}
        self.assertEqual(self.store.save_material(item, run), material_id)
        row = self.store.conn.execute('SELECT evidence_review FROM material WHERE material_id=?', (material_id,)).fetchone()
        self.assertEqual(json.loads(row[0])['status'], 'supported')
        # legacy rows without a verdict stay parseable as unknown
        self.assertIsNone(self.store.material_evidence_status({'evidence_review': None}))
        self.assertIsNone(self.store.material_evidence_status({'evidence_review': 'not-json'}))

    def test_funnel_counts_evidence_review_statuses(self):
        self.store = Store(':memory:')
        run = self.store.create_run('claim verifier funnel')
        self.store.conn.execute('INSERT INTO game_signal VALUES(?,?,?,?,?,?)',
            ('signal_x', 'topic_x', 'fp_x', run, now_iso(), dump({'evidence_review': {'status': 'insufficient'}})))
        for i, status in enumerate(('supported', 'draft')):
            self.store.conn.execute('INSERT INTO material VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('material_%d' % i, 'copy', 'original', 'title', 'content', '[]', '[]', '{}', 'rights', now_iso(), run, None, None,
                 dump({'status': status})))
        self.store.conn.commit()
        from agent_v3.delivery_audit import funnel
        counts = funnel(self.store, run)['persistence']['evidence_review']
        self.assertEqual(counts, {'supported': 1, 'draft': 1, 'insufficient': 1})


class PublicExportTests(unittest.TestCase):
    def test_material_projection_parses_verdict_and_keeps_legacy_rows_clean(self):
        from agent_v3.public_site import material as export_material
        verdict = {'status': 'insufficient', 'version': 'claim-verifier-v3.25', 'reasons': [{'code': 'placeholder_unknown_date', 'detail': '存在未填的日期占位'}]}
        row = {'material_id': 'm1', 'kind': 'copy', 'origin': 'original', 'title': 't', 'content': 'c', 'rights_status': 'r',
               'evidence_ids': ['e1'], 'source_versions': {}, 'created_at': now_iso(), 'evidence_review': dump(verdict)}
        exported = export_material(row)
        self.assertEqual(exported['evidence_review']['status'], 'insufficient')
        legacy = dict(row); legacy.pop('evidence_review')
        self.assertNotIn('evidence_review', export_material(legacy))


class AnalyzeWiringTests(unittest.TestCase):
    """The full main-agent path records verdicts on signals, materials and
    creative deliverables, and drafts stay in the usable bank while
    insufficient records would be excluded and kept only as saved drafts."""

    def test_analyze_and_creative_paths_record_evidence_reviews(self):
        from agent_v3.tests.test_main_agent import MainAgentTests
        from agent_v3.pipeline import process
        fixture = MainAgentTests('setUp')
        fixture.setUp()
        try:
            with patch('agent_v3.research.search_news', side_effect=AssertionError('fixture must not use network')):
                result = process(fixture.store, fixture.rid, model=fixture.model('opportunity'), topic_id=fixture.tid)
            self.assertEqual(result['status'], 'completed')
            steps = fixture.store.get_run(fixture.rid)['steps']
            verification = [s for s in steps if s['kind'] == 'claim_verification']
            self.assertEqual(len(verification), 1)
            # This fixture's signal selects one sentence as its quote while the
            # observed_change also carries the next sentence's content; the fuller
            # sentence is in the source body but not in the business packet's
            # quote candidates, so the signal is (correctly) marked insufficient
            # and stays saved as a marked draft instead of being dropped.
            signal_review = verification[0]['payload']['signals'][0]
            self.assertEqual(signal_review['status'], 'insufficient')
            self.assertEqual(signal_review['reasons'][0]['code'], 'claim_clause_not_quoted')
            self.assertIn('不进入可直接使用素材库', verification[0]['payload']['note'])
            statuses = sorted(json.loads(r[0])['status']
                              for r in fixture.store.conn.execute('SELECT evidence_review FROM material WHERE evidence_review IS NOT NULL'))
            self.assertEqual(statuses, ['draft', 'supported', 'supported'])
            self.assertEqual(fixture.store.game_signals()[0]['payload']['evidence_review']['status'], 'insufficient')
            self.assertEqual(len(fixture.store.usable_materials()), 3)
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
