"""观察策略的机制测试。

测试只依赖冻结契约 ``acquisition.model`` 与策略层自身，自行构造 PageView /
PolicyState 并模拟 harness 的「执行动作 → 记录结果」循环，不依赖 driver / store /
harness（它们由其他文件并行实现）。

覆盖点：
- 固定三屏基线与现状 V3 基线的偏移顺序、成本、不提前停止、不看阻塞页差异；
- 自适应策略的阻塞停止、懒加载先交互、纯视觉走 OCR、目标覆盖停止、预算停止、
  连续无增益停止、候选排序受 hint 影响、同动作不重复、登录墙停止；
- 默认不请求 vlm/tabbit，策略不修改 PolicyState（只读契约）。
"""
import unittest

from acquisition import model
from acquisition.policy import AdaptiveObservationPolicy, FixedFramePolicy, LegacyV3Policy


# --------------------------------------------------------------------- 构造器
def region(region_id='rules', role='activity_rules', bbox=(0, 100, 800, 400), **kwargs):
    return model.Region(region_id=region_id, role=role, bbox=bbox, **kwargs)


def objective(objective_id='deadline', *, question='确认活动截止日期', answer_kind='date',
              region_hints=('activity_rules',), required=True):
    return model.Objective(objective_id=objective_id, question=question, answer_kind=answer_kind,
                           region_hints=region_hints, required=required)


def goal(*objectives, task_id='research_001', question='确认活动信息'):
    return model.ResearchGoal(task_id=task_id, question=question,
                              objectives=tuple(objectives) or (objective(),))


def page_view(*regions, scroll_height=3000, viewport=(1440, 1000), dom_text='',
              blocked_reason=None, page_version='sha256:page_v1'):
    return model.PageView(page_version=page_version, url='https://example.test/activity',
                          title='合成样本页', viewport=viewport, scroll_height=scroll_height,
                          regions=tuple(regions), dom_text=dom_text, blocked_reason=blocked_reason)


def policy_state(goal_obj, view, *, budgets=None, **kwargs):
    return model.PolicyState(goal=goal_obj, budgets=budgets or model.Budget(),
                             page_version=view.page_version, **kwargs)


def make_observation(action, view, *, text='', role=None, new_view=None, blocked=False):
    current = new_view or view
    return model.Observation(
        action=action.kind,
        page_version=current.page_version,
        region_id=action.region_id,
        role=role,
        extraction=action.method if action.kind == 'extract' else None,
        text=text,
        blocked=blocked,
        view=new_view,
    )


def run_until_stop(policy, goal_obj, view, state, *, respond=None, limit=20):
    """模拟 harness：逐步执行策略动作并记录结果，直到 stop。"""
    actions = []
    for _ in range(limit):
        action = policy.next_action(goal_obj, view, state)
        actions.append(action)
        if action.kind == 'stop':
            return actions
        observation = respond(action) if respond is not None else make_observation(action, view)
        state.record(action, observation)
    raise AssertionError('策略在限定步数内没有停止')


class StubHintProvider:
    """实现 model.HintProvider 的测试替身：只返回构造时给定的 hint。"""

    name = 'stub_hints'

    def __init__(self, hints):
        self._hints = tuple(hints)

    def hints(self, view, goal_obj):
        return self._hints


class CountingHintProvider(StubHintProvider):
    name = 'counting_hints'

    def __init__(self, hints):
        super().__init__(hints)
        self.calls = 0

    def hints(self, view, goal_obj):
        self.calls += 1
        return super().hints(view, goal_obj)


# ----------------------------------------------------------- 固定三屏截图基线
class FixedFramePolicyTests(unittest.TestCase):
    def test_emits_three_viewport_ocr_actions_in_fixed_offset_order(self):
        view = page_view(scroll_height=3000)  # 偏移：(0, 850, 1700)
        policy = FixedFramePolicy()
        g = goal()
        state = policy_state(g, view)

        actions = run_until_stop(policy, g, view, state)

        extracts = [action for action in actions if action.kind != 'stop']
        self.assertEqual([action.region_id for action in extracts],
                         ['viewport:0', 'viewport:1', 'viewport:2'])
        self.assertEqual({action.method for action in extracts}, {'ocr'})
        self.assertTrue(all(action.reason for action in extracts))
        self.assertEqual(actions[-1].kind, 'stop')
        self.assertEqual(actions[-1].reason, 'fixed_frames_exhausted')
        # 三屏 OCR 的成本必须完整记账：3 units/屏，含 1 次截图与 1 次 OCR。
        self.assertEqual(state.spent, model.Cost(units=9, captures=3, ocr_calls=3))
        self.assertEqual(state.steps, 3)

    def test_offsets_follow_legacy_worker_and_duplicates_collapse(self):
        # scroll_height 小于视口时三屏落在同一 y，模型层去重，基线只截 1 张。
        view = page_view(scroll_height=1000)
        policy = FixedFramePolicy()
        g = goal()
        state = policy_state(g, view)
        actions = run_until_stop(policy, g, view, state)
        self.assertEqual([a.region_id for a in actions if a.kind != 'stop'], ['viewport:0'])
        self.assertEqual(state.spent, model.Cost(units=3, captures=1, ocr_calls=1))

        # 1500px 页面：第 3 屏与第 2 屏重合，只剩 2 张截图。
        view = page_view(scroll_height=1500)
        state = policy_state(g, view)
        actions = run_until_stop(policy, g, view, state)
        self.assertEqual([a.region_id for a in actions if a.kind != 'stop'], ['viewport:0', 'viewport:1'])
        self.assertEqual(state.spent, model.Cost(units=6, captures=2, ocr_calls=2))

    def test_does_not_stop_early_even_when_answer_already_seen(self):
        rules = region(text='活动截止 2026年11月20日')
        view = page_view(rules)
        policy = FixedFramePolicy()
        g = goal()
        # 目标已被覆盖也不能让基线提前停止：基线不做目标匹配。
        state = policy_state(g, view, candidates={'deadline': ['capture_old']})

        def respond(action):
            return make_observation(action, view, text='活动截止 2026年11月20日',
                                    role='article_body')

        actions = run_until_stop(policy, g, view, state, respond=respond)
        self.assertEqual([a.region_id for a in actions if a.kind != 'stop'],
                         ['viewport:0', 'viewport:1', 'viewport:2'])
        self.assertEqual(actions[-1].reason, 'fixed_frames_exhausted')

    def test_ignores_blocked_page_difference(self):
        # 基线保留原貌：阻塞页也照常截图（证据是否入库由 harness 拦截）。
        view = page_view(region(), blocked_reason='captcha')
        policy = FixedFramePolicy()
        g = goal()
        state = policy_state(g, view)
        actions = run_until_stop(policy, g, view, state)
        self.assertEqual([a.region_id for a in actions if a.kind != 'stop'],
                         ['viewport:0', 'viewport:1', 'viewport:2'])
        self.assertEqual(actions[-1].reason, 'fixed_frames_exhausted')
        self.assertFalse(any(a.reason.startswith('blocked:') for a in actions))


# ------------------------------------------------------------- 现状 V3 基线
class LegacyV3PolicyTests(unittest.TestCase):
    def test_reads_body_then_three_fixed_frames(self):
        view = page_view(scroll_height=3000)
        policy = LegacyV3Policy()
        g = goal()
        state = policy_state(g, view)

        actions = run_until_stop(policy, g, view, state)

        extracts = [action for action in actions if action.kind != 'stop']
        self.assertEqual([(a.region_id, a.method) for a in extracts],
                         [('dom:body', 'dom'), ('viewport:0', 'ocr'),
                          ('viewport:1', 'ocr'), ('viewport:2', 'ocr')])
        self.assertEqual(actions[-1].kind, 'stop')
        self.assertEqual(actions[-1].reason, 'legacy_v3_exhausted')
        # dom(1) + 三屏 ocr(3*3) = 10 units。
        self.assertEqual(state.spent, model.Cost(units=10, captures=3, ocr_calls=3))

    def test_no_early_stop_when_body_contains_answer(self):
        view = page_view(scroll_height=3000, dom_text='活动截止 2026年11月20日')
        policy = LegacyV3Policy()
        g = goal()
        state = policy_state(g, view)

        def respond(action):
            text = '活动截止 2026年11月20日' if action.region_id == model.DOM_BODY_REGION else ''
            return make_observation(action, view, text=text,
                                    role='activity_rules' if text else None)

        actions = run_until_stop(policy, g, view, state, respond=respond)
        # dom 已拿到命中，但现状基线仍固定跑完三屏 OCR。
        self.assertEqual([a.region_id for a in actions if a.kind != 'stop'],
                         ['dom:body', 'viewport:0', 'viewport:1', 'viewport:2'])
        self.assertTrue(state.candidates.get('deadline'))
        self.assertEqual(actions[-1].reason, 'legacy_v3_exhausted')


# ----------------------------------------------------------------- 自适应策略
class AdaptiveObservationPolicyTests(unittest.TestCase):
    def test_blocked_page_stops_immediately_without_actions(self):
        view = page_view(region(), blocked_reason='captcha')
        policy = AdaptiveObservationPolicy()
        g = goal()
        state = policy_state(g, view)
        action = policy.next_action(g, view, state)
        self.assertEqual(action.kind, 'stop')
        self.assertEqual(action.reason, 'blocked:captcha')
        self.assertEqual(state.steps, 0)
        self.assertEqual(state.spent, model.Cost())

    def test_dom_readable_region_extracted_then_objectives_covered(self):
        rules = region(text='活动时间：2026年11月20日 23:59')
        view = page_view(rules)
        policy = AdaptiveObservationPolicy()
        g = goal()
        state = policy_state(g, view)

        first = policy.next_action(g, view, state)
        self.assertEqual((first.kind, first.region_id, first.method), ('extract', 'rules', 'dom'))
        self.assertEqual(first.cost, model.Cost(units=1))  # DOM 比 OCR 便宜，优先 dom
        state.record(first, make_observation(first, view, text=rules.text, role='activity_rules'))
        self.assertEqual(state.spent, model.Cost(units=1))
        self.assertTrue(state.complete())

        second = policy.next_action(g, view, state)
        self.assertEqual((second.kind, second.reason), ('stop', 'objectives_covered'))

    def test_lazy_region_interacts_once_before_extract(self):
        lazy = region(lazy=True)  # 未展开时 text/visual_text 均不可读
        view = page_view(lazy, page_version='sha256:page_v1')
        # min_gain_rounds=1：如果错误地把 interact 算成「无新增候选」，这里会提前停止。
        policy = AdaptiveObservationPolicy(min_gain_rounds=1)
        g = goal()
        state = policy_state(g, view)

        first = policy.next_action(g, view, state)
        self.assertEqual((first.kind, first.region_id, first.method), ('interact', 'rules', None))

        expanded = region(text='活动截止 2026年11月20日')
        new_view = page_view(expanded, page_version='sha256:page_v2')
        state.record(first, model.Observation(action='interact', page_version='sha256:page_v2',
                                              region_id='rules', view=new_view))
        self.assertEqual(state.interacted, {'rules'})

        second = policy.next_action(g, new_view, state)
        self.assertEqual((second.kind, second.region_id, second.method), ('extract', 'rules', 'dom'))

        state.record(second, make_observation(second, new_view, text=expanded.text,
                                              role='activity_rules'))
        third = policy.next_action(g, new_view, state)
        self.assertEqual((third.kind, third.reason), ('stop', 'objectives_covered'))

    def test_visual_only_region_uses_ocr(self):
        poster = region('poster', 'poster', visual_text='活动截止：2026年11月20日')
        view = page_view(poster)
        g = goal(objective(region_hints=('poster',)))
        policy = AdaptiveObservationPolicy()
        state = policy_state(g, view)

        action = policy.next_action(g, view, state)
        self.assertEqual((action.kind, action.region_id, action.method), ('extract', 'poster', 'ocr'))
        self.assertEqual(action.cost, model.Cost(units=3, captures=1, ocr_calls=1))

    def test_visual_only_flag_without_transcript_still_offers_ocr(self):
        # 真实浏览器只告诉我们「这个区域只有图」，提取前没有任何可读文字；
        # visual_only 标记必须足以触发 OCR，而不是要求先有伪装的 visual_text。
        poster = region('poster', 'poster', visual_only=True)
        view = page_view(poster)
        g = goal(objective(region_hints=('poster',)))
        policy = AdaptiveObservationPolicy()
        state = policy_state(g, view)
        action = policy.next_action(g, view, state)
        self.assertEqual((action.kind, action.region_id, action.method), ('extract', 'poster', 'ocr'))
        # 没有视觉标记的普通空区域不应被误判为需要 OCR。
        blank = region('blank', 'section')
        blank_view = page_view(blank)
        blank_goal = goal(objective(region_hints=('section',)))
        blank_state = policy_state(blank_goal, blank_view)
        self.assertNotEqual(AdaptiveObservationPolicy().next_action(
            blank_goal, blank_view, blank_state).method, 'ocr')

    def test_default_policy_never_requests_vlm(self):
        poster = region('poster', 'poster', visual_text='活动截止：2026年11月20日')
        view = page_view(poster)
        g = goal(objective(region_hints=('poster',)))
        policy = AdaptiveObservationPolicy()
        state = policy_state(g, view)
        state.attempted['extract:poster:ocr'] = 1  # OCR 已试过也不会自动升级到 vlm
        action = policy.next_action(g, view, state)
        self.assertEqual(action.kind, 'stop')
        self.assertEqual(action.reason, 'no_new_evidence')

    def test_allow_vlm_only_used_as_visual_fallback_after_ocr_attempt(self):
        poster = region('poster', 'poster', visual_text='活动截止：2026年11月20日')
        view = page_view(poster)
        g = goal(objective(region_hints=('poster',)))
        budgets = model.Budget(max_units=8, max_captures=2, max_ocr_calls=1, max_vlm_calls=1)
        state = policy_state(g, view, budgets=budgets,
                             spent=model.Cost(units=3, captures=1, ocr_calls=1),
                             attempted={'extract:poster:ocr': 1})
        policy = AdaptiveObservationPolicy(allow_vlm=True, min_gain_rounds=3)

        action = policy.next_action(g, view, state)
        self.assertEqual((action.kind, action.method), ('extract', 'vlm'))

    def test_skill_hint_reorders_candidate_regions(self):
        article = region('article', 'article_body', text='这是一段足够长的活动正文说明')
        comments = region('comments', 'comments', text='评论区还有一段足够长的讨论内容')
        view = page_view(article, comments)
        g = goal(objective(objective_id='summary', question='确认活动说明',
                           answer_kind='text', region_hints=()))

        plain_policy = AdaptiveObservationPolicy()
        plain_state = policy_state(g, view)
        plain = plain_policy.next_action(g, view, plain_state)
        self.assertEqual(plain.region_id, 'article')  # 无 hint 时按文档顺序

        hinting_policy = AdaptiveObservationPolicy(
            hint_provider=StubHintProvider([model.RegionHint('comments', 0.9, 'skill:test')]))
        hinting_state = policy_state(g, view)
        hinted = hinting_policy.next_action(g, view, hinting_state)
        self.assertEqual(hinted.region_id, 'comments')  # 合法 Skill hint 优先

    def test_higher_scored_hint_goes_first(self):
        left = region('left', 'article_body', text='左侧区域有一段足够长的正文内容')
        right = region('right', 'article_body', text='右侧区域也有一段足够长的正文内容')
        view = page_view(left, right)
        g = goal(objective(objective_id='summary', question='确认活动说明',
                           answer_kind='text', region_hints=()))
        policy = AdaptiveObservationPolicy(hint_provider=StubHintProvider([
            model.RegionHint('left', 0.2, 'skill:test'),
            model.RegionHint('right', 0.8, 'skill:test'),
        ]))
        state = policy_state(g, view)
        action = policy.next_action(g, view, state)
        self.assertEqual(action.region_id, 'right')

    def test_hint_for_unknown_region_is_ignored(self):
        article = region('article', 'article_body', text='这是一段足够长的活动正文说明')
        view = page_view(article)
        g = goal(objective(objective_id='summary', question='确认活动说明',
                           answer_kind='text', region_hints=()))
        provider = CountingHintProvider([model.RegionHint('ghost', 1.0, 'skill:test')])
        policy = AdaptiveObservationPolicy(hint_provider=provider)
        state = policy_state(g, view)
        action = policy.next_action(g, view, state)
        # provider 被调用过，但它指向不存在的区域，不能影响候选顺序。
        self.assertGreaterEqual(provider.calls, 1)
        self.assertEqual(action.region_id, 'article')

    def test_hint_does_not_override_objective_region_filter(self):
        rules = region('rules', text='活动截止 2026年11月20日')
        footer = region('footer', 'footer', text='页脚版权信息')
        view = page_view(rules, footer)
        g = goal(objective(region_hints=('activity_rules',)))
        policy = AdaptiveObservationPolicy(
            hint_provider=StubHintProvider([model.RegionHint('footer', 1.0, 'skill:test')]))
        state = policy_state(g, view)
        action = policy.next_action(g, view, state)
        # footer 不满足 objective 的区域先验，即使被 hint 也不能成为候选。
        self.assertEqual(action.region_id, 'rules')

    def test_stops_after_consecutive_no_gain_rounds(self):
        first_region = region('rules', text='与截止日期无关的普通说明文字')
        second_region = region('rules2', text='仍然与截止日期无关的说明文字')
        view = page_view(first_region, second_region)
        g = goal()
        policy = AdaptiveObservationPolicy(min_gain_rounds=2)
        state = policy_state(g, view)

        first = policy.next_action(g, view, state)
        self.assertEqual(first.region_id, 'rules')
        state.record(first, make_observation(first, view, text=first_region.text,
                                            role='activity_rules'))

        second = policy.next_action(g, view, state)
        self.assertEqual(second.region_id, 'rules2')  # 第 1 步无增益还不够停
        state.record(second, make_observation(second, view, text=second_region.text,
                                             role='activity_rules'))

        third = policy.next_action(g, view, state)
        self.assertEqual((third.kind, third.reason), ('stop', 'no_new_evidence'))
        self.assertFalse(state.complete())

    def test_min_gain_rounds_controls_stop_timing(self):
        rules = region('rules', text='与截止日期无关的普通说明文字')
        follow_up = region('rules2', text='仍然与截止日期无关的说明文字')
        view = page_view(rules, follow_up)
        g = goal()
        policy = AdaptiveObservationPolicy(min_gain_rounds=1)
        state = policy_state(g, view)

        first = policy.next_action(g, view, state)
        state.record(first, make_observation(first, view, text=rules.text, role='activity_rules'))
        second = policy.next_action(g, view, state)
        self.assertEqual((second.kind, second.reason), ('stop', 'no_new_evidence'))

    def test_same_action_not_repeated_after_attempt_exhausted(self):
        rules = region(text='活动截止 2026年11月20日')
        view = page_view(rules)
        g = goal()
        policy = AdaptiveObservationPolicy()
        state = policy_state(g, view)
        state.attempted['extract:rules:dom'] = 1  # 已尝试过一次（默认最多 1 次）

        action = policy.next_action(g, view, state)
        self.assertEqual(action.kind, 'stop')
        self.assertEqual(action.reason, 'no_new_evidence')

    def test_max_attempts_per_action_enables_configured_retry(self):
        rules = region(text='活动截止 2026年11月20日')
        view = page_view(rules)
        g = goal()
        policy = AdaptiveObservationPolicy(max_attempts_per_action=2)
        state = policy_state(g, view)
        state.attempted['extract:rules:dom'] = 1

        action = policy.next_action(g, view, state)
        self.assertEqual((action.kind, action.region_id, action.method), ('extract', 'rules', 'dom'))

    def test_login_wall_stops_with_login_required(self):
        comments = region('comments', 'comments', requires='login:需要登录后查看评论')
        view = page_view(comments)
        g = goal(objective(objective_id='feedback', question='确认评论反馈',
                           answer_kind='text', region_hints=('comments',)))
        policy = AdaptiveObservationPolicy()
        state = policy_state(g, view)

        action = policy.next_action(g, view, state)
        self.assertEqual((action.kind, action.reason), ('stop', 'login_required'))
        self.assertEqual(state.steps, 0)

    def test_budget_exhausted_when_visual_action_unaffordable(self):
        poster = region('poster', 'poster', visual_text='活动截止：2026年11月20日')
        view = page_view(poster)
        g = goal(objective(region_hints=('poster',)))
        budgets = model.Budget(max_units=2, max_captures=1, max_ocr_calls=1)
        policy = AdaptiveObservationPolicy()
        state = policy_state(g, view, budgets=budgets)

        action = policy.next_action(g, view, state)
        self.assertEqual((action.kind, action.reason), ('stop', 'budget_exhausted'))

    def test_budget_exhausted_when_step_limit_reached(self):
        rules = region(text='活动截止 2026年11月20日')
        view = page_view(rules)
        g = goal()
        budgets = model.Budget(max_steps=1)
        policy = AdaptiveObservationPolicy()
        state = policy_state(g, view, budgets=budgets, steps=1)
        action = policy.next_action(g, view, state)
        self.assertEqual((action.kind, action.reason), ('stop', 'budget_exhausted'))

    def test_optional_objective_does_not_block_coverage_stop(self):
        required = objective(objective_id='deadline', required=True)
        optional = objective(objective_id='bonus', question='可选的补充信息',
                             answer_kind='text', region_hints=('comments',), required=False)
        view = page_view(region(text='活动截止 2026年11月20日'))
        g = goal(required, optional)
        policy = AdaptiveObservationPolicy()
        state = policy_state(g, view, candidates={'deadline': ['capture_old']})
        action = policy.next_action(g, view, state)
        self.assertEqual((action.kind, action.reason), ('stop', 'objectives_covered'))

    def test_no_candidate_regions_stops_without_wasting_actions(self):
        footer = region('footer', 'footer', text='页脚版权信息')
        view = page_view(footer)
        g = goal()  # 期望 activity_rules 区域，但页面上没有
        policy = AdaptiveObservationPolicy()
        state = policy_state(g, view)
        action = policy.next_action(g, view, state)
        self.assertEqual((action.kind, action.reason), ('stop', 'no_new_evidence'))
        self.assertEqual(state.steps, 0)

    def test_falls_back_to_dom_body_for_objective_without_region_hints(self):
        footer = region('footer', 'footer')  # 语义区域没有可读内容
        view = page_view(footer, dom_text='活动安排：2026年11月20日 开启')
        g = goal(objective(region_hints=()))
        policy = AdaptiveObservationPolicy()
        state = policy_state(g, view)
        action = policy.next_action(g, view, state)
        self.assertEqual((action.kind, action.region_id, action.method),
                         ('extract', 'dom:body', 'dom'))

    def test_policy_is_read_only_on_state(self):
        rules = region(text='活动截止 2026年11月20日')
        view = page_view(rules)
        g = goal()
        policy = AdaptiveObservationPolicy()
        state = policy_state(g, view)
        before = (state.steps, dict(state.candidates), dict(state.attempted),
                  state.spent, set(state.interacted), state.page_version)
        policy.next_action(g, view, state)
        after = (state.steps, dict(state.candidates), dict(state.attempted),
                 state.spent, set(state.interacted), state.page_version)
        self.assertEqual(before, after)

    def test_constructor_rejects_invalid_limits(self):
        with self.assertRaises(ValueError):
            AdaptiveObservationPolicy(max_attempts_per_action=0)
        with self.assertRaises(ValueError):
            AdaptiveObservationPolicy(min_gain_rounds=0)


if __name__ == '__main__':
    unittest.main()
