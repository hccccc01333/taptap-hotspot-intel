"""acquisition.skills 的单元测试与门禁/完成验证检查。

测试原则：断言的是机制（门禁是否生效、先验是否只在合法时产出、完成验证
是否只描述流程而不改写证据），不是把实现输出和实现自身比较。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from acquisition import model
from acquisition.driver import FixtureDriver
from acquisition.harness import AcquisitionHarness
from acquisition.model import (
    Cost,
    Evidence,
    Objective,
    PageView,
    Region,
    ResearchGoal,
    TaskTrace,
)
from acquisition.policy import AdaptiveObservationPolicy
from acquisition.skills import (
    SKILL_VERIFY_SCHEMA,
    VERIFY_KINDS,
    SkillHintProvider,
    SkillManifest,
    SkillRegistry,
    SkillVerification,
)
from acquisition.store import AcquisitionStore

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
PAGES = FIXTURES / 'pages'
SKILLS = FIXTURES / 'skills'
SKILL_ID = 'taptap-activity-rules'
TAIL_HOST = 'www.taptap.cn'


# ---------------------------------------------------------------------------
# 构造辅助：直接从冻结样本构造 PageView / ResearchGoal / TaskTrace。
# 这里不复制驱动逻辑，只是把样本字段映射到冻结契约，供 Skill 单元测试使用；
# 真正的驱动/Skill 集成测试在文件末尾用 FixtureDriver + AcquisitionHarness 跑。
# ---------------------------------------------------------------------------
def load_page(page_id: str) -> dict:
    return json.loads((PAGES / f'{page_id}.json').read_text(encoding='utf-8'))


def build_page_view(page: dict, *, blocked_reason: str | None = None,
                    page_version: str | None = None) -> PageView:
    regions = tuple(
        Region(
            region_id=raw['region_id'],
            role=raw['role'],
            bbox=tuple(raw['bbox']),
            text=raw['text'],
            visual_text=raw['visual_text'],
            lazy=raw['lazy'],
            requires=raw['requires'],
            blocked=raw['blocked'],
            scroll_y=raw['scroll_y'],
        )
        for raw in page['regions']
    )
    return PageView(
        page_version=page_version or model.derive_page_version(page),
        url=page['url'],
        title=page['title'],
        viewport=tuple(page['viewport']),
        scroll_height=page['scroll_height'],
        regions=regions,
        dom_text=page['dom_text'],
        blocked_reason=page['blocked_reason'] if blocked_reason is None else blocked_reason,
    )


def build_goal(page: dict) -> ResearchGoal:
    objectives = tuple(
        Objective(
            objective_id=raw['objective_id'],
            question=raw['question'],
            answer_kind=raw['answer_kind'],
            region_hints=tuple(raw['region_hints']),
            required=raw['required'],
        )
        for raw in page['objectives']
    )
    return ResearchGoal(
        task_id=f"skill-test::{page['page_id']}",
        question=page['title'],
        objectives=objectives,
        allowed_hosts=('www.taptap.cn',),
    )


def build_trace(view: PageView, evidence: tuple[Evidence, ...]) -> TaskTrace:
    return TaskTrace(
        task_id='skill-test::verify',
        question='确认合成活动的报名截止日期',
        backend='playwright',
        policy='adaptive',
        status='succeeded',
        stop_reason='objectives_covered',
        page_versions=(view.page_version,),
        receipts=(),
        evidence=evidence,
        cost=Cost(units=3, captures=0, ocr_calls=0, vlm_calls=0),
        steps=2,
    )


def build_evidence(view: PageView, *,
                   text: str = '活动规则（合成，v1 布局）：\n报名截止：2026年11月25日 23:59。\n参与方式：完成每日任务后领取奖励。',
                   page_version: str | None = None,
                   region: str = 'rules_v1',
                   extraction: str = 'ocr') -> Evidence:
    return Evidence(
        capture_id='capture_skill_test_1',
        page_version=page_version or view.page_version,
        region=region,
        extraction=extraction,
        text=text,
        source_url=view.url,
    )


class SkillFixtureConsistencyTest(unittest.TestCase):
    """样例 Skill 与冻结样本的一致性：签名必须由冻结页面角色序列算出。"""

    def test_manifest_signature_matches_layout_pages(self) -> None:
        registry = SkillRegistry.load(SKILLS)
        manifest = registry.manifests()[0]
        computed = {
            model.structural_signature(self._regions(page))
            for page in (load_page('layout_changed_v1'), load_page('layout_changed_v2'))
        }
        # 冻结契约要求：v1/v2 角色序列相同，签名必须与 manifest 完全一致。
        self.assertEqual(len(computed), 1)
        self.assertEqual(set(manifest.structural_signatures), computed)

    def test_manifest_declares_expected_gates(self) -> None:
        manifest = SkillRegistry.load(SKILLS).manifests()[0]
        self.assertEqual(manifest.skill_id, SKILL_ID)
        self.assertEqual(manifest.version, '0.1.0')
        self.assertEqual(manifest.allowed_hosts, (TAIL_HOST,))
        self.assertEqual(manifest.region_roles, ('activity_rules', 'comments'))
        self.assertIs(manifest.permissions['site_rules_reviewed'], True)
        self.assertIs(manifest.permissions['login_required'], False)
        self.assertTrue(manifest.skill_dir.is_dir())

    def test_skill_bundle_files_exist(self) -> None:
        skill_dir = SKILLS / SKILL_ID
        self.assertTrue((skill_dir / 'SKILL.md').is_file())
        self.assertTrue((skill_dir / 'manifest.json').is_file())
        self.assertTrue((skill_dir / 'verify.json').is_file())
        self.assertGreater(len((skill_dir / 'SKILL.md').read_text(encoding='utf-8').strip()), 100)

    @staticmethod
    def _regions(page: dict) -> tuple[Region, ...]:
        return tuple(
            Region(raw['region_id'], raw['role'], tuple(raw['bbox']), text=raw['text'],
                   visual_text=raw['visual_text'], lazy=raw['lazy'], requires=raw['requires'],
                   blocked=raw['blocked'], scroll_y=raw['scroll_y'])
            for raw in page['regions']
        )


class SkillLoadingValidationTest(unittest.TestCase):
    """坏 Skill 必须在加载期拒绝，不能带病进入评测。"""

    def setUp(self) -> None:
        self.registry = SkillRegistry.load(SKILLS)
        self.view = build_page_view(load_page('layout_changed_v1'))

    def _variant_root(self, *, remove: tuple[str, ...] = (),
                      mutate_manifest=None, mutate_verify=None) -> Path:
        temp_root = Path(tempfile.mkdtemp(prefix='skill_variant_'))
        self.addCleanup(shutil.rmtree, str(temp_root), True)
        target = temp_root / SKILL_ID
        shutil.copytree(SKILLS / SKILL_ID, target)
        for name in remove:
            (target / name).unlink()
        if mutate_manifest is not None:
            raw = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
            mutate_manifest(raw)
            (target / 'manifest.json').write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
        if mutate_verify is not None:
            raw = json.loads((target / 'verify.json').read_text(encoding='utf-8'))
            mutate_verify(raw)
            (target / 'verify.json').write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
        return temp_root

    def test_missing_root_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            SkillRegistry.load(PAGES / 'not-a-skill-root')

    def test_missing_skill_md_raises(self) -> None:
        root = self._variant_root(remove=('SKILL.md',))
        with self.assertRaises(ValueError):
            SkillRegistry.load(root)

    def test_unknown_region_role_raises(self) -> None:
        def mutate(raw: dict) -> None:
            raw['region_roles'] = ['activity_rules', 'made_up_role']
        with self.assertRaises(ValueError):
            SkillRegistry.load(self._variant_root(mutate_manifest=mutate))

    def test_bad_structural_signature_raises(self) -> None:
        def mutate(raw: dict) -> None:
            raw['structural_signatures'] = ['struct:not-a-real-signature']
        with self.assertRaises(ValueError):
            SkillRegistry.load(self._variant_root(mutate_manifest=mutate))

    def test_missing_verify_check_raises(self) -> None:
        def mutate(raw: dict) -> None:
            raw['checks'] = [check for check in raw['checks'] if check['kind'] != 'text_pattern']
        with self.assertRaises(ValueError):
            SkillRegistry.load(self._variant_root(mutate_verify=mutate))

    def test_skill_id_directory_mismatch_raises(self) -> None:
        root = self._variant_root()
        (root / SKILL_ID).rename(root / 'renamed-skill')
        with self.assertRaises(ValueError):
            SkillRegistry.load(root)

    def test_site_rules_unreviewed_is_loaded_but_ineligible(self) -> None:
        """站点规则未复核：加载合法，但门禁必须拒绝产出先验。"""
        def mutate(raw: dict) -> None:
            raw['permissions']['site_rules_reviewed'] = False
        registry = SkillRegistry.load(self._variant_root(mutate_manifest=mutate))
        self.assertEqual(len(registry.manifests()), 1)
        self.assertEqual(registry.eligible(self.view, host=TAIL_HOST), ())
        self.assertEqual(registry.hints(self.view, build_goal(load_page('layout_changed_v1'))), ())

    def test_valid_signature_change_makes_skill_ineligible(self) -> None:
        """结构签名改成另一个合法值时，签名门禁拒绝。"""
        def mutate(raw: dict) -> None:
            raw['structural_signatures'] = ['struct:0000000000000000']
        registry = SkillRegistry.load(self._variant_root(mutate_manifest=mutate))
        self.assertEqual(registry.eligible(self.view, host=TAIL_HOST), ())


class SkillEligibilityTest(unittest.TestCase):
    """真实样例上的门禁行为。"""

    def setUp(self) -> None:
        self.registry = SkillRegistry.load(SKILLS)

    def test_eligible_on_activity_structure_pages(self) -> None:
        for page_id in ('static_article', 'activity_rules_lazy',
                        'layout_changed_v1', 'layout_changed_v2'):
            view = build_page_view(load_page(page_id))
            eligible = self.registry.eligible(view, host=TAIL_HOST)
            self.assertEqual([manifest.skill_id for manifest in eligible], [SKILL_ID],
                             f'{page_id} 应可用合法 Skill')

    def test_wrong_host_is_rejected(self) -> None:
        view = build_page_view(load_page('layout_changed_v1'))
        self.assertEqual(self.registry.eligible(view, host='evil.example'), ())
        self.assertEqual(self.registry.eligible(view, host=''), ())

    def test_non_activity_structures_are_rejected(self) -> None:
        # 海报页/登录页/阻塞页的结构签名与 Skill 不同，签名门禁必须拒绝。
        for page_id in ('poster_image_only', 'login_comments', 'captcha_blocked'):
            view = build_page_view(load_page(page_id))
            self.assertEqual(self.registry.eligible(view, host=TAIL_HOST), (),
                             f'{page_id} 不应匹配该 Skill 的结构签名')

    def test_blocked_page_is_rejected_even_if_signature_matches(self) -> None:
        page = load_page('layout_changed_v1')
        blocked_view = build_page_view(page, blocked_reason='captcha')
        self.assertEqual(blocked_view.signature, build_page_view(page).signature)
        self.assertEqual(self.registry.eligible(blocked_view, host=TAIL_HOST), ())

    def test_login_scenario_gate_uses_manifest_permission(self) -> None:
        """登录场景必须与 permissions.login_required 一致：

        - 面向非登录场景的 Skill 不得用于需要登录的评论区；
        - 声明 login_required=true 的 Skill 才会被同一页面接受。
        """
        login_page = load_page('login_comments')
        login_view = build_page_view(login_page)
        login_signature = login_view.signature

        def build_variant_root(login_required: bool) -> Path:
            temp_root = Path(tempfile.mkdtemp(prefix='skill_login_'))
            self.addCleanup(shutil.rmtree, str(temp_root), True)
            target = temp_root / SKILL_ID
            shutil.copytree(SKILLS / SKILL_ID, target)
            raw = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
            raw['structural_signatures'] = [login_signature]
            raw['region_roles'] = ['comments']
            raw['permissions']['login_required'] = login_required
            (target / 'manifest.json').write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
            return temp_root

        non_login_registry = SkillRegistry.load(build_variant_root(login_required=False))
        self.assertEqual(non_login_registry.eligible(login_view, host=TAIL_HOST), (),
                         '非登录场景 Skill 不得在登录评论区页面上生效')

        login_registry = SkillRegistry.load(build_variant_root(login_required=True))
        self.assertEqual([manifest.skill_id for manifest in login_registry.eligible(login_view, host=TAIL_HOST)],
                         [SKILL_ID], '声明需登录的 Skill 应被登录评论区页面接受')


class SkillHintsTest(unittest.TestCase):
    """区域先验：只来自合法 Skill，且只影响排序不产生证据。"""

    def setUp(self) -> None:
        self.registry = SkillRegistry.load(SKILLS)

    def test_hints_present_on_eligible_page(self) -> None:
        page = load_page('layout_changed_v1')
        view = build_page_view(page)
        goal = build_goal(page)
        hints = self.registry.hints(view, goal)
        self.assertTrue(hints)
        self.assertTrue(all(hint.source == SKILL_ID for hint in hints))
        # 目标角色 activity_rules 必须被先验点出，且排在辅助角色之前。
        self.assertEqual(hints[0].region_id, 'rules_v1')
        self.assertEqual(hints[0].score, 1.0)
        hinted_roles = [view.region(hint.region_id).role for hint in hints]
        self.assertEqual(hinted_roles[0], 'activity_rules')
        self.assertIn('comments', hinted_roles)
        for hint in hints:
            self.assertGreaterEqual(hint.score, 0.0)
            self.assertLessEqual(hint.score, 1.0)
            self.assertIn(hint.region_id, [region.region_id for region in view.regions])

    def test_hints_empty_on_ineligible_pages(self) -> None:
        for page_id in ('poster_image_only', 'login_comments', 'captcha_blocked'):
            page = load_page(page_id)
            self.assertEqual(self.registry.hints(build_page_view(page), build_goal(page)), (),
                             f'{page_id} 不满足门禁时不得产出先验')

    def test_objective_role_boosts_hint_score(self) -> None:
        page = load_page('layout_changed_v1')
        view = build_page_view(page)
        boosted = {hint.region_id: hint.score
                   for hint in self.registry.hints(view, build_goal(page))}
        plain_goal = ResearchGoal(
            task_id='skill-test::plain',
            question='查看评论',
            objectives=(Objective(objective_id='comments_only',
                                  question='查看合成评论内容',
                                  answer_kind='text',
                                  region_hints=('poster',),
                                  required=True),),
        )
        plain = {hint.region_id: hint.score
                 for hint in self.registry.hints(view, plain_goal)}
        self.assertEqual(boosted['rules_v1'], 1.0)
        self.assertEqual(plain['rules_v1'], 0.9)
        self.assertGreater(boosted['rules_v1'], plain['rules_v1'])

    def test_hint_provider_requires_matching_host(self) -> None:
        page = load_page('layout_changed_v1')
        view = build_page_view(page)
        goal = build_goal(page)
        provider = SkillHintProvider(self.registry, TAIL_HOST)
        self.assertEqual(provider.name, 'skill_hints')
        self.assertTrue(provider.hints(view, goal))
        wrong_host_provider = SkillHintProvider(self.registry, 'evil.example')
        self.assertEqual(wrong_host_provider.hints(view, goal), ())
        # 用来源主机构造的 provider 在他人页面上也不得给出先验。
        foreign_view = build_page_view(load_page('poster_image_only'))
        self.assertEqual(provider.hints(foreign_view, goal), ())

    def test_scores_clamped_when_manifest_has_many_region_roles(self) -> None:
        """N1 回归：合法 manifest 的 region_roles ≥6 时，低优先级角色分值必须 clamp。

        修复前 ``0.9 - 0.2*index`` 在第 6 个角色（index=5）得到 -0.1，构造
        ``RegionHint`` 时会被冻结契约的 ``0 <= score <= 1`` 校验拒绝。
        """
        roles = ('navigation', 'article_body', 'activity_rules', 'comments', 'poster', 'form')
        regions = tuple(
            Region(region_id=f'region:{role}', role=role, bbox=(0, index * 100, 100, 100),
                   text=f'合成区域 {role}', scroll_y=index * 100)
            for index, role in enumerate(roles)
        )
        view = PageView(
            page_version='sha256:many-region-roles',
            url='https://www.taptap.cn/example/many-region-roles',
            title='多区域角色合成视图',
            viewport=(1440, 1000),
            scroll_height=1000,
            regions=regions,
            dom_text='',
        )
        # 走真实加载器：只替换签名与角色列表，验证「合法 manifest 不会因角色多而崩溃」。
        temp_root = Path(tempfile.mkdtemp(prefix='skill_many_roles_'))
        self.addCleanup(shutil.rmtree, str(temp_root), True)
        target = temp_root / SKILL_ID
        shutil.copytree(SKILLS / SKILL_ID, target)
        raw = json.loads((target / 'manifest.json').read_text(encoding='utf-8'))
        raw['region_roles'] = list(roles)
        raw['structural_signatures'] = [view.signature]
        (target / 'manifest.json').write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
        registry = SkillRegistry.load(temp_root)
        goal = ResearchGoal(
            task_id='skill-test::many-region-roles',
            question='确认合成活动规则',
            objectives=(Objective(objective_id='rules', question='确认合成活动规则',
                                  answer_kind='date', region_hints=('activity_rules',),
                                  required=True),),
        )
        hints = registry.hints(view, goal)  # 修复前这里会抛 ValueError
        self.assertEqual(len(hints), len(roles), '每个已加载角色都应产出先验')
        scores = {hint.region_id: hint.score for hint in hints}
        for hint in hints:
            self.assertGreaterEqual(hint.score, 0.0)
            self.assertLessEqual(hint.score, 1.0)
        self.assertEqual(scores['region:form'], 0.0, '最低优先级角色应 clamp 到 0.0')
        self.assertEqual(scores['region:navigation'], 0.9)
        # 基础 0.5，命中 objective.region_hints 再加权 0.1。
        self.assertEqual(scores['region:activity_rules'], 0.6)


class SkillVerificationTest(unittest.TestCase):
    """完成验证：只描述流程完成，绝不改写证据的 verified 状态。"""

    def setUp(self) -> None:
        self.registry = SkillRegistry.load(SKILLS)
        self.manifest = self.registry.manifests()[0]

    def _verification(self, view: PageView, trace: TaskTrace) -> SkillVerification:
        return self.registry.verify(self.manifest, view, trace)

    def test_completed_flow_passes_five_checks(self) -> None:
        view = build_page_view(load_page('layout_changed_v1'))
        evidence = build_evidence(view)
        verification = self._verification(view, build_trace(view, (evidence,)))

        self.assertTrue(verification.passed)
        self.assertEqual(verification.skill_id, SKILL_ID)
        self.assertEqual([check['kind'] for check in verification.checks], list(VERIFY_KINDS))
        self.assertTrue(all(check['passed'] for check in verification.checks))
        for check in verification.checks:
            self.assertIn('expected', check)
            self.assertIn('actual', check)
        # 完成验证不得改写证据：它只读轨迹。
        self.assertFalse(evidence.verified)
        self.assertIsNone(evidence.verifier)

    def test_missing_evidence_fails(self) -> None:
        view = build_page_view(load_page('layout_changed_v1'))
        verification = self._verification(view, build_trace(view, ()))
        self.assertFalse(verification.passed)
        failed = {check['kind'] for check in verification.checks if not check['passed']}
        self.assertEqual(failed, {'extraction_non_empty', 'text_pattern', 'page_version_current'})

    def test_text_pattern_miss_fails(self) -> None:
        view = build_page_view(load_page('layout_changed_v1'))
        evidence = build_evidence(view, text='活动规则（合成）：每日登录可参与。')
        verification = self._verification(view, build_trace(view, (evidence,)))
        self.assertFalse(verification.passed)
        self.assertIn('text_pattern',
                      {check['kind'] for check in verification.checks if not check['passed']})

    def test_stale_page_version_fails(self) -> None:
        view = build_page_view(load_page('layout_changed_v1'))
        evidence = build_evidence(view, page_version='sha256:stale-version')
        verification = self._verification(view, build_trace(view, (evidence,)))
        self.assertFalse(verification.passed)
        self.assertIn('page_version_current',
                      {check['kind'] for check in verification.checks if not check['passed']})

    def test_blocked_page_and_missing_region_fail(self) -> None:
        view = build_page_view(load_page('captcha_blocked'))
        evidence = build_evidence(view, text='活动规则（合成）：报名截止：2026年12月01日。')
        verification = self._verification(view, build_trace(view, (evidence,)))
        self.assertFalse(verification.passed)
        failed = {check['kind'] for check in verification.checks if not check['passed']}
        self.assertIn('page_not_blocked', failed)
        self.assertIn('region_present', failed)

    def test_verification_never_sets_verified_even_on_success(self) -> None:
        view = build_page_view(load_page('layout_changed_v2'))
        evidence = build_evidence(view, region='collapsed_rules_v2',
                                  extraction='dom',
                                  text='活动规则（合成，v2 布局）：\n报名截止：2026年11月25日 23:59。')
        trace = build_trace(view, (evidence,))
        before = [item.as_dict() for item in trace.evidence]
        verification = self._verification(view, trace)
        after = [item.as_dict() for item in trace.evidence]
        self.assertTrue(verification.passed)
        self.assertEqual(before, after, 'verify 不得修改证据')
        self.assertTrue(all(not item.verified for item in trace.evidence))
        self.assertNotIn('verified', json.dumps([check for check in verification.checks],
                                                ensure_ascii=False))

    def test_manifest_dataclass_is_frozen(self) -> None:
        with self.assertRaises(Exception):
            self.manifest.skill_id = 'mutated'  # type: ignore[misc]


class SkillHarnessIntegrationTest(unittest.TestCase):
    """真实 FixtureDriver + AcquisitionHarness + Store 跑 Skill 路径的集成边界。

    这些测试直接复用 ``acquisition.driver`` / ``harness`` / ``policy`` / ``store``，
    不重写任何驱动或执行循环；Skill 只通过 hint_provider 与 verify 参与。
    """

    def setUp(self) -> None:
        self.registry = SkillRegistry.load(SKILLS)
        self._workdirs: list[str] = []

    def tearDown(self) -> None:
        for workdir in self._workdirs:
            shutil.rmtree(workdir, ignore_errors=True)

    def _store(self) -> AcquisitionStore:
        workdir = tempfile.mkdtemp(prefix='skill_harness_')
        self._workdirs.append(workdir)
        return AcquisitionStore(Path(workdir) / 'store.sqlite3')

    def _run(self, page_id: str, policy) -> tuple[TaskTrace, AcquisitionStore, FixtureDriver, dict]:
        page = load_page(page_id)
        goal = build_goal(page)
        driver = FixtureDriver.from_file(PAGES / f'{page_id}.json')
        store = self._store()
        trace = AcquisitionHarness(driver, policy, store, budgets=model.Budget()).run(goal)
        return trace, store, driver, page

    def test_skill_hints_lazy_page_produces_unverified_located_evidence(self) -> None:
        provider = SkillHintProvider(self.registry, TAIL_HOST)
        trace, store, driver, page = self._run(
            'activity_rules_lazy', AdaptiveObservationPolicy(hint_provider=provider))
        try:
            expected = page['objectives'][0]['expected_answer']
            self.assertEqual(trace.stop_reason, 'objectives_covered')
            self.assertIn(expected, '\n'.join(item.text for item in trace.evidence))
            self.assertTrue(any(item.region == 'rules' for item in trace.evidence),
                            '证据应绑定活动规则语义区域')
            self.assertIn('interact', [receipt.action for receipt in trace.receipts])
            self.assertGreaterEqual(len(trace.page_versions), 2)
            self.assertTrue(all(item.verified is False for item in trace.evidence),
                            '提取成功不得被当作事实已核验')
            self.assertTrue(all(item.verified is False for item in store.evidence(goal_task_id(trace))),
                            'store 中的证据同样不得被置真')
        finally:
            store.close()
            driver.close()

    def test_completion_verification_passes_without_verifying_evidence(self) -> None:
        """跑完 harness 后 evidence.verified 仍全 False，而 SkillVerification.passed 可为 True。"""
        provider = SkillHintProvider(self.registry, TAIL_HOST)
        trace, store, _driver, page = self._run(
            'layout_changed_v1', AdaptiveObservationPolicy(hint_provider=provider))
        try:
            self.assertTrue(trace.evidence)
            self.assertTrue(all(item.verified is False for item in trace.evidence))
            # 布局 v1 无交互步骤，重新 open 得到的视图版本与证据版本一致。
            goal = build_goal(page)
            fresh = FixtureDriver.from_file(PAGES / 'layout_changed_v1.json')
            try:
                view = fresh.open(goal)
            finally:
                fresh.close()
            manifest = self.registry.eligible(view, host=TAIL_HOST)[0]
            verification = self.registry.verify(manifest, view, trace)
            self.assertTrue(verification.passed,
                            f'合法 Skill 的完成条件应通过：{[c for c in verification.checks if not c["passed"]]}')
            self.assertTrue(all(item.verified is False for item in trace.evidence),
                            '完成验证绝不能把 Evidence.verified 置真')
            self.assertTrue(all(item.verifier is None for item in trace.evidence))
        finally:
            store.close()

    def test_login_page_gets_no_skill_hints_and_no_fabricated_comments(self) -> None:
        page = load_page('login_comments')
        view = build_page_view(page)
        self.assertEqual(self.registry.hints(view, build_goal(page)), ())
        provider = SkillHintProvider(self.registry, TAIL_HOST)
        trace, store, driver, _page = self._run(
            'login_comments', AdaptiveObservationPolicy(hint_provider=provider))
        try:
            self.assertEqual(trace.stop_reason, 'login_required')
            self.assertEqual(trace.evidence, ())
            self.assertEqual(store.evidence(goal_task_id(trace)), [])
        finally:
            store.close()
            driver.close()

    def test_blocked_page_never_gets_skill_hints(self) -> None:
        page = load_page('captcha_blocked')
        view = build_page_view(page)
        self.assertEqual(self.registry.hints(view, build_goal(page)), ())
        provider = SkillHintProvider(self.registry, TAIL_HOST)
        trace, store, driver, _page = self._run(
            'captcha_blocked', AdaptiveObservationPolicy(hint_provider=provider))
        try:
            self.assertEqual(trace.evidence, ())
            self.assertTrue(trace.stop_reason.startswith('blocked'), trace.stop_reason)
        finally:
            store.close()
            driver.close()


def goal_task_id(trace: TaskTrace) -> str:
    """harness 的 trace.task_id 是持久化任务 id；store.evidence 用它查询。"""
    return trace.task_id


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
