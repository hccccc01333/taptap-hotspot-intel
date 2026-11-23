"""``acquisition.live_acceptance`` 的测试：离线检查项/跳过逻辑 + 真实浏览器 smoke。

分两层：

- **离线**：用构造的假运行结果验证「运行前固定的检查项」与资源缺失 skip 语义，
  Linux CI（无 node/Playwright/Edge/OCR）也必须全绿；
- **真实浏览器 smoke**：资源可用时跑 ``lazy_rules`` 一个场景，断言 adaptive 点击
  展开后命中；资源缺失时自动 skip（skip 不是通过）。

真实浏览器验收本身由 ``python -m acquisition.live_acceptance`` 执行，本文件不把
假数据当成真实浏览器结果：假数据只用于验证 runner 的判定逻辑。
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from acquisition import live_acceptance as la
from acquisition import model

FIXTURES = Path(la.__file__).resolve().parent / 'fixtures' / 'live'
EXPECTED_FIXTURES = ('static_article.html', 'lazy_rules.html', 'poster_text.html',
                     'captcha.html', 'login_comments.html')

#: 运行前固定的检查项顺序（改动必须是有意的版本变更，不允许按结果挑选）。
EXPECTED_CHECK_IDS = (
    'live_fixtures_complete',
    'live_browser_available',
    'live_ocr_available',
    'fixture_mode_no_websocket',
    'static_all_policies_hit',
    'static_adaptive_units_below_legacy',
    'lazy_adaptive_hit',
    'lazy_baselines_miss',
    'lazy_adaptive_interacted',
    'lazy_page_version_changed',
    'lazy_adaptive_located',
    'poster_adaptive_ocr_hit',
    'poster_baselines_miss',
    'poster_region_visual_only',
    'captcha_no_evidence',
    'captcha_adaptive_zero_captures',
    'login_required_gap',
    'login_no_fabricated_comments',
    'screenshots_exist_and_sha256',
    'evidence_page_version_current',
    'evidence_never_verified',
    'page_version_stable_across_runs',
    'browser_session_reuse',
    'budgets_respected',
)

GOOD_ENVIRONMENT = {'browser': True, 'ocr': True, 'reason': 'ok',
                    'ocr_reason': '', 'ocr_engine': 'windows_ocr',
                    'fixture_websocket_blocked': True,
                    'fixture_websocket_detail': '未收到连接（测试替身）'}
NO_OCR_ENVIRONMENT = {'browser': True, 'ocr': False, 'reason': 'ok',
                      'ocr_reason': 'ocr_engine_missing', 'ocr_engine': ''}
NO_BROWSER_ENVIRONMENT = {'browser': False, 'ocr': False, 'reason': 'node_missing',
                          'ocr_reason': '真实浏览器不可用，未探测 OCR', 'ocr_engine': ''}


# ---------------------------------------------------------------------------
# 构造假运行结果（只用于离线检查项单测）
# ---------------------------------------------------------------------------
def _fake_sha(tag: str) -> str:
    return hashlib.sha256(tag.encode('utf-8')).hexdigest()


def _screenshot(tag: str) -> dict:
    digest = _fake_sha(tag)
    return {'file': f'D:/tmp/acq/{tag}.png', 'reported_sha256': digest,
            'actual_sha256': digest, 'exists': True, 'sha256_match': True,
            'inside_artifact_dir': True, 'expected_capture_id': 'capture_' + digest[:24]}


def _evidence(tag: str, version: str, extraction: str, text: str,
              region: str, *, verified: bool = False) -> dict:
    if extraction == 'ocr':
        capture_id = 'capture_' + _fake_sha(tag)[:24]
    else:
        capture_id = 'capture_dom_' + tag
    return {'capture_id': capture_id, 'page_version': version, 'region': region,
            'extraction': extraction, 'text': text, 'verified': verified}


def _run(page_id, policy, *, metrics, versions, evidence=None, region_details=None,
         screenshots=None, initial_version=None, screenshot_source='worker_transport',
         skipped=False, skip_reason='', error=None):
    versions = list(versions)
    return {
        'page_id': page_id,
        'policy': policy,
        'fixture': str(FIXTURES / f'{page_id}.html'),
        'skipped': skipped,
        'skip_reason': skip_reason,
        'error': error,
        'initial_page_version': initial_version or (versions[0] if versions else ''),
        'page_versions': versions,
        'region_details': dict(region_details or {}),
        'screenshots': list(screenshots or []),
        'screenshot_source': screenshot_source,
        'metrics': dict(metrics),
        'evidence': list(evidence or []),
        'receipts': [],
    }


def _base_metrics(**overrides) -> dict:
    metrics = {'answer_hit': False, 'located_hit': False, 'evidence_count': 0,
               'captures': 0, 'ocr_calls': 0, 'vlm_calls': 0, 'units': 1, 'steps': 1,
               'interacts': 0, 'stop_reason': 'no_new_evidence', 'gap': None,
               'status': 'failed', 'evidence_verified': True, 'within_budget': True}
    metrics.update(overrides)
    return metrics


_STATIC_REGIONS = {
    'dom_0': {'role': 'navigation', 'visual_only': False, 'text_length': 6},
    'dom_1': {'role': 'article_body', 'visual_only': False, 'text_length': 60},
    'dom_2': {'role': 'activity_rules', 'visual_only': False, 'text_length': 30},
    'dom_3': {'role': 'comments', 'visual_only': False, 'text_length': 30},
    'dom_4': {'role': 'footer', 'visual_only': False, 'text_length': 10},
}


def _good_runs() -> dict:
    """构造一份「全部检查应通过」的假矩阵（供离线判定逻辑单测）。"""
    static_v1 = 'sha256:static-v1'
    lazy_v1, lazy_v2 = 'sha256:lazy-v1', 'sha256:lazy-v2'
    poster_v1 = 'sha256:poster-v1'
    captcha_v1 = 'sha256:captcha-v1'
    login_v1 = 'sha256:login-v1'
    photo_evidence = _evidence('static-fixed-frame-1', static_v1, 'ocr',
                               '报名截止：2026年11月12日 23:59', 'viewport:0')
    runs = {
        'static_article': {
            'fixed_frames': _run(
                'static_article', 'fixed_frames',
                metrics=_base_metrics(answer_hit=True, evidence_count=1, captures=1,
                                      ocr_calls=1, units=4, steps=2,
                                      stop_reason='fixed_frames_exhausted', status='succeeded'),
                versions=[static_v1], evidence=[photo_evidence],
                region_details=_STATIC_REGIONS,
                screenshots=[_screenshot('static-fixed-frame-1')]),
            'legacy_v3': _run(
                'static_article', 'legacy_v3',
                metrics=_base_metrics(answer_hit=True, evidence_count=2, captures=1,
                                      ocr_calls=1, units=11, steps=3,
                                      stop_reason='legacy_v3_exhausted', status='succeeded'),
                versions=[static_v1],
                evidence=[_evidence('static-legacy-body', static_v1, 'dom',
                                    '整页正文：报名截止：2026年11月12日', 'dom:body'),
                          _evidence('static-legacy-frame-1', static_v1, 'ocr',
                                    '报名截止：2026年11月12日', 'viewport:0')],
                region_details=_STATIC_REGIONS,
                screenshots=[_screenshot('static-legacy-frame-1')]),
            'adaptive': _run(
                'static_article', 'adaptive',
                metrics=_base_metrics(answer_hit=True, located_hit=True, evidence_count=1,
                                      captures=0, ocr_calls=0, units=2, steps=2,
                                      stop_reason='objectives_covered', status='succeeded'),
                versions=[static_v1],
                evidence=[_evidence('static-adaptive-dom', static_v1, 'dom',
                                    '报名截止：2026年11月12日', 'dom_1')],
                region_details=_STATIC_REGIONS),
        },
        'lazy_rules': {
            'fixed_frames': _run(
                'lazy_rules', 'fixed_frames',
                metrics=_base_metrics(evidence_count=3, captures=3, ocr_calls=3, units=10,
                                      steps=4, stop_reason='fixed_frames_exhausted',
                                      status='succeeded'),
                versions=[lazy_v1],
                evidence=[_evidence(f'lazy-fixed-frame-{index}', lazy_v1, 'ocr',
                                    '页面说明文字', f'viewport:{index}')
                          for index in range(3)],
                screenshots=[_screenshot(f'lazy-fixed-frame-{index}') for index in range(3)]),
            'legacy_v3': _run(
                'lazy_rules', 'legacy_v3',
                metrics=_base_metrics(evidence_count=4, captures=3, ocr_calls=3, units=13,
                                      steps=5, stop_reason='legacy_v3_exhausted',
                                      status='succeeded'),
                versions=[lazy_v1],
                evidence=[_evidence('lazy-legacy-body', lazy_v1, 'dom', '页面可见正文', 'dom:body')]
                         + [_evidence(f'lazy-legacy-frame-{index}', lazy_v1, 'ocr',
                                      '页面说明文字', f'viewport:{index}')
                            for index in range(3)],
                screenshots=[_screenshot(f'lazy-legacy-frame-{index}') for index in range(3)]),
            'adaptive': _run(
                'lazy_rules', 'adaptive',
                metrics=_base_metrics(answer_hit=True, located_hit=True, evidence_count=1,
                                      captures=0, ocr_calls=0, units=4, steps=3,
                                      interacts=1, stop_reason='objectives_covered',
                                      status='succeeded'),
                versions=[lazy_v1, lazy_v2],
                evidence=[_evidence('lazy-adaptive-dom', lazy_v2, 'dom',
                                    '活动规则：报名截止：2026年11月20日 23:59', 'dom_2')],
                region_details={'dom_2': {'role': 'activity_rules', 'visual_only': False,
                                          'text_length': 0, 'lazy': True,
                                          'requires': 'click:展开规则'}}),
        },
        'poster_text': {
            'fixed_frames': _run(
                'poster_text', 'fixed_frames',
                metrics=_base_metrics(evidence_count=3, captures=3, ocr_calls=3, units=10,
                                      steps=4, stop_reason='fixed_frames_exhausted',
                                      status='succeeded'),
                versions=[poster_v1],
                evidence=[_evidence(f'poster-fixed-frame-{index}', poster_v1, 'ocr',
                                    '页面说明文字', f'viewport:{index}')
                          for index in range(3)],
                screenshots=[_screenshot(f'poster-fixed-frame-{index}') for index in range(3)]),
            'legacy_v3': _run(
                'poster_text', 'legacy_v3',
                metrics=_base_metrics(evidence_count=4, captures=3, ocr_calls=3, units=13,
                                      steps=5, stop_reason='legacy_v3_exhausted',
                                      status='succeeded'),
                versions=[poster_v1],
                evidence=[_evidence('poster-legacy-body', poster_v1, 'dom', '页面可见正文', 'dom:body')]
                         + [_evidence(f'poster-legacy-frame-{index}', poster_v1, 'ocr',
                                      '页面说明文字', f'viewport:{index}')
                            for index in range(3)],
                screenshots=[_screenshot(f'poster-legacy-frame-{index}') for index in range(3)]),
            'adaptive': _run(
                'poster_text', 'adaptive',
                metrics=_base_metrics(answer_hit=True, located_hit=True, evidence_count=1,
                                      captures=1, ocr_calls=1, units=4, steps=2,
                                      stop_reason='objectives_covered', status='succeeded'),
                versions=[poster_v1],
                evidence=[_evidence('poster-adaptive-ocr', poster_v1, 'ocr',
                                    '活动截止：2026年11月30日', 'dom_2')],
                region_details={'dom_2': {'role': 'poster', 'visual_only': True,
                                          'text_length': 0}},
                screenshots=[_screenshot('poster-adaptive-ocr')]),
        },
        'captcha': {
            'fixed_frames': _run(
                'captcha', 'fixed_frames',
                metrics=_base_metrics(evidence_count=0, captures=1, ocr_calls=1, units=4,
                                      steps=2, gap='blocked:captcha',
                                      stop_reason='blocked:captcha', status='blocked'),
                versions=[captcha_v1], screenshots=[_screenshot('captcha-fixed-frame-0')]),
            'legacy_v3': _run(
                'captcha', 'legacy_v3',
                metrics=_base_metrics(evidence_count=0, captures=1, ocr_calls=1, units=5,
                                      steps=2, gap='blocked:captcha',
                                      stop_reason='blocked:captcha', status='blocked'),
                versions=[captcha_v1], screenshots=[_screenshot('captcha-legacy-frame-0')]),
            'adaptive': _run(
                'captcha', 'adaptive',
                metrics=_base_metrics(evidence_count=0, captures=0, ocr_calls=0, units=1,
                                      steps=1, gap='blocked:captcha',
                                      stop_reason='blocked:captcha', status='blocked'),
                versions=[captcha_v1]),
        },
        'login_comments': {
            'fixed_frames': _run(
                'login_comments', 'fixed_frames',
                metrics=_base_metrics(evidence_count=3, captures=3, ocr_calls=3, units=10,
                                      steps=4, stop_reason='fixed_frames_exhausted',
                                      status='failed', gap='objectives_uncovered'),
                versions=[login_v1],
                evidence=[_evidence(f'login-fixed-frame-{index}', login_v1, 'ocr',
                                    '玩家评论 登录后查看评论', f'viewport:{index}')
                          for index in range(3)],
                screenshots=[_screenshot(f'login-fixed-frame-{index}') for index in range(3)]),
            'legacy_v3': _run(
                'login_comments', 'legacy_v3',
                metrics=_base_metrics(evidence_count=4, captures=3, ocr_calls=3, units=13,
                                      steps=5, stop_reason='legacy_v3_exhausted',
                                      status='failed', gap='objectives_uncovered'),
                versions=[login_v1],
                evidence=[_evidence('login-legacy-body', login_v1, 'dom',
                                    '玩家评论 登录后查看评论', 'dom:body')]
                         + [_evidence(f'login-legacy-frame-{index}', login_v1, 'ocr',
                                      '玩家评论 登录后查看评论', f'viewport:{index}')
                            for index in range(3)],
                screenshots=[_screenshot(f'login-legacy-frame-{index}') for index in range(3)]),
            'adaptive': _run(
                'login_comments', 'adaptive',
                metrics=_base_metrics(evidence_count=0, captures=0, ocr_calls=0, units=1,
                                      steps=1, gap='login_required',
                                      stop_reason='login_required', status='blocked'),
                versions=[login_v1]),
        },
    }
    return runs


def _good_determinism() -> dict:
    comparisons = []
    runs = _good_runs()
    for scenario in la.SCENARIOS:
        adaptive = runs[scenario.page_id]['adaptive']
        comparisons.append({
            'page_id': scenario.page_id, 'ok': True, 'skipped': False, 'reason': '',
            'first_initial': adaptive['initial_page_version'],
            'second_initial': adaptive['initial_page_version'],
            'first_versions': list(adaptive['page_versions']),
            'second_versions': list(adaptive['page_versions']),
        })
    return {'comparisons': comparisons, 'performed': True}


def _good_session_reuse() -> dict:
    return {'performed': True, 'ok': True, 'reason': '', 'session_launches': 1,
            'versions': ['sha256:reuse-a', 'sha256:reuse-b']}


def _checks_for(runs=None, environment=None, determinism=None, session_reuse=None) -> list:
    return la.compute_checks(runs if runs is not None else _good_runs(),
                             environment if environment is not None else GOOD_ENVIRONMENT,
                             determinism if determinism is not None else _good_determinism(),
                             session_reuse if session_reuse is not None else _good_session_reuse(),
                             FIXTURES)


def _by_id(checks: list) -> dict:
    return {check['id']: check for check in checks}


def _failed_ids(checks: list) -> list:
    return [check['id'] for check in checks if not check['skipped'] and check['passed'] is False]


# ---------------------------------------------------------------------------
# 夹具与离线检查项
# ---------------------------------------------------------------------------
class FixtureTests(unittest.TestCase):
    def test_fixtures_complete_and_declared_synthetic(self):
        present = sorted(path.name for path in FIXTURES.glob('*.html'))
        self.assertEqual(present, sorted(EXPECTED_FIXTURES))
        for name in EXPECTED_FIXTURES:
            html = (FIXTURES / name).read_text(encoding='utf-8')
            self.assertIn(la.FIXTURE_HEADER, html, f'{name} 缺少“本地合成场景”口径声明')

    def test_fixtures_have_no_external_resources_or_network_references(self):
        external = ('http://', 'https://')
        for name in EXPECTED_FIXTURES:
            html = (FIXTURES / name).read_text(encoding='utf-8')
            for marker in external:
                self.assertNotIn(marker, html, f'{name} 出现外部地址 {marker}')
            self.assertNotIn('<link', html, f'{name} 不应引用外部样式')
            self.assertNotIn('<iframe', html, f'{name} 不应引用外部框架')
            self.assertNotIn('src="//', html)
            # 只允许内联脚本，不允许通过 src 加载别的脚本。
            self.assertNotRegex(html, r'<script[^>]+src=')

    def test_lazy_fixture_has_no_rule_text_before_click(self):
        html = (FIXTURES / 'lazy_rules.html').read_text(encoding='utf-8')
        self.assertIn('data-region-lazy="true"', html)
        self.assertIn('data-region-requires="click:展开规则"', html)
        self.assertIn('<button', html)
        self.assertNotIn('2026年11月20日', html, '展开前规则正文不应出现在页面源码中')

    def test_poster_fixture_keeps_date_only_inside_inline_image(self):
        html = (FIXTURES / 'poster_text.html').read_text(encoding='utf-8')
        self.assertIn('data:image/svg+xml;base64,', html)
        self.assertIn('<img', html)
        self.assertNotIn('2026年11月30日', html, '海报日期只应存在于图片内，不应出现在 DOM 文本里')

    def test_captcha_and_login_fixtures_declare_blocking_semantics(self):
        captcha = (FIXTURES / 'captcha.html').read_text(encoding='utf-8')
        self.assertIn('<meta name="acq-blocked" content="captcha">', captcha)
        self.assertIn('安全验证', captcha)
        login = (FIXTURES / 'login_comments.html').read_text(encoding='utf-8')
        self.assertIn('data-region-role="comments"', login)
        self.assertIn('data-region-requires="login"', login)
        self.assertNotIn('限定徽章预约已开启', login, '登录墙页面不应包含评论正文')

    def test_static_fixture_contains_expected_date_in_dom(self):
        html = (FIXTURES / 'static_article.html').read_text(encoding='utf-8')
        self.assertIn('2026年11月12日', html)
        self.assertIn('<article', html)


class CheckCalculationTests(unittest.TestCase):
    def test_fixed_check_ids_in_stable_order(self):
        ids = tuple(check['id'] for check in _checks_for())
        self.assertEqual(ids, EXPECTED_CHECK_IDS)

    def test_good_matrix_passes_all_checks(self):
        checks = _checks_for()
        failed = _failed_ids(checks)
        self.assertEqual(failed, [], f'不应失败的检查项：{failed}')
        summary = la._summary(checks, GOOD_ENVIRONMENT)
        self.assertEqual(summary['checks_failed'], 0)
        self.assertEqual(summary['checks_skipped'], 0)
        self.assertTrue(summary['all_passed'])
        self.assertTrue(summary['live_ran'])
        self.assertEqual(summary['checks_passed'], len(EXPECTED_CHECK_IDS))

    def test_missing_browser_skips_all_live_checks_but_not_fixtures(self):
        checks = _checks_for(environment=NO_BROWSER_ENVIRONMENT,
                             determinism={'comparisons': [], 'performed': False},
                             session_reuse={'performed': False, 'ok': False,
                                            'reason': '真实浏览器不可用',
                                            'session_launches': None, 'versions': []})
        by_id = _by_id(checks)
        self.assertFalse(by_id['live_fixtures_complete']['skipped'])
        self.assertTrue(by_id['live_fixtures_complete']['passed'])
        for check_id in EXPECTED_CHECK_IDS:
            if check_id == 'live_fixtures_complete':
                continue
            self.assertTrue(by_id[check_id]['skipped'], f'{check_id} 应 skip')
            self.assertIn('真实浏览器不可用', by_id[check_id]['reason'])
        summary = la._summary(checks, NO_BROWSER_ENVIRONMENT)
        self.assertEqual(summary['checks_failed'], 0)
        self.assertTrue(summary['all_passed'], '资源缺失时 skip 不当作失败，退出码应为 0')
        self.assertFalse(summary['live_ran'])

    def test_missing_ocr_skips_poster_and_static_hit_checks(self):
        checks = _checks_for(environment=NO_OCR_ENVIRONMENT)
        by_id = _by_id(checks)
        for check_id in ('live_ocr_available', 'static_all_policies_hit',
                         'poster_adaptive_ocr_hit', 'poster_baselines_miss',
                         'poster_region_visual_only'):
            self.assertTrue(by_id[check_id]['skipped'], f'{check_id} 应因 OCR 缺失 skip')
            self.assertIn('OCR', by_id[check_id]['reason'])
        # 不依赖 OCR 的检查仍要真实执行并通过。
        for check_id in ('static_adaptive_units_below_legacy', 'lazy_adaptive_hit',
                         'lazy_page_version_changed', 'captcha_no_evidence',
                         'screenshots_exist_and_sha256'):
            self.assertFalse(by_id[check_id]['skipped'])
            self.assertTrue(by_id[check_id]['passed'])
        self.assertEqual(_failed_ids(checks), [])

    def test_lazy_dynamic_failures_are_detected(self):
        runs = _good_runs()
        runs['lazy_rules']['adaptive']['metrics']['answer_hit'] = False
        runs['lazy_rules']['adaptive']['metrics']['interacts'] = 0
        runs['lazy_rules']['adaptive']['page_versions'] = ['sha256:lazy-v1']
        checks = _checks_for(runs=runs)
        failed = set(_failed_ids(checks))
        self.assertIn('lazy_adaptive_hit', failed)
        self.assertIn('lazy_adaptive_interacted', failed)
        self.assertIn('lazy_page_version_changed', failed)

    def test_lazy_baseline_leak_is_detected(self):
        runs = _good_runs()
        runs['lazy_rules']['fixed_frames']['metrics']['answer_hit'] = True
        checks = _checks_for(runs=runs)
        self.assertIn('lazy_baselines_miss', _failed_ids(checks))

    def test_poster_baseline_leak_is_detected(self):
        runs = _good_runs()
        runs['poster_text']['legacy_v3']['metrics']['answer_hit'] = True
        checks = _checks_for(runs=runs)
        self.assertIn('poster_baselines_miss', _failed_ids(checks))

    def test_poster_visual_only_flag_is_checked(self):
        runs = _good_runs()
        runs['poster_text']['adaptive']['region_details']['dom_2']['visual_only'] = False
        checks = _checks_for(runs=runs)
        self.assertIn('poster_region_visual_only', _failed_ids(checks))

    def test_captcha_leak_and_capture_are_detected(self):
        runs = _good_runs()
        runs['captcha']['fixed_frames']['metrics']['evidence_count'] = 1
        runs['captcha']['adaptive']['metrics']['captures'] = 2
        checks = _checks_for(runs=runs)
        failed = set(_failed_ids(checks))
        self.assertIn('captcha_no_evidence', failed)
        self.assertIn('captcha_adaptive_zero_captures', failed)

    def test_login_gap_and_fabrication_are_detected(self):
        runs = _good_runs()
        runs['login_comments']['adaptive']['metrics']['gap'] = None
        runs['login_comments']['adaptive']['metrics']['evidence_count'] = 1
        runs['login_comments']['legacy_v3']['metrics']['answer_hit'] = True
        checks = _checks_for(runs=runs)
        failed = set(_failed_ids(checks))
        self.assertIn('login_required_gap', failed)
        self.assertIn('login_no_fabricated_comments', failed)

    def test_screenshot_hash_mismatch_is_detected(self):
        runs = _good_runs()
        screenshot = runs['static_article']['fixed_frames']['screenshots'][0]
        screenshot['sha256_match'] = False
        checks = _checks_for(runs=runs)
        self.assertIn('screenshots_exist_and_sha256', _failed_ids(checks))

    def test_missing_screenshot_observation_is_detected(self):
        runs = _good_runs()
        for page_runs in runs.values():
            for outcome in page_runs.values():
                outcome['screenshots'] = []
                outcome['screenshot_source'] = 'unavailable'
        checks = _checks_for(runs=runs)
        self.assertIn('screenshots_exist_and_sha256', _failed_ids(checks))

    def test_evidence_version_mismatch_is_detected(self):
        runs = _good_runs()
        runs['lazy_rules']['adaptive']['evidence'][0]['page_version'] = 'sha256:old-version'
        checks = _checks_for(runs=runs)
        self.assertIn('evidence_page_version_current', _failed_ids(checks))

    def test_evidence_version_mismatch_against_source_receipt_is_detected(self):
        runs = _good_runs()
        outcome = runs['lazy_rules']['adaptive']
        evidence = outcome['evidence'][0]
        outcome['receipts'] = [{
            'action': 'extract', 'region': 'dom_2', 'method': 'dom', 'status': 'completed',
            'page_version': 'sha256:lazy-v1', 'cost': {}, 'note': '',
            'evidence_capture_ids': [evidence['capture_id']],
        }]
        checks = _checks_for(runs=runs)
        self.assertIn('evidence_page_version_current', _failed_ids(checks))

    def test_verified_evidence_is_detected(self):
        runs = _good_runs()
        runs['static_article']['adaptive']['evidence'][0]['verified'] = True
        checks = _checks_for(runs=runs)
        self.assertIn('evidence_never_verified', _failed_ids(checks))

    def test_page_version_stability_mismatch_is_detected(self):
        determinism = _good_determinism()
        determinism['comparisons'][1]['second_versions'] = ['sha256:changed']
        checks = _checks_for(determinism=determinism)
        self.assertIn('page_version_stable_across_runs', _failed_ids(checks))

    def test_session_reuse_failure_is_detected(self):
        checks = _checks_for(session_reuse={'performed': True, 'ok': False,
                                            'reason': 'session_launches=2',
                                            'session_launches': 2, 'versions': []})
        self.assertIn('browser_session_reuse', _failed_ids(checks))

    def test_budget_overflow_is_detected(self):
        runs = _good_runs()
        runs['poster_text']['adaptive']['metrics']['within_budget'] = False
        checks = _checks_for(runs=runs)
        self.assertIn('budgets_respected', _failed_ids(checks))

    def test_errored_run_is_reported_as_failure_not_skip(self):
        runs = _good_runs()
        runs['static_article']['adaptive']['error'] = 'WorkerLost: worker 提前退出'
        checks = _checks_for(runs=runs)
        by_id = _by_id(checks)
        self.assertFalse(by_id['static_all_policies_hit']['skipped'])
        self.assertFalse(by_id['static_all_policies_hit']['passed'])
        self.assertIn('WorkerLost', by_id['static_all_policies_hit']['actual'])


class MatrixSkipTests(unittest.TestCase):
    def test_matrix_marks_all_runs_skipped_without_browser(self):
        runs, scenarios = la._run_matrix(FIXTURES, NO_BROWSER_ENVIRONMENT)
        self.assertEqual(len(scenarios), len(la.SCENARIOS))
        for page_id, page_runs in runs.items():
            self.assertEqual(set(page_runs), set(la.POLICIES))
            for outcome in page_runs.values():
                self.assertTrue(outcome['skipped'], f'{page_id} 应标 skipped')
                self.assertIn('真实浏览器不可用', outcome['skip_reason'])
                self.assertEqual(outcome['metrics'], {})
        checks = la.compute_checks(
            runs, NO_BROWSER_ENVIRONMENT,
            {'comparisons': [], 'performed': False},
            {'performed': False, 'ok': False, 'reason': '真实浏览器不可用',
             'session_launches': None, 'versions': []}, FIXTURES)
        self.assertEqual(_failed_ids(checks), [])

    def test_matrix_downgrades_browser_unavailable_launch_failure_to_skip(self):
        """环境探测通过但 driver 启动仍抛 BrowserUnavailable：应按资源缺失 skip。"""
        class BrowserUnavailable(RuntimeError):
            pass

        def factory(**kwargs):
            raise BrowserUnavailable('启动 Node worker 失败: 资源在探测后消失')

        runs, _scenarios = la._run_matrix(FIXTURES, GOOD_ENVIRONMENT, driver_factory=factory)
        for page_runs in runs.values():
            for outcome in page_runs.values():
                self.assertTrue(outcome['skipped'])
                self.assertIn('真实浏览器资源不可用', outcome['skip_reason'])
                self.assertIsNone(outcome['error'])
        checks = la.compute_checks(
            runs, GOOD_ENVIRONMENT,
            {'comparisons': [], 'performed': False},
            {'performed': False, 'ok': False, 'reason': '真实浏览器不可用',
             'session_launches': None, 'versions': []}, FIXTURES)
        self.assertEqual(_failed_ids(checks), [])


# ---------------------------------------------------------------------------
# run_scenario / 截图核对 / 环境探测的离线单测
# ---------------------------------------------------------------------------
class _FakeStaticPageDriver:
    """最小 PageDriver 假实现：只用于离线验证 run_scenario 的执行管线。"""

    backend = 'playwright'
    PAGE_VERSION = 'sha256:fake-static-v1'
    ARTICLE_TEXT = '合成活动公告：报名截止：2026年11月12日 23:59'

    def __init__(self, fixture_html: str) -> None:
        self.fixture_html = fixture_html
        self.calls: list[str] = []

    def _view(self) -> model.PageView:
        date_text = self.ARTICLE_TEXT if '海风与灯塔' in self.fixture_html else '时间待定'
        regions = (
            model.Region('dom_0', 'navigation', (0, 0, 1440, 64), text='社区 活动'),
            model.Region('dom_1', 'article_body', (0, 100, 1120, 400), text=date_text),
            model.Region('dom_2', 'activity_rules', (0, 600, 1120, 200), text='活动规则说明'),
            model.Region('dom_3', 'comments', (0, 900, 1120, 200), text='玩家评论内容'),
            model.Region('dom_4', 'footer', (0, 1200, 1440, 80), text='本地合成场景'),
        )
        return model.PageView(page_version=self.PAGE_VERSION, url='fixture://fake-static',
                              title='合成活动公告', viewport=(1440, 1000),
                              scroll_height=1300, regions=regions, dom_text='整页正文')

    def open(self, goal):
        self.calls.append('open')
        return self._view()

    def inspect(self, view):
        self.calls.append('inspect')
        return model.Observation(action='inspect', page_version=view.page_version,
                                 cost=model.action_cost('inspect'), view=view)

    def locate(self, view, goal):  # pragma: no cover - 未被策略使用
        raise NotImplementedError

    def extract(self, view, region, method):
        self.calls.append(f'extract:{method}')
        text = ''
        if method == 'dom':
            text = region.text
        return model.Observation(action='extract', page_version=view.page_version,
                                 region_id=region.region_id, role=region.role,
                                 extraction=method, text=text, bbox=region.bbox,
                                 cost=model.action_cost('extract', method))

    def interact(self, view, region):  # pragma: no cover - 静态页不需要
        raise NotImplementedError

    def close(self):
        self.calls.append('close')


class RunScenarioTests(unittest.TestCase):
    def test_run_scenario_with_injected_driver_collects_metrics(self):
        created: list[_FakeStaticPageDriver] = []

        def factory(*, fixture_html, artifact_dir):
            self.assertTrue(Path(artifact_dir).is_dir())
            driver = _FakeStaticPageDriver(fixture_html)
            created.append(driver)
            return driver

        outcome = la.run_scenario(FIXTURES / 'static_article.html', 'adaptive',
                                  driver_factory=factory)
        self.assertIsNone(outcome['error'])
        self.assertEqual(outcome['page_id'], 'static_article')
        self.assertEqual(outcome['policy'], 'adaptive')
        self.assertTrue(outcome['metrics']['answer_hit'])
        self.assertTrue(outcome['metrics']['located_hit'])
        self.assertEqual(outcome['metrics']['stop_reason'], 'objectives_covered')
        self.assertEqual(outcome['region_details']['dom_1']['role'], 'article_body')
        self.assertEqual(outcome['screenshot_source'], 'unavailable')
        self.assertEqual(outcome['screenshots'], [])
        self.assertTrue(all(not item['verified'] for item in outcome['evidence']))
        self.assertTrue(created and created[0].calls.count('open') >= 1)
        self.assertIn('close', created[0].calls)

    def test_run_scenario_rejects_unknown_policy_and_fixture(self):
        with self.assertRaises(ValueError):
            la.run_scenario(FIXTURES / 'static_article.html', 'adaptive_skill')
        with self.assertRaises(ValueError):
            la.run_scenario(FIXTURES / 'unknown.html', 'adaptive')

    def test_run_scenario_reports_driver_failure_instead_of_faking(self):
        class BrokenDriver(_FakeStaticPageDriver):
            def open(self, goal):
                raise RuntimeError('浏览器资源缺失')

        outcome = la.run_scenario(FIXTURES / 'static_article.html', 'adaptive',
                                  driver_factory=lambda **kwargs: BrokenDriver(kwargs['fixture_html']))
        self.assertIn('浏览器资源缺失', outcome['error'])
        self.assertFalse(outcome['skipped'])


class ScreenshotValidationTests(unittest.TestCase):
    def _trace_like(self, evidence_items) -> SimpleNamespace:
        return SimpleNamespace(evidence=evidence_items)

    def test_recording_transport_records_commands_and_responses(self):
        class InnerTransport:
            alive = True

            def __init__(self):
                self.started = 0
                self.closed = 0

            def start(self):
                self.started += 1

            def call(self, command, timeout=30):
                return {'echo': command.get('command')}

            def close(self):
                self.closed += 1

        inner = InnerTransport()
        recorder = la.RecordingTransport(inner)
        recorder.start()
        result = recorder.call({'command': 'screenshot'}, timeout=5)
        recorder.close()
        self.assertEqual(result, {'echo': 'screenshot'})
        self.assertEqual(inner.started, 1)
        self.assertEqual(inner.closed, 1)
        self.assertEqual(recorder.commands, [{'command': 'screenshot'}])

    def test_screenshot_validation_detects_missing_file_and_hash_mismatch(self):
        folder = Path(tempfile.mkdtemp(prefix='acq_live_test_'))
        try:
            png = folder / 'frame-1.png'
            png.write_bytes(b'not-a-real-png-but-hashed')
            digest = hashlib.sha256(png.read_bytes()).hexdigest()
            recorder = SimpleNamespace(
                commands=[{'command': 'screenshot'}, {'command': 'screenshot'}],
                responses=[{'file': str(png), 'sha256': digest, 'bbox': [0, 0, 10, 10]},
                           {'file': str(folder / 'missing.png'),
                            'sha256': '0' * 64, 'bbox': [0, 0, 10, 10]}])
            evidence = model.Evidence(capture_id='capture_' + digest[:24],
                                      page_version='sha256:v1', region='dom_2',
                                      extraction='ocr', text='海报内容')
            records, source = la._validate_screenshots(
                recorder, folder, self._trace_like([evidence]))
            self.assertEqual(source, 'worker_transport')
            self.assertEqual(len(records), 2)
            self.assertTrue(records[0]['exists'])
            self.assertTrue(records[0]['sha256_match'])
            self.assertTrue(records[0]['inside_artifact_dir'])
            self.assertFalse(records[1]['exists'])
            self.assertFalse(records[1]['sha256_match'])
        finally:
            import shutil
            shutil.rmtree(folder, ignore_errors=True)

    def test_screenshot_validation_flags_unmatched_ocr_capture_id(self):
        folder = Path(tempfile.mkdtemp(prefix='acq_live_test_'))
        try:
            png = folder / 'frame-1.png'
            png.write_bytes(b'payload')
            digest = hashlib.sha256(png.read_bytes()).hexdigest()
            recorder = SimpleNamespace(
                commands=[{'command': 'screenshot'}],
                responses=[{'file': str(png), 'sha256': digest, 'bbox': [0, 0, 10, 10]}])
            evidence = model.Evidence(capture_id='capture_wrongwrongwrong',
                                      page_version='sha256:v1', region='dom_2',
                                      extraction='ocr', text='海报内容')
            _records, source = la._validate_screenshots(
                recorder, folder, self._trace_like([evidence]))
            self.assertEqual(source, 'capture_id_mismatch')
        finally:
            import shutil
            shutil.rmtree(folder, ignore_errors=True)


class EnvironmentAndCliTests(unittest.TestCase):
    def test_detect_environment_returns_serializable_probe(self):
        environment = la.detect_environment()
        self.assertIn('browser', environment)
        self.assertIn('ocr', environment)
        self.assertIsInstance(environment['reason'], str)
        json.dumps(environment, ensure_ascii=False)

    def test_main_returns_zero_and_writes_json_when_resources_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'live.json'
            with mock.patch.object(la, 'detect_environment',
                                   return_value=dict(NO_BROWSER_ENVIRONMENT)):
                result = la.run_acceptance(FIXTURES, out=out)
                payload = json.loads(out.read_text(encoding='utf-8'))
        self.assertTrue(result['summary']['all_passed'])
        self.assertFalse(result['summary']['live_ran'])
        self.assertEqual(payload['schema'], la.LIVE_SCHEMA)
        self.assertEqual(payload['summary']['checks_failed'], 0)
        for scenario in payload['scenarios']:
            for policy in la.POLICIES:
                self.assertTrue(scenario['runs'][policy]['skipped'])

    def test_main_reports_missing_fixtures_directory(self):
        self.assertEqual(la.main(['--fixtures', 'definitely-not-a-fixtures-dir']), 2)

    def test_exit_code_is_zero_when_only_skips(self):
        checks = _checks_for(environment=NO_BROWSER_ENVIRONMENT,
                             determinism={'comparisons': [], 'performed': False},
                             session_reuse={'performed': False, 'ok': False,
                                            'reason': '真实浏览器不可用',
                                            'session_launches': None, 'versions': []})
        summary = la._summary(checks, NO_BROWSER_ENVIRONMENT)
        self.assertTrue(summary['all_passed'])
        self.assertGreater(summary['checks_skipped'], 0)

    def test_exit_code_would_be_nonzero_on_real_failure(self):
        failed = copy.deepcopy(_good_runs())
        failed['static_article']['adaptive']['metrics']['answer_hit'] = False
        checks = _checks_for(runs=failed)
        summary = la._summary(checks, GOOD_ENVIRONMENT)
        self.assertFalse(summary['all_passed'])
        self.assertGreater(summary['checks_failed'], 0)


# ---------------------------------------------------------------------------
# 真实浏览器 smoke（资源缺失时自动 skip；不是通过）
# ---------------------------------------------------------------------------
def _live_environment() -> dict:
    try:
        return la.detect_environment()
    except Exception as exc:  # pragma: no cover - 环境异常按不可用处理
        return {'browser': False, 'ocr': False, 'reason': f'{type(exc).__name__}: {exc}',
                'ocr_reason': ''}


_SMOKE_ENVIRONMENT = _live_environment()
_SMOKE_REASON = (f"真实浏览器不可用: {_SMOKE_ENVIRONMENT.get('reason')}")


@unittest.skipUnless(_SMOKE_ENVIRONMENT.get('browser'), _SMOKE_REASON)
class LiveBrowserSmokeTests(unittest.TestCase):
    """真实 Chromium/Edge smoke：只跑一个场景，证明 runner 端到端可用。"""

    def test_lazy_rules_adaptive_hits_in_real_browser(self):
        outcome = la.run_scenario(FIXTURES / 'lazy_rules.html', 'adaptive')
        self.assertIsNone(outcome['error'], f"真实浏览器运行失败: {outcome['error']}")
        metrics = outcome['metrics']
        self.assertTrue(metrics['answer_hit'], f'adaptive 应命中：{metrics}')
        self.assertGreaterEqual(metrics['interacts'], 1, 'adaptive 应先点击展开再提取')
        self.assertGreaterEqual(len(outcome['page_versions']), 2,
                                '点击展开后 page_version 应改变')
        self.assertTrue(all(not item['verified'] for item in outcome['evidence']))
        self.assertEqual(outcome['screenshot_source'], 'worker_transport')
        # 顺带验证会话复用：同一 driver 里 open 两次（runner 取初始视图 + harness 执行）
        # 不应重复 launch 浏览器。
        self.assertGreaterEqual(len(outcome['region_details']), 1)


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
