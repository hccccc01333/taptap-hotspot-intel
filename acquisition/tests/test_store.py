"""store 模块测试：幂等、恢复、中断标记、租约栅栏、verify 唯一置真入口。"""
from __future__ import annotations

import os
import tempfile
import time
import unittest

from acquisition import model
from acquisition.store import AcquisitionStore, LeaseLost


def make_goal(task_id: str = 'goal_1') -> model.ResearchGoal:
    objective = model.Objective(objective_id='deadline', question='确认活动截止日期',
                                answer_kind='date', region_hints=('activity_rules',))
    return model.ResearchGoal(task_id=task_id, question='确认活动截止日期',
                              objectives=(objective,))


def make_receipt(task_id: str, *, action: str = 'inspect', region: str | None = None,
                 method: str | None = None, status: str = 'completed',
                 page_version: str = 'sha256:v1', evidence=(),
                 units: int = 1) -> model.Receipt:
    return model.Receipt(task_id=task_id, goal='确认活动截止日期', backend='playwright',
                         action=action, status=status, page_version=page_version,
                         region=region, method=method, evidence=tuple(evidence),
                         cost=model.Cost(units=units)).filled()


def make_evidence(*, capture_id: str = 'capture_1', page_version: str = 'sha256:v1',
                  text: str = '截止日期 2026年11月20日', extraction: str = 'dom',
                  region: str = 'rules') -> model.Evidence:
    return model.Evidence(capture_id=capture_id, page_version=page_version, region=region,
                          extraction=extraction, text=text,
                          source_url='https://www.taptap.cn/example/a')


class StoreTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, 'acq.db')
        self.store = AcquisitionStore(self.path)
        self.goal = make_goal()
        self.session_id = self.store.create_session('playwright')
        self.task_id = self.store.create_task(self.session_id, self.goal)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()


class SessionTaskTests(StoreTestBase):
    def test_create_session_and_task_roundtrip(self):
        task = self.store.task(self.task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task['session_id'], self.session_id)
        self.assertEqual(task['goal_task_id'], 'goal_1')
        self.assertEqual(task['goal']['question'], '确认活动截止日期')
        self.assertEqual(task['status'], 'pending')
        self.assertEqual(task['steps'], 0)
        found = self.store.find_task('goal_1')
        self.assertEqual(found['task_id'], self.task_id)
        self.assertIsNone(self.store.find_task('goal_1', session_id='session_missing'))

    def test_create_task_requires_existing_session(self):
        with self.assertRaises(KeyError):
            self.store.create_task('session_missing', self.goal)

    def test_create_session_rejects_unknown_backend(self):
        with self.assertRaises(ValueError):
            self.store.create_session('not-a-backend')


class ReceiptIdempotencyTests(StoreTestBase):
    def test_append_receipt_is_idempotent(self):
        receipt = make_receipt(self.task_id)
        first = self.store.append_receipt(receipt)
        second = self.store.append_receipt(receipt)
        self.assertEqual(first.receipt_id, second.receipt_id)
        self.assertEqual(first.idempotency_key, second.idempotency_key)
        self.assertEqual(len(self.store.receipts(self.task_id)), 1)
        self.assertEqual(self.store.task(self.task_id)['steps'], 1)
        self.assertEqual(self.store.task(self.task_id)['latest_page_version'], 'sha256:v1')

    def test_method_makes_receipts_distinct(self):
        dom = make_receipt(self.task_id, action='extract', region='rules', method='dom',
                           page_version='sha256:v1', units=1)
        ocr = make_receipt(self.task_id, action='extract', region='rules', method='ocr',
                           page_version='sha256:v1', units=3)
        self.assertNotEqual(dom.idempotency_key, ocr.idempotency_key)
        self.store.append_receipt(dom)
        self.store.append_receipt(ocr)
        self.assertEqual(len(self.store.receipts(self.task_id)), 2)

    def test_receipt_lookup_by_key(self):
        receipt = make_receipt(self.task_id)
        stored = self.store.append_receipt(receipt)
        found = self.store.receipt(self.task_id, stored.idempotency_key)
        self.assertIsNotNone(found)
        self.assertEqual(found.action, 'inspect')
        self.assertEqual(found.page_version, 'sha256:v1')
        self.assertIsNone(self.store.receipt(self.task_id, 'receipt_missing'))

    def test_append_receipt_rejects_blocked_with_evidence(self):
        forged = model.Receipt(task_id=self.task_id, goal='q', backend='playwright',
                               action='extract', status='blocked', page_version='sha256:v1',
                               region='rules', method='ocr',
                               evidence=(make_evidence(),)).filled()
        with self.assertRaises(ValueError):
            self.store.append_receipt(forged)
        self.assertEqual(self.store.receipts(self.task_id), [])

    def test_append_receipt_rejects_verified_evidence(self):
        verified = model.Evidence(capture_id='capture_v', page_version='sha256:v1',
                                  region='rules', extraction='dom', text='x',
                                  verified=True, verifier='human_checker')
        forged = model.Receipt(task_id=self.task_id, goal='q', backend='playwright',
                               action='extract', status='completed',
                               page_version='sha256:v1', region='rules', method='dom',
                               evidence=(verified,)).filled()
        with self.assertRaises(ValueError):
            self.store.append_receipt(forged)
        self.assertEqual(self.store.receipts(self.task_id), [])

    def test_append_receipt_unknown_task_rejected(self):
        with self.assertRaises(KeyError):
            self.store.append_receipt(make_receipt('task_missing'))

    def test_same_key_conflicting_content_is_not_silently_dropped(self):
        stored = self.store.append_receipt(make_receipt(self.task_id))
        # status 不参与幂等键：同键、不同内容必须报错，而不是被 INSERT OR IGNORE 静默丢弃。
        conflicting = make_receipt(self.task_id, status='failed')
        self.assertEqual(conflicting.idempotency_key, stored.idempotency_key)
        with self.assertRaises(ValueError):
            self.store.append_receipt(conflicting)
        self.assertEqual(len(self.store.receipts(self.task_id)), 1)
        self.assertEqual(self.store.receipts(self.task_id)[0].status, 'completed')
        # 同内容的新对象重放仍是幂等（at 等派生字段不算内容差异）。
        replay = self.store.append_receipt(make_receipt(self.task_id))
        self.assertEqual(replay.receipt_id, stored.receipt_id)


class EvidenceTests(StoreTestBase):
    def test_save_evidence_is_idempotent_by_fingerprint(self):
        evidence = make_evidence()
        self.store.save_evidence(evidence, task_id=self.task_id)
        self.store.save_evidence(evidence, task_id=self.task_id)
        self.assertEqual(len(self.store.evidence(self.task_id)), 1)

    def test_evidence_version_filter(self):
        self.store.save_evidence(make_evidence(capture_id='capture_1', page_version='sha256:v1'),
                                 task_id=self.task_id)
        self.store.save_evidence(make_evidence(capture_id='capture_2', page_version='sha256:v2'),
                                 task_id=self.task_id)
        self.assertEqual(len(self.store.evidence(self.task_id)), 2)
        only_v1 = self.store.evidence(self.task_id, page_version='sha256:v1')
        self.assertEqual([item.capture_id for item in only_v1], ['capture_1'])
        self.assertEqual(len(self.store.evidence(self.task_id, page_version='sha256:v3')), 0)

    def test_same_evidence_shared_between_tasks(self):
        other_task = self.store.create_task(self.session_id, make_goal('goal_2'))
        evidence = make_evidence()
        self.store.save_evidence(evidence, task_id=self.task_id)
        self.store.save_evidence(evidence, task_id=other_task)
        self.assertEqual(len(self.store.evidence(self.task_id)), 1)
        self.assertEqual(len(self.store.evidence(other_task)), 1)

    def test_save_evidence_rejects_verified(self):
        verified = model.Evidence(capture_id='capture_v', page_version='sha256:v1',
                                  region='rules', extraction='dom', text='x',
                                  verified=True, verifier='human_checker')
        with self.assertRaises(ValueError):
            self.store.save_evidence(verified, task_id=self.task_id)

    def test_save_evidence_unknown_task_rejected(self):
        with self.assertRaises(KeyError):
            self.store.save_evidence(make_evidence(), task_id='task_missing')


class VerifyTests(StoreTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.evidence = make_evidence()
        self.store.save_evidence(self.evidence, task_id=self.task_id)

    def _verification(self, verification_id: str, status: str,
                      capture_id: str = 'capture_1') -> model.Verification:
        return model.Verification(verification_id=verification_id, capture_id=capture_id,
                                  status=status, verifier='human_checker',
                                  detail='对照截图与页面版本')

    def test_needs_review_does_not_set_verified(self):
        changed = self.store.verify(self._verification('ver_1', 'needs_review'))
        self.assertFalse(changed)
        stored = self.store.evidence(self.task_id)[0]
        self.assertFalse(stored.verified)
        self.assertIsNone(stored.verifier)
        self.assertEqual(len(self.store.verifications(self.task_id)), 1)

    def test_supported_with_evidence_sets_verified_and_verifier(self):
        changed = self.store.verify(self._verification('ver_2', 'supported'))
        self.assertTrue(changed)
        stored = self.store.evidence(self.task_id)[0]
        self.assertTrue(stored.verified)
        self.assertEqual(stored.verifier, 'human_checker')
        self.assertEqual(len(self.store.verifications(self.task_id)), 1)

    def test_supported_without_evidence_returns_false(self):
        changed = self.store.verify(self._verification('ver_3', 'supported',
                                                       capture_id='capture_missing'))
        self.assertFalse(changed)
        stored = self.store.evidence(self.task_id)[0]
        self.assertFalse(stored.verified)
        # 核验记录仍追加，只是不归属任何任务
        self.assertEqual(len(self.store.verifications(self.task_id)), 0)

    def test_unsupported_after_supported_does_not_clear_verified(self):
        self.store.verify(self._verification('ver_4', 'supported'))
        changed = self.store.verify(self._verification('ver_5', 'unsupported'))
        self.assertFalse(changed)
        stored = self.store.evidence(self.task_id)[0]
        self.assertTrue(stored.verified)  # 核验失败只追加状态，不回退已核验证据
        self.assertEqual(len(self.store.verifications(self.task_id)), 2)


class TaskLifecycleTests(StoreTestBase):
    def test_finish_task_and_trace(self):
        evidence = make_evidence()
        first = make_receipt(self.task_id, action='extract', region='rules', method='dom',
                             page_version='sha256:v1', evidence=(evidence,), units=1)
        self.store.append_receipt(first)
        self.store.save_evidence(evidence, task_id=self.task_id)
        second = make_receipt(self.task_id, action='interact', region='rules',
                              page_version='sha256:v2', units=2)
        self.store.append_receipt(second)
        self.store.finish_task(self.task_id, status='succeeded',
                               stop_reason='objectives_covered')
        trace = self.store.trace(self.task_id, self.goal, policy='test_policy',
                                 backend='playwright')
        self.assertEqual(trace.status, 'succeeded')
        self.assertEqual(trace.stop_reason, 'objectives_covered')
        self.assertIsNone(trace.gap)
        self.assertEqual(trace.steps, 2)
        self.assertEqual(trace.cost.units, 3)
        self.assertEqual(trace.page_versions, ('sha256:v1', 'sha256:v2'))
        self.assertEqual(len(trace.receipts), 2)
        self.assertEqual(len(trace.evidence), 1)
        self.assertEqual(trace.evidence[0].verified, False)
        self.assertEqual(trace.policy, 'test_policy')
        self.assertEqual(trace.backend, 'playwright')

    def test_finish_task_with_gap(self):
        self.store.finish_task(self.task_id, status='blocked', stop_reason='login_required',
                               gap='login_required')
        trace = self.store.trace(self.task_id, self.goal, policy='p', backend='playwright')
        self.assertEqual(trace.status, 'blocked')
        self.assertEqual(trace.gap, 'login_required')
        self.assertEqual(trace.steps, 0)
        self.assertEqual(trace.cost.units, 0)

    def test_trace_includes_verifications(self):
        evidence = make_evidence()
        self.store.append_receipt(make_receipt(
            self.task_id, action='extract', region='rules', method='dom',
            evidence=(evidence,)))
        self.store.save_evidence(evidence, task_id=self.task_id)
        self.store.finish_task(self.task_id, status='succeeded',
                               stop_reason='objectives_covered')
        before = self.store.trace(self.task_id, self.goal, policy='p', backend='playwright')
        self.assertEqual(before.verifications, ())  # 默认空元组，向后兼容
        self.assertEqual(before.as_dict()['verifications'], [])

        verification = model.Verification(verification_id='ver_1', capture_id='capture_1',
                                          status='supported', verifier='human_checker',
                                          detail='对照截图与页面版本')
        self.assertTrue(self.store.verify(verification))
        trace = self.store.trace(self.task_id, self.goal, policy='p', backend='playwright')
        self.assertEqual(len(trace.verifications), 1)
        self.assertEqual(trace.verifications[0].status, 'supported')
        self.assertEqual(trace.verifications[0].verifier, 'human_checker')
        payload = trace.as_dict()
        self.assertEqual(payload['verifications'][0]['capture_id'], 'capture_1')
        self.assertEqual(payload['verifications'][0]['status'], 'supported')

    def test_finish_task_unknown_task_rejected(self):
        with self.assertRaises(KeyError):
            self.store.finish_task('task_missing', status='failed', stop_reason='x')

    def test_resume_reports_latest_page_version(self):
        self.store.append_receipt(make_receipt(self.task_id, page_version='sha256:v1'))
        self.store.append_receipt(make_receipt(
            self.task_id, action='extract', region='rules', method='dom',
            page_version='sha256:v2', units=3))
        snapshot = self.store.resume(self.task_id)
        self.assertEqual(snapshot['latest_page_version'], 'sha256:v2')
        self.assertEqual(len(snapshot['receipts']), 2)
        self.assertEqual(snapshot['session_id'], self.session_id)
        self.assertEqual(snapshot['task']['task_id'], self.task_id)

    def test_resume_without_receipts_has_empty_latest_version(self):
        snapshot = self.store.resume(self.task_id)
        self.assertEqual(snapshot['latest_page_version'], '')
        self.assertEqual(snapshot['receipts'], [])

    def test_resume_unknown_task_rejected(self):
        with self.assertRaises(KeyError):
            self.store.resume('task_missing')


class MarkInterruptedTests(StoreTestBase):
    def test_mark_interrupted_marks_stale_pending_task(self):
        marked = self.store.mark_interrupted(stale_after_seconds=0, now=time.time() + 1)
        self.assertEqual(marked, [self.task_id])
        task = self.store.task(self.task_id)
        self.assertEqual(task['status'], 'interrupted')
        self.assertEqual(task['stop_reason'], 'interrupted')
        # 已经 interrupted 的任务不会再次被标记
        self.assertEqual(self.store.mark_interrupted(stale_after_seconds=0,
                                                     now=time.time() + 2), [])

    def test_fresh_task_not_marked_by_default_staleness(self):
        self.assertEqual(self.store.mark_interrupted(), [])

    def test_only_pending_or_running_tasks_marked(self):
        self.store.finish_task(self.task_id, status='succeeded', stop_reason='done')
        self.assertEqual(self.store.mark_interrupted(stale_after_seconds=0,
                                                     now=time.time() + 1), [])


class LeaseTests(StoreTestBase):
    def test_lease_acquire_assert_renew_and_release(self):
        token = self.store.acquire_lease(self.session_id, 'owner-a', ttl_seconds=60)
        self.assertTrue(token)
        self.store.assert_owner(self.session_id, 'owner-a', token)
        self.store.renew_lease(self.session_id, 'owner-a', token)
        self.store.release_lease(self.session_id, 'owner-a', token)
        with self.assertRaises(LeaseLost):
            self.store.assert_owner(self.session_id, 'owner-a', token)

    def test_lease_fence_rejects_previous_owner(self):
        self.store.acquire_lease(self.session_id, 'owner-a', ttl_seconds=60)
        self.store.acquire_lease(self.session_id, 'owner-b', ttl_seconds=60)
        with self.assertRaises(LeaseLost):
            self.store.assert_owner(self.session_id, 'owner-a')
        with self.assertRaises(LeaseLost):
            self.store.renew_lease(self.session_id, 'owner-a')
        with self.assertRaises(LeaseLost):
            self.store.release_lease(self.session_id, 'owner-a')
        self.store.assert_owner(self.session_id, 'owner-b')

    def test_token_mismatch_and_reacquire_invalidates_old_token(self):
        token_a = self.store.acquire_lease(self.session_id, 'owner-a', ttl_seconds=60)
        with self.assertRaises(LeaseLost):
            self.store.assert_owner(self.session_id, 'owner-a', 'wrong-token')
        with self.assertRaises(LeaseLost):
            self.store.renew_lease(self.session_id, 'owner-a', 'wrong-token')
        with self.assertRaises(LeaseLost):
            self.store.release_lease(self.session_id, 'owner-a', 'wrong-token')
        # 同一 owner 重新获取租约：token 更新，旧 token 失效（fence token 名实相符）。
        token_a2 = self.store.acquire_lease(self.session_id, 'owner-a', ttl_seconds=60)
        self.assertNotEqual(token_a, token_a2)
        with self.assertRaises(LeaseLost):
            self.store.assert_owner(self.session_id, 'owner-a', token_a)
        self.store.assert_owner(self.session_id, 'owner-a', token_a2)

    def test_expired_lease_is_rejected(self):
        token = self.store.acquire_lease(self.session_id, 'owner-a', ttl_seconds=0.001)
        time.sleep(0.02)
        with self.assertRaises(LeaseLost):
            self.store.assert_owner(self.session_id, 'owner-a', token)

    def test_finish_task_fenced_after_owner_takeover(self):
        token_a = self.store.acquire_lease(self.session_id, 'owner-a', ttl_seconds=60)
        token_b = self.store.acquire_lease(self.session_id, 'owner-b', ttl_seconds=60)
        self.store.finish_task(self.task_id, status='succeeded', stop_reason='new_owner_done',
                               session_id=self.session_id, owner='owner-b', token=token_b)
        # 旧 owner 的终态写入被 fence 拒绝，不得覆盖新 owner 的结果。
        with self.assertRaises(LeaseLost):
            self.store.finish_task(self.task_id, status='interrupted',
                                   stop_reason='interrupted', session_id=self.session_id,
                                   owner='owner-a', token=token_a)
        task = self.store.task(self.task_id)
        self.assertEqual(task['status'], 'succeeded')
        self.assertEqual(task['stop_reason'], 'new_owner_done')

    def test_finish_task_fenced_expired_lease_rejected(self):
        token = self.store.acquire_lease(self.session_id, 'owner-a', ttl_seconds=0.001)
        time.sleep(0.02)
        with self.assertRaises(LeaseLost):
            self.store.finish_task(self.task_id, status='succeeded', stop_reason='done',
                                   session_id=self.session_id, owner='owner-a', token=token)
        self.assertEqual(self.store.task(self.task_id)['status'], 'pending')

    def test_save_evidence_fenced_after_owner_takeover(self):
        token_a = self.store.acquire_lease(self.session_id, 'owner-a', ttl_seconds=60)
        token_b = self.store.acquire_lease(self.session_id, 'owner-b', ttl_seconds=60)
        with self.assertRaises(LeaseLost):
            self.store.save_evidence(make_evidence(capture_id='capture_old'),
                                     task_id=self.task_id, session_id=self.session_id,
                                     owner='owner-a', token=token_a)
        self.assertEqual(self.store.evidence(self.task_id), [])
        self.store.save_evidence(make_evidence(capture_id='capture_new'),
                                 task_id=self.task_id, session_id=self.session_id,
                                 owner='owner-b', token=token_b)
        self.assertEqual(len(self.store.evidence(self.task_id)), 1)

    def test_fenced_write_requires_session_and_owner_together(self):
        with self.assertRaises(ValueError):
            self.store.finish_task(self.task_id, status='failed', stop_reason='x',
                                   session_id=self.session_id)
        with self.assertRaises(ValueError):
            self.store.save_evidence(make_evidence(), task_id=self.task_id, owner='owner-a')

    def test_release_missing_lease_is_noop(self):
        self.store.release_lease('session_missing', 'owner-x')

    def test_acquire_lease_validation(self):
        with self.assertRaises(ValueError):
            self.store.acquire_lease(self.session_id, '')
        with self.assertRaises(ValueError):
            self.store.acquire_lease(self.session_id, 'owner-a', ttl_seconds=0)


class ClosedStoreTests(unittest.TestCase):
    def test_operations_after_close_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AcquisitionStore(os.path.join(tmp, 'closed.db'))
            store.close()
            with self.assertRaises(RuntimeError):
                store.create_session('playwright')
            with self.assertRaises(RuntimeError):
                store.task('x')
            store.close()  # 重复关闭是幂等的


if __name__ == '__main__':
    unittest.main()
