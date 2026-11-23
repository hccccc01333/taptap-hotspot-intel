"""工具路由的机制测试。

测试只依赖冻结契约 ``acquisition.model`` 与 router 自身：自行构造 PageView 与
纯净的 EvidenceGap，验证「缺口 → 后端」对照表、优先级、登录/阻塞停止、预算拒绝、
被拒后端记录，以及 validate_action 对禁用后端/未知区域的拒绝。不依赖 driver/store。
"""
import unittest

from acquisition import model
from acquisition.router import AcquisitionRouter, EvidenceGap, RouteDecision


# --------------------------------------------------------------------- 构造器
def region(region_id='rules', role='activity_rules', bbox=(0, 100, 800, 400), **kwargs):
    return model.Region(region_id=region_id, role=role, bbox=bbox, **kwargs)


def page_view(*regions, scroll_height=3000, viewport=(1440, 1000), dom_text='',
              blocked_reason=None):
    return model.PageView(page_version='sha256:page_v1', url='https://example.test/activity',
                          title='合成样本页', viewport=viewport, scroll_height=scroll_height,
                          regions=tuple(regions), dom_text=dom_text, blocked_reason=blocked_reason)


def gap(objective_id='deadline', reason='dom_text_missing', region_id=None):
    return EvidenceGap(objective_id=objective_id, reason=reason, region_id=region_id)


def action(kind, region_id=None, method=None):
    return model.Action(kind=kind, region_id=region_id, method=method)


# --------------------------------------------------------------- 路由对照表
class RouteTableTests(unittest.TestCase):
    def test_default_gap_to_backend_table(self):
        text_region = region('rules', text='活动截止 2026年11月20日')
        visual_region = region('poster', 'poster', visual_text='活动截止 2026年11月20日')
        lazy_region = region('rules', lazy=True)
        cases = [
            # (缺口, 页面, 期望 backend, 期望 method, 期望 allowed)
            (gap(reason='dom_text_missing', region_id='rules'), page_view(text_region), 'dom', 'dom', True),
            (gap(reason='visual_only_region', region_id='poster'), page_view(visual_region), 'ocr', 'ocr', True),
            (gap(reason='lazy_region', region_id='rules'), page_view(lazy_region), 'playwright', None, True),
            (gap(reason='skill_available', region_id='rules'), page_view(text_region), 'dom', 'dom', True),
            (gap(reason='login_required', region_id='comments'),
             page_view(region('comments', 'comments', requires='login')), None, None, False),
            (gap(reason='blocked_page'), page_view(text_region), None, None, False),
        ]
        router = AcquisitionRouter()
        for evidence_gap, view, backend, method, allowed in cases:
            with self.subTest(reason=evidence_gap.reason):
                decision = router.route(evidence_gap, view)
                self.assertIsInstance(decision, RouteDecision)
                self.assertEqual((decision.backend, decision.method, decision.allowed),
                                 (backend, method, allowed))
                self.assertTrue(decision.reason)
                for rejected in decision.rejected:
                    self.assertNotEqual(rejected, backend)

    def test_visual_gap_uses_ocr_and_records_capability_refusals(self):
        view = page_view(region('poster', 'poster', visual_text='截止 2026年11月20日'))
        decision = AcquisitionRouter().route(gap(reason='visual_only_region', region_id='poster'), view)
        self.assertEqual((decision.backend, decision.method), ('ocr', 'ocr'))
        self.assertTrue(decision.reason)
        # ocr 之后优先级更低的 vlm 与 tabbit 默认关闭，属于被打回的能力。
        self.assertIn('vlm', decision.rejected)
        self.assertIn('tabbit', decision.rejected)
        self.assertNotIn('ocr', decision.rejected)

    def test_dom_gap_on_visual_region_falls_back_to_ocr(self):
        view = page_view(region('poster', 'poster', visual_text='截止 2026年11月20日'))
        decision = AcquisitionRouter().route(gap(reason='dom_text_missing', region_id='poster'), view)
        self.assertEqual((decision.backend, decision.method), ('ocr', 'ocr'))
        self.assertIn('dom', decision.rejected)
        self.assertIn('DOM', decision.reason)

    def test_skill_gap_uses_skill_only_when_registry_grants_it(self):
        text_region = region('rules', text='活动截止 2026年11月20日')
        visual_region = region('poster', 'poster', visual_text='截止 2026年11月20日')
        granted = AcquisitionRouter(allowed_backends=('dom', 'playwright', 'ocr', 'skill'))

        # Skill 通道优先于 ocr（skill=2 < ocr=3），即使目标只有视觉文字。
        decision = granted.route(gap(reason='skill_available', region_id='poster'), page_view(visual_region))
        self.assertEqual((decision.backend, decision.method), ('skill', 'skill'))

        # 默认路由未由注册表授予 Skill，必须把 skill 写进 rejected 并回退普通通道。
        fallback = AcquisitionRouter().route(gap(reason='skill_available', region_id='poster'),
                                             page_view(visual_region))
        self.assertIn('skill', fallback.rejected)
        self.assertEqual((fallback.backend, fallback.method), ('ocr', 'ocr'))
        dom_fallback = AcquisitionRouter().route(gap(reason='skill_available', region_id='rules'),
                                                 page_view(text_region))
        self.assertEqual((dom_fallback.backend, dom_fallback.method), ('dom', 'dom'))
        self.assertIn('skill', dom_fallback.rejected)

    def test_lazy_gap_requires_interaction_backend(self):
        lazy = region('rules', lazy=True)
        view = page_view(lazy)
        decision = AcquisitionRouter().route(gap(reason='lazy_region', region_id='rules'), view)
        self.assertEqual((decision.backend, decision.method), ('playwright', None))

        no_browser = AcquisitionRouter(allowed_backends=('dom', 'ocr'))
        refused = no_browser.route(gap(reason='lazy_region', region_id='rules'), view)
        self.assertFalse(refused.allowed)
        self.assertIsNone(refused.backend)
        self.assertIn('playwright', refused.rejected)

        # 已展开的懒加载区域有 DOM 文本，直接走 dom。
        expanded = page_view(region('rules', lazy=True, text='活动截止 2026年11月20日'))
        opened = AcquisitionRouter().route(gap(reason='lazy_region', region_id='rules'), expanded)
        self.assertEqual((opened.backend, opened.method), ('dom', 'dom'))

    def test_login_gap_stops_unless_authorized(self):
        view = page_view(region('comments', 'comments', requires='login:需要登录'))
        refused = AcquisitionRouter().route(gap(reason='login_required', region_id='comments'), view)
        self.assertFalse(refused.allowed)
        self.assertIsNone(refused.backend)
        self.assertIn('登录', refused.reason)
        self.assertEqual(refused.rejected, ('dom', 'skill', 'ocr', 'vlm', 'tabbit'))

        authorized = AcquisitionRouter(authorized_login=True)
        allowed = authorized.route(gap(reason='login_required', region_id='comments'), view)
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.backend, 'playwright')

        # 缺口不是 login_required，但目标区域本身需要登录，同样拒绝未授权提取。
        region_login = authorized.route(gap(reason='dom_text_missing', region_id='comments'), view)
        self.assertTrue(region_login.allowed)  # 已授权时可读登录后区域
        plain_login = AcquisitionRouter().route(gap(reason='dom_text_missing', region_id='comments'), view)
        self.assertFalse(plain_login.allowed)
        self.assertIn('登录', plain_login.reason)

    def test_blocked_page_stops_for_every_gap_shape(self):
        blocked = page_view(region('rules', text='正文'), blocked_reason='captcha')
        router = AcquisitionRouter()
        for reason in ('dom_text_missing', 'visual_only_region', 'lazy_region', 'skill_available'):
            with self.subTest(reason=reason):
                decision = router.route(gap(reason=reason, region_id='rules'), blocked)
                self.assertFalse(decision.allowed)
                self.assertIsNone(decision.backend)
                self.assertEqual(decision.rejected, ('dom', 'skill', 'ocr', 'vlm', 'tabbit'))
                self.assertIn('captcha', decision.reason)
        # 即使页面上没有阻塞标记，缺口被判定为 blocked_page 也必须停止。
        clean = router.route(gap(reason='blocked_page'), page_view(region('rules')))
        self.assertFalse(clean.allowed)
        self.assertIn('blocked_page', clean.reason)

    def test_route_budget_denies_unaffordable_capture(self):
        view = page_view(region('poster', 'poster', visual_text='截止 2026年11月20日'))
        budgets = model.Budget(max_units=2, max_captures=1, max_ocr_calls=1)
        decision = AcquisitionRouter().route(gap(reason='visual_only_region', region_id='poster'),
                                             view, budgets)
        self.assertFalse(decision.allowed)
        self.assertIsNone(decision.backend)
        self.assertIn('ocr', decision.rejected)
        self.assertIn('预算', decision.reason)

        dom_budget = model.Budget(max_units=0)
        dom_denied = AcquisitionRouter().route(gap(reason='dom_text_missing', region_id='rules'),
                                               page_view(region('rules', text='正文')), dom_budget)
        self.assertFalse(dom_denied.allowed)
        self.assertIn('dom', dom_denied.rejected)


# ------------------------------------------------------------------ 优先级
class BackendPriorityTests(unittest.TestCase):
    def test_ocr_outranks_vlm_even_when_vlm_enabled(self):
        view = page_view(region('poster', 'poster', visual_text='截止 2026年11月20日'))
        router = AcquisitionRouter(vlm_enabled=True)
        decision = router.route(gap(reason='visual_only_region', region_id='poster'), view)
        self.assertEqual((decision.backend, decision.method), ('ocr', 'ocr'))
        self.assertNotIn('vlm', decision.rejected)  # vlm 可用，只是优先级更低

    def test_vlm_used_when_ocr_not_authorized(self):
        view = page_view(region('poster', 'poster', visual_text='截止 2026年11月20日'))
        router = AcquisitionRouter(allowed_backends=('dom', 'playwright'), vlm_enabled=True)
        decision = router.route(gap(reason='visual_only_region', region_id='poster'), view)
        self.assertEqual((decision.backend, decision.method), ('vlm', 'vlm'))
        self.assertIn('ocr', decision.rejected)

    def test_visual_chain_refused_when_nothing_enabled(self):
        view = page_view(region('poster', 'poster', visual_text='截止 2026年11月20日'))
        router = AcquisitionRouter(allowed_backends=())
        decision = router.route(gap(reason='visual_only_region', region_id='poster'), view)
        self.assertFalse(decision.allowed)
        self.assertIsNone(decision.backend)
        self.assertEqual(decision.rejected, ('ocr', 'vlm', 'tabbit'))
        self.assertIn('未启用', decision.reason)

    def test_tabbit_is_last_resort_and_disabled_by_default(self):
        view = page_view(region('poster', 'poster', visual_text='截止 2026年11月20日'))
        default_router = AcquisitionRouter()
        decision = default_router.route(gap(reason='visual_only_region', region_id='poster'), view)
        self.assertNotEqual(decision.backend, 'tabbit')
        self.assertIn('tabbit', decision.rejected)

        tabbit_router = AcquisitionRouter(allowed_backends=(), tabbit_enabled=True)
        tabbit = tabbit_router.route(gap(reason='visual_only_region', region_id='poster'), view)
        self.assertTrue(tabbit.allowed)
        self.assertEqual((tabbit.backend, tabbit.method), ('tabbit', 'http'))
        self.assertIn('ocr', tabbit.rejected)

    def test_default_router_never_selects_vlm_or_tabbit(self):
        visual = region('poster', 'poster', visual_text='截止 2026年11月20日')
        text = region('rules', text='活动截止 2026年11月20日')
        lazy = region('lazy', lazy=True)
        views = {
            'dom_text_missing': page_view(text),
            'visual_only_region': page_view(visual),
            'lazy_region': page_view(lazy),
            'skill_available': page_view(text),
            'login_required': page_view(region('comments', 'comments', requires='login')),
            'blocked_page': page_view(text),
        }
        router = AcquisitionRouter()
        for reason, view in views.items():
            with self.subTest(reason=reason):
                decision = router.route(gap(reason=reason, region_id=view.regions[0].region_id), view)
                self.assertNotIn(decision.backend, ('vlm', 'tabbit'))


# ---------------------------------------------------------- 动作合法性复核
class ValidateActionTests(unittest.TestCase):
    def test_allows_regular_dom_and_ocr_actions(self):
        view = page_view(region('rules', text='活动截止 2026年11月20日'),
                         region('poster', 'poster', visual_text='截止 2026年11月20日'))
        router = AcquisitionRouter()
        allowed, reason = router.validate_action(action('extract', 'rules', 'dom'), view)
        self.assertTrue(allowed)
        self.assertIn('dom', reason)
        allowed, reason = router.validate_action(action('extract', 'poster', 'ocr'), view)
        self.assertTrue(allowed)
        self.assertIn('ocr', reason)
        allowed, _ = router.validate_action(action('extract', model.DOM_BODY_REGION, 'dom'), view)
        self.assertTrue(allowed)
        allowed, _ = router.validate_action(action('extract', 'viewport:0', 'ocr'), view)
        self.assertTrue(allowed)
        allowed, _ = router.validate_action(action('inspect'), view)
        self.assertTrue(allowed)
        allowed, _ = router.validate_action(model.Action('stop', reason='objectives_covered'), view)
        self.assertTrue(allowed)

    def test_rejects_disabled_backends_with_reason(self):
        view = page_view(region('poster', 'poster', visual_text='截止 2026年11月20日'))
        router = AcquisitionRouter()
        for method, expected in (('vlm', 'vlm'), ('skill', 'skill'), ('http', 'http')):
            with self.subTest(method=method):
                allowed, reason = router.validate_action(action('extract', 'poster', method), view)
                self.assertFalse(allowed)
                self.assertIn(expected, reason)

        vlm_router = AcquisitionRouter(vlm_enabled=True)
        allowed, _ = vlm_router.validate_action(action('extract', 'poster', 'vlm'), view)
        self.assertTrue(allowed)
        skill_router = AcquisitionRouter(allowed_backends=('dom', 'playwright', 'ocr', 'skill'))
        allowed, _ = skill_router.validate_action(action('extract', 'poster', 'ocr'), view)
        self.assertTrue(allowed)

    def test_rejects_unknown_region_and_out_of_range_viewport(self):
        view = page_view(region('rules', text='正文'), scroll_height=1200)  # 固定帧只有 2 屏
        router = AcquisitionRouter()
        cases = [
            (action('extract', 'ghost', 'dom'), '未知区域'),
            (action('extract', 'viewport:2', 'ocr'), '固定帧'),
            (action('extract', 'viewport:x', 'ocr'), '非法'),
            (action('interact', model.DOM_BODY_REGION), '虚拟区域'),
            (action('interact', 'rules'), None),  # 语义区域交互合法
        ]
        for candidate, token in cases:
            with self.subTest(action=candidate.kind, region=candidate.region_id):
                allowed, reason = router.validate_action(candidate, view)
                if token is None:
                    self.assertTrue(allowed)
                else:
                    self.assertFalse(allowed)
                    self.assertIn(token, reason)

    def test_rejects_extract_of_unexpanded_lazy_region(self):
        view = page_view(region('rules', lazy=True))
        router = AcquisitionRouter()
        allowed, reason = router.validate_action(action('extract', 'rules', 'dom'), view)
        self.assertFalse(allowed)
        self.assertIn('懒加载', reason)
        # 交互后页面新版本里区域文本可读，提取放行。
        expanded = page_view(region('rules', lazy=True, text='活动截止 2026年11月20日'))
        allowed, _ = router.validate_action(action('extract', 'rules', 'dom'), expanded)
        self.assertTrue(allowed)

    def test_blocked_page_stops_at_route_but_still_lets_harness_record_cost(self):
        blocked = page_view(region('rules', text='正文'), blocked_reason='captcha')

        # 1) 路由规划阶段：阻塞页必须停止，不得继续选择提取后端。
        decision = AcquisitionRouter().route(gap(reason='dom_text_missing', region_id='rules'), blocked)
        self.assertFalse(decision.allowed)
        self.assertIsNone(decision.backend)
        self.assertIn('captcha', decision.reason)

        # 2) 动作复核阶段：阻塞页的证据拦截由 harness 执行后统一处理（§2.3）。
        #    这里不能提前拒绝，否则固定基线无法「执行后记为 blocked 并停止」，
        #    阻塞页上的浪费成本也就无法如实入账。
        allowed, _ = AcquisitionRouter().validate_action(action('extract', 'rules', 'dom'), blocked)
        self.assertTrue(allowed)

        view = page_view(region('poster', 'poster', visual_text='截止 2026年11月20日'))
        budgets = model.Budget(max_units=1, max_captures=1, max_ocr_calls=1)
        allowed, reason = AcquisitionRouter().validate_action(action('extract', 'poster', 'ocr'),
                                                              view, budgets)
        self.assertFalse(allowed)
        self.assertIn('预算', reason)

    def test_rejects_login_gated_region_unless_authorized(self):
        view = page_view(region('comments', 'comments', requires='login:需要登录'))
        allowed, reason = AcquisitionRouter().validate_action(
            action('extract', 'comments', 'dom'), view)
        self.assertFalse(allowed)
        self.assertIn('登录', reason)
        allowed, _ = AcquisitionRouter(authorized_login=True).validate_action(
            action('extract', 'comments', 'dom'), view)
        self.assertTrue(allowed)

    def test_interact_requires_playwright_backend(self):
        view = page_view(region('rules', lazy=True))
        allowed, reason = AcquisitionRouter(allowed_backends=('dom', 'ocr')).validate_action(
            action('interact', 'rules'), view)
        self.assertFalse(allowed)
        self.assertIn('playwright', reason)


# ------------------------------------------------------------- 数据类不变量
class ContractShapeTests(unittest.TestCase):
    def test_gap_reason_is_validated(self):
        self.assertEqual(EvidenceGap('deadline', 'dom_text_missing').region_id, None)
        with self.assertRaises(ValueError):
            EvidenceGap('deadline', 'dom_missing')
        with self.assertRaises(ValueError):
            EvidenceGap('', 'dom_text_missing')

    def test_route_decision_invariants(self):
        with self.assertRaises(ValueError):
            RouteDecision(None, None, True, '允许却没有后端')
        with self.assertRaises(ValueError):
            RouteDecision('dom', 'dom', False, '拒绝却指定了后端')
        with self.assertRaises(ValueError):
            RouteDecision('dom', 'dom', True, '')
        decision = RouteDecision('dom', 'dom', True, '测试')
        self.assertEqual(decision.rejected, ())
        self.assertFalse(decision.stopped)


if __name__ == '__main__':
    unittest.main()
