"""Numeric repair, compact evidence parity and independent risk-intelligence delivery."""
import copy
import json
import unittest

from agent_v3.contracts import array_contract,schema_feedback,safe_schema_errors
from agent_v3.main_agent import business_schema
from agent_v3.task_packets import business_packet,topic_packet
from agent_v3.discovery import read_topic
from agent_v3.pipeline import process
from agent_v3.tests import test_main_agent as fixtures


class Delivery23Tests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.MainAgentTests();self.f.setUp();self.s=self.f.store
    def tearDown(self):self.f.tearDown()

    def packet(self):return topic_packet(read_topic(self.s,self.f.tid),self.s.context())

    def test_limits_are_exact_and_guidance_uses_risk_overrides(self):
        schema=business_schema(self.packet());p=schema['properties']
        self.assertEqual(p['game_signals']['maxItems'],3)
        self.assertEqual(p['game_signals']['items']['properties']['basis_refs']['maxItems'],3)
        self.assertEqual(p['fact_refs']['maxItems'],4)
        p['patterns']['maxItems']=0
        guide=array_contract(schema)
        for text in ('game_signals: 0..3','game_signals/*/basis_refs: 1..3','fact_refs: 1..4','patterns: 0..0','不可重复','重新选择'):self.assertIn(text,guide)

    def test_numeric_feedback_never_echoes_values_or_untrusted_property_paths(self):
        schema=business_schema(self.packet())
        value={'game_signals':[{'basis_refs':[0,1,2,3,4]}], 'fact_refs':[0,1,2,3,4], 'SECRET_VALUE':{'SECRET_KEY':'PRIVATE'}}
        errors=schema_feedback(schema,value)
        # Enough required-field errors may precede nested failures; check isolated schema.
        small={'type':'object','properties':{'items':{'type':'array','maxItems':3,'uniqueItems':True,'items':{'type':'integer'}}}}
        rows=schema_feedback(small,{'items':[0,0,1,2]})
        self.assertIn({'path':'items','constraint':'maxItems','actual_count':4,'allowed_max':3},rows)
        self.assertIn({'path':'items','constraint':'uniqueItems','actual_count':4,'distinct_count':3},rows)
        self.assertNotIn('SECRET',json.dumps(errors))
        self.assertNotIn('PRIVATE',json.dumps(safe_schema_errors([{'path':'items','constraint':'maxItems','actual_count':'PRIVATE','allowed_max':True,'instance':'PRIVATE'}])))

    def test_retry_reselects_refs_and_preserves_no_partial_write(self):
        model=self.f.model();original=model.run_task;inputs=[];first_refs=None
        def run(stage,packet,schema,system,**kwargs):
            nonlocal first_refs
            response=original(stage,packet,schema,system,**kwargs)
            if stage=='intelligence':
                inputs.append(copy.deepcopy(packet))
                self.assertIn('game_signals/*/basis_refs: 1..3',system)
                if len(inputs)==1:
                    first_refs=list(range(5));response['result']['game_signals'][0]['basis_refs']=first_refs
                else:
                    self.assertFalse(self.s.game_signals())
                    self.assertIsNone(self.s.intelligence(self.f.tid))
                    error=packet['validation_errors'][0]
                    for text in ('game_signals/0/basis_refs','maxItems','实际数量=5','允许上限=3','重新选择'):self.assertIn(text,error)
            return response
        model.run_task=run
        self.assertEqual(process(self.s,self.f.rid,model=model,topic_id=self.f.tid)['status'],'intelligence_ready')
        self.assertEqual(first_refs,[0,1,2,3,4]);self.assertEqual(len(inputs),2)
        self.assertEqual(len(self.s.game_signals()[0]['payload']['facts']),1)

    def test_repeated_overflow_cannot_be_silently_truncated_or_saved(self):
        model=self.f.model();original=model.run_task
        def run(stage,*args,**kwargs):
            response=original(stage,*args,**kwargs)
            if stage=='intelligence':response['result']['game_signals'][0]['basis_refs']=list(range(5))
            return response
        model.run_task=run
        self.assertEqual(process(self.s,self.f.rid,model=model,topic_id=self.f.tid)['status'],'ai_deferred')
        self.assertEqual(model.calls.count('intelligence'),2)
        self.assertFalse(self.s.game_signals());self.assertFalse(self.s.usable_materials())
        audit=self.s.get_run(self.f.rid)['steps'][-1]['payload']
        self.assertEqual(audit['persistence']['game_intelligence'],0)
        self.assertEqual(audit['resources']['stages']['intelligence']['schema_failures'],2)
        self.assertEqual(audit['quality']['verdict'],'unverified')

    def test_compact_packet_preserves_all_quotes_and_adds_verified_fact_excerpt(self):
        full=self.packet();source=full['evidence'][0];quote=source['body'][-32:]
        full['research_return']={'status':'ready','headline':'已保存解读','one_line':'已经研究',
            'core':[{'text':'已核验事实','facts':[{'evidence_id':source['evidence_id'],'quote':quote,'source_version':'large-internal-snapshot'}]}],
            'source_basis':{'private_storage':'not model input'},'knowledge':{'relations':['not mandatory']},'unknowns':[]}
        full['graph_context']={'sources':[source],'events':[full['research_return']]}
        saved=copy.deepcopy(full);compact=business_packet(full)
        self.assertEqual(full,saved)
        self.assertEqual(compact['quote_candidates'][:len(full['quote_candidates'])],full['quote_candidates'])
        ref=compact['research_return']['core'][0]['basis_refs'][0]
        self.assertEqual(compact['quote_candidates'][ref]['quote'],quote)
        self.assertIn(quote,compact['evidence'][0]['body'])
        self.assertNotIn('source_basis',compact['research_return']);self.assertNotIn('knowledge',compact['research_return'])
        self.assertNotIn('graph_context',compact)
        self.assertLess(len(json.dumps(compact)),len(json.dumps(full)))

    def test_compaction_rejects_ungrounded_summary_instead_of_dropping_fact(self):
        full=self.packet();full['research_return']={'core':[{'text':'bad','facts':[{'evidence_id':full['evidence'][0]['evidence_id'],'quote':'never in source'}]}]}
        with self.assertRaises(ValueError):business_packet(full)

    def test_semantically_rejected_research_plan_still_counts_completed_usage(self):
        from agent_v3.research import plan
        from agent_v3.delivery_audit import funnel
        model=self.f.model();original=model.run_task
        def run(stage,*args,**kwargs):
            response=original(stage,*args,**kwargs)
            if stage=='research_plan':
                response['result']['actions']=[{'tool':'sample_discussion','source_ref':0,'query':''}]
                response['usage']={'prompt_tokens':17,'completion_tokens':5,'total_tokens':22}
            return response
        model.run_task=run
        with self.assertRaises(ValueError):plan(self.s,read_topic(self.s,self.f.tid),model,run_id=self.f.rid)
        stage=funnel(self.s,self.f.rid)['resources']['stages']['research_plan']
        self.assertEqual(stage['requests'],1);self.assertEqual(stage['input_tokens'],17)

    def test_connection_failure_is_counted_as_unknown_usage_not_free_success(self):
        from agent_v3.main_agent import task
        from agent_v3.delivery_audit import funnel
        model=self.f.model()
        def fail(*args,**kwargs):raise TimeoutError('PRIVATE_SOURCE_OR_PROVIDER_BODY')
        model.run_task=fail
        with self.assertRaises(TimeoutError):task(self.s,self.f.rid,model,'intelligence',{}, {'type':'object'},'fixture')
        audit=funnel(self.s,self.f.rid)
        stage=audit['resources']['stages']['intelligence']
        self.assertEqual(stage['requests'],1);self.assertEqual(stage['request_failures'],1)
        self.assertEqual(stage['usage_unknown'],1)
        self.assertNotIn('PRIVATE',json.dumps(audit))

    def test_stopped_experiment_rejection_is_not_a_transport_call(self):
        from agent_v3.benchmark import RecordedModel,BudgetExceeded
        from agent_v3.main_agent import task
        from agent_v3.delivery_audit import funnel
        stopped=object.__new__(RecordedModel);stopped.stopped=True
        model=self.f.model();model.run_task=stopped.run_task
        with self.assertRaises(BudgetExceeded):
            task(self.s,self.f.rid,model,'intelligence',{}, {'type':'object'},'fixture')
        audit=funnel(self.s,self.f.rid)
        self.assertNotIn('intelligence',audit['resources']['stages'])
        self.assertEqual(len(audit['resources']['blocked_before_request']),1)
        self.assertFalse(audit['resources']['blocked_before_request'][0]['request_sent'])

    def test_negative_event_saves_neutral_risk_intelligence_without_promotional_outputs(self):
        model=self.f.model();model.patterns=False;original=model.run_task
        def run(stage,packet,schema,system,**kwargs):
            response=original(stage,packet,schema,system,**kwargs)
            if stage in ('interpretation','intelligence'):
                response['result']['risk_assessment'].update(polarity='negative',level='medium',reason='游戏服务终止，保留中立风险监测')
            if stage=='intelligence':
                response['result']['game_signals'][0]['category']='risk_monitoring'
                self.assertEqual(schema['properties']['patterns']['maxItems'],0)
                response['result']['opportunity'].update({k:'' for k in ('audience_need','taptap_bridge','growth_goal','hypothesis','validation_plan')})
                response['result']['opportunity']['prerequisites']=[]
            return response
        model.run_task=run
        self.assertEqual(process(self.s,self.f.rid,model=model,topic_id=self.f.tid)['status'],'intelligence_ready')
        self.assertEqual(len(self.s.game_signals()),1)
        self.assertEqual(self.s.game_signals()[0]['payload']['category'],'risk_monitoring')
        self.assertFalse(self.s.usable_materials());self.assertEqual(self.s.overview()['counts']['creative'],0)
        audit=self.s.get_run(self.f.rid)['steps'][-1]['payload']
        self.assertEqual(audit['persistence']['game_intelligence'],1)
        self.assertEqual(audit['persistence']['materials'],0)
        self.assertEqual(audit['persistence']['creatives'],0)


if __name__=='__main__':unittest.main()
