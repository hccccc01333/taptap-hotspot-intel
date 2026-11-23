"""Model preflight / selection tests (offline, fake clients)."""
import unittest

from context import model_router


class FakeHealthClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, prompt, *, model, max_tokens, event_id, session):
        self.calls.append({'model': model, 'prompt': prompt})
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        content, finish = value
        return {'content': content, 'finish_reason': finish, 'model': model,
                'usage': {'cost': 0, 'openai': {}}}


class PreflightTests(unittest.TestCase):
    def test_valid_tiny_json_passes_and_length_fails(self):
        client = FakeHealthClient([('{"ok": true}', 'stop'),
                                   ('{"ok": false}', 'length')])
        ok = model_router.check_model(client, 'hy4-flash-free')
        self.assertTrue(ok.ok)
        self.assertTrue(ok.json_valid)
        bad = model_router.check_model(client, 'hy4-flash-free')
        self.assertFalse(bad.ok)
        self.assertEqual(bad.reason, 'finish_reason_length')

    def test_invalid_json_and_exception_fail(self):
        client = FakeHealthClient([('not json', 'stop'), ConnectionError('boom')])
        first = model_router.check_model(client, 'hy4-flash-free')
        second = model_router.check_model(client, 'hy4-flash-free')
        self.assertEqual(first.reason, 'invalid_json')
        self.assertIn('ConnectionError', second.reason)

    def test_capped_and_training_models_are_skipped_by_default(self):
        client = FakeHealthClient([])
        results = model_router.preflight(
            client, ['hy4-flash-free', 'iceman-contributor-free', 'longcat-2.5-free'],
            allow_training=False, allow_capped=False)
        self.assertEqual([item.status for item in results],
                         ['failed', 'skipped', 'skipped'])
        self.assertEqual(results[1].reason, 'training_model_disabled')
        self.assertEqual(results[2].reason, 'capped_model_disabled')
        self.assertEqual([call['model'] for call in client.calls], ['hy4-flash-free'])

    def test_preflight_includes_training_when_allowed(self):
        client = FakeHealthClient([('{"ok": true}', 'stop')])
        results = model_router.preflight(client, ['iceman-contributor-free'],
                                         allow_training=True)
        self.assertTrue(results[0].ok)

    def test_select_prefers_healthy_allowed_candidate(self):
        health = [
            model_router.ModelHealth('beam-free', 'failed', 'HTTP 403'),
            model_router.ModelHealth('hy4-flash-free', 'ok', 'ok'),
        ]
        selection = model_router.select_model(
            health, ['beam-free', 'hy4-flash-free'])
        self.assertEqual(selection.model, 'hy4-flash-free')
        self.assertEqual(selection.fallback_from, ('beam-free:HTTP 403',))

    def test_select_returns_none_when_no_healthy_allowed(self):
        health = [model_router.ModelHealth('beam-free', 'failed', 'HTTP 403')]
        selection = model_router.select_model(health, ['beam-free'])
        self.assertIsNone(selection.model)
        self.assertIn('no healthy', selection.reason)


if __name__ == '__main__':
    unittest.main()
