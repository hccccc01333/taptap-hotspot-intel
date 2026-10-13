"""Manual-verifiable fact set scoring: false-pass and false-reject rates.

The fact set (claim_fact_set.json) fixes the review-flagged cases as regressions
and adds semantic-opposite, wrong-number, wrong-date, missing-step, platform and
prize cases plus real correct samples. Expectations were labeled by the assistant
against already-reviewed sources; the developer/human review of every label is
still pending, so the JSON records that state explicitly.

The scored verifier is deterministic word-level checking. A word-level pass is
never "verified": it stays needs_review until facts are independently confirmed.
"""
import json
import unittest
from pathlib import Path

from agent_v3.claim_verifier import verify_creative, verify_material, verify_signal

FACT_SET = Path(__file__).with_name('claim_fact_set.json')


def load_fact_set():
    return json.loads(FACT_SET.read_text(encoding='utf-8'))


def evaluate(case):
    kind, payload = case['kind'], case['input']
    quotes = case.get('quotes') or payload.get('facts') or []
    titles = case.get('titles')
    if kind == 'signal':
        return verify_signal(payload, titles)
    if kind == 'material':
        return verify_material(payload, quotes, titles)
    if kind == 'creative':
        return verify_creative(payload, quotes, titles)
    raise AssertionError('unknown case kind: ' + kind)


class ClaimFactSetTests(unittest.TestCase):
    def test_every_case_declares_expectation_provenance_and_basis(self):
        data = load_fact_set()
        self.assertIn('pending_human', json.dumps(data, ensure_ascii=False))
        self.assertGreaterEqual(len(data['cases']), 20)
        for case in data['cases']:
            for key in ('id', 'kind', 'expected', 'provenance', 'basis'):
                self.assertTrue(case.get(key), case.get('id', '?'))
            self.assertIn(case['expected'], ('accept', 'reject'))
            self.assertIn(case['kind'], ('signal', 'material', 'creative'))

    def test_false_pass_and_false_reject_rates_stay_at_zero(self):
        data = load_fact_set()
        false_pass, false_reject = [], []
        for case in data['cases']:
            flagged = evaluate(case)['status'] == 'insufficient'
            if case['expected'] == 'reject' and not flagged:
                false_pass.append(case['id'])
            if case['expected'] == 'accept' and flagged:
                false_reject.append(case['id'])
        rejects = sum(case['expected'] == 'reject' for case in data['cases'])
        accepts = sum(case['expected'] == 'accept' for case in data['cases'])
        false_pass_rate = len(false_pass) / rejects
        false_reject_rate = len(false_reject) / accepts
        self.assertEqual(false_pass, [], '误放行（应拦截但通过）：%s' % false_pass)
        self.assertEqual(false_reject, [], '误拒绝（应通过但被拦截）：%s' % false_reject)
        self.assertEqual(false_pass_rate, 0.0)
        self.assertEqual(false_reject_rate, 0.0)


if __name__ == '__main__':
    unittest.main()
