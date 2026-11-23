"""receipts 模块测试：构造、默认状态/下一步、阻塞页证据拒绝、核验标记校验。"""
from __future__ import annotations

import unittest

from acquisition import model
from acquisition.receipts import (
    build_receipt,
    derive_next,
    derive_status,
    receipt_key,
    retry_key,
    validate_receipt,
    validate_receipt_payload,
)


def make_goal(task_id: str = 'goal_1') -> model.ResearchGoal:
    objective = model.Objective(objective_id='deadline', question='确认活动截止日期',
                                answer_kind='date', region_hints=('activity_rules',))
    return model.ResearchGoal(task_id=task_id, question='确认活动截止日期',
                              objectives=(objective,), allowed_hosts=('www.taptap.cn',))


def make_view(*, blocked_reason: str | None = None,
              page_version: str = 'sha256:view-1') -> model.PageView:
    region = model.Region(region_id='rules', role='activity_rules', bbox=(10, 20, 300, 200),
                          text='截止日期 2026年11月20日', scroll_y=0)
    return model.PageView(page_version=page_version, url='https://www.taptap.cn/example/a',
                          title='活动公告（合成）', viewport=(1440, 1000),
                          scroll_height=2000, regions=(region,),
                          dom_text='截止日期 2026年11月20日', blocked_reason=blocked_reason)


def make_action(kind: str = 'extract', region_id: str | None = 'rules',
                method: str | None = 'dom') -> model.Action:
    return model.Action(kind=kind, region_id=region_id, method=method, reason='测试')


def make_observation(*, action: str = 'extract', page_version: str = 'sha256:view-1',
                     text: str = '截止日期 2026年11月20日',
                     extraction: str | None = 'dom') -> model.Observation:
    return model.Observation(action=action, page_version=page_version, region_id='rules',
                             role='activity_rules', extraction=extraction, text=text,
                             capture_id=None, bbox=(10, 20, 300, 200),
                             cost=model.action_cost('extract', 'dom'))


def make_evidence(page_version: str = 'sha256:view-1', **overrides) -> model.Evidence:
    fields = dict(capture_id='capture_1', page_version=page_version, region='rules',
                  extraction='dom', text='截止日期 2026年11月20日', bbox=(10, 20, 300, 200),
                  source_url='https://www.taptap.cn/example/a')
    fields.update(overrides)
    return model.Evidence(**fields)


class BuildReceiptTests(unittest.TestCase):
    def test_completed_extract_receipt_has_method_and_evidence_next(self):
        goal = make_goal()
        view = make_view()
        action = make_action()
        observation = make_observation()
        evidence = make_evidence()
        receipt = build_receipt(goal, 'playwright', action, observation,
                                view=view, evidence=(evidence,))
        self.assertEqual(receipt.task_id, goal.task_id)
        self.assertEqual(receipt.goal, goal.question)
        self.assertEqual(receipt.action, 'extract')
        self.assertEqual(receipt.method, 'dom')
        self.assertEqual(receipt.region, 'rules')
        self.assertEqual(receipt.status, 'completed')
        self.assertEqual(receipt.page_version, view.page_version)
        self.assertEqual(receipt.next, 'verify_against_source')
        self.assertEqual(receipt.evidence, (evidence,))
        self.assertTrue(receipt.receipt_id)
        self.assertTrue(receipt.idempotency_key)

    def test_no_evidence_completed_receipt_next_continue(self):
        receipt = build_receipt(make_goal(), 'playwright', make_action(), make_observation(text=''),
                                view=make_view())
        self.assertEqual(receipt.status, 'completed')
        self.assertEqual(receipt.next, 'continue')

    def test_stop_receipt_is_completed_and_next_stop(self):
        action = model.Action(kind='stop', reason='objectives_covered')
        observation = model.Observation(action='stop', page_version='sha256:view-1')
        receipt = build_receipt(make_goal(), 'playwright', action, observation,
                                view=make_view())
        self.assertEqual(receipt.status, 'completed')
        self.assertEqual(receipt.next, 'stop')
        self.assertEqual(receipt.note, '')

    def test_blocked_page_receipt_without_evidence_is_allowed_and_next_stop(self):
        view = make_view(blocked_reason='captcha')
        observation = make_observation(text='')
        observation = model.Observation(action='extract', page_version=view.page_version,
                                        region_id='rules', role='activity_rules',
                                        extraction='ocr', text='',
                                        cost=model.action_cost('extract', 'ocr'),
                                        blocked=True, note='blocked_page:captcha')
        receipt = build_receipt(make_goal(), 'playwright', make_action(method='ocr'),
                                observation, view=view)
        self.assertEqual(receipt.status, 'blocked')
        self.assertEqual(receipt.next, 'stop')
        self.assertEqual(receipt.evidence, ())

    def test_blocked_page_receipt_with_evidence_rejected(self):
        view = make_view(blocked_reason='captcha')
        observation = model.Observation(action='extract', page_version=view.page_version,
                                        region_id='rules', role='activity_rules',
                                        extraction='ocr', text='',
                                        cost=model.action_cost('extract', 'ocr'),
                                        blocked=True, note='blocked_page:captcha')
        with self.assertRaises(ValueError):
            build_receipt(make_goal(), 'playwright', make_action(method='ocr'), observation,
                          view=view, evidence=(make_evidence(page_version=view.page_version),))

    def test_blocked_observation_receipt_with_evidence_rejected(self):
        observation = model.Observation(action='extract', page_version='sha256:view-1',
                                        region_id='rules', role='activity_rules',
                                        extraction='dom', text='', blocked=True,
                                        note='login_required')
        with self.assertRaises(ValueError):
            build_receipt(make_goal(), 'playwright', make_action(), observation,
                          view=make_view(), evidence=(make_evidence(),))

    def test_explicit_blocked_status_with_evidence_rejected(self):
        with self.assertRaises(ValueError):
            build_receipt(make_goal(), 'playwright', make_action(), make_observation(),
                          view=make_view(), evidence=(make_evidence(),), status='blocked')

    def test_verified_evidence_without_verifier_rejected(self):
        # model.Evidence 自身就不允许 verified=True 且没有 verifier。
        with self.assertRaises(ValueError):
            model.Evidence(capture_id='capture_2', page_version='sha256:view-1',
                           region='rules', extraction='dom', text='x', verified=True)

    def test_unknown_backend_and_status_rejected(self):
        with self.assertRaises(ValueError):
            build_receipt(make_goal(), 'not-a-backend', make_action(), make_observation(),
                          view=make_view())
        with self.assertRaises(ValueError):
            build_receipt(make_goal(), 'playwright', make_action(), make_observation(),
                          view=make_view(), status='unknown-status')

    def test_derive_status_and_next_helpers(self):
        blocked_view = make_view(blocked_reason='login_wall')
        self.assertEqual(derive_status(make_action(), make_observation(), blocked_view), 'blocked')
        # inspect 不把阻塞页视为收据阻塞：它不产生文字证据，由策略决定停止。
        inspect_action = model.Action(kind='inspect')
        inspect_observation = model.Observation(action='inspect',
                                                page_version=blocked_view.page_version)
        self.assertEqual(derive_status(inspect_action, inspect_observation, blocked_view),
                         'completed')
        self.assertEqual(derive_next('blocked', ()), 'stop')
        self.assertEqual(derive_next('failed', ()), 'retry')
        self.assertEqual(derive_next('deferred', ()), 'continue')
        self.assertEqual(derive_next('completed', (make_evidence(),)), 'verify_against_source')


class IdempotencyKeyTests(unittest.TestCase):
    def _receipt(self, *, method: str) -> model.Receipt:
        goal = make_goal()
        action = make_action(method=method)
        observation = make_observation()
        return build_receipt(goal, 'playwright', action, observation, view=make_view())

    def test_dom_and_ocr_on_same_region_have_different_keys(self):
        dom_receipt = self._receipt(method='dom')
        ocr_receipt = self._receipt(method='ocr')
        self.assertNotEqual(dom_receipt.idempotency_key, ocr_receipt.idempotency_key)
        self.assertEqual(dom_receipt.idempotency_key, dom_receipt.key())

    def test_same_action_same_version_same_key_and_different_version_differs(self):
        first = self._receipt(method='dom')
        second = self._receipt(method='dom')
        self.assertEqual(first.idempotency_key, second.idempotency_key)
        helper_key = receipt_key(task_id=first.task_id, backend='playwright',
                                 page_version=first.page_version, action=make_action())
        self.assertEqual(first.idempotency_key, helper_key)
        other_view = make_view(page_version='sha256:view-2')
        other_observation = make_observation(page_version=other_view.page_version)
        other = build_receipt(make_goal(), 'playwright', make_action(), other_observation,
                              view=other_view)
        self.assertNotEqual(first.idempotency_key, other.idempotency_key)


class RetryKeyAndCustomKeyTests(unittest.TestCase):
    def test_retry_key_numbering(self):
        base = 'receipt_abc'
        self.assertEqual(retry_key(base, 0), base)
        self.assertEqual(retry_key(base, 1), base + ':retry:1')
        self.assertEqual(retry_key(base, 3), base + ':retry:3')
        with self.assertRaises(TypeError):
            retry_key(base, True)
        with self.assertRaises(ValueError):
            retry_key('', 1)

    def test_build_receipt_supports_explicit_idempotency_key(self):
        goal = make_goal()
        receipt = build_receipt(goal, 'playwright', make_action(), make_observation(),
                                view=make_view(), evidence=(make_evidence(),),
                                idempotency_key='receipt_custom:retry:1')
        self.assertEqual(receipt.idempotency_key, 'receipt_custom:retry:1')
        self.assertEqual(receipt.receipt_id, 'receipt_custom:retry:1')
        # 显式键与默认派生键不同，且可通过收据校验。
        default = build_receipt(goal, 'playwright', make_action(), make_observation(),
                                view=make_view(), evidence=(make_evidence(),))
        self.assertNotEqual(receipt.idempotency_key, default.idempotency_key)
        validate_receipt(receipt)
        validate_receipt_payload(receipt.as_dict())

    def test_build_receipt_rejects_empty_explicit_key(self):
        with self.assertRaises(ValueError):
            build_receipt(make_goal(), 'playwright', make_action(), make_observation(),
                          view=make_view(), idempotency_key='')


class ValidateReceiptTests(unittest.TestCase):
    def _valid_receipt(self) -> model.Receipt:
        return build_receipt(make_goal(), 'playwright', make_action(), make_observation(),
                             view=make_view(), evidence=(make_evidence(),))

    def test_validate_receipt_accepts_valid(self):
        receipt = self._valid_receipt()
        self.assertIs(validate_receipt(receipt), receipt)

    def test_validate_receipt_rejects_blocked_with_evidence(self):
        receipt = self._valid_receipt()
        forged = model.Receipt(task_id=receipt.task_id, goal=receipt.goal,
                               backend=receipt.backend, action=receipt.action,
                               status='blocked', page_version=receipt.page_version,
                               region=receipt.region, method=receipt.method,
                               evidence=receipt.evidence).filled()
        with self.assertRaises(ValueError):
            validate_receipt(forged)

    def test_validate_receipt_rejects_evidence_version_mismatch(self):
        other = make_evidence(page_version='sha256:other')
        with self.assertRaises(ValueError):
            validate_receipt(build_receipt(make_goal(), 'playwright', make_action(),
                                           make_observation(), view=make_view(),
                                           evidence=(other,)))

    def test_validate_payload_accepts_as_dict(self):
        payload = self._valid_receipt().as_dict()
        self.assertEqual(payload['method'], 'dom')
        self.assertIs(validate_receipt_payload(payload), payload)

    def test_validate_payload_rejects_blocked_with_evidence(self):
        payload = self._valid_receipt().as_dict()
        payload['status'] = 'blocked'
        with self.assertRaises(ValueError):
            validate_receipt_payload(payload)

    def test_validate_payload_rejects_verified_without_verifier(self):
        payload = self._valid_receipt().as_dict()
        payload['evidence'][0]['verified'] = True
        with self.assertRaises(ValueError):
            validate_receipt_payload(payload)

    def test_validate_payload_rejects_unknown_enum_and_schema(self):
        payload = self._valid_receipt().as_dict()
        bad_backend = dict(payload, backend='mystery')
        with self.assertRaises(ValueError):
            validate_receipt_payload(bad_backend)
        bad_schema = dict(payload, schema='unknown-schema')
        with self.assertRaises(ValueError):
            validate_receipt_payload(bad_schema)
        bad_method = dict(payload, method='screenshot')
        with self.assertRaises(ValueError):
            validate_receipt_payload(bad_method)

    def test_validate_payload_rejects_evidence_version_mismatch(self):
        payload = self._valid_receipt().as_dict()
        payload['evidence'][0]['page_version'] = 'sha256:tampered'
        with self.assertRaises(ValueError):
            validate_receipt_payload(payload)


if __name__ == '__main__':
    unittest.main()
