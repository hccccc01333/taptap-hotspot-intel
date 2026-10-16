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
from agent_v3.claim_verifier import aggregate, verify_creative, verify_material, verify_signal, summarize


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
        self.assertEqual(verdict['status'], 'needs_review')
        self.assertEqual(verdict['reasons'], [])

    def test_declared_link_slot_is_draft_not_insufficient(self):
        verdict = verify_material(MOONNIGHT_COPY_MATERIAL, MOONNIGHT_QUOTES)
        self.assertEqual(verdict['status'], 'needs_review')
        self.assertEqual(self.codes(verdict), {'placeholder_link_slot'})

    def test_real_case_copy_creative_keeps_grounded_numbers_across_spacing(self):
        creative = {'copy': '《月圆之夜》1V1对战S5赛季「追忆与决心」10月15日上线——65+张新卡牌。'}
        verdict = verify_creative(creative, MOONNIGHT_QUOTES)
        self.assertEqual(verdict['status'], 'needs_review')

    def test_unquoted_detail_tokens_are_flagged_with_the_tokens(self):
        material = {'content': '活动截止10月30日，共送出66张卡牌。', 'kind': 'copy', 'origin': 'original'}
        verdict = verify_material(material, MOONNIGHT_QUOTES)
        self.assertEqual(verdict['status'], 'insufficient')
        self.assertIn('detail_not_quoted', self.codes(verdict))
        self.assertIn('10月30日', json.dumps(verdict['reasons'], ensure_ascii=False))

    def test_sufficiency_claim_needs_the_same_mechanism_in_a_quote(self):
        text = {'content': '带话题发帖即可参与活动，作品请原创首发。'}
        unflagged = verify_material(text, [{'evidence_id': 'e1', 'quote': '带话题发帖即可参与活动，优秀作品有奖。'}])
        self.assertEqual(unflagged['status'], 'needs_review')
        flagged = verify_material(text, [{'evidence_id': 'e1',
            'quote': '点击话题进入活动专题页，点击「参与活动」投稿即可；发帖请遵守社区行为准则。'}])
        self.assertIn('sufficiency_not_grounded', self.codes(flagged))

    def test_negation_difference_is_insufficient_in_both_directions(self):
        # A claim that denies what the quote states.
        denied = verify_material({'content': '本作不支持多人联机。'},
                                 [{'evidence_id': 'e1', 'quote': '本作支持多人联机，可随时组队。'}])
        self.assertEqual(denied['status'], 'insufficient')
        self.assertIn('negation_mismatch', self.codes(denied))
        # A claim that affirms what the quote denies (the user's example).
        affirmed = verify_material({'content': '本作支持多人联机，可随时组队。'},
                                   [{'evidence_id': 'e1', 'quote': '本作不支持多人联机。'}])
        self.assertEqual(affirmed['status'], 'insufficient')
        self.assertIn('negation_mismatch', self.codes(affirmed))
        # Grounded negation and grounded affirmation stay unflagged.
        grounded_denial = verify_material({'content': '本作不支持多人联机。'},
                                          [{'evidence_id': 'e1', 'quote': '本作不支持多人联机。'}])
        self.assertNotIn('negation_mismatch', self.codes(grounded_denial))
        grounded = verify_material({'content': '本作支持多人联机。'},
                                   [{'evidence_id': 'e1', 'quote': '本作支持多人联机，可随时组队。'}])
        self.assertNotIn('negation_mismatch', self.codes(grounded))

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

    def test_aggregate_withholds_a_composite_when_any_part_fails(self):
        passing = verify_material({'content': '投稿截止10月31日23:59。'},
                                  [{'evidence_id': 'e1', 'quote': '即日起至10月31日23:59'}])
        self.assertEqual(passing['status'], 'needs_review')
        failing = verify_material({'content': '投稿截止10月99日。'},
                                  [{'evidence_id': 'e1', 'quote': '即日起至10月31日23:59'}])
        self.assertEqual(failing['status'], 'insufficient')
        combined = aggregate([passing, failing])
        self.assertEqual(combined['status'], 'insufficient')
        self.assertTrue(any(r['code'] == 'detail_not_quoted' for r in combined['reasons']))
        self.assertEqual(aggregate([passing, passing])['status'], 'needs_review')
        # merged reasons are de-duplicated
        duplicates = aggregate([failing, failing])
        codes = [r['code'] for r in duplicates['reasons']]
        self.assertEqual(len(codes), len(set(codes)))

    def test_summarize_counts_statuses(self):
        counts = summarize([{'status': 'verified'}, None, {'status': 'insufficient'}, {'status': 'needs_review'}])
        self.assertEqual(counts, {'verified': 1, 'needs_review': 1, 'insufficient': 1})


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
        self.store.assets = lambda *args, **kwargs: self.rows(['verified', 'needs_review', None, 'insufficient'])
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
        self.assertEqual(self.store.material_evidence_status({'evidence_review': None}), 'legacy_unreviewed')
        self.assertEqual(self.store.material_evidence_status({'evidence_review': 'not-json'}), 'legacy_unreviewed')

    def test_funnel_counts_evidence_review_statuses(self):
        self.store = Store(':memory:')
        run = self.store.create_run('claim verifier funnel')
        self.store.conn.execute('INSERT INTO game_signal VALUES(?,?,?,?,?,?)',
            ('signal_x', 'topic_x', 'fp_x', run, now_iso(), dump({'evidence_review': {'status': 'insufficient'}})))
        for i, status in enumerate(('needs_review', 'needs_review')):
            self.store.conn.execute('INSERT INTO material VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('material_%d' % i, 'copy', 'original', 'title', 'content', '[]', '[]', '{}', 'rights', now_iso(), run, None, None,
                 dump({'status': status})))
        self.store.conn.commit()
        from agent_v3.delivery_audit import funnel
        counts = funnel(self.store, run)['persistence']['evidence_review']
        self.assertEqual(counts, {'verified': 0, 'needs_review': 2, 'insufficient': 1})


class PublicExportTests(unittest.TestCase):
    def test_material_projection_parses_verdict_and_keeps_legacy_rows_clean(self):
        from agent_v3.public_site import material as export_material
        verdict = {'status': 'insufficient', 'version': 'claim-verifier-v3.25', 'reasons': [{'code': 'placeholder_unknown_date', 'detail': '存在未填的日期占位'}]}
        row = {'material_id': 'm1', 'kind': 'copy', 'origin': 'original', 'title': 't', 'content': 'c', 'rights_status': 'r',
               'evidence_ids': ['e1'], 'source_versions': {}, 'created_at': now_iso(), 'evidence_review': dump(verdict)}
        exported = export_material(row)
        self.assertEqual(exported['evidence_review']['status'], 'insufficient')
        self.assertEqual(exported['evidence_status'], 'insufficient')
        legacy = dict(row); legacy.pop('evidence_review')
        exported_legacy = export_material(legacy)
        self.assertNotIn('evidence_review', exported_legacy)
        self.assertEqual(exported_legacy['evidence_status'], 'legacy_unreviewed')


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
            # The fixture selects the quote that covers its claim, so the signal
            # passes deterministic checks (needs_review, never "verified") and
            # stays in the gated feed.
            signal_review = verification[0]['payload']['signals'][0]
            self.assertEqual(signal_review['status'], 'needs_review')
            self.assertIn('不进入可直接使用素材库', verification[0]['payload']['note'])
            statuses = sorted(json.loads(r[0])['status']
                              for r in fixture.store.conn.execute('SELECT evidence_review FROM material WHERE evidence_review IS NOT NULL'))
            self.assertEqual(statuses, ['needs_review', 'needs_review', 'needs_review'])
            self.assertEqual(len(fixture.store.game_signals()), 1)
            self.assertEqual(fixture.store.game_signals()[0]['payload']['evidence_review']['status'], 'needs_review')
            self.assertEqual(len(fixture.store.usable_materials()), 3)
        finally:
            fixture.tearDown()

    def test_insufficient_signal_is_withheld_from_feeds_but_kept_as_draft(self):
        from agent_v3.tests.test_main_agent import MainAgentTests
        from agent_v3.pipeline import process
        fixture = MainAgentTests('setUp')
        fixture.setUp()
        try:
            inner = fixture.model('watch')
            class Misquoted:
                def __init__(self, inner): self._inner = inner
                def __getattr__(self, name): return getattr(self._inner, name)
                def run_task(self, stage, packet, schema, system, **kwargs):
                    value = self._inner.run_task(stage, packet, schema, system, **kwargs)
                    if stage == 'intelligence':
                        for signal in value['result']['game_signals']:
                            signal['basis_refs'] = [1]  # quote no longer covers the claim
                    return value
            with patch('agent_v3.research.search_news', side_effect=AssertionError('fixture must not use network')):
                result = process(fixture.store, fixture.rid, model=Misquoted(inner), topic_id=fixture.tid)
            self.assertEqual(result['status'], 'intelligence_ready')
            saved = json.loads(fixture.store.conn.execute('SELECT payload FROM game_signal').fetchone()[0])
            self.assertEqual(saved['evidence_review']['status'], 'insufficient')
            self.assertEqual(fixture.store.game_signals(), [])
            self.assertEqual(fixture.store.overview()['counts']['withheld_insufficient'], 1)
            topic_outputs = __import__('agent_v3.presentation', fromlist=['business_outputs'])
            from agent_v3.discovery import read_topic
            self.assertEqual(read_topic(fixture.store, fixture.tid)['business_outputs']['game_signals'], [])
        finally:
            fixture.tearDown()


    def test_creative_with_failing_script_is_withheld_while_copy_passes(self):
        from agent_v3.tests.test_main_agent import MainAgentTests
        from agent_v3.pipeline import process
        fixture = MainAgentTests('setUp')
        fixture.setUp()
        try:
            inner = fixture.model('opportunity')
            class BadScript:
                def __init__(self, inner): self._inner = inner
                def __getattr__(self, name): return getattr(self._inner, name)
                def run_task(self, stage, packet, schema, system, **kwargs):
                    value = self._inner.run_task(stage, packet, schema, system, **kwargs)
                    if stage == 'creative_production':
                        # copy stays grounded; the script fabricates a deadline
                        value['result']['script'] = value['result']['script'] + ' 补充：活动截止10月99日。'
                    return value
            with patch('agent_v3.research.search_news', side_effect=AssertionError('fixture must not use network')):
                result = process(fixture.store, fixture.rid, model=BadScript(inner), topic_id=fixture.tid)
            self.assertEqual(result['status'], 'completed')
            creative = fixture.store.conn.execute('SELECT payload FROM creative').fetchone()
            payload = json.loads(creative[0])
            self.assertEqual(payload['evidence_review']['status'], 'insufficient')
            statuses = {d['kind']: d['evidence_review']['status'] for d in payload['deliverables']}
            self.assertEqual(statuses['copy'], 'needs_review')
            self.assertEqual(statuses['script'], 'insufficient')
            # the composite creative is withheld; its failing script is not usable
            self.assertEqual(fixture.store.creative_feed(), [])
            usable = {m['title'] for m in fixture.store.usable_materials()}
            self.assertFalse(any('短视频脚本' in title for title in usable))
            self.assertTrue(any('传播文案' in title for title in usable))
        finally:
            fixture.tearDown()


    def test_claim_feedback_retry_recovers_an_unsupported_clause(self):
        from agent_v3.tests.test_main_agent import MainAgentTests
        from agent_v3.pipeline import process
        fixture = MainAgentTests('setUp')
        fixture.setUp()
        try:
            inner = fixture.model('watch')
            state = {'calls': 0}
            class FirstBad:
                def __init__(self, inner): self._inner = inner
                def __getattr__(self, name): return getattr(self._inner, name)
                def run_task(self, stage, packet, schema, system, **kwargs):
                    if stage == 'intelligence':
                        state['calls'] += 1
                    value = self._inner.run_task(stage, packet, schema, system, **kwargs)
                    if stage == 'intelligence' and state['calls'] == 1:
                        for signal in value['result']['game_signals']:
                            signal['observed_change'] = signal['observed_change'] + '，玩家反响热烈'
                    return value
            with patch('agent_v3.research.search_news', side_effect=AssertionError('fixture must not use network')):
                result = process(fixture.store, fixture.rid, model=FirstBad(inner), topic_id=fixture.tid)
            self.assertEqual(result['status'], 'intelligence_ready')
            # the first attempt was rejected with safe claim feedback, retried once
            self.assertEqual(state['calls'], 2)
            steps = fixture.store.get_run(fixture.rid)['steps']
            feedback = [s for s in steps if s['kind'] == 'validation_error' and '引用核对' in (s['payload'].get('error') or '')]
            self.assertEqual(len(feedback), 1)
            # the corrected delivery passes the gate and is saved unflagged
            saved = json.loads(fixture.store.conn.execute('SELECT payload FROM game_signal').fetchone()[0])
            self.assertEqual(saved['evidence_review']['status'], 'needs_review')
            self.assertEqual(len(fixture.store.game_signals()), 1)
        finally:
            fixture.tearDown()


    def test_interpretation_claim_feedback_retries_and_recovers(self):
        from agent_v3.tests.test_main_agent import MainAgentTests
        from agent_v3.pipeline import process
        fixture = MainAgentTests('setUp')
        fixture.setUp()
        try:
            inner = fixture.model('watch')
            state = {'calls': 0}
            class FirstBadCite:
                def __init__(self, inner): self._inner = inner
                def __getattr__(self, name): return getattr(self._inner, name)
                def run_task(self, stage, packet, schema, system, **kwargs):
                    if stage == 'interpretation':
                        state['calls'] += 1
                    value = self._inner.run_task(stage, packet, schema, system, **kwargs)
                    if stage == 'interpretation' and state['calls'] == 1:
                        value['result']['core'] = [{'text': '完全不在引文中的断言内容', 'basis_refs': [1]}]
                    return value
            with patch('agent_v3.research.search_news', side_effect=AssertionError('fixture must not use network')):
                result = process(fixture.store, fixture.rid, model=FirstBadCite(inner), topic_id=fixture.tid)
            # one bounded retry with safe interpretation-claim feedback, then recovered
            self.assertEqual(state['calls'], 2)
            steps = fixture.store.get_run(fixture.rid)['steps']
            feedback = [s for s in steps if s['kind'] == 'validation_error' and '解读引用核对' in (s['payload'].get('error') or '')]
            self.assertEqual(len(feedback), 1)
            self.assertTrue(result['status'] in ('intelligence_ready', 'completed'))
            from agent_v3.discovery import read_topic
            fingerprint = read_topic(fixture.store, fixture.tid)['fingerprint']
            self.assertIsNotNone(fixture.store.interpretation(fixture.tid, fingerprint))
        finally:
            fixture.tearDown()


    def test_business_packet_carries_key_fact_hints_from_full_excerpts(self):
        from agent_v3.task_packets import business_packet
        packet = {
            'evidence': [{'evidence_id': 'e1', 'title': '标题', 'body': '由某工作室开发，将于10月9日上线。', 'role': 'direct'}],
            'quote_candidates': [{'ref': 0, 'evidence_id': 'e1', 'quote': '将于10月9日上线。'}],
            'research_return': {'status': 'ready', 'headline': 'h', 'one_line': 'o', 'core': [
                {'text': 't', 'facts': [{'evidence_id': 'e1', 'quote': '将于10月9日上线。'}]}],
                'timeline': [], 'views': [], 'controversies': [], 'background': []},
        }
        result = business_packet(packet)
        self.assertIn('key_fact_hints', result)
        self.assertIn('10月9日', result['key_fact_hints'])
        self.assertTrue(any('某工作室' in hint for hint in result['key_fact_hints']))


if __name__ == '__main__':
    unittest.main()
