"""冻结契约（acquisition/model.py）的不变量测试。

这些测试检查的是「契约不允许什么」，而不是实现细节；它们不依赖 driver、
store 或 policy，因此可以独立证明：提取成功不能自动变成事实已核验、
旧页面版本不能伪装成当前版本、策略无法绕过预算语义。
"""
import unittest

from acquisition import model


def region(region_id='rules', role='activity_rules', **kwargs):
    return model.Region(region_id=region_id, role=role, bbox=(0, 100, 800, 400), **kwargs)


def objective(**kwargs):
    base = {'objective_id': 'deadline', 'question': '确认活动截止日期',
            'answer_kind': 'date', 'region_hints': ('activity_rules',)}
    base.update(kwargs)
    return model.Objective(**base)


class ContractTests(unittest.TestCase):
    def test_extraction_never_implies_fact_verification(self):
        pending = model.Evidence(capture_id='capture_1', page_version='sha256:a',
                                 region='rules', extraction='ocr', text='截止 2026年11月20日')
        self.assertFalse(pending.verified)
        with self.assertRaises(ValueError):
            model.Evidence(capture_id='capture_1', page_version='sha256:a', region='rules',
                           extraction='ocr', text='截止 2026年11月20日', verified=True)
        verified = model.Evidence(capture_id='capture_1', page_version='sha256:a', region='rules',
                                  extraction='dom', text='截止 2026年11月20日',
                                  verified=True, verifier='deterministic_quote_v1')
        self.assertTrue(verified.verified)

    def test_receipt_key_binds_task_page_version_and_action(self):
        def receipt(page_version, text='x'):
            return model.Receipt(task_id='task_1', goal='确认日期', backend='playwright',
                                 action='extract', status='completed', page_version=page_version,
                                 region='rules').filled()

        first = receipt('sha256:page_a')
        again = receipt('sha256:page_a')
        other_page = receipt('sha256:page_b')
        self.assertEqual(first.idempotency_key, again.idempotency_key)
        self.assertNotEqual(first.idempotency_key, other_page.idempotency_key)
        self.assertTrue(first.receipt_id.startswith('receipt_'))

    def test_receipt_key_distinguishes_extraction_channels(self):
        def receipt(method):
            return model.Receipt(task_id='task_1', goal='确认日期', backend='playwright',
                                 action='extract', status='completed', page_version='sha256:page_a',
                                 region='rules', method=method).filled()

        self.assertNotEqual(receipt('dom').idempotency_key, receipt('ocr').idempotency_key)
        self.assertEqual(receipt('ocr').as_dict()['method'], 'ocr')
        with self.assertRaises(ValueError):
            model.Receipt(task_id='task_1', goal='g', backend='playwright', action='extract',
                          status='completed', method='browser')

    def test_receipt_rejects_unknown_states_and_next_steps(self):
        with self.assertRaises(ValueError):
            model.Receipt(task_id='task_1', goal='g', backend='playwright',
                          action='extract', status='done')
        with self.assertRaises(ValueError):
            model.Receipt(task_id='task_1', goal='g', backend='playwright',
                          action='extract', status='completed', next='maybe')
        with self.assertRaises(ValueError):
            model.Receipt(task_id='task_1', goal='g', backend='unknown',
                          action='extract', status='completed')

    def test_action_cost_is_fixed_and_visual_capture_charged(self):
        self.assertEqual(model.action_cost('inspect'), model.Cost(units=1))
        self.assertEqual(model.action_cost('interact'), model.Cost(units=2))
        self.assertEqual(model.action_cost('extract', 'dom'), model.Cost(units=1))
        self.assertEqual(model.action_cost('extract', 'ocr'), model.Cost(units=3, captures=1, ocr_calls=1))
        self.assertEqual(model.action_cost('extract', 'vlm'), model.Cost(units=5, captures=1, vlm_calls=1))
        with self.assertRaises(ValueError):
            model.action_cost('extract', None)
        with self.assertRaises(ValueError):
            model.action_cost('teleport')

    def test_action_space_rejects_incomplete_extract_or_interact(self):
        with self.assertRaises(ValueError):
            model.Action('extract')
        with self.assertRaises(ValueError):
            model.Action('extract', region_id='rules', method='browser')
        with self.assertRaises(ValueError):
            model.Action('interact', region_id='rules', method='dom')
        with self.assertRaises(ValueError):
            model.Action('stop')
        self.assertEqual(model.Action('stop', reason='blocked:captcha').cost, model.Cost())

    def test_budget_is_a_hard_bound(self):
        budget = model.Budget(max_units=4, max_captures=1, max_ocr_calls=1)
        self.assertTrue((model.Cost(units=4, captures=1, ocr_calls=1)).fits(budget))
        self.assertFalse((model.Cost(units=5)).fits(budget))
        self.assertFalse((model.Cost(units=1, captures=2)).fits(budget))
        state = model.PolicyState(goal=self.goal(), budgets=budget, page_version='sha256:a',
                                  spent=model.Cost(units=4))
        self.assertFalse(state.can_afford(model.action_cost('inspect')))
        self.assertEqual(state.remaining_units(), 0)

    def test_page_version_changes_with_page_content(self):
        first = model.derive_page_version({'dom_text': '旧规则 11月1日', 'revision': 1})
        second = model.derive_page_version({'dom_text': '新规则 11月20日', 'revision': 2})
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith('sha256:'))

    def test_structural_signature_survives_layout_but_not_role_loss(self):
        moved = [region('article', 'article_body'), region('rules', 'activity_rules')]
        same_roles_new_boxes = [
            model.Region(region_id='article', role='article_body', bbox=(0, 0, 800, 300)),
            model.Region(region_id='rules2', role='activity_rules', bbox=(0, 2600, 800, 400)),
        ]
        changed = [region('article', 'article_body'), region('poster', 'poster')]
        self.assertEqual(model.structural_signature(moved), model.structural_signature(same_roles_new_boxes))
        self.assertNotEqual(model.structural_signature(moved), model.structural_signature(changed))

    def test_candidate_matching_is_recall_not_verification(self):
        deadline = objective()
        self.assertTrue(model.candidate_text(deadline, '活动时间：2026年11月20日 23:59'))
        self.assertFalse(model.candidate_text(deadline, '活动时间另行通知'))
        count = objective(answer_kind='count', question='评论数量')
        self.assertTrue(model.candidate_text(count, '共 128 条评论'))
        presence = objective(answer_kind='presence', question='是否有登录墙')
        self.assertFalse(model.candidate_text(presence, '登录'))
        self.assertTrue(model.candidate_text(presence, '请先登录后查看评论'))
        self.assertFalse(model.candidate_region(deadline, region('comments', 'comments')))
        self.assertTrue(model.candidate_region(deadline, region('rules', 'activity_rules')))

    def test_policy_state_counts_candidates_without_declaring_them_verified(self):
        goal = self.goal()
        state = model.PolicyState(goal=goal, budgets=model.Budget(), page_version='sha256:a')
        observation = model.Observation(action='extract', page_version='sha256:a', region_id='rules',
                                        role='activity_rules', extraction='ocr',
                                        text='活动截止 2026年11月20日', capture_id='capture_1',
                                        bbox=(0, 100, 800, 400),
                                        cost=model.action_cost('extract', 'ocr'))
        state.record(model.Action('extract', region_id='rules', method='ocr'), observation)
        met, total = state.coverage()
        self.assertEqual((met, total), (1, 1))
        self.assertTrue(state.complete())
        self.assertEqual(state.spent, model.Cost(units=3, captures=1, ocr_calls=1))
        self.assertEqual(state.candidates['deadline'], ['capture_1'])

    def test_blocked_observation_is_never_readable(self):
        blocked = model.Observation(action='extract', page_version='sha256:a', region_id='captcha',
                                    role='captcha', extraction='ocr', text='请通过验证码',
                                    blocked=True, cost=model.action_cost('extract', 'ocr'))
        self.assertFalse(blocked.readable)
        self.assertFalse(model.Observation(action='extract', page_version='sha256:a',
                                           extraction='dom', text='').readable)

    def test_fixed_frame_offsets_match_legacy_worker_behavior(self):
        self.assertEqual(model.fixed_frame_offsets(3000), (0, 850, 1700))
        self.assertEqual(model.fixed_frame_offsets(1000), (0,))
        self.assertEqual(model.fixed_frame_offsets(1500), (0, 500))
        with self.assertRaises(ValueError):
            model.fixed_frame_offsets(-1)

    def test_goal_validation(self):
        with self.assertRaises(ValueError):
            model.ResearchGoal('', '问题', (objective(),))
        with self.assertRaises(ValueError):
            model.ResearchGoal('task_1', '问题', ())
        with self.assertRaises(ValueError):
            model.ResearchGoal('task_1', '问题', (objective(), objective()))
        self.assertEqual(len(self.goal().required_objectives), 1)

    def test_region_rejects_unknown_role_and_invalid_bbox(self):
        with self.assertRaises(ValueError):
            model.Region(region_id='x', role='main_content', bbox=(0, 0, 10, 10))
        with self.assertRaises(ValueError):
            model.Region(region_id='x', role='article_body', bbox=(0, 0, 0, 10))
        with self.assertRaises(ValueError):
            model.Region(region_id='x', role='article_body', bbox=(-1, 0, 10, 10))
        poster = model.Region(region_id='x', role='poster', bbox=(0, 0, 10, 10), visual_only=True)
        self.assertTrue(poster.visual_only)
        self.assertEqual(poster.text, '')
        self.assertFalse(model.Region(region_id='y', role='poster', bbox=(0, 0, 10, 10)).visual_only)
        with self.assertRaises(ValueError):
            model.Region(region_id='z', role='poster', bbox=(0, 0, 10, 10), visual_only='yes')

    def test_page_view_blocks_unknown_block_reason(self):
        with self.assertRaises(ValueError):
            model.PageView(page_version='sha256:a', url='https://example.com', title='t',
                           viewport=(1440, 1000), scroll_height=1000, regions=(),
                           blocked_reason='robot_check')

    def goal(self):
        return model.ResearchGoal('research_001', '确认活动截止日期', (objective(),))


if __name__ == '__main__':
    unittest.main()
