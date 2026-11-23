# -*- coding: utf-8 -*-
"""business_event_pack 的离线测试：不联网、不调用模型。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scripts.business_event_pack import (  # noqa: E402
    Event, Ledger, ModelBudgetExceeded, OpenCodeGoClient, parse_json, product_chars,
    run_pack, verify_quotes, write_group_summary,
)

BODY = (
    '合成活动公告正文。' * 20 +
    '\n活动时间：2026年10月20日上线。新增12人锦标赛，玩家需要连续进行三轮比赛。'
    '\n售价为1980日元（含税），支持简体中文。\n'
)
QUOTE_1 = '活动时间：2026年10月20日上线。'
QUOTE_2 = '售价为1980日元（含税），支持简体中文。'


class FakeResponse:
    def __init__(self, status_code=200, text='', payload=None):
        self.status_code = status_code
        self.text = text
        self.apparent_encoding = 'utf-8'
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


def html(body=BODY, title='合成活动页'):
    return f'<html><head><title>{title}</title></head><body><article>{body}</article></body></html>'


def fetcher_for(mapping: dict[str, str]):
    def fetch(url, **kwargs):
        if url not in mapping:
            raise RuntimeError('not found')
        return FakeResponse(text=html(mapping[url]))
    return fetch


def payload_for(quote1=QUOTE_1, quote2=QUOTE_2, *, extra_quote=None):
    facts = [{'text': '活动 10 月 20 日上线', 'quote': quote1},
             {'text': '售价 1980 日元', 'quote': quote2}]
    if extra_quote is not None:
        facts.append({'text': '额外事实', 'quote': extra_quote})
    return {
        'headline': '合成活动上线',
        'one_line': '活动 10 月 20 日上线，售价 1980 日元。',
        'facts': facts,
        'unknowns': ['具体奖励规则未说明'],
        'intelligence': {'summary': '合成摘要', 'why_taptap': '判断：适合社区速览',
                         'watch_next': ['官方后续公告']},
        'material': {'title': '速览文案', 'channel': '社区动态', 'draft': '可编辑文案',
                     'usage_boundary': '不得补写奖励规则'},
    }


def make_event(event_id='event_one', url='https://example.com/a'):
    return Event(event_id=event_id, url=url, title='合成活动', source='示例来源',
                 published_at='2026-10-20 10:00:00', objective='确认时间与售价')


class FakeHTTP:
    """OpenCodeGoClient 的假 HTTP：按 model 返回脚本化 JSON。"""

    def __init__(self, *, fail_models=(), payload=None):
        self.calls = []
        self.fail_models = set(fail_models)
        self.payload = payload or payload_for()

    def __call__(self, url, *, headers=None, json=None, timeout=None):
        self.calls.append({'url': url, 'headers': headers, 'json': json})
        if json['model'] in self.fail_models:
            return FakeResponse(status_code=500, text='upstream failed')
        content = __import__('json').dumps(self.payload, ensure_ascii=False)
        return FakeResponse(text=content, payload={
            'id': 'chatcmpl-test', 'model': json['model'],
            'choices': [{'message': {'content': content}}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150,
                      'completion_tokens_details': {'reasoning_tokens': 20}},
        })


class EventValidationTests(unittest.TestCase):
    def test_requires_https_and_string_fields(self):
        with self.assertRaises(ValueError):
            Event.from_dict({'event_id': 'x', 'url': 'http://example.com/a', 'title': 't',
                             'source': 's', 'published_at': 'p', 'objective': 'o'})
        with self.assertRaises(ValueError):
            Event.from_dict({'event_id': 'x', 'url': 'https://example.com/a', 'title': '',
                             'source': 's', 'published_at': 'p', 'objective': 'o'})
        event = Event.from_dict({'event_id': 'x1', 'url': 'https://example.com/a',
                                 'title': 't', 'source': 's', 'published_at': 'p',
                                 'objective': 'o'})
        self.assertEqual(event.event_id, 'x1')


class ParseAndQuoteTests(unittest.TestCase):
    def test_fenced_json_is_parsed(self):
        value = parse_json('```json\n{"a": 1}\n```')
        self.assertEqual(value, {'a': 1})

    def test_non_json_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_json('这里没有 JSON')

    def test_quotes_are_checked_against_body(self):
        checks = verify_quotes(payload_for(), BODY)
        self.assertEqual((checks['facts_supported'], checks['facts_total']), (2, 2))
        checks = verify_quotes(payload_for(quote1='原文没有这句话'), BODY)
        self.assertEqual((checks['facts_supported'], checks['facts_total']), (1, 2))
        self.assertFalse(checks['facts'][0]['supported'])

    def test_product_chars_excludes_usage_and_reasoning(self):
        chars = product_chars(payload_for())
        self.assertGreater(chars, 50)
        self.assertLess(chars, len(json.dumps(payload_for(), ensure_ascii=False)) + 1)


class PackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='business-pack-test-')
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ledger = Ledger(self.root / 'ledger.json')
        self.http = FakeHTTP()

    def client(self, http=None):
        return OpenCodeGoClient(api_key='test-key', ledger=self.ledger,
                                http_post=http or self.http)

    def events(self):
        return [make_event('event_one', 'https://example.com/a'),
                make_event('event_two', 'https://example.com/b')]

    def fetch(self):
        return fetcher_for({'https://example.com/a': BODY, 'https://example.com/b': BODY})

    def test_pack_writes_draft_final_checklist_and_ledger(self):
        summary = run_pack(self.events(), self.root / 'pack', client=self.client(),
                           fetcher=self.fetch(), max_tokens=12000)
        self.assertTrue(all(item['delivered'] for item in summary['events']))
        self.assertEqual([item['facts_supported'] for item in summary['events']], [2, 2])
        pack = self.root / 'pack'
        for event_id in ('event_one', 'event_two'):
            self.assertTrue((pack / event_id / 'evidence.json').is_file())
            self.assertTrue((pack / event_id / 'draft.json').is_file())
            self.assertTrue((pack / event_id / 'final.json').is_file())
            self.assertTrue((pack / event_id / 'intel-material.md').is_file())
        self.assertTrue((pack / 'README.md').is_file())
        self.assertTrue((pack / 'review-checklist.md').is_file())
        self.assertTrue((pack / 'summary.json').is_file())
        ledger = json.loads(self.ledger.path.read_text(encoding='utf-8'))
        self.assertEqual(len(ledger), 4)  # 2 draft + 2 final
        self.assertEqual(sum(1 for item in ledger if 'longcat' in item['model']), 2)
        self.assertTrue(all(item['status'] == 'ok' for item in ledger))
        markdown = (pack / 'event_one' / 'intel-material.md').read_text(encoding='utf-8')
        self.assertIn('2/2 条逐字支持', markdown)
        self.assertIn('不含 reasoning', markdown)
        self.assertNotIn('reasoning_content', markdown)

    def test_mismatched_quote_is_flagged_not_dropped(self):
        http = FakeHTTP(payload=payload_for(quote1='原文没有这句话'))
        summary = run_pack([make_event()], self.root / 'pack', client=self.client(http),
                           fetcher=self.fetch())
        self.assertEqual(summary['events'][0]['facts_supported'], 1)
        markdown = (self.root / 'pack' / 'event_one' / 'intel-material.md').read_text(encoding='utf-8')
        self.assertIn('⚠️ 未在原文逐字找到', markdown)

    def test_fetch_failure_is_preserved_and_next_event_continues(self):
        events = [make_event('bad', 'https://example.com/missing'), make_event('good', 'https://example.com/a')]
        summary = run_pack(events, self.root / 'pack', client=self.client(),
                           fetcher=self.fetch())
        self.assertFalse(summary['events'][0]['delivered'])
        self.assertIn('FetchError', summary['events'][0]['error'])
        self.assertTrue(summary['events'][1]['delivered'])
        self.assertTrue((self.root / 'pack' / 'bad' / 'error.json').is_file())

    def test_longcat_cap_blocks_final_but_keeps_draft(self):
        for index in range(100):
            self.ledger.reserve('longcat-2.5-free', f'prefill_{index}')
        summary = run_pack([make_event()], self.root / 'pack', client=self.client(),
                           fetcher=self.fetch())
        self.assertFalse(summary['events'][0]['delivered'])
        self.assertIn('ModelBudgetExceeded', summary['events'][0]['error'])
        self.assertTrue((self.root / 'pack' / 'event_one' / 'draft.json').is_file())
        self.assertFalse((self.root / 'pack' / 'event_one' / 'final.json').exists())

    def test_context_card_mode_is_optional_and_does_not_add_calls(self):
        summary = run_pack([make_event()], self.root / 'pack', client=self.client(),
                           fetcher=self.fetch(), use_context=True)
        self.assertTrue(summary['events'][0]['delivered'])
        self.assertTrue((self.root / 'pack' / 'event_one' / 'context-card.json').is_file())
        self.assertEqual(len(self.http.calls), 2)  # draft + final, no extra call
        prompts = [call['json']['messages'][-1]['content'] for call in self.http.calls]
        self.assertTrue(any('Context Card' in prompt for prompt in prompts))

    def test_no_final_writes_only_draft(self):
        summary = run_pack([make_event()], self.root / 'pack', client=self.client(),
                           fetcher=self.fetch(), final_model=None)
        self.assertTrue(summary['events'][0]['delivered'])
        self.assertTrue((self.root / 'pack' / 'event_one' / 'draft.md').is_file())
        self.assertFalse((self.root / 'pack' / 'event_one' / 'final.json').exists())
        ledger = json.loads(self.ledger.path.read_text(encoding='utf-8'))
        self.assertEqual(sum(1 for item in ledger if 'longcat' in item['model']), 0)

    def test_http_failure_is_recorded_and_counted(self):
        http = FakeHTTP(fail_models={'longcat-2.5-free'})
        summary = run_pack([make_event()], self.root / 'pack', client=self.client(http),
                           fetcher=self.fetch())
        self.assertFalse(summary['events'][0]['delivered'])
        self.assertIn('HTTP 500', summary['events'][0]['error'])
        self.assertTrue((self.root / 'pack' / 'event_one' / 'draft.json').is_file())
        ledger = json.loads(self.ledger.path.read_text(encoding='utf-8'))
        self.assertEqual(ledger[-1]['status'], 'failed')
        self.assertIn('longcat', ledger[-1]['model'])


class BadJsonClient:
    def __init__(self, base):
        self.base = base

    def complete(self, prompt, *, model, max_tokens, event_id, session):
        if 'longcat' in model:
            return {'content': '不是 JSON', 'usage': {'completion_tokens': 5},
                    'model': model, 'id': 'bad'}
        return self.base.complete(prompt, model=model, max_tokens=max_tokens,
                                  event_id=event_id, session=session)


class GroupAndFailureTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='business-pack-group-')
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ledger = Ledger(self.root / 'ledger.json')
        self.http = FakeHTTP()
        self.client = OpenCodeGoClient(api_key='test-key', ledger=self.ledger,
                                       http_post=self.http)
        self.fetch = fetcher_for({'https://example.com/a': BODY, 'https://example.com/b': BODY})

    def test_group_summary_is_written_for_same_subject(self):
        events = [make_event('event_one', 'https://example.com/a'),
                  make_event('event_two', 'https://example.com/b')]
        pack = self.root / 'pack'
        run_pack(events, pack, client=self.client, fetcher=self.fetch)
        payload = write_group_summary(pack, self.root / 'context.sqlite3')
        self.assertTrue((pack / 'groups.json').is_file())
        self.assertEqual(payload['ingest']['events'], 2)
        self.assertTrue(payload['groups'])
        self.assertEqual(len(payload['groups'][0]['group_key']), len('合成活动'))
        detail = next(iter(payload['details'].values()))
        self.assertEqual(len(detail['events']), 2)

    def test_invalid_final_json_keeps_raw_and_fails_cleanly(self):
        pack = self.root / 'pack'
        summary = run_pack([make_event()], pack, client=BadJsonClient(self.client),
                           fetcher=self.fetch)
        self.assertFalse(summary['events'][0]['delivered'])
        self.assertIn('JSON', summary['events'][0]['error'].upper())
        raw = pack / 'event_one' / 'final-raw.txt'
        self.assertTrue(raw.is_file())
        self.assertEqual(raw.read_text(encoding='utf-8'), '不是 JSON')


class ScriptedClient:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def complete(self, prompt, *, model, max_tokens, event_id, session):
        self.calls.append({'model': model, 'prompt': prompt})
        value = self.payloads.pop(0)
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return {'content': content, 'model': model, 'id': 'fake',
                'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}


class OrchestrateModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='business-pack-orch-')
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.fetch = fetcher_for({'https://example.com/a': BODY})
        self.events = [make_event()]

    def test_good_draft_is_accepted_without_final(self):
        client = ScriptedClient([payload_for()])
        summary = run_pack(self.events, self.root / 'pack', client=client,
                           fetcher=self.fetch, final_model=None, orchestrate=True,
                           min_facts=2)
        self.assertTrue(summary['events'][0]['delivered'])
        self.assertEqual(summary['events'][0]['status'], 'ready_for_final')
        self.assertEqual(summary['events'][0]['repair_count'], 0)
        self.assertTrue((self.root / 'pack' / 'event_one' / 'accepted.json').is_file())
        self.assertTrue((self.root / 'pack' / 'event_one' / 'orchestration-trace.json').is_file())
        self.assertFalse((self.root / 'pack' / 'event_one' / 'intel-material.md').exists() is False)

    def test_failed_gate_repairs_once_then_accepts(self):
        client = ScriptedClient([payload_for(quote1='原文没有这句话'), payload_for()])
        summary = run_pack(self.events, self.root / 'pack', client=client,
                           fetcher=self.fetch, final_model=None, orchestrate=True,
                           min_facts=2)
        self.assertTrue(summary['events'][0]['delivered'])
        self.assertEqual(summary['events'][0]['repair_count'], 1)
        self.assertEqual(len(client.calls), 2)

    def test_repair_failure_stays_needs_review(self):
        client = ScriptedClient([payload_for(quote1='错误一'), payload_for(quote1='错误二')])
        summary = run_pack(self.events, self.root / 'pack', client=client,
                           fetcher=self.fetch, final_model=None, orchestrate=True,
                           min_facts=2)
        self.assertFalse(summary['events'][0]['delivered'])
        self.assertEqual(summary['events'][0]['status'], 'needs_review')
        self.assertTrue((self.root / 'pack' / 'event_one' / 'needs-review.json').is_file())
        self.assertEqual(len(client.calls), 2)


class OrchestrateModelRoutingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='business-pack-route-')
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.fetch = fetcher_for({'https://example.com/a': BODY})

    def test_health_selects_first_allowed_healthy_model(self):
        from context.model_router import ModelHealth
        client = ScriptedClient([payload_for()])
        health = [ModelHealth('hy4-flash-free', 'failed', 'HTTP 503'),
                  ModelHealth('beam-free', 'ok', 'ok')]
        summary = run_pack([make_event()], self.root / 'pack', client=client,
                           fetcher=self.fetch, final_model=None, orchestrate=True,
                           min_facts=2, candidate_models=('hy4-flash-free', 'beam-free'),
                           model_health=health)
        self.assertTrue(summary['events'][0]['delivered'])
        self.assertEqual(client.calls[0]['model'], 'beam-free')
        trace = json.loads((self.root / 'pack' / 'event_one' /
                            'orchestration-trace.json').read_text(encoding='utf-8'))
        self.assertEqual(trace['model_route']['model'], 'beam-free')
        self.assertEqual(trace['steps'][0]['stage'], 'model_route')

    def test_skipped_training_model_is_not_called(self):
        from context.model_router import ModelHealth
        client = ScriptedClient([payload_for()])
        health = [ModelHealth('iceman-contributor-free', 'ok', 'ok', training=True),
                  ModelHealth('hy4-flash-free', 'ok', 'ok')]
        summary = run_pack([make_event()], self.root / 'pack', client=client,
                           fetcher=self.fetch, final_model=None, orchestrate=True,
                           min_facts=2,
                           candidate_models=('iceman-contributor-free', 'hy4-flash-free'),
                           model_health=health)
        self.assertTrue(summary['events'][0]['delivered'])
        self.assertEqual(client.calls[0]['model'], 'hy4-flash-free')


class ReplayModeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='business-pack-replay-')
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        event = make_event()
        replay = self.root / 'replay' / event.event_id
        replay.mkdir(parents=True)
        (replay / 'evidence.json').write_text(json.dumps(
            {'event': event.__dict__,
             'article': {'title': '缓存页', 'body': BODY, 'url': event.url,
                         'fetched_at': '2026-11-21T10:00:00+0800'}},
            ensure_ascii=False), encoding='utf-8')
        self.replay = self.root / 'replay'
        self.event = event

    def test_replay_skips_fetcher_and_delivers(self):
        def boom(*args, **kwargs):
            raise AssertionError('不应在回放模式抓取 URL')
        client = ScriptedClient([payload_for()])
        summary = run_pack([self.event], self.root / 'pack', client=client,
                           fetcher=boom, final_model=None, orchestrate=True,
                           min_facts=2, replay_dir=self.replay)
        self.assertTrue(summary['events'][0]['delivered'])
        self.assertTrue(summary['events'][0]['replayed'])
        self.assertTrue((self.root / 'pack' / 'event_one' / 'evidence.json').is_file())

    def test_missing_replay_evidence_is_preserved_as_error(self):
        client = ScriptedClient([])
        summary = run_pack([self.event], self.root / 'pack', client=client,
                           fetcher=lambda *a, **k: None, final_model=None,
                           orchestrate=True, min_facts=2,
                           replay_dir=self.root / 'empty')
        self.assertFalse(summary['events'][0]['delivered'])
        self.assertIn('回放证据不存在', summary['events'][0]['error'])


class OrchestrateContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='business-pack-orch-ctx-')
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.ledger = Ledger(self.root / 'ledger.json')
        self.http = FakeHTTP()
        self.client = OpenCodeGoClient(api_key='test-key', ledger=self.ledger,
                                       http_post=self.http)
        self.fetch = fetcher_for({'https://example.com/a': BODY})

    def test_orchestrate_with_context_builds_card_and_uses_it_for_final(self):
        summary = run_pack([make_event()], self.root / 'pack', client=self.client,
                           fetcher=self.fetch, final_model='longcat-2.5-free',
                           orchestrate=True, min_facts=2, use_context=True)
        self.assertTrue(summary['events'][0]['delivered'])
        self.assertTrue((self.root / 'pack' / 'event_one' / 'context-card.json').is_file())
        self.assertTrue((self.root / 'pack' / 'event_one' / 'final.json').is_file())
        self.assertEqual(len(self.http.calls), 2)  # draft + final, no extra call
        prompts = [call['json']['messages'][-1]['content'] for call in self.http.calls]
        self.assertIn('Context Card', prompts[-1])
        trace = json.loads((self.root / 'pack' / 'event_one' /
                            'orchestration-trace.json').read_text(encoding='utf-8'))
        self.assertEqual(trace['final_prompt_mode'], 'context_card')
        self.assertIn('context', [step['stage'] for step in trace['steps']])


class LedgerTests(unittest.TestCase):
    def test_counter_and_status_survive_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / 'ledger.json')
            call_id = ledger.reserve('beam-free', 'event')
            ledger.finalize(call_id, status='ok', usage={'completion_tokens': 10}, content_chars=5)
            reloaded = Ledger(Path(folder) / 'ledger.json')
            call_id2 = reloaded.reserve('longcat-2.5-free', 'event')
            reloaded.finalize(call_id2, status='ok')
            records = json.loads((Path(folder) / 'ledger.json').read_text(encoding='utf-8'))
            self.assertEqual([item['status'] for item in records], ['ok', 'ok'])
            self.assertEqual(records[1]['model'], 'longcat-2.5-free')


if __name__ == '__main__':
    unittest.main()
