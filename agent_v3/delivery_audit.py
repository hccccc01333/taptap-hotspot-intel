"""Read-only delivery funnel; calling a model is never a business success."""
import json


def funnel(store,run_id,topic_id=None,*,error_type=None):
    steps=store.get_run(run_id)['steps']
    by_kind={}
    resources={}
    for step in steps:
        by_kind.setdefault(step['kind'],[]).append(step['payload'])
        if step['kind'] in ('model_task','research_plan_model','opencode_task'):
            p=step['payload'];stage=p.get('stage') or ('research_plan' if step['kind']=='research_plan_model' else 'legacy')
            row=resources.setdefault(stage,{'requests':0,'input_tokens':0,'output_tokens':0,'usage_unknown':0,
                'seconds':0,'schema_failures':0,'validation_failures':0,'request_failures':0,'reported_cost':0,'cost_unknown':0})
            row['requests']+=1;row['seconds']+=p.get('seconds') or 0
            usage=p.get('usage') or {}
            if all(type(usage.get(k)) is int for k in ('prompt_tokens','completion_tokens')):
                row['input_tokens']+=usage['prompt_tokens'];row['output_tokens']+=usage['completion_tokens']
            else:row['usage_unknown']+=1
            row['schema_failures']+=bool(p.get('schema_errors'))
            row['request_failures']+=p.get('delivery_status')=='request_failed'
            if type(p.get('reported_cost')) in (int,float):row['reported_cost']+=p['reported_cost']
            else:row['cost_unknown']+=1
        if step['kind']=='validation_error':
            stage=step['payload'].get('name','unknown')
            resources.setdefault(stage,{'requests':0,'input_tokens':0,'output_tokens':0,'usage_unknown':0,
                'seconds':0,'schema_failures':0,'validation_failures':0,'request_failures':0,'reported_cost':0,'cost_unknown':0})['validation_failures']+=1
    def rows(table):
        sql='SELECT * FROM '+table+' WHERE run_id=?';args=[run_id]
        if topic_id:sql+=' AND topic_id=?';args.append(topic_id)
        return [dict(r) for r in store.conn.execute(sql,args)]
    interpretations=rows('topic_interpretation');intel=rows('topic_intelligence');signals=rows('game_signal')
    decisions=[d for s in by_kind.get('main_agent_plan',[]) for d in s.get('decisions',[]) if not topic_id or d.get('topic_id')==topic_id]
    business=by_kind.get('main_agent_delivery',[])
    graph=[json.loads(r['payload']).get('graph_extraction',{}).get('status','not_attempted') for r in interpretations]
    research='evidence_sufficient' if any(json.loads(r['payload']).get('status')=='ready' for r in interpretations) else 'evidence_insufficient' if interpretations else 'not_completed'
    if not interpretations and by_kind.get('research_child_reused'):research='interpretation_reused'
    tools=[{k:r[k] for k in ('call_id','tool','status','charged_units','seconds','monetary_cost','cost_status')}
        for r in store.conn.execute('SELECT * FROM tool_execution WHERE run_id=? ORDER BY started_at',(run_id,))]
    failed_tools=sum(t['status'] in ('failed','stale','interrupted') for t in tools)
    persisted_materials=sum(s.get('materials',0) for s in business)
    creative_count=store.conn.execute('SELECT COUNT(*) FROM creative WHERE run_id=?',(run_id,)).fetchone()[0]
    reviews=[json.loads(r['payload']).get('evidence_review') for r in signals]
    reviews+= [json.loads(r['evidence_review']) if r['evidence_review'] else None
               for r in store.conn.execute('SELECT evidence_review FROM material WHERE run_id=?',(run_id,))]
    evidence_review={status:sum(1 for r in reviews if (r or {}).get('status')==status)
                     for status in ('supported','draft','insufficient')}
    return {'version':'delivery-funnel-v3.23','scope':'this_run_only','screening':[
        {'topic_id':d['topic_id'],'decision':d.get('action'),'route':d.get('route'),'reason':d.get('reason')} for d in decisions],
        'research':{'status':research,'tool_failures':failed_tools},
        'interpretation':{'ready':sum(json.loads(r['payload']).get('status')=='ready' for r in interpretations),
            'schema_failures':resources.get('interpretation',{}).get('schema_failures',0),
            'time_evidence_insufficient':len(by_kind.get('temporal_validation',[])),
            'reused':len(by_kind.get('research_child_reused',[]))},
        'graph':{'statuses':graph,'nonblocking':True},
        'business':{'decisions':[{'signals':r.get('signals',0),'materials':r.get('materials',0),'opportunity':r.get('decision')} for r in business],
            'assessments':[json.loads(r['payload']).get('output_assessments',{}) for r in intel]},
        'persistence':{'business_records':len(intel),'game_intelligence':len(signals),
            'materials':persisted_materials,'creatives':creative_count,
            'evidence_review':evidence_review,
            'validation_failed':sum(v['validation_failures'] for v in resources.values()),
            'stale_rejected':len(by_kind.get('stale_delivery_rejected',[])),
            'error_type':error_type},
        'quality':{'verdict':'unverified','note':'Schema、逐字引用及入库成功不代表事实支持；证据核对为确定性词面校验，不替代对照原网页的语义核查。'},
        'resources':{'stages':resources,'tools':tools,'blocked_before_request':by_kind.get('model_request_blocked',[]),
            'input_packets':by_kind.get('business_input',[]),
            'note':'失败调用也计入；未报告价格保持未知，Go订阅成本不由token推算。'}}
