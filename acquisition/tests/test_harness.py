"""harness 端到端测试：trace、幂等恢复、阻塞页零证据、stale、预算、租约、路由。"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from acquisition import model
from acquisition.driver import FixtureDriver
from acquisition.harness import AcquisitionHarness
from acquisition.receipts import build_receipt
from acquisition.store import AcquisitionStore

PAGES = Path(__file__).resolve().parents[1] / 'fixtures' / 'pages'


# --------------------------------------------------------------------- 样本
def make_goal(task_id: str = 'goal_harness') -> model.ResearchGoal:
    objective = model.Objective(objective_id='deadline', question='确认活动截止日期',
                                answer_kind='date', region_hints=('activity_rules',))
    return model.ResearchGoal(task_id=task_id, question='确认活动截止日期',
                              objectives=(objective,), allowed_hosts=('www.taptap.cn',))


def load_page(page_id: str) -> dict:
    return json.loads((PAGES / f'{page_id}.json').read_text(encoding='utf-8-sig'))


def goal_for_page(page: dict) -> model.ResearchGoal:
    """由真实冻结样本构造目标；只复制非真值字段（expected_* 留在评测侧）。"""
    objectives = tuple(model.Objective(
        objective_id=item['objective_id'], question=item['question'],
        answer_kind=item['answer_kind'], region_hints=tuple(item['region_hints']),
        required=bool(item['required'])) for item in page['objectives'])
    return model.ResearchGoal(task_id=page['page_id'], question=page['title'],
                              objectives=objectives, allowed_hosts=('www.taptap.cn',))


def make_page(*, lazy: bool = False, blocked_reason: str | None = None,
              title: str = '活动公告（合成）', dom_text: str | None = None) -> dict:
    regions = [
        {'region_id': 'article', 'role': 'article_body', 'bbox': [100, 200, 1200, 600],
         'text': '活动时间为2026年11月10日。', 'visual_text': '', 'lazy': False,
         'requires': None, 'blocked': False, 'scroll_y': 200},
        {'region_id': 'comments', 'role': 'comments', 'bbox': [100, 1300, 1200, 300],
         'text': '玩家评论：期待上线。', 'visual_text': '', 'lazy': False,
         'requires': None, 'blocked': False, 'scroll_y': 1300},
    ]
    if lazy:
        regions.append({'region_id': 'rules', 'role': 'activity_rules',
                        'bbox': [120, 1500, 1200, 700], 'text': '',
                        'visual_text': '截止日期：2026年11月20日', 'lazy': True,
                        'requires': 'click:展开规则', 'blocked': False, 'scroll_y': 1500})
    return {
        'schema': 'acquisition-fixture-v0.1',
        'page_id': 'harness_page_lazy' if lazy else 'harness_page_static',
        'label': '合成测试页（不是真实站点录制）',
        'url': 'https://www.taptap.cn/example/harness-001',
        'title': title,
        'viewport': [1440, 1000],
        'scroll_height': 2500,
        'blocked_reason': blocked_reason,
        'dom_text': dom_text if dom_text is not None else '活动公告正文：活动时间为2026年11月10日。',
        'regions': regions,
        'objectives': [{
            'objective_id': 'deadline', 'question': '确认活动截止日期', 'answer_kind': 'date',
            'region_hints': ['activity_rules'], 'required': True,
            'expected_answer': '2026年11月20日', 'expected_region_role': 'activity_rules',
        }],
    }


# --------------------------------------------------------------------- 策略
class ScriptedPolicy:
    """按脚本顺序执行动作；已尝试过的动作跳过，最后以正常完成原因 stop。"""

    name = 'scripted'

    def __init__(self, steps) -> None:
        self.steps = list(steps)

    def next_action(self, goal, view, state):
        for action in self.steps:
            if state.attempts(action) == 0:
                return action
        return model.Action(kind='stop', reason='objectives_covered')


class BlockedStopPolicy:
    """看到阻塞页立即按真实原因停止（模拟自适应策略行为）。"""

    name = 'blocked_stop'

    def next_action(self, goal, view, state):
        if view.blocked_reason:
            return model.Action(kind='stop', reason=f'blocked:{view.blocked_reason}')
        return model.Action(kind='extract', region_id=model.DOM_BODY_REGION, method='dom')


class CycleOcrPolicy:
    """依次尝试固定三屏 OCR；用于预算耗尽测试。"""

    name = 'cycle_ocr'

    def next_action(self, goal, view, state):
        for index in range(3):
            action = model.Action(kind='extract', region_id=f'viewport:{index}', method='ocr')
            if state.attempts(action) == 0:
                return action
        return model.Action(kind='stop', reason='objectives_covered')


class AlwaysDomPolicy:
    """始终请求同一动作；用于验证幂等复用 + max_steps 上界。"""

    name = 'always_dom'

    def next_action(self, goal, view, state):
        return model.Action(kind='extract', region_id=model.DOM_BODY_REGION, method='dom')


class RaisingPolicy:
    name = 'raising'

    def next_action(self, goal, view, state):
        raise RuntimeError('策略实现缺陷')


class UnknownStopPolicy:
    """返回非「正常完成」的停止原因；用于验证未知 stop reason 不静默判成功。"""

    name = 'unknown_stop'

    def next_action(self, goal, view, state):
        return model.Action(kind='stop', reason='mystery_finished')


class RetryPolicy:
    """同一动作最多重试两次（模拟接口冻结 §2.5 的 max_attempts_per_action > 1）。"""

    name = 'retry'

    def __init__(self, action: model.Action, attempts: int = 2) -> None:
        self.action = action
        self.attempts = attempts

    def next_action(self, goal, view, state):
        if state.attempts(self.action) < self.attempts:
            return self.action
        return model.Action(kind='stop', reason='objectives_covered')


class RejectVlmRouter:
    """模拟 vlm 未启用的路由：拒绝 vlm 提取，其余放行。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def validate_action(self, action, view, budgets=None):
        self.calls.append((action.kind, action.region_id, action.method))
        if action.kind == 'extract' and action.method == 'vlm':
            return (False, 'vlm 未启用')
        return (True, 'ok')


class ExplodingDriver(FixtureDriver):
    """模拟截图失败；用于验证失败动作成本照记。"""

    def extract(self, view, region, method):
        raise RuntimeError('模拟截图失败')


class FlakyDriver(FixtureDriver):
    """第一次提取失败、之后成功；用于验证重试收据不被静默丢弃（S3）。"""

    def __init__(self, page) -> None:
        super().__init__(page)
        self.extract_calls = 0

    def extract(self, view, region, method):
        self.extract_calls += 1
        if self.extract_calls == 1:
            raise RuntimeError('首次截图失败（模拟）')
        return super().extract(view, region, method)


class OpenFailingDriver(FixtureDriver):
    """打开页面即失败；用于验证任务不会停留在 pending（S5）。"""

    def open(self, goal):
        raise RuntimeError('模拟导航失败')


class LeaseStealingDriver(FixtureDriver):
    """在 inspect 时模拟另一个执行者接管租约（可选：以新 owner 写终态）。"""

    def __init__(self, page, store: AcquisitionStore, session_id: str,
                 *, goal_task_id: str | None = None, finish_as_new_owner: bool = False) -> None:
        super().__init__(page)
        self._store = store
        self._session_id = session_id
        self._goal_task_id = goal_task_id
        self._finish_as_new_owner = finish_as_new_owner
        self.stolen = False

    def inspect(self, view):
        observation = super().inspect(view)
        if not self.stolen:
            self.stolen = True
            token = self._store.acquire_lease(self._session_id, 'intruder', ttl_seconds=60)
            if self._finish_as_new_owner and self._goal_task_id:
                task = self._store.find_task(self._goal_task_id)
                self._store.finish_task(task['task_id'], status='succeeded',
                                        stop_reason='new_owner_done', session_id=self._session_id,
                                        owner='intruder', token=token)
        return observation


# --------------------------------------------------------------------- 基类
class HarnessTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AcquisitionStore(os.path.join(self._tmp.name, 'acq.db'))
        self.goal = make_goal()

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def make_harness(self, driver, policy, **kwargs) -> AcquisitionHarness:
        return AcquisitionHarness(driver, policy, self.store, **kwargs)


# --------------------------------------------------------------------- 测试
class EndToEndTraceTests(HarnessTestBase):
    def test_trace_records_full_goal_to_evidence_flow(self):
        driver = FixtureDriver(make_page())
        policy = ScriptedPolicy([
            model.Action(kind='extract', region_id=model.DOM_BODY_REGION, method='dom'),
        ])
        harness = self.make_harness(driver, policy)
        trace = harness.run(self.goal)

        self.assertEqual(trace.status, 'succeeded')
        self.assertEqual(trace.stop_reason, 'objectives_covered')
        self.assertEqual(trace.backend, 'playwright')
        self.assertEqual(trace.policy, 'scripted')
        self.assertEqual(len(trace.receipts), 3)  # inspect + extract + stop
        self.assertEqual([receipt.action for receipt in trace.receipts],
                         ['inspect', 'extract', 'stop'])
        self.assertEqual(trace.receipts[0].status, 'completed')
        self.assertEqual(trace.receipts[1].method, 'dom')
        self.assertEqual(trace.receipts[2].next, 'stop')
        self.assertEqual(len(trace.evidence), 1)
        evidence = trace.evidence[0]
        self.assertIn('2026年11月10日', evidence.text)
        self.assertFalse(evidence.verified)
        self.assertIsNone(evidence.verifier)
        self.assertEqual(evidence.page_version, trace.page_versions[0])
        self.assertEqual(trace.cost.units, 2)  # inspect 1 + dom 1
        self.assertEqual(trace.steps, 3)
        self.assertIsNone(trace.gap)
        self.assertEqual(trace.coverage(self.goal), (1, 1))
        self.assertEqual(driver.call_counts.get('inspect'), 1)
        self.assertEqual(driver.call_counts.get('extract'), 1)
        # trace 可完整复盘且可序列化
        payload = trace.as_dict()
        self.assertEqual(json.loads(json.dumps(payload, ensure_ascii=False))['task_id'],
                         trace.task_id)
        self.assertEqual(len(payload['receipts']), 3)

    def test_interact_updates_view_and_page_versions(self):
        driver = FixtureDriver(make_page(lazy=True))
        policy = ScriptedPolicy([
            model.Action(kind='extract', region_id='rules', method='dom'),
            model.Action(kind='interact', region_id='rules'),
            model.Action(kind='extract', region_id='rules', method='ocr'),
        ])
        trace = self.make_harness(driver, policy).run(self.goal)
        self.assertEqual(len(trace.page_versions), 2)
        old_version, new_version = trace.page_versions
        self.assertNotEqual(old_version, new_version)
        interact_receipt = next(item for item in trace.receipts if item.action == 'interact')
        self.assertEqual(interact_receipt.page_version, new_version)
        self.assertEqual(len(trace.evidence), 1)
        evidence = trace.evidence[0]
        self.assertEqual(evidence.page_version, new_version)
        self.assertIn('2026年11月20日', evidence.text)
        self.assertEqual(trace.status, 'succeeded')

    def test_stop_receipt_carries_reason(self):
        trace = self.make_harness(FixtureDriver(make_page()),
                                  ScriptedPolicy([])).run(self.goal)
        stop_receipt = trace.receipts[-1]
        self.assertEqual(stop_receipt.action, 'stop')
        self.assertEqual(stop_receipt.status, 'completed')
        self.assertEqual(stop_receipt.note, 'objectives_covered')
        self.assertEqual(trace.cost.units, 1)  # 只有 inspect

    def test_same_region_dom_and_ocr_are_distinct_idempotent_actions(self):
        driver = FixtureDriver(make_page())
        policy = ScriptedPolicy([
            model.Action(kind='extract', region_id='article', method='dom'),
            model.Action(kind='extract', region_id='article', method='ocr'),
        ])
        trace = self.make_harness(driver, policy).run(self.goal)
        self.assertEqual(driver.call_counts.get('extract:dom'), 1)
        self.assertEqual(driver.call_counts.get('extract:ocr'), 1)
        methods = [item.method for item in trace.receipts if item.action == 'extract']
        self.assertEqual(methods, ['dom', 'ocr'])
        keys = [item.idempotency_key for item in trace.receipts]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(trace.evidence), 2)  # 两条通道产生两条独立证据


class BlockedPageTests(HarnessTestBase):
    def test_blocked_page_extraction_stores_no_evidence_and_stops(self):
        driver = FixtureDriver(make_page(blocked_reason='captcha'))
        policy = ScriptedPolicy([
            model.Action(kind='extract', region_id='article', method='ocr'),
        ])
        trace = self.make_harness(driver, policy).run(self.goal)
        self.assertEqual(trace.status, 'blocked')
        self.assertEqual(trace.stop_reason, 'blocked:captcha')
        self.assertEqual(trace.gap, 'blocked:captcha')
        self.assertEqual(trace.evidence, ())
        self.assertEqual(self.store.evidence(trace.task_id), [])
        blocked_receipts = [item for item in trace.receipts if item.status == 'blocked']
        self.assertEqual(len(blocked_receipts), 1)
        self.assertEqual(blocked_receipts[0].action, 'extract')
        # 固定基线在阻塞页最多浪费一次截图，但绝不入库
        self.assertEqual(driver.capture_count, 1)

    def test_policy_sees_blocked_view_and_stops(self):
        driver = FixtureDriver(make_page(blocked_reason='login_wall'))
        trace = self.make_harness(driver, BlockedStopPolicy()).run(self.goal)
        self.assertEqual(trace.status, 'blocked')
        self.assertEqual(trace.stop_reason, 'blocked:login_wall')
        self.assertEqual(trace.evidence, ())
        self.assertNotIn('extract', driver.call_counts)
        inspect_receipt = trace.receipts[0]
        self.assertEqual(inspect_receipt.action, 'inspect')
        self.assertEqual(inspect_receipt.status, 'completed')
        self.assertIn('blocked_page:login_wall', inspect_receipt.note)


class ResumeTests(HarnessTestBase):
    def test_resume_reuses_completed_receipts_without_recalling_driver(self):
        first_harness = self.make_harness(
            FixtureDriver(make_page()),
            ScriptedPolicy([
                model.Action(kind='extract', region_id=model.DOM_BODY_REGION, method='dom'),
                model.Action(kind='extract', region_id='viewport:0', method='ocr'),
                model.Action(kind='extract', region_id='article', method='dom'),
            ]),
            budgets=model.Budget(max_steps=2))
        first_trace = first_harness.run(self.goal)
        self.assertEqual(first_trace.stop_reason, 'max_steps')
        self.assertEqual(first_trace.status, 'failed')
        first_keys = [item.idempotency_key for item in first_trace.receipts]

        second_driver = FixtureDriver(make_page())
        second_harness = self.make_harness(second_driver, ScriptedPolicy([
            model.Action(kind='extract', region_id=model.DOM_BODY_REGION, method='dom'),
            model.Action(kind='extract', region_id='viewport:0', method='ocr'),
            model.Action(kind='extract', region_id='article', method='dom'),
        ]))
        second_trace = second_harness.run(self.goal, resume=True)

        self.assertEqual(second_trace.task_id, first_trace.task_id)
        self.assertEqual(second_trace.status, 'succeeded')
        # 已完成的 inspect 与 dom:body 不重复调用驱动，只执行剩余动作
        self.assertFalse(second_driver.call_counts.get('inspect'))
        self.assertEqual(second_driver.call_counts.get('extract'), 2)
        self.assertEqual(second_driver.call_counts.get('extract:ocr'), 1)
        keys = [item.idempotency_key for item in second_trace.receipts]
        self.assertEqual(len(keys), len(set(keys)), '恢复后出现重复幂等键')
        for key in first_keys:
            self.assertIn(key, keys)
        self.assertEqual(len(second_trace.receipts), 5)  # inspect + dom + ocr + article + stop
        self.assertEqual(len(second_trace.evidence), 3)
        self.assertEqual(second_trace.cost.units, first_trace.cost.units + 1 + 3)
        self.assertEqual(second_trace.page_versions, first_trace.page_versions)

    def test_resume_on_completed_task_is_read_only_replay(self):
        page = make_page()
        policy = ScriptedPolicy([
            model.Action(kind='extract', region_id=model.DOM_BODY_REGION, method='dom'),
        ])
        first = self.make_harness(FixtureDriver(page), policy).run(self.goal)
        replay_driver = FixtureDriver(page)
        replay = self.make_harness(replay_driver, policy).run(self.goal, resume=True)
        self.assertEqual(replay.task_id, first.task_id)
        self.assertEqual(len(replay.receipts), len(first.receipts))
        self.assertEqual([item.idempotency_key for item in replay.receipts],
                         [item.idempotency_key for item in first.receipts])
        self.assertFalse(replay_driver.call_counts.get('inspect'))
        self.assertFalse(replay_driver.call_counts.get('extract'))

    def test_resume_after_mark_interrupted_continues(self):
        page = make_page()
        view = FixtureDriver(page).open(self.goal)
        session_id = self.store.create_session('playwright')
        task_id = self.store.create_task(session_id, self.goal)
        task_goal = replace(self.goal, task_id=task_id)
        inspect_receipt = model.Receipt(task_id=task_id, goal=self.goal.question,
                                       backend='playwright', action='inspect',
                                       status='completed', page_version=view.page_version,
                                       cost=model.Cost(units=1)).filled()
        self.store.append_receipt(inspect_receipt)
        body_text = view.dom_text
        evidence = model.Evidence(capture_id='capture_manual', page_version=view.page_version,
                                  region=model.DOM_BODY_REGION, extraction='dom',
                                  text=body_text, source_url=view.url, verified=False)
        action = model.Action(kind='extract', region_id=model.DOM_BODY_REGION, method='dom')
        observation = model.Observation(action='extract', page_version=view.page_version,
                                        region_id=model.DOM_BODY_REGION, role='section',
                                        extraction='dom', text=body_text,
                                        cost=model.action_cost('extract', 'dom'))
        self.store.append_receipt(build_receipt(task_goal, 'playwright', action, observation,
                                                view=view, evidence=(evidence,)))
        self.store.save_evidence(evidence, task_id=task_id)

        marked = self.store.mark_interrupted(stale_after_seconds=0, now=time.time() + 1)
        self.assertEqual(marked, [task_id])
        self.assertEqual(self.store.task(task_id)['status'], 'interrupted')

        driver = FixtureDriver(page)
        harness = self.make_harness(driver, ScriptedPolicy([
            model.Action(kind='extract', region_id=model.DOM_BODY_REGION, method='dom'),
            model.Action(kind='extract', region_id='article', method='dom'),
        ]))
        trace = harness.run(self.goal, resume=True)
        self.assertEqual(trace.task_id, task_id)
        self.assertEqual(trace.status, 'succeeded')
        self.assertFalse(driver.call_counts.get('inspect'))  # 已存 inspect 被复用
        self.assertEqual(driver.call_counts.get('extract'), 1)  # 只执行未完成的 article
        self.assertEqual(len(trace.evidence), 2)
        self.assertEqual(len(self.store.receipts(task_id)), 4)

    def test_resume_without_existing_task_starts_new_one(self):
        trace = self.make_harness(FixtureDriver(make_page()), ScriptedPolicy([])).run(
            self.goal, resume=True)
        # 没有可恢复任务 → 按新任务执行；正常 stop 但 required 目标无证据，不能判成功（S4）。
        self.assertEqual(trace.status, 'failed')
        self.assertEqual(trace.gap, 'objectives_uncovered')
        self.assertEqual(len(trace.receipts), 2)  # inspect + stop


class StalePageVersionTests(HarnessTestBase):
    def _lazy_run(self):
        driver = FixtureDriver(make_page(lazy=True))
        policy = ScriptedPolicy([
            model.Action(kind='interact', region_id='rules'),
            model.Action(kind='extract', region_id='rules', method='ocr'),
        ])
        return self.make_harness(driver, policy).run(self.goal)

    def test_resume_after_page_version_change_stops_stale(self):
        first = self._lazy_run()
        self.assertEqual(first.status, 'succeeded')
        self.assertEqual(len(first.page_versions), 2)
        latest = first.page_versions[-1]

        replay_driver = FixtureDriver(make_page(lazy=True))
        trace = self.make_harness(replay_driver, ScriptedPolicy([])).run(self.goal, resume=True)
        self.assertEqual(trace.task_id, first.task_id)
        self.assertEqual(trace.status, 'blocked')
        self.assertEqual(trace.stop_reason, 'page_version_changed')
        self.assertEqual(trace.gap, 'page_version_changed')
        stale_receipts = [item for item in trace.receipts if item.status == 'stale']
        self.assertEqual(len(stale_receipts), 1)
        self.assertEqual(stale_receipts[0].next, 'stop')
        self.assertIn('page_version_changed', stale_receipts[0].note)
        self.assertIn(latest, stale_receipts[0].note)
        # 旧证据保留为历史，但不冒充当前事实
        self.assertEqual([item.page_version for item in trace.evidence], [latest])
        # 只 open 页面，未执行任何新动作
        self.assertEqual(replay_driver.call_counts.get('inspect', 0), 0)
        self.assertNotIn('extract', replay_driver.call_counts)

    def test_resume_with_changed_page_content_stops_stale(self):
        first = self._lazy_run()
        changed_page = make_page(lazy=True, title='页面标题已变化（外部更新）',
                                 dom_text='页面正文已更新，旧证据不再对应当前版本。')
        trace = self.make_harness(FixtureDriver(changed_page),
                                  ScriptedPolicy([])).run(self.goal, resume=True)
        self.assertEqual(trace.task_id, first.task_id)
        self.assertEqual(trace.stop_reason, 'page_version_changed')
        self.assertEqual(trace.status, 'blocked')


class BudgetAndFailureTests(HarnessTestBase):
    def test_budget_exhausted_stops_and_records_cost(self):
        driver = FixtureDriver(make_page())
        harness = self.make_harness(
            driver, CycleOcrPolicy(),
            budgets=model.Budget(max_units=4, max_captures=2, max_ocr_calls=2, max_steps=30))
        trace = harness.run(self.goal)
        self.assertEqual(trace.stop_reason, 'budget_exhausted')
        self.assertEqual(trace.status, 'failed')
        self.assertEqual(trace.gap, 'budget_exhausted')
        self.assertEqual(trace.cost.units, 4)
        self.assertEqual(trace.cost.captures, 1)
        self.assertEqual(trace.cost.ocr_calls, 1)
        self.assertLessEqual(trace.cost.captures, 2)
        self.assertEqual(driver.call_counts.get('extract'), 1)
        self.assertEqual(trace.receipts[-1].status, 'budget_exhausted')
        self.assertEqual(trace.receipts[-1].cost.units, 0)  # 未执行的动作不收费

    def test_max_steps_bounds_loop_and_idempotent_reuse(self):
        driver = FixtureDriver(make_page())
        harness = self.make_harness(driver, AlwaysDomPolicy(),
                                    budgets=model.Budget(max_units=24, max_steps=3))
        trace = harness.run(self.goal)
        self.assertEqual(trace.stop_reason, 'max_steps')
        self.assertEqual(trace.status, 'failed')
        self.assertEqual(driver.call_counts.get('extract'), 1)  # 幂等命中不重复调用驱动
        # 重复提议只消耗循环上界；trace.steps 是落库收据条数（inspect + extract）。
        self.assertEqual(trace.steps, 2)
        self.assertEqual(len(trace.receipts), 2)

    def test_failed_action_cost_is_recorded(self):
        driver = ExplodingDriver(make_page())
        policy = ScriptedPolicy([
            model.Action(kind='extract', region_id=model.DOM_BODY_REGION, method='dom'),
        ])
        trace = self.make_harness(driver, policy).run(self.goal)
        failed = [item for item in trace.receipts if item.status == 'failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].action, 'extract')
        self.assertEqual(failed[0].cost.units, 1)  # 失败动作成本照记
        self.assertIsNotNone(failed[0].error)
        self.assertEqual(trace.evidence, ())
        self.assertEqual(trace.cost.units, 2)  # inspect 1 + 失败动作 1
        # 任务正常结束但 zero evidence：required 目标未覆盖，不能判 succeeded（S4）。
        self.assertEqual(trace.status, 'failed')
        self.assertEqual(trace.gap, 'objectives_uncovered')

    def test_policy_error_is_recorded(self):
        trace = self.make_harness(FixtureDriver(make_page()), RaisingPolicy()).run(self.goal)
        self.assertEqual(trace.stop_reason, 'policy_error')
        self.assertEqual(trace.status, 'failed')
        self.assertEqual(trace.gap, 'policy_error')
        self.assertEqual(trace.receipts[-1].status, 'failed')
        self.assertIsNotNone(trace.receipts[-1].error)


class RouterTests(HarnessTestBase):
    def test_router_rejection_records_deferred_and_skips_driver(self):
        driver = FixtureDriver(make_page())
        router = RejectVlmRouter()
        policy = ScriptedPolicy([
            model.Action(kind='extract', region_id='viewport:0', method='vlm'),
            model.Action(kind='extract', region_id=model.DOM_BODY_REGION, method='dom'),
        ])
        trace = self.make_harness(driver, policy, router=router,
                                  budgets=model.Budget(max_vlm_calls=1)).run(self.goal)
        deferred = [item for item in trace.receipts if item.status == 'deferred']
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0].method, 'vlm')
        self.assertEqual(deferred[0].cost.units, 0)
        self.assertIn('vlm 未启用', deferred[0].note)
        self.assertEqual(driver.call_counts.get('extract'), 1)  # vlm 未执行
        self.assertEqual(driver.call_counts.get('extract:dom'), 1)
        self.assertEqual(trace.status, 'succeeded')
        # 首次 inspect 也交给路由校验；vlm 被拒后策略改选 dom。
        self.assertEqual([call[2] for call in router.calls], [None, 'vlm', 'dom'])


class LeaseTests(HarnessTestBase):
    def test_stale_owner_cannot_rewrite_terminal_state_after_takeover(self):
        """S2：租约被接管后旧执行者不能写终态；任务终态保持新 owner 的结果。"""
        session_id = self.store.create_session('playwright')
        driver = LeaseStealingDriver(make_page(), self.store, session_id,
                                     goal_task_id=self.goal.task_id,
                                     finish_as_new_owner=True)
        trace = self.make_harness(driver, ScriptedPolicy([])).run(
            self.goal, session_id=session_id)
        self.assertTrue(driver.stolen)
        # 新 owner 已把任务写成 succeeded；旧 harness 的收据写/终态写都被栅栏拒绝。
        self.assertEqual(trace.status, 'succeeded')
        self.assertEqual(trace.stop_reason, 'new_owner_done')
        self.assertEqual(trace.receipts, ())  # 旧执行者的 inspect 收据未落库
        stored = self.store.task(trace.task_id)
        self.assertEqual(stored['status'], 'succeeded')
        self.assertEqual(stored['stop_reason'], 'new_owner_done')

    def test_lease_stolen_without_new_terminal_leaves_task_state_untouched(self):
        """旧执行者 LeaseLost 后不得把任务改写成旧 owner 的终态。"""
        session_id = self.store.create_session('playwright')
        driver = LeaseStealingDriver(make_page(), self.store, session_id,
                                     goal_task_id=self.goal.task_id)
        trace = self.make_harness(driver, ScriptedPolicy([])).run(
            self.goal, session_id=session_id)
        self.assertTrue(driver.stolen)
        self.assertEqual(trace.receipts, ())
        self.assertEqual(self.store.task(trace.task_id)['status'], 'pending')
        self.assertEqual(trace.status, 'pending')


class ResumeBudgetTests(HarnessTestBase):
    """S1：恢复时初始 inspect 幂等命中不得重复计入步数预算。"""

    def test_resume_under_max_steps_does_not_double_count(self):
        steps = [model.Action(kind='extract', region_id=model.DOM_BODY_REGION, method='dom'),
                 model.Action(kind='extract', region_id='article', method='dom')]
        first = self.make_harness(FixtureDriver(make_page()), ScriptedPolicy(steps),
                                  budgets=model.Budget(max_steps=2)).run(self.goal)
        self.assertEqual(first.stop_reason, 'max_steps')
        self.assertEqual(len(first.receipts), 2)  # inspect + 1 个动作

        resumed_driver = FixtureDriver(make_page())
        resumed = self.make_harness(resumed_driver, ScriptedPolicy(steps),
                                    budgets=model.Budget(max_steps=3)).run(self.goal, resume=True)
        # 已存 2 条收据 + max_steps=3 → 恢复后仍能再执行 1 个动作（不是立刻又 max_steps）。
        self.assertEqual(resumed.stop_reason, 'max_steps')
        self.assertEqual(len(resumed.receipts), 3)
        self.assertEqual(resumed_driver.call_counts.get('inspect', 0), 0)  # 初始 inspect 被复用
        self.assertEqual(resumed_driver.call_counts.get('extract'), 1)

        # 与同样预算的全新运行执行同样多的动作：恢复不因重复计步少做一步。
        fresh = self.make_harness(FixtureDriver(make_page()), ScriptedPolicy(steps),
                                  budgets=model.Budget(max_steps=3)).run(
                                      make_goal('goal_harness_fresh'))
        self.assertEqual(len(fresh.receipts), len(resumed.receipts))


class RetryReceiptTests(HarnessTestBase):
    """S3：failed → 重试成功的收据/证据/成本都必须完整留痕，不得被静默丢弃。"""

    def test_failed_then_successful_retry_keeps_both_receipts(self):
        driver = FlakyDriver(make_page())
        action = model.Action(kind='extract', region_id=model.DOM_BODY_REGION, method='dom')
        trace = self.make_harness(driver, RetryPolicy(action, attempts=2)).run(self.goal)

        self.assertEqual(driver.extract_calls, 2)  # 首次失败后确实重试了一次驱动
        extract_receipts = [item for item in trace.receipts if item.action == 'extract']
        self.assertEqual([item.status for item in extract_receipts], ['failed', 'completed'])
        self.assertTrue(extract_receipts[0].idempotency_key != extract_receipts[1].idempotency_key)
        self.assertEqual(extract_receipts[1].idempotency_key,
                         f'{extract_receipts[0].idempotency_key}:retry:1')
        self.assertEqual(len(trace.evidence), 1)  # 成功重试的证据确实入库
        self.assertIn('2026年11月10日', trace.evidence[0].text)
        # 成本包含 inspect + 失败动作 + 成功动作，失败成本没有被抹掉。
        self.assertEqual(trace.cost.units, 3)
        keys = [item.idempotency_key for item in trace.receipts]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(self.store.receipts(trace.task_id)), 4)  # inspect/failed/retry/stop
        self.assertEqual(trace.status, 'succeeded')


class SuccessSemanticsTests(HarnessTestBase):
    """S4：succeeded 必须要求 required 目标有候选证据，未知 stop reason 不静默成功。"""

    def test_normal_stop_without_required_evidence_is_failed(self):
        trace = self.make_harness(FixtureDriver(make_page()), ScriptedPolicy([])).run(self.goal)
        self.assertEqual(trace.stop_reason, 'objectives_covered')
        self.assertEqual(trace.status, 'failed')
        self.assertEqual(trace.gap, 'objectives_uncovered')
        self.assertEqual(trace.evidence, ())

    def test_unknown_stop_reason_is_not_succeeded(self):
        trace = self.make_harness(FixtureDriver(make_page()), UnknownStopPolicy()).run(self.goal)
        self.assertEqual(trace.stop_reason, 'mystery_finished')
        self.assertEqual(trace.status, 'failed')
        self.assertNotEqual(trace.status, 'succeeded')
        self.assertEqual(trace.gap, 'objectives_uncovered')

    def test_evidence_counts_required_coverage_independent_of_role_hints(self):
        # 用 viewport 整屏 OCR（role=section，不在 objective.region_hints 内）也能命中
        # required objective 的候选文本：成功语义按 candidate_text 判断，不依赖 role 先验。
        policy = ScriptedPolicy([
            model.Action(kind='extract', region_id='viewport:0', method='ocr'),
        ])
        trace = self.make_harness(FixtureDriver(make_page(lazy=True)), policy).run(self.goal)
        self.assertEqual(trace.status, 'succeeded')
        self.assertEqual(trace.gap, None)
        self.assertEqual(trace.coverage(self.goal), (1, 1))

    def test_real_fixtures_success_semantics(self):
        from acquisition.policy import AdaptiveObservationPolicy, LegacyV3Policy

        static_page = load_page('static_article')
        legacy_static = self.make_harness(FixtureDriver(static_page),
                                          LegacyV3Policy()).run(goal_for_page(static_page))
        self.assertEqual(legacy_static.status, 'succeeded')
        self.assertTrue(any('2026年11月12日' in item.text for item in legacy_static.evidence))

        lazy_page = load_page('activity_rules_lazy')
        legacy_lazy = self.make_harness(FixtureDriver(lazy_page),
                                        LegacyV3Policy()).run(goal_for_page(lazy_page))
        self.assertEqual(legacy_lazy.status, 'failed')
        self.assertEqual(legacy_lazy.gap, 'objectives_uncovered')

        adaptive_lazy = self.make_harness(FixtureDriver(lazy_page),
                                          AdaptiveObservationPolicy()).run(
                                              goal_for_page(lazy_page))
        self.assertEqual(adaptive_lazy.status, 'succeeded')
        self.assertEqual(adaptive_lazy.gap, None)
        self.assertEqual(adaptive_lazy.coverage(goal_for_page(lazy_page)), (1, 1))


class OpenFailureTests(HarnessTestBase):
    """S5：driver.open 失败必须落 failed 终态与可复盘收据，而不是停留在 pending。"""

    def test_driver_open_failure_records_failed_terminal_state(self):
        trace = self.make_harness(OpenFailingDriver(make_page()), ScriptedPolicy([])).run(
            self.goal)
        self.assertEqual(trace.stop_reason, 'open_failed')
        self.assertEqual(trace.status, 'failed')
        self.assertEqual(trace.gap, 'open_failed')
        self.assertEqual(len(trace.receipts), 1)
        receipt = trace.receipts[0]
        self.assertEqual(receipt.action, 'inspect')
        self.assertEqual(receipt.status, 'failed')
        self.assertEqual(receipt.page_version, '')
        self.assertIn('模拟导航失败', receipt.error or '')
        self.assertEqual(trace.evidence, ())
        stored = self.store.task(trace.task_id)
        self.assertEqual(stored['status'], 'failed')
        self.assertEqual(stored['stop_reason'], 'open_failed')
        self.assertTrue(stored['finished_at'])

    def test_resume_with_open_failure_overwrites_pending_state(self):
        first = self.make_harness(FixtureDriver(make_page()), ScriptedPolicy([]),
                                  budgets=model.Budget(max_steps=1)).run(self.goal)
        self.assertEqual(first.status, 'failed')
        trace = self.make_harness(OpenFailingDriver(make_page()), ScriptedPolicy([])).run(
            self.goal, resume=True)
        self.assertEqual(trace.task_id, first.task_id)
        self.assertEqual(trace.stop_reason, 'open_failed')
        self.assertEqual(self.store.task(first.task_id)['status'], 'failed')
        self.assertEqual(self.store.task(first.task_id)['gap'], 'open_failed')


class NotedDriver(FixtureDriver):
    """提取成功但带视觉转录提示的驱动替身（模拟真实 OCR driver 的 note）。"""

    def extract(self, view, region, method):
        observation = super().extract(view, region, method)
        return replace(observation, note='视觉转录，需对照截图与语义核验')


class EvidenceNoteTests(HarnessTestBase):
    def test_observation_note_is_carried_into_evidence(self):
        driver = NotedDriver(make_page())
        policy = ScriptedPolicy([
            model.Action(kind='extract', region_id=model.DOM_BODY_REGION, method='dom'),
        ])
        trace = self.make_harness(driver, policy).run(self.goal)
        self.assertEqual(len(trace.evidence), 1)
        self.assertEqual(trace.evidence[0].note, '视觉转录，需对照截图与语义核验')
        # 证据核验状态仍不受提取提示影响。
        self.assertFalse(trace.evidence[0].verified)


class CliContractTests(unittest.TestCase):
    def test_goal_from_page_ignores_expected_truth(self):
        from acquisition.cli import goal_from_page

        page = make_page()
        goal = goal_from_page(page)
        self.assertEqual(goal.task_id, 'harness_page_static')
        self.assertEqual(goal.objectives[0].objective_id, 'deadline')
        self.assertEqual(goal.objectives[0].answer_kind, 'date')
        self.assertNotIn('expected', repr(goal).lower())
        self.assertNotIn('2026年11月20日', repr(goal))  # expected_answer 不进入研究目标

    def test_main_returns_2_for_missing_page(self):
        from acquisition.cli import main

        self.assertEqual(main(['--page', 'not-a-real-page.json']), 2)


if __name__ == '__main__':
    unittest.main()
