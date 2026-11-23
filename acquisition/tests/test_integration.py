"""纵向集成验收：从冻结样本到收据、证据、恢复与评测的端到端检查。

本文件由 Lead 独立编写，不复用各模块自测的断言，检查的是跨模块行为：

- 自适应策略在懒加载样本上获得可定位证据，且证据核验状态仍为未核验；
- 阻塞页面上所有策略的证据泄漏为 0；
- 现状 V3 基线在静态页命中、在懒加载页漏读，与离线回放语义一致；
- 中断后恢复不重复执行已完成动作，页面版本变化会被判 stale；
- Skill 完成验证通过不改变证据的事实核验状态；
- Trace 可序列化并包含完整契约字段。

这些检查只证明机制在合成冻结样本上的行为，不证明真实网页采集质量或事实核验。
"""
import json
import tempfile
import unittest
from pathlib import Path

from acquisition import model
from acquisition.driver import FixtureDriver
from acquisition.harness import AcquisitionHarness
from acquisition.policy import AdaptiveObservationPolicy, FixedFramePolicy, LegacyV3Policy
from acquisition.store import AcquisitionStore

PAGES = Path(__file__).resolve().parents[1] / 'fixtures' / 'pages'
SKILLS = Path(__file__).resolve().parents[1] / 'fixtures' / 'skills'


def load_page(page_id):
    path = PAGES / f'{page_id}.json'
    return json.loads(path.read_text(encoding='utf-8'))


def goal_for(page, *, task_id=None):
    objectives = tuple(model.Objective(
        objective_id=item['objective_id'],
        question=item['question'],
        answer_kind=item['answer_kind'],
        region_hints=tuple(item['region_hints']),
        required=bool(item['required']),
    ) for item in page['objectives'])
    return model.ResearchGoal(task_id or page['page_id'], page['title'], objectives,
                              allowed_hosts=('www.taptap.cn',))


def evidence_text(trace):
    return '\n'.join(item.text for item in trace.evidence)


class VerticalSliceTests(unittest.TestCase):
    def setUp(self):
        self._folders = []
        self._stores = []
        self._drivers = []

    def tearDown(self):
        for driver in self._drivers:
            try:
                driver.close()
            except RuntimeError:
                pass
        for store in self._stores:
            try:
                store.close()
            except RuntimeError:
                pass
        for folder in self._folders:
            try:
                folder.cleanup()
            except OSError:
                pass

    def store_path(self):
        folder = tempfile.TemporaryDirectory()
        self._folders.append(folder)
        return str(Path(folder.name) / 'acquisition.sqlite3')

    def store(self):
        store = AcquisitionStore(self.store_path())
        self._stores.append(store)
        return store

    def driver_for(self, page_id):
        driver = FixtureDriver.from_file(PAGES / f'{page_id}.json')
        self._drivers.append(driver)
        return driver

    def run_page(self, page_id, policy, *, budgets=None, store=None, resume=False, hint_provider=None):
        page = load_page(page_id)
        goal = goal_for(page)
        driver = self.driver_for(page_id)
        store = store or self.store()
        harness = AcquisitionHarness(driver, policy, store, budgets=budgets)
        trace = harness.run(goal, resume=resume)
        return trace, store, driver, page, goal

    # ------------------------------------------------------------------ 核心路径
    def test_lazy_page_end_to_end_produces_unverified_located_evidence(self):
        trace, store, _driver, page, goal = self.run_page('activity_rules_lazy', AdaptiveObservationPolicy())
        self.assertEqual(trace.status, 'succeeded')
        self.assertEqual(trace.stop_reason, 'objectives_covered')
        self.assertTrue(trace.evidence, '自适应策略应取得规则区证据')
        self.assertIn('2026年11月20日', evidence_text(trace))
        self.assertTrue(all(item.verified is False for item in trace.evidence),
                        '提取成功不得被当作事实已核验')
        self.assertTrue(any(item.region == 'rules' for item in trace.evidence),
                        '证据应绑定语义区域，而不是整屏截图')
        self.assertEqual(trace.receipts[0].action, 'inspect')
        self.assertLessEqual(trace.cost.units, model.Budget().max_units)
        self.assertIn('interact', [item.action for item in trace.receipts])
        self.assertGreaterEqual(len(trace.page_versions), 2)
        self.assertIn(page['objectives'][0]['expected_answer'], evidence_text(trace))
        # store 侧可离线复盘同一次任务，且证据归属该任务。
        stored = store.trace(trace.task_id, goal, policy='adaptive', backend='playwright')
        self.assertEqual(len(stored.receipts), len(trace.receipts))
        self.assertEqual(len(store.evidence(trace.task_id)), len(trace.evidence))

    def test_blocked_page_leaks_no_evidence_for_any_policy(self):
        for policy in (FixedFramePolicy(), LegacyV3Policy(), AdaptiveObservationPolicy()):
            trace, store, _driver, _page, _goal = self.run_page('captcha_blocked', policy)
            self.assertEqual(store.evidence(trace.task_id), [],
                             f'{policy.name} 在阻塞页产生了证据')
            self.assertEqual(trace.evidence, (), f'{policy.name} 的 trace 携带阻塞页证据')
            self.assertTrue(all(not item.evidence for item in trace.receipts),
                            f'{policy.name} 的 receipt 携带阻塞页证据')
            self.assertTrue(trace.stop_reason.startswith('blocked'),
                            f'{policy.name} 未记录阻塞停止原因: {trace.stop_reason}')

    def test_legacy_baseline_hits_static_but_misses_lazy(self):
        static_trace, _store, _driver, static_page, _goal = self.run_page('static_article', LegacyV3Policy())
        self.assertIn(static_page['objectives'][0]['expected_answer'], evidence_text(static_trace))
        lazy_trace, _store2, _driver2, lazy_page, _goal2 = self.run_page('activity_rules_lazy', LegacyV3Policy())
        self.assertNotIn(lazy_page['objectives'][0]['expected_answer'], evidence_text(lazy_trace))
        adaptive_trace, _store3, _driver3, _lazy, _goal3 = self.run_page(
            'activity_rules_lazy', AdaptiveObservationPolicy())
        self.assertIn(lazy_page['objectives'][0]['expected_answer'], evidence_text(adaptive_trace))

    # ------------------------------------------------------------------ 恢复与版本
    def test_resume_does_not_repeat_completed_actions(self):
        page = load_page('poster_image_only')
        goal = goal_for(page)
        driver = self.driver_for('poster_image_only')
        store = self.store()
        tight = model.Budget(max_units=1, max_captures=1, max_ocr_calls=1, max_steps=1)
        first = AcquisitionHarness(driver, AdaptiveObservationPolicy(), store, budgets=tight).run(goal)
        self.assertFalse(first.evidence)
        calls_after_first = dict(driver.call_counts)
        resumed = AcquisitionHarness(driver, AdaptiveObservationPolicy(), store,
                                     budgets=model.Budget()).run(goal, resume=True)
        self.assertIn(page['objectives'][0]['expected_answer'], evidence_text(resumed))
        keys = [item.idempotency_key for item in resumed.receipts]
        self.assertEqual(len(keys), len(set(keys)), '恢复后出现重复幂等键')
        repeated_inspect = driver.call_counts.get('inspect', 0) - calls_after_first.get('inspect', 0)
        self.assertEqual(repeated_inspect, 0, '恢复时已完成的 inspect 不应重复调用驱动')

    def test_resume_with_changed_page_version_stops_stale(self):
        page = load_page('static_article')
        goal = goal_for(page)
        driver = self.driver_for('static_article')
        store = self.store()
        session = store.create_session('playwright')
        task_id = store.create_task(session, goal)
        old_version = 'sha256:previous-page'
        store.append_receipt(model.Receipt(
            task_id=task_id, goal=goal.question, backend='playwright', action='inspect',
            status='completed', page_version=old_version, note='崩溃前的旧页面版本').filled())
        store.save_evidence(model.Evidence(
            capture_id='capture_old', page_version=old_version, region='article',
            extraction='dom', text='旧版本内容', source_url=page['url']), task_id=task_id)
        store.finish_task(task_id, status='interrupted', stop_reason='crashed', gap='interrupted')
        trace = AcquisitionHarness(driver, AdaptiveObservationPolicy(), store,
                                   budgets=model.Budget()).run(goal, resume=True)
        self.assertEqual(trace.stop_reason, 'page_version_changed')
        self.assertEqual(trace.task_id, task_id)
        current_version = driver.open(goal).page_version
        self.assertTrue(all(item.page_version != current_version for item in trace.evidence),
                        '页面版本变化后不得把旧证据当作当前证据')
        stored_task = store.task(task_id)
        self.assertEqual(stored_task['status'], 'blocked')
        historical = store.evidence(task_id)
        self.assertTrue(all(item.page_version == old_version for item in historical),
                        '历史证据保留为旧版本，不得伪装成当前版本')

    # ------------------------------------------------------------------ Skill 边界
    def test_skill_completion_verification_never_verifies_fact(self):
        from acquisition.skills import SkillHintProvider, SkillRegistry

        registry = SkillRegistry.load(SKILLS)
        page = load_page('layout_changed_v1')
        goal = goal_for(page)
        driver = self.driver_for('layout_changed_v1')
        store = self.store()
        provider = SkillHintProvider(registry, 'www.taptap.cn')
        trace = AcquisitionHarness(driver, AdaptiveObservationPolicy(hint_provider=provider), store,
                                   budgets=model.Budget()).run(goal)
        self.assertIn(page['objectives'][0]['expected_answer'], evidence_text(trace))
        self.assertTrue(all(item.verified is False for item in trace.evidence))
        view = FixtureDriver.from_file(PAGES / 'layout_changed_v1.json').open(goal)
        manifests = registry.eligible(view, host='www.taptap.cn')
        self.assertTrue(manifests, '活动结构样本上合法 Skill 应可用')
        verification = registry.verify(manifests[0], view, trace)
        self.assertTrue(verification.passed, '合法 Skill 的流程完成条件应通过')
        self.assertTrue(all(item.verified is False for item in store.evidence(trace.task_id)),
                        'Skill 完成验证不得把事实核验状态置真')

    # ------------------------------------------------------------------ 契约与序列化
    def test_trace_is_json_serializable_with_contract_fields(self):
        trace, _store, _driver, _page, _goal = self.run_page('static_article', AdaptiveObservationPolicy())
        payload = trace.as_dict()
        text = json.dumps(payload, ensure_ascii=False)
        self.assertGreater(len(text), 100)
        for key in ('schema', 'task_id', 'backend', 'policy', 'status', 'stop_reason',
                    'page_versions', 'receipts', 'evidence', 'cost', 'steps'):
            self.assertIn(key, payload)
        for receipt in payload['receipts']:
            self.assertIn('idempotency_key', receipt)
            self.assertIn('next', receipt)
            for item in receipt['evidence']:
                self.assertFalse(item['verified'])


if __name__ == '__main__':
    unittest.main()
