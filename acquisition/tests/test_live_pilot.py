"""``acquisition.live_pilot`` 测试：离线 runner 逻辑 + 真实外部试点 smoke。

分两层：

- **离线**：页面清单完整性与校验、证据文本截断、报告结构、失败保留（加载失败 /
  阻塞 / 无区域 / 无证据 / 预算耗尽）、跳过逻辑、CLI 参数与退出码。离线用例注入
  测试替身驱动验证 runner 逻辑，报告会标 ``synthetic=true``，**不冒充真实试点结果**。
- **真实 smoke**：node + Playwright + Edge 且出网隔离 API（``allow_network`` /
  ``egress_allowlist`` / ``egress_records`` / ``EgressProxy``）就绪时，只跑 1 页
  （固定清单第 1 页）并断言报告结构；资源缺失或 API 未就绪时自动 skip
  （skip 不是通过）。真实试点本身由 ``python -m acquisition.live_pilot`` 执行。
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from acquisition import live_driver, live_pilot, model

PAGES_FILE = live_pilot.DEFAULT_PAGES

#: 运行前固定的报告字段（改动必须是有意的版本变更）。
EXPECTED_RUN_KEYS = {
    'page_id', 'policy', 'skip', 'skip_reason', 'executed', 'failure_stage', 'error',
    'status', 'stop_reason', 'gap', 'page_version', 'page_versions', 'load', 'regions',
    'receipts', 'receipt_status_counts', 'evidence', 'evidence_count', 'cost', 'steps',
    'egress', 'launch', 'goal', 'artifacts',
}
EXPECTED_MANUAL_REVIEW_IDS = {
    'target_semantic_region_read', 'evidence_locatable', 'blocking_judgement_correct',
}
EXPECTED_MECHANICAL_CHECK_IDS = {
    'pages_retained', 'single_run_per_page_policy', 'egress_records_available',
    'egress_allowlist_respected', 'evidence_text_truncated', 'evidence_never_verified',
    'manual_review_listed',
}

READY_ENVIRONMENT = {
    'browser': True, 'browser_reason': 'ok',
    'ocr': True, 'ocr_reason': '',
    'egress_api': True, 'egress_reason': 'ok',
    'ready': True, 'driver_source': 'acquisition.live_driver',
}
NO_BROWSER_ENVIRONMENT = {
    'browser': False, 'browser_reason': 'node_missing',
    'ocr': False, 'ocr_reason': '真实浏览器不可用，未探测 OCR',
    'egress_api': True, 'egress_reason': 'ok',
    'ready': False, 'driver_source': 'acquisition.live_driver',
}
NO_EGRESS_ENVIRONMENT = {
    'browser': True, 'browser_reason': 'ok',
    'ocr': True, 'ocr_reason': '',
    'egress_api': False, 'egress_reason': 'driver_missing_params:egress_allowlist',
    'ready': False, 'driver_source': 'acquisition.live_driver',
}


# ---------------------------------------------------------------------------
# 测试替身（只用于离线 runner 逻辑；报告会标 synthetic=true）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FakeConnectionRecord:
    """与 ``EgressProxy.ConnectionRecord`` 同形的测试替身记录。"""

    host: str
    port: int = 443
    resolved_ips: tuple[str, ...] = ('93.184.216.34',)
    decision: str = 'allowed'
    reason: str = 'allowed'
    bytes_in: int = 1024
    bytes_out: int = 256
    at: str = '2026-11-13T00:00:00+08:00'


class FakeDriver:
    """脚本化 PageDriver：按页面行为返回合成 PageView，不访问任何网络。"""

    backend = 'playwright'

    def __init__(self, behavior: dict, artifact_dir: Path) -> None:
        self.behavior = dict(behavior)
        self.artifact_dir = Path(artifact_dir)
        self.closed = False
        self.session_launches = 1
        self.capture_count = 0
        self.launch_info = {
            'browser': 'FakeBrowser/1.0',
            'fixture_mode': False,
            'allow_url_mode': True,
            'proxy_server': 'http://127.0.0.1:9',
            'webrtc_policy': 'disable_non_proxied_udp',
            'websocket_route_blocked': True,
        }
        self.egress_records = tuple(self.behavior.get('egress_records', ()))

    # -------------------------------------------------------------- PageDriver
    def open(self, goal: model.ResearchGoal) -> model.PageView:
        error = self.behavior.get('open_error')
        if error:
            raise RuntimeError(str(error))
        regions = []
        for item in self.behavior.get('regions', ()):
            regions.append(model.Region(
                region_id=str(item['region_id']),
                role=str(item.get('role', 'article_body')),
                bbox=tuple(item.get('bbox', (10, 10, 400, 200))),
                text=str(item.get('text', '')),
                visual_only=bool(item.get('visual_only', False)),
                lazy=bool(item.get('lazy', False)),
                requires=item.get('requires'),
                blocked=bool(item.get('blocked', False)),
                scroll_y=int(item.get('scroll_y', 10)),
            ))
        return model.PageView(
            page_version=str(self.behavior.get('page_version', 'sha256:' + 'a' * 64)),
            url=str(self.behavior.get('url', goal.task_id)),
            title=str(self.behavior.get('title', 'Fake Page')),
            viewport=(1440, 1000),
            scroll_height=int(self.behavior.get('scroll_height', 1200)),
            regions=tuple(regions),
            dom_text=str(self.behavior.get('dom_text', '')),
            blocked_reason=self.behavior.get('blocked_reason'),
        )

    def inspect(self, view: model.PageView) -> model.Observation:
        return model.Observation(action='inspect', page_version=view.page_version,
                                 cost=model.action_cost('inspect'),
                                 candidates=tuple(region.region_id for region in view.regions),
                                 blocked=bool(view.blocked_reason), view=view)

    def locate(self, view: model.PageView, goal: model.ResearchGoal) -> model.Observation:
        return model.Observation(action='locate', page_version=view.page_version,
                                 cost=model.action_cost('locate'),
                                 candidates=tuple(region.region_id for region in view.regions))

    def extract(self, view: model.PageView, region: model.Region,
                method: str) -> model.Observation:
        if region.region_id == model.DOM_BODY_REGION:
            text = str(self.behavior.get('dom_text', ''))
            role = 'section'
        elif region.region_id.startswith(model.VIEWPORT_REGION_PREFIX):
            text = str(self.behavior.get('viewport_text', ''))
            role = 'section'
        else:
            text = region.text or str(self.behavior.get('extract_text', ''))
            role = region.role
        capture_id = 'capture_' + ('a' * 24 if method == 'ocr' else 'b' * 24)
        return model.Observation(action='extract', page_version=view.page_version,
                                 region_id=region.region_id, role=role, extraction=method,
                                 text=text, capture_id=capture_id, bbox=region.bbox,
                                 cost=model.action_cost('extract', method))

    def interact(self, view: model.PageView, region: model.Region) -> model.Observation:
        raise ValueError('测试替身不支持 interact')

    def close(self) -> None:
        self.closed = True


def fake_factory(behaviors: dict | None = None):
    """构造 ``driver_factory``：按 page_id 取行为，默认返回一个可读的 article_body 区域。"""
    behaviors = dict(behaviors or {})

    def factory(*, page: live_pilot.PilotPage, artifact_dir: Path) -> FakeDriver:
        behavior = dict(behaviors.get(page.page_id, {}))
        behavior.setdefault('url', page.url)
        if 'regions' not in behavior:
            behavior['regions'] = [
                {'region_id': 'main_content', 'role': 'article_body',
                 'text': 'x' * 500, 'bbox': (20, 40, 1200, 600)},
            ]
        return FakeDriver(behavior, artifact_dir)

    return factory


def pilot_pages() -> tuple[live_pilot.PilotPage, ...]:
    return live_pilot.load_pages(PAGES_FILE)


def find_check(report: dict, check_id: str) -> dict:
    for check in report['mechanical_checks']:
        if check['id'] == check_id:
            return check
    raise AssertionError(f'缺少机械检查 {check_id}')


def find_run(report: dict, page_id: str, policy: str = 'adaptive') -> dict:
    for page in report['pages']:
        if page['page_id'] == page_id:
            return page['runs'][policy]
    raise AssertionError(f'缺少页面 {page_id}')


# ---------------------------------------------------------------------------
# 固定页面清单
# ---------------------------------------------------------------------------
class PagesFixtureTests(unittest.TestCase):
    def test_fixture_file_exists(self):
        self.assertTrue(PAGES_FILE.is_file(), f'缺少固定页面清单: {PAGES_FILE}')

    def test_fixture_has_three_public_pages(self):
        pages = pilot_pages()
        self.assertEqual(len(pages), 3)
        self.assertEqual(
            {page.url for page in pages},
            {'https://example.com/', 'https://www.iana.org/help/example-domains',
             'https://www.rfc-editor.org/rfc/rfc2606.html'})

    def test_page_ids_unique(self):
        pages = pilot_pages()
        ids = [page.page_id for page in pages]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_page_has_required_fields(self):
        for page in pilot_pages():
            self.assertTrue(page.page_id)
            self.assertTrue(page.url.startswith('https://'))
            self.assertTrue(page.hosts)
            self.assertTrue(page.objective_question)
            self.assertIn(page.answer_kind, live_pilot.ANSWER_KINDS)
            self.assertTrue(page.note, f'{page.page_id} 缺少 note（可达性/失败样本说明）')

    def test_host_covers_url_and_is_allowlist(self):
        for page in pilot_pages():
            hostname = page.url.split('://', 1)[1].split('/', 1)[0].lower()
            self.assertTrue(
                any(hostname == host or hostname.endswith('.' + host) for host in page.hosts),
                f'{page.page_id} 的 host {page.hosts} 未覆盖 url hostname {hostname}')

    def test_no_taptap_or_internal_hosts(self):
        for page in pilot_pages():
            for host in page.hosts:
                self.assertNotIn('taptap', host.lower())
                self.assertFalse(host.endswith('.local'))
                self.assertFalse(host.endswith('.internal'))

    def test_region_hints_are_known_roles(self):
        for page in pilot_pages():
            for hint in page.region_hints:
                self.assertIn(hint, model.REGION_ROLES)

    def test_goal_built_from_page_has_no_expected_answer(self):
        page = pilot_pages()[0]
        goal = live_pilot.build_goal(page)
        self.assertEqual(goal.allowed_hosts, page.hosts)
        self.assertEqual(len(goal.objectives), 1)
        objective = goal.objectives[0]
        self.assertEqual(objective.question, page.objective_question)
        self.assertEqual(objective.region_hints, page.region_hints)
        # ResearchGoal/Objective 不承载任何预期答案字段（真值只允许在评测侧）。
        self.assertFalse(hasattr(objective, 'expected_answer'))
        self.assertFalse(hasattr(goal, 'expected_answer'))


class PageValidationTests(unittest.TestCase):
    def _write(self, folder: Path, payload) -> Path:
        path = folder / 'pages.json'
        if isinstance(payload, str):
            path.write_text(payload, encoding='utf-8')
        else:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return path

    @staticmethod
    def _page(**overrides) -> dict:
        page = {
            'page_id': 'example_com_root',
            'url': 'https://example.com/',
            'host': 'example.com',
            'objective_question': '页面标题是什么？',
            'answer_kind': 'text',
            'region_hints': [],
            'note': '测试样本',
        }
        page.update(overrides)
        return page

    def _assert_invalid(self, payload) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(Path(folder), payload)
            with self.assertRaises(ValueError):
                live_pilot.load_pages(path)

    def test_valid_page_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(Path(folder), {'pages': [self._page()]})
            pages = live_pilot.load_pages(path)
            self.assertEqual(len(pages), 1)
            self.assertEqual(pages[0].hosts, ('example.com',))

    def test_host_list_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._write(Path(folder), {'pages': [
                self._page(hosts=['example.com', 'www.example.com'])]})
            self.assertEqual(live_pilot.load_pages(path)[0].hosts,
                             ('example.com', 'www.example.com'))

    def test_duplicate_page_id_rejected(self):
        self._assert_invalid({'pages': [self._page(), self._page()]})

    def test_non_https_url_rejected(self):
        self._assert_invalid({'pages': [self._page(url='http://example.com/')]})

    def test_host_not_covering_url_rejected(self):
        self._assert_invalid({'pages': [self._page(host='www.iana.org')]})

    def test_taptap_host_rejected(self):
        self._assert_invalid({'pages': [self._page(url='https://www.taptap.cn/example',
                                                   host='www.taptap.cn')]})

    def test_private_host_rejected(self):
        self._assert_invalid({'pages': [self._page(url='https://127.0.0.1/', host='127.0.0.1')]})
        self._assert_invalid({'pages': [self._page(url='https://localhost/', host='localhost')]})

    def test_missing_field_rejected(self):
        page = self._page()
        page.pop('objective_question')
        self._assert_invalid({'pages': [page]})

    def test_unknown_answer_kind_rejected(self):
        self._assert_invalid({'pages': [self._page(answer_kind='image')]})

    def test_unknown_region_hint_rejected(self):
        self._assert_invalid({'pages': [self._page(region_hints=['not_a_role'])]})

    def test_empty_pages_rejected(self):
        self._assert_invalid({'pages': []})

    def test_malformed_json_rejected(self):
        self._assert_invalid('{not json')

    def test_top_level_array_rejected(self):
        self._assert_invalid([])


# ---------------------------------------------------------------------------
# 证据文本截断
# ---------------------------------------------------------------------------
class TruncateTests(unittest.TestCase):
    def test_short_text_unchanged(self):
        self.assertEqual(live_pilot.truncate_text('abc'), 'abc')
        self.assertEqual(live_pilot.truncate_text(''), '')

    def test_exact_limit_unchanged(self):
        text = 'a' * live_pilot.EVIDENCE_TEXT_LIMIT
        self.assertEqual(live_pilot.truncate_text(text), text)

    def test_long_ascii_truncated_to_200(self):
        out = live_pilot.truncate_text('x' * 500)
        self.assertEqual(len(out), live_pilot.EVIDENCE_TEXT_LIMIT)
        self.assertEqual(live_pilot.EVIDENCE_TEXT_LIMIT, 200)

    def test_long_cjk_truncated_to_200(self):
        out = live_pilot.truncate_text('中' * 300)
        self.assertEqual(len(out), live_pilot.EVIDENCE_TEXT_LIMIT)

    def test_invalid_arguments_rejected(self):
        with self.assertRaises(TypeError):
            live_pilot.truncate_text(123)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            live_pilot.truncate_text('abc', limit=-1)
        with self.assertRaises(ValueError):
            live_pilot.truncate_text('abc', limit=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 报告结构（离线，注入测试替身）
# ---------------------------------------------------------------------------
class ReportStructureTests(unittest.TestCase):
    def _run(self, *, behaviors=None, include_baseline=False, budgets=None,
             environment=None, out_name='pilot.json'):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        out = folder / out_name
        report = live_pilot.run_pilot(
            PAGES_FILE, environment=environment or READY_ENVIRONMENT,
            driver_factory=fake_factory(behaviors), artifacts_dir=folder / 'artifacts',
            out=out, include_baseline=include_baseline, budgets=budgets,
            print_report=False)
        json_data = json.loads(out.read_text(encoding='utf-8'))
        markdown = out.with_suffix('.md').read_text(encoding='utf-8')
        return report, json_data, markdown

    def test_report_top_level_structure(self):
        report, json_data, _ = self._run()
        self.assertEqual(report['schema'], live_pilot.PILOT_SCHEMA)
        self.assertEqual(report['contract'], model.SCHEMA_VERSION)
        self.assertTrue(report['executed'])
        self.assertTrue(report['synthetic'], '注入测试替身时报告必须标 synthetic=true')
        self.assertEqual(report['environment']['driver_source'], 'injected_test_double')
        self.assertEqual(report['policies'], ['adaptive'])
        self.assertEqual(len(report['pages']), 3)
        self.assertEqual(report['pages_file'], str(PAGES_FILE))
        self.assertEqual(report['conclusion_scope'], live_pilot.CONCLUSION_SCOPE)
        self.assertIn('outputs', report)
        self.assertEqual(json_data['schema'], live_pilot.PILOT_SCHEMA)
        self.assertEqual(len(json_data['pages']), 3)

    def test_page_and_run_structure(self):
        report, _, _ = self._run()
        page = report['pages'][0]
        self.assertEqual(set(page), {'page_id', 'url', 'hosts', 'objective_question',
                                     'answer_kind', 'region_hints', 'note', 'runs',
                                     'failures', 'manual_review'})
        run = page['runs']['adaptive']
        self.assertEqual(set(run), EXPECTED_RUN_KEYS)
        self.assertTrue(run['executed'])
        self.assertIsNone(run['failure_stage'])
        self.assertEqual(run['load']['status'], 'loaded')
        self.assertEqual(run['regions']['count'], 1)
        self.assertEqual(run['regions']['roles'], ['article_body'])
        self.assertTrue(run['page_version'].startswith('sha256:'))
        self.assertEqual(run['stop_reason'], 'objectives_covered')
        self.assertTrue(run['launch']['egress_proxy_configured'])
        self.assertEqual(run['goal']['egress_allowlist'], list(page['hosts']))

    def test_evidence_text_truncated_to_200(self):
        report, _, _ = self._run()
        run = find_run(report, report['pages'][0]['page_id'])
        self.assertEqual(len(run['evidence']), 1)
        evidence = run['evidence'][0]
        self.assertEqual(len(evidence['text']), 200)
        self.assertEqual(evidence['text_length'], 500)
        self.assertTrue(evidence['text_truncated'])
        self.assertFalse(evidence['verified'])
        self.assertTrue(evidence['locator_complete'])
        self.assertTrue(find_check(report, 'evidence_text_truncated')['passed'])
        self.assertTrue(find_check(report, 'evidence_never_verified')['passed'])

    def test_manual_review_items_have_empty_human_verdict(self):
        report, _, markdown = self._run()
        for page in report['pages']:
            ids = {item['item'] for item in page['manual_review']}
            self.assertEqual(ids, EXPECTED_MANUAL_REVIEW_IDS)
            for item in page['manual_review']:
                self.assertIsNone(item['human_verdict'])
                self.assertIn('machine_observation', item)
        self.assertEqual({item['item'] for item in report['manual_review_items']},
                         EXPECTED_MANUAL_REVIEW_IDS)
        self.assertIn('人工核查', markdown)

    def test_mechanical_checks_listed(self):
        report, _, _ = self._run()
        self.assertEqual({check['id'] for check in report['mechanical_checks']},
                         EXPECTED_MECHANICAL_CHECK_IDS)
        self.assertTrue(find_check(report, 'pages_retained')['passed'])
        self.assertTrue(find_check(report, 'single_run_per_page_policy')['passed'])
        self.assertTrue(find_check(report, 'egress_records_available')['passed'])
        self.assertTrue(find_check(report, 'manual_review_listed')['passed'])

    def test_conclusion_scope_limited(self):
        report, _, _ = self._run()
        conclusion = report['conclusion']
        self.assertIn(live_pilot.CONCLUSION_SCOPE, conclusion)
        self.assertIn('不提供采集成功率', conclusion)
        self.assertIn('synthetic=true', conclusion)
        self.assertTrue(report['limitations'])

    def test_include_baseline_adds_contrast_runs(self):
        report, _, _ = self._run(include_baseline=True)
        self.assertEqual(report['policies'], ['adaptive', 'fixed_frames', 'legacy_v3'])
        for page in report['pages']:
            self.assertEqual(set(page['runs']),
                             {'adaptive', 'fixed_frames', 'legacy_v3'})
        self.assertTrue(find_check(report, 'single_run_per_page_policy')['passed'])

    def test_markdown_contains_failures_and_egress_sections(self):
        report, _, markdown = self._run()
        self.assertIn('# V4 切片 3 外部试点报告', markdown)
        self.assertIn('保留的失败', markdown)
        self.assertIn('egress 连接记录摘要', markdown)
        self.assertIn('机械检查', markdown)
        for page in report['pages']:
            self.assertIn(page['page_id'], markdown)


# ---------------------------------------------------------------------------
# egress 记录摘要与越界检查
# ---------------------------------------------------------------------------
class EgressSummaryTests(unittest.TestCase):
    def _run_with_egress(self, records_by_page):
        behaviors = {page_id: {'egress_records': records}
                     for page_id, records in records_by_page.items()}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        return live_pilot.run_pilot(
            PAGES_FILE, environment=READY_ENVIRONMENT,
            driver_factory=fake_factory(behaviors), artifacts_dir=folder / 'artifacts',
            print_report=False)

    def test_allowed_and_denied_recorded(self):
        report = self._run_with_egress({
            'example_com_root': (
                FakeConnectionRecord(host='example.com'),
                FakeConnectionRecord(host='tracker.example.net', decision='denied',
                                     reason='host_not_allowed', bytes_in=0, bytes_out=0),
            ),
        })
        run = find_run(report, 'example_com_root')
        self.assertTrue(run['egress']['available'])
        self.assertEqual(run['egress']['allowed'], 1)
        self.assertEqual(run['egress']['denied'], 1)
        self.assertEqual(run['egress']['records_total'], 2)
        denied_hosts = [item['host'] for item in run['egress']['records']
                        if item['decision'] == 'denied']
        self.assertEqual(denied_hosts, ['tracker.example.net'])
        kinds = report['summary']['failure_kinds']
        self.assertIn('egress_denied', kinds)
        self.assertTrue(any(failure['kind'] == 'egress_denied'
                            for failure in report['failures']))
        self.assertTrue(find_check(report, 'egress_allowlist_respected')['passed'])

    def test_allowlist_violation_detected(self):
        report = self._run_with_egress({
            'example_com_root': (
                FakeConnectionRecord(host='not-allowed.example.net', decision='allowed'),
            ),
        })
        run = find_run(report, 'example_com_root')
        self.assertEqual(len(run['egress']['allowlist_violations']), 1)
        check = find_check(report, 'egress_allowlist_respected')
        self.assertFalse(check['passed'])
        self.assertIn('not-allowed.example.net', check['actual'])

    def test_driver_without_egress_records_marked_unavailable(self):
        class NoEgressDriver(FakeDriver):
            def __init__(self, behavior, artifact_dir):
                super().__init__(behavior, artifact_dir)
                del self.egress_records

        def factory(*, page, artifact_dir):
            behavior = {'url': page.url,
                        'regions': [{'region_id': 'main', 'role': 'article_body',
                                     'text': 'x' * 20}]}
            return NoEgressDriver(behavior, artifact_dir)

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        report = live_pilot.run_pilot(
            PAGES_FILE, environment=READY_ENVIRONMENT, driver_factory=factory,
            artifacts_dir=folder / 'artifacts', print_report=False)
        run = find_run(report, 'example_com_root')
        self.assertFalse(run['egress']['available'])
        self.assertEqual(run['egress']['reason'], 'driver_missing_egress_records')
        check = find_check(report, 'egress_records_available')
        self.assertFalse(check['passed'])


# ---------------------------------------------------------------------------
# 失败保留（加载失败 / 阻塞 / 无区域 / 无证据 / 预算耗尽）
# ---------------------------------------------------------------------------
class FailurePreservationTests(unittest.TestCase):
    def _run(self, behaviors, *, budgets=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)
        return live_pilot.run_pilot(
            PAGES_FILE, environment=READY_ENVIRONMENT,
            driver_factory=fake_factory(behaviors), artifacts_dir=folder / 'artifacts',
            budgets=budgets, print_report=False)

    def test_load_failure_blocked_page_and_no_region_all_retained(self):
        report = self._run({
            'example_com_root': {'open_error': 'fake open boom'},
            'iana_example_domains': {'blocked_reason': 'captcha', 'regions': []},
            'rfc2606_reserved_names': {'regions': [], 'dom_text': '', 'scroll_height': 0,
                                       'viewport_text': ''},
        })
        self.assertTrue(report['executed'])
        self.assertEqual(len(report['pages']), 3)
        kinds = report['summary']['failure_kinds']
        for kind in ('load_failed', 'blocked_page', 'no_regions', 'no_evidence'):
            self.assertIn(kind, kinds, f'失败类型 {kind} 未被保留')

        open_failed = find_run(report, 'example_com_root')
        self.assertEqual(open_failed['load']['status'], 'open_failed')
        self.assertEqual(open_failed['stop_reason'], 'open_failed')
        self.assertEqual(open_failed['status'], 'failed')
        self.assertEqual(open_failed['failure_stage'], None)  # harness 已落 open_failed 收据

        blocked = find_run(report, 'iana_example_domains')
        self.assertEqual(blocked['load']['status'], 'blocked:captcha')
        self.assertEqual(blocked['stop_reason'], 'blocked:captcha')
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['evidence_count'], 0)

        no_region = find_run(report, 'rfc2606_reserved_names')
        self.assertEqual(no_region['load']['status'], 'empty_page')
        self.assertEqual(no_region['regions']['count'], 0)
        self.assertEqual(no_region['evidence_count'], 0)
        self.assertEqual(no_region['gap'], 'objectives_uncovered')

        page_ids_with_failures = {failure['page_id'] for failure in report['failures']}
        self.assertEqual(page_ids_with_failures,
                         {'example_com_root', 'iana_example_domains',
                          'rfc2606_reserved_names'})
        # 不重试到成功：每页每策略只有 1 次运行记录。
        for page in report['pages']:
            self.assertEqual(len(page['runs']), 1)
        self.assertTrue(find_check(report, 'single_run_per_page_policy')['passed'])

    def test_budget_exhausted_retained(self):
        budgets = model.Budget(max_units=1, max_captures=8, max_ocr_calls=8,
                               max_vlm_calls=0, max_steps=30)
        report = self._run({}, budgets=budgets)
        run = find_run(report, 'example_com_root')
        self.assertEqual(run['stop_reason'], 'budget_exhausted')
        self.assertEqual(run['gap'], 'budget_exhausted')
        self.assertEqual(run['cost']['units'], 1)
        self.assertEqual(run['evidence_count'], 0)
        self.assertIn('budget_exhausted', report['summary']['failure_kinds'])
        self.assertTrue(any(failure['kind'] == 'budget_exhausted'
                            for failure in report['failures']))

    def test_failures_not_dropped_when_one_page_breaks(self):
        report = self._run({
            'example_com_root': {'open_error': 'boom'},
        })
        self.assertEqual(len(report['pages']), 3)
        self.assertGreaterEqual(report['summary']['failures_total'], 1)
        self.assertTrue(all(page['runs'] for page in report['pages']))


# ---------------------------------------------------------------------------
# 跳过逻辑（资源 / 出网隔离 API 未就绪）
# ---------------------------------------------------------------------------
class SkipLogicTests(unittest.TestCase):
    def _run(self, environment):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name)

        def forbidden_factory(**kwargs):
            raise AssertionError('未就绪时不得构造 driver')

        report = live_pilot.run_pilot(
            PAGES_FILE, environment=environment, driver_factory=forbidden_factory,
            artifacts_dir=folder / 'artifacts', print_report=False)
        return report, folder

    def test_no_browser_skips_without_driver(self):
        report, folder = self._run(NO_BROWSER_ENVIRONMENT)
        self.assertFalse(report['executed'])
        self.assertFalse((folder / 'artifacts').exists(), '未就绪时不应创建 artifacts 目录')
        self.assertEqual(report['blockers'][0]['stage'], 'environment')
        for page in report['pages']:
            for run in page['runs'].values():
                self.assertTrue(run['skip'])
                self.assertIn('node_missing', run['skip_reason'])
                self.assertEqual(run['load']['status'], 'not_attempted')
                self.assertFalse(run['executed'])
        self.assertIn('not_executed', report['summary']['failure_kinds'])
        self.assertIn('未执行', report['conclusion'])
        for check in report['mechanical_checks']:
            if check['id'] in ('pages_retained', 'single_run_per_page_policy',
                               'egress_records_available', 'evidence_text_truncated'):
                self.assertTrue(check['skipped'], f'{check["id"]} 未执行时应 skip')

    def test_egress_api_not_ready_blocks_pilot(self):
        report, _ = self._run(NO_EGRESS_ENVIRONMENT)
        self.assertFalse(report['executed'])
        stages = [item['stage'] for item in report['blockers']]
        self.assertEqual(stages, ['egress_api_not_ready'])
        self.assertIn('egress_allowlist', report['blockers'][0]['reason'])
        run = find_run(report, 'example_com_root')
        self.assertEqual(run['failure_stage'], 'egress_api_not_ready')

    def test_egress_api_status_returns_tuple(self):
        available, reason = live_pilot.egress_api_status()
        self.assertIsInstance(available, bool)
        self.assertIsInstance(reason, str)

    def test_detect_environment_shape(self):
        environment = live_pilot.detect_environment()
        for key in ('browser', 'browser_reason', 'ocr', 'ocr_reason',
                    'egress_api', 'egress_reason', 'ready', 'driver_source'):
            self.assertIn(key, environment)
        self.assertIsInstance(environment['ready'], bool)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
class CliTests(unittest.TestCase):
    def test_parser_defaults(self):
        args = live_pilot.build_parser().parse_args([])
        self.assertEqual(args.pages, str(live_pilot.DEFAULT_PAGES))
        self.assertIsNone(args.out)
        self.assertFalse(args.include_baseline)
        self.assertIsNone(args.artifacts_dir)
        self.assertEqual(args.timeout, 60.0)

    def test_parser_flags(self):
        args = live_pilot.build_parser().parse_args([
            '--pages', 'x.json', '--out', 'o.json', '--include-baseline',
            '--artifacts-dir', 'a', '--timeout', '5'])
        self.assertEqual(args.pages, 'x.json')
        self.assertEqual(args.out, 'o.json')
        self.assertTrue(args.include_baseline)
        self.assertEqual(args.artifacts_dir, 'a')
        self.assertEqual(args.timeout, 5.0)

    def test_main_missing_pages_file_returns_config_error(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            missing = Path(folder) / 'missing.json'
            self.assertEqual(live_pilot.main(['--pages', str(missing)]),
                             live_pilot.EXIT_CONFIG_ERROR)

    def test_main_invalid_pages_returns_config_error(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            path = Path(folder) / 'pages.json'
            path.write_text(json.dumps({'pages': [{'page_id': 'x'}]}), encoding='utf-8')
            self.assertEqual(live_pilot.main(['--pages', str(path)]),
                             live_pilot.EXIT_CONFIG_ERROR)

    def test_main_not_ready_returns_not_executed_and_writes_report(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            out = Path(folder) / 'pilot.json'
            with mock.patch.object(live_pilot, 'detect_environment',
                                   return_value=dict(NO_BROWSER_ENVIRONMENT)):
                code = live_pilot.main(['--pages', str(PAGES_FILE), '--out', str(out)])
            self.assertEqual(code, live_pilot.EXIT_NOT_EXECUTED)
            self.assertTrue(out.is_file())
            self.assertTrue(out.with_suffix('.md').is_file())
            data = json.loads(out.read_text(encoding='utf-8'))
            self.assertFalse(data['executed'])

    def test_output_paths(self):
        json_path, markdown_path = live_pilot.output_paths(Path('reports/pilot.json'))
        self.assertEqual(json_path, Path('reports/pilot.json'))
        self.assertEqual(markdown_path, Path('reports/pilot.md'))
        json_path, markdown_path = live_pilot.output_paths(Path('reports/pilot'))
        self.assertEqual(json_path, Path('reports/pilot.json'))
        self.assertEqual(markdown_path, Path('reports/pilot.md'))


# ---------------------------------------------------------------------------
# 真实外部试点 smoke（资源 + 出网隔离 API 就绪才跑，只跑 1 页）
# ---------------------------------------------------------------------------
_REAL_BROWSER_OK, _REAL_BROWSER_REASON = live_driver.available()
_EGRESS_API_OK, _EGRESS_API_REASON = live_pilot.egress_api_status()


class RealPilotSmokeTests(unittest.TestCase):
    """真实浏览器 + 出网隔离 API 就绪时跑 1 页；否则 skip（skip 不是通过）。"""

    @classmethod
    def setUpClass(cls):
        if not _REAL_BROWSER_OK:
            raise unittest.SkipTest(f'真实浏览器资源不可用：{_REAL_BROWSER_REASON}')
        if not _EGRESS_API_OK:
            raise unittest.SkipTest(f'出网隔离 API 未就绪：{_EGRESS_API_REASON}')

    def test_single_page_real_run_report_structure(self):
        pages = live_pilot.load_pages(PAGES_FILE)
        page = pages[0]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            out = root / 'real-smoke.json'
            report = live_pilot.run_pilot((page,), out=out,
                                          artifacts_dir=root / 'artifacts',
                                          timeout=90.0, print_report=False)
            self.assertTrue(report['executed'])
            self.assertFalse(report['synthetic'])
            self.assertEqual(len(report['pages']), 1)
            run = report['pages'][0]['runs']['adaptive']
            self.assertTrue(run['executed'])
            self.assertIsNone(run['failure_stage'])
            self.assertTrue(run['load']['status'])
            self.assertTrue(run['page_version'].startswith('sha256:'))
            for evidence in run['evidence']:
                self.assertLessEqual(len(evidence['text']),
                                     live_pilot.EVIDENCE_TEXT_LIMIT)
                self.assertFalse(evidence['verified'])
            self.assertIn('available', run['egress'])
            self.assertTrue(out.is_file())
            self.assertTrue(out.with_suffix('.md').is_file())
            data = json.loads(out.read_text(encoding='utf-8'))
            self.assertEqual(data['schema'], live_pilot.PILOT_SCHEMA)
            self.assertFalse(data['synthetic'])


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
