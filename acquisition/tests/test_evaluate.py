"""acquisition.evaluate 的样本设计检查与离线对照集成测试。

测试分三层：

1. 冻结样本设计检查：7 个页面必须按 ``fixtures/README.md`` 的语义构造
   （隐藏内容不进 ``dom_text``、真值只在 ``objectives``、每页至少一个 required
   objective、固定三屏覆盖不到 poster/长图页等）。
2. 离线对照集成：四种策略在 7 个样本上真跑 ``AcquisitionHarness``，检查矩阵
   指标与 checks 列表；独立重复关键断言，而不是只信任 evaluate 自己的 checks。
3. 边界：评测真值不得进入驱动视图/轨迹；``run_fixture`` 与 CLI 可用。

所有断言都是机制检查；命中 ``expected_answer`` 不等于事实成立，本文件也不
声称真实网页质量或事实核验能力。
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from acquisition import evaluate as evaluate_module
from acquisition import model
from acquisition.driver import FixtureDriver
from acquisition.evaluate import (
    CONCLUSION,
    EVAL_BUDGET,
    POLICIES,
    REQUIRED_PAGES,
    evaluate,
    goal_from_fixture,
    load_fixture,
    run_fixture,
)
from acquisition.harness import AcquisitionHarness
from acquisition.policy import AdaptiveObservationPolicy
from acquisition.store import AcquisitionStore

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures'
PAGES = FIXTURES / 'pages'
SKILLS = FIXTURES / 'skills'
HOST = 'www.taptap.cn'


# 完整评测矩阵只跑一次（约 5s），供多个测试类复用；避免每个测试重复跑 4×7 次 harness。
_MATRIX_RESULT: dict | None = None
_MATRIX_STDOUT: str = ''


def matrix_result() -> dict:
    """返回本次测试共享的完整评测结果（首次调用时真跑 evaluate()）。"""
    global _MATRIX_RESULT, _MATRIX_STDOUT
    if _MATRIX_RESULT is None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            _MATRIX_RESULT = evaluate(PAGES)
        _MATRIX_STDOUT = stdout.getvalue()
    return _MATRIX_RESULT


def matrix_stdout() -> str:
    matrix_result()
    return _MATRIX_STDOUT


def _norm(text: str) -> str:
    return ''.join(str(text or '').split())


def _page(page_id: str) -> dict:
    return load_fixture(PAGES / f'{page_id}.json')


def _answer_occurrences(page: dict) -> list[tuple[str, str | None, str | None]]:
    """expected_answer 在样本里出现在哪些字段（仅评测侧使用真值）。"""
    answer = _norm(page['objectives'][0]['expected_answer'])
    found: list[tuple[str, str | None, str | None]] = []
    if answer and answer in _norm(page['dom_text']):
        found.append(('dom_text', None, None))
    for raw in page['regions']:
        if answer and answer in _norm(raw['text']):
            found.append(('text', raw['region_id'], raw['role']))
        if answer and answer in _norm(raw['visual_text']):
            found.append(('visual_text', raw['region_id'], raw['role']))
    return found


class FrozenSampleDesignTest(unittest.TestCase):
    """冻结样本必须满足 fixtures/README.md 的页面语义与对照设计。"""

    def test_required_pages_present(self) -> None:
        present = sorted(path.stem for path in PAGES.glob('*.json'))
        self.assertEqual(present, sorted(REQUIRED_PAGES))

    def test_page_identity_and_source_note(self) -> None:
        for page_id in REQUIRED_PAGES:
            page = _page(page_id)
            self.assertEqual(page['schema'], 'acquisition-fixture-v0.1')
            self.assertEqual(page['page_id'], page_id)
            self.assertTrue(page['label'])
            self.assertIn('合成', page['source_note'])
            self.assertIn('不是真实站点录制', page['source_note'])
            self.assertTrue(page['url'].startswith('https://www.taptap.cn/example/'))
            self.assertEqual(tuple(page['viewport']), (1440, 1000))
            self.assertIn(page['blocked_reason'], (None, 'captcha', 'login_wall', 'access_denied'))

    def test_regions_follow_frozen_contract(self) -> None:
        for page_id in REQUIRED_PAGES:
            page = _page(page_id)
            ids = [raw['region_id'] for raw in page['regions']]
            self.assertEqual(len(ids), len(set(ids)), f'{page_id} 区域 id 重复')
            for raw in page['regions']:
                # 用冻结契约校验 role/bbox/scroll_y，保证驱动能构造 Region。
                region = model.Region(
                    region_id=raw['region_id'], role=raw['role'], bbox=tuple(raw['bbox']),
                    text=raw['text'], visual_text=raw['visual_text'], lazy=raw['lazy'],
                    requires=raw['requires'], blocked=raw['blocked'], scroll_y=raw['scroll_y'],
                )
                self.assertIn(region.role, model.REGION_ROLES)
                self.assertEqual(region.bbox, tuple(raw['bbox']))

    def test_hidden_text_never_enters_dom_text(self) -> None:
        """visual_text 只能 OCR 读；未触发的 lazy 文本不在初始 DOM 正文里。"""
        for page_id in REQUIRED_PAGES:
            page = _page(page_id)
            dom = _norm(page['dom_text'])
            for raw in page['regions']:
                if _norm(raw['visual_text']):
                    self.assertNotIn(_norm(raw['visual_text']), dom,
                                     f'{page_id}/{raw["region_id"]}: visual_text 不得进入 dom_text')
                if raw['lazy'] and _norm(raw['text']):
                    self.assertNotIn(_norm(raw['text']), dom,
                                     f'{page_id}/{raw["region_id"]}: 未触发 lazy 文本不得进入 dom_text')

    def test_objectives_declare_truth_and_required(self) -> None:
        for page_id in REQUIRED_PAGES:
            page = _page(page_id)
            self.assertTrue(page['objectives'])
            self.assertTrue(any(item['required'] for item in page['objectives']),
                            f'{page_id} 至少需要一个 required objective')
            for raw in page['objectives']:
                self.assertIn(raw['answer_kind'], ('date', 'text', 'presence', 'count'))
                self.assertTrue(raw['expected_answer'])
                self.assertTrue(raw['expected_region_role'])
                self.assertIsInstance(raw['region_hints'], list)
                self.assertIsInstance(raw['required'], bool)

    def test_control_design_conditions(self) -> None:
        """逐页确认对照设计的隐藏/可读条件（README 必需对照表）。"""
        # static_article：日期在 article_body 的 DOM 文本里，现状基线可命中。
        hits = _answer_occurrences(_page('static_article'))
        self.assertIn(('text', 'article', 'article_body'), hits)
        self.assertIn(('dom_text', None, None), hits)

        # activity_rules_lazy：规则只在懒加载区域，初始 DOM 读不到。
        laziness = _page('activity_rules_lazy')
        hits = _answer_occurrences(laziness)
        self.assertIn(('text', 'rules', 'activity_rules'), hits)
        self.assertNotIn(('dom_text', None, None), hits)
        rules = next(raw for raw in laziness['regions'] if raw['region_id'] == 'rules')
        self.assertTrue(rules['lazy'])
        self.assertIn('click', rules['requires'])

        # poster_image_only：期限只在海报 visual_text 里，且位于固定三屏之后。
        poster = _page('poster_image_only')
        hits = _answer_occurrences(poster)
        self.assertEqual(hits, [('visual_text', 'poster', 'poster')])
        poster_raw = next(raw for raw in poster['regions'] if raw['region_id'] == 'poster')
        self._assert_beyond_fixed_frames(poster, poster_raw)

        # layout_changed_v1：纯视觉长图 + 固定三屏之后。
        layout_v1 = _page('layout_changed_v1')
        hits = _answer_occurrences(layout_v1)
        self.assertEqual(hits, [('visual_text', 'rules_v1', 'activity_rules')])
        rules_v1 = next(raw for raw in layout_v1['regions'] if raw['region_id'] == 'rules_v1')
        self._assert_beyond_fixed_frames(layout_v1, rules_v1)

        # layout_changed_v2：懒加载折叠区，区域 id 与 v1 不同但角色序列相同。
        layout_v2 = _page('layout_changed_v2')
        hits = _answer_occurrences(layout_v2)
        self.assertEqual(hits, [('text', 'collapsed_rules_v2', 'activity_rules')])
        collapsed = next(raw for raw in layout_v2['regions']
                         if raw['region_id'] == 'collapsed_rules_v2')
        self.assertTrue(collapsed['lazy'])
        v1_roles = model.structural_signature([
            model.Region(raw['region_id'], raw['role'], tuple(raw['bbox']), text=raw['text'],
                         visual_text=raw['visual_text'], lazy=raw['lazy'], requires=raw['requires'],
                         blocked=raw['blocked'], scroll_y=raw['scroll_y'])
            for raw in layout_v1['regions']])
        v2_roles = model.structural_signature([
            model.Region(raw['region_id'], raw['role'], tuple(raw['bbox']), text=raw['text'],
                         visual_text=raw['visual_text'], lazy=raw['lazy'], requires=raw['requires'],
                         blocked=raw['blocked'], scroll_y=raw['scroll_y'])
            for raw in layout_v2['regions']])
        self.assertEqual(v1_roles, v2_roles, '布局 v1/v2 结构签名必须相同')
        self.assertNotEqual(rules_v1['region_id'], collapsed['region_id'],
                            '布局变化应改变区域 id，用来验证 Skill 靠 role 定位')

        # login_comments：评论需要登录，页面本身不整体阻塞。
        login = _page('login_comments')
        self.assertIsNone(login['blocked_reason'])
        hits = _answer_occurrences(login)
        self.assertEqual(hits, [('text', 'comments', 'comments')])
        comments = next(raw for raw in login['regions'] if raw['region_id'] == 'comments')
        self.assertIn('login', comments['requires'])
        self.assertNotIn(('dom_text', None, None), hits)

        # captcha_blocked：全页验证码阻塞，真值在页面上根本不可得。
        captcha = _page('captcha_blocked')
        self.assertEqual(captcha['blocked_reason'], 'captcha')
        self.assertEqual(_answer_occurrences(captcha), [])

    def _assert_beyond_fixed_frames(self, page: dict, raw: dict) -> None:
        frames = model.fixed_frame_offsets(page['scroll_height'], page['viewport'][1])
        last_band_bottom = frames[-1] + page['viewport'][1]
        self.assertGreaterEqual(raw['bbox'][1], last_band_bottom,
                                f'{raw["region_id"]} 必须落在固定三屏覆盖范围之后')


class OfflineMatrixTest(unittest.TestCase):
    """四种策略 × 7 个冻结样本的真集成对照。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = matrix_result()
        cls.pages = {page['page_id']: page for page in cls.result['pages']}

    def _run(self, page_id: str, policy: str) -> dict:
        return self.pages[page_id]['runs'][policy]

    def test_all_checks_pass(self) -> None:
        failed = [check for check in self.result['checks'] if not check['passed']]
        self.assertEqual([check['id'] for check in failed], [],
                         f'checks 失败：{[(check["id"], check["actual"]) for check in failed]}')
        self.assertTrue(self.result['summary']['all_passed'])
        self.assertEqual(self.result['summary']['checks_passed'],
                         self.result['summary']['checks_total'])

    def test_required_check_ids_present(self) -> None:
        required = {
            'fixture_pages_complete',
            'adaptive_answer_not_below_fixed_frames',
            'dynamic_answer_not_below_legacy_v3',
            'adaptive_beats_baselines_on_activity_rules_lazy',
            'adaptive_beats_baselines_on_poster_image_only',
            'adaptive_beats_baselines_on_layout_changed_v1',
            'adaptive_beats_baselines_on_layout_changed_v2',
            'layout_v1_adaptive_located_hit',
            'layout_v2_adaptive_located_hit',
            'adaptive_units_below_legacy_on_static',
            'no_blocked_evidence_leak',
            'login_gap_recorded',
            'budgets_respected',
            'fingerprints_deterministic',
            'fixture_truth_not_leaked',
            'skill_gate_eligible_on_activity_pages',
            'skill_gate_rejects_wrong_host',
            'skill_gate_rejects_changed_signature',
            'skill_verification_does_not_set_verified',
        }
        ids = {check['id'] for check in self.result['checks']}
        self.assertTrue(required <= ids, f'缺少 checks：{required - ids}')
        for check in self.result['checks']:
            self.assertIsInstance(check['passed'], bool)
            self.assertTrue(check['expected'])
            self.assertTrue(check['actual'])

    def test_matrix_shape_and_contract_metrics(self) -> None:
        self.assertEqual(set(self.pages), set(REQUIRED_PAGES))
        required_keys = {'answer_hit', 'located_hit', 'evidence_count', 'captures',
                         'ocr_calls', 'units', 'steps', 'stop_reason', 'gap',
                         'evidence_verified', 'blocked_evidence_leak'}
        for page in self.result['pages']:
            self.assertEqual(set(page['runs']), set(POLICIES))
            for policy in POLICIES:
                run = page['runs'][policy]
                self.assertTrue(required_keys <= set(run), f'{page["page_id"]}/{policy} 缺少指标字段')
                self.assertTrue(run['within_budget'])
                self.assertTrue(all(item is False for item in run['evidence_verified']),
                                '提取证据不得被标记为已核验')

    def test_dynamic_located_hits_on_lazy_and_poster(self) -> None:
        for page_id in ('activity_rules_lazy', 'poster_image_only'):
            adaptive = self._run(page_id, 'adaptive')
            adaptive_skill = self._run(page_id, 'adaptive_skill')
            fixed = self._run(page_id, 'fixed_frames')
            legacy = self._run(page_id, 'legacy_v3')
            self.assertTrue(adaptive['answer_hit'], page_id)
            self.assertTrue(adaptive['located_hit'], f'{page_id}: 证据应可定位到语义区域')
            self.assertTrue(adaptive_skill['answer_hit'], page_id)
            self.assertTrue(adaptive_skill['located_hit'], page_id)
            self.assertFalse(fixed['answer_hit'], f'{page_id}: 固定三屏基线不应命中')
            self.assertFalse(legacy['answer_hit'], f'{page_id}: 现状基线不应命中')

    def test_layout_changes_keep_dynamic_hits(self) -> None:
        """S7：两类布局改版样本都必须有独立命中断言（v1 视觉长图 / v2 折叠区）。

        结论口径来自架构 9.3 与 CHANGELOG：adaptive/adaptive_skill 命中并定位到
        activity_rules，fixed_frames/legacy_v3 均不命中；evaluate 也必须生成
        对应的 beats 与 located 检查项，而不是只写在矩阵输出里。
        """
        check_ids = {check['id'] for check in self.result['checks']}
        for page_id, located_id in (('layout_changed_v1', 'layout_v1_adaptive_located_hit'),
                                    ('layout_changed_v2', 'layout_v2_adaptive_located_hit')):
            page = load_fixture(PAGES / f'{page_id}.json')
            expected_role = page['objectives'][0]['expected_region_role']
            self.assertEqual(expected_role, 'activity_rules')
            adaptive = self._run(page_id, 'adaptive')
            adaptive_skill = self._run(page_id, 'adaptive_skill')
            fixed = self._run(page_id, 'fixed_frames')
            legacy = self._run(page_id, 'legacy_v3')
            for policy, run in (('adaptive', adaptive), ('adaptive_skill', adaptive_skill)):
                self.assertTrue(run['answer_hit'], f'{page_id}/{policy} 应命中改版后的规则区')
                self.assertTrue(run['located_hit'],
                                f'{page_id}/{policy} 证据应定位到 {expected_role}')
                self.assertTrue(run['objective_hits'][0]['located_hit'])
            self.assertFalse(fixed['answer_hit'], f'{page_id}: fixed_frames 不应命中')
            self.assertFalse(legacy['answer_hit'], f'{page_id}: legacy_v3 不应命中')
            self.assertIn(f'adaptive_beats_baselines_on_{page_id}', check_ids)
            self.assertIn(located_id, check_ids)

    def test_static_page_cost_and_location(self) -> None:
        adaptive = self._run('static_article', 'adaptive')
        legacy = self._run('static_article', 'legacy_v3')
        fixed = self._run('static_article', 'fixed_frames')
        # 现状基线与动态策略都能在静态页命中；固定三屏也能扫到正文。
        self.assertTrue(adaptive['answer_hit'])
        self.assertTrue(legacy['answer_hit'])
        self.assertTrue(fixed['answer_hit'])
        # 动态策略成本更低，且证据定位到语义区域（现状基线只有 dom:body/截图）。
        self.assertLess(adaptive['units'], legacy['units'])
        self.assertTrue(adaptive['located_hit'])
        self.assertFalse(legacy['located_hit'])

    def test_blocked_page_zero_leak_and_zero_evidence(self) -> None:
        for policy in POLICIES:
            run = self._run('captcha_blocked', policy)
            self.assertFalse(run['blocked_evidence_leak'], policy)
            self.assertEqual(run['evidence_count'], 0, f'{policy} 在阻塞页产生了证据')
            self.assertTrue(run['stop_reason'].startswith('blocked'), run['stop_reason'])
        adaptive = self._run('captcha_blocked', 'adaptive')
        self.assertEqual(adaptive['captures'], 0, '自适应应在 inspect 后立即停止，不浪费截图')

    def test_login_page_gap_and_no_fabricated_evidence(self) -> None:
        adaptive = self._run('login_comments', 'adaptive')
        self.assertEqual(adaptive['stop_reason'], 'login_required')
        self.assertEqual(adaptive['evidence_count'], 0)
        self.assertFalse(adaptive['answer_hit'])
        for policy in POLICIES:
            self.assertFalse(self._run('login_comments', policy)['answer_hit'],
                             f'{policy} 不应伪造登录墙后的评论证据')

    def test_two_representative_runs_have_identical_fingerprints(self) -> None:
        """重复运行可复现：对 2 个代表页重跑，证据指纹与矩阵一致。

        全矩阵逐格重跑会把测试耗时翻倍且不增加检查信息；evaluate() 内部的
        ``fingerprints_deterministic`` check 也只对代表页重复执行。
        """
        samples = (('layout_changed_v2', 'adaptive'), ('poster_image_only', 'adaptive_skill'))
        for page_id, policy in samples:
            rerun = run_fixture(PAGES / f'{page_id}.json', policy)
            original = self._run(page_id, policy)
            self.assertEqual(original['evidence_fingerprints'],
                             rerun['evidence_fingerprints'],
                             f'{page_id}/{policy} 两次运行证据指纹不一致')
            self.assertEqual(original['stop_reason'], rerun['stop_reason'])
            self.assertEqual(original['evidence_count'], rerun['evidence_count'])
            self.assertFalse(rerun['blocked_evidence_leak'])

    def test_report_markdown_is_printed(self) -> None:
        text = matrix_stdout()
        self.assertIn('| 页面 | 策略 | answer_hit |', text)
        self.assertIn('| check | passed | expected | actual |', text)
        self.assertIn('checks：23/23 通过', text)
        self.assertIn('仅限合成冻结样本', text)

    def test_conclusion_wording_is_bounded(self) -> None:
        self.assertEqual(self.result['conclusion'], CONCLUSION)
        self.assertIn('仅限合成冻结样本', CONCLUSION)
        self.assertIn('不代表真实网页质量', CONCLUSION)
        self.assertIn('不构成事实核验', CONCLUSION)
        # 不得出现「已证明真实网页收益 / 事实已核验」类结论（否定句允许）。
        for forbidden in ('已证明真实网页', '事实已核验', '已核验事实'):
            self.assertNotIn(forbidden, CONCLUSION)
        self.assertNotIn('已证明', CONCLUSION)

    def test_budget_constant_matches_contract(self) -> None:
        self.assertEqual(EVAL_BUDGET, model.Budget())


class RunFixtureApiTest(unittest.TestCase):
    """``run_fixture`` 的公开行为（单样本入口）。"""

    def test_unknown_policy_raises(self) -> None:
        with self.assertRaises(ValueError):
            run_fixture(PAGES / 'static_article.json', 'not_a_policy')

    def test_metrics_for_layout_page(self) -> None:
        metrics = run_fixture(PAGES / 'layout_changed_v1.json', 'adaptive')
        self.assertEqual(metrics['page_id'], 'layout_changed_v1')
        self.assertEqual(metrics['policy'], 'adaptive')
        self.assertTrue(metrics['answer_hit'])
        self.assertTrue(metrics['located_hit'])
        self.assertIn('evidence_fingerprints', metrics)
        self.assertTrue(metrics['within_budget'])

    def test_store_path_is_used(self) -> None:
        workdir = tempfile.mkdtemp(prefix='eval_store_probe_')
        self.addCleanup(shutil.rmtree, workdir, True)
        store_path = Path(workdir) / 'single.sqlite3'
        metrics = run_fixture(PAGES / 'poster_image_only.json', 'fixed_frames',
                              store_path=store_path)
        self.assertTrue(store_path.is_file(), '显式 store_path 应被真正使用')
        self.assertFalse(metrics['answer_hit'])

    def test_missing_fixtures_dir_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            evaluate(PAGES / 'does-not-exist')


class EvaluateCliTest(unittest.TestCase):
    """``python -m acquisition.evaluate --fixtures DIR --out FILE`` 的命令行契约。

    复用矩阵结果并替换执行层，避免为了测 CLI 再跑一遍 4×7 次 harness；
    CLI 的职责是参数解析、结果落盘/打印与退出码，这些在这里逐项验证。
    """

    def _fake_evaluate(self, result: dict, calls: list):
        def fake_evaluate(fixtures_dir, *, out=None):
            calls.append((Path(fixtures_dir), Path(out) if out is not None else None))
            if out is not None:
                evaluate_module._write_json(result, out)
            evaluate_module._print_report(result)
            return result
        return fake_evaluate

    def test_cli_writes_json_and_prints_markdown(self) -> None:
        result = matrix_result()
        workdir = tempfile.mkdtemp(prefix='eval_cli_probe_')
        self.addCleanup(shutil.rmtree, workdir, True)
        out = Path(workdir) / 'evaluation.json'
        calls: list = []
        stdout = io.StringIO()
        with mock.patch.object(evaluate_module, 'evaluate',
                               side_effect=self._fake_evaluate(result, calls)):
            with contextlib.redirect_stdout(stdout):
                code = evaluate_module.main(['--fixtures', str(PAGES), '--out', str(out)])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [(PAGES, out)], 'CLI 应把 --fixtures/--out 原样传给 evaluate')
        self.assertTrue(out.is_file(), 'CLI 应把完整 JSON 写入 --out 文件')
        payload = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(payload['schema'], 'acquisition-eval-v0.1')
        self.assertEqual(payload['contract'], model.SCHEMA_VERSION)
        self.assertTrue(payload['summary']['all_passed'])
        self.assertEqual(len(payload['pages']), len(REQUIRED_PAGES))
        text = stdout.getvalue()
        self.assertIn('| 页面 | 策略 | answer_hit |', text)
        self.assertIn('checks：', text)
        self.assertIn('仅限合成冻结样本', payload['conclusion'])

    def test_cli_exit_code_reflects_failed_checks(self) -> None:
        failing = json.loads(json.dumps(matrix_result(), ensure_ascii=False))
        failing['summary'] = {'checks_total': 1, 'checks_passed': 0, 'all_passed': False}
        with mock.patch.object(evaluate_module, 'evaluate',
                               side_effect=self._fake_evaluate(failing, [])):
            with contextlib.redirect_stdout(io.StringIO()):
                code = evaluate_module.main(['--fixtures', str(PAGES)])
        self.assertEqual(code, 1, '存在失败 checks 时 CLI 必须返回非零退出码')


class TruthLeakBoundaryTest(unittest.TestCase):
    """评测真值不得进入驱动的 PageView 或 harness 轨迹。"""

    SENTINEL = '__EXPECTED_ANSWER_SENTINEL__'
    ROLE_SENTINEL = '__EXPECTED_ROLE_SENTINEL__'

    def _poisoned(self) -> tuple[Path, dict]:
        page = load_fixture(PAGES / 'static_article.json')
        for objective in page['objectives']:
            objective['expected_answer'] = self.SENTINEL
            objective['expected_region_role'] = self.ROLE_SENTINEL
        workdir = tempfile.mkdtemp(prefix='truth_leak_probe_')
        self.addCleanup(shutil.rmtree, workdir, True)
        path = Path(workdir) / 'static_article.json'
        path.write_text(json.dumps(page, ensure_ascii=False, indent=2), encoding='utf-8')
        return path, page

    def test_driver_view_never_exposes_expected_fields(self) -> None:
        path, page = self._poisoned()
        goal, _host = goal_from_fixture(page)
        driver = FixtureDriver.from_file(path)
        try:
            view = driver.open(goal)
            blob = model.canonical({
                'url': view.url,
                'title': view.title,
                'dom_text': view.dom_text,
                'regions': [{'region_id': region.region_id, 'role': region.role,
                             'text': region.text, 'visual_text': region.visual_text}
                            for region in view.regions],
            })
        finally:
            driver.close()
        self.assertNotIn(self.SENTINEL, blob)
        self.assertNotIn(self.ROLE_SENTINEL, blob)

    def test_harness_trace_never_exposes_expected_fields(self) -> None:
        path, page = self._poisoned()
        goal, _host = goal_from_fixture(page)
        driver = FixtureDriver.from_file(path)
        workdir = tempfile.mkdtemp(prefix='truth_leak_store_')
        self.addCleanup(shutil.rmtree, workdir, True)
        store = AcquisitionStore(Path(workdir) / 'store.sqlite3')
        try:
            trace = AcquisitionHarness(driver, AdaptiveObservationPolicy(), store,
                                       budgets=EVAL_BUDGET).run(goal)
            blob = model.canonical(trace.as_dict())
        finally:
            store.close()
            driver.close()
        self.assertNotIn(self.SENTINEL, blob)
        self.assertNotIn(self.ROLE_SENTINEL, blob)
        self.assertTrue(trace.evidence, '静态样本上应取得证据')
        self.assertTrue(all(self.SENTINEL not in item.text for item in trace.evidence))
        self.assertTrue(all(item.verified is False for item in trace.evidence))


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
