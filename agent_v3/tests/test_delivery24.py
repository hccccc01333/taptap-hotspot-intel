import json
from pathlib import Path
import shutil
import subprocess
import time
import unittest
from unittest.mock import patch,MagicMock

import requests
from intelligence.llm import LLMUnavailable
from agent_v3.provider_adapter import ProfileModel,safe_failure_category
from agent_v3.tests.test_providers18 import profile
from agent_v3.store import Store
from agent_v3.model import request_failure
from agent_v3.delivery_audit import funnel


class Delivery24Tests(unittest.TestCase):
    def test_browser_reader_preserves_visible_text_boundaries_and_budgets(self):
        node=shutil.which('node')
        if not node:self.skipTest('Node runtime unavailable')
        test=Path(__file__).with_name('page_reader.test.cjs')
        result=subprocess.run([node,str(test)],capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_transport_failure_categories_keep_usage_unknown_and_never_echo_exception(self):
        for exception,category in [(requests.ConnectTimeout,'connect_timeout'),
                (requests.ReadTimeout,'read_timeout'),(requests.ConnectionError,'connection_error'),
                (requests.RequestException,'request_exception')]:
            with self.subTest(category=category),patch('requests.post',side_effect=exception('PRIVATE_KEY_AND_RESPONSE')):
                client=ProfileModel(profile('ollama'))
                with self.assertRaises(LLMUnavailable) as caught:
                    client.run_task('intelligence',{}, {'type':'object'},'fixture')
                self.assertEqual(safe_failure_category(caught.exception),category)
                self.assertNotIn('PRIVATE',str(caught.exception))
                store=Store(':memory:')
                try:
                    run=store.create_run('fixture');request_failure(store,run,'intelligence',caught.exception,time.monotonic())
                    metadata=store.get_run(run)['steps'][-1]['payload']
                    self.assertEqual(metadata['error_category'],category)
                    self.assertNotIn('PRIVATE',json.dumps(metadata))
                    self.assertEqual(funnel(store,run)['resources']['stages']['intelligence']['usage_unknown'],1)
                finally:store.close()

    def test_invalid_transport_json_has_safe_category_without_response_body(self):
        reply=MagicMock();reply.__enter__.return_value=reply;reply.status_code=200
        reply.headers={'Content-Type':'application/json'};reply.iter_content.return_value=[b'PRIVATE_INVALID_JSON']
        with patch('requests.post',return_value=reply):
            with self.assertRaises(LLMUnavailable) as caught:
                ProfileModel(profile('ollama')).run_task('intelligence',{}, {'type':'object'},'fixture')
        self.assertEqual(safe_failure_category(caught.exception),'response_json_invalid')
        self.assertNotIn('PRIVATE',str(caught.exception))

    def test_untrusted_exception_category_is_not_logged(self):
        for value in ['PRIVATE_SECRET',['connect_timeout']]:
            error=RuntimeError();error.failure_category=value
            self.assertEqual(safe_failure_category(error),'request_or_delivery_failure')

    def test_timeline_requires_selected_quote_not_unrelated_text_elsewhere(self):
        from agent_v3.research_child import validate_timeline
        from agent_v3.main_agent import SemanticDeliveryError
        packet={'evidence':[{'evidence_id':'e','published_at':'2026-10-10T00:00:00+00:00'}],
            'quote_candidates':[{'evidence_id':'e','quote':'预告公开，12月推出角色。'},
                                {'evidence_id':'e','quote':'作者YT17 9 小时前'}]}
        validate_timeline({'timeline':[{'time_text':'12月','time_kind':'event','basis_refs':[0]}]},packet)
        for raw,refs in [('12月',[1]),('179 小时前',[1])]:
            with self.assertRaises(SemanticDeliveryError) as caught:
                validate_timeline({'timeline':[{'time_text':raw,'time_kind':'event','basis_refs':refs}]},packet)
            self.assertIn('timeline/0/time_text',str(caught.exception))
            self.assertNotIn('YT17',str(caught.exception))

    def test_publication_metadata_only_supports_report_time(self):
        from agent_v3.research_child import validate_timeline
        from agent_v3.main_agent import SemanticDeliveryError
        packet={'evidence':[{'evidence_id':'e','published_at':'2026-10-10T00:00:00+00:00'}],
                'quote_candidates':[{'evidence_id':'e','quote':'公告发布，角色计划12月推出。'}]}
        item={'time_text':'2026-10-10','time_kind':'report','basis_refs':[0]}
        validate_timeline({'timeline':[item]},packet)
        with self.assertRaises(SemanticDeliveryError):
            validate_timeline({'timeline':[{**item,'time_kind':'event'}]},packet)

    def test_unverified_timeline_is_rejected_before_persistence_and_can_be_reselected(self):
        from agent_v3.tests.test_main_agent import MainAgentTests
        from agent_v3.pipeline import process
        from agent_v3.discovery import read_topic
        fixture=MainAgentTests();fixture.setUp()
        try:
            fingerprint=read_topic(fixture.store,fixture.tid)['fingerprint']
            client=fixture.model();original=client.run_task;attempts=[]
            def run(stage,packet,*args,**kwargs):
                response=original(stage,packet,*args,**kwargs)
                if stage=='interpretation':
                    attempts.append(packet)
                    self.assertIsNone(fixture.store.interpretation(fixture.tid,fingerprint))
                    if len(attempts)==1:
                        response['result']['timeline'][0].update(time_text='179 小时前',time_kind='report')
                    else:
                        self.assertIn('time_basis_missing',packet['validation_errors'][0])
                return response
            client.run_task=run
            self.assertEqual(process(fixture.store,fixture.rid,model=client,topic_id=fixture.tid)['status'],'intelligence_ready')
            self.assertEqual(len(attempts),2)
            saved=fixture.store.interpretation(fixture.tid,fingerprint)['payload']
            self.assertEqual(saved['timeline'][0]['time_text'],'')
        finally:fixture.tearDown()

    def test_repeated_unsupported_timeline_is_dropped_and_never_saved(self):
        from agent_v3.tests.test_main_agent import MainAgentTests
        from agent_v3.pipeline import process
        from agent_v3.discovery import read_topic
        fixture=MainAgentTests();fixture.setUp()
        try:
            client=fixture.model();original=client.run_task
            def run(stage,*args,**kwargs):
                response=original(stage,*args,**kwargs)
                if stage=='interpretation':
                    response['result']['timeline'][0].update(time_text='179 小时前',time_kind='report')
                return response
            client.run_task=run
            result=process(fixture.store,fixture.rid,model=client,topic_id=fixture.tid)
            self.assertIn(result['status'],('completed','intelligence_ready'))
            self.assertEqual(client.calls.count('interpretation'),2)
            saved=fixture.store.interpretation(fixture.tid,read_topic(fixture.store,fixture.tid)['fingerprint'])
            import json as _json
            self.assertNotIn('179 小时前',_json.dumps(saved['payload'],ensure_ascii=False))
            self.assertFalse(any('179 小时前' in _json.dumps(s['payload'],ensure_ascii=False) for s in fixture.store.game_signals()))
            audit=fixture.store.get_run(fixture.rid)['steps'][-1]['payload']
            self.assertEqual(audit['resources']['stages']['interpretation']['validation_failures'],1)
            self.assertEqual(audit['persistence']['game_intelligence'],1)
        finally:fixture.tearDown()


if __name__=='__main__':unittest.main()
